"""Tests for the optional read-only GBrain adapter (scripts/gbrain_history.py)
and its wiring into scripts/build_map.py.

Nothing here touches a real brain. Pure folding logic is tested directly, the
transport is tested against tests/fixtures/fake_gbrain.py — a scripted MCP
stdio server over invented pages — so timestamp precedence, missing history,
malformed payloads and backend failure are all deterministic.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fixtures" / "fake_gbrain.py"


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gb = _load("gbrain_history", "scripts/gbrain_history.py")
build_map = _load("build_map", "scripts/build_map.py")

AS_OF = "2026-06-15"

# ── shared fixtures ──────────────────────────────────────────────────────────
SECRET_BODY = "SECRET-ALPHA-BODY-v1"
SECRET_KEY = "do-not-export"

VAULT = {
    # matched, revised twice, last touch is a real content write
    "notes/alpha.md": "---\ncreated: 2026-01-01\nlast_updated: 2026-02-01\n---\n# Alpha\n[[Beta]]\n",
    # matched, updated_at moved after the last real revision (re-embed/rename)
    "notes/beta.md": "---\ncreated: 2026-01-02\n---\n# Beta\n",
    # matched, never revised
    "notes/gamma.md": "---\ncreated: 2026-01-03\nupdated: 2026-03-01\n---\n# Gamma\n",
    # not in the brain at all — must keep its Markdown timestamps
    "notes/orphan-file.md": "---\ncreated: 2026-01-04\nupdated: 2026-02-14\n---\n# Orphan File\n",
    # matched only through the frontmatter slug the exporter stamps
    "legacy.md": "---\ncreated: 2026-01-05\nslug: Legacy/Odd Slug\n---\n# Legacy\n",
    # creations in the months where revisions also happen, so the timeline can
    # show the two series diverging
    "notes/delta.md": "---\ncreated: 2026-04-10\n---\n# Delta\n",
    "notes/epsilon.md": "---\ncreated: 2026-06-02\n---\n# Epsilon\n",
}

BRAIN = {
    "pages": [
        {"slug": "notes/alpha", "source_id": "brain", "type": "note", "title": "Alpha",
         "updated_at": "2026-06-10T12:00:00.000Z"},
        {"slug": "notes/beta", "source_id": "brain", "type": "note", "title": "Beta",
         "updated_at": "2026-06-12T09:00:00.000Z"},
        {"slug": "notes/gamma", "source_id": "brain", "type": "note", "title": "Gamma",
         "updated_at": "2026-06-14T00:00:00.000Z"},
        {"slug": "Legacy/Odd Slug", "source_id": "brain", "type": "note", "title": "Legacy",
         "updated_at": "2026-05-01T00:00:00.000Z"},
        {"slug": "notes/delta", "source_id": "brain", "type": "note", "title": "Delta",
         "updated_at": "2026-04-20T00:00:00.000Z"},
        {"slug": "notes/epsilon", "source_id": "brain", "type": "note", "title": "Epsilon",
         "updated_at": "2026-06-03T00:00:00.000Z"},
    ],
    "versions": {
        "notes/alpha": [
            {"id": 2, "page_id": 1, "compiled_truth": SECRET_BODY,
             "frontmatter": {"note": SECRET_KEY}, "snapshot_at": "2026-06-10T11:59:59.600Z"},
            {"id": 1, "page_id": 1, "compiled_truth": SECRET_BODY,
             "frontmatter": {}, "snapshot_at": "2026-03-05T08:00:00.000Z"},
        ],
        "notes/beta": [
            {"id": 3, "page_id": 2, "compiled_truth": SECRET_BODY,
             "frontmatter": {}, "snapshot_at": "2026-04-02T00:00:00.000Z"},
        ],
    },
}


def brain_with(versions):
    """BRAIN with some pages' version rows replaced, and nothing else moved."""
    variant = json.loads(json.dumps(BRAIN))
    variant["versions"].update(versions)
    return variant


def write_vault(folder, files=None):
    for rel, text in (files or VAULT).items():
        target = Path(folder, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return str(folder)


def fake_gbrain(folder, brain=None, mode="ok", cap=100, log=None, cursor="inclusive"):
    """An executable that behaves like `gbrain` for `<bin> serve ...`."""
    brain_path = Path(folder, "brain.json")
    brain_path.write_text(json.dumps(brain if brain is not None else BRAIN), encoding="utf-8")
    binary = Path(folder, "gbrain-fake")
    parts = [sys.executable, str(FAKE), "--brain", str(brain_path),
             "--mode", mode, "--cap", str(cap), "--cursor", cursor]
    if log:
        parts += ["--log", str(log)]
    binary.write_text("#!/bin/sh\nexec %s \"$@\"\n" % " ".join('"%s"' % part for part in parts),
                      encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(binary)


def options(command, **overrides):
    base = {"command": command, "source": None, "history": True,
            "historyLimit": 0, "slugPrefix": "", "timeout": 20.0, "required": False}
    base.update(overrides)
    return base


class FakeSession:
    """An in-process stand-in for McpStdioSession with scripted replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.server_info = {"version": "fake"}
        self.closed = False

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        if not self.replies:
            raise AssertionError("unexpected extra call: %s" % tool)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


# ── timestamps ───────────────────────────────────────────────────────────────
class StampTests(unittest.TestCase):
    def test_backend_millisecond_zulu_stamps_normalize_to_utc_seconds(self):
        self.assertEqual(gb.normalize_stamp("2026-09-07T01:32:33.566Z"),
                         "2026-09-07T01:32:33+00:00")

    def test_offsets_are_converted_and_naive_stamps_are_assumed_utc(self):
        self.assertEqual(gb.normalize_stamp("2026-09-07T03:32:33+02:00"),
                         "2026-09-07T01:32:33+00:00")
        self.assertEqual(gb.normalize_stamp("2026-09-07T01:32:33"),
                         "2026-09-07T01:32:33+00:00")
        self.assertEqual(gb.normalize_stamp("2026-09-07"), "2026-09-07T00:00:00+00:00")

    def test_junk_and_non_strings_are_refused_rather_than_guessed(self):
        for value in ("", "  ", "yesterday", None, 1757200000, {"at": "now"}, ["x"]):
            with self.subTest(value=value):
                self.assertEqual(gb.normalize_stamp(value), "")
                self.assertIsNone(gb.parse_stamp(value))


# ── slug matching ────────────────────────────────────────────────────────────
class SlugTests(unittest.TestCase):
    def test_normalize_slug_matches_the_exporter_spelling(self):
        self.assertEqual(gb.normalize_slug("notes\\alpha.md"), "notes/alpha")
        self.assertEqual(gb.normalize_slug("./notes/alpha.MD"), "notes/alpha")
        self.assertEqual(gb.normalize_slug("/notes/alpha/"), "notes/alpha")
        self.assertEqual(gb.normalize_slug(None), "")

    def test_frontmatter_slug_outranks_the_path(self):
        self.assertEqual(gb.slug_candidates("legacy.md", "Legacy/Odd Slug"),
                         ["Legacy/Odd Slug", "legacy"])

    def test_prefix_variants_come_after_the_bare_candidates(self):
        self.assertEqual(gb.slug_candidates("alpha.md", "", "wiki"),
                         ["alpha", "wiki/alpha"])

    def test_duplicate_and_empty_candidates_collapse(self):
        self.assertEqual(gb.slug_candidates("alpha.md", "alpha"), ["alpha"])
        self.assertEqual(gb.slug_candidates("", ""), [])


class MatchSlugsTests(unittest.TestCase):
    """Note -> brain slug resolution. The result is used to look pages up and
    is then dropped; nothing about it is serialized, so this is where it has
    to be checked."""

    def match(self, nodes, slugs, prefix=""):
        index = {slug: {"slug": slug} for slug in slugs}
        return build_map.match_slugs(nodes, index, gb, prefix)

    def test_the_stamped_frontmatter_slug_outranks_the_path(self):
        nodes = {"Legacy": {"path": "legacy.md", "slugHint": "Legacy/Odd Slug"}}
        self.assertEqual(self.match(nodes, ["Legacy/Odd Slug", "legacy"]),
                         {"Legacy": "Legacy/Odd Slug"})

    def test_the_path_is_the_slug_when_nothing_was_stamped(self):
        nodes = {"Alpha": {"path": "notes/alpha.md", "slugHint": ""}}
        self.assertEqual(self.match(nodes, ["notes/alpha"]), {"Alpha": "notes/alpha"})

    def test_a_prefix_matches_a_subdirectory_of_an_export(self):
        nodes = {"Alpha": {"path": "notes/alpha.md", "slugHint": ""}}
        self.assertEqual(self.match(nodes, ["wiki/notes/alpha"], "wiki"),
                         {"Alpha": "wiki/notes/alpha"})

    def test_notes_the_index_does_not_hold_stay_unmatched(self):
        nodes = {"Alpha": {"path": "notes/alpha.md", "slugHint": ""}}
        self.assertEqual(self.match(nodes, ["notes/other"]), {})

    def test_two_notes_may_legitimately_resolve_to_one_page(self):
        nodes = {"Dup": {"path": "dup.md", "slugHint": ""},
                 "Other": {"path": "other.md", "slugHint": "dup"}}
        self.assertEqual(self.match(nodes, ["dup"]), {"Dup": "dup", "Other": "dup"})


# ── page index ───────────────────────────────────────────────────────────────
class IndexPagesTests(unittest.TestCase):
    def test_good_rows_are_indexed_by_normalized_slug(self):
        index = gb.index_pages([{"slug": "a/b.md", "source_id": "s", "title": "T",
                                 "type": "note", "updated_at": "2026-06-01T00:00:00.000Z"}])
        self.assertEqual(set(index["pages"]), {"a/b"})
        self.assertEqual(index["pages"]["a/b"]["updated"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(index["malformed"], 0)

    def test_malformed_rows_are_counted_and_never_indexed(self):
        index = gb.index_pages([
            "not-a-row",
            {"slug": "", "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "a", "updated_at": "whenever"},
            {"slug": "a", "updated_at": None},
            {"slug": "good", "updated_at": "2026-06-01T00:00:00Z"},
        ])
        self.assertEqual(set(index["pages"]), {"good"})
        self.assertEqual(index["malformed"], 4)

    def test_same_slug_in_two_sources_with_different_stamps_is_dropped(self):
        rows = [{"slug": "dup", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
                {"slug": "dup", "source_id": "b", "updated_at": "2026-06-02T00:00:00Z"},
                {"slug": "solo", "source_id": "a", "updated_at": "2026-06-03T00:00:00Z"}]
        index = gb.index_pages(rows)
        self.assertEqual(index["ambiguous"], {"dup"})
        self.assertEqual(set(index["pages"]), {"solo"})

    def test_two_sources_are_ambiguous_even_when_the_stamps_are_equal(self):
        """Regression: equal `updated_at` does not make two pages one page.

        They are still two rows with two histories, and `get_versions` takes a
        bare slug — so picking either one is a guess, not a fact."""
        rows = [{"slug": "dup", "source_id": "wiki", "updated_at": "2026-06-01T00:00:00Z"},
                {"slug": "dup", "source_id": "brain", "updated_at": "2026-06-01T00:00:00Z"}]
        for order in (rows, list(reversed(rows))):
            with self.subTest(order=[row["source_id"] for row in order]):
                index = gb.index_pages(order)
                self.assertEqual(index["ambiguous"], {"dup"})
                self.assertEqual(index["pages"], {})

    def test_a_missing_source_id_is_a_source_of_its_own_not_a_wildcard(self):
        rows = [{"slug": "dup", "updated_at": "2026-06-01T00:00:00Z"},
                {"slug": "dup", "source_id": "brain", "updated_at": "2026-06-01T00:00:00Z"}]
        self.assertEqual(gb.index_pages(rows)["ambiguous"], {"dup"})

    def test_the_same_row_repeated_by_the_cursor_quirk_is_not_ambiguity(self):
        row = {"slug": "dup", "source_id": "brain", "title": "Dup",
               "updated_at": "2026-06-01T00:00:00Z"}
        index = gb.index_pages([row, dict(row)])
        self.assertEqual(index["ambiguous"], set())
        self.assertEqual(index["pages"]["dup"]["updated"], "2026-06-01T00:00:00+00:00")

    def test_the_federated_token_is_a_scope_over_every_source_not_one_of_them(self):
        rows = [{"slug": "dup", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
                {"slug": "dup", "source_id": "b", "updated_at": "2026-06-01T00:00:00Z"}]
        index = gb.index_pages(rows, source_id=gb.FEDERATED_SOURCE)
        self.assertEqual(index["ambiguous"], {"dup"})
        self.assertEqual(index["foreign"], 0)

    def test_a_concrete_scope_keeps_only_rows_that_prove_they_match_it(self):
        rows = [{"slug": "dup", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
                {"slug": "dup", "source_id": "b", "updated_at": "2026-06-02T00:00:00Z"},
                {"slug": "nameless", "updated_at": "2026-06-03T00:00:00Z"}]
        index = gb.index_pages(rows, source_id="a")
        self.assertEqual(set(index["pages"]), {"dup"})
        self.assertEqual(index["pages"]["dup"]["updated"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(index["ambiguous"], set())
        self.assertEqual(index["foreign"], 2)      # source b, and the untagged row


# ── paging ───────────────────────────────────────────────────────────────────
def page_row(slug, stamp):
    return {"slug": slug, "source_id": "brain", "updated_at": stamp}


class FetchPagesTests(unittest.TestCase):
    def test_cursor_walks_until_an_empty_batch(self):
        session = FakeSession([
            [page_row("a", "2026-01-01T00:00:00Z"), page_row("b", "2026-01-02T00:00:00Z")],
            [page_row("c", "2026-01-03T00:00:00Z")],
            [],
        ])
        index = gb.fetch_pages(session, page_size=2)
        self.assertEqual(set(index["pages"]), {"a", "b", "c"})
        self.assertFalse(index["truncated"])
        self.assertEqual([call[1].get("updated_after") for call in session.calls],
                         [None, "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"])
        self.assertTrue(all(call[1]["sort"] == "updated_asc" for call in session.calls))

    def test_a_server_capped_short_batch_does_not_end_the_walk(self):
        """Regression: `list_pages` caps non-local callers, so "fewer rows than
        I asked for" is not a stop condition — stopping there loses pages."""
        session = FakeSession([
            [page_row("a", "2026-01-01T00:00:00Z")],      # asked 100, capped to 1
            [page_row("b", "2026-01-02T00:00:00Z")],
            [],
        ])
        index = gb.fetch_pages(session, page_size=100)
        self.assertEqual(set(index["pages"]), {"a", "b"})

    def test_an_inclusive_updated_after_neither_spins_nor_loses_rows(self):
        """gbrain 0.48.3.0 answers `updated_after` inclusively, so every batch
        after the first repeats the boundary row. The walk must still finish."""
        session = FakeSession([
            [page_row("a", "2026-01-01T00:00:00Z"), page_row("b", "2026-01-02T00:00:00Z")],
            [page_row("b", "2026-01-02T00:00:00Z"), page_row("c", "2026-01-03T00:00:00Z")],
            [page_row("c", "2026-01-03T00:00:00Z")],
        ])
        index = gb.fetch_pages(session, page_size=2)
        self.assertEqual(set(index["pages"]), {"a", "b", "c"})
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(index["truncated"], "the walk reached the end of the brain")

    def test_the_cursor_follows_the_greatest_stamp_even_if_rows_arrive_unsorted(self):
        session = FakeSession([
            [page_row("b", "2026-01-02T00:00:00Z"), page_row("a", "2026-01-01T00:00:00Z")],
            [],
        ])
        gb.fetch_pages(session, page_size=2)
        self.assertEqual(session.calls[1][1]["updated_after"], "2026-01-02T00:00:00Z")

    def test_differently_spelled_boundary_stamps_still_count_as_no_progress(self):
        session = FakeSession([
            [page_row("a", "2026-01-01T00:00:00Z")],
            [page_row("a", "2026-01-01T00:00:00+00:00")],
        ])
        index = gb.fetch_pages(session, page_size=1)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(set(index["pages"]), {"a"})

    def test_a_batch_of_tied_timestamps_stops_instead_of_spinning(self):
        tied = [page_row("a", "2026-01-01T00:00:00Z"), page_row("b", "2026-01-01T00:00:00Z")]
        session = FakeSession([tied, tied, tied])
        index = gb.fetch_pages(session, page_size=2)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(index["truncated"], "a full batch that cannot advance is truncation")
        self.assertEqual(set(index["pages"]), {"a", "b"})

    def test_call_budget_is_bounded_and_reported(self):
        session = FakeSession([[page_row("s%d" % i, "2026-01-%02dT00:00:00Z" % (i + 1))]
                               for i in range(5)])
        index = gb.fetch_pages(session, page_size=1, max_calls=3)
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(index["truncated"])

    def test_a_non_list_payload_is_a_tool_error(self):
        session = FakeSession([{"pages": []}])
        with self.assertRaises(gb.GBrainToolError):
            gb.fetch_pages(session)

    def test_the_source_scope_is_forwarded_when_asked_for(self):
        session = FakeSession([[]])
        gb.fetch_pages(session, source_id="__all__")
        self.assertEqual(session.calls[0][1]["source_id"], "__all__")

    def test_a_scope_the_server_ignores_does_not_get_to_enrich_anything(self):
        session = FakeSession([
            [page_row("a", "2026-01-01T00:00:00Z"),
             {"slug": "b", "source_id": "other", "updated_at": "2026-01-02T00:00:00Z"}],
            [],
        ])
        index = gb.fetch_pages(session, source_id="brain", page_size=2)
        self.assertEqual(session.calls[0][1]["source_id"], "brain")
        self.assertEqual(set(index["pages"]), {"a"})
        self.assertEqual(index["foreign"], 1)

    def test_no_scope_is_sent_when_none_was_requested(self):
        session = FakeSession([[]])
        gb.fetch_pages(session)
        self.assertNotIn("source_id", session.calls[0][1])


# ── version history ──────────────────────────────────────────────────────────
class SummarizeVersionsTests(unittest.TestCase):
    def test_counts_dates_and_months_come_from_snapshot_at(self):
        summary = gb.summarize_versions([
            {"snapshot_at": "2026-06-10T11:59:59.600Z", "compiled_truth": SECRET_BODY},
            {"snapshot_at": "2026-03-05T08:00:00.000Z", "compiled_truth": SECRET_BODY},
        ])
        self.assertEqual(summary["revisions"], 2)
        self.assertEqual(summary["first"], "2026-03-05T08:00:00+00:00")
        self.assertEqual(summary["last"], "2026-06-10T11:59:59+00:00")
        self.assertEqual(summary["months"], {"2026-03": 1, "2026-06": 1})

    def test_private_revision_bodies_are_dropped_on_the_way_in(self):
        summary = gb.summarize_versions([
            {"snapshot_at": "2026-06-10T00:00:00Z", "compiled_truth": SECRET_BODY,
             "frontmatter": {"note": SECRET_KEY}}])
        self.assertNotIn(SECRET_BODY, json.dumps(summary))
        self.assertNotIn(SECRET_KEY, json.dumps(summary))
        self.assertEqual(set(summary), {"revisions", "first", "last", "months",
                                        "malformed", "error"})

    def test_unusable_rows_are_counted_not_guessed(self):
        summary = gb.summarize_versions(["nope", {}, {"snapshot_at": "soon"},
                                         {"snapshot_at": "2026-06-10T00:00:00Z"}])
        self.assertEqual(summary["revisions"], 1)
        self.assertEqual(summary["malformed"], 3)

    def test_no_versions_is_an_empty_summary_not_an_error(self):
        summary = gb.summarize_versions([])
        self.assertEqual(summary["revisions"], 0)
        self.assertEqual(summary["last"], "")
        self.assertEqual(summary["error"], "")


class FetchHistoryTests(unittest.TestCase):
    def test_a_budget_reads_the_head_of_the_given_order(self):
        session = FakeSession([[], []])
        result = gb.fetch_history(session, ["a", "b", "c"], limit=2, order=["c", "b", "a"])
        self.assertEqual([call[1]["slug"] for call in session.calls], ["c", "b"])
        self.assertEqual(result["asked"], 2)

    def test_slugs_are_visited_in_a_stable_order_without_an_explicit_one(self):
        session = FakeSession([[], [], []])
        gb.fetch_history(session, ["c", "a", "b"])
        self.assertEqual([call[1]["slug"] for call in session.calls], ["a", "b", "c"])

    def test_one_failing_page_does_not_stop_the_others(self):
        session = FakeSession([gb.GBrainToolError("get_versions failed: no such page"),
                               [{"snapshot_at": "2026-06-01T00:00:00Z"}]])
        result = gb.fetch_history(session, ["a", "b"])
        self.assertIn("no such page", result["history"]["a"]["error"])
        self.assertEqual(result["history"]["b"]["revisions"], 1)
        self.assertEqual(result["failures"], 1)

    def test_a_non_list_history_payload_is_recorded_as_an_error(self):
        session = FakeSession([{"versions": []}])
        result = gb.fetch_history(session, ["a"])
        self.assertIn("did not return a list", result["history"]["a"]["error"])
        self.assertEqual(result["failures"], 1)

    def test_a_dead_session_propagates_instead_of_being_swallowed(self):
        session = FakeSession([gb.GBrainUnavailable("gbrain serve exited")])
        with self.assertRaises(gb.GBrainUnavailable):
            gb.fetch_history(session, ["a"])

    def test_partly_unreadable_rows_are_an_unknown_count_not_a_low_one(self):
        """Two rows arrived, one of them unreadable. Whether the page has two
        revisions or two hundred is exactly what cannot be known, so the count
        is withheld and the read is tallied as incomplete."""
        session = FakeSession([[{"snapshot_at": "2026-06-01T00:00:00Z"}, "nope"]])
        result = gb.fetch_history(session, ["a"])
        self.assertTrue(result["history"]["a"]["error"])
        self.assertEqual(result["history"]["a"]["malformed"], 1)
        self.assertEqual(result["incomplete"], 1)
        self.assertEqual(result["failures"], 0)
        self.assertIsNone(gb.classify("2026-06-02T00:00:00Z",
                                      result["history"]["a"])["revisions"])


class HistoryScopeTests(unittest.TestCase):
    """`get_versions` takes a bare slug and answers with the union of every
    source that holds it, so uniqueness has to be *proven* before it is sent."""

    def scope(self, index_batches, source_id=None, audit_batches=()):
        session = FakeSession(list(index_batches) + list(audit_batches))
        reader = gb.GBrainReader(session, source_id=source_id, page_size=2)
        reader.pages()
        return reader.history_scope(), reader, session

    def test_a_federated_index_is_its_own_collision_proof(self):
        scope, _reader, session = self.scope([[page_row("a", "2026-01-01T00:00:00Z")], []])
        self.assertTrue(scope["proven"])
        self.assertEqual(scope["unique"], {"a"})
        self.assertEqual(len(session.calls), 2, "no second walk when one already saw everything")

    def test_a_concrete_scope_is_audited_against_every_source(self):
        scoped = [{"slug": "a", "source_id": "wanted", "updated_at": "2026-01-01T00:00:00Z"},
                  {"slug": "b", "source_id": "wanted", "updated_at": "2026-01-02T00:00:00Z"}]
        federated = scoped + [{"slug": "a", "source_id": "elsewhere",
                               "updated_at": "2026-01-03T00:00:00Z"}]
        scope, reader, session = self.scope(
            [scoped, []], source_id="wanted",
            audit_batches=[federated[:2], federated[2:], []])
        self.assertTrue(scope["proven"])
        self.assertEqual(scope["unique"], {"b"}, "the slug two sources hold is not readable")
        self.assertEqual([call[1].get("source_id") for call in session.calls],
                         ["wanted", "wanted", "__all__", "__all__", "__all__"])
        self.assertEqual(reader.diagnostics["auditCollisions"], 1)

    def test_an_audit_that_stopped_early_proves_nothing_about_any_slug(self):
        tied = [{"slug": "t%d" % i, "source_id": "s", "updated_at": "2026-01-01T00:00:00Z"}
                for i in range(2)]
        scope, _reader, _session = self.scope(
            [[page_row("a", "2026-01-01T00:00:00Z")], []], source_id="wanted",
            audit_batches=[tied, tied])
        self.assertFalse(scope["proven"])
        self.assertEqual(scope["unique"], set())
        self.assertIn("stopped early", scope["reason"])

    def test_an_unreadable_audit_row_could_have_been_any_slug(self):
        scope, _reader, _session = self.scope(
            [[page_row("a", "2026-01-01T00:00:00Z")], []], source_id="wanted",
            audit_batches=[["junk", page_row("a", "2026-01-01T00:00:00Z")], []])
        self.assertFalse(scope["proven"])
        self.assertEqual(scope["unique"], set())
        self.assertIn("unreadable", scope["reason"])

    def test_an_audit_that_failed_outright_blocks_history_without_killing_the_build(self):
        private = "JSONRPC_BACKEND_PRIVATE_MESSAGE"
        scope, reader, _session = self.scope(
            [[page_row("a", "2026-01-01T00:00:00Z")], []], source_id="wanted",
            audit_batches=[gb.GBrainToolError("list_pages failed: " + private)])
        self.assertFalse(scope["proven"])
        self.assertEqual(scope["reason"], "the brain-wide collision audit failed")
        self.assertNotIn(private, scope["reason"])
        self.assertEqual(reader.diagnostics["historyProof"], "unproven")

    def test_a_truncated_federated_index_cannot_prove_uniqueness_either(self):
        tied = [page_row("a", "2026-01-01T00:00:00Z"), page_row("b", "2026-01-01T00:00:00Z")]
        scope, _reader, _session = self.scope([tied, tied])
        self.assertFalse(scope["proven"])
        self.assertEqual(scope["unique"], set())

    def test_the_index_has_to_be_read_before_its_history(self):
        reader = gb.GBrainReader(FakeSession([]))
        with self.assertRaises(gb.GBrainToolError):
            reader.history_scope()


class ReplyBoundTests(unittest.TestCase):
    """MCP frames one message per line and cannot stream one, so the bound on
    how much of a brain can end up in memory at once is the reply cap."""

    def test_a_reply_past_the_cap_is_dropped_and_only_that_page_is_lost(self):
        brain = {"pages": [], "versions": {
            "big": [{"snapshot_at": "2026-06-01T00:00:00Z", "compiled_truth": "x" * 20000}],
            "small": [{"snapshot_at": "2026-05-01T00:00:00Z"}]}}
        with tempfile.TemporaryDirectory() as folder:
            command = fake_gbrain(folder, brain=brain)
            with mock.patch.object(gb, "MAX_REPLY_CHARS", 4096):
                session = gb.McpStdioSession(command=command, timeout=20).open()
                try:
                    result = gb.fetch_history(session, ["big", "small"])
                finally:
                    session.close()
        self.assertIn("cap", result["history"]["big"]["error"])
        self.assertEqual(result["history"]["big"]["revisions"], 0)
        self.assertEqual(result["failures"], 1)
        # The session survives a dropped reply: the next page still reads.
        self.assertEqual(result["history"]["small"]["revisions"], 1)


# ── precedence ───────────────────────────────────────────────────────────────
class ClassifyTests(unittest.TestCase):
    def summary(self, last, revisions=1):
        return {"revisions": revisions, "last": last, "first": last,
                "months": {}, "malformed": 0, "error": ""}

    def test_history_not_read_leaves_updated_at_unverified(self):
        verdict = gb.classify("2026-06-10T12:00:00.000Z", None)
        self.assertEqual(verdict["status"], "unverified")
        self.assertEqual(verdict["source"], gb.SOURCE_UPDATED)
        self.assertEqual(verdict["updated"], "2026-06-10T12:00:00+00:00")
        self.assertIsNone(verdict["revisions"])

    def test_a_matching_snapshot_verifies_the_backend_stamp(self):
        verdict = gb.classify("2026-06-10T12:00:00.000Z",
                              self.summary("2026-06-10T11:59:59.600Z", revisions=2))
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["source"], gb.SOURCE_HISTORY)
        self.assertEqual(verdict["updated"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(verdict["revisions"], 2)

    def test_a_later_non_semantic_touch_falls_back_to_the_last_revision(self):
        verdict = gb.classify("2026-06-12T09:00:00.000Z",
                              self.summary("2026-04-02T00:00:00.000Z"))
        self.assertEqual(verdict["status"], "superseded")
        self.assertEqual(verdict["source"], gb.SOURCE_REVISION)
        self.assertEqual(verdict["updated"], "2026-04-02T00:00:00+00:00")

    def test_the_write_skew_window_is_not_a_non_semantic_touch(self):
        inside = gb.classify("2026-06-10T12:00:04.000Z", self.summary("2026-06-10T12:00:00Z"))
        outside = gb.classify("2026-06-10T12:00:06.000Z", self.summary("2026-06-10T12:00:00Z"))
        self.assertEqual(inside["status"], "verified")
        self.assertEqual(outside["status"], "superseded")

    def test_a_page_written_once_says_so_instead_of_claiming_verification(self):
        verdict = gb.classify("2026-06-14T00:00:00Z", self.summary("", revisions=0))
        self.assertEqual(verdict["status"], "single-write")
        self.assertEqual(verdict["source"], gb.SOURCE_UPDATED)
        self.assertEqual(verdict["revisions"], 0)

    def test_a_failed_history_read_is_labelled_error_and_keeps_updated_at(self):
        verdict = gb.classify("2026-06-14T00:00:00Z",
                              {"revisions": 0, "last": "", "months": {}, "error": "boom"})
        self.assertEqual(verdict["status"], "error")
        self.assertEqual(verdict["updated"], "2026-06-14T00:00:00+00:00")

    def test_an_unusable_backend_stamp_yields_nothing_to_apply(self):
        verdict = gb.classify("not-a-date", None)
        self.assertEqual(verdict["updated"], "")
        self.assertEqual(verdict["source"], "")
        self.assertEqual(verdict["status"], "missing")

    def test_a_snapshot_newer_than_updated_at_is_never_called_verified(self):
        """Regression: `createVersion` fires just *before* the upsert, so a
        snapshot later than the page's own `updated_at` cannot come from the
        write path. The two numbers disagree and neither confirms the other."""
        verdict = gb.classify("2026-06-10T12:00:00Z", self.summary("2026-06-11T00:00:00Z"))
        self.assertEqual(verdict["status"], "inconsistent")
        self.assertEqual(verdict["source"], gb.SOURCE_UPDATED)
        self.assertEqual(verdict["updated"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(verdict["revisions"], 1)
        self.assertEqual(verdict["lastRevision"], "2026-06-11T00:00:00+00:00")

    def test_the_skew_window_is_symmetric_around_a_single_write(self):
        inside = gb.classify("2026-06-10T12:00:00Z", self.summary("2026-06-10T12:00:04Z"))
        outside = gb.classify("2026-06-10T12:00:00Z", self.summary("2026-06-10T12:00:06Z"))
        self.assertEqual(inside["status"], "verified")
        self.assertEqual(outside["status"], "inconsistent")


# ── end to end, through a real MCP stdio subprocess ──────────────────────────
class AdapterEndToEndTests(unittest.TestCase):
    def build(self, folder, **overrides):
        vault = write_vault(Path(folder, "vault"))
        passthrough = ("brain", "mode", "cap", "log", "cursor")
        command = fake_gbrain(folder, **{k: v for k, v in overrides.items()
                                         if k in passthrough})
        opts = options(command, **{k: v for k, v in overrides.items()
                                   if k not in passthrough})
        return build_map.build(vault, "Test", as_of=AS_OF, gbrain=opts)

    def nodes(self, payload):
        return {node["data"]["id"]: node["data"] for node in payload["nodes"]}

    @contextlib.contextmanager
    def capped_reply(self, chars):
        """Shrink the reply cap for a whole build.

        build_map loads the adapter by path, so it would otherwise get a fresh
        module with the real 16 MiB ceiling; pinning it to the one under test
        makes an oversized reply a few kilobytes instead of megabytes.
        """
        with mock.patch.object(build_map, "gbrain_adapter", lambda: gb), \
             mock.patch.object(gb, "MAX_REPLY_CHARS", chars):
            yield

    def test_backend_updated_at_outranks_every_frontmatter_field(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder)
        data = self.nodes(payload)
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(data["Alpha"]["freshnessSource"], gb.SOURCE_HISTORY)
        self.assertEqual(data["Gamma"]["updated"], "2026-06-14T00:00:00+00:00")
        self.assertEqual(data["Gamma"]["freshnessSource"], gb.SOURCE_UPDATED)
        self.assertEqual(payload["gbrain"]["status"], "ok")
        self.assertEqual(payload["gbrain"]["matched"], 6)

    def test_freshness_bands_follow_the_backend_not_the_export(self):
        with tempfile.TemporaryDirectory() as folder:
            markdown = build_map.build(write_vault(Path(folder, "vault")), "Test", as_of=AS_OF)
            payload = self.build(folder)
        # 2026-02-01 in the export vs 2026-06-10 in the brain, banded at 2026-06-15
        self.assertEqual(self.nodes(markdown)["Alpha"]["freshness"], "dormant")
        self.assertEqual(self.nodes(payload)["Alpha"]["freshness"], "fresh")

    def test_a_non_semantic_touch_is_not_treated_as_a_content_update(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder)
        beta = self.nodes(payload)["Beta"]
        self.assertEqual(beta["historyStatus"], "superseded")
        self.assertEqual(beta["updated"], "2026-04-02T00:00:00+00:00")
        self.assertEqual(beta["freshnessSource"], gb.SOURCE_REVISION)
        self.assertEqual(beta["brainUpdated"], "2026-06-12T09:00:00+00:00")

    def test_notes_the_brain_does_not_know_keep_their_markdown_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder)
        orphan = self.nodes(payload)["Orphan File"]
        self.assertEqual(orphan["updated"], "2026-02-14T00:00:00+00:00")
        self.assertEqual(orphan["freshnessSource"], "updated")
        self.assertNotIn("brainSlug", orphan)

    def test_a_stamped_frontmatter_slug_matches_a_page_the_path_cannot(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder)
        legacy = self.nodes(payload)["Legacy"]
        # `legacy.md` is not the page's slug; only the stamped one matches, so
        # the backend stamp landing here *is* the proof the match happened.
        self.assertEqual(legacy["updated"], "2026-05-01T00:00:00+00:00")
        self.assertEqual(legacy["freshnessSource"], gb.SOURCE_UPDATED)

    def test_a_slug_prefix_matches_a_subdirectory_of_an_export(self):
        brain = {"pages": [{"slug": "wiki/notes/alpha", "source_id": "w",
                            "updated_at": "2026-06-09T00:00:00Z"}], "versions": {}}
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, brain=brain, slugPrefix="wiki", history=False)
        self.assertEqual(self.nodes(payload)["Alpha"]["updated"], "2026-06-09T00:00:00+00:00")
        self.assertEqual(payload["gbrain"]["matched"], 1)

    def test_brain_slugs_and_source_ids_never_reach_the_payload_or_the_html(self):
        """Matching metadata is an input, not map data: the generated file is
        meant to be shared, and it displays neither of them."""
        slug, source = "Private/Odd Slug \u2014 keep-out", "private-source-42"
        brain = {"pages": [{"slug": slug, "source_id": source, "title": "Legacy",
                            "updated_at": "2026-05-01T00:00:00Z"}],
                 "versions": {slug: [{"snapshot_at": "2026-05-01T00:00:00Z"}]}}
        vault = {"legacy.md": "---\ncreated: 2026-01-05\nslug: %s\n---\n# Legacy\n" % slug}
        with tempfile.TemporaryDirectory() as folder:
            write_vault(Path(folder, "vault"), vault)
            payload = build_map.build(str(Path(folder, "vault")), "Test", as_of=AS_OF,
                                      gbrain=options(fake_gbrain(folder, brain=brain)))
            html = build_map.render(payload)
        legacy = self.nodes(payload)["Legacy"]
        self.assertEqual(legacy["updated"], "2026-05-01T00:00:00+00:00")   # it did match
        self.assertEqual(legacy["revisions"], 1)
        for field in ("brainSlug", "brainSourceId", "slugHint"):
            with self.subTest(field=field):
                self.assertNotIn(field, legacy)
        for private in (slug, source):
            with self.subTest(private=private):
                self.assertNotIn(private, json.dumps(payload))
                self.assertNotIn(private, html)

    def test_backend_server_metadata_never_reaches_payload_or_html(self):
        secret = "BACKEND_PRIVATE_VERSION"
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, mode="private-server-info")
            html = build_map.render(payload)
        self.assertNotIn(secret, json.dumps(payload))
        self.assertNotIn(secret, html)
        self.assertNotIn("server", payload["gbrain"]["diagnostics"])

    def test_history_off_reads_updated_at_only_and_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder, "calls.log")
            payload = self.build(folder, history=False, log=log)
            tools = {json.loads(line)["tool"] for line in log.read_text().splitlines()}
        self.assertEqual(tools, {"list_pages"})
        alpha = self.nodes(payload)["Alpha"]
        self.assertEqual(alpha["historyStatus"], "unverified")
        self.assertEqual(alpha["freshnessSource"], gb.SOURCE_UPDATED)
        self.assertIsNone(alpha["revisions"])
        self.assertFalse(payload["gbrain"]["history"]["requested"])
        self.assertEqual(payload["gbrain"]["history"]["events"], 0)
        self.assertFalse(payload["revisions"]["available"])

    def test_only_read_tools_are_ever_called(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder, "calls.log")
            self.build(folder, log=log)
            tools = {json.loads(line)["tool"] for line in log.read_text().splitlines()}
        self.assertEqual(tools, {"list_pages", "get_versions"})

    def test_private_revision_content_never_reaches_the_generated_map(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder)
            html = build_map.render(payload)
        for secret in (SECRET_BODY, SECRET_KEY):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, json.dumps(payload))
                self.assertNotIn(secret, html)

    def test_the_timeline_separates_creation_growth_from_revisions(self):
        """The axis is the union of both kinds of month. 2026-03 created no
        notes and revised one, so it is a month on the chart with zero theme
        counts — not a month the revision series has to drop."""
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder)
        months = [row["month"] for row in payload["timeline"]]
        self.assertEqual(months, ["2026-01", "2026-03", "2026-04", "2026-06"])
        created = [sum(count for key, count in row.items() if key != "month")
                   for row in payload["timeline"]]
        self.assertEqual(created, [5, 0, 1, 1])
        themes = sorted(key for key in payload["timeline"][1] if key != "month")
        self.assertTrue(themes, "a revision-only month still carries every theme key")
        self.assertEqual([payload["timeline"][1][t] for t in themes], [0] * len(themes))
        self.assertTrue(payload["revisions"]["available"])
        self.assertEqual(payload["revisions"]["series"], [0, 1, 1, 1])
        self.assertEqual(payload["revisions"]["events"], 3)
        self.assertEqual(sum(payload["revisions"]["series"]),
                         payload["revisions"]["events"], "nothing falls off the axis")
        self.assertNotIn("offAxis", payload["revisions"])
        self.assertEqual(payload["revisions"]["pages"], 2)

    def test_a_history_budget_is_deterministic_and_honestly_labelled(self):
        """A budget that stops short of the matched pages is a partial read,
        not a clean one: it says how many it asked for, read and skipped, and
        the pages it never reached keep an unverified backend timestamp."""
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder, "calls.log")
            payload = self.build(folder, historyLimit=1, log=log)
            asked = [json.loads(line)["arguments"]["slug"]
                     for line in log.read_text().splitlines()
                     if json.loads(line)["tool"] == "get_versions"]
        data = self.nodes(payload)
        self.assertEqual(asked, ["notes/gamma"])          # freshest matched page
        self.assertEqual(data["Gamma"]["historyStatus"], "single-write")
        self.assertEqual(data["Alpha"]["historyStatus"], "unverified")
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(data["Alpha"]["freshnessSource"], gb.SOURCE_UPDATED)
        self.assertIsNone(data["Alpha"]["revisions"])
        self.assertEqual(payload["gbrain"]["status"], "partial")
        history = payload["gbrain"]["history"]
        self.assertEqual((history["matched"], history["eligible"], history["asked"],
                          history["read"], history["skipped"]), (6, 6, 1, 1, 5))
        gap = " ".join(payload["gbrain"]["gaps"])
        self.assertIn("--gbrain-history-limit 1 read the 1 freshest of 6 eligible", gap)
        self.assertIn("the other 5 kept an unverified backend timestamp", gap)
        self.assertIn("--gbrain-history-limit 0", gap)

    def test_a_budget_that_reaches_every_page_is_a_clean_read(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, historyLimit=build_map.DEFAULT_HISTORY_LIMIT)
        self.assertEqual(payload["gbrain"]["status"], "ok")
        self.assertEqual(payload["gbrain"]["gaps"], [])
        history = payload["gbrain"]["history"]
        self.assertEqual((history["asked"], history["skipped"]), (6, 0))

    def test_a_slug_another_source_also_holds_is_never_asked_for_history(self):
        """A concrete --gbrain-source filters the other source's rows out of
        `list_pages`, but `get_versions` takes a bare slug and would answer
        with both pages' revisions. The scoped index cannot see that, so the
        adapter audits every source before it asks — and does not ask."""
        brain = {"pages": [
            {"slug": "notes/alpha", "source_id": "src-wanted-7",
             "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/alpha", "source_id": "src-elsewhere-9",
             "updated_at": "2026-06-02T00:00:00Z"},
            {"slug": "notes/gamma", "source_id": "src-wanted-7",
             "updated_at": "2026-06-04T00:00:00Z"}],
            "versions": {
                "notes/alpha": [{"snapshot_at": "2025-11-03T00:00:00Z",
                                 "compiled_truth": SECRET_BODY},
                                {"snapshot_at": "2026-06-01T00:00:00Z"}],
                "notes/gamma": [{"snapshot_at": "2026-06-04T00:00:00Z"}]}}
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder, "calls.log")
            payload = self.build(folder, brain=brain, source="src-wanted-7", log=log)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        asked = [call["arguments"]["slug"] for call in calls if call["tool"] == "get_versions"]
        scopes = {call["arguments"].get("source_id") for call in calls
                  if call["tool"] == "list_pages"}
        data = self.nodes(payload)
        self.assertEqual(asked, ["notes/gamma"], "the colliding slug is never asked about")
        self.assertEqual(scopes, {"src-wanted-7", "__all__"}, "the audit is a read of its own")
        # Alpha keeps the scoped index's own updated_at — that much *was*
        # proven — but nothing about the other source's revisions is borrowed.
        self.assertEqual(data["Alpha"]["updated"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(data["Alpha"]["freshnessSource"], gb.SOURCE_UPDATED)
        self.assertEqual(data["Alpha"]["historyStatus"], "unverified")
        self.assertIsNone(data["Alpha"]["revisions"])
        self.assertEqual(data["Alpha"]["lastRevision"], "")
        self.assertEqual(data["Gamma"]["historyStatus"], "verified")
        # No foreign revision metadata reaches the map: the union's own month
        # is not on the axis, and the counts are Gamma's alone.
        self.assertNotIn("2025-11", [row["month"] for row in payload["timeline"]])
        self.assertEqual(payload["revisions"]["events"], 1)
        self.assertEqual(payload["revisions"]["pages"], 1)
        self.assertEqual(payload["gbrain"]["status"], "partial")
        history = payload["gbrain"]["history"]
        self.assertEqual((history["matched"], history["eligible"], history["collisions"]),
                         (2, 1, 1))
        self.assertIn("share a slug with another source", " ".join(payload["gbrain"]["gaps"]))
        self.assertNotIn(SECRET_BODY, json.dumps(payload))

    def test_a_collision_audit_that_cannot_be_read_reads_no_history_at_all(self):
        """Unprovable is unprovable: an audit that failed or stopped early
        might have hidden any slug, so nothing it saw once can be called
        unique. Every page keeps its backend stamp, labelled unverified."""
        for mode, phrase in (("federated-error", "failed"),
                             ("federated-tie", "stopped early")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                log = Path(folder, "calls.log")
                payload = self.build(folder, mode=mode, source="brain", log=log)
                tools = [json.loads(line)["tool"] for line in log.read_text().splitlines()]
                data = self.nodes(payload)
                self.assertNotIn("get_versions", tools)
                self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
                self.assertEqual(data["Alpha"]["historyStatus"], "unverified")
                self.assertIsNone(data["Alpha"]["revisions"])
                self.assertEqual(payload["gbrain"]["status"], "partial")
                self.assertEqual(payload["gbrain"]["history"]["eligible"], 0)
                self.assertEqual(payload["gbrain"]["history"]["collisions"], 0)
                gap = " ".join(payload["gbrain"]["gaps"])
                self.assertIn("no page history was read", gap)
                self.assertIn(phrase, gap)
                self.assertFalse(payload["revisions"]["available"])

    def test_private_jsonrpc_audit_error_never_reaches_payload_or_html(self):
        private = "JSONRPC_BACKEND_PRIVATE_MESSAGE"
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, mode="federated-error", source="brain")
            html = build_map.render(payload)
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertNotIn(private, json.dumps(payload))
        self.assertNotIn(private, html)
        self.assertIn("collision audit failed", " ".join(payload["gbrain"]["gaps"]))

    def test_a_reply_past_the_cap_is_a_partial_build_not_a_silent_zero(self):
        with tempfile.TemporaryDirectory() as folder, self.capped_reply(4096):
            payload = self.build(folder, brain=brain_with({"notes/alpha": [
                {"snapshot_at": "2026-06-10T11:59:59.600Z", "compiled_truth": "x" * 6000}]}))
        data = self.nodes(payload)
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertEqual(payload["gbrain"]["history"]["failed"], 1)
        self.assertIn("could not be read for 1 page(s)", " ".join(payload["gbrain"]["gaps"]))
        self.assertEqual(data["Alpha"]["historyStatus"], "error")
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertIsNone(data["Alpha"]["revisions"])
        # One runaway page, not the whole read: Beta's history still landed.
        self.assertEqual(data["Beta"]["historyStatus"], "superseded")

    def test_required_refuses_a_build_whose_reply_ran_past_the_cap(self):
        with tempfile.TemporaryDirectory() as folder, self.capped_reply(4096):
            with self.assertRaises(RuntimeError) as caught:
                self.build(folder, required=True, brain=brain_with({"notes/alpha": [
                    {"snapshot_at": "2026-06-10T11:59:59.600Z", "compiled_truth": "x" * 6000}]}))
        self.assertIn("incomplete", str(caught.exception))
        self.assertIn("could not be read for 1 page(s)", str(caught.exception))

    def test_unreadable_version_rows_are_not_turned_into_a_revision_count(self):
        """Some of the page's rows arrived and some did not, so its real
        revision count is unknowable — and an unknowable count is withheld,
        not rounded down to what happened to be readable."""
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, brain=brain_with(
                {"notes/alpha": [{"snapshot_at": "2026-06-10T11:59:59.600Z"}, "junk"]}))
        data = self.nodes(payload)
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertEqual(payload["gbrain"]["history"]["incomplete"], 1)
        self.assertIn("unreadable version rows", " ".join(payload["gbrain"]["gaps"]))
        self.assertEqual(data["Alpha"]["historyStatus"], "error")
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertIsNone(data["Alpha"]["revisions"])
        # Its readable row is not counted either: a partial count is not a count.
        self.assertEqual(payload["revisions"]["events"], 1)      # Beta's, alone
        self.assertEqual(payload["gbrain"]["history"]["revised"], 1)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError) as caught:
                self.build(folder, required=True, brain=brain_with(
                    {"notes/alpha": [{"snapshot_at": "2026-06-10T11:59:59.600Z"}, "junk"]}))
        self.assertIn("unreadable version rows", str(caught.exception))

    def test_paging_walks_every_page_when_the_server_caps_each_batch(self):
        for cursor in ("inclusive", "exclusive"):
            with self.subTest(cursor=cursor), tempfile.TemporaryDirectory() as folder:
                payload = self.build(folder, cap=2, history=False, cursor=cursor)
                self.assertEqual(payload["gbrain"]["matched"], 6)
                self.assertGreaterEqual(payload["gbrain"]["diagnostics"]["calls"], 4)
                self.assertFalse(payload["gbrain"]["diagnostics"]["truncated"])

    def test_malformed_rows_are_skipped_and_the_rest_still_enrich(self):
        brain = dict(BRAIN, malformed_rows=["junk", {"slug": "x"},
                                            {"slug": "notes/alpha", "updated_at": "soon"}])
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, brain=brain, history=False)
        self.assertEqual(payload["gbrain"]["diagnostics"]["malformed"], 3)
        self.assertEqual(self.nodes(payload)["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")

    def test_two_notes_resolving_to_one_page_count_its_revisions_once(self):
        vault = {"dup.md": "---\ncreated: 2026-05-01\n---\n# Dup\n",
                 "other.md": "---\ncreated: 2026-05-02\nslug: dup\n---\n# Other\n"}
        brain = {"pages": [{"slug": "dup", "source_id": "brain",
                            "updated_at": "2026-06-01T00:00:00Z"}],
                 "versions": {"dup": [{"snapshot_at": "2026-05-20T00:00:00Z"},
                                      {"snapshot_at": "2026-06-01T00:00:00Z"}]}}
        with tempfile.TemporaryDirectory() as folder:
            write_vault(Path(folder, "vault"), vault)
            payload = build_map.build(str(Path(folder, "vault")), "Test", as_of=AS_OF,
                                      gbrain=options(fake_gbrain(folder, brain=brain)))
        data = self.nodes(payload)
        self.assertEqual(data["Dup"]["updated"], data["Other"]["updated"])   # one page
        self.assertEqual(payload["gbrain"]["matched"], 2)
        self.assertEqual(payload["revisions"]["events"], 2)   # the page's two, once
        self.assertEqual(payload["revisions"]["pages"], 1)

    def test_an_ambiguous_slug_falls_back_to_markdown(self):
        brain = {"pages": [
            {"slug": "notes/alpha", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/alpha", "source_id": "b", "updated_at": "2026-06-02T00:00:00Z"}],
            "versions": {}}
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, brain=brain, history=False)
        alpha = self.nodes(payload)["Alpha"]
        self.assertEqual(alpha["freshnessSource"], "last_updated")
        self.assertEqual(payload["gbrain"]["diagnostics"]["ambiguous"], 1)
        self.assertEqual(payload["gbrain"]["matched"], 0)
        self.assertEqual(payload["gbrain"]["status"], "partial")

    def test_equal_timestamps_do_not_resolve_a_slug_that_two_sources_claim(self):
        """`get_versions` takes a bare slug and would answer with the union of
        both pages' rows, so an ambiguous slug is never asked about at all."""
        brain = {"pages": [
            {"slug": "notes/alpha", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/alpha", "source_id": "b", "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/gamma", "source_id": "a", "updated_at": "2026-06-04T00:00:00Z"}],
            "versions": {"notes/alpha": [{"snapshot_at": "2026-05-01T00:00:00Z"}],
                         "notes/gamma": [{"snapshot_at": "2026-06-04T00:00:00Z"}]}}
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder, "calls.log")
            payload = self.build(folder, brain=brain, log=log)
            asked = [json.loads(line)["arguments"]["slug"]
                     for line in log.read_text().splitlines()
                     if json.loads(line)["tool"] == "get_versions"]
        data = self.nodes(payload)
        self.assertEqual(asked, ["notes/gamma"], "no history read for an ambiguous slug")
        self.assertEqual(data["Alpha"]["freshnessSource"], "last_updated")
        self.assertEqual(data["Alpha"]["updated"], "2026-02-01T00:00:00+00:00")
        self.assertNotIn("historyStatus", data["Alpha"])
        self.assertEqual(data["Gamma"]["updated"], "2026-06-04T00:00:00+00:00")
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertIn("--gbrain-source", " ".join(payload["gbrain"]["gaps"]))

    def test_a_scoped_read_drops_rows_the_server_should_have_filtered(self):
        brain = {"pages": [
            {"slug": "notes/alpha", "source_id": "src-wanted-7",
             "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/alpha", "source_id": "src-elsewhere-9",
             "updated_at": "2026-06-02T00:00:00Z"},
            {"slug": "notes/gamma", "source_id": "src-elsewhere-9",
             "updated_at": "2026-06-04T00:00:00Z"}],
            "versions": {}}
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder, "calls.log")
            payload = self.build(folder, brain=brain, mode="ignore-scope",
                                 source="src-wanted-7", history=False, log=log)
            scopes = {json.loads(line)["arguments"].get("source_id")
                      for line in log.read_text().splitlines()}
        data = self.nodes(payload)
        # The collision audit exists to make `get_versions` safe; without
        # history to read it would be a second walk of the brain for nothing.
        self.assertEqual(scopes, {"src-wanted-7"})
        # Only the row that proves it came from `wanted` is used; the other
        # source's rows are not a fallback, not a tiebreak, not anything.
        self.assertEqual(data["Alpha"]["updated"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(data["Gamma"]["freshnessSource"], "updated")
        # Wire rows, like `malformed`: two foreign pages, one of them repeated
        # by the inclusive-cursor boundary.
        self.assertEqual(payload["gbrain"]["diagnostics"]["foreign"], 3)
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertTrue(payload["gbrain"]["diagnostics"]["scoped"])
        # The scope is recorded as a fact, not as a name to publish.
        for name in ("src-wanted-7", "src-elsewhere-9"):
            with self.subTest(name=name):
                self.assertNotIn(name, json.dumps(payload["gbrain"]))

    def test_a_page_index_that_stopped_early_is_reported_as_partial(self):
        """More pages tie on one `updated_at` than fit in a batch, so the
        cursor cannot step over them and the walk stops mid-brain."""
        tied = "2026-06-05T00:00:00.000Z"
        brain = {"pages": [{"slug": slug, "source_id": "brain", "updated_at": tied}
                           for slug in ("notes/alpha", "notes/beta", "notes/gamma")] +
                          [{"slug": "zfill/%04d" % i, "source_id": "brain", "updated_at": tied}
                           for i in range(98)],
                 "versions": {}}
        with tempfile.TemporaryDirectory() as folder:
            payload = self.build(folder, brain=brain, history=False)
        data = self.nodes(payload)
        self.assertTrue(payload["gbrain"]["diagnostics"]["truncated"])
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertIn("page index stopped early", " ".join(payload["gbrain"]["gaps"]))
        self.assertIn("Partial read", payload["gbrain"]["detail"])
        # Per note, not all-or-nothing: what was seen is still honest.
        self.assertEqual(data["Alpha"]["updated"], "2026-06-05T00:00:00+00:00")
        self.assertEqual(data["Alpha"]["freshnessSource"], gb.SOURCE_UPDATED)
        self.assertEqual(data["Orphan File"]["updated"], "2026-02-14T00:00:00+00:00")
        self.assertEqual(data["Orphan File"]["freshnessSource"], "updated")


class BackendFailureTests(unittest.TestCase):
    """Every failure mode must leave a usable Markdown-only map behind."""

    def run_mode(self, mode, **overrides):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            command = fake_gbrain(folder, mode=mode)
            return build_map.build(vault, "Test", as_of=AS_OF,
                                   gbrain=options(command, **overrides))

    def assert_failed_open(self, payload):
        data = {node["data"]["id"]: node["data"] for node in payload["nodes"]}
        self.assertEqual(payload["gbrain"]["status"], "unavailable")
        self.assertFalse(payload["gbrain"]["enabled"])
        self.assertIn("Markdown", payload["gbrain"]["detail"])
        self.assertEqual(data["Alpha"]["freshnessSource"], "last_updated")
        self.assertEqual(data["Alpha"]["updated"], "2026-02-01T00:00:00+00:00")
        self.assertFalse(payload["revisions"]["available"])

    def test_a_backend_that_will_not_start_fails_open(self):
        self.assert_failed_open(self.run_mode("no-start"))

    def test_a_missing_binary_fails_open(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            payload = build_map.build(vault, "Test", as_of=AS_OF,
                                      gbrain=options(str(Path(folder, "nope"))))
        self.assert_failed_open(payload)

    def test_a_refused_handshake_fails_open(self):
        self.assert_failed_open(self.run_mode("handshake-error"))

    def test_malformed_handshake_metadata_fails_open_without_a_traceback(self):
        self.assert_failed_open(self.run_mode("handshake-bad-info"))

    def test_private_child_stderr_never_reaches_payload_or_html(self):
        secret_slug = "private/slugs/customer-alpha"
        secret_body = "SUPER-SECRET-REVISION-BODY"
        payload = self.run_mode("private-stderr")
        public = json.dumps(payload) + build_map.render(payload)
        self.assertNotIn(secret_slug, public)
        self.assertNotIn(secret_body, public)
        self.assertEqual(payload["gbrain"]["detail"],
                         "GBrain unavailable — kept the Markdown timestamps.")

    def test_a_tool_error_on_list_pages_fails_open(self):
        self.assert_failed_open(self.run_mode("pages-error"))

    def test_non_json_content_fails_open(self):
        self.assert_failed_open(self.run_mode("pages-garbage"))

    def test_a_wrongly_shaped_list_pages_payload_fails_open(self):
        self.assert_failed_open(self.run_mode("pages-not-list"))

    def test_a_hung_backend_times_out_and_fails_open(self):
        self.assert_failed_open(self.run_mode("hang", timeout=0.5))

    def test_a_session_lost_while_indexing_pages_fails_open(self):
        self.assert_failed_open(self.run_mode("die-after-pages"))

    def test_a_session_lost_during_history_keeps_the_page_timestamps(self):
        payload = self.run_mode("die-in-history")
        data = {node["data"]["id"]: node["data"] for node in payload["nodes"]}
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertTrue(payload["gbrain"]["history"]["error"])
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(data["Alpha"]["historyStatus"], "unverified")

    def test_per_page_history_errors_keep_updated_at_and_say_history_failed(self):
        """A history read that failed for every page is *not* a clean build:
        the timestamps stand, but nothing confirmed them, so the read is
        partial and says how many pages it lost."""
        payload = self.run_mode("versions-error")
        data = {node["data"]["id"]: node["data"] for node in payload["nodes"]}
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertIn("could not be read for 6 page(s)", " ".join(payload["gbrain"]["gaps"]))
        self.assertEqual(payload["gbrain"]["history"]["failed"], 6)
        self.assertEqual(payload["gbrain"]["history"]["read"], 0)
        self.assertEqual(data["Alpha"]["historyStatus"], "error")
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertEqual(data["Alpha"]["freshnessSource"], gb.SOURCE_UPDATED)
        # There is no count to show — which is exactly why the inspector has
        # to key off the status and not off the count.
        self.assertIsNone(data["Alpha"]["revisions"])

    def test_a_non_list_history_payload_is_partial_and_claims_no_revisions(self):
        payload = self.run_mode("versions-not-list")
        data = {node["data"]["id"]: node["data"] for node in payload["nodes"]}
        self.assertEqual(payload["gbrain"]["status"], "partial")
        self.assertEqual(payload["gbrain"]["history"]["failed"], 6)
        self.assertIn("could not be read for 6 page(s)", " ".join(payload["gbrain"]["gaps"]))
        self.assertEqual(data["Alpha"]["historyStatus"], "error")
        self.assertEqual(data["Alpha"]["updated"], "2026-06-10T12:00:00+00:00")
        self.assertIsNone(data["Alpha"]["revisions"])
        self.assertFalse(payload["revisions"]["available"])

    def test_required_turns_a_failure_into_an_error_instead(self):
        for mode in ("no-start", "handshake-error", "handshake-bad-info", "pages-error"):
            with self.subTest(mode=mode):
                with self.assertRaises(RuntimeError):
                    self.run_mode(mode, required=True)

    def test_required_also_refuses_a_read_that_only_half_worked(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_mode("die-in-history", required=True)
        self.assertIn("incomplete", str(caught.exception))

    def test_required_refuses_every_way_one_page_history_can_fail(self):
        """Per-page history failures are exactly the states a required build
        must not paper over: the timestamps survive, the confirmation does
        not, and `--gbrain-required` asked for confirmation."""
        for mode in ("versions-error", "versions-not-list"):
            with self.subTest(mode=mode):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_mode(mode, required=True)
                self.assertIn("incomplete", str(caught.exception))
                self.assertIn("could not be read", str(caught.exception))


# ── Markdown-only mode is untouched ──────────────────────────────────────────
class MarkdownOnlyTests(unittest.TestCase):
    def payload(self):
        with tempfile.TemporaryDirectory() as folder:
            return build_map.build(write_vault(Path(folder, "vault")), "Test", as_of=AS_OF)

    def test_no_adapter_means_no_backend_fields_and_an_honest_label(self):
        payload = self.payload()
        self.assertEqual(payload["gbrain"]["status"], "off")
        self.assertFalse(payload["gbrain"]["enabled"])
        self.assertIn("Markdown only", payload["gbrain"]["detail"])
        for node in payload["nodes"]:
            self.assertNotIn("brainSlug", node["data"])
            self.assertNotIn("revisions", node["data"])
            self.assertNotIn("slugHint", node["data"])

    def test_the_revision_overlay_is_absent_rather_than_empty(self):
        payload = self.payload()
        self.assertFalse(payload["revisions"]["available"])
        self.assertEqual(payload["revisions"]["events"], 0)

    def test_markdown_precedence_is_unchanged(self):
        data = {node["data"]["id"]: node["data"] for node in self.payload()["nodes"]}
        self.assertEqual(data["Alpha"]["freshnessSource"], "last_updated")
        self.assertEqual(data["Gamma"]["freshnessSource"], "updated")
        self.assertEqual(data["Delta"]["freshnessSource"], "created")


# ── CLI ──────────────────────────────────────────────────────────────────────
def run_cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "build_map.py"), *args],
                          capture_output=True, text=True)


class CliTests(unittest.TestCase):
    def test_gbrain_build_reports_matches_and_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain-history",
                           "--gbrain-cmd", fake_gbrain(folder))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertIn("gbrain :", proc.stdout)
        self.assertIn("6/7 notes matched", proc.stdout)
        self.assertIn("3 revisions across 2 pages", proc.stdout)
        self.assertIn("gbrain_history", html)

    def test_an_unavailable_backend_warns_but_still_writes_the_map(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain",
                           "--gbrain-cmd", fake_gbrain(folder, mode="no-start"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.exists())
        self.assertIn("warning:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_required_refuses_the_build_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain", "--gbrain-required",
                           "--gbrain-cmd", fake_gbrain(folder, mode="no-start"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("Nothing was written", proc.stderr)
        self.assertFalse(out.exists())

    def test_required_rejects_malformed_handshake_metadata_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain",
                           "--gbrain-required", "--gbrain-cmd",
                           fake_gbrain(folder, mode="handshake-bad-info"))
            self.assertFalse(out.exists())
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("Nothing was written", proc.stderr)

    def test_a_partial_read_warns_on_stderr_and_labels_the_summary_line(self):
        brain = {"pages": [
            {"slug": "notes/alpha", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/alpha", "source_id": "b", "updated_at": "2026-06-01T00:00:00Z"}],
            "versions": {}}
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain",
                           "--gbrain-cmd", fake_gbrain(folder, brain=brain))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.exists(), "a partial read still writes the Markdown map")
        self.assertIn("PARTIAL", proc.stdout)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("more than one source", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_required_refuses_a_partial_read_and_writes_nothing(self):
        brain = {"pages": [
            {"slug": "notes/alpha", "source_id": "a", "updated_at": "2026-06-01T00:00:00Z"},
            {"slug": "notes/alpha", "source_id": "b", "updated_at": "2026-06-01T00:00:00Z"}],
            "versions": {}}
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain", "--gbrain-required",
                           "--gbrain-cmd", fake_gbrain(folder, brain=brain))
            self.assertFalse(out.exists())
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("incomplete", proc.stderr)
        self.assertIn("Nothing was written", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_history_budget_that_stopped_short_is_warned_about_not_hidden(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain-history",
                           "--gbrain-history-limit", "2",
                           "--gbrain-cmd", fake_gbrain(folder))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.exists(), "a budgeted read still writes the map")
        self.assertIn("PARTIAL", proc.stdout)
        self.assertIn("history read for 2/6", proc.stdout)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("--gbrain-history-limit 2 read the 2 freshest of 6", proc.stderr)
        self.assertIn("--gbrain-history-limit 0", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_required_refuses_a_budget_that_did_not_cover_every_page(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            command = fake_gbrain(folder)
            short = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain-history",
                            "--gbrain-history-limit", "2", "--gbrain-required",
                            "--gbrain-cmd", command)
            self.assertNotEqual(short.returncode, 0)
            self.assertFalse(out.exists(), "nothing is written when the budget fell short")
            full = run_cli(vault, str(out), "--as-of", AS_OF, "--gbrain-history",
                           "--gbrain-history-limit", "0", "--gbrain-required",
                           "--gbrain-cmd", command)
            self.assertEqual(full.returncode, 0, full.stderr)
            self.assertTrue(out.exists(), "an uncapped read covers every page")
        self.assertIn("incomplete", short.stderr)
        self.assertIn("--gbrain-history-limit", short.stderr)
        self.assertIn("Nothing was written", short.stderr)
        self.assertNotIn("Traceback", short.stderr)

    def test_tuning_flags_without_the_adapter_are_refused_not_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            proc = run_cli(vault, str(Path(folder, "out.html")), "--gbrain-source", "wiki")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--gbrain-source", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_negative_history_budget_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            proc = run_cli(vault, str(Path(folder, "out.html")), "--gbrain",
                           "--gbrain-history-limit", "-1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--gbrain-history-limit", proc.stderr)

    def test_a_markdown_only_build_never_mentions_gbrain_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            out = Path(folder, "out.html")
            proc = run_cli(vault, str(out), "--as-of", AS_OF)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("gbrain :", proc.stdout)


# ── template contract ────────────────────────────────────────────────────────
class TemplateContractTests(unittest.TestCase):
    """The map's JavaScript has to be able to name every state Python emits."""

    TEMPLATE = build_map.TEMPLATE

    def literal(self, name):
        match = re.search(r"const %s = \{(.*?)\};" % name, self.TEMPLATE, re.S)
        self.assertIsNotNone(match, "%s literal not found in the template" % name)
        return set(re.findall(r"(?:^|[\s{])'?([A-Za-z_][\w-]*)'?\s*:", match.group(1)))

    def test_every_gbrain_provenance_key_has_a_label(self):
        labels = self.literal("SOURCE_LABEL")
        for key in (gb.SOURCE_HISTORY, gb.SOURCE_REVISION, gb.SOURCE_UPDATED):
            with self.subTest(key=key):
                self.assertIn(key, labels)

    def test_every_history_status_python_can_emit_has_a_label(self):
        emitted = {
            gb.classify("2026-06-10T12:00:00Z", None)["status"],
            gb.classify("2026-06-10T12:00:00Z", {"revisions": 0, "last": "", "error": ""})["status"],
            gb.classify("2026-06-10T12:00:00Z",
                        {"revisions": 1, "last": "2026-06-10T12:00:00Z", "error": ""})["status"],
            gb.classify("2026-06-10T12:00:00Z",
                        {"revisions": 1, "last": "2026-01-01T00:00:00Z", "error": ""})["status"],
            gb.classify("2026-06-10T12:00:00Z",
                        {"revisions": 1, "last": "2026-07-01T00:00:00Z", "error": ""})["status"],
            gb.classify("2026-06-10T12:00:00Z", {"revisions": 0, "last": "", "error": "x"})["status"],
        }
        self.assertEqual(emitted, {"unverified", "single-write", "verified",
                                   "superseded", "inconsistent", "error"})
        self.assertEqual(emitted - self.literal("HISTORY_NOTE"), set())

    def test_the_inspector_states_revisions_and_non_content_touches(self):
        for label in ("'Revisions'", "'Backend touch'", "'Last revision'"):
            with self.subTest(label=label):
                self.assertIn(label, self.TEMPLATE)

    def test_a_failed_page_history_is_shown_even_though_the_count_is_null(self):
        """`classify` returns revisions=None for a page whose history read
        failed, so a count-only guard would hide exactly the case the reader
        most needs to see."""
        verdict = gb.classify("2026-06-10T12:00:00Z",
                              {"revisions": 0, "last": "", "error": "no such page"})
        self.assertIsNone(verdict["revisions"])
        block = re.search(r"const HS = d\.historyStatus.*?fact\(dl, 'Revisions'",
                          self.TEMPLATE, re.S)
        self.assertIsNotNone(block, "the Revisions fact no longer reads the status")
        guard = re.search(r"\bif\((.+?)\)\{", block.group(0), re.S)
        self.assertIsNotNone(guard, "the Revisions fact is not behind a single if()")
        self.assertIn("'%s'" % verdict["status"], guard.group(1),
                      "the guard must let a null count through on a failed read")
        self.assertIn("HISTORY_NOTE[HS]", self.TEMPLATE)

    def test_a_partial_read_is_labelled_in_the_header_not_passed_off_as_clean(self):
        tag = self.literal("GB_TAG")
        self.assertEqual(tag, {"ok", "partial"})
        self.assertIn("gbrain (partial)", self.TEMPLATE)

    def test_the_inspector_renders_each_history_state_the_adapter_can_produce(self):
        """The same snippet the map ships, run against the payloads Python
        actually emits — including the failed read, whose count is null."""
        checker = shutil.which("node")
        if not checker:
            self.skipTest("node is not installed")
        labels = re.search(r"const HISTORY_NOTE = \{.*?\};", self.TEMPLATE, re.S)
        block = re.search(r"(  const HS = d\.historyStatus.*?)\n  fact\(dl, 'Created'",
                          self.TEMPLATE, re.S)
        self.assertIsNotNone(labels)
        self.assertIsNotNone(block)
        cases = [
            {"revisions": None, "historyStatus": "error",
             "updated": "U", "brainUpdated": "U", "lastRevision": ""},
            {"revisions": 2, "historyStatus": "verified",
             "updated": "U", "brainUpdated": "U", "lastRevision": "U"},
            {"revisions": 1, "historyStatus": "inconsistent",
             "updated": "U", "brainUpdated": "U", "lastRevision": "L"},
            {"revisions": None, "historyStatus": "unverified",
             "updated": "U", "brainUpdated": "U", "lastRevision": ""},
        ]
        script = ("%s\nconst dl = null;\nfunction fmtStamp(s){ return 'STAMP:' + s; }\n"
                  "let rows = [];\n"
                  "function fact(dl, label, value, tone){ rows.push([label, value, tone||null]); }\n"
                  "console.log(JSON.stringify(%s.map(d => { rows = [];\n%s\nreturn rows; })));"
                  % (labels.group(0), json.dumps(cases), block.group(1)))
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "inspect.js")
            source.write_text(script, encoding="utf-8")
            proc = subprocess.run([checker, str(source)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        failed, verified, inconsistent, unread = json.loads(proc.stdout)
        self.assertEqual(failed, [["Revisions", "page history unavailable for this note",
                                   "flag"]], "a failed read must not render as a blank")
        self.assertEqual(verified[0][1], "2 revisions \u2014 confirmed by GBrain page history")
        self.assertEqual(inconsistent[0][0], "Revisions")
        self.assertEqual(inconsistent[1], ["Last revision",
                                           "STAMP:L \u2014 newer than the backend timestamp",
                                           "flag"])
        self.assertEqual(unread, [], "history that was never read stays quiet")

    def test_the_revision_overlay_only_feeds_numbers_to_innerhtml(self):
        overlay = re.search(r"if\(REV\)\{(.*?)\n  \}", self.TEMPLATE, re.S)
        self.assertIsNotNone(overlay)
        self.assertNotIn("textContent", overlay.group(1))
        self.assertIn("toFixed", overlay.group(1))

    def test_the_generated_javascript_parses(self):
        checker = shutil.which("node")
        if not checker:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as folder:
            vault = write_vault(Path(folder, "vault"))
            payload = build_map.build(vault, "Test", as_of=AS_OF,
                                      gbrain=options(fake_gbrain(folder)))
            html = build_map.render(payload)
            script = max(re.findall(r"<script>(.*?)</script>", html, re.S), key=len)
            source = Path(folder, "map.js")
            source.write_text(script, encoding="utf-8")
            proc = subprocess.run([checker, "--check", str(source)],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ── conformance with a real, installed gbrain ────────────────────────────────
@unittest.skipUnless(shutil.which("gbrain"), "gbrain is not installed")
class InstalledGBrainSchemaTests(unittest.TestCase):
    """Check the adapter's requests against the installed gbrain's own schemas.

    `gbrain --tools-json` is pure tool discovery — it prints the catalog and
    touches no brain data — so this stays a read-only test. It skips when the
    binary is absent or the catalog cannot be read (an environment problem),
    and fails when the schemas and the adapter actually disagree.
    """

    SENT = {"list_pages": {"limit": 100, "sort": "updated_asc",
                           "updated_after": "2026-01-01T00:00:00Z", "source_id": "__all__"},
            "get_versions": {"slug": "example"}}
    JSON_TYPES = {str: "string", int: "number", float: "number", bool: "boolean"}

    @classmethod
    def setUpClass(cls):
        try:
            proc = subprocess.run(["gbrain", "--tools-json"], capture_output=True,
                                  text=True, timeout=120)
            cls.tools = {tool["name"]: tool for tool in json.loads(proc.stdout)}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise unittest.SkipTest("cannot read the gbrain tool catalog: %s" % exc)

    def test_the_read_tools_the_adapter_needs_exist(self):
        for tool in self.SENT:
            with self.subTest(tool=tool):
                self.assertIn(tool, self.tools)

    def test_every_argument_the_adapter_sends_is_in_the_schema(self):
        for tool, arguments in self.SENT.items():
            schema = self.tools[tool]["schema"]
            for key, value in arguments.items():
                with self.subTest(tool=tool, argument=key):
                    self.assertIn(key, schema["properties"])
                    declared = schema["properties"][key]
                    if declared.get("enum"):
                        self.assertIn(value, declared["enum"])
                    if declared.get("type"):
                        self.assertEqual(declared["type"], self.JSON_TYPES[type(value)])

    def test_no_required_argument_is_left_out(self):
        for tool, arguments in self.SENT.items():
            with self.subTest(tool=tool):
                self.assertEqual(
                    [key for key in self.tools[tool]["schema"].get("required", [])
                     if key not in arguments], [])

    def test_the_adapter_never_names_a_mutating_tool(self):
        source = (ROOT / "scripts" / "gbrain_history.py").read_text(encoding="utf-8")
        mutating = [name for name in self.tools if name.split("_")[0] in {
            "put", "delete", "add", "remove", "revert", "restore", "purge",
            "submit", "forget", "capture", "remember", "sync", "migrate", "reload"}]
        self.assertTrue(mutating, "the catalog should contain write tools")
        named = [name for name in mutating if repr(name) in source or '"%s"' % name in source]
        self.assertEqual(named, [])


# ── documentation ────────────────────────────────────────────────────────────
class DocumentationTests(unittest.TestCase):
    DOC = ROOT / "docs" / "gbrain-history.md"

    def test_the_semantics_document_exists_where_the_code_points(self):
        self.assertTrue(self.DOC.exists(), "scripts/gbrain_history.py cites this file")
        self.assertIn("docs/gbrain-history.md",
                      (ROOT / "scripts" / "gbrain_history.py").read_text(encoding="utf-8"))

    def test_it_names_the_surface_it_reads_and_the_semantics_it_relies_on(self):
        text = self.DOC.read_text(encoding="utf-8")
        for token in ["gbrain serve", "list_pages", "get_versions", "page_versions",
                      "snapshot_at", "updated_at", "createVersion", "revert_version",
                      "read-only", "replay", "diff"]:
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_it_states_the_batching_limitation(self):
        text = self.DOC.read_text(encoding="utf-8").lower()
        self.assertIn("limitation", text)
        self.assertIn("one call per page", text)

    def test_it_states_the_multi_source_rule_and_the_partial_contract(self):
        text = self.DOC.read_text(encoding="utf-8")
        for token in ["more than one source", "--gbrain-source", "__all__",
                      "diagnostics.ambiguous", "diagnostics.foreign",
                      "partial", "--gbrain-required", "per note"]:
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_it_documents_the_budget_as_incomplete_and_how_to_get_full_coverage(self):
        doc = self.DOC.read_text(encoding="utf-8")
        for token in ["default 500", "--gbrain-history-limit 0", "skipped",
                      "The default budget makes a large brain a `partial` read"]:
            with self.subTest(doc="docs/gbrain-history.md", token=token):
                self.assertIn(token, doc)
        for name in ("README.md", "SKILL.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(doc=name):
                self.assertIn("--gbrain-history-limit 0", text)
                self.assertIn("500", text)

    def test_it_documents_the_brain_wide_collision_audit(self):
        doc = self.DOC.read_text(encoding="utf-8")
        for token in ["source_id='__all__'", "collision", "proven",
                      "read-only", "unverified"]:
            with self.subTest(doc="docs/gbrain-history.md", token=token):
                self.assertIn(token, doc)
        for name in ("README.md", "SKILL.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(doc=name):
                self.assertIn("__all__", text)
                self.assertIn("get_versions", text)

    def test_no_document_claims_bodies_vanish_before_they_are_decoded(self):
        """MCP cannot stream a reply, so "discarded on arrival" was never true:
        the rows are decoded, then projected. Say the true thing."""
        for name in ("README.md", "SKILL.md", "docs/gbrain-history.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(doc=name):
                for claim in ("discarded on arrival", "dropped on arrival",
                              "streams the", "streamed in"):
                    self.assertNotIn(claim, text)
                self.assertIn("projected to timestamps", text)
        doc = self.DOC.read_text(encoding="utf-8")
        self.assertIn("MAX_REPLY_CHARS", doc)          # the bound is named
        self.assertIn("cannot be streamed", doc)       # and streaming is denied, not claimed

    def test_the_readme_and_skill_document_the_flags(self):
        for name in ("README.md", "SKILL.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(doc=name):
                self.assertIn("--gbrain", text)
                self.assertIn("--gbrain-history", text)


if __name__ == "__main__":
    unittest.main()

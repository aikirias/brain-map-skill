"""Tests for scripts/build_map.py — freshness bands, payload shape, runtime
inlining/refusal, template safety contracts and CLI behaviour.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_map.py"
SPEC = importlib.util.spec_from_file_location("build_map", SCRIPT)
build_map = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_map)

AS_OF = "2026-06-15"
CDN_URL = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js"


def write_vault(folder, files):
    """files: {relative path: text}. Returns the folder path."""
    for rel, text in files.items():
        target = Path(folder, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return folder


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def fake_runtime(path, marker="/*cytoscape-test-bundle*/"):
    """A fixture with the size and distribution markers of an official browser bundle."""
    body = (
        "/** Copyright (c) 2016-2024, The Cytoscape Consortium. */\n"
        + marker
        + "\n!function(e,t){module.exports=t();e.cytoscape=t()}(this,function(){/*"
        + ("x" * 100_000)
        + "*/});\n"
    )
    Path(path).write_text(body, encoding="utf-8")
    return str(path)


class TimestampTests(unittest.TestCase):
    def test_parse_timestamp_accepts_date_and_iso_zulu(self):
        self.assertEqual(build_map.parse_timestamp("2026-06-12"), "2026-06-12T00:00:00+00:00")
        self.assertEqual(build_map.parse_timestamp("2026-06-12T09:45:00Z"), "2026-06-12T09:45:00+00:00")

    def test_parse_timestamp_rejects_garbage(self):
        self.assertEqual(build_map.parse_timestamp("tomorrow"), "")
        self.assertEqual(build_map.parse_timestamp(""), "")

    def test_invalid_as_of_raises_valueerror(self):
        with self.assertRaises(ValueError):
            build_map.parse_as_of("tomorrow")


class FreshnessBandTests(unittest.TestCase):
    """Exactly five bands: fresh <=7, recent <=30, settled <=90, dormant <=365,
    oxidized >365. `unknown` only when there is no date at all."""

    def test_band_order_is_the_five_agreed_bands_plus_unknown(self):
        self.assertEqual(list(build_map.FRESHNESS_ORDER),
                         ["fresh", "recent", "settled", "dormant", "oxidized", "unknown"])

    def test_band_boundaries(self):
        cases = [(0, "fresh"), (7, "fresh"), (8, "recent"), (30, "recent"),
                 (31, "settled"), (90, "settled"), (91, "dormant"), (365, "dormant"),
                 (366, "oxidized"), (5000, "oxidized")]
        for age, expected in cases:
            with self.subTest(age=age):
                self.assertEqual(build_map.freshness_band(age), expected)

    def test_band_is_unknown_only_without_a_date(self):
        self.assertEqual(build_map.freshness_band(None), "unknown")
        band, age, score = build_map.freshness_for("", build_map.parse_as_of(AS_OF))
        self.assertEqual(band, "unknown")
        self.assertIsNone(age)
        self.assertIsNone(score)

    def test_freshness_for_returns_band_age_and_score(self):
        as_of = build_map.parse_as_of(AS_OF)
        band, age, score = build_map.freshness_for("2026-06-10T00:00:00+00:00", as_of)
        self.assertEqual(band, "fresh")
        self.assertEqual(age, 5)
        self.assertGreater(score, 0.9)

    def test_score_is_continuous_normalized_and_decreasing(self):
        self.assertEqual(build_map.freshness_score(0), 1.0)
        ages = [0, 1, 7, 8, 30, 31, 90, 91, 365, 366, 1000, 5000]
        scores = [build_map.freshness_score(a) for a in ages]
        for age, score in zip(ages, scores):
            with self.subTest(age=age):
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)
        for older, newer in zip(scores[1:], scores[:-1]):
            self.assertLess(older, newer)
        self.assertLess(scores[-1], 0.05)  # approaches 0 with age
        self.assertIsNone(build_map.freshness_score(None))

    def test_score_distinguishes_notes_inside_one_band(self):
        """Bands are filters/labels; the score stays continuous within a band."""
        self.assertNotEqual(build_map.freshness_score(400), build_map.freshness_score(900))


class MetadataTests(unittest.TestCase):
    def test_load_uses_last_updated_alias_before_modified_and_created(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"note.md":
                "---\ncreated: 2024-01-01\nmodified: 2025-02-02\nlast_updated: 2026-03-03\n---\n# Note\n"})
            nodes, _ = build_map.load(folder)
        node = nodes["Note"]
        self.assertEqual(node["updated"], "2026-03-03T00:00:00+00:00")
        self.assertEqual(node["freshnessSource"], "last_updated")

    def test_gbrain_slug_wikilinks_resolve_and_frontmatter_type_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {
                "projects/hermes-gbrain.md":
                    "---\ntype: project\n---\n# Hermes and GBrain\n[[people/alejandro-kirias]]\n",
                "people/alejandro-kirias.md":
                    "---\ntype: person\n---\n# Alejandro Kirias\n",
            })
            nodes, edges = build_map.load(folder)
        self.assertEqual(nodes["Hermes and GBrain"]["type"], "project")
        self.assertEqual(nodes["Alejandro Kirias"]["type"], "person")
        self.assertEqual(edges, [{"source": "Hermes and GBrain", "target": "Alejandro Kirias"}])

    def test_gbrain_types_have_distinct_supported_shapes(self):
        for note_type in ("person", "company", "project", "pattern", "reflection", "idea", "atom"):
            with self.subTest(note_type=note_type):
                self.assertIn(note_type, build_map.TYPE_SHAPES)

    def test_created_only_note_reports_created_as_freshness_source(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"note.md": "---\ncreated: 2025-05-05\n---\n# Note\n"})
            nodes, _ = build_map.load(folder)
        self.assertEqual(nodes["Note"]["freshnessSource"], "created")
        self.assertEqual(nodes["Note"]["updated"], "2025-05-05T00:00:00+00:00")

    def test_dateless_note_falls_back_to_filesystem_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"note.md": "# Note\nno frontmatter\n"})
            nodes, _ = build_map.load(folder)
        node = nodes["Note"]
        self.assertEqual(node["freshnessSource"], "filesystem")
        self.assertTrue(node["updated"], "a meaningful updated timestamp is required")


class PayloadTests(unittest.TestCase):
    VAULT = {
        "Work/Ancient.md": "---\nupdated: 2024-01-01\n---\n# Ancient\n[[Hub]]\n",
        "Work/Dormant.md": "---\nupdated: 2025-10-01\n---\n# Dormant\n[[Hub]]\n",
        "Work/Settled.md": "---\nupdated: 2026-04-01\n---\n# Settled\n[[Hub]]\n",
        "Work/Recent.md": "---\nupdated: 2026-06-01\n---\n# Recent\n[[Hub]]\n",
        "Work/Fresh.md": "---\nupdated: 2026-06-12\n---\n# Fresh\n[[Hub]]\n",
        "Work/Hub.md": "---\nupdated: 2026-06-14\ncreated: 2023-01-01\ntags: [index]\n---\n# Hub\n",
        # created-only and unlinked: a recent orphan, so `orphans` and `oxidized`
        # stay independent and their union is observable
        "Life/Lone.md": "---\ncreated: 2026-06-05\n---\n# Lone\n",
    }

    def payload(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, self.VAULT)
            return build_map.build(folder, "Test", as_of=AS_OF)

    def test_every_band_is_assigned_from_the_reference_date(self):
        data = {n["data"]["id"]: n["data"] for n in self.payload()["nodes"]}
        self.assertEqual(data["Fresh"]["freshness"], "fresh")
        self.assertEqual(data["Recent"]["freshness"], "recent")
        self.assertEqual(data["Settled"]["freshness"], "settled")
        self.assertEqual(data["Dormant"]["freshness"], "dormant")
        self.assertEqual(data["Ancient"]["freshness"], "oxidized")
        self.assertEqual(data["Lone"]["freshness"], "recent")  # banded from `created`

    def test_nodes_carry_score_age_degree_and_orphan_flag(self):
        data = {n["data"]["id"]: n["data"] for n in self.payload()["nodes"]}
        self.assertEqual(data["Fresh"]["ageDays"], 3)
        self.assertGreater(data["Fresh"]["freshnessScore"], data["Ancient"]["freshnessScore"])
        self.assertTrue(data["Lone"]["orphan"])
        self.assertFalse(data["Hub"]["orphan"])
        self.assertEqual(data["Hub"]["deg"], 5)
        self.assertEqual(data["Hub"]["freshnessSource"], "updated")
        self.assertEqual(data["Hub"]["created"], "2023-01-01T00:00:00+00:00")

    def test_stats_report_counts_for_all_six_bands(self):
        stats = self.payload()["stats"]
        self.assertEqual(list(stats["freshness"]), list(build_map.FRESHNESS_ORDER))
        self.assertEqual(stats["freshness"]["fresh"], 2)     # Fresh + Hub
        self.assertEqual(stats["freshness"]["recent"], 2)    # Recent + Lone
        self.assertEqual(stats["freshness"]["settled"], 1)
        self.assertEqual(stats["freshness"]["dormant"], 1)
        self.assertEqual(stats["freshness"]["oxidized"], 1)
        self.assertEqual(stats["freshness"]["unknown"], 0)
        self.assertEqual(sum(stats["freshness"].values()), stats["nodes"])

    def test_audit_counts_oxidized_orphans_and_their_union(self):
        audit = self.payload()["stats"]["audit"]
        self.assertEqual(audit["oxidized"], 1)
        self.assertEqual(audit["orphans"], 1)
        self.assertEqual(audit["flagged"], 2)

    def test_as_of_is_recorded_and_freshness_order_exported(self):
        payload = self.payload()
        self.assertTrue(payload["stats"]["asOf"].startswith("2026-06-15"))
        self.assertEqual(list(payload["freshnessOrder"]), list(build_map.FRESHNESS_ORDER))

    def test_default_source_label_does_not_leak_absolute_vault_path(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder, "private-brain")
            vault.mkdir()
            write_vault(vault, {"note.md": "# Note\n"})
            payload = build_map.build(str(vault), "Test", as_of=AS_OF)
        self.assertEqual(payload["source"], "private-brain")
        self.assertNotIn(str(vault), str(payload))

    def test_timeline_still_buckets_by_month_and_theme(self):
        payload = self.payload()
        self.assertTrue(payload["timeline"])
        for row in payload["timeline"]:
            self.assertRegex(row["month"], r"^\d{4}-\d{2}$")
            self.assertIn("Work", row)
            self.assertIn("Life", row)


class TemplateContractTests(unittest.TestCase):
    """The inspector must state the freshness facts, and note-derived text must
    never reach the DOM through innerHTML."""

    INSPECTOR_LABELS = ["Freshness", "Age", "Updated", "Source", "Created", "Links", "Status"]

    def test_inspector_labels_present(self):
        for label in self.INSPECTOR_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, build_map.TEMPLATE)

    def test_only_the_timeline_svg_uses_innerhtml(self):
        targets = set(re.findall(r"(\w+)\.innerHTML", build_map.TEMPLATE))
        self.assertEqual(targets - {"svg"}, set(),
                         "note-derived data must be written with textContent/DOM nodes")

    def test_no_emoji_chrome_in_the_shell(self):
        for glyph in ["🗄", "▶", "❚", "◌"]:
            with self.subTest(glyph=glyph):
                self.assertNotIn(glyph, build_map.TEMPLATE)

    def test_reduced_motion_and_responsive_rules_present(self):
        self.assertIn("prefers-reduced-motion", build_map.TEMPLATE)
        self.assertIn("@media (max-width:", build_map.TEMPLATE.replace("@media (max-width: ", "@media (max-width:"))


class RuntimeTests(unittest.TestCase):
    def build_html(self, folder, **kw):
        payload = build_map.build(folder, "Test", as_of=AS_OF)
        return payload, build_map.render(payload, **kw)

    def test_default_uses_pinned_cdn_and_labels_network_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": "---\ncreated: 2026-06-01\n---\n# A\n"})
            payload, html = self.build_html(folder)
        self.assertEqual(payload["runtime"]["mode"], "network")
        self.assertIn(CDN_URL, html)
        self.assertIn("Network runtime", html)

    def test_local_runtime_is_inlined_and_drops_the_cdn(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": "---\ncreated: 2026-06-01\n---\n# A\n"})
            path = fake_runtime(Path(folder, "cytoscape.min.js"))
            runtime = build_map.read_local_runtime(path)
            payload = build_map.build(folder, "Test", as_of=AS_OF)
            html = build_map.render(payload, runtime=runtime)
        self.assertEqual(payload["runtime"]["mode"], "local")
        self.assertNotIn(CDN_URL, html)
        self.assertNotIn("<script src=", html)
        self.assertIn("/*cytoscape-test-bundle*/", html)
        self.assertIn("Local runtime", html)

    def test_inlined_runtime_escapes_closing_script_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": "---\ncreated: 2026-06-01\n---\n# A\n"})
            path = Path(folder, "cytoscape.min.js")
            fake_runtime(path, "/*fixture*/var s='</script>';/*fixture-end*/")
            runtime = build_map.read_local_runtime(str(path))
            html = build_map.render(build_map.build(folder, "T", as_of=AS_OF), runtime=runtime)
        self.assertIn(r"'<\/script>'", html)
        self.assertNotIn("var s='</script>'", html)

    def test_read_local_runtime_rejects_missing_tiny_and_html_files(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder, "nope.js"))
            tiny = Path(folder, "tiny.js"); tiny.write_text("var cytoscape=1;", encoding="utf-8")
            page = Path(folder, "page.js")
            page.write_text("<!DOCTYPE html><html>cytoscape " + "x" * 4096 + "</html>", encoding="utf-8")
            for bad in (missing, str(tiny), str(page)):
                with self.subTest(path=bad):
                    with self.assertRaises(build_map.RuntimeUnavailable):
                        build_map.read_local_runtime(bad)

    def test_read_local_runtime_rejects_prose_that_merely_mentions_cytoscape(self):
        """A doc file is big enough and says "cytoscape" — it is still not a runtime."""
        with tempfile.TemporaryDirectory() as folder:
            doc = Path(folder, "README.md")
            doc.write_text("# brain-map\n\nPass --cytoscape-js to inline cytoscape.\n" + "prose. " * 400,
                           encoding="utf-8")
            prose_js = Path(folder, "notes.js")
            prose_js.write_text("cytoscape notes, no code here at all.\n" + "words " * 400,
                                encoding="utf-8")
            for bad in (doc, prose_js):
                with self.subTest(path=bad.name):
                    with self.assertRaises(build_map.RuntimeUnavailable):
                        build_map.read_local_runtime(str(bad))

    def test_read_local_runtime_rejects_function_stub_that_cannot_power_a_graph(self):
        with tempfile.TemporaryDirectory() as folder:
            stub = Path(folder, "cytoscape.min.js")
            stub.write_text("var cytoscape = function(){};/*" + "x" * 2048 + "*/",
                            encoding="utf-8")
            with self.assertRaises(build_map.RuntimeUnavailable):
                build_map.read_local_runtime(str(stub))

    def test_read_local_runtime_accepts_a_plausible_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = fake_runtime(Path(folder, "cytoscape.min.js"))
            self.assertIn("cytoscape", build_map.read_local_runtime(path))


class KeepPositionsTests(unittest.TestCase):
    """--keep-positions: read the old sky back, keep every surviving note where
    it was, place new notes beside their neighbours, and refuse junk payloads."""

    VAULT = {
        "Work/Hub.md": "---\nupdated: 2026-06-14\n---\n# Hub\n[[Anchor]]\n[[Far]]\n",
        "Work/Anchor.md": "---\nupdated: 2026-06-10\n---\n# Anchor\n",
        "Work/Far.md": "---\nupdated: 2026-05-01\n---\n# Far\n",
    }

    def map_file(self, folder, vault=None, name="vault", **kw):
        """Build a real map HTML from a vault of its own; returns (path, payload).

        Each vault gets its own directory so a "rebuild after edits" really does
        drop deleted notes instead of quietly reusing the previous files.
        """
        source = Path(folder, name)
        source.mkdir(parents=True, exist_ok=True)
        write_vault(source, vault or self.VAULT)
        payload = build_map.build(str(source), "Test", as_of=AS_OF, **kw)
        out = Path(folder, name + ".html")
        out.write_text(build_map.render(payload), encoding="utf-8")
        return out, payload

    def payload_positions(self, payload):
        return {n["data"]["id"]: (n["position"]["x"], n["position"]["y"])
                for n in payload["nodes"]}

    def write_map_with_nodes(self, path, nodes):
        """A minimal file shaped like a generated map, with a chosen node list."""
        data = json.dumps({"title": "T", "nodes": nodes, "edges": []})
        path.write_text("<html><script>\nconst DATA = " + data + ";\n</script></html>",
                        encoding="utf-8")
        return str(path)

    # ── parsing ──────────────────────────────────────────────────────────────
    def test_read_positions_round_trips_a_generated_map(self):
        with tempfile.TemporaryDirectory() as folder:
            out, payload = self.map_file(folder)
            kept = build_map.read_positions(str(out))
        self.assertEqual(set(kept), {"Hub", "Anchor", "Far"})
        self.assertEqual(kept, self.payload_positions(payload))
        for nid, (x, y) in kept.items():
            with self.subTest(node=nid):
                self.assertIsInstance(x, float)
                self.assertIsInstance(y, float)

    def test_read_positions_skips_nodes_without_usable_coordinates(self):
        """A partly readable map still yields the notes it can place."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "map.html")
            self.write_map_with_nodes(path, [
                {"data": {"id": "Good"}, "position": {"x": 10.5, "y": -20.25}},
                {"data": {"id": "NoPosition"}},
                {"data": {"id": "HalfPosition"}, "position": {"x": 3}},
                {"data": {"id": "PositionIsAList"}, "position": [1, 2]},
                {"data": "not-a-dict", "position": {"x": 1, "y": 2}},
                "not-a-node",
            ])
            kept = build_map.read_positions(str(path))
        self.assertEqual(kept, {"Good": (10.5, -20.25)})

    # ── surviving positions ──────────────────────────────────────────────────
    def test_surviving_notes_keep_their_exact_positions_on_rebuild(self):
        """Deleting a note must not reshuffle the sky around the survivors."""
        with tempfile.TemporaryDirectory() as folder:
            out, first = self.map_file(folder)
            kept = build_map.read_positions(str(out))
            shrunk = {k: v for k, v in self.VAULT.items() if k != "Work/Far.md"}
            second = self.map_file(folder, vault=shrunk, name="after",
                                   keep_positions=kept)[1]
        before, after = self.payload_positions(first), self.payload_positions(second)
        self.assertEqual(set(after), {"Hub", "Anchor"})
        self.assertEqual(after["Hub"], before["Hub"])
        self.assertEqual(after["Anchor"], before["Anchor"])
        self.assertEqual(second["layout"], "preset")

    def test_keeping_positions_forces_a_preset_layout_even_without_networkx(self):
        with tempfile.TemporaryDirectory() as folder:
            out, _ = self.map_file(folder)
            kept = build_map.read_positions(str(out))
            payload = build_map.build(str(Path(folder, "vault")), "Test", as_of=AS_OF,
                                      keep_positions=kept)
        self.assertEqual(payload["layout"], "preset")

    def test_positions_from_an_unrelated_map_are_ignored_for_unknown_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            _, plain = self.map_file(folder)
            stale = {"Someone Else's Note": (999.0, -999.0)}
            payload = build_map.build(str(Path(folder, "vault")), "Test", as_of=AS_OF,
                                      keep_positions=stale)
        self.assertEqual(self.payload_positions(payload), self.payload_positions(plain))
        self.assertEqual(payload["layout"], plain["layout"])

    # ── new nodes ────────────────────────────────────────────────────────────
    def rebuild_with_new_note(self, folder, note_body, name="New Note.md"):
        """(old positions, positions after adding one note and rebuilding)."""
        out, _ = self.map_file(folder)
        kept = build_map.read_positions(str(out))
        grown = dict(self.VAULT)
        grown["Work/" + name] = note_body
        second = self.map_file(folder, vault=grown, name="after", keep_positions=kept)[1]
        return kept, self.payload_positions(second)

    def test_a_new_linked_note_lands_beside_its_placed_neighbour(self):
        with tempfile.TemporaryDirectory() as folder:
            kept, after = self.rebuild_with_new_note(
                folder, "---\nupdated: 2026-06-13\n---\n# New Note\n[[Anchor]]\n")
        anchor, new = kept["Anchor"], after["New Note"]
        # centroid of the placed neighbours (here: Anchor alone) plus bounded jitter
        self.assertLessEqual(abs(new[0] - anchor[0]), 60.0)
        self.assertLessEqual(abs(new[1] - anchor[1]), 60.0)
        self.assertNotEqual(new, anchor)   # jittered, so it never hides under it

    def test_a_new_note_sits_at_the_centroid_of_several_placed_neighbours(self):
        with tempfile.TemporaryDirectory() as folder:
            kept, after = self.rebuild_with_new_note(
                folder, "---\nupdated: 2026-06-13\n---\n# New Note\n[[Anchor]]\n[[Far]]\n")
        cx = (kept["Anchor"][0] + kept["Far"][0]) / 2
        cy = (kept["Anchor"][1] + kept["Far"][1]) / 2
        self.assertLessEqual(abs(after["New Note"][0] - cx), 60.0)
        self.assertLessEqual(abs(after["New Note"][1] - cy), 60.0)

    def test_new_note_placement_is_deterministic_across_rebuilds(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.rebuild_with_new_note(
                folder, "---\nupdated: 2026-06-13\n---\n# New Note\n[[Anchor]]\n")[1]
        with tempfile.TemporaryDirectory() as folder:
            second = self.rebuild_with_new_note(
                folder, "---\nupdated: 2026-06-13\n---\n# New Note\n[[Anchor]]\n")[1]
        self.assertEqual(first["New Note"], second["New Note"])

    def test_an_unlinked_new_note_keeps_its_computed_position(self):
        """Nothing ties it to the old sky, so it stays where the layout put it."""
        with tempfile.TemporaryDirectory() as folder:
            out, _ = self.map_file(folder)               # map of the older vault
            kept = build_map.read_positions(str(out))
            grown = dict(self.VAULT)
            grown["Work/Loner.md"] = "---\nupdated: 2026-06-13\n---\n# Loner\n"
            source = Path(folder, "grown")
            source.mkdir()
            write_vault(source, grown)
            without = build_map.build(str(source), "Test", as_of=AS_OF)
            with_kept = build_map.build(str(source), "Test", as_of=AS_OF, keep_positions=kept)
        self.assertEqual(self.payload_positions(with_kept)["Loner"],
                         self.payload_positions(without)["Loner"])

    def test_merged_positions_stay_finite_for_every_node(self):
        with tempfile.TemporaryDirectory() as folder:
            kept, after = self.rebuild_with_new_note(
                folder, "---\nupdated: 2026-06-13\n---\n# New Note\n[[Anchor]]\n")
        for nid, (x, y) in after.items():
            with self.subTest(node=nid):
                self.assertTrue(math.isfinite(x) and math.isfinite(y))

    # ── malformed payloads ───────────────────────────────────────────────────
    def test_read_positions_rejects_malformed_payloads_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder, "nope.html")
            not_a_map = Path(folder, "plain.html")
            not_a_map.write_text("<html><body>just a page</body></html>", encoding="utf-8")
            broken_json = Path(folder, "broken.html")
            broken_json.write_text("<script>\nconst DATA = {\"nodes\": [oops};\n</script>",
                                   encoding="utf-8")
            no_positions = Path(folder, "nopos.html")
            self.write_map_with_nodes(no_positions, [{"data": {"id": "A"}}])
            empty_nodes = Path(folder, "empty.html")
            self.write_map_with_nodes(empty_nodes, [])
            cases = [(missing, "cannot read"),
                     (not_a_map, "does not look like"),
                     (broken_json, "not valid JSON"),
                     (no_positions, "no node positions"),
                     (empty_nodes, "no node positions")]
            for path, fragment in cases:
                with self.subTest(case=path.name):
                    with self.assertRaises(ValueError) as caught:
                        build_map.read_positions(str(path))
                    self.assertIn(fragment, str(caught.exception))
                    self.assertIn(path.name, str(caught.exception))

    def test_read_positions_rejects_a_nodes_field_that_is_not_a_list(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "map.html")
            path.write_text('<script>\nconst DATA = {"nodes": {"A": {"x": 1}}};\n</script>',
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                build_map.read_positions(str(path))

    # ── non-finite coordinates ───────────────────────────────────────────────
    def test_non_finite_and_non_numeric_coordinates_are_rejected(self):
        """JSON allows NaN/Infinity literals; a poisoned coordinate must never
        reach the layout — it would drop the node and infect its neighbours."""
        bad_values = [float("nan"), float("inf"), float("-inf"),
                      "NaN", "Infinity", "abc", None, True, [1], {"x": 1}]
        with tempfile.TemporaryDirectory() as folder:
            for i, bad in enumerate(bad_values):
                for axis in ("x", "y"):
                    with self.subTest(value=repr(bad), axis=axis):
                        position = {"x": 1.0, "y": 2.0}
                        position[axis] = bad
                        path = Path(folder, f"bad{i}{axis}.html")
                        self.write_map_with_nodes(path, [
                            {"data": {"id": "Poisoned"}, "position": position},
                            {"data": {"id": "Good"}, "position": {"x": 5.0, "y": 6.0}},
                        ])
                        kept = build_map.read_positions(str(path))
                        self.assertEqual(kept, {"Good": (5.0, 6.0)})

    def test_a_map_whose_coordinates_are_all_non_finite_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "nan.html")
            path.write_text(
                '<script>\nconst DATA = {"nodes": [{"data": {"id": "A"}, '
                '"position": {"x": NaN, "y": Infinity}}]};\n</script>', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                build_map.read_positions(str(path))
        self.assertIn("no node positions", str(caught.exception))

    def test_finite_numeric_strings_and_integers_are_still_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "map.html")
            self.write_map_with_nodes(path, [
                {"data": {"id": "Str"}, "position": {"x": "12.5", "y": "-3"}},
                {"data": {"id": "Int"}, "position": {"x": 7, "y": 0}},
            ])
            kept = build_map.read_positions(str(path))
        self.assertEqual(kept, {"Str": (12.5, -3.0), "Int": (7.0, 0.0)})


class CliTests(unittest.TestCase):
    NOTE = "---\ncreated: 2026-06-01\nupdated: 2026-06-10\n---\n# A\n"

    def test_default_build_writes_a_map(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--title", "T", "--source-label", "demo",
                           "--as-of", AS_OF)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.exists())
            html = out.read_text(encoding="utf-8")
        self.assertIn(CDN_URL, html)
        self.assertIn("demo", html)

    def test_invalid_as_of_fails_without_a_traceback_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--as-of", "tomorrow")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("--as-of", proc.stderr)
        self.assertFalse(out.exists(), "no output may be written when --as-of is invalid")

    def test_strict_offline_refuses_without_a_local_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--strict-offline")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("--cytoscape-js", proc.stderr)
        self.assertFalse(out.exists(), "refusal must happen before any output")

    def test_strict_offline_refuses_an_invalid_local_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            bogus = Path(folder, "bogus.js"); bogus.write_text("nope", encoding="utf-8")
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--strict-offline", "--cytoscape-js", str(bogus))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(out.exists(), "refusal must happen before any output")

    def test_strict_offline_succeeds_with_a_valid_local_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            path = fake_runtime(Path(folder, "cytoscape.min.js"))
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--strict-offline", "--cytoscape-js", path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertNotIn(CDN_URL, html)
        self.assertIn("/*cytoscape-test-bundle*/", html)
        self.assertIn("Local runtime", html)

    def test_cytoscape_js_alone_inlines_without_strict_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            path = fake_runtime(Path(folder, "cytoscape.min.js"))
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--cytoscape-js", path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = out.read_text(encoding="utf-8")
        self.assertNotIn(CDN_URL, html)

    def test_bad_cytoscape_js_fails_cleanly_without_strict_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--cytoscape-js", str(Path(folder, "missing.js")))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(out.exists())

    def test_keep_positions_round_trips_through_the_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder, "vault")
            write_vault(vault, {"Work/Hub.md": "---\nupdated: 2026-06-14\n---\n# Hub\n[[Anchor]]\n",
                                "Work/Anchor.md": "---\nupdated: 2026-06-10\n---\n# Anchor\n"})
            first = Path(folder, "first.html")
            proc = run_cli(str(vault), str(first), "--as-of", AS_OF)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # edit the vault, then rebuild keeping the sky we already know
            Path(vault, "Work/New.md").write_text(
                "---\nupdated: 2026-06-13\n---\n# New\n[[Anchor]]\n", encoding="utf-8")
            second = Path(folder, "second.html")
            proc = run_cli(str(vault), str(second), "--as-of", AS_OF,
                           "--keep-positions", str(first))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            before = build_map.read_positions(str(first))
            after = build_map.read_positions(str(second))
        self.assertEqual(set(after), {"Hub", "Anchor", "New"})
        self.assertEqual(after["Hub"], before["Hub"])
        self.assertEqual(after["Anchor"], before["Anchor"])
        self.assertLessEqual(abs(after["New"][0] - before["Anchor"][0]), 60.0)
        self.assertLessEqual(abs(after["New"][1] - before["Anchor"][1]), 60.0)

    def test_keep_positions_rejects_junk_without_a_traceback_and_writes_nothing(self):
        cases = {
            "missing.html": None,
            "plain.html": "<html><body>not a map</body></html>",
            "broken.html": '<script>\nconst DATA = {"nodes": [oops};\n</script>',
            "nopos.html": '<script>\nconst DATA = {"nodes": [{"data": {"id": "A"}}]};\n</script>',
            "nan.html": ('<script>\nconst DATA = {"nodes": [{"data": {"id": "A"}, '
                         '"position": {"x": NaN, "y": 3}}]};\n</script>'),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as folder:
                    write_vault(folder, {"a.md": self.NOTE})
                    stale = Path(folder, name)
                    if body is not None:
                        stale.write_text(body, encoding="utf-8")
                    out = Path(folder, "out.html")
                    proc = run_cli(folder, str(out), "--keep-positions", str(stale))
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertIn("--keep-positions", proc.stderr)
                    self.assertIn("Nothing was written", proc.stderr)
                    self.assertFalse(out.exists(), "no output may be written on refusal")

    def test_pointing_cytoscape_js_at_the_readme_is_refused(self):
        """Guards a real trap: the README is large and mentions cytoscape."""
        with tempfile.TemporaryDirectory() as folder:
            write_vault(folder, {"a.md": self.NOTE})
            out = Path(folder, "out.html")
            proc = run_cli(folder, str(out), "--cytoscape-js", str(ROOT / "README.md"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(out.exists())


class ReadmeTests(unittest.TestCase):
    README = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_identifies_the_maintained_fork_and_keeps_upstream_credit(self):
        self.assertIn("aikirias/brain-map-skill", self.README)
        self.assertIn("vladignatyev/brain-map-skill", self.README)
        self.assertRegex(self.README, r"(?i)fork")

    def test_documents_all_five_bands_and_the_local_runtime_option(self):
        for band in ["fresh", "recent", "settled", "dormant", "oxidized"]:
            with self.subTest(band=band):
                self.assertIn(band, self.README)
        self.assertIn("--cytoscape-js", self.README)
        self.assertIn("--strict-offline", self.README)


if __name__ == "__main__":
    unittest.main()

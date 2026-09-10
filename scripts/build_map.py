#!/usr/bin/env python3
"""
brain-map — turn a folder of save-note Markdown (an Obsidian vault or a
`gbrain export` dir) into ONE self-contained, interactive HTML knowledge map.

Graph  : Cytoscape.js, force-directed positions pre-computed here (networkx
         spring layout) so a 1000-node map opens instantly.
Time   : a brushable timeline strip (built from each note's `created`) that
         filters the graph — scrub or hit Play to watch the brain grow.
Fresh  : every note gets an age in days, a continuous freshness score in [0,1]
         used for luminance/saturation/halo, and one of five bands used as
         filters and labels (fresh/recent/settled/dormant/oxidized; `unknown`
         only when no date can be found at all).
Facts  : click any node for its summary, tags, dates, freshness and neighbours.

Usage:
    python build_map.py <vault_dir> <out.html> [--title "My Brain"]
                        [--as-of 2026-06-15] [--cytoscape-js path/to/cytoscape.min.js]
                        [--strict-offline]

Network honesty: by default the generated file loads Cytoscape from a pinned CDN
URL and says so in its header ("Network runtime"). Nothing is ever downloaded at
build time. Pass --cytoscape-js PATH to inline a Cytoscape bundle you already
have, which makes the file work with no network at all ("Local runtime").
--strict-offline refuses to write anything unless such a local bundle is given.
"""
import os, re, sys, json, html, math, datetime, argparse, hashlib

try:
    import networkx as nx
except ImportError:
    nx = None

# ── runtime (Cytoscape) ──────────────────────────────────────────────────────
CYTOSCAPE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js"
# Official Cytoscape 3 browser bundles are hundreds of KiB. A generous lower
# bound plus distribution-specific UMD/copyright markers rejects stubs, prose,
# saved error pages and unrelated JavaScript without executing untrusted code.
MIN_RUNTIME_BYTES = 100_000
RUNTIME_SUFFIXES = (".js", ".mjs", ".cjs")
RUNTIME_MARKERS = (
    "The Cytoscape Consortium",
    "module.exports=",
    ".cytoscape=",
)


class RuntimeUnavailable(Exception):
    """Raised when a local Cytoscape bundle is missing or implausible."""


def read_local_runtime(path):
    """Read and sanity-check a local Cytoscape bundle. Returns its JS source.

    Never fetches anything: the file must already exist on disk. The checks are
    deliberately cheap and non-executing — enough to refuse a stub, a saved error
    page or a documentation file, so we never inline something that would leave a
    silently broken map behind.
    """
    if not path:
        raise RuntimeUnavailable("no path given")
    if not os.path.isfile(path):
        raise RuntimeUnavailable(f"no such file: {path}")
    if not path.lower().endswith(RUNTIME_SUFFIXES):
        raise RuntimeUnavailable(
            f"{path} is not a {'/'.join(RUNTIME_SUFFIXES)} file — point this at a Cytoscape bundle")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise RuntimeUnavailable(f"cannot read {path}: {exc}") from exc
    if text.lstrip().startswith("<"):
        raise RuntimeUnavailable(f"{path} looks like HTML, not a JavaScript bundle")
    encoded_size = len(text.encode("utf-8"))
    if encoded_size < MIN_RUNTIME_BYTES:
        raise RuntimeUnavailable(
            f"{path} is only {encoded_size} bytes — too small to be a Cytoscape browser bundle")
    missing = [marker for marker in RUNTIME_MARKERS if marker not in text]
    if missing:
        raise RuntimeUnavailable(
            f"{path} lacks official Cytoscape browser-bundle markers — wrong file?")
    return text


def escape_inline_js(text):
    """Make arbitrary JS safe to embed inside a <script> element."""
    text = re.sub(r"</(script)", lambda m: "<\\/" + m.group(1), text, flags=re.I)
    return text.replace("<!--", "<\\!--")


# ── freshness ────────────────────────────────────────────────────────────────
# Exactly five bands, plus `unknown` for notes with no usable date at all.
# Bands are for filtering and labelling; rendering uses the continuous score.
FRESHNESS_BANDS = (
    ("fresh",    7),      # updated within a week
    ("recent",   30),     # 8–30 days
    ("settled",  90),     # 31–90 days
    ("dormant",  365),    # 91–365 days
    ("oxidized", None),   # over a year
)
FRESHNESS_ORDER = tuple(name for name, _ in FRESHNESS_BANDS) + ("unknown",)
# Score half-life: a note updated 120 days ago reads at half the luminance of a
# brand new one. Exponential decay keeps the scale continuous and never reaches 0.
FRESHNESS_HALF_LIFE_DAYS = 120.0


def freshness_band(age_days):
    """Map an age in days onto one of the five bands; None -> 'unknown'."""
    if age_days is None:
        return "unknown"
    age = max(0, int(age_days))
    for name, upper in FRESHNESS_BANDS:
        if upper is None or age <= upper:
            return name
    return "oxidized"


def freshness_score(age_days):
    """Continuous freshness in [0,1]: 1.0 brand new, approaching 0 with age."""
    if age_days is None:
        return None
    age = max(0, int(age_days))
    return round(0.5 ** (age / FRESHNESS_HALF_LIFE_DAYS), 4)


def freshness_for(updated, as_of):
    """(band, age_days, score) for an ISO `updated` stamp against a reference."""
    if not updated:
        return "unknown", None, None
    age = max(0, (as_of - datetime.datetime.fromisoformat(updated)).days)
    return freshness_band(age), age, freshness_score(age)


# ── theme + type taxonomy ────────────────────────────────────────────────────
THEME_COLORS = {
    "Work":  "#38bdf8",   # cyan
    "Study": "#a78bfa",   # violet
    "Life":  "#fb923c",   # amber
    "Other": "#94a3b8",   # slate
}

# Themes are data-driven: every top-level folder in the notes dir becomes a theme.
# Work/Study/Life keep their legacy colors when present; root-level notes (no
# folder) fall under "Other". Any other folder draws a stable color from this
# palette, so the map fits ANY vault's own categories — no fixed taxonomy imposed.
THEME_PALETTE = [
    "#38bdf8", "#a78bfa", "#fb923c", "#34d399", "#f472b6", "#facc15",
    "#60a5fa", "#c084fc", "#fb7185", "#4ade80", "#22d3ee", "#fbbf24",
    "#a3e635", "#e879f9", "#2dd4bf", "#f87171", "#818cf8", "#fdba74",
    "#5eead4", "#fca5a5",
]
THEME_OTHER = "#94a3b8"

def assign_theme_colors(theme_counts):
    """Map each observed theme -> color. Canonical Work/Study/Life keep their
    legacy colors; "Other" is slate; the rest draw from THEME_PALETTE in a stable
    order (size desc, then name) so re-runs are deterministic."""
    canon = {"Work": "#38bdf8", "Study": "#a78bfa", "Life": "#fb923c"}
    colors = {"Other": THEME_OTHER}
    used = {THEME_OTHER}
    for k, c in canon.items():
        if k in theme_counts:
            colors[k] = c; used.add(c)
    pal = [c for c in THEME_PALETTE if c not in used]
    i = 0
    for name in sorted((t for t in theme_counts if t not in colors),
                       key=lambda t: (-theme_counts[t], t.lower())):
        colors[name] = pal[i % len(pal)] if pal else THEME_OTHER
        i += 1
    return colors

TYPE_SHAPES = {
    "person":      "ellipse",
    "company":     "round-rectangle",
    "project":     "hexagon",
    "pattern":     "diamond",
    "reflection":  "round-diamond",
    "idea":        "star",
    "atom":        "ellipse",
    "meeting":     "round-diamond",
    "journal":     "round-rectangle",
    "todo":        "round-tag",
    "index":       "star",
    "lecture":     "round-rectangle",
    "link":        "round-pentagon",
    "note":        "ellipse",
}

def subtype_of(relpath, tags, title):
    parts = relpath.split(os.sep)
    folders = set(p.lower() for p in parts[:-1])
    name = title.lower()
    if "people" in folders: return "person"
    if "meetings" in folders: return "meeting"
    if "journal" in folders: return "journal"
    if "lectures" in folders: return "lecture"
    if "project" in folders: return "project"
    if "links" in folders: return "link"
    if "todo" in name or "todo" in tags: return "todo"
    if "index" in tags or "moc" in tags or "— index" in name: return "index"
    return "note"

# ── parse one markdown file ──────────────────────────────────────────────────
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
H1_RE = re.compile(r"^#\s+(.+)$", re.M)

def parse_fm(block):
    fm = {}
    for line in block.splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if not m: continue
        k, v = m.group(1), m.group(2).strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
        fm[k] = v
    return fm

def parse_timestamp(value):
    """Normalize a frontmatter date to an ISO-8601 UTC timestamp, or empty."""
    if not value:
        return ""
    try:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.datetime.fromisoformat(text).replace(tzinfo=datetime.timezone.utc)
        else:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            parsed = (parsed.replace(tzinfo=datetime.timezone.utc) if parsed.tzinfo is None
                      else parsed.astimezone(datetime.timezone.utc))
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        return ""

def parse_as_of(value):
    """Parse --as-of into an aware datetime. Raises ValueError on garbage."""
    parsed = parse_timestamp(value)
    if not parsed:
        raise ValueError(f"{value!r} is not an ISO date (2026-06-15) or timestamp")
    return datetime.datetime.fromisoformat(parsed)

# ── freshness field aliases ──────────────────────────────────────────────────
UPDATED_FIELDS = ("last_updated", "updated", "modified")

def summarize(body):
    """First non-empty prose under ## Summary, else first prose paragraph."""
    m = re.search(r"##\s*Summary\s*\n(.+?)(?:\n##\s|\Z)", body, re.S)
    chunk = m.group(1) if m else body
    chunk = re.sub(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]", r"\1", chunk)  # strip wikilink syntax
    lines = [l.strip() for l in chunk.splitlines() if l.strip() and not l.strip().startswith("#")]
    text = " ".join(lines)
    return (text[:280] + "…") if len(text) > 280 else text

def _link_key(value):
    """Normalize a Markdown/GBrain link target without changing its identity."""
    key = str(value).strip().replace("\\", "/")
    if key.startswith("./"):
        key = key[2:]
    if key.lower().endswith(".md"):
        key = key[:-3]
    return key.strip("/")


def load(vault):
    nodes = {}         # title -> node dict
    raw_links = []     # (src_title, target title/slug text)
    aliases = {}       # title, filename and relative GBrain slug -> node id

    def register(alias, node_id):
        key = _link_key(alias)
        if not key:
            return
        # Ambiguous aliases must not silently point at whichever file was read last.
        existing = aliases.get(key)
        if key not in aliases or existing == node_id:
            aliases[key] = node_id
        else:
            aliases[key] = None

    for root, dirs, files in os.walk(vault):
        dirs.sort()          # deterministic on every filesystem, so collision
        for fn in sorted(files):  # suffixes and alias winners never depend on walk order
            if not fn.endswith(".md"): continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, vault)
            with open(path, encoding="utf-8") as note_file:
                text = note_file.read()
            fmm = FM_RE.match(text)
            fm = parse_fm(fmm.group(1)) if fmm else {}
            body = text[fmm.end():] if fmm else text
            h1 = H1_RE.search(body)
            title = (h1.group(1).strip() if h1 else os.path.splitext(fn)[0]).strip()
            parts = rel.split(os.sep)
            theme = parts[0] if len(parts) > 1 else "Other"  # top folder = theme; root notes = Other
            tags = fm.get("tags", [])
            if isinstance(tags, str): tags = [tags]
            fm_type = str(fm.get("type", "")).strip().lower()
            stype = fm_type or subtype_of(rel, [t.lower() for t in tags], title)
            created = parse_timestamp(fm.get("created", ""))
            # freshness reads the first update-ish field present, then falls back
            # to `created`, then to the file's own timestamp — so the inspector
            # can always say *where* the date came from.
            updated = ""
            freshness_source = ""
            for field in UPDATED_FIELDS:
                updated = parse_timestamp(fm.get(field, ""))
                if updated:
                    freshness_source = field
                    break
            if not updated and created:
                updated, freshness_source = created, "created"
            if not created:  # vanilla vaults often lack a date — fall back to the file's own timestamp
                try:
                    st = os.stat(path)
                    ts = getattr(st, "st_birthtime", 0) or st.st_mtime
                    created = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat(timespec="seconds")
                    if not updated:
                        updated, freshness_source = created, "filesystem"
                except OSError:
                    created = ""
            if title in nodes:  # collision: keep first, skip dup title
                title = f"{title} ⟨{hashlib.md5(rel.encode()).hexdigest()[:4]}⟩"
            nodes[title] = {
                "id": title, "title": title, "theme": theme, "type": stype,
                "tags": tags, "created": created, "updated": updated,
                "freshnessSource": freshness_source, "summary": summarize(body),
                "path": rel, "source": fm.get("source", ""),
                # `gbrain export` stamps `slug:` only when the stored slug is not
                # a fixed point of its path slugifier; the GBrain adapter prefers
                # it over the path. Consumed in build(), never rendered.
                "slugHint": fm.get("slug", "") if isinstance(fm.get("slug", ""), str) else "",
            }
            register(title, title)
            register(os.path.splitext(fn)[0], title)
            register(os.path.splitext(rel)[0], title)
            for tgt in WIKILINK_RE.findall(body):
                raw_links.append((title, tgt.strip()))
    # Resolve both ordinary title links and GBrain's relative slug links.
    edges = []
    seen = set()
    for source_id, target_text in raw_links:
        target_id = aliases.get(_link_key(target_text))
        if target_id and source_id in nodes and target_id != source_id:
            key = (source_id, target_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"source": source_id, "target": target_id})
    return nodes, edges

# ── optional read-only GBrain enrichment ─────────────────────────────────────
# Markdown-only stays the default and the fallback: every function below is
# reached only when --gbrain is passed, and every failure inside them degrades
# to the frontmatter/filesystem timestamps load() already computed.
DEFAULT_HISTORY_LIMIT = 500

GBRAIN_OFF = {"enabled": False, "status": "off", "mode": "markdown", "gaps": [],
              "detail": "Markdown only — timestamps come from frontmatter, "
                        "falling back to the file's own timestamp."}


def gbrain_adapter():
    """Load the sibling read-only adapter by path, or None if it is missing.

    Loaded lazily and by path so a Markdown-only build never imports it and a
    copy of build_map.py without the adapter still runs.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gbrain_history.py")
    spec = importlib.util.spec_from_file_location("gbrain_history", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError, SyntaxError):
        return None
    return module


def match_slugs(nodes, index, adapter, slug_prefix=""):
    """{note title: brain slug} for the notes that exist in the brain."""
    matched = {}
    for title in sorted(nodes):
        node = nodes[title]
        for candidate in adapter.slug_candidates(node["path"], node.get("slugHint", ""), slug_prefix):
            if candidate in index:
                matched[title] = candidate
                break
    return matched


def apply_gbrain(nodes, reader, adapter, want_history=False,
                 history_limit=DEFAULT_HISTORY_LIMIT, slug_prefix=""):
    """Overwrite matched notes' freshness with the brain's own numbers.

    Precedence, highest first: the semantic GBrain stamp (a verified
    `updated_at`, or the last version snapshot when a non-semantic touch came
    after it), then the frontmatter update fields, then `created`, then the
    file timestamp. Unmatched notes are left exactly as Markdown-only built
    them — enrichment is per note, never all-or-nothing.
    """
    index = reader.pages()
    matched = match_slugs(nodes, index, adapter, slug_prefix)
    history, coverage = {}, history_coverage(want_history, history_limit)
    if want_history and matched:
        # Freshest first, slug-ascending on ties: a budget-truncated run reads
        # the same pages every time.
        order = sorted(set(matched.values()))
        order.sort(key=lambda slug: index[slug]["updated"], reverse=True)
        history, coverage = read_history(reader, adapter, order, history_limit)
    for title, slug in matched.items():
        facts = index[slug]
        summary = history.get(slug) if want_history else None
        verdict = adapter.classify(facts["updated"], summary)
        if not verdict["updated"]:
            continue
        # Timestamps, counts and a status the inspector renders — nothing
        # else. The slug and the source id that produced this match stay in
        # `matched`/`index`: they are brain-internal names, they are never
        # displayed, and a generated map is a file people share.
        nodes[title].update({
            "updated": verdict["updated"],
            "freshnessSource": verdict["source"],
            "brainUpdated": facts["updated"],
            "revisions": verdict["revisions"],
            "lastRevision": verdict["lastRevision"],
            "historyStatus": verdict["status"],
        })
    # Counted per brain page, not per note: two files can legitimately resolve
    # to the same slug, and one page's revisions are one page's revisions.
    revisions_by_month, revision_events, revised_pages = {}, 0, 0
    for slug in sorted(set(matched.values())):
        summary = history.get(slug)
        if not summary or summary.get("error"):
            continue
        revision_events += summary["revisions"]
        revised_pages += 1 if summary["revisions"] else 0
        for month, count in summary["months"].items():
            revisions_by_month[month] = revisions_by_month.get(month, 0) + count
    coverage.update({"revised": revised_pages, "events": revision_events})
    diagnostics = dict(reader.diagnostics)
    gaps = gbrain_gaps(diagnostics, coverage)
    detail = ("Freshness comes from GBrain's own updated_at" +
              (", cross-checked against page version history." if want_history
               else " (version history not read)."))
    if gaps:
        # Per note, not all-or-nothing: the notes that matched a page keep the
        # brain's numbers and say where they came from, and every other note
        # keeps the Markdown ones. What the read missed is named here instead
        # of being smoothed over.
        detail += (" Partial read — %s. Matched notes keep the brain's timestamps; "
                   "everything else keeps the Markdown ones." % "; ".join(gaps))
    report = {
        "enabled": True, "status": "partial" if gaps else "ok", "mode": "gbrain",
        "detail": detail, "gaps": gaps,
        "matched": len(matched), "indexed": len(index), "notes": len(nodes),
        "history": coverage,
        "diagnostics": diagnostics,
    }
    return report, revisions_by_month


def history_coverage(requested, limit):
    """The empty history ledger: what was asked for, read, refused and skipped."""
    return {"requested": bool(requested), "matched": 0, "eligible": 0, "asked": 0,
            "read": 0, "failed": 0, "incomplete": 0, "collisions": 0, "skipped": 0,
            "limit": limit if requested else 0, "error": "", "unproven": "",
            "revised": 0, "events": 0}


def read_history(reader, adapter, order, limit):
    """Version history for the slugs it is *safe* to ask about, and the ledger.

    `get_versions` takes a bare slug and answers with the union of every source
    that holds it, so only slugs the reader can prove are unique brain-wide are
    ever asked for (see GBrainReader.history_scope). Everything it refuses —
    a slug another source also holds, every slug when the collision check could
    not cover the brain, and whatever a budget left over — keeps the backend
    `updated_at` it already has and stays labelled `unverified`. None of that
    is a clean read, so all of it is counted here and named in the gaps.
    """
    coverage = history_coverage(True, limit)
    coverage["matched"] = len(order)
    try:
        scope = reader.history_scope()
        safe = [slug for slug in order if slug in scope["unique"]]
        coverage["eligible"] = len(safe)
        # Refused *because they collide* only when the check actually proved
        # something; an audit that fell short refused everything for one
        # reason, and that reason is reported once instead of as N collisions.
        coverage["collisions"] = len(order) - len(safe) if scope["proven"] else 0
        coverage["unproven"] = scope["reason"]
        if limit and limit > 0:
            coverage["skipped"] = max(0, len(safe) - limit)
        history = reader.history(safe, limit=limit, order=order) if safe else {}
    except (adapter.GBrainUnavailable, adapter.GBrainToolError) as exc:
        # The page index already came back clean. Losing the session half-way
        # through history downgrades those notes to "history not read" — it
        # does not throw away the updated_at we did get. Whatever a budget
        # would have skipped is moot once the walk died: one gap, one reason.
        # Backend-controlled exception text can contain private slugs or
        # content. Keep the shareable payload categorical and non-sensitive.
        coverage["error"], coverage["skipped"] = "the history transport failed", 0
        return {}, coverage
    coverage["asked"] = len(history)
    coverage["failed"] = sum(1 for summary in history.values()
                             if summary.get("error") and not summary.get("malformed"))
    coverage["incomplete"] = sum(1 for summary in history.values()
                                 if summary.get("malformed"))
    coverage["read"] = coverage["asked"] - coverage["failed"] - coverage["incomplete"]
    return history, coverage


def gbrain_gaps(diagnostics, history):
    """Everything the read did not cover, phrased for a human. [] when clean.

    A page index that stopped early, a row that could not be read, a slug the
    adapter refused to guess at, a history read that failed for one page and a
    budget that stopped short of the rest all leave the same hole: notes that
    exist in the brain but were not enriched from it, or were enriched without
    the history that would confirm the number. They are reported as `partial`
    rather than as a clean `ok`, so a build never claims coverage it does not
    have — and `--gbrain-required` refuses such a build outright.
    """
    gaps = []
    if diagnostics.get("truncated"):
        gaps.append("the page index stopped early, so some pages were never seen")
    for key, phrase in (("ambiguous", "%d slug(s) exist in more than one source and "
                                      "cannot be resolved (use --gbrain-source)"),
                        ("foreign", "%d row(s) came back from a source other than the "
                                    "one requested and were dropped"),
                        ("malformed", "%d row(s) were unreadable")):
        count = diagnostics.get(key) or 0
        if count:
            gaps.append(phrase % count)
    return gaps + history_gaps(history or {})


def history_gaps(history):
    """What a version-history read left unproven. [] when it covered everything."""
    if not history.get("requested"):
        return []
    gaps = []
    if history.get("error"):
        gaps.append("version history stopped early (%s)" % history["error"])
    if history.get("unproven"):
        gaps.append("no page history was read: %s, so no slug could be shown to "
                    "belong to exactly one page" % history["unproven"])
    if history.get("collisions"):
        gaps.append("%d matched page(s) share a slug with another source, so their "
                    "history would be a union of both and was not read"
                    % history["collisions"])
    if history.get("skipped"):
        gaps.append("--gbrain-history-limit %d read the %d freshest of %d eligible "
                    "page(s); the other %d kept an unverified backend timestamp "
                    "(use --gbrain-history-limit 0 for all of them)"
                    % (history["limit"], history["asked"], history["eligible"],
                       history["skipped"]))
    if history.get("failed"):
        gaps.append("version history could not be read for %d page(s)" % history["failed"])
    if history.get("incomplete"):
        gaps.append("%d page(s) came back with unreadable version rows, so their "
                    "revision counts are unknown" % history["incomplete"])
    return gaps


def enrich_from_gbrain(nodes, options):
    """Run the adapter, or explain in the payload why it did not run.

    Fail-open is the contract: an absent binary, a brain that will not start,
    a protocol change or a malformed answer all leave the Markdown-derived map
    intact and record the reason. A read that only half worked is reported as
    `partial` — the notes that did match keep the brain's timestamps, every
    other note keeps the Markdown ones, and the gaps are named. Both cases
    become hard errors under --gbrain-required, before anything is written.
    """
    adapter = gbrain_adapter()
    empty = {}
    if adapter is None:
        detail = "scripts/gbrain_history.py is missing — cannot read the brain."
        if options.get("required"):
            raise RuntimeError(detail)
        return dict(GBRAIN_OFF, status="unavailable", detail=detail), empty
    reader = None
    try:
        reader = adapter.open_reader(
            command=options.get("command") or adapter.DEFAULT_COMMAND,
            source_id=options.get("source"),
            want_history=bool(options.get("history")),
            timeout=options.get("timeout") or adapter.DEFAULT_TIMEOUT)
        report, revisions_by_month = apply_gbrain(
            nodes, reader, adapter,
            want_history=bool(options.get("history")),
            history_limit=options.get("historyLimit", DEFAULT_HISTORY_LIMIT),
            slug_prefix=options.get("slugPrefix", ""))
        if options.get("required") and report["status"] != "ok":
            raise RuntimeError("GBrain read was incomplete: %s" % "; ".join(report["gaps"]))
        return report, revisions_by_month
    except (adapter.GBrainUnavailable, adapter.GBrainToolError) as exc:
        if options.get("required"):
            raise RuntimeError("GBrain adapter failed: %s" % exc)
        return dict(GBRAIN_OFF, status="unavailable",
                    detail="GBrain unavailable — kept the Markdown timestamps."), empty
    finally:
        if reader is not None:
            reader.close()


# ── layout ───────────────────────────────────────────────────────────────────
CANVAS_W, CANVAS_H = 4200, 3000

def seed_positions(nodes):
    """Deterministic starting positions in unit space: themes on a ring (Other
    in the middle), each node hash-jittered around its theme anchor. Used to
    seed the spring layout, and — scaled — as the starting state of the
    in-browser cose fallback, so stdlib-only builds are deterministic too."""
    ring = sorted({n["theme"] for n in nodes.values()} - {"Other"})
    theme_anchor = {t: (math.cos(2 * math.pi * i / max(1, len(ring))),
                        math.sin(2 * math.pi * i / max(1, len(ring))))
                    for i, t in enumerate(ring)}
    theme_anchor["Other"] = (0.0, 0.0)
    init = {}
    for nid, n in nodes.items():
        ax, ay = theme_anchor.get(n["theme"], (0, 0))
        h = int(hashlib.md5(nid.encode()).hexdigest(), 16)
        init[nid] = (ax + ((h % 1000) / 1000 - 0.5) * 0.6,
                     ay + (((h // 1000) % 1000) / 1000 - 0.5) * 0.6)
    return init

def scale_positions(pos):
    """Map unit-space positions onto the fixed pixel canvas, centred on 0,0."""
    xs = [p[0] for p in pos.values()]; ys = [p[1] for p in pos.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    return {nid: ((x - minx) / (maxx - minx + 1e-9) * CANVAS_W - CANVAS_W / 2,
                  (y - miny) / (maxy - miny + 1e-9) * CANVAS_H - CANVAS_H / 2)
            for nid, (x, y) in pos.items()}

def layout(nodes, edges):
    if nx is None:
        return None  # signal: let the browser compute a force layout (cose)
    G = nx.Graph()
    G.add_nodes_from(nodes.keys())
    # intra-theme links pull twice as hard, so communities separate cleanly
    # without moving colour out of its data role
    for e in edges:
        same = nodes[e["source"]]["theme"] == nodes[e["target"]]["theme"]
        G.add_edge(e["source"], e["target"], weight=2.0 if same else 1.0)
    pos = nx.spring_layout(G, pos=seed_positions(nodes), k=0.45, iterations=240, seed=7)
    return scale_positions(pos)


def node_size(deg):
    """Node diameter grows with the square root of degree, so node *area* tracks
    link count roughly linearly — hubs read as landmarks instead of drowning the
    field the way linear diameter growth did."""
    return round(12 + 5.0 * math.sqrt(min(max(deg, 0), 49)), 1)


def _coordinate(value):
    """A finite float, or None when the payload holds something unusable.

    JSON permits `NaN`/`Infinity` literals and hand-edited maps hold anything at
    all; a non-finite coordinate would silently poison the layout (Cytoscape
    drops the node, and any new note placed at the centroid of its neighbours
    inherits the NaN). Rejecting it here keeps the bad value out of the map."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_positions(path):
    """{node id: (x, y)} recovered from a previously generated brain-map HTML.

    Powers --keep-positions: rebuilding after edits keeps every surviving note
    where the reader already knows it, instead of reshuffling the whole sky.
    Nodes whose coordinates are missing, non-numeric or non-finite are skipped
    rather than trusted — a partly readable map still keeps what it can."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    m = re.search(r"const DATA = (\{.*?\});\n", text, re.S)
    if not m:
        raise ValueError(f"{path} does not look like a generated brain-map HTML file")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: embedded payload is not valid JSON: {exc}") from exc
    out = {}
    raw_nodes = data.get("nodes")
    for node in raw_nodes if isinstance(raw_nodes, list) else []:
        if not isinstance(node, dict):
            continue
        meta = node.get("data")
        nid = meta.get("id") if isinstance(meta, dict) else None
        p = node.get("position")
        if not isinstance(nid, str) or not isinstance(p, dict):
            continue
        x, y = _coordinate(p.get("x")), _coordinate(p.get("y"))
        if x is None or y is None:
            continue
        out[nid] = (x, y)
    if not out:
        raise ValueError(f"{path} holds no node positions (was it built with a preset layout?)")
    return out


def merge_kept_positions(nodes, edges, pos, kept):
    """Overlay positions from a previous map. New notes land at the centroid of
    their already-placed neighbours (hash-jittered), or keep their computed/seed
    position when nothing links them to the old sky. Deterministic throughout."""
    neighbours = {}
    for e in edges:
        neighbours.setdefault(e["source"], []).append(e["target"])
        neighbours.setdefault(e["target"], []).append(e["source"])
    merged = {}
    for nid in nodes:
        if nid in kept:
            merged[nid] = kept[nid]
            continue
        placed = [kept[o] for o in neighbours.get(nid, []) if o in kept]
        if placed:
            h = int(hashlib.md5(nid.encode()).hexdigest(), 16)
            merged[nid] = (sum(p[0] for p in placed) / len(placed) + ((h % 200) - 100) * 0.6,
                           sum(p[1] for p in placed) / len(placed) + (((h // 200) % 200) - 100) * 0.6)
        else:
            merged[nid] = pos[nid]
    return merged

# ── build payload ────────────────────────────────────────────────────────────
def build(vault, title, source_label=None, as_of=None, keep_positions=None, gbrain=None):
    nodes, edges = load(vault)
    # Markdown first, brain second: enrichment overwrites the freshness of the
    # notes it can match and leaves every other note untouched.
    gbrain_report, revisions_by_month = (enrich_from_gbrain(nodes, gbrain) if gbrain
                                         else (dict(GBRAIN_OFF), {}))
    for node in nodes.values():
        node.pop("slugHint", None)      # matching input, not map data
    reference = parse_as_of(as_of) if as_of else datetime.datetime.now(datetime.timezone.utc)
    pos = layout(nodes, edges)
    use_preset = pos is not None
    if pos is None:
        # no networkx: ship deterministic theme-seeded positions as the starting
        # state for the in-browser force layout (cose, randomize off)
        pos = scale_positions(seed_positions(nodes))
    if keep_positions:
        kept = {nid: keep_positions[nid] for nid in nodes if nid in keep_positions}
        if kept:
            pos = merge_kept_positions(nodes, edges, pos, kept)
            use_preset = True
    deg = {nid: 0 for nid in nodes}
    for e in edges:
        deg[e["source"]] += 1; deg[e["target"]] += 1
    cy_nodes = []
    for nid, n in nodes.items():
        band, age_days, score = freshness_for(n["updated"], reference)
        x, y = pos[nid]
        node = {"data": {**n, "deg": deg[nid], "orphan": deg[nid] == 0,
                         "freshness": band, "ageDays": age_days,
                         "freshnessScore": score,
                         "size": node_size(deg[nid])},
                "position": {"x": round(x, 1), "y": round(y, 1)}}
        cy_nodes.append(node)
    cy_edges = [{"data": {"id": f"e{i}", "source": e["source"], "target": e["target"],
                          "theme": nodes[e["source"]]["theme"]}}
                for i, e in enumerate(edges)]
    # timeline buckets (month) — keyed by whatever themes exist
    all_themes = sorted({n["theme"] for n in nodes.values()})
    months = {}
    for n in nodes.values():
        c = n["created"][:7]
        if re.match(r"\d{4}-\d{2}", c):
            months.setdefault(c, {t: 0 for t in all_themes})
            months[c][n["theme"]] += 1
    # Creation growth (the stacked areas) and true revisions (the overlay) are
    # different events, so the axis is the union of the months either of them
    # happened in: a month that added no notes but rewrote a dozen is a real
    # month on this chart, with zero creations and a revision count, not a gap
    # in the axis and a number dropped off the edge.
    for month in revisions_by_month:
        months.setdefault(month, {t: 0 for t in all_themes})
    timeline = [{"month": m, **months[m]} for m in sorted(months)]
    axis = [row["month"] for row in timeline]
    revisions = {
        "available": bool(gbrain_report.get("history", {}).get("requested")) and bool(revisions_by_month),
        "series": [revisions_by_month.get(m, 0) for m in axis],
        "events": sum(revisions_by_month.values()),
        "pages": gbrain_report.get("history", {}).get("revised", 0),
    }
    themes = {}
    types = {}
    for n in nodes.values():
        themes[n["theme"]] = themes.get(n["theme"], 0) + 1
        types[n["type"]] = types.get(n["type"], 0) + 1
    theme_colors = assign_theme_colors(themes)
    theme_order = sorted(themes, key=lambda t: (t == "Other", -themes[t], t.lower()))
    src_abs = os.path.abspath(vault)
    safe_source = os.path.basename(os.path.normpath(src_abs)) or "notes"
    freshness_counts = {band: 0 for band in FRESHNESS_ORDER}
    for item in cy_nodes:
        freshness_counts[item["data"]["freshness"]] += 1
    flagged = sum(1 for item in cy_nodes
                  if item["data"]["freshness"] == "oxidized" or item["data"]["orphan"])
    audit = {"oxidized": freshness_counts["oxidized"],
             "orphans": sum(1 for item in cy_nodes if item["data"]["orphan"]),
             "flagged": flagged}
    return {
        "title": title, "nodes": cy_nodes, "edges": cy_edges,
        "source": source_label or safe_source,
        # Deliberately omit the absolute vault path: generated maps are often shared.
        "layout": "preset" if use_preset else "cose",
        "timeline": timeline, "revisions": revisions, "gbrain": gbrain_report,
        "themeColors": theme_colors, "themeOrder": theme_order,
        "typeShapes": TYPE_SHAPES, "freshnessOrder": list(FRESHNESS_ORDER),
        "runtime": {"mode": "network", "label": "Network runtime", "source": CYTOSCAPE_CDN,
                    "detail": "Cytoscape loads from a pinned CDN URL; your notes stay in this file."},
        "stats": {"nodes": len(nodes), "edges": len(edges),
                  "themes": themes, "types": types,
                  "span": [min((n["created"][:10] for n in nodes.values() if n["created"]), default=""),
                           max((n["created"][:10] for n in nodes.values() if n["created"]), default="")],
                  "asOf": reference.isoformat(timespec="seconds"),
                  "freshness": freshness_counts, "audit": audit,
                  "gbrain": {"status": gbrain_report["status"],
                             "matched": gbrain_report.get("matched", 0),
                             "revisions": revisions["events"]}},
    }

# ── HTML ─────────────────────────────────────────────────────────────────────
def render(payload, runtime=None, runtime_path=None):
    """Serialize the payload into the template.

    `runtime` is the JS source of a local Cytoscape bundle (see
    read_local_runtime); when given it is inlined and the file needs no network.
    The runtime actually embedded is recorded on `payload["runtime"]` so the
    header can state it honestly.
    """
    if runtime:
        payload["runtime"] = {
            "mode": "local", "label": "Local runtime",
            "source": os.path.basename(runtime_path) if runtime_path else "inlined bundle",
            "detail": "Cytoscape is inlined from a local bundle — this file needs no network.",
        }
        runtime_tag = "<script>\n" + escape_inline_js(runtime) + "\n</script>"
    else:
        payload["runtime"] = {
            "mode": "network", "label": "Network runtime", "source": CYTOSCAPE_CDN,
            "detail": "Cytoscape loads from a pinned CDN URL; your notes stay in this file.",
        }
        runtime_tag = f'<script src="{CYTOSCAPE_CDN}"></script>'
    # "<" can only occur inside JSON string literals, so escaping it wholesale is
    # safe and keeps note text from ever closing the <script> element.
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    subs = {"<!--__RUNTIME__-->": runtime_tag, "/*__DATA__*/": data}
    # one pass, so neither substitution can be rescanned for the other's marker
    return re.sub(r"<!--__RUNTIME__-->|/\*__DATA__\*/", lambda m: subs[m.group(0)], TEMPLATE)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Knowledge Map</title>
<!--__RUNTIME__-->
<style>
  :root{
    /* dark graphite constellation — no coloured gradients, colour is data only */
    --bg:#0a0c10; --surface:#13171d; --surface-2:#191e25;
    --line:#242b34; --line-soft:#1d232b;
    --ink:#e7ecf3; --ink-2:#bcc6d2; --muted:#7c8794; --muted-2:#5d6773;
    --accent:#8ec3e0;        /* steel — focus, selection, active controls */
    --warn:#d5a163;          /* audit + dormant */
    --alert:#cd7361;         /* oxidized */
    --r:12px; --r-sm:8px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--ink);
     font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Inter,sans-serif;
     -webkit-font-smoothing:antialiased}
  /* faint star grid + neutral vignettes: constellation, not nebula */
  #sky{position:fixed;inset:0;z-index:0;pointer-events:none;
     background:
       radial-gradient(circle at 1px 1px, rgba(198,212,230,.045) 1px, transparent 1.6px) 0 0/52px 52px,
       radial-gradient(1100px 620px at 50% -12%, rgba(176,194,216,.055), transparent 62%),
       radial-gradient(900px 520px at 96% 108%, rgba(140,160,184,.04), transparent 58%)}
  #cy{position:fixed;inset:0;z-index:1}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
     box-shadow:0 1px 0 rgba(255,255,255,.02) inset, 0 10px 30px rgba(0,0,0,.55)}
  .eyebrow{font-size:10.5px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);
     font-weight:600}
  .vsep{width:1px;align-self:stretch;background:var(--line);flex:0 0 auto}
  .h{display:none!important}

  /* ── top dock ─────────────────────────────────────────────────────────── */
  #dock{position:fixed;left:16px;top:14px;z-index:7;display:flex;align-items:center;gap:14px;
     padding:9px 14px;max-width:calc(100vw - 32px)}
  #dock .ident{display:flex;flex-direction:column;gap:1px;min-width:0}
  #dock h1{margin:0;font-size:14.5px;font-weight:650;letter-spacing:.2px;white-space:nowrap}
  #dock .src{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:11px;
     font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:38vw;overflow:hidden}
  #dock .src span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #dock .src span[data-tone="flag"]{color:var(--warn)}
  #dock .src svg{width:11px;height:11px;flex:0 0 auto;opacity:.65}
  .stat{display:flex;flex-direction:column;gap:1px;white-space:nowrap}
  .stat b{font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--ink)}
  .stat i{font-style:normal;font-size:10px;text-transform:uppercase;letter-spacing:.11em;
     color:var(--muted-2)}
  #rt{display:flex;align-items:center;gap:7px;padding:5px 9px;border-radius:999px;
     border:1px solid var(--line);background:var(--surface-2);white-space:nowrap}
  #rt span.d{width:6px;height:6px;border-radius:50%;background:var(--warn);flex:0 0 auto}
  #rt.local span.d{background:#6fbf9a}
  #rt b{font-size:11px;font-weight:600;color:var(--ink-2)}

  /* ── explore panel ────────────────────────────────────────────────────── */
  #panel{position:fixed;left:16px;top:78px;width:252px;z-index:6;padding:13px 13px 9px;
     max-height:calc(100vh - 250px);display:flex;flex-direction:column;overflow:hidden}
  #panel .p-head{flex:0 0 auto}
  #panel .p-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}
  .minbtn{width:22px;height:22px;padding:0;display:flex;align-items:center;justify-content:center;
     border:1px solid transparent;border-radius:6px;background:none;color:var(--muted);cursor:pointer}
  .minbtn:hover{color:var(--ink);border-color:var(--line)}
  .minbtn svg{width:13px;height:13px;transition:transform .18s ease}
  #panel.min .minbtn svg{transform:rotate(-90deg)}
  #panel.min .p-body,#panel.min .tools,#panel.min .note,#panel.min .s-info{display:none}
  .searchwrap{position:relative;display:flex;align-items:center}
  .searchwrap .s-ico{position:absolute;left:10px;width:13px;height:13px;color:var(--muted-2);
     pointer-events:none}
  #search{width:100%;padding:8px 52px 8px 30px;border-radius:var(--r-sm);border:1px solid var(--line);
     background:#0d1117;color:var(--ink);outline:none;font-size:13px;appearance:none}
  #search::placeholder{color:var(--muted-2)}
  #search:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(142,195,224,.12)}
  #search::-webkit-search-cancel-button{filter:invert(.6)}
  .kbd{position:absolute;right:8px;padding:2px 5px;border:1px solid var(--line);border-radius:5px;
     background:var(--surface-2);color:var(--muted);font:10.5px ui-monospace,monospace;
     pointer-events:none}
  .s-info{margin:7px 2px 0;font-size:11.5px;color:var(--muted);
     font-variant-numeric:tabular-nums}
  .s-info[hidden]{display:none}
  .tools{display:flex;gap:6px;margin-top:9px}
  .tool{flex:1;padding:7px 6px;border-radius:var(--r-sm);border:1px solid var(--line);
     background:var(--surface-2);color:var(--ink-2);cursor:pointer;font-size:12px;font-weight:550;
     display:flex;align-items:center;justify-content:center;gap:5px;white-space:nowrap}
  .tool:hover{border-color:#33404e;color:var(--ink)}
  .tool:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  .tool[aria-pressed="false"]{color:var(--muted-2)}
  .tool.on{border-color:var(--accent);color:#0c1016;background:var(--accent)}
  .tool.audit.on{border-color:var(--warn);background:var(--warn);color:#17120a}
  .badge{font-variant-numeric:tabular-nums;font-size:10.5px;padding:1px 4px;border-radius:4px;
     background:rgba(255,255,255,.1);color:inherit}
  .tool.audit.on .badge{background:rgba(0,0,0,.18)}
  .note{margin:9px 0 0;padding:7px 9px;border-radius:var(--r-sm);border:1px solid rgba(213,161,99,.32);
     background:rgba(213,161,99,.08);color:#e7cba0;font-size:11.5px;line-height:1.45}
  .note b{color:#f2dcb8;font-variant-numeric:tabular-nums}
  .p-body{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
     margin:12px -5px 0 0;padding-right:5px}
  .grp{border-top:1px solid var(--line-soft)}
  .grp:first-child{border-top:none}
  .grp>summary{display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;
     padding:9px 2px}
  .grp>summary::-webkit-details-marker{display:none}
  .grp>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:6px}
  .chev{width:11px;height:11px;color:var(--muted-2);transition:transform .18s ease;flex:0 0 auto}
  .grp[open]>summary .chev{transform:rotate(90deg)}
  .grp>summary .ct{margin-left:auto;color:var(--muted-2);font-size:11px;
     font-variant-numeric:tabular-nums}
  .rows{padding:0 0 8px}
  .filter{display:flex;align-items:center;gap:8px;padding:5px 7px;border-radius:7px;cursor:pointer;
     user-select:none}
  .filter:hover{background:rgba(190,208,230,.055)}
  .filter .box{width:14px;height:14px;border-radius:4px;border:1px solid #3a4450;flex:0 0 auto;
     display:flex;align-items:center;justify-content:center;background:#c9d7e6}
  .filter .box svg{width:10px;height:10px;color:#0c1016}
  .filter.off .box{background:transparent}
  .filter.off .box svg{display:none}
  .filter.off{opacity:.45}
  .filter .sw{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .filter .glyph{width:12px;height:12px;flex:0 0 auto;color:#8fa0b3}
  .filter .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}
  .filter .only{margin-left:auto;flex:0 0 auto;padding:1px 6px;border:1px solid var(--line);
     border-radius:5px;background:var(--surface-2);color:var(--muted);font-size:10px;
     cursor:pointer;opacity:0;transition:opacity .15s ease}
  .filter:hover .only,.filter:focus-within .only,.filter .only:focus-visible{opacity:1}
  .filter .only:hover{color:var(--ink);border-color:#33404e}
  .filter .ct{color:var(--muted);font-variant-numeric:tabular-nums;font-size:11.5px}

  /* ── inspector ────────────────────────────────────────────────────────── */
  #insp{position:fixed;right:16px;top:78px;width:326px;z-index:6;padding:15px;
     max-height:calc(100vh - 250px);overflow-y:auto;overscroll-behavior:contain;
     animation:insp-in .18s ease}
  #insp[hidden]{display:none}
  @keyframes insp-in{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
  .p-body::-webkit-scrollbar,#insp::-webkit-scrollbar{width:8px}
  .p-body::-webkit-scrollbar-thumb,#insp::-webkit-scrollbar-thumb{background:#2b333d;border-radius:8px}
  .p-body::-webkit-scrollbar-track,#insp::-webkit-scrollbar-track{background:transparent}
  #insp .close{position:absolute;right:11px;top:11px;width:24px;height:24px;padding:0;display:flex;
     align-items:center;justify-content:center;border:1px solid transparent;border-radius:6px;
     background:none;color:var(--muted);cursor:pointer}
  #insp .close:hover{color:var(--ink);border-color:var(--line)}
  #insp .close svg{width:12px;height:12px}
  #iTitle{margin:6px 34px 8px 0;font-size:15.5px;font-weight:650;line-height:1.32}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;border-radius:999px;
     border:1px solid var(--line);background:var(--surface-2);font-size:11px;color:var(--ink-2)}
  .chip .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .meter{height:3px;border-radius:3px;background:#20262e;overflow:hidden;margin:0 0 12px}
  .meter i{display:block;height:100%;border-radius:3px;background:var(--accent)}
  .facts{display:grid;grid-template-columns:auto 1fr;gap:5px 12px;margin:0 0 14px;font-size:12px}
  .facts dt{color:var(--muted);white-space:nowrap}
  .facts dd{margin:0;color:var(--ink-2);overflow-wrap:anywhere}
  .facts dd.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
     color:var(--muted)}
  .facts dd[data-tone="fresh"]{color:#e8eef7}
  .facts dd[data-tone="recent"]{color:#c3d0de}
  .facts dd[data-tone="settled"]{color:#9fadbe}
  .facts dd[data-tone="dormant"]{color:var(--warn)}
  .facts dd[data-tone="oxidized"]{color:var(--alert)}
  .facts dd[data-tone="unknown"]{color:var(--muted-2)}
  .facts dd[data-tone="flag"]{color:var(--warn)}
  .isec{border-top:1px solid var(--line-soft);padding-top:11px;margin-top:11px}
  .isec .eyebrow{display:block;margin-bottom:6px}
  #iSummary{margin:0;font-size:12.5px;line-height:1.55;color:var(--ink-2)}
  .tags{display:flex;flex-wrap:wrap;gap:5px}
  .tag{font-size:10.5px;padding:2px 7px;border-radius:999px;background:#1e242c;color:#a9b6c5}
  .nb a{display:flex;align-items:center;gap:7px;padding:4px 0;color:#a8c9e4;font-size:12.5px;
     text-decoration:none;cursor:pointer}
  .nb a:hover{color:#d5e7f7}
  .nb a .dot{width:6px;height:6px;border-radius:50%;flex:0 0 auto}
  .nb a span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .nb .more{color:var(--muted-2);font-size:11px;padding-top:4px}

  /* ── timeline ─────────────────────────────────────────────────────────── */
  #tl{position:fixed;left:16px;right:16px;bottom:16px;height:118px;padding:11px 15px 3px;z-index:4}
  #tl .head{display:flex;align-items:center;gap:13px}
  #tl .play{display:flex;align-items:center;gap:6px;cursor:pointer;border:1px solid var(--line);
     background:var(--surface-2);color:var(--ink-2);border-radius:var(--r-sm);padding:5px 11px;
     font-size:12px;font-weight:550}
  #tl .play:hover{border-color:#33404e;color:var(--ink)}
  #tl .play[aria-pressed="true"]{border-color:var(--accent);color:var(--ink)}
  #tl .play svg{width:11px;height:11px}
  #tl .read{margin-left:auto;display:flex;gap:12px;align-items:baseline}
  #tl .read .now{font-size:12.5px;font-variant-numeric:tabular-nums;color:var(--ink)}
  #tl .read .tot{font-size:11.5px;font-variant-numeric:tabular-nums;color:var(--muted)}
  #tlchart{position:relative;width:100%;height:82px;cursor:ew-resize}
  #tlsvg{width:100%;height:100%;display:block;touch-action:none}
  #tltip{position:absolute;top:-4px;transform:translateX(-50%);pointer-events:none;display:none;
     background:#0d1117;border:1px solid var(--line);border-radius:var(--r-sm);padding:7px 9px;
     font-size:11px;white-space:nowrap;z-index:6;box-shadow:0 8px 24px rgba(0,0,0,.5)}
  #tltip .tt-h{font-weight:600;margin-bottom:4px;font-variant-numeric:tabular-nums}
  #tltip .tt-r{display:flex;align-items:center;gap:6px;color:var(--ink-2)}
  #tltip .tt-r i{width:7px;height:7px;border-radius:50%}
  #tltip .tt-r b{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
  #hint{position:fixed;left:50%;bottom:148px;transform:translateX(-50%);color:var(--muted);
     font-size:11.5px;z-index:4;pointer-events:none;opacity:0;transition:opacity .5s ease}
  #hint.show{opacity:1}

  /* ── responsive ───────────────────────────────────────────────────────── */
  @media (max-width: 980px){
    #dock{left:10px;right:10px;top:10px;gap:11px;padding:8px 11px}
    #dock .src{max-width:34vw}
    #panel{left:10px;top:72px;width:min(238px,54vw);max-height:42vh;padding:11px}
    #insp{left:10px;right:10px;top:auto;bottom:146px;width:auto;max-height:44vh;padding:13px}
    #tl{left:10px;right:10px;bottom:10px;height:108px;padding:10px 12px 2px}
    #tlchart{height:74px}
    #hint{display:none}
  }
  @media (max-width: 680px){
    #dock .src,#dock .vsep,#dock .stat.opt{display:none}
    #panel{width:64vw}
    #tl .head .eyebrow{display:none}
  }
  @media (prefers-reduced-motion: reduce){
    *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
      transition-duration:.01ms!important;scroll-behavior:auto!important}
  }
</style></head>
<body>
<div id="sky"></div>
<div id="cy" role="application" aria-label="Knowledge constellation"></div>

<header id="dock" class="panel">
  <div class="ident">
    <h1 id="ttl">Knowledge Map</h1>
    <div class="src">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 7.5V18a1.5 1.5 0 001.5 1.5h15A1.5 1.5 0 0021 18V9a1.5 1.5 0 00-1.5-1.5h-7L10 5H4.5A1.5 1.5 0 003 6.5z"/></svg>
      <span id="srcTxt"></span>
    </div>
  </div>
  <span class="vsep"></span>
  <div class="stat"><b id="cShown">—</b><i>notes shown</i></div>
  <div class="stat opt"><b id="cLinks">—</b><i>links</i></div>
  <div class="stat opt"><b id="cSpan">—</b><i id="cAsOf">span</i></div>
  <span class="vsep"></span>
  <div id="rt"><span class="d"></span><b id="rtLabel"></b></div>
</header>

<section id="panel" class="panel" aria-label="Explore controls">
  <div class="p-head">
    <div class="p-title">
      <span class="eyebrow">Explore</span>
      <button class="minbtn" id="panelMin" aria-expanded="true" aria-controls="panelBody" title="Collapse controls">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
    </div>
    <div class="searchwrap">
      <svg class="s-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>
      <input id="search" type="search" placeholder="Search notes and tags" aria-label="Search notes and tags" autocomplete="off" spellcheck="false">
      <kbd class="kbd" id="kbd">Ctrl K</kbd>
    </div>
    <p class="s-info" id="searchInfo" role="status" hidden></p>
    <div class="tools">
      <button class="tool" id="fit" title="Fit the whole constellation">Reset</button>
      <button class="tool on" id="toggleEdges" aria-pressed="true" title="Show or hide links">Links</button>
      <button class="tool audit" id="audit" aria-pressed="false" title="Isolate oxidized and orphan notes">Audit <span class="badge" id="auditCt">0</span></button>
    </div>
    <p class="note" id="auditNote" role="status" hidden></p>
  </div>
  <div class="p-body" id="panelBody">
    <details class="grp" id="grpThemes" open>
      <summary>
        <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        <span class="eyebrow">Themes</span><span class="ct" id="ctThemes"></span>
      </summary>
      <div class="rows" id="themeFilters"></div>
    </details>
    <details class="grp" id="grpTypes">
      <summary>
        <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        <span class="eyebrow">Types</span><span class="ct" id="ctTypes"></span>
      </summary>
      <div class="rows" id="typeFilters"></div>
    </details>
    <details class="grp" id="grpFresh" open>
      <summary>
        <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        <span class="eyebrow">Freshness</span><span class="ct" id="ctFresh"></span>
      </summary>
      <div class="rows" id="freshFilters"></div>
    </details>
  </div>
</section>

<aside id="insp" class="panel" aria-live="polite" hidden>
  <button class="close" id="inspClose" aria-label="Close inspector">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
  </button>
  <span class="eyebrow">Note</span>
  <h2 id="iTitle"></h2>
  <div class="chips" id="iChips"></div>
  <div class="meter" title="Continuous freshness score"><i id="iMeter"></i></div>
  <dl class="facts" id="iFacts"></dl>
  <div class="isec"><span class="eyebrow">Summary</span><p id="iSummary"></p></div>
  <div class="isec" id="iTagsSec"><span class="eyebrow">Tags</span><div class="tags" id="iTags"></div></div>
  <div class="isec" id="iLinksSec"><span class="eyebrow" id="iLinksHead">Connected</span><div class="nb" id="iLinks"></div></div>
</aside>

<footer id="tl" class="panel">
  <div class="head">
    <span class="eyebrow" id="tlEyebrow">Timeline &middot; cumulative</span>
    <button class="play" id="play" aria-pressed="false">
      <svg viewBox="0 0 24 24" fill="currentColor" id="icPlay"><path d="M8 5l12 7-12 7z"/></svg>
      <svg viewBox="0 0 24 24" fill="currentColor" id="icPause" class="h"><path d="M7 5h4v14H7zm6 0h4v14h-4z"/></svg>
      <span id="playLab">Play growth</span>
    </button>
    <span class="read"><span class="now" id="now"></span><span class="tot" id="tot"></span></span>
  </div>
  <div id="tlchart"><svg id="tlsvg"></svg><div id="tltip"></div></div>
</footer>
<div id="hint">drag the timeline to travel through time &middot; click a node to inspect it</div>

<script>
const DATA = /*__DATA__*/;
const TC = DATA.themeColors;
const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const DUR = REDUCED ? 0 : 300;
const $ = id => document.getElementById(id);

// ---- freshness vocabulary (bands label + filter; the score drives rendering) --
const BANDS = {
  fresh:    {label:'Fresh',    hint:'updated within 7 days', color:'#e8eef7'},
  recent:   {label:'Recent',   hint:'8 to 30 days',          color:'#bcc9d8'},
  settled:  {label:'Settled',  hint:'31 to 90 days',         color:'#93a2b5'},
  dormant:  {label:'Dormant',  hint:'91 to 365 days',        color:'#d5a163'},
  oxidized: {label:'Oxidized', hint:'over a year old',       color:'#cd7361'},
  unknown:  {label:'Unknown',  hint:'no usable date',        color:'#5d6773'}
};
const BAND_ORDER = DATA.freshnessOrder || Object.keys(BANDS);
const SOURCE_LABEL = {
  gbrain_history:'GBrain updated_at (verified by page history)',
  gbrain_revision:'GBrain page history (last content revision)',
  gbrain_updated:'GBrain updated_at (history not read)',
  last_updated:'frontmatter last_updated', updated:'frontmatter updated',
  modified:'frontmatter modified', created:'frontmatter created (no update field)',
  filesystem:'file timestamp (no date in frontmatter)'
};
const HISTORY_NOTE = {
  verified:'confirmed by GBrain page history',
  superseded:'last backend touch was not a content write',
  'single-write':'written once, never revised',
  inconsistent:'page history disagrees with the backend timestamp — neither is confirmed',
  unverified:'page history was not read',
  error:'page history unavailable for this note'
};
// GBrain enrichment is optional; DATA.gbrain always says which mode produced
// the timestamps, so the map never implies a freshness it did not measure.
const GB = DATA.gbrain || {status:'off', detail:''};

// ---- header ----
$('ttl').textContent = DATA.title;
// A partial read says so in the header rather than passing for a clean one;
// the reason is in `detail`, which is the tooltip.
const GB_TAG = {ok:' \u00b7 gbrain', partial:' \u00b7 gbrain (partial)'};
$('srcTxt').textContent = DATA.source + (GB_TAG[GB.status] || '');
$('srcTxt').title = DATA.source + (GB.detail ? ' \u2014 ' + GB.detail : '');
if(GB.status === 'partial') $('srcTxt').dataset.tone = 'flag';
$('cLinks').textContent = DATA.stats.edges;
const SPAN = DATA.stats.span;
$('cSpan').textContent = (SPAN[0] && SPAN[1]) ? SPAN[0] + ' to ' + SPAN[1] : 'undated';
$('cAsOf').textContent = 'as of ' + (DATA.stats.asOf || '').slice(0,10);
const RT = DATA.runtime || {mode:'network', label:'Network runtime'};
$('rtLabel').textContent = RT.label;
$('rt').classList.toggle('local', RT.mode === 'local');
$('rt').title = RT.detail ? RT.detail + ' (' + RT.source + ')' : RT.source || '';
$('kbd').textContent = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? '⌘ K' : 'Ctrl K';

// ---- continuous freshness -> colour/luminance/halo ----
const GRAPHITE = [0x6b, 0x74, 0x80];
function themeColor(t){ return TC[t] || TC.Other || '#8b96a5'; }
function rgbOf(hex){
  let h = String(hex).replace('#','');
  if(h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  const v = parseInt(h, 16) || 0;
  return [(v>>16)&255, (v>>8)&255, v&255];
}
function toward(hex, t){                       // t=0 keep hue, t=1 flat graphite
  const c = rgbOf(hex);
  return 'rgb(' + c.map((v,i)=>Math.round(v + (GRAPHITE[i]-v)*t)).join(',') + ')';
}
// nodes with no date at all sit low but not at zero, so they stay findable
const FS = n => { const s = n.data('freshnessScore'); return typeof s === 'number' ? s : 0.22; };
const colorCache = {};
function nodeColor(n){
  const step = Math.round(FS(n) * 16);
  const key = n.data('theme') + '|' + step;
  if(!(key in colorCache)) colorCache[key] = toward(themeColor(n.data('theme')), 0.62*(1 - step/16));
  return colorCache[key];
}

function layoutOpts(){
  if(DATA.layout === 'cose'){
    // the builder ships deterministic theme-seeded positions; starting cose from
    // them (randomize off) keeps stdlib-only builds deterministic too
    const seeded = !!(DATA.nodes[0] && DATA.nodes[0].position);
    return { name:'cose', animate:false, randomize:!seeded, fit:true, padding:70,
             nodeRepulsion:9000, idealEdgeLength:62, edgeElasticity:0.4,
             gravity:0.35, numIter:1200, coolingFactor:0.95, nodeDimensionsIncludeLabels:false };
  }
  return { name:'preset' };
}
const cy = cytoscape({
  container: $('cy'),
  elements: { nodes: DATA.nodes, edges: DATA.edges },
  layout: layoutOpts(),
  wheelSensitivity: 0.22,
  hideEdgesOnViewport: DATA.edges.length > 1500,   // keep pan/zoom fluid on big graphs
  pixelRatio: Math.min(window.devicePixelRatio||1, 2),
  style: [
    { selector:'node', style:{
        'width':'data(size)','height':'data(size)',
        'shape': n => DATA.typeShapes[n.data('type')] || 'ellipse',
        'background-color': nodeColor,                                  // saturation falls with age
        'background-opacity': n => 0.48 + 0.50*FS(n),                   // luminance falls with age
        'background-blacken': n => 0.30*(1 - FS(n)),
        'underlay-color': n => themeColor(n.data('theme')),             // halo only for fresh notes
        'underlay-shape':'ellipse',
        'underlay-padding': n => 1 + 9*Math.pow(FS(n), 1.6),
        'underlay-opacity': n => 0.03 + 0.26*Math.pow(FS(n), 2),
        'border-width': n => n.data('freshness') === 'unknown' ? 1 : 0,
        'border-color':'#3c4653','border-style':'dashed','border-opacity':0.9,
        'label':'','transition-property':'opacity','transition-duration':'120ms'
    }},
    // semantic hierarchy: landmark hubs named even at overview zoom, ordinary
    // hubs named once you approach, mid-degree names appear zoomed right in
    { selector:'node.hub', style:{ 'label':'data(title)','color':'#cdd8e6','font-size':11,
        'font-weight':600,'text-outline-width':2.4,'text-outline-color':'#0a0c10',
        'text-max-width':126,'text-wrap':'ellipsis','min-zoomed-font-size':7,
        'border-width':1.2,'border-style':'solid','border-opacity':0.5,
        'border-color': n => themeColor(n.data('theme')) }},
    { selector:'node.landmark', style:{ 'font-size':26,'color':'#dfe7f1',
        'text-outline-width':3.5,'text-max-width':300,'min-zoomed-font-size':6 }},
    { selector:'node.lab', style:{ 'label':'data(title)','color':'#9fabba','font-size':9.5,
        'text-outline-width':2,'text-outline-color':'#0a0c10','text-max-width':104,
        'text-wrap':'ellipsis','min-zoomed-font-size':8 }},
    { selector:'edge', style:{                                          // quiet by default
        'width':0.8,'curve-style':'haystack','haystack-radius':0,
        'opacity':0.12,'line-color':'#63707e' }},
    { selector:'edge.hov', style:{ 'opacity':0.4,'width':1.3,
        'line-color': e => themeColor(e.data('theme')) }},
    { selector:'node.sdim', style:{ 'opacity':0.15 }},                  // search: non-matches recede
    { selector:'.dim', style:{ 'opacity':0.05 }},
    { selector:'node.hl', style:{ 'opacity':1,'label':'data(title)','color':'#e7ecf3','font-size':11.5,
        'text-outline-width':0,'text-background-color':'#0a0c10','text-background-opacity':0.82,
        'text-background-padding':3,'text-background-shape':'roundrectangle','z-index':50,
        'min-zoomed-font-size':0 }},
    { selector:'edge.hl', style:{ 'opacity':0.7,'width':1.5,
        'line-color': e => themeColor(e.data('theme')) }},
    // the selected node reads at full strength whatever its age
    { selector:'node.sel', style:{ 'background-color': n => themeColor(n.data('theme')),
        'background-opacity':1,'background-blacken':0,
        'border-width':2,'border-color':'#f1f5fa','border-style':'solid','border-opacity':1,
        'underlay-opacity':0.34,'underlay-padding':9,
        'label':'data(title)','color':'#ffffff','font-size':13,'font-weight':650,
        'text-background-color':'#0a0c10','text-background-opacity':0.9,'text-background-padding':4,
        'text-background-shape':'roundrectangle','text-max-width':180,'text-wrap':'wrap',
        'z-index':99,'min-zoomed-font-size':0 }},
    { selector:'node.search', style:{ 'border-width':2.5,'border-color':'#f1f5fa','border-style':'solid',
        'border-opacity':1,'z-index':40 }},
    // audit lens: only while Audit is active, so bands never colour the default view
    { selector:'node.flag', style:{ 'border-width':2,'border-color':'#d5a163','border-style':'solid',
        'border-opacity':1 }},
    { selector:'node.flag.oxid', style:{ 'border-color':'#cd7361' }},
    { selector:'.hidden', style:{ 'display':'none' }}
  ]
});

cy.nodes().forEach(n => {
  const d = n.data('deg');
  if(d >= 7) n.addClass('hub'); else if(d >= 3) n.addClass('mid');
});
// the strongest hubs stay named even at overview zoom — deterministic pick:
// degree desc, then id, so the same map always names the same landmarks
cy.nodes('.hub').sort((a,b) => b.data('deg') - a.data('deg') ||
                               (a.id() < b.id() ? -1 : 1))
  .slice(0, 12).filter(n => n.data('deg') >= 10).addClass('landmark');
let wideLabels = false;
cy.on('zoom', () => {
  const on = cy.zoom() > 1.2;
  if(on === wideLabels) return;
  wideLabels = on;
  cy.batch(() => cy.nodes('.mid').toggleClass('lab', on));
});

// ---- filter state ----
const themeOn = {}, typeOn = {}, freshOn = {};
Object.keys(DATA.stats.themes).forEach(t => themeOn[t] = true);
Object.keys(DATA.stats.types).forEach(t => typeOn[t] = true);
BAND_ORDER.forEach(b => freshOn[b] = true);
let auditOn = false, edgesVisible = true, cutoff = 1.0;
const syncers = [];

function checkMark(){
  const s = document.createElementNS('http://www.w3.org/2000/svg','svg');
  s.setAttribute('viewBox','0 0 24 24'); s.setAttribute('fill','none');
  s.setAttribute('stroke','currentColor'); s.setAttribute('stroke-width','3.4');
  s.setAttribute('stroke-linecap','round'); s.setAttribute('stroke-linejoin','round');
  const p = document.createElementNS('http://www.w3.org/2000/svg','path');
  p.setAttribute('d','M5 13l4.5 4.5L19 7'); s.append(p);
  return s;
}
// static outline glyphs mirroring the node shapes, so the Types list doubles
// as the legend for what each shape means in the graph
const SHAPE_GLYPHS = {
  'ellipse':'<circle cx="6" cy="6" r="4.6"/>',
  'round-rectangle':'<rect x="1.8" y="1.8" width="8.4" height="8.4" rx="2"/>',
  'hexagon':'<polygon points="6.0,1.4 10.0,3.7 10.0,8.3 6.0,10.6 2.0,8.3 2.0,3.7"/>',
  'round-pentagon':'<polygon points="6.0,1.4 10.4,4.6 8.7,9.7 3.3,9.7 1.6,4.6"/>',
  'diamond':'<polygon points="6.0,1.2 10.8,6.0 6.0,10.8 1.2,6.0"/>',
  'round-diamond':'<polygon points="6.0,1.2 10.8,6.0 6.0,10.8 1.2,6.0"/>',
  'star':'<polygon points="6.0,1.1 7.2,4.3 10.7,4.5 8.0,6.6 8.9,10.0 6.0,8.1 3.1,10.0 4.0,6.6 1.3,4.5 4.8,4.3"/>',
  'round-tag':'<path d="M1.8 3h5.4L10.6 6 7.2 9H1.8z"/>'
};
function shapeGlyph(shape){
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 12 12'); svg.setAttribute('class','glyph');
  svg.setAttribute('fill','none'); svg.setAttribute('stroke','currentColor');
  svg.setAttribute('stroke-width','1.3'); svg.setAttribute('stroke-linejoin','round');
  svg.innerHTML = SHAPE_GLYPHS[shape] || SHAPE_GLYPHS.ellipse;   // static chrome, never note data
  return svg;
}
function buildFilters(elId, entries, state){
  const box = $(elId);
  entries.forEach(item => {
    const row = document.createElement('div');
    row.className = 'filter'; row.tabIndex = 0; row.setAttribute('role','checkbox');
    if(item.hint) row.title = item.label + ' — ' + item.hint;
    const bx = document.createElement('span'); bx.className = 'box'; bx.append(checkMark());
    const mark = item.shape ? shapeGlyph(item.shape)
      : (() => { const sw = document.createElement('span'); sw.className = 'sw';
                 sw.style.background = item.color; return sw; })();
    const nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = item.label;
    const only = document.createElement('button'); only.type = 'button'; only.className = 'only';
    only.textContent = 'only'; only.setAttribute('aria-label', 'Show only ' + item.label);
    only.onclick = ev => {
      ev.stopPropagation();
      // second press on an already-solo entry restores the whole group
      const solo = entries.every(e2 => !!state[e2.key] === (e2.key === item.key));
      entries.forEach(e2 => state[e2.key] = solo ? true : e2.key === item.key);
      syncers.forEach(s => s()); apply();
    };
    const ct = document.createElement('span'); ct.className = 'ct'; ct.textContent = item.count;
    row.append(bx, mark, nm, only, ct); box.append(row);
    const sync = () => { const on = !!state[item.key];
      row.classList.toggle('off', !on); row.setAttribute('aria-checked', String(on)); };
    const toggle = () => { state[item.key] = !state[item.key]; sync(); apply(); };
    row.onclick = toggle;
    row.onkeydown = e => { if(e.target !== row) return;
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggle(); } };
    syncers.push(sync);
    sync();
  });
}
const themeEntries = (DATA.themeOrder || Object.keys(DATA.stats.themes))
  .filter(t => DATA.stats.themes[t])
  .map(t => ({key:t, label:t, count:DATA.stats.themes[t], color:themeColor(t)}));
const typeEntries = Object.keys(DATA.stats.types)
  .sort((a,b) => DATA.stats.types[b] - DATA.stats.types[a] || a.localeCompare(b))
  .map(t => ({key:t, label:t, count:DATA.stats.types[t],
              shape:DATA.typeShapes[t] || 'ellipse'}));
const freshEntries = BAND_ORDER
  .filter(b => (DATA.stats.freshness || {})[b])
  .map(b => ({key:b, label:BANDS[b].label, hint:BANDS[b].hint,
              count:DATA.stats.freshness[b], color:BANDS[b].color}));
buildFilters('themeFilters', themeEntries, themeOn);
buildFilters('typeFilters', typeEntries, typeOn);
buildFilters('freshFilters', freshEntries, freshOn);

function groupCount(el, entries, state){
  const on = entries.filter(e => state[e.key]).length;
  $(el).textContent = on + '/' + entries.length;
}

// ---- timeline (cumulative growth, SVG) ----
const months = DATA.timeline.map(m => m.month);
function cutoffMonth(){
  const idx = Math.max(0, Math.ceil(cutoff*months.length) - 1);
  return months[idx] || months[months.length-1] || '';
}
const TL_ORDER = DATA.themeOrder || Object.keys(DATA.themeColors);
// Creation growth is the stacked area; revisions are a separate line, because a
// quiet month for new notes can still be a busy month for rewrites.
const REVS = DATA.revisions || {};
const REV = (REVS.available && Array.isArray(REVS.series) &&
             REVS.series.length === DATA.timeline.length) ? REVS.series : null;
if(REV) $('tlEyebrow').textContent = 'Timeline \u00b7 cumulative + revisions';
const MN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const chart = $('tlchart'), svg = $('tlsvg'), tip = $('tltip');
let TLG = null;
function shortMonth(ym){ const p = String(ym).split('-'); return (MN[(+p[1])-1] || p[1]) + " '" + String(p[0]).slice(2); }

function buildTimeline(){
  const PAD = {l:14, r:14, t:13, b:19};
  const w = chart.clientWidth || 900, h = chart.clientHeight || 96;
  const iw = w-PAD.l-PAD.r, ih = h-PAD.t-PAD.b, n = DATA.timeline.length;
  if(!n){ TLG = null; return; }
  const cum = {}; TL_ORDER.forEach(t => cum[t] = 0);
  const pts = DATA.timeline.map(m => { TL_ORDER.forEach(t => cum[t] += m[t]||0); return Object.assign({}, cum); });
  const total = TL_ORDER.reduce((s,t) => s+cum[t], 0) || 1;
  const X = i => PAD.l + (n>1 ? i/(n-1) : 0)*iw;
  const Y = v => PAD.t + ih - v/total*ih;
  function areaPath(k){
    const top = i => { let s=0; for(let j=0;j<=k;j++) s += pts[i][TL_ORDER[j]]; return s; };
    const bot = i => { let s=0; for(let j=0;j<k;j++) s += pts[i][TL_ORDER[j]]; return s; };
    let d = 'M'+X(0).toFixed(1)+','+Y(top(0)).toFixed(1);
    for(let i=1;i<n;i++) d += ' L'+X(i).toFixed(1)+','+Y(top(i)).toFixed(1);
    for(let i=n-1;i>=0;i--) d += ' L'+X(i).toFixed(1)+','+Y(bot(i)).toFixed(1);
    return d+'Z';
  }
  let defs = '<defs>';
  TL_ORDER.forEach((t,k) => { const c = themeColor(t);   // id by index — theme names may hold spaces/non-ASCII
    defs += '<linearGradient id="g_'+k+'" x1="0" y1="0" x2="0" y2="1">'+
      '<stop offset="0" stop-color="'+c+'" stop-opacity="0.82"/>'+
      '<stop offset="1" stop-color="'+c+'" stop-opacity="0.22"/></linearGradient>'; });
  defs += '<clipPath id="past"><rect id="pastrect" x="'+PAD.l+'" y="0" width="0" height="'+h+'"/></clipPath></defs>';
  let dim = '', bright = '';
  TL_ORDER.forEach((t,k) => { if(!cum[t]) return; const p = areaPath(k);
    dim += '<path d="'+p+'" fill="url(#g_'+k+')" opacity="0.12"/>';
    bright += '<path d="'+p+'" fill="url(#g_'+k+')" opacity="0.9"/>'; });
  let revline = '';
  if(REV){
    const peak = Math.max.apply(null, REV) || 1;
    const RY = v => PAD.t + ih - (v/peak)*ih*0.62;
    let d = '';
    for(let i=0;i<n;i++) d += (i ? ' L' : 'M') + X(i).toFixed(1) + ',' + RY(REV[i]||0).toFixed(1);
    revline = '<path d="'+d+'" fill="none" stroke="#d5a163" stroke-width="1.3" ' +
              'stroke-opacity="0.85" stroke-linejoin="round" stroke-linecap="round"/>' +
              (n === 1 ? '<circle cx="'+X(0).toFixed(1)+'" cy="'+RY(REV[0]||0).toFixed(1)+
                         '" r="2" fill="#d5a163"/>' : '');
  }
  let axis = '<line x1="'+PAD.l+'" y1="'+(PAD.t+ih)+'" x2="'+(w-PAD.r)+'" y2="'+(PAD.t+ih)+
    '" stroke="rgba(198,212,230,.16)"/>';
  const step = Math.max(1, Math.ceil(n/Math.max(3, Math.floor(iw/66))));
  for(let i=0;i<n;i+=step){
    axis += '<text x="'+X(i).toFixed(1)+'" y="'+(h-5)+'" fill="#6e7986" font-size="9.5" text-anchor="middle">'+shortMonth(months[i])+'</text>';
    axis += '<line x1="'+X(i).toFixed(1)+'" y1="'+(PAD.t+ih)+'" x2="'+X(i).toFixed(1)+'" y2="'+(PAD.t+ih+3)+'" stroke="rgba(198,212,230,.2)"/>';
  }
  const ph = '<g id="phead"><line y1="'+(PAD.t-3)+'" y2="'+(PAD.t+ih+3)+'" stroke="#e7ecf3" stroke-width="1.4" opacity="0.85"/>'+
    '<circle cy="'+(PAD.t-3)+'" r="3.4" fill="#e7ecf3"/></g>';
  svg.setAttribute('viewBox','0 0 '+w+' '+h);
  svg.innerHTML = defs+'<g>'+dim+'</g><g clip-path="url(#past)">'+bright+'</g>'+revline+axis+ph;
  TLG = {PAD, iw, n, pts};
  renderPlayhead();
}
function renderPlayhead(){
  if(!TLG) return;
  const x = TLG.PAD.l + cutoff*TLG.iw;
  const rect = $('pastrect'); if(rect) rect.setAttribute('width', Math.max(0, x-TLG.PAD.l).toFixed(1));
  const g = $('phead'); if(g) g.setAttribute('transform','translate('+x.toFixed(1)+',0)');
  const i = Math.max(0, Math.ceil(cutoff*TLG.n) - 1);
  const upto = TL_ORDER.reduce((s,t) => s + (TLG.pts[i][t]||0), 0);
  $('now').textContent = cutoffMonth();
  let read = upto + ' notes created';
  if(REV){
    let revised = 0;
    for(let j=0;j<=i;j++) revised += REV[j]||0;
    read += ' \u00b7 ' + revised + ' revisions';
  }
  $('tot').textContent = read;
}
function tlSet(ev){ if(!TLG) return; const r = svg.getBoundingClientRect();
  setCutoff(((ev.clientX-r.left) - TLG.PAD.l)/TLG.iw); }
function showTip(ev){
  if(!TLG) return;
  const r = svg.getBoundingClientRect();
  const i = Math.round(((ev.clientX-r.left) - TLG.PAD.l)/TLG.iw*(TLG.n-1));
  if(i < 0 || i >= TLG.n){ tip.style.display = 'none'; return; }
  const m = DATA.timeline[i];
  const sum = TL_ORDER.reduce((s,t) => s + (m[t]||0), 0);
  tip.textContent = '';
  const head = document.createElement('div'); head.className = 'tt-h';
  head.textContent = shortMonth(months[i]) + '  +' + sum;
  tip.append(head);
  TL_ORDER.filter(t => (m[t]||0) > 0).forEach(t => {
    const row = document.createElement('div'); row.className = 'tt-r';
    const dot = document.createElement('i'); dot.style.background = themeColor(t);
    const nm = document.createElement('span'); nm.textContent = t;
    const ct = document.createElement('b'); ct.textContent = m[t];
    row.append(dot, nm, ct); tip.append(row);
  });
  if(REV && REV[i]){
    const row = document.createElement('div'); row.className = 'tt-r';
    const dot = document.createElement('i'); dot.style.background = '#d5a163';
    const nm = document.createElement('span'); nm.textContent = 'revisions';
    const ct = document.createElement('b'); ct.textContent = REV[i];
    row.append(dot, nm, ct); tip.append(row);
  }
  tip.style.display = 'block';
  tip.style.left = (ev.clientX - r.left) + 'px';
}
let tlDrag = false;
svg.addEventListener('pointerdown', e => { tlDrag = true; svg.setPointerCapture(e.pointerId); tlSet(e); });
svg.addEventListener('pointermove', e => { if(tlDrag) tlSet(e); showTip(e); });
svg.addEventListener('pointerup', () => { tlDrag = false; });
svg.addEventListener('pointerleave', () => { tip.style.display = 'none'; });
window.addEventListener('resize', buildTimeline);

// ---- filtering ----
function withinTime(n){
  const c = (n.data('created')||'').slice(0,7);
  if(!c) return true;
  return c <= cutoffMonth();
}
function isFlagged(n){ return n.data('freshness') === 'oxidized' || n.data('orphan'); }
function apply(){
  const q = ($('search').value||'').toLowerCase().trim();
  let hits = 0;
  cy.batch(() => {
    cy.nodes().forEach(n => {
      const ok = themeOn[n.data('theme')] && typeOn[n.data('type')] &&
                 freshOn[n.data('freshness')] && withinTime(n) &&
                 (!auditOn || isFlagged(n));
      n.toggleClass('hidden', !ok);
      n.toggleClass('flag', auditOn && ok && isFlagged(n));
      n.toggleClass('oxid', n.data('freshness') === 'oxidized');
      if(q){
        const hit = ok && ((n.data('title')||'').toLowerCase().includes(q) ||
                           (n.data('tags')||[]).join(' ').toLowerCase().includes(q));
        if(hit) hits++;
        n.toggleClass('search', hit);
        n.toggleClass('sdim', ok && !hit);   // non-matches recede so hits ring out
      } else n.removeClass('search sdim');
    });
    cy.edges().forEach(e => {
      const vis = edgesVisible && !e.source().hasClass('hidden') && !e.target().hasClass('hidden');
      e.toggleClass('hidden', !vis);
    });
  });
  const shown = cy.nodes().filter(n => !n.hasClass('hidden')).length;
  $('cShown').textContent = shown + ' / ' + DATA.stats.nodes;
  $('cLinks').textContent = (edgesVisible ? cy.edges().filter(e => !e.hasClass('hidden')).length
                                          : 0) + ' / ' + DATA.stats.edges;
  $('search').setAttribute('aria-label', q ? hits + ' matches for "' + q + '"' : 'Search notes and tags');
  const si = $('searchInfo');
  si.hidden = !q;
  if(q) si.textContent = hits ? hits + (hits === 1 ? ' match' : ' matches') + ' — Esc clears'
                              : 'No matches in the current view';
  groupCount('ctThemes', themeEntries, themeOn);
  groupCount('ctTypes', typeEntries, typeOn);
  groupCount('ctFresh', freshEntries, freshOn);
  $('now').textContent = cutoffMonth();
}
function setCutoff(f){ cutoff = Math.min(1, Math.max(0, f)); renderPlayhead(); apply(); }
$('search').oninput = apply;

// ---- audit lens ----
const AUDIT = DATA.stats.audit || {flagged:0, oxidized:0, orphans:0};
$('auditCt').textContent = AUDIT.flagged;
function setAudit(on){
  auditOn = on;
  const btn = $('audit');
  btn.classList.toggle('on', on);
  btn.setAttribute('aria-pressed', String(on));
  const note = $('auditNote');
  note.textContent = '';
  if(on){
    // the lens is useless with its own bands filtered out
    if(!freshOn.oxidized){ freshOn.oxidized = true; syncers.forEach(s => s()); }
    const strong = document.createElement('b');
    strong.textContent = AUDIT.flagged + ' flagged';
    note.append(strong, document.createTextNode(
      ' — ' + AUDIT.oxidized + ' oxidized (over a year), ' + AUDIT.orphans + ' orphan (no links). ' +
      'Theme, type and timeline filters still apply.'));
    note.hidden = false;
  } else note.hidden = true;
  apply();
}
$('audit').onclick = () => setAudit(!auditOn);

// ---- inspector ----
function fmtStamp(iso){
  if(!iso) return 'none';
  const d = iso.slice(0,10), t = iso.slice(11,16);
  return t === '00:00' ? d : d + ' ' + t + ' UTC';
}
function fmtAge(days){
  if(days === null || days === undefined) return 'unknown';
  if(days === 0) return '0 days (today)';
  return days + (days === 1 ? ' day' : ' days');
}
function chip(box, text, color){
  const c = document.createElement('span'); c.className = 'chip';
  if(color){ const d = document.createElement('span'); d.className = 'dot'; d.style.background = color; c.append(d); }
  const t = document.createElement('span'); t.textContent = text; c.append(t);
  box.append(c); return c;
}
function fact(dl, label, value, tone, mono){
  const dt = document.createElement('dt'); dt.textContent = label;
  const dd = document.createElement('dd'); dd.textContent = value;
  if(tone) dd.dataset.tone = tone;
  if(mono) dd.className = 'mono';
  dl.append(dt, dd);
}
function inspect(n){
  const d = n.data();
  const band = BANDS[d.freshness] || BANDS.unknown;
  $('insp').hidden = false;
  $('iTitle').textContent = d.title;

  const chips = $('iChips'); chips.textContent = '';
  chip(chips, d.theme, themeColor(d.theme));
  chip(chips, d.type);
  chip(chips, band.label, band.color);
  if(d.orphan) chip(chips, 'orphan', '#d5a163');

  const score = typeof d.freshnessScore === 'number' ? d.freshnessScore : 0;
  const meter = $('iMeter');
  meter.style.width = Math.round(score*100) + '%';
  meter.style.background = band.color;
  meter.parentNode.title = 'Freshness score ' + score.toFixed(2) + ' of 1.00 (' + band.hint + ')';

  const dl = $('iFacts'); dl.textContent = '';
  fact(dl, 'Freshness', band.label + ' — ' + band.hint, d.freshness);
  fact(dl, 'Age', fmtAge(d.ageDays), d.freshness);
  fact(dl, 'Updated', fmtStamp(d.updated));
  fact(dl, 'Source', SOURCE_LABEL[d.freshnessSource] || 'none');
  // `revisions` is null whenever the count is unknown — including when the
  // history read for this page failed — so the count cannot be the condition
  // for showing what happened. The status is.
  const HS = d.historyStatus || '';
  if(typeof d.revisions === 'number' || HS === 'error'){
    const note = HISTORY_NOTE[HS] || '';
    const count = typeof d.revisions === 'number'
      ? (d.revisions === 1 ? '1 revision' : d.revisions + ' revisions') : '';
    const text = count ? (note ? count + ' \u2014 ' + note : count) : (note || HS);
    fact(dl, 'Revisions', text, HS === 'error' || HS === 'inconsistent' ? 'flag' : null);
  }
  if(d.brainUpdated && d.brainUpdated !== d.updated)
    fact(dl, 'Backend touch', fmtStamp(d.brainUpdated) + ' \u2014 not a content write');
  if(HS === 'inconsistent' && d.lastRevision)
    fact(dl, 'Last revision', fmtStamp(d.lastRevision) +
         ' \u2014 newer than the backend timestamp', 'flag');
  fact(dl, 'Created', fmtStamp(d.created));
  fact(dl, 'Links', String(d.deg));
  fact(dl, 'Status', d.orphan ? 'Orphan — no resolved links' : 'Connected',
       d.orphan ? 'flag' : null);
  fact(dl, 'File', d.path || '—', null, true);

  $('iSummary').textContent = d.summary || 'No summary in this note.';
  const tags = $('iTags'); tags.textContent = '';
  const tagList = d.tags || [];
  $('iTagsSec').hidden = tagList.length === 0;
  tagList.forEach(t => { const s = document.createElement('span'); s.className = 'tag';
    s.textContent = t; tags.append(s); });

  const nb = n.closedNeighborhood();
  const others = nb.nodes().filter(x => x.id() !== n.id());
  const box = $('iLinks'); box.textContent = '';
  $('iLinksSec').hidden = others.length === 0;
  $('iLinksHead').textContent = 'Connected (' + others.length + ')';
  others.slice(0, 20).forEach(x => {
    const a = document.createElement('a'); a.tabIndex = 0; a.setAttribute('role','button');
    const dot = document.createElement('span'); dot.className = 'dot';
    dot.style.background = themeColor(x.data('theme'));
    const label = document.createElement('span'); label.textContent = x.data('title');
    a.append(dot, label);
    const go = () => { cy.animate({center:{eles:x}, zoom:Math.max(cy.zoom(), 1.05)}, {duration:DUR}); focus(x); };
    a.onclick = go;
    a.onkeydown = e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); go(); } };
    box.append(a);
  });
  if(others.length > 20){
    const more = document.createElement('div'); more.className = 'more';
    more.textContent = '+' + (others.length - 20) + ' more';
    box.append(more);
  }
  return nb;
}
function focus(n){
  cy.batch(() => {
    cy.elements().addClass('dim').removeClass('hl sel');
    const nb = n.closedNeighborhood();
    nb.removeClass('dim').addClass('hl');
    n.removeClass('hl').addClass('sel');
  });
  inspect(n);
  $('insp').scrollTop = 0;
}
function clearFocus(){
  cy.batch(() => cy.elements().removeClass('dim hl sel'));
  $('insp').hidden = true;
}
cy.on('tap', 'node', e => {
  cy.animate({center:{eles:e.target}, zoom:Math.max(cy.zoom(), 0.9)}, {duration:DUR});
  focus(e.target);
});
cy.on('tap', e => { if(e.target === cy) clearFocus(); });
cy.on('mouseover', 'node', e => {
  $('cy').style.cursor = 'pointer';
  if($('insp').hidden){ e.target.addClass('hl'); e.target.connectedEdges().addClass('hov'); }
});
cy.on('mouseout', 'node', e => {
  $('cy').style.cursor = '';
  if($('insp').hidden) e.target.removeClass('hl');
  e.target.connectedEdges().removeClass('hov');
});

// ---- chrome wiring ----
$('inspClose').onclick = clearFocus;
$('fit').onclick = () => cy.animate({fit:{padding:60}}, {duration:DUR ? 380 : 0});
$('toggleEdges').onclick = function(){
  edgesVisible = !edgesVisible;
  this.classList.toggle('on', edgesVisible);
  this.setAttribute('aria-pressed', String(edgesVisible));
  apply();
};
$('panelMin').onclick = function(){
  const min = $('panel').classList.toggle('min');
  this.setAttribute('aria-expanded', String(!min));
  this.title = min ? 'Expand controls' : 'Collapse controls';
};
document.addEventListener('keydown', e => {
  if((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')){ e.preventDefault(); $('search').focus(); return; }
  if(e.key === 'Escape'){
    if(document.activeElement === $('search') && $('search').value){ $('search').value = ''; apply(); }
    else clearFocus();
  }
});

// play growth — one long sweep, or a few discrete steps when motion is reduced
let playing = false, raf = null, timer = null;
$('play').onclick = function(){
  playing = !playing;
  this.setAttribute('aria-pressed', String(playing));
  $('playLab').textContent = playing ? 'Pause' : 'Play growth';
  $('icPlay').classList.toggle('h', playing);
  $('icPause').classList.toggle('h', !playing);
  const stop = () => { playing = false; $('play').setAttribute('aria-pressed','false');
    $('playLab').textContent = 'Play growth'; $('icPlay').classList.remove('h');
    $('icPause').classList.add('h'); };
  if(!playing){ cancelAnimationFrame(raf); clearTimeout(timer); return; }
  if(cutoff >= 1) setCutoff(0.02);
  if(REDUCED){
    const hop = () => { if(!playing) return;
      setCutoff(cutoff + 0.08);
      if(cutoff >= 1){ stop(); return; }
      timer = setTimeout(hop, 260); };
    hop();
  } else {
    const step = () => { if(!playing) return;
      setCutoff(cutoff + 0.010);
      if(cutoff >= 1){ stop(); return; }
      raf = requestAnimationFrame(step); };
    step();
  }
};

cy.ready(() => {
  cy.fit(undefined, 70);
  buildTimeline();
  apply();
  const h = $('hint');
  h.classList.add('show');
  setTimeout(() => h.classList.remove('show'), 4600);
});
</script>
</body></html>
"""

def main():
    ap = argparse.ArgumentParser(
        description="Build one self-contained interactive HTML knowledge map from Markdown notes.")
    ap.add_argument("vault")
    ap.add_argument("out")
    ap.add_argument("--title", default="Knowledge Map")
    ap.add_argument("--source-label", default=None,
                    help="storage label shown in the header (e.g. 'gbrain: demo'); defaults to the path")
    ap.add_argument("--as-of", default=None,
                    help="reference ISO date/timestamp for deterministic freshness bands")
    ap.add_argument("--keep-positions", default=None, metavar="MAP_HTML",
                    help="reuse node positions from a previously generated map, so a rebuild "
                         "keeps every surviving note where you already know it; new notes "
                         "land beside their neighbours")
    ap.add_argument("--cytoscape-js", default=None, metavar="PATH",
                    help="inline a local cytoscape.min.js so the map needs no network "
                         "(nothing is ever downloaded for you)")
    ap.add_argument("--strict-offline", action="store_true",
                    help="refuse to write anything unless --cytoscape-js supplies a valid local runtime")
    grp = ap.add_argument_group(
        "GBrain (optional, read-only)",
        "Off by default. When on, freshness comes from the brain's own updated_at "
        "instead of the export's frontmatter. Nothing is ever written to GBrain.")
    grp.add_argument("--gbrain", action="store_true",
                     help="read semantic updated_at from GBrain (one `gbrain serve` "
                          "MCP session, batched list_pages)")
    grp.add_argument("--gbrain-history", action="store_true",
                     help="also read per-page version history (get_versions) to tell real "
                          "revisions from re-embeds and renames; implies --gbrain")
    grp.add_argument("--gbrain-history-limit", type=int, default=DEFAULT_HISTORY_LIMIT,
                     metavar="N",
                     help="cap history reads at the N most recently updated matched pages "
                          f"(default {DEFAULT_HISTORY_LIMIT}; 0 = every matched page)")
    grp.add_argument("--gbrain-cmd", default=None, metavar="BIN",
                     help="gbrain executable to run (default: gbrain on PATH)")
    grp.add_argument("--gbrain-source", default=None, metavar="ID",
                     help="scope reads to one GBrain source id ('__all__' spans every source)")
    grp.add_argument("--gbrain-slug-prefix", default="", metavar="P",
                     help="slug prefix to prepend to path-derived slugs when the vault is a "
                          "subdirectory of a `gbrain export` tree")
    grp.add_argument("--gbrain-timeout", type=float, default=None, metavar="S",
                     help="seconds to wait for any single GBrain read (default 20)")
    grp.add_argument("--gbrain-required", action="store_true",
                     help="fail the build instead of falling back to Markdown when GBrain "
                          "cannot be read, or could only be read in part (a truncated page "
                          "index, dropped rows, ambiguous slugs, history that stopped early)")
    a = ap.parse_args()

    use_gbrain = a.gbrain or a.gbrain_history
    if not use_gbrain:
        tuning = [name for name, given in (
            ("--gbrain-cmd", a.gbrain_cmd is not None),
            ("--gbrain-source", a.gbrain_source is not None),
            ("--gbrain-slug-prefix", bool(a.gbrain_slug_prefix)),
            ("--gbrain-timeout", a.gbrain_timeout is not None),
            ("--gbrain-history-limit", a.gbrain_history_limit != DEFAULT_HISTORY_LIMIT),
            ("--gbrain-required", a.gbrain_required)) if given]
        if tuning:
            ap.error(f"{', '.join(tuning)} needs --gbrain (or --gbrain-history); "
                     "without it the build is Markdown-only and the option would do nothing.")
    if a.gbrain_history_limit < 0:
        ap.error("--gbrain-history-limit: must be 0 (no cap) or a positive count")
    if a.gbrain_timeout is not None and a.gbrain_timeout <= 0:
        ap.error("--gbrain-timeout: must be greater than 0 seconds")
    gbrain = {"command": a.gbrain_cmd, "source": a.gbrain_source,
              "history": a.gbrain_history, "historyLimit": a.gbrain_history_limit,
              "slugPrefix": a.gbrain_slug_prefix, "timeout": a.gbrain_timeout,
              "required": a.gbrain_required} if use_gbrain else None

    # validate everything that can fail *before* touching the output path
    if a.as_of:
        try:
            parse_as_of(a.as_of)
        except ValueError as exc:
            ap.error(f"--as-of: {exc}")
    kept_positions = None
    if a.keep_positions:
        try:
            kept_positions = read_positions(a.keep_positions)
        except ValueError as exc:
            ap.error(f"--keep-positions: {exc}. Nothing was written.")
    runtime = None
    if a.cytoscape_js:
        try:
            runtime = read_local_runtime(a.cytoscape_js)
        except RuntimeUnavailable as exc:
            ap.error(f"--cytoscape-js: {exc}. Nothing was written.")
    elif a.strict_offline:
        ap.error("--strict-offline needs a local Cytoscape runtime: pass "
                 "--cytoscape-js PATH (e.g. a cytoscape.min.js you already have). "
                 "Nothing is downloaded automatically and nothing was written.")

    try:
        payload = build(a.vault, a.title, a.source_label, a.as_of,
                        keep_positions=kept_positions, gbrain=gbrain)
    except RuntimeError as exc:      # --gbrain-required and the read was not clean
        ap.error(f"{exc}. Nothing was written.")
    report = payload["gbrain"]
    if report["status"] not in ("ok", "off"):
        print(f"warning: {report['detail']}", file=sys.stderr)
    out_html = render(payload, runtime=runtime, runtime_path=a.cytoscape_js)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(out_html)
    s = payload["stats"]
    print(f"map: {s['nodes']} nodes, {s['edges']} edges -> {a.out}")
    print(f"themes: {s['themes']}")
    print(f"types : {s['types']}")
    print(f"fresh : {s['freshness']} (as of {s['asOf'][:10]})")
    print(f"audit : {s['audit']['flagged']} flagged "
          f"({s['audit']['oxidized']} oxidized, {s['audit']['orphans']} orphans)")
    print(f"runtime: {payload['runtime']['label']} — {payload['runtime']['source']}")
    if report["status"] in ("ok", "partial"):
        history = report["history"]
        line = (f"gbrain : {report['matched']}/{s['nodes']} notes matched "
                f"({report['indexed']} pages indexed)")
        if history["requested"]:
            line += (f", {history['events']} revisions across {history['revised']} pages"
                     f" (history read for {history['read']}/{history['matched']})")
        if report["status"] == "partial":
            line += " — PARTIAL: " + "; ".join(report["gaps"])
        print(line)
    elif report["status"] != "off":
        print(f"gbrain : {report['status']} — Markdown timestamps kept")

if __name__ == "__main__":
    main()

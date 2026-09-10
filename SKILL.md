---
name: brain-map
description: Render an interactive HTML knowledge map from a folder of Markdown notes (an Obsidian vault or a `gbrain export` directory) — a force-directed graph coloured by theme, with a brushable timeline that shows the knowledge base growing over time and a click-to-inspect detail panel. Use when the user says "visualize my notes/vault/brain", "show me the knowledge graph", "make a brain map", "graph of my gbrain/obsidian", "/brain-map", or wants an at-a-glance picture of what's in their notes for analysis or a presentation/demo.
license: MIT
---

# brain-map — interactive knowledge map from your notes

One command turns a folder of Markdown into a single self-contained `.html`: a
dark graphite constellation (themes as colour, note types as shape, freshness as
luminance, hubs labelled), a timeline you can scrub to watch the base grow,
search plus theme/type/freshness filters, an Audit lens for stale and orphaned
notes, and an inspector for any node. Reads plain Markdown — pairs with the
`save-note` skill, but works on any Obsidian vault.

## Try the bundled demo first (no setup)

A prebuilt map ships in this skill — **no notes to generate, no embeddings, no
gbrain, no Python**. Just open it:

```bash
open demo/brain-map.html        # macOS   (xdg-open on Linux, start on Windows)
```

992 fictional notes across three themes (work · study · life). Scrub the
timeline, hit Play, click nodes, toggle filters. That's the whole experience.

## Build from the user's own notes

The builder reads a directory of Markdown with YAML frontmatter (`tags`,
`created`) and `[[wikilinks]]`. Two common sources:

- **Obsidian vault** — point straight at the vault folder. This always works.
- **gbrain** — `GBRAIN_HOME=<brain> gbrain export --dir <out>`, then point at `<out>`.
  Add `--gbrain-history` to the build to read the brain's real `updated_at` and revision
  history back out (read-only, optional — see below).

```bash
python3 scripts/build_map.py <notes_dir> out.html --title "My Second Brain"
```

Open `out.html`. It's one file.

Useful flags:

- `--as-of 2026-06-15` — band freshness against a fixed date, so reports are reproducible.
- `--cytoscape-js ./cytoscape.min.js` — inline a Cytoscape bundle the user already has,
  making the file work with no network at all.
- `--strict-offline` — refuse to write anything unless a plausible official Cytoscape 3
  browser bundle is given. Validation is non-executing and best-effort.
- `--gbrain` / `--gbrain-history` — read real timestamps from the brain (see below).

By default the file loads Cytoscape from a pinned CDN URL and labels itself
**Network runtime** in its header; with an inlined bundle it says **Local runtime**.
The builder never downloads anything, and never writes to the vault.

### Real GBrain timestamps (optional, read-only)

An export does not carry GBrain's `updated_at` or any revision history, so a
freshly exported or re-cloned vault reads as uniformly fresh by file time and
uniformly ancient by `created`. When the user's notes came from a brain and
`gbrain` is on PATH, offer:

```bash
python3 scripts/build_map.py <export_dir> out.html --gbrain            # backend updated_at
python3 scripts/build_map.py <export_dir> out.html --gbrain-history    # + version history
```

- `--gbrain` batches `list_pages` through one `gbrain serve` MCP session;
  `--gbrain-history` adds `get_versions` per matched page, which is what
  separates a real content write from a re-embed, a rename or a revert.
- Tuning: `--gbrain-history-limit N` (default 500, `0` = every eligible page),
  `--gbrain-source ID`, `--gbrain-slug-prefix P`, `--gbrain-cmd BIN`,
  `--gbrain-timeout S`, `--gbrain-required`.
- **The default budget caps history at 500 pages.** On a larger brain that is a
  `partial` build by construction — the unread pages keep an unverified backend
  timestamp and `--gbrain-required` fails. Say so, and offer
  `--gbrain-history-limit 0` for full coverage (one round-trip per page).
- **Read-only and fail-open.** Only `list_pages` and `get_versions` are ever
  called; past revision bodies are projected to timestamps as soon as they are
  decoded and never rendered, and brain slugs/source ids never enter the file.
  If gbrain is missing, broken, slow or unreadable the build still succeeds
  from Markdown and warns — so it is safe to try. Markdown-only is the default.
- **Partial reads say so.** A truncated page index, a slug that lives in two
  sources, a page whose history failed or came back partly unreadable, or a
  budget that stopped short all make the build `partial`: the CLI warns and
  lists the gaps with counts, matched notes still keep the brain's timestamps,
  the rest keep Markdown, and `--gbrain-required` refuses to write at all.
  History is only ever read for slugs proven to belong to exactly one page — a
  concrete `--gbrain-source` is audited against `source_id='__all__'` first,
  because `get_versions` takes a bare slug and would otherwise answer with two
  sources' revisions merged.
- Semantics, replay/diff rules and backend limits: `docs/gbrain-history.md`.

### Dependencies are optional

- **None required.** With only stdlib Python, the builder ships no positions and
  the browser computes the force layout (Cytoscape `cose`) — works anywhere,
  rougher on big/disconnected graphs.
- **Prettier + instant** for 1000+ node maps: `pip install networkx numpy scipy`
  (see `requirements.txt`). Then layout is pre-computed and the file opens
  immediately. The builder auto-detects networkx and picks the better path.

## How it reads the notes

- **Theme** = top-level folder — _your own_ folders become the themes, each given a
  stable colour from a palette (no fixed taxonomy is imposed). `Work/`, `Study/`,
  `Life/` keep their legacy colours when present; root-level notes → "Other". Drives
  node + edge colour.
- **Type** = subfolder/tags → node shape: `People/`→person, `Meetings/`→meeting,
  `Journal/`→journal, `Lectures/`→lecture, `Project/`→project, `Links/`→link,
  `todo`/`index` tags → those; else `note`.
- **Edges** = resolved `[[wikilinks]]` (matched by note title). Node size scales
  with degree; nodes with ≥7 links get a permanent label (the hubs).
- **Timeline** = `created` timestamps bucketed by month, stacked by theme. When a
  note has no `created` in its frontmatter, the file's own birth/modified time is
  used instead, so vanilla Obsidian vaults still get a timeline.
- **Freshness** = the GBrain stamp when `--gbrain` is on, else the first of
  `last_updated`, `updated`, `modified`, then `created`, then
  the file timestamp. Age in days lands each note in one of five bands — **fresh** ≤7d,
  **recent** ≤30d, **settled** ≤90d, **dormant** ≤365d, **oxidized** >365d (`unknown`
  only when no date exists at all). Bands drive filters and labels; a separate
  continuous score in [0,1] drives node luminance, saturation and halo.

Richer cross-linking (people cards, meeting attendees, index/MOC pages) ⇒ a more
legible map. save-note's people-registry + `[[links]]` are what make it cluster.

## What the user can do in the HTML

- **Scrub / Play** the timeline → graph filters to notes up to that month; Play
  animates the base growing from empty to today.
- **Filter** by theme, type and freshness band in collapsible groups (live counts);
  **search** rings matches (⌘/Ctrl K focuses, Esc clears).
- **Audit** isolates oxidized and orphan (unlinked) notes and reports both counts.
- **Click** a node → dim the rest, light up its neighbourhood, render the selection at
  full strength, and open the inspector: freshness band, age in days, updated timestamp,
  which field it came from, created date, link count, orphan status, path, summary, tags,
  and a clickable list of connected notes. With `--gbrain-history` it also states the
  revision count, whether history confirmed the timestamp, and any later non-content touch.
- Responsive: collapses to a phone-friendly layout on narrow screens; honours
  `prefers-reduced-motion`.

## Regenerate the demo corpus (optional)

`scripts/generate_demo_notes.py` writes the 992-note fictional vault (all
invented — no real data) in save-note format:

```bash
python3 scripts/generate_demo_notes.py /tmp/demo-vault
python3 scripts/build_map.py /tmp/demo-vault demo.html --title "Demo"
```

## Report

Give the user the output path, headline counts (nodes/edges/date span), and the
theme + type breakdown. When the adapter ran, also relay the `gbrain :` line —
how many notes matched the brain and how many revisions were found — and say so
plainly if it fell back to Markdown or came back `PARTIAL` (that line names what
was missed). Offer `open out.html`, or screenshot it
headless:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --window-size=1600,1000 \
  --virtual-time-budget=9000 --screenshot=preview.png "file://$PWD/out.html"
```

## Notes

- Read-only: never writes to the vault or the brain, with or without `--gbrain`.
- Title collisions get a short hash suffix so no note is dropped.
- Re-run after adding notes to refresh; it's idempotent.

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

By default the file loads Cytoscape from a pinned CDN URL and labels itself
**Network runtime** in its header; with an inlined bundle it says **Local runtime**.
The builder never downloads anything, and never writes to the vault.

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
- **Freshness** = first of `last_updated`, `updated`, `modified`, then `created`, then
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
  and a clickable list of connected notes.
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
theme + type breakdown. Offer `open out.html`, or screenshot it headless:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --window-size=1600,1000 \
  --virtual-time-budget=9000 --screenshot=preview.png "file://$PWD/out.html"
```

## Notes

- Read-only: never writes to the vault or the brain.
- Title collisions get a short hash suffix so no note is dropped.
- Re-run after adding notes to refresh; it's idempotent.

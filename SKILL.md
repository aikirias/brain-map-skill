---
name: brain-map
description: Render an interactive HTML knowledge map from a folder of Markdown notes (an Obsidian vault or a `gbrain export` directory) — a force-directed graph coloured by theme, with a brushable timeline that shows the knowledge base growing over time and a click-to-inspect detail panel. Use when the user says "visualize my notes/vault/brain", "show me the knowledge graph", "make a brain map", "graph of my gbrain/obsidian", "/brain-map", or wants an at-a-glance picture of what's in their notes for analysis or a presentation/demo.
license: MIT
---

# brain-map — interactive knowledge map from your notes

One command turns a folder of Markdown into a single self-contained `.html`: a
force-directed graph (themes as colour, note types as shape, hubs labelled), a
timeline you can scrub to watch the base grow, search + theme/type filters, and
a detail panel for any node. Reads plain Markdown — pairs with the `save-note`
skill, but works on any Obsidian vault.

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

Open `out.html`. It's one file, Cytoscape loaded from CDN.

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

Richer cross-linking (people cards, meeting attendees, index/MOC pages) ⇒ a more
legible map. save-note's people-registry + `[[links]]` are what make it cluster.

## What the user can do in the HTML

- **Scrub / Play** the timeline → graph filters to notes up to that month; Play
  animates the base growing from empty to today.
- **Filter** by theme and type (live counts); **search** highlights matches.
- **Click** a node → dim the rest, light up its neighbourhood, open a panel with
  summary, tags, date, and a clickable list of connected notes.
- Responsive: collapses to a phone-friendly layout on narrow screens.

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

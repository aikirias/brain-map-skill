# brain-map — constellation edition

> Turn a folder of Markdown notes (an Obsidian vault or a `gbrain export`) into **one
> self-contained, interactive HTML knowledge map** — a dark graphite constellation coloured
> by the best available freshness signal, with a timeline you can scrub to watch the base grow
> and a click-to-inspect panel that exposes exactly where each date came from.

**This is the maintained fork** — [`aikirias/brain-map-skill`](https://github.com/aikirias/brain-map-skill).
It carries the constellation redesign, the five-band freshness model, the Audit lens and the
honest offline runtime options described below. Originally forked from
[`vladignatyev/brain-map-skill`](https://github.com/vladignatyev/brain-map-skill) by Vladimir
Ignatev, whose builder, timeline and demo corpus are the foundation this is built on — thank
you. Still MIT, still one file out, still no server.

**Works with:** Claude Code · OpenAI Codex · Cursor · Gemini CLI · OpenClaw · or just run the script.

![preview](demo/preview.png)

## See it in 5 seconds (no setup)

A prebuilt demo ships in this repo — **no notes to generate, no embeddings, no gbrain,
no Python.** Open the file:

```bash
open demo/brain-map.html        # macOS
xdg-open demo/brain-map.html    # Linux
start demo/brain-map.html       # Windows
```

992 fictional notes across three themes (**work · study · life**). Scrub the timeline,
press **Play growth**, click nodes, toggle filters, flip on **Audit**.

## Build from your own notes

```bash
python3 scripts/build_map.py <notes_dir> out.html --title "My Second Brain"

# Reproducible freshness: band every note against a fixed reporting date
python3 scripts/build_map.py <notes_dir> out.html --as-of 2026-06-15

# Fully offline file: inline a Cytoscape bundle you already have on disk
python3 scripts/build_map.py <notes_dir> out.html --cytoscape-js ./cytoscape.min.js

# GBrain export? Read the brain's real updated_at and revision history (read-only)
python3 scripts/build_map.py <export_dir> out.html --gbrain-history

open out.html
```

`<notes_dir>` = your Obsidian vault, or a `gbrain export --dir <out>` directory. The map
reads plain Markdown: YAML frontmatter (`tags`, `created`, `updated`) + `[[wikilinks]]`.

### Dependencies are optional

| Setup | Result |
|-------|--------|
| **Nothing** (stdlib Python only) | Builds anywhere; the browser computes the layout (Cytoscape `cose`). |
| `pip install -r requirements.txt` (networkx, numpy, scipy) | Layout pre-computed → 1000-node maps open instantly and look cleaner. |

The builder auto-detects networkx and picks the better path. No gbrain, no embeddings,
no server required either way.

## What it reads

- **Theme** = top-level folder — _your_ folders, whatever they are → node & edge colour.
  Colours are assigned automatically from a palette (no fixed taxonomy); `Work/`,
  `Study/`, `Life/` keep their legacy colours if present, root-level notes are `Other`.
- **Type** = subfolder / tags → node shape (person, meeting, journal, lecture, project,
  link, todo, index, note).
- **Edges** = resolved `[[wikilinks]]`, including ordinary note titles and GBrain-style
  relative slugs such as `[[projects/hermes-gbrain]]`. Node size scales with link count;
  hubs get labels.
- **Timeline** = `created` timestamps, bucketed by month, stacked by theme. No `created`
  in the frontmatter? It falls back to the file's own timestamp, so plain vaults still
  get a timeline.
- **Freshness** = the first of `last_updated`, `updated`, `modified` present, then `created`,
  then the file's own timestamp. The inspector always names which one it used. Filesystem
  time is an approximate transport signal and may be reset by a copy, checkout, export or
  sync; it must not be interpreted as semantic GBrain history. With `--gbrain` the brain's
  own `updated_at` outranks all of them — see below.

### Freshness: five bands and a continuous score

Age is measured in whole days from the note's update date to the reference date (`--as-of`,
or now). Every note lands in exactly one band:

| Band | Age | Meaning |
|------|-----|---------|
| **fresh** | ≤ 7 days | touched this week |
| **recent** | 8–30 days | touched this month |
| **settled** | 31–90 days | stable, not stale |
| **dormant** | 91–365 days | drifting out of mind |
| **oxidized** | > 365 days | over a year untouched |
| _unknown_ | — | only when no date can be found at all |

Bands are what you **filter and label** with. Rendering uses a separate **continuous
freshness score in [0,1]** (1 = brand new, decaying with a 120-day half-life and
approaching 0 with age), which drives node luminance, saturation and halo — so two
oxidized notes three years apart still look different, and the constellation reads as a
gradient rather than five flat buckets. The inspector shows the exact score.

**Audit** is a lens, not a band: it isolates oxidized **and** orphan (unlinked) notes, and
states the counts for each while it's active. Theme, type and timeline filters keep applying.

## Real GBrain timestamps and history (optional)

A Markdown export carries `created` and whatever update field the page happened to have.
It does **not** carry GBrain's own `updated_at`, and no revision history at all — so a
vault you exported or re-cloned this morning reads as uniformly fresh by mtime, or
uniformly ancient by `created`. `--gbrain` goes and asks the brain instead.

```bash
python3 scripts/build_map.py <export_dir> out.html --gbrain            # backend updated_at
python3 scripts/build_map.py <export_dir> out.html --gbrain-history    # + version history
```

| Flag | What it does |
|------|--------------|
| `--gbrain` | freshness comes from the brain's `updated_at`, read in batches through one `gbrain serve` MCP session |
| `--gbrain-history` | also reads `get_versions` per matched page, so a re-embed or a rename is not mistaken for a content update; implies `--gbrain` |
| `--gbrain-history-limit N` | cap history at the N most recently updated eligible pages (default 500, `0` = all — see below) |
| `--gbrain-source ID` | scope reads to one source (`__all__` spans every source) |
| `--gbrain-slug-prefix P` | match when the vault is a subdirectory of an export tree |
| `--gbrain-cmd BIN` | use a specific `gbrain` executable |
| `--gbrain-timeout S` | per-read timeout (default 20s) |
| `--gbrain-required` | fail the build — writing nothing — if the brain cannot be read, or could only be read in part |

- **Read-only, always.** Only `list_pages` and `get_versions` are ever called — both
  `scope: 'read'`. Nothing is written to the brain or the vault. MCP cannot stream a reply,
  so a page's history does arrive as one decoded message — it is projected to timestamps
  immediately after decoding, capped at 16 MiB per reply, and nothing but counts and dates
  survives the call, so no private prose from any old version reaches the HTML. Brain slugs
  and source ids are matching inputs and are not written into the file either.
- **Fail-open by default.** No `gbrain` on PATH, no brain, a refused handshake, a garbled
  answer or a hung server all leave a normal Markdown-built map plus a `warning:` line.
  Markdown-only remains the default mode and the fallback.
- **Partial is said out loud.** If the page index stopped early, a slug turned out to live
  in two sources, a page's history failed or came back partly unreadable, or the history
  budget stopped short, the build is labelled `partial` in the CLI and in the map's header,
  with the reasons and counts listed. The notes that did match still keep the brain's
  timestamps — enrichment is per note, never all-or-nothing — and everything else keeps its
  Markdown ones. `--gbrain-required` refuses such a build outright and writes nothing.
- **History is only read for slugs that provably belong to one page.** `get_versions` takes
  a bare slug and answers with the union of every source that holds it, so a slug two
  sources share is never asked about — including under a concrete `--gbrain-source`, which
  is checked against one extra read-only `list_pages source_id='__all__'` collision audit
  before any history is read. Such a page keeps the backend `updated_at` and is labelled
  unverified, never given a revision count.
- **`--gbrain-history-limit` is a real limit.** The default reads the 500 most recently
  updated eligible pages; a brain with more than that comes back `partial` with the
  requested / read / skipped counts, the unread pages keep an unverified backend timestamp,
  and `--gbrain-required` fails. Pass `--gbrain-history-limit 0` for full coverage, at one
  round-trip per page.
- **Honest labels.** The inspector says which of `gbrain_history` (confirmed by a version
  snapshot), `gbrain_revision` (the last backend touch was *not* a content write) or
  `gbrain_updated` (unconfirmed — a single write, history not read, history unreadable, or
  history that contradicts `updated_at`) produced the date, and the timeline grows a second
  series for true revisions on top of creation growth.

The exact `updated_at` / `page_versions` semantics, the replay and diff rules, and the
backend limitations are written up in [docs/gbrain-history.md](docs/gbrain-history.md).

### Privacy and offline behavior

The builder only reads local Markdown. It never uploads note contents and **never downloads
anything** — not even the graph library. Generated HTML defaults to the vault's basename
instead of embedding its absolute filesystem path; use `--source-label` when you want a
specific display name.

| Mode | What the HTML does | Header says |
|------|--------------------|-------------|
| default | loads Cytoscape from a pinned CDN URL (`cdnjs`, version-locked) | **Network runtime** |
| `--cytoscape-js PATH` | inlines that local bundle; the file works with no network at all | **Local runtime** |
| `--strict-offline` | succeeds **only** with a plausible official Cytoscape 3 browser bundle supplied via `--cytoscape-js`; otherwise refuses and writes no file | **Local runtime** |

`--strict-offline` fails before the output is created, so a refusal never leaves a
half-honest file behind. Validation is deliberately non-executing and therefore best-effort:
the bundle must have an official-distribution size and Cytoscape Consortium/UMD markers.
The builder rejects stubs, prose, HTML error pages, and unrelated JavaScript, but it does not
execute untrusted input to prove every API. Use an official Cytoscape 3 browser bundle from
your own `node_modules`, a release download, or an existing offline mirror.

The richer your cross-linking (people cards, meeting attendees, index pages), the more
legible the map. Designed to pair with the [save-note](https://github.com/vladignatyev/save-note-skill)
skill, which writes exactly this shape.

## In the HTML

- **Explore surface** — a compact top dock (title, source, live counts, as-of date, runtime
  label), a collapsible controls panel on the left, the inspector on the right, timeline
  along the bottom. Dark graphite; colour means data, never decoration.
- **Scrub / Play** the timeline → the graph reveals notes up to that month; Play animates
  the whole base growing from empty to today.
- **Filter** by theme, type and freshness band, each group collapsible with live
  active/total counts; **search** rings matches (**⌘/Ctrl K** focuses it, **Esc** clears).
- **Audit** isolates oxidized and orphan notes, highlights them, and reports
  `N flagged — X oxidized, Y orphan`.
- **Click** a node → the rest dims, its neighbourhood lights up, and the selected node
  renders at full strength (age never costs you legibility once you've selected it).
- **Inspector** states the freshness band, age in days, the updated timestamp, *which field*
  that came from, the created date, link count, orphan status, file path, summary, tags and
  clickable neighbours.
- **Hierarchy**: hubs (≥7 links) stay labelled, mid-degree notes label up as you zoom in,
  edges stay quiet until you select something.
- Responsive down to a phone; honours `prefers-reduced-motion`.

## Install as an agent skill

**Claude Code**
```bash
git clone https://github.com/aikirias/brain-map-skill ~/.claude/skills/brain-map
```

**OpenAI Codex**
```bash
git clone https://github.com/aikirias/brain-map-skill ~/.agents/skills/brain-map
```

**Cursor / others** — paste `SKILL.md` into your agent's instructions; it's self-contained.

## Regenerate the demo corpus

```bash
python3 scripts/generate_demo_notes.py /tmp/demo-vault   # 992 invented notes
python3 scripts/build_map.py /tmp/demo-vault demo.html
```

All demo people, orgs and events are fictional — no real data.

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest` only — covers the freshness bands and score, GBrain slug resolution,
payload shape, runtime inlining and refusal, template safety contracts, and CLI error handling.
`tests/test_gbrain_history.py` adds the adapter: timestamp precedence, missing and malformed
history, backend failure and fail-open, deterministic budgets, and the read-only guarantee —
all against `tests/fixtures/fake_gbrain.py`, a scripted MCP server over invented pages, so
the suite never needs a real brain.

## GBrain coverage and roadmap

A **read-only review of the brain**, not a second memory system. Markdown pages,
frontmatter and resolved wikilinks (including GBrain relative slugs) are always read from
disk; with `--gbrain` the builder additionally reads `list_pages` and `get_versions` through
one `gbrain serve` MCP session. It never writes to GBrain, and it keeps no database of its
own.

Covered now:

- backend `updated_at` as the freshness signal, batched and paged, with the provenance
  named in the inspector;
- page version history: revision counts and dates per page, used to tell a real content
  write apart from a re-embed, a rename or a revert;
- a timeline that separates creation growth from true revisions.

Still not represented:

- hot facts, supersessions, expirations or fact provenance;
- typed backend edges that are not rendered as Markdown wikilinks;
- embedding coverage or retrieval health;
- **side-by-side version diffs.** The semantics are documented and the data is one
  `get_versions` call away, but rendering old bodies would put private prose from every
  past revision into a shareable HTML file, so the adapter deliberately keeps only
  timestamps. See [docs/gbrain-history.md](docs/gbrain-history.md) §5 for the exact diff
  and replay rules if you want to build that on top.

Without `--gbrain`, freshness uses the best signal present in each file (`last_updated`,
`updated`, `modified`, then `created`). Filesystem `mtime` is only an explicitly labelled
approximation and may be reset by copying or syncing a vault. Deeper structural analysis can
be exported to Gephi rather than turning this static viewer into another operational service.

## Layout

```
brain-map-skill/
├── SKILL.md                      # agent skill spec
├── docs/
│   └── gbrain-history.md         # updated_at / version semantics, replay, limits
├── scripts/
│   ├── build_map.py              # the builder (Markdown dir → interactive HTML)
│   ├── gbrain_history.py         # optional read-only GBrain adapter (MCP stdio)
│   └── generate_demo_notes.py    # writes the fictional demo vault
├── tests/
│   ├── test_build_map.py         # stdlib unittest suite
│   ├── test_gbrain_history.py    # adapter: precedence, failure, determinism
│   └── fixtures/fake_gbrain.py   # scripted MCP server over invented pages
├── demo/
│   ├── brain-map.html            # PREBUILT — open it, zero setup
│   ├── vault/                    # 992 source Markdown notes
│   └── preview.png
├── requirements.txt              # optional: networkx, numpy, scipy
└── LICENSE
```

## License

[MIT](LICENSE) — © Vladimir Ignatev (upstream) and the brain-map constellation fork
contributors.

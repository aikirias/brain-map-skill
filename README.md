# brain-map

> Turn a folder of Markdown notes (an Obsidian vault or a `gbrain export`) into **one
> self-contained, interactive HTML knowledge map** — a force-directed graph coloured by
> theme, a timeline you can scrub to watch the base grow, and a click-to-inspect panel.

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
press **▶ Play growth**, click nodes, toggle filters.

## Build from your own notes

```bash
python3 scripts/build_map.py <notes_dir> out.html --title "My Second Brain"
open out.html
```

`<notes_dir>` = your Obsidian vault, or a `gbrain export --dir <out>` directory. The map
reads plain Markdown: YAML frontmatter (`tags`, `created`) + `[[wikilinks]]`.

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
- **Edges** = resolved `[[wikilinks]]`. Node size scales with link count; hubs get labels.
- **Timeline** = `created` timestamps, bucketed by month, stacked by theme. No `created`
  in the frontmatter? It falls back to the file's own timestamp, so plain vaults still
  get a timeline.

The richer your cross-linking (people cards, meeting attendees, index pages), the more
legible the map. Designed to pair with the [save-note](https://github.com/vladignatyev/save-note-skill)
skill, which writes exactly this shape.

## In the HTML

- **Scrub / Play** the timeline → the graph reveals notes up to that month; Play animates
  the whole base growing from empty to today.
- **Filter** by theme and type (live counts); **search** highlights matches.
- **Click** a node → dim the rest, light its neighbourhood, open a detail panel (summary,
  tags, date, connected notes).
- Responsive down to a phone.

## Install as an agent skill

**Claude Code**
```bash
git clone https://github.com/vladignatyev/brain-map-skill ~/.claude/skills/brain-map
```

**OpenAI Codex**
```bash
git clone https://github.com/vladignatyev/brain-map-skill ~/.agents/skills/brain-map
```

**Cursor / others** — paste `SKILL.md` into your agent's instructions; it's self-contained.

## Regenerate the demo corpus

```bash
python3 scripts/generate_demo_notes.py /tmp/demo-vault   # 992 invented notes
python3 scripts/build_map.py /tmp/demo-vault demo.html
```

All demo people, orgs and events are fictional — no real data.

## Layout

```
brain-map-skill/
├── SKILL.md                      # agent skill spec
├── scripts/
│   ├── build_map.py              # the builder (Markdown dir → interactive HTML)
│   └── generate_demo_notes.py    # writes the fictional demo vault
├── demo/
│   ├── brain-map.html            # PREBUILT — open it, zero setup
│   ├── vault/                    # 992 source Markdown notes
│   └── preview.png
├── requirements.txt              # optional: networkx, numpy, scipy
└── LICENSE
```

## License

[MIT](LICENSE)

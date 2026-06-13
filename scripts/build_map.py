#!/usr/bin/env python3
"""
brain-map — turn a folder of save-note Markdown (an Obsidian vault or a
`gbrain export` dir) into ONE self-contained, interactive HTML knowledge map.

Graph  : Cytoscape.js, force-directed positions pre-computed here (networkx
         spring layout) so a 1000-node map opens instantly.
Time   : a brushable timeline strip (built from each note's `created`) that
         filters the graph — scrub or hit Play to watch the brain grow.
Facts  : click any node for its summary, tags, date, neighbours.

Usage:
    python build_map.py <vault_dir> <out.html> [--title "My Brain"]

No network at runtime: Cytoscape is inlined from a CDN fetch at build time if
available, else loaded from CDN by the browser (the file still works online).
"""
import os, re, sys, json, html, datetime, argparse, hashlib

try:
    import networkx as nx
except ImportError:
    nx = None

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
    "person":  "ellipse",
    "meeting": "round-diamond",
    "journal": "round-rectangle",
    "todo":    "round-tag",
    "index":   "star",
    "project": "hexagon",
    "lecture": "round-rectangle",
    "link":    "round-pentagon",
    "note":    "ellipse",
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

def summarize(body):
    """First non-empty prose under ## Summary, else first prose paragraph."""
    m = re.search(r"##\s*Summary\s*\n(.+?)(?:\n##\s|\Z)", body, re.S)
    chunk = m.group(1) if m else body
    chunk = re.sub(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]", r"\1", chunk)  # strip wikilink syntax
    lines = [l.strip() for l in chunk.splitlines() if l.strip() and not l.strip().startswith("#")]
    text = " ".join(lines)
    return (text[:280] + "…") if len(text) > 280 else text

def load(vault):
    nodes = {}         # title -> node dict
    raw_links = []     # (src_title, target_title_text)
    by_title = {}
    for root, _, files in os.walk(vault):
        for fn in files:
            if not fn.endswith(".md"): continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, vault)
            text = open(path, encoding="utf-8").read()
            fmm = FM_RE.match(text)
            fm = parse_fm(fmm.group(1)) if fmm else {}
            body = text[fmm.end():] if fmm else text
            h1 = H1_RE.search(body)
            title = (h1.group(1).strip() if h1 else os.path.splitext(fn)[0]).strip()
            parts = rel.split(os.sep)
            theme = parts[0] if len(parts) > 1 else "Other"  # top folder = theme; root notes = Other
            tags = fm.get("tags", [])
            if isinstance(tags, str): tags = [tags]
            stype = subtype_of(rel, [t.lower() for t in tags], title)
            created = fm.get("created", "")
            if not created:  # vanilla vaults often lack a date — fall back to the file's own timestamp
                try:
                    st = os.stat(path)
                    ts = getattr(st, "st_birthtime", 0) or st.st_mtime
                    created = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")
                except OSError:
                    created = ""
            if title in nodes:  # collision: keep first, skip dup title
                title = f"{title} ⟨{hashlib.md5(rel.encode()).hexdigest()[:4]}⟩"
            nodes[title] = {
                "id": title, "title": title, "theme": theme, "type": stype,
                "tags": tags, "created": created, "summary": summarize(body),
                "path": rel, "source": fm.get("source", ""),
            }
            by_title[title] = title
            for tgt in WIKILINK_RE.findall(body):
                raw_links.append((title, tgt.strip()))
    # resolve links by exact title
    edges = []
    seen = set()
    for s, t in raw_links:
        if t in nodes and s in nodes and s != t:
            key = (s, t)
            if key in seen: continue
            seen.add(key); edges.append({"source": s, "target": t})
    return nodes, edges

# ── layout ───────────────────────────────────────────────────────────────────
def layout(nodes, edges):
    if nx is None:
        return None  # signal: let the browser compute a force layout (cose)
    G = nx.Graph()
    G.add_nodes_from(nodes.keys())
    for e in edges: G.add_edge(e["source"], e["target"])
    # seed by theme so clusters separate cleanly — anchors generated on a ring
    # for whatever themes exist, with "Other" parked in the middle.
    import math
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
    pos = nx.spring_layout(G, pos=init, k=0.45, iterations=240, seed=7)
    # scale to pixels
    xs = [p[0] for p in pos.values()]; ys = [p[1] for p in pos.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    W, H = 4200, 3000
    out = {}
    for nid, (x, y) in pos.items():
        out[nid] = ((x - minx) / (maxx - minx + 1e-9) * W - W / 2,
                    (y - miny) / (maxy - miny + 1e-9) * H - H / 2)
    return out

# ── build payload ────────────────────────────────────────────────────────────
def build(vault, title, source_label=None):
    nodes, edges = load(vault)
    pos = layout(nodes, edges)
    use_preset = pos is not None
    deg = {nid: 0 for nid in nodes}
    for e in edges:
        deg[e["source"]] += 1; deg[e["target"]] += 1
    cy_nodes = []
    for nid, n in nodes.items():
        node = {"data": {**n, "deg": deg[nid], "size": 14 + min(deg[nid], 40) * 2.4}}
        if use_preset:
            x, y = pos[nid]
            node["position"] = {"x": round(x, 1), "y": round(y, 1)}
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
    timeline = [{"month": m, **months[m]} for m in sorted(months)]
    themes = {}
    types = {}
    for n in nodes.values():
        themes[n["theme"]] = themes.get(n["theme"], 0) + 1
        types[n["type"]] = types.get(n["type"], 0) + 1
    theme_colors = assign_theme_colors(themes)
    theme_order = sorted(themes, key=lambda t: (t == "Other", -themes[t], t.lower()))
    src_abs = os.path.abspath(vault)
    parts = src_abs.split(os.sep)
    short = (".../" + "/".join(parts[-3:])) if len(parts) > 4 else src_abs
    return {
        "title": title, "nodes": cy_nodes, "edges": cy_edges,
        "source": source_label or short, "sourceFull": src_abs,
        "layout": "preset" if use_preset else "cose",
        "timeline": timeline, "themeColors": theme_colors, "themeOrder": theme_order,
        "typeShapes": TYPE_SHAPES,
        "stats": {"nodes": len(nodes), "edges": len(edges),
                  "themes": themes, "types": types,
                  "span": [min((n["created"][:10] for n in nodes.values() if n["created"]), default=""),
                           max((n["created"][:10] for n in nodes.values() if n["created"]), default="")]},
    }

# ── HTML ─────────────────────────────────────────────────────────────────────
def render(payload):
    data = json.dumps(payload, ensure_ascii=False)
    return TEMPLATE.replace("/*__DATA__*/", data)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Knowledge Map</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js"></script>
<style>
  :root{
    --bg:#070b16; --panel:rgba(17,24,42,.82); --line:rgba(148,163,184,.14);
    --ink:#e6edf6; --muted:#8aa0bd; --work:#38bdf8; --study:#a78bfa; --life:#fb923c;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:
     radial-gradient(1200px 800px at 20% -10%, rgba(56,189,248,.10), transparent 60%),
     radial-gradient(1100px 700px at 100% 110%, rgba(167,139,250,.10), transparent 55%),
     var(--bg); color:var(--ink);
     font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Inter,sans-serif;
     overflow:hidden}
  #cy{position:fixed;inset:0}
  .glass{background:var(--panel);backdrop-filter:blur(12px);
     border:1px solid var(--line);border-radius:16px;
     box-shadow:0 10px 40px rgba(0,0,0,.45)}
  /* header */
  #top{position:fixed;left:18px;top:18px;display:flex;gap:16px;align-items:center;
     padding:12px 16px;z-index:7;max-width:calc(100vw - 36px)}
  #top .grp{display:flex;flex-direction:column;gap:2px;min-width:0}
  #top h1{font-size:15px;margin:0;letter-spacing:.3px;font-weight:650;white-space:nowrap}
  #top .sub{color:var(--muted);font-size:12px;white-space:nowrap}
  #top #src{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11.5px;
     font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:46vw;overflow:hidden;
     text-overflow:ellipsis;white-space:nowrap}
  #top .vsep{width:1px;height:30px;background:var(--line)}
  #top #counts{white-space:nowrap}
  .chip{display:inline-flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)}
  .dot{width:9px;height:9px;border-radius:50%}
  /* left controls */
  #side{position:fixed;left:18px;top:86px;width:248px;padding:16px;z-index:6;
     max-height:calc(100vh - 240px);display:flex;flex-direction:column;overflow:hidden}
  .side-head{flex:0 0 auto}
  .side-scroll{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
     margin:2px -6px 0 0;padding-right:6px}
  #side h2{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);
     margin:2px 0 10px}
  #search{width:100%;padding:9px 12px;border-radius:10px;border:1px solid var(--line);
     background:rgba(2,6,18,.6);color:var(--ink);outline:none;font-size:13px}
  #search:focus{border-color:rgba(56,189,248,.55)}
  .filter{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:9px;cursor:pointer;
     user-select:none}
  .filter:hover{background:rgba(148,163,184,.08)}
  .filter .box{width:15px;height:15px;border-radius:4px;border:1px solid rgba(148,163,184,.5);
     display:flex;align-items:center;justify-content:center;font-size:11px;color:#0b1020;
     background:#cfe0f5;line-height:1}
  .filter.off .box{background:transparent;color:transparent}
  .filter.off{opacity:.5}
  .filter .sw{width:11px;height:11px;border-radius:50%}
  .filter .ct{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px}
  .sec{margin-top:18px}
  .btn{display:block;width:100%;margin-top:8px;padding:9px;border-radius:10px;border:1px solid var(--line);
     background:rgba(2,6,18,.5);color:var(--ink);cursor:pointer;font-size:13px}
  .btn:hover{border-color:rgba(167,139,250,.5)}
  .btnrow{display:flex;gap:8px;margin-top:8px}
  .btnrow .btn{flex:1;margin-top:0}
  .iconbtn{flex:0 0 38px;width:38px;margin-top:0;padding:0;display:flex;align-items:center;justify-content:center;
     border-radius:10px;border:1px solid var(--line);background:rgba(2,6,18,.5);color:var(--ink);cursor:pointer}
  .iconbtn:hover{border-color:rgba(167,139,250,.5)}
  .iconbtn.off{opacity:.4}
  .iconbtn svg{width:16px;height:16px;display:block}
  /* detail */
  #detail{position:fixed;right:18px;top:86px;width:330px;padding:16px;z-index:6;display:none;
     max-height:calc(100vh - 240px);overflow-y:auto;overscroll-behavior:contain}
  .side-scroll::-webkit-scrollbar,#detail::-webkit-scrollbar{width:8px}
  .side-scroll::-webkit-scrollbar-thumb,#detail::-webkit-scrollbar-thumb{
     background:rgba(148,163,184,.28);border-radius:8px}
  .side-scroll::-webkit-scrollbar-track,#detail::-webkit-scrollbar-track{background:transparent}
  #detail .t{font-size:16px;font-weight:650;margin:0 0 4px;line-height:1.3}
  #detail .meta{color:var(--muted);font-size:12px;margin-bottom:12px}
  #detail .tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
  #detail .tag{font-size:11px;padding:3px 8px;border-radius:999px;background:rgba(148,163,184,.12);
     color:#cbd6e6}
  #detail .sum{font-size:13px;color:#d4deec}
  #detail .nb{margin-top:14px}
  #detail .nb a{display:block;color:#9fc7ff;text-decoration:none;font-size:12.5px;
     padding:4px 0;cursor:pointer}
  #detail .nb a:hover{color:#cfe6ff}
  #detail .close{position:absolute;right:14px;top:12px;color:var(--muted);cursor:pointer;font-size:18px}
  /* timeline — full-width strip along the bottom; panels reserve room above it */
  #tl{position:fixed;left:18px;right:18px;bottom:18px;height:120px;padding:12px 16px 4px;z-index:4}
  #tl .head{display:flex;align-items:center;gap:14px;margin-bottom:0}
  #tl .head .lab{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}
  #tl .play{cursor:pointer;border:1px solid var(--line);background:rgba(2,6,18,.5);color:var(--ink);
     border-radius:8px;padding:5px 14px;font-size:12px}
  #tl .play:hover{border-color:rgba(167,139,250,.6)}
  #tl .read{margin-left:auto;display:flex;gap:14px;align-items:baseline}
  #tl .read .now{color:var(--ink);font-size:13px;font-variant-numeric:tabular-nums}
  #tl .read .tot{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
  #tlchart{position:relative;width:100%;height:84px;cursor:ew-resize}
  #tlsvg{width:100%;height:100%;display:block;touch-action:none}
  #tltip{position:absolute;top:-2px;transform:translateX(-50%);pointer-events:none;display:none;
     background:rgba(2,6,18,.94);border:1px solid var(--line);border-radius:8px;padding:6px 9px;
     font-size:11px;white-space:nowrap;z-index:6;color:var(--ink)}
  .hint{position:fixed;left:50%;bottom:150px;transform:translateX(-50%);color:var(--muted);
     font-size:12px;z-index:4;pointer-events:none;opacity:.0;transition:opacity .4s}

  /* ── mobile / narrow ─────────────────────────────────────────────── */
  @media (max-width: 860px){
    #top{left:10px;right:10px;top:10px;padding:10px 12px;gap:12px}
    #top h1{font-size:14px}
    #top #src{max-width:42vw;font-size:10.5px}
    #top .vsep,#top #counts{display:none}
    #side{left:10px;top:66px;width:min(240px,56vw);padding:12px;max-height:40vh}
    #detail{left:10px;right:10px;top:66px;width:auto;padding:12px;max-height:calc(100vh - 204px)}
    #tl{left:10px;right:10px;bottom:10px;height:104px;padding:10px 12px 2px}
    #tlchart{height:72px}
    .hint{display:none}
  }
  @media (max-width: 520px){
    #top #src{display:none}
    #side{width:64vw}
    #tl .head .lab{display:none}
  }
</style></head>
<body>
<div id="cy"></div>

<div id="top" class="glass">
  <div class="grp">
    <h1 id="ttl">Knowledge Map</h1>
    <div id="src"></div>
  </div>
  <span class="vsep"></span>
  <span class="sub" id="counts"></span>
</div>

<div id="side" class="glass">
  <div class="side-head">
    <h2>Search</h2>
    <input id="search" placeholder="Filter nodes…" autocomplete="off">
    <div class="btnrow">
      <button class="btn" id="fit">Reset view</button>
      <button class="iconbtn" id="toggleEdges" title="Toggle links" aria-label="Toggle links">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2.4"/><circle cx="18" cy="6" r="2.4"/><circle cx="18" cy="18" r="2.4"/><line x1="8.2" y1="10.9" x2="15.8" y2="7.1"/><line x1="8.2" y1="13.1" x2="15.8" y2="16.9"/></svg>
      </button>
    </div>
  </div>
  <div class="side-scroll">
    <div class="sec"><h2>Themes</h2><div id="themeFilters"></div></div>
    <div class="sec"><h2>Types</h2><div id="typeFilters"></div></div>
  </div>
</div>

<div id="detail" class="glass">
  <span class="close" id="dClose">×</span>
  <p class="t" id="dT"></p>
  <div class="meta" id="dM"></div>
  <div class="tags" id="dTags"></div>
  <div class="sum" id="dS"></div>
  <div class="nb" id="dNb"></div>
</div>

<div id="tl" class="glass">
  <div class="head">
    <span class="lab">Timeline — cumulative</span>
    <button class="play" id="play">▶ Play growth</button>
    <span class="read"><span class="now" id="now"></span><span class="tot" id="tot"></span></span>
  </div>
  <div id="tlchart"><svg id="tlsvg"></svg><div id="tltip"></div></div>
</div>
<div class="hint" id="hint">drag the timeline to travel through time · ▶ Play to watch it grow</div>

<script>
const DATA = /*__DATA__*/;
const TC = DATA.themeColors;
document.getElementById('ttl').textContent = DATA.title;
const srcEl = document.getElementById('src');
srcEl.innerHTML = `<span style="opacity:.7">🗄</span> ${DATA.source}`;
srcEl.title = DATA.sourceFull || DATA.source;
document.getElementById('counts').innerHTML =
  `<span class="sub">${DATA.stats.nodes} notes · ${DATA.stats.edges} links · ${DATA.stats.span[0]} → ${DATA.stats.span[1]}</span>`;

// ---- Cytoscape ----
function layoutOpts(){
  if(DATA.layout === 'cose'){
    return { name:'cose', animate:false, randomize:true, fit:true, padding:70,
             nodeRepulsion:9000, idealEdgeLength:62, edgeElasticity:0.4,
             gravity:0.35, numIter:1200, coolingFactor:0.95, nodeDimensionsIncludeLabels:false };
  }
  return { name:'preset' };
}
const cy = cytoscape({
  container: document.getElementById('cy'),
  elements: { nodes: DATA.nodes, edges: DATA.edges },
  layout: layoutOpts(),
  wheelSensitivity: 0.22,
  pixelRatio: Math.min(window.devicePixelRatio||1, 2),
  style: [
    { selector:'node', style:{
        'width':'data(size)','height':'data(size)',
        'background-color': n => TC[n.data('theme')]||TC.Other,
        'shape': n => DATA.typeShapes[n.data('type')] || 'ellipse',
        'border-width':0, 'label':'', 'background-opacity':0.95,
        'shadow-blur':14,'shadow-color': n => TC[n.data('theme')]||TC.Other,
        'shadow-opacity':0.55,'shadow-offset-x':0,'shadow-offset-y':0,
        'transition-property':'opacity,width,height','transition-duration':'140ms'
    }},
    { selector:'node.hub', style:{ 'label':'data(title)','color':'#dfe9f6','font-size':11,
        'text-outline-width':2,'text-outline-color':'#070b16','text-max-width':120,
        'text-wrap':'ellipsis' } },
    { selector:'edge', style:{
        'width':1,'curve-style':'straight','opacity':0.20,
        'line-color': e => TC[e.data('theme')]||TC.Other }},
    { selector:'.dim', style:{ 'opacity':0.05 }},
    { selector:'.hl', style:{ 'opacity':1,'label':'data(title)','color':'#fff','font-size':12,
        'text-outline-width':2,'text-outline-color':'#070b16','z-index':99 }},
    { selector:'edge.hl', style:{ 'opacity':0.9,'width':2 }},
    { selector:'.hidden', style:{ 'display':'none' }},
    { selector:'node.search', style:{ 'border-width':3,'border-color':'#fff' }},
  ]
});

// label only well-connected hubs
cy.nodes().forEach(n => { if (n.data('deg') >= 7) n.addClass('hub'); });

// ---- filters state ----
const themeOn = {}, typeOn = {};
Object.keys(DATA.stats.themes).forEach(t=>themeOn[t]=true);
Object.keys(DATA.stats.types).forEach(t=>typeOn[t]=true);
let cutoff = 1.0; // timeline fraction
let edgesVisible = true;

function buildFilters(el, counts, state, colorize){
  const box = document.getElementById(el);
  Object.entries(counts).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>{
    const row=document.createElement('div'); row.className='filter';
    const bx=document.createElement('span'); bx.className='box';
    const sw=document.createElement('span'); sw.className='sw';
    sw.style.background = colorize? (TC[k]||TC.Other) : 'rgba(148,163,184,.55)';
    const tx=document.createElement('span'); tx.className='nm'; tx.textContent=k;
    const ct=document.createElement('span'); ct.className='ct'; ct.textContent=v;
    row.append(bx,sw,tx,ct); box.append(row);
    const sync=()=>{ row.classList.toggle('off',!state[k]); bx.textContent=state[k]?'✓':''; };
    row.onclick=()=>{ state[k]=!state[k]; sync(); apply(); };
    sync();
  });
}
buildFilters('themeFilters', DATA.stats.themes, themeOn, true);
buildFilters('typeFilters', DATA.stats.types, typeOn, false);

// ---- timeline (cumulative growth, SVG) ----
const months = DATA.timeline.map(m=>m.month);
function cutoffMonth(){ const idx=Math.max(0,Math.ceil(cutoff*months.length)-1); return months[idx]||months[months.length-1]; }

const TL_ORDER = DATA.themeOrder || Object.keys(DATA.themeColors);
const MN=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const chart=document.getElementById('tlchart');
const svg=document.getElementById('tlsvg');
const tip=document.getElementById('tltip');
let TLG=null;
function shortMonth(ym){ const [y,m]=ym.split('-'); return (MN[(+m)-1]||m)+" '"+y.slice(2); }

function buildTimeline(){
  const PAD={l:14,r:14,t:14,b:20};
  const w=chart.clientWidth||900, h=chart.clientHeight||98;
  const iw=w-PAD.l-PAD.r, ih=h-PAD.t-PAD.b, n=DATA.timeline.length;
  let cum={}; TL_ORDER.forEach(t=>cum[t]=0);
  const pts=DATA.timeline.map(m=>{ TL_ORDER.forEach(t=>cum[t]+=m[t]||0); return Object.assign({},cum); });
  const total=TL_ORDER.reduce((s,t)=>s+cum[t],0)||1;
  const X=i=> PAD.l + (n>1? i/(n-1):0)*iw;
  const Y=v=> PAD.t + ih - v/total*ih;
  function areaPath(k){
    const top=i=>{let s=0;for(let j=0;j<=k;j++)s+=pts[i][TL_ORDER[j]];return s;};
    const bot=i=>{let s=0;for(let j=0;j<k;j++)s+=pts[i][TL_ORDER[j]];return s;};
    let d='M'+X(0).toFixed(1)+','+Y(top(0)).toFixed(1);
    for(let i=1;i<n;i++) d+=' L'+X(i).toFixed(1)+','+Y(top(i)).toFixed(1);
    for(let i=n-1;i>=0;i--) d+=' L'+X(i).toFixed(1)+','+Y(bot(i)).toFixed(1);
    return d+'Z';
  }
  let defs='<defs>';
  TL_ORDER.forEach((t,k)=>{ const c=TC[t]||TC.Other;   // id by index — theme names may hold spaces/non-ASCII
    defs+='<linearGradient id="g_'+k+'" x1="0" y1="0" x2="0" y2="1">'+
      '<stop offset="0" stop-color="'+c+'" stop-opacity="0.95"/>'+
      '<stop offset="1" stop-color="'+c+'" stop-opacity="0.30"/></linearGradient>';});
  defs+='<clipPath id="past"><rect id="pastrect" x="'+PAD.l+'" y="0" width="0" height="'+h+'"/></clipPath></defs>';
  let dim='', bright='';
  TL_ORDER.forEach((t,k)=>{ if(!cum[t]) return; const p=areaPath(k);
    dim+='<path d="'+p+'" fill="url(#g_'+k+')" opacity="0.15"/>';
    bright+='<path d="'+p+'" fill="url(#g_'+k+')" opacity="0.92"/>';});
  let axis='<line x1="'+PAD.l+'" y1="'+(PAD.t+ih)+'" x2="'+(w-PAD.r)+'" y2="'+(PAD.t+ih)+'" stroke="rgba(148,163,184,.22)"/>';
  const step=Math.max(1,Math.ceil(n/Math.max(3,Math.floor(iw/66))));
  for(let i=0;i<n;i+=step){ axis+='<text x="'+X(i).toFixed(1)+'" y="'+(h-5)+'" fill="#7f93b0" font-size="10" text-anchor="middle">'+shortMonth(months[i])+'</text>';
    axis+='<line x1="'+X(i).toFixed(1)+'" y1="'+(PAD.t+ih)+'" x2="'+X(i).toFixed(1)+'" y2="'+(PAD.t+ih+3)+'" stroke="rgba(148,163,184,.3)"/>';}
  const ph='<g id="phead"><line y1="'+(PAD.t-3)+'" y2="'+(PAD.t+ih+3)+'" stroke="#eef4fb" stroke-width="1.5" opacity="0.9"/>'+
    '<circle cy="'+(PAD.t-3)+'" r="4" fill="#eef4fb"/></g>';
  svg.setAttribute('viewBox','0 0 '+w+' '+h);
  svg.innerHTML=defs+'<g>'+dim+'</g><g clip-path="url(#past)">'+bright+'</g>'+axis+ph;
  TLG={PAD,iw,n,pts};
  renderPlayhead();
}
function renderPlayhead(){
  if(!TLG) return; const {PAD,iw,n,pts}=TLG;
  const x=PAD.l+cutoff*iw;
  const rect=document.getElementById('pastrect'); if(rect) rect.setAttribute('width',Math.max(0,x-PAD.l).toFixed(1));
  const g=document.getElementById('phead'); if(g) g.setAttribute('transform','translate('+x.toFixed(1)+',0)');
  const i=Math.max(0,Math.ceil(cutoff*n)-1);
  const upto=TL_ORDER.reduce((s,t)=>s+pts[i][t],0);
  document.getElementById('now').textContent=cutoffMonth();
  document.getElementById('tot').textContent=upto+' notes';
}
function tlSet(ev){ if(!TLG) return; const r=svg.getBoundingClientRect(); const {PAD,iw}=TLG;
  setCutoff(((ev.clientX-r.left)-PAD.l)/iw); }
let tlDrag=false;
svg.addEventListener('pointerdown',e=>{tlDrag=true; svg.setPointerCapture(e.pointerId); tlSet(e);});
svg.addEventListener('pointermove',e=>{
  if(tlDrag) tlSet(e);
  if(!TLG) return; const r=svg.getBoundingClientRect(); const {PAD,iw,n}=TLG;
  const i=Math.round(((e.clientX-r.left)-PAD.l)/iw*(n-1));
  if(i>=0 && i<n){ const m=DATA.timeline[i];
    tip.style.display='block'; tip.style.left=(e.clientX-r.left)+'px';
    const tot=TL_ORDER.reduce((s,t)=>s+(m[t]||0),0);
    tip.innerHTML='<b>'+shortMonth(months[i])+'</b> · +'+tot+' that month<br>'+
      TL_ORDER.filter(t=>(m[t]||0)>0)
              .map(t=>'<span style="color:'+(TC[t]||TC.Other)+'">'+t+' '+(m[t]||0)+'</span>')
              .join(' · '); }
});
svg.addEventListener('pointerup',()=>{tlDrag=false;});
svg.addEventListener('pointerleave',()=>{tip.style.display='none';});
window.addEventListener('resize',buildTimeline);

function withinTime(n){
  const c=(n.data('created')||'').slice(0,7);
  if(!c) return true;
  return c <= cutoffMonth();
}
function apply(){
  const q=(document.getElementById('search').value||'').toLowerCase().trim();
  cy.batch(()=>{
    cy.nodes().forEach(n=>{
      const ok = themeOn[n.data('theme')] && typeOn[n.data('type')] && withinTime(n);
      n.toggleClass('hidden', !ok);
      if(q){
        const hit = ok && (n.data('title').toLowerCase().includes(q) ||
                           (n.data('tags')||[]).join(' ').toLowerCase().includes(q));
        n.toggleClass('search', hit);
      } else n.removeClass('search');
    });
    cy.edges().forEach(e=>{
      const vis = edgesVisible && !e.source().hasClass('hidden') && !e.target().hasClass('hidden');
      e.toggleClass('hidden', !vis);
    });
  });
  document.getElementById('now').textContent = cutoffMonth();
  const shown = cy.nodes().filter(n=>!n.hasClass('hidden')).length;
  document.getElementById('counts').innerHTML =
    `<span class="sub">${shown} / ${DATA.stats.nodes} notes · ${DATA.stats.edges} links</span>`;
}
function setCutoff(f){ cutoff=Math.min(1,Math.max(0,f)); renderPlayhead(); apply(); }

document.getElementById('search').oninput=apply;

// play growth
let playing=false, raf=null;
document.getElementById('play').onclick=function(){
  playing=!playing; this.textContent=playing?'❚❚ Pause':'▶ Play growth';
  if(playing){ if(cutoff>=1) setCutoff(0.02);
    const step=()=>{ if(!playing) return;
      setCutoff(cutoff+0.010);
      if(cutoff>=1){ playing=false; document.getElementById('play').textContent='▶ Play growth'; return;}
      raf=requestAnimationFrame(step); };
    step();
  } else cancelAnimationFrame(raf);
};

// ---- interaction ----
function focus(n){
  cy.elements().addClass('dim');
  const nb=n.closedNeighborhood();
  nb.removeClass('dim').addClass('hl');
  document.getElementById('detail').style.display='block';
  document.getElementById('dT').textContent=n.data('title');
  const created=(n.data('created')||'').replace('T',' ').slice(0,16);
  document.getElementById('dM').innerHTML=
     `<span class="chip"><span class="dot" style="background:${TC[n.data('theme')]||TC.Other}"></span>${n.data('theme')}</span>
      &nbsp;·&nbsp; ${n.data('type')} &nbsp;·&nbsp; ${created} &nbsp;·&nbsp; ${n.data('deg')} links`;
  const tg=document.getElementById('dTags'); tg.innerHTML='';
  (n.data('tags')||[]).forEach(t=>{const s=document.createElement('span');s.className='tag';s.textContent=t;tg.append(s);});
  document.getElementById('dS').textContent=n.data('summary')||'—';
  const nbBox=document.getElementById('dNb'); nbBox.innerHTML='';
  const others=nb.nodes().filter(x=>x.id()!==n.id());
  if(others.length){
    const h=document.createElement('div'); h.style.cssText='color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px';
    h.textContent='Connected ('+others.length+')'; nbBox.append(h);
    others.slice(0,18).forEach(x=>{const a=document.createElement('a');a.textContent='› '+x.data('title');
      a.onclick=()=>{cy.animate({center:{eles:x},zoom:1.1},{duration:300}); focus(x);}; nbBox.append(a);});
  }
}
cy.on('tap','node',e=>{ cy.animate({center:{eles:e.target},zoom:Math.max(cy.zoom(),0.9)},{duration:300}); focus(e.target); });
cy.on('tap',e=>{ if(e.target===cy){ cy.elements().removeClass('dim hl'); document.getElementById('detail').style.display='none'; }});
cy.on('mouseover','node',e=>{ if(document.getElementById('detail').style.display!=='block'){ e.target.addClass('hl'); }});
cy.on('mouseout','node',e=>{ if(document.getElementById('detail').style.display!=='block'){ e.target.removeClass('hl'); }});

document.getElementById('dClose').onclick=()=>{cy.elements().removeClass('dim hl');document.getElementById('detail').style.display='none';};
document.getElementById('fit').onclick=()=>cy.animate({fit:{padding:60}},{duration:400});
document.getElementById('toggleEdges').onclick=function(){edgesVisible=!edgesVisible;this.classList.toggle('off',!edgesVisible);apply();};

cy.ready(()=>{ cy.fit(undefined,70); buildTimeline(); apply();
  const h=document.getElementById('hint'); h.style.opacity=1; setTimeout(()=>h.style.opacity=0,4200); });
</script>
</body></html>
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    ap.add_argument("out")
    ap.add_argument("--title", default="Knowledge Map")
    ap.add_argument("--source-label", default=None,
                    help="storage label shown in the header (e.g. 'gbrain: demo'); defaults to the path")
    a = ap.parse_args()
    payload = build(a.vault, a.title, a.source_label)
    open(a.out, "w", encoding="utf-8").write(render(payload))
    s = payload["stats"]
    print(f"map: {s['nodes']} nodes, {s['edges']} edges -> {a.out}")
    print(f"themes: {s['themes']}")
    print(f"types : {s['types']}")

if __name__ == "__main__":
    main()

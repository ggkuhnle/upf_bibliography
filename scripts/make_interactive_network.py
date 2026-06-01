#!/usr/bin/env python3
"""
make_interactive_network.py
Generates output/network_interactive.html — a fully self-contained interactive
UPF co-authorship network with year slider, author search, and click-to-explore.

Usage:
    python make_interactive_network.py
    python make_interactive_network.py --primary   # first↔last-author edges only
"""

import argparse
import collections
import json
import json as _json
import os

import networkx as nx
import pandas as pd


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--primary", action="store_true",
                   help="Use first↔last-author edges (filters honorary middle authors)")
    p.add_argument("--output-dir", default="output")
    p.add_argument("--data-dir", default="data")
    return p.parse_args()

_ARGS = _args()

def _load_cfg():
    for _p in [os.path.join(os.path.dirname(__file__), "config.json"), "config.json"]:
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as _f:
                return _json.load(_f)
    return {}

_CFG    = _load_cfg()
_PREFIX = _CFG.get("prefix", "")
_TITLE  = _CFG.get("title", "Research")

def _pf(name):
    """Apply topic prefix to a filename."""
    return name

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR  = _ARGS.output_dir if _ARGS.output_dir != "output" else (os.path.join("output", _PREFIX) if _PREFIX else "output")
DATA_DIR    = _ARGS.data_dir if _ARGS.data_dir != "data" else (os.path.join("data", _PREFIX) if _PREFIX else "data")
_suffix     = "_primary" if _ARGS.primary else ""
OUTPUT_FILE = os.path.join(OUTPUT_DIR, _pf(f"network_interactive{_suffix}.html"))
MIN_PAPERS  = 3
PLOT_CAP    = 400
LAYOUT_SEED = 42
START_YEAR  = 2005

PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
    "#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD",
    "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF",
]

# ── Load ──────────────────────────────────────────────────────────────────────
print("Loading data…" + (" [primary-author mode]" if _ARGS.primary else ""))
_e  = _pf("coauthorship_edges_primary.csv")     if _ARGS.primary else _pf("coauthorship_edges.csv")
_ey = _pf("coauthorship_edges_by_year_primary.csv") if _ARGS.primary else _pf("coauthorship_edges_by_year.csv")
edges_df    = pd.read_csv(os.path.join(DATA_DIR, _e))
authors_df  = pd.read_csv(os.path.join(DATA_DIR, _pf("papers_by_author.csv")))
edges_yr_df = pd.read_csv(os.path.join(DATA_DIR, _ey))

auth_lookup: dict = {}
for _, r in authors_df.iterrows():
    auth_lookup[r["author_id"]] = {
        "name":        str(r.get("author_name", "") or ""),
        "institution": str(r.get("institution",  "") or ""),
        "country":     str(r.get("country",      "") or ""),
        "papers":      int(r.get("papers",    0)),
        "citations":   int(r.get("citations", 0)),
    }

# ── Build graph ───────────────────────────────────────────────────────────────
print("Building graph…")
G = nx.Graph()
for _, row in edges_df.iterrows():
    a1, a2 = row["author1_id"], row["author2_id"]
    w = int(row.get("shared_papers", 1))
    if not a1 or not a2 or pd.isna(a1) or pd.isna(a2):
        continue
    G.add_edge(a1, a2, weight=w)

# Attach author metadata
for node in G.nodes():
    G.nodes[node].update(auth_lookup.get(node, {"name": node, "papers": 0}))

# Filter by min papers
remove = [n for n in G.nodes() if auth_lookup.get(n, {}).get("papers", 0) < MIN_PAPERS]
G.remove_nodes_from(remove)

# LCC
components = sorted(nx.connected_components(G), key=len, reverse=True)
G_lcc = G.subgraph(components[0]).copy()

# Cap to PLOT_CAP top-degree nodes
if G_lcc.number_of_nodes() > PLOT_CAP:
    top_nodes = sorted(G_lcc.nodes(), key=lambda n: G_lcc.degree(n), reverse=True)[:PLOT_CAP]
    G_plot = G_lcc.subgraph(top_nodes).copy()
else:
    G_plot = G_lcc

print(f"  Graph: {G_plot.number_of_nodes()} nodes, {G_plot.number_of_edges()} edges")

# ── Community detection ───────────────────────────────────────────────────────
print("Detecting communities…")
communities = nx.community.louvain_communities(G_plot, weight="weight", seed=LAYOUT_SEED)
community_map = {node: cid for cid, comm in enumerate(communities) for node in comm}

# ── Layout ────────────────────────────────────────────────────────────────────
print("Computing layout (this may take a moment)…")
pos = nx.spring_layout(G_plot, weight="weight", seed=LAYOUT_SEED, k=0.8)

# ── Prepare node JSON ─────────────────────────────────────────────────────────
node_ids = list(G_plot.nodes())
node_idx = {nid: i for i, nid in enumerate(node_ids)}

nodes_json = []
for nid in node_ids:
    meta = auth_lookup.get(nid, {})
    x, y = pos[nid]
    nodes_json.append({
        "i":    node_idx[nid],
        "name": meta.get("name", nid),
        "inst": meta.get("institution", ""),
        "ctr":  meta.get("country", ""),
        "pap":  meta.get("papers", 0),
        "cit":  meta.get("citations", 0),
        "deg":  G_plot.degree(nid),
        "com":  community_map.get(nid, 0),
        "x":    round(float(x), 6),
        "y":    round(float(y), 6),
    })

# ── Prepare edge JSON (deduplicated to first collaboration year) ───────────────
known = set(node_idx)
pair_first: dict[tuple, int] = {}
pair_papers: dict[tuple, int] = {}

# Total shared papers from aggregate edges
for _, row in edges_df.iterrows():
    a1, a2 = row["author1_id"], row["author2_id"]
    if a1 not in known or a2 not in known:
        continue
    key = (node_idx[a1], node_idx[a2])
    pair_papers[key] = int(row.get("shared_papers", 1))

# First year from per-year edges
for _, row in edges_yr_df.iterrows():
    a1, a2, yr = row["author1_id"], row["author2_id"], row["year"]
    if a1 not in known or a2 not in known or pd.isna(yr):
        continue
    yr = int(yr)
    if yr < START_YEAR:
        continue
    key = (node_idx[a1], node_idx[a2])
    if key not in pair_first or yr < pair_first[key]:
        pair_first[key] = yr

# [a, b, first_year, shared_papers]
edges_json = [
    [a, b, yr, pair_papers.get((a, b), pair_papers.get((b, a), 1))]
    for (a, b), yr in sorted(pair_first.items())
]

max_year = int(edges_yr_df["year"].max())
min_year_data = int(edges_yr_df[edges_yr_df["year"] >= START_YEAR]["year"].min())

print(f"  Edges: {len(edges_json)} unique pairs,  years {min_year_data}–{max_year}")

# ── Embed into HTML ───────────────────────────────────────────────────────────
nodes_str  = json.dumps(nodes_json, separators=(",", ":"))
edges_str  = json.dumps(edges_json, separators=(",", ":"))
palette_str = json.dumps(PALETTE)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_TITLE} — Co-authorship Network Explorer</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js" charset="utf-8"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f8;color:#333;height:100vh;display:flex;flex-direction:column}}
#hdr{{background:#1a2742;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0}}
#hdr h1{{font-size:1.1rem;font-weight:600}}
#hdr p{{font-size:0.75rem;opacity:0.65;margin-top:1px}}
#hdr .spacer{{flex:1}}
#hdr .badge{{background:rgba(255,255,255,0.12);border-radius:20px;padding:3px 10px;font-size:0.72rem}}
#ctrl{{background:#fff;border-bottom:1px solid #d8dde5;padding:10px 20px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;flex-shrink:0}}
.cg{{display:flex;flex-direction:column;gap:3px}}
.cg label{{font-size:0.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.04em}}
.cg-row{{display:flex;align-items:center;gap:8px}}
#yr-val{{font-size:1.15rem;font-weight:800;color:#1a2742;min-width:3rem}}
#yr-sl,#minpap-sl{{width:200px;accent-color:#1a2742;cursor:pointer}}
#minpap-val{{font-size:1.05rem;font-weight:700;color:#1a2742;min-width:1.8rem}}
#auth-in{{padding:6px 10px;border:1px solid #c8ced5;border-radius:5px;font-size:0.88rem;width:210px;outline:none}}
#auth-in:focus{{border-color:#1a2742;box-shadow:0 0 0 2px rgba(26,39,66,.12)}}
#srch-wrap{{position:relative}}
#auth-dd{{position:absolute;top:100%;left:0;z-index:200;background:#fff;border:1px solid #c8ced5;border-top:none;border-radius:0 0 5px 5px;max-height:200px;overflow-y:auto;width:280px;box-shadow:0 6px 16px rgba(0,0,0,.12);display:none}}
.dd-item{{padding:6px 10px;cursor:pointer;font-size:0.83rem;border-bottom:1px solid #f2f2f2}}
.dd-item:hover{{background:#f0f4ff}}
.dd-sub{{font-size:0.72rem;color:#999;margin-top:1px}}
#clear-btn{{padding:5px 13px;background:#6c757d;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:0.82rem;white-space:nowrap}}
#clear-btn:hover{{background:#555}}
#stats{{font-size:0.78rem;color:#888;margin-left:auto}}
#main{{display:flex;flex:1;overflow:hidden}}
#net{{flex:1;min-width:0}}
#panel{{width:270px;background:#fff;border-left:1px solid #d8dde5;display:none;flex-direction:column;overflow-y:auto;flex-shrink:0}}
#panel-hdr{{background:#1a2742;color:#fff;padding:12px 14px;display:flex;justify-content:space-between;align-items:center}}
#panel-hdr h3{{font-size:0.95rem;font-weight:600;line-height:1.2}}
#close-btn{{background:none;border:none;color:#fff;font-size:1.2rem;cursor:pointer;opacity:.7;line-height:1}}
#close-btn:hover{{opacity:1}}
#panel-body{{padding:12px 14px;flex:1}}
.pr{{display:flex;justify-content:space-between;padding:5px 0;font-size:0.8rem;border-bottom:1px solid #f0f0f0}}
.pk{{color:#888}}
.pv{{font-weight:600;text-align:right;max-width:160px}}
#panel-body h4{{font-size:0.72rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}}
#coauth-list{{list-style:none}}
#coauth-list li{{padding:4px 0;font-size:0.8rem;cursor:pointer;color:#1a2742;display:flex;justify-content:space-between;border-bottom:1px solid #f6f6f6}}
#coauth-list li:hover span.name{{text-decoration:underline}}
#coauth-list li span.cnt{{color:#aaa;font-size:0.72rem}}
#panel-footer{{padding:10px 14px;border-top:1px solid #eee;font-size:0.72rem;color:#aaa}}
</style>
</head>
<body>
<div id="hdr">
  <div>
    <h1>{_TITLE} — Co-authorship Network</h1>
    <p>Ultra-processed food literature · OpenAlex</p>
  </div>
  <div class="spacer"></div>
  <span class="badge" id="hdr-stats"></span>
</div>
<div id="ctrl">
  <div class="cg">
    <label>Show papers up to year</label>
    <div class="cg-row">
      <span id="yr-val">{max_year}</span>
      <input type="range" id="yr-sl" min="{min_year_data}" max="{max_year}" value="{max_year}" step="1">
      <span style="font-size:.72rem;color:#aaa">{min_year_data} – {max_year}</span>
    </div>
  </div>
  <div class="cg">
    <label>Find author</label>
    <div id="srch-wrap">
      <input type="text" id="auth-in" placeholder="Type name…" autocomplete="off">
      <div id="auth-dd"></div>
    </div>
  </div>
  <div class="cg">
    <label>Min. papers per author</label>
    <div class="cg-row">
      <span id="minpap-val">3</span>
      <input type="range" id="minpap-sl" min="3" max="30" value="3" step="1">
    </div>
  </div>
  <button id="clear-btn">Clear highlight</button>
  <div id="stats">hover or click a node to explore</div>
</div>
<div id="main">
  <div id="net"></div>
  <div id="panel">
    <div id="panel-hdr">
      <h3 id="panel-name"></h3>
      <button id="close-btn">&#x2715;</button>
    </div>
    <div id="panel-body">
      <div id="panel-rows"></div>
      <h4>Collaborators in this view</h4>
      <ul id="coauth-list"></ul>
    </div>
    <div id="panel-footer">Click a collaborator name to navigate</div>
  </div>
</div>
<script>
// ── Embedded data ─────────────────────────────────────────────────────────────
const NODES   = {nodes_str};   // {{i,name,inst,ctr,pap,cit,deg,com,x,y}}
const EDGES   = {edges_str};   // [a,b,first_year,shared_papers]
const PAL     = {palette_str};
const MAX_YR  = {max_year};
const MIN_YR  = {min_year_data};

// ── Pre-compute adjacency  {{nodeIdx: {{neighbourIdx: shared_papers}}}} ──────────
const adj = NODES.map(() => ({{}}));
EDGES.forEach(([a,b,yr,pap]) => {{
  adj[a][b] = pap;
  adj[b][a] = pap;
}});

// ── State ─────────────────────────────────────────────────────────────────────
let curYear = MAX_YR;
let hlNode  = null;  // integer index or null
let minPap  = 3;

// ── Active nodes / edges for a given year ─────────────────────────────────────
function activeSet(year) {{
  const nodes = new Set(), edges = [];
  EDGES.forEach(([a,b,yr,pap]) => {{
    if (yr <= year) {{ nodes.add(a); nodes.add(b); edges.push([a,b,pap]); }}
  }});
  return {{nodes, edges}};
}}

// ── Build Plotly traces ───────────────────────────────────────────────────────
function buildTraces(year, hl, minPap=3) {{
  // Active nodes/edges: within year AND meeting minPap threshold
  const activeNodes = new Set();
  const activeEdges = [];
  EDGES.forEach(([a,b,yr,pap]) => {{
    if (yr <= year && NODES[a].pap >= minPap && NODES[b].pap >= minPap) {{
      activeNodes.add(a); activeNodes.add(b);
      activeEdges.push([a,b,pap]);
    }}
  }});

  // When highlighting: neighbours of hl node (in active set)
  const hlNeighbours = new Set();
  if (hl !== null) {{
    activeEdges.forEach(([a,b]) => {{
      if (a === hl) hlNeighbours.add(b);
      if (b === hl) hlNeighbours.add(a);
    }});
  }}

  // Edge trace — only hl's edges when highlighted, all otherwise
  const ex=[], ey=[];
  activeEdges.forEach(([a,b]) => {{
    if (hl !== null && a !== hl && b !== hl) return;
    ex.push(NODES[a].x, NODES[b].x, null);
    ey.push(NODES[a].y, NODES[b].y, null);
  }});

  // Node trace
  const nx_=[], ny_=[], nc=[], ns=[], no_=[], nt=[], nci=[];
  NODES.forEach((n,i) => {{
    if (!activeNodes.has(i)) return;
    let op = 1;
    if (hl !== null) {{
      if      (i === hl)              op = 1.0;
      else if (hlNeighbours.has(i))   op = 0.85;
      else                            op = 0.07;
    }}
    nx_.push(n.x); ny_.push(n.y);
    nc.push(PAL[n.com % PAL.length]);
    ns.push(Math.max(5, Math.min(20, 4 + n.deg * 0.2)));
    no_.push(op);
    nt.push(`<b>${{n.name}}</b><br>${{n.inst||'—'}}<br>${{n.ctr||'—'}}<br>Papers: ${{n.pap}} · Citations: ${{n.cit.toLocaleString()}}<extra></extra>`);
    nci.push(i);
  }});

  return [
    {{type:'scatter', mode:'lines', x:ex, y:ey,
      line:{{width:0.5, color: hl===null ? 'rgba(130,130,130,0.25)' : 'rgba(26,39,66,0.35)'}},
      hoverinfo:'none', showlegend:false}},
    {{type:'scatter', mode:'markers', x:nx_, y:ny_,
      marker:{{size:ns, color:nc, opacity:no_, line:{{width:0.5,color:'white'}}}},
      hovertemplate:nt, customdata:nci, showlegend:false}},
  ];
}}

const layout = {{
  margin:{{l:8,r:8,t:8,b:8}},
  xaxis:{{showgrid:false,zeroline:false,showticklabels:false,fixedrange:false}},
  yaxis:{{showgrid:false,zeroline:false,showticklabels:false,scaleanchor:'x'}},
  hovermode:'closest', paper_bgcolor:'#f9fafc', plot_bgcolor:'#f9fafc',
  showlegend:false,
}};
const config = {{
  displayModeBar:true, responsive:true,
  modeBarButtonsToRemove:['select2d','lasso2d','toggleSpikelines','autoScale2d'],
}};

function updateStats(year, minPap=3) {{
  const nodes = new Set(), edges = [];
  EDGES.forEach(([a,b,yr]) => {{
    if (yr <= year && NODES[a].pap >= minPap && NODES[b].pap >= minPap)
      {{ nodes.add(a); nodes.add(b); edges.push(1); }}
  }});
  document.getElementById('stats').textContent =
    `${{nodes.size}} authors · ${{edges.length}} collaborations`;
  document.getElementById('hdr-stats').textContent =
    `${{nodes.size}} authors · ${{edges.length}} co-authorship links`;
}}

// ── Initial render ────────────────────────────────────────────────────────────
Plotly.newPlot('net', buildTraces(curYear, null, minPap), layout, config);
updateStats(curYear, minPap);

// ── Year slider ───────────────────────────────────────────────────────────────
const slEl = document.getElementById('yr-sl');
const yrEl = document.getElementById('yr-val');
let slTimer = null;
slEl.addEventListener('input', () => {{
  yrEl.textContent = slEl.value;
  clearTimeout(slTimer);
  slTimer = setTimeout(() => {{
    curYear = +slEl.value;
    Plotly.react('net', buildTraces(curYear, hlNode, minPap), layout);
    updateStats(curYear, minPap);
    if (hlNode !== null) refreshPanel(hlNode);
  }}, 60);
}});

// ── Author search dropdown ────────────────────────────────────────────────────
const authIn = document.getElementById('auth-in');
const authDd = document.getElementById('auth-dd');
const nameList = NODES.map((n,i) => ({{i, name:n.name, inst:n.inst}}))
                      .sort((a,b) => a.name.localeCompare(b.name));

function showDd(q) {{
  const ql = q.toLowerCase();
  const hits = nameList.filter(x => x.name.toLowerCase().includes(ql)).slice(0,18);
  if (!hits.length) {{ authDd.style.display='none'; return; }}
  authDd.innerHTML = hits.map(h =>
    `<div class="dd-item" data-i="${{h.i}}">
       <div>${{h.name}}</div>
       <div class="dd-sub">${{h.inst||''}}</div>
     </div>`
  ).join('');
  authDd.querySelectorAll('.dd-item').forEach(el =>
    el.addEventListener('mousedown', e => {{
      e.preventDefault();
      pickAuthor(+el.dataset.i);
      authDd.style.display='none';
    }})
  );
  authDd.style.display='block';
}}
authIn.addEventListener('input', () => {{
  const q = authIn.value.trim();
  q.length >= 2 ? showDd(q) : (authDd.style.display='none');
}});
authIn.addEventListener('blur', () => setTimeout(() => authDd.style.display='none', 160));
authIn.addEventListener('focus', () => authIn.value.trim().length >= 2 && showDd(authIn.value.trim()));

// ── Node click ────────────────────────────────────────────────────────────────
document.getElementById('net').on('plotly_click', data => {{
  const pt = data.points[0];
  if (pt.customdata !== undefined && pt.customdata !== null) pickAuthor(+pt.customdata);
}});

function pickAuthor(idx) {{
  hlNode = idx;
  authIn.value = NODES[idx].name;
  Plotly.react('net', buildTraces(curYear, hlNode, minPap), layout);
  refreshPanel(idx);
}}

// ── Info panel ────────────────────────────────────────────────────────────────
function refreshPanel(idx) {{
  const n = NODES[idx];
  document.getElementById('panel-name').textContent = n.name;

  const rows = [
    ['Institution', n.inst || '—'],
    ['Country',     n.ctr  || '—'],
    ['Papers', n.pap],
    ['Citations',   n.cit.toLocaleString()],
    ['Co-authors (network)', Object.keys(adj[idx]).length],
  ];
  document.getElementById('panel-rows').innerHTML = rows.map(([k,v]) =>
    `<div class="pr"><span class="pk">${{k}}</span><span class="pv">${{v}}</span></div>`
  ).join('');

  // Co-authors active in current year, sorted by shared papers
  const coauths = Object.entries(adj[idx])
    .filter(([j]) => {{
      const jn = +j;
      return EDGES.some(([a,b,yr]) => yr <= curYear && ((a===idx&&b===jn)||(b===idx&&a===jn)));
    }})
    .map(([j,pap]) => [+j, pap])
    .sort((a,b) => b[1]-a[1])
    .slice(0,12);

  const list = document.getElementById('coauth-list');
  list.innerHTML = coauths.length
    ? coauths.map(([j,pap]) =>
        `<li data-j="${{j}}">
           <span class="name">${{NODES[j].name}}</span>
           <span class="cnt">${{pap}} paper${{pap>1?'s':''}}</span>
         </li>`).join('')
    : '<li style="color:#aaa;cursor:default">None visible at this year</li>';

  list.querySelectorAll('li[data-j]').forEach(el =>
    el.addEventListener('click', () => pickAuthor(+el.dataset.j))
  );

  document.getElementById('panel').style.display = 'flex';
}}

document.getElementById('close-btn').addEventListener('click', () => {{
  document.getElementById('panel').style.display = 'none';
}});

document.getElementById('clear-btn').addEventListener('click', () => {{
  hlNode = null; authIn.value = '';
  Plotly.react('net', buildTraces(curYear, null, minPap), layout);
  document.getElementById('panel').style.display = 'none';
  document.getElementById('stats').textContent = 'hover or click a node to explore';
}});

// ── Min-papers slider ────────────────────────────────────────────────────────
const minPapSl  = document.getElementById('minpap-sl');
const minPapVal = document.getElementById('minpap-val');
let minPapTimer = null;
minPapSl.addEventListener('input', () => {{
  minPapVal.textContent = minPapSl.value;
  clearTimeout(minPapTimer);
  minPapTimer = setTimeout(() => {{
    minPap = +minPapSl.value;
    if (hlNode !== null && NODES[hlNode].pap < minPap) {{
      hlNode = null; authIn.value = '';
      document.getElementById('panel').style.display = 'none';
    }}
    Plotly.react('net', buildTraces(curYear, hlNode, minPap), layout);
    updateStats(curYear, minPap);
  }}, 60);
}});
</script>
</body>
</html>
"""

with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
    fh.write(html)

size_kb = os.path.getsize(OUTPUT_FILE) // 1024
print(f"\nWritten: {OUTPUT_FILE}  ({size_kb} KB)")
print("Open in any browser — no server needed.")

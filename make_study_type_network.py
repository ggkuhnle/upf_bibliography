#!/usr/bin/env python3
"""
make_study_type_network.py
Co-authorship network where each node is coloured and shaped by the author's
dominant study type (RCT / Observational / Systematic Review etc.).

Requires papers_by_author_study_type.csv produced by upf_bibliometrics.py.

Usage:
    python make_study_type_network.py
    python make_study_type_network.py --primary   # first↔last-author edges only
"""

import argparse
import collections
import json
import os

import networkx as nx
import pandas as pd


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--primary", action="store_true",
                   help="Use first↔last-author edges (filters honorary middle authors)")
    return p.parse_args()

_ARGS = _args()

OUTPUT_DIR  = "output"
_suffix     = "_primary" if _ARGS.primary else ""
OUTPUT_FILE = os.path.join(OUTPUT_DIR, f"network_study_type{_suffix}.html")
MIN_PAPERS  = 1
PLOT_CAP    = 600
LAYOUT_SEED = 42
START_YEAR  = 2005

# Study-type visual styles  (order = legend order)
ST_STYLES = [
    {"name": "Observational",                    "color": "#27ae60", "symbol": "circle"},
    {"name": "Systematic Review / Meta-analysis","color": "#8e44ad", "symbol": "diamond"},
    {"name": "Review",                           "color": "#9b59b6", "symbol": "square"},
    {"name": "RCT",                              "color": "#2980b9", "symbol": "triangle-up"},
    {"name": "Clinical Trial",                   "color": "#e67e22", "symbol": "cross"},
    {"name": "Other",                            "color": "#95a5a6", "symbol": "circle-open"},
]
ST_INDEX = {s["name"]: i for i, s in enumerate(ST_STYLES)}

# ── Load ──────────────────────────────────────────────────────────────────────
print("Loading data…" + (" [primary-author mode]" if _ARGS.primary else ""))
_e  = "coauthorship_edges_primary.csv"         if _ARGS.primary else "coauthorship_edges.csv"
_ey = "coauthorship_edges_by_year_primary.csv" if _ARGS.primary else "coauthorship_edges_by_year.csv"
edges_df    = pd.read_csv(os.path.join(OUTPUT_DIR, _e))
authors_df  = pd.read_csv(os.path.join(OUTPUT_DIR, "papers_by_author.csv"))
edges_yr_df = pd.read_csv(os.path.join(OUTPUT_DIR, _ey))

author_st_path = os.path.join(OUTPUT_DIR, "papers_by_author_study_type.csv")
if not os.path.exists(author_st_path):
    print(f"Missing {author_st_path} — run upf_bibliometrics.py first.")
    raise SystemExit(1)
author_st_df = pd.read_csv(author_st_path)

# Dominant study type per author (most papers in that category)
dominant_st: dict = {}
for _, row in author_st_df.iterrows():
    aid = str(row["author_id"])
    if aid not in dominant_st or int(row["papers"]) > dominant_st[aid]["papers"]:
        dominant_st[aid] = {
            "study_type": str(row["study_type"]),
            "papers":     int(row["papers"]),
        }

auth_lookup: dict = {}
for _, r in authors_df.iterrows():
    aid = str(r["author_id"])
    st_name = dominant_st.get(aid, {}).get("study_type", "Other")
    auth_lookup[aid] = {
        "name":        str(r.get("author_name", "") or ""),
        "institution": str(r.get("institution",  "") or ""),
        "country":     str(r.get("country",      "") or ""),
        "papers":      int(r.get("papers",    0)),
        "citations":   int(r.get("citations", 0)),
        "st":          ST_INDEX.get(st_name, ST_INDEX["Other"]),
    }

# ── Build graph ───────────────────────────────────────────────────────────────
print("Building graph…")
G = nx.Graph()
for _, row in edges_df.iterrows():
    a1, a2 = str(row["author1_id"]), str(row["author2_id"])
    w = int(row.get("shared_papers", 1))
    if not a1 or not a2 or a1 == "nan" or a2 == "nan":
        continue
    G.add_edge(a1, a2, weight=w)

for node in G.nodes():
    G.nodes[node].update(auth_lookup.get(node, {"name": node, "papers": 0, "st": ST_INDEX["Other"]}))

remove = [n for n in G.nodes() if auth_lookup.get(n, {}).get("papers", 0) < MIN_PAPERS]
G.remove_nodes_from(remove)

components = sorted(nx.connected_components(G), key=len, reverse=True)
G_lcc = G.subgraph(components[0]).copy()

if G_lcc.number_of_nodes() > PLOT_CAP:
    top_nodes = sorted(G_lcc.nodes(), key=lambda n: G_lcc.degree(n), reverse=True)[:PLOT_CAP]
    G_plot = G_lcc.subgraph(top_nodes).copy()
else:
    G_plot = G_lcc

print(f"  Graph: {G_plot.number_of_nodes()} nodes, {G_plot.number_of_edges()} edges")

# ── Layout ────────────────────────────────────────────────────────────────────
print("Computing layout…")
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
        "st":   meta.get("st", ST_INDEX["Other"]),
        "x":    round(float(x), 6),
        "y":    round(float(y), 6),
    })

# ── Prepare edge JSON ─────────────────────────────────────────────────────────
known = set(node_idx)
pair_first:  dict[tuple, int] = {}
pair_papers: dict[tuple, int] = {}

for _, row in edges_df.iterrows():
    a1, a2 = str(row["author1_id"]), str(row["author2_id"])
    if a1 not in known or a2 not in known:
        continue
    key = (node_idx[a1], node_idx[a2])
    pair_papers[key] = int(row.get("shared_papers", 1))

for _, row in edges_yr_df.iterrows():
    a1, a2, yr = str(row["author1_id"]), str(row["author2_id"]), row["year"]
    if a1 not in known or a2 not in known or pd.isna(yr):
        continue
    yr = int(yr)
    if yr < START_YEAR:
        continue
    key = (node_idx[a1], node_idx[a2])
    if key not in pair_first or yr < pair_first[key]:
        pair_first[key] = yr

edges_json = [
    [a, b, yr, pair_papers.get((a, b), pair_papers.get((b, a), 1))]
    for (a, b), yr in sorted(pair_first.items())
]

max_year      = int(edges_yr_df["year"].max())
min_year_data = int(edges_yr_df[edges_yr_df["year"] >= START_YEAR]["year"].min())

print(f"  Edges: {len(edges_json)} unique pairs,  years {min_year_data}–{max_year}")

# ── Embed into HTML ───────────────────────────────────────────────────────────
nodes_str   = json.dumps(nodes_json,  separators=(",", ":"))
edges_str   = json.dumps(edges_json,  separators=(",", ":"))
styles_str  = json.dumps(ST_STYLES,   separators=(",", ":"))

DISCLAIMER = (
    '<div style="background:#fff8e1;border-top:4px solid #f9a825;'
    'border-bottom:1px solid #f9a825;padding:10px 20px;'
    'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;'
    'font-size:.82rem;color:#4a3800;line-height:1.5;flex-shrink:0">'
    "<strong>Disclaimer:</strong> Automatically generated bibliometric data from "
    '<a href="https://openalex.org" style="color:#4a3800;font-weight:600" target="_blank">OpenAlex</a>. '
    "Does not represent the views of any individual, institution, or organisation. "
    "Study-type labels are algorithmically assigned and may contain errors."
    "</div>"
)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPF Co-authorship — Study Type Network</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js" charset="utf-8"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f8;color:#333;height:100vh;display:flex;flex-direction:column}}
#hdr{{background:#1a2742;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0}}
#hdr h1{{font-size:1.1rem;font-weight:600}}
#hdr p{{font-size:0.75rem;opacity:0.65;margin-top:1px}}
#hdr .spacer{{flex:1}}
#hdr .badge{{background:rgba(255,255,255,0.12);border-radius:20px;padding:3px 10px;font-size:0.72rem}}
#ctrl{{background:#fff;border-bottom:1px solid #d8dde5;padding:8px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;flex-shrink:0}}
#ctrl2{{background:#fafbfc;border-bottom:1px solid #d8dde5;padding:7px 20px;display:flex;align-items:center;gap:10px;flex-shrink:0}}
.cg{{display:flex;flex-direction:column;gap:3px}}
.cg label{{font-size:0.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.04em}}
.cg-row{{display:flex;align-items:center;gap:8px}}
#yr-val{{font-size:1.15rem;font-weight:800;color:#1a2742;min-width:3rem}}
#yr-sl,#minpap-sl{{width:160px;accent-color:#1a2742;cursor:pointer}}
#minpap-val{{font-size:1.05rem;font-weight:700;color:#1a2742;min-width:1.8rem}}
#auth-in{{padding:6px 10px;border:1px solid #c8ced5;border-radius:5px;font-size:0.88rem;width:200px;outline:none}}
#auth-in:focus{{border-color:#1a2742;box-shadow:0 0 0 2px rgba(26,39,66,.12)}}
#srch-wrap{{position:relative}}
#auth-dd{{position:absolute;top:100%;left:0;z-index:200;background:#fff;border:1px solid #c8ced5;border-top:none;border-radius:0 0 5px 5px;max-height:200px;overflow-y:auto;width:280px;box-shadow:0 6px 16px rgba(0,0,0,.12);display:none}}
.dd-item{{padding:6px 10px;cursor:pointer;font-size:0.83rem;border-bottom:1px solid #f2f2f2}}
.dd-item:hover{{background:#f0f4ff}}
.dd-sub{{font-size:0.72rem;color:#999;margin-top:1px}}
.act-btn{{padding:5px 13px;border:none;border-radius:4px;cursor:pointer;font-size:0.82rem;white-space:nowrap;transition:background .15s}}
#clear-btn{{background:#6c757d;color:#fff}}.anim-btn{{background:#1a2742;color:#fff}}
#clear-btn:hover{{background:#555}}.anim-btn:hover{{background:#2c4a8a}}
#stats{{font-size:0.78rem;color:#888;margin-left:auto}}
/* study type pills */
.st-pill-wrap{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.st-pill{{padding:3px 11px;border-radius:12px;cursor:pointer;font-size:0.75rem;font-weight:600;border:2px solid var(--c,#888);color:var(--c,#888);background:#fff;transition:background .13s,color .13s;white-space:nowrap;user-select:none}}
.st-pill.active{{background:var(--c,#888);color:#fff}}
.st-pill:hover:not(.active){{background:rgba(0,0,0,0.04)}}
.pill-label{{font-size:0.7rem;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}}
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
{DISCLAIMER}
<div id="hdr">
  <div>
    <h1>UPF Co-authorship — Study Type Network</h1>
    <p>Node shape &amp; colour = author's dominant study type · OpenAlex</p>
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
      <span style="font-size:.72rem;color:#aaa">{min_year_data}–{max_year}</span>
      <button class="act-btn anim-btn" id="play-btn">&#9654; Play</button>
      <button class="act-btn anim-btn" id="reset-btn">&#8634; Reset</button>
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
    <label>Min. papers</label>
    <div class="cg-row">
      <span id="minpap-val">1</span>
      <input type="range" id="minpap-sl" min="1" max="30" value="1" step="1">
    </div>
  </div>
  <button class="act-btn" id="clear-btn">Clear all</button>
  <div id="stats">hover or click a node to explore</div>
</div>

<div id="ctrl2">
  <span class="pill-label">Highlight type:</span>
  <div class="st-pill-wrap" id="st-pills"></div>
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
const NODES   = {nodes_str};
const EDGES   = {edges_str};
const STYLES  = {styles_str};
const MAX_YR  = {max_year};
const MIN_YR  = {min_year_data};

// ── Pre-compute adjacency ─────────────────────────────────────────────────────
const adj = NODES.map(() => ({{}}));
EDGES.forEach(([a,b,,pap]) => {{ adj[a][b] = pap; adj[b][a] = pap; }});

// ── State ─────────────────────────────────────────────────────────────────────
let curYear     = MAX_YR;
let hlNode      = null;
let hlType      = null;
let minPap      = 1;
let animTimer   = null;
let animRunning = false;

// ── Build Plotly traces ───────────────────────────────────────────────────────
function buildTraces(year, hl, mp, ht) {{
  const activeNodes = new Set();
  const activeEdges = [];
  EDGES.forEach(([a,b,yr,pap]) => {{
    if (yr <= year && NODES[a].pap >= mp && NODES[b].pap >= mp) {{
      activeNodes.add(a); activeNodes.add(b);
      activeEdges.push([a,b,pap]);
    }}
  }});

  const hlNeighbours = new Set();
  if (hl !== null) {{
    activeEdges.forEach(([a,b]) => {{
      if (a === hl) hlNeighbours.add(b);
      if (b === hl) hlNeighbours.add(a);
    }});
  }}

  const ex=[], ey=[];
  activeEdges.forEach(([a,b]) => {{
    if (hl !== null && a !== hl && b !== hl) return;
    ex.push(NODES[a].x, NODES[b].x, null);
    ey.push(NODES[a].y, NODES[b].y, null);
  }});

  const edgeTrace = {{
    type:'scatter', mode:'lines', x:ex, y:ey,
    line:{{width:0.6, color: hl===null ? 'rgba(130,130,130,0.22)' : 'rgba(26,39,66,0.35)'}},
    hoverinfo:'none', showlegend:false,
  }};

  const nodeTraces = STYLES.map((st, si) => {{
    const nx_=[], ny_=[], ns=[], no_=[], nt=[], nci=[];
    NODES.forEach((n,i) => {{
      if (!activeNodes.has(i) || n.st !== si) return;
      let op;
      if (ht !== null && si !== ht) {{
        op = 0.06;
      }} else if (hl !== null) {{
        if      (i === hl)             op = 1.0;
        else if (hlNeighbours.has(i))  op = 0.85;
        else                           op = 0.07;
      }} else {{
        op = 1.0;
      }}
      nx_.push(n.x); ny_.push(n.y);
      ns.push(Math.max(6, Math.min(22, 5 + n.deg * 0.22)));
      no_.push(op);
      nt.push(`<b>${{n.name}}</b><br>${{n.inst||'—'}}<br>Study type: ${{st.name}}<br>Papers: ${{n.pap}} · Citations: ${{n.cit.toLocaleString()}}<extra></extra>`);
      nci.push(i);
    }});
    return {{
      type:'scatter', mode:'markers', name: st.name,
      x: nx_, y: ny_,
      marker:{{ symbol:st.symbol, size:ns, color:st.color, opacity:no_, line:{{width:0.8,color:'white'}} }},
      hovertemplate:nt, customdata:nci,
      showlegend:true, legendgroup:st.name,
    }};
  }});

  return [edgeTrace, ...nodeTraces];
}}

const layout = {{
  margin:{{l:8,r:8,t:8,b:8}},
  xaxis:{{showgrid:false,zeroline:false,showticklabels:false,fixedrange:false}},
  yaxis:{{showgrid:false,zeroline:false,showticklabels:false,scaleanchor:'x'}},
  hovermode:'closest',
  paper_bgcolor:'#f9fafc', plot_bgcolor:'#f9fafc',
  showlegend:true,
  legend:{{x:0,y:1,xanchor:'left',yanchor:'top',bgcolor:'rgba(255,255,255,0.85)',bordercolor:'#ddd',borderwidth:1,font:{{size:11}}}},
}};
const config = {{
  displayModeBar:true, responsive:true,
  modeBarButtonsToRemove:['select2d','lasso2d','toggleSpikelines','autoScale2d'],
}};

function updateStats(year, mp) {{
  const nodes = new Set(); let ec = 0;
  EDGES.forEach(([a,b,yr]) => {{
    if (yr <= year && NODES[a].pap >= mp && NODES[b].pap >= mp)
      {{ nodes.add(a); nodes.add(b); ec++; }}
  }});
  document.getElementById('stats').textContent = `${{nodes.size}} authors · ${{ec}} collaborations`;
  document.getElementById('hdr-stats').textContent = `${{nodes.size}} authors · ${{ec}} co-authorship links`;
}}

// ── Initial render ────────────────────────────────────────────────────────────
Plotly.newPlot('net', buildTraces(curYear, null, minPap, null), layout, config);
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
    Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
    updateStats(curYear, minPap);
    if (hlNode !== null) refreshPanel(hlNode);
  }}, 60);
}});

// ── Animation ────────────────────────────────────────────────────────────────
const playBtn  = document.getElementById('play-btn');
const resetBtn = document.getElementById('reset-btn');

function stopAnim() {{
  clearInterval(animTimer); animTimer = null; animRunning = false;
  playBtn.innerHTML = '&#9654; Play';
}}

function startAnim() {{
  if (+slEl.value >= MAX_YR) {{
    slEl.value = MIN_YR; curYear = MIN_YR; yrEl.textContent = MIN_YR;
    Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
    updateStats(curYear, minPap);
  }}
  animRunning = true; playBtn.innerHTML = '&#9646;&#9646; Pause';
  animTimer = setInterval(() => {{
    const next = +slEl.value + 1;
    if (next > MAX_YR) {{ stopAnim(); return; }}
    slEl.value = next; yrEl.textContent = next; curYear = next;
    Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
    updateStats(curYear, minPap);
    if (hlNode !== null) refreshPanel(hlNode);
  }}, 700);
}}

playBtn.addEventListener('click', () => {{ if (animRunning) stopAnim(); else startAnim(); }});
resetBtn.addEventListener('click', () => {{
  stopAnim();
  slEl.value = MIN_YR; curYear = MIN_YR; yrEl.textContent = MIN_YR;
  Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
  updateStats(curYear, minPap);
}});

// ── Study type pills ──────────────────────────────────────────────────────────
const pillsWrap = document.getElementById('st-pills');
pillsWrap.innerHTML =
  `<span class="st-pill active" data-st="-1" style="--c:#555">All</span>` +
  STYLES.map((s,i) => `<span class="st-pill" data-st="${{i}}" style="--c:${{s.color}}">${{s.name}}</span>`).join('');

function setPillActive(stIdx) {{
  document.querySelectorAll('.st-pill').forEach(p => p.classList.remove('active'));
  const sel = stIdx === null
    ? document.querySelector('[data-st="-1"]')
    : document.querySelector(`[data-st="${{stIdx}}"]`);
  if (sel) sel.classList.add('active');
}}

pillsWrap.addEventListener('click', e => {{
  const pill = e.target.closest('.st-pill');
  if (!pill) return;
  const st = +pill.dataset.st;
  hlType = (st === -1 || hlType === st) ? null : st;
  setPillActive(hlType);
  Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
}});

// Legend click highlights the type instead of toggling visibility
document.getElementById('net').on('plotly_legendclick', data => {{
  const stIdx = data.curveNumber - 1;
  if (stIdx < 0 || stIdx >= STYLES.length) return false;
  hlType = (hlType === stIdx) ? null : stIdx;
  setPillActive(hlType);
  Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
  return false;
}});

// ── Author search ─────────────────────────────────────────────────────────────
const authIn = document.getElementById('auth-in');
const authDd = document.getElementById('auth-dd');
const nameList = NODES.map((n,i) => ({{i,name:n.name,inst:n.inst}})).sort((a,b) => a.name.localeCompare(b.name));

function showDd(q) {{
  const ql = q.toLowerCase();
  const hits = nameList.filter(x => x.name.toLowerCase().includes(ql)).slice(0,18);
  if (!hits.length) {{ authDd.style.display='none'; return; }}
  authDd.innerHTML = hits.map(h =>
    `<div class="dd-item" data-i="${{h.i}}"><div>${{h.name}}</div><div class="dd-sub">${{h.inst||''}}</div></div>`
  ).join('');
  authDd.querySelectorAll('.dd-item').forEach(el =>
    el.addEventListener('mousedown', e => {{ e.preventDefault(); pickAuthor(+el.dataset.i); authDd.style.display='none'; }})
  );
  authDd.style.display='block';
}}
authIn.addEventListener('input', () => {{ const q=authIn.value.trim(); q.length>=2 ? showDd(q) : (authDd.style.display='none'); }});
authIn.addEventListener('blur',  () => setTimeout(() => authDd.style.display='none', 160));
authIn.addEventListener('focus', () => authIn.value.trim().length>=2 && showDd(authIn.value.trim()));

// ── Node click & panel ────────────────────────────────────────────────────────
document.getElementById('net').on('plotly_click', data => {{
  const pt = data.points[0];
  if (pt.customdata !== undefined && pt.customdata !== null) pickAuthor(+pt.customdata);
}});

function pickAuthor(idx) {{
  hlNode = idx; authIn.value = NODES[idx].name;
  Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
  refreshPanel(idx);
}}

function refreshPanel(idx) {{
  const n = NODES[idx];
  const stName = STYLES[n.st] ? STYLES[n.st].name : 'Unknown';
  document.getElementById('panel-name').textContent = n.name;
  const rows = [
    ['Institution',          n.inst||'—'],
    ['Country',              n.ctr||'—'],
    ['Dominant study type',  stName],
    ['Papers (UPF)',         n.pap],
    ['Citations',            n.cit.toLocaleString()],
    ['Co-authors (network)', Object.keys(adj[idx]).length],
  ];
  document.getElementById('panel-rows').innerHTML = rows.map(([k,v]) =>
    `<div class="pr"><span class="pk">${{k}}</span><span class="pv">${{v}}</span></div>`).join('');

  const coauths = Object.entries(adj[idx])
    .filter(([j]) => {{ const jn=+j; return EDGES.some(([a,b,yr]) => yr<=curYear&&((a===idx&&b===jn)||(b===idx&&a===jn))); }})
    .map(([j,pap]) => [+j,pap]).sort((a,b) => b[1]-a[1]).slice(0,12);

  const list = document.getElementById('coauth-list');
  list.innerHTML = coauths.length
    ? coauths.map(([j,pap]) =>
        `<li data-j="${{j}}"><span class="name">${{NODES[j].name}}</span><span class="cnt">${{pap}} paper${{pap>1?'s':''}}</span></li>`).join('')
    : '<li style="color:#aaa;cursor:default">None visible at this year</li>';
  list.querySelectorAll('li[data-j]').forEach(el => el.addEventListener('click', () => pickAuthor(+el.dataset.j)));
  document.getElementById('panel').style.display = 'flex';
}}

document.getElementById('close-btn').addEventListener('click', () => {{ document.getElementById('panel').style.display='none'; }});

document.getElementById('clear-btn').addEventListener('click', () => {{
  hlNode = null; hlType = null; authIn.value = '';
  setPillActive(null);
  Plotly.react('net', buildTraces(curYear, null, minPap, null), layout);
  document.getElementById('panel').style.display = 'none';
  document.getElementById('stats').textContent = 'hover or click a node to explore';
}});

// ── Min-papers slider ─────────────────────────────────────────────────────────
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
    Plotly.react('net', buildTraces(curYear, hlNode, minPap, hlType), layout);
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

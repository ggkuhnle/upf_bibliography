#!/usr/bin/env python3
"""
make_citation_network.py
Standalone HTML dashboard: internal citation network for this corpus.

Reads:
  output/<prefix>_citation_edges_author.csv
  output/<prefix>_papers_by_author.csv
  output/<prefix>_citation_edges_author_by_year.csv  (optional — enables year filter)

Writes:
  output/<prefix>_citation_network.html
"""

import argparse
import json as _json
import os
from collections import Counter, defaultdict

import networkx as nx
import pandas as pd
import plotly.graph_objects as go

OUTPUT_DIR = "output"


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
    return name


PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
    "#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD",
    "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF",
]

TOP_NODES       = 2000   # safety cap — normally overridden by min-papers/min-citations filter
PLOTLY_NODES    = 300    # cap for Plotly overview (static scatter)
DEFAULT_DISPLAY = 500    # initial nodes shown in Cytoscape
TOP_BAR     = 25
TOP_LABELS  = 20
LAYOUT_SEED = 42

PLOTLY_CDN    = "https://cdn.plot.ly/plotly-3.5.0.min.js"
CYTOSCAPE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"

SUBCLASS_SHAPES = {
    "Flavan-3-ol":  "ellipse",
    "Flavanone":    "triangle",
    "Flavone":      "rectangle",
    "Flavonol":     "diamond",
    "Anthocyanin":  "pentagon",
    "Isoflavone":   "hexagon",
    "—":            "ellipse",
}
SUBCLASS_COLORS = {
    "Flavan-3-ol":  "#636EFA",
    "Flavanone":    "#EF553B",
    "Flavone":      "#00CC96",
    "Flavonol":     "#AB63FA",
    "Anthocyanin":  "#FFA15A",
    "Isoflavone":   "#19D3F3",
    "—":            "#bbbbbb",
}

DISCLAIMER = (
    '<div style="background:#fff8e1;border-top:4px solid #f9a825;'
    'border-bottom:1px solid #f9a825;padding:10px 24px;'
    'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;'
    'font-size:.82rem;color:#4a3800;line-height:1.5">'
    "<strong>Disclaimer:</strong> Automatically generated bibliometric data from "
    '<a href="https://openalex.org" style="color:#4a3800;font-weight:600" '
    'target="_blank">OpenAlex</a>. '
    "Does not represent the views of any individual, institution, or organisation. "
    "Citation edges are restricted to works within this corpus; they do not reflect "
    "total citation counts. "
    "For papers with more than 20 authors (large consortium studies), citation edges "
    "are attributed to the first and last author only."
    "</div>"
)


def _community_partition(G_undirected):
    try:
        from community import best_partition
        return best_partition(G_undirected)
    except ImportError:
        import networkx.algorithms.community as nx_comm
        communities = nx_comm.greedy_modularity_communities(G_undirected)
        partition = {}
        for i, comm in enumerate(communities):
            for node in comm:
                partition[node] = i
        return partition


def fig_citation_network(G, author_lookup, partition, pos):
    """Return (fig, node_ids, node_names_list).

    Trace order (fixed — JS constants must match):
      0  edge_trace_normal
      1  edge_trace_recip
      2  node_trace
      3  label_trace
      4+ community legend traces
    """
    node_ids = list(G.nodes())
    indegree  = dict(G.in_degree())

    # ── Classify edges as reciprocal or normal ────────────────────────────────
    recip_set = {(u, v) for u, v in G.edges() if G.has_edge(v, u)}
    normal_ex, normal_ey = [], []
    recip_ex,  recip_ey  = [], []
    for u, v in G.edges():
        xu, yu = pos[u]; xv, yv = pos[v]
        if (u, v) in recip_set:
            recip_ex  += [xu, xv, None]; recip_ey  += [yu, yv, None]
        else:
            normal_ex += [xu, xv, None]; normal_ey += [yu, yv, None]

    edge_trace_normal = go.Scatter(
        x=normal_ex, y=normal_ey, mode="lines",
        line=dict(width=0.4, color="rgba(150,150,150,0.3)"),
        hoverinfo="none", showlegend=False,
    )
    edge_trace_recip = go.Scatter(
        x=recip_ex, y=recip_ey, mode="lines",
        line=dict(width=1.3, color="rgba(230,100,30,0.55)"),
        hoverinfo="none", showlegend=True, name="Mutual citation",
    )

    # ── Node trace ────────────────────────────────────────────────────────────
    nx_, ny_, nc, ns, nt = [], [], [], [], []
    node_names_list = []
    for nid in node_ids:
        meta   = author_lookup.get(nid, {})
        indeg  = indegree.get(nid, 0)
        outdeg = G.out_degree(nid)
        cit    = int(meta.get("citations", 0))
        name   = meta.get("author_name") or str(nid)
        inst   = meta.get("institution") or "—"
        ctr    = meta.get("country") or "—"
        x, y   = pos[nid]
        nx_.append(x); ny_.append(y)
        nc.append(PALETTE[partition.get(nid, 0) % len(PALETTE)])
        ns.append(max(5, min(22, 5 + indeg * 0.6)))
        nt.append(
            f"<b>{name}</b><br>"
            f"{inst}<br>Country: {ctr}<br>"
            f"Cited by {indeg} author(s) in corpus<br>"
            f"Cites {outdeg} author(s) in corpus<br>"
            f"Total OpenAlex citations: {cit:,}"
            "<extra></extra>"
        )
        node_names_list.append(name)

    node_trace = go.Scatter(
        x=nx_, y=ny_, mode="markers",
        marker=dict(size=ns, color=nc, line=dict(width=0.5, color="white")),
        hovertemplate=nt, showlegend=False,
    )

    # ── Label trace: names for the top TOP_LABELS most-cited nodes ────────────
    top_label_nodes = sorted(node_ids, key=lambda n: indegree.get(n, 0), reverse=True)[:TOP_LABELS]
    label_trace = go.Scatter(
        x=[pos[n][0] for n in top_label_nodes],
        y=[pos[n][1] for n in top_label_nodes],
        mode="text",
        text=[(author_lookup.get(n, {}).get("author_name") or "")[:28]
              for n in top_label_nodes],
        textposition="top center",
        textfont=dict(size=8, color="#222"),
        hoverinfo="none", showlegend=False,
    )

    # ── Community legend (top 8 communities by size) ──────────────────────────
    comm_sizes = Counter(partition.values())
    top_comms  = [c for c, _ in comm_sizes.most_common(min(8, len(comm_sizes)))]
    legend_traces = [
        go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(color=PALETTE[c % len(PALETTE)], size=12),
            name=f"Research cluster {c + 1}", showlegend=True,
        )
        for c in sorted(top_comms)
    ]

    fig = go.Figure(
        data=[edge_trace_normal, edge_trace_recip, node_trace, label_trace] + legend_traces
    )
    fig.update_layout(
        title=dict(
            text=f"{_TITLE} — Internal Citation Network (top {len(node_ids)} authors by in-degree)",
            font=dict(size=14),
        ),
        height=720,
        margin=dict(l=10, r=10, t=50, b=10),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        plot_bgcolor="white",
        paper_bgcolor="white",
        hovermode="closest",
        legend=dict(
            title=dict(text="Legend"),
            itemsizing="constant",
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#ddd", borderwidth=1,
            font=dict(size=11),
        ),
    )
    return fig, node_ids, node_names_list


def fig_top_cited(author_lookup, indegree_map):
    rows = []
    for aid, indeg in indegree_map.items():
        meta = author_lookup.get(aid, {})
        rows.append({"name": (meta.get("author_name") or str(aid))[:50], "indeg": indeg})
    df = pd.DataFrame(rows).nlargest(TOP_BAR, "indeg").sort_values("indeg")
    fig = go.Figure(go.Bar(
        x=df["indeg"], y=df["name"], orientation="h",
        marker_color="#636EFA",
        text=df["indeg"], textposition="outside",
        hovertemplate="<b>%{y}</b><br>Cited by %{x} author(s) in corpus<extra></extra>",
    ))
    fig.update_layout(
        title=f"Top {TOP_BAR} Most-Cited Authors (within corpus)",
        xaxis_title="In-degree (cited by N distinct authors in corpus)",
        height=max(500, TOP_BAR * 22),
        margin=dict(l=10, r=80, t=50, b=40),
        plot_bgcolor="white", paper_bgcolor="white",
        yaxis=dict(tickfont=dict(size=11)),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_betweenness(author_lookup, betweenness_map):
    rows = []
    for aid, btwn in betweenness_map.items():
        meta = author_lookup.get(aid, {})
        rows.append({"name": (meta.get("author_name") or str(aid))[:50], "btwn": btwn})
    df = pd.DataFrame(rows).nlargest(TOP_BAR, "btwn").sort_values("btwn")
    df["label"] = df["btwn"].apply(lambda x: f"{x:.4f}")
    fig = go.Figure(go.Bar(
        x=df["btwn"], y=df["name"], orientation="h",
        marker_color="#E76F51",
        text=df["label"], textposition="outside",
        hovertemplate="<b>%{y}</b><br>Betweenness centrality: %{x:.4f}<extra></extra>",
    ))
    fig.update_layout(
        title=f"Top {TOP_BAR} Bridge Authors (Betweenness Centrality)",
        xaxis_title="Betweenness centrality (normalised 0–1)",
        height=max(500, TOP_BAR * 22),
        margin=dict(l=10, r=100, t=50, b=40),
        plot_bgcolor="white", paper_bgcolor="white",
        yaxis=dict(tickfont=dict(size=11)),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    return fig


def write_cytoscape_html(out, G_plot, author_lookup, partition, pos,
                          pagerank, edge_papers_map,
                          n_communities, generated_date, author_subclass=None,
                          author_funders=None):
    """Write a fullscreen Cytoscape.js citation-network explorer."""
    indegree_map = dict(G_plot.in_degree())
    # Sort all nodes by in-degree so rank 1 = most cited
    node_ids = sorted(G_plot.nodes(), key=lambda n: indegree_map.get(n, 0), reverse=True)
    n_nodes = len(node_ids)
    n_edges = G_plot.number_of_edges()

    pr_vals = list(pagerank.values())
    pr_min, pr_max = (min(pr_vals), max(pr_vals)) if pr_vals else (0, 1)

    def _indeg_sz(indeg):
        return round(max(8, min(44, 8 + indeg * 1.4)), 2)

    def _pr_sz(pr):
        if pr_max == pr_min:
            return 18
        return round(8 + 36 * (pr - pr_min) / (pr_max - pr_min), 2)

    # Country colours
    ctrs_all = [(author_lookup.get(nid, {}).get("country") or "—") for nid in node_ids]
    unique_ctrs = list(dict.fromkeys(c for c in ctrs_all if c and c != "—"))
    ctr_color: dict = {"—": "#bbbbbb"}
    for k, c in enumerate(unique_ctrs):
        ctr_color[c] = PALETTE[k % len(PALETTE)]

    # Papers-count gradient (light → dark blue)
    papers_counts = [int(author_lookup.get(nid, {}).get("papers", 0)) for nid in node_ids]
    p_max = max(papers_counts) if papers_counts else 1

    def _pcol(n):
        t = min(1.0, n / p_max) if p_max else 0
        return f"rgb({int(210 - 160 * t)},{int(225 - 170 * t)},{int(255 - 60 * t)})"

    # Edge width 0.8–5 px, proportional to weight
    all_w = [G_plot[u][v].get("weight", 1) for u, v in G_plot.edges()]
    w_max_e = max(all_w) if all_w else 1

    def _ewidth(w):
        return round(0.8 + 4.2 * min(1.0, (w - 1) / max(1, w_max_e - 1)), 2)

    recip_set = {(u, v) for u, v in G_plot.edges() if G_plot.has_edge(v, u)}
    node_id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
    n_recip = len(recip_set) // 2
    cy_scale = 1200
    _asc = author_subclass or {}
    _afu = author_funders or {}

    # Rebuild in/out adjacency lists using this function's node ordering
    in_adj_list  = [[] for _ in node_ids]
    out_adj_list = [[] for _ in node_ids]
    for u, v in G_plot.edges():
        ui = node_id_to_idx[u]; vi = node_id_to_idx[v]
        w  = G_plot[u][v].get("weight", 1)
        out_adj_list[ui].append([vi, w])
        in_adj_list[vi].append([ui, w])
    for i in range(len(node_ids)):
        in_adj_list[i].sort(key=lambda x: -x[1])
        out_adj_list[i].sort(key=lambda x: -x[1])

    # ── Nodes ─────────────────────────────────────────────────────────────────
    cy_nodes = []
    for i, nid in enumerate(node_ids):
        m = author_lookup.get(nid, {})
        name = m.get("author_name") or str(nid)
        country = m.get("country") or "—"
        papers_n = int(m.get("papers", 0))
        sc = _asc.get(nid, "—")
        funders_str = _afu.get(nid, "")
        x, y = pos[nid]
        cy_nodes.append({
            "data": {
                "id": str(i), "name": name, "label": name[:30],
                "inst":     m.get("institution") or "",
                "country":  country,
                "papers":   papers_n,
                "citations": int(m.get("citations", 0)),
                "indeg":    indegree_map.get(nid, 0),
                "outdeg":   G_plot.out_degree(nid),
                "subclass":       sc,
                "funders":        funders_str,
                "colorComm":      PALETTE[partition.get(nid, 0) % len(PALETTE)],
                "colorCountry":   ctr_color.get(country, "#bbbbbb"),
                "colorPapers":    _pcol(papers_n),
                "colorSubclass":  SUBCLASS_COLORS.get(sc, "#bbbbbb"),
                "sizeIndeg": _indeg_sz(indegree_map.get(nid, 0)),
                "sizePR":    _pr_sz(pagerank.get(nid, pr_min)),
                "shape":     SUBCLASS_SHAPES.get(sc, "ellipse"),
                "rank":      i + 1,
            },
            "position": {
                "x": round(float(x) * cy_scale, 2),
                "y": round(float(y) * cy_scale, 2),
            },
        })

    # ── Edges ─────────────────────────────────────────────────────────────────
    cy_edges = []
    for u, v in G_plot.edges():
        ui = node_id_to_idx[u]; vi = node_id_to_idx[v]
        w = G_plot[u][v].get("weight", 1)
        cy_edges.append({
            "data": {
                "id": f"e{ui}_{vi}",
                "source": str(ui), "target": str(vi),
                "weight": w, "width": _ewidth(w),
                "recip": (u, v) in recip_set,
                "papers": edge_papers_map.get((u, v), [])[:5],
            }
        })

    node_names_js = _json.dumps([n["data"]["name"] for n in cy_nodes], separators=(",", ":"))
    node_inst_js  = _json.dumps([n["data"]["inst"]  for n in cy_nodes], separators=(",", ":"))
    cy_nodes_js   = _json.dumps(cy_nodes,  separators=(",", ":"))
    cy_edges_js   = _json.dumps(cy_edges,  separators=(",", ":"))
    in_adj_js     = _json.dumps(in_adj_list,  separators=(",", ":"))
    out_adj_js    = _json.dumps(out_adj_list, separators=(",", ":"))

    # Build shape legend HTML (shown when colour-by-subclass is selected)
    _shape_syms = {"ellipse": "●", "triangle": "▲", "rectangle": "■",
                   "diamond": "◆", "pentagon": "⬠", "hexagon": "⬡"}
    _legend_items = [
        f'<span style="margin-right:8px;color:{SUBCLASS_COLORS[sc]}">'
        f'{_shape_syms.get(SUBCLASS_SHAPES[sc], "●")} {sc}</span>'
        for sc in SUBCLASS_SHAPES if sc != "—"
    ]
    shape_legend_html = (
        '<div id="shape-legend" style="font-size:.63rem;color:#555;'
        'margin-top:4px;flex-basis:100%;display:none">'
        + "".join(_legend_items) + "</div>"
    )

    css = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden;background:#f4f6f8}
#hdr{background:#1a2742;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0}
#hdr h1{font-size:1.05rem;font-weight:700}
#hdr p{font-size:.72rem;opacity:.65;margin-top:1px}
.spacer{flex:1}
#hdr a{color:rgba(255,255,255,.65);font-size:.75rem;text-decoration:none}
#hdr a:hover{color:#fff}
#ctrl{background:#fff;border-bottom:1px solid #d8dde5;padding:7px 16px;display:flex;flex-wrap:wrap;gap:7px 14px;align-items:center;flex-shrink:0}
.cg{display:flex;flex-direction:column;gap:2px}
.cg>label{font-size:.66rem;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:.04em}
.sep{width:1px;height:28px;background:#dde1e7;margin:0 2px;align-self:center;flex-shrink:0}
select{padding:4px 7px;border:1px solid #c8ced5;border-radius:5px;font-size:.8rem;background:#fff;cursor:pointer;outline:none;color:#333}
select:focus{border-color:#1a2742}
.btn-grp{display:flex}
.tog{background:#eee;border:1px solid #ccc;padding:4px 10px;cursor:pointer;font-size:.78rem;color:#555}
.tog:first-child{border-radius:5px 0 0 5px}.tog:last-child{border-radius:0 5px 5px 0}
.tog.active{background:#1a2742;color:#fff;border-color:#1a2742}
.tog:not(.active):hover{background:#ddd}
.reset-btn{background:#e74c3c;color:#fff;border:none;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:.78rem;display:none;white-space:nowrap}
.reset-btn:hover{background:#c0392b}
.dl-btn{background:#2c4a8a;color:#fff;border:none;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:.78rem;white-space:nowrap}
.dl-btn:hover{background:#1a2742}
.path-btn{background:#27ae60;color:#fff;border:none;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:.78rem;white-space:nowrap;align-self:flex-end}
.path-btn:hover{background:#1e8449}
#path-result{font-size:.74rem;color:#1a5296;font-style:italic;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.srch-wrap{position:relative;display:inline-block}
.srch-in{padding:5px 9px;border:1px solid #c8ced5;border-radius:5px;font-size:.82rem;width:170px;outline:none}
.srch-in:focus{border-color:#1a2742;box-shadow:0 0 0 2px rgba(26,39,66,.1)}
.dd{position:absolute;top:100%;left:0;z-index:500;background:#fff;border:1px solid #c8ced5;border-top:none;border-radius:0 0 5px 5px;max-height:200px;overflow-y:auto;width:250px;box-shadow:0 6px 16px rgba(0,0,0,.12);display:none}
.dd-item{padding:5px 9px;cursor:pointer;font-size:.8rem;border-bottom:1px solid #f2f2f2}
.dd-item:hover{background:#f0f4ff}
.dd-sub{font-size:.68rem;color:#999;margin-top:1px}
.nb-lbl{display:flex;align-items:center;gap:5px;font-size:.78rem;color:#555;cursor:pointer;white-space:nowrap}
.nb-lbl input{cursor:pointer}
#workspace{display:flex;flex:1;min-height:0;position:relative}
#cy{flex:1;min-width:0}
#side-panel{width:280px;background:#fff;border-left:1px solid #d8dde5;display:none;flex-direction:column;overflow-y:auto;flex-shrink:0}
#panel-hdr{background:#1a2742;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
#panel-hdr h3{font-size:.88rem;font-weight:600;line-height:1.3;word-break:break-word}
#close-panel{background:none;border:none;color:#fff;font-size:1.1rem;cursor:pointer;opacity:.7}
#close-panel:hover{opacity:1}
#panel-body{padding:10px 14px;flex:1;overflow-y:auto}
.pr{display:flex;justify-content:space-between;padding:3px 0;font-size:.79rem;border-bottom:1px solid #f2f2f2}
.pk{color:#888}.pv{font-weight:600;text-align:right}
#panel-body h4{font-size:.66rem;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.04em;margin:10px 0 4px}
.cit-list{list-style:none}
.cit-list li{display:flex;justify-content:space-between;padding:3px 0;font-size:.79rem;cursor:pointer;border-bottom:1px solid #f6f6f6}
.cit-list li.empty{color:#aaa;cursor:default;font-style:italic}
.cn{color:#1a5296}.cn:hover{text-decoration:underline}
.cc{color:#bbb;font-size:.7rem;white-space:nowrap;margin-left:4px}
#edge-popup{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#fff;border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,.22);width:460px;max-width:92%;max-height:65%;display:none;flex-direction:column;z-index:400}
#ep-hdr{background:#1a2742;color:#fff;padding:11px 16px;border-radius:10px 10px 0 0;display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
#ep-title{font-size:.88rem;font-weight:600;line-height:1.4;word-break:break-word}
#close-ep{background:none;border:none;color:#fff;font-size:1.1rem;cursor:pointer;opacity:.7;flex-shrink:0}
#close-ep:hover{opacity:1}
#ep-body{padding:12px 16px;overflow-y:auto;flex:1}
#ep-count{font-size:.8rem;color:#666;margin-bottom:8px}
#ep-list{list-style:none}
#ep-list li{padding:5px 0;font-size:.81rem;border-bottom:1px solid #f2f2f2;line-height:1.4;color:#2c3e50}
#ep-list li:last-child{border-bottom:none}
"""

    js_vars = f"""
var CY_NODES   = {cy_nodes_js};
var CY_EDGES   = {cy_edges_js};
var NODE_NAMES = {node_names_js};
var NODE_INST  = {node_inst_js};
var IN_ADJ     = {in_adj_js};
var OUT_ADJ    = {out_adj_js};
var DEFAULT_DISPLAY = {min(DEFAULT_DISPLAY, n_nodes)};
var TOTAL_NODES = {n_nodes};
"""

    js_code = r"""
var state = {egoIdx:-1, pathFrom:-1, pathTo:-1, sizeMode:'indeg', nbOnly:false};
var cy;
var _displayN = DEFAULT_DISPLAY;

var CY_STYLE = [
  {selector:'node', style:{
    'background-color':'data(colorComm)', 'width':'data(sizeIndeg)', 'height':'data(sizeIndeg)',
    'shape':'data(shape)',
    'label':'', 'font-size':13, 'text-valign':'top', 'text-halign':'center',
    'text-wrap':'wrap', 'text-max-width':120, 'color':'#111',
    'text-outline-width':3, 'text-outline-color':'rgba(255,255,255,0.9)',
    'border-width':0, 'z-index':1,
  }},
  {selector:'node.labeled',       style:{'label':'data(label)'}},
  {selector:'node.hovered',       style:{'label':'data(label)','border-width':2.5,'border-color':'#e74c3c','z-index':9999}},
  {selector:'node.ego',           style:{'border-width':3,'border-color':'#e74c3c','z-index':100}},
  {selector:'node.neighbor',      style:{'border-width':2,'border-color':'rgba(231,76,60,0.4)','z-index':50}},
  {selector:'node.faded',         style:{'opacity':0.07}},
  {selector:'node.hidden',        style:{'display':'none'}},
  {selector:'node.path-node',     style:{'border-width':3,'border-color':'#27ae60','z-index':100}},
  {selector:'node.funder-match',  style:{'border-width':4,'border-color':'#f39c12','z-index':110,'label':'data(label)'}},
  {selector:'node.funder-faded',  style:{'opacity':0.06}},
  {selector:'node.funder-unknown',style:{'opacity':0.22}},
  {selector:'node.inf-seed',  style:{'border-width':5,'border-color':'#c0392b','z-index':120,'label':'data(label)'}},
  {selector:'node.inf-dn1',  style:{'border-width':3,'border-color':'#8e44ad','z-index':110,'label':'data(label)'}},
  {selector:'node.inf-dn2',  style:{'border-width':2,'border-color':'#bb8fce','z-index':100}},
  {selector:'node.inf-dn3',  style:{'border-width':1.5,'border-color':'#d7bde2','z-index':90}},
  {selector:'node.inf-up1',  style:{'border-width':3,'border-color':'#0d9488','z-index':110,'label':'data(label)'}},
  {selector:'node.inf-up2',  style:{'border-width':2,'border-color':'#5eead4','z-index':100}},
  {selector:'node.inf-up3',  style:{'border-width':1.5,'border-color':'#99f6e4','z-index':90}},
  {selector:'node.inf-faded',style:{'opacity':0.05}},
  {selector:'edge.inf-faded',style:{'opacity':0.04}},
  {selector:'edge', style:{
    'width':'data(width)',
    'line-color':'rgba(120,120,120,0.3)',
    'target-arrow-color':'rgba(120,120,120,0.35)',
    'target-arrow-shape':'triangle',
    'arrow-scale':0.6,
    'curve-style':'bezier',
  }},
  {selector:'edge[?recip]', style:{
    'line-color':'rgba(210,95,30,0.5)',
    'target-arrow-color':'rgba(210,95,30,0.55)',
    'source-arrow-shape':'triangle',
    'source-arrow-color':'rgba(210,95,30,0.55)',
  }},
  {selector:'edge.faded',    style:{'opacity':0.04}},
  {selector:'edge.path-edge',style:{'line-color':'#27ae60','target-arrow-color':'#27ae60','width':3,'z-index':99}},
];

function initCy() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements: {nodes: CY_NODES, edges: CY_EDGES},
    style: CY_STYLE,
    layout: {name:'preset'},
    minZoom:0.04, maxZoom:8,
    boxSelectionEnabled:false,
    selectionType:'single',
    pixelRatio: 'auto',
  });
  cy.nodes().sort(function(a,b){return b.data('indeg')-a.data('indeg');}).slice(0,30).addClass('labeled');
  cy.on('tap','node',  function(e){ selectNode(e.target); });
  cy.on('tap','edge',  function(e){ openEdgePopup(e.target); });
  cy.on('tap',         function(e){ if(e.target===cy) resetAll(); });
  cy.on('mouseover','node', function(e){ e.target.addClass('hovered'); });
  cy.on('mouseout', 'node', function(e){ e.target.removeClass('hovered'); });
  // Hide nodes beyond DEFAULT_DISPLAY initially
  cy.batch(function() {
    cy.nodes().forEach(function(n) {
      if (n.data('rank') > DEFAULT_DISPLAY) n.hide();
    });
  });
  _displayN = DEFAULT_DISPLAY;
  updateNodeSlider();
}

function applyNodeLimit(n) {
  _displayN = n;
  cy.batch(function() {
    cy.nodes().forEach(function(nd) {
      if (nd.data('rank') <= n) nd.show(); else nd.hide();
    });
  });
  updateNodeSlider();
}

function updateNodeSlider() {
  var sl = document.getElementById('node-limit-slider');
  var lb = document.getElementById('node-limit-label');
  if (sl) sl.value = _displayN;
  if (lb) lb.textContent = 'Showing ' + Math.min(_displayN, TOTAL_NODES) + ' of ' + TOTAL_NODES + ' authors';
}

function selectNode(node) {
  clearPath();
  cy.elements().removeClass('ego neighbor faded hidden');
  state.egoIdx = parseInt(node.id());
  var nhd = node.closedNeighborhood();
  node.addClass('ego');
  nhd.nodes().not(node).addClass('neighbor');
  cy.elements().not(nhd).addClass(state.nbOnly ? 'hidden' : 'faded');
  document.getElementById('reset-btn').style.display = 'inline-block';
  openPanel(state.egoIdx);
}

function resetAll() {
  state.egoIdx=-1; state.pathFrom=-1; state.pathTo=-1;
  cy.elements().removeClass('ego neighbor faded hidden path-node path-edge');
  clearFunderHighlight();
  clearInfluence();
  document.getElementById('reset-btn').style.display='none';
  document.getElementById('side-panel').style.display='none';
  document.getElementById('edge-popup').style.display='none';
  document.getElementById('path-result').textContent='';
  ['srch-in','srch-in2','srch-in3'].forEach(function(id){document.getElementById(id).value='';});
}

function highlightFunder() {
  var q = document.getElementById('funder-in').value.trim().toLowerCase();
  cy.nodes().removeClass('funder-match funder-faded funder-unknown');
  // Clear influence so classes don't conflict
  cy.nodes().removeClass('inf-seed inf-dn1 inf-dn2 inf-dn3 inf-up1 inf-up2 inf-up3 inf-faded');
  document.getElementById('inf-badge').textContent = '';
  var badge = document.getElementById('funder-badge');
  if (q.length < 2) { badge.textContent = ''; return; }
  var matched = 0;
  cy.nodes().forEach(function(n) {
    var fs = (n.data('funders') || '').toLowerCase();
    if (fs === '') {
      n.addClass('funder-unknown');
    } else if (fs.indexOf(q) >= 0) {
      n.addClass('funder-match');
      matched++;
    } else {
      n.addClass('funder-faded');
    }
  });
  badge.textContent = matched + ' author' + (matched !== 1 ? 's' : '') + ' matched';
}

function clearFunderHighlight() {
  document.getElementById('funder-in').value = '';
  document.getElementById('funder-badge').textContent = '';
  cy.nodes().removeClass('funder-match funder-faded funder-unknown');
}

var _infDepth = 1;
var _infDir   = 'down';

function setInfluenceDepth(d) {
  _infDepth = d;
  [0,1,2,3].forEach(function(n){
    document.getElementById('inf-d'+n).classList.toggle('active', n===d);
  });
  exploreInfluence();
}

function setInfluenceDir(dir) {
  _infDir = dir;
  document.getElementById('inf-dir-down').classList.toggle('active', dir==='down');
  document.getElementById('inf-dir-up').classList.toggle('active', dir==='up');
  exploreInfluence();
}

function exploreInfluence() {
  var q = document.getElementById('inf-in').value.trim().toLowerCase();
  cy.elements().removeClass('inf-seed inf-dn1 inf-dn2 inf-dn3 inf-up1 inf-up2 inf-up3 inf-faded');
  var badge = document.getElementById('inf-badge');
  if (q.length < 2) { badge.textContent = ''; return; }

  // Clear funder highlight so classes don't conflict
  cy.nodes().removeClass('funder-match funder-faded funder-unknown');
  document.getElementById('funder-badge').textContent = '';

  // Seeds: match author name OR any funder string
  var seeds = cy.nodes().filter(function(n) {
    return (n.data('name')    || '').toLowerCase().indexOf(q) >= 0
        || (n.data('funders') || '').toLowerCase().indexOf(q) >= 0;
  });
  if (!seeds.length) { badge.textContent = 'No match'; return; }

  // BFS through directed citation edges
  var distMap = new Map();
  seeds.forEach(function(n){ distMap.set(n.id(), 0); });
  var frontier = seeds.toArray();

  for (var d = 1; d <= _infDepth; d++) {
    var next = [];
    frontier.forEach(function(n){
      var nbrs = _infDir === 'down' ? n.incomers('node') : n.outgoers('node');
      nbrs.forEach(function(nb){
        if (!distMap.has(nb.id())){ distMap.set(nb.id(), d); next.push(nb); }
      });
    });
    frontier = next;
  }

  // Apply node classes — downstream (purple) vs upstream (teal)
  var pfx = _infDir === 'down' ? 'dn' : 'up';
  var counts = [0,0,0,0];
  cy.nodes().forEach(function(n){
    var d = distMap.get(n.id());
    if (d === undefined) { n.addClass('inf-faded'); return; }
    counts[d]++;
    if      (d===0) n.addClass('inf-seed');
    else if (d===1) n.addClass('inf-'+pfx+'1');
    else if (d===2) n.addClass('inf-'+pfx+'2');
    else            n.addClass('inf-'+pfx+'3');
  });

  // Fade edges where either endpoint is faded
  cy.edges().forEach(function(e){
    if (e.source().hasClass('inf-faded') || e.target().hasClass('inf-faded')) {
      e.addClass('inf-faded');
    }
  });

  var label = _infDir === 'down' ? 'citing' : 'cited';
  var parts = [counts[0] + ' seed' + (counts[0]>1?'s':'')];
  for (var i = 1; i <= _infDepth; i++) {
    if (counts[i]) parts.push(counts[i] + ' ' + label + '@' + i);
  }
  badge.textContent = parts.join(' · ');
}

function clearInfluence() {
  document.getElementById('inf-in').value = '';
  document.getElementById('inf-badge').textContent = '';
  cy.elements().removeClass('inf-seed inf-dn1 inf-dn2 inf-dn3 inf-up1 inf-up2 inf-up3 inf-faded');
}

function toggleNeighborsOnly() {
  state.nbOnly = document.getElementById('nb-only').checked;
  if (state.egoIdx >= 0) {
    var nhd = cy.getElementById(String(state.egoIdx)).closedNeighborhood();
    var others = cy.elements().not(nhd);
    if (state.nbOnly) { others.removeClass('faded').addClass('hidden'); }
    else              { others.removeClass('hidden').addClass('faded'); }
  }
}

function setColorMode(mode) {
  var field = {comm:'colorComm', country:'colorCountry', papers:'colorPapers', subclass:'colorSubclass'}[mode]||'colorComm';
  cy.nodes().style('background-color', function(ele){ return ele.data(field); });
  var leg = document.getElementById('shape-legend');
  if (leg) leg.style.display = mode === 'subclass' ? 'block' : 'none';
}

function setSizeMode(mode) {
  state.sizeMode = mode;
  document.getElementById('btn-indeg').classList.toggle('active', mode==='indeg');
  document.getElementById('btn-pr').classList.toggle('active', mode==='pagerank');
  var field = mode==='indeg' ? 'sizeIndeg' : 'sizePR';
  cy.nodes().forEach(function(n){ n.style('width',n.data(field)); n.style('height',n.data(field)); });
}

function setLayout(name) {
  var opts = {name:name, animate:true, animationDuration:500, fit:true, padding:40};
  if (name==='concentric') {
    opts.concentric  = function(ele){ return ele.data('indeg'); };
    opts.levelWidth  = function(nodes){ return Math.max(1, Math.ceil(nodes.length/8)); };
    opts.minNodeSpacing = 20;
  }
  if (name==='cose') { opts.animate=false; opts.randomize=false; opts.numIter=1000; }
  cy.layout(opts).run();
}

function openPanel(idx) {
  var d = CY_NODES[idx].data;
  document.getElementById('panel-name').textContent = d.name;
  document.getElementById('panel-rows').innerHTML = [
    ['Institution', d.inst||'—'],['Country', d.country||'—'],
    ['Subclass', d.subclass||'—'],
    ['Cited by (in-degree)', d.indeg],['Cites (out-degree)', d.outdeg],
    ['Papers in corpus', d.papers],
    ['Total citations (OpenAlex)', (d.citations||0).toLocaleString()],
    ['Funders', d.funders ? d.funders.replace(/\|/g,', ') : '—'],
  ].map(function(r){
    return '<div class="pr"><span class="pk">'+r[0]+'</span><span class="pv">'+r[1]+'</span></div>';
  }).join('');
  function fillList(adj, listId) {
    var ul = document.getElementById(listId);
    ul.innerHTML = adj.length
      ? adj.slice(0,12).map(function(x){
          var n=CY_NODES[x[0]].data;
          return '<li data-i="'+x[0]+'"><span class="cn">'+n.name+'</span>'
               + '<span class="cc">'+x[1]+(x[1]>1?' papers':' paper')+'</span></li>';
        }).join('')
      : '<li class="empty">None within this view</li>';
    ul.querySelectorAll('li[data-i]').forEach(function(el){
      el.addEventListener('click',function(){ selectNode(cy.getElementById(String(+el.dataset.i))); });
    });
  }
  fillList(IN_ADJ[idx],  'list-cited-by');
  fillList(OUT_ADJ[idx], 'list-cites');
  document.getElementById('side-panel').style.display='flex';
}

function openEdgePopup(edge) {
  var d = edge.data();
  var sn = cy.getElementById(d.source).data('name');
  var tn = cy.getElementById(d.target).data('name');
  document.getElementById('ep-title').textContent = sn + ' → ' + tn;
  document.getElementById('ep-count').textContent =
    d.weight+' citing paper'+(d.weight!==1?'s':'')+' within this corpus';
  var pps = d.papers || [];
  document.getElementById('ep-list').innerHTML = pps.length
    ? pps.map(function(t){
        return '<li>'+String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</li>';
      }).join('')
    : '<li style="color:#aaa;font-style:italic">Paper titles not yet available — re-run with --fetch to embed them</li>';
  document.getElementById('edge-popup').style.display='flex';
}

function runPath() {
  if (state.pathFrom<0 || state.pathTo<0) {
    document.getElementById('path-result').textContent='Select both From and To authors first';
    return;
  }
  clearPath();
  cy.elements().removeClass('ego neighbor faded hidden');
  var res = cy.elements().aStar({
    root: cy.getElementById(String(state.pathFrom)),
    goal: cy.getElementById(String(state.pathTo)),
    directed: false,
  });
  if (res.found) {
    res.path.addClass('path-node path-edge');
    document.getElementById('path-result').textContent =
      res.path.nodes().map(function(n){ return n.data('name'); }).join(' → ');
    cy.elements().not(res.path).addClass('faded');
    document.getElementById('reset-btn').style.display='inline-block';
  } else {
    document.getElementById('path-result').textContent='No path found between these authors';
  }
}

function clearPath() {
  cy.elements().removeClass('path-node path-edge faded hidden');
  document.getElementById('path-result').textContent='';
}

function downloadCSV() {
  var lines=['author,institution,country,in_degree,out_degree,papers,citations'];
  function esc(s){s=String(s==null?'':s);return(s.indexOf(',')>=0||s.indexOf('"')>=0)?'"'+s.replace(/"/g,'""')+'"':s;}
  cy.nodes().filter(function(n){return n.style('display')!=='none'&&parseFloat(n.style('opacity'))>0.1;})
    .forEach(function(n){
      var d=n.data();
      lines.push([d.name,d.inst,d.country,d.indeg,d.outdeg,d.papers,d.citations].map(esc).join(','));
    });
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([lines.join('\n')],{type:'text/csv'}));
  a.download='citation_network_export.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

function makeDd(inId, ddId, onSelect) {
  var inp=document.getElementById(inId), dd=document.getElementById(ddId);
  var sorted=NODE_NAMES.map(function(n,i){return{i:i,name:n,inst:NODE_INST[i]||''};})
    .sort(function(a,b){return a.name.localeCompare(b.name);});
  function show(q) {
    var ql=q.toLowerCase();
    var hits=sorted.filter(function(x){return x.name.toLowerCase().indexOf(ql)>=0;}).slice(0,18);
    if(!hits.length){dd.style.display='none';return;}
    dd.innerHTML=hits.map(function(h){
      return '<div class="dd-item" data-i="'+h.i+'"><div>'+h.name+'</div>'
           + '<div class="dd-sub">'+(h.inst||'')+'</div></div>';
    }).join('');
    dd.querySelectorAll('.dd-item').forEach(function(el){
      el.addEventListener('mousedown',function(e){
        e.preventDefault(); inp.value=NODE_NAMES[+el.dataset.i];
        onSelect(+el.dataset.i); dd.style.display='none';
      });
    });
    dd.style.display='block';
  }
  inp.addEventListener('input',function(){var q=inp.value.trim();if(q.length>=2)show(q);else dd.style.display='none';});
  inp.addEventListener('blur', function(){setTimeout(function(){dd.style.display='none';},160);});
  inp.addEventListener('focus',function(){if(inp.value.trim().length>=2)show(inp.value.trim());});
}

var FUNDER_NAMES = (function() {
  var seen = new Set(), list = [];
  CY_NODES.forEach(function(n) {
    (n.data.funders || '').split('|').forEach(function(f) {
      f = f.trim();
      if (f && !seen.has(f)) { seen.add(f); list.push(f); }
    });
  });
  return list.sort();
})();

function makeFunderDd() {
  var inp = document.getElementById('funder-in');
  var dd  = document.getElementById('funder-dd');
  function show(q) {
    var ql = q.toLowerCase();
    var hits = FUNDER_NAMES.filter(function(f){ return f.toLowerCase().indexOf(ql)>=0; }).slice(0,16);
    if (!hits.length) { dd.style.display='none'; return; }
    dd.innerHTML = hits.map(function(f){
      return '<div class="dd-item" data-f="'+f.replace(/"/g,'&quot;')+'">'+f+'</div>';
    }).join('');
    dd.querySelectorAll('.dd-item').forEach(function(el){
      el.addEventListener('mousedown',function(e){
        e.preventDefault(); inp.value=el.dataset.f; dd.style.display='none'; highlightFunder();
      });
    });
    dd.style.display='block';
  }
  inp.addEventListener('input', function(){ var q=inp.value.trim(); if(q.length>=2)show(q); else dd.style.display='none'; });
  inp.addEventListener('blur',  function(){ setTimeout(function(){ dd.style.display='none'; },160); });
  inp.addEventListener('focus', function(){ if(inp.value.trim().length>=2)show(inp.value.trim()); });
}

function makeInfluenceDd() {
  var inp = document.getElementById('inf-in');
  var dd  = document.getElementById('inf-dd');
  var authors = NODE_NAMES.map(function(n,i){ return {label:n, sub:NODE_INST[i]||'', type:'author'}; });
  var funders = FUNDER_NAMES.map(function(f){ return {label:f, sub:'', type:'funder'}; });
  var combined = authors.concat(funders).sort(function(a,b){ return a.label.localeCompare(b.label); });
  function show(q) {
    var ql = q.toLowerCase();
    var hits = combined.filter(function(x){ return x.label.toLowerCase().indexOf(ql)>=0; }).slice(0,20);
    if (!hits.length) { dd.style.display='none'; return; }
    dd.innerHTML = hits.map(function(h){
      var badge = h.type==='funder'
        ? '<span style="font-size:.6rem;background:#ede9fe;color:#5b21b6;padding:1px 5px;border-radius:10px;margin-left:4px">funder</span>'
        : '';
      return '<div class="dd-item" data-lbl="'+h.label.replace(/"/g,'&quot;')+'"><div>'+h.label+badge+'</div>'
           + (h.sub?'<div class="dd-sub">'+h.sub+'</div>':'')+'</div>';
    }).join('');
    dd.querySelectorAll('.dd-item').forEach(function(el){
      el.addEventListener('mousedown',function(e){
        e.preventDefault(); inp.value=el.dataset.lbl; dd.style.display='none'; exploreInfluence();
      });
    });
    dd.style.display='block';
  }
  inp.addEventListener('input', function(){ var q=inp.value.trim(); if(q.length>=2)show(q); else dd.style.display='none'; });
  inp.addEventListener('blur',  function(){ setTimeout(function(){ dd.style.display='none'; },160); });
  inp.addEventListener('focus', function(){ if(inp.value.trim().length>=2)show(inp.value.trim()); });
}

document.addEventListener('DOMContentLoaded', function() {
  initCy();
  makeDd('srch-in',  'srch-dd1', function(i){ selectNode(cy.getElementById(String(i))); });
  makeDd('srch-in2', 'srch-dd2', function(i){ state.pathFrom=i; });
  makeDd('srch-in3', 'srch-dd3', function(i){ state.pathTo=i; });
  makeFunderDd();
  makeInfluenceDd();
  document.getElementById('close-panel').addEventListener('click',function(){
    document.getElementById('side-panel').style.display='none';
  });
  document.getElementById('close-ep').addEventListener('click',function(){
    document.getElementById('edge-popup').style.display='none';
  });
});
"""

    node_slider_html = f"""
  <div class="cg">
    <label>Max nodes</label>
    <div class="yr-wrap">
      <input id="node-limit-slider" type="range"
             min="50" max="{n_nodes}" step="50" value="{min(DEFAULT_DISPLAY, n_nodes)}"
             oninput="applyNodeLimit(+this.value)" style="width:120px"/>
      <span id="node-limit-label" style="font-size:.72rem;color:#555">Showing {min(DEFAULT_DISPLAY, n_nodes)} of {n_nodes} authors</span>
    </div>
  </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_TITLE} — Citation Network Explorer</title>
<script src="{CYTOSCAPE_CDN}"></script>
<style>
{css}
</style>
</head>
<body>
<div id="hdr">
  <div>
    <h1>{_TITLE} — Citation Network Explorer</h1>
    <p>{n_nodes:,} authors &middot; {n_edges:,} citation links &middot; {n_recip:,} mutual pairs &middot; {n_communities} research clusters &middot; Generated {generated_date}</p>
    <span style="display:none">hippo</span>
  </div>
  <div class="spacer"></div>
  <a href="{_pf('citation_network.html')}">&#8592; Overview &amp; charts</a>
</div>
<div id="ctrl">
  <div class="cg">
    <label>Find author</label>
    <div class="srch-wrap">
      <input id="srch-in" class="srch-in" type="text" placeholder="Type name…" autocomplete="off" />
      <div class="dd" id="srch-dd1"></div>
    </div>
  </div>
  <div class="sep"></div>
  <div class="cg">
    <label>Path — from</label>
    <div class="srch-wrap">
      <input id="srch-in2" class="srch-in" type="text" placeholder="Author A…" autocomplete="off" />
      <div class="dd" id="srch-dd2"></div>
    </div>
  </div>
  <div class="cg">
    <label>to</label>
    <div class="srch-wrap">
      <input id="srch-in3" class="srch-in" type="text" placeholder="Author B…" autocomplete="off" />
      <div class="dd" id="srch-dd3"></div>
    </div>
  </div>
  <button class="path-btn" onclick="runPath()">&#8594; Find path</button>
  <span id="path-result"></span>
  <div class="sep"></div>
  <div class="cg">
    <label>Highlight funder</label>
    <div style="display:flex;gap:4px;align-items:center">
      <div style="position:relative">
        <input id="funder-in" class="srch-in" type="text" placeholder="e.g. NIH, MRC, ERC, EU…"
               autocomplete="off" oninput="highlightFunder()" style="width:150px" />
        <div class="dd" id="funder-dd"></div>
      </div>
      <button class="tog" onclick="clearFunderHighlight()" title="Clear">&#x2715;</button>
    </div>
    <span id="funder-badge" style="font-size:.68rem;color:#e67e22;margin-top:2px"></span>
  </div>
  <div class="sep"></div>
  <div class="cg">
    <label>Influence explorer</label>
    <div style="display:flex;gap:4px;align-items:center">
      <div style="position:relative">
        <input id="inf-in" class="srch-in" type="text" placeholder="Author or funder…"
               autocomplete="off" oninput="exploreInfluence()" style="width:150px" />
        <div class="dd" id="inf-dd"></div>
      </div>
      <button class="tog" onclick="clearInfluence()" title="Clear">&#x2715;</button>
    </div>
    <div style="display:flex;gap:4px;margin-top:4px;align-items:center;flex-wrap:wrap">
      <span style="font-size:.68rem;color:#888;white-space:nowrap">Direction:</span>
      <div class="btn-grp">
        <button id="inf-dir-down" class="tog active" onclick="setInfluenceDir('down')"
                title="Downstream: find papers that CITE the seed — the seed influenced these">seed &#8594; field</button>
        <button id="inf-dir-up"   class="tog"        onclick="setInfluenceDir('up')"
                title="Upstream: find papers the seed CITES — these influenced the seed">field &#8594; seed</button>
      </div>
    </div>
    <div style="display:flex;gap:4px;margin-top:4px;align-items:center">
      <span style="font-size:.68rem;color:#888">Depth:</span>
      <div class="btn-grp">
        <button id="inf-d0" class="tog"        onclick="setInfluenceDepth(0)" title="Seed only — no expansion">0</button>
        <button id="inf-d1" class="tog active" onclick="setInfluenceDepth(1)">1</button>
        <button id="inf-d2" class="tog"        onclick="setInfluenceDepth(2)">2</button>
        <button id="inf-d3" class="tog"        onclick="setInfluenceDepth(3)">3</button>
      </div>
    </div>
    <div style="display:flex;gap:3px;align-items:center;margin-top:4px;flex-wrap:wrap">
      <span style="width:9px;height:9px;border-radius:50%;background:#c0392b;display:inline-block"></span><span style="font-size:.63rem;color:#888;margin-right:6px">seed</span>
      <span style="font-size:.63rem;color:#888;margin-right:2px">↓</span>
      <span style="width:9px;height:9px;border-radius:50%;background:#8e44ad;display:inline-block"></span>
      <span style="width:9px;height:9px;border-radius:50%;background:#bb8fce;display:inline-block"></span>
      <span style="width:9px;height:9px;border-radius:50%;background:#d7bde2;display:inline-block;margin-right:6px"></span>
      <span style="font-size:.63rem;color:#888;margin-right:2px">↑</span>
      <span style="width:9px;height:9px;border-radius:50%;background:#0d9488;display:inline-block"></span>
      <span style="width:9px;height:9px;border-radius:50%;background:#5eead4;display:inline-block"></span>
      <span style="width:9px;height:9px;border-radius:50%;background:#99f6e4;display:inline-block"></span>
    </div>
    <span id="inf-badge" style="font-size:.68rem;color:#8e44ad;margin-top:2px"></span>
  </div>
  <div class="sep"></div>
  <div class="cg">
    <label>Colour by</label>
    <select onchange="setColorMode(this.value)">
      <option value="comm">Research cluster</option>
      <option value="country">Country</option>
      <option value="papers">Publication volume</option>
      <option value="subclass">Subclass</option>
    </select>
    {shape_legend_html}
  </div>
  <div class="cg">
    <label>Node size</label>
    <div class="btn-grp">
      <button id="btn-indeg" class="tog active" onclick="setSizeMode('indeg')">In-degree</button>
      <button id="btn-pr"    class="tog"        onclick="setSizeMode('pagerank')">PageRank</button>
    </div>
  </div>
  <div class="cg">
    <label>Layout</label>
    <select onchange="setLayout(this.value)">
      <option value="preset">Spring (default)</option>
      <option value="concentric">Concentric</option>
      <option value="circle">Circle</option>
      <option value="grid">Grid</option>
      <option value="cose">Force-directed</option>
    </select>
  </div>
  <div class="sep"></div>
  {node_slider_html}
  <div class="sep"></div>
  <label class="nb-lbl"><input type="checkbox" id="nb-only" onchange="toggleNeighborsOnly()" />&nbsp;Neighbours only</label>
  <button class="reset-btn" id="reset-btn" onclick="resetAll()">&#8857; Reset</button>
  <button class="dl-btn" onclick="downloadCSV()">&#8595; CSV</button>
</div>
<div id="workspace">
  <div id="cy"></div>
  <div id="side-panel">
    <div id="panel-hdr">
      <h3 id="panel-name"></h3>
      <button id="close-panel">&#x2715;</button>
    </div>
    <div id="panel-body">
      <div id="panel-rows"></div>
      <h4>Cited by (within corpus)</h4>
      <ul class="cit-list" id="list-cited-by"></ul>
      <h4>Cites (within corpus)</h4>
      <ul class="cit-list" id="list-cites"></ul>
    </div>
  </div>
  <div id="edge-popup">
    <div id="ep-hdr">
      <span id="ep-title"></span>
      <button id="close-ep">&#x2715;</button>
    </div>
    <div id="ep-body">
      <p id="ep-count"></p>
      <ul id="ep-list"></ul>
    </div>
  </div>
</div>
<script>
{js_vars}
</script>
<script>
{js_code}
</script>
</body>
</html>
"""

    cy_file = os.path.join(out, _pf("citation_network_cytoscape.html"))
    with open(cy_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Written: {cy_file}  ({os.path.getsize(cy_file) // 1024} KB)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=os.path.join(OUTPUT_DIR, _PREFIX) if _PREFIX else OUTPUT_DIR)
    parser.add_argument("--data-dir", default=os.path.join("data", _PREFIX) if _PREFIX else "data")
    parser.add_argument("--min-papers",    type=int, default=3,  help="Include authors with at least this many papers")
    parser.add_argument("--min-citations", type=int, default=10, help="Include authors with at least this many citations (catches unicorns)")
    args = parser.parse_args()
    out  = args.output_dir
    data = args.data_dir

    cite_path    = os.path.join(data, _pf("citation_edges_author.csv"))
    author_path  = os.path.join(data, _pf("papers_by_author.csv"))
    by_year_path = os.path.join(data, _pf("citation_edges_author_by_year.csv"))

    print("Loading data…")
    cite_df   = pd.read_csv(cite_path)
    author_df = pd.read_csv(author_path)

    has_year_data = os.path.exists(by_year_path)
    year_df = None  # loaded later, after node_set is known

    # Load dominant subclass per author (optional — requires --fetch with subclasses config)
    sc_path = os.path.join(data, _pf("papers_by_author_subclass.csv"))
    author_subclass: dict = {}
    if os.path.exists(sc_path):
        sc_df = pd.read_csv(sc_path)
        for aid, grp in sc_df.groupby("author_id"):
            top_row = grp.loc[grp["papers"].idxmax()]
            author_subclass[aid] = top_row["subclass"]
        print(f"  Subclass data: {len(author_subclass)} authors with dominant subclass")

    # Load funder data per author (optional)
    funders_path = os.path.join(data, _pf("funders_by_author.csv"))
    author_funders: dict = {}
    if os.path.exists(funders_path):
        fdf = pd.read_csv(funders_path)
        for _, row in fdf.iterrows():
            aid = row.get("author_id")
            fs  = str(row.get("funders") or "")
            if aid and fs and fs != "nan":
                author_funders[aid] = fs
        print(f"  Funder data: {len(author_funders)} authors with funding information")

    papers_path = os.path.join(data, _pf("citation_edges_author_papers.csv"))
    edge_papers_map: dict = {}  # populated later, after node_set is known

    # ── Author lookup + pre-filter (vectorised) ───────────────────────────────
    adf = author_df.dropna(subset=["author_id"]).copy()
    adf["papers"]    = pd.to_numeric(adf.get("papers",    0), errors="coerce").fillna(0).astype(int)
    adf["citations"] = pd.to_numeric(adf.get("citations", 0), errors="coerce").fillna(0).astype(int)
    author_lookup = {
        row["author_id"]: {
            "author_name": str(row.get("author_name") or ""),
            "institution": str(row.get("institution") or ""),
            "country":     str(row.get("country") or ""),
            "papers":      row["papers"],
            "citations":   row["citations"],
        }
        for _, row in adf.iterrows()
    }
    keep_mask = (adf["papers"] >= args.min_papers) | (adf["citations"] >= args.min_citations)
    keep_set  = set(adf.loc[keep_mask, "author_id"])
    print(f"  Pre-filter: {len(keep_set):,} authors with ≥{args.min_papers} papers or ≥{args.min_citations} citations")

    # ── Build directed graph (vectorised) ────────────────────────────────────
    print("Building directed citation graph…")
    cdf = cite_df.dropna(subset=["citing_author_id", "cited_author_id"]).copy()
    cdf = cdf[cdf["citing_author_id"].isin(keep_set) & cdf["cited_author_id"].isin(keep_set)]
    cdf["citations"] = pd.to_numeric(cdf.get("citations", 1), errors="coerce").fillna(1).astype(int)
    edge_weights = cdf.groupby(["citing_author_id", "cited_author_id"])["citations"].sum()
    G = nx.DiGraph()
    for (ca, cd), w in edge_weights.items():
        G.add_edge(ca, cd, weight=int(w))

    print(f"  Full graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    G_plot = G.copy()
    indegree_all = dict(G_plot.in_degree())
    print(f"  Full graph: {G_plot.number_of_nodes()} nodes, {G_plot.number_of_edges()} edges")

    # Safety cap
    if G_plot.number_of_nodes() > TOP_NODES:
        print(f"  Applying safety cap at {TOP_NODES} nodes…")
        top_nodes = sorted(indegree_all, key=lambda n: indegree_all[n], reverse=True)[:TOP_NODES]
        G_plot = G_plot.subgraph(top_nodes).copy()
        indegree_all = dict(G_plot.in_degree())
    node_set = set(G_plot.nodes())

    if has_year_data:
        print("  Loading year-annotated edges (chunked, filtered to plot nodes)…")
        chunks = []
        for chunk in pd.read_csv(by_year_path, chunksize=500_000):
            chunks.append(chunk[chunk["citing_author_id"].isin(node_set) & chunk["cited_author_id"].isin(node_set)])
        year_df = pd.concat(chunks, ignore_index=True) if chunks else None
        print(f"  Year-annotated edges after filter: {len(year_df):,} rows")
    else:
        print("  No by-year edge file found; year filter disabled (re-run --fetch to generate)")

    if os.path.exists(papers_path):
        print("  Loading edge-papers data (chunked, filtered to plot nodes)…")
        for chunk in pd.read_csv(papers_path, chunksize=500_000):
            chunk = chunk[chunk["citing_author_id"].isin(node_set) & chunk["cited_author_id"].isin(node_set)]
            chunk = chunk.dropna(subset=["citing_author_id", "cited_author_id", "citing_titles"])
            for ca, cd, ts in zip(chunk["citing_author_id"], chunk["cited_author_id"], chunk["citing_titles"].astype(str)):
                if ts and ts != "nan":
                    edge_papers_map[(ca, cd)] = [t.strip() for t in ts.split(";") if t.strip()]
        print(f"  Edge-papers after filter: {len(edge_papers_map):,} edges with title data")

    G_undirected = G_plot.to_undirected()

    print("Detecting communities…")
    partition     = _community_partition(G_undirected)
    n_communities = len(set(partition.values()))

    print("Computing layout…")
    pos = nx.spring_layout(G_undirected, weight="weight", seed=LAYOUT_SEED, k=0.8)

    indegree_map = dict(G_plot.in_degree())

    # ── PageRank ──────────────────────────────────────────────────────────────
    print("Computing PageRank…")
    pagerank = nx.pagerank(G_plot, weight="weight")
    pr_vals  = list(pagerank.values())
    pr_min, pr_max = min(pr_vals), max(pr_vals)
    def _pr_size(pr):
        if pr_max == pr_min:
            return 10
        return round(5 + 17 * (pr - pr_min) / (pr_max - pr_min), 3)

    # ── Betweenness centrality ────────────────────────────────────────────────
    print("Computing betweenness centrality…")
    betweenness = nx.betweenness_centrality(G_plot, normalized=True, weight="weight")

    # ── Export author centrality CSV ──────────────────────────────────────────
    print("Exporting author centrality CSV…")
    indegree_dict  = dict(G_plot.in_degree())
    outdegree_dict = dict(G_plot.out_degree())
    centrality_rows = []
    for nid in G_plot.nodes():
        meta = author_lookup.get(nid, {})
        centrality_rows.append({
            "author_id":   nid,
            "author_name": meta.get("author_name", ""),
            "pagerank":    pagerank.get(nid, 0.0),
            "betweenness": betweenness.get(nid, 0.0),
            "indegree":    indegree_dict.get(nid, 0),
            "outdegree":   outdegree_dict.get(nid, 0),
        })
    cent_df = pd.DataFrame(centrality_rows).sort_values("pagerank", ascending=False)
    cent_path = os.path.join(data, _pf("author_centrality.csv"))
    cent_df.to_csv(cent_path, index=False)
    print(f"Saved → {cent_path}  ({len(cent_df):,} authors)")

    # ── Node-level data for JS ────────────────────────────────────────────────
    # node_ids order comes from fig_citation_network; we compute it here first
    # to keep all JS arrays in sync.
    plotly_top = sorted(indegree_all, key=lambda n: indegree_all[n], reverse=True)[:PLOTLY_NODES]
    G_plotly = G_plot.subgraph(plotly_top) if len(plotly_top) < G_plot.number_of_nodes() else G_plot
    f_net, node_ids, node_names_list = fig_citation_network(
        G_plotly, author_lookup, partition, pos
    )

    sizes_indeg = [max(5, min(22, 5 + indegree_map.get(nid, 0) * 0.6)) for nid in node_ids]
    sizes_pr    = [_pr_size(pagerank.get(nid, pr_min)) for nid in node_ids]

    node_meta = [
        {
            "name":    author_lookup.get(nid, {}).get("author_name") or "",
            "inst":    author_lookup.get(nid, {}).get("institution") or "",
            "country": author_lookup.get(nid, {}).get("country") or "",
            "indeg":   indegree_map.get(nid, 0),
            "outdeg":  G_plot.out_degree(nid),
        }
        for nid in node_ids
    ]

    node_id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
    adj_list = [
        [node_id_to_idx[n] for n in G_undirected.neighbors(nid) if n in node_id_to_idx]
        for nid in node_ids
    ]

    # ── Edge data for JS (positions + year sets + reciprocal flag) ────────────
    recip_set = {(u, v) for u, v in G_plot.edges() if G_plot.has_edge(v, u)}
    edge_years_map: dict = defaultdict(set)
    if year_df is not None:
        for _, row in year_df.iterrows():
            ca = row.get("citing_author_id")
            cd = row.get("cited_author_id")
            yr = row.get("year")
            if ca and cd and yr and ca in node_set and cd in node_set:
                edge_years_map[(ca, cd)].add(int(yr))

    edge_data_list = []
    for u, v in G_plot.edges():
        xu, yu = pos[u]; xv, yv = pos[v]
        edge_data_list.append({
            "x0": round(float(xu), 5), "y0": round(float(yu), 5),
            "x1": round(float(xv), 5), "y1": round(float(yv), 5),
            "recip": (u, v) in recip_set,
            "years": sorted(edge_years_map.get((u, v), set())),
        })

    all_edge_years = sorted({y for ys in edge_years_map.values() for y in ys})
    year_min = min(all_edge_years) if all_edge_years else 2000
    year_max = max(all_edge_years) if all_edge_years else 2024

    # ── Figures ───────────────────────────────────────────────────────────────
    print("Building figures…")
    f_bar = fig_top_cited(author_lookup, indegree_map)
    f_btw = fig_betweenness(author_lookup, betweenness)

    net_div = f_net.to_html(
        full_html=False, include_plotlyjs=False,
        config={"responsive": True}, div_id="net-plot",
    )
    bar_div = f_bar.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True})
    btw_div = f_btw.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True})

    # ── JS data payloads ──────────────────────────────────────────────────────
    node_names_js  = _json.dumps(node_names_list)
    node_meta_js   = _json.dumps(node_meta)
    adj_js         = _json.dumps(adj_list)
    sizes_indeg_js = _json.dumps(sizes_indeg)
    sizes_pr_js    = _json.dumps(sizes_pr)
    edge_data_js   = _json.dumps(edge_data_list)

    n_nodes = G_plot.number_of_nodes()
    n_edges = G_plot.number_of_edges()
    n_recip = len(recip_set) // 2  # pairs
    generated_date = pd.Timestamp.now().strftime("%B %Y")

    year_filter_html = ""
    if has_year_data and all_edge_years:
        year_filter_html = f"""
  <div class="ctrl-group">
    <label class="ctrl-label">Citing year range</label>
    <div class="yr-wrap">
      <span id="yr-from-val">{year_min}</span>
      <input id="yr-from" type="range" min="{year_min}" max="{year_max}" value="{year_min}"
             oninput="syncYearFilter()" />
      <input id="yr-to"   type="range" min="{year_min}" max="{year_max}" value="{year_max}"
             oninput="syncYearFilter()" />
      <span id="yr-to-val">{year_max}</span>
      <button class="tog" onclick="resetYearFilter()">Reset</button>
    </div>
  </div>"""
    elif not has_year_data:
        year_filter_html = (
            '<div class="ctrl-group">'
            '<span class="ctrl-note">Year filter: re-run with <code>--fetch</code> to enable</span>'
            "</div>"
        )

    # JS variable declarations use f-string; function bodies use raw string
    js_vars = f"""
var NODE_NAMES       = {node_names_js};
var NODE_META        = {node_meta_js};
var NODE_ADJ         = {adj_js};
var NODE_SIZES_INDEG = {sizes_indeg_js};
var NODE_SIZES_PR    = {sizes_pr_js};
var EDGE_DATA        = {edge_data_js};
var YEAR_MIN = {year_min};
var YEAR_MAX = {year_max};
var HAS_YEAR_DATA = {'true' if has_year_data and all_edge_years else 'false'};
var TRACE_EDGE_NORMAL = 0;
var TRACE_EDGE_RECIP  = 1;
var TRACE_NODE        = 2;
"""

    # Raw string — real JS braces, no Python escaping needed
    js_code = r"""
var state = {search: '', egoIdx: -1, sizeMetric: 'indeg', yearFrom: null, yearTo: null, zoomFactor: 1.0};
var _initXRange = null;

function computeOpacities() {
    var q = state.search.toLowerCase();
    var ego = state.egoIdx;
    var neighbors = null;
    if (ego >= 0) { neighbors = new Set([ego].concat(NODE_ADJ[ego])); }
    return NODE_NAMES.map(function(name, i) {
        if (ego >= 0)  return neighbors.has(i) ? 1.0 : 0.06;
        if (q)         return name.toLowerCase().indexOf(q) >= 0 ? 1.0 : 0.06;
        return 1.0;
    });
}

function computeEdgeTraces() {
    var yfrom = state.yearFrom, yto = state.yearTo;
    var nx = [], ny = [], rx = [], ry = [];
    EDGE_DATA.forEach(function(e) {
        var ok = true;
        if (HAS_YEAR_DATA && e.years && e.years.length > 0) {
            ok = e.years.some(function(y) { return y >= yfrom && y <= yto; });
        }
        if (!ok) return;
        if (e.recip) { rx.push(e.x0, e.x1, null); ry.push(e.y0, e.y1, null); }
        else         { nx.push(e.x0, e.x1, null); ny.push(e.y0, e.y1, null); }
    });
    return {nx: nx, ny: ny, rx: rx, ry: ry};
}

function render() {
    var d  = document.getElementById('net-plot');
    var op = computeOpacities();
    var baseSz = state.sizeMetric === 'pagerank' ? NODE_SIZES_PR : NODE_SIZES_INDEG;
    var zf = state.zoomFactor;
    var sz = zf === 1.0 ? baseSz : baseSz.map(function(s) { return Math.max(2, s * zf); });
    var ed = computeEdgeTraces();
    Plotly.restyle(d, {'marker.opacity': [op], 'marker.size': [sz]}, [TRACE_NODE]);
    Plotly.restyle(d, {x: [ed.nx], y: [ed.ny]}, [TRACE_EDGE_NORMAL]);
    Plotly.restyle(d, {x: [ed.rx], y: [ed.ry]}, [TRACE_EDGE_RECIP]);
}

function doSearch() {
    state.search = document.getElementById('search-box').value;
    state.egoIdx = -1;
    render();
}

function egoNetwork(idx) {
    state.egoIdx = idx;
    state.search = '';
    document.getElementById('search-box').value = '';
    document.getElementById('reset-btn').style.display = 'inline-flex';
    var name = NODE_META[idx] ? NODE_META[idx].name : '';
    document.getElementById('ego-label').textContent = name ? 'Showing: ' + name : '';
    render();
}

function resetView() {
    state.egoIdx = -1; state.search = '';
    document.getElementById('search-box').value = '';
    document.getElementById('reset-btn').style.display = 'none';
    document.getElementById('ego-label').textContent = '';
    render();
}

function setMetric(metric) {
    state.sizeMetric = metric;
    document.getElementById('btn-indeg').classList.toggle('active', metric === 'indeg');
    document.getElementById('btn-pr').classList.toggle('active', metric === 'pagerank');
    document.getElementById('metric-label').textContent =
        metric === 'pagerank' ? 'PageRank' : 'in-degree';
    render();
}

function syncYearFilter() {
    var yfrom = parseInt(document.getElementById('yr-from').value);
    var yto   = parseInt(document.getElementById('yr-to').value);
    if (yfrom > yto) { yfrom = yto; document.getElementById('yr-from').value = yfrom; }
    state.yearFrom = yfrom; state.yearTo = yto;
    document.getElementById('yr-from-val').textContent = yfrom;
    document.getElementById('yr-to-val').textContent   = yto;
    render();
}

function resetYearFilter() {
    state.yearFrom = YEAR_MIN; state.yearTo = YEAR_MAX;
    document.getElementById('yr-from').value = YEAR_MIN;
    document.getElementById('yr-to').value   = YEAR_MAX;
    document.getElementById('yr-from-val').textContent = YEAR_MIN;
    document.getElementById('yr-to-val').textContent   = YEAR_MAX;
    render();
}

function downloadCSV() {
    var op = computeOpacities();
    var lines = ['author,institution,country,in_degree,out_degree'];
    function esc(s) {
        s = s == null ? '' : String(s);
        return (s.indexOf(',') >= 0 || s.indexOf('"') >= 0)
            ? '"' + s.replace(/"/g, '""') + '"' : s;
    }
    NODE_META.forEach(function(m, i) {
        if (op[i] > 0.5)
            lines.push([m.name, m.inst, m.country, m.indeg, m.outdeg].map(esc).join(','));
    });
    var blob = new Blob([lines.join('\n')], {type: 'text/csv'});
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'citation_network_export.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

// ── Init ──────────────────────────────────────────────────────────────────────
state.yearFrom = YEAR_MIN; state.yearTo = YEAR_MAX;
document.getElementById('net-plot').on('plotly_click', function(data) {
    if (!data || !data.points || !data.points.length) return;
    var pt = data.points[0];
    if (pt.curveNumber === TRACE_NODE) egoNetwork(pt.pointIndex);
});
document.getElementById('net-plot').on('plotly_afterplot', function() {
    if (_initXRange) return;
    var d = document.getElementById('net-plot');
    if (d.layout && d.layout.xaxis && d.layout.xaxis.range) _initXRange = d.layout.xaxis.range.slice();
});
document.getElementById('net-plot').on('plotly_relayout', function(ev) {
    var xlo = ev['xaxis.range[0]'], xhi = ev['xaxis.range[1]'];
    if (xlo !== undefined && xhi !== undefined) {
        if (_initXRange) {
            var initSpan = _initXRange[1] - _initXRange[0];
            var currSpan = xhi - xlo;
            state.zoomFactor = (currSpan > 0 && initSpan > 0) ? initSpan / currSpan : 1.0;
            render();
        }
    } else if (ev['xaxis.autorange'] === true) {
        state.zoomFactor = 1.0;
        render();
    }
});
(function() {
    var authIn = document.getElementById('search-box');
    var authDd = document.getElementById('search-dd');
    var nameList = NODE_NAMES.map(function(n, i) {
        return {i: i, name: n, inst: NODE_META[i] ? NODE_META[i].inst : ''};
    }).sort(function(a, b) { return a.name.localeCompare(b.name); });
    function showDd(q) {
        var ql = q.toLowerCase();
        var hits = nameList.filter(function(x) { return x.name.toLowerCase().indexOf(ql) >= 0; }).slice(0, 18);
        if (!hits.length) { authDd.style.display = 'none'; return; }
        authDd.innerHTML = hits.map(function(h) {
            return '<div class="dd-item" data-i="' + h.i + '"><div>' + h.name + '</div>'
                 + '<div class="dd-sub">' + (h.inst || '') + '</div></div>';
        }).join('');
        authDd.querySelectorAll('.dd-item').forEach(function(el) {
            el.addEventListener('mousedown', function(e) {
                e.preventDefault();
                authIn.value = NODE_NAMES[+el.dataset.i];
                egoNetwork(+el.dataset.i);
                authDd.style.display = 'none';
            });
        });
        authDd.style.display = 'block';
    }
    authIn.addEventListener('input', function() {
        var q = authIn.value.trim();
        if (q.length >= 2) showDd(q); else authDd.style.display = 'none';
    });
    authIn.addEventListener('blur', function() { setTimeout(function() { authDd.style.display = 'none'; }, 160); });
    authIn.addEventListener('focus', function() { if (authIn.value.trim().length >= 2) showDd(authIn.value.trim()); });
})();
"""

    css = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
     background:#f5f6fa;color:#2c3e50;line-height:1.6}
#hdr{background:linear-gradient(135deg,#1a2742 0%,#2c4a8a 100%);
     color:#fff;padding:36px 32px 28px;text-align:center}
#hdr h1{font-size:1.7rem;font-weight:800;margin-bottom:8px}
#hdr p{opacity:.75;font-size:.95rem}
.stats-bar{display:flex;flex-wrap:wrap;gap:8px 24px;padding:10px 0 4px;font-size:.88rem;color:#555}
.stats-bar span b{color:#1a2742}
main{max-width:1100px;margin:0 auto;padding:32px 24px 80px}
h2.sec{font-size:.9rem;font-weight:700;color:#888;text-transform:uppercase;
       letter-spacing:1px;margin:40px 0 14px;padding-bottom:6px;border-bottom:2px solid #eee}
.fig-wrap{background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.07);
          padding:12px;margin-bottom:8px;overflow-x:auto}
.note{font-size:.84rem;color:#666;margin:8px 0 8px;line-height:1.5}
.controls{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-end;
          padding:8px 0 12px;border-bottom:1px solid #eee;margin-bottom:10px}
.ctrl-group{display:flex;flex-direction:column;gap:4px}
.ctrl-label{font-size:.78rem;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.4px}
.ctrl-note{font-size:.8rem;color:#aaa;font-style:italic}
.ctrl-note code{background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:.78rem}
.search-wrap{display:flex;align-items:center;gap:8px}
.search-wrap input{width:280px;padding:7px 12px;border:1px solid #ccc;border-radius:6px;
                   font-size:.9rem;outline:none}
.search-wrap input:focus{border-color:#2c4a8a;box-shadow:0 0 0 2px rgba(44,74,138,.15)}
.btn-group{display:flex;gap:0}
.tog{background:#eee;border:1px solid #ccc;padding:6px 14px;cursor:pointer;
     font-size:.83rem;color:#555;transition:background .15s}
.tog:first-child{border-radius:6px 0 0 6px}
.tog:last-child{border-radius:0 6px 6px 0}
.tog.active{background:#1a2742;color:#fff;border-color:#1a2742}
.tog:not(.active):hover{background:#ddd}
.yr-wrap{display:flex;align-items:center;gap:8px;font-size:.85rem}
.yr-wrap input[type=range]{width:100px}
.ego-label{font-size:.82rem;color:#2c4a8a;font-style:italic;min-width:120px}
.dl-btn{background:#2c4a8a;color:#fff;border:none;border-radius:6px;padding:7px 14px;
        cursor:pointer;font-size:.85rem}
.dl-btn:hover{background:#1a2742}
.reset-btn{background:#e74c3c;color:#fff;border:none;border-radius:6px;padding:6px 12px;
           cursor:pointer;font-size:.82rem;display:none;align-items:center;gap:4px}
.reset-btn:hover{background:#c0392b}
#srch-wrap{position:relative;display:inline-block}
#search-dd{position:absolute;top:100%;left:0;z-index:200;background:#fff;border:1px solid #c8ced5;
           border-top:none;border-radius:0 0 5px 5px;max-height:200px;overflow-y:auto;
           width:320px;box-shadow:0 6px 16px rgba(0,0,0,.12);display:none}
.dd-item{padding:6px 10px;cursor:pointer;font-size:.83rem;border-bottom:1px solid #f2f2f2}
.dd-item:hover{background:#f0f4ff}
.dd-sub{font-size:.72rem;color:#999;margin-top:1px}
footer{text-align:center;padding:24px;font-size:.78rem;color:#aaa}
a{color:inherit}"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_TITLE} — Citation Network</title>
<script src="{PLOTLY_CDN}" charset="utf-8"></script>
<style>
{css}
</style>
</head>
<body>
{DISCLAIMER}
<div id="hdr">
  <h1>{_TITLE} — Internal Citation Network</h1>
  <p>Who cites whom within this corpus — directed author-level citation graph and most-cited authors</p>
</div>
<main>

<h2 class="sec">Citation Network</h2>
<div class="fig-wrap">
  <div class="stats-bar">
    <span><b>{n_nodes:,}</b> authors</span>
    <span><b>{n_edges:,}</b> citation links</span>
    <span><b>{n_recip:,}</b> mutual citation pairs</span>
    <span><b>{n_communities}</b> research clusters</span>
  </div>
  <p class="note">
    Each node is an author; A → B means at least one paper by A cites a paper by B
    <em>within this corpus</em>. Orange edges = mutual citations (A cites B and B cites A).
    Node size = <span id="metric-label">in-degree</span> (how many other corpus authors cite this person).
    Labels show the {TOP_LABELS} most-cited names. <strong>Click any node</strong>
    to highlight its direct citation neighbourhood.
  </p>
  <div class="controls">
    <div class="ctrl-group">
      <label class="ctrl-label">Search author</label>
      <div class="search-wrap">
        <div id="srch-wrap">
          <input id="search-box" type="search" placeholder="Type author name…"
                 oninput="doSearch()" autocomplete="off" spellcheck="false" />
          <div id="search-dd"></div>
        </div>
        <button class="reset-btn" id="reset-btn" onclick="resetView()">⊙ Reset</button>
        <span class="ego-label" id="ego-label"></span>
      </div>
    </div>
    <div class="ctrl-group">
      <label class="ctrl-label">Node size metric</label>
      <div class="btn-group">
        <button id="btn-indeg" class="tog active" onclick="setMetric('indeg')">In-degree</button>
        <button id="btn-pr"    class="tog"        onclick="setMetric('pagerank')">PageRank</button>
      </div>
    </div>
    {year_filter_html}
    <div class="ctrl-group">
      <label class="ctrl-label">Export</label>
      <button class="dl-btn" onclick="downloadCSV()">↓ Visible nodes (CSV)</button>
    </div>
  </div>
  {net_div}
</div>

<h2 class="sec">Top {TOP_BAR} Most-Cited Authors (within corpus)</h2>
<div class="fig-wrap">
  {bar_div}
  <p class="note">
    In-degree counts how many distinct authors in this corpus have cited this author's work.
    This measures influence <em>within the field</em>; it differs from total citation count,
    which reflects all citing sources worldwide.
  </p>
</div>

<h2 class="sec">Top {TOP_BAR} Bridge Authors (Betweenness Centrality)</h2>
<div class="fig-wrap">
  {btw_div}
  <p class="note">
    Betweenness centrality measures how often an author lies on the shortest citation path
    between other authors. High betweenness = a <em>bridge</em> connecting otherwise separate
    research communities, even if not the most-cited person overall.
  </p>
</div>

  <p style="font-size:.82rem;color:#888;margin-top:24px">
    ← <a href="index.html" style="color:#2980b9">Back to overview</a>
  </p>
</main>
<footer>Data: <a href="https://openalex.org">OpenAlex</a> · Analysis by G. Kuhnle · Generated {generated_date}</footer>
<script>
{js_vars}
</script>
<script>
{js_code}
</script>
</body>
</html>
"""

    output_file = os.path.join(out, _pf("citation_network.html"))
    os.makedirs(out, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Written: {output_file}  ({os.path.getsize(output_file) // 1024} KB)")

    # ── Fullscreen standalone explorer ────────────────────────────────────────
    # Directed in/out adjacency for the side panel
    in_adj_list  = [[] for _ in node_ids]
    out_adj_list = [[] for _ in node_ids]
    for u, v in G_plot.edges():
        ui = node_id_to_idx[u]; vi = node_id_to_idx[v]
        w  = G_plot[u][v].get("weight", 1)
        out_adj_list[ui].append([vi, w])
        in_adj_list[vi].append([ui, w])
    for i in range(len(node_ids)):
        in_adj_list[i].sort(key=lambda x: -x[1])
        out_adj_list[i].sort(key=lambda x: -x[1])
    in_adj_js  = _json.dumps(in_adj_list, separators=(",", ":"))
    out_adj_js = _json.dumps(out_adj_list, separators=(",", ":"))

    # Extract Plotly figure data so the fullscreen can call Plotly.newPlot
    f_net_dict  = f_net.to_dict()
    traces_json = _json.dumps(f_net_dict["data"], separators=(",", ":"))
    layout_dict = {k: v for k, v in f_net_dict["layout"].items() if k != "height"}
    layout_dict["autosize"] = True
    layout_json = _json.dumps(layout_dict, separators=(",", ":"))

    year_filter_ctrl = ""
    if has_year_data and all_edge_years:
        year_filter_ctrl = f"""  <div class="cg">
    <label>Citing year</label>
    <div class="cg-row">
      <span id="yr-from-val">{year_min}</span>
      <input id="yr-from" type="range" min="{year_min}" max="{year_max}" value="{year_min}" oninput="syncYearFilter()" />
      <input id="yr-to"   type="range" min="{year_min}" max="{year_max}" value="{year_max}" oninput="syncYearFilter()" />
      <span id="yr-to-val">{year_max}</span>
      <button class="ctl-btn" onclick="resetYearFilter()">Reset</button>
    </div>
  </div>"""

    js_vars_full = f"""
var NODE_NAMES       = {node_names_js};
var NODE_META        = {node_meta_js};
var NODE_ADJ         = {adj_js};
var NODE_SIZES_INDEG = {sizes_indeg_js};
var NODE_SIZES_PR    = {sizes_pr_js};
var EDGE_DATA        = {edge_data_js};
var YEAR_MIN = {year_min};
var YEAR_MAX = {year_max};
var HAS_YEAR_DATA = {'true' if has_year_data and all_edge_years else 'false'};
var TRACE_EDGE_NORMAL = 0;
var TRACE_EDGE_RECIP  = 1;
var TRACE_NODE        = 2;
var IN_ADJ   = {in_adj_js};
var OUT_ADJ  = {out_adj_js};
var TRACES   = {traces_json};
var LAYOUT   = {layout_json};
"""

    js_code_full = r"""
var state = {search: '', egoIdx: -1, sizeMetric: 'indeg', yearFrom: null, yearTo: null, zoomFactor: 1.0};
var _initXRange = null;

function computeOpacities() {
    var q = state.search.toLowerCase(), ego = state.egoIdx, neighbors = null;
    if (ego >= 0) { neighbors = new Set([ego].concat(NODE_ADJ[ego])); }
    return NODE_NAMES.map(function(name, i) {
        if (ego >= 0)  return neighbors.has(i) ? 1.0 : 0.06;
        if (q)         return name.toLowerCase().indexOf(q) >= 0 ? 1.0 : 0.06;
        return 1.0;
    });
}
function computeEdgeTraces() {
    var yfrom = state.yearFrom, yto = state.yearTo, nx = [], ny = [], rx = [], ry = [];
    EDGE_DATA.forEach(function(e) {
        var ok = true;
        if (HAS_YEAR_DATA && e.years && e.years.length > 0)
            ok = e.years.some(function(y) { return y >= yfrom && y <= yto; });
        if (!ok) return;
        if (e.recip) { rx.push(e.x0, e.x1, null); ry.push(e.y0, e.y1, null); }
        else         { nx.push(e.x0, e.x1, null); ny.push(e.y0, e.y1, null); }
    });
    return {nx: nx, ny: ny, rx: rx, ry: ry};
}
function render() {
    var d = document.getElementById('net-plot');
    var op = computeOpacities();
    var baseSz = state.sizeMetric === 'pagerank' ? NODE_SIZES_PR : NODE_SIZES_INDEG;
    var zf = state.zoomFactor;
    var sz = zf === 1.0 ? baseSz : baseSz.map(function(s) { return Math.max(2, s * zf); });
    var ed = computeEdgeTraces();
    Plotly.restyle(d, {'marker.opacity': [op], 'marker.size': [sz]}, [TRACE_NODE]);
    Plotly.restyle(d, {x: [ed.nx], y: [ed.ny]}, [TRACE_EDGE_NORMAL]);
    Plotly.restyle(d, {x: [ed.rx], y: [ed.ry]}, [TRACE_EDGE_RECIP]);
}
function doSearch() { state.search = document.getElementById('search-box').value; state.egoIdx = -1; render(); }
function egoNetwork(idx) {
    state.egoIdx = idx; state.search = '';
    document.getElementById('search-box').value = '';
    document.getElementById('reset-btn').style.display = 'inline-flex';
    render();
}
function resetView() {
    state.egoIdx = -1; state.search = '';
    document.getElementById('search-box').value = '';
    document.getElementById('reset-btn').style.display = 'none';
    document.getElementById('side-panel').style.display = 'none';
    render();
}
function setMetric(m) {
    state.sizeMetric = m;
    document.getElementById('btn-indeg').classList.toggle('active', m === 'indeg');
    document.getElementById('btn-pr').classList.toggle('active', m === 'pagerank');
    render();
}
function syncYearFilter() {
    var yfrom = parseInt(document.getElementById('yr-from').value);
    var yto   = parseInt(document.getElementById('yr-to').value);
    if (yfrom > yto) { yfrom = yto; document.getElementById('yr-from').value = yfrom; }
    state.yearFrom = yfrom; state.yearTo = yto;
    document.getElementById('yr-from-val').textContent = yfrom;
    document.getElementById('yr-to-val').textContent   = yto;
    render();
}
function resetYearFilter() {
    state.yearFrom = YEAR_MIN; state.yearTo = YEAR_MAX;
    document.getElementById('yr-from').value = YEAR_MIN;
    document.getElementById('yr-to').value   = YEAR_MAX;
    document.getElementById('yr-from-val').textContent = YEAR_MIN;
    document.getElementById('yr-to-val').textContent   = YEAR_MAX;
    render();
}
function downloadCSV() {
    var op = computeOpacities(), lines = ['author,institution,country,in_degree,out_degree'];
    function esc(s) { s = s == null ? '' : String(s); return (s.indexOf(',') >= 0 || s.indexOf('"') >= 0) ? '"' + s.replace(/"/g, '""') + '"' : s; }
    NODE_META.forEach(function(m, i) { if (op[i] > 0.5) lines.push([m.name, m.inst, m.country, m.indeg, m.outdeg].map(esc).join(',')); });
    var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([lines.join('\n')], {type: 'text/csv'}));
    a.download = 'citation_network_export.csv'; document.body.appendChild(a); a.click(); document.body.removeChild(a);
}
function openPanel(idx) {
    var m = NODE_META[idx];
    document.getElementById('panel-name').textContent = m.name;
    document.getElementById('panel-rows').innerHTML = [
        ['Institution', m.inst || '—'], ['Country', m.country || '—'],
        ['Cited by (in-degree)', m.indeg], ['Cites (out-degree)', m.outdeg]
    ].map(function(r) {
        return '<div class="pr"><span class="pk">' + r[0] + '</span><span class="pv">' + r[1] + '</span></div>';
    }).join('');
    function fillList(adj, listId) {
        var list = document.getElementById(listId);
        list.innerHTML = adj.length
            ? adj.slice(0, 10).map(function(x) {
                return '<li data-i="' + x[0] + '"><span class="cn">' + NODE_META[x[0]].name
                     + '</span><span class="cc">' + x[1] + ' paper' + (x[1] > 1 ? 's' : '') + '</span></li>';
              }).join('')
            : '<li class="empty">None in this view</li>';
        list.querySelectorAll('li[data-i]').forEach(function(el) {
            el.addEventListener('click', function() { egoNetwork(+el.dataset.i); openPanel(+el.dataset.i); });
        });
    }
    fillList(IN_ADJ[idx],  'list-cited-by');
    fillList(OUT_ADJ[idx], 'list-cites');
    document.getElementById('side-panel').style.display = 'flex';
}
// ── Init ─────────────────────────────────────────────────────────────────────
state.yearFrom = YEAR_MIN; state.yearTo = YEAR_MAX;
Plotly.newPlot('net-plot', TRACES, LAYOUT, {responsive: true, displayModeBar: true,
    modeBarButtonsToRemove: ['select2d', 'lasso2d', 'toggleSpikelines']});
document.getElementById('net-plot').on('plotly_click', function(data) {
    if (!data || !data.points || !data.points.length) return;
    var pt = data.points[0];
    if (pt.curveNumber === TRACE_NODE) { egoNetwork(pt.pointIndex); openPanel(pt.pointIndex); }
});
document.getElementById('close-panel').addEventListener('click', function() { resetView(); });
document.getElementById('net-plot').on('plotly_afterplot', function() {
    if (_initXRange) return;
    var d = document.getElementById('net-plot');
    if (d.layout && d.layout.xaxis && d.layout.xaxis.range) _initXRange = d.layout.xaxis.range.slice();
});
document.getElementById('net-plot').on('plotly_relayout', function(ev) {
    var xlo = ev['xaxis.range[0]'], xhi = ev['xaxis.range[1]'];
    if (xlo !== undefined && xhi !== undefined) {
        if (_initXRange) {
            var initSpan = _initXRange[1] - _initXRange[0];
            var currSpan = xhi - xlo;
            state.zoomFactor = (currSpan > 0 && initSpan > 0) ? initSpan / currSpan : 1.0;
            render();
        }
    } else if (ev['xaxis.autorange'] === true) {
        state.zoomFactor = 1.0;
        render();
    }
});
(function() {
    var authIn = document.getElementById('search-box');
    var authDd = document.getElementById('search-dd');
    var nameList = NODE_NAMES.map(function(n, i) {
        return {i: i, name: n, inst: NODE_META[i] ? NODE_META[i].inst : ''};
    }).sort(function(a, b) { return a.name.localeCompare(b.name); });
    function showDd(q) {
        var ql = q.toLowerCase();
        var hits = nameList.filter(function(x) { return x.name.toLowerCase().indexOf(ql) >= 0; }).slice(0, 18);
        if (!hits.length) { authDd.style.display = 'none'; return; }
        authDd.innerHTML = hits.map(function(h) {
            return '<div class="dd-item" data-i="' + h.i + '"><div>' + h.name + '</div>'
                 + '<div class="dd-sub">' + (h.inst || '') + '</div></div>';
        }).join('');
        authDd.querySelectorAll('.dd-item').forEach(function(el) {
            el.addEventListener('mousedown', function(e) {
                e.preventDefault();
                authIn.value = NODE_NAMES[+el.dataset.i];
                egoNetwork(+el.dataset.i); openPanel(+el.dataset.i);
                authDd.style.display = 'none';
            });
        });
        authDd.style.display = 'block';
    }
    authIn.addEventListener('input', function() { var q = authIn.value.trim(); if (q.length >= 2) showDd(q); else authDd.style.display = 'none'; });
    authIn.addEventListener('blur', function() { setTimeout(function() { authDd.style.display = 'none'; }, 160); });
    authIn.addEventListener('focus', function() { if (authIn.value.trim().length >= 2) showDd(authIn.value.trim()); });
})();
"""

    css_full = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f8;height:100vh;display:flex;flex-direction:column;overflow:hidden}
#hdr{background:#1a2742;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0}
#hdr h1{font-size:1.05rem;font-weight:700}
#hdr p{font-size:.72rem;opacity:.65;margin-top:1px}
#hdr .spacer{flex:1}
#hdr a{color:rgba(255,255,255,.65);font-size:.75rem;text-decoration:none}
#hdr a:hover{color:#fff}
#ctrl{background:#fff;border-bottom:1px solid #d8dde5;padding:8px 20px;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;flex-shrink:0}
.cg{display:flex;flex-direction:column;gap:2px}
.cg label{font-size:.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.04em}
.cg-row{display:flex;align-items:center;gap:6px;font-size:.82rem}
.cg-row input[type=range]{width:90px;accent-color:#1a2742;cursor:pointer}
#srch-wrap{position:relative}
#search-box{padding:6px 10px;border:1px solid #c8ced5;border-radius:5px;font-size:.88rem;width:200px;outline:none}
#search-box:focus{border-color:#1a2742;box-shadow:0 0 0 2px rgba(26,39,66,.12)}
#search-dd{position:absolute;top:100%;left:0;z-index:200;background:#fff;border:1px solid #c8ced5;border-top:none;border-radius:0 0 5px 5px;max-height:200px;overflow-y:auto;width:280px;box-shadow:0 6px 16px rgba(0,0,0,.12);display:none}
.dd-item{padding:6px 10px;cursor:pointer;font-size:.83rem;border-bottom:1px solid #f2f2f2}
.dd-item:hover{background:#f0f4ff}
.dd-sub{font-size:.72rem;color:#999;margin-top:1px}
.btn-grp{display:flex}
.tog{background:#eee;border:1px solid #ccc;padding:5px 12px;cursor:pointer;font-size:.8rem;color:#555}
.tog:first-child{border-radius:5px 0 0 5px}
.tog:last-child{border-radius:0 5px 5px 0}
.tog.active{background:#1a2742;color:#fff;border-color:#1a2742}
.ctl-btn{background:#eee;border:1px solid #ccc;border-radius:5px;padding:5px 10px;cursor:pointer;font-size:.8rem;color:#555}
.ctl-btn:hover{background:#ddd}
.reset-btn{background:#e74c3c;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer;font-size:.8rem;display:none}
.reset-btn:hover{background:#c0392b}
.dl-btn{background:#2c4a8a;color:#fff;border:none;border-radius:5px;padding:5px 12px;cursor:pointer;font-size:.8rem}
.dl-btn:hover{background:#1a2742}
#workspace{display:flex;flex:1;min-height:0}
#net-plot{flex:1;min-width:0}
#side-panel{width:270px;background:#fff;border-left:1px solid #d8dde5;display:none;flex-direction:column;overflow-y:auto;flex-shrink:0}
#panel-hdr{background:#1a2742;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
#panel-hdr h3{font-size:.9rem;font-weight:600;line-height:1.3;word-break:break-word}
#close-panel{background:none;border:none;color:#fff;font-size:1.1rem;cursor:pointer;opacity:.7;flex-shrink:0}
#close-panel:hover{opacity:1}
#panel-body{padding:10px 14px;flex:1}
.pr{display:flex;justify-content:space-between;padding:4px 0;font-size:.8rem;border-bottom:1px solid #f0f0f0}
.pk{color:#888}
.pv{font-weight:600;text-align:right}
#panel-body h4{font-size:.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.04em;margin:12px 0 5px}
.cit-list{list-style:none}
.cit-list li{display:flex;justify-content:space-between;padding:4px 0;font-size:.8rem;cursor:pointer;border-bottom:1px solid #f6f6f6}
.cit-list li.empty{color:#aaa;cursor:default;font-style:italic}
.cn{color:#2c3e50}
.cn:hover{text-decoration:underline}
.cc{color:#aaa;font-size:.72rem;white-space:nowrap;margin-left:4px}"""

    html_full = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_TITLE} — Citation Network Explorer</title>
<script src="{PLOTLY_CDN}" charset="utf-8"></script>
<style>
{css_full}
</style>
</head>
<body>
<div id="hdr">
  <div>
    <h1>{_TITLE} — Citation Network Explorer</h1>
    <p>{n_nodes:,} authors &middot; {n_edges:,} citation links &middot; {n_communities} research clusters</p>
  </div>
  <div class="spacer"></div>
  <a href="{_pf('citation_network.html')}">&#8592; Overview &amp; charts</a>
</div>
<div id="ctrl">
  <div class="cg">
    <label>Find author</label>
    <div id="srch-wrap">
      <input id="search-box" type="text" placeholder="Type name…" autocomplete="off" oninput="doSearch()" />
      <div id="search-dd"></div>
    </div>
  </div>
  <div class="cg">
    <label>Node size</label>
    <div class="btn-grp">
      <button id="btn-indeg" class="tog active" onclick="setMetric('indeg')">In-degree</button>
      <button id="btn-pr"    class="tog"        onclick="setMetric('pagerank')">PageRank</button>
    </div>
  </div>
  {year_filter_ctrl}
  <button class="reset-btn" id="reset-btn" onclick="resetView()">&#8857; Reset</button>
  <button class="dl-btn" onclick="downloadCSV()">&#8595; Export CSV</button>
</div>
<div id="workspace">
  <div id="net-plot"></div>
  <div id="side-panel">
    <div id="panel-hdr">
      <h3 id="panel-name"></h3>
      <button id="close-panel">&#x2715;</button>
    </div>
    <div id="panel-body">
      <div id="panel-rows"></div>
      <h4>Cited by (within corpus)</h4>
      <ul class="cit-list" id="list-cited-by"></ul>
      <h4>Cites (within corpus)</h4>
      <ul class="cit-list" id="list-cites"></ul>
    </div>
  </div>
</div>
<script>
{js_vars_full}
</script>
<script>
{js_code_full}
</script>
</body>
</html>
"""

    full_file = os.path.join(out, _pf("citation_network_explore.html"))
    with open(full_file, "w", encoding="utf-8") as fh:
        fh.write(html_full)
    print(f"Written: {full_file}  ({os.path.getsize(full_file) // 1024} KB)")

    # ── Cytoscape.js standalone explorer ─────────────────────────────────────
    write_cytoscape_html(
        out, G_plot, author_lookup, partition, pos,
        pagerank, edge_papers_map,
        n_communities, generated_date,
        author_subclass=author_subclass or None,
        author_funders=author_funders or None,
    )


if __name__ == "__main__":
    main()

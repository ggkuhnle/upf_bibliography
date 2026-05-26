#!/usr/bin/env python3
"""
make_citation_network.py
Standalone HTML dashboard: internal citation network for this corpus.

Reads:
  output/<prefix>_citation_edges_author.csv
  output/<prefix>_papers_by_author.csv

Writes:
  output/<prefix>_citation_network.html
"""

import argparse
import json as _json
import os

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
    return f"{_PREFIX}_{name}" if _PREFIX else name


PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
    "#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD",
    "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF",
]

TOP_NODES   = 300   # max nodes by in-degree for readability
TOP_BAR     = 25    # top-N for bar chart
LAYOUT_SEED = 42

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
    "total citation counts."
    "</div>"
)


def _community_partition(G_undirected):
    """Return {node: community_int} using Louvain if available, else greedy modularity."""
    try:
        from community import best_partition  # python-louvain
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
    """Return a Plotly figure of the citation network."""
    node_ids = list(G.nodes())

    # Edge trace
    ex, ey = [], []
    for u, v in G.edges():
        ex += [pos[u][0], pos[v][0], None]
        ey += [pos[u][1], pos[v][1], None]

    edge_trace = go.Scatter(
        x=ex, y=ey,
        mode="lines",
        line=dict(width=0.5, color="rgba(160,160,160,0.35)"),
        hoverinfo="none",
        showlegend=False,
    )

    # Node trace
    nx_, ny_, nc, ns, nt = [], [], [], [], []
    for nid in node_ids:
        meta = author_lookup.get(nid, {})
        indeg  = G.in_degree(nid)
        outdeg = G.out_degree(nid)
        cit    = int(meta.get("citations", 0))
        name   = meta.get("author_name") or str(nid)
        inst   = meta.get("institution") or "—"
        ctr    = meta.get("country") or "—"

        x, y = pos[nid]
        nx_.append(x)
        ny_.append(y)
        nc.append(PALETTE[partition.get(nid, 0) % len(PALETTE)])
        ns.append(max(5, min(22, 5 + indeg * 0.6)))
        nt.append(
            f"<b>{name}</b><br>"
            f"{inst}<br>"
            f"Country: {ctr}<br>"
            f"Cited by {indeg} author(s) in corpus<br>"
            f"Cites {outdeg} author(s) in corpus<br>"
            f"Total OpenAlex citations: {cit:,}"
            "<extra></extra>"
        )

    node_trace = go.Scatter(
        x=nx_, y=ny_,
        mode="markers",
        marker=dict(
            size=ns, color=nc,
            line=dict(width=0.5, color="white"),
        ),
        hovertemplate=nt,
        showlegend=False,
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        title=f"{_TITLE} — Internal Citation Network (top {TOP_NODES} nodes by in-degree)",
        height=720,
        margin=dict(l=10, r=10, t=50, b=10),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        plot_bgcolor="white",
        paper_bgcolor="white",
        hovermode="closest",
    )
    return fig


def fig_top_cited(author_lookup, indegree_map):
    """Horizontal bar chart: top-N authors by in-degree."""
    rows = []
    for aid, indeg in indegree_map.items():
        meta = author_lookup.get(aid, {})
        rows.append({
            "name":  (meta.get("author_name") or str(aid))[:50],
            "indeg": indeg,
        })
    df = pd.DataFrame(rows).nlargest(TOP_BAR, "indeg").sort_values("indeg")

    fig = go.Figure(go.Bar(
        x=df["indeg"], y=df["name"],
        orientation="h",
        marker_color="#636EFA",
        text=df["indeg"], textposition="outside",
        hovertemplate="<b>%{y}</b><br>Cited by %{x} author(s) in corpus<extra></extra>",
    ))
    fig.update_layout(
        title=f"Top {TOP_BAR} Most-Cited Authors (within corpus)",
        xaxis_title="In-degree (cited by N authors in corpus)",
        yaxis_title="",
        height=max(500, TOP_BAR * 22),
        margin=dict(l=10, r=80, t=50, b=40),
        plot_bgcolor="white",
        paper_bgcolor="white",
        yaxis=dict(tickfont=dict(size=11)),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    return fig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    args = parser.parse_args()
    out  = args.output_dir

    cite_path   = os.path.join(out, _pf("citation_edges_author.csv"))
    author_path = os.path.join(out, _pf("papers_by_author.csv"))

    print("Loading data…")
    cite_df   = pd.read_csv(cite_path)
    author_df = pd.read_csv(author_path)

    # Build author lookup
    author_lookup = {}
    for _, r in author_df.iterrows():
        aid = r.get("author_id")
        if pd.notna(aid) and aid:
            author_lookup[aid] = {
                "author_name": str(r.get("author_name") or ""),
                "institution": str(r.get("institution") or ""),
                "country":     str(r.get("country") or ""),
                "papers":      int(r.get("papers", 0)),
                "citations":   int(r.get("citations", 0)),
            }

    # Build directed graph
    print("Building directed citation graph…")
    G = nx.DiGraph()
    for _, row in cite_df.iterrows():
        ca  = row.get("citing_author_id")
        cd  = row.get("cited_author_id")
        cnt = int(row.get("citations", 1))
        if pd.isna(ca) or pd.isna(cd) or not ca or not cd:
            continue
        if G.has_edge(ca, cd):
            G[ca][cd]["weight"] += cnt
        else:
            G.add_edge(ca, cd, weight=cnt)

    print(f"  Full graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    # Limit to top TOP_NODES by in-degree
    indegree_all = dict(G.in_degree())
    if G.number_of_nodes() > TOP_NODES:
        top_nodes = sorted(indegree_all, key=lambda n: indegree_all[n], reverse=True)[:TOP_NODES]
        G_plot = G.subgraph(top_nodes).copy()
    else:
        G_plot = G.copy()

    print(f"  Plotting graph: {G_plot.number_of_nodes()} nodes, {G_plot.number_of_edges()} edges")

    # Undirected projection for layout and community detection
    G_undirected = G_plot.to_undirected()

    print("Detecting communities…")
    partition = _community_partition(G_undirected)

    print("Computing layout…")
    pos = nx.spring_layout(G_undirected, weight="weight", seed=LAYOUT_SEED, k=0.8)

    # in-degree map for the plot subgraph
    indegree_map = dict(G_plot.in_degree())

    print("Building figures…")
    PLOTLY_CDN = "https://cdn.plot.ly/plotly-3.5.0.min.js"

    f_net = fig_citation_network(G_plot, author_lookup, partition, pos)
    f_bar = fig_top_cited(author_lookup, indegree_map)

    figs = {
        "Internal Citation Network":       f_net,
        f"Top {TOP_BAR} Most-Cited Authors": f_bar,
    }
    fig_divs = {
        title: fig.to_html(full_html=False, include_plotlyjs=False,
                           config={"responsive": True})
        for title, fig in figs.items()
    }
    sections = "\n".join(
        f'<h2 class="sec">{t}</h2>\n<div class="fig-wrap">{d}</div>'
        for t, d in fig_divs.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_TITLE} — Citation Network</title>
<script src="{PLOTLY_CDN}" charset="utf-8"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
      background:#f5f6fa;color:#2c3e50;line-height:1.6}}
#hdr{{background:linear-gradient(135deg,#1a2742 0%,#2c4a8a 100%);
      color:#fff;padding:36px 32px 28px;text-align:center}}
#hdr h1{{font-size:1.7rem;font-weight:800;margin-bottom:8px}}
#hdr p{{opacity:.75;font-size:.95rem}}
main{{max-width:1100px;margin:0 auto;padding:32px 24px 80px}}
h2.sec{{font-size:.9rem;font-weight:700;color:#888;text-transform:uppercase;
        letter-spacing:1px;margin:40px 0 14px;padding-bottom:6px;
        border-bottom:2px solid #eee}}
.fig-wrap{{background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.07);
           padding:8px;margin-bottom:8px;overflow-x:auto}}
footer{{text-align:center;padding:24px;font-size:.78rem;color:#aaa}}
a{{color:inherit}}
</style>
</head>
<body>
{DISCLAIMER}
<div id="hdr">
  <h1>{_TITLE} — Citation Network</h1>
  <p>Who cites whom within this literature — internal citation graph and most-cited authors</p>
</div>
<main>
{sections}
  <p style="font-size:.82rem;color:#888;margin-top:24px">
    ← <a href="index.html" style="color:#2980b9">Back to overview</a>
  </p>
</main>
<footer>Data: <a href="https://openalex.org">OpenAlex</a> · Analysis by G. Kuhnle · Generated {pd.Timestamp.now().strftime("%B %Y")}</footer>
</body>
</html>
"""

    output_file = os.path.join(out, _pf("citation_network.html"))
    os.makedirs(out, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Written: {output_file}  ({os.path.getsize(output_file) // 1024} KB)")


if __name__ == "__main__":
    main()

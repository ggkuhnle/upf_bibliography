#!/usr/bin/env python3
"""
make_dashboard.py
Rebuild output/upf_dashboard.html and output/author_centrality.csv from the
CSV files written by upf_bibliometrics.py.  Equivalent to running every cell
in notebooks/author_network.ipynb, without needing Jupyter.

Usage:
    python make_dashboard.py
    python make_dashboard.py --output-dir results  # custom output dir
"""

import argparse
import collections
import json
import math
import os
import warnings

import numpy as np
import networkx as nx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", default="output")
    return p.parse_args()

MIN_PAPERS      = 3
MIN_EDGE_WEIGHT = 2
TOP_N_LABELS    = 30
TOP_N_RANKING   = 25
LAYOUT_SEED     = 42
PLOT_CAP        = 500

ST_COLORS = {
    "Observational":                     "#27ae60",
    "Systematic Review / Meta-analysis": "#8e44ad",
    "Review":                            "#9b59b6",
    "RCT":                               "#2980b9",
    "Clinical Trial":                    "#e67e22",
    "Other":                             "#95a5a6",
}
ST_SYMBOLS = {
    "Observational":                     "circle",
    "Systematic Review / Meta-analysis": "diamond",
    "Review":                            "square",
    "RCT":                               "triangle-up",
    "Clinical Trial":                    "cross",
    "Other":                             "circle-open",
}

DISCLAIMER = (
    '<div style="background:#fff8e1;border-top:4px solid #f9a825;'
    'border-bottom:1px solid #f9a825;padding:13px 24px;'
    'font-family:Arial,Helvetica,sans-serif;font-size:.88rem;'
    'color:#4a3800;line-height:1.55">'
    "<strong>Disclaimer:</strong> This page presents automatically generated "
    "bibliometric data retrieved from "
    '<a href="https://openalex.org" style="color:#4a3800;font-weight:600" '
    'target="_blank">OpenAlex</a>. '
    "It does not represent the views, opinions, or endorsement of any individual, "
    "institution, or organisation. Data may contain errors, omissions, or "
    "misattributions inherent to automated retrieval. Results reflect publication "
    "patterns only and should not be interpreted as assessments of individual "
    "researchers or institutions."
    "</div>"
)


# ── Load data ─────────────────────────────────────────────────────────────────

def load_data(out):
    print("Loading CSVs…")
    edges_df       = pd.read_csv(os.path.join(out, "coauthorship_edges.csv"))
    authors_df     = pd.read_csv(os.path.join(out, "papers_by_author.csv"))
    institutions_df = pd.read_csv(os.path.join(out, "papers_by_institution.csv"))
    institutions_df = institutions_df[
        institutions_df["institution"].notna() & (institutions_df["institution"] != "")
    ].copy()

    try:
        funders_df      = pd.read_csv(os.path.join(out, "papers_by_funder.csv"))
        funding_cty_df  = pd.read_csv(os.path.join(out, "funding_by_country.csv"))
        dept_df         = pd.read_csv(os.path.join(out, "papers_by_department.csv"))
    except FileNotFoundError:
        funders_df = funding_cty_df = dept_df = pd.DataFrame()
        print("  ⚠  Funding/department CSVs not found")

    year_df         = pd.read_csv(os.path.join(out, "papers_by_year.csv"))
    country_year_df = pd.read_csv(os.path.join(out, "papers_by_country_year.csv"))
    net_metrics_df  = pd.read_csv(os.path.join(out, "network_metrics_by_year.csv"))
    edges_yr_df     = pd.read_csv(os.path.join(out, "coauthorship_edges_by_year.csv"))
    country_df      = pd.read_csv(os.path.join(out, "papers_by_country.csv"))
    country_df      = country_df[country_df["country"].notna() & (country_df["country"] != "")].copy()

    # Clip temporal data to 2000+
    year_df         = year_df[year_df["year"] >= 2000].copy()
    country_year_df = country_year_df[country_year_df["year"] >= 2000].copy()
    net_metrics_df  = net_metrics_df[net_metrics_df["year"] >= 2000].copy()

    # Study type CSVs (optional)
    st_df = st_year_df = st_auth_df = None
    for name, attr in [
        ("papers_by_study_type.csv",        "st_df"),
        ("papers_by_study_type_year.csv",   "st_year_df"),
        ("papers_by_author_study_type.csv", "st_auth_df"),
    ]:
        path = os.path.join(out, name)
        if os.path.exists(path):
            locals()[attr]  # just to reference it
    _st_path      = os.path.join(out, "papers_by_study_type.csv")
    _st_year_path = os.path.join(out, "papers_by_study_type_year.csv")
    _st_auth_path = os.path.join(out, "papers_by_author_study_type.csv")
    if os.path.exists(_st_path):
        st_df      = pd.read_csv(_st_path)
        st_year_df = pd.read_csv(_st_year_path)
        st_auth_df = pd.read_csv(_st_auth_path)
        print(f"  Study type data loaded  ({int(st_df['papers'].sum()):,} papers classified)")
    else:
        print("  ⚠  Study type CSVs not found — re-run upf_bibliometrics.py")

    print(f"  {len(edges_df):,} edge rows · {len(authors_df):,} author rows · "
          f"{len(institutions_df):,} institution rows")
    return (edges_df, authors_df, institutions_df, funders_df, funding_cty_df,
            dept_df, year_df, country_year_df, net_metrics_df, edges_yr_df,
            country_df, st_df, st_year_df, st_auth_df)


# ── Build graph ───────────────────────────────────────────────────────────────

def build_graph(edges_df, authors_df):
    print("Building graph…")
    node_attrs = (
        authors_df
        .groupby("author_id", as_index=True)
        .first()[["author_name", "institution", "country", "papers", "citations"]]
        .to_dict(orient="index")
    )
    core_authors = {aid for aid, a in node_attrs.items() if a["papers"] >= MIN_PAPERS}

    G = nx.Graph()
    for _, row in edges_df.iterrows():
        a1, a2, w = row["author1_id"], row["author2_id"], row["shared_papers"]
        if a1 not in core_authors or a2 not in core_authors or w < MIN_EDGE_WEIGHT:
            continue
        if G.has_edge(a1, a2):
            G[a1][a2]["weight"] += w
        else:
            G.add_edge(a1, a2, weight=w)

    for node in G.nodes():
        a = node_attrs.get(node, {})
        G.nodes[node]["name"]        = a.get("author_name", node)
        G.nodes[node]["institution"] = a.get("institution", "")
        G.nodes[node]["country"]     = a.get("country", "")
        G.nodes[node]["papers"]      = a.get("papers", 0)
        G.nodes[node]["citations"]   = a.get("citations", 0)

    lcc_nodes = max(nx.connected_components(G), key=len)
    G_lcc = G.subgraph(lcc_nodes).copy()
    print(f"  Full graph: {G.number_of_nodes():,} nodes  LCC: "
          f"{G_lcc.number_of_nodes():,} nodes, {G_lcc.number_of_edges():,} edges")
    return G, G_lcc, node_attrs


# ── Centrality + community ────────────────────────────────────────────────────

def compute_centrality(G_lcc):
    print("Computing centrality…")
    degree_cent = nx.degree_centrality(G_lcc)
    betweenness = nx.betweenness_centrality(G_lcc, weight="weight", normalized=True)
    pagerank    = nx.pagerank(G_lcc, weight="weight")
    clustering  = nx.clustering(G_lcc, weight="weight")

    df = pd.DataFrame({
        "author_id":          list(G_lcc.nodes()),
        "name":               [G_lcc.nodes[n]["name"]        for n in G_lcc.nodes()],
        "institution":        [G_lcc.nodes[n]["institution"] for n in G_lcc.nodes()],
        "country":            [G_lcc.nodes[n]["country"]     for n in G_lcc.nodes()],
        "papers":             [G_lcc.nodes[n]["papers"]      for n in G_lcc.nodes()],
        "citations":          [G_lcc.nodes[n].get("citations", 0) for n in G_lcc.nodes()],
        "degree":             [G_lcc.degree(n)               for n in G_lcc.nodes()],
        "degree_centrality":  [degree_cent[n]                for n in G_lcc.nodes()],
        "betweenness":        [betweenness[n]                for n in G_lcc.nodes()],
        "pagerank":           [pagerank[n]                   for n in G_lcc.nodes()],
        "clustering":         [clustering[n]                 for n in G_lcc.nodes()],
    })
    df.sort_values("betweenness", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def compute_communities(G_lcc, centrality_df):
    print("Detecting communities…")
    communities = nx.community.louvain_communities(G_lcc, weight="weight", seed=LAYOUT_SEED)
    communities = sorted(communities, key=len, reverse=True)
    print(f"  {len(communities)} communities  "
          f"(top-5 sizes: {[len(c) for c in communities[:5]]})")

    node_community = {node: idx for idx, comm in enumerate(communities) for node in comm}
    nx.set_node_attributes(G_lcc, node_community, "community")
    centrality_df["community"] = centrality_df["author_id"].map(node_community)

    comm_summary = (
        centrality_df.groupby("community")
        .agg(
            size=("name", "count"),
            total_papers=("papers", "sum"),
            total_citations=("citations", "sum"),
            top_country=("country", lambda x: x.value_counts().index[0] if len(x) else ""),
            top_author=("name", lambda x: (
                centrality_df.loc[x.index]
                .sort_values("betweenness", ascending=False)["name"].iloc[0]
                if len(x) else ""
            )),
        )
        .reset_index()
        .sort_values("size", ascending=False)
    )

    def _label(row):
        last = row["top_author"].split()[-1] if row["top_author"] else "?"
        return f"{row['top_country'] or '?'} · {last} ({int(row['size'])})"

    community_names = {int(r["community"]): _label(r) for _, r in comm_summary.iterrows()}
    nx.set_node_attributes(G_lcc, {n: community_names.get(v, str(v))
                                    for n, v in node_community.items()}, "community_label")
    centrality_df["community_label"] = centrality_df["community"].map(community_names)
    return communities, community_names


# ── Network layout ────────────────────────────────────────────────────────────

def compute_layout(G_lcc):
    print("Computing spring layout…")
    if G_lcc.number_of_nodes() > PLOT_CAP:
        top_nodes = sorted(G_lcc.nodes(), key=lambda n: G_lcc.degree(n), reverse=True)[:PLOT_CAP]
        G_plot = G_lcc.subgraph(top_nodes).copy()
    else:
        G_plot = G_lcc
    pos = nx.spring_layout(G_plot, weight="weight", seed=LAYOUT_SEED, k=0.8)
    return G_plot, pos


# ── Figures ───────────────────────────────────────────────────────────────────

def fig_institutions(institutions_df):
    inst_p = institutions_df.nlargest(TOP_N_RANKING, "papers").assign(
        label=lambda df: df["institution"].str[:55]).sort_values("papers")
    inst_c = institutions_df.nlargest(TOP_N_RANKING, "citations").assign(
        label=lambda df: df["institution"].str[:55]).sort_values("citations")

    fig = go.Figure()
    fig.add_trace(go.Bar(x=inst_p["papers"], y=inst_p["label"], orientation="h",
        name="Papers", visible=True,
        customdata=inst_p[["institution","country","citations"]].values,
        hovertemplate="<b>%{customdata[0]}</b><br>Country: %{customdata[1]}<br>"
                      "Papers: %{x:,}<br>Citations: %{customdata[2]:,}<extra></extra>",
        marker_color="steelblue"))
    fig.add_trace(go.Bar(x=inst_c["citations"], y=inst_c["label"], orientation="h",
        name="Citations", visible=False,
        customdata=inst_c[["institution","country","papers"]].values,
        hovertemplate="<b>%{customdata[0]}</b><br>Country: %{customdata[1]}<br>"
                      "Citations: %{x:,}<br>Papers: %{customdata[2]:,}<extra></extra>",
        marker_color="coral"))
    fig.update_layout(
        title=f"Top {TOP_N_RANKING} Institutions by Paper Count",
        height=700, xaxis_title="Papers", showlegend=False,
        margin=dict(l=10, r=20, t=60, b=40),
        updatemenus=[dict(type="buttons", direction="right",
            x=0.0, xanchor="left", y=1.08, yanchor="top",
            buttons=[
                dict(label="By Papers", method="update",
                     args=[{"visible":[True,False]},
                           {"title":f"Top {TOP_N_RANKING} Institutions by Paper Count",
                            "xaxis.title.text":"Papers"}]),
                dict(label="By Citations", method="update",
                     args=[{"visible":[False,True]},
                           {"title":f"Top {TOP_N_RANKING} Institutions by Citation Count",
                            "xaxis.title.text":"Citations"}]),
            ])])
    return fig


def fig_authors(authors_df):
    clean = authors_df[authors_df["author_name"].notna() & (authors_df["author_name"] != "")].copy()
    auth_p = clean.nlargest(TOP_N_RANKING, "papers").sort_values("papers")
    auth_c = clean.nlargest(TOP_N_RANKING, "citations").sort_values("citations")

    fig = go.Figure()
    fig.add_trace(go.Bar(x=auth_p["papers"], y=auth_p["author_name"], orientation="h",
        name="Papers", visible=True,
        customdata=auth_p[["institution","country","citations"]].values,
        hovertemplate="<b>%{y}</b><br>Institution: %{customdata[0]}<br>"
                      "Country: %{customdata[1]}<br>Papers: %{x:,}<br>"
                      "Citations: %{customdata[2]:,}<extra></extra>",
        marker_color="steelblue"))
    fig.add_trace(go.Bar(x=auth_c["citations"], y=auth_c["author_name"], orientation="h",
        name="Citations", visible=False,
        customdata=auth_c[["institution","country","papers"]].values,
        hovertemplate="<b>%{y}</b><br>Institution: %{customdata[0]}<br>"
                      "Country: %{customdata[1]}<br>Citations: %{x:,}<br>"
                      "Papers: %{customdata[2]:,}<extra></extra>",
        marker_color="coral"))
    fig.update_layout(
        title=f"Top {TOP_N_RANKING} Authors by Paper Count",
        height=700, xaxis_title="Papers", showlegend=False,
        margin=dict(l=10, r=20, t=60, b=40),
        updatemenus=[dict(type="buttons", direction="right",
            x=0.0, xanchor="left", y=1.08, yanchor="top",
            buttons=[
                dict(label="By Papers", method="update",
                     args=[{"visible":[True,False]},
                           {"title":f"Top {TOP_N_RANKING} Authors by Paper Count",
                            "xaxis.title.text":"Papers"}]),
                dict(label="By Citations", method="update",
                     args=[{"visible":[False,True]},
                           {"title":f"Top {TOP_N_RANKING} Authors by Citation Count",
                            "xaxis.title.text":"Citations"}]),
            ])])
    return fig


def fig_degree_dist(G_lcc):
    degrees = [d for _, d in G_lcc.degree()]
    freq    = collections.Counter(degrees)
    deg_df  = pd.DataFrame({"degree": list(freq.keys()), "count": list(freq.values())}).sort_values("degree")

    fig = make_subplots(rows=1, cols=2,
        subplot_titles=["Degree Distribution (linear)", "Degree Distribution (log–log)"])
    fig.add_trace(go.Histogram(x=degrees, nbinsx=40, name="Count", marker_color="steelblue"), row=1, col=1)
    fig.add_trace(go.Scatter(x=deg_df["degree"], y=deg_df["count"], mode="markers",
        marker=dict(size=5, color="steelblue", opacity=0.7),
        hovertemplate="Degree: %{x}<br>Count: %{y}<extra></extra>"), row=1, col=2)
    fig.update_xaxes(title_text="Degree", row=1, col=1)
    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_xaxes(type="log", title_text="Degree (log)", row=1, col=2)
    fig.update_yaxes(type="log", title_text="Count (log)", row=1, col=2)
    fig.update_layout(height=420, showlegend=False,
        title_text=f"Degree Distribution  |  Mean: {sum(degrees)/len(degrees):.2f}  |  Max: {max(degrees)}")
    return fig


def fig_network_community(G_lcc, G_plot, pos):
    palette = px.colors.qualitative.Alphabet
    edge_x, edge_y = [], []
    for u, v in G_plot.edges():
        x0, y0 = pos[u]; x1, y1 = pos[v]
        edge_x += [x0, x1, None]; edge_y += [y0, y1, None]

    edge_trace = go.Scatter(x=edge_x, y=edge_y, mode="lines",
        line=dict(width=0.5, color="#888"), hoverinfo="none", showlegend=False)

    comm_ids = sorted(set(G_plot.nodes[n].get("community", 0) for n in G_plot.nodes()))
    degree_threshold = sorted([G_plot.degree(n) for n in G_plot.nodes()])[-TOP_N_LABELS] \
        if G_plot.number_of_nodes() >= TOP_N_LABELS else 0
    node_traces = []
    for cid in comm_ids:
        nodes_c = [n for n in G_plot.nodes() if G_plot.nodes[n].get("community", 0) == cid]
        label = G_plot.nodes[nodes_c[0]].get("community_label", str(cid)) if nodes_c else str(cid)
        node_traces.append(go.Scatter(
            x=[pos[n][0] for n in nodes_c], y=[pos[n][1] for n in nodes_c],
            mode="markers+text",
            marker=dict(size=[6 + 2*G_plot.degree(n) for n in nodes_c],
                        color=palette[cid % len(palette)], opacity=0.85,
                        line=dict(width=0.5, color="white")),
            text=[G_plot.nodes[n].get("name", n).split()[-1]
                  if G_plot.degree(n) >= degree_threshold else "" for n in nodes_c],
            textposition="top center", textfont=dict(size=7),
            hovertext=[f"{G_plot.nodes[n].get('name','')}<br>"
                       f"{G_plot.nodes[n].get('institution','')}<br>"
                       f"{G_plot.nodes[n].get('country','')}  deg={G_plot.degree(n)}"
                       for n in nodes_c],
            hoverinfo="text", name=label, legendgroup=str(cid)))

    fig = go.Figure(data=[edge_trace] + node_traces)
    fig.update_layout(
        title=f"UPF Co-authorship Network  |  {G_plot.number_of_nodes()} authors, "
              f"{G_plot.number_of_edges()} edges  |  colour = community",
        showlegend=True, hovermode="closest", height=750,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        legend=dict(title="Community", itemsizing="constant", font=dict(size=9)),
        margin=dict(l=10, r=10, t=50, b=10))
    return fig


def fig_betweenness_scatter(centrality_df):
    plot_df = centrality_df.copy()
    plot_df["community_label"] = plot_df["community_label"].fillna("Other")
    fig = px.scatter(plot_df, x="degree", y="betweenness", size="papers",
        color="community_label", hover_name="name",
        hover_data={"institution":True,"country":True,"papers":True,
                    "degree":True,"betweenness":":.4f","pagerank":":.4f",
                    "community_label":False},
        title="Degree vs Betweenness Centrality  |  point size ∝ papers  |  colour = community",
        labels={"degree":"Degree (number of co-authors)",
                "betweenness":"Betweenness centrality","community_label":"Community"},
        color_discrete_sequence=px.colors.qualitative.Alphabet,
        size_max=30, height=650)
    fig.update_traces(marker_opacity=0.7)
    return fig


def fig_country_heatmap(G_lcc):
    def _safe(v):
        return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)

    country_edges = collections.Counter()
    for u, v, data in G_lcc.edges(data=True):
        c1 = _safe(G_lcc.nodes[u].get("country", ""))
        c2 = _safe(G_lcc.nodes[v].get("country", ""))
        if c1 and c2 and c1 != c2:
            country_edges[tuple(sorted([c1, c2]))] += data.get("weight", 1)

    top_pairs = country_edges.most_common(20)
    countries = sorted({c for pair, _ in top_pairs for c in pair})
    matrix = pd.DataFrame(0, index=countries, columns=countries)
    for (c1, c2), w in top_pairs:
        matrix.loc[c1, c2] = w; matrix.loc[c2, c1] = w

    fig = go.Figure(go.Heatmap(z=matrix.values.tolist(), x=list(matrix.columns),
        y=list(matrix.index), colorscale="YlOrRd",
        hovertemplate="%{y} ↔ %{x}<br>Co-authored papers: %{z}<extra></extra>",
        colorbar=dict(title="Papers")))
    fig.update_layout(title="Cross-country Co-authorship (top-20 pairs)",
        height=520, xaxis=dict(tickangle=45), margin=dict(l=60, r=20, t=60, b=80))
    return fig


def fig_citation_impact(centrality_df):
    plot_cit = centrality_df[centrality_df["papers"] >= MIN_PAPERS].copy()
    plot_cit["cit_per_paper"] = (plot_cit["citations"] / plot_cit["papers"]).round(1)
    plot_cit["community_label"] = plot_cit["community_label"].fillna("Other")

    fig_a = px.scatter(plot_cit, x="papers", y="citations", size="degree",
        color="community_label", hover_name="name",
        hover_data={"institution":True,"country":True,"cit_per_paper":True,
                    "degree":True,"community_label":False},
        log_x=True, log_y=True,
        title="Author Citation Impact  |  size ∝ degree  |  colour = community<br>"
              "<sup>Log scales — top-right = many papers AND highly cited</sup>",
        labels={"papers":"Papers (log)","citations":"Total citations (log)",
                "community_label":"Community"},
        color_discrete_sequence=px.colors.qualitative.Alphabet,
        size_max=25, height=600)
    fig_a.update_traces(marker_opacity=0.7)

    comm_cit = (centrality_df.groupby("community_label")
        .agg(authors=("name","count"), total_papers=("papers","sum"),
             total_citations=("citations","sum"))
        .assign(cit_per_paper=lambda df: (df["total_citations"]/df["total_papers"]).round(1))
        .sort_values("total_citations", ascending=False).head(15).reset_index())
    fig_b = go.Figure(go.Bar(x=comm_cit["total_citations"], y=comm_cit["community_label"],
        orientation="h", marker_color="steelblue",
        customdata=comm_cit[["total_papers","cit_per_paper","authors"]].values,
        hovertemplate="<b>%{y}</b><br>Citations: %{x:,}<br>Papers: %{customdata[0]:,}"
                      "<br>Cit/paper: %{customdata[1]}<br>Authors: %{customdata[2]}<extra></extra>"))
    fig_b.update_layout(title="Total Citations by Research Community (top 15)",
        xaxis_title="Total citations", height=500, yaxis=dict(autorange="reversed"))
    return fig_a, fig_b


def fig_country_impact(country_df):
    top20 = country_df.nlargest(20, "papers")
    top20 = top20.assign(cit_per_paper=lambda df: (df["citations"]/df["papers"]).round(1))
    fig = make_subplots(rows=1, cols=2,
        subplot_titles=["Papers by Country (top 20)", "Citations per Paper (top 20 by papers)"])
    fig.add_trace(go.Bar(x=top20["papers"], y=top20["country"], orientation="h",
        name="Papers", marker_color="steelblue",
        hovertemplate="<b>%{y}</b>: %{x:,} papers<extra></extra>"), row=1, col=1)
    srt = top20.sort_values("cit_per_paper", ascending=True)
    fig.add_trace(go.Bar(x=srt["cit_per_paper"], y=srt["country"], orientation="h",
        name="Cit/paper", marker_color="coral",
        hovertemplate="<b>%{y}</b>: %{x} cit/paper<extra></extra>"), row=1, col=2)
    fig.update_layout(height=550, showlegend=False, title_text="Country-level Output vs Impact")
    fig.update_yaxes(autorange="reversed", row=1, col=1)
    return fig


def fig_funders(funders_df, funding_cty_df):
    top_f = funders_df[funders_df["funder_name"].notna()].head(25).copy()
    top_f["cit_per_paper"] = (top_f["citations"] / top_f["papers"]).round(1)
    fig_a = go.Figure(go.Bar(x=top_f.sort_values("papers")["papers"],
        y=top_f.sort_values("papers")["funder_name"], orientation="h",
        marker_color="steelblue",
        customdata=top_f.sort_values("papers")[["citations","cit_per_paper"]].values,
        hovertemplate="<b>%{y}</b><br>Papers: %{x:,}<br>"
                      "Citations: %{customdata[0]:,}<br>Cit/paper: %{customdata[1]}<extra></extra>"))
    fig_a.update_layout(title="Top 25 Funders by Funded Paper Count",
        xaxis_title="Papers", height=600, yaxis=dict(autorange="reversed"),
        margin=dict(l=300))

    top_cty = funding_cty_df.nlargest(20, "papers").sort_values("pct_funded")
    fig_b = make_subplots(rows=1, cols=2,
        subplot_titles=["% Papers with Funding Acknowledged", "Funded vs Unfunded Papers"])
    fig_b.add_trace(go.Bar(x=top_cty["pct_funded"], y=top_cty["country"], orientation="h",
        name="% Funded", marker_color="teal",
        hovertemplate="<b>%{y}</b>: %{x}% funded<extra></extra>"), row=1, col=1)
    top_cty_s = top_cty.sort_values("papers")
    fig_b.add_trace(go.Bar(x=top_cty_s["funded_papers"], y=top_cty_s["country"],
        orientation="h", name="Funded", marker_color="teal", opacity=0.8), row=1, col=2)
    fig_b.add_trace(go.Bar(x=top_cty_s["papers"]-top_cty_s["funded_papers"],
        y=top_cty_s["country"], orientation="h", name="No funding record",
        marker_color="lightgrey"), row=1, col=2)
    fig_b.update_layout(barmode="stack", height=550,
        title_text="Funding Coverage by Country (top 20 by paper count)")
    fig_b.update_yaxes(autorange="reversed", row=1, col=1)
    return fig_a, fig_b


def fig_departments(dept_df):
    top_d = dept_df[dept_df["department"].notna()].head(25).copy()
    top_d["label"] = top_d.apply(
        lambda r: f"{r['department']} ({r['institution'][:30]})", axis=1)
    top_d["cit_per_paper"] = (top_d["citations"] / top_d["papers"]).round(1)
    fig = go.Figure(go.Bar(x=top_d.sort_values("papers")["papers"],
        y=top_d.sort_values("papers")["label"], orientation="h",
        marker_color="mediumpurple",
        customdata=top_d.sort_values("papers")[["institution","country","citations","cit_per_paper"]].values,
        hovertemplate="<b>%{y}</b><br>Institution: %{customdata[0]}<br>"
                      "Country: %{customdata[1]}<br>Papers: %{x}<br>"
                      "Citations: %{customdata[2]:,}<br>Cit/paper: %{customdata[3]}<extra></extra>"))
    fig.update_layout(
        title="Top 25 Departments by Paper Count ⚠ Experimental — partial coverage",
        xaxis_title="Papers", height=650, yaxis=dict(autorange="reversed"),
        margin=dict(l=350))
    return fig


def fig_temporal(year_df, country_year_df, net_metrics_df):
    x = year_df["year"].values; y = year_df["papers"].values
    trend = np.poly1d(np.polyfit(x, y, 1))(x)
    fig_a = go.Figure()
    fig_a.add_trace(go.Bar(x=x, y=y, name="Papers published",
        marker_color="steelblue", opacity=0.85))
    fig_a.add_trace(go.Scatter(x=x, y=trend, mode="lines", name="Linear trend",
        line=dict(color="crimson", width=2, dash="dash")))
    fig_a.update_layout(title="UPF Publications per Year",
        xaxis_title="Year", yaxis_title="Papers published",
        template="plotly_white", bargap=0.25, legend=dict(x=0.02, y=0.98))

    TOP_N_C = 8
    top_c = (country_year_df.groupby("country")["papers"].sum()
             .sort_values(ascending=False).head(TOP_N_C).index.tolist())
    pivot = (country_year_df[country_year_df["country"].isin(top_c)]
             .pivot_table(index="year", columns="country", values="papers", fill_value=0)
             .reset_index())
    fig_b = go.Figure()
    for c in top_c:
        if c in pivot.columns:
            fig_b.add_trace(go.Scatter(x=pivot["year"], y=pivot[c],
                mode="lines+markers", name=c, line=dict(width=2)))
    fig_b.update_layout(title="Annual UPF Publications — Top 8 Countries",
        xaxis_title="Year", yaxis_title="Papers published", template="plotly_white",
        legend=dict(title="Country", x=1.01, y=0.99))

    fig_c = go.Figure()
    fig_c.add_trace(go.Scatter(x=net_metrics_df["year"],
        y=net_metrics_df["cumulative_authors"], mode="lines+markers",
        name="Authors (nodes)", line=dict(color="steelblue", width=2)))
    fig_c.add_trace(go.Scatter(x=net_metrics_df["year"],
        y=net_metrics_df["cumulative_edges"], mode="lines+markers",
        name="Collaborations (edges)", line=dict(color="tomato", width=2, dash="dot"),
        yaxis="y2"))
    fig_c.update_layout(title="Cumulative Growth of the UPF Co-authorship Network",
        xaxis_title="Year",
        yaxis=dict(title="Cumulative authors", side="left", showgrid=True),
        yaxis2=dict(title="Cumulative collaborations", side="right",
                    overlaying="y", showgrid=False),
        template="plotly_white", legend=dict(x=0.02, y=0.98))

    return fig_a, fig_b, fig_c


def fig_animated_network(G_lcc, G_plot, pos, edges_yr_df):
    START_YEAR = 2005
    known = set(pos.keys())
    palette = px.colors.qualitative.Alphabet
    node_color_map = {n: palette[G_plot.nodes[n].get("community", 0) % len(palette)]
                      for n in G_plot.nodes()}

    ey = edges_yr_df[
        edges_yr_df["author1_id"].isin(known) &
        edges_yr_df["author2_id"].isin(known) &
        (edges_yr_df["year"] >= START_YEAR)
    ].copy()

    if ey.empty:
        return go.Figure()

    anim_years = sorted(ey["year"].unique())
    pos_df = pd.DataFrame.from_dict(pos, orient="index", columns=["px", "py"])

    def _traces(cum_df):
        m = (cum_df
             .merge(pos_df.rename(columns={"px":"x1","py":"y1"}),
                    left_on="author1_id", right_index=True, how="inner")
             .merge(pos_df.rename(columns={"px":"x2","py":"y2"}),
                    left_on="author2_id", right_index=True, how="inner"))
        nans = np.full(len(m), np.nan)
        ex = np.stack([m["x1"].values, m["x2"].values, nans], axis=1).flatten()
        ey_c = np.stack([m["y1"].values, m["y2"].values, nans], axis=1).flatten()
        active = list(set(m["author1_id"].tolist()) | set(m["author2_id"].tolist()))
        return [
            go.Scatter(x=ex.tolist(), y=ey_c.tolist(), mode="lines",
                line=dict(width=0.4, color="rgba(140,140,140,0.2)"),
                hoverinfo="none", showlegend=False),
            go.Scatter(
                x=[pos[n][0] for n in active], y=[pos[n][1] for n in active],
                mode="markers",
                marker=dict(size=[max(4, min(14, 4+G_plot.degree(n))) if n in G_plot else 4
                                   for n in active],
                            color=[node_color_map.get(n, "#aaaaaa") for n in active],
                            line=dict(width=0.5, color="white")),
                hovertext=[f"{G_lcc.nodes[n].get('name','')} | "
                           f"{G_lcc.nodes[n].get('institution','')} | "
                           f"{G_lcc.nodes[n].get('country','')}"
                           if n in G_lcc else n for n in active],
                hoverinfo="text", showlegend=False),
        ]

    frames = [go.Frame(data=_traces(ey[ey["year"] <= y]), name=str(y))
              for y in anim_years]

    fig = go.Figure(
        data=_traces(ey[ey["year"] <= anim_years[0]]),
        frames=frames,
        layout=go.Layout(
            title=dict(text="UPF Co-authorship Network — cumulative growth (2005 → present)",
                       x=0.5, xanchor="center"),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            template="plotly_white", height=720, margin=dict(t=60, b=120),
            updatemenus=[dict(type="buttons", showactive=False,
                x=0.02, y=0.98, xanchor="left", yanchor="top",
                buttons=[
                    dict(label="▶ Play", method="animate",
                         args=[None, dict(frame=dict(duration=700, redraw=True),
                                          fromcurrent=True, transition=dict(duration=0))]),
                    dict(label="⏸ Pause", method="animate",
                         args=[[None], dict(frame=dict(duration=0, redraw=False), mode="immediate")]),
                ])],
            sliders=[dict(active=len(anim_years)-1, pad=dict(b=10, t=10),
                len=0.9, x=0.05, y=0,
                currentvalue=dict(prefix="Year: ", visible=True, xanchor="center",
                                   font=dict(size=14, color="#333")),
                transition=dict(duration=200),
                steps=[dict(args=[[str(y)], dict(frame=dict(duration=700, redraw=True),
                                                  mode="immediate")],
                             label=str(y), method="animate")
                       for y in anim_years])],
        ))
    print(f"  Animation: {len(anim_years)} frames")
    return fig


# ── §12 Study type figures ────────────────────────────────────────────────────

def fig_study_type_dist(st_df):
    st_plot = st_df.sort_values("papers", ascending=False).reset_index(drop=True)
    colors  = [ST_COLORS.get(t, "#aaa") for t in st_plot["study_type"]]

    fig = make_subplots(rows=1, cols=2,
        specs=[[{"type":"domain"}, {"type":"xy"}]],
        subplot_titles=("Proportion", "Paper count"))
    fig.add_trace(go.Pie(labels=st_plot["study_type"], values=st_plot["papers"],
        hole=0.42, marker=dict(colors=colors, line=dict(color="white", width=2)),
        textinfo="label+percent", textfont=dict(size=11), showlegend=False,
        hovertemplate="<b>%{label}</b><br>Papers: %{value:,}<br>%{percent}<extra></extra>"),
        row=1, col=1)
    fig.add_trace(go.Bar(y=st_plot["study_type"][::-1], x=st_plot["papers"][::-1],
        orientation="h", marker=dict(color=colors[::-1]),
        text=st_plot["papers"][::-1].apply(lambda v: f"{int(v):,}"),
        textposition="inside", insidetextanchor="end",
        textfont=dict(color="white", size=11), showlegend=False,
        hovertemplate="<b>%{y}</b><br>Papers: %{x:,}<extra></extra>"),
        row=1, col=2)
    fig.update_layout(title="§12a  Study type distribution", height=360,
        xaxis=dict(title="Papers", range=[0, int(st_plot["papers"].max())*1.08]),
        margin=dict(t=60, b=20, l=20, r=20),
        plot_bgcolor="white", paper_bgcolor="white")
    return fig


def fig_study_type_time(st_df, st_year_df):
    _yr = st_year_df[st_year_df["year"].between(2000, 2024)].copy()
    years = sorted(_yr["year"].unique())
    type_order = st_df.sort_values("papers", ascending=False)["study_type"].tolist()

    fig = go.Figure()
    for stype in type_order:
        sub = _yr[_yr["study_type"] == stype].set_index("year")
        vals = [int(sub.loc[y, "papers"]) if y in sub.index else 0 for y in years]
        fig.add_trace(go.Bar(name=stype, x=years, y=vals,
            marker_color=ST_COLORS.get(stype, "#aaa"),
            hovertemplate=f"<b>{stype}</b><br>Year: %{{x}}<br>Papers: %{{y:,}}<extra></extra>"))
    fig.update_layout(title="§12b  Study types over time (2000–2024)", barmode="stack",
        height=400, xaxis=dict(title="Year"), yaxis=dict(title="Papers published"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="white", paper_bgcolor="white", margin=dict(t=80, b=40, l=50, r=20))
    return fig


def fig_study_type_country(st_df, st_auth_df, authors_df):
    auth_ctr = authors_df[["author_id","country"]].drop_duplicates(subset="author_id")
    st_ctr   = st_auth_df.merge(auth_ctr, on="author_id", how="left")
    st_ctr   = st_ctr[st_ctr["country"].notna() & (st_ctr["country"] != "")]

    top_ctr = (st_ctr.groupby("country")["papers"].sum()
               .nlargest(15).index.tolist())
    pivot = (st_ctr[st_ctr["country"].isin(top_ctr)]
             .groupby(["country","study_type"])["papers"].sum().reset_index()
             .pivot(index="country", columns="study_type", values="papers").fillna(0))
    pivot_pct = pivot.div(pivot.sum(axis=1), axis=0) * 100
    pivot_pct = pivot_pct.loc[pivot.sum(axis=1).sort_values(ascending=False).index]

    present = [t for t in st_df.sort_values("papers", ascending=False)["study_type"].tolist()
               if t in pivot_pct.columns]
    fig = go.Figure()
    for stype in present:
        fig.add_trace(go.Bar(name=stype, x=pivot_pct.index, y=pivot_pct[stype].round(1),
            marker_color=ST_COLORS.get(stype, "#aaa"),
            hovertemplate=f"<b>{stype}</b><br>%{{x}}: %{{y:.1f}} %<extra></extra>"))
    fig.update_layout(
        title="§12c  Study type mix by country (top 15, % of author-attributed papers)",
        barmode="stack", height=420,
        xaxis=dict(title="Country (ISO code)"), yaxis=dict(title="%", range=[0,100]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="white", paper_bgcolor="white", margin=dict(t=80, b=40, l=50, r=20))
    return fig


def fig_study_type_network(G_lcc, G_plot, pos, st_auth_df):
    dom_st = {}
    for _, r in st_auth_df.iterrows():
        aid = r["author_id"]
        if pd.isna(aid):
            continue
        if aid not in dom_st or int(r["papers"]) > dom_st[aid]["papers"]:
            dom_st[aid] = {"study_type": r["study_type"], "papers": int(r["papers"])}

    node_ids = list(G_plot.nodes())
    cent_map = {n: {"study_type": dom_st.get(n, {}).get("study_type", "Other"),
                    "name": G_lcc.nodes[n].get("name","") if n in G_lcc else "",
                    "inst": G_lcc.nodes[n].get("institution","") if n in G_lcc else "",
                    "pap":  G_lcc.nodes[n].get("papers",0) if n in G_lcc else 0,
                    "cit":  G_lcc.nodes[n].get("citations",0) if n in G_lcc else 0,
                    "deg":  G_plot.degree(n)}
               for n in node_ids}

    ex, ey = [], []
    for u, v in G_plot.edges():
        ex += [pos[u][0], pos[v][0], None]; ey += [pos[u][1], pos[v][1], None]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines",
        line=dict(width=0.4, color="rgba(140,140,140,0.2)"),
        hoverinfo="none", showlegend=False))

    for stype in ST_COLORS:
        nx_, ny_, ns_, nt_ = [], [], [], []
        for n in node_ids:
            if cent_map[n]["study_type"] != stype:
                continue
            nx_.append(pos[n][0]); ny_.append(pos[n][1])
            ns_.append(max(5, min(20, 4 + cent_map[n]["deg"] * 0.2)))
            nt_.append(f"<b>{cent_map[n]['name']}</b><br>{cent_map[n]['inst']}<br>"
                       f"Study type: {stype}<br>Papers: {cent_map[n]['pap']} · "
                       f"Citations: {cent_map[n]['cit']:,}<extra></extra>")
        if not nx_:
            continue
        fig.add_trace(go.Scatter(name=stype, x=nx_, y=ny_, mode="markers",
            marker=dict(symbol=ST_SYMBOLS.get(stype,"circle"), size=ns_,
                        color=ST_COLORS[stype], opacity=0.85,
                        line=dict(width=0.6, color="white")),
            hovertemplate=nt_, showlegend=True))

    fig.update_layout(
        title="§12d  Co-authorship network — nodes coloured & shaped by dominant study type",
        height=680,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, scaleanchor="x"),
        hovermode="closest", plot_bgcolor="#f9fafc", paper_bgcolor="#f9fafc",
        legend=dict(title="Study type", x=0.01, y=0.99,
                    bgcolor="rgba(255,255,255,0.85)", bordercolor="#ddd", borderwidth=1,
                    font=dict(size=11)),
        margin=dict(t=60, b=10, l=10, r=10))
    return fig


# ── HTML assembly ─────────────────────────────────────────────────────────────

EXPLANATIONS = {
    "Top-N Institutions": (
        "Ranks institutions by the number of UPF papers. Toggle between "
        "<b>Papers</b> and <b>Citations</b>. Hover for full details."
    ),
    "Top-N Authors": (
        "Ranks individual authors by paper count or total citations. "
        "Toggle between metrics using the buttons above the chart."
    ),
    "Degree Distribution": (
        "Shows how many co-authors each researcher has. A long right tail and "
        "straight log–log plot suggest a scale-free network dominated by a few hubs."
    ),
    "Co-authorship Network": (
        "Each node is an author; edges connect co-authors. "
        "<b>Colour</b> = Louvain community; <b>size</b> = degree. "
        "Click a community in the legend to isolate it."
    ),
    "Degree vs Betweenness Centrality": (
        "<b>X</b> = degree (number of co-authors). "
        "<b>Y</b> = betweenness (how often an author lies on shortest paths between others — "
        "a measure of brokerage across the field). Top-right = hubs that also bridge communities."
    ),
    "Country Collaboration Heatmap": (
        "Co-authored papers between top country pairs. Darker = more collaboration."
    ),
    "Author Citation Impact": (
        "Papers vs citations on log scales. Top-right = prolific and highly cited."
    ),
    "Citations by Community": (
        "Total citations per Louvain community. Hover for papers and cit/paper."
    ),
    "Country Output vs Impact": (
        "Left: paper count (volume). Right: citations per paper (quality proxy). "
        "Rankings often diverge markedly between the two."
    ),
    "Top Funders": (
        "Paper count per funder (OpenAlex funding coverage is partial — "
        "treat as lower bounds)."
    ),
    "Funding Coverage by Country": (
        "% papers acknowledging funding (left) and funded vs no-record stacked counts (right)."
    ),
    "Departments (experimental)": (
        "<strong>⚠ Experimental — partial coverage.</strong> "
        "Department names extracted heuristically from author affiliation strings."
    ),
    "Publications per Year": (
        "Annual paper count with linear regression trend line."
    ),
    "Country Trends Over Time": (
        "Annual output for the top 8 countries. "
        "Brazil's steep rise from ~2010 reflects the NOVA classification uptake."
    ),
    "Network Growth Over Time": (
        "Cumulative authors (blue, left axis) and collaborations (red, right axis). "
        "Both accelerating together means the field is growing and densifying."
    ),
    "Network Animation": (
        "Co-authorship network built cumulatively year by year. "
        "Use the slider or ▶ Play button."
    ),
    "§12a Study Type Distribution": (
        "Overall breakdown by study design. MeSH publication-type tags take priority; "
        "title keywords are used when MeSH is absent. "
        "<em>Other</em> = unclassifiable with current data."
    ),
    "§12b Study Types Over Time": (
        "Annual paper counts stacked by study design (2000–2024). "
        "Watch whether RCTs and systematic reviews grow relative to observational work."
    ),
    "§12c Study Type Mix by Country": (
        "100 % stacked bar: each country's papers broken down by study design. "
        "Compare the RCT share across high-output countries."
    ),
    "§12d Study Type Network": (
        "Co-authorship network coloured and shaped by each author's dominant study type. "
        "Click legend entries to isolate a study type. "
        "Shapes: ● Observational · ◆ SR/MA · ■ Review · ▲ RCT · ✚ Clinical Trial · ○ Other."
    ),
}


def build_html(dashboard_figures, centrality_df, institutions_df, country_df,
               authors_df, dept_df, funders_df, G_lcc, communities):
    # Narrative stats
    _auth  = authors_df[authors_df["author_name"].notna() & (authors_df["author_name"] != "")]
    _inst  = institutions_df
    _ctr   = country_df
    _total = int(_ctr["papers"].sum())
    _top_c = _ctr.iloc[0];  _top_i = _inst.iloc[0]; _top_a = _auth.iloc[0]
    _top_a2 = _auth.iloc[1]
    _top5_pct = round(100 * _inst.head(5)["papers"].sum() / _total, 1)
    _top_c_pct = round(100 * int(_top_c["papers"]) / _total, 1)
    _top_i_pct = round(100 * int(_top_i["papers"]) / _total, 1)
    _n_nodes = G_lcc.number_of_nodes(); _n_edges = G_lcc.number_of_edges()
    _n_comm  = len(communities)
    _top_betw = centrality_df.sort_values("betweenness", ascending=False).iloc[0]
    _funder_note = ""
    if not funders_df.empty:
        _tf = funders_df.iloc[0]
        _funder_note = (f"The leading funder is <b>{_tf['funder_name']}</b> "
                        f"({int(_tf['papers']):,} papers).")

    REPORT = f"""
<div class="report">
  <h2>About this analysis</h2>
  <p>This dashboard summarises <b>{_total:,} papers</b> on ultra-processed food (UPF) retrieved
  from <a href="https://openalex.org" target="_blank">OpenAlex</a> using the NOVA classification
  search terms. The analysis covers <b>{len(_auth):,} unique authors</b> across
  <b>{len(_inst):,} institutions</b> in <b>{len(_ctr)} countries</b>.</p>

  <h2>Key findings</h2>
  <ul>
    <li><b>{_top_c["country"]} leads</b> with {int(_top_c["papers"]):,} papers
    ({_top_c_pct}% of total).</li>
    <li><b>{_top_i["institution"]}</b> is the most productive institution
    ({_top_i_pct}% of all papers, {int(_top_i["citations"]):,} citations).
    The top 5 institutions account for {_top5_pct}% of the literature.</li>
    <li><b>{_top_a["author_name"]}</b> is the most prolific author
    ({int(_top_a["papers"])} papers, {int(_top_a["citations"]):,} citations),
    followed by <b>{_top_a2["author_name"]}</b>
    ({int(_top_a2["papers"])} papers, {int(_top_a2["citations"]):,} citations).</li>
    <li>The co-authorship network contains <b>{_n_nodes:,} authors</b> and
    <b>{_n_edges:,} links</b> in <b>{_n_comm} communities</b>.
    The most central broker is <b>{_top_betw["name"]}</b>
    ({_top_betw["institution"]}).</li>
    {"<li>" + _funder_note + "</li>" if _funder_note else ""}
  </ul>

  <h2>Caveats</h2>
  <ul>
    <li>Author disambiguation is handled by OpenAlex; occasional mis-attribution may occur.</li>
    <li>Funding data is incomplete — many papers have no recorded funder.</li>
    <li>Books and grey literature are poorly indexed.</li>
    <li>Study-type classification uses MeSH tags where available, falling back to
    title keywords; unclassified papers appear as <em>Other</em>.</li>
  </ul>
</div>"""

    # Search data
    search_authors = (
        centrality_df[["name","institution","country","papers","citations",
                        "degree","betweenness","community_label"]]
        .fillna("").rename(columns={"name":"Author","institution":"Institution",
                                     "country":"Country","papers":"Papers",
                                     "citations":"Citations","degree":"Degree",
                                     "betweenness":"Betweenness",
                                     "community_label":"Community"})
        .sort_values("Papers", ascending=False).head(2000).to_dict(orient="records")
    )
    for r in search_authors:
        r["Betweenness"] = round(float(r["Betweenness"]), 4)
    search_inst = (
        institutions_df[["institution","country","papers","citations"]].fillna("")
        .rename(columns={"institution":"Institution","country":"Country",
                          "papers":"Papers","citations":"Citations"})
        .sort_values("Papers", ascending=False).head(1000).to_dict(orient="records")
    )
    search_dept = []
    if not dept_df.empty:
        search_dept = (
            dept_df.fillna("")
            .rename(columns={"department":"Department","institution":"Institution",
                              "country":"Country","papers":"Papers","citations":"Citations"})
            .sort_values("Papers", ascending=False).head(500).to_dict(orient="records")
        )

    CSS = """
body{font-family:Arial,Helvetica,sans-serif;max-width:1400px;margin:0 auto;
     padding:24px;background:#f4f6f9;color:#333}
h1{font-size:1.6rem;border-bottom:3px solid #2c7bb6;padding-bottom:8px}
h2{font-size:1.15rem;color:#2c7bb6;margin:40px 0 4px}
.report{background:#fff;border-radius:8px;padding:24px 28px;margin-bottom:32px;
        box-shadow:0 1px 5px rgba(0,0,0,.1)}
.report h2{margin-top:20px}
.report ul{padding-left:20px}
.report li{margin-bottom:6px}
.explanation{font-size:.92rem;color:#555;margin:0 0 10px;background:#eef4fb;
             border-left:3px solid #2c7bb6;padding:8px 12px;border-radius:0 4px 4px 0}
.chart{background:#fff;border-radius:8px;padding:12px;margin-bottom:32px;
       box-shadow:0 1px 5px rgba(0,0,0,.1)}
#search-section{background:#fff;border-radius:8px;padding:20px;margin-bottom:32px;
                box-shadow:0 1px 5px rgba(0,0,0,.1)}
#search-section h2{margin-top:0}
.search-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
#search-input{flex:1;min-width:200px;padding:8px 12px;font-size:.95rem;
              border:1px solid #ccc;border-radius:4px}
.tab-btn{padding:7px 18px;border:none;border-radius:4px;cursor:pointer;
         font-size:.9rem;background:#e0e8f0;color:#333}
.tab-btn.active{background:#2c7bb6;color:#fff}
.tab-btn.dept{background:#e8dff5}
.tab-btn.dept.active{background:#9b59b6}
#search-results{overflow-x:auto}
#search-results table{border-collapse:collapse;width:100%;font-size:.88rem}
#search-results th{background:#2c7bb6;color:#fff;padding:7px 10px;
                   text-align:left;white-space:nowrap}
.dept-header th{background:#9b59b6 !important}
#search-results td{padding:6px 10px;border-bottom:1px solid #eee}
#search-results tr:hover td{background:#f0f6ff}
#result-count{font-size:.85rem;color:#888;margin-bottom:6px}
footer{color:#999;font-size:.8rem;margin-top:40px;border-top:1px solid #ddd;padding-top:8px}"""

    SEARCH_JS = f"""
const AUTHORS={json.dumps(search_authors)};
const INSTS={json.dumps(search_inst)};
const DEPTS={json.dumps(search_dept)};
let mode='authors';
const input=document.getElementById('search-input');
const tbody=document.getElementById('result-body');
const thead=document.getElementById('result-head');
const counter=document.getElementById('result-count');
const btnA=document.getElementById('btn-authors');
const btnI=document.getElementById('btn-inst');
const btnD=document.getElementById('btn-dept');
function setMode(m){{mode=m;btnA.classList.toggle('active',m==='authors');
  btnI.classList.toggle('active',m==='inst');btnD.classList.toggle('active',m==='dept');
  input.placeholder={{authors:'Search by author name, institution or country…',
    inst:'Search by institution name or country…',
    dept:'Search by department, institution or country…'}}[m];render();}}
function render(){{
  const q=input.value.trim().toLowerCase();
  const src=mode==='authors'?AUTHORS:mode==='inst'?INSTS:DEPTS;
  const keys={{authors:['Author','Institution','Country'],
    inst:['Institution','Country'],dept:['Department','Institution','Country']}}[mode];
  const rows=q?src.filter(r=>keys.some(k=>String(r[k]).toLowerCase().includes(q))):src.slice(0,50);
  counter.textContent=q?rows.length+' result'+(rows.length!==1?'s':'')+' for "'+input.value+'"':
    'Showing top 50 of '+src.length.toLocaleString()+' — type to filter';
  thead.className=mode==='dept'?'dept-header':'';
  if(mode==='authors'){{
    thead.innerHTML='<tr><th>#</th><th>Author</th><th>Institution</th><th>Country</th><th>Papers</th><th>Citations</th><th>Degree</th><th>Betweenness</th><th>Community</th></tr>';
    tbody.innerHTML=rows.slice(0,200).map((r,i)=>`<tr><td>${{i+1}}</td><td><b>${{r.Author}}</b></td><td>${{r.Institution}}</td><td>${{r.Country}}</td><td>${{r.Papers}}</td><td>${{r.Citations.toLocaleString()}}</td><td>${{r.Degree}}</td><td>${{r.Betweenness}}</td><td>${{r.Community}}</td></tr>`).join('');
  }}else if(mode==='inst'){{
    thead.innerHTML='<tr><th>#</th><th>Institution</th><th>Country</th><th>Papers</th><th>Citations</th></tr>';
    tbody.innerHTML=rows.slice(0,200).map((r,i)=>`<tr><td>${{i+1}}</td><td><b>${{r.Institution}}</b></td><td>${{r.Country}}</td><td>${{r.Papers}}</td><td>${{r.Citations.toLocaleString()}}</td></tr>`).join('');
  }}else{{
    thead.innerHTML='<tr><th>#</th><th>Department</th><th>Institution</th><th>Country</th><th>Papers</th><th>Citations</th></tr>';
    tbody.innerHTML=rows.slice(0,200).map((r,i)=>`<tr><td>${{i+1}}</td><td><b>${{r.Department}}</b></td><td>${{r.Institution}}</td><td>${{r.Country}}</td><td>${{r.Papers}}</td><td>${{r.Citations.toLocaleString()}}</td></tr>`).join('');
  }}
}}
input.addEventListener('input',render);render();"""

    SEARCH_WIDGET = """
<div id="search-section">
  <h2>&#128269; Search Authors, Institutions &amp; Departments</h2>
  <div class="search-bar">
    <button class="tab-btn active" id="btn-authors" onclick="setMode('authors')">Authors</button>
    <button class="tab-btn"        id="btn-inst"    onclick="setMode('inst')">Institutions</button>
    <button class="tab-btn dept"   id="btn-dept"    onclick="setMode('dept')">Departments &#9888;</button>
    <input id="search-input" type="text"
           placeholder="Search by author name, institution or country…" />
  </div>
  <div id="result-count"></div>
  <div id="search-results">
    <table><thead id="result-head"></thead><tbody id="result-body"></tbody></table>
  </div>
</div>"""

    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    header = (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
              f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
              f'<title>UPF Bibliometrics — Interactive Dashboard</title>\n'
              f'<style>{CSS}</style>\n</head>\n<body>\n'
              f'{DISCLAIMER}\n'
              f'<h1>UPF Bibliometrics — Interactive Dashboard</h1>\n'
              f'<p>Generated: {generated_at} &nbsp;|&nbsp; '
              f'Source: <a href="https://openalex.org" target="_blank">OpenAlex</a></p>\n'
              f'{REPORT}\n{SEARCH_WIDGET}\n')
    footer = f'\n<script>{SEARCH_JS}</script>\n<footer>Generated with Plotly and the UPF bibliometrics pipeline.</footer>\n</body></html>'

    parts = [header]
    first = True
    for title, fig in dashboard_figures:
        expl = EXPLANATIONS.get(title, "")
        is_exp = "experimental" in title.lower()
        expl_cls = "explanation" + (" experimental" if is_exp else "")
        expl_html = f'<p class="{expl_cls}">{expl}</p>' if expl else ""
        chart_html = fig.to_html(full_html=False,
            include_plotlyjs="cdn" if first else False,
            config={"displayModeBar": True, "scrollZoom": False})
        parts.append(f'<h2>{title}</h2>\n{expl_html}\n<div class="chart">\n{chart_html}\n</div>')
        first = False
    parts.append(footer)
    return "\n".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    out  = args.output_dir

    (edges_df, authors_df, institutions_df, funders_df, funding_cty_df,
     dept_df, year_df, country_year_df, net_metrics_df, edges_yr_df,
     country_df, st_df, st_year_df, st_auth_df) = load_data(out)

    G, G_lcc, node_attrs = build_graph(edges_df, authors_df)
    centrality_df        = compute_centrality(G_lcc)
    communities, _       = compute_communities(G_lcc, centrality_df)
    G_plot, pos          = compute_layout(G_lcc)

    # Save centrality CSV
    cent_path = os.path.join(out, "author_centrality.csv")
    cols = ["author_id","name","institution","country","community",
            "papers","degree","degree_centrality","betweenness","pagerank","clustering"]
    centrality_df[cols].sort_values("betweenness", ascending=False).to_csv(cent_path, index=False)
    print(f"Saved → {cent_path}")

    # Build all figures
    print("Building figures…")
    fig_inst    = fig_institutions(institutions_df)
    fig_auth    = fig_authors(authors_df)
    fig_deg     = fig_degree_dist(G_lcc)
    fig_net     = fig_network_community(G_lcc, G_plot, pos)
    fig_scat    = fig_betweenness_scatter(centrality_df)
    fig_heat    = fig_country_heatmap(G_lcc)
    fig_cit, fig_comm_cit = fig_citation_impact(centrality_df)
    fig_cty     = fig_country_impact(country_df)
    fig_yr, fig_ctr_trend, fig_net_growth = fig_temporal(year_df, country_year_df, net_metrics_df)
    print("  Building animated network…")
    fig_anim    = fig_animated_network(G_lcc, G_plot, pos, edges_yr_df)

    dashboard_figs = [
        ("Top-N Institutions",              fig_inst),
        ("Top-N Authors",                   fig_auth),
        ("Degree Distribution",             fig_deg),
        ("Co-authorship Network",           fig_net),
        ("Degree vs Betweenness Centrality",fig_scat),
        ("Country Collaboration Heatmap",   fig_heat),
        ("Author Citation Impact",          fig_cit),
        ("Citations by Community",          fig_comm_cit),
        ("Country Output vs Impact",        fig_cty),
    ]

    if not funders_df.empty:
        fig_f, fig_fc = fig_funders(funders_df, funding_cty_df)
        dashboard_figs += [("Top Funders", fig_f), ("Funding Coverage by Country", fig_fc)]

    if not dept_df.empty:
        dashboard_figs.append(("Departments (experimental)", fig_departments(dept_df)))

    dashboard_figs += [
        ("Publications per Year",       fig_yr),
        ("Country Trends Over Time",    fig_ctr_trend),
        ("Network Growth Over Time",    fig_net_growth),
        ("Network Animation",           fig_anim),
    ]

    # §12 study type figures
    if st_df is not None:
        print("  Building study type figures…")
        dashboard_figs += [
            ("§12a Study Type Distribution",  fig_study_type_dist(st_df)),
            ("§12b Study Types Over Time",    fig_study_type_time(st_df, st_year_df)),
            ("§12c Study Type Mix by Country",fig_study_type_country(st_df, st_auth_df, authors_df)),
            ("§12d Study Type Network",       fig_study_type_network(G_lcc, G_plot, pos, st_auth_df)),
        ]

    print("Assembling HTML…")
    html = build_html(dashboard_figs, centrality_df, institutions_df, country_df,
                      authors_df, dept_df, funders_df, G_lcc, communities)

    dash_path = os.path.join(out, "upf_dashboard.html")
    with open(dash_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Saved → {dash_path}  ({os.path.getsize(dash_path)//1024} KB)")


if __name__ == "__main__":
    main()

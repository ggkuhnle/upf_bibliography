#!/usr/bin/env python3
"""
make_author_report.py
Generate a bespoke author profile: HTML (interactive) + LaTeX/PDF (print-ready).

Reads existing CSVs — no re-fetching needed.
LLM text via Ollama (local model, e.g. llama3.2); gracefully skipped if not running.

Usage:
  python make_author_report.py --author "Hollman"
  python make_author_report.py --author-id "https://openalex.org/A5021449136"
  python make_author_report.py --author "Crozier" --prefix flavonoid --top-coauthors 20
  python make_author_report.py --author "Schroeter" --no-llm
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import networkx as nx
import numpy as np
import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────

PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]
DEFAULT_DATA_BASE = "data"
OLLAMA_URL        = "http://localhost:11434/api/generate"
DEFAULT_LLM_MODEL = "llama3.1"

# ── LaTeX escaping ────────────────────────────────────────────────────────────

_LATEX_SPECIAL = {
    '\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
    '#':  r'\#', '_': r'\_', '{': r'\{', '}': r'\}',
    '~':  r'\textasciitilde{}', '^': r'\textasciicircum{}',
}

def _ltx(s):
    if s is None:
        return ""
    return re.sub(r'[\\&%$#_{}~^]', lambda m: _LATEX_SPECIAL[m.group()], str(s))


# ── Config ────────────────────────────────────────────────────────────────────

def _load_cfg(extra_dir=None):
    candidates = []
    if extra_dir:
        candidates.append(os.path.join(extra_dir, "config.json"))
    candidates += [os.path.join(os.path.dirname(__file__), "config.json"), "config.json"]
    for p in candidates:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return {}


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(data_dir, author_id=None):
    def _read(name, **kwargs):
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            return None
        df = pd.read_csv(path, low_memory=False, **kwargs)
        for col in df.columns:
            if "id" in col.lower():
                df[col] = df[col].astype(str)
        return df

    def _read_chunked(name, id_cols, filter_id=None, chunksize=200_000):
        """Read a large CSV in chunks, keeping only rows mentioning filter_id."""
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            return None
        kept = []
        for chunk in pd.read_csv(path, chunksize=chunksize, low_memory=False,
                                  dtype={c: str for c in id_cols}):
            if filter_id:
                mask = pd.Series([False] * len(chunk), index=chunk.index)
                for c in id_cols:
                    if c in chunk.columns:
                        mask |= chunk[c] == filter_id
                chunk = chunk[mask]
            if len(chunk):
                kept.append(chunk)
        return pd.concat(kept, ignore_index=True) if kept else pd.DataFrame()

    cit_id_cols = ["citing_author_id", "cited_author_id"]
    return {
        "authors":       _read("papers_by_author.csv"),
        "centrality":    _read("author_centrality.csv"),
        "coauth":        _read("coauthorship_edges.csv"),
        "cit_edges":     _read_chunked("citation_edges_author.csv",
                                       cit_id_cols, filter_id=author_id),
        "cit_papers":    _read_chunked("citation_edges_author_papers.csv",
                                       ["citing_author_id"], filter_id=author_id),
        "papers_detail": _read("papers_detail.csv"),
        "subclass":      _read("papers_by_author_subclass.csv"),
        "study_type":    _read("papers_by_author_study_type.csv"),
    }


# ── Author lookup ─────────────────────────────────────────────────────────────

def find_author(query, data):
    """Return (author_id, row) — picks highest-PageRank match if multiple."""
    base = data.get("centrality")
    if base is None:
        base = data.get("authors")
    if base is None:
        sys.exit("ERROR: no author data loaded.")

    if query.startswith("http"):
        matches = base[base["author_id"] == query]
    else:
        name_col = next((c for c in base.columns if "name" in c.lower()), None)
        if name_col is None:
            sys.exit("ERROR: no name column in author data.")
        matches = base[base[name_col].str.contains(query, case=False, na=False)]

    if len(matches) == 0:
        print(f"No author found matching '{query}'.")
        return None, None

    sort_col = next((c for c in ["pagerank", "papers", "citations"] if c in matches.columns), None)
    if sort_col:
        matches = matches.sort_values(sort_col, ascending=False)

    if len(matches) > 1:
        print(f"Found {len(matches)} matches for '{query}' — using top result. Others:")
        for _, row in matches.head(6).iloc[1:].iterrows():
            pr = f"  PR={row['pagerank']:.5f}" if "pagerank" in row else ""
            print(f"  {row.get('author_name','')}  ({row.get('institution','')}){pr}")
        print("  Use --author-id to specify exactly.\n")

    row = matches.iloc[0]
    return str(row["author_id"]), row


# ── Statistics + percentile ranks ─────────────────────────────────────────────

def author_stats(author_id, data, field_title):
    stats = {"author_id": author_id, "field": field_title}

    auth = data.get("authors")
    if auth is not None:
        r = auth[auth["author_id"] == author_id]
        if len(r):
            r = r.iloc[0]
            for col in ["author_name", "institution", "country", "papers", "citations",
                        "first_author_papers", "last_author_papers", "middle_author_papers"]:
                stats[col] = r.get(col, "")

    cent = data.get("centrality")
    if cent is not None:
        r = cent[cent["author_id"] == author_id]
        if len(r):
            r = r.iloc[0]
            for col in ["author_name", "pagerank", "betweenness", "indegree", "outdegree"]:
                if col in r.index:
                    stats[col] = float(r[col]) if col != "author_name" else r[col]
            total = len(cent)
            for metric in ["pagerank", "betweenness", "indegree", "outdegree"]:
                if metric in cent.columns:
                    val = float(stats.get(metric, 0) or 0)
                    pct = (pd.to_numeric(cent[metric], errors="coerce").fillna(0) < val).sum() / total * 100
                    stats[f"{metric}_pct"] = round(pct, 1)

    if auth is not None:
        total_a = len(auth)
        for metric in ["papers", "citations"]:
            if metric in auth.columns:
                val = float(stats.get(metric, 0) or 0)
                pct = (pd.to_numeric(auth[metric], errors="coerce").fillna(0) < val).sum() / total_a * 100
                stats[f"{metric}_pct"] = round(pct, 1)

    co = data.get("coauth")
    if co is not None:
        mask = (co["author1_id"] == author_id) | (co["author2_id"] == author_id)
        stats["n_coauthors"] = int(mask.sum())

    sub = data.get("subclass")
    if sub is not None and "author_id" in sub.columns:
        rows = sub[sub["author_id"] == author_id]
        if len(rows):
            stats["subclass_breakdown"] = (rows[["subclass", "papers"]]
                                           .sort_values("papers", ascending=False)
                                           .to_dict(orient="records"))

    stype = data.get("study_type")
    if stype is not None and "author_id" in stype.columns:
        rows = stype[stype["author_id"] == author_id]
        if len(rows):
            st = rows[["study_type", "papers"]].sort_values("papers", ascending=False).copy()
            st = st.rename(columns={"study_type": "Study type"})
            stats["study_type_breakdown"] = st.to_dict(orient="records")
    return stats


# ── Paper extraction ──────────────────────────────────────────────────────────

def get_papers(author_id, data, max_papers=20):
    """Return papers sorted by citation count (desc), then year (desc)."""
    cp = data.get("cit_papers")
    if cp is None:
        return []
    raw_titles: set = set()
    for ts in cp[cp["citing_author_id"] == author_id]["citing_titles"].dropna():
        for t in str(ts).split(";"):
            t = t.strip()
            if t and t != "nan":
                raw_titles.add(t[:120])

    pd_ = data.get("papers_detail")
    if pd_ is not None and len(raw_titles):
        pd_["_key"] = pd_["title"].str[:70].str.lower().str.strip()
        # Keep highest-citation row when titles collide at 70 chars
        key_map = (pd_.sort_values("citations", ascending=False)
                     .drop_duplicates(subset="_key")
                     .set_index("_key")[["title", "citations", "year"]]
                     .to_dict("index"))
        scored = []
        for rt in raw_titles:
            k = rt[:70].lower().strip()
            info = key_map.get(k)
            if info:
                scored.append((int(info.get("citations") or 0),
                               int(info.get("year") or 0),
                               str(info["title"])))
            else:
                scored.append((0, 0, rt))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [t for _, _, t in scored[:max_papers]]

    return sorted(raw_titles)[:max_papers]


# ── Ego graph builders ────────────────────────────────────────────────────────

def build_coauthor_ego(author_id, data, max_coauthors=25):
    co = data.get("coauth")
    if co is None:
        return nx.Graph()
    # Edges directly involving the focal author
    mask = (co["author1_id"] == author_id) | (co["author2_id"] == author_id)
    rows = co[mask].sort_values("shared_papers", ascending=False).head(max_coauthors)
    G = nx.Graph()
    G.add_node(author_id, role="focal")
    for _, row in rows.iterrows():
        a1, a2 = str(row["author1_id"]), str(row["author2_id"])
        G.add_node(a1, name=str(row.get("author1_name", "")),
                   institution=str(row.get("author1_institution", "")),
                   country=str(row.get("author1_country", "")))
        G.add_node(a2, name=str(row.get("author2_name", "")),
                   institution=str(row.get("author2_institution", "")),
                   country=str(row.get("author2_country", "")))
        G.add_edge(a1, a2, weight=int(row.get("shared_papers", 1)))
    # Also add edges between co-authors (depth-2 connections within the ego set)
    ego_set = set(G.nodes())
    mask2 = (co["author1_id"].isin(ego_set) & co["author2_id"].isin(ego_set))
    for _, row in co[mask2].iterrows():
        a1, a2 = str(row["author1_id"]), str(row["author2_id"])
        if not G.has_edge(a1, a2):
            G.add_edge(a1, a2, weight=int(row.get("shared_papers", 1)))
    return G


def build_citation_ego(author_id, data, max_each=20):
    ce = data.get("cit_edges")
    if ce is None:
        return nx.DiGraph()
    G = nx.DiGraph()
    G.add_node(author_id, role="focal")
    # Focal cites others (outgoing)
    out = ce[ce["citing_author_id"] == author_id].sort_values("citations", ascending=False).head(max_each)
    for _, row in out.iterrows():
        nid = str(row["cited_author_id"])
        G.add_node(nid, role="cited_by_focal",
                   name=str(row.get("cited_author_name", "")))
        G.add_edge(author_id, nid, weight=int(row.get("citations", 1)))
    # Others cite focal (incoming)
    inc = ce[ce["cited_author_id"] == author_id].sort_values("citations", ascending=False).head(max_each)
    for _, row in inc.iterrows():
        nid = str(row["citing_author_id"])
        if nid not in G:
            G.add_node(nid, role="cites_focal",
                       name=str(row.get("citing_author_name", "")))
        G.add_edge(nid, author_id, weight=int(row.get("citations", 1)))
    # Cross-edges within the ego set (e.g. cited authors also citing each other)
    ego_set = set(G.nodes())
    mask2 = (ce["citing_author_id"].isin(ego_set) & ce["cited_author_id"].isin(ego_set))
    for _, row in ce[mask2].iterrows():
        a1, a2 = str(row["citing_author_id"]), str(row["cited_author_id"])
        if not G.has_edge(a1, a2):
            G.add_edge(a1, a2, weight=int(row.get("citations", 1)))
    return G


# ── Name helpers ──────────────────────────────────────────────────────────────

def _short(name):
    parts = str(name).strip().split()
    return f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) >= 2 else name


def _name(G, nid):
    n = G.nodes.get(nid, {})
    name = n.get("name", "")
    return name if name and name not in ("nan", "None") else nid.split("/")[-1]


# ── Plotly helpers (HTML) ─────────────────────────────────────────────────────

def _plotly_coauthor_html(G, focal_id, title):
    try:
        import plotly.graph_objects as go
    except ImportError:
        return "<p><em>plotly not available</em></p>"
    if G.number_of_nodes() == 0:
        return "<p><em>No co-author data</em></p>"

    pos = nx.spring_layout(G, weight="weight", seed=42, k=1.5)
    w_max = max((G[u][v].get("weight", 1) for u, v in G.edges()), default=1)

    traces = []
    for u, v in G.edges():
        w  = G[u][v].get("weight", 1)
        t  = min(1.0, w / w_max)
        traces.append(go.Scatter(
            x=[pos[u][0], pos[v][0], None], y=[pos[u][1], pos[v][1], None],
            mode="lines",
            line=dict(width=0.5 + 2.5 * t, color=f"rgba(140,140,140,{0.25 + 0.5*t:.2f})"),
            hoverinfo="none", showlegend=False,
        ))

    others = [n for n in G.nodes() if n != focal_id]
    # Edge weight to focal for hover
    def _w(n):
        if G.has_edge(focal_id, n): return G[focal_id][n].get("weight", 1)
        if G.has_edge(n, focal_id): return G[n][focal_id].get("weight", 1)
        return "?"

    traces.append(go.Scatter(
        x=[pos[n][0] for n in others], y=[pos[n][1] for n in others],
        mode="markers+text",
        marker=dict(size=10, color="#636EFA", line=dict(width=1, color="white")),
        text=[_short(_name(G, n)) for n in others],
        textposition="top center", textfont=dict(size=8),
        hovertext=[f"{_name(G,n)}<br>{G.nodes[n].get('institution','')}<br>shared papers: {_w(n)}"
                   for n in others],
        hoverinfo="text", showlegend=False,
    ))
    traces.append(go.Scatter(
        x=[pos[focal_id][0]], y=[pos[focal_id][1]],
        mode="markers+text",
        marker=dict(size=20, color="#EF553B", symbol="star",
                    line=dict(width=2, color="white")),
        text=[_short(_name(G, focal_id))],
        textposition="top center", textfont=dict(size=11, color="#c0392b"),
        hoverinfo="text", hovertext=[_name(G, focal_id)], showlegend=False,
    ))

    fig = go.Figure(data=traces, layout=go.Layout(
        title=dict(text=title, font=dict(size=12)),
        showlegend=False, hovermode="closest",
        margin=dict(l=10, r=10, t=36, b=10), height=400,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        paper_bgcolor="white", plot_bgcolor="white",
    ))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _plotly_citation_ego_html(G, focal_id, title):
    try:
        import plotly.graph_objects as go
    except ImportError:
        return "<p><em>plotly not available</em></p>"
    if G.number_of_nodes() == 0:
        return "<p><em>No citation data</em></p>"

    G_undir = nx.Graph(G)
    pos = nx.spring_layout(G_undir, seed=42, k=1.5)

    traces = []
    for u, v in G.edges():
        color = "rgba(192,57,43,0.4)" if v == focal_id else "rgba(52,152,219,0.4)"
        traces.append(go.Scatter(
            x=[pos[u][0], pos[v][0], None], y=[pos[u][1], pos[v][1], None],
            mode="lines", line=dict(width=1, color=color),
            hoverinfo="none", showlegend=False,
        ))

    role_cfg = {
        "cited_by_focal": ("#00CC96", "Cites →"),
        "cites_focal":    ("#AB63FA", "← Cited by"),
    }
    for role, (color, label) in role_cfg.items():
        nodes = [n for n in G.nodes() if G.nodes[n].get("role") == role]
        if not nodes:
            continue
        w_key = lambda n: (G[focal_id][n]["weight"] if G.has_edge(focal_id, n)
                           else G[n][focal_id]["weight"] if G.has_edge(n, focal_id) else 0)
        traces.append(go.Scatter(
            x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes],
            mode="markers+text",
            marker=dict(size=10, color=color, line=dict(width=1, color="white")),
            text=[_short(_name(G, n)) for n in nodes],
            textposition="top center", textfont=dict(size=8),
            hovertext=[f"{_name(G,n)}<br>citations: {w_key(n)}" for n in nodes],
            hoverinfo="text", name=label,
        ))

    traces.append(go.Scatter(
        x=[pos[focal_id][0]], y=[pos[focal_id][1]],
        mode="markers+text",
        marker=dict(size=20, color="#EF553B", symbol="star",
                    line=dict(width=2, color="white")),
        text=[_short(_name(G, focal_id))],
        textposition="top center", textfont=dict(size=11, color="#c0392b"),
        hoverinfo="text", hovertext=[_name(G, focal_id)], name="Author",
    ))

    fig = go.Figure(data=traces, layout=go.Layout(
        title=dict(text=title, font=dict(size=12)),
        showlegend=True, legend=dict(x=0, y=1, bgcolor="rgba(255,255,255,0.85)"),
        hovermode="closest",
        margin=dict(l=10, r=10, t=36, b=10), height=400,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        paper_bgcolor="white", plot_bgcolor="white",
    ))
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ── Matplotlib figures (LaTeX PDF) ────────────────────────────────────────────

def _mpl_coauthor(G, focal_id, ax, title):
    ax.axis("off")
    ax.set_title(title, fontsize=8, pad=3)
    if G.number_of_nodes() == 0:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return
    pos = nx.spring_layout(G, weight="weight", seed=42, k=1.5)
    w_max = max((G[u][v].get("weight", 1) for u, v in G.edges()), default=1)
    for u, v in G.edges():
        t = min(1.0, G[u][v].get("weight", 1) / w_max)
        ax.plot([pos[u][0], pos[v][0]], [pos[u][1], pos[v][1]],
                color="#aaaaaa", alpha=0.2 + 0.45 * t, lw=0.3 + 1.2 * t, zorder=1)
    for nid in G.nodes():
        x, y = pos[nid]
        focal = (nid == focal_id)
        ax.scatter(x, y, s=180 if focal else 40, zorder=2,
                   c="#EF553B" if focal else "#636EFA",
                   marker="*" if focal else "o",
                   linewidths=0.6, edgecolors="white")
        ax.text(x, y + 0.055, _short(_name(G, nid)),
                fontsize=3.8 if not focal else 5.0,
                fontweight="bold" if focal else "normal",
                ha="center", va="bottom", zorder=3,
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.7))


def _mpl_citation_ego(G, focal_id, ax, title):
    ax.axis("off")
    ax.set_title(title, fontsize=8, pad=3)
    if G.number_of_nodes() == 0:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return
    G_u = nx.Graph(G)
    pos = nx.spring_layout(G_u, seed=42, k=1.5)
    for u, v in G.edges():
        color = "#c0392b" if v == focal_id else "#2980b9"
        ax.annotate("", xy=pos[v], xytext=pos[u],
                    arrowprops=dict(arrowstyle="-|>", color=color,
                                   lw=0.5, alpha=0.5, mutation_scale=7), zorder=1)
    role_c = {"focal": "#EF553B", "cited_by_focal": "#00CC96", "cites_focal": "#AB63FA"}
    for nid in G.nodes():
        x, y = pos[nid]
        focal = (nid == focal_id)
        c = role_c.get(G.nodes[nid].get("role", ""), "#636EFA")
        ax.scatter(x, y, s=180 if focal else 35, zorder=2, c=c,
                   marker="*" if focal else "o",
                   linewidths=0.6, edgecolors="white")
        ax.text(x, y + 0.055, _short(_name(G, nid)),
                fontsize=3.8 if not focal else 5.0,
                fontweight="bold" if focal else "normal",
                ha="center", va="bottom", zorder=3,
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.7))
    ax.legend(handles=[
        mpatches.Patch(color="#00CC96", label="Author cites →"),
        mpatches.Patch(color="#AB63FA", label="← Cites author"),
        mpatches.Patch(color="#EF553B", label="Author"),
    ], loc="lower right", fontsize=4, framealpha=0.85)


# ── LLM via Ollama ────────────────────────────────────────────────────────────

def generate_llm_text(stats, papers, model=DEFAULT_LLM_MODEL):
    """Call local Ollama. Returns text string or None if unavailable."""
    name    = stats.get("author_name", "this researcher")
    inst    = stats.get("institution", "unknown institution")
    country = stats.get("country", "")
    field   = stats.get("field", "this field")
    sub_str = ", ".join(f"{r['subclass']} ({r['papers']})"
                        for r in stats.get("subclass_breakdown", [])[:5])
    paper_str = "\n".join(f"  - {p}" for p in papers[:8]) or "  (not available)"

    def _pct(key):
        v = stats.get(key, "")
        return f"{v}%" if v != "" else "n/a"

    prompt = f"""Write a concise academic profile (2–3 paragraphs) for an author report.
Be factual; use only the data provided; third person; no invented details.

Author: {name}
Institution: {inst}{(', ' + country) if country else ''}
Field: {field}
Papers: {stats.get('papers','?')}  Citations: {stats.get('citations','?')}
Network influence — PageRank percentile: {_pct('pagerank_pct')}
Network bridging — betweenness percentile: {_pct('betweenness_pct')}
Citation in-degree percentile: {_pct('indegree_pct')}
Co-authors: {stats.get('n_coauthors','?')}
{('Research areas: ' + sub_str) if sub_str else ''}

Sample papers:
{paper_str}"""

    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.25, "num_predict": 450},
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read()).get("response", "").strip()
    except (urllib.error.URLError, OSError):
        return None


# ── HTML report ───────────────────────────────────────────────────────────────

def write_html(stats, papers, coauth_G, cit_G, output_path, llm_text=None):
    author_name = stats.get("author_name", "Unknown Author")
    inst    = stats.get("institution", "")
    country = stats.get("country", "")
    field   = stats.get("field", "")
    aid     = stats.get("author_id", "")

    coauth_fig = _plotly_coauthor_html(
        coauth_G, aid, f"Co-author Network — {_short(author_name)}")
    cit_fig = _plotly_citation_ego_html(
        cit_G, aid, f"Citation Ego Network — {_short(author_name)}")

    metrics = [
        ("Papers",                  stats.get("papers", ""),          stats.get("papers_pct", "")),
        ("Citations",               stats.get("citations", ""),       stats.get("citations_pct", "")),
        ("PageRank",                f"{stats.get('pagerank',0):.5f}", stats.get("pagerank_pct", "")),
        ("Betweenness centrality",  f"{stats.get('betweenness',0):.5f}", stats.get("betweenness_pct", "")),
        ("In-degree (cited by)",    stats.get("indegree", ""),        stats.get("indegree_pct", "")),
        ("Out-degree (cites)",      stats.get("outdegree", ""),       stats.get("outdegree_pct", "")),
        ("Co-authors",              stats.get("n_coauthors", ""),     ""),
        ("First-author papers",     stats.get("first_author_papers", ""), ""),
        ("Last-author papers",      stats.get("last_author_papers", ""),  ""),
    ]

    def _bar(pct):
        if pct == "":
            return "—"
        p = float(pct)
        c = "#27ae60" if p >= 75 else "#e67e22" if p >= 50 else "#e74c3c"
        bar = f'<span style="display:inline-block;width:{p*0.55:.0f}px;height:7px;background:{c};border-radius:3px;margin-right:5px;vertical-align:middle"></span>'
        return f'{bar}{p:.1f} %'

    metric_rows = "\n".join(
        f"<tr><td>{m}</td><td class='num'>{v}</td><td>{_bar(p)}</td></tr>"
        for m, v, p in metrics if v not in ("", None, "nan")
    )

    def _mini_table(key, col1, col2):
        rows_data = stats.get(key, [])
        if not rows_data:
            return ""
        rows_html = "".join(
            f"<tr><td>{r[col1]}</td><td class='num'>{r[col2]}</td></tr>"
            for r in rows_data)
        return (f"<table class='mini'><tr><th>{col1.capitalize()}</th>"
                f"<th>{col2.capitalize()}</th></tr>{rows_html}</table>")

    sub_html   = _mini_table("subclass_breakdown",   "subclass",   "papers")
    stype_html = _mini_table("study_type_breakdown", "Study type", "papers")

    papers_html = ""
    if papers:
        items = "".join(f"<li>{p}</li>" for p in papers[:15])
        papers_html = f"<h2>Sample papers</h2><ul class='papers'>{items}</ul>"

    llm_html = ""
    if llm_text:
        paras = "".join(f"<p>{p.strip()}</p>"
                        for p in llm_text.split("\n\n") if p.strip())
        llm_html = f"""<section class="profile-box">
<h2>Profile <span class="llm-tag">AI-generated · verify before use</span></h2>
<div class="profile-text">{paras}</div>
</section>"""

    oa_link = f'<a href="{aid}" target="_blank">OpenAlex</a>' if aid.startswith("http") else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{author_name} — Author Report</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
     background:#f0f2f5;color:#222;padding:20px}}
.page{{max-width:1080px;margin:0 auto}}
h1{{font-size:1.8rem;font-weight:700;margin-bottom:.15em}}
.sub{{color:#666;font-size:.9rem;margin-bottom:1em}}
.chips{{display:flex;gap:.7em;flex-wrap:wrap;margin-bottom:1.2em}}
.chip{{background:#e8edf2;border-radius:20px;padding:3px 12px;
       font-size:.8rem;color:#445}}
h2{{font-size:1rem;font-weight:600;color:#2c3e50;border-bottom:2px solid #3498db;
    padding-bottom:3px;margin:1.4em 0 .6em}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:.8em 0}}
.card{{background:white;border-radius:8px;padding:16px;
       box-shadow:0 1px 4px rgba(0,0,0,.08)}}
table{{border-collapse:collapse;width:100%}}
th{{background:#f0f4f8;padding:6px 10px;font-size:.78rem;text-align:left}}
td{{padding:5px 10px;font-size:.83rem;border-bottom:1px solid #eee}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mini th,.mini td{{font-size:.78rem;padding:3px 8px}}
.papers{{font-size:.8rem;line-height:1.8;padding-left:1.2em;color:#333}}
.profile-box{{background:#fff8f0;border-left:4px solid #e67e22;
              border-radius:4px;padding:14px 20px;margin:1em 0}}
.profile-text p{{margin:.6em 0;line-height:1.7;font-size:.88rem}}
.llm-tag{{font-size:.7rem;font-weight:400;color:#e67e22;margin-left:.5em}}
footer{{margin-top:2em;font-size:.72rem;color:#aaa;
        border-top:1px solid #ddd;padding-top:.8em}}
</style>
</head>
<body>
<div class="page">

<h1>{author_name}</h1>
<p class="sub">{field} · Author report</p>
<div class="chips">
  {'<span class="chip">🏛 ' + inst + '</span>' if inst else ''}
  {'<span class="chip">🌍 ' + country + '</span>' if country else ''}
  {'<span class="chip">📄 ' + str(stats.get('papers','')) + ' papers</span>' if stats.get('papers') else ''}
  {'<span class="chip">🔖 ' + str(stats.get('citations','')) + ' citations</span>' if stats.get('citations') else ''}
  {('<span class="chip">' + oa_link + '</span>') if oa_link else ''}
</div>

{llm_html}

<h2>Metrics &amp; field ranks</h2>
<div class="card" style="max-width:560px">
<table>
<tr><th>Metric</th><th>Value</th><th>Field percentile</th></tr>
{metric_rows}
</table>
<p style="font-size:.7rem;color:#aaa;margin-top:8px">
Percentile: share of authors in the {field} corpus scoring below this author.
Betweenness is computed for top-ranked authors only.
</p>
</div>

<h2>Networks</h2>
<div class="grid2">
  <div class="card">{coauth_fig}</div>
  <div class="card">{cit_fig}</div>
</div>

<h2>Research breakdown</h2>
<div class="grid2">
  {'<div class="card"><h3 style="font-size:.85rem;margin-bottom:.5em">Subclass</h3>' + sub_html + '</div>' if sub_html else ''}
  {'<div class="card"><h3 style="font-size:.85rem;margin-bottom:.5em">Study type</h3>' + _mini_table("study_type_breakdown", "Study type", "papers") + '</div>' if stats.get("study_type_breakdown") else ''}
</div>

{('<div class="card">' + papers_html + '</div>') if papers_html else ''}

<footer>
  Data: <a href="https://openalex.org">OpenAlex</a> ·
  Metrics computed within the {field} corpus ·
  Generated {__import__('datetime').date.today().isoformat()}
</footer>
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML  → {output_path}")


# ── LaTeX / PDF report ────────────────────────────────────────────────────────

def write_latex(stats, papers, coauth_G, cit_G, out_dir, llm_text=None):
    """Write .tex + network figure PDF. Returns path to .tex file."""
    os.makedirs(out_dir, exist_ok=True)

    author_name = stats.get("author_name", "Unknown")
    aid     = stats.get("author_id", "")
    inst    = stats.get("institution", "")
    country = stats.get("country", "")
    field   = stats.get("field", "")

    # Network figure (saved as PDF for vector quality)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))
    _mpl_coauthor(coauth_G, aid, ax1, f"Co-author Network — {_short(author_name)}")
    _mpl_citation_ego(cit_G, aid, ax2, f"Citation Ego Network — {_short(author_name)}")
    plt.tight_layout(pad=1.5)
    net_fig_path = os.path.join(out_dir, "networks.pdf")
    fig.savefig(net_fig_path, format="pdf", bbox_inches="tight")
    plt.close()
    print(f"  Network fig → {net_fig_path}")

    # Metrics table
    metrics = [
        ("Papers",                     stats.get("papers","--"),              stats.get("papers_pct","")),
        ("Citations",                  stats.get("citations","--"),           stats.get("citations_pct","")),
        ("PageRank",                   f"{stats.get('pagerank',0):.5f}",      stats.get("pagerank_pct","")),
        ("Betweenness centrality",     f"{stats.get('betweenness',0):.5f}",   stats.get("betweenness_pct","")),
        ("In-degree (cited by)",       stats.get("indegree","--"),            stats.get("indegree_pct","")),
        ("Out-degree (cites others)",  stats.get("outdegree","--"),           stats.get("outdegree_pct","")),
        ("Co-authors",                 stats.get("n_coauthors","--"),         ""),
        ("First-author papers",        stats.get("first_author_papers","--"), ""),
        ("Last-author papers",         stats.get("last_author_papers","--"),  ""),
    ]
    def _metric_row(m, v, p):
        pct_str = (_ltx(str(p)) + r"\,\%") if str(p) not in ("", "None") else "---"
        return rf"    {_ltx(m)} & {_ltx(str(v))} & {pct_str} \\"
    metric_rows = "\n".join(
        _metric_row(m, v, p)
        for m, v, p in metrics if str(v) not in ("", "nan", "None")
    )

    def _small_table(key, col1, col2, caption):
        rows_data = stats.get(key, [])
        if not rows_data:
            return ""
        body = "\n".join(
            rf"    {_ltx(str(r[col1]))} & {r[col2]} \\"
            for r in rows_data)
        return rf"""
\subsection*{{{_ltx(caption)}}}
\begin{{tabular}}{{lr}}
\toprule
{_ltx(col1.capitalize())} & {_ltx(col2.capitalize())} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
"""

    sub_tex   = _small_table("subclass_breakdown",   "subclass",   "papers", "Research areas (subclass)")
    stype_tex = _small_table("study_type_breakdown", "Study type", "papers", "Study types")

    papers_tex = ""
    if papers:
        items = "\n".join(r"  \item " + _ltx(p) for p in papers[:15])
        papers_tex = rf"""
\section*{{Sample Papers}}
\begin{{itemize}}\setlength\itemsep{{2pt}}
{items}
\end{{itemize}}"""

    llm_tex = ""
    if llm_text:
        paras = "\n\n".join(_ltx(p.strip()) for p in llm_text.split("\n\n") if p.strip())
        llm_tex = rf"""
\section*{{Profile}}
\begin{{quote}}
\itshape
{paras}
\end{{quote}}
{{\small\textcolor{{gray}}{{Generated by a local language model via Ollama. Verify before use.}}}}
\bigskip"""

    oa_url = aid if aid.startswith("http") else ""
    oa_url_tex = (r"{\small\url{" + oa_url + r"}}") if oa_url else ""

    tex = rf"""\documentclass[a4paper,11pt]{{article}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{lmodern}}
\usepackage{{microtype}}
\usepackage[top=2.5cm,bottom=2.5cm,left=2.5cm,right=2.5cm]{{geometry}}
\usepackage{{booktabs}}
\usepackage{{graphicx}}
\usepackage{{xcolor}}
\usepackage{{array}}
\usepackage[colorlinks=true,urlcolor=blue,linkcolor=blue]{{hyperref}}
\usepackage{{fancyhdr}}
\usepackage{{caption}}
\usepackage{{amssymb}}
\usepackage{{pdflscape}}

\definecolor{{accent}}{{RGB}}{{52,152,219}}

\pagestyle{{fancy}}\fancyhf{{}}
\lhead{{\small\itshape {_ltx(author_name)}}}
\rhead{{\small\itshape {_ltx(field)} --- Author Report}}
\rfoot{{\small\thepage}}
\renewcommand{{\headrulewidth}}{{0.4pt}}

\begin{{document}}

%% ── Title block ──────────────────────────────────────────────────────────────
\begin{{center}}
  {{\LARGE\bfseries {_ltx(author_name)}}}\\[0.4em]
  {{\large\color{{accent}} {_ltx(field)}}}\\[0.25em]
  {{\normalsize {_ltx(inst)}{(', ' + _ltx(country)) if country else ''}}}\\[0.15em]
  {oa_url_tex}
\end{{center}}
\vspace{{0.4em}}\noindent\rule{{\linewidth}}{{0.5pt}}\vspace{{0.4em}}

{llm_tex}

%% ── Metrics ──────────────────────────────────────────────────────────────────
\section*{{Network Metrics and Field Ranks}}

\begin{{tabular}}{{p{{6.5cm}}rr}}
\toprule
Metric & Value & Percentile\textsuperscript{{$\dagger$}} \\
\midrule
{metric_rows}
\bottomrule
\end{{tabular}}

\smallskip
{{\footnotesize $\dagger$\,Proportion of authors in the {_ltx(field)} corpus scoring below
this author. Betweenness is approximated for top-ranked authors only.}}

%% ── Research breakdown ───────────────────────────────────────────────────────
\section*{{Research Breakdown}}

\noindent
\begin{{minipage}}[t]{{0.46\linewidth}}
{sub_tex}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.46\linewidth}}
{stype_tex}
\end{{minipage}}

{papers_tex}

%% ── Network figures ──────────────────────────────────────────────────────────
\begin{{landscape}}
\section*{{Networks}}

\noindent\includegraphics[width=\linewidth]{{networks.pdf}}

\noindent{{\small
  \textbf{{Left:}} Co-author ego network. Edge width $\propto$ shared papers.
  \textbf{{Right:}} Citation ego network.
  \textcolor[RGB]{{0,204,150}}{{Green}} = authors this author cites;\;
  \textcolor[RGB]{{171,99,250}}{{Purple}} = authors who cite this author.\;
  $\bigstar$ = focal author.
}}
\end{{landscape}}

\vfill
\noindent\rule{{\linewidth}}{{0.4pt}}\\
{{\small Data source: \href{{https://openalex.org}}{{OpenAlex}}.
Metrics are computed within the {_ltx(field)} corpus and reflect
within-field standing only.
Generated \today.}}

\end{{document}}
"""

    tex_path = os.path.join(out_dir, "author_report.tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex)
    print(f"LaTeX → {tex_path}")
    return tex_path


def compile_latex(tex_path):
    tex_dir = os.path.dirname(os.path.abspath(tex_path))
    tex_name = os.path.basename(tex_path)
    for compiler in ["pdflatex", "xelatex"]:
        try:
            for _ in range(2):   # two passes for cross-references
                subprocess.run(
                    [compiler, "-interaction=nonstopmode", tex_name],
                    cwd=tex_dir, capture_output=True, timeout=120,
                )
            pdf = tex_path.replace(".tex", ".pdf")
            if os.path.exists(pdf):
                print(f"PDF   → {pdf}")
                return pdf
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            print("WARNING: LaTeX compilation timed out.")
            return None
    print("WARNING: pdflatex/xelatex not found.")
    print(f"         Install MacTeX or BasicTeX, then run: pdflatex {tex_path}")
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    # Pre-parse --data-dir so we can load a dataset-local config.json for defaults
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--data-dir", default=None)
    _pre.add_argument("--prefix",   default=None)
    _pre_args, _ = _pre.parse_known_args()
    _data_dir_hint = _pre_args.data_dir or (
        os.path.join(DEFAULT_DATA_BASE, _pre_args.prefix) if _pre_args.prefix else None)
    cfg = _load_cfg(_data_dir_hint)

    ap = argparse.ArgumentParser(description="Bespoke author report (HTML + LaTeX/PDF)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--author",    help="Author name (substring search)")
    grp.add_argument("--author-id", dest="author_id", help="OpenAlex author ID")
    ap.add_argument("--prefix",        default=cfg.get("prefix", ""))
    ap.add_argument("--data-dir",      default=None)
    ap.add_argument("--title",         default=cfg.get("title", "Research Field"),
                    help="Field label for the report")
    ap.add_argument("--output-dir",    default=None,
                    help="Output directory (default: reports/<prefix>/<author_slug>/)")
    ap.add_argument("--top-coauthors", type=int, default=25)
    ap.add_argument("--top-cited",     type=int, default=20,
                    help="Max nodes in each direction of citation ego graph")
    ap.add_argument("--top-papers",    type=int, default=20)
    ap.add_argument("--llm-model",     default=DEFAULT_LLM_MODEL,
                    help=f"Ollama model (default: {DEFAULT_LLM_MODEL})")
    ap.add_argument("--no-llm",        action="store_true")
    args = ap.parse_args()

    data_dir = (args.data_dir or
                (os.path.join(DEFAULT_DATA_BASE, args.prefix) if args.prefix
                 else DEFAULT_DATA_BASE))

    # Phase 1: load small index files to resolve the author ID
    print(f"Loading data from {data_dir} …")
    data = load_data(data_dir, author_id=None)

    query = args.author_id or args.author
    author_id, _ = find_author(query, data)
    if not author_id:
        sys.exit(1)

    # Phase 2: reload large citation files filtered to this author only
    print(f"Loading citation data for {author_id.split('/')[-1]} …")
    cit_data = load_data(data_dir, author_id=author_id)
    data["cit_edges"]  = cit_data["cit_edges"]
    data["cit_papers"] = cit_data["cit_papers"]

    stats   = author_stats(author_id, data, args.title)
    name    = stats.get("author_name", author_id.split("/")[-1])
    print(f"  {name}  |  papers={stats.get('papers','?')}  "
          f"citations={stats.get('citations','?')}  "
          f"PR={stats.get('pagerank',0):.5f}")

    print("Building ego graphs …")
    coauth_G = build_coauthor_ego(author_id, data, args.top_coauthors)
    cit_G    = build_citation_ego(author_id, data, args.top_cited)
    print(f"  co-author: {coauth_G.number_of_nodes()} nodes  "
          f"citation: {cit_G.number_of_nodes()} nodes")

    papers = get_papers(author_id, data, args.top_papers)
    print(f"  papers extracted: {len(papers)}")

    llm_text = None
    if not args.no_llm:
        print(f"Generating LLM profile ({args.llm_model}) …")
        llm_text = generate_llm_text(stats, papers, model=args.llm_model)
        if llm_text:
            print(f"  {len(llm_text)} chars")
        else:
            print("  Ollama unavailable — skipping")

    # Resolve output directory
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    out_dir = args.output_dir or os.path.join(
        "reports", args.prefix or "default", slug)
    os.makedirs(out_dir, exist_ok=True)

    write_html(stats, papers, coauth_G, cit_G,
               os.path.join(out_dir, "report.html"), llm_text=llm_text)
    tex_path = write_latex(stats, papers, coauth_G, cit_G,
                           out_dir, llm_text=llm_text)
    compile_latex(tex_path)

    print(f"\nOutput: {out_dir}/")


if __name__ == "__main__":
    main()

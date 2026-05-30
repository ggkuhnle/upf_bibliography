#!/usr/bin/env python3
"""
make_network_pdf.py
Generate a static, print-ready PDF of the citation network.

Reads (from --data-dir / config.json prefix):
  author_centrality.csv      (pagerank, betweenness, indegree, outdegree)
  citation_edges_author.csv  (citing_author_id, cited_author_id, citations)
  papers_by_author.csv       (author_id, author_name, papers, citations)  [optional]

Writes:
  <output>.pdf   (default: <prefix>_citation_network.pdf)

Usage examples:
  python make_network_pdf.py
  python make_network_pdf.py --prefix flavonoid --top-nodes 400 --top-labels 40
  python make_network_pdf.py --data-dir data/upf --prefix upf --output upf_net.pdf
"""

import argparse
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import networkx as nx
import pandas as pd
import numpy as np


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_cfg():
    for p in [os.path.join(os.path.dirname(__file__), "config.json"), "config.json"]:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return {}


PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
    "#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD",
    "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF",
]

DEFAULT_DATA_BASE = "data"


# ── Community detection ───────────────────────────────────────────────────────

def _partition(G_undir):
    try:
        from community import best_partition
        return best_partition(G_undir)
    except ImportError:
        import networkx.algorithms.community as nx_comm
        comms = nx_comm.greedy_modularity_communities(G_undir)
        p = {}
        for i, c in enumerate(comms):
            for n in c:
                p[n] = i
        return p


# ── Main ──────────────────────────────────────────────────────────────────────

def build_graph(data_dir, prefix, top_nodes):
    """Return (G_dir, centrality_df, name_lookup) filtered to top_nodes by PageRank."""
    pf = f"{prefix}_" if prefix else ""

    cent_path   = os.path.join(data_dir, f"{pf}author_centrality.csv"   if prefix else "author_centrality.csv")
    edges_path  = os.path.join(data_dir, f"{pf}citation_edges_author.csv" if prefix else "citation_edges_author.csv")
    author_path = os.path.join(data_dir, f"{pf}papers_by_author.csv"    if prefix else "papers_by_author.csv")

    # Try without prefix too (data/<prefix>/ layout has no prefix in filename)
    for candidate in [cent_path, os.path.join(data_dir, "author_centrality.csv")]:
        if os.path.exists(candidate):
            cent_path = candidate
            break
    for candidate in [edges_path, os.path.join(data_dir, "citation_edges_author.csv")]:
        if os.path.exists(candidate):
            edges_path = candidate
            break
    for candidate in [author_path, os.path.join(data_dir, "papers_by_author.csv")]:
        if os.path.exists(candidate):
            author_path = candidate
            break

    if not os.path.exists(cent_path):
        sys.exit(f"ERROR: centrality file not found: {cent_path}")
    if not os.path.exists(edges_path):
        sys.exit(f"ERROR: edges file not found: {edges_path}")

    cent = pd.read_csv(cent_path)
    edges = pd.read_csv(edges_path)

    # Name lookup from papers_by_author if available (richer names)
    name_lookup = {}
    if os.path.exists(author_path):
        ab = pd.read_csv(author_path)
        if "author_id" in ab.columns and "author_name" in ab.columns:
            name_lookup = dict(zip(ab["author_id"].astype(str), ab["author_name"].fillna("").astype(str)))

    # Fall back to centrality names
    if "author_id" in cent.columns and "author_name" in cent.columns:
        for _, r in cent.iterrows():
            aid = str(r["author_id"])
            if aid not in name_lookup or not name_lookup[aid]:
                name_lookup[aid] = str(r.get("author_name", ""))

    # Select top nodes by PageRank
    if "pagerank" not in cent.columns:
        sys.exit("ERROR: author_centrality.csv has no 'pagerank' column.")
    cent = cent.sort_values("pagerank", ascending=False).head(top_nodes)
    keep = set(cent["author_id"].astype(str))

    # Build directed graph
    G = nx.DiGraph()
    for aid in keep:
        G.add_node(aid)

    w_max = int(edges["citations"].max()) if len(edges) else 1
    for _, row in edges.iterrows():
        u = str(row["citing_author_id"])
        v = str(row["cited_author_id"])
        if u in keep and v in keep and u != v:
            w = int(row["citations"])
            if G.has_edge(u, v):
                G[u][v]["weight"] += w
            else:
                G.add_edge(u, v, weight=w)

    return G, cent, name_lookup, w_max


def _short_name(full):
    parts = full.strip().split()
    if len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    return full


def make_pdf(data_dir, prefix, top_nodes, top_labels, output_path, title,
             paper_size, seed, top_edges_per_node=6):
    print(f"Loading data from {data_dir} …")
    G, cent, name_lookup, w_max = build_graph(data_dir, prefix, top_nodes)

    # Keep only top-K outgoing edges per node (reduces hairball, mirrors Cytoscape view)
    top_k = max(3, min(8, top_edges_per_node))
    keep_edges = set()
    out_by_node: dict = {}
    for u, v, d in G.edges(data=True):
        out_by_node.setdefault(u, []).append((d.get("weight", 1), v))
    for u, nbrs in out_by_node.items():
        for _, v in sorted(nbrs, reverse=True)[:top_k]:
            keep_edges.add((u, v))
    remove_edges = [(u, v) for u, v in G.edges() if (u, v) not in keep_edges]
    G.remove_edges_from(remove_edges)

    # Drop isolated nodes — they scatter randomly and obscure the main graph
    isolated = [n for n in G.nodes() if G.degree(n) == 0]
    G.remove_nodes_from(isolated)
    if isolated:
        print(f"  Removed {len(isolated)} isolated nodes (no edges to other top nodes)")

    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()
    print(f"  {n_nodes} nodes, {n_edges} edges after filtering")

    # PageRank map
    pr_map = dict(zip(cent["author_id"].astype(str), cent["pagerank"].astype(float)))

    # Community detection on undirected version
    print("Detecting communities …")
    G_undir = nx.Graph(G)
    partition = _partition(G_undir)

    # Sort communities by size (largest = community 0 in palette)
    comm_sizes = {}
    for n, c in partition.items():
        comm_sizes[c] = comm_sizes.get(c, 0) + 1
    sorted_comms = sorted(comm_sizes, key=lambda c: -comm_sizes[c])
    comm_remap = {old: new for new, old in enumerate(sorted_comms)}
    partition = {n: comm_remap[c] for n, c in partition.items()}

    # Layout — kamada_kawai gives better separation than spring for citation nets
    if n_nodes <= 400:
        print(f"Computing Kamada-Kawai layout for {n_nodes} nodes …")
        pos = nx.kamada_kawai_layout(G_undir)
    else:
        print(f"Computing spring layout for {n_nodes} nodes (seed={seed}) …")
        pos = nx.spring_layout(G_undir, seed=seed, k=2.0 / max(1, n_nodes ** 0.5),
                               iterations=100)

    # Radial stretch — push central nodes outward (power transform, exponent < 1)
    pos_cx = np.mean([p[0] for p in pos.values()])
    pos_cy = np.mean([p[1] for p in pos.values()])
    radii  = [((p[0]-pos_cx)**2 + (p[1]-pos_cy)**2)**0.5 for p in pos.values()]
    r_max  = max(radii) or 1.0
    stretch_exp = 0.55   # < 1 pushes centre out; 1.0 = no change
    for n in pos:
        dx, dy = pos[n][0] - pos_cx, pos[n][1] - pos_cy
        r = (dx**2 + dy**2) ** 0.5
        if r > 1e-9:
            r_new = r_max * (r / r_max) ** stretch_exp
            pos[n] = (pos_cx + r_new * dx / r, pos_cy + r_new * dy / r)

    # Node sizes (sqrt PageRank, 10–120 pt²)
    pr_vals = [pr_map.get(n, 0.0) for n in G.nodes()]
    pr_p95  = np.percentile(pr_vals, 95) if pr_vals else 1e-9
    def _sz(pr):
        t = min(1.0, (pr ** 0.5) / max(1e-12, pr_p95 ** 0.5))
        return 10 + 110 * t

    node_ids    = list(G.nodes())
    node_sizes  = [_sz(pr_map.get(n, 0.0)) for n in node_ids]
    node_colors = [PALETTE[partition.get(n, 0) % len(PALETTE)] for n in node_ids]
    xs = [pos[n][0] for n in node_ids]
    ys = [pos[n][1] for n in node_ids]

    print("Rendering …")
    if paper_size == "a3":
        fig_w, fig_h = 16.54, 11.69
    elif paper_size == "a4":
        fig_w, fig_h = 11.69, 8.27
    else:
        fig_w, fig_h = 13.0, 9.0

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=300)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.axis("off")

    # Draw edges — sort lightest first; use log-scaled alpha so weak edges visible
    w95 = np.percentile([d.get("weight", 1) for _, _, d in G.edges(data=True)], 95) \
          if n_edges else 1
    edge_list = sorted(G.edges(data=True), key=lambda e: e[2].get("weight", 1))
    for u, v, data in edge_list:
        w = data.get("weight", 1)
        t = min(1.0, w / max(1, w95))
        alpha = 0.12 + 0.38 * t
        lw    = 0.3  + 0.9  * t
        reciprocal = G.has_edge(v, u)
        color = "#e07040" if reciprocal else "#999999"
        ax.plot(
            [pos[u][0], pos[v][0]],
            [pos[u][1], pos[v][1]],
            color=color, linewidth=lw, alpha=alpha, zorder=1, solid_capstyle="round"
        )

    # Draw nodes
    ax.scatter(xs, ys, s=node_sizes, c=node_colors, zorder=2,
               linewidths=0.4, edgecolors="white", alpha=0.92)

    # Labels — top N by PageRank, offset radially outward from graph centroid
    top_label_ids = (cent[cent["author_id"].astype(str).isin(set(node_ids))]
                     .sort_values("pagerank", ascending=False)
                     .head(top_labels)["author_id"].astype(str).tolist())

    # Centroid of all node positions (used to compute outward direction)
    all_xs = np.array([pos[n][0] for n in node_ids])
    all_ys = np.array([pos[n][1] for n in node_ids])
    cx, cy = all_xs.mean(), all_ys.mean()
    x_range = float(all_xs.max() - all_xs.min()) or 1.0
    y_range = float(all_ys.max() - all_ys.min()) or 1.0
    offset_r = max(x_range, y_range) * 0.055  # ~5.5 % of graph width

    placed: list[tuple[float, float]] = []   # label text anchor positions
    min_sep = offset_r * 0.9                 # minimum separation between label anchors

    for nid in top_label_ids:
        if nid not in pos:
            continue
        x, y = pos[nid]
        label = name_lookup.get(nid, nid)
        if not label or label == "nan":
            continue
        short = _short_name(label)

        # Direction away from centroid
        vx, vy = x - cx, y - cy
        norm = max(1e-9, (vx**2 + vy**2) ** 0.5)
        tx = x + offset_r * vx / norm
        ty = y + offset_r * vy / norm

        # Nudge if too close to an already-placed label
        for px, py in placed:
            dist = ((tx - px)**2 + (ty - py)**2) ** 0.5
            if dist < min_sep:
                # Rotate offset 30° clockwise and try again
                ang = np.arctan2(vy, vx) - np.pi / 6
                tx = x + offset_r * np.cos(ang)
                ty = y + offset_r * np.sin(ang)
                break

        placed.append((tx, ty))
        ha = "left" if tx >= x else "right"
        ax.annotate(
            short,
            xy=(x, y), xytext=(tx, ty),
            fontsize=5.5, fontweight="bold", ha=ha, va="center", zorder=4,
            arrowprops=dict(arrowstyle="-", color="#777777", lw=0.5,
                            shrinkA=0, shrinkB=2),
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.80),
        )

    # Community legend (top 8)
    n_legend = min(8, len(sorted_comms))
    handles = []
    for ci in range(n_legend):
        color = PALETTE[ci % len(PALETTE)]
        members = [n for n, c in partition.items() if c == ci]
        rep = max(members, key=lambda n: pr_map.get(n, 0), default=None)
        rep_name = name_lookup.get(rep, "") if rep else ""
        rep_short = _short_name(rep_name) if rep_name else "—"
        size = comm_sizes.get(sorted_comms[ci], 0)
        patch = mpatches.Patch(color=color,
                               label=f"Community {ci + 1}  (n={size}; lead: {rep_short})")
        handles.append(patch)

    ax.legend(handles=handles, loc="lower left", fontsize=5.5,
              framealpha=0.85, edgecolor="#cccccc",
              title="Communities (by modularity)", title_fontsize=5.5)

    # Title, subtitle, caption
    fig.suptitle(title, fontsize=12, fontweight="bold", y=0.99)
    fig.text(0.5, 0.955,
             f"Labelled nodes: top {top_labels} authors by PageRank",
             ha="center", fontsize=7, color="#333333", style="italic")
    caption = (
        f"{n_nodes} authors (top by PageRank, connected only) · {n_edges} citation edges · "
        f"Node size ∝ PageRank · Top {top_labels} labelled · "
        "Orange = reciprocal citations · Source: OpenAlex"
    )
    fig.text(0.5, 0.01, caption, ha="center", fontsize=5, color="#555555")

    plt.tight_layout(rect=[0, 0.02, 1, 0.97])
    plt.savefig(output_path, format="pdf", bbox_inches="tight", dpi=300)
    plt.close()
    print(f"Saved: {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    cfg = _load_cfg()
    default_prefix = cfg.get("prefix", "")
    default_title  = cfg.get("title", "Citation Network")

    ap = argparse.ArgumentParser(description="Print-ready PDF of citation network")
    ap.add_argument("--data-dir",    default=None,
                    help="Directory containing the CSV files "
                         "(default: data/<prefix> if prefix set, else data/)")
    ap.add_argument("--prefix",      default=default_prefix,
                    help="Dataset prefix (e.g. 'flavonoid')")
    ap.add_argument("--top-nodes",   type=int, default=300,
                    help="Number of top-PageRank nodes to include (default 300)")
    ap.add_argument("--top-labels",  type=int, default=30,
                    help="Number of nodes to label (default 30)")
    ap.add_argument("--output",      default=None,
                    help="Output PDF path (default: <prefix>_citation_network.pdf)")
    ap.add_argument("--title",       default=default_title,
                    help="Figure title")
    ap.add_argument("--paper-size",  choices=["a3", "a4", "letter"], default="a3",
                    help="Paper size in landscape (default a3)")
    ap.add_argument("--seed",        type=int, default=11088,
                    help="Layout random seed (default 11088)")
    ap.add_argument("--top-edges",   type=int, default=6,
                    help="Max outgoing edges kept per node (default 6)")
    args = ap.parse_args()

    # Resolve data_dir
    if args.data_dir:
        data_dir = args.data_dir
    elif args.prefix:
        data_dir = os.path.join(DEFAULT_DATA_BASE, args.prefix)
    else:
        data_dir = DEFAULT_DATA_BASE

    # Resolve output path
    if args.output:
        output_path = args.output
    else:
        stem = args.prefix if args.prefix else "network"
        output_path = f"{stem}_citation_network.pdf"

    make_pdf(
        data_dir           = data_dir,
        prefix             = "",
        top_nodes          = args.top_nodes,
        top_labels         = args.top_labels,
        output_path        = output_path,
        title              = args.title,
        paper_size         = args.paper_size,
        seed               = args.seed,
        top_edges_per_node = args.top_edges,
    )


if __name__ == "__main__":
    main()

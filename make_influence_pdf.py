#!/usr/bin/env python3
# Run inside the project venv: venv/bin/python3 make_influence_pdf.py ...
"""
make_influence_pdf.py

Print-quality A3 PDF from any *_influence.html produced by make_influence_report.py.
Layout: timeline scatter — publication year on x, citation count (log scale) on y,
nodes coloured and sized by role.  No edges; clean academic figure.

Usage:
    venv/bin/python3 make_influence_pdf.py reports/authors/ottaviani/author_influence.html
    venv/bin/python3 make_influence_pdf.py reports/projects/nct02422745/project_influence.html \\
                     --output cosmos_a3.pdf --top-labels 20
    venv/bin/python3 make_influence_pdf.py <file.html> --dpi 300   # press quality
"""

import argparse
import json
import math
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
import matplotlib.lines as mlines
import matplotlib.ticker

# ── Page geometry ──────────────────────────────────────────────────────────────
A3_W, A3_H = 16.54, 11.69   # A3 landscape, inches

# ── Role palette ───────────────────────────────────────────────────────────────
ROLE_COLOR = {
    "focal":            "#C0392B",
    "author_paper":     "#C0392B",
    "project_paper":    "#C0392B",
    "funded_work":      "#C0392B",
    "cites_focal":      "#7C3AED",
    "cites_author":     "#7C3AED",
    "cites_project":    "#7C3AED",
    "cites_funded":     "#7C3AED",
    "cited_by_focal":   "#047857",
    "cited_by_author":  "#047857",
    "cited_by_project": "#047857",
    "cited_by_funded":  "#047857",
    "d2":               "#94a3b8",
    "corpus":           "#cbd5e1",
}

FOCAL_ROLES = frozenset({"focal", "author_paper", "project_paper", "funded_work"})
CITE_ROLES  = frozenset({"cites_focal", "cites_author",
                          "cites_project", "cites_funded"})
REF_ROLES   = frozenset({"cited_by_focal", "cited_by_author",
                          "cited_by_project", "cited_by_funded"})

# ── HTML parsing ───────────────────────────────────────────────────────────────

def parse_html(path):
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    m = re.search(r"<title>([^<]+)</title>", html)
    title = m.group(1).strip() if m else os.path.splitext(os.path.basename(path))[0]
    m = re.search(r"const\s+ELEMENTS\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        sys.exit(f"ERROR: ELEMENTS array not found in {path}")
    try:
        elements = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: failed to parse ELEMENTS JSON: {exc}")
    return title, elements


# ── Node selection ─────────────────────────────────────────────────────────────

def select_nodes(elements, max_focal, max_cite, max_ref, max_d2):
    """Return (nodes list, keep_ids set) filtered to per-role caps.
    A cap of 0 means unlimited."""
    raw = [e["data"] for e in elements if "source" not in e.get("data", {})]

    def top(role_set, n):
        pool = [nd for nd in raw if nd.get("role") in role_set]
        pool.sort(key=lambda nd: -int(nd.get("cit", 0) or 0))
        return pool[:n] if n > 0 else []

    nodes = (top(FOCAL_ROLES, max_focal) +
             top(CITE_ROLES, max_cite) +
             top(REF_ROLES, max_ref) +
             top({"d2", "corpus"}, max_d2))
    keep_ids = {nd["id"] for nd in nodes}
    return nodes, keep_ids


def select_edges(elements, keep_ids):
    """Return edge dicts where both endpoints are in keep_ids."""
    return [e["data"] for e in elements
            if "source" in e.get("data", {})
            and e["data"]["source"] in keep_ids
            and e["data"]["target"] in keep_ids]


# ── Scatter coordinates ────────────────────────────────────────────────────────

def _cit(nd):
    return int(nd.get("cit", 0) or 0)

def _role(nd):
    return nd.get("role", "d2")

def _y(nd):
    return math.log1p(_cit(nd))

def _jitter(nd):
    """Deterministic x-jitter ±0.38 years based on node id hash."""
    h = hash(nd.get("id", "")) & 0xFFFF
    return 0.76 * (h / 0xFFFF) - 0.38


# ── Label placement ────────────────────────────────────────────────────────────

_HTML_TAG    = re.compile(r"<[^>]+>")
_MULTI_SPACE = re.compile(r"  +")

def _clean_title(nd):
    """Return display label with HTML markup replaced by spaces."""
    raw = nd.get("title") or nd.get("label") or nd.get("id", "")
    return _MULTI_SPACE.sub(" ", _HTML_TAG.sub(" ", raw)).strip()


def choose_labels(nodes, xs, ys, top_labels, y_cap=None):
    """
    Select candidates and compute repelled positions for 90° rotated labels.
    Vertical label bbox ≈ 0.25 yr wide × 3.2 log-cit tall.
    y_cap: maximum allowed label anchor y (data coords); labels are clamped to this.
    Returns ([(nd, lx, ly), ...], id_to_pos).
    """
    focal  = [nd for nd in nodes if _role(nd) in FOCAL_ROLES]
    others = [nd for nd in nodes if _role(nd) not in FOCAL_ROLES | {"d2", "corpus"}]
    others.sort(key=lambda nd: -_cit(nd))

    id_to_pos = {nd["id"]: (xs[i], ys[i]) for i, nd in enumerate(nodes)}

    candidates, seen = [], set()
    for nd in (focal[:8] + others):
        if len(candidates) >= top_labels:
            break
        nid = nd["id"]
        if nid not in id_to_pos or nid in seen:
            continue
        seen.add(nid)
        candidates.append(nd)

    if not candidates:
        return [], id_to_pos

    # Initial label positions: above each data point
    lxs = [id_to_pos[nd["id"]][0]          for nd in candidates]
    lys = [id_to_pos[nd["id"]][1] + 0.5    for nd in candidates]
    oxs = list(lxs)   # original x anchor for spring

    # Iterative repulsion (prefer y-direction since labels are tall)
    MIN_X, MIN_Y = 0.28, 3.3
    for _ in range(300):
        moved = False
        for i in range(len(lxs)):
            # Light spring: pull label back toward its anchor x
            lxs[i] += (oxs[i] - lxs[i]) * 0.04
            for j in range(i + 1, len(lxs)):
                dx, dy = lxs[i] - lxs[j], lys[i] - lys[j]
                ox = max(0.0, MIN_X - abs(dx))
                oy = max(0.0, MIN_Y - abs(dy))
                if ox > 0 and oy > 0:
                    moved = True
                    # Push in whichever direction gives more relief
                    if oy / MIN_Y >= ox / MIN_X:
                        push = oy / 2 + 0.08
                        sgn = 1 if dy >= 0 else -1
                        lys[i] += sgn * push
                        lys[j] -= sgn * push
                    else:
                        push = ox / 2 + 0.03
                        sgn = 1 if dx >= 0 else -1
                        lxs[i] += sgn * push
                        lxs[j] -= sgn * push
        if not moved:
            break

    if y_cap is not None:
        lys = [min(ly, y_cap) for ly in lys]

    return [(nd, lxs[i], lys[i]) for i, nd in enumerate(candidates)], id_to_pos


# ── Render ─────────────────────────────────────────────────────────────────────

def render(nodes, edges, title, output_path, top_labels, top_numbered, dpi):
    fig = plt.figure(figsize=(A3_W, A3_H), facecolor="white")

    # ── Plot axes — upper 28 % for rotated labels, lower 26 % for number legend ──
    ax = fig.add_axes([0.055, 0.27, 0.91, 0.46])
    ax.set_facecolor("white")

    if not nodes:
        fig.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=16)
        plt.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return

    # Scatter coordinates
    valid = [nd for nd in nodes if nd.get("year") and int(nd.get("year") or 0) > 1950]
    if not valid:
        valid = nodes

    xs  = [int(nd.get("year") or 0) + _jitter(nd) for nd in valid]
    ys  = [_y(nd) for nd in valid]

    # x-axis range: add 1 year padding each side
    yr_min = math.floor(min(int(nd.get("year") or 0) for nd in valid)) - 1
    yr_max = math.ceil( max(int(nd.get("year") or 0) for nd in valid)) + 1

    # Build id→(x,y) map for edge drawing
    id_to_xy = {nd["id"]: (xs[i], ys[i]) for i, nd in enumerate(valid)}

    # ── Arrows: citing → focal ──
    for edge in edges:
        src = id_to_xy.get(edge.get("source"))
        tgt = id_to_xy.get(edge.get("target"))
        if src is None or tgt is None:
            continue
        ax.annotate(
            "", xy=tgt, xytext=src,
            arrowprops=dict(
                arrowstyle="-|>",
                color="#7C3AED",
                alpha=0.18,
                lw=0.45,
                mutation_scale=5,
            ),
            zorder=1,
        )

    # ── Plot layers back → front ──
    layers = [
        (CITE_ROLES,  "o", 12, 18, 0.80),
        (FOCAL_ROLES, "*", 80, 70, 1.00),
    ]
    for role_set, marker, base_sz, scale_sz, alpha in layers:
        sub = [(nd, xs[i], ys[i])
               for i, nd in enumerate(valid)
               if _role(nd) in role_set]
        if not sub:
            continue
        sx     = [x for _, x, _ in sub]
        sy     = [y for _, _, y in sub]
        colors = [ROLE_COLOR.get(_role(nd), "#cccccc") for nd, _, _ in sub]
        sizes  = [base_sz + scale_sz * math.log1p(_cit(nd)) for nd, _, _ in sub]
        ax.scatter(sx, sy, s=sizes, c=colors, marker=marker,
                   alpha=alpha, linewidths=0.25, edgecolors="white", zorder=3)

    # ── y-axis: citation tick marks ──
    ytick_vals = [0, 1, 5, 20, 100, 500, 2000, 10000]
    ax.set_yticks([math.log1p(v) for v in ytick_vals])
    ax.set_yticklabels([f"{v:,}" if v else "0" for v in ytick_vals],
                       fontsize=8.5, color="#334155")

    y_max = max(ys) * 1.12 if ys else 5
    ax.set_ylim(-0.15, max(y_max, math.log1p(50)))

    # ── x-axis ──
    ax.set_xlim(yr_min, yr_max)
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True, nbins=12))
    ax.tick_params(axis="x", labelsize=8.5, colors="#334155")

    # ── Grid & spines ──
    ax.grid(axis="y", linestyle="--", linewidth=0.45, color="#e2e8f0", zorder=0)
    ax.grid(axis="x", linestyle=":",  linewidth=0.35, color="#f1f5f9", zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cbd5e1")
    ax.spines["bottom"].set_color("#cbd5e1")

    ax.set_xlabel("Publication year", fontsize=10, color="#334155", labelpad=6)
    ax.set_ylabel("Citations", fontsize=10, color="#334155", labelpad=6)

    # ── Labels (90° rotated, ggrepel-style) ──
    y_data_max = max(ys) if ys else 9.0
    label_triples, id_pos = choose_labels(valid, xs, ys, top_labels,
                                          y_cap=y_data_max + 2.5)
    text_labeled_ids = {nd["id"] for nd, _lx, _ly in label_triples}

    for nd, lx, ly in label_triples:
        x, y = id_pos[nd["id"]]
        raw   = _clean_title(nd)
        year  = nd.get("year") or ""
        short = (raw[:48] + "…") if len(raw) > 48 else raw
        lbl   = f"{short}  ({year})" if year else short

        is_focal = _role(nd) in FOCAL_ROLES
        fsz   = 7.0 if is_focal else 6.5
        col   = "#991b1b" if is_focal else "#1e3a5f"
        fw    = "semibold" if is_focal else "normal"

        ax.plot([x, lx], [y, ly], color="#d1d5db", lw=0.6,
                zorder=1, solid_capstyle="round")
        txt = ax.text(lx, ly, lbl,
                      rotation=90, ha="center", va="bottom",
                      fontsize=fsz, fontweight=fw, color=col,
                      clip_on=False, zorder=5)
        txt.set_path_effects([pe.withStroke(linewidth=2.2, foreground="white")])

    # ── Numbered badges on unlabelled focal papers ──
    numbered_pool = [nd for nd in valid
                     if _role(nd) in FOCAL_ROLES
                     and nd["id"] not in text_labeled_ids]
    numbered_pool.sort(key=lambda nd: -_cit(nd))
    numbered = numbered_pool[:top_numbered]

    for i, nd in enumerate(numbered):
        x, y = id_to_xy.get(nd["id"], (None, None))
        if x is None:
            continue
        n = i + 1
        txt = ax.text(x, y + 0.13, str(n),
                      fontsize=5.0, fontweight="bold",
                      color="#4B0082", ha="center", va="bottom",
                      zorder=6, clip_on=True)
        txt.set_path_effects([pe.withStroke(linewidth=1.8, foreground="white")])

    # ── Title ──
    fig.text(0.055, 0.993, title,
             fontsize=17, fontweight="bold",
             va="top", ha="left", color="#0f172a")
    n_focal  = sum(1 for nd in valid if _role(nd) in FOCAL_ROLES)
    n_citing = sum(1 for nd in valid if _role(nd) in CITE_ROLES)
    subtitle = f"{n_focal} focal papers  ·  {n_citing} citing  ·  source: OpenAlex"
    fig.text(0.055, 0.969, subtitle,
             fontsize=7.5, va="top", ha="left", color="#64748b")

    # ── Legend ──
    handles = [
        mlines.Line2D([], [], marker="*", color="#C0392B", linestyle="None",
                      markersize=10, label="Focal / project papers"),
        mlines.Line2D([], [], marker="o", color="#7C3AED", linestyle="None",
                      markersize=7, alpha=0.85, label="Citing papers"),
    ]
    ax.legend(handles=handles, loc="lower right",
              fontsize=8, frameon=True, framealpha=0.95,
              edgecolor="#e2e8f0", labelcolor="#334155",
              handletextpad=0.6, borderpad=0.7)

    # ── Numbered reference legend ──
    if numbered:
        leg_ax = fig.add_axes([0.055, 0.025, 0.91, 0.225])
        leg_ax.set_facecolor("white")
        leg_ax.axis("off")

        leg_ax.plot([0, 1], [1.0, 1.0], color="#e2e8f0", lw=0.8,
                    transform=leg_ax.transAxes, clip_on=False)

        n_cols   = 3
        n_rows   = math.ceil(len(numbered) / n_cols)
        col_w    = 1.0 / n_cols
        row_step = 0.97 / max(n_rows, 1)

        for i, nd in enumerate(numbered):
            col  = i % n_cols
            row  = i // n_cols
            xpos = col * col_w + 0.004
            ypos = 0.97 - row * row_step

            raw   = _clean_title(nd)
            year  = nd.get("year") or ""
            cit   = _cit(nd)
            short = (raw[:42] + "…") if len(raw) > 42 else raw
            entry = f"{i+1:2d}.  {short}  ({year})  [{cit:,} cit.]"

            leg_ax.text(xpos, ypos, entry,
                        fontsize=6.0, va="top", ha="left",
                        transform=leg_ax.transAxes, color="#334155",
                        fontfamily="monospace")

        leg_ax.text(0.004, 1.0 + 0.06, "Numbered papers (ranked by citations):",
                    fontsize=6.5, va="bottom", ha="left",
                    transform=leg_ax.transAxes, color="#64748b",
                    fontweight="semibold")

    # ── Node-size note + credit ──
    fig.text(0.057, 0.018,
             "Node size  ∝  citation count",
             fontsize=6.0, va="bottom", ha="left", color="#94a3b8")
    fig.text(1 - 0.03, 0.010,
             "make_influence_report.py  ·  openalex.org",
             fontsize=5.5, ha="right", va="bottom", color="#cbd5e1")

    plt.savefig(output_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    print(f"Written: {output_path}  ({os.path.getsize(output_path) // 1024} KB)")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="A3 print-quality PDF from an influence HTML file")
    ap.add_argument("html",
                    help="Path to *_influence.html (from make_influence_report.py)")
    ap.add_argument("--output",     default=None,
                    help="Output PDF path (default: <html_basename>_a3.pdf)")
    ap.add_argument("--title",      default=None,
                    help="Override the title text")
    ap.add_argument("--top-labels", type=int, default=20,
                    help="Max labelled nodes (default 20)")
    ap.add_argument("--max-focal",  type=int, default=999,
                    help="Max focal papers shown (default: all)")
    ap.add_argument("--max-cite",   type=int, default=60,
                    help="Max citing-layer nodes (default 60)")
    ap.add_argument("--max-ref",    type=int, default=0,
                    help="Max cited-layer (reference) nodes; 0 = off (default)")
    ap.add_argument("--max-d2",     type=int, default=0,
                    help="Max layer-2 nodes; 0 = off (default)")
    ap.add_argument("--top-numbered", type=int, default=40,
                    help="Focal papers to label with numbers + legend (default 40)")
    ap.add_argument("--dpi",        type=int, default=200,
                    help="Resolution in DPI (default 200; use 300 for print)")
    args = ap.parse_args()

    if not os.path.isfile(args.html):
        sys.exit(f"File not found: {args.html}")

    print(f"Parsing: {args.html}")
    title, elements = parse_html(args.html)
    if args.title:
        title = args.title
    print(f"  Title: {title}")

    raw_count = sum(1 for e in elements if "source" not in e.get("data", {}))
    print(f"  {raw_count} nodes in HTML")

    nodes, keep_ids = select_nodes(elements, args.max_focal, args.max_cite,
                                    args.max_ref, args.max_d2)
    edges = select_edges(elements, keep_ids)
    print(f"  Selected: {len(nodes)} nodes  "
          f"(focal={sum(1 for n in nodes if n.get('role') in FOCAL_ROLES)}, "
          f"citing={sum(1 for n in nodes if n.get('role') in CITE_ROLES)}, "
          f"cited={sum(1 for n in nodes if n.get('role') in REF_ROLES)})  "
          f"·  {len(edges)} edges")

    out = args.output or (os.path.splitext(args.html)[0] + "_a3.pdf")

    print(f"Rendering at {args.dpi} DPI …")
    render(nodes, edges, title, out, top_labels=args.top_labels,
           top_numbered=args.top_numbered, dpi=args.dpi)


if __name__ == "__main__":
    main()

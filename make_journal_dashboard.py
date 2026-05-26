#!/usr/bin/env python3
"""
make_journal_dashboard.py
Standalone HTML dashboard: where is research published and what type?

Reads:
  output/<prefix>_papers_by_journal.csv
  output/<prefix>_papers_by_journal_year.csv
  output/<prefix>_papers_by_journal_study_type.csv

Writes:
  output/<prefix>_journal_dashboard.html
"""

import argparse
import json as _json
import os
import pandas as pd
import plotly.graph_objects as go

OUTPUT_DIR  = "output"

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

TOP_N       = 25
TOP_TREND   = 10

STUDY_COLORS = {
    "Observational":                    "#27ae60",
    "Systematic Review / Meta-analysis":"#8e44ad",
    "Review":                           "#9b59b6",
    "RCT":                              "#2980b9",
    "Clinical Trial":                   "#e67e22",
    "Other":                            "#95a5a6",
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
    "Study-type labels are algorithmically assigned and may contain errors."
    "</div>"
)

_NON_JOURNALS = {
    "biorxiv", "medrxiv", "ssrn electronic journal", "research square",
    "authorea", "chemrxiv", "preprints.org", "arxiv",
    "figshare", "zenodo", "dryad", "osf preprints",
    "la referencia",
    "hal open science",
    "unknown",
}

def _is_non_journal(name):
    if not isinstance(name, str):
        return True
    n = name.lower()
    return any(nj in n for nj in _NON_JOURNALS)


def fig_top_journals(df, n=TOP_N):
    d = df.nlargest(n, "papers").sort_values("papers")
    fig = go.Figure(go.Bar(
        x=d["papers"], y=d["journal"],
        orientation="h",
        marker_color="#2980b9",
        text=d["papers"], textposition="outside",
        hovertemplate="<b>%{y}</b><br>Papers: %{x:,}<extra></extra>",
    ))
    fig.update_layout(
        title=f"Top {n} Journals by Paper Count",
        xaxis_title="Papers", yaxis_title="",
        height=max(500, n * 22),
        margin=dict(l=10, r=80, t=50, b=40),
        plot_bgcolor="white", paper_bgcolor="white",
        yaxis=dict(tickfont=dict(size=11)),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_impact_bubble(df, n=60):
    d = df.nlargest(n, "papers")
    fig = go.Figure(go.Scatter(
        x=d["papers"], y=d["cites_per_paper"],
        mode="markers+text",
        marker=dict(
            size=d["papers"] ** 0.45 * 3,
            color="#e67e22", opacity=0.7,
            line=dict(width=0.5, color="white"),
        ),
        text=d["journal"].str[:28],
        textposition="top center",
        textfont=dict(size=8),
        hovertemplate=(
            "<b>%{text}</b><br>"
            "Papers: %{x:,}<br>"
            "Citations / paper: %{y:.1f}<extra></extra>"
        ),
    ))
    fig.update_layout(
        title=f"Impact vs Volume — top {n} journals",
        xaxis_title="Papers", yaxis_title="Citations per paper",
        height=520,
        margin=dict(l=10, r=10, t=50, b=50),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    fig.update_yaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_study_type_by_journal(jst_df, top_list):
    d = jst_df[jst_df["journal"].isin(top_list)].copy()
    totals = d.groupby("journal")["papers"].sum().reset_index(name="total")
    d = d.merge(totals, on="journal")
    d["pct"] = 100 * d["papers"] / d["total"]
    order = (d.groupby("journal")["total"].first()
              .sort_values(ascending=True).index.tolist())
    fig = go.Figure()
    for st, color in STUDY_COLORS.items():
        sub = d[d["study_type"] == st]
        sub = sub.set_index("journal").reindex(order).fillna(0).reset_index()
        fig.add_trace(go.Bar(
            name=st,
            x=sub["pct"], y=sub["journal"],
            orientation="h",
            marker_color=color,
            hovertemplate=f"<b>{st}</b><br>%{{y}}: %{{x:.1f}}%<extra></extra>",
        ))
    fig.update_layout(
        title=f"Study-type mix — top {len(top_list)} journals (% of papers)",
        barmode="stack",
        xaxis=dict(title="% papers", range=[0, 100]),
        yaxis_title="",
        height=max(500, len(top_list) * 22),
        legend=dict(orientation="h", y=-0.12, x=0.5, xanchor="center"),
        margin=dict(l=10, r=10, t=50, b=80),
        plot_bgcolor="white", paper_bgcolor="white",
        yaxis=dict(tickfont=dict(size=11)),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_journal_trends(jy_df, top_list, n=TOP_TREND):
    recent = (jy_df[jy_df["journal"].isin(top_list)]
              .groupby("journal")["papers"].sum()
              .nlargest(n).index.tolist())
    fig = go.Figure()
    d = jy_df[jy_df["journal"].isin(recent)]
    d = d[(d["year"] >= 2013) & (d["year"] <= 2025)]
    for j in recent:
        sub = d[d["journal"] == j].sort_values("year")
        fig.add_trace(go.Scatter(
            x=sub["year"], y=sub["papers"],
            mode="lines+markers", name=j[:35],
            hovertemplate=f"<b>{j[:40]}</b><br>%{{x}}: %{{y}} papers<extra></extra>",
        ))
    fig.update_layout(
        title=f"Publication trends — top {n} journals (2013–2025)",
        xaxis_title="Year", yaxis_title="Papers",
        height=480,
        margin=dict(l=10, r=10, t=50, b=50),
        plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(font=dict(size=10), orientation="v", x=1.01, y=1),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    fig.update_yaxes(showgrid=True, gridcolor="#eee")
    return fig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    args = parser.parse_args()
    out = args.output_dir

    print("Loading data…")
    j_df   = pd.read_csv(os.path.join(out, _pf("papers_by_journal.csv")))
    jy_df  = pd.read_csv(os.path.join(out, _pf("papers_by_journal_year.csv")))
    jst_df = pd.read_csv(os.path.join(out, _pf("papers_by_journal_study_type.csv")))

    j_df   = j_df[~j_df["journal"].apply(_is_non_journal)].copy()
    jy_df  = jy_df[~jy_df["journal"].apply(_is_non_journal)].copy()
    jst_df = jst_df[~jst_df["journal"].apply(_is_non_journal)].copy()

    j_df = j_df[j_df["journal"].notna() & (j_df["journal"] != "Unknown")]
    j_df["cites_per_paper"] = (j_df["citations"] / j_df["papers"]).round(1)
    top_journals = j_df.nlargest(TOP_N, "papers")["journal"].tolist()

    print("Building figures…")
    PLOTLY_CDN = "https://cdn.plot.ly/plotly-3.5.0.min.js"
    figs = {
        "Top journals by papers":    fig_top_journals(j_df),
        "Impact vs volume":          fig_impact_bubble(j_df),
        "Study-type mix by journal": fig_study_type_by_journal(jst_df, top_journals),
        "Publication trends":        fig_journal_trends(jy_df, top_journals),
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
<title>{_TITLE} — Journal Analysis</title>
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
  <h1>{_TITLE} — Journal Analysis</h1>
  <p>Where is this research published, and what type of research appears where?</p>
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

    output_file = os.path.join(out, _pf("journal_dashboard.html"))
    os.makedirs(out, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Written: {output_file}  ({os.path.getsize(output_file)//1024} KB)")

    print(f"\nTop 10 journals by papers:")
    for _, r in j_df.nlargest(10, "papers").iterrows():
        print(f"  {r['papers']:>5}  {r['cites_per_paper']:>6.1f} cit/paper  {r['journal']}")


if __name__ == "__main__":
    main()

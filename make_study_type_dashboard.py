#!/usr/bin/env python3
"""
make_study_type_dashboard.py
Build output/study_type.html from papers_by_study_type.csv and
papers_by_study_type_year.csv (written by bibliometrics.py).

Usage:
    python make_study_type_dashboard.py
    python make_study_type_dashboard.py --output-dir results
"""

import argparse
import json as _json
import os
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
    """Apply topic prefix to a filename."""
    return f"{_PREFIX}_{name}" if _PREFIX else name

DISCLAIMER = (
    '<div style="background:#fff8e1;border-top:4px solid #f9a825;'
    'border-bottom:1px solid #f9a825;padding:13px 24px;'
    'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;'
    'font-size:.88rem;color:#4a3800;line-height:1.55">'
    "<strong>Disclaimer:</strong> This page presents automatically generated "
    "bibliometric data retrieved from "
    '<a href="https://openalex.org" style="color:#4a3800;font-weight:600" target="_blank">OpenAlex</a>. '
    "It does not represent the views, opinions, or endorsement of any individual, "
    "institution, or organisation. Data may contain errors, omissions, or "
    "misattributions inherent to automated retrieval. Results reflect publication "
    "patterns only and should not be interpreted as assessments of individual "
    "researchers or institutions."
    "</div>"
)

TYPE_ORDER = [
    "Observational",
    "Other",
    "Review",
    "Systematic Review / Meta-analysis",
    "RCT",
    "Clinical Trial",
]

TYPE_COLORS = {
    "RCT":                             "#2980b9",
    "Observational":                   "#27ae60",
    "Systematic Review / Meta-analysis": "#8e44ad",
    "Review":                          "#9b59b6",
    "Clinical Trial":                  "#e67e22",
    "Other":                           "#95a5a6",
}

STYLE = """\
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
     background:#f5f6fa;color:#2c3e50;line-height:1.6}
#hdr{background:linear-gradient(135deg,#1a2742 0%,#2c4a8a 100%);
     color:#fff;padding:40px 32px 36px;text-align:center}
#hdr h1{font-size:1.8rem;font-weight:800;margin-bottom:8px}
#hdr p{font-size:1rem;opacity:.80;max-width:600px;margin:0 auto 24px}
.stats{display:flex;flex-wrap:wrap;justify-content:center;gap:16px;
       max-width:900px;margin:0 auto}
.stat{background:rgba(255,255,255,.12);border-radius:10px;padding:10px 20px;
      text-align:center;min-width:140px}
.stat .n{font-size:1.6rem;font-weight:700;display:block}
.stat .l{font-size:.75rem;opacity:.75;text-transform:uppercase;letter-spacing:.5px}
main{max-width:960px;margin:0 auto;padding:32px 20px 72px}
h3.sec{font-size:.9rem;font-weight:700;color:#888;text-transform:uppercase;
        letter-spacing:1px;margin-bottom:14px;padding-bottom:6px;
        border-bottom:2px solid #eee}
.panel{background:#fff;border-radius:10px;padding:20px;
       box-shadow:0 2px 8px rgba(0,0,0,.07);margin-bottom:28px}
.panel p.note{font-size:.84rem;color:#666;margin-top:8px;line-height:1.5}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{background:#1a2742;color:#fff;padding:8px 12px;text-align:left}
td{padding:7px 12px;border-bottom:1px solid #eee}
tr:hover td{background:#f0f6ff}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
footer{text-align:center;padding:20px;font-size:.75rem;color:#aaa}
a{color:inherit}
</style>"""


def _swatch(study_type: str) -> str:
    col = TYPE_COLORS.get(study_type, "#aaa")
    return f'<span class="dot" style="background:{col}"></span>'


def build_html(st_df: pd.DataFrame, st_year_df: pd.DataFrame) -> str:
    # ── Donut chart ────────────────────────────────────────────────────────────
    st_plot = st_df.sort_values("papers", ascending=False)
    colors  = [TYPE_COLORS.get(t, "#aaa") for t in st_plot["study_type"]]

    donut = go.Figure(go.Pie(
        labels=st_plot["study_type"],
        values=st_plot["papers"],
        hole=0.45,
        marker=dict(colors=colors, line=dict(color="#fff", width=2)),
        textinfo="label+percent",
        textfont=dict(size=12),
        hovertemplate="<b>%{label}</b><br>Papers: %{value:,}<br>%{percent}<extra></extra>",
    ))
    donut.update_layout(
        margin=dict(t=20, b=20, l=20, r=20),
        height=380,
        showlegend=False,
        plot_bgcolor="#fff",
        paper_bgcolor="#fff",
    )

    # ── Stacked bar chart by year ──────────────────────────────────────────────
    # Filter to 2000–2024 for readability; earlier years are near-zero
    st_year_df = st_year_df[st_year_df["year"].between(2000, 2024)].copy()
    years = sorted(st_year_df["year"].unique())
    study_types = st_df["study_type"].tolist()  # already sorted by papers desc

    bar_fig = go.Figure()
    for stype in study_types:
        sub = st_year_df[st_year_df["study_type"] == stype].set_index("year")
        vals = [int(sub.loc[y, "papers"]) if y in sub.index else 0 for y in years]
        bar_fig.add_trace(go.Bar(
            name=stype,
            x=years,
            y=vals,
            marker_color=TYPE_COLORS.get(stype, "#aaa"),
            hovertemplate=f"<b>{stype}</b><br>Year: %{{x}}<br>Papers: %{{y:,}}<extra></extra>",
        ))

    bar_fig.update_layout(
        barmode="stack",
        height=380,
        margin=dict(t=10, b=40, l=50, r=20),
        xaxis=dict(title="Year", tickangle=0),
        yaxis=dict(title="Papers published"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="#fff",
        paper_bgcolor="#fff",
    )

    # ── Convert charts to HTML divs ────────────────────────────────────────────
    donut_div  = donut.to_html(full_html=False, include_plotlyjs=False)
    bar_div    = bar_fig.to_html(full_html=False, include_plotlyjs=False)

    # ── Stats bar ─────────────────────────────────────────────────────────────
    total_papers = int(st_df["papers"].sum())
    stat_items = ""
    for _, row in st_plot.iterrows():
        stat_items += (
            f'<div class="stat"><span class="n" style="color:{TYPE_COLORS.get(row.study_type,"#fff")}">'
            f'{int(row.papers):,}</span>'
            f'<span class="l">{row.study_type}</span></div>\n'
        )

    # ── Summary table ─────────────────────────────────────────────────────────
    table_rows = ""
    for _, row in st_plot.iterrows():
        table_rows += (
            f"<tr><td>{_swatch(row.study_type)}{row.study_type}</td>"
            f"<td>{int(row.papers):,}</td>"
            f"<td>{row.pct:.1f}%</td>"
            f"<td>{int(row.citations):,}</td></tr>\n"
        )

    # ── Assemble ──────────────────────────────────────────────────────────────
    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPF Research — Study Type Breakdown</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js"></script>
{STYLE}
</head>
<body>
{DISCLAIMER}

<div id="hdr">
  <h1>Study Type Breakdown</h1>
  <p>Classification of {total_papers:,} UPF papers by study design,
     based on MeSH publication-type tags and title keywords.</p>
  <div class="stats">
    {stat_items}
  </div>
</div>

<main>

  <h3 class="sec" style="margin-top:8px">Overall Distribution</h3>
  <div class="panel">
    {donut_div}
    <p class="note">
      Classification priority: PubMed MeSH publication-type tags (most reliable,
      covers ~65–70 % of the corpus) → OpenAlex work type →
      title-keyword heuristics. Papers without a detectable study design
      are classified as <em>Other</em>.
    </p>
  </div>

  <h3 class="sec">Trends Over Time (2000–2024)</h3>
  <div class="panel">
    {bar_div}
    <p class="note">
      Annual paper counts by study type (2000–2024).
    </p>
  </div>

  <h3 class="sec">Summary Table</h3>
  <div class="panel">
    <table>
      <tr><th>Study type</th><th>Papers</th><th>%</th><th>Citations</th></tr>
      {table_rows}
    </table>
  </div>

  <p style="font-size:.82rem;color:#888;margin-top:-12px">
    ← <a href="index.html" style="color:#2980b9">Back to overview</a>
  </p>

</main>
<footer>
  Data: <a href="https://openalex.org">OpenAlex</a> · Analysis by G. Kuhnle ·
  Generated May 2025
</footer>
</body>
</html>
"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    args = parser.parse_args()

    st_path      = os.path.join(args.output_dir, _pf("papers_by_study_type.csv"))
    st_year_path = os.path.join(args.output_dir, _pf("papers_by_study_type_year.csv"))

    for p in (st_path, st_year_path):
        if not os.path.exists(p):
            print(f"Missing {p} — run bibliometrics.py first.")
            raise SystemExit(1)

    st_df      = pd.read_csv(st_path)
    st_year_df = pd.read_csv(st_year_path)

    html = build_html(st_df, st_year_df)

    out_path = os.path.join(args.output_dir, _pf("study_type.html"))
    os.makedirs(args.output_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Written → {out_path}  ({os.path.getsize(out_path)//1024} KB)")


if __name__ == "__main__":
    main()

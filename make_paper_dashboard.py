#!/usr/bin/env python3
"""
make_paper_dashboard.py
Standalone HTML dashboard: individual paper details.

Reads:
  data/<prefix>/papers_detail.csv

Writes:
  output/<prefix>/paper_dashboard.html
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

PLOTLY_CDN = "https://cdn.plot.ly/plotly-3.5.0.min.js"

STUDY_COLORS = {
    "Observational":                     "#27ae60",
    "Systematic Review / Meta-analysis": "#8e44ad",
    "Review":                            "#9b59b6",
    "RCT":                               "#2980b9",
    "Clinical Trial":                    "#e67e22",
    "Other":                             "#95a5a6",
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


def _pf(name):
    return name


def _trunc(s, n=60):
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[:n - 1] + "…"


# ── Figures ───────────────────────────────────────────────────────────────────

def fig_top_papers(df, n=50):
    """Horizontal bar: top N papers by citations."""
    top = df.nlargest(n, "citations").copy()
    top = top.sort_values("citations")
    top["label"] = top["title"].map(lambda t: _trunc(t, 60))
    top["doi_link"] = top["doi"].map(
        lambda d: f'<a href="https://doi.org/{d}" target="_blank">{d}</a>' if d else ""
    )
    top["hover_title"] = top["title"].fillna("").map(lambda t: _trunc(t, 120))
    top["hover_journal"] = top["journal"].fillna("")
    top["hover_year"] = top["year"].fillna("").astype(str)
    top["hover_doi"] = top["doi"].fillna("")

    fig = go.Figure(go.Bar(
        x=top["citations"],
        y=top["label"],
        orientation="h",
        marker_color="#2980b9",
        text=top["citations"].astype(int).astype(str),
        textposition="outside",
        customdata=top[["hover_title", "hover_journal", "hover_year", "hover_doi"]].values,
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            "Journal: %{customdata[1]}<br>"
            "Year: %{customdata[2]}<br>"
            "DOI: %{customdata[3]}<br>"
            "Citations: %{x:,}"
            "<extra></extra>"
        ),
    ))
    fig.update_layout(
        title=f"Top {n} Papers by Citations",
        xaxis_title="Citations", yaxis_title="",
        height=max(600, n * 18),
        margin=dict(l=10, r=100, t=60, b=40),
        plot_bgcolor="white", paper_bgcolor="white",
        yaxis=dict(tickfont=dict(size=10)),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_citation_histogram(df):
    """Histogram of citations per paper, log x-axis."""
    cites = df["citations"].dropna().astype(int)
    cites = cites[cites > 0]
    fig = go.Figure(go.Histogram(
        x=cites,
        marker_color="#2980b9",
        opacity=0.85,
        hovertemplate="Citations: %{x}<br>Papers: %{y}<extra></extra>",
    ))
    fig.update_layout(
        title="Citation Distribution (papers with ≥1 citation, log x-axis)",
        xaxis_title="Citations", yaxis_title="Number of papers",
        xaxis_type="log",
        height=420,
        margin=dict(l=10, r=10, t=60, b=50),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee")
    fig.update_yaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_papers_by_year(df):
    """Stacked bar: papers per year, OA vs non-OA."""
    if "open_access" not in df.columns:
        df = df.copy()
        df["open_access"] = False

    df2 = df.copy()
    df2["open_access"] = df2["open_access"].astype(bool)
    df2 = df2[df2["year"].notna()].copy()
    df2["year"] = df2["year"].astype(int)
    df2 = df2[df2["year"] >= 2000]

    oa  = df2[df2["open_access"]].groupby("year").size().reset_index(name="oa_papers")
    non = df2[~df2["open_access"]].groupby("year").size().reset_index(name="non_oa_papers")
    years = pd.DataFrame({"year": range(df2["year"].min(), df2["year"].max() + 1)})
    merged = years.merge(oa, on="year", how="left").merge(non, on="year", how="left").fillna(0)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=merged["year"], y=merged["oa_papers"],
        name="Open Access", marker_color="#27ae60",
        hovertemplate="Year: %{x}<br>Open Access papers: %{y:,}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=merged["year"], y=merged["non_oa_papers"],
        name="Non-OA", marker_color="#2980b9",
        hovertemplate="Year: %{x}<br>Non-OA papers: %{y:,}<extra></extra>",
    ))
    fig.update_layout(
        title="Papers per Year (Open Access vs Non-OA)",
        barmode="stack",
        xaxis_title="Year", yaxis_title="Papers",
        height=420,
        margin=dict(l=10, r=10, t=60, b=50),
        legend=dict(orientation="h", y=-0.15, x=0.5, xanchor="center"),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee", dtick=1)
    fig.update_yaxes(showgrid=True, gridcolor="#eee")
    return fig


def fig_study_type_donut(df):
    """Donut chart of study type breakdown."""
    if "study_type" not in df.columns:
        return None
    counts = df.groupby("study_type").size().reset_index(name="papers")
    counts = counts.sort_values("papers", ascending=False)
    colors = [STUDY_COLORS.get(st, "#95a5a6") for st in counts["study_type"]]

    fig = go.Figure(go.Pie(
        labels=counts["study_type"],
        values=counts["papers"],
        hole=0.42,
        marker_colors=colors,
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b><br>Papers: %{value:,} (%{percent})<extra></extra>",
    ))
    fig.update_layout(
        title="Study Type Breakdown",
        height=480,
        margin=dict(l=10, r=10, t=60, b=10),
        legend=dict(orientation="v", x=1.0, y=0.5, xanchor="left"),
    )
    return fig


def fig_open_access_donut(df):
    """Donut chart: OA vs non-OA."""
    if "open_access" not in df.columns:
        return None
    df2 = df.copy()
    df2["open_access"] = df2["open_access"].astype(bool)
    n_oa  = int(df2["open_access"].sum())
    n_non = int((~df2["open_access"]).sum())

    fig = go.Figure(go.Pie(
        labels=["Open Access", "Non-OA"],
        values=[n_oa, n_non],
        hole=0.42,
        marker_colors=["#27ae60", "#2980b9"],
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b><br>Papers: %{value:,} (%{percent})<extra></extra>",
    ))
    fig.update_layout(
        title="Open Access Status",
        height=420,
        margin=dict(l=10, r=10, t=60, b=10),
    )
    return fig


# ── HTML assembly ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=os.path.join(OUTPUT_DIR, _PREFIX) if _PREFIX else OUTPUT_DIR,
    )
    parser.add_argument(
        "--data-dir",
        default=os.path.join("data", _PREFIX) if _PREFIX else "data",
    )
    args = parser.parse_args()
    out  = args.output_dir
    data = args.data_dir

    detail_path = os.path.join(data, _pf("papers_detail.csv"))
    if not os.path.exists(detail_path):
        print(f"papers_detail.csv not found at {detail_path} — skipping paper dashboard.")
        print("Re-run bibliometrics.py to generate papers_detail.csv first.")
        return

    print("Loading data…")
    df = pd.read_csv(detail_path)
    n_papers = len(df)
    n_oa = int(df["open_access"].astype(bool).sum()) if "open_access" in df.columns else 0
    print(f"  {n_papers:,} papers loaded")

    print("Building figures…")
    figs = []
    figs.append(("Top 50 Papers by Citations",     fig_top_papers(df, n=50)))
    figs.append(("Citation Distribution",          fig_citation_histogram(df)))
    figs.append(("Papers by Year (OA vs Non-OA)",  fig_papers_by_year(df)))

    st_fig = fig_study_type_donut(df)
    if st_fig is not None:
        figs.append(("Study Type Breakdown", st_fig))

    oa_fig = fig_open_access_donut(df)
    if oa_fig is not None:
        figs.append(("Open Access Status", oa_fig))

    fig_divs = {
        title: fig.to_html(full_html=False, include_plotlyjs=False,
                           config={"responsive": True})
        for title, fig in figs if fig is not None
    }
    sections = "\n".join(
        f'<h2 class="sec">{t}</h2>\n<div class="fig-wrap">{d}</div>'
        for t, d in fig_divs.items()
    )

    # ── Searchable paper table ────────────────────────────────────────────────
    import json as _json
    search_cols = ["title", "journal", "year", "citations", "study_type", "open_access", "doi"]
    _sdf = df[search_cols].fillna("").copy()
    _sdf["citations"] = pd.to_numeric(_sdf["citations"], errors="coerce").fillna(0).astype(int)
    _sdf["year"]      = _sdf["year"].astype(str).str.replace(r"\.0$", "", regex=True)
    _sdf["open_access"] = _sdf["open_access"].map(lambda v: "OA" if str(v).lower() in ("true","1","yes") else "")
    papers_json = _json.dumps(_sdf.sort_values("citations", ascending=False).to_dict(orient="records"))

    generated = pd.Timestamp.now().strftime("%B %Y")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_TITLE} — Paper Analysis</title>
<script src="{PLOTLY_CDN}" charset="utf-8"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
      background:#f5f6fa;color:#2c3e50;line-height:1.6}}
#hdr{{background:linear-gradient(135deg,#1a2742 0%,#2c4a8a 100%);
      color:#fff;padding:36px 32px 28px;text-align:center}}
#hdr h1{{font-size:1.7rem;font-weight:800;margin-bottom:8px}}
#hdr p{{opacity:.75;font-size:.95rem}}
.stats{{display:flex;flex-wrap:wrap;justify-content:center;gap:20px;
        max-width:600px;margin:16px auto 0}}
.stat{{background:rgba(255,255,255,.12);border-radius:10px;
       padding:10px 22px;text-align:center;min-width:110px}}
.stat .n{{font-size:1.6rem;font-weight:700;display:block}}
.stat .l{{font-size:.76rem;opacity:.75;text-transform:uppercase;letter-spacing:.5px}}
main{{max-width:1100px;margin:0 auto;padding:32px 24px 80px}}
h2.sec{{font-size:.9rem;font-weight:700;color:#888;text-transform:uppercase;
        letter-spacing:1px;margin:40px 0 14px;padding-bottom:6px;
        border-bottom:2px solid #eee}}
.fig-wrap{{background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.07);
           padding:8px;margin-bottom:8px;overflow-x:auto}}
footer{{text-align:center;padding:24px;font-size:.78rem;color:#aaa}}
a{{color:inherit}}
#search-box{{background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.07);
             padding:20px 24px;margin-bottom:32px}}
#search-box h2{{font-size:.9rem;font-weight:700;color:#888;text-transform:uppercase;
               letter-spacing:1px;margin-bottom:12px;padding-bottom:6px;border-bottom:2px solid #eee}}
#paper-search{{width:100%;padding:9px 14px;font-size:.95rem;border:1px solid #ccc;
               border-radius:5px;margin-bottom:10px;box-sizing:border-box}}
#paper-count{{font-size:.82rem;color:#888;margin-bottom:8px}}
#paper-results{{overflow-x:auto}}
#paper-results table{{border-collapse:collapse;width:100%;font-size:.83rem}}
#paper-results th{{background:#2c7bb6;color:#fff;padding:7px 10px;text-align:left;white-space:nowrap;cursor:pointer}}
#paper-results th:hover{{background:#1a5a8a}}
#paper-results td{{padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}}
#paper-results tr:hover td{{background:#f0f6ff}}
.oa-badge{{background:#27ae60;color:#fff;font-size:.68rem;padding:1px 5px;border-radius:8px;white-space:nowrap}}
</style>
</head>
<body>
{DISCLAIMER}
<div id="hdr">
  <h1>{_TITLE} — Paper Analysis</h1>
  <p>Individual paper details: citations, open access, study types and year-by-year output.</p>
  <div class="stats">
    <div class="stat"><span class="n">{n_papers:,}</span><span class="l">Papers</span></div>
    <div class="stat"><span class="n">{n_oa:,}</span><span class="l">Open Access</span></div>
  </div>
</div>
<main>
<div id="search-box">
  <h2>&#128269; Search Papers</h2>
  <input id="paper-search" type="text" placeholder="Search by title, journal, year or study type…" oninput="paperSearch()" />
  <div id="paper-count"></div>
  <div id="paper-results">
    <table>
      <thead><tr>
        <th onclick="paperSort('citations')">#&nbsp;Citations &#8597;</th>
        <th onclick="paperSort('title')">Title &#8597;</th>
        <th onclick="paperSort('journal')">Journal &#8597;</th>
        <th onclick="paperSort('year')">Year &#8597;</th>
        <th onclick="paperSort('study_type')">Study type &#8597;</th>
        <th>OA</th>
      </tr></thead>
      <tbody id="paper-tbody"></tbody>
    </table>
  </div>
</div>
{sections}
  <p style="font-size:.82rem;color:#888;margin-top:24px">
    &#8592; <a href="index.html" style="color:#2980b9">Back to overview</a>
  </p>
</main>
<footer>Data: <a href="https://openalex.org">OpenAlex</a> · Analysis by G. Kuhnle · Generated {generated}</footer>
<script>
const PAPERS = {papers_json};
let _sortKey = 'citations', _sortAsc = false;
function paperSort(k) {{
  if (_sortKey === k) _sortAsc = !_sortAsc; else {{ _sortKey = k; _sortAsc = k !== 'citations'; }}
  paperSearch();
}}
function paperSearch() {{
  const q = document.getElementById('paper-search').value.trim().toLowerCase();
  let rows = q ? PAPERS.filter(p =>
    (p.title||'').toLowerCase().includes(q) ||
    (p.journal||'').toLowerCase().includes(q) ||
    String(p.year||'').includes(q) ||
    (p.study_type||'').toLowerCase().includes(q)
  ) : PAPERS.slice();
  rows.sort((a,b) => {{
    let av = a[_sortKey]||'', bv = b[_sortKey]||'';
    if (typeof av === 'number') return _sortAsc ? av-bv : bv-av;
    return _sortAsc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
  }});
  const cnt = document.getElementById('paper-count');
  cnt.textContent = q ? rows.length + ' result' + (rows.length!==1?'s':'') + ' for "' + document.getElementById('paper-search').value + '"'
                      : 'Showing all ' + rows.length.toLocaleString() + ' papers — type to filter';
  const doi_url = d => d ? `<a href="https://doi.org/${{d}}" target="_blank" style="color:#2980b9">${{d}}</a>` : '';
  const oa_badge = v => v==='OA' ? '<span class="oa-badge">OA</span>' : '';
  document.getElementById('paper-tbody').innerHTML = rows.slice(0,500).map(p =>
    `<tr>
      <td style="text-align:right;white-space:nowrap">${{(p.citations||0).toLocaleString()}}</td>
      <td><b>${{(p.title||'').substring(0,120)}}${{(p.title||'').length>120?'…':''}}</b><br>
          <span style="font-size:.75rem;color:#888">${{doi_url(p.doi)}}</span></td>
      <td style="white-space:nowrap;font-size:.8rem">${{p.journal||''}}</td>
      <td style="white-space:nowrap">${{p.year||''}}</td>
      <td style="font-size:.8rem">${{p.study_type||''}}</td>
      <td>${{oa_badge(p.open_access)}}</td>
    </tr>`
  ).join('');
}}
paperSearch();
</script>
</body>
</html>
"""

    os.makedirs(out, exist_ok=True)
    output_file = os.path.join(out, _pf("paper_dashboard.html"))
    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Written: {output_file}  ({os.path.getsize(output_file) // 1024} KB)")

    # Quick summary
    print(f"\nTop 10 papers by citations:")
    for _, r in df.nlargest(10, "citations").iterrows():
        print(f"  {int(r['citations']):>6,}  {_trunc(str(r.get('title', '')), 70)}")


if __name__ == "__main__":
    main()

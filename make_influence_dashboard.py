#!/usr/bin/env python3
"""
make_influence_dashboard.py
Generates output/influence_dashboard.html — a self-contained interactive
dashboard for the influence analysis results.

Run influence_analysis.py first to generate the input CSVs.

Usage:
    python make_influence_dashboard.py
"""

import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

OUTPUT_DIR  = "output"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "influence_dashboard.html")
TOP_N       = 30   # bars in ranked charts

# ── Load data ─────────────────────────────────────────────────────────────────
print("Loading data…")
works_df   = pd.read_csv(os.path.join(OUTPUT_DIR, "most_cited_works.csv"))
authors_df = pd.read_csv(os.path.join(OUTPUT_DIR, "most_cited_authors.csv"))

altmetric_path = os.path.join(OUTPUT_DIR, "altmetric_scores.csv")
has_altmetric  = os.path.exists(altmetric_path)
alt_df = pd.read_csv(altmetric_path) if has_altmetric else pd.DataFrame()

print(f"  {len(works_df)} cited works, {len(authors_df)} cited authors"
      + (f", {len(alt_df)} Altmetric records" if has_altmetric else " (no Altmetric data)"))

# ── Helpers ────────────────────────────────────────────────────────────────────

PLOTLY_CONF = dict(
    displayModeBar=True,
    modeBarButtonsToRemove=["select2d", "lasso2d", "toggleSpikelines"],
    responsive=True,
)

def fig_html(fig) -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       config=PLOTLY_CONF)

def short_title(t, n=60):
    t = str(t)
    return t[:n] + "…" if len(t) > n else t

# ── Figure 1: Top cited works ─────────────────────────────────────────────────
top_works = works_df.head(TOP_N).copy()
top_works["label"] = top_works.apply(
    lambda r: f"{r['authors'].split(';')[0].strip()[:28]} ({int(r['year']) if pd.notna(r['year']) else '?'})",
    axis=1
)
top_works["hover"] = top_works.apply(
    lambda r: (
        f"<b>{short_title(r['title'])}</b><br>"
        f"{r['authors'][:80]}<br>"
        f"Year: {r['year']}  ·  Cited by UPF papers: {r['times_cited_by_upf']:,}<br>"
        f"Total citations: {int(r['total_citations']) if pd.notna(r['total_citations']) else '?':,}<br>"
        f"DOI: {r['doi']}"
    ), axis=1
)

fig1 = go.Figure(go.Bar(
    x=top_works["times_cited_by_upf"],
    y=top_works["label"],
    orientation="h",
    marker_color=top_works["times_cited_by_upf"],
    marker_colorscale="Blues",
    marker_showscale=False,
    text=top_works["times_cited_by_upf"].astype(int),
    textposition="outside",
    hovertext=top_works["hover"],
    hoverinfo="text",
))
fig1.update_layout(
    title=f"Top {TOP_N} Works Cited by UPF Literature",
    xaxis_title="Times cited by UPF papers",
    yaxis=dict(autorange="reversed", tickfont=dict(size=11)),
    height=max(500, TOP_N * 26),
    margin=dict(l=260, r=60, t=50, b=40),
    template="plotly_white",
)

# ── Figure 2: External influencers ────────────────────────────────────────────
# Exclude likely mass-author GBD entries: require citations_per_work ≥ 10
# (people with one cited work that has 200 co-authors)
ext = authors_df[authors_df["in_upf_literature"] == False].copy()
ext["cit_per_work"] = ext["times_cited_total"] / ext["works_cited"].clip(lower=1)
ext = ext[ext["cit_per_work"] >= 10].head(TOP_N)
ext["hover"] = ext.apply(
    lambda r: (
        f"<b>{r['author_name']}</b><br>"
        f"{r.get('institution','') or '—'} · {r.get('country','') or '—'}<br>"
        f"Works cited: {r['works_cited']}  ·  Total cites in UPF: {r['times_cited_total']:,}<br>"
        f"Avg per work: {r['cit_per_work']:.0f}"
    ), axis=1
)

fig2 = go.Figure(go.Bar(
    x=ext["times_cited_total"],
    y=ext["author_name"],
    orientation="h",
    marker_color="#9b59b6",
    text=ext["times_cited_total"].astype(int),
    textposition="outside",
    hovertext=ext["hover"],
    hoverinfo="text",
))
fig2.update_layout(
    title="External Influencers — Cited in UPF Literature but Not UPF Authors",
    xaxis_title="Total citations in UPF papers",
    yaxis=dict(autorange="reversed", tickfont=dict(size=11)),
    height=max(400, len(ext) * 26 + 80),
    margin=dict(l=200, r=60, t=50, b=40),
    template="plotly_white",
)

# ── Figure 3: Insider vs outsider scatter ────────────────────────────────────
# x = times_cited_total in UPF literature
# y = total_citations (overall academic impact, proxy from works)
# Only authors with known total_citations — merge from works_df
author_cit = (
    authors_df[authors_df["times_cited_total"] >= 20].copy()
)
author_cit["cit_per_work"] = author_cit["times_cited_total"] / author_cit["works_cited"].clip(1)
author_cit["category"] = author_cit["in_upf_literature"].map(
    {True: "UPF researcher", False: "External voice"}
)
author_cit["hover"] = author_cit.apply(
    lambda r: (
        f"<b>{r['author_name']}</b><br>"
        f"{r.get('institution','') or '—'}<br>"
        f"Cited in UPF papers: {r['times_cited_total']:,} across {r['works_cited']} works"
    ), axis=1
)

fig3 = px.scatter(
    author_cit,
    x="times_cited_total",
    y="cit_per_work",
    color="category",
    size="works_cited",
    size_max=20,
    color_discrete_map={"UPF researcher": "#2980b9", "External voice": "#9b59b6"},
    hover_name="author_name",
    custom_data=["hover"],
    log_x=True,
    log_y=True,
    title="Citation Influence: UPF Researchers vs External Voices",
    labels={
        "times_cited_total": "Total citations in UPF papers (log)",
        "cit_per_work":      "Citations per work (log)",
    },
    template="plotly_white",
    height=500,
)
fig3.update_traces(hovertemplate="%{customdata[0]}<extra></extra>")

# ── Figure 4: Top UPF insiders by citation influence ─────────────────────────
insiders = authors_df[authors_df["in_upf_literature"] == True].head(TOP_N).copy()
insiders["hover"] = insiders.apply(
    lambda r: (
        f"<b>{r['author_name']}</b><br>"
        f"{r.get('institution','') or '—'} · {r.get('country','') or '—'}<br>"
        f"Works cited: {r['works_cited']}  ·  Total: {r['times_cited_total']:,}"
    ), axis=1
)

fig4 = go.Figure(go.Bar(
    x=insiders["times_cited_total"],
    y=insiders["author_name"],
    orientation="h",
    marker_color="#2980b9",
    text=insiders["times_cited_total"].astype(int),
    textposition="outside",
    hovertext=insiders["hover"],
    hoverinfo="text",
))
fig4.update_layout(
    title=f"Top {TOP_N} UPF Researchers by Citation Impact",
    xaxis_title="Total citations in UPF papers",
    yaxis=dict(autorange="reversed", tickfont=dict(size=11)),
    height=max(500, TOP_N * 26),
    margin=dict(l=200, r=60, t=50, b=40),
    template="plotly_white",
)

# ── Figure 5 (optional): Altmetric ───────────────────────────────────────────
fig5 = None
if has_altmetric and not alt_df.empty:
    alt_top = alt_df.nlargest(TOP_N, "altmetric_score").copy()
    alt_top["label"] = alt_top.apply(
        lambda r: f"{str(r.get('title',''))[:50]}… ({int(r['year']) if pd.notna(r.get('year')) else '?'})",
        axis=1
    )
    # Stacked bar: news / policy / tweets / blogs / reddit / wikipedia
    sources = [
        ("news_mentions",       "News",       "#e74c3c"),
        ("policy_mentions",     "Policy",     "#2ecc71"),
        ("tweet_mentions",      "Twitter/X",  "#3498db"),
        ("blog_mentions",       "Blogs",      "#f39c12"),
        ("reddit_mentions",     "Reddit",     "#ff6b35"),
        ("wikipedia_mentions",  "Wikipedia",  "#9b59b6"),
    ]
    fig5 = go.Figure()
    for col, name, color in sources:
        if col in alt_top.columns:
            fig5.add_trace(go.Bar(
                name=name,
                y=alt_top["label"],
                x=alt_top[col].fillna(0).astype(int),
                orientation="h",
                marker_color=color,
            ))
    fig5.update_layout(
        barmode="stack",
        title=f"Top {TOP_N} UPF Papers by Altmetric Attention Score",
        xaxis_title="Mentions by source",
        yaxis=dict(autorange="reversed", tickfont=dict(size=10)),
        height=max(500, len(alt_top) * 28 + 80),
        margin=dict(l=380, r=60, t=50, b=40),
        template="plotly_white",
        legend=dict(orientation="h", y=1.05),
    )

# ── Figure 6 (optional): Altmetric score vs academic citations ───────────────
fig6 = None
if has_altmetric and not alt_df.empty and "upf_citations" in alt_df.columns:
    fig6 = px.scatter(
        alt_df[alt_df["altmetric_score"] > 0],
        x="upf_citations",
        y="altmetric_score",
        size="altmetric_score",
        size_max=20,
        color="news_mentions",
        color_continuous_scale="Reds",
        hover_name="title",
        custom_data=["doi", "year", "news_mentions", "policy_mentions", "tweet_mentions"],
        log_x=True,
        log_y=True,
        title="Academic Impact vs Public Attention",
        labels={
            "upf_citations":    "Academic citations (log)",
            "altmetric_score":  "Altmetric score (log)",
            "news_mentions":    "News mentions",
        },
        template="plotly_white",
        height=500,
    )
    fig6.update_traces(
        hovertemplate=(
            "<b>%{hovertext}</b><br>"
            "Year: %{customdata[1]}  ·  DOI: %{customdata[0]}<br>"
            "Citations: %{x:.0f}  ·  Altmetric: %{y:.1f}<br>"
            "News: %{customdata[2]}  ·  Policy: %{customdata[3]}  ·  Tweets: %{customdata[4]}"
            "<extra></extra>"
        )
    )

# ── Assemble HTML ─────────────────────────────────────────────────────────────
sections = [
    ("§1 — Most Cited Works",
     "The works cited most frequently across all UPF papers — the foundational "
     "canon the field builds on. Hover for full title, authors and DOI.",
     fig_html(fig1)),

    ("§2 — External Influencers",
     "Authors cited heavily in the UPF literature who are <em>not</em> themselves UPF "
     "researchers. These are people whose ideas the field draws on without always "
     "co-authoring with them — policy voices, epidemiologists, sustainability "
     "scientists. Mass-authored multi-institutional papers (e.g. GBD reports) are "
     "filtered out by requiring ≥10 citations per individual work.",
     fig_html(fig2)),

    ("§3 — Insider vs Outsider Influence",
     "Each dot is an author. <b>Blue = UPF researcher</b> (publishes in the UPF "
     "literature); <b>purple = external voice</b> (cited but not authoring). "
     "X axis: total citations their work receives within the UPF corpus. "
     "Y axis: citations per work (measures intensity of influence per paper, not "
     "just volume). Dot size = number of works cited. Both axes are log-scaled.",
     fig_html(fig3)),

    ("§4 — UPF Researchers by Citation Impact",
     "The most-cited UPF researchers — those whose papers are referenced most "
     "often by other UPF papers. This reflects intellectual centrality within the "
     "field, not just publication count.",
     fig_html(fig4)),
]

if fig5 is not None:
    sections.append((
        "§5 — Altmetric Attention Scores",
        "Public and policy attention received by UPF papers, broken down by source. "
        "<b>News</b>: mainstream media coverage. <b>Policy</b>: government or NGO "
        "policy documents. <b>Twitter/X</b>: social media posts linking the paper. "
        "<b>Blogs</b>: science blogs and commentary. High policy scores indicate "
        "papers that crossed from academia into governance.",
        fig_html(fig5),
    ))

if fig6 is not None:
    sections.append((
        "§6 — Academic Impact vs Public Attention",
        "Are highly-cited papers also the most publicly discussed? Each dot is a "
        "paper. Papers in the <em>top-right</em> are both academically influential "
        "and publicly visible. Papers in the <em>top-left</em> have outsized media "
        "attention relative to their citation count — often review articles or "
        "papers that were actively promoted. Colour = news mentions.",
        fig_html(fig6),
    ))

nav_items = "".join(
    f'<a href="#sec{i}">{title.split(" — ")[0]}</a>'
    for i, (title, _, _) in enumerate(sections)
)

section_html = ""
for i, (title, explanation, chart) in enumerate(sections):
    section_html += f"""
    <section id="sec{i}">
      <h2>{title}</h2>
      <p class="explain">{explanation}</p>
      <div class="chart">{chart}</div>
    </section>
    """

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPF Influence Analysis Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
        background:#f5f6fa;color:#2c3e50}}
  #hdr{{background:#1a2742;color:#fff;padding:18px 32px}}
  #hdr h1{{font-size:1.3rem;font-weight:700}}
  #hdr p{{font-size:0.8rem;opacity:.65;margin-top:4px}}
  nav{{background:#fff;border-bottom:1px solid #dde;padding:10px 32px;
       display:flex;flex-wrap:wrap;gap:6px;position:sticky;top:0;z-index:50;
       box-shadow:0 1px 4px rgba(0,0,0,.06)}}
  nav a{{padding:4px 12px;border-radius:20px;background:#eef0f8;color:#1a2742;
         text-decoration:none;font-size:.78rem;font-weight:600;white-space:nowrap}}
  nav a:hover{{background:#1a2742;color:#fff}}
  main{{max-width:1200px;margin:0 auto;padding:24px 32px 60px}}
  section{{background:#fff;border-radius:8px;padding:24px;margin-bottom:28px;
           box-shadow:0 1px 6px rgba(0,0,0,.06)}}
  h2{{font-size:1.05rem;font-weight:700;color:#1a2742;margin-bottom:8px}}
  .explain{{font-size:.84rem;color:#555;line-height:1.55;margin-bottom:16px;
            max-width:820px}}
  .chart{{overflow-x:auto}}
  footer{{text-align:center;padding:20px;font-size:.75rem;color:#aaa}}
</style>
</head>
<body>
<div id="hdr">
  <h1>UPF Influence Analysis</h1>
  <p>Reference network · {"Altmetric attention scores · " if has_altmetric else ""}OpenAlex</p>
</div>
<nav>{nav_items}</nav>
<main>
{section_html}
</main>
<footer>Generated {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')} · Data: OpenAlex{' + Altmetric' if has_altmetric else ''}</footer>
</body>
</html>
"""

os.makedirs(OUTPUT_DIR, exist_ok=True)
with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
    fh.write(html)

size_kb = os.path.getsize(OUTPUT_FILE) // 1024
print(f"\nWritten: {OUTPUT_FILE}  ({size_kb} KB)")
if not has_altmetric:
    print("  Tip: run influence_analysis.py with your ALTMETRIC_KEY to add §5 and §6")

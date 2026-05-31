#!/usr/bin/env python3
"""
make_paper_report.py
Interactive Cytoscape.js citation-network report for a single paper.

The full field corpus is shown as a background graph; the focal paper and its
n-hop ego neighbourhood are highlighted.  Depth (n=0/1/2) is controlled live
in the browser.  No PDF output.

Usage:
  python make_paper_report.py --paper "Zutphen" --data-dir data/flavonoids
  python make_paper_report.py --doi "10.1093/ajcn/nqac055" --data-dir data/flavanol
  python make_paper_report.py --paper-id "https://openalex.org/W1234" \\
        --data-dir data/flavonoids --corpus-size 3000
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_DATA_BASE  = "data"
OLLAMA_URL         = "http://localhost:11434/api/generate"
DEFAULT_LLM_MODEL  = "llama3.1"
DEFAULT_CORPUS_SIZE = 2000   # top-N papers by citations; 0 = whole corpus


# ── Config ────────────────────────────────────────────────────────────────────

def _load_cfg(extra_dir=None):
    def _read(p):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return {}
    base = _read(os.path.join(os.path.dirname(__file__), "config.json")) or _read("config.json")
    return {**base, **(_read(os.path.join(extra_dir, "config.json")) if extra_dir else {})}


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(data_dir):
    def _csv(name, **kw):
        p = os.path.join(data_dir, name)
        if not os.path.exists(p):
            return None
        df = pd.read_csv(p, low_memory=False, **kw)
        for c in df.columns:
            if "id" in c.lower():
                df[c] = df[c].astype(str)
        return df

    papers = _csv("papers_detail.csv")
    cit_path = os.path.join(data_dir, "citation_edges_work.csv")
    if os.path.exists(cit_path):
        cit = pd.read_csv(cit_path, low_memory=False,
                          dtype={"citing_work_id": str, "cited_work_id": str})
    else:
        cit = None
    return {"papers": papers, "cit_edges": cit}


# ── Paper lookup ──────────────────────────────────────────────────────────────

def find_paper(query, papers_df, by="title"):
    if papers_df is None:
        sys.exit("ERROR: papers_detail.csv not found.")
    if by == "id":
        m = papers_df[papers_df["work_id"] == query]
    elif by == "doi":
        q = query.lower().replace("https://doi.org/", "").strip()
        m = papers_df[papers_df["doi"].str.lower().str.contains(q, na=False)]
    else:
        m = papers_df[papers_df["title"].str.contains(query, case=False, na=False)]

    if len(m) == 0:
        print(f"No paper found matching '{query}'.")
        return None, None
    m = m.sort_values("citations", ascending=False)
    if len(m) > 1:
        print(f"Found {len(m)} matches — using top result. Others:")
        for _, r in m.head(5).iloc[1:].iterrows():
            print(f"  [{r.get('year','')}]  {str(r['title'])[:70]}  ({r.get('citations',0)} cit.)")
        print("  Use --paper-id to specify exactly.\n")
    row = m.iloc[0]
    return str(row["work_id"]), row


# ── Graph data preparation ────────────────────────────────────────────────────

def prepare_graph_data(papers_df, cit_edges, focal_id,
                       corpus_size=DEFAULT_CORPUS_SIZE, max_ego=80):
    """Return (nodes_list, edges_list, search_index) ready for JSON serialisation.

    Node fields: id, label, title, year, citations, journal, study_type,
                 depth (0=focal,1=d1,2=d2,3=corpus), role, size.
    Edge fields: id, source, target.
    """
    if papers_df is None or cit_edges is None:
        return [], [], []

    # ── Corpus: top-N papers published same year or later as focal ───────────
    sorted_papers = papers_df.sort_values("citations", ascending=False)

    focal_yr_s = papers_df.loc[papers_df["work_id"] == focal_id, "year"]
    focal_year = None
    if len(focal_yr_s):
        try:
            focal_year = int(float(focal_yr_s.iloc[0]))
        except (ValueError, TypeError):
            pass
    if focal_year is not None:
        yr_num = pd.to_numeric(sorted_papers["year"], errors="coerce").fillna(0).astype(int)
        sorted_papers = sorted_papers[yr_num >= focal_year]

    if corpus_size > 0:
        corpus_df = sorted_papers.head(corpus_size).copy()
    else:
        corpus_df = sorted_papers.copy()

    corpus_ids = set(corpus_df["work_id"])

    # Always ensure focal is present
    if focal_id not in corpus_ids:
        focal_rows = papers_df[papers_df["work_id"] == focal_id]
        corpus_df = pd.concat([focal_rows, corpus_df], ignore_index=True)
        corpus_ids.add(focal_id)

    # ── Ego neighbours ────────────────────────────────────────────────────────
    meta_cit = papers_df.set_index("work_id")["citations"].to_dict()

    def _top_neighbours(work_id, direction, n):
        if direction == "in":   # papers that cite work_id
            ids = cit_edges[cit_edges["cited_work_id"] == work_id]["citing_work_id"].tolist()
        else:                   # papers cited by work_id
            ids = cit_edges[cit_edges["citing_work_id"] == work_id]["cited_work_id"].tolist()
        ids.sort(key=lambda w: int(meta_cit.get(w, 0)), reverse=True)
        return set(ids[:n])

    d1_in  = _top_neighbours(focal_id, "in",  max_ego)   # papers that cite focal
    d1_out = _top_neighbours(focal_id, "out", max_ego)   # papers focal cites
    d1_all = d1_in | d1_out

    # Depth-2: top-k neighbours of each depth-1 node (cap total)
    d2_all: set = set()
    for nid in sorted(d1_all, key=lambda w: int(meta_cit.get(w, 0)), reverse=True)[:30]:
        d2_all |= _top_neighbours(nid, "in",  5)
        d2_all |= _top_neighbours(nid, "out", 5)
    d2_all -= d1_all
    d2_all.discard(focal_id)

    # ── All nodes ─────────────────────────────────────────────────────────────
    all_ids = corpus_ids | d1_all | d2_all
    all_papers = papers_df[papers_df["work_id"].isin(all_ids)].copy()

    # Drop nodes published before the focal paper (unknown year → keep)
    if focal_year is not None:
        yr_num = pd.to_numeric(all_papers["year"], errors="coerce").fillna(focal_year).astype(int)
        all_papers = all_papers[(yr_num >= focal_year) | (all_papers["work_id"] == focal_id)]
        all_ids = set(all_papers["work_id"])

    # ── Edges (computed early so we can drop isolated corpus nodes) ───────────
    mask = (cit_edges["citing_work_id"].isin(all_ids) &
            cit_edges["cited_work_id"].isin(all_ids))
    edge_df = cit_edges[mask].drop_duplicates()

    # Drop corpus (depth=3) nodes that have no edges — they scatter randomly
    ego_ids = {focal_id} | d1_all | d2_all
    connected_ids = set(edge_df["citing_work_id"]) | set(edge_df["cited_work_id"])
    all_papers = all_papers[
        all_papers["work_id"].isin(ego_ids) |
        all_papers["work_id"].isin(connected_ids)
    ]
    all_ids = set(all_papers["work_id"])

    def _depth(wid):
        if wid == focal_id:  return 0
        if wid in d1_all:    return 1
        if wid in d2_all:    return 2
        return 3

    def _role(wid):
        if wid == focal_id:       return "focal"
        if wid in d1_in:          return "cites_focal"
        if wid in d1_out:         return "cited_by_focal"
        if wid in d2_all:         return "d2"
        return "corpus"

    ROLE_COLOR = {
        "focal":          "#EF553B",
        "cites_focal":    "#AB63FA",
        "cited_by_focal": "#00CC96",
        "d2":             "#FFA15A",
        "corpus":         "#b0bec5",
    }

    nodes = []
    for _, row in all_papers.drop_duplicates("work_id").iterrows():
        wid  = str(row["work_id"])
        cit  = int(row.get("citations", 0) or 0)
        yr   = row.get("year", "")
        yr_i = int(float(yr)) if yr and str(yr) not in ("", "nan") else 0
        dep  = _depth(wid)
        role = _role(wid)
        size = (40 if dep == 0 else
                max(8, min(50, 8 + 12 * math.log1p(cit))) if dep <= 2 else
                max(6, min(28, 6 + 8 * math.log1p(cit))))
        nodes.append({"data": {
            "id":         wid,
            "label":      (str(row.get("title", ""))[:30] + "…") if len(str(row.get("title",""))) > 30 else str(row.get("title","")),
            "title":      str(row.get("title", "")),
            "year":       yr_i,
            "citations":  cit,
            "journal":    str(row.get("journal", "")),
            "study_type": str(row.get("study_type", "")),
            "doi":        str(row.get("doi", "")),
            "depth":      dep,
            "role":       role,
            "color":      ROLE_COLOR[role],
            "size":       size,
            "focal":      dep == 0,
        }})

    edges = [{"data": {
        "id":     f"{r.citing_work_id}__{r.cited_work_id}",
        "source": r.citing_work_id,
        "target": r.cited_work_id,
    }} for r in edge_df.itertuples(index=False)]

    # ── Search index: all corpus papers ───────────────────────────────────────
    search_index = []
    for _, row in corpus_df.drop_duplicates("work_id").iterrows():
        search_index.append({
            "id":    str(row["work_id"]),
            "title": str(row.get("title", "")),
            "year":  int(float(row["year"])) if str(row.get("year","")) not in ("","nan") else 0,
            "cit":   int(row.get("citations", 0) or 0),
        })
    search_index.sort(key=lambda x: -x["cit"])

    print(f"  corpus={len(corpus_df)}  ego_d1={len(d1_all)}  ego_d2={len(d2_all)}  "
          f"edges={len(edges)}")
    return nodes, edges, search_index


# ── Statistics & citing papers ────────────────────────────────────────────────

def paper_stats(paper_id, data, field_title):
    papers, ce = data.get("papers"), data.get("cit_edges")
    stats = {"work_id": paper_id, "field": field_title}
    if papers is not None:
        r = papers[papers["work_id"] == paper_id]
        if len(r):
            r = r.iloc[0]
            for col in ["title","doi","year","journal","citations",
                        "study_type","author_count","open_access"]:
                stats[col] = r.get(col, "")
    if ce is not None:
        stats["n_citing"] = int((ce["cited_work_id"]  == paper_id).sum())
        stats["n_cited"]  = int((ce["citing_work_id"] == paper_id).sum())
    return stats


def top_citing_papers(paper_id, data, n=25):
    ce, papers = data.get("cit_edges"), data.get("papers")
    if ce is None or papers is None:
        return []
    ids = ce[ce["cited_work_id"] == paper_id]["citing_work_id"].tolist()
    sub = papers[papers["work_id"].isin(ids)].sort_values("citations", ascending=False)
    def _yr(v):
        try: return int(float(v))
        except (ValueError, TypeError): return ""
    return [{"title":   str(r.get("title","")),
             "year":    _yr(r.get("year","")),
             "journal": str(r.get("journal","")),
             "citations": int(r.get("citations",0)),
             "doi":     str(r.get("doi",""))}
            for _, r in sub.head(n).iterrows()]


# ── LLM via Ollama ────────────────────────────────────────────────────────────

def generate_llm_text(stats, citing_papers, model=DEFAULT_LLM_MODEL):
    field = stats.get("field", "this field")
    title = stats.get("title", "this paper")
    citing_str = "\n".join(
        f"  - {p['title'][:70]} ({p['year']}, {p['citations']} cit.)"
        for p in citing_papers[:8]) or "  (not available)"
    prompt = (
        f"Write a brief academic commentary (2 paragraphs, third person, factual) "
        f"about the impact of this paper within the field of {field}.\n"
        f"First paragraph: describe the paper and its study type.\n"
        f"Second paragraph: describe its influence in the {field} literature.\n\n"
        f"Title: {title}\n"
        f"Year: {stats.get('year','?')}  Journal: {stats.get('journal','')}\n"
        f"Study type: {stats.get('study_type','')}\n"
        f"Total citations: {stats.get('citations','?')}\n"
        f"Citing papers in {field} corpus: {stats.get('n_citing','?')}\n\n"
        f"Top citing papers:\n{citing_str}\n\n"
        f"Write the commentary now. Do not repeat the data as a list."
    )
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.3, "num_predict": 500}}).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read()).get("response", "").strip()
    except (urllib.error.URLError, OSError):
        return None


# ── HTML (Cytoscape full-page app) ────────────────────────────────────────────

def _short(s, n=35):
    s = str(s).strip()
    return s[:n] + "…" if len(s) > n else s


def write_html(stats, nodes, edges, search_index, citing_papers, output_path,
               llm_text=None):
    title    = stats.get("title", "Unknown Paper")
    year     = stats.get("year", "")
    jour     = stats.get("journal", "")
    doi      = stats.get("doi", "")
    field    = stats.get("field", "")
    cit      = stats.get("citations", "?")
    st       = stats.get("study_type", "")
    paper_id = stats.get("work_id", "")

    doi_href = f'<a href="{doi}" target="_blank" class="hdr-link">DOI ↗</a>' \
               if doi and doi != "nan" else ""
    oa_href  = f'<a href="{paper_id}" target="_blank" class="hdr-link">OpenAlex ↗</a>' \
               if paper_id.startswith("http") else ""

    llm_html = ""
    if llm_text:
        paras = "".join(f"<p>{p.strip()}</p>" for p in llm_text.split("\n\n") if p.strip())
        llm_html = (f'<div class="llm-box"><div class="llm-head">Impact commentary '
                    f'<span class="llm-tag">AI-generated · verify before use</span>'
                    f'</div>{paras}</div>')

    citing_rows = "".join(
        f"<tr><td>{p['year']}</td><td>{p['title'][:80]}</td>"
        f"<td>{p['journal'][:38]}</td><td class='num'>{p['citations']}</td></tr>"
        for p in citing_papers)

    elements_json    = json.dumps(nodes + edges, separators=(",", ":"))
    search_json      = json.dumps(search_index,  separators=(",", ":"))
    focal_json       = json.dumps(paper_id)
    corpus_size_note = f"{len(search_index):,} papers in corpus view"

    # year_min from corpus (depth=3) only — ego reference nodes may be older
    _corpus_years = [n["data"]["year"] for n in nodes
                     if n["data"].get("year", 0) and n["data"].get("depth", 3) == 3]
    _all_years    = [n["data"]["year"] for n in nodes if n["data"].get("year", 0)]
    year_min = min(_corpus_years) if _corpus_years else (min(_all_years) if _all_years else 1950)
    year_max = max(_all_years)    if _all_years    else 2025

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_short(title,70)} — Citation Network</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  font-size:13px;background:#f0f2f5;color:#222}}

/* ── App shell ── */
#app{{display:grid;grid-template-rows:auto 1fr auto;height:100vh;overflow:hidden}}

/* ── Top bar ── */
#topbar{{background:#1a252f;color:#fff;padding:.45rem 1rem;
  display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;flex-shrink:0}}
#topbar h1{{font-size:.88rem;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:38vw}}
#topbar .meta{{font-size:.72rem;color:#95a5a6;white-space:nowrap}}
.hdr-link{{color:#5dade2;font-size:.72rem;text-decoration:none;white-space:nowrap}}
.hdr-link:hover{{text-decoration:underline}}
#search-wrap{{position:relative;margin-left:auto;flex-shrink:0}}
#search-input{{width:220px;padding:.3rem .6rem;border:1px solid #4a6278;border-radius:4px;
  background:#243342;color:#ecf0f1;font-size:.8rem;outline:none}}
#search-input:focus{{border-color:#5dade2}}
#search-drop{{position:absolute;top:100%;right:0;width:380px;background:#fff;
  border:1px solid #ccd;border-radius:0 0 6px 6px;box-shadow:0 4px 12px rgba(0,0,0,.15);
  max-height:280px;overflow-y:auto;z-index:999;display:none}}
.search-item{{padding:.4rem .7rem;cursor:pointer;border-bottom:1px solid #f0f0f0;
  font-size:.78rem;line-height:1.4}}
.search-item:hover{{background:#eef4fb}}
.search-item .s-title{{color:#222;font-weight:500}}
.search-item .s-meta{{color:#888;font-size:.7rem}}

/* ── Main area ── */
#main{{display:grid;grid-template-columns:270px 1fr;overflow:hidden}}

/* ── Sidebar ── */
#sidebar{{background:#fff;border-right:1px solid #dde1e7;overflow-y:auto;
  display:flex;flex-direction:column}}
.sec{{padding:.65rem .85rem;border-bottom:1px solid #eef0f3}}
.sec h3{{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
  color:#7f8c8d;margin-bottom:.45rem}}
.chips{{display:grid;grid-template-columns:1fr 1fr;gap:.35rem}}
.chip{{background:#f7f9fc;border:1px solid #e3e8ef;border-radius:6px;
  padding:.3rem .5rem;text-align:center}}
.chip .cv{{font-size:.95rem;font-weight:700;color:#2c3e50}}
.chip .cl{{font-size:.62rem;color:#888;margin-top:1px}}

/* depth buttons */
.depth-row{{display:flex;gap:.4rem;margin-top:.2rem}}
.dbtn{{flex:1;padding:.35rem;border:2px solid #ccd;border-radius:5px;
  background:#fff;cursor:pointer;font-size:.8rem;text-align:center;transition:.15s}}
.dbtn:hover{{background:#eef4fb}}
.dbtn.active{{border-color:#2980b9;background:#eaf2fb;color:#2980b9;font-weight:600}}

/* corpus toggle */
.tog-row{{display:flex;align-items:center;gap:.5rem;margin-top:.35rem;font-size:.76rem}}
.tog{{position:relative;width:32px;height:16px;flex-shrink:0}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;inset:0;background:#ccc;border-radius:16px;cursor:pointer;transition:.2s}}
.tog-slider::before{{content:'';position:absolute;width:12px;height:12px;left:2px;top:2px;
  background:#fff;border-radius:50%;transition:.2s}}
.tog input:checked+.tog-slider{{background:#2980b9}}
.tog input:checked+.tog-slider::before{{transform:translateX(16px)}}

/* legend */
.leg{{display:flex;align-items:center;gap:.4rem;font-size:.7rem;margin-bottom:.28rem}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}

/* layout controls */
.ctrl-row{{display:flex;align-items:center;gap:.4rem;margin-bottom:.35rem}}
.ctrl-row label{{font-size:.73rem;color:#555;flex:1}}
select,button{{font-size:.73rem;border:1px solid #ccd;border-radius:4px;
  padding:.22rem .45rem;background:#fff;cursor:pointer}}
button.primary{{background:#34495e;color:#fff;border-color:#34495e}}
button.primary:hover{{background:#2c3e50}}

/* detail panel */
#detail{{flex:1;padding:.65rem .85rem;min-height:100px}}
#detail h3{{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
  color:#7f8c8d;margin-bottom:.45rem}}
#detail-body{{font-size:.76rem;color:#444;line-height:1.55}}
#detail-body .dt{{font-weight:600;color:#1a252f;line-height:1.4}}
#detail-body .hint{{color:#aaa;font-style:italic}}
#detail-body a{{color:#2980b9;font-size:.72rem}}

/* ── Cytoscape canvas ── */
#cy{{width:100%;height:100%;background:#fafbfc}}

/* ── Bottom panel ── */
#bottom{{background:#fff;border-top:2px solid #dde1e7;overflow-y:auto;
  max-height:260px;flex-shrink:0}}
#tabs{{display:flex;border-bottom:1px solid #dde}}
.tab{{padding:.42rem 1rem;font-size:.76rem;cursor:pointer;
  border-bottom:3px solid transparent;color:#666}}
.tab.active{{border-bottom-color:#2980b9;color:#2980b9;font-weight:600}}
.tab-pane{{display:none;padding:.6rem .9rem}}
.tab-pane.active{{display:block}}
table{{border-collapse:collapse;width:100%;font-size:.76rem}}
th{{background:#2c3e50;color:#fff;padding:.32rem .65rem;text-align:left;
  position:sticky;top:0;z-index:1}}
td{{padding:.28rem .65rem;border-bottom:1px solid #eee;vertical-align:top}}
tr:hover td{{background:#f5f7fa}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.llm-box{{background:#fffdf5;border:1px solid #f0e0a0;border-radius:5px;
  padding:.7rem .9rem;font-size:.8rem;line-height:1.6}}
.llm-box p{{margin:.35em 0}}
.llm-head{{font-weight:600;margin-bottom:.4rem;color:#5d4e00}}
.llm-tag{{font-size:.66rem;font-weight:400;color:#e67e22;margin-left:.35em}}

/* ── Year slider ── */
.yr-inputs{{display:flex;align-items:center;gap:.35rem;margin-bottom:.3rem}}
.yr-inputs input[type=number]{{width:60px;padding:.2rem .35rem;font-size:.8rem;
  border:1px solid #ccd;border-radius:4px;text-align:center;background:#fff;color:#2980b9;font-weight:600}}
.yr-inputs span{{color:#888;font-size:.75rem}}
.dual-range{{position:relative;height:28px;margin:.2rem 0 .1rem}}
.dual-range input[type=range]{{
  position:absolute;width:100%;height:4px;top:12px;
  pointer-events:none;-webkit-appearance:none;appearance:none;
  background:transparent;outline:none;margin:0}}
.dual-range input[type=range]::-webkit-slider-thumb{{
  pointer-events:all;-webkit-appearance:none;
  width:14px;height:14px;border-radius:50%;
  background:#2980b9;cursor:pointer;
  border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.dual-range input[type=range]::-moz-range-thumb{{
  pointer-events:all;width:14px;height:14px;border-radius:50%;
  background:#2980b9;cursor:pointer;
  border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3);
  border:none}}
.dr-track{{position:absolute;top:14px;left:0;right:0;height:4px;
  background:#e0e6ec;border-radius:2px;pointer-events:none}}
.dr-fill{{position:absolute;height:100%;background:#2980b9;border-radius:2px}}
</style>
</head>
<body>
<div id="app">

<!-- TOP BAR -->
<div id="topbar">
  <h1>★ {title}</h1>
  <span class="meta">{year}</span>
  <span class="meta">{_short(jour,40)}</span>
  <span class="meta">{field}</span>
  {doi_href} {oa_href}
  <div id="search-wrap">
    <input id="search-input" type="text" placeholder="Search corpus…" autocomplete="off">
    <div id="search-drop"></div>
  </div>
</div>

<!-- MAIN -->
<div id="main">

  <!-- SIDEBAR -->
  <div id="sidebar">

    <div class="sec">
      <h3>Paper metrics</h3>
      <div class="chips">
        <div class="chip"><div class="cv">{cit}</div><div class="cl">Total citations</div></div>
        <div class="chip"><div class="cv">{stats.get('n_citing','?')}</div><div class="cl">Citing in corpus</div></div>
        <div class="chip"><div class="cv">{stats.get('n_cited','?')}</div><div class="cl">References in corpus</div></div>
        <div class="chip"><div class="cv">{st or '—'}</div><div class="cl">Study type</div></div>
        <div class="chip"><div class="cv">{stats.get('author_count','?')}</div><div class="cl">Authors</div></div>
        <div class="chip"><div class="cv">{'Open' if str(stats.get('open_access','')).lower()=='true' else 'Closed'}</div><div class="cl">Access</div></div>
      </div>
    </div>

    <div class="sec">
      <h3>Ego depth</h3>
      <div class="depth-row">
        <div class="dbtn" data-d="0">n=0<br><span style="font-size:.65rem;font-weight:400">focal only</span></div>
        <div class="dbtn active" data-d="1">n=1<br><span style="font-size:.65rem;font-weight:400">direct</span></div>
        <div class="dbtn" data-d="2">n=2<br><span style="font-size:.65rem;font-weight:400">2-hop</span></div>
      </div>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-corpus" checked>
          <span class="tog-slider"></span></label>
        Show corpus background
      </div>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-labels">
          <span class="tog-slider"></span></label>
        Show labels
      </div>
    </div>

    <div class="sec">
      <h3>Year filter</h3>
      <div class="yr-inputs">
        <input type="number" id="yr-from-num" min="{year_min}" max="{year_max}" value="{year_min}" step="1">
        <span>–</span>
        <input type="number" id="yr-to-num" min="{year_min}" max="{year_max}" value="{year_max}" step="1">
      </div>
      <div class="dual-range">
        <div class="dr-track"><div id="yr-fill" class="dr-fill"></div></div>
        <input type="range" id="yr-from" min="{year_min}" max="{year_max}" value="{year_min}" step="1">
        <input type="range" id="yr-to"   min="{year_min}" max="{year_max}" value="{year_max}" step="1">
      </div>
    </div>

    <div class="sec">
      <h3>Legend</h3>
      <div class="leg"><span class="leg-dot" style="background:#EF553B"></span>Focal paper</div>
      <div class="leg"><span class="leg-dot" style="background:#AB63FA"></span>Papers citing this work (depth 1)</div>
      <div class="leg"><span class="leg-dot" style="background:#00CC96"></span>Papers cited by this work (depth 1)</div>
      <div class="leg"><span class="leg-dot" style="background:#FFA15A"></span>Depth-2 neighbours</div>
      <div class="leg"><span class="leg-dot" style="background:#b0bec5"></span>Corpus (background)</div>
      <div style="font-size:.67rem;color:#999;margin-top:.3rem">{corpus_size_note} · node size ∝ log(citations)</div>
    </div>

    <div class="sec">
      <h3>Layout</h3>
      <div class="ctrl-row">
        <label>Algorithm</label>
        <select id="layout-sel">
          <option value="cose">Force-directed (cose)</option>
          <option value="concentric">Concentric (depth)</option>
          <option value="breadthfirst">Breadth-first</option>
          <option value="circle">Circle</option>
          <option value="random">Random</option>
        </select>
      </div>
      <div class="ctrl-row">
        <button class="primary" id="fit-btn">Fit</button>
        <button class="primary" id="relayout-btn">Re-layout</button>
        <button class="primary" id="center-btn">Centre focal</button>
      </div>
    </div>

    <div id="detail">
      <h3>Selected paper</h3>
      <div id="detail-body"><span class="hint">Click a node to see details</span></div>
    </div>

  </div><!-- /sidebar -->

  <!-- CYTOSCAPE -->
  <div id="cy"></div>

</div><!-- /main -->

<!-- BOTTOM PANEL -->
<div id="bottom">
  <div id="tabs">
    <div class="tab active" data-tab="citing">Top citing papers ({len(citing_papers)})</div>
    {('<div class="tab" data-tab="llm">Impact commentary</div>') if llm_text else ""}
  </div>
  <div class="tab-pane active" id="tab-citing">
    <table>
      <tr><th>Year</th><th>Title</th><th>Journal</th><th>Citations</th></tr>
      {citing_rows}
    </table>
  </div>
  {(f'<div class="tab-pane" id="tab-llm">{llm_html}</div>') if llm_text else ""}
</div>

</div><!-- /app -->

<script>
/* ── Data ── */
const ELEMENTS   = {elements_json};
const SEARCH_IDX = {search_json};
const FOCAL_ID   = {focal_json};

/* ── Cytoscape init ── */
const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{ selector: 'node',
       style: {{
         'background-color':   'data(color)',
         'width':              'data(size)',
         'height':             'data(size)',
         'label':              '',
         'font-size':          '8px',
         'text-valign':        'bottom',
         'text-halign':        'center',
         'text-margin-y':      '2px',
         'text-wrap':          'wrap',
         'text-max-width':     '90px',
         'color':              '#333',
         'text-outline-color': '#fff',
         'text-outline-width': '1.5px',
         'border-width':       0,
         'opacity':            0.9,
         'z-index':            1,
       }}
    }},
    {{ selector: 'node[?focal]',
       style: {{
         'shape':         'star',
         'border-width':  3,
         'border-color':  '#c0392b',
         'font-size':     '9px',
         'font-weight':   'bold',
         'z-index':       20,
       }}
    }},
    {{ selector: 'node[depth = 3]',
       style: {{ 'opacity': 0.55, 'z-index': 0 }}
    }},
    {{ selector: 'node:selected',
       style: {{ 'border-width': 3, 'border-color': '#2980b9', 'z-index': 25 }}
    }},
    {{ selector: 'edge',
       style: {{
         'width':               0.5,
         'line-color':          '#cfd8dc',
         'target-arrow-color':  '#b0bec5',
         'target-arrow-shape':  'triangle',
         'arrow-scale':         0.5,
         'curve-style':         'bezier',
         'opacity':             0.4,
       }}
    }},
    {{ selector: 'edge.active-edge',
       style: {{
         'width':              1.5,
         'line-color':         '#78909c',
         'target-arrow-color': '#607d8b',
         'opacity':            0.8,
         'z-index':            5,
       }}
    }},
    {{ selector: '.corpus-hidden',
       style: {{ 'display': 'none' }}
    }},
    {{ selector: '.year-hidden',
       style: {{ 'display': 'none' }}
    }},
  ],
  layout: {{ name: 'cose', animate: false,
             nodeRepulsion: 6000, idealEdgeLength: 70, gravity: 0.3,
             numIter: 500 }},
  wheelSensitivity: 0.3,
}});

/* ── State ── */
let currentFocal = FOCAL_ID;
let currentDepth = 1;
let showCorpus   = true;
let showLabels   = false;

const YEAR_MIN = {year_min};
const YEAR_MAX = {year_max};

/* ── Year filter ── */
function applyYearFilter(y1, y2) {{
  cy.nodes().forEach(n => {{
    const yr = n.data('year');
    if (yr && (yr < y1 || yr > y2)) n.addClass('year-hidden');
    else                             n.removeClass('year-hidden');
  }});
}}

function _updateFill(y1, y2) {{
  const span = (YEAR_MAX - YEAR_MIN) || 1;
  document.getElementById('yr-fill').style.left  = ((y1 - YEAR_MIN) / span * 100) + '%';
  document.getElementById('yr-fill').style.right = ((YEAR_MAX - y2) / span * 100) + '%';
}}

let _yrTimer = null;
function syncYearSlider() {{
  let y1 = parseInt(document.getElementById('yr-from').value);
  let y2 = parseInt(document.getElementById('yr-to').value);
  if (y1 > y2) {{
    const e = document.activeElement;
    if (e && e.id === 'yr-from') {{ y1 = y2; document.getElementById('yr-from').value = y1; }}
    else                          {{ y2 = y1; document.getElementById('yr-to').value   = y2; }}
  }}
  document.getElementById('yr-from-num').value = y1;
  document.getElementById('yr-to-num').value   = y2;
  _updateFill(y1, y2);
  // debounce the expensive node-walk
  clearTimeout(_yrTimer);
  _yrTimer = setTimeout(() => applyYearFilter(y1, y2), 250);
}}

function syncYearNums() {{
  let y1 = parseInt(document.getElementById('yr-from-num').value) || YEAR_MIN;
  let y2 = parseInt(document.getElementById('yr-to-num').value)   || YEAR_MAX;
  y1 = Math.max(YEAR_MIN, Math.min(YEAR_MAX, y1));
  y2 = Math.max(YEAR_MIN, Math.min(YEAR_MAX, y2));
  if (y1 > y2) [y1, y2] = [y2, y1];
  document.getElementById('yr-from-num').value = y1;
  document.getElementById('yr-to-num').value   = y2;
  document.getElementById('yr-from').value     = y1;
  document.getElementById('yr-to').value       = y2;
  _updateFill(y1, y2);
  applyYearFilter(y1, y2);
}}

document.getElementById('yr-from').addEventListener('input', syncYearSlider);
document.getElementById('yr-to').addEventListener('input', syncYearSlider);
document.getElementById('yr-from-num').addEventListener('change', syncYearNums);
document.getElementById('yr-to-num').addEventListener('change', syncYearNums);
document.getElementById('yr-from-num').addEventListener('keydown', e => {{ if (e.key==='Enter') syncYearNums(); }});
document.getElementById('yr-to-num').addEventListener('keydown',   e => {{ if (e.key==='Enter') syncYearNums(); }});

/* ── Depth visualisation ── */
function getEgoNodes(focalId, depth) {{
  const focal = cy.getElementById(focalId);
  if (!focal.length) return {{ focal, d1: cy.collection(), d2: cy.collection() }};
  const d1 = focal.neighborhood('node');
  const d2 = depth >= 2 ? d1.neighborhood('node').difference(d1).filter(n => n.id() !== focalId)
                         : cy.collection();
  return {{ focal, d1, d2 }};
}}

function applyDepth(focalId, depth) {{
  currentFocal = focalId;
  currentDepth = depth;

  // Reset edge classes
  cy.edges().removeClass('active-edge');

  const {{ focal, d1, d2 }} = getEgoNodes(focalId, depth);

  // Recolour nodes by relationship to new focal
  cy.nodes().forEach(n => {{
    const id = n.id();
    if (id === focalId) {{ n.data('color', '#EF553B'); return; }}
    const role = n.data('role') || 'corpus';
    // Recalculate role relative to current focal
    const edgesIn  = cy.edges(`[source = "${{id}}"][target = "${{focalId}}"]`);
    const edgesOut = cy.edges(`[source = "${{focalId}}"][target = "${{id}}"]`);
    if (edgesIn.length)  {{ n.data('color','#AB63FA'); return; }}
    if (edgesOut.length) {{ n.data('color','#00CC96'); return; }}
    if (d2.has(n))       {{ n.data('color','#FFA15A'); return; }}
    n.data('color','#b0bec5');
  }});

  // Active edges: connected to focal (d1) or d1 nodes (d2)
  if (depth >= 1) {{
    focal.connectedEdges().addClass('active-edge');
    if (depth >= 2) d1.connectedEdges().addClass('active-edge');
  }}

  // Labels — 'data(label)' only works in stylesheets, not inline .style() calls
  cy.nodes().forEach(n => n.style('label', showLabels ? (n.data('label') || '') : ''));

  updateDetailPanel(cy.getElementById(focalId).data());
}}

/* ── Corpus visibility ── */
function applyCorpusVisibility(visible) {{
  showCorpus = visible;
  if (visible) {{
    cy.nodes('[depth = 3]').removeClass('corpus-hidden');
  }} else {{
    cy.nodes('[depth = 3]').addClass('corpus-hidden');
  }}
}}

/* ── Detail panel ── */
function updateDetailPanel(d) {{
  if (!d) return;
  const doi = d.doi && d.doi !== 'nan' ? d.doi : '';
  const oa  = d.id  && d.id.startsWith('http') ? d.id : '';
  document.getElementById('detail-body').innerHTML =
    `<div class="dt">${{d.title || d.label}}</div>` +
    `<div style="margin:.3em 0 .1em;color:#555">${{d.year || '—'}} · ${{d.journal || ''}}</div>` +
    `<div style="color:#888">${{d.study_type || ''}}</div>` +
    `<div style="margin-top:.3em"><b>Citations:</b> ${{d.citations}}</div>` +
    (doi ? `<div style="margin-top:.3em"><a href="${{doi}}" target="_blank">DOI ↗</a>` : '') +
    (oa  ? ` <a href="${{oa}}"  target="_blank">OpenAlex ↗</a></div>` : '');
}}

/* ── Node click ── */
cy.on('tap', 'node', e => {{
  const d = e.target.data();
  updateDetailPanel(d);
}});
cy.on('dblclick', 'node', e => {{
  applyDepth(e.target.id(), currentDepth);
}});
cy.on('tap', e => {{
  if (e.target === cy)
    document.getElementById('detail-body').innerHTML =
      '<span class="hint">Click a node for details · Double-click to set as focal</span>';
}});

/* ── Depth buttons ── */
document.querySelectorAll('.dbtn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.dbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyDepth(currentFocal, parseInt(btn.dataset.d));
  }});
}});

/* ── Corpus toggle ── */
document.getElementById('show-corpus').addEventListener('change', e =>
  applyCorpusVisibility(e.target.checked));

/* ── Label toggle ── */
document.getElementById('show-labels').addEventListener('change', e => {{
  showLabels = e.target.checked;
  applyDepth(currentFocal, currentDepth);  // reapply to update labels
}});

/* ── Layout ── */
function runLayout(name) {{
  const opts = {{
    name, animate: false, fit: true, padding: 30,
    ...(name === 'cose' ? {{nodeRepulsion:6000,idealEdgeLength:70,gravity:0.3,numIter:500}} : {{}}),
    ...(name === 'concentric' ? {{
      concentric: n => n.data('depth') === 0 ? 4
                     : n.data('depth') === 1 ? 3
                     : n.data('depth') === 2 ? 2 : 1,
      levelWidth: () => 1,
    }} : {{}}),
  }};
  cy.layout(opts).run();
}}
document.getElementById('layout-sel').addEventListener('change', e => runLayout(e.target.value));
document.getElementById('relayout-btn').addEventListener('click', () =>
  runLayout(document.getElementById('layout-sel').value));
document.getElementById('fit-btn').addEventListener('click', () => cy.fit(30));
document.getElementById('center-btn').addEventListener('click', () => {{
  const n = cy.getElementById(currentFocal);
  if (n.length) cy.animate({{center:{{eles:n}},zoom:1.2}}, {{duration:400}});
}});

/* ── Tab switching ── */
document.getElementById('tabs').addEventListener('click', e => {{
  const tab = e.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  tab.classList.add('active');
  const pane = document.getElementById('tab-' + tab.dataset.tab);
  if (pane) pane.classList.add('active');
}});

/* ── Search ── */
const searchInput = document.getElementById('search-input');
const searchDrop  = document.getElementById('search-drop');

searchInput.addEventListener('input', () => {{
  const q = searchInput.value.trim().toLowerCase();
  if (q.length < 2) {{ searchDrop.style.display = 'none'; return; }}
  const hits = SEARCH_IDX.filter(p => p.title.toLowerCase().includes(q)).slice(0, 12);
  if (!hits.length) {{ searchDrop.style.display = 'none'; return; }}
  searchDrop.innerHTML = hits.map(p =>
    `<div class="search-item" data-id="${{p.id}}">
       <div class="s-title">${{p.title.substring(0,80)}}</div>
       <div class="s-meta">${{p.year}} · ${{p.cit.toLocaleString()}} citations</div>
     </div>`).join('');
  searchDrop.style.display = 'block';
}});

searchDrop.addEventListener('click', e => {{
  const item = e.target.closest('.search-item');
  if (!item) return;
  const id = item.dataset.id;
  searchInput.value = '';
  searchDrop.style.display = 'none';
  const node = cy.getElementById(id);
  if (node.length) {{
    cy.animate({{center:{{eles:node}},zoom:2}}, {{duration:500}});
    node.select();
    updateDetailPanel(node.data());
  }}
}});

document.addEventListener('click', e => {{
  if (!document.getElementById('search-wrap').contains(e.target))
    searchDrop.style.display = 'none';
}});

/* ── Init ── */
syncYearSlider();
applyDepth(FOCAL_ID, 1);
cy.fit(30);
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML  → {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--data-dir", default=None)
    _pre.add_argument("--prefix",   default=None)
    _pre_args, _ = _pre.parse_known_args()
    _hint = _pre_args.data_dir or (
        os.path.join(DEFAULT_DATA_BASE, _pre_args.prefix) if _pre_args.prefix else None)
    cfg = _load_cfg(_hint)

    ap = argparse.ArgumentParser(
        description="Cytoscape.js citation-network report for a single paper")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--paper",    help="Title substring search")
    grp.add_argument("--doi",      help="DOI")
    grp.add_argument("--paper-id", dest="paper_id", help="OpenAlex work ID")
    ap.add_argument("--prefix",       default=None)
    ap.add_argument("--data-dir",     default=None)
    ap.add_argument("--title",        default=cfg.get("title","Research Field"),
                    help="Field label")
    ap.add_argument("--output-dir",   default=None)
    ap.add_argument("--corpus-size",  type=int, default=DEFAULT_CORPUS_SIZE,
                    help=f"Top-N papers for background corpus (0=all, default {DEFAULT_CORPUS_SIZE})")
    ap.add_argument("--max-ego",      type=int, default=80,
                    help="Max direct neighbours per direction")
    ap.add_argument("--llm-model",    default=DEFAULT_LLM_MODEL)
    ap.add_argument("--no-llm",       action="store_true")
    args = ap.parse_args()

    cfg_prefix = cfg.get("prefix", "")
    data_dir = (args.data_dir or
                (os.path.join(DEFAULT_DATA_BASE, args.prefix or cfg_prefix)
                 if (args.prefix or cfg_prefix) else DEFAULT_DATA_BASE))
    out_prefix = args.prefix or os.path.basename(data_dir.rstrip("/\\"))

    print(f"Loading data from {data_dir} …")
    data = load_data(data_dir)

    if args.paper_id:
        paper_id, _ = find_paper(args.paper_id, data["papers"], by="id")
    elif args.doi:
        paper_id, _ = find_paper(args.doi, data["papers"], by="doi")
    else:
        paper_id, _ = find_paper(args.paper, data["papers"], by="title")
    if not paper_id:
        sys.exit(1)

    stats = paper_stats(paper_id, data, args.title)
    print(f"  {str(stats.get('title','?'))[:70]}")
    print(f"  year={stats.get('year','?')}  citations={stats.get('citations','?')}  "
          f"citing_in_corpus={stats.get('n_citing','?')}")

    print(f"Preparing graph (corpus_size={args.corpus_size}) …")
    nodes, edges, search_index = prepare_graph_data(
        data["papers"], data["cit_edges"], paper_id,
        corpus_size=args.corpus_size, max_ego=args.max_ego)

    top_citing = top_citing_papers(paper_id, data)

    llm_text = None
    if not args.no_llm:
        print(f"Generating LLM commentary ({args.llm_model}) …")
        llm_text = generate_llm_text(stats, top_citing, model=args.llm_model)
        print(f"  {len(llm_text)} chars" if llm_text else "  Ollama unavailable — skipping")

    slug = re.sub(r"[^a-z0-9]+", "_",
                  str(stats.get("title", paper_id))[:40].lower()).strip("_")
    out_dir = args.output_dir or os.path.join("reports", out_prefix, "papers", slug)
    os.makedirs(out_dir, exist_ok=True)

    write_html(stats, nodes, edges, search_index, top_citing,
               os.path.join(out_dir, "report.html"), llm_text=llm_text)
    print(f"\nOutput: {out_dir}/")


if __name__ == "__main__":
    main()

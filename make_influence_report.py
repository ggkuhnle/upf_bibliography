#!/usr/bin/env python3
"""
make_influence_report.py
Influence reports for a paper or an author: corpus network + global OpenAlex crawl.

Paper mode  (--paper / --doi / --paper-id):
  report.html        — compact corpus graph centred on the focal paper
  report_full.html   — full corpus graph
  influence.html     — global paper influence map (OpenAlex live crawl)

Author mode (--author / --author-id):
  author_corpus.html    — author position inside the field corpus
  author_influence.html — global author influence map (OpenAlex live crawl)

Both corpus and influence sections run by default; skip with --no-corpus / --no-influence.
Results are cached in cache/influence_cache.sqlite.

Usage (paper mode):
  python make_influence_report.py --doi "10.1093/ajcn/nqac055" --data-dir data/flavanol
  python make_influence_report.py --paper "Zutphen" --data-dir data/flavonoids
  python make_influence_report.py --doi "10.1093/..." --no-corpus

Usage (author mode):
  python make_influence_report.py --author "Monteiro" --data-dir data/upf
  python make_influence_report.py --author-id "https://openalex.org/A2149006804" --data-dir data/upf
  python make_influence_report.py --author "Hollman" --data-dir data/flavonoids --no-corpus
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd
import requests
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_DATA_BASE   = "data"
DEFAULT_CORPUS_SIZE = 2000
SMALL_CORPUS_SIZE   = 300
SMALL_MAX_EGO       = 40

OLLAMA_URL        = "http://localhost:11434/api/generate"
DEFAULT_LLM_MODEL = "llama3.1"

MAILTO        = "ggkuhnle@googlemail.com"
BASE_URL      = "https://api.openalex.org/works"
PAGE_SIZE     = 200
REQUEST_DELAY = 0.12
MAX_RETRIES   = 5
CACHE_FILE    = os.path.join(os.path.dirname(__file__), "cache", "influence_cache.sqlite")

DEFAULT_DEPTH     = 2
DEFAULT_MAX_NODES = 300
DEFAULT_MAX_REFS  = 50
DEFAULT_MAX_CITES = 100


# ── Config ────────────────────────────────────────────────────────────────────

def _load_cfg(extra_dir=None):
    def _read(p):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return {}
    base = (_read(os.path.join(os.path.dirname(__file__), "config.json"))
            or _read("config.json"))
    return {**base, **(_read(os.path.join(extra_dir, "config.json")) if extra_dir else {})}


# ═══════════════════════════════════════════════════════════════════════════════
# CORPUS SECTION
# Reads local CSV data produced by bibliometrics.py.
# ═══════════════════════════════════════════════════════════════════════════════

def load_data(data_dir):
    def _csv(name):
        p = os.path.join(data_dir, name)
        if not os.path.exists(p):
            return None
        df = pd.read_csv(p, low_memory=False)
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


def prepare_graph_data(papers_df, cit_edges, focal_id,
                       corpus_size=DEFAULT_CORPUS_SIZE, max_ego=80,
                       ego_adjacent_only=False):
    if papers_df is None or cit_edges is None:
        return [], [], []

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

    corpus_df = sorted_papers.head(corpus_size).copy() if corpus_size > 0 else sorted_papers.copy()
    corpus_ids = set(corpus_df["work_id"])
    if focal_id not in corpus_ids:
        focal_rows = papers_df[papers_df["work_id"] == focal_id]
        corpus_df = pd.concat([focal_rows, corpus_df], ignore_index=True)
        corpus_ids.add(focal_id)

    meta_cit = papers_df.set_index("work_id")["citations"].to_dict()

    def _top_neighbours(work_id, direction, n):
        if direction == "in":
            ids = cit_edges[cit_edges["cited_work_id"] == work_id]["citing_work_id"].tolist()
        else:
            ids = cit_edges[cit_edges["citing_work_id"] == work_id]["cited_work_id"].tolist()
        ids.sort(key=lambda w: int(meta_cit.get(w, 0)), reverse=True)
        return set(ids[:n])

    d1_in  = _top_neighbours(focal_id, "in",  max_ego)
    d1_out = _top_neighbours(focal_id, "out", max_ego)
    d1_all = d1_in | d1_out

    d2_all: set = set()
    for nid in sorted(d1_in, key=lambda w: int(meta_cit.get(w, 0)), reverse=True)[:30]:
        d2_all |= _top_neighbours(nid, "in", 5)
    d2_all -= d1_all
    d2_all.discard(focal_id)

    all_ids = corpus_ids | d1_all | d2_all
    all_papers = papers_df[papers_df["work_id"].isin(all_ids)].copy()
    if focal_year is not None:
        yr_num = pd.to_numeric(all_papers["year"], errors="coerce").fillna(focal_year).astype(int)
        all_papers = all_papers[(yr_num >= focal_year) | (all_papers["work_id"] == focal_id)]
        all_ids = set(all_papers["work_id"])

    mask = (cit_edges["citing_work_id"].isin(all_ids) &
            cit_edges["cited_work_id"].isin(all_ids))
    edge_df = cit_edges[mask].drop_duplicates()

    ego_ids = {focal_id} | d1_all | d2_all
    if ego_adjacent_only:
        ego_adjacent = set()
        for r in edge_df.itertuples(index=False):
            src, tgt = r.citing_work_id, r.cited_work_id
            if src in ego_ids and tgt not in ego_ids:
                ego_adjacent.add(tgt)
            elif tgt in ego_ids and src not in ego_ids:
                ego_adjacent.add(src)
        all_papers = all_papers[
            all_papers["work_id"].isin(ego_ids) |
            all_papers["work_id"].isin(ego_adjacent)
        ]
    else:
        connected_ids = set(edge_df["citing_work_id"]) | set(edge_df["cited_work_id"])
        all_papers = all_papers[
            all_papers["work_id"].isin(ego_ids) |
            all_papers["work_id"].isin(connected_ids)
        ]
    all_ids = set(all_papers["work_id"])

    ROLE_COLOR = {
        "focal":          "#EF553B",
        "cites_focal":    "#AB63FA",
        "cited_by_focal": "#00CC96",
        "d2":             "#FFA15A",
        "corpus":         "#b0bec5",
    }

    def _depth(wid):
        if wid == focal_id: return 0
        if wid in d1_all:   return 1
        if wid in d2_all:   return 2
        return 3

    def _role(wid):
        if wid == focal_id:   return "focal"
        if wid in d1_in:      return "cites_focal"
        if wid in d1_out:     return "cited_by_focal"
        if wid in d2_all:     return "d2"
        return "corpus"

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
        t = str(row.get("title", ""))
        nodes.append({"data": {
            "id":         wid,
            "label":      (t[:30] + "…") if len(t) > 30 else t,
            "title":      t,
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
    return [{"title":    str(r.get("title","")),
             "year":     _yr(r.get("year","")),
             "journal":  str(r.get("journal","")),
             "citations": int(r.get("citations",0)),
             "doi":      str(r.get("doi",""))}
            for _, r in sub.head(n).iterrows()]


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


def _short(s, n=35):
    s = str(s).strip()
    return s[:n] + "…" if len(s) > n else s


def _write_corpus_html(stats, nodes, edges, search_index, citing_papers, output_path,
                       llm_text=None, caveat_html=None):
    title    = stats.get("title", "Unknown Paper")
    year     = stats.get("year", "")
    jour     = stats.get("journal", "")
    doi      = stats.get("doi", "")
    field    = stats.get("field", "")
    cit      = stats.get("citations", "?")
    st       = stats.get("study_type", "")
    paper_id = stats.get("work_id", "")

    doi_href = (f'<a href="{doi}" target="_blank" class="hdr-link">DOI ↗</a>'
                if doi and doi != "nan" else "")
    oa_href  = (f'<a href="{paper_id}" target="_blank" class="hdr-link">OpenAlex ↗</a>'
                if paper_id.startswith("http") else "")

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

    _corpus_years = [n["data"]["year"] for n in nodes
                     if n["data"].get("year", 0) and n["data"].get("depth", 3) == 3]
    _all_years    = [n["data"]["year"] for n in nodes if n["data"].get("year", 0)]
    year_min = min(_corpus_years) if _corpus_years else (min(_all_years) if _all_years else 1950)
    year_max = max(_all_years)    if _all_years    else 2025

    caveat_banner = ""
    if caveat_html:
        close_js = "document.getElementById('caveat-bar').remove()"
        caveat_banner = (
            f'<!-- CAVEAT BANNER -->\n'
            f'<div id="caveat-bar">{caveat_html}\n'
            f'  <button onclick="{close_js}" aria-label="Dismiss">✕</button>\n'
            f'</div>'
        )

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
#app{{display:grid;grid-template-rows:auto 1fr auto;height:100vh;overflow:hidden}}
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
#main{{display:grid;grid-template-columns:270px 1fr;overflow:hidden}}
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
.depth-row{{display:flex;gap:.4rem;margin-top:.2rem}}
.dbtn{{flex:1;padding:.35rem;border:2px solid #ccd;border-radius:5px;
  background:#fff;cursor:pointer;font-size:.8rem;text-align:center;transition:.15s}}
.dbtn:hover{{background:#eef4fb}}
.dbtn.active{{border-color:#2980b9;background:#eaf2fb;color:#2980b9;font-weight:600}}
.tog-row{{display:flex;align-items:center;gap:.5rem;margin-top:.35rem;font-size:.76rem}}
.tog{{position:relative;width:32px;height:16px;flex-shrink:0}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;inset:0;background:#ccc;border-radius:16px;cursor:pointer;transition:.2s}}
.tog-slider::before{{content:'';position:absolute;width:12px;height:12px;left:2px;top:2px;
  background:#fff;border-radius:50%;transition:.2s}}
.tog input:checked+.tog-slider{{background:#2980b9}}
.tog input:checked+.tog-slider::before{{transform:translateX(16px)}}
.leg{{display:flex;align-items:center;gap:.4rem;font-size:.7rem;margin-bottom:.28rem}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.ctrl-row{{display:flex;align-items:center;gap:.4rem;margin-bottom:.35rem}}
.ctrl-row label{{font-size:.73rem;color:#555;flex:1}}
select,button{{font-size:.73rem;border:1px solid #ccd;border-radius:4px;
  padding:.22rem .45rem;background:#fff;cursor:pointer}}
button.primary{{background:#34495e;color:#fff;border-color:#34495e}}
button.primary:hover{{background:#2c3e50}}
#detail{{flex:1;padding:.65rem .85rem;min-height:100px}}
#detail h3{{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
  color:#7f8c8d;margin-bottom:.45rem}}
#detail-body{{font-size:.76rem;color:#444;line-height:1.55}}
#detail-body .dt{{font-weight:600;color:#1a252f;line-height:1.4}}
#detail-body .hint{{color:#aaa;font-style:italic}}
#detail-body a{{color:#2980b9;font-size:.72rem}}
#cy{{width:100%;height:100%;background:#fafbfc}}
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
  background:#2980b9;cursor:pointer;border:none;
  box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.dr-track{{position:absolute;top:14px;left:0;right:0;height:4px;
  background:#e0e6ec;border-radius:2px;pointer-events:none}}
.dr-fill{{position:absolute;height:100%;background:#2980b9;border-radius:2px}}
#caveat-bar{{display:flex;align-items:center;gap:.5rem;padding:.45rem 1rem;
  background:#fff3cd;border-bottom:1px solid #ffc107;font-size:.8rem;color:#856404}}
#caveat-bar button{{margin-left:auto;border:none;background:none;cursor:pointer;
  font-size:1rem;color:#856404;padding:0 .25rem;line-height:1}}
</style>
</head>
<body>
<div id="app">

{caveat_banner}
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

<div id="main">
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

  </div>

  <div id="cy"></div>
</div>

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

</div>
<script>
const ELEMENTS   = {elements_json};
const SEARCH_IDX = {search_json};
const FOCAL_ID   = {focal_json};

const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{ selector: 'node', style: {{
        'background-color':   'data(color)', 'width': 'data(size)', 'height': 'data(size)',
        'label': '', 'font-size': '8px', 'text-valign': 'bottom', 'text-halign': 'center',
        'text-margin-y': '2px', 'text-wrap': 'wrap', 'text-max-width': '90px',
        'color': '#333', 'text-outline-color': '#fff', 'text-outline-width': '1.5px',
        'border-width': 0, 'opacity': 0.9, 'z-index': 1,
    }} }},
    {{ selector: 'node[?focal]', style: {{
        'shape': 'star', 'border-width': 3, 'border-color': '#c0392b',
        'font-size': '9px', 'font-weight': 'bold', 'z-index': 20,
    }} }},
    {{ selector: 'node[depth = 3]', style: {{ 'opacity': 0.55, 'z-index': 0 }} }},
    {{ selector: 'node:selected', style: {{ 'border-width': 3, 'border-color': '#2980b9', 'z-index': 25 }} }},
    {{ selector: 'edge', style: {{
        'width': 0.5, 'line-color': '#cfd8dc', 'target-arrow-color': '#b0bec5',
        'target-arrow-shape': 'triangle', 'arrow-scale': 0.5, 'curve-style': 'bezier', 'opacity': 0.4,
    }} }},
    {{ selector: 'edge.active-edge', style: {{
        'width': 1.5, 'line-color': '#78909c', 'target-arrow-color': '#607d8b', 'opacity': 0.8, 'z-index': 5,
    }} }},
    {{ selector: '.corpus-hidden', style: {{ 'display': 'none' }} }},
    {{ selector: '.year-hidden',   style: {{ 'display': 'none' }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 6000, idealEdgeLength: 70, gravity: 0.3, numIter: 500 }},
  wheelSensitivity: 0.3,
}});

let currentFocal = FOCAL_ID, currentDepth = 1, showCorpus = true, showLabels = false;
const YEAR_MIN = {year_min}, YEAR_MAX = {year_max};

function applyYearFilter(y1, y2) {{
  cy.nodes().forEach(n => {{
    const yr = n.data('year');
    if (yr && (yr < y1 || yr > y2)) n.addClass('year-hidden');
    else n.removeClass('year-hidden');
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
    else {{ y2 = y1; document.getElementById('yr-to').value = y2; }}
  }}
  document.getElementById('yr-from-num').value = y1;
  document.getElementById('yr-to-num').value   = y2;
  _updateFill(y1, y2);
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
  document.getElementById('yr-from').value = y1;
  document.getElementById('yr-to').value   = y2;
  _updateFill(y1, y2);
  applyYearFilter(y1, y2);
}}
document.getElementById('yr-from').addEventListener('input', syncYearSlider);
document.getElementById('yr-to').addEventListener('input', syncYearSlider);
document.getElementById('yr-from-num').addEventListener('change', syncYearNums);
document.getElementById('yr-to-num').addEventListener('change', syncYearNums);
['yr-from-num','yr-to-num'].forEach(id =>
  document.getElementById(id).addEventListener('keydown', e => {{ if (e.key==='Enter') syncYearNums(); }}));

function getEgoNodes(focalId, depth) {{
  const focal = cy.getElementById(focalId);
  if (!focal.length) return {{ focal, d1: cy.collection(), d2: cy.collection() }};
  const d1 = focal.neighborhood('node');
  const d2 = depth >= 2 ? d1.neighborhood('node').difference(d1).filter(n => n.id() !== focalId)
                         : cy.collection();
  return {{ focal, d1, d2 }};
}}
function applyDepth(focalId, depth) {{
  currentFocal = focalId; currentDepth = depth;
  cy.edges().removeClass('active-edge');
  const {{ focal, d1, d2 }} = getEgoNodes(focalId, depth);
  cy.nodes().forEach(n => {{
    const id = n.id();
    if (id === focalId) {{ n.data('color', '#EF553B'); return; }}
    const edgesIn  = cy.edges(`[source = "${{id}}"][target = "${{focalId}}"]`);
    const edgesOut = cy.edges(`[source = "${{focalId}}"][target = "${{id}}"]`);
    if (edgesIn.length)  {{ n.data('color','#AB63FA'); return; }}
    if (edgesOut.length) {{ n.data('color','#00CC96'); return; }}
    if (d2.has(n))       {{ n.data('color','#FFA15A'); return; }}
    n.data('color','#b0bec5');
  }});
  if (depth >= 1) {{
    focal.connectedEdges().addClass('active-edge');
    if (depth >= 2) d1.connectedEdges().addClass('active-edge');
  }}
  cy.nodes().forEach(n => n.style('label', showLabels ? (n.data('label') || '') : ''));
  updateDetailPanel(cy.getElementById(focalId).data());
}}
function applyCorpusVisibility(visible) {{
  showCorpus = visible;
  if (visible) cy.nodes('[depth = 3]').removeClass('corpus-hidden');
  else         cy.nodes('[depth = 3]').addClass('corpus-hidden');
}}
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
cy.on('tap', 'node', e => updateDetailPanel(e.target.data()));
cy.on('dblclick', 'node', e => applyDepth(e.target.id(), currentDepth));
cy.on('tap', e => {{
  if (e.target === cy)
    document.getElementById('detail-body').innerHTML =
      '<span class="hint">Click a node for details · Double-click to set as focal</span>';
}});
document.querySelectorAll('.dbtn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.dbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyDepth(currentFocal, parseInt(btn.dataset.d));
  }});
}});
document.getElementById('show-corpus').addEventListener('change',
  e => applyCorpusVisibility(e.target.checked));
document.getElementById('show-labels').addEventListener('change', e => {{
  showLabels = e.target.checked;
  applyDepth(currentFocal, currentDepth);
}});
function runLayout(name) {{
  cy.layout({{
    name, animate: false, fit: true, padding: 30,
    ...(name === 'cose' ? {{nodeRepulsion:6000,idealEdgeLength:70,gravity:0.3,numIter:500}} : {{}}),
    ...(name === 'concentric' ? {{
      concentric: n => n.data('depth')===0 ? 4 : n.data('depth')===1 ? 3 : n.data('depth')===2 ? 2 : 1,
      levelWidth: () => 1,
    }} : {{}}),
  }}).run();
}}
document.getElementById('layout-sel').addEventListener('change', e => runLayout(e.target.value));
document.getElementById('relayout-btn').addEventListener('click',
  () => runLayout(document.getElementById('layout-sel').value));
document.getElementById('fit-btn').addEventListener('click', () => cy.fit(30));
document.getElementById('center-btn').addEventListener('click', () => {{
  const n = cy.getElementById(currentFocal);
  if (n.length) cy.animate({{center:{{eles:n}},zoom:1.2}}, {{duration:400}});
}});
document.getElementById('tabs').addEventListener('click', e => {{
  const tab = e.target.closest('.tab'); if (!tab) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  tab.classList.add('active');
  const pane = document.getElementById('tab-' + tab.dataset.tab);
  if (pane) pane.classList.add('active');
}});
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
  const item = e.target.closest('.search-item'); if (!item) return;
  const node = cy.getElementById(item.dataset.id);
  searchInput.value = ''; searchDrop.style.display = 'none';
  if (node.length) {{
    cy.animate({{center:{{eles:node}},zoom:2}}, {{duration:500}});
    node.select(); updateDetailPanel(node.data());
  }}
}});
document.addEventListener('click', e => {{
  if (!document.getElementById('search-wrap').contains(e.target))
    searchDrop.style.display = 'none';
}});
syncYearSlider();
applyDepth(FOCAL_ID, 1);
cy.fit(30);
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Written: {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHOR CORPUS SECTION
# Corpus-based influence map centred on an author's body of work.
# ═══════════════════════════════════════════════════════════════════════════════

def _load_author_corpus_data(data_dir):
    """Load the CSVs needed for an author corpus influence map."""
    def _csv(name, id_cols=None):
        p = os.path.join(data_dir, name)
        if not os.path.exists(p):
            return None
        kw = {"dtype": {c: str for c in id_cols}} if id_cols else {}
        df = pd.read_csv(p, low_memory=False, **kw)
        for c in df.columns:
            if "id" in c.lower():
                df[c] = df[c].astype(str)
        return df

    cit_path = os.path.join(data_dir, "citation_edges_work.csv")
    cit = (pd.read_csv(cit_path, low_memory=False,
                       dtype={"citing_work_id": str, "cited_work_id": str})
           if os.path.exists(cit_path) else None)
    return {
        "papers":      _csv("papers_detail.csv"),
        "centrality":  _csv("author_centrality.csv"),
        "authors":     _csv("papers_by_author.csv"),
        "cit_papers":  _csv("citation_edges_author_papers.csv",
                            id_cols=["citing_author_id"]),
        "cit_edges":   cit,
    }


def find_author_in_corpus(query, data):
    """Return (author_id, author_name, row) from centrality or authors CSV."""
    base = data.get("centrality")
    if base is None or (isinstance(base, pd.DataFrame) and base.empty):
        base = data.get("authors")
    if base is None or (isinstance(base, pd.DataFrame) and base.empty):
        return None, None, None
    if query.startswith("http"):
        matches = base[base["author_id"] == query]
    else:
        name_col = next((c for c in base.columns if "name" in c.lower()), None)
        if name_col is None:
            return None, None, None
        matches = base[base[name_col].str.contains(query, case=False, na=False)]
    if len(matches) == 0:
        return None, None, None
    # Prefer highest PageRank / citations
    sort_col = next((c for c in ("pagerank", "citations", "papers") if c in matches.columns), None)
    if sort_col:
        matches = matches.sort_values(sort_col, ascending=False)
    if len(matches) > 1:
        name_col = next((c for c in matches.columns if "name" in c.lower()), "author_id")
        print(f"  Found {len(matches)} authors matching '{query}' — using top result:")
        for _, r in matches.head(4).iloc[1:].iterrows():
            print(f"    {r.get(name_col, r['author_id'])}")
        print("  Use --author-id to specify exactly.\n")
    row = matches.iloc[0]
    name_col = next((c for c in row.index if "name" in c.lower()), None)
    author_name = str(row[name_col]) if name_col else str(row["author_id"])
    return str(row["author_id"]), author_name, row


def find_project_work_ids(focal_works: list, papers_df) -> list:
    """Match OpenAlex focal works against corpus papers_df by DOI then title."""
    if papers_df is None or papers_df.empty:
        return []
    doi_col   = next((c for c in papers_df.columns if c.lower() == "doi"), None)
    title_col = next((c for c in papers_df.columns if c.lower() == "title"), None)
    wid_col   = next((c for c in papers_df.columns if c.lower() == "work_id"), None)
    if wid_col is None:
        return []
    matched = []
    if doi_col:
        doi_map = {str(d).lower().strip(): wid
                   for d, wid in zip(papers_df[doi_col], papers_df[wid_col])
                   if str(d) not in ("nan", "", "None")}
        for w in focal_works:
            d = (w.get("doi") or "").lower().strip()
            if d and d in doi_map:
                matched.append(doi_map[d])
    already = set(matched)
    if title_col:
        title_map = {str(t)[:70].lower().strip(): wid
                     for t, wid in zip(papers_df[title_col], papers_df[wid_col])}
        for w in focal_works:
            t = (w.get("title") or "")[:70].lower().strip()
            wid = title_map.get(t)
            if wid and wid not in already:
                matched.append(wid)
                already.add(wid)
    return matched


def find_author_work_ids(author_id, data):
    """Return list of work_ids in corpus for papers by this author."""
    cp = data.get("cit_papers")
    papers_df = data.get("papers")
    if cp is None or papers_df is None:
        return []
    raw_titles: set = set()
    for ts in cp[cp["citing_author_id"] == author_id]["citing_titles"].dropna():
        for t in str(ts).split(";"):
            t = t.strip()
            if t and t != "nan":
                raw_titles.add(t[:120])
    if not raw_titles:
        return []
    key_to_wid = (papers_df.sort_values("citations", ascending=False)
                            .drop_duplicates(subset=papers_df["title"].str[:70].str.lower().str.strip().rename("_k"))
                            .assign(_k=papers_df["title"].str[:70].str.lower().str.strip())
                            .set_index("_k")["work_id"]
                            .to_dict())
    return [wid for rt in raw_titles
            if (wid := key_to_wid.get(rt[:70].lower().strip()))]


def prepare_author_corpus_graph(papers_df, cit_edges, focal_ids,
                                 corpus_size=DEFAULT_CORPUS_SIZE, max_ego=80):
    """Build corpus graph centred on a set of focal papers (author's works)."""
    if papers_df is None or cit_edges is None or not focal_ids:
        return [], [], []

    focal_ids = set(focal_ids)
    sorted_papers = papers_df.sort_values("citations", ascending=False)

    # Corpus: top-N papers, always include all focal papers
    corpus_df = sorted_papers.head(corpus_size).copy() if corpus_size > 0 else sorted_papers.copy()
    corpus_ids = set(corpus_df["work_id"])
    missing = focal_ids - corpus_ids
    if missing:
        extra = papers_df[papers_df["work_id"].isin(missing)]
        corpus_df = pd.concat([extra, corpus_df], ignore_index=True)
        corpus_ids |= missing

    meta_cit = papers_df.set_index("work_id")["citations"].to_dict()

    def _top_neighbours(work_ids, direction, n_each):
        ids: set = set()
        for wid in work_ids:
            if direction == "in":
                nb = cit_edges[cit_edges["cited_work_id"] == wid]["citing_work_id"].tolist()
            else:
                nb = cit_edges[cit_edges["citing_work_id"] == wid]["cited_work_id"].tolist()
            nb.sort(key=lambda w: int(meta_cit.get(w, 0)), reverse=True)
            ids |= set(nb[:n_each])
        return ids - focal_ids

    d1_in  = _top_neighbours(focal_ids, "in",  max_ego)
    d1_out = _top_neighbours(focal_ids, "out", max_ego)
    d1_all = d1_in | d1_out

    d2_all: set = set()
    for nid in sorted(d1_in, key=lambda w: int(meta_cit.get(w, 0)), reverse=True)[:30]:
        d2_all |= _top_neighbours({nid}, "in", 5)
    d2_all -= d1_all | focal_ids

    all_ids = corpus_ids | d1_all | d2_all
    all_papers = papers_df[papers_df["work_id"].isin(all_ids)].copy()

    mask = (cit_edges["citing_work_id"].isin(all_ids) &
            cit_edges["cited_work_id"].isin(all_ids))
    edge_df = cit_edges[mask].drop_duplicates()

    ego_ids = focal_ids | d1_all | d2_all
    connected_ids = set(edge_df["citing_work_id"]) | set(edge_df["cited_work_id"])
    all_papers = all_papers[
        all_papers["work_id"].isin(ego_ids) |
        all_papers["work_id"].isin(connected_ids)
    ]

    ROLE_COLOR = {
        "focal":          "#EF553B",
        "cites_focal":    "#AB63FA",
        "cited_by_focal": "#00CC96",
        "d2":             "#FFA15A",
        "corpus":         "#b0bec5",
    }

    def _role(wid):
        if wid in focal_ids: return "focal"
        if wid in d1_in:     return "cites_focal"
        if wid in d1_out:    return "cited_by_focal"
        if wid in d2_all:    return "d2"
        return "corpus"

    def _depth(wid):
        if wid in focal_ids: return 0
        if wid in d1_all:    return 1
        if wid in d2_all:    return 2
        return 3

    nodes = []
    for _, row in all_papers.drop_duplicates("work_id").iterrows():
        wid  = str(row["work_id"])
        cit  = int(row.get("citations", 0) or 0)
        yr   = row.get("year", "")
        yr_i = int(float(yr)) if yr and str(yr) not in ("", "nan") else 0
        dep  = _depth(wid)
        role = _role(wid)
        size = (28 if dep == 0 else
                max(8, min(50, 8 + 12 * math.log1p(cit))) if dep <= 2 else
                max(6, min(28, 6 + 8 * math.log1p(cit))))
        t = str(row.get("title", ""))
        nodes.append({"data": {
            "id":         wid,
            "label":      (t[:30] + "…") if len(t) > 30 else t,
            "title":      t,
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

    search_index = []
    for _, row in corpus_df.drop_duplicates("work_id").iterrows():
        search_index.append({
            "id":    str(row["work_id"]),
            "title": str(row.get("title", "")),
            "year":  int(float(row["year"])) if str(row.get("year","")) not in ("","nan") else 0,
            "cit":   int(row.get("citations", 0) or 0),
        })
    search_index.sort(key=lambda x: -x["cit"])

    print(f"  focal={len(focal_ids)}  d1={len(d1_all)}  d2={len(d2_all)}  "
          f"nodes={len(nodes)}  edges={len(edges)}")
    return nodes, edges, search_index


def _write_author_corpus_html(author_name, author_row, nodes, edges,
                               search_index, output_path, subject_type="author"):
    """Corpus influence map HTML for an author's or project's body of work."""
    subject_label = "Author" if subject_type == "author" else "Project"
    n_papers  = int(author_row.get("papers", 0) or 0)
    total_cit = int(author_row.get("citations", 0) or 0)
    pagerank  = author_row.get("pagerank", "")
    pr_str    = f"{float(pagerank):.4f}" if pagerank and str(pagerank) != "nan" else "—"

    elements_json = json.dumps(nodes + edges, separators=(",", ":"))
    search_json   = json.dumps(search_index,  separators=(",", ":"))

    _all_years    = [n["data"]["year"] for n in nodes if n["data"].get("year", 0)]
    year_min = min(_all_years) if _all_years else 1950
    year_max = max(_all_years) if _all_years else 2025

    corpus_size_note = f"{len(search_index):,} papers in corpus view"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{author_name} — Corpus Influence</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  font-size:13px;background:#f0f2f5;color:#222}}
#app{{display:grid;grid-template-rows:auto 1fr;height:100vh;overflow:hidden}}
#topbar{{background:#1a1a2e;color:#fff;padding:.45rem 1rem;
  display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;flex-shrink:0}}
#topbar h1{{font-size:.95rem;font-weight:600}}
#topbar .meta{{font-size:.72rem;color:#95a5a6;white-space:nowrap}}
#search-wrap{{position:relative;margin-left:auto;flex-shrink:0}}
#search-input{{width:220px;padding:.3rem .6rem;border:1px solid #4a6278;border-radius:4px;
  background:#243342;color:#ecf0f1;font-size:.8rem;outline:none}}
#search-input:focus{{border-color:#5dade2}}
#search-drop{{position:absolute;top:100%;right:0;width:380px;background:#fff;
  border:1px solid #ccd;border-radius:0 0 6px 6px;box-shadow:0 4px 12px rgba(0,0,0,.15);
  max-height:280px;overflow-y:auto;z-index:999;display:none}}
.search-item{{padding:.4rem .7rem;cursor:pointer;border-bottom:1px solid #f0f0f0;font-size:.78rem}}
.search-item:hover{{background:#eef4fb}}
.search-item .s-title{{color:#222;font-weight:500}}
.search-item .s-meta{{color:#888;font-size:.7rem}}
#main{{display:grid;grid-template-columns:260px 1fr;overflow:hidden}}
#sidebar{{background:#fff;border-right:1px solid #dde1e7;overflow-y:auto;display:flex;flex-direction:column}}
.sec{{padding:.65rem .85rem;border-bottom:1px solid #eef0f3}}
.sec h3{{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:#7f8c8d;margin-bottom:.45rem}}
.chips{{display:grid;grid-template-columns:1fr 1fr;gap:.35rem}}
.chip{{background:#f7f9fc;border:1px solid #e3e8ef;border-radius:6px;padding:.3rem .5rem;text-align:center}}
.chip .cv{{font-size:.95rem;font-weight:700;color:#2c3e50}}
.chip .cl{{font-size:.62rem;color:#888;margin-top:1px}}
.depth-row{{display:flex;gap:.4rem;margin-top:.2rem}}
.dbtn{{flex:1;padding:.35rem;border:2px solid #ccd;border-radius:5px;background:#fff;
  cursor:pointer;font-size:.8rem;text-align:center;transition:.15s}}
.dbtn:hover{{background:#eef4fb}}
.dbtn.active{{border-color:#2980b9;background:#eaf2fb;color:#2980b9;font-weight:600}}
.tog-row{{display:flex;align-items:center;gap:.5rem;margin-top:.35rem;font-size:.76rem}}
.tog{{position:relative;width:32px;height:16px;flex-shrink:0}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;inset:0;background:#ccc;border-radius:16px;cursor:pointer;transition:.2s}}
.tog-slider::before{{content:'';position:absolute;width:12px;height:12px;left:2px;top:2px;
  background:#fff;border-radius:50%;transition:.2s}}
.tog input:checked+.tog-slider{{background:#2980b9}}
.tog input:checked+.tog-slider::before{{transform:translateX(16px)}}
.leg{{display:flex;align-items:center;gap:.4rem;font-size:.7rem;margin-bottom:.28rem}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.ctrl-row{{display:flex;align-items:center;gap:.4rem;margin-bottom:.35rem}}
.ctrl-row label{{font-size:.73rem;color:#555;flex:1}}
select,button{{font-size:.73rem;border:1px solid #ccd;border-radius:4px;padding:.22rem .45rem;
  background:#fff;cursor:pointer}}
button.primary{{background:#34495e;color:#fff;border-color:#34495e}}
button.primary:hover{{background:#2c3e50}}
#detail{{flex:1;padding:.65rem .85rem;min-height:100px}}
#detail h3{{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:#7f8c8d;margin-bottom:.45rem}}
#detail-body{{font-size:.76rem;color:#444;line-height:1.55}}
#detail-body .dt{{font-weight:600;color:#1a252f;line-height:1.4}}
#detail-body .hint{{color:#aaa;font-style:italic}}
#detail-body a{{color:#2980b9;font-size:.72rem}}
#cy{{width:100%;height:100%;background:#fafbfc}}
.yr-inputs{{display:flex;align-items:center;gap:.35rem;margin-bottom:.3rem}}
.yr-inputs input[type=number]{{width:60px;padding:.2rem .35rem;font-size:.8rem;
  border:1px solid #ccd;border-radius:4px;text-align:center;background:#fff;color:#2980b9;font-weight:600}}
.yr-inputs span{{color:#888;font-size:.75rem}}
.dual-range{{position:relative;height:28px;margin:.2rem 0 .1rem}}
.dual-range input[type=range]{{position:absolute;width:100%;height:4px;top:12px;
  pointer-events:none;-webkit-appearance:none;appearance:none;background:transparent;outline:none;margin:0}}
.dual-range input[type=range]::-webkit-slider-thumb{{pointer-events:all;-webkit-appearance:none;
  width:14px;height:14px;border-radius:50%;background:#2980b9;cursor:pointer;
  border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.dual-range input[type=range]::-moz-range-thumb{{pointer-events:all;width:14px;height:14px;
  border-radius:50%;background:#2980b9;cursor:pointer;border:none;box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.dr-track{{position:absolute;top:14px;left:0;right:0;height:4px;background:#e0e6ec;
  border-radius:2px;pointer-events:none}}
.dr-fill{{position:absolute;height:100%;background:#2980b9;border-radius:2px}}
</style>
</head>
<body>
<div id="app">
<div id="topbar">
  <h1>◈ {author_name}</h1>
  <span class="meta">{n_papers} papers in corpus</span>
  <span class="meta">{total_cit:,} total citations</span>
  <span class="meta">PageRank {pr_str}</span>
  <div id="search-wrap">
    <input id="search-input" type="text" placeholder="Search corpus…" autocomplete="off">
    <div id="search-drop"></div>
  </div>
</div>
<div id="main">
  <div id="sidebar">

    <div class="sec">
      <h3>{subject_label} metrics</h3>
      <div class="chips">
        <div class="chip"><div class="cv">{n_papers}</div><div class="cl">Papers in corpus</div></div>
        <div class="chip"><div class="cv">{total_cit:,}</div><div class="cl">Total citations</div></div>
        <div class="chip"><div class="cv">{pr_str}</div><div class="cl">PageRank</div></div>
        <div class="chip"><div class="cv">{len([n for n in nodes if n["data"]["depth"]==1])}</div><div class="cl">Direct neighbours</div></div>
      </div>
    </div>

    <div class="sec">
      <h3>Ego depth</h3>
      <div class="depth-row">
        <div class="dbtn" data-d="0">n=0<br><span style="font-size:.65rem;font-weight:400">{subject_label.lower()} only</span></div>
        <div class="dbtn active" data-d="1">n=1<br><span style="font-size:.65rem;font-weight:400">direct</span></div>
        <div class="dbtn" data-d="2">n=2<br><span style="font-size:.65rem;font-weight:400">2-hop</span></div>
      </div>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-corpus">
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
      <div class="leg"><span class="leg-dot" style="background:#EF553B"></span>{subject_label}'s papers</div>
      <div class="leg"><span class="leg-dot" style="background:#AB63FA"></span>Papers citing this work</div>
      <div class="leg"><span class="leg-dot" style="background:#00CC96"></span>Papers cited by this work</div>
      <div class="leg"><span class="leg-dot" style="background:#FFA15A"></span>Depth-2 neighbours</div>
      <div class="leg"><span class="leg-dot" style="background:#b0bec5"></span>Corpus background</div>
      <div style="font-size:.67rem;color:#999;margin-top:.3rem">{corpus_size_note}</div>
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
      </div>
    </div>

    <div id="detail">
      <h3>Selected paper</h3>
      <div id="detail-body"><span class="hint">Click a node to see details</span></div>
    </div>
  </div>

  <div id="cy"></div>
</div>
</div>

<script>
const ELEMENTS   = {elements_json};
const SEARCH_IDX = {search_json};
const YEAR_MIN   = {year_min};
const YEAR_MAX   = {year_max};

const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{ selector: 'node', style: {{
        'background-color': 'data(color)', 'width': 'data(size)', 'height': 'data(size)',
        'label': '', 'font-size': '8px', 'text-valign': 'bottom', 'text-halign': 'center',
        'text-margin-y': '2px', 'text-wrap': 'wrap', 'text-max-width': '90px',
        'color': '#333', 'text-outline-color': '#fff', 'text-outline-width': '1.5px',
        'border-width': 0, 'opacity': 0.9, 'z-index': 1,
    }} }},
    {{ selector: 'node[?focal]', style: {{
        'shape': 'pentagon', 'border-width': 3, 'border-color': '#c0392b', 'z-index': 20,
    }} }},
    {{ selector: 'node[depth = 3]', style: {{ 'opacity': 0.5, 'z-index': 0 }} }},
    {{ selector: 'node:selected', style: {{ 'border-width': 3, 'border-color': '#2980b9', 'z-index': 25 }} }},
    {{ selector: 'edge', style: {{
        'width': 0.5, 'line-color': '#cfd8dc', 'target-arrow-color': '#b0bec5',
        'target-arrow-shape': 'triangle', 'arrow-scale': 0.5,
        'curve-style': 'bezier', 'opacity': 0.4,
    }} }},
    {{ selector: 'edge.active-edge', style: {{
        'width': 1.5, 'line-color': '#78909c', 'target-arrow-color': '#607d8b',
        'opacity': 0.8, 'z-index': 5,
    }} }},
    {{ selector: '.corpus-hidden', style: {{ 'display': 'none' }} }},
    {{ selector: '.year-hidden',   style: {{ 'display': 'none' }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 6000,
             idealEdgeLength: 70, gravity: 0.3, numIter: 500 }},
  wheelSensitivity: 0.3,
}});

let currentDepth = 1, showCorpus = false, showLabels = false;

function applyYearFilter(y1, y2) {{
  cy.nodes().forEach(n => {{
    const yr = n.data('year');
    if (yr && (yr < y1 || yr > y2)) n.addClass('year-hidden');
    else n.removeClass('year-hidden');
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
  if (y1 > y2) {{ const t = y1; y1 = y2; y2 = t; }}
  document.getElementById('yr-from-num').value = y1;
  document.getElementById('yr-to-num').value   = y2;
  _updateFill(y1, y2);
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
  document.getElementById('yr-from').value = y1;
  document.getElementById('yr-to').value   = y2;
  _updateFill(y1, y2); applyYearFilter(y1, y2);
}}
document.getElementById('yr-from').addEventListener('input', syncYearSlider);
document.getElementById('yr-to').addEventListener('input', syncYearSlider);
['yr-from-num','yr-to-num'].forEach(id => {{
  const el = document.getElementById(id);
  el.addEventListener('change', syncYearNums);
  el.addEventListener('keydown', e => {{ if (e.key==='Enter') syncYearNums(); }});
}});

function applyDepth(depth) {{
  currentDepth = depth;
  cy.edges().removeClass('active-edge');
  const focal = cy.nodes('[?focal]');
  if (depth >= 1) {{
    focal.connectedEdges().addClass('active-edge');
    if (depth >= 2) focal.neighborhood('node').connectedEdges().addClass('active-edge');
  }}
  // Visibility: hide d1/d2 based on depth setting
  cy.nodes().forEach(n => {{
    const d = n.data('depth');
    if (d === 0) return;
    if (d === 1 && depth < 1) {{ n.addClass('corpus-hidden'); return; }}
    if (d === 2 && depth < 2) {{ n.addClass('corpus-hidden'); return; }}
    if (d === 3 && !showCorpus) {{ n.addClass('corpus-hidden'); return; }}
    n.removeClass('corpus-hidden');
  }});
  cy.nodes().forEach(n => n.style('label', showLabels ? (n.data('label') || '') : ''));
}}

document.querySelectorAll('.dbtn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.dbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyDepth(parseInt(btn.dataset.d));
  }});
}});
document.getElementById('show-corpus').addEventListener('change', e => {{
  showCorpus = e.target.checked;
  applyDepth(currentDepth);
}});
document.getElementById('show-labels').addEventListener('change', e => {{
  showLabels = e.target.checked;
  cy.nodes().forEach(n => n.style('label', showLabels ? (n.data('label') || '') : ''));
}});

function updateDetail(d) {{
  if (!d) return;
  const doi = d.doi && d.doi !== 'nan' ? d.doi : '';
  const oa  = d.id  && d.id.startsWith('http') ? d.id : '';
  document.getElementById('detail-body').innerHTML =
    `<div class="dt">${{d.title || d.label}}</div>` +
    `<div style="margin:.3em 0 .1em;color:#555">${{d.year||'—'}} · ${{d.journal||''}}</div>` +
    `<div style="color:#888">${{d.study_type||''}}</div>` +
    `<div style="margin-top:.3em"><b>Citations:</b> ${{d.citations}}</div>` +
    (doi ? `<div style="margin-top:.3em"><a href="${{doi}}" target="_blank">DOI ↗</a>` : '') +
    (oa  ? ` <a href="${{oa}}" target="_blank">OpenAlex ↗</a></div>` : '');
}}
cy.on('tap','node', e => updateDetail(e.target.data()));
cy.on('tap', e => {{
  if (e.target === cy)
    document.getElementById('detail-body').innerHTML =
      '<span class="hint">Click a node to see details</span>';
}});

function runLayout(name) {{
  cy.layout({{ name, animate: false, fit: true, padding: 30,
    ...(name==='cose' ? {{nodeRepulsion:6000,idealEdgeLength:70,gravity:0.3,numIter:500}} : {{}}),
    ...(name==='concentric' ? {{
      concentric: n => n.data('depth')===0?4:n.data('depth')===1?3:n.data('depth')===2?2:1,
      levelWidth: ()=>1,
    }} : {{}}),
  }}).run();
}}
document.getElementById('layout-sel').addEventListener('change', e => runLayout(e.target.value));
document.getElementById('relayout-btn').addEventListener('click',
  () => runLayout(document.getElementById('layout-sel').value));
document.getElementById('fit-btn').addEventListener('click', () => cy.fit(30));

const searchInput = document.getElementById('search-input');
const searchDrop  = document.getElementById('search-drop');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.trim().toLowerCase();
  if (q.length < 2) {{ searchDrop.style.display='none'; return; }}
  const hits = SEARCH_IDX.filter(p => p.title.toLowerCase().includes(q)).slice(0,12);
  if (!hits.length) {{ searchDrop.style.display='none'; return; }}
  searchDrop.innerHTML = hits.map(p =>
    `<div class="search-item" data-id="${{p.id}}">
       <div class="s-title">${{p.title.substring(0,80)}}</div>
       <div class="s-meta">${{p.year}} · ${{p.cit.toLocaleString()}} citations</div>
     </div>`).join('');
  searchDrop.style.display = 'block';
}});
searchDrop.addEventListener('click', e => {{
  const item = e.target.closest('.search-item'); if (!item) return;
  const node = cy.getElementById(item.dataset.id);
  searchInput.value=''; searchDrop.style.display='none';
  if (node.length) {{ cy.animate({{center:{{eles:node}},zoom:2}},{{duration:500}}); node.select(); updateDetail(node.data()); }}
}});
document.addEventListener('click', e => {{
  if (!document.getElementById('search-wrap').contains(e.target)) searchDrop.style.display='none';
}});

syncYearSlider(); applyDepth(1); cy.fit(30);
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Written: {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# INFLUENCE SECTION
# Crawls OpenAlex live; results cached in cache/influence_cache.sqlite.
# ═══════════════════════════════════════════════════════════════════════════════

def _open_cache(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS works (
        work_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, fetched INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS edges (
        citing_id TEXT NOT NULL, cited_id TEXT NOT NULL, PRIMARY KEY (citing_id, cited_id))""")
    con.commit()
    return con


def _cache_get(con, work_id):
    row = con.execute("SELECT data_json FROM works WHERE work_id=?", (work_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _cache_put(con, work_id, data):
    con.execute("INSERT OR REPLACE INTO works (work_id, data_json, fetched) VALUES (?,?,?)",
                (work_id, json.dumps(data, separators=(",", ":")), int(time.time())))
    con.commit()


def _cache_edges_put(con, citing_id, cited_ids):
    con.executemany("INSERT OR IGNORE INTO edges (citing_id, cited_id) VALUES (?,?)",
                    [(citing_id, c) for c in cited_ids])
    con.commit()


def _cache_edges_get(con, work_id, direction):
    if direction == "out":
        rows = con.execute("SELECT cited_id FROM edges WHERE citing_id=?", (work_id,)).fetchall()
    else:
        rows = con.execute("SELECT citing_id FROM edges WHERE cited_id=?", (work_id,)).fetchall()
    return [r[0] for r in rows] if rows else None


def _get(url, params):
    params = {**params, "mailto": MAILTO}
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(REQUEST_DELAY * 2 ** attempt)
    return {}


_OA_SELECT = ("id,doi,title,publication_year,cited_by_count,"
              "primary_location,topics,authorships,referenced_works")
_OA_SELECT_NOGRANTS = _OA_SELECT  # grants not supported in select; kept as alias


def _extract_work(w):
    wid   = w.get("id", "")
    doi   = (w.get("doi") or "").replace("https://doi.org/", "")
    title = (w.get("title") or "").strip()
    year  = w.get("publication_year")
    cit   = w.get("cited_by_count", 0) or 0
    source  = ((w.get("primary_location") or {}).get("source") or {})
    journal = source.get("display_name", "")
    topics = w.get("topics") or []
    field  = (topics[0].get("field") or {}).get("display_name", "") if topics else ""
    authors  = w.get("authorships") or []
    countries = list({inst.get("country_code", "")
                      for a in authors
                      for inst in (a.get("institutions") or [])
                      if inst.get("country_code")})
    refs = [r for r in (w.get("referenced_works") or []) if isinstance(r, str) and r]
    grants  = w.get("grants") or []
    funders = list({g.get("funder_display_name", "")
                    for g in grants if g.get("funder_display_name")})
    return {"id": wid, "doi": doi, "title": title, "year": year, "cit": cit,
            "journal": journal, "field": field, "countries": countries,
            "funders": funders, "ref_ids": refs}


def _fetch_work(work_id, con) -> Optional[dict]:
    cached = _cache_get(con, work_id)
    if cached:
        return cached
    time.sleep(REQUEST_DELAY)
    try:
        data = _get(f"{BASE_URL}/{work_id.split('/')[-1]}", {"select": _OA_SELECT})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    if not data.get("id"):
        return None
    out = _extract_work(data)
    _cache_put(con, work_id, out)
    return out


def _fetch_by_doi(doi, con) -> Optional[dict]:
    doi_clean = doi.strip().lstrip("https://doi.org/")
    row = con.execute("SELECT data_json FROM works WHERE data_json LIKE ?",
                      (f'%"doi":"{doi_clean}"%',)).fetchone()
    if row:
        return json.loads(row[0])
    time.sleep(REQUEST_DELAY)
    data = _get(BASE_URL, {"filter": f"doi:{doi_clean}", "select": _OA_SELECT_NOGRANTS})
    results = data.get("results", [])
    if not results:
        return None
    out = _extract_work(results[0])
    _cache_put(con, out["id"], out)
    return out


def _fetch_references(work, con, limit):
    work_id = work["id"]
    cached  = _cache_edges_get(con, work_id, "out")
    ref_ids = work.get("ref_ids", [])[:limit]
    if cached is not None:
        ref_ids = cached[:limit]
    else:
        _cache_edges_put(con, work_id, ref_ids)
    return [w for rid in ref_ids for w in [_fetch_work(rid, con)] if w]


def _fetch_citers(work_id, con, limit):
    cached = _cache_edges_get(con, work_id, "in")
    if cached is not None:
        return [w for cid in cached[:limit] for w in [_fetch_work(cid, con)] if w]
    citers = []
    cursor = "*"
    while len(citers) < limit:
        page_limit = min(PAGE_SIZE, limit - len(citers))
        data = _get(BASE_URL, {
            "filter":   f"cites:{work_id.split('/')[-1]}",
            "sort":     "cited_by_count:desc",
            "select":   _OA_SELECT_NOGRANTS,
            "per-page": page_limit,
            "cursor":   cursor,
        })
        results = data.get("results", [])
        if not results:
            break
        for w in results:
            meta = _extract_work(w)
            _cache_put(con, meta["id"], meta)
            citers.append(meta)
            time.sleep(REQUEST_DELAY)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    # Store as (citer → focal) so the "in" lookup (cited_id=work_id) finds them
    for c in citers:
        _cache_edges_put(con, c["id"], [work_id])
    return citers


def crawl(focal, depth, max_refs, max_cites, con):
    works     = {focal["id"]: focal}
    edges_set = set()

    print(f"  layer 1 forward — fetching up to {max_cites} citers …")
    l1_fwd = _fetch_citers(focal["id"], con, max_cites)
    for w in l1_fwd:
        works[w["id"]] = w
        edges_set.add((focal["id"], w["id"]))
    print(f"    {len(l1_fwd)} citers")

    print(f"  layer 1 backward — fetching up to {max_refs} references …")
    l1_bwd = _fetch_references(focal, con, max_refs)
    for w in l1_bwd:
        works[w["id"]] = w
        edges_set.add((w["id"], focal["id"]))
    print(f"    {len(l1_bwd)} references")

    if depth >= 2:
        top_fwd_seeds = sorted(l1_fwd, key=lambda w: w.get("cit", 0), reverse=True)[:20]
        print(f"  layer 2 forward — {len(top_fwd_seeds)} seeds × up to 20 citers …")
        for seed in top_fwd_seeds:
            for w in _fetch_citers(seed["id"], con, 20):
                if w["id"] not in works:
                    works[w["id"]] = w
                edges_set.add((seed["id"], w["id"]))

        print(f"  layer 2 backward — scanning refs of {len(l1_bwd)} layer-1B nodes …")
        for seed in l1_bwd:
            for rid in (seed.get("ref_ids") or [])[:max_refs]:
                if rid not in works:
                    w = _fetch_work(rid, con)
                    if w:
                        works[rid] = w
                if rid in works:
                    edges_set.add((rid, seed["id"]))

    all_ids = set(works.keys())
    added = 0
    for wid, w in works.items():
        for rid in (w.get("ref_ids") or []):
            if rid in all_ids and rid != wid and (rid, wid) not in edges_set:
                edges_set.add((rid, wid))
                added += 1
    print(f"  cross-edges from metadata: +{added}")

    roles: dict = {focal["id"]: "focal"}
    for w in l1_fwd: roles.setdefault(w["id"], "cites_focal")
    for w in l1_bwd: roles.setdefault(w["id"], "cited_by_focal")
    for wid in works: roles.setdefault(wid, "d2")
    return works, list(edges_set), roles


def trim_graph(focal_id, works, edges, max_nodes, roles):
    focal_neighbors = set()
    for src, tgt in edges:
        if src == focal_id: focal_neighbors.add(tgt)
        if tgt == focal_id: focal_neighbors.add(src)

    keep = {focal_id}
    l1_sorted = sorted([works[wid] for wid in focal_neighbors if wid in works],
                       key=lambda w: w.get("cit", 0), reverse=True)
    for w in l1_sorted[:max_nodes - 1]:
        keep.add(w["id"])

    if len(keep) < max_nodes:
        adj_to_keep = set()
        for src, tgt in edges:
            if src in keep and tgt not in keep: adj_to_keep.add(tgt)
            elif tgt in keep and src not in keep: adj_to_keep.add(src)
        remaining = max_nodes - len(keep)
        l2_sorted = sorted([works[wid] for wid in adj_to_keep if wid in works],
                           key=lambda w: w.get("cit", 0), reverse=True)
        for w in l2_sorted[:remaining]:
            keep.add(w["id"])

    trimmed_edges = [(s, t) for s, t in edges if s in keep and t in keep]
    connected = {n for e in trimmed_edges for n in e}
    keep = {focal_id} | (keep & connected)
    trimmed_works = {wid: works[wid] for wid in keep if wid in works}
    trimmed_edges = [(s, t) for s, t in trimmed_edges if s in keep and t in keep]
    trimmed_roles = {wid: roles.get(wid, "d2") for wid in keep}
    return trimmed_works, trimmed_edges, trimmed_roles


_FIELD_COLORS = [
    "#EF553B","#636EFA","#00CC96","#AB63FA","#FFA15A",
    "#19D3F3","#FF6692","#B6E880","#FF97FF","#FECB52",
]
INF_ROLE_COLOR = {
    "focal": "#EF553B", "cites_focal": "#AB63FA",
    "cited_by_focal": "#00CC96", "d2": "#b0bec5",
}


def _write_influence_html(focal_id, works, edges, roles, output_path):
    focal = works[focal_id]
    field_map = {}
    for w in works.values():
        f = w.get("field") or "Unknown"
        if f not in field_map:
            field_map[f] = _FIELD_COLORS[len(field_map) % len(_FIELD_COLORS)]

    role_counts = {}
    for r in roles.values():
        role_counts[r] = role_counts.get(r, 0) + 1

    nodes = []
    for wid, w in works.items():
        cit   = w.get("cit", 0) or 0
        role  = roles.get(wid, "d2")
        field = w.get("field") or "Unknown"
        size  = 22 if role == "focal" else max(5, min(16, 5 + 5 * math.log1p(cit)))
        t     = w.get("title") or ""
        label = (t[:28] + "…") if len(t) > 28 else t
        countries = w.get("countries") or []
        country_str = (", ".join(countries[:3]) + ("…" if len(countries) > 3 else "")) if countries else "—"
        funders = w.get("funders") or []
        funder_str = (", ".join(funders[:3]) + ("…" if len(funders) > 3 else "")) if funders else "—"
        nodes.append({"data": {
            "id":          wid,
            "label":       label,
            "title":       t,
            "year":        w.get("year") or 0,
            "cit":         cit,
            "journal":     w.get("journal") or "",
            "field":       field,
            "countries":   country_str,
            "funders":     funder_str,
            "funder_list": funders,
            "doi":         w.get("doi") or "",
            "role":        role,
            "focal":       role == "focal",
            "role_color":  INF_ROLE_COLOR[role],
            "field_color": field_map.get(field, "#b0bec5"),
            "color":       INF_ROLE_COLOR[role],
            "size":        size,
        }})

    cy_edges = [{"data": {"id": f"{s}__{t}", "source": s, "target": t}}
                for s, t in edges]
    elements_json   = json.dumps(nodes + cy_edges, separators=(",", ":"))
    field_colors_json = json.dumps(field_map)

    focal_title = (focal.get("title") or "Unknown")[:80]
    focal_year  = focal.get("year") or ""
    focal_cit   = focal.get("cit") or 0
    focal_doi   = focal.get("doi") or ""
    doi_href = (f'<a href="https://doi.org/{focal_doi}" target="_blank" class="hdr-link">DOI ↗</a>'
                if focal_doi else "")

    field_legend = "".join(
        f'<div class="leg"><span class="leg-dot" style="background:{col}"></span>{fld}</div>'
        for fld, col in sorted(field_map.items()))

    n_cites = role_counts.get("cites_focal", 0)
    n_cited = role_counts.get("cited_by_focal", 0)
    n_d2    = role_counts.get("d2", 0)

    all_years = sorted({w.get("year") or 0 for w in works.values() if w.get("year")})
    year_min  = all_years[0]  if all_years else 1950
    year_max  = all_years[-1] if all_years else 2025

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Influence map — {focal_title[:60]}</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  font-size:13px;background:#f0f2f5;color:#222}}
#app{{display:grid;grid-template-rows:auto 1fr auto;height:100vh;overflow:hidden}}
#topbar{{display:flex;align-items:center;gap:.6rem;padding:.5rem 1rem;
  background:#1a1a2e;color:#eee;flex-wrap:wrap;z-index:10}}
#topbar h1{{font-size:.95rem;font-weight:600;color:#fff;margin-right:.4rem}}
.meta{{font-size:.75rem;color:#aaa;white-space:nowrap}}
.hdr-link{{font-size:.75rem;color:#7ec8e3;text-decoration:none}}
.hdr-link:hover{{text-decoration:underline}}
#main{{display:grid;grid-template-columns:230px 1fr;overflow:hidden}}
#sidebar{{background:#fff;border-right:1px solid #dde;overflow-y:auto;padding:.6rem;
  display:flex;flex-direction:column;gap:.5rem}}
#sidebar h3{{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin-bottom:.2rem}}
.sec{{border-bottom:1px solid #eee;padding-bottom:.5rem}}
.chip{{background:#f4f6fa;border-radius:6px;padding:.25rem .5rem;margin-bottom:.25rem;
  display:flex;justify-content:space-between}}
.cv{{font-weight:700;font-size:.9rem}}
.cl{{font-size:.67rem;color:#777;margin-top:.05rem}}
.tog-row{{display:flex;align-items:center;gap:.5rem;font-size:.78rem;margin-bottom:.2rem}}
.tog{{position:relative;display:inline-block;width:32px;height:17px}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;cursor:pointer;inset:0;background:#ccc;border-radius:17px;transition:.25s}}
.tog-slider::before{{content:"";position:absolute;height:13px;width:13px;left:2px;bottom:2px;
  background:#fff;border-radius:50%;transition:.25s}}
.tog input:checked+.tog-slider{{background:#2980b9}}
.tog input:checked+.tog-slider::before{{transform:translateX(15px)}}
.badge{{font-size:.67rem;background:#e8ecf0;border-radius:8px;padding:.05rem .35rem;color:#555}}
.primary{{background:#2980b9;color:#fff;border:none;border-radius:4px;padding:.3rem .6rem;
  cursor:pointer;font-size:.78rem}}
.primary:hover{{background:#1f6391}}
.ctrl-row{{display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem;font-size:.78rem}}
.ctrl-row label{{color:#555;white-space:nowrap}}
.ctrl-row select{{flex:1;font-size:.78rem;padding:.15rem .3rem;border:1px solid #ccc;border-radius:3px}}
#detail{{flex:1;padding:.4rem;overflow-y:auto}}
#detail h3{{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin-bottom:.3rem}}
.hint{{color:#aaa;font-size:.78rem}}
.d-row{{display:flex;justify-content:space-between;font-size:.78rem;
  border-bottom:1px solid #f0f0f0;padding:.15rem 0}}
.d-lbl{{color:#777}}
.d-val{{font-weight:500;max-width:130px;text-align:right;word-break:break-word}}
.d-doi{{color:#2980b9;text-decoration:none;font-size:.7rem}}
.leg{{display:flex;align-items:center;gap:.35rem;font-size:.73rem;margin-bottom:.2rem}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.yr-inputs{{display:flex;align-items:center;gap:.3rem;margin-bottom:.4rem}}
.yr-inputs input[type=number]{{width:58px;padding:.15rem .3rem;border:1px solid #ccc;
  border-radius:3px;font-size:.78rem;text-align:center}}
.yr-inputs span{{color:#aaa;font-size:.85rem}}
.dual-range{{position:relative;height:30px;margin:0 4px}}
.dual-range input[type=range]{{
  position:absolute;width:100%;height:4px;top:12px;
  pointer-events:none;-webkit-appearance:none;appearance:none;background:transparent;outline:none;margin:0}}
.dual-range input[type=range]::-webkit-slider-thumb{{
  pointer-events:all;-webkit-appearance:none;width:14px;height:14px;border-radius:50%;
  background:#2980b9;cursor:pointer;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.dual-range input[type=range]::-moz-range-thumb{{
  pointer-events:all;width:14px;height:14px;border-radius:50%;
  background:#2980b9;cursor:pointer;border:none;box-shadow:0 1px 3px rgba(0,0,0,.3)}}
.dr-track{{position:absolute;top:14px;left:0;right:0;height:4px;
  background:#e0e6ec;border-radius:2px;pointer-events:none}}
.dr-fill{{position:absolute;height:100%;background:#2980b9;border-radius:2px}}
#cy{{background:#fafbfc}}
#bottom{{height:160px;border-top:2px solid #dde;background:#fff;
  display:flex;flex-direction:column;overflow:hidden}}
#tabs{{display:flex;gap:0;border-bottom:1px solid #dde;flex-shrink:0}}
.tab{{padding:.35rem .8rem;font-size:.78rem;cursor:pointer;color:#666;
  border-bottom:2px solid transparent;user-select:none}}
.tab.active{{color:#2980b9;border-bottom-color:#2980b9;font-weight:600}}
.tab-pane{{display:none;overflow:auto;flex:1;padding:.4rem .6rem}}
.tab-pane.active{{display:block}}
table{{border-collapse:collapse;width:100%;font-size:.73rem}}
th{{background:#f4f6fa;position:sticky;top:0;padding:.25rem .4rem;
  text-align:left;font-weight:600;color:#555}}
td{{padding:.18rem .4rem;border-bottom:1px solid #f0f2f5;vertical-align:top}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
/* help modal */
#help-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;
  justify-content:center;align-items:center}}
#help-overlay.open{{display:flex}}
#help-box{{background:#fff;border-radius:8px;max-width:560px;width:94%;max-height:88vh;
  overflow-y:auto;padding:1.2rem 1.4rem;font-size:.82rem;line-height:1.55}}
#help-box h2{{font-size:1rem;margin-bottom:.7rem;color:#1a1a2e}}
#help-box h3{{font-size:.8rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin:.9rem 0 .3rem}}
#help-box p{{margin-bottom:.5rem;color:#333}}
#help-box table{{font-size:.78rem;margin-bottom:.5rem}}
#help-box td{{padding:.12rem .4rem;vertical-align:top;border-bottom:1px solid #f0f0f0}}
#help-box td:first-child{{font-weight:600;white-space:nowrap;color:#2980b9;padding-right:.7rem}}
.help-btn{{background:transparent;border:1px solid #666;border-radius:50%;
  width:22px;height:22px;color:#ccc;font-size:.82rem;cursor:pointer;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.help-btn:hover{{background:#333;color:#fff}}
.close-btn{{float:right;background:transparent;border:none;font-size:1.1rem;
  cursor:pointer;color:#888;line-height:1}}
.close-btn:hover{{color:#222}}
</style>
</head>
<body>
<div id="app">

<!-- ── Help modal ─────────────────────────────────────────────────────────── -->
<div id="help-overlay">
  <div id="help-box">
    <button class="close-btn" id="help-close">✕</button>
    <h2>Influence map — how to use</h2>

    <h3>What this graph shows</h3>
    <p>Each node is a published work; arrows point from cited work → citing work (influence direction).
    Node size reflects global citation count. Colour codes the node's role:</p>
    <table>
      <tr><td style="color:#EF553B">● Focal (red)</td><td>The paper you are exploring</td></tr>
      <tr><td style="color:#AB63FA">● Purple</td><td>Papers that <em>cite</em> the focal work</td></tr>
      <tr><td style="color:#00CC96">● Teal</td><td>Papers <em>cited by</em> the focal work (references)</td></tr>
      <tr><td style="color:#b0bec5">● Grey</td><td>Second-hop neighbours</td></tr>
    </table>
    <p>When <strong>Colour by field</strong> is on, each colour represents a research field instead.</p>

    <h3>Navigation</h3>
    <table>
      <tr><td>Scroll / pinch</td><td>Zoom in and out</td></tr>
      <tr><td>Drag (background)</td><td>Pan the canvas</td></tr>
      <tr><td>Click a node</td><td>Show details in the left panel</td></tr>
      <tr><td>Double-click a node</td><td>Highlight its immediate neighbours; click background to clear</td></tr>
    </table>

    <h3>Sidebar controls</h3>
    <table>
      <tr><td>Layers toggles</td><td>Show/hide the purple, teal, and grey node groups</td></tr>
      <tr><td>Show labels</td><td>Display title snippets on each node (can slow large graphs)</td></tr>
      <tr><td>Colour by field</td><td>Recolour nodes by research field; field legend appears below</td></tr>
      <tr><td>Year filter</td><td>Drag the sliders or type years to hide works outside the range; focal paper is always shown</td></tr>
      <tr><td>Layout → Re-layout</td><td>Re-run the chosen graph layout algorithm</td></tr>
      <tr><td>Fit</td><td>Zoom to fit all visible nodes</td></tr>
      <tr><td>Centre focal</td><td>Zoom and pan to the focal paper node</td></tr>
    </table>

    <h3>Bottom tables</h3>
    <p><strong>Top cited in map</strong> — the 50 most-cited works in the graph (excluding the focal paper).<br>
    <strong>Top citers</strong> — the 50 most-cited papers that directly cite the focal work.</p>

    <h3>Data source</h3>
    <p>All data is fetched live from <a href="https://openalex.org" target="_blank">OpenAlex</a>
    and cached locally in <code>cache/influence_cache.sqlite</code>.
    Funder data comes from the OpenAlex <code>grants</code> field; coverage is incomplete —
    many papers carry no recorded funder.</p>
  </div>
</div>

<div id="topbar">
  <h1>↗ {focal_title}</h1>
  <span class="meta">{focal_year}</span>
  <span class="meta">{(focal.get('journal') or '')[:40]}</span>
  <span class="meta">{focal_cit:,} citations</span>
  {doi_href}
  <span class="meta" style="margin-left:auto">{len(works):,} works · {len(edges):,} edges</span>
  <button class="help-btn" id="help-open" title="How to use">?</button>
</div>

<div id="main">
  <div id="sidebar">

    <div class="sec">
      <h3>Graph stats</h3>
      <div class="chip"><div><div class="cv">{len(works):,}</div><div class="cl">Total works</div></div></div>
      <div class="chip"><div><div class="cv">{len(edges):,}</div><div class="cl">Citation edges</div></div></div>
      <div class="chip"><div><div class="cv">{focal_cit:,}</div><div class="cl">Focal citations (global)</div></div></div>
    </div>

    <div class="sec">
      <h3>Layers</h3>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-cites-focal" checked>
          <span class="tog-slider" style="background:#AB63FA"></span></label>
        <span class="leg-dot" style="background:#AB63FA"></span>
        Citing this work&nbsp;<span class="badge">{n_cites}</span>
      </div>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-cited-focal" checked>
          <span class="tog-slider" style="background:#00CC96"></span></label>
        <span class="leg-dot" style="background:#00CC96"></span>
        Cited by this work&nbsp;<span class="badge">{n_cited}</span>
      </div>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-d2" checked>
          <span class="tog-slider" style="background:#b0bec5"></span></label>
        <span class="leg-dot" style="background:#b0bec5"></span>
        2nd-hop papers&nbsp;<span class="badge">{n_d2}</span>
      </div>
    </div>

    <div class="sec">
      <h3>Display</h3>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="show-labels">
          <span class="tog-slider"></span></label>
        Show labels
      </div>
      <div class="tog-row">
        <label class="tog"><input type="checkbox" id="color-by-field">
          <span class="tog-slider"></span></label>
        Colour by field
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

    <div class="sec" id="field-legend-sec" style="display:none">
      <h3>Legend (field)</h3>
      {field_legend}
    </div>

    <div id="detail">
      <h3>Selected paper</h3>
      <div id="detail-body"><span class="hint">Click a node to see details</span></div>
    </div>

  </div>

  <div id="cy"></div>
</div>

<div id="bottom">
  <div id="tabs">
    <div class="tab active" data-tab="top-cited">Top cited in map</div>
    <div class="tab" data-tab="top-citers">Top citers</div>
  </div>
  <div class="tab-pane active" id="tab-top-cited">
    <table id="tbl-cited">
      <tr><th>Year</th><th>Title</th><th>Journal</th><th>Field</th><th class="num">Citations</th></tr>
    </table>
  </div>
  <div class="tab-pane" id="tab-top-citers">
    <table id="tbl-citers">
      <tr><th>Year</th><th>Title</th><th>Journal</th><th>Field</th><th class="num">Citations</th></tr>
    </table>
  </div>
</div>

</div>
<script>
const ELEMENTS     = {elements_json};
const FOCAL_ID     = {json.dumps(focal_id)};
const YEAR_MIN     = {year_min};
const YEAR_MAX     = {year_max};
const FIELD_COLORS = {field_colors_json};
const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements:  ELEMENTS,
  style: [
    {{ selector: 'node', style: {{
        'background-color': 'data(color)', 'width': 'data(size)', 'height': 'data(size)',
        'label': '', 'font-size': '9px', 'text-valign': 'bottom', 'text-margin-y': '3px',
        'color': '#333', 'text-outline-width': '1.5px', 'text-outline-color': '#fff',
        'min-zoomed-font-size': 6,
    }} }},
    {{ selector: 'node[?focal]', style: {{
        'border-width': 3, 'border-color': '#fff',
    }} }},
    {{ selector: 'edge', style: {{
        'width': 1, 'line-color': '#ccc', 'target-arrow-color': '#bbb',
        'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'opacity': 0.5,
    }} }},
    {{ selector: ':selected', style: {{ 'border-width': 3, 'border-color': '#2980b9' }} }},
    {{ selector: '.faded', style: {{ 'opacity': 0.12 }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 6000, idealEdgeLength: 70,
             gravity: 0.3, numIter: 500 }},
  wheelSensitivity: 0.3,
}});

function showDetail(n) {{
  const d = n.data();
  const doi = d.doi ? `<a class="d-doi" href="https://doi.org/${{d.doi}}" target="_blank">DOI ↗</a>` : '';
  document.getElementById('detail-body').innerHTML =
    `<div class="d-row"><span class="d-lbl">Year</span><span class="d-val">${{d.year||'—'}}</span></div>
     <div class="d-row"><span class="d-lbl">Citations</span><span class="d-val">${{d.cit}}</span></div>
     <div class="d-row"><span class="d-lbl">Journal</span><span class="d-val">${{d.journal||'—'}}</span></div>
     <div class="d-row"><span class="d-lbl">Field</span><span class="d-val">${{d.field||'—'}}</span></div>
     <div class="d-row"><span class="d-lbl">Countries</span><span class="d-val">${{d.countries||'—'}}</span></div>
     <div class="d-row"><span class="d-lbl">Funder(s)</span><span class="d-val">${{d.funders||'—'}}</span></div>
     <div class="d-row"><span class="d-lbl">Title</span><span class="d-val" style="font-size:.7rem">${{d.title}}</span></div>
     <div style="margin-top:.3rem">${{doi}}</div>`;
}}
cy.on('tap', 'node', e => showDetail(e.target));
cy.on('tap', e => {{ if (e.target === cy)
  document.getElementById('detail-body').innerHTML = '<span class="hint">Click a node to see details</span>'; }});

// double-click → neighbour highlight
cy.on('dblclick', 'node', e => {{
  cy.elements().addClass('faded');
  e.target.closedNeighborhood().removeClass('faded');
}});
cy.on('tap', e => {{ if (e.target === cy) cy.elements().removeClass('faded'); }});

let _visRoles = {{'cites_focal': true, 'cited_by_focal': true, 'd2': true}};
let _byField = false, _showLabels = false, _yrFrom = YEAR_MIN, _yrTo = YEAR_MAX;

function applyFilters() {{
  cy.nodes().forEach(n => {{
    const role = n.data('role'), yr = n.data('year') || 0;
    const yearOk = n.data('focal') || yr === 0 || (yr >= _yrFrom && yr <= _yrTo);
    const roleOk = n.data('focal') || _visRoles[role] !== false;
    n.style('display', (yearOk && roleOk) ? 'element' : 'none');
  }});
  cy.edges().forEach(e => {{
    e.style('display',
      (e.source().style('display') !== 'none' && e.target().style('display') !== 'none')
      ? 'element' : 'none');
  }});
}}
function applyColors() {{
  cy.nodes().forEach(n => {{
    n.style('background-color', _byField
      ? (FIELD_COLORS[n.data('field')] || '#b0bec5')
      : n.data('role_color'));
  }});
  document.getElementById('field-legend-sec').style.display = _byField ? '' : 'none';
}}

[['show-cites-focal','cites_focal'],['show-cited-focal','cited_by_focal'],['show-d2','d2']]
  .forEach(([id, role]) => {{
    document.getElementById(id).addEventListener('change', e => {{
      _visRoles[role] = e.target.checked; applyFilters();
    }});
  }});
document.getElementById('show-labels').addEventListener('change', e => {{
  _showLabels = e.target.checked;
  cy.nodes().forEach(n => n.style('label', _showLabels ? (n.data('label') || '') : ''));
}});
document.getElementById('color-by-field').addEventListener('change', e => {{
  _byField = e.target.checked; applyColors();
}});

function _updateFill(y1, y2) {{
  const span = (YEAR_MAX - YEAR_MIN) || 1;
  document.getElementById('yr-fill').style.left  = ((y1 - YEAR_MIN) / span * 100) + '%';
  document.getElementById('yr-fill').style.right = ((YEAR_MAX - y2) / span * 100) + '%';
}}
_updateFill(YEAR_MIN, YEAR_MAX);
let _yrTimer = null;
function syncYearSlider() {{
  let y1 = parseInt(document.getElementById('yr-from').value);
  let y2 = parseInt(document.getElementById('yr-to').value);
  if (y1 > y2) {{ const t = y1; y1 = y2; y2 = t; }}
  document.getElementById('yr-from-num').value = y1;
  document.getElementById('yr-to-num').value   = y2;
  _updateFill(y1, y2);
  clearTimeout(_yrTimer);
  _yrTimer = setTimeout(() => {{ _yrFrom = y1; _yrTo = y2; applyFilters(); }}, 250);
}}
function syncYearNums() {{
  let y1 = parseInt(document.getElementById('yr-from-num').value) || YEAR_MIN;
  let y2 = parseInt(document.getElementById('yr-to-num').value)   || YEAR_MAX;
  y1 = Math.max(YEAR_MIN, Math.min(YEAR_MAX, y1));
  y2 = Math.max(YEAR_MIN, Math.min(YEAR_MAX, y2));
  if (y1 > y2) [y1, y2] = [y2, y1];
  document.getElementById('yr-from').value     = y1;
  document.getElementById('yr-to').value       = y2;
  document.getElementById('yr-from-num').value = y1;
  document.getElementById('yr-to-num').value   = y2;
  _updateFill(y1, y2); _yrFrom = y1; _yrTo = y2; applyFilters();
}}
['yr-from','yr-to'].forEach(id =>
  document.getElementById(id).addEventListener('input', syncYearSlider));
['yr-from-num','yr-to-num'].forEach(id => {{
  const el = document.getElementById(id);
  el.addEventListener('change', syncYearNums);
  el.addEventListener('keydown', e => {{ if (e.key === 'Enter') syncYearNums(); }});
}});

function runLayout(name) {{
  cy.layout({{ name, animate: false, fit: true,
    ...(name === 'cose' ? {{nodeRepulsion:6000,idealEdgeLength:70,gravity:0.3,numIter:500}} : {{}}),
  }}).run();
}}
document.getElementById('layout-sel').addEventListener('change', e => runLayout(e.target.value));
document.getElementById('relayout-btn').addEventListener('click',
  () => runLayout(document.getElementById('layout-sel').value));
document.getElementById('fit-btn').addEventListener('click', () => cy.fit(undefined, 30));
document.getElementById('center-btn').addEventListener('click', () => {{
  const node = cy.getElementById(FOCAL_ID);
  if (node.length) cy.animate({{center:{{eles:node}},zoom:2}}, {{duration:400}});
}});

document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  }});
}});

(function buildTables() {{
  const works = {{}};
  ELEMENTS.forEach(el => {{ if (el.data && el.data.id && !el.data.source) works[el.data.id] = el.data; }});
  const byCit = Object.values(works).filter(w => w.id !== FOCAL_ID)
    .sort((a,b) => b.cit - a.cit).slice(0,50);
  const tbody1 = document.getElementById('tbl-cited');
  byCit.forEach(w => {{
    const tr = tbody1.insertRow();
    tr.innerHTML = `<td>${{w.year||''}}</td><td>${{w.title.slice(0,80)}}</td>
      <td>${{w.journal.slice(0,35)}}</td><td>${{w.field.slice(0,25)}}</td>
      <td class="num">${{w.cit}}</td>`;
  }});
  const citingIds = new Set(
    ELEMENTS.filter(el => el.data && el.data.source && el.data.target === FOCAL_ID)
            .map(el => el.data.source));
  const citers = Object.values(works).filter(w => citingIds.has(w.id))
    .sort((a,b) => b.cit - a.cit).slice(0,50);
  const tbody2 = document.getElementById('tbl-citers');
  citers.forEach(w => {{
    const tr = tbody2.insertRow();
    tr.innerHTML = `<td>${{w.year||''}}</td><td>${{w.title.slice(0,80)}}</td>
      <td>${{w.journal.slice(0,35)}}</td><td>${{w.field.slice(0,25)}}</td>
      <td class="num">${{w.cit}}</td>`;
  }});
}})();

// ── Help modal ──────────────────────────────────────────────────────────────
document.getElementById('help-open').addEventListener('click',
  () => document.getElementById('help-overlay').classList.add('open'));
document.getElementById('help-close').addEventListener('click',
  () => document.getElementById('help-overlay').classList.remove('open'));
document.getElementById('help-overlay').addEventListener('click', e => {{
  if (e.target === document.getElementById('help-overlay'))
    document.getElementById('help-overlay').classList.remove('open');
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Written: {output_path}  ({os.path.getsize(output_path)//1024} KB)")


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL AUTHOR INFLUENCE SECTION
# Crawls OpenAlex live across all works by a given author.
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_author_openalex(query: str, con) -> Optional[dict]:
    """Search OpenAlex /authors by display name; return best match dict."""
    time.sleep(REQUEST_DELAY)
    data = _get("https://api.openalex.org/authors", {
        "search": query,
        "per-page": 5,
        "select": "id,display_name,works_count,cited_by_count,orcid",
    })
    results = data.get("results") or []
    if not results:
        return None
    best = results[0]
    return {
        "id":             best.get("id", ""),
        "name":           best.get("display_name", query),
        "works_count":    best.get("works_count", 0),
        "cited_by_count": best.get("cited_by_count", 0),
        "orcid":          best.get("orcid") or "",
    }


def _fetch_author_works_openalex(author_id: str, con, limit: int = 500) -> list:
    """Fetch all works by an author from OpenAlex (cursor pagination)."""
    # Use full URL in filter; grants not available on paginated author-works queries
    full_id = author_id if author_id.startswith("https://") else f"https://openalex.org/{author_id}"
    works = []
    cursor = "*"
    while len(works) < limit:
        page_limit = min(PAGE_SIZE, limit - len(works))
        time.sleep(REQUEST_DELAY)
        data = _get(BASE_URL, {
            "filter":   f"authorships.author.id:{full_id}",
            "sort":     "cited_by_count:desc",
            "select":   _OA_SELECT_NOGRANTS,
            "per-page": page_limit,
            "cursor":   cursor,
        })
        results = data.get("results") or []
        if not results:
            break
        for w in results:
            meta = _extract_work(w)
            _cache_put(con, meta["id"], meta)
            works.append(meta)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return works


def _crawl_author(focal_works: list, depth: int, max_refs: int,
                  max_cites_per_paper: int, con, d2_seeds: int = 15) -> tuple:
    """
    Crawl citation neighbourhood around all author papers.
    Returns (works, edges, roles) where roles are:
      author_paper  — one of the focal author's own papers
      cites_author  — paper that cites ≥1 author paper
      cited_by_author — paper cited by ≥1 author paper
      d2            — depth-2 neighbour
    """
    focal_ids = {w["id"] for w in focal_works}
    works: dict = {w["id"]: w for w in focal_works}
    edges_set: set = set()
    roles: dict = {wid: "author_paper" for wid in focal_ids}

    # Layer 1 forward: papers that cite any author paper (top N per paper)
    print(f"  layer 1 forward — fetching citers of {len(focal_works)} author papers …")
    total_fwd = 0
    for fw in focal_works:
        citers = _fetch_citers(fw["id"], con, max_cites_per_paper)
        for w in citers:
            works[w["id"]] = w
            edges_set.add((fw["id"], w["id"]))
            roles.setdefault(w["id"], "cites_author")
        total_fwd += len(citers)
    print(f"    {total_fwd} citer links across all author papers")

    # Layer 1 backward: papers cited by author papers (references)
    print(f"  layer 1 backward — fetching references of {len(focal_works)} author papers …")
    total_bwd = 0
    for fw in focal_works:
        refs = _fetch_references(fw, con, max_refs)
        for w in refs:
            if w["id"] not in focal_ids:
                works[w["id"]] = w
                edges_set.add((w["id"], fw["id"]))
                roles.setdefault(w["id"], "cited_by_author")
        total_bwd += len(refs)
    print(f"    {total_bwd} reference links across all author papers")

    if depth >= 2:
        # Layer 2 forward: top citers of the top-cited d1 citers
        d1_citers = [works[wid] for wid in roles
                     if roles[wid] == "cites_author" and wid in works]
        top_seeds = sorted(d1_citers, key=lambda w: w.get("cit", 0), reverse=True)[:d2_seeds]
        print(f"  layer 2 forward — {len(top_seeds)} seeds × up to {d2_seeds} citers …")
        for seed in top_seeds:
            for w in _fetch_citers(seed["id"], con, d2_seeds):
                if w["id"] not in works:
                    works[w["id"]] = w
                    roles.setdefault(w["id"], "d2")
                edges_set.add((seed["id"], w["id"]))


    # Cross-edges within known works — source must not predate target
    # (guards against OpenAlex referenced_works errors and preprint dating mismatches)
    all_ids = set(works.keys())
    added = 0
    for wid, w in works.items():
        src_year = int(w.get("year") or 0)
        for rid in (w.get("ref_ids") or []):
            if rid not in all_ids or rid == wid or (rid, wid) in edges_set:
                continue
            tgt_year = int((works[rid].get("year") or 0))
            if tgt_year and src_year and src_year < tgt_year - 1:
                continue  # rid (reference) predates wid by >1 year — skip bogus edge
            edges_set.add((rid, wid))
            added += 1
    print(f"  cross-edges from metadata: +{added}")

    return works, list(edges_set), roles


def _trim_author_graph(focal_ids: set, works: dict, edges: list,
                       max_nodes: int, roles: dict) -> tuple:
    """Keep all focal nodes + neighbours, budgeting by role so citers are not
    crowded out by highly-cited references."""
    keep = set(focal_ids) & set(works.keys())
    budget = max(0, max_nodes - len(keep))

    def _top(wid_set, n):
        return sorted(wid_set & works.keys(),
                      key=lambda wid: works[wid].get("cit", 0), reverse=True)[:n]

    # Classify non-focal d1 neighbours by role
    citers  = {wid for wid, r in roles.items() if r == "cites_author"    and wid in works}
    refs    = {wid for wid, r in roles.items() if r == "cited_by_author" and wid in works}
    d2_all  = {wid for wid, r in roles.items() if r == "d2"              and wid in works}

    # Give each role a proportional share; citers and refs share equally first,
    # any leftover goes to d2 then back to whichever still has candidates.
    share = budget // 3
    leftover = budget - share * 3

    keep.update(_top(citers, share + leftover))
    keep.update(_top(refs   - keep, share))
    keep.update(_top(d2_all - keep, share))

    # Fill any remaining slots with whatever is left (highest citation first)
    remaining = max_nodes - len(keep)
    if remaining > 0:
        rest = (citers | refs | d2_all) - keep
        keep.update(_top(rest, remaining))

    trimmed_edges = [(s, t) for s, t in edges if s in keep and t in keep]
    connected = {n for e in trimmed_edges for n in e} | focal_ids
    keep = keep & (connected | focal_ids)
    trimmed_works = {wid: works[wid] for wid in keep if wid in works}
    trimmed_edges = [(s, t) for s, t in trimmed_edges if s in keep and t in keep]
    trimmed_roles = {wid: roles.get(wid, "d2") for wid in keep}
    return trimmed_works, trimmed_edges, trimmed_roles


AUT_INF_ROLE_COLOR = {
    "author_paper":     "#EF553B",
    "cites_author":     "#AB63FA",
    "cited_by_author":  "#00CC96",
    "d2":               "#b0bec5",
}


def _write_author_influence_html(author_info: dict, focal_ids: set,
                                  works: dict, edges: list, roles: dict,
                                  output_path: str):
    field_map = {}
    for w in works.values():
        f = w.get("field") or "Unknown"
        if f not in field_map:
            field_map[f] = _FIELD_COLORS[len(field_map) % len(_FIELD_COLORS)]

    role_counts = {}
    for r in roles.values():
        role_counts[r] = role_counts.get(r, 0) + 1

    nodes = []
    for wid, w in works.items():
        cit   = w.get("cit", 0) or 0
        role  = roles.get(wid, "d2")
        field = w.get("field") or "Unknown"
        is_focal = wid in focal_ids
        size  = 20 if is_focal else max(5, min(16, 5 + 5 * math.log1p(cit)))
        t     = w.get("title") or ""
        label = (t[:28] + "…") if len(t) > 28 else t
        countries = w.get("countries") or []
        country_str = (", ".join(countries[:3]) + ("…" if len(countries) > 3 else "")) if countries else "—"
        funders = w.get("funders") or []
        funder_str = (", ".join(funders[:3]) + ("…" if len(funders) > 3 else "")) if funders else "—"
        nodes.append({"data": {
            "id":          wid,
            "label":       label,
            "title":       t,
            "year":        w.get("year") or 0,
            "cit":         cit,
            "journal":     w.get("journal") or "",
            "field":       field,
            "countries":   country_str,
            "funders":     funder_str,
            "funder_list": funders,
            "doi":         w.get("doi") or "",
            "role":        role,
            "is_focal":    is_focal,
            "role_color":  AUT_INF_ROLE_COLOR.get(role, "#b0bec5"),
            "field_color": field_map.get(field, "#b0bec5"),
            "color":       AUT_INF_ROLE_COLOR.get(role, "#b0bec5"),
            "size":        size,
        }})

    cy_edges = [{"data": {"id": f"{s}__{t}", "source": s, "target": t}}
                for s, t in edges]
    elements_json     = json.dumps(nodes + cy_edges, separators=(",", ":"))
    focal_ids_json    = json.dumps(list(focal_ids))
    field_colors_json = json.dumps(field_map)

    author_name   = author_info.get("name", "Author")
    works_count   = author_info.get("works_count", len(focal_ids))
    cited_total   = author_info.get("cited_by_count", 0)
    orcid         = author_info.get("orcid", "")
    orcid_href = (f'<a href="{orcid}" target="_blank" class="hdr-link">ORCID ↗</a>'
                  if orcid else "")

    field_legend = "".join(
        f'<div class="leg"><span class="leg-dot" style="background:{col}"></span>{fld}</div>'
        for fld, col in sorted(field_map.items()))

    n_focal    = role_counts.get("author_paper", 0)
    n_citers   = role_counts.get("cites_author", 0)
    n_refs     = role_counts.get("cited_by_author", 0)
    n_d2       = role_counts.get("d2", 0)

    all_years = sorted({w.get("year") or 0 for w in works.values() if w.get("year")})
    year_min  = all_years[0]  if all_years else 1950
    year_max  = all_years[-1] if all_years else 2025

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Author influence — {author_name}</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  font-size:13px;background:#f0f2f5;color:#222}}
#app{{display:grid;grid-template-rows:auto 1fr auto;height:100vh;overflow:hidden}}
#topbar{{display:flex;align-items:center;gap:.6rem;padding:.5rem 1rem;
  background:#1a1a2e;color:#eee;flex-wrap:wrap;z-index:10}}
#topbar h1{{font-size:.95rem;font-weight:600;color:#fff;margin-right:.4rem}}
.meta{{font-size:.75rem;color:#aaa;white-space:nowrap}}
.hdr-link{{font-size:.75rem;color:#7ec8e3;text-decoration:none}}
.hdr-link:hover{{text-decoration:underline}}
#main{{display:grid;grid-template-columns:230px 1fr;overflow:hidden}}
#sidebar{{background:#fff;border-right:1px solid #dde;overflow-y:auto;padding:.6rem;
  display:flex;flex-direction:column;gap:.5rem}}
#sidebar h3{{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin-bottom:.2rem}}
.sec{{border-bottom:1px solid #eee;padding-bottom:.5rem}}
.chip{{background:#f4f6fa;border-radius:6px;padding:.25rem .5rem;margin-bottom:.25rem;
  display:flex;justify-content:space-between}}
.cv{{font-weight:700;font-size:.9rem}}
.cl{{font-size:.67rem;color:#777;margin-top:.05rem}}
.tog-row{{display:flex;align-items:center;gap:.5rem;font-size:.78rem;margin-bottom:.2rem}}
.tog{{position:relative;display:inline-block;width:32px;height:17px}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;cursor:pointer;inset:0;background:#ccc;border-radius:17px;transition:.25s}}
.tog-slider::before{{content:"";position:absolute;height:13px;width:13px;left:2px;bottom:2px;
  background:#fff;border-radius:50%;transition:.25s}}
.tog input:checked+.tog-slider{{background:#2980b9}}
.tog input:checked+.tog-slider::before{{transform:translateX(15px)}}
.badge{{font-size:.67rem;background:#e8ecf0;border-radius:8px;padding:.05rem .35rem;color:#555}}
.primary{{background:#2980b9;color:#fff;border:none;border-radius:4px;padding:.3rem .6rem;
  cursor:pointer;font-size:.78rem}}
.primary:hover{{background:#1f6391}}
.ctrl-row{{display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem;font-size:.78rem}}
.ctrl-row label{{color:#555;white-space:nowrap}}
.ctrl-row select{{flex:1;font-size:.78rem;padding:.15rem .3rem;border:1px solid #ccc;border-radius:3px}}
#detail{{flex:1;padding:.4rem;overflow-y:auto}}
#detail h3{{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin-bottom:.3rem}}
.hint{{color:#aaa;font-size:.78rem}}
.d-row{{display:flex;justify-content:space-between;font-size:.78rem;
  border-bottom:1px solid #f0f0f0;padding:.15rem 0}}
.d-lbl{{color:#777}}
.d-val{{font-weight:500;max-width:130px;text-align:right;word-break:break-word}}
.d-doi{{color:#2980b9;text-decoration:none;font-size:.7rem}}
.leg{{display:flex;align-items:center;gap:.35rem;font-size:.73rem;margin-bottom:.2rem}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
#cy{{width:100%;height:100%}}
.slider-wrap{{display:flex;align-items:center;gap:.5rem;font-size:.78rem}}
.slider-wrap input{{flex:1}}
/* help modal */
#help-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;
  justify-content:center;align-items:center}}
#help-overlay.open{{display:flex}}
#help-box{{background:#fff;border-radius:8px;max-width:560px;width:94%;max-height:88vh;
  overflow-y:auto;padding:1.2rem 1.4rem;font-size:.82rem;line-height:1.55}}
#help-box h2{{font-size:1rem;margin-bottom:.7rem;color:#1a1a2e}}
#help-box h3{{font-size:.8rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin:.9rem 0 .3rem}}
#help-box p{{margin-bottom:.5rem;color:#333}}
#help-box table{{font-size:.78rem;margin-bottom:.5rem}}
#help-box td{{padding:.12rem .4rem;vertical-align:top;border-bottom:1px solid #f0f0f0}}
#help-box td:first-child{{font-weight:600;white-space:nowrap;color:#2980b9;padding-right:.7rem}}
.help-btn{{background:transparent;border:1px solid #666;border-radius:50%;
  width:22px;height:22px;color:#ccc;font-size:.82rem;cursor:pointer;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.help-btn:hover{{background:#333;color:#fff}}
.close-btn{{float:right;background:transparent;border:none;font-size:1.1rem;
  cursor:pointer;color:#888;line-height:1}}
.close-btn:hover{{color:#222}}
</style>
</head>
<body>
<div id="app">

<!-- ── Help modal ─────────────────────────────────────────────────────────── -->
<div id="help-overlay">
  <div id="help-box">
    <button class="close-btn" id="help-close">✕</button>
    <h2>Author influence map — how to use</h2>

    <h3>What this graph shows</h3>
    <p>Each node is a published work; arrows point from cited work → citing work (influence direction).
    Node size reflects global citation count. The author's own papers are shown as
    red stars; other nodes are coloured by their relationship to those papers:</p>
    <table>
      <tr><td style="color:#EF553B">★ Red star</td><td>One of the author's own papers</td></tr>
      <tr><td style="color:#AB63FA">● Purple</td><td>Papers that <em>cite</em> any author paper</td></tr>
      <tr><td style="color:#00CC96">● Teal</td><td>Papers <em>cited by</em> any author paper (references)</td></tr>
      <tr><td style="color:#b0bec5">● Grey</td><td>Second-hop neighbours</td></tr>
    </table>
    <p>When <strong>Colour by field</strong> is on, each colour represents a research field instead.</p>

    <h3>Navigation</h3>
    <table>
      <tr><td>Scroll / pinch</td><td>Zoom in and out</td></tr>
      <tr><td>Drag (background)</td><td>Pan the canvas</td></tr>
      <tr><td>Click a node</td><td>Show details in the left panel</td></tr>
      <tr><td>Double-click a node</td><td>Highlight its immediate neighbours; click background to clear</td></tr>
    </table>

    <h3>Sidebar controls</h3>
    <table>
      <tr><td>Labels toggle</td><td>Show/hide title snippets on nodes (can slow large graphs)</td></tr>
      <tr><td>Colour by field</td><td>Recolour nodes by research field; field legend appears below</td></tr>
      <tr><td>Year filter</td><td>Drag sliders to hide works outside the year range; author papers are always shown</td></tr>
      <tr><td>Layout → Re-layout</td><td>Re-run the chosen graph layout algorithm</td></tr>
      <tr><td>Reset view</td><td>Zoom to fit all visible nodes</td></tr>
    </table>

    <h3>Data source</h3>
    <p>Data fetched live from <a href="https://openalex.org" target="_blank">OpenAlex</a>
    and cached in <code>cache/influence_cache.sqlite</code>.
    Funder data comes from the OpenAlex <code>grants</code> field; coverage is incomplete.</p>
  </div>
</div>

  <div id="topbar">
    <h1>Author influence — {author_name}</h1>
    <span class="meta">{works_count} papers · {cited_total:,} citations total</span>
    {orcid_href}
    <span style="flex:1"></span>
    <span class="meta">{len(works)} nodes · {len(edges)} edges · OpenAlex live crawl</span>
    <button class="help-btn" id="help-open" title="How to use">?</button>
  </div>
  <div id="main">
    <div id="sidebar">
      <div class="sec">
        <h3>Author</h3>
        <div class="chip"><div><div class="cv">{n_focal}</div>
          <div class="cl">papers in graph</div></div></div>
        <div class="chip"><div><div class="cv">{cited_total:,}</div>
          <div class="cl">total citations (OA)</div></div></div>
        <div class="chip"><div><div class="cv">{n_citers}</div>
          <div class="cl">papers citing author</div></div></div>
        <div class="chip"><div><div class="cv">{n_refs}</div>
          <div class="cl">papers cited by author</div></div></div>
      </div>
      <div class="sec">
        <h3>Layers</h3>
        <div class="tog-row">
          <label class="tog"><input type="checkbox" id="show-cites-author" checked>
            <span class="tog-slider" style="background:#AB63FA"></span></label>
          <span class="leg-dot" style="background:#AB63FA"></span>
          Citing author&nbsp;<span class="badge">{n_citers}</span>
        </div>
        <div class="tog-row">
          <label class="tog"><input type="checkbox" id="show-cited-author" checked>
            <span class="tog-slider" style="background:#00CC96"></span></label>
          <span class="leg-dot" style="background:#00CC96"></span>
          Cited by author&nbsp;<span class="badge">{n_refs}</span>
        </div>
        <div class="tog-row">
          <label class="tog"><input type="checkbox" id="show-d2" checked>
            <span class="tog-slider" style="background:#b0bec5"></span></label>
          <span class="leg-dot" style="background:#b0bec5"></span>
          Depth-2&nbsp;<span class="badge">{n_d2}</span>
        </div>
      </div>
      <div id="detail" class="sec">
        <h3>Selected node</h3>
        <p class="hint">Click a node to see details</p>
      </div>
      <div class="sec">
        <h3>Year filter</h3>
        <div class="slider-wrap">
          <span id="yr-lo-lbl">{year_min}</span>
          <input type="range" id="yr-lo" min="{year_min}" max="{year_max}" value="{year_min}" style="flex:1">
        </div>
        <div class="slider-wrap">
          <span id="yr-hi-lbl">{year_max}</span>
          <input type="range" id="yr-hi" min="{year_min}" max="{year_max}" value="{year_max}" style="flex:1">
        </div>
        <button class="primary" id="btn-yr-reset" style="margin-top:.3rem;width:100%">Reset years</button>
      </div>
      <div class="sec">
        <h3>Display</h3>
        <div class="tog-row"><label class="tog"><input type="checkbox" id="tog-labels">
          <span class="tog-slider"></span></label>Labels</div>
        <div class="tog-row"><label class="tog"><input type="checkbox" id="tog-field">
          <span class="tog-slider"></span></label>Colour by field</div>
        <div class="ctrl-row"><label>Layout</label>
          <select id="sel-layout">
            <option value="cose">CoSE (default)</option>
            <option value="circle">Circle</option>
            <option value="concentric">Concentric</option>
            <option value="grid">Grid</option>
          </select>
        </div>
        <button class="primary" id="btn-relayout" style="margin-top:.3rem;width:100%">Re-layout</button>
        <button class="primary" id="btn-reset" style="margin-top:.3rem;width:100%">Reset view</button>
      </div>
      <div class="sec" id="field-legend-sec" style="display:none">
        <h3>Field legend</h3>
        <div id="field-legend">{field_legend}</div>
      </div>
    </div>
    <div id="cy"></div>
  </div>
</div>
<script>
const ELEMENTS     = {elements_json};
const FOCAL_IDS    = new Set({focal_ids_json});
const FIELD_COLORS = {field_colors_json};
const ROLE_COLOR   = {{
  author_paper:    "#EF553B",
  cites_author:    "#AB63FA",
  cited_by_author: "#00CC96",
  d2:              "#b0bec5",
}};
const YEAR_MIN = {year_min};
const YEAR_MAX = {year_max};

let useField = false;
let yearLo = YEAR_MIN, yearHi = YEAR_MAX;
let _visRoles = {{ cites_author: true, cited_by_author: true, d2: true }};

function nodeColor(d) {{
  return useField ? (FIELD_COLORS[d.field] || "#b0bec5") : (ROLE_COLOR[d.role] || "#b0bec5");
}}

const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{ selector: 'node', style: {{
        'background-color': d => nodeColor(d.data()),
        'width':  'data(size)', 'height': 'data(size)',
        'label':  '',
        'font-size': 10,
        'color': '#444', 'text-valign': 'bottom', 'text-halign': 'center',
        'text-margin-y': 3,
        'text-outline-color': '#fff', 'text-outline-width': 2,
        'min-zoomed-font-size': 6,
        'shape': d => FOCAL_IDS.has(d.data('id')) ? 'star' : 'ellipse',
        'border-width': d => FOCAL_IDS.has(d.data('id')) ? 3 : 1,
        'border-color': d => FOCAL_IDS.has(d.data('id')) ? '#c0392b' : '#fff',
    }}}},
    {{ selector: 'edge', style: {{
        'width': 1, 'line-color': '#ccc',
        'target-arrow-color': '#aaa', 'target-arrow-shape': 'triangle',
        'curve-style': 'bezier', 'opacity': 0.5,
    }}}},
    {{ selector: 'node:selected', style: {{
        'border-width': 3, 'border-color': '#2980b9',
    }}}},
    {{ selector: '.dimmed', style: {{ 'opacity': 0.18 }} }},
    {{ selector: '.highlighted', style: {{ 'opacity': 1 }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 6000, idealEdgeLength: 80 }},
  minZoom: 0.05, maxZoom: 10,
}});

// zoom-compensated font size
function _labelFs() {{ return 10 / Math.max(0.05, cy.zoom()); }}
function _updateLabelFs() {{
  if (document.getElementById('tog-labels').checked)
    cy.nodes().style('font-size', _labelFs());
}}
cy.on('zoom', _updateLabelFs);

// labels toggle
document.getElementById('tog-labels').addEventListener('change', function() {{
  if (this.checked) {{
    cy.nodes().forEach(n => n.style('label', n.data('label') || ''));
    cy.nodes().style('font-size', _labelFs());
  }} else {{
    cy.nodes().style('label', '');
  }}
}});

// field colour toggle — also shows/hides field legend
document.getElementById('tog-field').addEventListener('change', function() {{
  useField = this.checked;
  cy.nodes().forEach(n => n.style('background-color', nodeColor(n.data())));
  document.getElementById('field-legend-sec').style.display = useField ? '' : 'none';
}});

// hover labels
cy.on('mouseover', 'node', function(e) {{
  if (!document.getElementById('tog-labels').checked) {{
    e.target.style('label', e.target.data('label') || '');
    e.target.style('font-size', _labelFs());
  }}
}});
cy.on('mouseout', 'node', function(e) {{
  if (!document.getElementById('tog-labels').checked) {{
    e.target.style('label', '');
    e.target.style('font-size', 10);
  }}
}});

// layer toggles
[['show-cites-author','cites_author'],['show-cited-author','cited_by_author'],['show-d2','d2']].forEach(([id, role]) => {{
  document.getElementById(id).addEventListener('change', function() {{
    _visRoles[role] = this.checked; applyFilters();
  }});
}});

// combined year + role filter
function applyFilters() {{
  cy.nodes().forEach(n => {{
    const y = n.data('year') || 0;
    const focal = FOCAL_IDS.has(n.id());
    const role = n.data('role') || '';
    const yearOk = focal || (y >= yearLo && y <= yearHi);
    const roleOk = focal || _visRoles[role] !== false;
    n.style('display', yearOk && roleOk ? 'element' : 'none');
  }});
  cy.edges().forEach(e => {{
    const src = cy.getElementById(e.data('source'));
    const tgt = cy.getElementById(e.data('target'));
    const v = src.style('display') !== 'none' && tgt.style('display') !== 'none';
    e.style('display', v ? 'element' : 'none');
  }});
}}
document.getElementById('yr-lo').addEventListener('input', function() {{
  yearLo = +this.value; document.getElementById('yr-lo-lbl').textContent = yearLo;
  if (yearLo > yearHi) {{ yearHi = yearLo; document.getElementById('yr-hi').value = yearHi; document.getElementById('yr-hi-lbl').textContent = yearHi; }}
  applyFilters();
}});
document.getElementById('yr-hi').addEventListener('input', function() {{
  yearHi = +this.value; document.getElementById('yr-hi-lbl').textContent = yearHi;
  if (yearHi < yearLo) {{ yearLo = yearHi; document.getElementById('yr-lo').value = yearLo; document.getElementById('yr-lo-lbl').textContent = yearLo; }}
  applyFilters();
}});
document.getElementById('btn-yr-reset').addEventListener('click', function() {{
  yearLo = YEAR_MIN; yearHi = YEAR_MAX;
  document.getElementById('yr-lo').value = yearLo; document.getElementById('yr-lo-lbl').textContent = yearLo;
  document.getElementById('yr-hi').value = yearHi; document.getElementById('yr-hi-lbl').textContent = yearHi;
  applyFilters();
}});

// node click → detail panel + neighbourhood highlight
cy.on('tap', 'node', function(e) {{
  const d = e.target.data();
  const doi_link = d.doi ? `<a class="d-doi" href="https://doi.org/${{d.doi}}" target="_blank">DOI ↗</a>` : '—';
  const focal_badge = FOCAL_IDS.has(d.id) ? ' <span class="badge">author paper</span>' : '';
  document.getElementById('detail').innerHTML = `
    <h3>Selected node</h3>
    <div class="d-row"><span class="d-lbl">Title</span><span class="d-val">${{d.title.slice(0,120)}}${{focal_badge}}</span></div>
    <div class="d-row"><span class="d-lbl">Year</span><span class="d-val">${{d.year||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Citations</span><span class="d-val">${{d.cit}}</span></div>
    <div class="d-row"><span class="d-lbl">Journal</span><span class="d-val">${{d.journal.slice(0,40)||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Field</span><span class="d-val">${{d.field.slice(0,30)||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Countries</span><span class="d-val">${{d.countries}}</span></div>
    <div class="d-row"><span class="d-lbl">Funder(s)</span><span class="d-val">${{d.funders||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Role</span><span class="d-val">${{d.role}}</span></div>
    <div class="d-row"><span class="d-lbl">DOI</span><span class="d-val">${{doi_link}}</span></div>
  `;
  cy.elements().addClass('dimmed');
  e.target.closedNeighborhood().removeClass('dimmed');
}});

// click background → restore all
cy.on('tap', function(e) {{
  if (e.target === cy) {{
    cy.elements().removeClass('dimmed');
    document.getElementById('detail').innerHTML =
      '<h3>Selected node</h3><p class="hint">Click a node to see details</p>';
  }}
}});

// layout
function runLayout(name) {{
  const opts = {{ name, animate: false }};
  if (name === 'cose') {{ opts.nodeRepulsion = 6000; opts.idealEdgeLength = 80; }}
  if (name === 'concentric') {{ opts.concentric = n => FOCAL_IDS.has(n.id()) ? 10 : n.data('cit'); opts.levelWidth = () => 3; }}
  cy.layout(opts).run();
}}
document.getElementById('btn-relayout').addEventListener('click', function() {{
  runLayout(document.getElementById('sel-layout').value);
}});
document.getElementById('btn-reset').addEventListener('click', function() {{ cy.fit(30); }});
document.getElementById('sel-layout').addEventListener('change', function() {{
  runLayout(this.value);
}});

// ── Help modal ──────────────────────────────────────────────────────────────
document.getElementById('help-open').addEventListener('click',
  () => document.getElementById('help-overlay').classList.add('open'));
document.getElementById('help-close').addEventListener('click',
  () => document.getElementById('help-overlay').classList.remove('open'));
document.getElementById('help-overlay').addEventListener('click', e => {{
  if (e.target === document.getElementById('help-overlay'))
    document.getElementById('help-overlay').classList.remove('open');
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Written: {output_path}  ({os.path.getsize(output_path)//1024} KB)")


# ═══════════════════════════════════════════════════════════════════════════════
# PROJECT INFLUENCE SECTION
# NCT trial or named grant (award_id) → focal papers → Cytoscape influence map.
# ═══════════════════════════════════════════════════════════════════════════════

NCT_BASE  = "https://clinicaltrials.gov/api/v2/studies"
PMID_BASE = "https://api.openalex.org/works"

PROJ_INF_ROLE_COLOR = {
    "project_paper":    "#EF553B",
    "cites_project":    "#AB63FA",
    "cited_by_project": "#00CC96",
    "d2":               "#b0bec5",
}


def _fetch_nct_info(nct_id: str) -> Optional[dict]:
    """Fetch trial metadata from ClinicalTrials.gov v2 API."""
    nct_id = nct_id.upper().strip()
    if not nct_id.startswith("NCT"):
        nct_id = "NCT" + nct_id
    time.sleep(REQUEST_DELAY)
    try:
        r = requests.get(f"{NCT_BASE}/{nct_id}",
                         params={"format": "json"}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as exc:
        print(f"  ClinicalTrials.gov error: {exc}")
        return None
    proto = data.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    desc  = proto.get("descriptionModule", {})
    refs  = proto.get("referencesModule", {}).get("references", [])
    pmids = [r["pmid"] for r in refs
             if r.get("pmid") and r.get("type") in ("RESULT", "DERIVED")]
    return {
        "id":          nct_id,
        "name":        ident.get("briefTitle", nct_id),
        "type":        "nct",
        "description": desc.get("briefSummary", "")[:300],
        "pmids":       pmids,
    }


def _pmids_to_openalex(pmids: list, con) -> list:
    """Convert PubMed IDs to OpenAlex work dicts (batched)."""
    works = []
    for i in range(0, len(pmids), 50):
        batch = pmids[i:i+50]
        time.sleep(REQUEST_DELAY)
        data = _get(PMID_BASE, {
            "filter":   "ids.pmid:" + "|".join(batch),
            "select":   _OA_SELECT_NOGRANTS,
            "per-page": 50,
        })
        for w in data.get("results", []):
            meta = _extract_work(w)
            _cache_put(con, meta["id"], meta)
            works.append(meta)
    return works


def _search_nct_openalex(nct_id: str, con, limit: int = 200) -> list:
    """Search OpenAlex for works mentioning an NCT ID in abstract or full text."""
    seen: set = set()
    works: list = []

    def _run_filter(flt: str, n: int) -> None:
        cursor = "*"
        fetched = 0
        while fetched < n:
            page_limit = min(PAGE_SIZE, n - fetched)
            time.sleep(REQUEST_DELAY)
            data = _get(BASE_URL, {
                "filter":   flt,
                "sort":     "cited_by_count:desc",
                "select":   _OA_SELECT_NOGRANTS,
                "per-page": page_limit,
                "cursor":   cursor,
            })
            results = data.get("results") or []
            if not results:
                break
            for w in results:
                meta = _extract_work(w)
                if meta["id"] not in seen:
                    _cache_put(con, meta["id"], meta)
                    works.append(meta)
                    seen.add(meta["id"])
            fetched += len(results)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break

    _run_filter(f'abstract.search:"{nct_id}"', limit)
    _run_filter(f'fulltext.search:"{nct_id}"', limit)
    return works


def _fetch_award_papers(award_id: str, con, limit: int = 200) -> list:
    """Fetch works from OpenAlex by grant award_id (cursor pagination)."""
    works  = []
    cursor = "*"
    while len(works) < limit:
        page_limit = min(PAGE_SIZE, limit - len(works))
        time.sleep(REQUEST_DELAY)
        data = _get(BASE_URL, {
            "filter":   f"awards.funder_award_id:{award_id}",
            "sort":     "cited_by_count:desc",
            "select":   _OA_SELECT_NOGRANTS,
            "per-page": page_limit,
            "cursor":   cursor,
        })
        results = data.get("results") or []
        if not results:
            break
        for w in results:
            meta = _extract_work(w)
            _cache_put(con, meta["id"], meta)
            works.append(meta)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return works


def _write_project_influence_html(project_info: dict, focal_ids: set,
                                   works: dict, edges: list, roles: dict,
                                   output_path: str):
    field_map = {}
    for w in works.values():
        f = w.get("field") or "Unknown"
        if f not in field_map:
            field_map[f] = _FIELD_COLORS[len(field_map) % len(_FIELD_COLORS)]

    role_counts = {}
    for r in roles.values():
        role_counts[r] = role_counts.get(r, 0) + 1

    nodes = []
    for wid, w in works.items():
        cit      = w.get("cit", 0) or 0
        role     = roles.get(wid, "d2")
        field    = w.get("field") or "Unknown"
        is_focal = wid in focal_ids
        size     = 20 if is_focal else max(5, min(16, 5 + 5 * math.log1p(cit)))
        t        = w.get("title") or ""
        label    = (t[:28] + "…") if len(t) > 28 else t
        countries = w.get("countries") or []
        country_str = (", ".join(countries[:3]) + ("…" if len(countries) > 3 else "")) if countries else "—"
        funders  = w.get("funders") or []
        funder_str = (", ".join(funders[:3]) + ("…" if len(funders) > 3 else "")) if funders else "—"
        nodes.append({"data": {
            "id":          wid,
            "label":       label,
            "title":       t,
            "year":        w.get("year") or 0,
            "cit":         cit,
            "journal":     w.get("journal") or "",
            "field":       field,
            "countries":   country_str,
            "funders":     funder_str,
            "funder_list": funders,
            "doi":         w.get("doi") or "",
            "role":        role,
            "is_focal":    is_focal,
            "size":        size,
        }})

    cy_edges = [{"data": {"id": f"{s}__{t}", "source": s, "target": t}}
                for s, t in edges]
    elements_json     = json.dumps(nodes + cy_edges, separators=(",", ":"))
    focal_ids_json    = json.dumps(list(focal_ids))
    field_colors_json = json.dumps(field_map)

    year_vals  = [w.get("year") or 0 for w in works.values() if w.get("year")]
    year_min   = min(year_vals) if year_vals else 2000
    year_max   = max(year_vals) if year_vals else 2024

    n_focal    = role_counts.get("project_paper", 0)
    n_citers   = role_counts.get("cites_project", 0)
    n_refs     = role_counts.get("cited_by_project", 0)
    n_d2       = role_counts.get("d2", 0)

    proj_name  = project_info.get("name", "Project")
    proj_id    = project_info.get("id", "")
    proj_type  = project_info.get("type", "award")
    proj_desc  = project_info.get("description", "")

    type_label = "NCT Trial" if proj_type == "nct" else "Grant / Award"

    field_legend = "".join(
        f'<div class="leg-row"><span class="leg-dot" style="background:{c}"></span>'
        f'<span class="leg-lbl">{f}</span></div>'
        for f, c in sorted(field_map.items()))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{proj_name} — Project Influence Map</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{display:flex;height:100vh;font-family:system-ui,sans-serif;font-size:13px;background:#f0f2f5;color:#1a1a2e}}
#sidebar{{width:270px;min-width:270px;background:#fff;padding:12px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;border-right:1px solid #dde1e8;box-shadow:2px 0 6px rgba(0,0,0,.06)}}
#cy-wrap{{flex:1;position:relative;background:#f7f8fa}}
#cy{{width:100%;height:100%}}
h1{{font-size:1rem;color:#1a1a2e;line-height:1.3}}
h2{{font-size:.75rem;color:#6070a0;font-weight:400;margin-top:2px}}
.sec{{background:#f4f6fb;border:1px solid #dde1ee;border-radius:6px;padding:8px 10px}}
.sec h3{{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:#6070a0;margin-bottom:6px}}
.chip{{display:flex;gap:6px;flex-wrap:wrap}}
.chip>div{{background:#fff;border:1px solid #dde1ee;border-radius:4px;padding:4px 7px;flex:1;min-width:80px}}
.cv{{font-size:1.1rem;font-weight:700;color:#1a1a2e}}
.cl{{font-size:.65rem;color:#8090b0}}
.tog-row{{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:.75rem;color:#2a2a3e}}
.tog{{position:relative;display:inline-block;width:34px;height:18px;flex-shrink:0}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;inset:0;border-radius:9px;background:#c8d0e0;transition:.2s;cursor:pointer}}
.tog-slider:before{{content:"";position:absolute;width:12px;height:12px;left:3px;top:3px;border-radius:50%;background:#fff;transition:.2s}}
.tog input:checked+.tog-slider:before{{transform:translateX(16px)}}
.leg-dot{{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}}
.badge{{background:#e8ecf8;border-radius:3px;padding:1px 5px;font-size:.65rem;color:#5060a0}}
.ctrl-row{{display:flex;align-items:center;justify-content:space-between;gap:6px;padding:2px 0;font-size:.75rem;color:#2a2a3e}}
.ctrl-row select{{background:#fff;color:#1a1a2e;border:1px solid #c8d0e0;border-radius:3px;padding:2px 4px;font-size:.72rem;flex:1}}
button.primary{{background:#e8ecf8;color:#2a2a4e;border:1px solid #c8d0e0;border-radius:4px;padding:4px 8px;cursor:pointer;font-size:.72rem}}
button.primary:hover{{background:#d8e0f0}}
.hint{{font-size:.7rem;color:#8090b0;font-style:italic}}
.slider-wrap{{display:flex;align-items:center;gap:6px;font-size:.7rem;color:#6070a0}}
.slider-wrap input{{flex:1;accent-color:#5060a0}}
.d-row{{display:flex;gap:4px;padding:2px 0;border-bottom:1px solid #e8ecf4;font-size:.72rem}}
.d-lbl{{color:#6070a0;min-width:60px;flex-shrink:0}}
.d-val{{color:#1a1a2e;word-break:break-word}}
a.d-doi{{color:#3060c0;text-decoration:none}}
a.d-doi:hover{{text-decoration:underline}}
.leg-row{{display:flex;align-items:center;gap:5px;padding:1px 0;font-size:.7rem}}
.leg-lbl{{color:#2a2a3e}}
.proj-desc{{font-size:.68rem;color:#6070a0;line-height:1.4;font-style:italic}}
</style>
</head>
<body>
<div id="sidebar">
  <div>
    <h1>{proj_name}</h1>
    <h2>{type_label} · {proj_id}</h2>
    {f'<p class="proj-desc">{proj_desc}</p>' if proj_desc else ''}
  </div>
  <div class="sec">
    <h3>Project</h3>
    <div class="chip">
      <div><div class="cv">{n_focal}</div><div class="cl">project papers</div></div>
      <div><div class="cv">{n_citers}</div><div class="cl">citing papers</div></div>
      <div><div class="cv">{n_refs}</div><div class="cl">cited papers</div></div>
    </div>
  </div>
  <div class="sec">
    <h3>Layers</h3>
    <div class="tog-row">
      <label class="tog"><input type="checkbox" id="show-cited-project" checked>
        <span class="tog-slider" style="background:#00CC96"></span></label>
      <span class="leg-dot" style="background:#00CC96"></span>
      Cited by project&nbsp;<span class="badge">{n_refs}</span>
    </div>
    <div class="tog-row">
      <label class="tog"><input type="checkbox" id="show-cites-project" checked>
        <span class="tog-slider" style="background:#AB63FA"></span></label>
      <span class="leg-dot" style="background:#AB63FA"></span>
      Citing (n=1)&nbsp;<span class="badge">{n_citers}</span>
    </div>
    <div class="tog-row">
      <label class="tog"><input type="checkbox" id="show-d2" checked>
        <span class="tog-slider" style="background:#b0bec5"></span></label>
      <span class="leg-dot" style="background:#b0bec5"></span>
      Citing (n=2)&nbsp;<span class="badge">{n_d2}</span>
    </div>
  </div>
  <div id="detail" class="sec">
    <h3>Selected node</h3>
    <p class="hint">Click a node to see details</p>
  </div>
  <div class="sec">
    <h3>Year filter</h3>
    <div class="slider-wrap">
      <span id="yr-lo-lbl">{year_min}</span>
      <input type="range" id="yr-lo" min="{year_min}" max="{year_max}" value="{year_min}" style="flex:1">
    </div>
    <div class="slider-wrap">
      <span id="yr-hi-lbl">{year_max}</span>
      <input type="range" id="yr-hi" min="{year_min}" max="{year_max}" value="{year_max}" style="flex:1">
    </div>
    <button class="primary" id="btn-yr-reset" style="margin-top:.3rem;width:100%">Reset years</button>
  </div>
  <div class="sec">
    <h3>Display</h3>
    <div class="tog-row"><label class="tog"><input type="checkbox" id="tog-labels">
      <span class="tog-slider"></span></label>Labels</div>
    <div class="tog-row"><label class="tog"><input type="checkbox" id="tog-field">
      <span class="tog-slider"></span></label>Colour by field</div>
    <div class="ctrl-row"><label>Layout</label>
      <select id="sel-layout">
        <option value="cose">CoSE (default)</option>
        <option value="circle">Circle</option>
        <option value="concentric">Concentric</option>
        <option value="grid">Grid</option>
      </select>
    </div>
    <button class="primary" id="btn-relayout" style="margin-top:.3rem;width:100%">Re-layout</button>
    <button class="primary" id="btn-reset" style="margin-top:.3rem;width:100%">Reset view</button>
  </div>
  <div class="sec" id="field-legend-sec" style="display:none">
    <h3>Field legend</h3>
    <div id="field-legend">{field_legend}</div>
  </div>
</div>
<div id="cy-wrap">
  <div id="cy"></div>
</div>
<script>
const ELEMENTS     = {elements_json};
const FOCAL_IDS    = new Set({focal_ids_json});
const FIELD_COLORS = {field_colors_json};
const ROLE_COLOR   = {{
  project_paper:    "#EF553B",
  cites_project:    "#AB63FA",
  cited_by_project: "#00CC96",
  d2:               "#b0bec5",
}};
const YEAR_MIN = {year_min};
const YEAR_MAX = {year_max};

let useField = false;
let yearLo = YEAR_MIN, yearHi = YEAR_MAX;
let _visRoles = {{ cites_project: true, cited_by_project: true, d2: true }};

function nodeColor(d) {{
  return useField ? (FIELD_COLORS[d.field] || "#b0bec5") : (ROLE_COLOR[d.role] || "#b0bec5");
}}

const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{ selector: 'node', style: {{
        'background-color': d => nodeColor(d.data()),
        'width':  'data(size)', 'height': 'data(size)',
        'label':  '',
        'font-size': 10,
        'color': '#1a1a2e', 'text-valign': 'bottom', 'text-halign': 'center',
        'text-margin-y': 3,
        'text-outline-color': '#f0f2f5', 'text-outline-width': 2,
        'min-zoomed-font-size': 6,
        'shape': d => FOCAL_IDS.has(d.data('id')) ? 'star' : 'ellipse',
        'border-width': d => FOCAL_IDS.has(d.data('id')) ? 3 : 1,
        'border-color': d => FOCAL_IDS.has(d.data('id')) ? '#c0392b' : '#ccc',
    }}}},
    {{ selector: 'edge', style: {{
        'width': 1, 'line-color': '#b0b8cc',
        'target-arrow-color': '#909aac', 'target-arrow-shape': 'triangle',
        'curve-style': 'bezier', 'opacity': 0.5,
    }}}},
    {{ selector: 'node:selected', style: {{ 'border-width': 3, 'border-color': '#2980b9' }} }},
    {{ selector: '.dimmed', style: {{ 'opacity': 0.18 }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 6000, idealEdgeLength: 80 }},
  minZoom: 0.05, maxZoom: 10,
}});

function _labelFs() {{ return 10 / Math.max(0.05, cy.zoom()); }}
cy.on('zoom', () => {{ if (document.getElementById('tog-labels').checked) cy.nodes().style('font-size', _labelFs()); }});

document.getElementById('tog-labels').addEventListener('change', function() {{
  if (this.checked) {{ cy.nodes().forEach(n => n.style('label', n.data('label') || '')); cy.nodes().style('font-size', _labelFs()); }}
  else {{ cy.nodes().style('label', ''); }}
}});

document.getElementById('tog-field').addEventListener('change', function() {{
  useField = this.checked;
  cy.nodes().forEach(n => n.style('background-color', nodeColor(n.data())));
  document.getElementById('field-legend-sec').style.display = useField ? '' : 'none';
}});

cy.on('mouseover', 'node', function(e) {{
  if (!document.getElementById('tog-labels').checked) {{
    e.target.style('label', e.target.data('label') || '');
    e.target.style('font-size', _labelFs());
  }}
}});
cy.on('mouseout', 'node', function(e) {{
  if (!document.getElementById('tog-labels').checked) {{
    e.target.style('label', '');
    e.target.style('font-size', 10);
  }}
}});

[['show-cites-project','cites_project'],['show-cited-project','cited_by_project'],['show-d2','d2']].forEach(([id, role]) => {{
  document.getElementById(id).addEventListener('change', function() {{
    _visRoles[role] = this.checked; applyFilters();
  }});
}});

function applyFilters() {{
  cy.nodes().forEach(n => {{
    const y = n.data('year') || 0;
    const focal = FOCAL_IDS.has(n.id());
    const role  = n.data('role') || '';
    const yearOk = focal || (y >= yearLo && y <= yearHi);
    const roleOk = focal || _visRoles[role] !== false;
    n.style('display', yearOk && roleOk ? 'element' : 'none');
  }});
  cy.edges().forEach(e => {{
    const src = cy.getElementById(e.data('source'));
    const tgt = cy.getElementById(e.data('target'));
    e.style('display', src.style('display') !== 'none' && tgt.style('display') !== 'none' ? 'element' : 'none');
  }});
}}
document.getElementById('yr-lo').addEventListener('input', function() {{
  yearLo = +this.value; document.getElementById('yr-lo-lbl').textContent = yearLo;
  if (yearLo > yearHi) {{ yearHi = yearLo; document.getElementById('yr-hi').value = yearHi; document.getElementById('yr-hi-lbl').textContent = yearHi; }}
  applyFilters();
}});
document.getElementById('yr-hi').addEventListener('input', function() {{
  yearHi = +this.value; document.getElementById('yr-hi-lbl').textContent = yearHi;
  if (yearHi < yearLo) {{ yearLo = yearHi; document.getElementById('yr-lo').value = yearLo; document.getElementById('yr-lo-lbl').textContent = yearLo; }}
  applyFilters();
}});
document.getElementById('btn-yr-reset').addEventListener('click', function() {{
  yearLo = YEAR_MIN; yearHi = YEAR_MAX;
  document.getElementById('yr-lo').value = yearLo; document.getElementById('yr-lo-lbl').textContent = yearLo;
  document.getElementById('yr-hi').value = yearHi; document.getElementById('yr-hi-lbl').textContent = yearHi;
  applyFilters();
}});

cy.on('tap', 'node', function(e) {{
  const d = e.target.data();
  const doi_link = d.doi ? `<a class="d-doi" href="https://doi.org/${{d.doi}}" target="_blank">DOI ↗</a>` : '—';
  const focal_badge = FOCAL_IDS.has(d.id) ? ' <span class="badge">project paper</span>' : '';
  document.getElementById('detail').innerHTML = `
    <h3>Selected node</h3>
    <div class="d-row"><span class="d-lbl">Title</span><span class="d-val">${{d.title.slice(0,120)}}${{focal_badge}}</span></div>
    <div class="d-row"><span class="d-lbl">Year</span><span class="d-val">${{d.year||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Citations</span><span class="d-val">${{d.cit}}</span></div>
    <div class="d-row"><span class="d-lbl">Journal</span><span class="d-val">${{d.journal.slice(0,40)||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Field</span><span class="d-val">${{d.field.slice(0,30)||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Countries</span><span class="d-val">${{d.countries}}</span></div>
    <div class="d-row"><span class="d-lbl">Funder(s)</span><span class="d-val">${{d.funders||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Role</span><span class="d-val">${{d.role}}</span></div>
    <div class="d-row"><span class="d-lbl">DOI</span><span class="d-val">${{doi_link}}</span></div>
  `;
  cy.elements().addClass('dimmed');
  e.target.closedNeighborhood().removeClass('dimmed');
}});
cy.on('tap', e => {{
  if (e.target === cy) {{
    cy.elements().removeClass('dimmed');
    document.getElementById('detail').innerHTML =
      '<h3>Selected node</h3><p class="hint">Click a node to see details</p>';
  }}
}});

function runLayout(name) {{
  const opts = {{ name, animate: false }};
  if (name === 'cose') {{ opts.nodeRepulsion = 6000; opts.idealEdgeLength = 80; }}
  if (name === 'concentric') {{ opts.concentric = n => FOCAL_IDS.has(n.id()) ? 10 : n.data('cit'); opts.levelWidth = () => 3; }}
  cy.layout(opts).run();
}}
document.getElementById('btn-relayout').addEventListener('click', () => runLayout(document.getElementById('sel-layout').value));
document.getElementById('btn-reset').addEventListener('click', () => cy.fit(30));
document.getElementById('sel-layout').addEventListener('change', function() {{ runLayout(this.value); }});

</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Written: {output_path}  ({os.path.getsize(output_path)//1024} KB)")


# ═══════════════════════════════════════════════════════════════════════════════
# FUNDER INFLUENCE SECTION
# Crawls OpenAlex live across all works funded by a given organisation.
# ═══════════════════════════════════════════════════════════════════════════════

FUNDER_INF_ROLE_COLOR = {
    "funded_work":     "#EF553B",
    "cites_funded":    "#AB63FA",
    "cited_by_funded": "#00CC96",
    "d2":              "#b0bec5",
}


def _fetch_funder_openalex(query: str) -> Optional[dict]:
    """Search OpenAlex /funders by display name; return best match."""
    time.sleep(REQUEST_DELAY)
    data = _get("https://api.openalex.org/funders", {
        "search":   query,
        "per-page": 5,
        "select":   "id,display_name,works_count,description",
    })
    results = data.get("results") or []
    if not results:
        return None
    best = results[0]
    funder_id = best.get("id", "")
    # Fetch the full funder record to get works_api_url (not selectable in search)
    works_api_url = ""
    if funder_id:
        time.sleep(REQUEST_DELAY)
        short_id = funder_id.split("/")[-1]
        detail = _get(f"https://api.openalex.org/funders/{short_id}", {})
        works_api_url = detail.get("works_api_url", "")
        print(f"  works_api_url from OpenAlex: {works_api_url}")
    return {
        "id":           funder_id,
        "name":         best.get("display_name", query),
        "works_count":  best.get("works_count", 0),
        "grants_count": 0,
        "description":  (best.get("description") or "")[:120],
        "works_api_url": works_api_url,
    }


_OA_SELECT_MINIMAL = "id,doi,title,publication_year,cited_by_count,primary_location,authorships"


def _fetch_funder_works_openalex(funder_id: str, con, limit: int = 50,
                                  works_api_url: str = "") -> list:
    """Fetch top-cited works funded by this funder.

    Tries multiple filter variants (OpenAlex has renamed funder fields) and
    falls back to a minimal select + per-work enrichment if the full select
    triggers a 400.
    """
    short_id = funder_id.split("/")[-1]

    # Build ordered list of filter candidates
    filter_candidates: list = []
    if works_api_url:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(works_api_url).query)
        api_filter = qs.get("filter", [""])[0]
        if api_filter:
            filter_candidates.append(api_filter)
    for candidate in (f"awards.funder:{short_id}",
                      f"grants.funder:{short_id}",
                      f"grants.funder:https://openalex.org/{short_id}"):
        if candidate not in filter_candidates:
            filter_candidates.append(candidate)

    def _try_fetch(flt, sel=None):
        """Fetch works with filter; sel=None omits the select param.
        Returns (works, ok, reason) where reason is 'ok'/'400'/'empty'."""
        result, cursor = [], "*"
        while len(result) < limit:
            params = {
                "filter":   flt,
                "sort":     "cited_by_count:desc",
                "per-page": min(PAGE_SIZE, limit - len(result)),
                "cursor":   cursor,
            }
            if sel is not None:
                params["select"] = sel
            time.sleep(REQUEST_DELAY)
            try:
                data = _get(BASE_URL, params)
            except Exception as exc:
                if "400" in str(exc):
                    return [], False, "400"
                raise
            rows = data.get("results") or []
            if not rows:
                break
            for w in rows:
                meta = _extract_work(w)
                _cache_put(con, meta["id"], meta)
                result.append(meta)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        reason = "ok" if result else "empty"
        return result, bool(result), reason

    for flt in filter_candidates:
        print(f"  trying filter: {flt}")
        # Probe with no select to confirm filter is valid before adding select fields
        _, filter_ok, reason = _try_fetch(flt, sel=None)
        if reason == "400":
            print(f"    filter itself returns 400 — skipping")
            continue
        if reason == "empty":
            print(f"    filter valid but 0 results — skipping")
            continue
        # Filter works — now try with full select, fall back to minimal
        works, ok, reason = _try_fetch(flt, _OA_SELECT_NOGRANTS)
        if reason == "400":
            print(f"    full select returned 400; retrying with minimal select …")
            works, ok, reason = _try_fetch(flt, _OA_SELECT_MINIMAL)
            if reason == "400":
                print(f"    minimal select also 400; using no-select fetch …")
                works, ok, _ = _try_fetch(flt, sel=None)
            if ok and works:
                print(f"    enriching {len(works)} works with full metadata …")
                works = [_fetch_work(w["id"], con) or w for w in works]
        if works:
            print(f"  found {len(works)} works via {flt}")
            return works
        print(f"    0 results — trying next filter …")

    # All funder filters failed — fall back to institution-based lookup.
    # For companies, OpenAlex tracks research output via author affiliation rather
    # than grants, so institution filter is the practical equivalent.
    print("  funder filters exhausted — trying institution fallback …")
    inst_id = _find_institution_id(funder_id.split("/")[-1])
    if inst_id:
        inst_filter = f"authorships.institutions.id:{inst_id}"
        print(f"  trying institution filter: {inst_filter}")
        works, ok, reason = _try_fetch(inst_filter, _OA_SELECT_NOGRANTS)
        if reason == "400":
            works, ok, _ = _try_fetch(inst_filter, _OA_SELECT_MINIMAL)
            if ok and works:
                works = [_fetch_work(w["id"], con) or w for w in works]
        if works:
            print(f"  found {len(works)} works via institution affiliation")
            return works

    print("  WARNING: no works found via funder or institution filters")
    return []


def _find_institution_id(funder_short_id: str) -> str:
    """Look up the OpenAlex institution ID that corresponds to a funder.

    Fetches the funder record and checks if it lists an associated institution
    (funders often have a matching institution entry in OpenAlex).  Returns the
    short institution ID (e.g. 'I12345678') or empty string if not found.
    """
    time.sleep(REQUEST_DELAY)
    try:
        detail = _get(f"https://api.openalex.org/funders/{funder_short_id}", {})
    except Exception:
        return ""
    # Some funders carry an 'ids.ror' or 'ids.openalex' for their institution
    ids = detail.get("ids") or {}
    roles = detail.get("roles") or []
    # Prefer a role entry of type 'institution'
    for role in roles:
        if role.get("role") == "institution":
            inst_url = role.get("id", "")
            if inst_url:
                return inst_url.split("/")[-1]
    # Fall back: search institutions by the funder display name
    name = detail.get("display_name", "")
    if not name:
        return ""
    time.sleep(REQUEST_DELAY)
    try:
        data = _get("https://api.openalex.org/institutions", {
            "search": name, "per-page": 3,
            "select": "id,display_name",
        })
    except Exception:
        return ""
    results = data.get("results") or []
    if results:
        return results[0].get("id", "").split("/")[-1]
    return ""


def _write_funder_influence_html(funder_info: dict, focal_ids: set,
                                  works: dict, edges: list, roles: dict,
                                  output_path: str):
    field_map = {}
    for w in works.values():
        f = w.get("field") or "Unknown"
        if f not in field_map:
            field_map[f] = _FIELD_COLORS[len(field_map) % len(_FIELD_COLORS)]

    role_counts = {}
    for r in roles.values():
        role_counts[r] = role_counts.get(r, 0) + 1

    nodes = []
    for wid, w in works.items():
        cit      = w.get("cit", 0) or 0
        role     = roles.get(wid, "d2")
        field    = w.get("field") or "Unknown"
        is_focal = wid in focal_ids
        size     = 20 if is_focal else max(5, min(16, 5 + 5 * math.log1p(cit)))
        t        = w.get("title") or ""
        label    = (t[:28] + "…") if len(t) > 28 else t
        countries = w.get("countries") or []
        country_str = (", ".join(countries[:3]) + ("…" if len(countries) > 3 else "")) if countries else "—"
        funders  = w.get("funders") or []
        funder_str = (", ".join(funders[:3]) + ("…" if len(funders) > 3 else "")) if funders else "—"
        nodes.append({"data": {
            "id":          wid,
            "label":       label,
            "title":       t,
            "year":        w.get("year") or 0,
            "cit":         cit,
            "journal":     w.get("journal") or "",
            "field":       field,
            "countries":   country_str,
            "funders":     funder_str,
            "funder_list": funders,
            "doi":         w.get("doi") or "",
            "role":        role,
            "is_focal":    is_focal,
            "role_color":  FUNDER_INF_ROLE_COLOR.get(role, "#b0bec5"),
            "field_color": field_map.get(field, "#b0bec5"),
            "color":       FUNDER_INF_ROLE_COLOR.get(role, "#b0bec5"),
            "size":        size,
        }})

    cy_edges = [{"data": {"id": f"{s}__{t}", "source": s, "target": t}}
                for s, t in edges]
    elements_json     = json.dumps(nodes + cy_edges, separators=(",", ":"))
    focal_ids_json    = json.dumps(list(focal_ids))
    field_colors_json = json.dumps(field_map)

    funder_name   = funder_info.get("name", "Funder")
    works_count   = funder_info.get("works_count", 0)
    grants_count  = funder_info.get("grants_count", 0)
    description   = funder_info.get("description", "")

    field_legend = "".join(
        f'<div class="leg"><span class="leg-dot" style="background:{col}"></span>{fld}</div>'
        for fld, col in sorted(field_map.items()))

    n_focal   = role_counts.get("funded_work", 0)
    n_citers  = role_counts.get("cites_funded", 0)
    n_refs    = role_counts.get("cited_by_funded", 0)
    n_d2      = role_counts.get("d2", 0)

    all_years = sorted({w.get("year") or 0 for w in works.values() if w.get("year")})
    year_min  = all_years[0]  if all_years else 1950
    year_max  = all_years[-1] if all_years else 2025

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Funder influence — {funder_name}</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  font-size:13px;background:#f0f2f5;color:#222}}
#app{{display:grid;grid-template-rows:auto 1fr;height:100vh;overflow:hidden}}
#topbar{{display:flex;align-items:center;gap:.6rem;padding:.5rem 1rem;
  background:#1a1a2e;color:#eee;flex-wrap:wrap;z-index:10}}
#topbar h1{{font-size:.95rem;font-weight:600;color:#fff;margin-right:.4rem}}
.meta{{font-size:.75rem;color:#aaa;white-space:nowrap}}
.hdr-link{{font-size:.75rem;color:#7ec8e3;text-decoration:none}}
.hdr-link:hover{{text-decoration:underline}}
#main{{display:grid;grid-template-columns:230px 1fr;overflow:hidden}}
#sidebar{{background:#fff;border-right:1px solid #dde;overflow-y:auto;padding:.6rem;
  display:flex;flex-direction:column;gap:.5rem}}
#sidebar h3{{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin-bottom:.2rem}}
.sec{{border-bottom:1px solid #eee;padding-bottom:.5rem}}
.chip{{background:#f4f6fa;border-radius:6px;padding:.25rem .5rem;margin-bottom:.25rem;
  display:flex;justify-content:space-between}}
.cv{{font-weight:700;font-size:.9rem}}
.cl{{font-size:.67rem;color:#777;margin-top:.05rem}}
.tog-row{{display:flex;align-items:center;gap:.5rem;font-size:.78rem;margin-bottom:.2rem}}
.tog{{position:relative;display:inline-block;width:32px;height:17px}}
.tog input{{opacity:0;width:0;height:0}}
.tog-slider{{position:absolute;cursor:pointer;inset:0;background:#ccc;border-radius:17px;transition:.25s}}
.tog-slider::before{{content:"";position:absolute;height:13px;width:13px;left:2px;bottom:2px;
  background:#fff;border-radius:50%;transition:.25s}}
.tog input:checked+.tog-slider{{background:#2980b9}}
.tog input:checked+.tog-slider::before{{transform:translateX(15px)}}
.badge{{font-size:.67rem;background:#e8ecf0;border-radius:8px;padding:.05rem .35rem;color:#555}}
.primary{{background:#2980b9;color:#fff;border:none;border-radius:4px;padding:.3rem .6rem;
  cursor:pointer;font-size:.78rem}}
.primary:hover{{background:#1f6391}}
.ctrl-row{{display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem;font-size:.78rem}}
.ctrl-row label{{color:#555;white-space:nowrap}}
.ctrl-row select{{flex:1;font-size:.78rem;padding:.15rem .3rem;border:1px solid #ccc;border-radius:3px}}
#detail{{flex:1;padding:.4rem;overflow-y:auto}}
#detail h3{{font-size:.72rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin-bottom:.3rem}}
.hint{{color:#aaa;font-size:.78rem}}
.d-row{{display:flex;justify-content:space-between;font-size:.78rem;
  border-bottom:1px solid #f0f0f0;padding:.15rem 0}}
.d-lbl{{color:#777}}
.d-val{{font-weight:500;max-width:130px;text-align:right;word-break:break-word}}
.d-doi{{color:#2980b9;text-decoration:none;font-size:.7rem}}
.leg{{display:flex;align-items:center;gap:.35rem;font-size:.73rem;margin-bottom:.2rem}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
#cy{{width:100%;height:100%}}
.slider-wrap{{display:flex;align-items:center;gap:.5rem;font-size:.78rem}}
.slider-wrap input{{flex:1}}
/* help modal */
#help-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;
  justify-content:center;align-items:center}}
#help-overlay.open{{display:flex}}
#help-box{{background:#fff;border-radius:8px;max-width:560px;width:94%;max-height:88vh;
  overflow-y:auto;padding:1.2rem 1.4rem;font-size:.82rem;line-height:1.55}}
#help-box h2{{font-size:1rem;margin-bottom:.7rem;color:#1a1a2e}}
#help-box h3{{font-size:.8rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;color:#555;margin:.9rem 0 .3rem}}
#help-box p{{margin-bottom:.5rem;color:#333}}
#help-box table{{font-size:.78rem;margin-bottom:.5rem}}
#help-box td{{padding:.12rem .4rem;vertical-align:top;border-bottom:1px solid #f0f0f0}}
#help-box td:first-child{{font-weight:600;white-space:nowrap;color:#2980b9;padding-right:.7rem}}
.help-btn{{background:transparent;border:1px solid #666;border-radius:50%;
  width:22px;height:22px;color:#ccc;font-size:.82rem;cursor:pointer;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.help-btn:hover{{background:#333;color:#fff}}
.close-btn{{float:right;background:transparent;border:none;font-size:1.1rem;
  cursor:pointer;color:#888;line-height:1}}
.close-btn:hover{{color:#222}}
</style>
</head>
<body>
<div id="app">

<div id="help-overlay">
  <div id="help-box">
    <button class="close-btn" id="help-close">✕</button>
    <h2>Funder influence map — how to use</h2>
    <h3>What this graph shows</h3>
    <p>Each node is a published work; arrows point from cited → citing (influence direction).
    Node size reflects global citation count. Hexagonal nodes are the top-cited
    works funded by <strong>{funder_name}</strong>:</p>
    <table>
      <tr><td style="color:#EF553B">⬡ Red hex</td><td>A funded work (top-cited, up to --max-works)</td></tr>
      <tr><td style="color:#AB63FA">● Purple</td><td>Papers that cite any funded work</td></tr>
      <tr><td style="color:#00CC96">● Teal</td><td>Papers cited by any funded work (references)</td></tr>
      <tr><td style="color:#b0bec5">● Grey</td><td>Second-hop neighbours</td></tr>
    </table>
    <p>Note: only the top-cited funded papers are included, not all funded works.</p>
    <h3>Navigation</h3>
    <table>
      <tr><td>Scroll / pinch</td><td>Zoom</td></tr>
      <tr><td>Drag background</td><td>Pan</td></tr>
      <tr><td>Click node</td><td>Show details</td></tr>
      <tr><td>Double-click node</td><td>Highlight neighbours; click background to clear</td></tr>
    </table>
    <h3>Controls</h3>
    <table>
      <tr><td>Labels</td><td>Toggle title snippets on nodes</td></tr>
      <tr><td>Colour by field</td><td>Recolour by research field</td></tr>
      <tr><td>Year filter</td><td>Hide works outside the year range; funded papers always shown</td></tr>
      <tr><td>Re-layout</td><td>Re-run the chosen layout algorithm</td></tr>
    </table>
    <h3>Data source</h3>
    <p>Data fetched live from <a href="https://openalex.org" target="_blank">OpenAlex</a>,
    cached in <code>cache/influence_cache.sqlite</code>.
    Only works where OpenAlex records the grant are included — coverage is incomplete.</p>
  </div>
</div>

<div id="topbar">
  <h1>Funder influence — {funder_name}</h1>
  <span class="meta">{works_count:,} total funded works (OA)</span>
  <span class="meta">{n_focal} shown</span>
  {f'<span class="meta">{description}</span>' if description else ''}
  <span style="flex:1"></span>
  <span class="meta">{len(works):,} nodes · {len(edges):,} edges</span>
  <button class="help-btn" id="help-open" title="How to use">?</button>
</div>

<div id="main">
  <div id="sidebar">
    <div class="sec">
      <h3>Funder stats</h3>
      <div class="chip"><div><div class="cv">{n_focal}</div><div class="cl">funded papers shown</div></div></div>
      <div class="chip"><div><div class="cv">{works_count:,}</div><div class="cl">total funded works (OA)</div></div></div>
      <div class="chip"><div><div class="cv">{n_citers}</div><div class="cl">papers citing funded works</div></div></div>
      <div class="chip"><div><div class="cv">{n_refs}</div><div class="cl">papers cited by funded works</div></div></div>
    </div>
    <div class="sec">
      <h3>Role colours</h3>
      <div class="leg"><span class="leg-dot" style="background:#EF553B"></span>Funded work (hexagon)</div>
      <div class="leg"><span class="leg-dot" style="background:#AB63FA"></span>Cites funded work</div>
      <div class="leg"><span class="leg-dot" style="background:#00CC96"></span>Cited by funded work</div>
      <div class="leg"><span class="leg-dot" style="background:#b0bec5"></span>Depth-2</div>
    </div>
    <div class="sec">
      <h3>Year filter</h3>
      <div class="slider-wrap">
        <span id="yr-lo-lbl">{year_min}</span>
        <input type="range" id="yr-lo" min="{year_min}" max="{year_max}" value="{year_min}" style="flex:1">
      </div>
      <div class="slider-wrap">
        <span id="yr-hi-lbl">{year_max}</span>
        <input type="range" id="yr-hi" min="{year_min}" max="{year_max}" value="{year_max}" style="flex:1">
      </div>
      <button class="primary" id="btn-yr-reset" style="margin-top:.3rem;width:100%">Reset years</button>
    </div>
    <div class="sec">
      <h3>Display</h3>
      <div class="tog-row"><label class="tog"><input type="checkbox" id="tog-labels">
        <span class="tog-slider"></span></label>Labels</div>
      <div class="tog-row"><label class="tog"><input type="checkbox" id="tog-field">
        <span class="tog-slider"></span></label>Colour by field</div>
      <div class="ctrl-row"><label>Layout</label>
        <select id="sel-layout">
          <option value="cose">CoSE (default)</option>
          <option value="circle">Circle</option>
          <option value="concentric">Concentric</option>
          <option value="grid">Grid</option>
        </select>
      </div>
      <button class="primary" id="btn-relayout" style="margin-top:.3rem;width:100%">Re-layout</button>
      <button class="primary" id="btn-reset" style="margin-top:.3rem;width:100%">Reset view</button>
    </div>
    <div class="sec">
      <h3>Field legend</h3>
      <div id="field-legend">{field_legend}</div>
    </div>
    <div id="detail">
      <h3>Selected node</h3>
      <p class="hint">Click a node to see details</p>
    </div>
  </div>
  <div id="cy"></div>
</div>
</div>
<script>
const ELEMENTS     = {elements_json};
const FOCAL_IDS    = new Set({focal_ids_json});
const FIELD_COLORS = {field_colors_json};
const ROLE_COLOR   = {{
  funded_work:     "#EF553B",
  cites_funded:    "#AB63FA",
  cited_by_funded: "#00CC96",
  d2:              "#b0bec5",
}};
const YEAR_MIN = {year_min};
const YEAR_MAX = {year_max};

let useField = false;
let yearLo = YEAR_MIN, yearHi = YEAR_MAX;

function nodeColor(d) {{
  return useField ? (FIELD_COLORS[d.field] || "#b0bec5") : (ROLE_COLOR[d.role] || "#b0bec5");
}}

const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: [
    {{ selector: 'node', style: {{
        'background-color': d => nodeColor(d.data()),
        'width': 'data(size)', 'height': 'data(size)',
        'label': '',
        'font-size': 10,
        'color': '#444', 'text-valign': 'bottom', 'text-halign': 'center',
        'text-margin-y': 3,
        'text-outline-color': '#fff', 'text-outline-width': 2,
        'min-zoomed-font-size': 6,
        'shape': d => FOCAL_IDS.has(d.data('id')) ? 'hexagon' : 'ellipse',
        'border-width': d => FOCAL_IDS.has(d.data('id')) ? 3 : 1,
        'border-color': d => FOCAL_IDS.has(d.data('id')) ? '#c0392b' : '#fff',
    }}}},
    {{ selector: 'edge', style: {{
        'width': 1, 'line-color': '#ccc',
        'target-arrow-color': '#aaa', 'target-arrow-shape': 'triangle',
        'curve-style': 'bezier', 'opacity': 0.5,
    }}}},
    {{ selector: 'node:selected', style: {{ 'border-width': 3, 'border-color': '#2980b9' }} }},
    {{ selector: '.dimmed', style: {{ 'opacity': 0.18 }} }},
    {{ selector: '.highlighted', style: {{ 'opacity': 1 }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 6000, idealEdgeLength: 80 }},
  minZoom: 0.05, maxZoom: 10,
}});

function _labelFs() {{ return 10 / Math.max(0.05, cy.zoom()); }}
cy.on('zoom', () => {{ if (document.getElementById('tog-labels').checked) cy.nodes().style('font-size', _labelFs()); }});

document.getElementById('tog-labels').addEventListener('change', function() {{
  if (this.checked) {{ cy.nodes().forEach(n => n.style('label', n.data('label') || '')); cy.nodes().style('font-size', _labelFs()); }}
  else {{ cy.nodes().style('label', ''); }}
}});
document.getElementById('tog-field').addEventListener('change', function() {{
  useField = this.checked;
  cy.nodes().forEach(n => n.style('background-color', nodeColor(n.data())));
}});

// hover labels
cy.on('mouseover', 'node', function(e) {{
  if (!document.getElementById('tog-labels').checked) {{
    e.target.style('label', e.target.data('label') || '');
    e.target.style('font-size', _labelFs());
  }}
}});
cy.on('mouseout', 'node', function(e) {{
  if (!document.getElementById('tog-labels').checked) {{
    e.target.style('label', '');
    e.target.style('font-size', 10);
  }}
}});

function applyYearFilter() {{
  cy.nodes().forEach(n => {{
    const visible = FOCAL_IDS.has(n.id()) || (() => {{ const y = n.data('year')||0; return y >= yearLo && y <= yearHi; }})();
    n.style('display', visible ? 'element' : 'none');
  }});
  cy.edges().forEach(e => {{
    e.style('display',
      cy.getElementById(e.data('source')).style('display') !== 'none' &&
      cy.getElementById(e.data('target')).style('display') !== 'none' ? 'element' : 'none');
  }});
}}
document.getElementById('yr-lo').addEventListener('input', function() {{
  yearLo = +this.value; document.getElementById('yr-lo-lbl').textContent = yearLo;
  if (yearLo > yearHi) {{ yearHi = yearLo; document.getElementById('yr-hi').value = yearHi; document.getElementById('yr-hi-lbl').textContent = yearHi; }}
  applyYearFilter();
}});
document.getElementById('yr-hi').addEventListener('input', function() {{
  yearHi = +this.value; document.getElementById('yr-hi-lbl').textContent = yearHi;
  if (yearHi < yearLo) {{ yearLo = yearHi; document.getElementById('yr-lo').value = yearLo; document.getElementById('yr-lo-lbl').textContent = yearLo; }}
  applyYearFilter();
}});
document.getElementById('btn-yr-reset').addEventListener('click', function() {{
  yearLo = YEAR_MIN; yearHi = YEAR_MAX;
  document.getElementById('yr-lo').value = yearLo; document.getElementById('yr-lo-lbl').textContent = yearLo;
  document.getElementById('yr-hi').value = yearHi; document.getElementById('yr-hi-lbl').textContent = yearHi;
  applyYearFilter();
}});

cy.on('tap', 'node', function(e) {{
  const d = e.target.data();
  const doi_link = d.doi ? `<a class="d-doi" href="https://doi.org/${{d.doi}}" target="_blank">DOI ↗</a>` : '—';
  const focal_badge = FOCAL_IDS.has(d.id) ? ' <span class="badge">funded work</span>' : '';
  document.getElementById('detail').innerHTML = `
    <h3>Selected node</h3>
    <div class="d-row"><span class="d-lbl">Title</span><span class="d-val">${{d.title.slice(0,120)}}${{focal_badge}}</span></div>
    <div class="d-row"><span class="d-lbl">Year</span><span class="d-val">${{d.year||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Citations</span><span class="d-val">${{d.cit}}</span></div>
    <div class="d-row"><span class="d-lbl">Journal</span><span class="d-val">${{d.journal.slice(0,40)||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Field</span><span class="d-val">${{d.field.slice(0,30)||'—'}}</span></div>
    <div class="d-row"><span class="d-lbl">Countries</span><span class="d-val">${{d.countries}}</span></div>
    <div class="d-row"><span class="d-lbl">Role</span><span class="d-val">${{d.role}}</span></div>
    <div class="d-row"><span class="d-lbl">DOI</span><span class="d-val">${{doi_link}}</span></div>`;
  cy.elements().addClass('dimmed');
  e.target.closedNeighborhood().removeClass('dimmed');
}});
cy.on('tap', e => {{
  if (e.target === cy) {{
    cy.elements().removeClass('dimmed');
    document.getElementById('detail').innerHTML =
      '<h3>Selected node</h3><p class="hint">Click a node to see details</p>';
  }}
}});

function runLayout(name) {{
  const opts = {{ name, animate: false }};
  if (name === 'cose') {{ opts.nodeRepulsion = 6000; opts.idealEdgeLength = 80; }}
  if (name === 'concentric') {{ opts.concentric = n => FOCAL_IDS.has(n.id()) ? 10 : n.data('cit'); opts.levelWidth = () => 3; }}
  cy.layout(opts).run();
}}
document.getElementById('btn-relayout').addEventListener('click', () => runLayout(document.getElementById('sel-layout').value));
document.getElementById('btn-reset').addEventListener('click', () => cy.fit(30));
document.getElementById('sel-layout').addEventListener('change', function() {{ runLayout(this.value); }});

document.getElementById('help-open').addEventListener('click', () => document.getElementById('help-overlay').classList.add('open'));
document.getElementById('help-close').addEventListener('click', () => document.getElementById('help-overlay').classList.remove('open'));
document.getElementById('help-overlay').addEventListener('click', e => {{
  if (e.target === document.getElementById('help-overlay')) document.getElementById('help-overlay').classList.remove('open');
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  Written: {output_path}  ({os.path.getsize(output_path)//1024} KB)")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--data-dir", default=None)
    _pre.add_argument("--prefix",   default=None)
    _pre_args, _ = _pre.parse_known_args()
    _hint = _pre_args.data_dir or (
        os.path.join(DEFAULT_DATA_BASE, _pre_args.prefix) if _pre_args.prefix else None)
    cfg = _load_cfg(_hint)

    ap = argparse.ArgumentParser(
        description="Influence report: paper, author, or funder — corpus + global OpenAlex crawl")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--paper",      help="Title substring to search in corpus (paper mode)")
    grp.add_argument("--doi",        help="Paper DOI (paper mode)")
    grp.add_argument("--paper-id",   dest="paper_id",
                     help="OpenAlex work URL (paper mode)")
    grp.add_argument("--author",     help="Author name to search (author mode)")
    grp.add_argument("--author-id",  dest="author_id",
                     help="OpenAlex author URL, e.g. https://openalex.org/A1234 (author mode)")
    grp.add_argument("--funder",     help="Funder name to search, e.g. 'Wellcome Trust' (funder mode)")
    grp.add_argument("--funder-id",  dest="funder_id",
                     help="OpenAlex funder URL, e.g. https://openalex.org/F126220448 (funder mode)")
    grp.add_argument("--nct",        help="ClinicalTrials.gov NCT ID (project mode)")
    grp.add_argument("--award-id",  dest="award_id",
                     help="Grant award ID as in OpenAlex, e.g. 312090 (project mode)")

    # Extra project source: allow both --nct and --award-id together
    ap.add_argument("--also-nct",      dest="also_nct",
                    help="Additional NCT ID to combine with --award-id")
    ap.add_argument("--also-award-id", dest="also_award_id",
                    help="Additional award ID to combine with --nct")

    # Shared / corpus options
    ap.add_argument("--data-dir",    default=None,
                    help="Directory containing CSV data (bibliometrics.py output)")
    ap.add_argument("--prefix",      default=None)
    ap.add_argument("--title",       default=cfg.get("title", "Research Field"),
                    help="Field label for display")
    ap.add_argument("--corpus-size", type=int, default=DEFAULT_CORPUS_SIZE)
    ap.add_argument("--max-ego",     type=int, default=80)
    ap.add_argument("--no-corpus",   action="store_true",
                    help="Skip corpus network report")
    ap.add_argument("--llm-model",   default=DEFAULT_LLM_MODEL)
    ap.add_argument("--no-llm",      action="store_true")

    # Influence / crawl options
    ap.add_argument("--depth",        type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--max-nodes",    type=int, default=DEFAULT_MAX_NODES)
    ap.add_argument("--max-refs",     type=int, default=DEFAULT_MAX_REFS)
    ap.add_argument("--max-cites",    type=int, default=DEFAULT_MAX_CITES)
    ap.add_argument("--max-works",    type=int, default=200,
                    help="Max author works to fetch from OpenAlex (author mode)")
    ap.add_argument("--d2-seeds",     type=int, default=15,
                    help="Number of top-cited layer-1 nodes used as seeds for layer-2 crawl (default 15)")
    ap.add_argument("--max-funded",   type=int, default=50,
                    help="Max funded works to fetch from OpenAlex (funder mode)")
    ap.add_argument("--cache",        default=CACHE_FILE)
    ap.add_argument("--no-influence", action="store_true",
                    help="Skip global influence map")

    ap.add_argument("--output-dir",  default=None,
                    help="Output directory (default: reports/{prefix}/papers/{slug}/ "
                         "or reports/{prefix}/authors/{slug}/)")
    args = ap.parse_args()

    author_mode  = bool(args.author or args.author_id)
    funder_mode  = bool(args.funder or args.funder_id)
    project_mode = bool(args.nct or args.award_id)

    cfg_prefix = cfg.get("prefix", "")
    # In author/project/funder mode, corpus is opt-in: only use config prefix when
    # the user explicitly passed --data-dir or --prefix.
    if (author_mode or project_mode or funder_mode) and not args.data_dir and not args.prefix:
        cfg_prefix = ""
    data_dir   = (args.data_dir or
                  (os.path.join(DEFAULT_DATA_BASE, args.prefix or cfg_prefix)
                   if (args.prefix or cfg_prefix) else DEFAULT_DATA_BASE))
    out_prefix = args.prefix or os.path.basename(data_dir.rstrip("/\\"))

    # ══════════════════════════════════════════════════════════════════════════
    # AUTHOR MODE
    # ══════════════════════════════════════════════════════════════════════════
    if author_mode:
        author_query = args.author or args.author_id
        slug = re.sub(r"[^a-z0-9]+", "_", author_query[:40].lower()).strip("_")
        out_dir = args.output_dir or os.path.join("reports", "authors", slug)
        os.makedirs(out_dir, exist_ok=True)

        # ── Author corpus ─────────────────────────────────────────────────────
        corpus_written = False
        if not args.no_corpus:
            print(f"Loading corpus data from {data_dir} …")
            adata = _load_author_corpus_data(data_dir)
            if adata["papers"] is None:
                print("  papers_detail.csv not found — skipping author corpus.")
                print(f"  Tip: check available data dirs with: ls data/")
            else:
                author_id_c, author_name_c, author_row_c = find_author_in_corpus(
                    author_query, adata)
                if not author_id_c:
                    print("  Author not found in corpus — skipping author corpus.")
                else:
                    print(f"  Found: {author_name_c}")
                    focal_wids = find_author_work_ids(author_id_c, adata)
                    print(f"  {len(focal_wids)} author papers matched in corpus")
                    if focal_wids:
                        an, ae, asi = prepare_author_corpus_graph(
                            adata["papers"], adata["cit_edges"],
                            set(focal_wids),
                            corpus_size=args.corpus_size,
                            max_ego=args.max_ego)
                        _write_author_corpus_html(
                            author_name_c, author_row_c, an, ae, asi,
                            os.path.join(out_dir, "author_corpus.html"))
                        corpus_written = True

        # ── Author global influence ───────────────────────────────────────────
        if not args.no_influence:
            con = _open_cache(args.cache)
            author_info = None

            if args.author_id:
                short = args.author_id.split("/")[-1]
                oa_id = f"https://openalex.org/{short}"
                print(f"\nFetching author from OpenAlex: {oa_id} …")
                time.sleep(REQUEST_DELAY)
                raw = _get(f"https://api.openalex.org/authors/{short}", {
                    "select": "id,display_name,works_count,cited_by_count,orcid"})
                if raw.get("id"):
                    author_info = {
                        "id":             raw["id"],
                        "name":           raw.get("display_name", short),
                        "works_count":    raw.get("works_count", 0),
                        "cited_by_count": raw.get("cited_by_count", 0),
                        "orcid":          raw.get("orcid") or "",
                    }
            else:
                print(f"\nSearching OpenAlex for author: {args.author} …")
                author_info = _fetch_author_openalex(args.author, con)

            if not author_info:
                print("  Author not found in OpenAlex — skipping author influence map.")
            else:
                print(f"  {author_info['name']}  "
                      f"({author_info['works_count']} works, "
                      f"{author_info['cited_by_count']:,} citations)")
                print(f"Fetching author works from OpenAlex (limit={args.max_works}) …")
                focal_works = _fetch_author_works_openalex(
                    author_info["id"], con, limit=args.max_works)
                print(f"  {len(focal_works)} works fetched")

                if not focal_works:
                    print("  No works found — skipping author influence map.")
                else:
                    focal_ids_set = {w["id"] for w in focal_works}
                    print(f"Crawling author influence (depth={args.depth}) …")
                    aw, ae, ar = _crawl_author(
                        focal_works, args.depth, args.max_refs, args.max_cites, con,
                        d2_seeds=args.d2_seeds)
                    print(f"  {len(aw)} works, {len(ae)} edges")
                    print("Writing author influence map …")
                    _write_author_influence_html(
                        author_info, focal_ids_set, aw, ae, ar,
                        os.path.join(out_dir, "author_influence.html"))
            con.close()

        print(f"\nOutput: {out_dir}/")
        if corpus_written:
            print("  author_corpus.html    — author position in field corpus")
        if not args.no_influence:
            print("  author_influence.html — global author influence (OpenAlex crawl)")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # FUNDER MODE
    # ══════════════════════════════════════════════════════════════════════════
    if funder_mode:
        funder_query = args.funder or args.funder_id
        slug = re.sub(r"[^a-z0-9]+", "_", funder_query[:40].lower()).strip("_")
        out_dir = args.output_dir or os.path.join("reports", "funders", slug)
        os.makedirs(out_dir, exist_ok=True)

        con = _open_cache(args.cache)
        funder_info = None

        if args.funder_id:
            short = args.funder_id.split("/")[-1]
            print(f"\nFetching funder from OpenAlex: {args.funder_id} …")
            time.sleep(REQUEST_DELAY)
            raw = _get(f"https://api.openalex.org/funders/{short}", {
                "select": "id,display_name,works_count,grants_count,description"})
            if raw.get("id"):
                funder_info = {
                    "id":           raw["id"],
                    "name":         raw.get("display_name", short),
                    "works_count":  raw.get("works_count", 0),
                    "grants_count": raw.get("grants_count", 0),
                    "description":  (raw.get("description") or "")[:120],
                }
        else:
            print(f"\nSearching OpenAlex for funder: {args.funder} …")
            funder_info = _fetch_funder_openalex(args.funder)

        if not funder_info:
            print("  Funder not found in OpenAlex.")
            con.close()
            return

        print(f"  {funder_info['name']}  "
              f"({funder_info['works_count']:,} funded works in OA)")
        print(f"Fetching top-cited funded works (limit={args.max_funded}) …")
        focal_works = _fetch_funder_works_openalex(
            funder_info["id"], con, limit=args.max_funded,
            works_api_url=funder_info.get("works_api_url", ""))
        print(f"  {len(focal_works)} works fetched")

        if not focal_works:
            print("  No works found — skipping funder influence map.")
        else:
            focal_ids_set = {w["id"] for w in focal_works}
            print(f"Crawling funder influence (depth={args.depth}) …")
            fw, fe, fr = _crawl_author(
                focal_works, args.depth, args.max_refs, args.max_cites, con,
                d2_seeds=args.d2_seeds)
            # rename roles to funder-specific labels
            role_map = {"author_paper": "funded_work", "cites_author": "cites_funded",
                        "cited_by_author": "cited_by_funded"}
            fr = {wid: role_map.get(r, r) for wid, r in fr.items()}
            print(f"  {len(fw)} works, {len(fe)} edges")
            print("Writing funder influence map …")
            _write_funder_influence_html(
                funder_info, focal_ids_set, fw, fe, fr,
                os.path.join(out_dir, "funder_influence.html"))
        con.close()

        print(f"\nOutput: {out_dir}/")
        print("  funder_influence.html — global funder influence (OpenAlex crawl)")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # PROJECT MODE  (--nct / --award-id)
    # ══════════════════════════════════════════════════════════════════════════
    if project_mode:
        # Resolve combined sources: primary arg + optional companion
        nct_id    = args.nct      or getattr(args, "also_nct",      None)
        award_id  = args.award_id or getattr(args, "also_award_id", None)
        if args.nct and getattr(args, "also_award_id", None):
            award_id = args.also_award_id
        if args.award_id and getattr(args, "also_nct", None):
            nct_id = args.also_nct

        con = _open_cache(args.cache)
        seen_ids    : set  = set()
        focal_works : list = []
        nct_info    = None
        award_info  = None

        if nct_id:
            print(f"\nFetching trial from ClinicalTrials.gov: {nct_id} …")
            nct_info = _fetch_nct_info(nct_id)
            if not nct_info:
                print("  Trial not found — skipping NCT source.")
            else:
                pmids = nct_info.pop("pmids", [])
                print(f"  {nct_info['name']}")
                print(f"  {len(pmids)} linked PMIDs — fetching from OpenAlex …")
                nct_works = _pmids_to_openalex(pmids, con)
                print(f"  {len(nct_works)} works resolved via PMID")
                for w in nct_works:
                    if w["id"] not in seen_ids:
                        focal_works.append(w); seen_ids.add(w["id"])
                # Also full-text search OpenAlex for the NCT ID (ClinicalTrials links are often incomplete)
                print(f"  Searching OpenAlex for papers mentioning {nct_id} …")
                search_works = _search_nct_openalex(nct_id, con)
                new_from_search = 0
                for w in search_works:
                    if w["id"] not in seen_ids:
                        focal_works.append(w); seen_ids.add(w["id"]); new_from_search += 1
                print(f"  {new_from_search} additional works found via OpenAlex search")

        if award_id:
            print(f"\nSearching OpenAlex for award_id={award_id} …")
            award_works = _fetch_award_papers(award_id, con, limit=args.max_works)
            print(f"  {len(award_works)} works found")
            new = 0
            for w in award_works:
                if w["id"] not in seen_ids:
                    focal_works.append(w); seen_ids.add(w["id"]); new += 1
            if nct_id:
                print(f"  {new} additional works not already in NCT list")
            award_info = {
                "id":          award_id,
                "name":        f"Grant {award_id}",
                "type":        "award",
                "description": "",
            }

        # Build combined project_info
        if nct_info and award_info:
            project_info = {
                "id":          f"{nct_info['id']}+{award_info['id']}",
                "name":        nct_info["name"],
                "type":        "nct+award",
                "description": nct_info.get("description", ""),
            }
        elif nct_info:
            project_info = nct_info
        else:
            project_info = award_info

        if not focal_works:
            print("  No focal papers found — aborting.")
            con.close(); return

        slug    = re.sub(r"[^a-z0-9]+", "_", project_info["id"][:40].lower()).strip("_")
        out_dir = args.output_dir or os.path.join("reports", "projects", slug)
        os.makedirs(out_dir, exist_ok=True)

        # ── Optional corpus embedding ─────────────────────────────────────────
        corpus_written = False
        if not args.no_corpus:
            print(f"Loading corpus data from {data_dir} …")
            cdata = _load_author_corpus_data(data_dir)
            if cdata["papers"] is None:
                print("  papers_detail.csv not found — skipping corpus overlay.")
                print("  Tip: use --prefix flavanol (or --data-dir data/flavanol)")
            else:
                focal_wids = find_project_work_ids(focal_works, cdata["papers"])
                print(f"  {len(focal_wids)} project papers matched in corpus")
                if focal_wids:
                    pn, pe_c, psi = prepare_author_corpus_graph(
                        cdata["papers"], cdata["cit_edges"],
                        set(focal_wids),
                        corpus_size=args.corpus_size,
                        max_ego=args.max_ego)
                    dummy_row = {"papers": len(focal_wids), "citations": 0, "pagerank": ""}
                    _write_author_corpus_html(
                        project_info["name"], dummy_row, pn, pe_c, psi,
                        os.path.join(out_dir, "project_corpus.html"),
                        subject_type="project")
                    corpus_written = True
                else:
                    print("  No matches found — try a different --prefix.")

        # ── Influence map ─────────────────────────────────────────────────────
        focal_ids_set = {w["id"] for w in focal_works}
        print(f"Crawling project influence (depth={args.depth}) …")
        pw, pe, pr = _crawl_author(
            focal_works, args.depth, args.max_refs, args.max_cites, con,
            d2_seeds=args.d2_seeds)
        role_map = {"author_paper": "project_paper", "cites_author": "cites_project",
                    "cited_by_author": "cited_by_project"}
        pr = {wid: role_map.get(r, r) for wid, r in pr.items()}
        print(f"  {len(pw)} works, {len(pe)} edges")
        print("Writing project influence map …")
        _write_project_influence_html(
            project_info, focal_ids_set, pw, pe, pr,
            os.path.join(out_dir, "project_influence.html"))
        con.close()

        print(f"\nOutput: {out_dir}/")
        if corpus_written:
            print("  project_corpus.html    — project papers in field corpus")
        print("  project_influence.html — project influence map (OpenAlex crawl)")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # PAPER MODE
    # ══════════════════════════════════════════════════════════════════════════

    # ── Corpus section ────────────────────────────────────────────────────────
    paper_id   = None
    corpus_doi = None
    stats      = {}
    top_citing = []
    data       = None

    if not args.no_corpus:
        print(f"Loading corpus data from {data_dir} …")
        data = load_data(data_dir)
        if data["papers"] is None:
            print("  papers_detail.csv not found — skipping corpus report.")
            args.no_corpus = True
        else:
            if args.paper_id:
                paper_id, row = find_paper(args.paper_id, data["papers"], by="id")
            elif args.doi:
                paper_id, row = find_paper(args.doi, data["papers"], by="doi")
            else:
                paper_id, row = find_paper(args.paper, data["papers"], by="title")

            if not paper_id:
                print("  Paper not found in corpus — skipping corpus report.")
                args.no_corpus = True
            else:
                corpus_doi = str(row.get("doi", "")) if row is not None else None
                stats      = paper_stats(paper_id, data, args.title)
                top_citing = top_citing_papers(paper_id, data)
                print(f"  {str(stats.get('title','?'))[:70]}")
                print(f"  year={stats.get('year','?')}  citations={stats.get('citations','?')}  "
                      f"citing_in_corpus={stats.get('n_citing','?')}")

    # Determine slug and output directory from whatever we found
    slug_source = stats.get("title") or args.doi or args.paper_id or args.paper or "paper"
    slug = re.sub(r"[^a-z0-9]+", "_", str(slug_source)[:40].lower()).strip("_")
    out_dir = args.output_dir or os.path.join("reports", out_prefix, "papers", slug)
    os.makedirs(out_dir, exist_ok=True)

    if not args.no_corpus:
        llm_text = None
        if not args.no_llm:
            print(f"Generating LLM commentary ({args.llm_model}) …")
            llm_text = generate_llm_text(stats, top_citing, model=args.llm_model)
            print(f"  {len(llm_text)} chars" if llm_text else "  Ollama unavailable — skipping")

        print(f"Preparing compact corpus graph (corpus_size={SMALL_CORPUS_SIZE}) …")
        nodes_s, edges_s, search_s = prepare_graph_data(
            data["papers"], data["cit_edges"], paper_id,
            corpus_size=SMALL_CORPUS_SIZE, max_ego=SMALL_MAX_EGO, ego_adjacent_only=True)

        print(f"Preparing full corpus graph (corpus_size={args.corpus_size}) …")
        nodes_f, edges_f, search_f = prepare_graph_data(
            data["papers"], data["cit_edges"], paper_id,
            corpus_size=args.corpus_size, max_ego=args.max_ego)

        _write_corpus_html(stats, nodes_s, edges_s, search_s, top_citing,
                           os.path.join(out_dir, "report.html"), llm_text=llm_text)

        _full_caveat = (
            "<strong>Full-corpus graph</strong> — this file may be slow to open. "
            "For everyday use open <code>report.html</code> instead."
        )
        _write_corpus_html(stats, nodes_f, edges_f, search_f, top_citing,
                           os.path.join(out_dir, "report_full.html"),
                           llm_text=llm_text, caveat_html=_full_caveat)

    # ── Influence section ─────────────────────────────────────────────────────
    if not args.no_influence:
        inf_doi     = corpus_doi or args.doi
        inf_work_id = args.paper_id if not inf_doi else None

        if not inf_doi and not inf_work_id:
            print("\nNo DOI available for influence map. "
                  "Use --doi or --paper-id, or ensure the corpus paper has a DOI.")
        else:
            con = _open_cache(args.cache)
            print("\nFetching focal paper from OpenAlex …")
            if inf_doi:
                focal = _fetch_by_doi(inf_doi, con)
            else:
                focal = _fetch_work(inf_work_id, con)

            if not focal:
                print("  Paper not found in OpenAlex — skipping influence map.")
            else:
                print(f"  {focal.get('title','?')[:70]}")
                print(f"  year={focal.get('year')}  citations={focal.get('cit')}")
                print(f"Crawling influence graph (depth={args.depth}) …")
                works, edges, roles = crawl(focal, args.depth, args.max_refs,
                                            args.max_cites, con)
                print(f"  {len(works)} works, {len(edges)} edges before trim")
                works, edges, roles = trim_graph(focal["id"], works, edges,
                                                 args.max_nodes, roles)
                print(f"  {len(works)} works, {len(edges)} edges after trim")
                print("Writing influence map …")
                _write_influence_html(focal["id"], works, edges, roles,
                                      os.path.join(out_dir, "influence.html"))
            con.close()

    print(f"\nOutput: {out_dir}/")
    if not args.no_corpus and paper_id:
        print(f"  report.html       — compact corpus graph ({len(nodes_s)} nodes)")
        print(f"  report_full.html  — full corpus graph ({len(nodes_f)} nodes)")
    if not args.no_influence:
        print(f"  influence.html    — global influence map")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

"""
bibliometrics.py  (formerly upf_bibliometrics.py)
Retrieve literature from OpenAlex and produce ranked CSV tables.
Keywords and topic title are read from config.json in the same directory.

Usage
-----
    python bibliometrics.py                       # uses config.json keywords
    python bibliometrics.py --output-dir results  # custom output dir
    python bibliometrics.py --terms "flavanol" "flavan-3-ol"
    python bibliometrics.py --dry-run             # first 2 pages only
"""

import argparse
import collections
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    for path in [os.path.join(os.path.dirname(__file__), "config.json"), "config.json"]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    return {}

_CFG = _load_config()

DEFAULT_TERMS = _CFG.get("keywords", [
    "ultra-processed food",
    "ultra-processed foods",
    "ultraprocessed food",
    "ultraprocessed foods",
    "ultra-processed diet",
    "ultraprocessed diet",
    "NOVA food classification",
])
DEFAULT_OUTPUT_DIR = "output"
MAILTO = "g.kuhnle@reading.ac.uk"
BASE_URL = "https://api.openalex.org/works"
PAGE_SIZE = 200          # OpenAlex max per-cursor page
REQUEST_DELAY = 1.0      # seconds between paginated requests
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0      # exponential backoff base (seconds)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── API helpers ────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": f"upf_bibliometrics/1.0 (mailto:{MAILTO})"})
    return s


def _get(session: requests.Session, url: str, params: dict, retries: int = MAX_RETRIES) -> dict:
    """GET with exponential-backoff retry on network / 5xx errors."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = RETRY_BACKOFF ** attempt
                log.warning("Rate-limited (429); waiting %.0fs before retry %d/%d", wait, attempt, retries)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF ** attempt
            log.warning("Request error: %s — retry %d/%d in %.0fs", exc, attempt, retries, wait)
            if attempt == retries:
                raise
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")  # unreachable, but satisfies type checkers


def build_filter(terms: list[str]) -> str:
    """
    Build an OpenAlex filter string that matches any of the given terms in
    title or abstract (title_and_abstract.search).
    OpenAlex OR-search: pipe-separate values within a single filter key.
    """
    # Wrap multi-word terms in quotes so OpenAlex treats them as phrases.
    quoted = [f'"{t}"' if " " in t else t for t in terms]
    return "title_and_abstract.search:" + "|".join(quoted)


# ── Fetch all pages ────────────────────────────────────────────────────────────

def fetch_all_works(terms: list[str], session: requests.Session, dry_run: bool = False) -> list[dict]:
    """Paginate through OpenAlex cursor and return every work object."""
    filter_str = build_filter(terms)
    log.info("Filter: %s", filter_str)

    cursor = "*"
    works = []
    page_num = 0

    while cursor:
        params = {
            "filter": filter_str,
            "per-page": PAGE_SIZE,
            "cursor": cursor,
            "mailto": MAILTO,
            "select": (
                "id,doi,title,publication_year,"
                "cited_by_count,authorships,funders,awards,"
                "type,mesh,primary_location"
            ),
        }

        data = _get(session, BASE_URL, params)
        meta = data.get("meta", {})
        results = data.get("results", [])
        next_cursor = meta.get("next_cursor")

        page_num += 1
        total = meta.get("count", "?")
        log.info("Page %3d  —  fetched %d works  (total in API: %s)", page_num, len(results), total)

        works.extend(results)

        if not results or not next_cursor:
            break

        cursor = next_cursor

        if dry_run and page_num >= 2:
            log.info("Dry-run: stopping after 2 pages")
            break

        time.sleep(REQUEST_DELAY)

    log.info("Fetched %d works total across %d pages", len(works), page_num)
    return works


# ── Department extraction ──────────────────────────────────────────────────────

_DEPT_RE = re.compile(
    r'\b(department|dept\.?|school|faculty|center|centre|division|institute|'
    r'unit|laboratory|lab|programme?|group|section|college|clinic|service|'
    r'research\s+\w+)\b',
    re.IGNORECASE,
)
_LEAD_NUM = re.compile(r'^\d+\s*')


def extract_department(raw_strings: list) -> str:
    """
    Heuristic: scan comma-separated segments of the longest raw affiliation
    string for one that looks like a department/school/centre.
    Returns empty string when nothing plausible is found (fail-safe).
    """
    if not raw_strings:
        return ""
    raw = max(raw_strings, key=len)
    for part in re.split(r'[,;]', raw):
        part = _LEAD_NUM.sub("", part).strip()
        if _DEPT_RE.search(part) and 5 < len(part) < 120:
            return part
    return ""


# ── Parse and flatten ──────────────────────────────────────────────────────────

def flatten_works(works: list[dict]) -> list[dict]:
    """
    Explode each work into one row per (work × authorship × institution).
    A single paper may produce multiple rows if it has multiple authors or
    an author affiliated with multiple institutions.
    Includes author_position ("first"/"middle"/"last") and journal_name per row.
    """
    rows = []
    for work in works:
        work_id  = work.get("id", "")
        title    = (work.get("title") or "").strip()
        year     = work.get("publication_year")
        doi      = work.get("doi") or ""
        citations = work.get("cited_by_count", 0)

        # Journal / venue info (work-level, same for every authorship row)
        loc = work.get("primary_location") or {}
        src = loc.get("source") or {}
        journal_name = (src.get("display_name") or "").strip()
        journal_id   = src.get("id") or ""

        authorships = work.get("authorships") or []
        if not authorships:
            rows.append({
                "work_id": work_id, "title": title, "year": year,
                "doi": doi, "citations": citations,
                "author_name": "", "author_id": "",
                "author_position": "",
                "institution": "", "country": "",
                "journal_name": journal_name, "journal_id": journal_id,
            })
            continue

        for authorship in authorships:
            author   = authorship.get("author") or {}
            author_name = author.get("display_name", "")
            author_id   = author.get("id", "")
            author_pos  = authorship.get("author_position", "middle") or "middle"
            department  = extract_department(
                authorship.get("raw_affiliation_strings") or []
            )

            institutions = authorship.get("institutions") or []
            base = {
                "work_id": work_id, "title": title, "year": year,
                "doi": doi, "citations": citations,
                "author_name": author_name, "author_id": author_id,
                "author_position": author_pos,
                "department": department,
                "journal_name": journal_name, "journal_id": journal_id,
            }
            if not institutions:
                rows.append({**base, "institution": "", "country": ""})
            else:
                for inst in institutions:
                    rows.append({
                        **base,
                        "institution": inst.get("display_name", ""),
                        "country":     inst.get("country_code", ""),
                    })
    return rows


# ── Aggregation helpers ────────────────────────────────────────────────────────

def papers_by_country(rows: list[dict]) -> list[dict]:
    """One row per country: unique paper count + total citations (per paper)."""
    # To avoid double-counting citations when a paper has multiple authors,
    # map each (work_id, country) → citations once.
    seen: dict[tuple, int] = {}
    for r in rows:
        key = (r["work_id"], r["country"])
        seen[key] = r["citations"]

    counter: dict[str, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for (_, country), cites in seen.items():
        counter[country]["papers"] += 1
        counter[country]["citations"] += cites

    return sorted(
        [{"country": c, **v} for c, v in counter.items()],
        key=lambda x: x["papers"],
        reverse=True,
    )


def papers_by_institution(rows: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r["work_id"], r["institution"])
        if key not in seen:
            seen[key] = {"country": r["country"], "citations": r["citations"]}

    counter: dict[str, dict] = collections.defaultdict(
        lambda: {"country": "", "papers": 0, "citations": 0}
    )
    for (_, inst), meta in seen.items():
        counter[inst]["country"] = counter[inst]["country"] or meta["country"]
        counter[inst]["papers"] += 1
        counter[inst]["citations"] += meta["citations"]

    return sorted(
        [{"institution": i, **v} for i, v in counter.items()],
        key=lambda x: x["papers"],
        reverse=True,
    )


def papers_by_author(rows: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r["work_id"], r["author_id"])
        if key not in seen:
            seen[key] = {
                "author_name": r["author_name"],
                "institution":  r["institution"],
                "country":      r["country"],
                "citations":    r["citations"],
                "position":     r.get("author_position", "middle") or "middle",
            }

    counter: dict[str, dict] = collections.defaultdict(lambda: {
        "author_name": "", "institution": "", "country": "",
        "papers": 0, "citations": 0,
        "first_author_papers": 0, "last_author_papers": 0, "middle_author_papers": 0,
    })
    # Collect all institution/country values per author to pick the most frequent.
    inst_votes:    dict[str, list] = collections.defaultdict(list)
    country_votes: dict[str, list] = collections.defaultdict(list)

    for (_, author_id), meta in seen.items():
        key = author_id or meta["author_name"]
        counter[key]["author_name"] = meta["author_name"]
        counter[key]["papers"]    += 1
        counter[key]["citations"] += meta["citations"]
        if meta["institution"]:
            inst_votes[key].append(meta["institution"])
        if meta["country"]:
            country_votes[key].append(meta["country"])
        pos = meta.get("position", "middle")
        if pos == "first":
            counter[key]["first_author_papers"]  += 1
        elif pos == "last":
            counter[key]["last_author_papers"]   += 1
        else:
            counter[key]["middle_author_papers"] += 1

    for key in counter:
        if inst_votes[key]:
            counter[key]["institution"] = collections.Counter(inst_votes[key]).most_common(1)[0][0]
        if country_votes[key]:
            counter[key]["country"] = collections.Counter(country_votes[key]).most_common(1)[0][0]

    result = []
    for aid, v in counter.items():
        total = v["papers"] or 1
        v["middle_author_rate"] = round(v["middle_author_papers"] / total, 3)
        result.append({"author_id": aid, **v})
    return sorted(result, key=lambda x: x["papers"], reverse=True)


# ── Department aggregation ────────────────────────────────────────────────────

def papers_by_department(rows: list[dict]) -> list[dict]:
    """
    Aggregate by (department, institution) pair.
    Department name is extracted heuristically from raw_affiliation_strings;
    rows without a detected department are excluded.
    Results are marked experimental — coverage is partial.
    """
    seen: dict[tuple, dict] = {}
    for r in rows:
        dept = r.get("department", "")
        if not dept:
            continue
        key = (r["work_id"], dept, r["institution"])
        if key not in seen:
            seen[key] = {
                "institution": r["institution"],
                "country": r["country"],
                "citations": r["citations"],
            }

    counter: dict[tuple, dict] = collections.defaultdict(
        lambda: {"institution": "", "country": "", "papers": 0, "citations": 0}
    )
    for (_, dept, inst), meta in seen.items():
        key = (dept, inst)
        counter[key]["institution"] = inst
        counter[key]["country"] = counter[key]["country"] or meta["country"]
        counter[key]["papers"] += 1
        counter[key]["citations"] += meta["citations"]

    return sorted(
        [{"department": k[0], **v} for k, v in counter.items()],
        key=lambda x: x["papers"],
        reverse=True,
    )


# ── Funding aggregation ───────────────────────────────────────────────────────

def papers_by_funder(works: list[dict]) -> list[dict]:
    """
    Aggregate funded papers by funder.
    OpenAlex `funders` field: list of {id, display_name, ror}.
    A paper with multiple funders contributes once to each funder's count.
    Citations are attributed once per (work_id, funder) pair.
    """
    seen: dict[tuple, dict] = {}
    for work in works:
        work_id = work.get("id", "")
        citations = work.get("cited_by_count", 0)
        for funder in work.get("funders") or []:
            funder_id   = funder.get("id", "")
            funder_name = funder.get("display_name", "")
            if not funder_name:
                continue
            key = (work_id, funder_id or funder_name)
            if key not in seen:
                seen[key] = {"funder_id": funder_id, "funder_name": funder_name,
                             "citations": citations}

    counter: dict[str, dict] = collections.defaultdict(
        lambda: {"funder_name": "", "papers": 0, "citations": 0}
    )
    for (_, fid), meta in seen.items():
        key = fid or meta["funder_name"]
        counter[key]["funder_name"] = meta["funder_name"]
        counter[key]["papers"]    += 1
        counter[key]["citations"] += meta["citations"]

    return sorted(
        [{"funder_id": fid, **v} for fid, v in counter.items()],
        key=lambda x: x["papers"],
        reverse=True,
    )


def funding_by_country(works: list[dict], rows: list[dict]) -> list[dict]:
    """
    For each country: how many of its papers acknowledged any external funding.
    Uses the authorships rows to map work_id → country, and grants to flag funding.
    """
    funded_works = {
        w["id"] for w in works
        if w.get("funders")
    }
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r["work_id"], r["country"])
        if key not in seen:
            seen[key] = {
                "citations": r["citations"],
                "funded": r["work_id"] in funded_works,
            }

    country_total: dict[str, dict] = collections.defaultdict(
        lambda: {"papers": 0, "funded": 0, "citations": 0}
    )
    for (_, country), meta in seen.items():
        if not country:
            continue
        country_total[country]["papers"]    += 1
        country_total[country]["citations"] += meta["citations"]
        if meta["funded"]:
            country_total[country]["funded"] += 1

    result = []
    for country, v in country_total.items():
        pct = round(100 * v["funded"] / v["papers"], 1) if v["papers"] else 0
        result.append({"country": country, "papers": v["papers"],
                        "funded_papers": v["funded"], "pct_funded": pct,
                        "citations": v["citations"]})
    return sorted(result, key=lambda x: x["papers"], reverse=True)


# ── Study type classification ────────────────────────────────────────────────

_MESH_RCT = {
    "Randomized Controlled Trial", "Randomized Controlled Trials as Topic",
}
_MESH_META = {
    "Meta-Analysis", "Meta-Analysis as Topic",
    "Systematic Review", "Systematic Reviews as Topic",
    "Network Meta-Analysis",
}
_MESH_OBS = {
    "Observational Study", "Observational Studies as Topic",
    "Cohort Studies", "Cross-Sectional Studies", "Case-Control Studies",
    "Longitudinal Studies", "Prospective Studies", "Retrospective Studies",
    "Follow-Up Studies", "Epidemiologic Studies",
}
_MESH_TRIAL = {
    "Clinical Trial", "Clinical Trial, Phase I", "Clinical Trial, Phase II",
    "Clinical Trial, Phase III", "Clinical Trial, Phase IV",
    "Controlled Clinical Trial", "Clinical Trials as Topic",
}
_MESH_REVIEW = {"Review", "Review Literature as Topic"}

# Stem match: catches randomised/randomized/randomisation/randomization/
# randomised crossover / randomized feeding trial / etc.
_RCT_RE  = re.compile(r'\brandomis[a-z]*\b|\brandomiz[a-z]*\b')
_KW_META = {"systematic review", "meta-analysis", "meta analysis"}
_KW_OBS  = {
    "cohort study", "cohort studies", "cross-sectional",
    "case-control", "longitudinal study", "prospective study",
    "retrospective study", "observational study",
}


def classify_study_type(work: dict) -> str:
    """Return one of: RCT | Observational | Systematic Review / Meta-analysis |
    Clinical Trial | Review | Other."""
    mesh_names = {m.get("descriptor_name", "") for m in (work.get("mesh") or [])}
    work_type  = (work.get("type") or "").lower()
    title      = (work.get("title") or "").lower()

    if mesh_names & _MESH_RCT:
        return "RCT"
    if mesh_names & _MESH_META:
        return "Systematic Review / Meta-analysis"
    if mesh_names & _MESH_OBS:
        return "Observational"
    if mesh_names & _MESH_TRIAL:
        return "Clinical Trial"
    if mesh_names & _MESH_REVIEW or work_type == "review":
        return "Review"

    if _RCT_RE.search(title):
        return "RCT"
    if any(kw in title for kw in _KW_META):
        return "Systematic Review / Meta-analysis"
    if any(kw in title for kw in _KW_OBS):
        return "Observational"
    if "review" in title:
        return "Review"

    return "Other"


def papers_by_author_study_type(works: list[dict], rows: list[dict]) -> list[dict]:
    """
    For each (author, study_type) pair: how many UPF papers they have of that type.
    Used by make_study_type_network.py to assign each author their dominant study type.
    """
    work_st = {w.get("id", ""): classify_study_type(w) for w in works}

    seen: dict[tuple, dict] = {}
    for r in rows:
        author_key = r["author_id"] or r["author_name"]
        if not author_key:
            continue
        st  = work_st.get(r["work_id"], "Other")
        key = (r["work_id"], author_key)
        if key not in seen:
            seen[key] = {
                "author_name": r["author_name"],
                "author_id":   r["author_id"],
                "institution": r["institution"],
                "study_type":  st,
                "citations":   r["citations"],
            }

    counter: dict[tuple, dict] = collections.defaultdict(
        lambda: {"author_name": "", "author_id": "", "institution": "", "papers": 0, "citations": 0}
    )
    inst_votes: dict[tuple, list] = collections.defaultdict(list)
    for (_, author_key), meta in seen.items():
        key = (author_key, meta["study_type"])
        counter[key]["author_name"] = counter[key]["author_name"] or meta["author_name"]
        counter[key]["author_id"]   = counter[key]["author_id"]   or meta["author_id"]
        counter[key]["papers"]    += 1
        counter[key]["citations"] += meta["citations"]
        if meta["institution"]:
            inst_votes[key].append(meta["institution"])
    for key in counter:
        if inst_votes[key]:
            counter[key]["institution"] = collections.Counter(inst_votes[key]).most_common(1)[0][0]

    return sorted(
        [{"author_id": k[0], "study_type": k[1], **v} for k, v in counter.items()],
        key=lambda x: (x["author_id"] or "", -x["papers"]),
    )


def papers_by_study_type(works: list[dict]) -> list[dict]:
    counter: dict[str, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        st = classify_study_type(work)
        counter[st]["papers"] += 1
        counter[st]["citations"] += work.get("cited_by_count", 0)
    total = sum(v["papers"] for v in counter.values()) or 1
    return sorted(
        [{"study_type": k, "papers": v["papers"],
          "pct": round(100 * v["papers"] / total, 1),
          "citations": v["citations"]}
         for k, v in counter.items()],
        key=lambda x: x["papers"], reverse=True,
    )


def papers_by_study_type_year(works: list[dict]) -> list[dict]:
    counter: dict[tuple, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        year = work.get("publication_year")
        if year is None:
            continue
        st = classify_study_type(work)
        counter[(year, st)]["papers"] += 1
        counter[(year, st)]["citations"] += work.get("cited_by_count", 0)
    return sorted(
        [{"year": k[0], "study_type": k[1], **v} for k, v in counter.items()],
        key=lambda x: (x["year"], x["study_type"]),
    )


# ── Journal aggregations ──────────────────────────────────────────────────────

def _journal_name(work: dict) -> str:
    src = (work.get("primary_location") or {}).get("source") or {}
    return (src.get("display_name") or "").strip() or "Unknown"


def papers_by_journal(works: list[dict]) -> list[dict]:
    counter: dict[str, dict] = collections.defaultdict(
        lambda: {"journal_id": "", "papers": 0, "citations": 0}
    )
    for work in works:
        jname = _journal_name(work)
        src   = (work.get("primary_location") or {}).get("source") or {}
        jid   = src.get("id") or ""
        counter[jname]["journal_id"] = counter[jname]["journal_id"] or jid
        counter[jname]["papers"]    += 1
        counter[jname]["citations"] += work.get("cited_by_count", 0)
    return sorted(
        [{"journal": k, **v} for k, v in counter.items()],
        key=lambda x: x["papers"], reverse=True,
    )


def papers_by_journal_year(works: list[dict]) -> list[dict]:
    counter: dict[tuple, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        year = work.get("publication_year")
        if year is None:
            continue
        jname = _journal_name(work)
        counter[(jname, year)]["papers"]    += 1
        counter[(jname, year)]["citations"] += work.get("cited_by_count", 0)
    return sorted(
        [{"journal": k[0], "year": k[1], **v} for k, v in counter.items()],
        key=lambda x: (x["journal"], x["year"]),
    )


def papers_by_journal_study_type(works: list[dict]) -> list[dict]:
    counter: dict[tuple, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        jname = _journal_name(work)
        st    = classify_study_type(work)
        counter[(jname, st)]["papers"]    += 1
        counter[(jname, st)]["citations"] += work.get("cited_by_count", 0)
    return sorted(
        [{"journal": k[0], "study_type": k[1], **v} for k, v in counter.items()],
        key=lambda x: (-x["papers"], x["journal"]),
    )


# ── Primary co-authorship edges (first ↔ last author only) ───────────────────

def _primary_pairs(rows: list[dict]) -> dict[str, dict]:
    """Per-work: identify first and last author IDs (deduplicated by author_id)."""
    work_pos: dict[str, dict] = {}
    seen_auth: dict[tuple, bool] = {}
    for r in rows:
        wid = r["work_id"]
        aid = r.get("author_id")
        if not aid:
            continue
        if wid not in work_pos:
            work_pos[wid] = {"year": r["year"], "first": None, "last": None}
        dedup_key = (wid, aid)
        if dedup_key in seen_auth:
            continue
        seen_auth[dedup_key] = True
        pos = r.get("author_position", "middle") or "middle"
        if pos == "first":
            work_pos[wid]["first"] = aid
        elif pos == "last":
            work_pos[wid]["last"] = aid
    return work_pos


def primary_coauthorship_edges(rows: list[dict]) -> list[dict]:
    """
    Co-authorship edges restricted to the first ↔ last author pair per paper.
    Captures the "principal contributor" relationship and filters out honorary
    middle authors (e.g. standing cohort PI lists).
    """
    name_map: dict[str, dict] = {}
    for r in rows:
        aid = r.get("author_id")
        if aid and aid not in name_map:
            name_map[aid] = {
                "name": r["author_name"],
                "institution": r["institution"],
                "country":     r["country"],
            }

    pair_counter: dict[tuple, dict] = {}
    for wid, info in _primary_pairs(rows).items():
        a1, a2 = info.get("first"), info.get("last")
        if not a1 or not a2 or a1 == a2:
            continue
        key = (min(a1, a2), max(a1, a2))
        if key not in pair_counter:
            m1, m2 = name_map.get(a1, {}), name_map.get(a2, {})
            pair_counter[key] = {
                "author1_id": a1, "author1_name": m1.get("name", ""),
                "author1_institution": m1.get("institution", ""),
                "author1_country": m1.get("country", ""),
                "author2_id": a2, "author2_name": m2.get("name", ""),
                "author2_institution": m2.get("institution", ""),
                "author2_country": m2.get("country", ""),
                "shared_papers": 0,
            }
        pair_counter[key]["shared_papers"] += 1

    return sorted(pair_counter.values(), key=lambda x: x["shared_papers"], reverse=True)


def primary_coauthorship_edges_by_year(rows: list[dict]) -> list[dict]:
    name_map: dict[str, str] = {
        r["author_id"]: r["author_name"]
        for r in rows if r.get("author_id")
    }
    pair_year: dict[tuple, int] = collections.defaultdict(int)
    for wid, info in _primary_pairs(rows).items():
        a1, a2 = info.get("first"), info.get("last")
        year   = info.get("year")
        if not a1 or not a2 or a1 == a2 or year is None:
            continue
        pair_year[(min(a1, a2), max(a1, a2), year)] += 1

    result = []
    for (a1, a2, year), papers in pair_year.items():
        result.append({
            "author1_id": a1, "author1_name": name_map.get(a1, ""),
            "author2_id": a2, "author2_name": name_map.get(a2, ""),
            "year": year, "papers": papers,
        })
    return sorted(result, key=lambda x: (x["year"], -x["papers"]))


# ── Temporal aggregations ─────────────────────────────────────────────────────

def papers_by_year(works: list[dict]) -> list[dict]:
    """Annual paper count and total citations."""
    counter: dict[int, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        year = work.get("publication_year")
        if year is None:
            continue
        counter[year]["papers"] += 1
        counter[year]["citations"] += work.get("cited_by_count", 0)
    return sorted([{"year": y, **v} for y, v in counter.items()], key=lambda x: x["year"])


def papers_by_country_year(rows: list[dict]) -> list[dict]:
    """Paper count per (country, year), deduplicated by work_id."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        key = (r["work_id"], r["country"])
        if key not in seen:
            seen[key] = {"year": r["year"], "citations": r["citations"]}

    counter: dict[tuple, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for (_, country), meta in seen.items():
        if not country or meta["year"] is None:
            continue
        counter[(country, meta["year"])]["papers"] += 1
        counter[(country, meta["year"])]["citations"] += meta["citations"]

    return sorted(
        [{"country": k[0], "year": k[1], **v} for k, v in counter.items()],
        key=lambda x: (x["year"], -x["papers"]),
    )


def network_metrics_by_year(rows: list[dict]) -> list[dict]:
    """
    Cumulative co-authorship network size by year.
    For each year: total papers published up to that year, unique authors seen,
    and unique author-pair collaborations seen.
    """
    work_info: dict[str, dict] = {}
    for r in rows:
        wid = r["work_id"]
        if wid not in work_info:
            work_info[wid] = {"year": r["year"], "authors": set()}
        if r["author_id"]:
            work_info[wid]["authors"].add(r["author_id"])

    by_year: dict[int, list] = collections.defaultdict(list)
    for info in work_info.values():
        if info["year"] is not None:
            by_year[info["year"]].append(info["authors"])

    cumulative_authors: set = set()
    cumulative_edges: set = set()
    cumulative_papers = 0
    results = []

    for year in sorted(by_year.keys()):
        yearly_papers = 0
        for authors in by_year[year]:
            cumulative_papers += 1
            yearly_papers += 1
            cumulative_authors.update(authors)
            al = list(authors)
            for i in range(len(al)):
                for j in range(i + 1, len(al)):
                    cumulative_edges.add((min(al[i], al[j]), max(al[i], al[j])))
        results.append({
            "year": year,
            "papers_this_year": yearly_papers,
            "cumulative_papers": cumulative_papers,
            "cumulative_authors": len(cumulative_authors),
            "cumulative_edges": len(cumulative_edges),
        })

    return results


# ── Co-authorship edge list ────────────────────────────────────────────────────

def coauthorship_edges_by_year(rows: list[dict]) -> list[dict]:
    """
    One row per (author1, author2, year): papers they co-authored in that year.
    Useful for building cumulative year-by-year network animation snapshots.
    """
    work_info: dict[str, dict] = {}
    for r in rows:
        wid = r["work_id"]
        if wid not in work_info:
            work_info[wid] = {"year": r["year"], "authors": {}}
        if r["author_id"] and r["author_id"] not in work_info[wid]["authors"]:
            work_info[wid]["authors"][r["author_id"]] = r["author_name"]

    pair_year: dict[tuple, int] = collections.defaultdict(int)
    for info in work_info.values():
        year = info["year"]
        if year is None:
            continue
        ids = list(info["authors"].keys())
        names = info["authors"]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                if a > b:
                    a, b = b, a
                pair_year[(a, b, year)] += 1

    # Attach names (from the aggregated authors table is cleaner; here we re-derive)
    name_map: dict[str, str] = {}
    for r in rows:
        if r["author_id"] and r["author_id"] not in name_map:
            name_map[r["author_id"]] = r["author_name"]

    result = []
    for (a, b, year), papers in pair_year.items():
        result.append({
            "author1_id": a, "author1_name": name_map.get(a, ""),
            "author2_id": b, "author2_name": name_map.get(b, ""),
            "year": year, "papers": papers,
        })
    return sorted(result, key=lambda x: (x["year"], -x["papers"]))


def coauthorship_edges(rows: list[dict]) -> list[dict]:
    """
    Return one row per ordered pair of co-authors on the same paper.
    Edges are undirected; we emit (a, b) with a_id < b_id to avoid duplicates.
    Weight = number of papers shared by the pair.
    """
    # Build work_id → list of (author_id, author_name, institution, country)
    work_authors: dict[str, list[dict]] = collections.defaultdict(list)
    seen_per_work: dict[tuple, bool] = {}
    for r in rows:
        key = (r["work_id"], r["author_id"] or r["author_name"])
        if not r["author_name"] or key in seen_per_work:
            continue
        seen_per_work[key] = True
        work_authors[r["work_id"]].append({
            "id": r["author_id"] or r["author_name"],
            "name": r["author_name"],
            "institution": r["institution"],
            "country": r["country"],
        })

    # Count shared papers per pair
    pair_counter: dict[tuple, dict] = {}
    for work_id, authors in work_authors.items():
        for i in range(len(authors)):
            for j in range(i + 1, len(authors)):
                a, b = authors[i], authors[j]
                key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if key not in pair_counter:
                    pair_counter[key] = {
                        "author1_id": a["id"], "author1_name": a["name"],
                        "author1_institution": a["institution"], "author1_country": a["country"],
                        "author2_id": b["id"], "author2_name": b["name"],
                        "author2_institution": b["institution"], "author2_country": b["country"],
                        "shared_papers": 0,
                    }
                pair_counter[key]["shared_papers"] += 1

    return sorted(pair_counter.values(), key=lambda x: x["shared_papers"], reverse=True)


# ── CSV writers ────────────────────────────────────────────────────────────────

def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows → %s", len(rows), path)


# ── Summary report ─────────────────────────────────────────────────────────────

def print_summary(works: list[dict], rows: list[dict], country_tbl: list[dict], inst_tbl: list[dict], study_type_tbl: Optional[list[dict]] = None) -> None:
    total_papers = len(works)
    unique_authors = len({r["author_id"] or r["author_name"] for r in rows if r["author_name"]})
    unique_insts = len({r["institution"] for r in rows if r["institution"]})
    unique_countries = len({r["country"] for r in rows if r["country"]})

    sep = "─" * 60

    print()
    print(sep)
    print("  UPF BIBLIOMETRICS — SUMMARY REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sep)
    print(f"  Total papers retrieved   : {total_papers:,}")
    print(f"  Unique authors           : {unique_authors:,}")
    print(f"  Unique institutions      : {unique_insts:,}")
    print(f"  Unique countries         : {unique_countries:,}")
    print()

    # Top 10 countries
    print("  TOP 10 COUNTRIES BY PAPER COUNT")
    print(f"  {'Country':<12}  {'Papers':>8}  {'%':>6}  {'Citations':>10}")
    print("  " + "-" * 42)
    for rec in country_tbl[:10]:
        pct = 100 * rec["papers"] / total_papers if total_papers else 0
        country = rec["country"] or "(unknown)"
        print(f"  {country:<12}  {rec['papers']:>8,}  {pct:>5.1f}%  {rec['citations']:>10,}")
    print()

    # Top 10 institutions
    print("  TOP 10 INSTITUTIONS BY PAPER COUNT")
    print(f"  {'Institution':<40}  {'Papers':>8}  {'%':>6}")
    print("  " + "-" * 58)
    for rec in inst_tbl[:10]:
        inst = (rec["institution"] or "(unknown)")[:40]
        pct = 100 * rec["papers"] / total_papers if total_papers else 0
        print(f"  {inst:<40}  {rec['papers']:>8,}  {pct:>5.1f}%")
    print()

    # Concentration metric: top-5 institutions
    top5_papers = sum(r["papers"] for r in inst_tbl[:5])
    top5_pct = 100 * top5_papers / total_papers if total_papers else 0
    print(f"  CONCENTRATION: top-5 institutions account for "
          f"{top5_papers:,} papers ({top5_pct:.1f}% of total)")

    # Study types
    if study_type_tbl:
        print()
        print("  STUDY TYPES")
        print(f"  {'Type':<36}  {'Papers':>8}  {'%':>6}")
        print("  " + "-" * 52)
        for rec in study_type_tbl:
            print(f"  {rec['study_type']:<36}  {rec['papers']:>8,}  {rec['pct']:>5.1f}%")

    print(sep)
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve UPF literature from OpenAlex and produce ranked CSV tables."
    )
    parser.add_argument(
        "--terms",
        nargs="+",
        default=DEFAULT_TERMS,
        metavar="TERM",
        help="Search terms (title+abstract). Default: %(default)s",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help="Directory for CSV output files. Default: %(default)s",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch only the first 2 pages (for testing).",
    )
    return parser.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    session = _session()

    log.info("Search terms: %s", args.terms)
    log.info("Output dir  : %s", args.output_dir)
    if args.dry_run:
        log.info("DRY RUN — only first 2 pages will be fetched")

    works = fetch_all_works(args.terms, session, dry_run=args.dry_run)

    if not works:
        log.warning("No works retrieved — check search terms or API availability.")
        sys.exit(0)

    rows = flatten_works(works)

    country_tbl = papers_by_country(rows)
    inst_tbl = papers_by_institution(rows)
    author_tbl = papers_by_author(rows)

    out = args.output_dir
    write_csv(
        os.path.join(out, "papers_by_country.csv"),
        ["country", "papers", "citations"],
        country_tbl,
    )
    write_csv(
        os.path.join(out, "papers_by_institution.csv"),
        ["institution", "country", "papers", "citations"],
        inst_tbl,
    )
    write_csv(
        os.path.join(out, "papers_by_author.csv"),
        ["author_id", "author_name", "institution", "country",
         "papers", "citations",
         "first_author_papers", "last_author_papers", "middle_author_papers",
         "middle_author_rate"],
        author_tbl,
    )

    dept_tbl = papers_by_department(rows)
    write_csv(
        os.path.join(out, "papers_by_department.csv"),
        ["department", "institution", "country", "papers", "citations"],
        dept_tbl,
    )

    funder_tbl = papers_by_funder(works)
    write_csv(
        os.path.join(out, "papers_by_funder.csv"),
        ["funder_id", "funder_name", "papers", "citations"],
        funder_tbl,
    )
    funding_country_tbl = funding_by_country(works, rows)
    write_csv(
        os.path.join(out, "funding_by_country.csv"),
        ["country", "papers", "funded_papers", "pct_funded", "citations"],
        funding_country_tbl,
    )

    study_type_tbl = papers_by_study_type(works)
    write_csv(
        os.path.join(out, "papers_by_study_type.csv"),
        ["study_type", "papers", "pct", "citations"],
        study_type_tbl,
    )
    study_type_year_tbl = papers_by_study_type_year(works)
    write_csv(
        os.path.join(out, "papers_by_study_type_year.csv"),
        ["year", "study_type", "papers", "citations"],
        study_type_year_tbl,
    )
    author_st_tbl = papers_by_author_study_type(works, rows)
    write_csv(
        os.path.join(out, "papers_by_author_study_type.csv"),
        ["author_id", "author_name", "institution", "study_type", "papers", "citations"],
        author_st_tbl,
    )

    year_tbl = papers_by_year(works)
    write_csv(
        os.path.join(out, "papers_by_year.csv"),
        ["year", "papers", "citations"],
        year_tbl,
    )
    country_year_tbl = papers_by_country_year(rows)
    write_csv(
        os.path.join(out, "papers_by_country_year.csv"),
        ["country", "year", "papers", "citations"],
        country_year_tbl,
    )
    net_metrics_tbl = network_metrics_by_year(rows)
    write_csv(
        os.path.join(out, "network_metrics_by_year.csv"),
        ["year", "papers_this_year", "cumulative_papers", "cumulative_authors", "cumulative_edges"],
        net_metrics_tbl,
    )

    edges_by_year = coauthorship_edges_by_year(rows)
    write_csv(
        os.path.join(out, "coauthorship_edges_by_year.csv"),
        ["author1_id", "author1_name", "author2_id", "author2_name", "year", "papers"],
        edges_by_year,
    )

    edges = coauthorship_edges(rows)
    write_csv(
        os.path.join(out, "coauthorship_edges.csv"),
        [
            "author1_id", "author1_name", "author1_institution", "author1_country",
            "author2_id", "author2_name", "author2_institution", "author2_country",
            "shared_papers",
        ],
        edges,
    )

    # Primary (first ↔ last) co-authorship edges
    primary_edges = primary_coauthorship_edges(rows)
    write_csv(
        os.path.join(out, "coauthorship_edges_primary.csv"),
        [
            "author1_id", "author1_name", "author1_institution", "author1_country",
            "author2_id", "author2_name", "author2_institution", "author2_country",
            "shared_papers",
        ],
        primary_edges,
    )
    primary_edges_yr = primary_coauthorship_edges_by_year(rows)
    write_csv(
        os.path.join(out, "coauthorship_edges_by_year_primary.csv"),
        ["author1_id", "author1_name", "author2_id", "author2_name", "year", "papers"],
        primary_edges_yr,
    )

    # Journal analyses
    journal_tbl = papers_by_journal(works)
    write_csv(
        os.path.join(out, "papers_by_journal.csv"),
        ["journal", "journal_id", "papers", "citations"],
        journal_tbl,
    )
    journal_year_tbl = papers_by_journal_year(works)
    write_csv(
        os.path.join(out, "papers_by_journal_year.csv"),
        ["journal", "year", "papers", "citations"],
        journal_year_tbl,
    )
    journal_st_tbl = papers_by_journal_study_type(works)
    write_csv(
        os.path.join(out, "papers_by_journal_study_type.csv"),
        ["journal", "study_type", "papers", "citations"],
        journal_st_tbl,
    )

    print_summary(works, rows, country_tbl, inst_tbl, study_type_tbl)


if __name__ == "__main__":
    main()

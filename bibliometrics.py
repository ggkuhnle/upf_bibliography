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
from difflib import SequenceMatcher
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional

import requests

from openalex_client import MAILTO, PAGE_SIZE, WORKS_URL as BASE_URL, polite_get

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
DEFAULT_DATA_DIR = "data"
REQUEST_DELAY = 1.0      # seconds between paginated requests
MAX_RETRIES = 12
RETRY_BACKOFF = 2.0      # exponential backoff base (seconds) — used for network errors only

# Subclass keyword matching (populated from config.json "subclasses" key; empty = disabled)
SUBCLASS_KWS: dict = {
    sc: [kw.lower() for kw in kws]
    for sc, kws in (_CFG.get("subclasses") or {}).items()
}


def _classify_subclasses(title: str) -> list:
    """Return list of subclass names whose keywords appear in title (word-boundary prefix match)."""
    if not SUBCLASS_KWS or not title:
        return []
    tl = title.lower()
    return [sc for sc, kws in SUBCLASS_KWS.items()
            if any(re.search(r'\b' + re.escape(kw), tl) for kw in kws)]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── API helpers ──────────────────────────────────────────── hippo ────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": f"bibliometrics/1.0 (mailto:{MAILTO})"})
    return s


def _quota_exit(retry_after: int) -> None:
    # Daily quota exhausted — Retry-After points to midnight reset
    hrs  = retry_after // 3600
    mins = (retry_after % 3600) // 60
    log.warning(
        "Daily API quota reached (Retry-After: %ds ≈ %dh %dm). "
        "Checkpoint saved — restart tomorrow to continue.",
        retry_after, hrs, mins,
    )
    raise SystemExit(0)


def _get(session: requests.Session, url: str, params: dict, retries: int = MAX_RETRIES) -> dict:
    return polite_get(url, params, session=session, retries=retries, timeout=30,
                      rate_wait=60, err_backoff=RETRY_BACKOFF, on_quota=_quota_exit)


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

def fetch_all_works(terms: list[str], session: requests.Session,
                    dry_run: bool = False, checkpoint_path: str = None) -> list[dict]:
    """Paginate through OpenAlex cursor and return every work object.

    If checkpoint_path is given, progress is written to:
      <checkpoint_path>.cursor  — last good cursor + page count (JSON)
      <checkpoint_path>.jsonl   — fetched works, one JSON object per line

    On restart, if both files exist the fetch resumes from the saved cursor
    instead of starting over.  Both files are deleted on clean completion.
    """
    filter_str = build_filter(terms)

    cursor   = "*"
    works: list[dict] = []
    page_num = 0

    _cursor_file = (checkpoint_path + ".cursor") if checkpoint_path else None
    _works_file  = (checkpoint_path + ".jsonl")  if checkpoint_path else None
    _works_fh    = None

    # ── Resume from checkpoint if present ─────────────────────────────────────
    if checkpoint_path and os.path.exists(_cursor_file) and os.path.exists(_works_file):
        try:
            with open(_cursor_file, encoding="utf-8") as fh:
                saved    = json.load(fh)
            cursor   = saved["cursor"]
            page_num = saved["page_num"]
            with open(_works_file, encoding="utf-8") as fh:
                works = [json.loads(ln) for ln in fh if ln.strip()]
            log.info("Resuming from checkpoint: page %d, %d works already fetched",
                     page_num, len(works))
            _works_fh = open(_works_file, "a", encoding="utf-8")
        except Exception as exc:
            log.warning("Checkpoint unreadable (%s); starting from scratch", exc)
            cursor = "*"; works = []; page_num = 0
            _works_fh = open(_works_file, "w", encoding="utf-8") if checkpoint_path else None
    elif checkpoint_path:
        _works_fh = open(_works_file, "w", encoding="utf-8")

    log.info("Filter: %s", filter_str)

    try:
        while cursor:
            params = {
                "filter": filter_str,
                "per-page": PAGE_SIZE,
                "cursor": cursor,
                "mailto": MAILTO,
                "select": (
                    "id,doi,title,publication_year,"
                    "cited_by_count,authorships,funders,awards,"
                    "type,mesh,primary_location,referenced_works"
                ),
            }

            data        = _get(session, BASE_URL, params)
            meta        = data.get("meta", {})
            results     = data.get("results", [])
            next_cursor = meta.get("next_cursor")

            page_num += 1
            total = meta.get("count", "?")
            log.info("Page %3d  —  fetched %d works  (total in API: %s)",
                     page_num, len(results), total)

            works.extend(results)

            if _works_fh:
                for w in results:
                    _works_fh.write(json.dumps(w, separators=(",", ":")) + "\n")
                _works_fh.flush()

            if _cursor_file:
                with open(_cursor_file, "w", encoding="utf-8") as fh:
                    json.dump({"cursor": next_cursor or "", "page_num": page_num}, fh)

            if not results or not next_cursor:
                break

            cursor = next_cursor

            if dry_run and page_num >= 2:
                log.info("Dry-run: stopping after 2 pages")
                break

            time.sleep(REQUEST_DELAY)

    finally:
        if _works_fh:
            _works_fh.close()

    # ── Clean up checkpoint on successful completion ───────────────────────────
    if checkpoint_path:
        for f in (_cursor_file, _works_file):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

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
        subclasses_str = "|".join(_classify_subclasses(title)) or "—"

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
                "subclasses": subclasses_str,
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
                "subclasses": subclasses_str,
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


def _load_aliases() -> dict:
    """Load aliases.json if present.

    Format::

        {
          "aliases": [
            {
              "canonical_id":   "https://openalex.org/A5046912319",
              "canonical_name": "Jeremy P.E. Spencer",
              "duplicate_ids":  [
                "https://openalex.org/A5112075882",
                "https://openalex.org/A5110494473"
              ]
            }
          ]
        }

    Returns a dict mapping each duplicate_id -> (canonical_id, canonical_name).
    """
    _here = os.path.dirname(__file__)
    for path in [os.path.join(_here, "config", "aliases.json"),
                 os.path.join(_here, "aliases.json"), "aliases.json"]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            mapping = {}
            for entry in data.get("aliases", []):
                cid  = entry["canonical_id"]
                cname = entry["canonical_name"]
                for dup in entry.get("duplicate_ids", []):
                    mapping[dup] = (cid, cname)
            log.info("Loaded %d alias mapping(s) from %s", len(mapping), path)
            return mapping
    return {}


def _norm_name(name) -> str:
    """Normalise author name for similarity comparison.

    Collapses spaces around dots ("P. E." -> "P.E.") and folds to lowercase,
    so that minor formatting differences don't prevent matching.
    """
    if not isinstance(name, str):
        return ""
    name = re.sub(r'\.\s+', '.', name)
    return re.sub(r'\s+', ' ', name).strip().lower()


def _dedup_authors(records: list[dict]) -> list[dict]:
    """Merge author records that OpenAlex split into multiple IDs.

    Two records are merged when:
      - They share the same institution (or one has no institution), AND
      - Their normalised names are ≥ 0.92 similar (SequenceMatcher ratio).
    The record with more papers is kept as the canonical entry; counts are summed.

    Comparisons are restricted to authors sharing the same last-name token,
    reducing complexity from O(n²) to O(n × avg_surname_group_size).
    """
    records = sorted(records, key=lambda x: x["papers"], reverse=True)

    # Group by (first-initial + last-name) to limit comparisons.
    # Authors with only 1 paper are excluded — OpenAlex rarely splits them.
    def _group_key(name: str) -> str:
        parts = _norm_name(name).split()
        if not parts:
            return "\x00"
        last  = parts[-1]
        first = parts[0][0] if len(parts) > 1 else ""
        return first + last

    name_groups: dict[str, list[int]] = collections.defaultdict(list)
    for i, r in enumerate(records):
        if r["papers"] < 2:
            continue
        name_groups[_group_key(r["author_name"])].append(i)

    merged = [False] * len(records)
    out = []
    n_merged = 0

    for i, r1 in enumerate(records):
        if merged[i]:
            continue
        n1    = _norm_name(r1["author_name"])
        inst1 = r1.get("institution") or ""
        key   = _group_key(r1["author_name"])

        for j in name_groups.get(key, []):
            if j <= i or merged[j]:
                continue
            r2    = records[j]
            inst2 = r2.get("institution") or ""
            if inst1 and inst2 and inst1 != inst2:
                continue
            n2 = _norm_name(r2["author_name"])
            if SequenceMatcher(None, n1, n2).ratio() >= 0.92:
                r1["papers"]               += r2["papers"]
                r1["citations"]            += r2["citations"]
                r1["first_author_papers"]  += r2["first_author_papers"]
                r1["last_author_papers"]   += r2["last_author_papers"]
                r1["middle_author_papers"] += r2["middle_author_papers"]
                if not inst1 and inst2:
                    r1["institution"] = inst2
                    inst1 = inst2
                if not r1.get("country") and r2.get("country"):
                    r1["country"] = r2["country"]
                merged[j] = True
                n_merged  += 1
                log.debug("Merged '%s' (%s) into '%s' (%s)",
                          r2["author_name"], r2["author_id"],
                          r1["author_name"], r1["author_id"])

        total = r1["papers"] or 1
        r1["middle_author_rate"] = round(r1["middle_author_papers"] / total, 3)
        out.append(r1)

    if n_merged:
        log.info("Author deduplication: merged %d duplicate record(s)", n_merged)
    return out


def papers_by_author(rows: list[dict]) -> list[dict]:
    aliases = _load_aliases()

    seen: dict[tuple, dict] = {}
    for r in rows:
        author_id = r["author_id"]
        author_name = r["author_name"]
        if author_id in aliases:
            author_id, author_name = aliases[author_id]
        key = (r["work_id"], author_id)
        if key not in seen:
            seen[key] = {
                "author_name": author_name,
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

    result = [{"author_id": aid, **v} for aid, v in counter.items()]
    return _dedup_authors(result)


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


def papers_by_subclass(works: list[dict]) -> list[dict]:
    """Paper and citation counts per flavonoid subclass (requires config 'subclasses')."""
    if not SUBCLASS_KWS:
        return []
    counter: dict[str, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        scs = _classify_subclasses(work.get("title") or "")
        if not scs:
            scs = ["—"]
        cites = work.get("cited_by_count", 0)
        for sc in scs:
            counter[sc]["papers"] += 1
            counter[sc]["citations"] += cites
    return sorted(
        [{"subclass": sc, **v} for sc, v in counter.items()],
        key=lambda x: x["papers"], reverse=True,
    )


def papers_by_subclass_year(works: list[dict]) -> list[dict]:
    """Paper count per (subclass, year); multi-label papers counted once per subclass."""
    if not SUBCLASS_KWS:
        return []
    counter: dict[tuple, dict] = collections.defaultdict(lambda: {"papers": 0, "citations": 0})
    for work in works:
        year = work.get("publication_year")
        if year is None:
            continue
        scs = _classify_subclasses(work.get("title") or "")
        if not scs:
            continue
        cites = work.get("cited_by_count", 0)
        for sc in scs:
            counter[(year, sc)]["papers"] += 1
            counter[(year, sc)]["citations"] += cites
    return sorted(
        [{"year": k[0], "subclass": k[1], **v} for k, v in counter.items()],
        key=lambda x: (x["year"], x["subclass"]),
    )


def papers_by_author_subclass(works: list[dict], rows: list[dict]) -> list[dict]:
    """For each (author, subclass): how many papers in that subclass they have.
    Used to assign each author a dominant subclass for network visualisation."""
    if not SUBCLASS_KWS:
        return []
    work_scs: dict[str, list] = {
        w.get("id", ""): _classify_subclasses(w.get("title") or "")
        for w in works
    }

    seen: dict[tuple, dict] = {}
    for r in rows:
        author_key = r["author_id"] or r["author_name"]
        if not author_key:
            continue
        scs = work_scs.get(r["work_id"], [])
        if not scs:
            continue
        key = (r["work_id"], author_key)
        if key not in seen:
            seen[key] = {
                "author_name": r["author_name"],
                "author_id":   r["author_id"],
                "institution": r["institution"],
                "subclasses":  scs,
                "citations":   r["citations"],
            }

    counter: dict[tuple, dict] = collections.defaultdict(
        lambda: {"author_name": "", "author_id": "", "institution": "", "papers": 0, "citations": 0}
    )
    inst_votes: dict[tuple, list] = collections.defaultdict(list)
    for (_, author_key), meta in seen.items():
        for sc in meta["subclasses"]:
            key = (author_key, sc)
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
        [{"author_id": k[0], "subclass": k[1], **v} for k, v in counter.items()],
        key=lambda x: (x["author_id"] or "", -x["papers"]),
    )


def funders_by_author(works: list[dict], rows: list[dict]) -> list[dict]:
    """Map author_id → pipe-separated funder display names across all their papers."""
    work_funders: dict[str, set] = {}
    for work in works:
        wid = work.get("id", "")
        if not wid:
            continue
        fnames = {f.get("display_name", "") for f in (work.get("funders") or [])
                  if f.get("display_name")}
        if fnames:
            work_funders[wid] = fnames

    author_funders: dict[str, set] = collections.defaultdict(set)
    author_names: dict[str, str] = {}
    for r in rows:
        aid = r["author_id"]
        if not aid:
            continue
        wid = r["work_id"]
        if wid in work_funders:
            author_funders[aid].update(work_funders[wid])
        if aid not in author_names:
            author_names[aid] = r["author_name"]

    return sorted(
        [{"author_id": aid, "author_name": author_names.get(aid, ""),
          "funders": "|".join(sorted(fnames))}
         for aid, fnames in author_funders.items() if fnames],
        key=lambda x: x["author_id"],
    )


def papers_detail(works: list[dict], rows: list[dict]) -> list[dict]:
    """One row per paper with key metadata.

    Columns: work_id, title, doi, year, journal, citations, study_type,
             country, author_count, open_access.
    country = first author's country (from rows); author_count = distinct authors on paper.
    """
    # Build per-work first-author country and author count from rows
    work_first_country: dict[str, str] = {}
    work_author_ids: dict[str, set] = collections.defaultdict(set)
    seen_first: set[str] = set()
    for r in rows:
        wid = r["work_id"]
        aid = r.get("author_id") or r.get("author_name")
        if aid:
            work_author_ids[wid].add(aid)
        pos = r.get("author_position", "") or ""
        if pos == "first" and wid not in seen_first:
            seen_first.add(wid)
            work_first_country[wid] = r.get("country", "")

    result = []
    for work in works:
        wid   = work.get("id", "")
        title = (work.get("title") or "").strip()
        doi   = work.get("doi") or ""
        year  = work.get("publication_year")
        citations = work.get("cited_by_count", 0)
        study_type = classify_study_type(work)

        loc = work.get("primary_location") or {}
        src = loc.get("source") or {}
        journal = (src.get("display_name") or "").strip()

        oa_field = work.get("open_access") or {}
        open_access = bool(oa_field.get("is_oa", False))

        country = work_first_country.get(wid, "")
        author_count = len(work_author_ids.get(wid, set()))

        result.append({
            "work_id":      wid,
            "title":        title,
            "doi":          doi,
            "year":         year,
            "journal":      journal,
            "citations":    citations,
            "study_type":   study_type,
            "country":      country,
            "author_count": author_count,
            "open_access":  open_access,
        })

    return sorted(result, key=lambda x: x.get("citations", 0), reverse=True)


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


# ── Citation network edges ────────────────────────────────────────────────────

def citation_edges(works: list[dict], rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Build citation edges restricted to works within this corpus.
    Uses pandas merges instead of Python nested loops for performance.

    Returns
    -------
    work_edges           : list of {citing_work_id, cited_work_id}
    author_edges         : list of {citing_author_id, citing_author_name,
                                    cited_author_id,  cited_author_name, citations}
    author_edges_by_year : list of {citing_author_id, citing_author_name,
                                    cited_author_id,  cited_author_name, year, citations}
    author_edges_papers  : list of {citing_author_id, cited_author_id, citing_titles}
    """
    import pandas as _pd

    corpus_ids: set[str] = {w["id"] for w in works if w.get("id")}

    # ── Work metadata ─────────────────────────────────────────────────────────
    work_year  = {w["id"]: w["publication_year"] for w in works
                  if w.get("id") and w.get("publication_year") is not None}
    work_title = {w["id"]: (w.get("title") or "").strip()[:150] for w in works
                  if w.get("id") and w.get("title")}

    # ── Work-author table (deduplicated) ──────────────────────────────────────
    seen_wa: set[tuple] = set()
    wa_rows = []
    for r in rows:
        wid   = r["work_id"]
        aid   = r.get("author_id") or ""
        aname = r.get("author_name") or ""
        if not aid:
            continue
        key = (wid, aid)
        if key not in seen_wa:
            seen_wa.add(key)
            wa_rows.append((wid, aid, aname))
    del seen_wa

    wa = _pd.DataFrame(wa_rows, columns=["work_id", "author_id", "author_name"])
    del wa_rows

    # Cap large-consortium papers to first + last author
    _MAX_AUTHORS = 20
    sz = wa.groupby("work_id")["author_id"].transform("size")
    large = sz > _MAX_AUTHORS
    if large.any():
        rank     = wa.groupby("work_id").cumcount()
        last_pos = sz - 1
        wa = wa[~large | (rank == 0) | (rank == last_pos)].copy()

    # ── Work citation edges ───────────────────────────────────────────────────
    cite_rows = []
    for work in works:
        cid = work.get("id", "")
        if not cid:
            continue
        for ref in (work.get("referenced_works") or []):
            if ref in corpus_ids and ref != cid:
                cite_rows.append((cid, ref))

    if not cite_rows:
        return [], [], [], []

    we = _pd.DataFrame(cite_rows, columns=["citing_work_id", "cited_work_id"]).drop_duplicates()
    del cite_rows
    we["year"]  = we["citing_work_id"].map(work_year)
    we["title"] = we["citing_work_id"].map(work_title).fillna("")
    log.info("  Work-level citation edges: %d — expanding to author edges…", len(we))

    # Save work edges before we enrich/delete the DataFrame
    work_edges = (we[["citing_work_id", "cited_work_id"]]
                  .sort_values(["citing_work_id", "cited_work_id"])
                  .to_dict(orient="records"))

    # ── Expand to author pairs via merge ──────────────────────────────────────
    ca = wa.rename(columns={"work_id": "citing_work_id",
                             "author_id": "citing_author_id",
                             "author_name": "citing_author_name"})
    cd = wa.rename(columns={"work_id": "cited_work_id",
                             "author_id": "cited_author_id",
                             "author_name": "cited_author_name"})
    del wa

    ae = (we.merge(ca, on="citing_work_id", how="inner")
            .merge(cd, on="cited_work_id",  how="inner"))
    ae = ae[ae["citing_author_id"] != ae["cited_author_id"]]
    del we, ca, cd
    log.info("  Author-level pairs: %d — aggregating…", len(ae))

    # First-occurrence names per pair
    names = (ae.drop_duplicates(subset=["citing_author_id", "cited_author_id"])
               [["citing_author_id", "cited_author_id",
                 "citing_author_name", "cited_author_name"]])

    # ── Author edges (total citation count) ──────────────────────────────────
    cnt = (ae.groupby(["citing_author_id", "cited_author_id"], sort=False)
             .size().reset_index(name="citations"))
    cnt = cnt.merge(names, on=["citing_author_id", "cited_author_id"], how="left")
    cnt.sort_values("citations", ascending=False, inplace=True)
    author_edges = cnt.to_dict(orient="records")
    del cnt

    # ── Author edges by year ──────────────────────────────────────────────────
    ae_yr = ae.loc[ae["year"].notna(), ["citing_author_id", "cited_author_id", "year"]].copy()
    ae_yr["year"] = ae_yr["year"].astype(int)
    yr = (ae_yr.groupby(["citing_author_id", "cited_author_id", "year"], sort=False)
               .size().reset_index(name="citations"))
    yr = yr.merge(names, on=["citing_author_id", "cited_author_id"], how="left")
    yr.sort_values(["year", "citations"], ascending=[True, False], inplace=True)
    author_edges_by_year = yr.to_dict(orient="records")
    del ae_yr, yr

    # ── Author edges with paper titles ───────────────────────────────────────
    ae_t = ae.loc[ae["title"] != "", ["citing_author_id", "cited_author_id", "title"]]
    papers = (ae_t.groupby(["citing_author_id", "cited_author_id"], sort=False)["title"]
                  .apply(lambda x: "; ".join(sorted(set(x))[:5]))
                  .reset_index(name="citing_titles"))
    author_edges_papers = papers.to_dict(orient="records")
    del ae, ae_t, papers, names

    return work_edges, author_edges, author_edges_by_year, author_edges_papers


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
    print("  LIMITATIONS")
    print("  * Citation edges are restricted to works within this corpus.")
    print("  * For papers with >20 authors, citation edges are attributed to")
    print("    first and last author only (large consortium studies).")
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
    _pfx = _CFG.get("prefix", "")
    parser.add_argument(
        "--output-dir",
        default=os.path.join("data", _pfx) if _pfx else "data",
        metavar="DIR",
        help="Directory for CSV output files. Default: %(default)s",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch only the first 2 pages (for testing).",
    )
    parser.add_argument(
        "--from-checkpoint",
        action="store_true",
        help="Skip fetch; process works already saved in the checkpoint JSONL file.",
    )
    return parser.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    session = _session()
    prefix  = _CFG.get("prefix", "")

    data_dir = os.path.join(DEFAULT_DATA_DIR, prefix) if prefix else DEFAULT_DATA_DIR

    def _out(name: str) -> str:
        return os.path.join(args.output_dir, name)

    def _data(name: str) -> str:
        return os.path.join(data_dir, name)

    log.info("Search terms: %s", args.terms)
    log.info("Output dir  : %s", args.output_dir)
    log.info("Data dir    : %s", data_dir)
    log.info("Prefix      : %s", prefix or "(none)")
    if args.dry_run:
        log.info("DRY RUN — only first 2 pages will be fetched")

    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    if args.from_checkpoint:
        works_file = _data("fetch_checkpoint") + ".jsonl"
        if not os.path.exists(works_file):
            log.error("No checkpoint file found at %s", works_file)
            sys.exit(1)
        log.info("Loading works from checkpoint: %s", works_file)
        with open(works_file, encoding="utf-8") as fh:
            works = [json.loads(ln) for ln in fh if ln.strip()]
        log.info("Loaded %d works from checkpoint (fetch still in progress — checkpoint preserved)", len(works))
    else:
        checkpoint_path = None if args.dry_run else _data("fetch_checkpoint")
        works = fetch_all_works(args.terms, session, dry_run=args.dry_run,
                                checkpoint_path=checkpoint_path)

    if not works:
        log.warning("No works retrieved — check search terms or API availability.")
        sys.exit(0)

    log.info("Flattening %d works into author-institution rows…", len(works))
    rows = flatten_works(works)
    log.info("Flattened into %d rows", len(rows))

    log.info("Aggregating by country…")
    country_tbl = papers_by_country(rows)
    log.info("Aggregating by institution…")
    inst_tbl    = papers_by_institution(rows)
    log.info("Aggregating by author…")
    author_tbl  = papers_by_author(rows)
    log.info("Author table: %d unique authors", len(author_tbl))

    write_csv(_out("papers_by_country.csv"),
              ["country", "papers", "citations"], country_tbl)
    write_csv(_out("papers_by_institution.csv"),
              ["institution", "country", "papers", "citations"], inst_tbl)
    write_csv(_out("papers_by_author.csv"),
              ["author_id", "author_name", "institution", "country",
               "papers", "citations",
               "first_author_papers", "last_author_papers", "middle_author_papers",
               "middle_author_rate"],
              author_tbl)

    log.info("Aggregating by department…")
    dept_tbl = papers_by_department(rows)
    write_csv(_out("papers_by_department.csv"),
              ["department", "institution", "country", "papers", "citations"], dept_tbl)

    log.info("Aggregating by funder…")
    funder_tbl = papers_by_funder(works)
    write_csv(_out("papers_by_funder.csv"),
              ["funder_id", "funder_name", "papers", "citations"], funder_tbl)
    funding_country_tbl = funding_by_country(works, rows)
    write_csv(_out("funding_by_country.csv"),
              ["country", "papers", "funded_papers", "pct_funded", "citations"],
              funding_country_tbl)
    funders_auth_tbl = funders_by_author(works, rows)
    write_csv(_out("funders_by_author.csv"),
              ["author_id", "author_name", "funders"], funders_auth_tbl)

    log.info("Classifying study types…")
    study_type_tbl = papers_by_study_type(works)
    write_csv(_out("papers_by_study_type.csv"),
              ["study_type", "papers", "pct", "citations"], study_type_tbl)
    study_type_year_tbl = papers_by_study_type_year(works)
    write_csv(_out("papers_by_study_type_year.csv"),
              ["year", "study_type", "papers", "citations"], study_type_year_tbl)
    log.info("Aggregating author × study type…")
    author_st_tbl = papers_by_author_study_type(works, rows)
    write_csv(_out("papers_by_author_study_type.csv"),
              ["author_id", "author_name", "institution", "study_type", "papers", "citations"],
              author_st_tbl)

    log.info("Building papers detail table…")
    detail_tbl = papers_detail(works, rows)
    write_csv(_out("papers_detail.csv"),
              ["work_id", "title", "doi", "year", "journal", "citations",
               "study_type", "country", "author_count", "open_access"],
              detail_tbl)

    if SUBCLASS_KWS:
        subclass_tbl = papers_by_subclass(works)
        write_csv(_out("papers_by_subclass.csv"),
                  ["subclass", "papers", "citations"], subclass_tbl)
        subclass_year_tbl = papers_by_subclass_year(works)
        write_csv(_out("papers_by_subclass_year.csv"),
                  ["year", "subclass", "papers", "citations"], subclass_year_tbl)
        author_sc_tbl = papers_by_author_subclass(works, rows)
        write_csv(_out("papers_by_author_subclass.csv"),
                  ["author_id", "author_name", "institution", "subclass", "papers", "citations"],
                  author_sc_tbl)
        log.info("Subclass tables: %d subclasses, %d author-subclass rows",
                 len(subclass_tbl), len(author_sc_tbl))

    year_tbl = papers_by_year(works)
    write_csv(_out("papers_by_year.csv"), ["year", "papers", "citations"], year_tbl)
    country_year_tbl = papers_by_country_year(rows)
    write_csv(_out("papers_by_country_year.csv"),
              ["country", "year", "papers", "citations"], country_year_tbl)
    net_metrics_tbl = network_metrics_by_year(rows)
    write_csv(_out("network_metrics_by_year.csv"),
              ["year", "papers_this_year", "cumulative_papers", "cumulative_authors",
               "cumulative_edges"],
              net_metrics_tbl)

    log.info("Building coauthorship edges…")
    edges_by_year = coauthorship_edges_by_year(rows)
    write_csv(_out("coauthorship_edges_by_year.csv"),
              ["author1_id", "author1_name", "author2_id", "author2_name", "year", "papers"],
              edges_by_year)

    _edge_cols = ["author1_id", "author1_name", "author1_institution", "author1_country",
                  "author2_id", "author2_name", "author2_institution", "author2_country",
                  "shared_papers"]
    edges = coauthorship_edges(rows)
    write_csv(_out("coauthorship_edges.csv"), _edge_cols, edges)

    primary_edges = primary_coauthorship_edges(rows)
    write_csv(_out("coauthorship_edges_primary.csv"), _edge_cols, primary_edges)
    primary_edges_yr = primary_coauthorship_edges_by_year(rows)
    write_csv(_out("coauthorship_edges_by_year_primary.csv"),
              ["author1_id", "author1_name", "author2_id", "author2_name", "year", "papers"],
              primary_edges_yr)

    log.info("Building citation edges…")
    work_cite_edges, author_cite_edges, author_cite_edges_yr, author_cite_edges_papers = citation_edges(works, rows)
    write_csv(_out("citation_edges_work.csv"),
              ["citing_work_id", "cited_work_id"], work_cite_edges)
    write_csv(_out("citation_edges_author.csv"),
              ["citing_author_id", "citing_author_name",
               "cited_author_id", "cited_author_name", "citations"],
              author_cite_edges)
    write_csv(_out("citation_edges_author_by_year.csv"),
              ["citing_author_id", "citing_author_name",
               "cited_author_id", "cited_author_name", "year", "citations"],
              author_cite_edges_yr)
    write_csv(_out("citation_edges_author_papers.csv"),
              ["citing_author_id", "cited_author_id", "citing_titles"],
              author_cite_edges_papers)
    log.info("Citation edges: %d work-level, %d author-level, %d author-by-year, %d with paper titles",
             len(work_cite_edges), len(author_cite_edges), len(author_cite_edges_yr),
             len(author_cite_edges_papers))

    journal_tbl = papers_by_journal(works)
    write_csv(_out("papers_by_journal.csv"),
              ["journal", "journal_id", "papers", "citations"], journal_tbl)
    journal_year_tbl = papers_by_journal_year(works)
    write_csv(_out("papers_by_journal_year.csv"),
              ["journal", "year", "papers", "citations"], journal_year_tbl)
    journal_st_tbl = papers_by_journal_study_type(works)
    write_csv(_out("papers_by_journal_study_type.csv"),
              ["journal", "study_type", "papers", "citations"], journal_st_tbl)

    print_summary(works, rows, country_tbl, inst_tbl, study_type_tbl)


if __name__ == "__main__":
    main()

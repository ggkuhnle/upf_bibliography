#!/usr/bin/env python3

"""
upf_bibliometrics.py
Retrieve ultra-processed food literature from OpenAlex and produce ranked
CSV tables + a printed summary report.

Usage
-----
    python upf_bibliometrics.py                       # defaults
    python upf_bibliometrics.py --output-dir results  # custom output dir
    python upf_bibliometrics.py --terms "ultra-processed" "NOVA"
    python upf_bibliometrics.py --dry-run             # first 2 pages only
"""

import argparse
import collections
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_TERMS = [
    "ultra-processed food",
    "ultra-processed foods",
    "ultraprocessed food",
    "ultraprocessed foods",
    "ultra-processed diet",
    "ultraprocessed diet",
    "NOVA food classification",
]
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
                "cited_by_count,authorships,funders,awards"
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
    """
    rows = []
    for work in works:
        work_id = work.get("id", "")
        title = (work.get("title") or "").strip()
        year = work.get("publication_year")
        doi = work.get("doi") or ""
        citations = work.get("cited_by_count", 0)

        authorships = work.get("authorships") or []
        if not authorships:
            # Keep the work with empty author fields so it still counts.
            rows.append({
                "work_id": work_id,
                "title": title,
                "year": year,
                "doi": doi,
                "citations": citations,
                "author_name": "",
                "author_id": "",
                "institution": "",
                "country": "",
            })
            continue

        for authorship in authorships:
            author = authorship.get("author") or {}
            author_name = author.get("display_name", "")
            author_id = author.get("id", "")
            department = extract_department(
                authorship.get("raw_affiliation_strings") or []
            )

            institutions = authorship.get("institutions") or []
            if not institutions:
                rows.append({
                    "work_id": work_id,
                    "title": title,
                    "year": year,
                    "doi": doi,
                    "citations": citations,
                    "author_name": author_name,
                    "author_id": author_id,
                    "institution": "",
                    "department": department,
                    "country": "",
                })
            else:
                for inst in institutions:
                    rows.append({
                        "work_id": work_id,
                        "title": title,
                        "year": year,
                        "doi": doi,
                        "citations": citations,
                        "author_name": author_name,
                        "author_id": author_id,
                        "institution": inst.get("display_name", ""),
                        "department": department,
                        "country": inst.get("country_code", ""),
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
                "institution": r["institution"],
                "country": r["country"],
                "citations": r["citations"],
            }

    counter: dict[str, dict] = collections.defaultdict(
        lambda: {"author_name": "", "institution": "", "country": "", "papers": 0, "citations": 0}
    )
    for (_, author_id), meta in seen.items():
        key = author_id or meta["author_name"]
        counter[key]["author_name"] = meta["author_name"]
        counter[key]["institution"] = counter[key]["institution"] or meta["institution"]
        counter[key]["country"] = counter[key]["country"] or meta["country"]
        counter[key]["papers"] += 1
        counter[key]["citations"] += meta["citations"]

    return sorted(
        [{"author_id": aid, **v} for aid, v in counter.items()],
        key=lambda x: x["papers"],
        reverse=True,
    )


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


# ── Co-authorship edge list ────────────────────────────────────────────────────

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

def print_summary(works: list[dict], rows: list[dict], country_tbl: list[dict], inst_tbl: list[dict]) -> None:
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
        ["author_id", "author_name", "institution", "country", "papers", "citations"],
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

    print_summary(works, rows, country_tbl, inst_tbl)


if __name__ == "__main__":
    main()

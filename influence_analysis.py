#!/usr/bin/env python3
"""
influence_analysis.py
Extends the UPF bibliometric analysis with two lenses on influence beyond raw
publication counts:

  Part 1 — Reference network (no API key needed)
    Fetches the reference lists of every UPF paper from OpenAlex and asks:
    which works — and which authors — are cited most often BY the UPF
    literature?  This surfaces intellectual influencers (book authors, policy
    writers, foundational scientists) who may publish little or nothing in the
    UPF literature itself.

    Outputs:
      output/most_cited_works.csv    top works cited inside UPF papers
      output/most_cited_authors.csv  their authors, with a flag for whether
                                     they also appear as UPF authors

  Part 2 — Altmetric attention scores (requires a free API key)
    Fetches attention scores (news, policy documents, social media) for each
    UPF paper.  Register for a free key at:
      https://www.altmetric.com/solutions/altmetric-api/
    then pass it with --altmetric-key YOUR_KEY.

    Outputs:
      output/altmetric_scores.csv    per-paper attention breakdown

Usage:
    python influence_analysis.py                          # Part 1 only
    python influence_analysis.py --altmetric-key KEY      # both parts
    python influence_analysis.py --skip-refs              # Part 2 only
    python influence_analysis.py --top-refs 200           # top 200 cited works
    python influence_analysis.py --dry-run                # first 2 pages only
"""

import argparse
import collections
import csv
import logging
import os
import time
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────

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
MAILTO            = "g.kuhnle@reading.ac.uk"
BASE_URL          = "https://api.openalex.org/works"
ALTMETRIC_BASE    = "https://api.altmetric.com/v1/doi/{doi}"
PAGE_SIZE         = 200
REQUEST_DELAY     = 1.0
MAX_RETRIES       = 5
RETRY_BACKOFF     = 2.0
TOP_REFS_DEFAULT  = 300   # how many most-cited referenced works to look up
ALTMETRIC_DELAY   = 1.2   # seconds between Altmetric requests (free-tier safe)
REF_BATCH_SIZE    = 50    # OpenAlex batch lookup size

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _session(extra_headers: dict | None = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = f"upf_influence/1.0 (mailto:{MAILTO})"
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def _get(session, url, params, retries=MAX_RETRIES):
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = RETRY_BACKOFF ** attempt
                log.warning("Rate-limited; waiting %.0fs (attempt %d/%d)", wait, attempt, retries)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF ** attempt
            log.warning("Request error: %s — retry %d/%d in %.0fs", exc, attempt, retries, wait)
            if attempt == retries:
                raise
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")


def write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info("Wrote %d rows → %s", len(rows), path)


def _short_id(openalex_url) -> str:
    """'https://openalex.org/W123' → 'W123'"""
    if not openalex_url:
        return ""
    return str(openalex_url).rsplit("/", 1)[-1]


# ── Part 1: Reference network ─────────────────────────────────────────────────

def fetch_papers_with_refs(terms: list[str], session, dry_run=False) -> list[dict]:
    """Fetch all UPF papers including their referenced_works lists."""
    quoted = [f'"{t}"' if " " in t else t for t in terms]
    filter_str = "title_and_abstract.search:" + "|".join(quoted)

    cursor, works, page_num = "*", [], 0
    while cursor:
        params = {
            "filter":   filter_str,
            "per-page": PAGE_SIZE,
            "cursor":   cursor,
            "mailto":   MAILTO,
            "select":   "id,doi,title,publication_year,cited_by_count,"
                        "authorships,referenced_works",
        }
        data    = _get(session, BASE_URL, params)
        meta    = data.get("meta", {})
        results = data.get("results", [])
        cursor  = meta.get("next_cursor")
        page_num += 1
        log.info("Page %3d  — %d works  (total: %s)", page_num, len(results),
                 meta.get("count", "?"))
        works.extend(results)
        if not results or not cursor:
            break
        if dry_run and page_num >= 2:
            log.info("Dry-run: stopping after 2 pages")
            break
        time.sleep(REQUEST_DELAY)

    log.info("Fetched %d works with reference lists", len(works))
    return works


def count_references(works: list[dict]) -> collections.Counter:
    """Count how many UPF papers cite each referenced work."""
    counter: collections.Counter = collections.Counter()
    for work in works:
        for ref_url in work.get("referenced_works") or []:
            short = _short_id(ref_url)
            if short:
                counter[short] += 1
    return counter


def batch_fetch_work_metadata(work_ids: list[str], session) -> dict[str, dict]:
    """
    Fetch titles, years, authors for a list of OpenAlex work IDs.
    Returns {work_id: metadata_dict}.
    """
    results = {}
    for start in range(0, len(work_ids), REF_BATCH_SIZE):
        batch = work_ids[start:start + REF_BATCH_SIZE]
        filter_str = "openalex:" + "|".join(batch)
        params = {
            "filter":   filter_str,
            "per-page": REF_BATCH_SIZE,
            "select":   "id,title,publication_year,doi,cited_by_count,authorships",
            "mailto":   MAILTO,
        }
        data = _get(session, BASE_URL, params)
        if not data:
            continue
        for item in data.get("results", []):
            wid = _short_id(item.get("id", ""))
            results[wid] = item
        log.info("  Batch metadata: %d/%d works fetched", len(results), len(work_ids))
        time.sleep(REQUEST_DELAY)
    return results


def build_reference_tables(
    works: list[dict],
    ref_counter: collections.Counter,
    ref_metadata: dict[str, dict],
    top_n: int,
) -> tuple[list[dict], list[dict]]:
    """
    Returns:
      cited_works_rows   — one row per referenced work
      cited_authors_rows — one row per author of those works, aggregated
    """
    # Build set of UPF author IDs for the "in_upf_literature" flag
    upf_author_ids: set[str] = set()
    for work in works:
        for auth in work.get("authorships") or []:
            aid = (auth.get("author") or {}).get("id", "")
            if aid:
                upf_author_ids.add(_short_id(aid))

    cited_works_rows   = []
    author_aggregator: dict[str, dict] = {}

    for wid, freq in ref_counter.most_common(top_n):
        meta = ref_metadata.get(wid, {})
        if not meta:
            continue

        # Authors string (abbreviated)
        auths = meta.get("authorships") or []
        author_names = [
            (a.get("author") or {}).get("display_name", "")
            for a in auths[:5]
        ]
        author_str = "; ".join(n for n in author_names if n)
        if len(auths) > 5:
            author_str += f" [+{len(auths) - 5} more]"

        doi = (meta.get("doi") or "").replace("https://doi.org/", "")
        cited_works_rows.append({
            "work_id":           wid,
            "times_cited_by_upf": freq,
            "title":             (meta.get("title") or "")[:200],
            "year":              meta.get("publication_year", ""),
            "doi":               doi,
            "total_citations":   meta.get("cited_by_count", ""),
            "authors":           author_str,
        })

        # Per-author aggregation
        for auth in auths:
            author = auth.get("author") or {}
            aid    = _short_id(author.get("id", ""))
            aname  = author.get("display_name", "")
            if not aid or not aname:
                continue

            insts  = auth.get("institutions") or []
            inst   = (insts[0].get("display_name", "") if insts else "")
            ctr    = (insts[0].get("country_code", "") if insts else "")

            if aid not in author_aggregator:
                author_aggregator[aid] = {
                    "author_id":         aid,
                    "author_name":       aname,
                    "institution":       inst,
                    "country":           ctr,
                    "works_cited":       0,
                    "times_cited_total": 0,
                    "in_upf_literature": aid in upf_author_ids,
                }
            author_aggregator[aid]["works_cited"]       += 1
            author_aggregator[aid]["times_cited_total"] += freq

    cited_authors_rows = sorted(
        author_aggregator.values(),
        key=lambda x: x["times_cited_total"],
        reverse=True,
    )
    return cited_works_rows, cited_authors_rows


# ── Part 2: Altmetric ─────────────────────────────────────────────────────────

def fetch_altmetric_scores(works: list[dict], api_key: str, out_path: str) -> list[dict]:
    """
    Fetch Altmetric attention scores for UPF papers that have DOIs.
    Writes incrementally to out_path (CSV) so a crash doesn't lose progress.
    """
    # Sort by citation count descending — most impactful papers first
    doi_works = sorted(
        [w for w in works if w.get("doi")],
        key=lambda w: w.get("cited_by_count", 0),
        reverse=True,
    )
    log.info("Fetching Altmetric scores for %d papers with DOIs …", len(doi_works))

    fieldnames = [
        "doi", "title", "year", "upf_citations",
        "altmetric_score", "altmetric_id",
        "news_mentions", "policy_mentions", "tweet_mentions",
        "blog_mentions", "reddit_mentions", "wikipedia_mentions",
        "readers_mendeley", "total_attention_count",
    ]

    session = _session()
    rows = []
    found = not_found = errors = 0

    # Open file for incremental writing
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for i, work in enumerate(doi_works, 1):
            raw_doi = work.get("doi", "")
            doi = raw_doi.replace("https://doi.org/", "").strip()
            if not doi:
                continue

            url = ALTMETRIC_BASE.format(doi=doi)
            try:
                resp = session.get(url, params={"key": api_key}, timeout=20)

                if resp.status_code == 404:
                    not_found += 1
                    time.sleep(ALTMETRIC_DELAY)
                    continue
                if resp.status_code == 403:
                    log.error(
                        "Altmetric returned 403 — check your API key.\n"
                        "Register free at: https://www.altmetric.com/solutions/altmetric-api/"
                    )
                    break
                resp.raise_for_status()
                d = resp.json()

                counts = d.get("counts", {})
                row = {
                    "doi":                doi,
                    "title":              (work.get("title") or "")[:200],
                    "year":               work.get("publication_year", ""),
                    "upf_citations":      work.get("cited_by_count", 0),
                    "altmetric_score":    round(d.get("score", 0), 1),
                    "altmetric_id":       d.get("altmetric_id", ""),
                    "news_mentions":      d.get("cited_by_msm_count", 0),
                    "policy_mentions":    d.get("cited_by_policies_count", 0),
                    "tweet_mentions":     d.get("cited_by_tweeters_count", 0),
                    "blog_mentions":      d.get("cited_by_feeds_count", 0),
                    "reddit_mentions":    d.get("cited_by_rdts_count", 0),
                    "wikipedia_mentions": d.get("cited_by_wikipedia_count", 0),
                    "readers_mendeley":   d.get("readers", {}).get("mendeley", 0),
                    "total_attention_count": d.get("cited_by_accounts_count", 0),
                }
                writer.writerow(row)
                fh.flush()
                rows.append(row)
                found += 1

            except requests.RequestException as exc:
                log.warning("Altmetric error for %s: %s", doi, exc)
                errors += 1

            if i % 50 == 0:
                log.info("  Altmetric: %d/%d  (found=%d  not_tracked=%d  errors=%d)",
                         i, len(doi_works), found, not_found, errors)
            time.sleep(ALTMETRIC_DELAY)

    log.info("Altmetric done: %d scored, %d not tracked, %d errors → %s",
             found, not_found, errors, out_path)
    return rows


# ── Summary report ─────────────────────────────────────────────────────────────

def print_ref_summary(cited_works: list[dict], cited_authors: list[dict]) -> None:
    sep = "─" * 64
    print()
    print(sep)
    print("  REFERENCE NETWORK — SUMMARY")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sep)

    print(f"\n  TOP 20 MOST-CITED WORKS IN THE UPF LITERATURE")
    print(f"  {'Times cited':>11}  {'Year':>4}  {'Authors / Title'}")
    print("  " + "-" * 60)
    for r in cited_works[:20]:
        authors_short = r["authors"].split(";")[0].strip()[:30]
        title_short   = r["title"][:45]
        print(f"  {r['times_cited_by_upf']:>11,}  {r['year']:>4}  "
              f"{authors_short} — {title_short}")

    print(f"\n  TOP 20 MOST-CITED AUTHORS (by work × frequency)")
    print(f"  {'Times cited':>11}  {'Works':>5}  {'In UPF?':>7}  {'Name'}")
    print("  " + "-" * 60)
    for r in cited_authors[:20]:
        flag = "yes" if r["in_upf_literature"] else "NO ← external influence"
        print(f"  {r['times_cited_total']:>11,}  {r['works_cited']:>5}  "
              f"  {flag:<22}  {r['author_name']}")
    print(sep)
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="UPF influence analysis: reference network + Altmetric scores."
    )
    p.add_argument("--terms", nargs="+", default=DEFAULT_TERMS, metavar="TERM")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, metavar="DIR")
    p.add_argument("--top-refs",   default=TOP_REFS_DEFAULT, type=int,
                   help="How many most-cited referenced works to look up (default: %(default)s)")
    p.add_argument("--altmetric-key", default="", metavar="KEY",
                   help="Altmetric API key (free registration at altmetric.com). "
                        "Omit to skip Part 2.")
    p.add_argument("--skip-refs",     action="store_true",
                   help="Skip Part 1 (reference network)")
    p.add_argument("--dry-run",       action="store_true",
                   help="Fetch only first 2 pages (for testing)")
    return p.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    args  = parse_args()
    out   = args.output_dir
    sess  = _session()

    if args.skip_refs and not args.altmetric_key:
        print("Nothing to do: --skip-refs with no --altmetric-key supplied.")
        return

    # ── Phase 0: fetch papers (needed by both parts) ──────────────────────────
    log.info("Fetching UPF papers with reference lists …")
    works = fetch_papers_with_refs(args.terms, sess, dry_run=args.dry_run)

    # ── Phase 1: reference network ─────────────────────────────────────────────
    if not args.skip_refs:
        log.info("Counting reference frequencies …")
        ref_counter = count_references(works)
        log.info("Found %d unique referenced works across the corpus", len(ref_counter))

        top_ids = [wid for wid, _ in ref_counter.most_common(args.top_refs)]
        log.info("Looking up metadata for top %d cited works …", len(top_ids))
        ref_metadata = batch_fetch_work_metadata(top_ids, sess)
        log.info("Retrieved metadata for %d/%d works", len(ref_metadata), len(top_ids))

        cited_works, cited_authors = build_reference_tables(
            works, ref_counter, ref_metadata, args.top_refs
        )

        write_csv(
            os.path.join(out, "most_cited_works.csv"),
            ["work_id", "times_cited_by_upf", "title", "year", "doi",
             "total_citations", "authors"],
            cited_works,
        )
        write_csv(
            os.path.join(out, "most_cited_authors.csv"),
            ["author_id", "author_name", "institution", "country",
             "works_cited", "times_cited_total", "in_upf_literature"],
            cited_authors,
        )
        print_ref_summary(cited_works, cited_authors)

    # ── Phase 2: Altmetric ─────────────────────────────────────────────────────
    if args.altmetric_key:
        fetch_altmetric_scores(
            works,
            api_key=args.altmetric_key,
            out_path=os.path.join(out, "altmetric_scores.csv"),
        )
    else:
        log.info(
            "Altmetric scores skipped (no --altmetric-key provided).\n"
            "  Register free at: https://www.altmetric.com/solutions/altmetric-api/\n"
            "  Then re-run with: python influence_analysis.py --skip-refs --altmetric-key YOUR_KEY"
        )


if __name__ == "__main__":
    main()

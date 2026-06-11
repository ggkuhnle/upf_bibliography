"""Shared OpenAlex HTTP helpers used by bibliometrics.py and
make_influence_report.py, so politeness settings live in one place."""

import logging
import time

import requests

MAILTO    = "ggkuhnle@googlemail.com"
API_BASE  = "https://api.openalex.org"
WORKS_URL = f"{API_BASE}/works"
PAGE_SIZE = 200          # OpenAlex max per-cursor page

_log = logging.getLogger("openalex")


def polite_get(url, params, *, session=None, mailto=MAILTO, retries=5,
               timeout=20, rate_wait=30, err_backoff=2.0, on_quota=None):
    """GET with retries against the OpenAlex API.

    429s honour the Retry-After header (capped at 120 s) and otherwise wait
    rate_wait × attempt (capped at 600 s). Other request errors back off
    exponentially (err_backoff ** attempt). Raises after `retries` failures
    instead of returning empty data.

    on_quota(retry_after_seconds) is called when Retry-After exceeds 600 s,
    which signals the daily quota reset — callers can checkpoint and exit.
    """
    getter = session.get if session is not None else requests.get
    params = {**params, "mailto": mailto}
    for attempt in range(1, retries + 1):
        try:
            resp = getter(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                ra_raw = resp.headers.get("Retry-After", "")
                retry_after = int(ra_raw) if ra_raw.isdigit() else 0
                if retry_after > 600 and on_quota is not None:
                    on_quota(retry_after)
                if attempt == retries:
                    resp.raise_for_status()
                wait = (min(retry_after, 120) if retry_after
                        else min(rate_wait * attempt, 600))
                _log.warning("Rate-limited (429); waiting %.0fs before retry %d/%d",
                             wait, attempt, retries)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            wait = err_backoff ** attempt
            _log.warning("Request error: %s — retry %d/%d in %.0fs",
                         exc, attempt, retries, wait)
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")  # unreachable, but satisfies type checkers

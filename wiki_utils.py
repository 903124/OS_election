"""
wiki_utils.py — MediaWiki Action API client (rate-limited) + shared wikitext helpers.

Replaces the previous local-dump pipeline (22 GB bz2 multistream + SQLite index)
with polite, rate-limited API access. Compliance measures implemented here:

* Batched queries  — up to 50 titles per request (API max for non-bot clients),
  so a whole election cycle is only a handful of HTTP requests.
* Serial requests  — a configurable minimum interval (default 1 s) is enforced
  between consecutive requests; no parallelism.
* maxlag=5         — standard Wikimedia politeness parameter; the client backs
  off whenever the cluster reports lag or returns 503 with Retry-After.
* 429 / Retry-After — honoured verbatim, plus exponential backoff with jitter
  on transient failures (network errors, 5xx, maxlag, ratelimited).
* User-Agent policy — a descriptive, contact-bearing User-Agent is required by
  Wikimedia; override via the WIKI_USER_AGENT env var or --user-agent.

Environment variables:
    WIKI_API_URL        API endpoint        (default: en.wikipedia.org/w/api.php)
    WIKI_USER_AGENT     User-Agent header   (PLEASE set a real contact!)
    WIKI_REQUEST_DELAY  seconds between requests           (default: 1.0)
    WIKI_BATCH_SIZE     titles per request, <= 50          (default: 50)
    WIKI_MAX_RETRIES    retry attempts for transient errors (default: 5)
    WIKI_TIMEOUT        per-request timeout, seconds       (default: 60)
"""

from __future__ import annotations

import html
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# CONFIGURATION (env-overridable defaults)
# ──────────────────────────────────────────────

DEFAULT_API_URL = os.getenv("WIKI_API_URL", "https://en.wikipedia.org/w/api.php")
DEFAULT_USER_AGENT = os.getenv(
    "WIKI_USER_AGENT",
    "wiki-elections-pipeline/2.0 (https://github.com/your-org/your-repo; "
    "contact: you@example.com)",
)
DEFAULT_DELAY = float(os.getenv("WIKI_REQUEST_DELAY", "1.0"))
DEFAULT_BATCH_SIZE = int(os.getenv("WIKI_BATCH_SIZE", "50"))
DEFAULT_MAX_RETRIES = int(os.getenv("WIKI_MAX_RETRIES", "5"))
DEFAULT_TIMEOUT = int(os.getenv("WIKI_TIMEOUT", "60"))

#: API error codes that are transient and worth retrying.
RETRYABLE_API_ERRORS = {"maxlag", "readonly", "ratelimited", "internal_api_error"}

#: Hard API limit on titles per query for non-bot clients.
API_MAX_TITLES_PER_QUERY = 50


def _as_float(value: Optional[str], default: float) -> float:
    """Parse *value* as float, falling back to *default* when missing/invalid."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class RateLimiter:
    """Serialise calls so that at least *min_interval* seconds elapse between them."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._next_ok = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_ok - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_ok = now + self.min_interval


class WikiAPIClient:
    """
    Small, polite client for the MediaWiki Action API.

    Example:
        client = WikiAPIClient(user_agent="my-bot/1.0 (me@example.com)")
        texts = client.fetch_wikitext(["2018 United States Senate election in Arizona"])
    """

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        delay: float = DEFAULT_DELAY,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        if batch_size > API_MAX_TITLES_PER_QUERY:
            raise ValueError(
                f"batch_size must be <= {API_MAX_TITLES_PER_QUERY} "
                "(Action API limit for non-bot clients)"
            )
        self.api_url = api_url
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.timeout = timeout
        self._limiter = RateLimiter(delay)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Api-User-Agent": user_agent,
                "Accept": "application/json",
            }
        )

    # ──────────────────────────────────────────────
    # LOW-LEVEL REQUEST (rate limited + retries)
    # ──────────────────────────────────────────────

    def _get(self, params: Dict) -> Dict:
        """Perform a GET against the API with backoff on transient failures."""
        backoff = 2.0
        for attempt in range(1, self.max_retries + 1):
            self._limiter.wait()
            try:
                resp = self._session.get(self.api_url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                retry_after = backoff
                logger.warning(
                    "Network error: %s — retrying in %.1fs (attempt %d/%d)",
                    exc, retry_after, attempt, self.max_retries,
                )
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    err = data.get("error") or {}
                    code = err.get("code", "")
                    if code in RETRYABLE_API_ERRORS:
                        retry_after = _as_float(resp.headers.get("Retry-After"), backoff)
                        logger.warning(
                            "API busy (%s) — retrying in %.1fs (attempt %d/%d)",
                            code, retry_after, attempt, self.max_retries,
                        )
                    elif err:
                        raise RuntimeError(
                            f"API error {err.get('code')}: {err.get('info')}"
                        )
                    else:
                        return data
                elif resp.status_code == 429 or 500 <= resp.status_code < 600:
                    retry_after = _as_float(resp.headers.get("Retry-After"), backoff)
                    logger.warning(
                        "HTTP %d — retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, retry_after, attempt, self.max_retries,
                    )
                else:
                    resp.raise_for_status()
                    raise RuntimeError(f"Unexpected HTTP status {resp.status_code}")

            if attempt < self.max_retries:
                time.sleep(retry_after + random.uniform(0, retry_after * 0.25))
                backoff = min(backoff * 2, 60.0)

        raise RuntimeError(
            f"Giving up after {self.max_retries} attempts "
            f"(titles={str(params.get('titles'))[:80]}...)"
        )

    # ──────────────────────────────────────────────
    # PUBLIC FETCHERS
    # ──────────────────────────────────────────────

    def fetch_wikitext(
        self, titles: Iterable[str], sub_batch: Optional[int] = None
    ) -> Dict[str, Optional[str]]:
        """
        Fetch current wikitext for *titles*.

        Returns a dict mapping each *requested* title to its wikitext, or None
        when the page is missing.  Requested titles are transparently matched
        through API normalisation and redirects.  *sub_batch* overrides the
        batch size for this call (used internally when large batches hit the
        API result-size cap).
        """
        size = max(1, min(sub_batch or self.batch_size, self.batch_size))
        requested = list(dict.fromkeys(titles))  # dedupe, preserve order
        result: Dict[str, Optional[str]] = {t: None for t in requested}

        total_batches = (len(requested) + size - 1) // max(size, 1)
        for batch_idx, chunk in enumerate(_chunked(requested, size), start=1):
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "redirects": 1,
                "maxlag": 5,
                "titles": "|".join(chunk),
            }
            query = self._get(params).get("query", {})

            # requested → canonical chain (normalisation + redirects)
            chain: Dict[str, str] = {}
            for item in query.get("normalized", []):
                chain[item["from"]] = item["to"]
            for item in query.get("redirects", []):
                chain[item["from"]] = item["to"]

            def canonical(title: str) -> str:
                seen: set = set()
                while title in chain and title not in seen:
                    seen.add(title)
                    title = chain[title]
                return title

            by_canonical: Dict[str, List[str]] = defaultdict(list)
            for t in chunk:
                by_canonical[canonical(t)].append(t)

            pages = query.get("pages", [])
            seen_pages = set()
            for page in pages:
                text: Optional[str] = None
                if not page.get("missing") and page.get("revisions"):
                    slots = page["revisions"][0].get("slots", {})
                    text = slots.get("main", {}).get("content")
                seen_pages.add(page.get("title", ""))
                for req_title in by_canonical.get(page.get("title", ""), []):
                    result[req_title] = text
                if text is None:
                    logger.warning("No wikitext for '%s' (missing or empty)", page.get("title"))

            # When a batch of large articles exceeds the API's result-size cap
            # the response silently drops pages (they appear neither in
            # pages[] nor with missing=true).  Re-request any title that was
            # neither delivered nor explicitly reported missing, in small
            # sub-batches so each response fits.
            dropped = [
                t for t in chunk
                if result[t] is None and canonical(t) not in seen_pages
            ]
            if dropped and len(chunk) > 1:
                logger.info(
                    "Batch %d/%d: %d title(s) dropped (result-size cap) — refetching in small batches",
                    batch_idx, total_batches, len(dropped),
                )
                sub = self.fetch_wikitext(dropped, sub_batch=min(self.batch_size, 5))
                result.update(sub)

            fetched = sum(1 for t in chunk if result[t] is not None)
            logger.info(
                "Batch %d/%d: fetched %d/%d titles", batch_idx, total_batches,
                fetched, len(chunk),
            )

        return result

    def fetch_single(self, title: str) -> Optional[str]:
        """Fetch wikitext for a single title."""
        return self.fetch_wikitext([title]).get(title)


def _chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def even_years(start_year: int, end_year: int) -> List[int]:
    """
    Even (federal election) years within ``[start_year, end_year]``, inclusive.

    U.S. federal general elections fall on even years, so odd bounds are
    clamped inward: ``even_years(2019, 2023)`` → ``[2020, 2022]``.
    Returns an empty list when the clamped range is empty.
    """
    start = start_year if start_year % 2 == 0 else start_year + 1
    end = end_year if end_year % 2 == 0 else end_year - 1
    return list(range(start, end + 1, 2)) if start <= end else []


# ──────────────────────────────────────────────
# MODULE-LEVEL DEFAULT CLIENT
# ──────────────────────────────────────────────

_default_client: Optional[WikiAPIClient] = None


def set_default_client(client: WikiAPIClient) -> None:
    """Install *client* as the module-wide default (used by the CLI)."""
    global _default_client
    _default_client = client


def get_default_client() -> WikiAPIClient:
    """Return the default client, creating one from env-var config on first use."""
    global _default_client
    if _default_client is None:
        _default_client = WikiAPIClient()
    return _default_client


def fetch_articles_batch(titles: List[str]) -> Dict[str, Optional[str]]:
    """Convenience wrapper used by the parser modules (batches of <= 50 titles)."""
    return get_default_client().fetch_wikitext(titles)


# ──────────────────────────────────────────────
# WIKITEXT CLEANING HELPERS
# ──────────────────────────────────────────────

def remove_wikilinks(text: str) -> str:
    """``[[Target|Display]]`` → ``Display``;  ``[[Target]]`` → ``Target``."""
    return re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text or "")


def clean_wikitext(text: str) -> str:
    """Strip common wikitext markup, leaving plain text."""
    if not text:
        return ""
    text = html.unescape(text)                               # &nbsp; &amp; ...
    text = re.sub(r"\{\{nowrap\|([^}]+)\}\}", r"\1", text)   # {{nowrap|x}} → x
    text = re.sub(r"\{\{[^}]+\}\}", "", text)                # remove other templates
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", " ", text)                   # <br> → space
    text = text.replace("'''", "").replace("''", "")         # bold / italic
    text = remove_wikilinks(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_incumbent_flag(name: str) -> Tuple[str, bool]:
    """
    Strip a trailing '(incumbent)' / '- incumbent' marker from *name*.

    Returns ``(clean_name, is_incumbent)``.
    """
    patterns = [
        r"\s*\(incumbent\)\s*$",
        r"\s*[-–]\s*incumbent\s*$",
        r"\s+incumbent\s*$",
    ]
    for pat in patterns:
        if re.search(pat, name or "", re.IGNORECASE):
            return re.sub(pat, "", name, flags=re.IGNORECASE).strip(), True
    return name, False

"""Decodo SERP Scraping API client and parser.

Wraps the `/v2/scrape` endpoint with `target=google_search` and `parse=true`,
then walks the parsed JSON to fill out a `RankResult`.

Decodo response shape (parsed):
    results[0].content.results.results.{organic, ai_overviews, related_questions,
                                        featured_snippet, knowledge, local_pack, ...}
Each `organic` item carries `pos` (within page) and `pos_overall` (across SERP).
AI Overview citations live in `ai_overviews[*].source_panel.items[*]`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from canary import validate_response
from models import AIOverviewCitation, CompetitorEntry, RankCheckConfig, RankResult

logger = logging.getLogger(__name__)

DECODO_ENDPOINT = "https://scraper-api.decodo.com/v2/scrape"

# Retry on these status codes – transient infra issues, not auth/payload problems.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class DecodoCredentialError(RuntimeError):
    """Raised on 401/403 – auth token missing or invalid."""


class DecodoAPIError(RuntimeError):
    """Raised on non-retryable Decodo API errors."""


class DecodoFetcher:
    def __init__(
        self,
        auth: Optional[str] = None,
        timeout: int = 90,
        max_retries: int = 3,
        backoff_base: float = 1.5,
    ):
        auth = auth or os.environ.get("DECODO_AUTH")
        if not auth:
            raise DecodoCredentialError(
                "Decodo credentials missing. Set DECODO_AUTH (base64 user:pass) "
                "in the environment or pass auth= explicitly. "
                "Hint: copy .env.example to .env and fill in DECODO_AUTH."
            )
        self.auth = auth
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.session = requests.Session()
        # Counts every HTTP POST to Decodo, including retries. Useful for
        # cost visibility and capacity planning. Locked so multiple worker
        # threads can increment safely.
        self._call_count = 0
        self._call_count_lock = threading.Lock()

    @property
    def api_call_count(self) -> int:
        with self._call_count_lock:
            return self._call_count

    def reset_call_count(self) -> None:
        with self._call_count_lock:
            self._call_count = 0

    def _increment_call_count(self) -> None:
        with self._call_count_lock:
            self._call_count += 1

    def fetch(self, config: RankCheckConfig) -> RankResult:
        payload: Dict[str, Any] = {
            "target": "google_search",
            "query": config.keyword,
            "parse": True,
            "page_from": "1",
            "page_count": config.pages,
            "google_results_language": config.google_results_language,
            "geo": config.geo,
            "locale": config.locale,
            "device_type": config.device_type,
        }
        data = self._post(payload)
        issues = validate_response(data)
        if issues:
            # Soft warn – surface schema drift early without breaking the run.
            for issue in issues:
                logger.warning("Canary: %s (keyword=%r)", issue, config.keyword)
        return self._parse(data, config)

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {self.auth}",
            "Content-Type": "application/json",
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._increment_call_count()
            try:
                resp = self.session.post(
                    DECODO_ENDPOINT,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                logger.warning(
                    "Decodo request transport error (attempt %d/%d): %s",
                    attempt, self.max_retries, exc,
                )
                self._sleep_backoff(attempt)
                continue

            if resp.status_code in (401, 403):
                raise DecodoCredentialError(
                    f"Decodo rejected credentials (HTTP {resp.status_code}). "
                    "Verify DECODO_AUTH is the base64-encoded 'user:password' "
                    "from your Decodo dashboard."
                )
            if resp.status_code in RETRYABLE_STATUSES and attempt < self.max_retries:
                logger.warning(
                    "Decodo returned HTTP %d (attempt %d/%d), retrying...",
                    resp.status_code, attempt, self.max_retries,
                )
                self._sleep_backoff(attempt)
                continue
            if not resp.ok:
                # Surface the response body if available – Decodo includes
                # diagnostic hints (e.g. unsupported geo strings).
                body = resp.text[:500].replace("\n", " ")
                raise DecodoAPIError(
                    f"Decodo API error HTTP {resp.status_code}: {body}"
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise DecodoAPIError(
                    f"Decodo returned non-JSON body: {resp.text[:200]!r}"
                ) from exc

        raise DecodoAPIError(
            f"Decodo request failed after {self.max_retries} retries: {last_exc}"
        )

    def _sleep_backoff(self, attempt: int) -> None:
        # Exponential with a small jitter – keeps retries from synchronizing
        # across concurrent workers hitting the same upstream incident.
        delay = (self.backoff_base ** attempt) + (0.1 * attempt)
        time.sleep(delay)

    def _parse(self, data: Dict[str, Any], config: RankCheckConfig) -> RankResult:
        try:
            outer = data["results"][0]
            inner = outer["content"]["results"]
            serp = inner.get("results") or {}
            serp_url = inner.get("url")
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("Unexpected Decodo response shape: %s", exc)
            raise

        domain = config.target_domain.lower().lstrip(".")

        organic = serp.get("organic") or []
        organic_position, matched_url = self._find_in_organic(
            organic, domain, config.target_url
        )

        ai_overviews = serp.get("ai_overviews") or []
        ai_present = bool(ai_overviews)
        ai_cited, ai_rank, citations = self._scan_ai_overviews(ai_overviews, domain)

        fs_present, fs_owned = self._scan_featured_snippets(serp, domain)

        paa = serp.get("related_questions") or {}
        paa_items = paa.get("items", []) if isinstance(paa, dict) else (paa or [])
        paa_present = bool(paa_items)

        top_results = self._extract_top_competitors(organic, config.top_n)

        return RankResult(
            keyword=config.keyword,
            target_domain=config.target_domain,
            organic_position=organic_position,
            matched_url=matched_url,
            featured_snippet=fs_present,
            featured_snippet_owned=fs_owned,
            ai_overview_present=ai_present,
            ai_overview_cited=ai_cited,
            ai_overview_citation_rank=ai_rank,
            ai_overview_citations=citations,
            paa_present=paa_present,
            paa_question_count=len(paa_items),
            local_pack_present=bool(serp.get("local_pack")),
            knowledge_panel_present=bool(serp.get("knowledge")),
            location=config.geo,
            device=config.device_type,
            locale=config.locale,
            total_results=serp.get("total_results_count"),
            serp_url=serp_url,
            raw_organic_count=len(organic),
            top_results=top_results,
        )

    @staticmethod
    def _extract_top_competitors(
        organic: List[Dict[str, Any]], top_n: int
    ) -> List[CompetitorEntry]:
        """Capture the top N organic results as competitor records.

        Sorted by `pos_overall` (SERP-wide rank including AI/featured blocks)
        so the user can see exactly who's above and around them.
        """
        if top_n <= 0:
            return []
        entries: List[CompetitorEntry] = []
        for item in organic[:top_n]:
            url = item.get("url", "")
            if not url:
                continue
            try:
                host = urlparse(url).netloc.lower()
            except ValueError:
                host = ""
            if host.startswith("www."):
                host = host[4:]
            position = item.get("pos_overall")
            if position is None:
                position = item.get("pos")
            # Defensive: Decodo *should* return ints, but a malformed row with
            # a non-numeric position would otherwise abort the whole keyword.
            # Skip the bad row, keep the rest of the SERP.
            try:
                position_int = int(position) if position is not None else None
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping organic row with non-numeric position %r (url=%s)",
                    position, url,
                )
                continue
            if position_int is None:
                continue
            entries.append(CompetitorEntry(
                position=position_int,
                url=url,
                domain=host,
                title=str(item.get("title") or ""),
            ))
        return entries

    def _find_in_organic(
        self,
        organic: List[Dict[str, Any]],
        domain: str,
        target_url: Optional[str] = None,
    ):
        for item in organic:
            url = item.get("url", "")
            matched = (
                self._exact_url_match(url, target_url)
                if target_url
                else self._domain_match(url, domain)
            )
            if matched:
                # pos_overall counts across the full SERP (including AI/featured blocks),
                # which is what an SEO operator wants to track.
                position = item.get("pos_overall")
                if position is None:
                    position = item.get("pos")
                return position, url
        return None, None

    @staticmethod
    def _exact_url_match(url: str, target_url: str) -> bool:
        if not url or not target_url:
            return False
        return url.rstrip("/").lower() == target_url.rstrip("/").lower()

    def _scan_featured_snippets(self, serp: Dict[str, Any], domain: str):
        # Decodo's docs document `featured_snippet` (singular) but the live API
        # returns `featured_snippets` (plural) wrapping `{items: [...], pos_overall}`.
        # Accept both names + both shapes so a future schema flip doesn't break us.
        block = serp.get("featured_snippets")
        if block is None:
            block = serp.get("featured_snippet")
        if block is None:
            return False, False

        if isinstance(block, dict):
            items = block.get("items")
            if isinstance(items, list):
                fs_items = items
            else:
                fs_items = [block]  # legacy: dict is itself the single snippet
        elif isinstance(block, list):
            fs_items = block
        else:
            return False, False

        if not fs_items:
            return False, False

        owned = any(
            self._domain_match(item.get("url", ""), domain)
            for item in fs_items if isinstance(item, dict)
        )
        return True, owned

    def _scan_ai_overviews(self, overviews: List[Dict[str, Any]], domain: str):
        cited = False
        citation_rank: Optional[int] = None
        citations: List[AIOverviewCitation] = []
        for ai in overviews:
            sp = (ai.get("source_panel") or {}).get("items") or []
            for c in sp:
                url = c.get("url", "")
                citations.append(
                    AIOverviewCitation(
                        url=url,
                        title=c.get("title", "") or "",
                        source=c.get("source", "") or "",
                        position=c.get("pos") or 0,
                    )
                )
                if not cited and self._domain_match(url, domain):
                    cited = True
                    citation_rank = c.get("pos")
        return cited, citation_rank, citations

    @staticmethod
    def _domain_match(url: str, domain: str) -> bool:
        if not url or not domain:
            return False
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host == domain or host.endswith(f".{domain}")

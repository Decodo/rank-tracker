"""Comprehensive unit + integration tests for the rank tracker.

Run from the project root:

    python -m unittest tests.test_all -v

The Decodo client is exercised with a real captured response (no network).
Storage is exercised against a tempfile SQLite DB.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests as requests_module
requests_module_ConnectionError = requests_module.ConnectionError

# Make the project root importable when invoking via `python -m unittest`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from canary import validate_response
from fetcher import (
    DECODO_ENDPOINT,
    DecodoAPIError,
    DecodoCredentialError,
    DecodoFetcher,
)
from models import CompetitorEntry, RankCheckConfig, RankResult
from report import (
    _competitor_diff,
    render_competitors,
    render_html,
    render_summary,
)
from scheduler import (
    Alerter,
    BatchRunner,
    _webhook_payload,
    build_run_heartbeat,
    filter_due_configs,
    load_keywords_csv,
)
from scoring import visibility_score, visibility_score_from_row
from storage import Storage


FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
FIXTURE_PATH = FIXTURES_DIR / "decodo_best_laptop_2026.json"
FS_FIXTURE_PATH = FIXTURES_DIR / "decodo_featured_snippet.json"


def load_fixture(path=FIXTURE_PATH) -> dict:
    with open(path) as fh:
        return json.load(fh)


def minimal_response(**serp_overrides) -> dict:
    """Build a minimal Decodo response that passes the canary."""
    serp = {
        "organic": [
            {"pos": 1, "pos_overall": 1, "url": "https://example.com/a", "title": "A"},
            {"pos": 2, "pos_overall": 2, "url": "https://www.target.com/b", "title": "B"},
        ],
        "total_results_count": 1234,
    }
    serp.update(serp_overrides)
    return {
        "results": [{
            "content": {
                "results": {
                    "parse_status_code": 12000,
                    "results": serp,
                    "url": "https://www.google.com/search?q=test",
                },
                "errors": [],
                "status_code": 200,
            },
            "status_code": 200,
        }]
    }


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------

class TestCanary(unittest.TestCase):
    def test_real_response_passes(self):
        self.assertEqual(validate_response(load_fixture()), [])

    def test_minimal_response_passes(self):
        self.assertEqual(validate_response(minimal_response()), [])

    def test_empty_data_fails(self):
        issues = validate_response({})
        self.assertTrue(any("top-level path missing" in i for i in issues))

    def test_missing_organic_fails(self):
        data = minimal_response()
        del data["results"][0]["content"]["results"]["results"]["organic"]
        issues = validate_response(data)
        self.assertTrue(any("organic" in i for i in issues))

    def test_organic_wrong_type_fails(self):
        data = minimal_response(organic="not a list")
        issues = validate_response(data)
        self.assertTrue(any("expected list" in i for i in issues))

    def test_organic_item_missing_url_fails(self):
        data = minimal_response(organic=[{"pos": 1, "title": "no url"}])
        issues = validate_response(data)
        self.assertTrue(any("'url'" in i for i in issues))

    def test_parse_status_non_12000_fails(self):
        data = minimal_response()
        data["results"][0]["content"]["results"]["parse_status_code"] = 12001
        issues = validate_response(data)
        self.assertTrue(any("parse_status_code" in i for i in issues))

    def test_no_features_does_not_alarm(self):
        # A query with only organic results – niche content – should pass.
        data = minimal_response()
        self.assertEqual(validate_response(data), [])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoring(unittest.TestCase):
    def _result(self, **kwargs) -> RankResult:
        return RankResult(keyword="k", target_domain="example.com", **kwargs)

    def test_not_ranked_zero(self):
        self.assertEqual(visibility_score(self._result()), 0.0)

    def test_position_1_baseline(self):
        self.assertAlmostEqual(
            visibility_score(self._result(organic_position=1)), 100.0
        )

    def test_position_decay(self):
        s1 = visibility_score(self._result(organic_position=1))
        s10 = visibility_score(self._result(organic_position=10))
        self.assertGreater(s1, s10)
        self.assertGreater(s10, 0)

    def test_featured_snippet_bonus(self):
        base = visibility_score(self._result(organic_position=5))
        boosted = visibility_score(
            self._result(organic_position=5, featured_snippet_owned=True)
        )
        self.assertEqual(boosted - base, 30.0)

    def test_ai_citation_bonus_decays_by_rank(self):
        first = visibility_score(self._result(
            ai_overview_cited=True, ai_overview_citation_rank=1
        ))
        fifth = visibility_score(self._result(
            ai_overview_cited=True, ai_overview_citation_rank=5
        ))
        self.assertGreater(first, fifth)

    def test_from_row_matches_score(self):
        result = self._result(
            organic_position=2,
            ai_overview_cited=True,
            ai_overview_citation_rank=1,
            paa_present=True,
        )
        row = {
            "organic_position": 2,
            "ai_overview_cited": 1,
            "ai_overview_citation_rank": 1,
            "featured_snippet_owned": 0,
            "paa_present": 1,
        }
        self.assertAlmostEqual(
            visibility_score(result), visibility_score_from_row(row), places=1
        )


# ---------------------------------------------------------------------------
# DecodoFetcher: credentials, retries, parsing
# ---------------------------------------------------------------------------

class TestFetcherCredentials(unittest.TestCase):
    def test_missing_auth_raises_credential_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DecodoCredentialError):
                DecodoFetcher()

    def test_explicit_auth_used(self):
        with patch.dict(os.environ, {}, clear=True):
            f = DecodoFetcher(auth="abc")
            self.assertEqual(f.auth, "abc")


class TestFetcherTransport(unittest.TestCase):
    def setUp(self):
        self.cfg = RankCheckConfig(keyword="x", target_domain="example.com")

    def _fetcher(self) -> DecodoFetcher:
        return DecodoFetcher(auth="abc", max_retries=3, backoff_base=1.0, timeout=5)

    def test_401_raises_credential_error_no_retry(self):
        fetcher = self._fetcher()
        mock_resp = MagicMock(status_code=401, ok=False, text="bad creds")
        with patch.object(fetcher.session, "post", return_value=mock_resp) as posted:
            with self.assertRaises(DecodoCredentialError):
                fetcher.fetch(self.cfg)
        self.assertEqual(posted.call_count, 1)

    def test_503_retries_then_succeeds(self):
        fetcher = self._fetcher()
        ok_resp = MagicMock(status_code=200, ok=True)
        ok_resp.json.return_value = minimal_response()
        flaky = MagicMock(status_code=503, ok=False, text="busy")
        with patch.object(fetcher.session, "post",
                          side_effect=[flaky, flaky, ok_resp]) as posted, \
             patch("fetcher.time.sleep"):  # don't actually sleep
            result = fetcher.fetch(self.cfg)
        self.assertEqual(posted.call_count, 3)
        self.assertEqual(result.keyword, "x")

    def test_503_all_attempts_fail_raises(self):
        fetcher = self._fetcher()
        flaky = MagicMock(status_code=503, ok=False, text="busy")
        with patch.object(fetcher.session, "post", return_value=flaky), \
             patch("fetcher.time.sleep"):
            with self.assertRaises(DecodoAPIError):
                fetcher.fetch(self.cfg)

    def test_400_raises_api_error_no_retry(self):
        fetcher = self._fetcher()
        bad = MagicMock(status_code=400, ok=False, text="bad payload")
        with patch.object(fetcher.session, "post", return_value=bad) as posted:
            with self.assertRaises(DecodoAPIError):
                fetcher.fetch(self.cfg)
        self.assertEqual(posted.call_count, 1)

    def test_non_json_body_raises(self):
        fetcher = self._fetcher()
        weird = MagicMock(status_code=200, ok=True, text="<html>oops</html>")
        weird.json.side_effect = ValueError("no json")
        with patch.object(fetcher.session, "post", return_value=weird):
            with self.assertRaises(DecodoAPIError):
                fetcher.fetch(self.cfg)


class TestFetcherParse(unittest.TestCase):
    """Parse the real captured Decodo response and verify each field."""

    def setUp(self):
        self.fetcher = DecodoFetcher(auth="abc")
        self.data = load_fixture()

    def _fetch(self, cfg: RankCheckConfig) -> RankResult:
        with patch.object(self.fetcher, "_post", return_value=self.data):
            return self.fetcher.fetch(cfg)

    def test_wired_is_position_2(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com",
        ))
        self.assertEqual(result.organic_position, 2)
        self.assertEqual(result.matched_url, "https://www.wired.com/story/best-laptops/")

    def test_target_not_in_serp_returns_none(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="not-in-serp.example",
        ))
        self.assertIsNone(result.organic_position)

    def test_ai_overview_detected_and_cited(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com",
        ))
        self.assertTrue(result.ai_overview_present)
        self.assertTrue(result.ai_overview_cited)
        self.assertEqual(result.ai_overview_citation_rank, 1)
        self.assertGreater(len(result.ai_overview_citations), 0)

    def test_ai_overview_present_but_not_cited(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="not-cited.example",
        ))
        self.assertTrue(result.ai_overview_present)
        self.assertFalse(result.ai_overview_cited)
        self.assertIsNone(result.ai_overview_citation_rank)

    def test_paa_detected(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com",
        ))
        self.assertTrue(result.paa_present)
        self.assertEqual(result.paa_question_count, 4)

    def test_organic_count_and_total_results(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com",
        ))
        self.assertEqual(result.raw_organic_count, 8)
        self.assertIsNotNone(result.total_results)

    def test_config_dimensions_carried_through(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com",
            geo="Germany", locale="de-de", device_type="mobile",
        ))
        self.assertEqual(result.location, "Germany")
        self.assertEqual(result.locale, "de-de")
        self.assertEqual(result.device, "mobile")

    def test_exact_target_url_match(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026",
            target_domain="wired.com",
            target_url="https://www.wired.com/story/best-laptops/",
        ))
        self.assertEqual(result.organic_position, 2)

    def test_exact_target_url_mismatch(self):
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026",
            target_domain="wired.com",
            target_url="https://www.wired.com/different-page",
        ))
        self.assertIsNone(result.organic_position)


class TestFetcherFeaturedSnippet(unittest.TestCase):
    """Featured-snippet detection against a real captured response.

    Locks in the (currently observed) `featured_snippets` plural / `{items: [...]}`
    shape, plus fall-back support for the docs' `featured_snippet` singular form.
    """

    def setUp(self):
        self.fetcher = DecodoFetcher(auth="abc")

    def _fetch(self, cfg: RankCheckConfig, data) -> RankResult:
        with patch.object(self.fetcher, "_post", return_value=data):
            return self.fetcher.fetch(cfg)

    def test_real_response_detects_snippet(self):
        data = load_fixture(FS_FIXTURE_PATH)
        # The captured response has youtube.com owning the snippet for
        # "how to tie a tie". Detect presence regardless of target.
        result = self._fetch(RankCheckConfig(
            keyword="how to tie a tie", target_domain="example.com",
        ), data)
        self.assertTrue(result.featured_snippet)
        self.assertFalse(result.featured_snippet_owned)

    def test_real_response_detects_ownership(self):
        data = load_fixture(FS_FIXTURE_PATH)
        result = self._fetch(RankCheckConfig(
            keyword="how to tie a tie", target_domain="youtube.com",
        ), data)
        self.assertTrue(result.featured_snippet)
        self.assertTrue(result.featured_snippet_owned)

    def test_plural_items_shape(self):
        # featured_snippets: {items: [...], pos_overall}
        data = minimal_response(featured_snippets={
            "items": [{"pos": 1, "url": "https://example.com/x", "title": "T"}],
            "pos_overall": 1,
        })
        result = self._fetch(RankCheckConfig(
            keyword="x", target_domain="example.com"), data)
        self.assertTrue(result.featured_snippet)
        self.assertTrue(result.featured_snippet_owned)

    def test_singular_legacy_shape(self):
        # featured_snippet: [{url, ...}] – legacy/documented form
        data = minimal_response(featured_snippet=[
            {"url": "https://example.com/y", "title": "T"},
        ])
        result = self._fetch(RankCheckConfig(
            keyword="x", target_domain="example.com"), data)
        self.assertTrue(result.featured_snippet)
        self.assertTrue(result.featured_snippet_owned)

    def test_no_snippet_block(self):
        data = minimal_response()
        result = self._fetch(RankCheckConfig(
            keyword="x", target_domain="example.com"), data)
        self.assertFalse(result.featured_snippet)
        self.assertFalse(result.featured_snippet_owned)


class TestFetcherDomainMatch(unittest.TestCase):
    """Edge cases in URL/domain matching."""

    def _match(self, url, domain):
        return DecodoFetcher._domain_match(url, domain)

    def test_exact_match(self):
        self.assertTrue(self._match("https://example.com/page", "example.com"))

    def test_www_prefix_stripped(self):
        self.assertTrue(self._match("https://www.example.com/page", "example.com"))

    def test_subdomain_matches(self):
        self.assertTrue(self._match("https://blog.example.com/x", "example.com"))

    def test_different_domain_does_not_match(self):
        self.assertFalse(self._match("https://other.com/x", "example.com"))

    def test_substring_does_not_match(self):
        # Guard against naive "in" matching: example.com.evil.com should not match.
        self.assertFalse(self._match("https://example.com.evil.com/x", "example.com"))

    def test_empty_url(self):
        self.assertFalse(self._match("", "example.com"))

    def test_empty_domain(self):
        self.assertFalse(self._match("https://example.com/x", ""))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        self.storage = Storage(self.path)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _result(self, **kwargs) -> RankResult:
        defaults = dict(
            keyword="kw", target_domain="example.com",
            location="United States", device="desktop", locale="en-us",
        )
        defaults.update(kwargs)
        return RankResult(**defaults)

    def test_round_trip(self):
        run_id = self.storage.start_run("notes")
        self.storage.save_result(run_id, self._result(organic_position=3))
        self.storage.finish_run(run_id)
        rows = self.storage.history(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["organic_position"], 3)

    def test_previous_position_returns_prior_run(self):
        r1 = self.storage.start_run()
        self.storage.save_result(r1, self._result(organic_position=5))
        r2 = self.storage.start_run()
        prev = self.storage.previous_position(
            "kw", "United States", "desktop", "en-us", before_run_id=r2
        )
        self.assertEqual(prev, 5)

    def test_previous_position_with_no_prior_returns_none(self):
        r1 = self.storage.start_run()
        prev = self.storage.previous_position(
            "kw", "X", "desktop", "en-us", before_run_id=r1
        )
        self.assertIsNone(prev)

    def test_previous_position_isolates_by_dimension(self):
        r1 = self.storage.start_run()
        self.storage.save_result(
            r1, self._result(organic_position=5, location="United States")
        )
        r2 = self.storage.start_run()
        # Same keyword but different location should not see the US prior.
        prev = self.storage.previous_position(
            "kw", "Germany", "desktop", "en-us", before_run_id=r2
        )
        self.assertIsNone(prev)

    def test_export_csv_writes_rows(self):
        r1 = self.storage.start_run()
        self.storage.save_result(r1, self._result(organic_position=2))
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as out:
            out_path = out.name
        try:
            n = self.storage.export_csv(out_path)
            self.assertEqual(n, 1)
            content = Path(out_path).read_text()
            self.assertIn("organic_position", content)  # header
            self.assertIn(",2,", content)  # value
        finally:
            os.unlink(out_path)

    def test_export_empty_returns_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as out:
            out_path = out.name
        try:
            n = self.storage.export_csv(out_path)
            self.assertEqual(n, 0)
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def test_ai_citations_json_round_trip(self):
        r1 = self.storage.start_run()
        from models import AIOverviewCitation
        cit = AIOverviewCitation(url="https://example.com", title="t",
                                 source="s", position=1)
        self.storage.save_result(r1, self._result(
            ai_overview_present=True, ai_overview_cited=True,
            ai_overview_citation_rank=1, ai_overview_citations=[cit],
        ))
        rows = self.storage.history(limit=1)
        payload = json.loads(rows[0]["ai_overview_citations_json"])
        self.assertEqual(payload[0]["url"], "https://example.com")


# ---------------------------------------------------------------------------
# Alerter
# ---------------------------------------------------------------------------

class TestAlerter(unittest.TestCase):
    def _result(self, pos):
        return RankResult(
            keyword="kw", target_domain="example.com",
            location="US", device="desktop", locale="en-us",
            organic_position=pos,
        )

    def test_no_change_no_alert(self):
        alerter = Alerter(threshold=3)
        self.assertIsNone(alerter.evaluate(self._result(5), 5))

    def test_below_threshold_no_alert(self):
        alerter = Alerter(threshold=3)
        self.assertIsNone(alerter.evaluate(self._result(7), 5))

    def test_drop_at_threshold_alerts(self):
        alerter = Alerter(threshold=3)
        msg = alerter.evaluate(self._result(8), 5)
        self.assertIsNotNone(msg)
        self.assertIn("DROPPED", msg)

    def test_improvement_alerts(self):
        alerter = Alerter(threshold=3)
        msg = alerter.evaluate(self._result(2), 8)
        self.assertIsNotNone(msg)
        self.assertIn("IMPROVED", msg)

    def test_new_ranking(self):
        alerter = Alerter(threshold=3)
        msg = alerter.evaluate(self._result(5), None)
        self.assertIn("NEW", msg)

    def test_dropped_out(self):
        alerter = Alerter(threshold=3)
        msg = alerter.evaluate(self._result(None), 5)
        self.assertIn("DROPPED OUT", msg)

    def test_both_none_no_alert(self):
        alerter = Alerter(threshold=3)
        self.assertIsNone(alerter.evaluate(self._result(None), None))

    def test_webhook_called_when_configured(self):
        alerter = Alerter(threshold=3, webhook_url="https://hook.example/x")
        with patch("scheduler.requests.post") as posted:
            posted.return_value = MagicMock(status_code=200, text="ok")
            statuses = alerter.send("hello")
        posted.assert_called_once()
        self.assertEqual(posted.call_args.args[0], "https://hook.example/x")
        self.assertEqual(statuses, ["webhook: ok"])

    def test_webhook_4xx_reports_failure(self):
        alerter = Alerter(threshold=3, webhook_url="https://hook.example/x")
        with patch("scheduler.requests.post") as posted:
            posted.return_value = MagicMock(status_code=404, text="not found")
            statuses = alerter.send("hello")
        self.assertEqual(statuses, ["webhook: HTTP 404"])

    def test_webhook_204_counted_as_success(self):
        # Discord returns 204 on success.
        alerter = Alerter(threshold=3,
                          webhook_url="https://discord.com/api/webhooks/123/abc")
        with patch("scheduler.requests.post") as posted:
            posted.return_value = MagicMock(status_code=204, text="")
            statuses = alerter.send("hello")
        self.assertEqual(statuses, ["webhook: ok"])

    def test_webhook_transport_error_reports_failure(self):
        alerter = Alerter(threshold=3, webhook_url="https://hook.example/x")
        with patch("scheduler.requests.post",
                   side_effect=requests_module_ConnectionError()) as posted:
            statuses = alerter.send("hello")
        self.assertEqual(len(statuses), 1)
        self.assertTrue(statuses[0].startswith("webhook: transport error"))

    def test_no_channel_returns_empty(self):
        alerter = Alerter(threshold=3)
        statuses = alerter.send("hello")
        self.assertEqual(statuses, [])


class TestWebhookPayload(unittest.TestCase):
    def test_slack_format(self):
        p = _webhook_payload("https://hooks.slack.com/services/T/B/X", "hi")
        self.assertEqual(p, {"text": "hi"})

    def test_discord_format(self):
        p = _webhook_payload("https://discord.com/api/webhooks/1/abc", "hi")
        self.assertEqual(p, {"content": "hi"})

    def test_discord_alternate_host(self):
        p = _webhook_payload("https://discordapp.com/api/webhooks/1/abc", "hi")
        self.assertEqual(p, {"content": "hi"})

    def test_unknown_host_defaults_to_text(self):
        p = _webhook_payload("https://example.com/hook", "hi")
        self.assertEqual(p, {"text": "hi"})

    def test_invalid_url_does_not_raise(self):
        p = _webhook_payload("not-a-url", "hi")
        self.assertEqual(p, {"text": "hi"})


# ---------------------------------------------------------------------------
# Keyword loader
# ---------------------------------------------------------------------------

class TestKeywordLoader(unittest.TestCase):
    def _write_csv(self, content: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_minimal_csv(self):
        path = self._write_csv(
            "keyword,target_domain\nfoo,example.com\nbar,other.com\n"
        )
        try:
            configs = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0].keyword, "foo")
        self.assertEqual(configs[1].target_domain, "other.com")

    def test_optional_fields_parsed(self):
        path = self._write_csv(
            "keyword,target_domain,geo,locale,device_type,google_results_language,pages,target_url\n"
            "kw,ex.com,Germany,de-de,mobile,de,2,https://ex.com/p\n"
        )
        try:
            [cfg] = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg.geo, "Germany")
        self.assertEqual(cfg.locale, "de-de")
        self.assertEqual(cfg.device_type, "mobile")
        self.assertEqual(cfg.pages, 2)
        self.assertEqual(cfg.target_url, "https://ex.com/p")

    def test_skips_blank_keyword(self):
        path = self._write_csv(
            "keyword,target_domain\n,example.com\nfoo,example.com\n"
        )
        try:
            configs = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].keyword, "foo")

    def test_skips_missing_target_domain(self):
        path = self._write_csv("keyword,target_domain\nfoo,\nbar,example.com\n")
        try:
            configs = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].keyword, "bar")

    def test_comment_rows_ignored(self):
        path = self._write_csv(
            "keyword,target_domain\n# comment,foo\nfoo,example.com\n"
        )
        try:
            configs = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(configs), 1)

    def test_bad_pages_value_falls_back(self):
        path = self._write_csv(
            "keyword,target_domain,pages\nfoo,example.com,not-a-number\n"
        )
        try:
            [cfg] = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg.pages, 1)  # default

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_keywords_csv("/this/path/does/not/exist.csv")


# ---------------------------------------------------------------------------
# BatchRunner: end-to-end with mocked fetcher
# ---------------------------------------------------------------------------

class TestBatchRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _make_fetcher(self, results):
        fetcher = MagicMock(spec=DecodoFetcher)
        fetcher.fetch.side_effect = results
        # BatchRunner reads these to record per-run API counts. MagicMock
        # would otherwise return more MagicMocks, which SQLite rejects.
        fetcher.api_call_count = 0
        fetcher.reset_call_count = MagicMock()
        return fetcher

    def test_sequential_run_persists_all(self):
        configs = [
            RankCheckConfig(keyword="a", target_domain="a.com"),
            RankCheckConfig(keyword="b", target_domain="b.com"),
        ]
        results = [
            RankResult(keyword="a", target_domain="a.com", organic_position=1,
                       location="United States", device="desktop", locale="en-us"),
            RankResult(keyword="b", target_domain="b.com", organic_position=5,
                       location="United States", device="desktop", locale="en-us"),
        ]
        fetcher = self._make_fetcher(results)
        runner = BatchRunner(fetcher, self.storage, delay_seconds=0)
        run_id = runner.run(configs)
        rows = self.storage.history(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["run_id"], run_id)

    def test_concurrent_run_persists_all(self):
        configs = [
            RankCheckConfig(keyword=f"k{i}", target_domain="x.com") for i in range(5)
        ]
        results = [
            RankResult(keyword=f"k{i}", target_domain="x.com", organic_position=i + 1,
                       location="United States", device="desktop", locale="en-us")
            for i in range(5)
        ]
        fetcher = self._make_fetcher(results)
        runner = BatchRunner(fetcher, self.storage, delay_seconds=0, concurrency=3)
        runner.run(configs)
        rows = self.storage.history(limit=10)
        self.assertEqual(len(rows), 5)

    def test_one_failure_does_not_abort_run(self):
        configs = [
            RankCheckConfig(keyword="ok", target_domain="x.com"),
            RankCheckConfig(keyword="boom", target_domain="x.com"),
            RankCheckConfig(keyword="also-ok", target_domain="x.com"),
        ]
        good = RankResult(keyword="ok", target_domain="x.com", organic_position=1,
                          location="United States", device="desktop", locale="en-us")
        good2 = RankResult(keyword="also-ok", target_domain="x.com", organic_position=4,
                           location="United States", device="desktop", locale="en-us")
        fetcher = self._make_fetcher([good, RuntimeError("Decodo blew up"), good2])
        runner = BatchRunner(fetcher, self.storage, delay_seconds=0)
        runner.run(configs)
        rows = self.storage.history(limit=10)
        # Two successful saves, the failed one is skipped.
        self.assertEqual(len(rows), 2)
        keywords = {r["keyword"] for r in rows}
        self.assertEqual(keywords, {"ok", "also-ok"})


class TestCompetitorCapture(unittest.TestCase):
    """Top-N competitor extraction from organic results."""

    def setUp(self):
        self.fetcher = DecodoFetcher(auth="abc")

    def _fetch(self, cfg: RankCheckConfig, data) -> RankResult:
        with patch.object(self.fetcher, "_post", return_value=data):
            return self.fetcher.fetch(cfg)

    def test_default_top_n_captures_all_organic(self):
        data = load_fixture()
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com",
        ), data)
        # Fixture has 8 organic results; default top_n is 10, so capture all 8.
        self.assertEqual(len(result.top_results), 8)
        self.assertEqual(result.top_results[0].domain, "wired.com")
        self.assertEqual(result.top_results[0].position, 2)

    def test_top_n_clamps_to_available(self):
        data = load_fixture()
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com", top_n=3,
        ), data)
        self.assertEqual(len(result.top_results), 3)

    def test_top_n_zero_captures_nothing(self):
        data = load_fixture()
        result = self._fetch(RankCheckConfig(
            keyword="best laptop 2026", target_domain="wired.com", top_n=0,
        ), data)
        self.assertEqual(result.top_results, [])

    def test_domain_extraction_strips_www(self):
        data = minimal_response(organic=[
            {"pos": 1, "pos_overall": 1, "url": "https://www.example.com/a", "title": "A"},
        ])
        result = self._fetch(RankCheckConfig(
            keyword="x", target_domain="example.com"), data)
        self.assertEqual(result.top_results[0].domain, "example.com")


class TestCompetitorDiff(unittest.TestCase):
    def test_entered_dropped_moved(self):
        prev = [
            {"position": 1, "domain": "a.com", "url": "https://a.com", "title": "A"},
            {"position": 2, "domain": "b.com", "url": "https://b.com", "title": "B"},
            {"position": 3, "domain": "c.com", "url": "https://c.com", "title": "C"},
        ]
        curr = [
            {"position": 1, "domain": "a.com", "url": "https://a.com", "title": "A"},
            {"position": 2, "domain": "d.com", "url": "https://d.com", "title": "D"},
            {"position": 3, "domain": "b.com", "url": "https://b.com", "title": "B"},
        ]
        diff = _competitor_diff(curr, prev)
        self.assertEqual([c["domain"] for c in diff["entered"]], ["d.com"])
        self.assertEqual([c["domain"] for c in diff["dropped"]], ["c.com"])
        # b.com moved 2 -> 3
        self.assertEqual(len(diff["moved"]), 1)
        c, p, n = diff["moved"][0]
        self.assertEqual(c["domain"], "b.com")
        self.assertEqual((p, n), (2, 3))

    def test_no_diff(self):
        prev = [{"position": 1, "domain": "a.com", "url": "u", "title": "T"}]
        diff = _competitor_diff(prev, prev)
        self.assertEqual(diff["entered"], [])
        self.assertEqual(diff["dropped"], [])
        self.assertEqual(diff["moved"], [])


class TestStorageNewColumns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_top_results_round_trip(self):
        run_id = self.storage.start_run()
        result = RankResult(
            keyword="kw", target_domain="example.com",
            location="US", device="desktop", locale="en-us",
            organic_position=1,
            top_results=[
                CompetitorEntry(position=1, url="https://x.com",
                                domain="x.com", title="X"),
                CompetitorEntry(position=2, url="https://y.com",
                                domain="y.com", title="Y"),
            ],
        )
        self.storage.save_result(run_id, result)
        rows = self.storage.history(limit=1)
        payload = json.loads(rows[0]["top_results_json"])
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["domain"], "x.com")

    def test_api_calls_recorded(self):
        run_id = self.storage.start_run()
        self.storage.record_api_calls(run_id, 17)
        self.assertEqual(self.storage.lifetime_api_calls(), 17)
        run_id_2 = self.storage.start_run()
        self.storage.record_api_calls(run_id_2, 5)
        self.assertEqual(self.storage.lifetime_api_calls(), 22)

    def test_latest_finished_run_none_when_empty(self):
        self.assertIsNone(self.storage.latest_finished_run())

    def test_latest_finished_run_after_finish(self):
        run_id = self.storage.start_run()
        self.storage.finish_run(run_id)
        latest = self.storage.latest_finished_run()
        self.assertEqual(latest["id"], run_id)
        self.assertIsNotNone(latest["finished_at"])


class TestFetcherCounter(unittest.TestCase):
    def test_counter_increments_per_post(self):
        fetcher = DecodoFetcher(auth="abc", max_retries=3, backoff_base=1.0)
        self.assertEqual(fetcher.api_call_count, 0)

        cfg = RankCheckConfig(keyword="x", target_domain="example.com")
        ok = MagicMock(status_code=200, ok=True)
        ok.json.return_value = minimal_response()
        with patch.object(fetcher.session, "post", return_value=ok):
            fetcher.fetch(cfg)
        self.assertEqual(fetcher.api_call_count, 1)

        # Reset, then a 503-retry-success scenario should count THREE calls.
        fetcher.reset_call_count()
        flaky = MagicMock(status_code=503, ok=False, text="busy")
        with patch.object(fetcher.session, "post",
                          side_effect=[flaky, flaky, ok]), \
             patch("fetcher.time.sleep"):
            fetcher.fetch(cfg)
        self.assertEqual(fetcher.api_call_count, 3)


class TestFrequencyFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _cfg(self, **overrides):
        kwargs = dict(keyword="kw", target_domain="example.com")
        kwargs.update(overrides)
        return RankCheckConfig(**kwargs)

    def _save_check(self, cfg: RankCheckConfig, hours_ago: float):
        run_id = self.storage.start_run()
        ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        r = RankResult(
            keyword=cfg.keyword, target_domain=cfg.target_domain,
            location=cfg.geo, device=cfg.device_type, locale=cfg.locale,
            organic_position=5, timestamp=ts,
        )
        self.storage.save_result(run_id, r)
        self.storage.finish_run(run_id)

    def test_inactive_skipped(self):
        cfg = self._cfg(active=False)
        due, skipped = filter_due_configs([cfg], self.storage)
        self.assertEqual(due, [])
        self.assertEqual(skipped[0][1], "inactive")

    def test_paused_skipped(self):
        cfg = self._cfg(frequency="paused")
        due, skipped = filter_due_configs([cfg], self.storage)
        self.assertEqual(due, [])
        self.assertEqual(skipped[0][1], "paused")

    def test_daily_skipped_if_checked_recently(self):
        cfg = self._cfg(frequency="daily")
        self._save_check(cfg, hours_ago=2)
        due, skipped = filter_due_configs([cfg], self.storage)
        self.assertEqual(due, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("frequency=daily", skipped[0][1])

    def test_daily_due_if_checked_long_ago(self):
        cfg = self._cfg(frequency="daily")
        self._save_check(cfg, hours_ago=25)
        due, _ = filter_due_configs([cfg], self.storage)
        self.assertEqual(len(due), 1)

    def test_weekly_window(self):
        cfg = self._cfg(frequency="weekly")
        # 3 days ago – still within the weekly window, should skip
        self._save_check(cfg, hours_ago=72)
        due, _ = filter_due_configs([cfg], self.storage)
        self.assertEqual(due, [])
        # 7 days ago – past window, should be due
        run_id = self.storage.start_run()
        ts = datetime.now(timezone.utc) - timedelta(days=7)
        self.storage.save_result(run_id, RankResult(
            keyword=cfg.keyword, target_domain=cfg.target_domain,
            location=cfg.geo, device=cfg.device_type, locale=cfg.locale,
            organic_position=5, timestamp=ts,
        ))
        self.storage.finish_run(run_id)
        due, _ = filter_due_configs([cfg], self.storage)
        self.assertEqual(len(due), 1)

    def test_first_check_always_due(self):
        cfg = self._cfg(frequency="daily")
        due, skipped = filter_due_configs([cfg], self.storage)
        self.assertEqual(len(due), 1)
        self.assertEqual(skipped, [])


class TestCSVDedupeAndNewColumns(unittest.TestCase):
    def _write(self, content: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_active_no_parsed(self):
        path = self._write(
            "keyword,target_domain,active\nkw,example.com,no\n"
        )
        try:
            [cfg] = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertFalse(cfg.active)

    def test_frequency_parsed(self):
        path = self._write(
            "keyword,target_domain,frequency\nkw,example.com,weekly\n"
        )
        try:
            [cfg] = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg.frequency, "weekly")

    def test_unknown_frequency_falls_back_to_daily(self):
        path = self._write(
            "keyword,target_domain,frequency\nkw,example.com,quarterly\n"
        )
        try:
            [cfg] = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg.frequency, "daily")

    def test_duplicate_rows_collapsed(self):
        path = self._write(
            "keyword,target_domain\nkw,example.com\nkw,example.com\n"
        )
        try:
            configs = load_keywords_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(configs), 1)


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _add_result(self, run_id, keyword, pos):
        self.storage.save_result(run_id, RankResult(
            keyword=keyword, target_domain="x.com",
            location="US", device="desktop", locale="en-us",
            organic_position=pos,
        ))

    def test_first_run_treats_entries_as_movers(self):
        # A keyword entering the SERP for the first time IS a movement.
        run_id = self.storage.start_run()
        self._add_result(run_id, "kw1", 5)
        self.storage.record_api_calls(run_id, 1)
        self.storage.finish_run(run_id)
        msg = build_run_heartbeat(self.storage, run_id, alert_threshold=3)
        self.assertIn("1 check(s) saved", msg)
        self.assertIn("NEW", msg)
        self.assertIn("kw1", msg)

    def test_stable_run_reports_no_movers(self):
        run1 = self.storage.start_run()
        self._add_result(run1, "kw1", 5)
        self.storage.finish_run(run1)
        run2 = self.storage.start_run()
        self._add_result(run2, "kw1", 5)  # same position
        self.storage.record_api_calls(run2, 1)
        self.storage.finish_run(run2)
        msg = build_run_heartbeat(self.storage, run2, alert_threshold=3)
        self.assertIn("No keywords moved", msg)

    def test_movers_surfaced(self):
        # Run 1: pos 5
        run1 = self.storage.start_run()
        self._add_result(run1, "kw1", 5)
        self.storage.finish_run(run1)
        # Run 2: pos 10 (dropped 5 positions)
        run2 = self.storage.start_run()
        self._add_result(run2, "kw1", 10)
        self.storage.record_api_calls(run2, 1)
        self.storage.finish_run(run2)
        msg = build_run_heartbeat(self.storage, run2, alert_threshold=3)
        self.assertIn("Top movers", msg)
        self.assertIn("kw1", msg)
        self.assertIn("5→10", msg)

    def test_new_and_dropped_out(self):
        run1 = self.storage.start_run()
        self._add_result(run1, "kw-gone", 5)
        self.storage.finish_run(run1)
        run2 = self.storage.start_run()
        # kw-gone fell out; kw-new entered at 3
        self.storage.save_result(run2, RankResult(
            keyword="kw-gone", target_domain="x.com",
            location="US", device="desktop", locale="en-us",
            organic_position=None,
        ))
        self._add_result(run2, "kw-new", 3)
        self.storage.record_api_calls(run2, 2)
        self.storage.finish_run(run2)
        msg = build_run_heartbeat(self.storage, run2, alert_threshold=3)
        self.assertIn("NEW", msg)
        self.assertIn("DROPPED OUT", msg)


class TestSafeCompetitorPosition(unittest.TestCase):
    """A malformed organic row must not crash the whole keyword check."""

    def setUp(self):
        self.fetcher = DecodoFetcher(auth="abc")

    def _fetch(self, cfg, data):
        with patch.object(self.fetcher, "_post", return_value=data):
            return self.fetcher.fetch(cfg)

    def test_non_numeric_position_skipped_not_crashed(self):
        data = minimal_response(organic=[
            {"pos": "1", "pos_overall": "1", "url": "https://good.com/a", "title": "ok"},
            {"pos": "abc", "pos_overall": None, "url": "https://bad.com/b", "title": "no"},
            {"pos": 3, "pos_overall": 3, "url": "https://also-good.com/c", "title": "ok2"},
        ])
        result = self._fetch(RankCheckConfig(
            keyword="x", target_domain="good.com"), data)
        # 2 valid rows survive, the malformed middle one is dropped.
        domains = [c.domain for c in result.top_results]
        self.assertIn("good.com", domains)
        self.assertIn("also-good.com", domains)
        self.assertNotIn("bad.com", domains)


class TestSMTPUnicode(unittest.TestCase):
    """Heartbeat arrows must not crash SMTP delivery (default ASCII would)."""

    def test_smtp_message_with_arrows_does_not_raise(self):
        alerter = Alerter(
            threshold=3,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="u",
            smtp_password="p",
            email_from="a@b",
            email_to="c@d",
        )
        with patch("scheduler.smtplib.SMTP") as smtp_cls:
            smtp = MagicMock()
            smtp_cls.return_value.__enter__.return_value = smtp
            statuses = alerter.send("Heartbeat ↑ ↓ → done")
        self.assertEqual(statuses, ["smtp: ok"])
        smtp.sendmail.assert_called_once()
        # Ensure the encoded message actually contains the unicode (base64'd is fine).
        sent_body = smtp.sendmail.call_args.args[2]
        self.assertIn("utf-8", sent_body.lower())


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _insert_old_run(self, days_ago: float, keyword: str = "kw"):
        ts = (datetime.now(timezone.utc)
              - timedelta(days=days_ago)).isoformat(timespec="seconds")
        with self.storage.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, finished_at) VALUES (?, ?)",
                (ts, ts),
            )
            run_id = cur.lastrowid
        self.storage.save_result(run_id, RankResult(
            keyword=keyword, target_domain="x.com",
            location="US", device="desktop", locale="en-us",
            organic_position=5,
            timestamp=datetime.fromisoformat(ts),
        ))
        return run_id

    def test_prune_deletes_old_runs_only(self):
        old_id = self._insert_old_run(days_ago=400, keyword="ancient")
        recent_id = self._insert_old_run(days_ago=10, keyword="recent")

        stats = self.storage.prune(older_than_days=365)
        self.assertEqual(stats["runs_deleted"], 1)
        self.assertEqual(stats["results_deleted"], 1)

        # Recent row survives.
        rows = self.storage.history(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["keyword"], "recent")

    def test_prune_nothing_to_delete(self):
        self._insert_old_run(days_ago=5)
        stats = self.storage.prune(older_than_days=30)
        self.assertEqual(stats["runs_deleted"], 0)
        self.assertEqual(stats["results_deleted"], 0)

    def test_prune_rejects_zero_days(self):
        with self.assertRaises(ValueError):
            self.storage.prune(older_than_days=0)


class TestHTMLReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_html_renders_empty(self):
        out = render_html(self.storage)
        self.assertIn("<title>Rank Tracker Report", out)
        self.assertIn("(no data)", out)

    def test_html_includes_keyword_and_competitor(self):
        run_id = self.storage.start_run()
        result = RankResult(
            keyword="best laptop", target_domain="wired.com",
            location="US", device="desktop", locale="en-us",
            organic_position=2,
            top_results=[
                CompetitorEntry(position=1, url="https://wired.com",
                                domain="wired.com", title="Top"),
                CompetitorEntry(position=2, url="https://pcmag.com",
                                domain="pcmag.com", title="Runner up"),
            ],
        )
        self.storage.save_result(run_id, result)
        self.storage.finish_run(run_id)
        out = render_html(self.storage)
        self.assertIn("best laptop", out)
        self.assertIn("wired.com", out)
        self.assertIn("pcmag.com", out)
        self.assertIn("own", out)  # marker class for your own domain

    def test_html_escapes_keyword(self):
        run_id = self.storage.start_run()
        self.storage.save_result(run_id, RankResult(
            keyword="<script>x</script>",
            target_domain="example.com",
            location="US", device="desktop", locale="en-us",
        ))
        out = render_html(self.storage)
        self.assertNotIn("<script>x</script>", out)
        self.assertIn("&lt;script&gt;", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Batch runner, keyword loader, and alerting.

`BatchRunner.run(configs)` performs one sweep across all configured
(keyword x location x device) tuples, persists each result, and fires
alerts when the position delta exceeds the configured threshold.
"""

from __future__ import annotations

import csv
import logging
import os
import smtplib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from fetcher import DecodoFetcher
from models import RankCheckConfig, RankResult
from scoring import visibility_score
from storage import Storage


# Minimum elapsed time before re-checking a (keyword, geo, device, locale) tuple.
# Slightly under the natural cadence so a daily cron always lands; the buffer
# kills accidental double-bills from manual+cron overlap.
FREQUENCY_WINDOWS = {
    "daily": timedelta(hours=22),
    "weekly": timedelta(days=6, hours=12),
    "monthly": timedelta(days=28),
}
PAUSED_FREQUENCIES = {"paused", "off", "never"}
VALID_FREQUENCIES = set(FREQUENCY_WINDOWS) | PAUSED_FREQUENCIES

logger = logging.getLogger(__name__)


def load_keywords_csv(path: str) -> List[RankCheckConfig]:
    """Load keyword configs from CSV.

    Required columns: `keyword`, `target_domain`.
    Optional columns: `geo`, `locale`, `device_type`, `google_results_language`, `pages`.
    Comment lines starting with `#` are ignored.
    """
    configs: List[RankCheckConfig] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row:
                continue
            cleaned = {k.strip(): (v.strip() if isinstance(v, str) else v)
                       for k, v in row.items() if k}
            if not cleaned.get("keyword") or cleaned["keyword"].startswith("#"):
                continue
            if not cleaned.get("target_domain"):
                logger.warning("Skipping row without target_domain: %r", cleaned)
                continue
            kwargs: Dict[str, Any] = {
                "keyword": cleaned["keyword"],
                "target_domain": cleaned["target_domain"],
            }
            for key in ("geo", "locale", "device_type",
                        "google_results_language", "target_url"):
                if cleaned.get(key):
                    kwargs[key] = cleaned[key]
            if cleaned.get("pages"):
                try:
                    kwargs["pages"] = int(str(cleaned["pages"]))
                except ValueError:
                    logger.warning("Bad pages value %r for keyword %r – using default",
                                   cleaned.get("pages"), cleaned["keyword"])
            if cleaned.get("active") is not None and cleaned.get("active") != "":
                kwargs["active"] = _parse_bool(cleaned["active"])
            if cleaned.get("frequency"):
                freq = cleaned["frequency"].lower()
                if freq not in VALID_FREQUENCIES:
                    logger.warning(
                        "Unknown frequency %r for keyword %r – using 'daily'. "
                        "Valid: %s", freq, cleaned["keyword"],
                        ", ".join(sorted(VALID_FREQUENCIES)),
                    )
                else:
                    kwargs["frequency"] = freq
            try:
                configs.append(RankCheckConfig(**kwargs))
            except Exception as exc:
                logger.warning("Skipping invalid keyword row %r: %s", cleaned, exc)
    configs = _dedupe_configs(configs)
    return configs


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() in {"1", "yes", "y", "true", "t", "on"}


def _config_key(c: RankCheckConfig) -> Tuple[str, str, str, str]:
    return (c.keyword.lower(), c.geo.lower(), c.device_type.lower(),
            c.locale.lower())


def _dedupe_configs(configs: List[RankCheckConfig]) -> List[RankCheckConfig]:
    """Collapse exact (keyword, geo, device, locale) duplicates.

    A typo'd row (e.g. two `device_type=desktop` rows for the same keyword) would
    otherwise silently double API spend. We keep the first occurrence and warn.
    """
    seen: Set[Tuple[str, str, str, str]] = set()
    out: List[RankCheckConfig] = []
    for c in configs:
        key = _config_key(c)
        if key in seen:
            logger.warning(
                "Duplicate row skipped: keyword=%r geo=%r device=%r locale=%r",
                c.keyword, c.geo, c.device_type, c.locale,
            )
            continue
        seen.add(key)
        out.append(c)
    return out


def filter_due_configs(
    configs: List[RankCheckConfig], storage: Storage
) -> Tuple[List[RankCheckConfig], List[Tuple[RankCheckConfig, str]]]:
    """Return (due_configs, skipped_with_reason).

    Skip rules:
      * active=False  -> "inactive"
      * frequency in PAUSED -> "paused"
      * last check within frequency window -> "checked Xh ago, frequency=Y"
    """
    due: List[RankCheckConfig] = []
    skipped: List[Tuple[RankCheckConfig, str]] = []
    now = datetime.now(timezone.utc)
    for cfg in configs:
        if not cfg.active:
            skipped.append((cfg, "inactive"))
            continue
        if cfg.frequency in PAUSED_FREQUENCIES:
            skipped.append((cfg, "paused"))
            continue
        window = FREQUENCY_WINDOWS.get(cfg.frequency, FREQUENCY_WINDOWS["daily"])
        last = storage.latest_result_for(
            cfg.keyword, cfg.geo, cfg.device_type, cfg.locale,
        )
        if last and last.get("timestamp"):
            try:
                ts = datetime.fromisoformat(last["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = now - ts
                if age < window:
                    hours = age.total_seconds() / 3600.0
                    skipped.append(
                        (cfg, f"checked {hours:.1f}h ago, frequency={cfg.frequency}")
                    )
                    continue
            except (TypeError, ValueError):
                pass
        due.append(cfg)
    return due, skipped


class Alerter:
    """Compare current vs. previous rank, send notifications when needed.

    Threshold is treated as a config knob, not a hardcoded constant – a 3-position
    drop matters for a head term but is noise for a long-tail keyword.
    """

    def __init__(
        self,
        threshold: int = 3,
        webhook_url: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        email_from: Optional[str] = None,
        email_to: Optional[str] = None,
    ):
        self.threshold = threshold
        self.webhook_url = webhook_url
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.email_from = email_from
        self.email_to = email_to

    @classmethod
    def from_env(cls, threshold: int = 3) -> "Alerter":
        return cls(
            threshold=threshold,
            webhook_url=os.environ.get("ALERT_WEBHOOK_URL"),
            smtp_host=os.environ.get("SMTP_HOST"),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER"),
            smtp_password=os.environ.get("SMTP_PASSWORD"),
            email_from=os.environ.get("ALERT_EMAIL_FROM"),
            email_to=os.environ.get("ALERT_EMAIL_TO"),
        )

    def evaluate(self, current: RankResult, previous: Optional[int]) -> Optional[str]:
        cur = current.organic_position
        ctx = f"{current.location} / {current.device} / {current.locale}"
        if cur is None and previous is None:
            return None
        if previous is None:
            return (f"[NEW] '{current.keyword}' entered SERP at position {cur} "
                    f"({ctx}).")
        if cur is None:
            return (f"[DROPPED OUT] '{current.keyword}' fell out of tracked SERP "
                    f"(was {previous}) ({ctx}).")
        delta = cur - previous
        if abs(delta) < self.threshold:
            return None
        direction = "DROPPED" if delta > 0 else "IMPROVED"
        return (f"[{direction}] '{current.keyword}' moved {previous} -> {cur} "
                f"({ctx}).")

    def send(self, message: str) -> List[str]:
        """Deliver `message` to every configured channel.

        Returns a list of delivery statuses ("webhook: ok", "smtp: ok",
        "webhook: HTTP 404", etc) – useful for `test-alert` and for
        operators tailing logs. An empty list means no channels are
        configured at all; the caller logs the message in that case.
        """
        results: List[str] = []
        if self.webhook_url:
            results.append(self._send_webhook(message))
        if (self.smtp_host and self.smtp_user and self.smtp_password
                and self.email_from and self.email_to):
            results.append(self._send_smtp(message))
        if not results:
            logger.info("ALERT (no channel configured): %s", message)
        return results

    def _send_webhook(self, message: str) -> str:
        url = self.webhook_url or ""
        payload = _webhook_payload(url, message)
        try:
            resp = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as exc:
            status = f"webhook: transport error ({exc.__class__.__name__})"
            logger.warning("Webhook alert failed: %s", exc)
            return status

        # Discord returns 204 on success, Slack returns 200, Teams returns 200.
        # Anything non-2xx is a delivery failure – surface it loudly.
        if 200 <= resp.status_code < 300:
            return "webhook: ok"
        body_preview = resp.text[:200].replace("\n", " ")
        logger.warning(
            "Webhook returned HTTP %d – alert NOT delivered. Body: %s",
            resp.status_code, body_preview,
        )
        return f"webhook: HTTP {resp.status_code}"

    def _send_smtp(self, message: str) -> str:
        try:
            # utf-8 explicitly so heartbeat arrows (↑ ↓ →) and any non-ASCII
            # keyword characters don't crash MIMEText's default ASCII encoder.
            mime = MIMEText(message, "plain", "utf-8")
            mime["Subject"] = "Rank tracker alert"
            mime["From"] = self.email_from or ""
            mime["To"] = self.email_to or ""
            with smtplib.SMTP(self.smtp_host or "", self.smtp_port) as smtp:
                smtp.starttls()
                smtp.login(self.smtp_user or "", self.smtp_password or "")
                smtp.sendmail(
                    self.email_from or "",
                    [self.email_to or ""],
                    mime.as_string(),
                )
            return "smtp: ok"
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("SMTP alert failed: %s", exc)
            return f"smtp: {exc.__class__.__name__}"


# Public so it can be unit-tested directly.
def _webhook_payload(webhook_url: str, message: str) -> Dict[str, Any]:
    """Pick the right payload schema for the destination.

    Slack incoming webhooks expect `{"text": ...}`.
    Discord webhooks expect `{"content": ...}`.
    Microsoft Teams accepts `{"text": ...}` for simple connector cards.
    Generic / unknown hosts get `{"text": ...}` as the safest default.
    """
    host = ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(webhook_url).netloc or "").lower()
    except Exception:
        pass
    if "discord" in host:
        return {"content": message}
    return {"text": message}


class BatchRunner:
    def __init__(
        self,
        fetcher: DecodoFetcher,
        storage: Storage,
        alerter: Optional[Alerter] = None,
        delay_seconds: float = 1.0,
        concurrency: int = 1,
    ):
        self.fetcher = fetcher
        self.storage = storage
        self.alerter = alerter
        self.delay_seconds = delay_seconds
        self.concurrency = max(1, concurrency)
        # SQLite serializes writes via its own file lock, but multiple Python
        # threads must not share a single sqlite3.Connection. Storage opens a
        # new connection per call, so we add our own writer lock to keep log
        # output and previous_position/save_result ordering deterministic.
        self._write_lock = threading.Lock()

    def run(self, configs: List[RankCheckConfig], notes: str = "") -> int:
        run_id = self.storage.start_run(notes=notes)
        self.fetcher.reset_call_count()

        due, skipped = filter_due_configs(configs, self.storage)
        if skipped:
            logger.info(
                "Run %s – skipping %d of %d (active/frequency rules)",
                run_id, len(skipped), len(configs),
            )
            for cfg, reason in skipped:
                logger.debug("  skip %r @ %s/%s: %s",
                             cfg.keyword, cfg.geo, cfg.device_type, reason)

        logger.info(
            "Run %s started – %d check(s) due, concurrency=%d",
            run_id, len(due), self.concurrency,
        )
        if self.concurrency == 1:
            self._run_sequential(run_id, due)
        else:
            self._run_concurrent(run_id, due)
        api_calls = self.fetcher.api_call_count
        self.storage.record_api_calls(run_id, api_calls)
        self.storage.finish_run(run_id)
        lifetime = self.storage.lifetime_api_calls()
        logger.info(
            "Run %s complete – %d API call(s) this run, %d lifetime "
            "(%d skipped by frequency rules)",
            run_id, api_calls, lifetime, len(skipped),
        )
        return run_id

    def _run_sequential(self, run_id: int, configs: List[RankCheckConfig]) -> None:
        for i, cfg in enumerate(configs, start=1):
            self._check_one(run_id, cfg, i, len(configs))
            if i < len(configs) and self.delay_seconds > 0:
                time.sleep(self.delay_seconds)

    def _run_concurrent(self, run_id: int, configs: List[RankCheckConfig]) -> None:
        total = len(configs)
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self._check_one, run_id, cfg, idx, total): cfg
                for idx, cfg in enumerate(configs, start=1)
            }
            for fut in as_completed(futures):
                exc = fut.exception()
                if exc:
                    logger.error("Worker raised: %s", exc)

    def _check_one(
        self,
        run_id: int,
        cfg: RankCheckConfig,
        index: int,
        total: int,
    ) -> None:
        try:
            result = self.fetcher.fetch(cfg)
        except Exception as exc:
            logger.exception("Check failed for %r: %s", cfg.keyword, exc)
            return
        with self._write_lock:
            previous = self.storage.previous_position(
                keyword=cfg.keyword,
                location=cfg.geo,
                device=cfg.device_type,
                locale=cfg.locale,
                before_run_id=run_id,
            )
            self.storage.save_result(run_id, result)
        logger.info(
            "[%d/%d] %r @ %s/%s -> pos=%s prev=%s score=%s "
            "(AI-cited=%s FS-owned=%s PAA=%s)",
            index, total,
            cfg.keyword, cfg.geo, cfg.device_type,
            result.organic_position, previous, visibility_score(result),
            result.ai_overview_cited,
            result.featured_snippet_owned,
            result.paa_present,
        )
        if self.alerter:
            message = self.alerter.evaluate(result, previous)
            if message:
                self.alerter.send(message)


def build_run_heartbeat(
    storage: Storage,
    run_id: int,
    alert_threshold: int = 3,
    top_n_movers: int = 5,
) -> str:
    """Compose a daily/run heartbeat message.

    Includes count of checks + failures + top movers (where the position
    changed by >= alert_threshold vs the prior run) + lifetime API total.
    """
    with storage.connect() as conn:
        run = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM rank_results WHERE run_id = ?", (run_id,)
        ).fetchall()
    if run is None:
        return f"Rank tracker run {run_id}: not found."
    run_d = dict(run)
    rows_d = [dict(r) for r in rows]

    movers: List[Tuple[str, str, Optional[int], Optional[int]]] = []
    for r in rows_d:
        prev = storage.previous_result_for(
            r["keyword"], r["location"], r["device"], r["locale"], before_id=r["id"]
        )
        prev_pos = prev["organic_position"] if prev else None
        cur_pos = r["organic_position"]
        if cur_pos is None and prev_pos is None:
            continue
        if prev_pos is None or cur_pos is None:
            movers.append((r["keyword"], f"{r['location']}/{r['device']}",
                           prev_pos, cur_pos))
            continue
        if abs(cur_pos - prev_pos) >= alert_threshold:
            movers.append((r["keyword"], f"{r['location']}/{r['device']}",
                           prev_pos, cur_pos))

    movers.sort(key=lambda m: abs((m[3] or 100) - (m[2] or 100)), reverse=True)
    lifetime = storage.lifetime_api_calls()
    api_calls = run_d.get("api_calls", 0)

    lines = [
        f"Rank tracker heartbeat – run #{run_id}",
        f"• {len(rows_d)} check(s) saved; {api_calls} API call(s) this run "
        f"(lifetime {lifetime}).",
    ]
    if not movers:
        lines.append(f"• No keywords moved by ≥{alert_threshold} positions.")
    else:
        lines.append(f"• Top movers (≥{alert_threshold} positions):")
        for kw, ctx, prev_pos, cur_pos in movers[:top_n_movers]:
            if prev_pos is None:
                arrow = "NEW"
            elif cur_pos is None:
                arrow = "DROPPED OUT"
            elif cur_pos < prev_pos:
                arrow = f"{prev_pos}→{cur_pos} ↑"
            else:
                arrow = f"{prev_pos}→{cur_pos} ↓"
            lines.append(f"   - '{kw}' ({ctx}): {arrow}")
        if len(movers) > top_n_movers:
            lines.append(f"   ...and {len(movers) - top_n_movers} more.")
    return "\n".join(lines)

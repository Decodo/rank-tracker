"""CLI entry point for the rank tracker.

Usage examples:

    python main.py check --keywords keywords.csv --alerts
    python main.py schedule --keywords keywords.csv --every 86400 --alerts
    python main.py export --format csv --output results.csv
    python main.py export --format json --output results.json --run-id 3
    python main.py history --keyword "best laptop 2026"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

from datetime import datetime, timezone

from fetcher import (
    DecodoAPIError,
    DecodoCredentialError,
    DecodoFetcher,
)
from models import RankCheckConfig
from report import render_competitors, render_html, render_summary, render_trend
from scheduler import (
    Alerter,
    BatchRunner,
    build_run_heartbeat,
    load_keywords_csv,
)
from storage import Storage

log = logging.getLogger("rank-tracker")


def _build_runner(args: argparse.Namespace):
    fetcher = DecodoFetcher()
    storage = Storage(args.db)
    alerter = Alerter.from_env(threshold=args.threshold) if args.alerts else None
    configs = load_keywords_csv(args.keywords)
    runner = BatchRunner(
        fetcher,
        storage,
        alerter=alerter,
        delay_seconds=args.delay,
        concurrency=args.concurrency,
    )
    return runner, configs


def _maybe_send_heartbeat(
    runner: BatchRunner, run_id: int, threshold: int
) -> None:
    """Post a per-run summary to the alerter if one is configured.

    Heartbeat is intentionally cheap to read: total checks, API calls,
    and the top movers. Silence in your Slack/email after enabling this
    is a real signal that the tracker has stopped, not a guess.
    """
    if not runner.alerter:
        return
    msg = build_run_heartbeat(runner.storage, run_id, alert_threshold=threshold)
    statuses = runner.alerter.send(msg)
    if statuses:
        log.info("Heartbeat → %s", ", ".join(statuses))


def cmd_check(args: argparse.Namespace) -> int:
    runner, configs = _build_runner(args)
    if not configs:
        log.error("No keyword configs found in %s", args.keywords)
        return 2
    run_id = runner.run(configs, notes=args.notes or "")
    _maybe_send_heartbeat(runner, run_id, args.threshold)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    runner, configs = _build_runner(args)
    if not configs:
        log.error("No keyword configs found in %s", args.keywords)
        return 2
    log.info("Scheduled mode: running every %d seconds. Ctrl+C to stop.", args.every)
    while True:
        try:
            run_id = runner.run(configs, notes="scheduled")
            _maybe_send_heartbeat(runner, run_id, args.threshold)
        except Exception:
            log.exception("Scheduled run failed")
        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            log.info("Scheduler interrupted.")
            return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    """Send a sample alert through every configured channel and report status."""
    alerter = Alerter.from_env(threshold=0)
    if not alerter.webhook_url and not (
        alerter.smtp_host and alerter.email_from and alerter.email_to
    ):
        print("[FAIL]   No alerting channel configured. Set ALERT_WEBHOOK_URL "
              "and/or SMTP_HOST + ALERT_EMAIL_FROM + ALERT_EMAIL_TO in .env.")
        return 2

    msg = args.message or (
        "[TEST] Rank tracker alert delivery check. "
        "If you can read this, alerting is wired up correctly."
    )
    statuses = alerter.send(msg)
    if not statuses:
        # send() already logged the no-channel case.
        return 2

    ok = True
    for status in statuses:
        marker = "[OK]    " if status.endswith(": ok") else "[FAIL]  "
        if not status.endswith(": ok"):
            ok = False
        print(f"{marker} {status}")
    print("---")
    if ok:
        print("Alert delivered to every configured channel.")
        return 0
    print("One or more channels failed. Check the warnings above the divider.")
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Smoke-test the environment: credentials, API reachability, DB writability."""
    ok = True

    if os.environ.get("DECODO_AUTH"):
        print("[OK]     DECODO_AUTH is set")
    else:
        print("[FAIL]   DECODO_AUTH is not set. Copy .env.example to .env "
              "and fill it in.")
        ok = False

    try:
        storage = Storage(args.db)
        with storage.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        print(f"[OK]     Database is writable at {args.db}")
    except Exception as exc:
        print(f"[FAIL]   Database error at {args.db}: {exc}")
        ok = False

    if ok and not args.skip_api:
        try:
            fetcher = DecodoFetcher(timeout=30, max_retries=1)
            result = fetcher.fetch(RankCheckConfig(
                keyword="decodo",
                target_domain="decodo.com",
                geo="United States",
                locale="en-us",
            ))
            print(
                f"[OK]     Decodo API reachable – got {result.raw_organic_count} "
                f"organic results for 'decodo' (decodo.com pos={result.organic_position})"
            )
        except DecodoCredentialError as exc:
            print(f"[FAIL]   Credentials rejected: {exc}")
            ok = False
        except DecodoAPIError as exc:
            print(f"[FAIL]   Decodo API error: {exc}")
            ok = False
        except Exception as exc:
            print(f"[FAIL]   Unexpected error talking to Decodo: {exc!r}")
            ok = False

    # Staleness / heartbeat – flag if no successful run in a while.
    try:
        storage = Storage(args.db)
        latest = storage.latest_finished_run()
        if latest is None:
            print("[INFO]   No completed runs yet – schedule a check.")
        else:
            finished = latest.get("finished_at")
            try:
                ts = datetime.fromisoformat(finished)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - ts
                hours = age.total_seconds() / 3600.0
                if hours > args.max_staleness_hours:
                    print(
                        f"[FAIL]   Last successful run was {hours:.1f}h ago "
                        f"(> {args.max_staleness_hours}h threshold). "
                        "Tracker may have stopped."
                    )
                    ok = False
                else:
                    print(
                        f"[OK]     Last successful run {hours:.1f}h ago "
                        f"({finished})"
                    )
            except (TypeError, ValueError):
                print(f"[INFO]   Last run finished_at is unparseable: {finished!r}")

        lifetime = storage.lifetime_api_calls()
        print(f"[INFO]   Lifetime Decodo API calls: {lifetime}")
    except Exception as exc:
        print(f"[FAIL]   Staleness check failed: {exc}")
        ok = False

    print("---")
    if ok:
        print("All checks passed. You're ready to run `python main.py check`.")
        return 0
    print("Some checks failed. See messages above.")
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    if args.format == "csv":
        n = storage.export_csv(args.output, run_id=args.run_id)
    else:
        n = storage.export_json(args.output, run_id=args.run_id)
    if n == 0:
        log.warning(
            "Wrote 0 row(s) to %s – %s",
            args.output,
            f"no rows for run-id {args.run_id}" if args.run_id else "database is empty",
        )
        return 3
    log.info("Wrote %d row(s) to %s", n, args.output)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    rows = storage.history(keyword=args.keyword, limit=args.limit)
    if not rows:
        print("(no history)")
        return 0
    for r in rows:
        print(
            f"[{r['timestamp']}] {r['keyword']!r} @ {r['location']}/{r['device']}/{r['locale']}"
            f"  pos={r['organic_position']}"
            f"  AI-cited={bool(r['ai_overview_cited'])}"
            f"  FS-owned={bool(r['featured_snippet_owned'])}"
            f"  PAA={bool(r['paa_present'])}"
        )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    if not args.yes:
        print(
            f"This will permanently delete runs older than {args.older_than_days} days "
            f"from {args.db}. Re-run with --yes to confirm."
        )
        return 2
    stats = storage.prune(older_than_days=args.older_than_days)
    log.info(
        "Pruned %d run(s) and %d result row(s) older than %s. DB vacuumed.",
        stats["runs_deleted"], stats["results_deleted"], stats["cutoff"],
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    if args.html:
        with open(args.html, "w") as fh:
            fh.write(render_html(storage))
        log.info("HTML report written to %s", args.html)
        return 0
    if args.keyword:
        print(render_trend(storage, args.keyword, limit=args.limit))
    else:
        print(render_summary(storage, location=args.location))
    return 0


def cmd_competitors(args: argparse.Namespace) -> int:
    storage = Storage(args.db)
    print(render_competitors(
        storage,
        keyword=args.keyword,
        location=args.location,
        device=args.device,
        locale=args.locale,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rank-tracker",
        description="SEO rank tracker built on the Decodo SERP Scraping API.",
    )
    p.add_argument(
        "--db",
        default=os.environ.get("RANK_DB", "rank_tracker.db"),
        help="SQLite database path (default: rank_tracker.db).",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common_run_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--keywords", default="keywords.csv")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="Seconds between sequential API calls.")
        parser.add_argument("--threshold", type=int, default=3,
                            help="Alert threshold in positions (default 3).")
        parser.add_argument("--alerts", action="store_true",
                            help="Send alerts via webhook/SMTP configured in env.")
        parser.add_argument("--concurrency", type=int, default=1,
                            help="Parallel API calls (default 1, sequential).")

    chk = sub.add_parser("check", help="Run a single batch of checks.")
    _common_run_args(chk)
    chk.add_argument("--notes", default="")
    chk.set_defaults(func=cmd_check)

    sch = sub.add_parser("schedule", help="Run checks on a fixed interval.")
    _common_run_args(sch)
    sch.add_argument("--every", type=int, default=86400,
                     help="Seconds between runs (default 86400 = daily).")
    sch.set_defaults(func=cmd_schedule)

    exp = sub.add_parser("export", help="Export stored results.")
    exp.add_argument("--format", choices=("csv", "json"), default="csv")
    exp.add_argument("--output", required=True)
    exp.add_argument("--run-id", type=int, default=None,
                     help="Filter to a single run (default: all runs).")
    exp.set_defaults(func=cmd_export)

    his = sub.add_parser("history", help="Show recent stored results.")
    his.add_argument("--keyword", default=None)
    his.add_argument("--limit", type=int, default=20)
    his.set_defaults(func=cmd_history)

    rep = sub.add_parser(
        "report",
        help="Print a summary table, trend, or HTML report.",
    )
    rep.add_argument("--keyword", default=None,
                     help="Show chronological trend for one keyword.")
    rep.add_argument("--location", default=None,
                     help="Filter the summary to one geo.")
    rep.add_argument("--limit", type=int, default=30,
                     help="Trend rows to show when --keyword is set.")
    rep.add_argument("--html", default=None,
                     help="Write a self-contained HTML report to PATH.")
    rep.set_defaults(func=cmd_report)

    comp = sub.add_parser(
        "competitors",
        help="Show top-N organic results for a keyword + diff vs prior run.",
    )
    comp.add_argument("--keyword", required=True,
                      help="Keyword to inspect.")
    comp.add_argument("--location", default=None,
                      help="Filter to a specific geo.")
    comp.add_argument("--device", default=None,
                      help="Filter to a specific device.")
    comp.add_argument("--locale", default=None,
                      help="Filter to a specific locale.")
    comp.set_defaults(func=cmd_competitors)

    doc = sub.add_parser(
        "doctor",
        help="Verify credentials, API reachability, DB, and run freshness.",
    )
    doc.add_argument("--skip-api", action="store_true",
                     help="Skip the live Decodo call.")
    doc.add_argument("--max-staleness-hours", type=float, default=48.0,
                     help="Hours since last run before warning (default 48).")
    doc.set_defaults(func=cmd_doctor)

    ta = sub.add_parser(
        "test-alert",
        help="Send a sample alert through every configured channel.",
    )
    ta.add_argument("--message", default=None,
                    help="Custom test message (default: a recognisable test string).")
    ta.set_defaults(func=cmd_test_alert)

    pr = sub.add_parser(
        "prune",
        help="Delete runs (and their results) older than N days, then VACUUM.",
    )
    pr.add_argument("--older-than-days", type=int, default=365,
                    help="Cutoff in days (default 365).")
    pr.add_argument("--yes", action="store_true",
                    help="Actually do it. Without this, prune is a dry-run that prints what would happen.")
    pr.set_defaults(func=cmd_prune)

    return p


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    try:
        return args.func(args)
    except DecodoCredentialError as exc:
        log.error("%s", exc)
        return 4
    except FileNotFoundError as exc:
        log.error("File not found: %s", exc)
        return 5
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

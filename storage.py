"""SQLite storage for rank tracker runs.

Two tables:
- `runs` groups a batch of checks (one row per scheduler tick).
- `rank_results` holds one row per (keyword, location, device, locale, run).

`run_id` lets us compare the current position against any previous run for the
same (keyword, location, device, locale) tuple – the basis for change alerts.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional

from models import RankResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    notes TEXT,
    api_calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rank_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    organic_position INTEGER,
    matched_url TEXT,
    featured_snippet INTEGER NOT NULL DEFAULT 0,
    featured_snippet_owned INTEGER NOT NULL DEFAULT 0,
    ai_overview_present INTEGER NOT NULL DEFAULT 0,
    ai_overview_cited INTEGER NOT NULL DEFAULT 0,
    ai_overview_citation_rank INTEGER,
    ai_overview_citations_json TEXT,
    paa_present INTEGER NOT NULL DEFAULT 0,
    paa_question_count INTEGER NOT NULL DEFAULT 0,
    local_pack_present INTEGER NOT NULL DEFAULT 0,
    knowledge_panel_present INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    device TEXT,
    locale TEXT,
    total_results INTEGER,
    serp_url TEXT,
    raw_organic_count INTEGER NOT NULL DEFAULT 0,
    top_results_json TEXT,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rank_results_keyword ON rank_results(keyword);
CREATE INDEX IF NOT EXISTS idx_rank_results_run ON rank_results(run_id);
CREATE INDEX IF NOT EXISTS idx_rank_results_dim
    ON rank_results(keyword, location, device, locale);
"""


# Each entry: (table, column, ddl-fragment). Run on every open so existing
# DBs gain new columns without a manual migration step.
_MIGRATIONS = [
    ("rank_results", "top_results_json", "TEXT"),
    ("runs", "api_calls", "INTEGER NOT NULL DEFAULT 0"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str = "rank_tracker.db"):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            _apply_migrations(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(self, notes: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, notes) VALUES (?, ?)",
                (_now_iso(), notes),
            )
            run_id = cur.lastrowid
            assert run_id is not None
            return run_id

    def finish_run(self, run_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ? WHERE id = ?",
                (_now_iso(), run_id),
            )

    def save_result(self, run_id: int, r: RankResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO rank_results (
                    run_id, keyword, target_domain, organic_position, matched_url,
                    featured_snippet, featured_snippet_owned,
                    ai_overview_present, ai_overview_cited, ai_overview_citation_rank,
                    ai_overview_citations_json,
                    paa_present, paa_question_count,
                    local_pack_present, knowledge_panel_present,
                    location, device, locale,
                    total_results, serp_url, raw_organic_count, top_results_json, timestamp
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id, r.keyword, r.target_domain, r.organic_position, r.matched_url,
                    int(r.featured_snippet), int(r.featured_snippet_owned),
                    int(r.ai_overview_present), int(r.ai_overview_cited), r.ai_overview_citation_rank,
                    json.dumps([c.model_dump() for c in r.ai_overview_citations]),
                    int(r.paa_present), r.paa_question_count,
                    int(r.local_pack_present), int(r.knowledge_panel_present),
                    r.location, r.device, r.locale,
                    r.total_results, r.serp_url, r.raw_organic_count,
                    json.dumps([c.model_dump() for c in r.top_results]),
                    r.timestamp.isoformat(timespec="seconds"),
                ),
            )

    def record_api_calls(self, run_id: int, api_calls: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET api_calls = ? WHERE id = ?",
                (api_calls, run_id),
            )

    def lifetime_api_calls(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(api_calls), 0) AS total FROM runs"
            ).fetchone()
            return int(row["total"]) if row else 0

    def latest_finished_run(self) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE finished_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def latest_result_for(
        self, keyword: str, location: str, device: str, locale: str
    ) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rank_results WHERE keyword=? AND location=? "
                "AND device=? AND locale=? ORDER BY id DESC LIMIT 1",
                (keyword, location, device, locale),
            ).fetchone()
            return dict(row) if row else None

    def previous_result_for(
        self,
        keyword: str,
        location: str,
        device: str,
        locale: str,
        before_id: int,
    ) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rank_results WHERE keyword=? AND location=? "
                "AND device=? AND locale=? AND id < ? "
                "ORDER BY id DESC LIMIT 1",
                (keyword, location, device, locale, before_id),
            ).fetchone()
            return dict(row) if row else None

    def previous_position(
        self,
        keyword: str,
        location: str,
        device: str,
        locale: str,
        before_run_id: int,
    ) -> Optional[int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT organic_position FROM rank_results
                WHERE keyword = ? AND location = ? AND device = ? AND locale = ?
                  AND run_id < ?
                ORDER BY run_id DESC
                LIMIT 1
                """,
                (keyword, location, device, locale, before_run_id),
            ).fetchone()
            return row["organic_position"] if row else None

    def history(self, keyword: Optional[str] = None, limit: int = 50) -> List[dict]:
        with self.connect() as conn:
            if keyword:
                rows = conn.execute(
                    "SELECT * FROM rank_results WHERE keyword = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (keyword, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rank_results ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def export_csv(self, output_path: str, run_id: Optional[int] = None) -> int:
        rows = self._fetch_for_export(run_id)
        if not rows:
            return 0
        with open(output_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def export_json(self, output_path: str, run_id: Optional[int] = None) -> int:
        rows = self._fetch_for_export(run_id)
        if not rows:
            return 0
        with open(output_path, "w") as fh:
            json.dump(rows, fh, indent=2, default=str)
        return len(rows)

    def prune(self, older_than_days: int) -> dict:
        """Delete runs (and cascading rank_results) older than N days.

        Returns a small dict of counts so the caller can show "deleted X rows
        from Y runs". Uses ON DELETE CASCADE on the foreign key so dropping
        runs takes the related rank_results with them.

        Also runs VACUUM after the delete to reclaim file space – important
        for SQLite, which otherwise leaves the file at its peak size.
        """
        if older_than_days <= 0:
            raise ValueError("older_than_days must be > 0")
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self.connect() as conn:
            old_runs = conn.execute(
                "SELECT id FROM runs WHERE started_at < ?", (cutoff,)
            ).fetchall()
            run_ids = [r["id"] for r in old_runs]
            if not run_ids:
                return {"runs_deleted": 0, "results_deleted": 0, "cutoff": cutoff}
            placeholders = ",".join("?" * len(run_ids))
            cur = conn.execute(
                f"SELECT COUNT(*) AS n FROM rank_results WHERE run_id IN ({placeholders})",
                run_ids,
            ).fetchone()
            results_count = cur["n"]
            conn.execute(
                f"DELETE FROM rank_results WHERE run_id IN ({placeholders})",
                run_ids,
            )
            conn.execute(
                f"DELETE FROM runs WHERE id IN ({placeholders})",
                run_ids,
            )
        # VACUUM has to run outside an explicit transaction.
        with sqlite3.connect(self.path) as conn:
            conn.execute("VACUUM")
        return {
            "runs_deleted": len(run_ids),
            "results_deleted": results_count,
            "cutoff": cutoff,
        }

    def _fetch_for_export(self, run_id: Optional[int]) -> List[dict]:
        with self.connect() as conn:
            if run_id is not None:
                rows = conn.execute(
                    "SELECT * FROM rank_results WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rank_results ORDER BY id"
                ).fetchall()
        return [dict(r) for r in rows]

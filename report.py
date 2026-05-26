"""Terminal + HTML reports across stored rank_results.

`render_summary` – latest snapshot per (keyword, location, device, locale),
including visibility score and delta vs. previous run.
`render_trend` – multi-run history for a single keyword.
`render_competitors` – top-N organic SERP for a keyword + diff vs. previous run.
`render_html` – single self-contained HTML report (summary + trends + competitors).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from scoring import visibility_score_from_row
from storage import Storage


def _fmt_pos(p) -> str:
    return "–" if p is None else str(p)


def _fmt_delta(curr, prev) -> str:
    if curr is None and prev is None:
        return ""
    if prev is None:
        return "NEW"
    if curr is None:
        return "GONE"
    delta = curr - prev
    if delta == 0:
        return "0"
    sign = "+" if delta > 0 else ""
    # In SERPs, *higher position number = worse*, so positive delta is a drop.
    return f"{sign}{delta} ({'↓' if delta > 0 else '↑'})"


def render_summary(storage: Storage, location: Optional[str] = None) -> str:
    """One row per tracked (keyword, location, device, locale) – the most recent
    observation, score, and movement vs. the previous run."""
    with storage.connect() as conn:
        rows = conn.execute(
            """
            SELECT r1.*
            FROM rank_results r1
            JOIN (
                SELECT keyword, location, device, locale, MAX(id) AS max_id
                FROM rank_results
                GROUP BY keyword, location, device, locale
            ) latest
              ON r1.id = latest.max_id
            ORDER BY r1.keyword, r1.location, r1.device, r1.locale
            """
        ).fetchall()
        latest = [dict(r) for r in rows]

    if location:
        latest = [r for r in latest if r["location"] == location]
    if not latest:
        return "(no data)"

    output: List[str] = []
    header = (
        f"{'Keyword':<32} {'Geo/Device':<28} {'Pos':>4} {'Δ vs prev':>14} "
        f"{'Score':>7} {'AI':>4} {'FS':>4} {'PAA':>5}"
    )
    output.append(header)
    output.append("-" * len(header))

    with storage.connect() as conn:
        for row in latest:
            prev_row = conn.execute(
                """
                SELECT organic_position FROM rank_results
                WHERE keyword=? AND location=? AND device=? AND locale=?
                  AND id < ?
                ORDER BY id DESC LIMIT 1
                """,
                (row["keyword"], row["location"], row["device"],
                 row["locale"], row["id"]),
            ).fetchone()
            prev_pos = prev_row["organic_position"] if prev_row else None
            ctx = f"{row['location']}/{row['device']}"
            output.append(
                f"{row['keyword'][:31]:<32} {ctx[:27]:<28} "
                f"{_fmt_pos(row['organic_position']):>4} "
                f"{_fmt_delta(row['organic_position'], prev_pos):>14} "
                f"{visibility_score_from_row(row):>7} "
                f"{'✓' if row['ai_overview_cited'] else '·':>4} "
                f"{'✓' if row['featured_snippet_owned'] else '·':>4} "
                f"{'✓' if row['paa_present'] else '·':>5}"
            )
    return "\n".join(output)


def render_trend(storage: Storage, keyword: str, limit: int = 30) -> str:
    """Chronological history for a single keyword, across runs."""
    with storage.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM rank_results
            WHERE keyword = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (keyword, limit),
        ).fetchall()
        history = [dict(r) for r in rows][::-1]

    if not history:
        return f"(no data for keyword {keyword!r})"

    output: List[str] = [f"Trend for {keyword!r}:"]
    header = (
        f"{'When':<22} {'Geo/Device/Locale':<32} {'Pos':>4} {'Score':>7} "
        f"{'AI':>4} {'FS':>4}"
    )
    output.append(header)
    output.append("-" * len(header))
    for row in history:
        ctx = f"{row['location']}/{row['device']}/{row['locale']}"
        output.append(
            f"{row['timestamp'][:19]:<22} {ctx[:31]:<32} "
            f"{_fmt_pos(row['organic_position']):>4} "
            f"{visibility_score_from_row(row):>7} "
            f"{'✓' if row['ai_overview_cited'] else '·':>4} "
            f"{'✓' if row['featured_snippet_owned'] else '·':>4}"
        )
    return "\n".join(output)


def _parse_competitors(row: dict) -> List[dict]:
    raw = row.get("top_results_json")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _competitor_diff(
    current: List[dict], previous: List[dict]
) -> Dict[str, List[Any]]:
    """Compute entered / dropped / moved sets between two competitor lists.

    Identity = domain (so the same domain ranking with a different URL still
    counts as the same competitor).
    """
    cur_by_domain = {c["domain"]: c for c in current}
    prev_by_domain = {c["domain"]: c for c in previous}

    entered = [cur_by_domain[d] for d in cur_by_domain if d not in prev_by_domain]
    dropped = [prev_by_domain[d] for d in prev_by_domain if d not in cur_by_domain]
    moved: List[Tuple[dict, int, int]] = []
    for d, cur in cur_by_domain.items():
        if d in prev_by_domain:
            prev = prev_by_domain[d]
            if cur["position"] != prev["position"]:
                moved.append((cur, prev["position"], cur["position"]))

    return {"entered": entered, "dropped": dropped, "moved": moved}


def render_competitors(
    storage: Storage,
    keyword: str,
    location: Optional[str] = None,
    device: Optional[str] = None,
    locale: Optional[str] = None,
) -> str:
    """Show the latest top-N competitors for a keyword + diff vs. prior run."""
    with storage.connect() as conn:
        query = (
            "SELECT * FROM rank_results WHERE keyword = ? "
            + ("AND location = ? " if location else "")
            + ("AND device = ? " if device else "")
            + ("AND locale = ? " if locale else "")
            + "ORDER BY id DESC LIMIT 2"
        )
        params: List = [keyword]
        if location: params.append(location)
        if device:   params.append(device)
        if locale:   params.append(locale)
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    if not rows:
        return f"(no data for keyword {keyword!r})"

    latest = rows[0]
    previous = rows[1] if len(rows) > 1 else None

    cur_competitors = _parse_competitors(latest)
    prev_competitors = _parse_competitors(previous) if previous else []
    target = latest["target_domain"].lower()

    output: List[str] = []
    output.append(
        f"Keyword: {latest['keyword']}  ({latest['location']}/{latest['device']}/{latest['locale']})"
    )
    output.append(f"Checked: {latest['timestamp']}")
    own_pos = latest["organic_position"]
    output.append(
        f"Your domain ({target}): "
        + (f"position {own_pos}" if own_pos else "not in tracked SERP")
    )
    output.append("")
    output.append("Top organic results:")
    output.append(f"  {'Pos':>3}  {'Domain':<28}  Title")
    output.append("  " + "-" * 70)
    for c in cur_competitors:
        marker = " ← YOU" if c["domain"] == target else ""
        title = (c.get("title") or "")[:50]
        output.append(
            f"  {c['position']:>3}  {c['domain'][:27]:<28}  {title}{marker}"
        )

    if previous and prev_competitors:
        diff = _competitor_diff(cur_competitors, prev_competitors)
        if diff["entered"] or diff["dropped"] or diff["moved"]:
            output.append("")
            output.append(f"Δ vs previous run ({previous['timestamp']}):")
            for c in diff["entered"]:
                output.append(f"  + ENTERED  {c['domain']} at position {c['position']}")
            for c in diff["dropped"]:
                output.append(f"  - DROPPED  {c['domain']} (was at {c['position']})")
            for c, prev_pos, cur_pos in diff["moved"]:
                arrow = "↑" if cur_pos < prev_pos else "↓"
                output.append(
                    f"  {arrow} MOVED    {c['domain']}  {prev_pos} → {cur_pos}"
                )

    return "\n".join(output)


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Rank Tracker Report – {generated}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
          max-width: 1100px; margin: 2rem auto; padding: 0 1rem; line-height: 1.45; }}
  h1 {{ margin-bottom: 0.25rem; }}
  .meta {{ color: #6a737d; font-size: 0.9rem; margin-bottom: 2rem; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 2.5rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #d0d7de; }}
  th {{ background: #f6f8fa; font-weight: 600; font-size: 0.85rem; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pos-1 {{ background: #d4edda; }}
  .pos-2, .pos-3 {{ background: #fef3cd; }}
  .own {{ font-weight: 600; }}
  .own::after {{ content: " ← you"; color: #0969da; font-weight: normal; }}
  .delta-up {{ color: #1a7f37; }}
  .delta-down {{ color: #cf222e; }}
  .badge {{ display: inline-block; padding: 0.05rem 0.4rem; border-radius: 4px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-ai {{ background: #ddf4ff; color: #0969da; }}
  .badge-fs {{ background: #fff8c5; color: #57606a; }}
  .keyword-section {{ margin-bottom: 2.5rem; padding: 1rem; border: 1px solid #d0d7de;
                       border-radius: 6px; }}
  h2 {{ margin-top: 0; font-size: 1.1rem; }}
  details summary {{ cursor: pointer; font-weight: 500; padding: 0.25rem 0; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #c9d1d9; }}
    th {{ background: #161b22; }}
    th, td {{ border-color: #30363d; }}
    .keyword-section {{ border-color: #30363d; }}
    .pos-1 {{ background: rgba(34,134,58,0.2); }}
    .pos-2, .pos-3 {{ background: rgba(212,167,44,0.15); }}
    .badge-ai {{ background: rgba(31,111,235,0.2); color: #58a6ff; }}
    .badge-fs {{ background: rgba(187,128,9,0.2); color: #d29922; }}
  }}
</style>
</head>
<body>
<h1>Rank Tracker Report</h1>
<p class="meta">Generated {generated} · {row_count} tracked tuple(s) · {lifetime_calls} lifetime API call(s)</p>

<h2>Latest snapshot</h2>
<table>
<thead><tr>
  <th>Keyword</th><th>Geo / Device</th><th class="num">Pos</th>
  <th class="num">Δ</th><th class="num">Score</th><th>Features</th>
</tr></thead>
<tbody>
{summary_rows}
</tbody>
</table>

<h2>Per-keyword detail</h2>
{keyword_sections}

</body>
</html>
"""


def _html_summary_row(row: dict, prev_pos) -> str:
    pos = row["organic_position"]
    pos_cls = ""
    if pos == 1:
        pos_cls = "pos-1"
    elif pos in (2, 3):
        pos_cls = "pos-2"
    delta_html = ""
    if pos is None and prev_pos is None:
        delta_html = "–"
    elif prev_pos is None:
        delta_html = "<em>NEW</em>"
    elif pos is None:
        delta_html = '<span class="delta-down">GONE</span>'
    else:
        d = pos - prev_pos
        if d == 0:
            delta_html = "0"
        elif d > 0:
            delta_html = f'<span class="delta-down">+{d} ↓</span>'
        else:
            delta_html = f'<span class="delta-up">{d} ↑</span>'

    badges = []
    if row["ai_overview_cited"]:
        badges.append('<span class="badge badge-ai">AI</span>')
    if row["featured_snippet_owned"]:
        badges.append('<span class="badge badge-fs">FS</span>')
    badge_html = " ".join(badges) or "–"

    return (
        f'<tr class="{pos_cls}">'
        f'<td>{html.escape(row["keyword"])}</td>'
        f'<td>{html.escape(row["location"])} / {html.escape(row["device"])}</td>'
        f'<td class="num">{pos if pos is not None else "–"}</td>'
        f'<td class="num">{delta_html}</td>'
        f'<td class="num">{visibility_score_from_row(row):.1f}</td>'
        f'<td>{badge_html}</td>'
        f'</tr>'
    )


def _html_competitor_section(row: dict) -> str:
    competitors = _parse_competitors(row)
    if not competitors:
        return ""
    target = row["target_domain"].lower()
    rows = []
    for c in competitors:
        own = c["domain"] == target
        cls = "own" if own else ""
        title = html.escape((c.get("title") or "")[:80])
        rows.append(
            f'<tr><td class="num">{c["position"]}</td>'
            f'<td class="{cls}">{html.escape(c["domain"])}</td>'
            f'<td>{title}</td></tr>'
        )
    return (
        '<details><summary>Top organic results</summary>'
        '<table><thead><tr><th class="num">Pos</th>'
        '<th>Domain</th><th>Title</th></tr></thead><tbody>'
        + "".join(rows) +
        '</tbody></table></details>'
    )


def render_html(storage: Storage) -> str:
    """Build a single self-contained HTML report."""
    with storage.connect() as conn:
        latest_rows = conn.execute(
            """
            SELECT r1.*
            FROM rank_results r1
            JOIN (
                SELECT keyword, location, device, locale, MAX(id) AS max_id
                FROM rank_results
                GROUP BY keyword, location, device, locale
            ) latest ON r1.id = latest.max_id
            ORDER BY r1.keyword, r1.location, r1.device, r1.locale
            """
        ).fetchall()
        latest = [dict(r) for r in latest_rows]

    summary_rows = []
    keyword_sections = []
    with storage.connect() as conn:
        for row in latest:
            prev = conn.execute(
                "SELECT organic_position FROM rank_results "
                "WHERE keyword=? AND location=? AND device=? AND locale=? "
                "AND id < ? ORDER BY id DESC LIMIT 1",
                (row["keyword"], row["location"], row["device"],
                 row["locale"], row["id"]),
            ).fetchone()
            prev_pos = prev["organic_position"] if prev else None
            summary_rows.append(_html_summary_row(row, prev_pos))

            comp_html = _html_competitor_section(row)
            ctx = f"{row['location']} / {row['device']} / {row['locale']}"
            keyword_sections.append(
                f'<div class="keyword-section">'
                f'<h2>{html.escape(row["keyword"])}</h2>'
                f'<p class="meta">{html.escape(ctx)} · target: '
                f'<code>{html.escape(row["target_domain"])}</code> · '
                f'checked {html.escape(row["timestamp"][:19])}</p>'
                f'{comp_html}'
                f'</div>'
            )

    return _HTML_TEMPLATE.format(
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        row_count=len(latest),
        lifetime_calls=storage.lifetime_api_calls(),
        summary_rows="\n".join(summary_rows) or '<tr><td colspan="6">(no data)</td></tr>',
        keyword_sections="\n".join(keyword_sections) or "<p>(no data)</p>",
    )

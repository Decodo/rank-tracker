"""Composite SERP visibility score.

A single keyword's score blends organic position with SERP feature ownership.
Position decays sub-linearly (pos 1 = 100, pos 10 ~ 32) so first-page slots
beyond #1 still carry weight. Featured-snippet ownership and AI Overview
citation each add bonuses, because owning those surfaces drives clicks even
without a top organic slot. The score is intentionally bounded – it's a
relative health signal, not a unit of traffic.
"""

from __future__ import annotations

from typing import Optional

from models import RankResult


MAX_POSITION_POINTS = 100.0
FEATURED_SNIPPET_BONUS = 30.0
AI_OVERVIEW_CITED_BONUS = 25.0
PAA_PRESENT_BONUS = 3.0


def visibility_score(result: RankResult) -> float:
    score = 0.0

    pos = result.organic_position
    if pos is not None and pos > 0:
        score += MAX_POSITION_POINTS / (pos ** 0.5)

    if result.featured_snippet_owned:
        score += FEATURED_SNIPPET_BONUS

    if result.ai_overview_cited:
        rank = result.ai_overview_citation_rank or 5
        score += AI_OVERVIEW_CITED_BONUS / (max(rank, 1) ** 0.5)

    if result.paa_present:
        score += PAA_PRESENT_BONUS

    return round(score, 1)


def visibility_score_from_row(row: dict) -> float:
    """Compute the score directly from a `rank_results` SQLite row."""
    pos: Optional[int] = row.get("organic_position")
    score = 0.0
    if pos is not None and pos > 0:
        score += MAX_POSITION_POINTS / (pos ** 0.5)
    if row.get("featured_snippet_owned"):
        score += FEATURED_SNIPPET_BONUS
    if row.get("ai_overview_cited"):
        rank = row.get("ai_overview_citation_rank") or 5
        score += AI_OVERVIEW_CITED_BONUS / (max(rank, 1) ** 0.5)
    if row.get("paa_present"):
        score += PAA_PRESENT_BONUS
    return round(score, 1)

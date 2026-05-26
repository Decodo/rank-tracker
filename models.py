"""Typed data models for the rank tracker.

`RankCheckConfig` describes *what* to check; `RankResult` describes *what we found*.
Keeping these as Pydantic models means malformed API responses surface as
validation errors instead of silent corruption downstream.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class RankCheckConfig(BaseModel):
    keyword: str
    target_domain: str
    target_url: Optional[str] = None
    search_engine: str = "google"
    locale: str = "en-us"
    google_results_language: str = "en"
    geo: str = "United States"
    device_type: str = "desktop"
    pages: int = 1
    top_n: int = 10  # how many competitor results to capture per check
    active: bool = True  # set False to keep history without paying for new checks
    frequency: str = "daily"  # one of: daily, weekly, monthly, paused


class AIOverviewCitation(BaseModel):
    url: str
    title: str = ""
    source: str = ""
    position: int


class CompetitorEntry(BaseModel):
    """One organic SERP row, kept so we can show 'you vs. who's above you'."""
    position: int
    url: str
    domain: str
    title: str = ""


class RankResult(BaseModel):
    keyword: str
    target_domain: str

    organic_position: Optional[int] = None
    matched_url: Optional[str] = None

    featured_snippet: bool = False
    featured_snippet_owned: bool = False

    ai_overview_present: bool = False
    ai_overview_cited: bool = False
    ai_overview_citation_rank: Optional[int] = None
    ai_overview_citations: List[AIOverviewCitation] = Field(default_factory=list)

    paa_present: bool = False
    paa_question_count: int = 0
    local_pack_present: bool = False
    knowledge_panel_present: bool = False

    location: str = ""
    device: str = ""
    locale: str = ""

    total_results: Optional[int] = None
    serp_url: Optional[str] = None
    raw_organic_count: int = 0

    top_results: List[CompetitorEntry] = Field(default_factory=list)

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

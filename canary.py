"""Schema-drift canary for Decodo SERP responses.

Decodo's parsed schema can shift without warning – a renamed field can silently
become `None` everywhere. `validate_response` asserts the fields we depend on
are present and returns a list of issues. The fetcher logs these but does not
raise on a single drift event; the runner can escalate after consecutive failures.
"""

from __future__ import annotations

from typing import Any, Dict, List


EXPECTED_INNER_KEYS = ("organic",)
EXPECTED_ORGANIC_ITEM_KEYS = ("url",)
# Whether any given feature appears is content-dependent (a niche query may
# legitimately have no AI overview or PAA), so we don't alert on feature
# absence. We only alert if the keys we DO read change shape (e.g. organic
# stops being a list, or items lose their url/pos fields).


def validate_response(data: Dict[str, Any]) -> List[str]:
    issues: List[str] = []

    try:
        outer = data["results"][0]
        content = outer["content"]
        inner = content["results"]
        parse_status = inner.get("parse_status_code")
        serp = inner.get("results") or {}
    except (KeyError, IndexError, TypeError) as exc:
        issues.append(f"top-level path missing: {exc!r}")
        return issues

    if parse_status is not None and parse_status != 12000:
        # Decodo returns 12000 on a clean parse – anything else is parser error.
        issues.append(f"parse_status_code={parse_status} (expected 12000)")

    for key in EXPECTED_INNER_KEYS:
        if key not in serp:
            issues.append(f"missing required SERP key: {key!r}")

    organic = serp.get("organic")
    if organic is None:
        # already reported by the EXPECTED_INNER_KEYS check above
        return issues
    if not isinstance(organic, list):
        issues.append(f"'organic' is {type(organic).__name__}, expected list")
        return issues

    if organic:
        first = organic[0]
        if not isinstance(first, dict):
            issues.append(f"organic[0] is {type(first).__name__}, expected dict")
            return issues
        for key in EXPECTED_ORGANIC_ITEM_KEYS:
            if key not in first:
                issues.append(f"organic[0] missing {key!r}")
        if "pos" not in first and "pos_overall" not in first:
            issues.append("organic[0] missing both 'pos' and 'pos_overall'")

    # If an AI overview block is present, its citation shape is what we depend on.
    ai = serp.get("ai_overviews")
    if isinstance(ai, list) and ai:
        sp = (ai[0].get("source_panel") or {})
        items = sp.get("items")
        if items is not None and not isinstance(items, list):
            issues.append("ai_overviews[0].source_panel.items is not a list")
        elif isinstance(items, list) and items and "url" not in items[0]:
            issues.append("ai_overviews[0].source_panel.items[0] missing 'url'")

    return issues

#!/usr/bin/env python3
# Status: production
"""Feedback provider — extracts recent fix patterns from activity_log and returns
them as few-shot message arrays for injection into LLM conversations.

Sub-modules:
  state      — generation tracking, rollback detection
  patterns   — review fetching, pattern extraction
  messages   — pattern → few-shot message conversion
"""

from typing import Any, Dict, List

from lib.feedback.patterns import _fetch_review_results, _extract_findings
from lib.feedback.state import (
    _pattern_fingerprint, _register_generation, _check_rollback, _load_state,
)
from lib.feedback.messages import _patterns_to_messages


def get_feedback_for_model(
    model: str,
    max_gold: int = 2,
    max_edge: int = 2,
) -> List[Dict[str, str]]:
    try:
        results = _fetch_review_results()
    except Exception:
        return []

    if not results:
        return []

    patterns = _extract_findings(results)
    if not patterns:
        return []

    active_fp = _register_generation(patterns)
    active_fp = _check_rollback(active_fp)

    if active_fp != _pattern_fingerprint(patterns):
        state = _load_state()
        gen = state.get("generations", {}).get(active_fp, {})
        rolled_back = gen.get("patterns", [])
        if rolled_back:
            patterns = rolled_back

    return _patterns_to_messages(patterns, max_gold, max_edge)

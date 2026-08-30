#!/usr/bin/env python3
# Status: production
"""Feedback injection — few-shot patterns from activity_log."""

import time
from typing import Any, Dict, List

FEEDBACK_TTL = 300

_feedback_cache: Dict[str, List[Dict[str, str]]] = {}
_feedback_ts: float = 0.0


def _get_feedback(model: str) -> List[Dict[str, str]]:
    global _feedback_cache, _feedback_ts
    now = time.monotonic()
    if (now - _feedback_ts) < FEEDBACK_TTL:
        return _feedback_cache.get(model, [])
    from lib.feedback import get_feedback_for_model
    from lib.llm_client import MODEL_REGISTRY  # lazy import avoids circular

    _feedback_cache = {}
    for m, cfg in MODEL_REGISTRY.items():
        if "_model" in cfg:
            continue
        _feedback_cache[m] = get_feedback_for_model(m, max_gold=2, max_edge=2)
    _feedback_ts = now
    return _feedback_cache.get(model, [])


def _inject_feedback(messages: List[Dict], model: str) -> List[Dict]:
    fb = _get_feedback(model)
    if not fb:
        return messages
    sys_idx = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
    if sys_idx is not None:
        return messages[: sys_idx + 1] + fb + messages[sys_idx + 1:]
    return fb + messages

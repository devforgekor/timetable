#!/usr/bin/env python3
# Status: production
# Path: all pipeline files, lib modules
"""Shared utilities — log, timestamp, and other common helpers."""

from datetime import datetime, timezone


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{timestamp()}] {msg}", flush=True)

import re as _re
_THINK_RE = _re.compile(r"<think[^>]*>.*?</think>", _re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return _THINK_RE.sub("", text).strip()


def context_limit(text: str, max_chars: int = 2000, *, ratio_front: float = 0.5) -> str:
    """Truncate text keeping front and back portions (mitigates "Lost in the Middle").

    Splits at ratio_front/1-ratio_front front/back by default (50/50).
    Adds truncated marker in middle. If text fits within max_chars, returns unchanged.
    Set ratio_front=1.0 for front-only truncation (kiwi cleaning, etc.).

    Examples:
        context_limit(long_text, 2000)                # 1000 front + 1000 back
        context_limit(long_text, 2000, ratio_front=1.0)  # 2000 front only
        context_limit(short_text, 2000)                # unchanged
    """
    if not text or len(text) <= max_chars:
        return text
    front = max(1, int(max_chars * ratio_front))
    back = max(0, max_chars - front)
    if back <= 0 or ratio_front >= 1.0:
        return text[:max_chars]
    return text[:front] + "\n... (truncated) ...\n" + text[-back:]

#!/usr/bin/env python3
# Status: production
"""Cache-control helpers for Anthropic/DeepSeek proxy.

Deterministic padding, cache_control stripping, and
system-first JSON serialization for stable prefix caching.
"""

import json
import os
from typing import Any, Dict, Tuple


_CACHE_PADDING = None
_CACHE_PADDING_LOCK = [None]


def _get_cache_padding() -> str:
    global _CACHE_PADDING
    if _CACHE_PADDING is not None:
        return _CACHE_PADDING

    _lock = _CACHE_PADDING_LOCK
    if _lock[0] is not None:
        return _lock[0]

    pad_chars = int(os.environ.get("ANTHROPIC_PROXY_CACHE_PAD_CHARS", "0"))
    if pad_chars <= 0:
        _CACHE_PADDING = ""
        return _CACHE_PADDING

    pad_chars = min(pad_chars, 50_000)
    filler = "\n# [pad] " + "x" * 60
    repeats = pad_chars // len(filler) + 1
    result = ("\n[system continuation]" + filler * repeats)[:pad_chars]
    _CACHE_PADDING_LOCK[0] = result
    return result


def _apply_cache_padding(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    system = payload.get("system")
    if not isinstance(system, str):
        return payload, False
    pad = _get_cache_padding()
    if not pad:
        return payload, False
    payload = dict(payload)
    payload["system"] = system + "\n" + pad
    return payload, True


def _json_dumps_system_first(payload: dict, **kwargs: Any) -> str:
    if "system" not in payload:
        return json.dumps(payload, **kwargs)
    p = dict(payload)
    sys_val = p.pop("system")
    rest = json.dumps(p, **kwargs)
    sys_json = json.dumps(sys_val, **kwargs)
    return '{"system":' + sys_json + "," + rest[1:]


def _strip_cache_control(obj: Any) -> bool:
    if isinstance(obj, dict):
        changed = False
        if "cache_control" in obj:
            del obj["cache_control"]
            changed = True
        for value in obj.values():
            if _strip_cache_control(value):
                changed = True
        return changed
    if isinstance(obj, list):
        changed = False
        for item in obj:
            if _strip_cache_control(item):
                changed = True
        return changed
    return False

#!/usr/bin/env python3
# Status: production
"""Utility functions for Anthropic/DeepSeek proxy.

Sub-modules:
  sanitize   — text sanitization, billing header stripping
  cache      — deterministic padding, cache_control stripping
  tools      — tool_use/tool_result adjacency repair
  usage      — streaming parser, balance fetch, token logging
"""

from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

from lib.proxy_utils.cache import (
    _apply_cache_padding,
    _get_cache_padding,
    _json_dumps_system_first,
    _strip_cache_control,
)
from lib.proxy_utils.sanitize import (
    _flatten_system_blocks,
    _flatten_text,
    _sanitize_messages,
    _strip_billing_header,
    _strip_system_billing_header,
)
from lib.proxy_utils.tools import (
    _cleanup,
    _collect_referenced_tool_use_ids,
    _collect_tool_use_ids_present,
    _fix_orphan_tool_results,
    _message_has_nonempty_content,
    _prev_has_tool_use,
    _remove_adjacent_orphans,
    _strict_tool_adjacency_fix,
)
from lib.proxy_utils.usage import (
    _extract_stream_usage,
    _fetch_proxy_balance,
    _format_context_bar,
    _has_cache_stats,
    log_usage,
)

HOP_BY_HOP_HEADERS: set = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def normalize_proxy_path(path: str, base_path: str) -> str:
    parsed = urlsplit(path)
    path_only = parsed.path
    query = parsed.query

    while path_only.startswith("/anthropic/anthropic"):
        path_only = path_only.replace("/anthropic/anthropic", "/anthropic", 1)

    if path_only.startswith("/anthropic/v1"):
        path_only = path_only.replace("/anthropic", "", 1)

    if not path_only.startswith("/v1") and path_only.startswith("/anthropic/v1"):
        path_only = path_only.replace("/anthropic", "", 1)

    if base_path.endswith("/"):
        base_path = base_path[:-1]
    if base_path and not path_only.startswith(base_path):
        prefixed = base_path + path_only
    else:
        prefixed = path_only
    return prefixed + ("?" + query if query else "")


def filter_response_headers(resp_headers: list) -> tuple[list, bool]:
    filtered: list = []
    is_chunked = False
    for key, value in resp_headers:
        lk = key.lower()
        if lk in HOP_BY_HOP_HEADERS and lk != "transfer-encoding":
            continue
        if lk == "content-length":
            continue
        if lk == "transfer-encoding":
            if "chunked" in value.lower():
                is_chunked = True
            continue
        filtered.append((key, value))
    return filtered, is_chunked

#!/usr/bin/env python3
# Status: production
"""Usage tracking, streaming parsing, and balance helpers for proxy.

Extracts usage from streamed SSE responses, fetches DeepSeek
balance, formats context-window bars, and logs token usage.
"""

import http.client
import json
import os
import sys
import time
from typing import Any, Dict, Optional


# Lazy DB import — avoid circular dependency at module level
def _observe_token_usage(
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_miss: int,
    hit_rate: float,
    body_kb: float,
) -> None:
    try:
        sys.path.insert(
            0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        from lib.observation import observe

        ctx = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read": cache_read,
            "cache_miss": cache_miss,
            "hit_rate": round(hit_rate, 1),
            "body_kb": round(body_kb, 1),
        }
        observe(
            f"proxy: input={input_tokens} output={output_tokens} hit={hit_rate:.0f}%",
            category="usage",
            source="proxy:anthropic",
            context=ctx,
            tags={"domain": ["proxy", "token_usage"]},
        )
    except Exception:
        pass


def _extract_stream_usage(tail_bytes: bytes) -> bytes:
    try:
        text = tail_bytes.decode("utf-8", errors="replace")
        for line in reversed(text.split("\n")):
            line = line.strip()
            if line.startswith("data: ") and "[DONE]" not in line:
                try:
                    payload = json.loads(line[6:])
                    if "usage" in payload:
                        return json.dumps({"usage": payload["usage"]}).encode("utf-8")
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return b""


_balance_cache: Dict[str, Any] = {"cny": None, "time": 0.0}
_BALANCE_CACHE_TTL = 120


def _fetch_proxy_balance() -> Optional[str]:
    global _balance_cache
    now = time.time()
    if _balance_cache["cny"] is not None and now - _balance_cache["time"] < _BALANCE_CACHE_TTL:
        return _balance_cache["cny"]

    api_key: Optional[str] = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
        "ANTHROPIC_AUTH_TOKEN"
    )
    if not api_key:
        return None

    try:
        conn = http.client.HTTPSConnection("api.deepseek.com", timeout=5)
        conn.request("GET", "/user/balance", headers={"Authorization": f"Bearer {api_key}"})
        resp = conn.getresponse()
        if resp.status == 200:
            data: Dict[str, Any] = json.loads(resp.read().decode())
            for bi in data.get("balance_infos", []):
                if bi.get("currency") == "CNY":
                    bal: str = bi.get("topped_up_balance", "0.00")
                    _balance_cache["cny"] = bal
                    _balance_cache["time"] = now
                    return bal
    except Exception:
        pass
    return None


def _format_context_bar(tokens: int, max_tokens: int) -> str:
    pct = min(tokens / max_tokens * 100, 100.0) if max_tokens > 0 else 0
    total_half = round(pct / 5)
    chars = []
    for i in range(10):
        remain = total_half - i * 2
        if remain <= 0:
            chars.append("░")
        elif remain == 1:
            chars.append("▌")
        else:
            chars.append("█")
    bar = "".join(chars)
    if pct >= 90.0:
        bar = f"\033[31m{bar}\033[0m"
    elif pct >= 70.0:
        bar = f"\033[33m{bar}\033[0m"
    return f"{bar} {pct:.0f}%"


_KNOWN_CACHE_KEYS = {
    "prompt_cache_hit_tokens",
    "cache_read_input_tokens",
    "prompt_cache_miss_tokens",
    "cache_creation_input_tokens",
}


def _has_cache_stats(u: dict) -> bool:
    if any(k in u for k in _KNOWN_CACHE_KEYS):
        return True
    if "prompt_tokens_details" in u and isinstance(u["prompt_tokens_details"], dict):
        return True
    return False


def log_usage(body: Optional[bytes], data: Optional[bytes], resp_status: int) -> None:
    if body and resp_status == 200 and data:
        try:
            r = json.loads(data.decode("utf-8"))
            u = r.get("usage", {})

            input_tokens = u.get("input_tokens", u.get("prompt_tokens", 0))
            output_tokens = u.get("output_tokens", u.get("completion_tokens", 0))

            body_kb = len(body) / 1024
            body_chars = len(body)
            COMPACT_BODY_LIMIT = int(os.environ.get("ANTHROPIC_PROXY_BAR_LIMIT", "870400"))
            context_bar = _format_context_bar(body_chars, COMPACT_BODY_LIMIT)
            balance = _fetch_proxy_balance()
            balance_str = f" ¥{balance}" if balance else ""

            if not _has_cache_stats(u):
                print(
                    f"[anthropic_proxy] usage: input={input_tokens} "
                    f"output={output_tokens} "
                    f"stats=none body={body_kb:.0f}KB {context_bar}{balance_str}",
                    file=sys.stderr,
                )
                _observe_token_usage(input_tokens, output_tokens, 0, 0, 0.0, body_kb)
            else:
                cache_read = u.get("prompt_cache_hit_tokens", 0)
                if not cache_read:
                    cache_read = u.get("cache_read_input_tokens", 0)
                if not cache_read:
                    ptd = u.get("prompt_tokens_details")
                    if isinstance(ptd, dict):
                        cache_read = ptd.get("cached_tokens", 0)

                cache_miss = u.get("prompt_cache_miss_tokens", 0)
                if not cache_miss:
                    cache_miss = u.get("cache_creation_input_tokens", 0)

                denom = input_tokens + cache_read
                if cache_miss > 0:
                    denom = max(denom, cache_read + cache_miss)
                pct = min((cache_read / denom * 100), 100.0) if denom > 0 else 0

                print(
                    f"[anthropic_proxy] usage: input={input_tokens} cache_read={cache_read} "
                    f"cache_miss={cache_miss} output={output_tokens} "
                    f"hit_rate={pct:.0f}% body={body_kb:.0f}KB {context_bar}{balance_str}",
                    file=sys.stderr,
                )
                _observe_token_usage(
                    input_tokens, output_tokens, cache_read, cache_miss, pct, body_kb
                )
        except Exception:
            pass

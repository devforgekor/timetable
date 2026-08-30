#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""
Multi-API web search manager with intent-based routing and key rotation.

Each API has strengths — queries are routed to the best service first:
  - code:  Exa (semantic code search) → Brave → Travily → You.com
  - docs:  Brave (fast web docs) → Exa → Travily → You.com
  - fact:  Travily (structured answers) → Brave → Exa → You.com
  - general: Brave → Travily → Exa → You.com

Usage:
    from lib.search.manager import WebSearchManager
    sm = WebSearchManager()
    result = sm.search("Podman Quadlet tutorial")       # auto-detect intent
    result = sm.search("TypeError: NoneType", "code")   # explicit intent
    # {"source": "brave", "results": [...]}  or None if all exhausted
"""

import os
import re
import sys
from pathlib import Path
from typing import Optional

import requests

from lib.auth.api_key_cipher import decrypt_data
from lib.auth.key_rotator import KeyRotator


API_CONFIGS = {
    "brave": {
        "url": "https://api.search.brave.com/res/v1/web/search",
        "method": "GET",
        "header_name": "X-Subscription-Token",
        "prefix": "",
        "timeout": 15,
    },
    "exa": {
        "url": "https://api.exa.ai/search",
        "method": "POST",
        "header_name": "x-api-key",
        "prefix": "",
        "timeout": 15,
    },
    "travily": {
        "url": "https://api.travily.com/search",
        "method": "GET",
        "header_name": "Authorization",
        "prefix": "Bearer ",
        "timeout": 15,
    },
    "youcom": {
        "url": "https://api.you.com/search",
        "method": "GET",
        "header_name": "X-API-Key",
        "prefix": "",
        "timeout": 15,
    },
}

SECRET_KEYS = {
    "brave": "BRAVE_API_KEYS",
    "exa": "EXA_API_KEYS",
    "travily": "TRAVILY_API_KEYS",
    "youcom": "YOUCOM_API_KEYS",
}


def _read_secret(key: str) -> str:
    try:
        for line in Path("~/.config/devforge/secrets.env").expanduser().read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return os.getenv(key, "")


def _load_keys(service: str) -> list[tuple[str, str]]:
    """Load API keys for a service from secrets.env (supports encryption)."""
    var_name = SECRET_KEYS[service]
    keys_str = _read_secret(var_name)
    if not keys_str:
        return []

    keys = []
    for item in keys_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, cipher = item.split(":", 1)
            name = name.strip()
            cipher = cipher.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                print(f"경고: {service} 키 복호화 실패 — {name} (평문으로 시도)", file=sys.stderr)
                plain = cipher
            keys.append((name, plain))
        else:
            cipher = item.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                plain = cipher
            keys.append((f"{service}-{len(keys)}", plain))
    return keys


def _call_api(service: str, api_key: str, query: str) -> Optional[list]:
    """Call a search API. Returns list of results or None on failure."""
    cfg = API_CONFIGS[service]
    headers = {
        cfg["header_name"]: cfg["prefix"] + api_key,
        "Content-Type": "application/json",
    }
    params = {"q": query, "count": 5}

    try:
        if cfg["method"] == "POST":
            r = requests.post(cfg["url"], json={"query": query, "num_results": 5},
                              headers=headers, timeout=cfg["timeout"])
        else:
            r = requests.get(cfg["url"], headers=headers, params=params, timeout=cfg["timeout"])

        if r.status_code == 200:
            data = r.json()
            return _extract_results(service, data)
        elif r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "60"))
            raise RateLimitError(retry_after)
    except RateLimitError:
        raise
    except Exception:
        pass
    return None


class RateLimitError(Exception):
    def __init__(self, retry_seconds: int = 60):
        self.retry_seconds = retry_seconds


def _extract_results(service: str, data: dict) -> list:
    """Normalize API responses into a uniform result schema."""
    # Brave
    if service == "brave" and "web" in data:
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("description", "")}
                for r in data["web"].get("results", [])]

    # Exa
    if service == "exa" and "results" in data:
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("text", r.get("snippet", ""))}
                for r in data["results"]]

    # Travily
    if service == "travily" and "results" in data:
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("snippet", r.get("content", ""))}
                for r in data["results"]]

    # You.com
    if service == "youcom" and "hits" in data:
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("snippet", r.get("description", ""))}
                for r in data["hits"]]

    # Fallback: try common keys
    results = data.get("results") or data.get("hits") or data.get("web", {}).get("results")
    if results:
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("snippet", r.get("description", r.get("text", "")))}
                for r in results]
    return []


# Route order per intent: best API first, You.com always last (backup)
INTENT_ORDER = {
    "code":   ["exa", "brave", "travily", "youcom"],
    "docs":   ["brave", "exa", "travily", "youcom"],
    "fact":   ["travily", "brave", "exa", "youcom"],
    "general":["brave", "travily", "exa", "youcom"],
}

_CODE_RE = re.compile(
    r"\b(traceback|exception|typeerror|valueerror|keyerror|attributeerror|"
    r"syntaxerror|indexerror|modulenotfounderror|importerror|runtimeerror|"
    r"segfault|core\s*dump|undefined|null\s*pointer|deprecated|"
    r"warning:|panic|fatal|abort|assertion|cannot\s+find|not\s+found|"
    r"no\s+method|no\s+module|unresolved|symbol\s+not|link\s+error)\b",
    re.IGNORECASE,
)

_DOCS_RE = re.compile(
    r"\b(how\s+to|how\s+do\s+i|documentation|docs?\b|api\s+reference|"
    r"tutorial|guide|install|configure|setup|getting\s+started|"
    r"quickstart|syntax|parameter|changelog|release\s+notes?|"
    r"migration|upgrade|compatibility)\b",
    re.IGNORECASE,
)

_FACT_RE = re.compile(
    r"\b(what\s+is|what\s+are|when\s+did|when\s+was|who\s+is|who\s+was|"
    r"where\s+is|define|definition|meaning\s+of|difference\s+between|"
    r"vs\.?|versus|compare|why\s+does|why\s+is|how\s+does|how\s+is|"
    r"how\s+many|how\s+much|which\s+is|best\s+practice)\b",
    re.IGNORECASE,
)


def classify_query_intent(query: str, context: str = "") -> str:
    """Determine query intent for API routing. LLM-free keyword matching."""
    if context in INTENT_ORDER:
        return context

    q = " " + query.lower() + " "

    if _CODE_RE.search(q):
        return "code"
    if _DOCS_RE.search(q):
        return "docs"
    if _FACT_RE.search(q):
        return "fact"
    return "general"


class WebSearchManager:
    def __init__(self):
        state_base = "~/.cache/devforge"
        self._rotators = {
            "brave":   KeyRotator(_load_keys("brave"),
                                  state_file=f"{state_base}/brave_state.json"),
            "exa":     KeyRotator(_load_keys("exa"),
                                  state_file=f"{state_base}/exa_state.json"),
            "travily": KeyRotator(_load_keys("travily"),
                                  state_file=f"{state_base}/travily_state.json"),
            "youcom":  KeyRotator(_load_keys("youcom"),
                                  state_file=f"{state_base}/youcom_state.json"),
        }

    def search(self, query: str, context: str = "") -> Optional[dict]:
        """
        Search with intent-based routing + tiered fallback.

        context: "", "code", "docs", "fact", or "general".
                 Empty = auto-detect from query keywords.
        Returns {'source': str, 'results': list} or None if all exhausted.
        """
        intent = classify_query_intent(query, context)
        order = INTENT_ORDER[intent]

        for service in order:
            rotator = self._rotators[service]
            picked = rotator.pick()
            if picked is None:
                continue

            idx, name, key = picked
            try:
                results = _call_api(service, key, query)
                if results is not None:
                    rotator.success(idx)
                    return {"source": service, "intent": intent, "results": results}
                rotator.rate_limited(idx, 30)
            except RateLimitError as e:
                rotator.rate_limited(idx, e.retry_seconds)

        return None

    def stats(self) -> dict:
        """Per-service rotation stats."""
        return {name: rotator.stats() for name, rotator in self._rotators.items()}


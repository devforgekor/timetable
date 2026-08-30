#!/usr/bin/env python3
# Status: production
# Path: imported by proxies/gemini_openai.py
"""Daily per-key API quota tracker.
Tracks Requests Per Day (RPD) and token usage per key,
persists to JSON, auto-resets at midnight UTC.
"""

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

DEFAULT_RPD = 1500  # Gemini 2.5 Flash / Flash-Lite free tier


class QuotaTracker:
    """Per-key daily quota state with JSON persistence."""

    def __init__(self, state_file: str, key_names: list[tuple[int, str]]):
        self._state_file = os.path.expanduser(state_file)
        self._key_names = {idx: name for idx, name in key_names}
        self._state = self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _load(self) -> dict:
        path = Path(self._state_file)
        if not path.exists():
            return {"date": self._today(), "keys": {}}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {"date": self._today(), "keys": {}}

    def _save(self):
        path = Path(self._state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "date": self._today(),
            "keys": self._state.get("keys", {}),
        }
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with open(fd, "w") as f:
                f.write(payload)
            Path(tmp).rename(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)

    def _get_key_state(self, key_idx: int) -> dict:
        today = self._today()
        keys = self._state.setdefault("keys", {})
        skey = str(key_idx)
        entry = keys.get(skey)
        if entry is None or entry.get("date") != today:
            entry = {
                "date": today,
                "name": self._key_names.get(key_idx, f"key-{key_idx}"),
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
            keys[skey] = entry
            self._state["date"] = today
        return entry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_rpd_exhausted(self, key_idx: int):
        """Mark a key as having exhausted its daily RPD.
        Sets requests_used = DEFAULT_RPD so remaining_rpd returns 0.
        """
        entry = self._get_key_state(key_idx)
        entry["requests"] = DEFAULT_RPD
        self._save()

    def record_usage(self, key_idx: int, input_tokens: int, output_tokens: int):
        """Record a successful API call with token counts."""
        entry = self._get_key_state(key_idx)
        entry["requests"] += 1
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        self._save()

    def remaining_rpd(self, key_idx: int) -> int:
        """Return remaining requests for today (RPD limit - used)."""
        entry = self._get_key_state(key_idx)
        return max(0, DEFAULT_RPD - entry["requests"])

    def is_exhausted(self, key_idx: int) -> bool:
        """True if the key has hit its daily RPD limit."""
        return self.remaining_rpd(key_idx) <= 0

    def get_key_stats(self, key_idx: int) -> dict:
        """Return detailed quota stats for a single key."""
        entry = self._get_key_state(key_idx)
        remaining = max(0, DEFAULT_RPD - entry["requests"])
        return {
            "index": key_idx,
            "name": entry.get("name", f"key-{key_idx}"),
            "date": entry["date"],
            "requests_used": entry["requests"],
            "requests_remaining": remaining,
            "rpd_limit": DEFAULT_RPD,
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "total_tokens": entry["input_tokens"] + entry["output_tokens"],
            "exhausted": remaining <= 0,
        }

    def get_all_stats(self) -> dict:
        """Return aggregate quota stats for all tracked keys."""
        indices = sorted(
            self._key_names.keys(),
            key=lambda i: self._key_names.get(i, f"key-{i}"),
        )
        keys = [self.get_key_stats(i) for i in indices]
        total_req = sum(k["requests_used"] for k in keys)
        available = sum(1 for k in keys if not k["exhausted"])
        total_input = sum(k["input_tokens"] for k in keys)
        total_output = sum(k["output_tokens"] for k in keys)
        return {
            "date": self._today(),
            "total_keys": len(keys),
            "available_keys": available,
            "total_requests_today": total_req,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "per_key_rpd_limit": DEFAULT_RPD,
            "keys": keys,
        }

#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""
Lightweight API Key Rotator — in-memory, no DB dependency.

Usage:
    rotator = KeyRotator([
        ("acct1_key1", "AIza..."),
        ("acct1_key2", "AIza..."),
        ("acct2_key1", "AIza..."),
    ])
    idx, name, key = rotator.pick()
    # ... use key ...
    rotator.success(idx)
    # or on 429:
    rotator.rate_limited(idx, retry_seconds=45)
"""

import time
import random
from datetime import datetime, timezone, timedelta
from typing import Optional


DAILY_QUOTA_THRESHOLD = 300  # seconds — >= 5min = daily quota exhaustion
ACCOUNT_RPM_INTERVAL = 3.0  # seconds between uses of keys from the same account
STALE_FAIL_SECONDS = 300    # clear fail counts older than 5 min


def extract_account_from_key_name(name: str) -> str:
    """Extract account prefix from key name.

    Gemini keys:  'mesids_senedu_gemini_09' → 'mesids_senedu'
    Search keys:  'brave:mesids_kuhwa_brave_search_1' → 'brave'
    """
    idx = name.rfind("_gemini_")
    if idx != -1:
        return name[:idx]
    if ":" in name:
        return name.split(":", 1)[0]
    return name


class KeyRotator:
    """Key rotation converging to even distribution across keys and accounts.

    Per-turn rotation: each pick() prefers keys with fewer calls and
    deprioritizes accounts used within the RPM window. 429 → backoff,
    200 → call count increases → next pick naturally picks another key.
    """

    def __init__(self, keys: list[tuple[str, str]], state_file: str = ""):
        """
        keys: [(display_name, api_key), ...]
        state_file: optional path to JSON state file for persistence across runs.
        """
        self.keys = keys
        self.n = len(keys)
        self._state_file = state_file

        self._calls: dict[int, int] = {}           # total calls per key
        self._fails: dict[int, int] = {}           # total failures per key
        self._last_used: dict[int, float] = {}     # last use timestamp
        self._backoff_until: dict[int, float] = {} # backoff expiry

        if state_file:
            self._load_state()

    def _clear_stale_fails(self):
        """Clear fail counts for keys not used recently — their RPM quota has reset."""
        now = time.time()
        for i in list(self._fails.keys()):
            last = self._last_used.get(i, 0)
            if self._fails[i] > 0 and (now - last) > STALE_FAIL_SECONDS:
                self._fails[i] = 0

    def pick(self) -> Optional[tuple[int, str, str]]:
        """
        Return (index, name, key) of the next available key.

        Selection priority:
        1. Skip keys in backoff
        2. Skip keys from accounts used within ACCOUNT_RPM_INTERVAL
           (falls back to inside-window keys only when none outside)
        3. Among available: fewest fails → fewest calls → oldest last_used
        → converges to even distribution across all keys over time,
        with hard RPM guard per account.
        """
        now = time.time()
        self._clear_stale_fails()

        # Find the most recent use time for each account
        account_last_used: dict[str, float] = {}
        for i in range(self.n):
            acct = extract_account_from_key_name(self.keys[i][0])
            last = self._last_used.get(i, 0)
            account_last_used[acct] = max(account_last_used.get(acct, 0), last)

        # Build sort key for a given index
        def _sort_key(i: int, acct: str) -> tuple:
            return (
                self._fails.get(i, 0),
                self._calls.get(i, 0),
                account_last_used.get(acct, 0),
                self._last_used.get(i, 0),
            )

        # Collect non-backoff keys, split by RPM window
        outside: list[tuple[tuple, int]] = []
        inside: list[tuple[tuple, int]] = []
        for i in range(self.n):
            if self._backoff_until.get(i, 0) > now:
                continue
            acct = extract_account_from_key_name(self.keys[i][0])
            acct_last = account_last_used.get(acct, 0)
            if acct_last == 0:
                in_window = False  # never used — allow first pick
            else:
                in_window = (now - acct_last) < ACCOUNT_RPM_INTERVAL
            sk = _sort_key(i, acct)
            if in_window:
                inside.append((sk, i))
            else:
                outside.append((sk, i))

        # Prefer outside-window keys; fall back to inside-window only when none available
        if outside:
            pool = outside
        elif inside:
            # All accounts inside RPM window — wait until the oldest exits
            oldest_acct_last = min(account_last_used.get(extract_account_from_key_name(self.keys[i][0]), 0)
                                   for _, i in inside)
            wait = ACCOUNT_RPM_INTERVAL - (now - oldest_acct_last)
            if wait > 0:
                time.sleep(wait)
            pool = inside
        else:
            return None  # all keys in backoff

        pool.sort()
        idx = pool[0][1]

        self._last_used[idx] = now
        return idx, self.keys[idx][0], self.keys[idx][1]

    def _load_state(self):
        """Restore rotation state from disk (JSON)."""
        import json
        from pathlib import Path

        path = Path(self._state_file).expanduser()
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            for i_str, v in state.get("_calls", {}).items():
                self._calls[int(i_str)] = v
            for i_str, v in state.get("_fails", {}).items():
                self._fails[int(i_str)] = v
            for i_str, v in state.get("_last_used", {}).items():
                self._last_used[int(i_str)] = v
            for i_str, v in state.get("_backoff_until", {}).items():
                self._backoff_until[int(i_str)] = v
        except Exception:
            pass  # corrupt state → start fresh

    def _save_state(self):
        """Persist rotation state to disk (atomic write)."""
        import json
        import tempfile
        from pathlib import Path

        path = Path(self._state_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "_calls": {str(k): v for k, v in self._calls.items()},
            "_fails": {str(k): v for k, v in self._fails.items()},
            "_last_used": {str(k): v for k, v in self._last_used.items()},
            "_backoff_until": {str(k): v for k, v in self._backoff_until.items()},
        }
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with open(fd, "w") as f:
                f.write(payload)
            Path(tmp).rename(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)

    def wait_seconds(self) -> float:
        """Return seconds until the next key becomes available, or 0."""
        now = time.time()
        waits = [t - now for t in self._backoff_until.values() if t > now]
        return max(waits) if waits else 0.0

    def success(self, idx: int):
        """Record successful call. Fails are NOT reset — they clear naturally
        via _clear_stale_fails() after STALE_FAIL_SECONDS of inactivity.
        Calls counter increase deprioritizes this key for the next pick(),
        ensuring natural rotation across all keys."""
        self._calls[idx] = self._calls.get(idx, 0) + 1
        self._backoff_until.pop(idx, None)
        if self._state_file:
            self._save_state()

    def rate_limited(self, idx: int, retry_seconds: int):
        """
        Record 429 ResourceExhausted.
        - retry_seconds >= DAILY_QUOTA_THRESHOLD → backoff until next day 17:00 KST
        - retry_seconds < DAILY_QUOTA_THRESHOLD → jittered backoff
        """
        self._fails[idx] = self._fails.get(idx, 0) + 1

        if retry_seconds >= DAILY_QUOTA_THRESHOLD:
            # Daily quota exhausted — lock until tomorrow 17:00 KST
            kst = timezone(timedelta(hours=9))
            now_kst = datetime.now(kst)
            today_5pm = now_kst.replace(hour=17, minute=0, second=0, microsecond=0)
            if now_kst >= today_5pm:
                today_5pm += timedelta(days=1)
            self._backoff_until[idx] = today_5pm.timestamp()
        else:
            # Rate-limited — jittered backoff
            jitter = retry_seconds * random.uniform(-0.2, 0.2)
            delay = max(1, retry_seconds + jitter)
            self._backoff_until[idx] = time.time() + delay

        if self._state_file:
            self._save_state()

    def stats(self) -> dict:
        """Return current rotation stats for monitoring."""
        now = time.time()
        key_stats = []
        for i, (name, _) in enumerate(self.keys):
            backoff_remaining = max(0.0, self._backoff_until.get(i, 0) - now)
            key_stats.append({
                "index": i,
                "name": name,
                "calls": self._calls.get(i, 0),
                "fails": self._fails.get(i, 0),
                "last_used": self._last_used.get(i, 0),
                "in_backoff": backoff_remaining > 0,
                "backoff_remaining": round(backoff_remaining, 1),
            })

        total_calls = sum(ks["calls"] for ks in key_stats)
        avg_calls = total_calls / max(self.n, 1)
        active = [ks for ks in key_stats if not ks["in_backoff"]]

        return {
            "total_keys": self.n,
            "available_keys": len(active),
            "total_calls": total_calls,
            "avg_calls_per_key": round(avg_calls, 1),
            "keys": key_stats,
        }


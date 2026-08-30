#!/usr/bin/env python3
# Status: production
"""Feedback generation state management.

Tracks pattern generations, pass rates, and automatic rollback
when a new generation underperforms its predecessor.
"""

import hashlib
import json
import os
import subprocess as sp
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


FEEDBACK_WINDOW_HOURS = 48
ROLLBACK_THRESHOLD = 0.05
ROLLBACK_MIN_SAMPLES = 10

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "feedback_state.json")
STATE_FILE = os.path.abspath(STATE_FILE)


def _pattern_fingerprint(patterns: List[Dict]) -> str:
    raw = json.dumps(patterns, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_state() -> Dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"generation": 0, "active_fingerprint": None, "generations": {}}


def _save_state(state: Dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _get_pass_rate() -> Optional[float]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=FEEDBACK_WINDOW_HOURS)).isoformat()
    sql = (
        "SELECT body::text FROM activity_log "
        "WHERE queue_status = 'done' "
        "  AND type IN ('review', 'debate_result', 'verify_result', 'extract_result') "
        f"  AND created_at > '{cutoff}'::timestamptz "
        "  AND body->'verify_result' IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 50"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None

    total = 0
    passed = 0
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue
        v_result = body.get("verify_result", {})
        if not isinstance(v_result, dict):
            continue
        items = v_result.get("verification_items", [])
        if not isinstance(items, list):
            continue
        for vi in items:
            result = vi.get("result", "")
            total += 1
            if result == "pass":
                passed += 1

    if total < ROLLBACK_MIN_SAMPLES:
        return None
    return passed / total


def _register_generation(patterns: List[Dict]) -> str:
    state = _load_state()
    fp = _pattern_fingerprint(patterns)
    active = state.get("active_fingerprint")

    if fp == active:
        return fp

    existing = state.get("generations", {})
    if fp in existing:
        state["active_fingerprint"] = fp
        _save_state(state)
        return fp

    prev_rate = None
    prev_fp = active
    if prev_fp and prev_fp in existing:
        prev_rate = existing[prev_fp].get("pass_rate")

    state.setdefault("generations", {})[fp] = {
        "patterns": patterns,
        "pass_rate": None,
        "sample_size": 0,
        "prev_fingerprint": prev_fp,
        "prev_rate": prev_rate,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state["active_fingerprint"] = fp
    state["generation"] = state.get("generation", 0) + 1
    _save_state(state)
    return fp


def _check_rollback(active_fp: str) -> Optional[str]:
    state = _load_state()
    gens = state.get("generations", {})
    gen = gens.get(active_fp)
    if not gen:
        return active_fp

    prev_fp = gen.get("prev_fingerprint")
    if not prev_fp or prev_fp not in gens:
        return active_fp

    prev = gens[prev_fp]
    prev_rate = prev.get("pass_rate")
    if prev_rate is None:
        return active_fp

    current_rate = _get_pass_rate()
    if current_rate is None:
        return active_fp

    gen["pass_rate"] = current_rate
    gen["sample_size"] = max(gen.get("sample_size", 0), ROLLBACK_MIN_SAMPLES)
    _save_state(state)

    if current_rate >= prev_rate - ROLLBACK_THRESHOLD:
        return active_fp

    print(f"  FEEDBACK ROLLBACK: gen {active_fp[:8]} pass_rate={current_rate:.1%} "
          f"< prev {prev_fp[:8]} pass_rate={prev_rate:.1%} (threshold {ROLLBACK_THRESHOLD:.0%})")

    _enqueue_rollback_analysis(active_fp, prev_fp, current_rate, prev_rate, gen)
    state["active_fingerprint"] = prev_fp
    _save_state(state)
    return prev_fp


def _enqueue_rollback_analysis(
    failed_fp: str, prev_fp: str,
    current_rate: float, prev_rate: float,
    gen: Dict,
) -> bool:
    patterns = gen.get("patterns", [])
    prev_gen = _load_state().get("generations", {}).get(prev_fp, {})
    prev_patterns = prev_gen.get("patterns", [])

    body = {
        "analysis_type": "feedback_rollback",
        "failed_fingerprint": failed_fp,
        "failed_pass_rate": round(current_rate, 3),
        "prev_fingerprint": prev_fp,
        "prev_pass_rate": round(prev_rate, 3),
        "failed_patterns": [
            {"issue": p["issue"], "fix": p["fix"], "classification": p.get("classification", "")}
            for p in patterns
        ],
        "previous_patterns": [
            {"issue": p["issue"], "fix": p["fix"], "classification": p.get("classification", "")}
            for p in prev_patterns
        ],
    }
    body_json = json.dumps(body, ensure_ascii=False).replace("'", "''")
    title = f"feedback rollback: {failed_fp[:8]} ({current_rate:.0%}) < {prev_fp[:8]} ({prev_rate:.0%})"
    sql = (
        "INSERT INTO activity_log "
        "(type, source, title, summary, body, model, summary_status, queue_status, exec_status) "
        "VALUES ("
        f"'analysis_request', 'feedback', '{title.replace(chr(39), chr(39)+chr(39))}', "
        f"'Gold Standard pass_rate dropped from {prev_rate:.0%} to {current_rate:.0%}', "
        f"'{body_json}', 'deepseek-v4-flash', 'raw', 'reviewed', 'DONE'"
        ")"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    ok = r.returncode == 0
    if ok:
        print(f"  Rollback analysis enqueued (analysis_request)")
    return ok

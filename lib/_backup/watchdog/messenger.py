#!/usr/bin/env python3
# Status: production
# Path: imported by — watchdog.py, day_pipeline.py, pipelines/*
"""Watchdog Messenger — 정보 중개 시스템.

PostgreSQL-backed: watchdog_pulses table replaces file-based queue.
Idempotent pulse IDs (date + file_hash) prevent duplicate insertion.
"""

import hashlib
from datetime import datetime, timezone
from typing import Optional

from lib.db import psql_ok, psql_json, esc_sql


def _make_pulse_id(instruction: str, target_file: str = "", date_str: str = "") -> str:
    """Generate deterministic pulse_id: pulse_{date}_{content_hash[:12]}."""
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    payload = f"{date_str}:{target_file}:{instruction[:100]}"
    content_hash = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"pulse_{date_str}_{content_hash}"


def log_message(source: str, target: str, type: str, content: str, detail: str = "",
                priority: str = "P1_CONTEXT", category: str = "",
                target_file: str = "", target_test: str = "",
                max_retries: int = 3) -> Optional[str]:
    """메시지 기록 → watchdog_pulses table.

    Returns pulse_id if created, None if duplicate (idempotent).
    Signature backward-compatible with old file-based log_message().
    """
    pulse_id = _make_pulse_id(content, target_file)

    ct = esc_sql(content)
    dt = esc_sql(detail)
    cat = esc_sql(category)
    tf = esc_sql(target_file)
    tt = esc_sql(target_test)

    ok = psql_ok(
        f"INSERT INTO watchdog_pulses (pulse_id, priority, category, target_file, "
        f"target_test, instruction, max_retries) "
        f"VALUES ('{pulse_id}', '{esc_sql(priority)}', "
        f"NULLIF('{cat}', ''), NULLIF('{tf}', ''), NULLIF('{tt}', ''), "
        f"'{ct}', {max_retries}) "
        f"ON CONFLICT (pulse_id) DO UPDATE SET "
        f"last_failure = NULLIF('{dt}', ''), "
        f"retry_count = watchdog_pulses.retry_count + 1 "
        f"WHERE watchdog_pulses.status = 'PENDING'"
    )
    return pulse_id if ok else None


def get_undelivered(target: Optional[str] = None) -> list:  # Python 3.9: list[dict] not supported
    """미전달 pulse 조회 및 IN_PROGRESS 마킹 (atomic via transaction).

    Returns list of dicts with keys matching old file format:
        type, content, detail, target_file, target_test, retry_count, max_retries
    """
    target_clause = f"AND target_file = '{esc_sql(target)}'" if target else ""
    pulse_ids = []

    # 1. PENDING → IN_PROGRESS (mark-and-fetch pattern)
    try:
        rows = psql_json(
            f"SELECT pulse_id, priority, category, instruction, target_file, "
            f"target_test, retry_count, max_retries "
            f"FROM watchdog_pulses "
            f"WHERE status = 'PENDING' {target_clause} "
            f"ORDER BY "
            f"  CASE priority "
            f"    WHEN 'P0_HOT_FIX' THEN 1 "
            f"    WHEN 'P1_CONTEXT' THEN 2 "
            f"    WHEN 'HUMAN_REQUIRED' THEN 3 "
            f"    ELSE 4 END, "
            f"  created_at ASC "
            f"LIMIT 20"
        )
    except Exception:
        return []

    if not rows:
        return []

    for r in rows:
        pulse_ids.append(r["pulse_id"])

    # 2. Mark IN_PROGRESS
    id_list = ", ".join(f"'{esc_sql(pid)}'" for pid in pulse_ids)
    psql_ok(
        f"UPDATE watchdog_pulses SET status = 'IN_PROGRESS' "
        f"WHERE pulse_id IN ({id_list}) AND status = 'PENDING'"
    )

    # 3. Return in old format
    messages = []
    for r in rows:
        messages.append({
            "type": "alert",
            "content": f"[{r['priority']}] {r['instruction']}",
            "detail": f"pulse_id={r['pulse_id']}",
            "target_file": r.get("target_file", ""),
            "target_test": r.get("target_test", ""),
            "retry_count": r.get("retry_count", 0),
            "max_retries": r.get("max_retries", 3),
        })
    return messages


def resolve_pulse(pulse_id: str, status: str = "RESOLVED") -> bool:
    """Mark a pulse as RESOLVED or IGNORED."""
    return psql_ok(
        f"UPDATE watchdog_pulses SET status = '{esc_sql(status)}', "
        f"resolved_at = now() "
        f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )


def escalate_pulse(pulse_id: str, reason: str = "") -> bool:
    """Escalate pulse to HUMAN_REQUIRED (retry_count >= max_retries)."""
    r = esc_sql(reason)
    return psql_ok(
        f"UPDATE watchdog_pulses "
        f"SET status = 'HUMAN_REQUIRED', last_failure = NULLIF('{r}', '') "
        f"WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )


# ── Heartbeat (Dead Man's Switch) ─────────────────────────────────────


def heartbeat(worker_name: str, detail: str = "") -> bool:
    """Worker heartbeat — fixed pulse_id per worker, upserts timestamp.

    Long-running tasks call this periodically so the watchdog can detect hangs.
    Pulse ID is always ``heartbeat_{worker_name}`` — deterministic, upsert-only.
    Optional ``detail`` (e.g. elapsed_ms) is appended to the instruction field.
    """
    pulse_id = f"heartbeat_{worker_name}"
    instruction = f"{worker_name} {detail}".strip() if detail else worker_name
    return psql_ok(
        f"INSERT INTO watchdog_pulses "
        f"  (pulse_id, priority, category, instruction, status, created_at) "
        f"VALUES ('{pulse_id}', 'P0_HOT_FIX', 'heartbeat', "
        f"        '{esc_sql(instruction)}', 'IN_PROGRESS', now()) "
        f"ON CONFLICT (pulse_id) DO UPDATE "
        f"SET created_at = now(), status = 'IN_PROGRESS', "
        f"instruction = EXCLUDED.instruction"
    )


def check_heartbeat(worker_name: str,
                    max_age_seconds: int = 600) -> tuple[bool, Optional[str]]:
    """Check if a worker's heartbeat is fresh.

    Returns:
        (is_alive, last_heartbeat_timestamp_utc)

    RESOLVED/IGNORED pulses (completed tests) count as alive
    — they are not stale, just finished.
    """
    pulse_id = f"heartbeat_{worker_name}"
    rows = psql_json(
        f"SELECT created_at::text AS created_at, status "
        f"FROM watchdog_pulses "
        f"WHERE pulse_id = '{pulse_id}'"
    )
    if not rows:
        return False, None

    # Completed / ignored workers are not stale
    status = rows[0].get("status", "")
    if status in ("RESOLVED", "IGNORED"):
        return True, rows[0].get("created_at")

    ts_str = rows[0]["created_at"]
    if ts_str is None:
        return False, None
    try:
        last_beat = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return False, ts_str
    age = (datetime.now(timezone.utc) - last_beat).total_seconds()
    return age < max_age_seconds, ts_str


def list_pulses(status: str = "PENDING", limit: int = 20) -> list:  # Python 3.9: list[dict] not supported
    """List pulses by status."""
    return psql_json(
        f"SELECT pulse_id, priority, category, instruction, target_file, "
        f"retry_count, max_retries, status, created_at "
        f"FROM watchdog_pulses "
        f"WHERE status = '{esc_sql(status)}' "
        f"ORDER BY created_at DESC LIMIT {limit}"
    )


def get_pulse(pulse_id: str) -> Optional[dict]:
    """Get single pulse by ID."""
    rows = psql_json(
        f"SELECT * FROM watchdog_pulses WHERE pulse_id = '{esc_sql(pulse_id)}'"
    )
    return rows[0] if rows else None

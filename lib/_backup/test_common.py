#!/usr/bin/env python3
# Status: production
# Path: imported by — scripts/tests/*
"""Shared test utilities — setup, heartbeat, cleanup, re-exports.

IMPORTANT: 모든 test script는 반드시 ``test_setup()`` / ``test_complete()``를
사용해야 합니다 (직접 ``stop_day_cycle()`` / ``start_day_cycle()`` 호출 불가).
``test_setup()``이 day_cycle service를 중단하고 ``test_complete()``가 재시작하여
inference 경합을 방지합니다. 이 함수들을 사용하지 않은 test script는 day_cycle과의
inference 충돌로 실패하거나 OOM이 발생할 수 있습니다.

Usage::

    from lib.test_common import test_setup, test_heartbeat, test_complete, log
    TEST = test_setup("14b_comparison_q8", "14B Q8 model comparison")

    test_heartbeat("batch 3/10 done")
    result = call_llm(messages, model="reviewer")
    test_complete()
"""

import atexit
import os
import signal
import sys
import time as _time

from lib.common import log
from lib.watchdog.messenger import heartbeat as _heartbeat
from lib.watchdog.messenger import resolve_pulse

# Ensure scripts dir is on path for direct execution
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# ── Day cycle control ──────────────────────────────────────────────────

_DAY_CYCLE_SVC = "devforge-day-cycle.service"


def stop_day_cycle():
    """Stop day-cycle service so it doesn't compete for inference during a test.

    Safe to call even if already stopped. Logs status either way.
    """
    r = os.system(f"systemctl --user stop {_DAY_CYCLE_SVC} 2>/dev/null")
    code = ">>" if r == 0 else "--"
    log(f"  [{code}] systemctl --user stop {_DAY_CYCLE_SVC}")
    _time.sleep(1)


def start_day_cycle():
    """Restart day-cycle service after a test completes.

    Day-cycle has no timer — watchdog or direct service start triggers it.
    """
    r = os.system(f"systemctl --user start {_DAY_CYCLE_SVC} 2>/dev/null")
    code = ">>" if r == 0 else "--"
    log(f"  [{code}] systemctl --user start {_DAY_CYCLE_SVC}")


# Module-level state
_test_name: str = ""
_test_start: float = 0.0
_test_cleanup_done: bool = False


def test_setup(name: str, description: str = "") -> dict:
    """Register test heartbeat + signal handlers + atexit cleanup.

    Call once at the top of every test script.
    Returns dict with keys: name, description, start_time, scripts_dir, pulse_id.

    The heartbeat pulse ``heartbeat_test_{name}`` is created with IN_PROGRESS.
    Watchdog automatically discovers it and monitors for staleness.
    """
    global _test_name, _test_start, _test_cleanup_done
    _test_name = name
    _test_start = _time.time()
    _test_cleanup_done = False

    pulse_id = f"test_{name}"

    # Stale pulse guard — auto-resolve any leftover IN_PROGRESS pulse first.
    # This replaces the old "DUPLICATE DETECTED → sys.exit(1)" pattern which
    # was fragile: TaskStop/SIGKILL could leave the pulse stuck, blocking all
    # future runs. The resolve-first approach is idempotent - if the previous
    # run already cleaned up, this is a no-op; if not, it unblocks us.
    try:
        psql_ok(
            f"UPDATE watchdog_pulses SET status = 'RESOLVED', resolved_at = now() "
            f"WHERE pulse_id = 'heartbeat_{pulse_id}' AND status = 'IN_PROGRESS'"
        )
    except Exception:
        pass  # DB unavailable → best-effort

    # Register heartbeat (creates or upserts IN_PROGRESS)
    _heartbeat(pulse_id, detail="started")

    # Stop day-cycle — prevents inference contention during test
    stop_day_cycle()

    def _cleanup(signum=None, frame=None):
        global _test_cleanup_done
        if _test_cleanup_done:
            return
        _test_cleanup_done = True
        elapsed = _time.time() - _test_start
        reason = "atexit"
        if signum == signal.SIGTERM:
            reason = "SIGTERM"
        elif signum == signal.SIGINT:
            reason = "SIGINT"
        resolve_pulse(f"heartbeat_{pulse_id}")
        log(f"[test:{name}] Cleanup ({reason}, {elapsed:.0f}s)")

        # Restart day-cycle (stopped at setup) — critical on abrupt exit
        start_day_cycle()

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)
    atexit.register(_cleanup)

    log(f"[test:{name}] {'=' * 50}")
    log(f"[test:{name}] Started — {description or name}")
    log(f"[test:{name}] {'=' * 50}")

    return {
        "name": name,
        "description": description,
        "start_time": _test_start,
        "scripts_dir": _SCRIPTS_DIR,
        "pulse_id": f"heartbeat_{pulse_id}",
    }


def test_heartbeat(detail: str = ""):
    """Update test heartbeat with progress detail.

    Call periodically during long-running tests so the watchdog
    can distinguish "still running" from "hung".
    """
    if not _test_name:
        return
    _heartbeat(f"test_{_test_name}", detail=detail)
    log(f"[test:{_test_name}] {detail}")


def test_complete(detail: str = "completed"):
    """Mark test heartbeat as RESOLVED + log final status.

    Watchdog will NOT report this heartbeat as stale since the
    pulse status is RESOLVED (non-running).
    """
    global _test_cleanup_done
    if not _test_name or _test_cleanup_done:
        return
    _test_cleanup_done = True

    elapsed = _time.time() - _test_start
    msg = f"{detail} ({elapsed:.0f}s)"
    _heartbeat(f"test_{_test_name}", detail=msg)
    resolve_pulse(f"heartbeat_{_test_name}")
    log(f"[test:{_test_name}] Completed — {msg}")

    # Restart day-cycle (stopped at setup)
    start_day_cycle()

#!/usr/bin/env python3
# Status: production
# Path: imported by — exp_runner.py, prj_cycle.py, preflight.py, watchdog
"""Experiment state file — 실험 중 watchdog과 pipeline 간 상태 공유.

실험 시작 시 .experiment_state.json 생성, 종료 시 정리.
watchdog: 파일 존재 + PID alive → monitor-only mode (mode 변경/서비스 재시작 안 함).
"""

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "data", "experiment",
)
STATE_FILE = os.path.join(STATE_DIR, ".experiment_state.json")
os.makedirs(STATE_DIR, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")




def read_state() -> Dict[str, Any]:
    """Read experiment_state.json. Returns empty dict if not exists or invalid."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def is_experiment_active() -> bool:
    """Check if experiment is currently running (state file + alive PID).

    Returns True only if:
      1. .experiment_state.json exists
      2. The PID in it is alive
      3. The process cmdline matches expected pattern
    """
    state = read_state()
    if not state:
        return False
    pid = state.get("pid", 0)
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except OSError:
        return False


def is_experiment_stale() -> bool:
    """Check if state file exists but PID is dead (stale)."""
    state = read_state()
    if not state:
        return False
    if not state.get("pid"):
        return False
    return not is_experiment_active()


def cleanup_stale():
    """Remove stale experiment state file (PID dead). Idempotent."""
    if is_experiment_stale():
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass


def update_state(**kwargs):
    """Update specific fields in experiment_state.json. Creates if not exists."""
    state = read_state()
    state.update(kwargs)
    state["last_updated"] = _utc_now()
    _write_atomic(state)


def mark_phase_complete(phase_name: str) -> None:
    """Append phase_name to completed_phases in experiment state. No-op if no experiment."""
    state = read_state()
    if not state:
        return
    completed = state.get("completed_phases", [])
    if not isinstance(completed, list):
        completed = []
    if phase_name not in completed:
        update_state(completed_phases=completed + [phase_name])


def _write_atomic(state: dict):
    """Atomic write via temp file + rename."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.rename(tmp, STATE_FILE)




class ExperimentState:
    """Context manager for experiment lifecycle.

    Usage:
        with ExperimentState(phase=0, ports={"pod_a": 8080, "pod_b": 8082}):
            ...  # state file exists during this block

    On __enter__: writes .experiment_state.json
    On __exit__:  removes .experiment_state.json
    """

    DEFAULT_PORTS = {"pod_a": 8080, "pod_b": 8082, "verify": 8084}

    def __init__(
        self,
        phase: int = 0,
        phases: Optional[list] = None,
        ports: Optional[dict] = None,
        step: str = "starting",
    ):
        self._phase = phase
        self._phases = phases or [phase]
        self._ports = ports or dict(self.DEFAULT_PORTS)
        self._step = step

    def __enter__(self):
        state = {
            "pid": os.getpid(),
            "phases": self._phases,
            "current_phase": self._phase,
            "step": self._step,
            "ports": self._ports,
            "started_at": _utc_now(),
            "last_updated": _utc_now(),
            "error_log": None,
            "fix_attempts": 0,
        }
        _write_atomic(state)
        return state

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except OSError:
            pass
        return False  # don't suppress exceptions

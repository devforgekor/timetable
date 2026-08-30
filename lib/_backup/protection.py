#!/usr/bin/env python3
# Status: production
# Path: imported by — pod_manager.py, embed_batch.py, test_common.py, tests/*
"""Protection context manager — file-based dead man's switch.

Any process can register a protection context via ``protect()`` to prevent:
  - Timer/cycle from stopping inference
  - Port-stray-kill from killing its ports
  - Preflight from resetting mode

Protection files are JSON at ``/opt/ai_data/scripts/.protect_{context}``
with PID, reason, ports, and start_time. Stale files (PID dead) are
automatically ignored by query functions.
"""

import json
import os
import signal
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Set

from lib.common import log

PROTECT_DIR = "/opt/ai_data/scripts"


def _protect_path(context: str) -> str:
    return os.path.join(PROTECT_DIR, f".protect_{context}")


def _read_protect(context: str) -> Optional[dict]:
    """Read protection file; return None if missing, stale, or corrupt."""
    path = _protect_path(context)
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    pid = data.get("pid", 0)
    if pid and not _pid_alive(pid):
        return None  # stale — owner process is dead

    return data


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive without raising."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_protect(context: str, reason: str = "", ports: Optional[List[int]] = None):
    """Write protection file atomically (temp + rename)."""
    path = _protect_path(context)
    data = {
        "pid": os.getpid(),
        "reason": reason,
        "ports": ports or [],
        "start_time": time.time(),
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.rename(tmp, path)
    except OSError:
        pass


def _remove_protect(context: str):
    """Remove protection file."""
    try:
        os.unlink(_protect_path(context))
    except FileNotFoundError:
        pass


# ── Public init/teardown (alternative to context manager) ────────────


def register_protect(context: str, reason: str = "",
                     ports: Optional[List[int]] = None):
    """Write protection file. Call unregister_protect() on cleanup."""
    _write_protect(context, reason=reason, ports=ports)
    log(f"[protect:{context}] ON ports={ports or []}")


def unregister_protect(context: str):
    """Remove protection file."""
    _remove_protect(context)
    log(f"[protect:{context}] OFF")


# ── Public API ──────────────────────────────────────────────────────


def active_contexts() -> List[str]:
    """Return non-stale protection context names."""
    if not os.path.isdir(PROTECT_DIR):
        return []
    try:
        names = []
        for entry in os.listdir(PROTECT_DIR):
            if entry.startswith(".protect_"):
                ctx = entry[len(".protect_"):]
                if _read_protect(ctx) is not None:
                    names.append(ctx)
        return sorted(names)
    except OSError:
        return []


def protected_ports() -> Set[int]:
    """Aggregate all ports protected by any non-stale context."""
    ports: Set[int] = set()
    if not os.path.isdir(PROTECT_DIR):
        return ports
    try:
        for entry in os.listdir(PROTECT_DIR):
            if entry.startswith(".protect_"):
                ctx = entry[len(".protect_"):]
                data = _read_protect(ctx)
                if data is not None:
                    ports.update(data.get("ports", []))
    except OSError:
        pass
    return ports


def is_protected(context: str) -> bool:
    """Check if a specific protection context is active (non-stale)."""
    return _read_protect(context) is not None


@contextmanager
def protect(context: str, reason: str = "",
            ports: Optional[List[int]] = None) -> Iterator[None]:
    """Register protection for a context. Guaranteed cleanup on exit.

    Usage::

        with protect("embed_batch", "backfill embedding", ports=[8081]):
            run_embed()
    """
    port_str = f" ports={ports}" if ports else ""
    log(f"[protect:{context}] ON{port_str} — {reason or context}")
    _write_protect(context, reason=reason, ports=ports)

    prev = signal.getsignal(signal.SIGTERM)
    handler = lambda s, f: (_remove_protect(context), os._exit(1))

    try:
        signal.signal(signal.SIGTERM, handler)
        yield
    finally:
        signal.signal(signal.SIGTERM, prev)
        _remove_protect(context)
        log(f"[protect:{context}] OFF")


def run_protected(context: str, fn: Callable[[], Any],
                  retries: int = 3, ports: Optional[List[int]] = None,
                  reason: str = "") -> Any:
    """Run a callable under protection with auto-retry on exception.

    The protection is active for each attempt — if attempt N fails,
    protection is released, then re-acquired for attempt N+1.

    Returns the callable's return value, or raises the last exception
    after exhausting retries.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with protect(f"{context}", reason=reason, ports=ports):
                return fn()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                log(f"[protect:{context}] attempt {attempt}/{retries} failed: {e} — retry")
                time.sleep(5)
            else:
                log(f"[protect:{context}] all {retries} attempts failed")
                raise
    raise RuntimeError("unreachable")  # type: ignore[misc]

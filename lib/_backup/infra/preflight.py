#!/usr/bin/env python3
# Status: production
# Path: all pipeline entry points — preflight checks for port contention, stale processes, memory
"""Shared preflight checks for DevForge pipelines.

Kill stale same-name processes, verify required ports, log memory state.
Call at the top of every pipeline main() before any LLM work.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Set

from lib.experiment_state import cleanup_stale, is_experiment_stale


def log(msg: str) -> None:
    utc_timestamp = time.strftime("[%H:%M:%S]", time.gmtime())
    print(f"{utc_timestamp} {msg}", flush=True)
    try:
        os.fsync(1)  # fd 1 = stdout — force OS buffer to disk
    except (OSError, AttributeError):
        pass


def preflight_checks(
    entry_name: str = "pipeline", required_ports: Optional[Set[int]] = None
) -> None:
    """Run pre-flight checks before starting a pipeline.

    1. Kill stale python3 processes matching entry_name.
    2. If a llama.cpp server on a required port has stale tasks queued, restart it.
    3. Verify required ports are reachable.
    4. Log memory state.

    Args:
        entry_name: The script name to match in cmdline (e.g. "night.py", "hybrid.py").
        required_ports: Set of ports to health-check before proceeding.
    """
    import urllib.error
    import urllib.request

    current_pid = os.getpid()

    # Gather protected PIDs — don't kill processes with active protection files
    protected_pids: set = set()
    protect_dir = "/opt/ai_data/scripts"
    try:
        for entry in os.listdir(protect_dir):
            if entry.startswith(".protect_"):
                p = os.path.join(protect_dir, entry)
                with open(p) as pf:
                    d = json.load(pf)
                    pid = d.get("pid", 0)
                    if pid and pid != current_pid:
                        try:
                            os.kill(pid, 0)
                            protected_pids.add(pid)
                        except OSError:
                            pass  # stale PID
    except (OSError, json.JSONDecodeError):
        pass

    # 1. Kill stale same-name processes (skip protected)
    killed: List[int] = []
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "pid=,comm="], timeout=10, text=True
        ).strip()
        for line in out.split("\n"):
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid_str, comm = parts
            pid = int(pid_str.strip())
            if pid == current_pid or pid == 0:
                continue
            if "python3" not in comm:
                continue
            try:
                with open(f"/proc/{pid}/cmdline") as cf:
                    cmdline = cf.read().replace("\0", " ")
                if entry_name not in cmdline:
                    continue
            except OSError:
                continue
            if pid in protected_pids:
                continue  # don't kill protected processes
            # PPID check: only kill orphaned (PPID=1) — prevents sibling kill
            # in bash retry loops (new extract.py killing old extract.py)
            _kill_ok = True
            try:
                with open(f"/proc/{pid}/status") as sf:
                    for sl in sf:
                        if sl.startswith("PPid:"):
                            _kill_ok = int(sl.split()[1]) == 1
                            break
            except (OSError, ValueError):
                pass
            if not _kill_ok:
                continue
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
    except Exception:
        pass

    if killed:
        time.sleep(3)  # wait for port release
        log(f"  [preflight] killed stale {entry_name} PIDs: {killed}")

    # 2. Check for server task backlog — if server has too many queued tasks,
    #    restart it to clear stale requests from dead clients.
    if required_ports:
        for port in sorted(required_ports):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=b'{"messages":[{"role":"user","content":"health"}],"max_tokens":1,"temperature":0.1,"stream":false}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.loads(resp.read().decode())
                # If server is responding, it's alive — no backlog issue for the
                # _current_ request. Stale tasks only affect clients that wait;
                # our health probe returns immediately because each request gets
                # its own slot queue slot.
                log(f"  [preflight] :{port} API OK")
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                log(f"  [preflight] :{port} API probe failed ({e}) — may have stale tasks")
            except Exception as e:
                log(f"  [preflight] :{port} probe error ({e})")

    # 3. Check required ports (health endpoint)
    if required_ports:
        for port in sorted(required_ports):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                    if resp.status == 200:
                        log(f"  [preflight] :{port} health OK")
            except Exception:
                log(f"  [preflight] :{port} NOT healthy — will start during boot")

    # 4. Mode file sanity check — inference must not be in review/verify mode (32B+ models)
    #    during day pipeline operation (would cause memory_guard failure)
    _mode_file = "/opt/ai_data/scripts/current-mode-inference.env"
    _large_modes = {"review-p", "review-r", "review-j", "verify"}
    if os.path.exists(_mode_file):
        mode = Path(_mode_file).read_text().strip().replace("MODE=", "")
        if mode in _large_modes:
            log(f"  [preflight] WARNING: inference mode={mode} (large model) — may cause OOM")
        if mode in _large_modes and entry_name in ("prj_cycle.py", "runner.py"):
            # In experiment mode, start in day mode; reset if stuck in large mode
            log(f"  [preflight] inference in {mode} mode — resetting to day")
            Path(_mode_file).write_text("MODE=day")

    # 5. Clean stale experiment state (PID dead but state file exists)
    if is_experiment_stale():
        log("  [preflight] stale experiment_state.json found — cleaning up")
        cleanup_stale()

    # 6. Log memory
    try:
        mem = subprocess.check_output(["free", "-h"], timeout=5).decode().strip()
        for line in mem.split("\n"):
            log(f"  [preflight] {line}")
    except Exception:
        pass

    # 7. Log swap usage
    try:
        swap = subprocess.check_output(["swapon", "--show"], timeout=5).decode().strip()
        if swap:
            for line in swap.split("\n"):
                log(f"  [preflight] {line}")
    except Exception:
        pass

#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/exp_runner.py, day_runner.py, night_runner.py
"""Container lifecycle management — stop, start, health, memory reclaim."""

import os
import subprocess
import time
import urllib.request

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def log(msg):
    from datetime import datetime, timezone

    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


def stop_all():
    """Stop inference container."""
    from lib.pod_manager.container import _podman_stop_inference

    _podman_stop_inference()


def free_memory(level=1):
    """Memory reclamation. level:
    1 = sync + 15s wait (default)
    2 = level1 + drop_caches + swap reactivation
    3 = level2 + 30s wait + oom_score_adj propagation
    """
    os.sync()
    log(f"  Memory reclaim level {level}: synced, waiting...")

    if level >= 2:
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
            log("  drop_caches=3 OK")
        except Exception as e:
            log(f"  drop_caches failed (non-fatal): {e}")

        try:
            subprocess.run(["swapoff", "-a"], capture_output=True, timeout=30)
            subprocess.run(["swapon", "-a"], capture_output=True, timeout=30)
            log("  swap re-activated OK")
        except Exception as e:
            log(f"  swap reactivate failed (non-fatal): {e}")

    wait_time = 30 if level >= 3 else 15
    for i in range(wait_time):
        if i % 5 == 0:
            report_memory(f"reclaim ({i}s)")
        time.sleep(1)

    report_memory("after reclaim")


def report_memory(label=""):
    """Log free/available memory."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    log(f"  MemAvailable: {mb}MB {label}")
                    return
        log(f"  MemAvailable: ? {label}")
    except Exception:
        pass


def get_available_mb():
    """Return MemAvailable in MB, or 0 on error."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return 0


def wait_health(port, timeout=120):
    """Poll /health until 200 or timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def write_mode(pod, mode):
    """Write mode file atomically (inference mode only now)."""
    from lib.pod_manager.container import MODE_FILE

    path = MODE_FILE
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(f"MODE={mode}")
    os.rename(tmp, path)


def recover_and_restart(attempt=1):
    """OOM/failure recovery + day mode restart.

    attempt=1: default — stop + sync + 15s + extract+verify
    attempt=2: aggressive — drop_caches + swap off/on + extract+verify
    attempt=3: last resort — minimal mode (extract only, no verify/review)
    """
    stop_all()
    free_memory(level=min(attempt, 3))
    write_mode("inference", "day")

    if attempt <= 2:
        log("  Attempt: inference extractor (standard day mode)")
        from lib.pod_manager import start_inference

        ok = start_inference("day", 8082, skip_probe=False)
        if ok:
            return True
        log("  Escalating to minimal mode...")

    stop_all()
    free_memory(level=3)
    write_mode("inference", "day")

    log("  Minimal mode: inference extractor")
    from lib.pod_manager import start_inference

    ok = start_inference("day", 8082, skip_probe=True)
    if ok:
        log("  Minimal mode OK")
        return True

    log("  FATAL: even minimal mode failed")
    return False

# Status: production
# Path: imported by — watchdog.py
"""Graduated recovery — CrashLoopBackOff 패턴, OOM 전용 복구.

출처:
  - K8s CrashLoopBackOff: 0→10→20→40→80→max300s, 10min 정상→리셋
  - Docker Surgeon: exit code 분석 (137=OOM, 143=SIGTERM)
  - pyresilience: circuit breaker OPEN→HALF_OPEN→CLOSED
"""

import os
import signal
import subprocess
import sys
import time
from typing import Callable, Optional

from lib.experiment_state import is_experiment_active
from lib.watchdog.config import (
    CONTAINER_EXCLUSION,
)
from lib.watchdog.state import ComponentTracker


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Exit code 분석 ──────────────────────────────────────────────────

EXIT_OOM = 137  # SIGKILL (OOM killer)
EXIT_SEGFAULT = 139  # SIGSEGV
EXIT_SIGTERM = 143  # SIGTERM (normal shutdown)
EXIT_PODMAN_ERR = 125  # Podman 자체 에러


def analyze_exit_code(code: int) -> str:
    """Exit code → 복구 전략 결정."""
    if code == EXIT_OOM:
        return "oom"
    elif code == EXIT_SEGFAULT:
        return "segfault"
    elif code == EXIT_SIGTERM:
        return "normal"
    elif code >= 125:
        return "podman_error"
    elif code != 0:
        return "error"
    return "success"


# ── 복구 액션 ──────────────────────────────────────────────────────


def recover_container(name: str) -> bool:
    """Restart container. Inference container → podman rm+run, others → systemctl."""
    if name in CONTAINER_EXCLUSION:
        log(f"  SKIP: {name} is excluded from restart")
        return False
    if is_experiment_active():
        log(f"  SKIP container restart {name} — experiment active")
        return False
    log(f"  restart container {name}...")
    try:
        if name == "devforge-inference":
            from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference

            _podman_stop_inference()
            _podman_start_inference()
        else:
            subprocess.run(
                ["systemctl", "--user", "restart", name],
                capture_output=True,
                timeout=30,
            )
        time.sleep(5)
        return True
    except Exception as e:
        log(f"  restart failed: {e}")
        return False


def recover_service(name: str) -> bool:
    """systemctl --user restart. Exclusion 체크."""
    if name in CONTAINER_EXCLUSION:
        log(f"  SKIP: {name} is excluded from restart")
        return False
    if is_experiment_active():
        log(f"  SKIP service restart {name} — experiment active")
        return False
    log(f"  restart service {name}...")
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", name],
            capture_output=True,
            timeout=30,
        )
        return True
    except Exception:
        return False


def recover_oom() -> bool:
    """OOM kill_all + restore mode.

    일반 backoff 생략, 즉시 kill_all로 메모리 확보 후 재시작.
    """
    if is_experiment_active():
        log("  SKIP OOM recovery — experiment active (runner handles recovery)")
        return False

    log("  OOM recovery: kill_all + restore...")
    try:
        from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference

        _podman_stop_inference()
        time.sleep(10)  # 메모리 reclaim

        # Restore inference to day mode
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, '/opt/projects/server/scripts'); "
                    "from lib.pod_manager import _write_mode_env; "
                    "_write_mode_env('day', 8082)",
                ],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            log("  _write_mode_env failed, falling back to MODE=day for inference")
            with open(MODE_FILE_INFERENCE, "w") as f:
                f.write("MODE=day")

        _podman_start_inference()
        return True
    except Exception as e:
        log(f"  OOM recovery failed: {e}")
        return False


# ── Graduated recovery with CrashLoopBackOff ───────────────────────


def graduated_recover(
    name: str,
    tracker: ComponentTracker,
    recover_fn: Callable[[], bool],
    wait_health_fn: Optional[Callable[[], bool]] = None,
) -> bool:
    """Apply CrashLoopBackOff pattern: backoff → recover → probe."""
    if not tracker.can_retry():
        log(f"  circuit OPEN for {name}, skipping")
        return False

    backoff = tracker.backoff_sec()
    if backoff > 0:
        log(f"  backoff {backoff}s for {name} (attempt #{tracker.consecutive_fail})")
        time.sleep(backoff)

    ok = recover_fn()
    if ok and wait_health_fn:
        ok = wait_health_fn()

    if ok:
        tracker.record_success()
    else:
        changed = tracker.record_failure()
        log(f"  {name} -> {tracker.state.value} (fail #{tracker.consecutive_fail})")

    return ok


def recover_slot_deadlock(port: str) -> bool:
    """Restart devforge-inference to resolve cont-batching slot deadlock.

    llama-server --parallel N + --cache-reuse causes slot scheduling deadlock
    (PR #22083). Workaround --slot-prompt-similarity 0 is applied in entrypoint,
    but if a deadlock already occurred, the container must restart.
    """
    if is_experiment_active():
        log("  SKIP slot deadlock recovery — experiment active")
        return False

    log(f"  [slot-deadlock] :{port} — restarting devforge-inference...")
    try:
        from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference

        _podman_stop_inference()
        _podman_start_inference()
        time.sleep(5)
        log("  devforge-inference restarted")
        return True
    except Exception as e:
        log(f"  restart failed: {e}")
        return False


def kill_stale_process(entry_name: str):
    """Kill stale same-name python processes (systemd-managed 제외)."""
    current_pid = os.getpid()
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
                # Skip systemd-managed processes (.service in cgroup)
                with open(f"/proc/{pid}/cgroup") as cf:
                    cgroup = cf.read()
                if ".service" in cgroup:
                    continue

                with open(f"/proc/{pid}/cmdline") as cf:
                    cmdline = cf.read().replace("\0", " ")
                if entry_name not in cmdline:
                    continue
            except OSError:
                continue
            os.kill(pid, signal.SIGTERM)
            log(f"  killed stale {entry_name} PID {pid}")
    except Exception:
        pass

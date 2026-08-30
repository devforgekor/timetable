#!/usr/bin/env python3
# Status: experimental
# Path: imported by — orchestrator.py
"""Test execution sandbox — Podman read-only tmpfs for safe code execution.

Usage:
    from lib.test_sandbox import run_code_in_sandbox

    result = run_code_in_sandbox('print("hello world")')
    # => {"ok": True, "stdout": "hello world\\n", "stderr": "", "exit_code": 0,
    #     "timed_out": False, "elapsed": 0.85}

    result = run_code_in_sandbox('import os; os.system("rm -rf /")')
    # => {"ok": False, "stderr": "... Read-only file system ...", "exit_code": 1}
"""

import os
import shutil
import subprocess
import tempfile
import time
from typing import Dict

SANDBOX_IMAGE = "python:3.12-slim"
SANDBOX_TIMEOUT = 60          # max wall-clock seconds (container-level)
SANDBOX_MEMORY = "256m"       # memory limit
SANDBOX_TMPFS_SIZE = "64M"    # size of writable /tmp
_PODMAN_BIN = "podman"


def run_code_in_sandbox(code: str,
                        timeout: int = SANDBOX_TIMEOUT,
                        memory: str = SANDBOX_MEMORY,
                        image: str = SANDBOX_IMAGE,
                        network: bool = False) -> Dict:
    """Run Python code in an isolated Podman sandbox.

    The container runs with:
      - read-only root filesystem
      - writable tmpfs at /tmp (64MB)
      - no network (unless network=True)
      - bounded memory
      - automatic cleanup (--rm)
      - container-level timeout

    Args:
        code: Python source code to execute (via python3 -c)
        timeout: Max wall-clock seconds (default 60)
        memory: Container memory limit (default 256m)
        image: OCI image to use (default python:3.12-slim)
        network: Allow network access (default False)

    Returns:
        Dict with keys:
          ok:        True if exit_code == 0
          stdout:    captured stdout text
          stderr:    captured stderr text
          exit_code: exit code from python3
          timed_out: True if process was killed by container timeout
          elapsed:   wall-clock seconds
    """
    start = time.monotonic()
    result = {
        "ok": False,
        "stdout": "",
        "stderr": "",
        "exit_code": -1,
        "timed_out": False,
        "elapsed": 0.0,
    }

    # Write code to a temp file for mounting read-only
    tmp_dir = tempfile.mkdtemp(prefix="sandbox_")
    script_path = os.path.join(tmp_dir, "script.py")
    try:
        with open(script_path, "w") as f:
            f.write(code)

        # Build podman run arguments
        podman_args = [
            _PODMAN_BIN, "run", "--rm",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={SANDBOX_TMPFS_SIZE}",
            "--memory", memory,
            "--workdir", "/tmp",
            "--timeout", str(timeout),
            "-v", f"{script_path}:/script.py:ro,Z",
        ]

        if not network:
            podman_args.append("--network=none")

        podman_args += [image, "python3", "/script.py"]

        proc = subprocess.run(
            podman_args, capture_output=True, timeout=timeout + 30,
        )

        result["stdout"] = proc.stdout.decode("utf-8", errors="replace")
        result["stderr"] = proc.stderr.decode("utf-8", errors="replace")
        result["exit_code"] = proc.returncode
        result["ok"] = proc.returncode == 0

        # Detect container-level timeout: podman exits 255 when --timeout kills
        elapsed = time.monotonic() - start
        if proc.returncode != 0 and elapsed >= timeout - 1:
            result["timed_out"] = True
            result["stderr"] = f"Execution timed out after {timeout}s"

    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["stderr"] = f"Execution timed out after {timeout}s"
    except FileNotFoundError:
        result["stderr"] = f"Podman not found at {_PODMAN_BIN}"
    except Exception as e:
        result["stderr"] = str(e)
    finally:
        # Cleanup temp dir (suppress errors — file may be Z-labeled by podman)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        result["elapsed"] = round(time.monotonic() - start, 2)

    return result


def sandbox_available() -> bool:
    """Check if the sandbox (podman + image) is available."""
    try:
        proc = subprocess.run(
            [_PODMAN_BIN, "image", "exists", SANDBOX_IMAGE],
            capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


#!/usr/bin/env python3
# Status: production
# Path: sourced by — day_cycle.sh, night_cycle.sh (via model_ctl.sh delegation)
"""CLI for inference container model management.

Usage:
    python3 -m lib.model_ctl <command> [args]

Commands:
    model-port <model_key>            — resolve port from registry
    model-env-vars <model_key>        — resolve env vars (prints KEY=VAL lines)
    write-mode-env <model_key> [port] — write MODE_ENV file
    wait-health <port> [max_wait]     — wait for /health endpoint
    wait-probe <port> [max_wait]      — wait for LLM probe response
    check-model-id <port> <model_key> — verify model fingerprint matches
    test-heartbeat-active             — check for active test heartbeat
    stop-model                        — stop inference container
    run-model <model_key> [port] [skip_probe] [health_timeout] — start + wait
    ensure-model <model_key> [port] [skip_probe] [health_timeout] — smart start
    kill-model                        — emergency stop
"""

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

SCRIPT_DIR = "/opt/projects/server/scripts"
MODEL_MODE_ENV = "/opt/ai_data/scripts/current-mode-inference.env"

INFERENCE_RUN_CMD = [
    "podman",
    "run",
    "-d",
    "--replace",
    "--name",
    "devforge-inference",
    "--rm",
    "--entrypoint",
    "/bin/bash",
    "--pull",
    "newer",
    "--network",
    "devforge-net",
    "-v",
    "/opt/ai_data/models/gguf:/models:Z",
    "-v",
    "/opt/ai_data/scripts/inference-entrypoint.sh:/entrypoint.d/inference-entrypoint.sh:Z",
    "-v",
    "/opt/ai_data/scripts/current-mode-inference.env:/entrypoint.d/current-mode.env:Z",
    "--publish",
    "127.0.0.1:8080:8080",
    "--publish",
    "127.0.0.1:8081:8081",
    "--publish",
    "127.0.0.1:8082:8082",
    "--publish",
    "127.0.0.1:8083:8083",
    "--publish",
    "127.0.0.1:8084:8084",
    "--env",
    "SERVER_TIMEOUT=28800",
    "ghcr.io/ggml-org/llama.cpp:server",
    "/entrypoint.d/inference-entrypoint.sh",
]

# Model registry key -> env var name mapping
ENV_VAR_MAP = {
    "ctx": "CTX_SIZE",
    "threads": "THREADS",
    "threads_batch": "THREADS_BATCH",
    "cache_ram": "CACHE_RAM",
    "mlock": "MLOCK",
    "evict_room": "EVICT_ROOM",
    "memory_check": "MEMORY_CHECK",
    "memory_check_mode": "MEMORY_CHECK_MODE",
    "report_memory": "REPORT_MEMORY",
    "cache_type_k": "CACHE_TYPE_K",
    "cache_type_v": "CACHE_TYPE_V",
    "flash_attn": "FLASH_ATTN",
    "batch_size": "BATCH_SIZE",
    "ubatch_size": "UBATCH_SIZE",
    "parallel": "PARALLEL",
    "cpus": "CPUS",
}


def _get_model_meta(model_key: str) -> dict:
    """Resolve model metadata from registry."""
    sys.path.insert(0, SCRIPT_DIR)
    from lib.model_registry import MODEL_METADATA  # noqa: PLC0415

    m = MODEL_METADATA.get(model_key, {})
    if not m:
        print(f"[model_ctl] ERROR: unknown model key '{model_key}'", file=sys.stderr)
        return {}
    return m


# ── Public CLI Commands ──────────────────────────────────────────────


def cmd_model_port(model_key: str) -> int:
    """Resolve port from registry."""
    m = _get_model_meta(model_key)
    return m.get("port", 8082)


def cmd_model_env_vars(model_key: str) -> Optional[str]:
    """Resolve full env vars from registry, return as KEY=VAL string."""
    m = _get_model_meta(model_key)
    if not m:
        return None
    pairs: list[tuple[str, str]] = [
        ("MODE", m.get("mode", "day")),
        ("MODEL_NAME", m.get("model_name", model_key)),
        ("PORT", str(m.get("port", 8082))),
        ("MODEL_FILE", m.get("file", "?")),
    ]
    for reg_key, env_key in ENV_VAR_MAP.items():
        v = m.get(reg_key)
        if v is not None and v != "":
            pairs.append((env_key, str(v)))
    return "\n".join(f"{k}={v}" for k, v in pairs) + "\n"


def cmd_write_mode_env(model_key: str, port: Optional[int] = None) -> bool:
    """Write mode env file from registry."""
    if port is None:
        port = cmd_model_port(model_key)
    env_data = cmd_model_env_vars(model_key)
    if env_data is None:
        return False
    with open(MODEL_MODE_ENV, "w") as f:
        f.write(env_data)
    print(f"[model_ctl] wrote env for {model_key} (:{port}) -> {MODEL_MODE_ENV}")
    return True


def cmd_wait_health(port: int, max_wait: int = 600) -> bool:
    """Wait for /health endpoint."""
    waited = 0
    while waited < max_wait:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as _resp:
                print(f"[model_ctl] healthy on :{port} ({waited}s)")
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
        waited += 2
    print(f"[model_ctl] health TIMEOUT on :{port} after {max_wait}s", file=sys.stderr)
    return False


def cmd_wait_probe(port: int, max_wait: int = 300) -> bool:
    """Wait for LLM probe (text generation works)."""
    waited = 0
    body = (
        b'{"messages":[{"role":"user","content":"hi"}],'
        b'"max_tokens":5,"temperature":0.1,"stream":false}'
    )
    while waited < max_wait:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as _resp:
                print("[model_ctl] probe OK")
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
        waited += 3
    print("[model_ctl] probe TIMEOUT", file=sys.stderr)
    return False


def cmd_check_model_id(port: int, model_key: str) -> bool:
    """Verify model fingerprint matches expected file."""
    m = _get_model_meta(model_key)
    expected = m.get("file", "")
    if not expected:
        return True  # no check needed
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json  # noqa: PLC0415

            data = json.loads(resp.read().decode())
            models = data.get("models", [])
            if not models:
                return False
            actual = models[0].get("model", "") or models[0].get("name", "")
            if not actual:
                return False
            actual = os.path.basename(actual)
            if actual == expected:
                return True
            print(
                f"[model_ctl] MISMATCH :{port} has {actual}, expected {expected}", file=sys.stderr
            )
            return False
    except Exception as e:
        print(f"[model_ctl] check_model_id error: {e}", file=sys.stderr)
        return False


def cmd_test_heartbeat_active() -> bool:
    """Check if a test heartbeat pulse is active."""
    sys.path.insert(0, SCRIPT_DIR)
    from lib.db import psql_json  # noqa: PLC0415

    rows = psql_json(
        "SELECT pulse_id FROM watchdog_pulses "
        "WHERE pulse_id LIKE 'heartbeat_test_%' AND status = 'IN_PROGRESS' LIMIT 1"
    )
    if rows:
        print(rows[0]["pulse_id"])
        return True
    return False


def cmd_stop_model() -> bool:
    """Stop the inference container."""
    print("[model_ctl] Stopping inference...")
    subprocess.run(
        ["podman", "rm", "-v", "-f", "-i", "devforge-inference"],
        capture_output=True,
        timeout=30,
    )
    print("[model_ctl] Inference stopped")
    return True


def cmd_run_model(
    model_key: str, port: Optional[int] = None, skip_probe: bool = False, health_timeout: int = 600
) -> bool:
    """Start inference container + wait for readiness."""
    if port is None:
        port = cmd_model_port(model_key)
    print(f"[model_ctl] INFERENCE -> {model_key} (:{port})")

    if not cmd_write_mode_env(model_key, port):
        return False

    # Start container
    try:
        r = subprocess.run(
            INFERENCE_RUN_CMD,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            print(
                f"[model_ctl] ERROR: failed to start inference: {r.stderr.strip()[:500]}",
                file=sys.stderr,
            )
            return False
    except subprocess.TimeoutExpired:
        print("[model_ctl] ERROR: podman run timed out (120s)", file=sys.stderr)
        return False

    # Wait for health
    if not cmd_wait_health(port, health_timeout):
        print("[model_ctl] health timeout - restarting", file=sys.stderr)
        subprocess.run(
            ["podman", "rm", "-v", "-f", "-i", "devforge-inference"],
            capture_output=True,
            timeout=30,
        )
        try:
            r = subprocess.run(
                INFERENCE_RUN_CMD,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode != 0:
                return False
        except subprocess.TimeoutExpired:
            return False
        if not cmd_wait_health(port, 300):
            return False

    # Probe (skip for embed-only models)
    if not skip_probe:
        if not cmd_wait_probe(port, health_timeout):
            return False

    print(f"[model_ctl] :{port} ready ({model_key})")

    # Verify identity (non-fatal warning on mismatch)
    if not cmd_check_model_id(port, model_key):
        print(f"[model_ctl] WARNING: model identity mismatch on :{port}", file=sys.stderr)

    return True


def cmd_ensure_model(
    model_key: str, port: Optional[int] = None, skip_probe: bool = False, health_timeout: int = 600
) -> bool:
    """Smart start: skip if already healthy with correct model."""
    if port is None:
        port = cmd_model_port(model_key)

    # Check if already healthy with correct model
    current_model = ""
    if os.path.isfile(MODEL_MODE_ENV):
        with open(MODEL_MODE_ENV) as f:
            for line in f:
                if line.startswith("MODEL_NAME="):
                    current_model = line.split("=", 1)[1].strip()
                    break

    if current_model == model_key:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as _resp:
                print(f"[model_ctl] Inference already {model_key} (:{port}) - skip restart")
                return True
        except (urllib.error.URLError, OSError):
            pass

    # Check test heartbeat
    if cmd_test_heartbeat_active():
        print("[model_ctl] Test active - skip inference restart")
        return True

    return cmd_run_model(model_key, port, skip_probe, health_timeout)


def cmd_kill_model() -> bool:
    """Emergency stop inference."""
    print("[model_ctl] kill: stopping inference...")
    cmd_stop_model()
    print("[model_ctl] kill complete")
    return True


# ── CLI Dispatch ────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Inference container model management")
    parser.add_argument(
        "command",
        choices=[
            "model-port",
            "model-env-vars",
            "write-mode-env",
            "wait-health",
            "wait-probe",
            "check-model-id",
            "test-heartbeat-active",
            "stop-model",
            "run-model",
            "ensure-model",
            "kill-model",
        ],
    )
    parser.add_argument("args", nargs="*", help="command arguments")
    opts = parser.parse_args()

    cmd = opts.command
    args = opts.args

    if cmd == "model-port":
        if len(args) < 1:
            print("Usage: model-port <model_key>", file=sys.stderr)
            sys.exit(1)
        port = cmd_model_port(args[0])
        print(port)
        sys.exit(0)

    elif cmd == "model-env-vars":
        if len(args) < 1:
            print("Usage: model-env-vars <model_key>", file=sys.stderr)
            sys.exit(1)
        result = cmd_model_env_vars(args[0])
        if result is None:
            sys.exit(1)
        print(result, end="")
        sys.exit(0)

    elif cmd == "write-mode-env":
        model_key = args[0] if len(args) > 0 else ""
        port = int(args[1]) if len(args) > 1 else None
        if not model_key:
            print("Usage: write-mode-env <model_key> [port]", file=sys.stderr)
            sys.exit(1)
        sys.exit(0 if cmd_write_mode_env(model_key, port) else 1)

    elif cmd == "wait-health":
        port = int(args[0]) if len(args) > 0 else 8082
        max_wait = int(args[1]) if len(args) > 1 else 600
        sys.exit(0 if cmd_wait_health(port, max_wait) else 1)

    elif cmd == "wait-probe":
        port = int(args[0]) if len(args) > 0 else 8082
        max_wait = int(args[1]) if len(args) > 1 else 300
        sys.exit(0 if cmd_wait_probe(port, max_wait) else 1)

    elif cmd == "check-model-id":
        if len(args) < 2:
            print("Usage: check-model-id <port> <model_key>", file=sys.stderr)
            sys.exit(1)
        sys.exit(0 if cmd_check_model_id(int(args[0]), args[1]) else 1)

    elif cmd == "test-heartbeat-active":
        sys.exit(0 if cmd_test_heartbeat_active() else 1)

    elif cmd == "stop-model":
        sys.exit(0 if cmd_stop_model() else 1)

    elif cmd == "kill-model":
        sys.exit(0 if cmd_kill_model() else 1)

    elif cmd == "run-model":
        model_key = args[0] if len(args) > 0 else ""
        port = int(args[1]) if len(args) > 1 else None
        skip_probe = args[2].lower() == "true" if len(args) > 2 else False
        health_timeout = int(args[3]) if len(args) > 3 else 600
        if not model_key:
            print(
                "Usage: run-model <model_key> [port] [skip_probe] [health_timeout]", file=sys.stderr
            )
            sys.exit(1)
        sys.exit(0 if cmd_run_model(model_key, port, skip_probe, health_timeout) else 1)

    elif cmd == "ensure-model":
        model_key = args[0] if len(args) > 0 else ""
        port = int(args[1]) if len(args) > 1 else None
        skip_probe = args[2].lower() == "true" if len(args) > 2 else False
        health_timeout = int(args[3]) if len(args) > 3 else 600
        if not model_key:
            print(
                "Usage: ensure-model <model_key> [port] [skip_probe] [health_timeout]",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(0 if cmd_ensure_model(model_key, port, skip_probe, health_timeout) else 1)


if __name__ == "__main__":
    main()

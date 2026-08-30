#!/usr/bin/env python3
# Status: production
"""Container health checks, model fingerprint, env writing, podman lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

from lib.model_registry import MODEL_METADATA

# ── Inference container: podman run --rm ───────────────────────────
INFERENCE_CONTAINER = "devforge-inference"
_INFERENCE_RUN_ARGS = [
    "podman",
    "run",
    "-d",
    "--replace",
    "--name",
    INFERENCE_CONTAINER,
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
    # Port 8080 is NOW published — Pod A is inactive, inference can serve reranker.
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


def _podman_start_inference():
    """Start inference container via podman run --rm. Returns True on success."""
    r = subprocess.run(_INFERENCE_RUN_ARGS, capture_output=True, timeout=120)
    if r.returncode != 0:
        err = r.stderr.strip()[:200] if r.stderr else "(no stderr)"
        log(f"  podman run failed (rc={r.returncode}): {err}")
        return False
    return True


def _podman_stop_inference():
    """Stop inference container via podman rm -f --volumes."""
    subprocess.run(
        ["podman", "rm", "-v", "-f", "-i", INFERENCE_CONTAINER],
        capture_output=True,
        timeout=30,
    )


MODE_FILE = "/opt/ai_data/scripts/current-mode-inference.env"
_cached_entrypoint = "/opt/ai_data/scripts/inference-entrypoint.sh"


def _ts():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}", flush=True)


def _reclaim_memory():
    try:
        from lib.infra.container_manager import free_memory

        free_memory(level=2)
    except ImportError:
        os.sync()
        log("  Memory reclaim (fallback): synced fs, waiting 15s...")
        time.sleep(15)


def _check_container_health(_port=None, _label=None):
    """Check inference container running state and entrypoint."""
    warnings = []
    try:
        r = subprocess.run(
            ["podman", "ps", "--filter", f"name={INFERENCE_CONTAINER}", "--format", "{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = r.stdout.strip()
        if not status:
            warnings.append("inference container not in podman ps — container may be dead")
        elif status.startswith("Up ") and "second" in status:
            warnings.append(f"inference just started ({status}) — may not be fully initialized")
        elif "unhealthy" in status:
            warnings.append("inference status=unhealthy — health check failing")
    except Exception:
        pass

    if not os.path.exists(_cached_entrypoint):
        warnings.append(f"entrypoint missing: {_cached_entrypoint}")

    for w in warnings:
        log(f"  [container-warn] {w}")
    return len(warnings) == 0, warnings


def _get_model_fingerprint(port):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            models = data.get("models", [])
            if models:
                path = models[0].get("model", "") or models[0].get("name", "")
                if path:
                    return os.path.basename(path)
    except Exception:
        pass
    return None


def _check_model_identity(port, model_key):
    expected_file = MODEL_METADATA.get(model_key, {}).get("file")
    if not expected_file:
        return True
    actual_file = _get_model_fingerprint(port)
    if not actual_file:
        return False
    ok = actual_file == expected_file
    if not ok:
        log(f"  [model-id] :{port} has {actual_file}, expected {expected_file}")
    return ok


def _write_mode_env(mode: str, port: int, model_key: str | None = None) -> None:
    """Write mode env for inference container. Single file — all models run here."""
    meta = None
    if model_key:
        meta = MODEL_METADATA.get(model_key)
    if meta is None:
        for v in MODEL_METADATA.values():
            if v.get("port") == port and (v.get("mode") == mode or v.get("model_name") == mode):
                meta = v
                break
    if meta is None:
        meta = MODEL_METADATA.get(mode)
        if meta and meta.get("port") != port:
            meta = None

    entrypoint_mode = meta["mode"] if meta else mode
    pairs = [("MODE", entrypoint_mode)]
    if meta:
        f = meta.get
        pairs += [
            ("MODEL_NAME", f("model_name", mode)),
            ("PORT", str(port)),
            ("MODEL_FILE", meta["file"]),
            ("CTX_SIZE", str(f("ctx", 8192))),
            ("THREADS", str(f("threads", 4))),
            ("THREADS_BATCH", str(f("threads_batch", 4))),
        ]
        for key, env_key in [
            ("cache_ram", "CACHE_RAM"),
            ("mlock", "MLOCK"),
            ("evict_room", "EVICT_ROOM"),
            ("memory_check", "MEMORY_CHECK"),
            ("memory_check_mode", "MEMORY_CHECK_MODE"),
            ("report_memory", "REPORT_MEMORY"),
            ("cache_type_k", "CACHE_TYPE_K"),
            ("cache_type_v", "CACHE_TYPE_V"),
            ("flash_attn", "FLASH_ATTN"),
            ("batch_size", "BATCH_SIZE"),
            ("ubatch_size", "UBATCH_SIZE"),
            ("parallel", "PARALLEL"),
            ("cpus", "CPUS"),
            ("cpu_range", "CPU_RANGE"),
            ("cpu_strict", "CPU_STRICT"),
        ]:
            val = f(key)
            if val is not None and val != "":
                pairs.append((env_key, str(val)))

    lines = [f"{k}={v}" for k, v in pairs]
    with open(MODE_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"  wrote env for {mode}:{port} → inference ({meta['file'] if meta else '?'})")


def _write_dual_env(model_key_a: str, model_key_b: str) -> None:
    """Write dual-server mode env for inference container.

    Two llama-server instances in one container, config via _A / _B suffixes.
    """
    meta_a = MODEL_METADATA.get(model_key_a)
    meta_b = MODEL_METADATA.get(model_key_b)
    if not meta_a or not meta_b:
        log(f"  FATAL: unknown model keys for dual: {model_key_a} / {model_key_b}")
        return

    lines = ["MODE=dual"]
    for prefix, meta in [("A", meta_a), ("B", meta_b)]:
        f = meta.get
        key_name = model_key_a if prefix == "A" else model_key_b
        field_map: list[tuple[str, str]] = [
            ("model_name", f"MODEL_NAME_{prefix}"),
            ("", f"PORT_{prefix}"),
            ("file", f"MODEL_FILE_{prefix}"),
            ("ctx", f"CTX_SIZE_{prefix}"),
            ("threads", f"THREADS_{prefix}"),
            ("threads_batch", f"THREADS_BATCH_{prefix}"),
        ]
        for meta_key, env_key in field_map:
            val = f(meta_key) if meta_key else meta.get("port")
            if val is not None:
                lines.append(f"{env_key}={val}")
        for meta_key, env_key in [
            ("cache_ram", f"CACHE_RAM_{prefix}"),
            ("mlock", f"MLOCK_{prefix}"),
            ("evict_room", f"EVICT_ROOM_{prefix}"),
            ("flash_attn", f"FLASH_ATTN_{prefix}"),
            ("batch_size", f"BATCH_SIZE_{prefix}"),
            ("ubatch_size", f"UBATCH_SIZE_{prefix}"),
            ("parallel", f"PARALLEL_{prefix}"),
            ("cpus", f"CPUS_{prefix}"),
            ("cpu_range", f"CPU_RANGE_{prefix}"),
            ("cpu_strict", f"CPU_STRICT_{prefix}"),
        ]:
            val = f(meta_key)
            if val is not None and val != "":
                lines.append(f"{env_key}={val}")

    with open(MODE_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"  wrote dual env: {model_key_a}(:{meta_a['port']}) + {model_key_b}(:{meta_b['port']})")

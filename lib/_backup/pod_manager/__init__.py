#!/usr/bin/env python3
# Status: production
"""Container management for DevForge — all models run in inference container."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from typing import Optional

from lib.model_registry import DAY_PHASE_MODELS, MODEL_METADATA, NIGHT_MODELS
from lib.pod_manager.container import (
    INFERENCE_CONTAINER,
    MODE_FILE,
    _check_container_health,
    _check_model_identity,
    _podman_start_inference,
    _podman_stop_inference,
    _reclaim_memory,
    _write_dual_env,
    _write_mode_env,
    log,
)

TIMEOUT = 7200


def model_info(key):
    m = MODEL_METADATA.get(key, {})
    return f"{key}({m.get('file', '?')} {m.get('size', '?')} :{m.get('port', '?')})"


def wait_health(port, timeout=600):
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


def wait_probe(port, model_name, timeout=300):
    t0 = time.monotonic()
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "temperature": 0.1,
            "stream": False,
        }
    ).encode()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                if data.get("choices") and data["choices"][0].get("message"):
                    log(f"  probe OK ({model_name})")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  probe TIMEOUT ({model_name})")
    return False


def kill_all(night=False, dry_run=False):
    """Stop inference container and optionally night cycle timers."""
    if dry_run:
        log("  [DRY] kill_all() skipped")
        return

    if night:
        _podman_stop_inference()
        for svc in (
            "devforge-day-cycle.service",
            "devforge-night-cycle.service",
            "devforge-night-cycle.timer",
        ):
            subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
            subprocess.run(
                ["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10
            )
        _reclaim_memory()
    else:
        _podman_stop_inference()
        _reclaim_memory()


def _start_and_wait(port, health_timeout, skip_probe, mode):
    """Start inference container and wait for health."""
    if not _podman_start_inference():
        log(f"  podman start FAILED — :{port} will not be available")
        return False
    ok = wait_health(port, timeout=health_timeout)
    if not ok:
        log(f"  :{port} health timeout — restarting container")
        _podman_stop_inference()
        if not _podman_start_inference():
            return False
        ok = wait_health(port, timeout=min(health_timeout, 300))
    if ok and not skip_probe:
        ok = wait_probe(port, mode, timeout=600)
    return ok


def start_inference(mode, port, night=False, dry_run=False, skip_probe=False, model_key=None):
    """Start inference container with the given model mode."""
    log(f"  INFERENCE -> {mode} (:{port})")
    _write_mode_env(mode, port, model_key=model_key)
    if not dry_run:
        kill_all(night=night)
    health_timeout = 1200 if night else 600
    _write_mode_env(mode, port, model_key=model_key)
    ok = _start_and_wait(port, health_timeout, skip_probe, mode)
    if ok and model_key:
        if not _check_model_identity(port, model_key):
            log(f"  :{port} wrong model after start — retrying with env re-write")
            _write_mode_env(mode, port, model_key=model_key)
            _podman_stop_inference()
            _podman_start_inference()
            ok = _start_and_wait(port, min(health_timeout, 300), skip_probe, mode)
            if ok:
                # Retry identity check with backoff — ARM loads ~70s, may not
                # be ready immediately even after probe passes
                for attempt in range(5):
                    if _check_model_identity(port, model_key):
                        break
                    log(
                        f"  :{port} model identity check #{attempt + 1} failed — waiting {10 * (attempt + 1)}s"
                    )
                    time.sleep(10 * (attempt + 1))
                    _write_mode_env(mode, port, model_key=model_key)
                else:
                    log(f"  FATAL: :{port} wrong model after 5 retries — continuing anyway")
    if ok:
        log(f"  :{port} ready")
        _check_container_health()
        time.sleep(5)
    return ok


def ensure_model(physical_name, skip_if_healthy=False, dry_run=False):
    """Start inference container with the requested model."""
    if dry_run:
        log(f"  [DRY] ensure_model({physical_name}) -> OK (mock)")
        return True
    meta = MODEL_METADATA.get(physical_name)
    if not meta:
        log(f"  Unknown model: {physical_name}")
        return False
    night = physical_name in NIGHT_MODELS
    if skip_if_healthy:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{meta['port']}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    if _check_model_identity(meta["port"], physical_name):
                        log(f"  :{meta['port']} already healthy and correct model — skip restart")
                        _check_container_health()
                        return True
                    else:
                        log(f"  :{meta['port']} healthy but wrong model — restart needed")
        except Exception:
            pass
    ok = start_inference(
        meta["mode"], meta["port"], night=night, dry_run=dry_run, model_key=physical_name
    )
    if ok:
        return True
    log(f"  ensure_model({physical_name}) failed — retrying after GC + 10s")
    _reclaim_memory()
    import gc as _gc

    _gc.collect()
    time.sleep(10)
    return start_inference(
        meta["mode"], meta["port"], night=night, dry_run=dry_run, model_key=physical_name
    )


def ensure_dual(
    model_key_a: str = "day-extractor",
    model_key_b: str = "day-extractor-b",
    skip_if_healthy: bool = True,
    dry_run: bool = False,
) -> bool:
    """Start inference container with two llama-servers (dual mode).

    Both servers run inside a single container via the entrypoint ``dual`` case.
    Uses _write_dual_env() to write A/B suffixed config to MODE_FILE.
    """
    if dry_run:
        log(f"  [DRY] ensure_dual({model_key_a}, {model_key_b}) -> OK (mock)")
        return True

    meta_a = MODEL_METADATA.get(model_key_a)
    meta_b = MODEL_METADATA.get(model_key_b)
    if not meta_a or not meta_b:
        log(f"  Unknown model key(s): {model_key_a}/{model_key_b}")
        return False

    night = model_key_a in NIGHT_MODELS or model_key_b in NIGHT_MODELS

    if skip_if_healthy:
        try:
            healthy_a = False
            req = urllib.request.Request(f"http://127.0.0.1:{meta_a['port']}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                healthy_a = r.status == 200
            healthy_b = False
            req = urllib.request.Request(f"http://127.0.0.1:{meta_b['port']}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                healthy_b = r.status == 200
            if healthy_a and healthy_b:
                id_a = _check_model_identity(meta_a["port"], model_key_a)
                id_b = _check_model_identity(meta_b["port"], model_key_b)
                if id_a and id_b:
                    log(
                        f"  Dual already healthy — {model_key_a}(:{meta_a['port']}) + {model_key_b}(:{meta_b['port']})"
                    )
                    _check_container_health()
                    return True
        except Exception:
            pass

    log(f"  Ensure dual: {model_key_a}(:{meta_a['port']}) + {model_key_b}(:{meta_b['port']})")
    _write_dual_env(model_key_a, model_key_b)
    kill_all(night=night)
    _reclaim_memory()
    if not _podman_start_inference():
        log("  dual start FAILED — inference container could not start")
        return False

    ok_a = wait_health(meta_a["port"], timeout=600)
    ok_b = wait_health(meta_b["port"], timeout=600)

    if ok_a and not _check_model_identity(meta_a["port"], model_key_a):
        log(f"  FATAL: :{meta_a['port']} wrong model after dual start")
    if ok_b and not _check_model_identity(meta_b["port"], model_key_b):
        log(f"  FATAL: :{meta_b['port']} wrong model after dual start")

    _check_container_health()
    time.sleep(5)
    return ok_a and ok_b


def ensure_sequential_dual(
    model_key_a: str = "day-extractor",
    model_key_b: str = "day-extractor-b",
    dry_run: bool = False,
) -> bool:
    """Start inference with primary model, then launch secondary via podman exec.

    Unlike ensure_dual which relies on the entrypoint's dual case (and the fragile
    shared MODE_FILE), this starts the container in single mode then exec's a
    second llama-server inside it. Avoids MODE_FILE race conditions between
    ensure_model/recover_8082 and dual mode writes.
    """
    if dry_run:
        log(f"  [DRY] ensure_sequential_dual({model_key_a}, {model_key_b}) -> OK (mock)")
        return True

    meta_a = MODEL_METADATA.get(model_key_a)
    meta_b = MODEL_METADATA.get(model_key_b)
    if not meta_a or not meta_b:
        log(f"  Unknown model key(s): {model_key_a}/{model_key_b}")
        return False

    # Quick check: both already healthy?
    a_ok = _check_model_identity(meta_a["port"], model_key_a)
    b_ok = _check_model_identity(meta_b["port"], model_key_b)
    if a_ok and b_ok:
        log(f"  Both healthy — {model_key_a}(:{meta_a['port']}) + {model_key_b}(:{meta_b['port']})")
        _check_container_health()
        return True

    if a_ok and not b_ok:
        log(f"  Primary healthy, secondary :{meta_b['port']} needs launch")
        return _launch_dual_secondary(meta_b)

    # Restart primary (forces fresh container with right model)
    log(f"  Starting primary {model_key_a} on :{meta_a['port']}")
    ok = ensure_model(model_key_a, skip_if_healthy=False)
    if not ok:
        log(f"  ensure_sequential_dual: primary {model_key_a} failed to start")
        return False

    # Check secondary after restart
    if _check_model_identity(meta_b["port"], model_key_b):
        log(f"  Secondary :{meta_b['port']} also healthy after restart")
        return True

    return _launch_dual_secondary(meta_b)


def _launch_dual_secondary(meta: dict) -> bool:
    """Launch a second llama-server inside the running inference container via podman exec.

    Args:
        meta: MODEL_METADATA entry for the secondary model.

    Returns:
        True if secondary started and healthy.
    """
    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 8192)
    threads = meta.get("threads", 4)
    threads_batch = meta.get("threads_batch", 4)
    ubatch = meta.get("ubatch_size", 256)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")
    cpu_range = meta.get("cpu_range", "")
    cpu_strict = meta.get("cpu_strict", "")
    flash_attn = meta.get("flash_attn", "")
    cache_ram = meta.get("cache_ram", "")
    cache_type_k = meta.get("cache_type_k", "")
    cache_type_v = meta.get("cache_type_v", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        ["podman", "exec", "-d", INFERENCE_CONTAINER]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--threads-batch",
            str(threads_batch),
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            str(ubatch),
            "--temp",
            "0.1",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "6",
            "--metrics",
            "--reasoning",
            "off",
            "--slot-prompt-similarity",
            "0",
        ]
    )
    if cpu_range:
        cmd += ["--cpu-range", cpu_range]
    if cpu_strict:
        cmd += ["--cpu-strict", cpu_strict]
    if flash_attn:
        cmd += ["--flash-attn", "on"]
    if cache_ram:
        cmd += [
            "--cache-ram",
            str(cache_ram),
            "--kv-unified",
            "--cache-idle-slots",
            "--cache-reuse",
            "256",
        ]
    if cache_type_k:
        cmd += ["--cache-type-k", cache_type_k]
    if cache_type_v:
        cmd += ["--cache-type-v", cache_type_v]

    log(f"  launching secondary {model_file} on :{port} via podman exec")
    log(f"  {' '.join(str(c) for c in cmd[:8])} ...")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        log(f"  secondary launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False

    ok = wait_health(port, timeout=300)
    if ok:
        log(f"  secondary :{port} healthy with {model_file}")
    else:
        log(f"  secondary :{port} health timeout")
    return ok


def ensure_dual_extraction(dry_run=False):
    """Ensure both extraction models are running: day-extractor (8082) + day-extractor-b (8083).

    Starts main inference with day-extractor on 8082, then launches a second
    llama-server inside the container on 8083 with the 4B model via podman exec.
    Both servers run concurrently in the same container.
    """
    if dry_run:
        log("  [DRY] ensure_dual_extraction() -> OK (mock)")
        return True

    # Step 1: ensure primary model on 8082
    ok = ensure_model("day-extractor", skip_if_healthy=True)
    if not ok:
        log("  ensure_dual_extraction: primary model failed to start")
        return False

    meta_b = MODEL_METADATA.get("day-extractor-b")
    if not meta_b:
        log("  ensure_dual_extraction: day-extractor-b not in MODEL_METADATA")
        return False

    # Step 2: check if 8083 already has the right model
    if _check_model_identity(8083, "day-extractor-b"):
        log("  :8083 already healthy and correct model — skip secondary launch")
        return True

    # Step 3: launch second server on 8083 via podman exec
    port = meta_b["port"]
    model_file = meta_b["file"]
    ctx = meta_b.get("ctx", 8192)
    threads = meta_b.get("threads", 4)
    threads_batch = meta_b.get("threads_batch", 4)
    ubatch = meta_b.get("ubatch_size", 256)
    parallel = meta_b.get("parallel", 1)
    cpus = meta_b.get("cpus", "")
    cpu_range = meta_b.get("cpu_range", "")
    cpu_strict = meta_b.get("cpu_strict", "")
    flash_attn = meta_b.get("flash_attn", "")
    cache_ram = meta_b.get("cache_ram", "")
    cache_type_k = meta_b.get("cache_type_k", "")
    cache_type_v = meta_b.get("cache_type_v", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        [
            "podman",
            "exec",
            "-d",
            INFERENCE_CONTAINER,
        ]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--threads-batch",
            str(threads_batch),
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            str(ubatch),
            "--temp",
            "0.0",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "6",
            "--metrics",
            "--reasoning",
            "off",
            "--slot-prompt-similarity",
            "0",
        ]
    )
    if cpu_range:
        cmd += ["--cpu-range", cpu_range]
    if cpu_strict:
        cmd += ["--cpu-strict", cpu_strict]
    if flash_attn:
        cmd += ["--flash-attn", "on"]
    if cache_ram:
        cmd += [
            "--cache-ram",
            str(cache_ram),
            "--kv-unified",
            "--cache-idle-slots",
            "--cache-reuse",
            "256",
        ]
    if cache_type_k:
        cmd += ["--cache-type-k", cache_type_k]
    if cache_type_v:
        cmd += ["--cache-type-v", cache_type_v]

    log(f"  launching secondary {model_file} on :{port} via podman exec")
    log(f"  {' '.join(str(c) for c in cmd[:8])} ...")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        log(f"  secondary launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False

    # Step 4: wait for health
    ok = wait_health(port, timeout=300)
    if ok:
        log(f"  secondary :{port} healthy with {model_file}")
    else:
        log(f"  secondary :{port} health timeout")
    return ok

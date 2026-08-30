# Status: production
# Path: imported by — watchdog.py
"""Health check runners — LLM 3-tier probe, timer, service, resource.

LLM probe tiers (TensorRT-LLM RFC #4513):
  T1: GET /health → {"status":"ok"}
  T2: POST /v1/chat max_tokens=1 — 진짜 추론 검증
  T3: T2 latency 분석 — hang/리소스 부족 감지
"""

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from lib.db import psql_json
from lib.infra.health_checks import svc_active
from lib.watchdog.config import (
    DAY_PORTS,
    HEARTBEAT_WORKERS,
    LATENCY_CHECK_INTERVAL,
    LLM_TARGETS,
    MEM_CRIT_PCT,
    MEM_WARN_PCT,
    MODE_FILE,
    SERVICE_TARGETS,
    SWAP_CRIT_MB,
    TIMER_TARGETS,
)
from lib.watchdog.messenger import check_heartbeat


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── MODE ────────────────────────────────────────────────────────────


def read_mode() -> str:
    try:
        with open(MODE_FILE) as f:
            for line in f:
                if line.startswith("MODE="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return "day"


def _current_inference_port() -> Optional[int]:
    """Read inference container's currently serving port from env file.

    The inference container runs a single llama-server per mode. The port is
    written to current-mode-inference.env on each mode switch.
    Returns None if the env file can't be read.
    """
    try:
        with open(MODE_FILE_INFERENCE) as f:
            for line in f:
                if line.startswith("PORT="):
                    return int(line.strip().split("=", 1)[1])
    except Exception:
        pass
    return None


# ── T1: HTTP Health ─────────────────────────────────────────────────


def check_health(port: int, label: str = "") -> tuple[bool, str]:
    """T1: GET /health. Returns (ok, detail)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            if resp.status == 200:
                return True, body
            return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)


# ── T2: LLM Probe (실제 추론 검증) ─────────────────────────────────


def check_llm_probe(port: int, label: str = "", timeout: int = 15) -> tuple[bool, str]:
    """T2: POST /v1/chat with max_tokens=1. Returns (ok, latency_ms)."""
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "temperature": 0.1,
            "stream": False,
        }
    ).encode()
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            if data.get("choices") and data["choices"][0].get("message"):
                latency = int((time.monotonic() - t0) * 1000)
                return True, f"{latency}ms"
            return False, "bad response format"
    except Exception as e:
        return False, str(e)


# ── T3: Probe Latency 분석 ─────────────────────────────────────────

_last_t3_check: dict[int, float] = {}  # port → last_check_time


def check_probe_latency(port: int, baseline_ms: int = 2000) -> tuple[bool, str]:
    """T3: 5분 주기로 T2 latency 확인. (False, detail) if baseline 초과."""
    now = time.monotonic()
    last = _last_t3_check.get(port, 0)
    if now - last < LATENCY_CHECK_INTERVAL:
        return True, "skip"
    _last_t3_check[port] = now

    ok, detail = check_llm_probe(port)
    if not ok:
        return False, detail
    try:
        ms = int(detail.replace("ms", ""))
        if ms > baseline_ms * 3:
            return False, f"latency {ms}ms > {baseline_ms * 3}ms (3x baseline)"
        return True, f"{ms}ms"
    except ValueError:
        return False, f"parse error: {detail}"


# ── Service / Container ─────────────────────────────────────────────


def check_service(name: str) -> tuple[bool, str]:
    ok = svc_active(name)
    return ok, "active" if ok else "inactive"


def container_running(name: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        names = r.stdout.strip().split("\n")
        return name in names, "running" if name in names else "not found"
    except Exception as e:
        return False, str(e)


# ── PostgreSQL 실제 헬스체크 ────────────────────────────────────────


# ── Model file 존재 확인 ────────────────────────────────────────────


def check_model_file(model_key: str) -> tuple[bool, str]:
    """GGUF model file 존재 확인. (ok, detail).

    Args:
        model_key: MODEL_METADATA key (e.g. 'day-extractor', 'reranker').
    """
    try:
        from lib.model_registry import MODEL_METADATA

        meta = MODEL_METADATA.get(model_key)
        if not meta:
            return False, f"unknown model key: {model_key}"
        path = f"/opt/ai_data/models/gguf/{meta['file']}"
        if os.path.exists(path):
            return True, f"{meta['file']} ({meta.get('size', '?')})"
        return False, f"not found: {path}"
    except Exception as e:
        return False, str(e)


def check_memory_budget(required_gb: float) -> tuple[bool, str]:
    """MemAvailable >= required_gb 확인. (ok, detail)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0].rstrip(":") == "MemAvailable":
                    avail_kb = int(parts[1])
                    avail_gb = avail_kb / 1024 / 1024
                    ok = avail_gb >= required_gb
                    detail = f"{avail_gb:.1f}GB available, need {required_gb:.0f}GB"
                    return ok, detail
        return False, "MemAvailable not found in /proc/meminfo"
    except Exception as e:
        return False, str(e)


# ── PostgreSQL 실제 헬스체크 ────────────────────────────────────────


def check_postgres() -> tuple[bool, str]:
    """실제 PG 쿼리로 postgres 상태 확인."""
    try:
        r = subprocess.run(
            [
                "podman",
                "exec",
                "postgres",
                "psql",
                "-U",
                "devforge",
                "-d",
                "devforge_app",
                "-c",
                "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and "1" in r.stdout:
            return True, "query ok"
        return False, r.stderr.strip() or r.stdout.strip()
    except Exception as e:
        return False, str(e)


# ── 시스템 리소스 ──────────────────────────────────────────────────


def check_memory() -> tuple[bool, dict]:
    """return (all_ok, {used_gb, total_gb, pct, swap_used_mb, swap_total_mb, swap_pct, detail})"""
    result = {
        "used_gb": 0,
        "total_gb": 0,
        "pct": 0,
        "swap_used_mb": 0,
        "swap_total_mb": 0,
        "swap_pct": 0,
    }
    try:
        r = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split("\n")
        for line in lines:
            parts = line.split()
            if line.startswith("Mem:"):
                total = int(parts[1])
                used = int(parts[2])
                result["total_gb"] = round(total / 1024, 1)
                result["used_gb"] = round(used / 1024, 1)
                result["pct"] = round(used / total * 100) if total > 0 else 0
            elif line.startswith("Swap:"):
                total = int(parts[1])
                used = int(parts[2])
                result["swap_total_mb"] = total
                result["swap_used_mb"] = used
                result["swap_pct"] = round(used / total * 100) if total > 0 else 0
    except Exception:
        pass

    warn = result["pct"] >= MEM_WARN_PCT or result["swap_pct"] >= 50
    crit = result["pct"] >= MEM_CRIT_PCT or result["swap_used_mb"] >= SWAP_CRIT_MB
    result["all_ok"] = not crit
    return not crit, result


def check_disk() -> list[dict]:
    mounts = []
    try:
        r = subprocess.run(
            ["df", "-h", "--output=target,size,used,pcent"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in r.stdout.strip().split("\n")[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 4:
                mounts.append(
                    {
                        "mount": parts[0],
                        "size": parts[1],
                        "used": parts[2],
                        "pct": int(parts[3].replace("%", "")),
                    }
                )
    except Exception:
        pass
    return mounts


# ── 타이머 ─────────────────────────────────────────────────────────


def check_timer(timer_name: str, max_idle_sec: int = 2100) -> tuple[bool, str]:
    """Timer가 max_idle_sec 내에 마지막으로 실행됐는지 확인."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", timer_name, "--property=LastTriggerUSec", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        last_str = r.stdout.strip()
        if not last_str or last_str == "n/a":
            return False, "never triggered"

        last_dt = datetime.strptime(last_str, "%a %Y-%m-%d %H:%M:%S %Z")
        last_dt = last_dt.replace(tzinfo=timezone.utc)
        idle = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if idle > max_idle_sec:
            return False, f"{int(idle)}s idle > {max_idle_sec}s limit"
        return True, f"{int(idle)}s ago"
    except Exception as e:
        return False, str(e)


# ── Pipeline 프로세스 감시 ─────────────────────────────────────────


def check_pipeline(name: str) -> tuple[bool, int]:
    """Pipeline 프로세스 생존 확인. (running, pid)."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.stdout.strip():
            pids = r.stdout.strip().split("\n")
            return True, int(pids[0])
        return False, 0
    except Exception:
        return False, 0


# ── Heartbeat 감시 ─────────────────────────────────────────────────


def check_heartbeats() -> list[dict]:
    """Check all registered worker + ad-hoc test heartbeats.

    Registered workers: from HEARTBEAT_WORKERS config dict.
    Ad-hoc test workers: discovered in DB with pulse_id LIKE 'heartbeat_test_%'
    that are not in HEARTBEAT_WORKERS. Uses default 1800s stale threshold.

    Returns list of alert dicts: [{worker, alive, last_beat, age_sec}]
    """
    results = []

    # 1. Registered workers (from HEARTBEAT_WORKERS config)
    known_workers = set()
    for worker, max_age in HEARTBEAT_WORKERS.items():
        known_workers.add(worker)
        alive, last_beat = check_heartbeat(worker, max_age_seconds=max_age)
        if not alive:
            age_str = ""
            if last_beat:
                try:
                    last = datetime.fromisoformat(last_beat.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - last).total_seconds()
                    age_str = f"{age:.0f}s"
                except Exception:
                    age_str = "unknown"
            results.append(
                {
                    "worker": worker,
                    "alive": False,
                    "last_beat": last_beat or "never",
                    "age_sec": age_str,
                }
            )

    # 2. Ad-hoc test heartbeats (discovered in DB)
    # Test scripts register heartbeat("test_*") at startup.
    # Watchdog discovers them dynamically without any config entry.
    try:
        adhoc = psql_json(
            "SELECT pulse_id FROM watchdog_pulses "
            "WHERE pulse_id LIKE 'heartbeat_test_%' "
            "  AND status = 'IN_PROGRESS' "
            "  AND created_at < now() - interval '1800 seconds'"
        )
        for row in adhoc:
            worker = row["pulse_id"].replace("heartbeat_", "", 1)
            if worker in known_workers:
                continue  # already checked above
            known_workers.add(worker)
            alive, last_beat = check_heartbeat(worker)
            if not alive:
                age_str = ""
                if last_beat:
                    try:
                        last = datetime.fromisoformat(last_beat.replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - last).total_seconds()
                        age_str = f"{age:.0f}s"
                    except Exception:
                        age_str = "unknown"
                results.append(
                    {
                        "worker": worker,
                        "alive": False,
                        "last_beat": last_beat or "never",
                        "age_sec": age_str,
                    }
                )
    except Exception:
        pass  # best-effort — registered workers still checked

    return results


# ── Health check ────────────────────────────────────────────────────


def check_all_llm() -> list[dict]:
    """Check active LLM endpoints: T1 + T2 probe.

    The inference container runs one model at a time on its configured port.
    Only the currently serving port is probed (reads from current-mode-inference.env).
    This prevents false alerts on ports that aren't currently serving a model.
    """
    results = []
    inference_port = _current_inference_port()

    for key, cfg in LLM_TARGETS.items():
        if cfg["port"] not in DAY_PORTS and read_mode() == "day":
            continue  # Night-only ports, skip during day

        # Only probe the currently active port
        if inference_port is not None and cfg["port"] != inference_port:
            continue  # Not currently serving — skip false alert

        t1_ok, t1_detail = check_health(cfg["port"], cfg["label"])
        t2_ok = False
        t2_detail = ""
        if t1_ok:
            t2_ok, t2_detail = check_llm_probe(cfg["port"], cfg["label"])

        results.append(
            {
                "name": key,
                "port": cfg["port"],
                "t1_ok": t1_ok,
                "t1_detail": t1_detail,
                "t2_ok": t2_ok,
                "t2_detail": t2_detail,
            }
        )
    return results


def check_all_services() -> list[dict]:
    results = []
    for name in SERVICE_TARGETS:
        ok, detail = check_service(name)
        results.append({"name": name, "ok": ok, "detail": detail})
    return results


def check_all_timers() -> list[dict]:
    results = []
    for name, cfg in TIMER_TARGETS.items():
        ok, detail = check_timer(name, cfg["max_idle"])
        results.append({"name": name, "ok": ok, "detail": detail})
    return results


# ── LLM Metrics (/metrics) ────────────────────────────────────────


def _parse_metrics_value(text: str, key: str) -> float:
    """Extract a Prometheus gauge/counter value by key prefix."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(key + " "):
            try:
                return float(line[len(key) + 1 :].split()[0])
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def check_llm_metrics(port: int) -> dict:
    """GET /metrics, parse key values. Returns {processing, deferred, prompt_tps, gen_tps, max_ctx, total_prompt, total_gen}."""
    result = {
        "processing": 0,
        "deferred": 0,
        "prompt_tps": 0.0,
        "gen_tps": 0.0,
        "max_ctx": 0,
        "total_prompt": 0,
        "total_gen": 0,
    }
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
        result["processing"] = int(_parse_metrics_value(body, "llamacpp:requests_processing"))
        result["deferred"] = int(_parse_metrics_value(body, "llamacpp:requests_deferred"))
        result["prompt_tps"] = round(
            _parse_metrics_value(body, "llamacpp:prompt_tokens_seconds"), 2
        )
        result["gen_tps"] = round(
            _parse_metrics_value(body, "llamacpp:predicted_tokens_seconds"), 2
        )
        result["max_ctx"] = int(_parse_metrics_value(body, "llamacpp:n_tokens_max"))
        result["total_prompt"] = int(_parse_metrics_value(body, "llamacpp:prompt_tokens_total"))
        result["total_gen"] = int(_parse_metrics_value(body, "llamacpp:tokens_predicted_total"))
    except Exception:
        pass
    return result


# ── LLM Slots (/slots) ────────────────────────────────────────────


def check_llm_slots(port: int) -> list[dict]:
    """GET /slots, return slot state list. Detect potential hangs is_processing=True beyond threshold."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/slots")
        with urllib.request.urlopen(req, timeout=5) as resp:
            slots = json.loads(resp.read())
        result = []
        for s in slots:
            result.append(
                {
                    "id": s.get("id", 0),
                    "is_processing": s.get("is_processing", False),
                    "id_task": s.get("id_task", 0),
                    "n_prompt_tokens_processed": s.get("n_prompt_tokens_processed", 0),
                    "n_prompt_tokens": s.get("n_prompt_tokens", 0),
                    "cache_tokens": s.get("n_prompt_tokens_cache", 0),
                    "ctx_size": s.get("n_ctx", 8192),
                    "cache_pct": round(
                        s.get("n_prompt_tokens_cache", 0) / max(s.get("n_ctx", 1), 1) * 100, 1
                    ),
                }
            )
        return result
    except Exception:
        return []

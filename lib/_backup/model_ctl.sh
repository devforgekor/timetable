#!/bin/bash
# model_ctl.sh — DevForge inference container model management shell functions
# Source this file in cycle scripts and pipeline wrappers.
#
# All functions delegate to Python backend: python3 -m lib.model_ctl
#
# Usage:
#   source /opt/projects/server/scripts/lib/model_ctl.sh
#   _run_model "day-extractor"              # start on default port
#   _run_model "verifier" 8084              # start on custom port
#   _stop_model                            # stop inference
#   _ensure_model "day-extractor"           # smart start (skip if healthy)
#   _ensure_model "verifier" 8084 true 600  # with skip_probe + custom timeout
#
# Port resolution (from model_registry):
#   8080 — reranker
#   8081 — embeder, proposer
#   8082 — day-extractor, day-enricher, day-verifier, reflector
#   8083 — judge, verify-enrich, test-nextcoder, test-qwen
#   8084 — verifier

MODEL_CTL_SCRIPT_DIR="/opt/projects/server/scripts"
MODEL_CTL_PY="python3 -m lib.model_ctl"

# ── Resolve model port from registry ──────────────────────────────────
_model_port() {
    local model_key="$1"
    $MODEL_CTL_PY model-port "$model_key"
}

# ── Resolve full env vars from registry ───────────────────────────────
_model_env_vars() {
    local model_key="$1"
    $MODEL_CTL_PY model-env-vars "$model_key"
}

# ── Write mode env file ───────────────────────────────────────────────
_write_mode_env() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    $MODEL_CTL_PY write-mode-env "$model_key" "$port" || {
        echo "[model_ctl] ERROR: unknown model key '$model_key'" >&2
        return 1
    }
}

# ── Wait for health endpoint ──────────────────────────────────────────
_wait_health() {
    local port="$1" max_wait="${2:-600}"
    $MODEL_CTL_PY wait-health "$port" "$max_wait"
}

# ── Wait for probe (text generation works) ────────────────────────────
_wait_probe() {
    local port="$1" model_key="$2" max_wait="${3:-300}"
    $MODEL_CTL_PY wait-probe "$port" "$max_wait"
}

# ── Check model identity (fingerprint match) ──────────────────────────
_check_model_id() {
    local port="$1" model_key="$2"
    $MODEL_CTL_PY check-model-id "$port" "$model_key"
}

# ── Check if a test heartbeat is active ───────────────────────────────
_test_heartbeat_active() {
    $MODEL_CTL_PY test-heartbeat-active 2>/dev/null
}

# ── Stop inference ────────────────────────────────────────────────────────
_stop_model() {
    $MODEL_CTL_PY stop-model
}

# ── Start inference + wait ───────────────────────────────────────────────
_run_model() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    local skip_probe="${3:-false}"
    local health_timeout="${4:-600}"
    $MODEL_CTL_PY run-model "$model_key" "$port" "$skip_probe" "$health_timeout"
}

# ── Smart ensure: skip if healthy + correct model ─────────────────────
_ensure_model() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    local skip_probe="${3:-false}"
    local health_timeout="${4:-600}"
    $MODEL_CTL_PY ensure-model "$model_key" "$port" "$skip_probe" "$health_timeout"
}

# ── Kill all (emergency stop) ────────────────────────────────────────
_kill_model() {
    $MODEL_CTL_PY kill-model
}

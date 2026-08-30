# Status: production
# Path: imported by — lib/watchdog modules
"""Watchdog 설정 — 체크 대상, 간격, 임계값.

MODE=day (관찰형, 60s 주기):
  inference=reranker (:8080)
  inference=day (:8082) extractor (day verify via model swap on :8082)
  day_cycle.sh — watchdog-managed async pipeline (embed → extract → enrich → verify)
  Fix loop: inference (:8080)가 수정 담당

MODE=night (능동형, 60s 주기):
  Night Debate (:8081 P → :8082 R → :8083 J, sequential)
  Night Verify (:8084 V) → review_consumer.py
  Proxy Audit → proxy_reviewer.py (DeepSeek Pro)
  Fix loop: watchdog이 임시 podman 검증 후 feedback 문서 생성
"""

import os

# ── 인터벌 ──────────────────────────────────────────────────────────
CHECK_INTERVAL = 60  # seconds between check cycles
HEARTBEAT_INTERVAL = 1800  # 30min Slack heartbeat (aligned to :15 / :45)
LIVENESS_STALE_SEC = 900  # 15min — watchdog dead man's switch threshold
LATENCY_CHECK_INTERVAL = 300  # 5min between T3 latency checks

# ── MODE ────────────────────────────────────────────────────────────
MODE_FILE = "/opt/ai_data/scripts/current-system-mode.env"
MODE_FILE_INFERENCE = "/opt/ai_data/scripts/current-mode-inference.env"

# ── 포트 / 라벨 ─────────────────────────────────────────────────────
# Inference container serves all models across ports 8080-8084
LLM_TARGETS = {
    "day-extract": {"port": 8082, "label": "day-extract", "day_model": "extractor"},
    # Night-only model: verifier on :8084
    "night-verify": {"port": 8084, "label": "night-verify", "day_model": None},
}

# Day mode: check these ports for LLM probes
DAY_PORTS = {8080, 8082}

# ── 서비스 / 타이머 ─────────────────────────────────────────────────
SERVICE_TARGETS = [
    "devforge-turn-watcher",
]

# Alert-only targets (monitor only, no recovery)
ALERT_ONLY_TARGETS = [
    "container-postgres",
]

TIMER_TARGETS = {
    "devforge-night-cycle.timer": {"expected": "night_cycle", "max_idle": 90000},  # 25h
}

# ── 컨테이너 exclusion (절대 재시작 금지) ───────────────────────────
CONTAINER_EXCLUSION = {"data-pod-infra", "postgres", "container-postgres"}

# ── CrashLoopBackOff 백오프 (K8s 패턴) ──────────────────────────────
BACKOFF_SCHEDULE = [0, 10, 20, 40, 80, 120, 300]  # seconds
MAX_RETRIES = 3
BACKOFF_RESET_SEC = 600  # 10min 정상 → 카운터 리셋
CIRCUIT_BREAKER_TIMEOUT = 120  # 2min OPEN → HALF_OPEN

# ── 리소스 임계값 ───────────────────────────────────────────────────
DISK_WARN_PCT = 85
DISK_CRIT_PCT = 92
SWAP_WARN_MB = 6000
SWAP_CRIT_MB = 9000
MEM_WARN_PCT = 80
MEM_CRIT_PCT = 90

# ── Slack ──────────────────────────────────────────────────────────
SLACK_SECRETS = os.path.expanduser("~/.config/devforge/secrets.env")
SLACK_CHANNEL = "U0APJGD8CBW"
ALERT_DEDUP_SEC = 300  # 5min per-component dedup

# ── Heartbeat (Dead Man's Switch) ────────────────────────────────────
HEARTBEAT_STALE_SEC = 1800  # 30min without heartbeat → hang 판정
HEARTBEAT_WORKERS: dict[str, int] = {
    "embed_batch": 1800,  # embed_batch.py batch loop
    "liveness_embed_batch": 1800,  # background liveness thread (embed_batch.py)
    "entity_scan": 1800,  # entity_scan.py — deterministic entity scan
    "text_clean": 1800,  # text_clean.py — unified text preprocessing
    "day_extract": 1800,  # extract.py — LLM extraction pipeline
    "day_enrich": 1800,  # enrich.py — LLM enrichment pipeline
}  # worker_name → max_age_seconds. Only register workers that actually call heartbeat().

# ── Pipeline intermediate state recovery ──────────────────────────
# Stale intermediate states indicate worker crash mid-batch.
# Threshold per state: max single LLM call time + safety margin.
# extracting → scanned, enriching → verified
PIPELINE_INTERMEDIATE_STATES: dict[str, dict] = {
    "extracting": {"to_state": "scanned", "stale_sec": 1800},  # 30 min
    "enriching": {"to_state": "verified", "stale_sec": 1800},  # 30 min
}

# ── Token stagnation detection ────────────────────────────────────
# If /metrics shows processing > 0 but aggregate token counters don't
# advance for STAGNATION_STUCK_CYCLES consecutive cycles → system hang.
# Catches cont-batching deadlocks that slot-level check misses (task_id
# keeps changing but no tokens generated).
TOKEN_STAGNATION_THRESHOLD = 5  # cycles (~5 min @ 60s)

# ── 임시 podman 검증 ───────────────────────────────────────────────
SANDBOX_IMAGE = "python:3.12-alpine"
SANDBOX_TIMEOUT = 30  # seconds
SANDBOX_MEM_LIMIT = "128m"

# ── Deep Dive 7단계 sandbox 검증 (task #24) ─────────────────────────
# fixloop의 compile()-only 검증과 별도로, 실제 test 실행용 상수.
# --network none --read-only로 프로젝트 디렉토리만 읽기 마운트.
# 주의: SANDBOX_IMAGE(python:3.12-alpine)에는 pytest가 없고 --network none이라
# 설치도 불가 — 1차 구현은 stdlib unittest만 지원(예: "python -m unittest discover").
SANDBOX_VERIFY_TIMEOUT = 120  # seconds
SANDBOX_VERIFY_MEM_LIMIT = "256m"
# project_dir이 이 경로 하위가 아니면 sandbox_verify를 거부한다.
# host 임의 경로(예: secrets.env가 있는 디렉토리) 읽기전용 마운트로 인한
# 정보 유출을 막기 위한 allowlist.
SANDBOX_VERIFY_ALLOWED_ROOT = "/opt/projects/server"

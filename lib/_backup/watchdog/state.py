# Status: production
# Path: imported by — watchdog.py, fixloop.py
"""Component state machine — HEALTHY↔DEGRADED↔UNHEALTHY↔DOWN.

Circuit breaker pattern (pyresilience, pybreaker):
  CLOSED (정상) → 실패 감지 → OPEN (차단)
  OPEN → timeout → HALF_OPEN (테스트)
  HALF_OPEN → 성공 → CLOSED / 실패 → OPEN
"""

import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ComponentState(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"    # 1-2 failures, still retrying
    UNHEALTHY = "UNHEALTHY"  # 3+ failures, circuit open
    DOWN = "DOWN"            # 5+ failures, escalated


class TrendTracker:
    """Ring buffer for metric trend tracking + linear prediction.

    Stores up to max_samples readings {time → value}.
    predict_eta(threshold) returns minutes until threshold breach, or None.
    Lightweight — pure Python, zero DB writes.
    """

    __slots__ = ("_data", "max_samples")

    def __init__(self, max_samples: int = 60):
        self.max_samples = max_samples
        self._data: list[tuple[float, float]] = []

    def add(self, value: float):
        now = time.monotonic()
        self._data.append((now, value))
        if len(self._data) > self.max_samples:
            self._data.pop(0)

    def predict_eta(self, threshold: float) -> Optional[float]:
        """Minutes until threshold breached. None if descending or insufficient data."""
        if len(self._data) < 10:
            return None
        xs = [t - self._data[0][0] for t, _ in self._data]
        ys = [v for _, v in self._data]
        n = len(xs)
        if n < 2:
            return None
        sx = sum(xs); sy = sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if denom == 0:
            return None
        slope = (n * sxy - sx * sy) / denom
        if slope <= 0:
            return None
        latest = ys[-1]
        if threshold <= latest:
            return 0.0
        eta_sec = (threshold - latest) / slope
        return eta_sec / 60.0  # minutes

    def latest(self) -> Optional[float]:
        return self._data[-1][1] if self._data else None

    def clear(self):
        self._data.clear()

class ComponentTracker:
    """Tracks state + failure count for one component.

    CrashLoopBackOff reset: if healthy for BACKOFF_RESET_SEC → reset counter.
    """

    __slots__ = ("name", "state", "fail_count", "consecutive_fail",
                 "last_state_change", "last_alert_ts", "last_success_ts",
                 "last_fail_ts", "circuit_open_until")

    def __init__(self, name: str):
        self.name = name
        self.state = ComponentState.HEALTHY
        self.fail_count = 0
        self.consecutive_fail = 0
        self.last_state_change = 0.0
        self.last_alert_ts = 0.0
        self.last_success_ts = time.monotonic()
        self.last_fail_ts = 0.0
        self.circuit_open_until = 0.0

    def record_success(self):
        """Reset consecutive counter; if healthy long enough, reset all."""
        now = time.monotonic()
        self.consecutive_fail = 0
        self.last_success_ts = now

        # CrashLoopBackOff reset: 10min 정상 → 전체 리셋
        if self.fail_count > 0 and (now - self.last_fail_ts) >= 600:
            self.fail_count = 0

        self._transition(ComponentState.HEALTHY)
        self.circuit_open_until = 0.0

    def record_failure(self) -> bool:
        """Record failure, update state, return True if state changed."""
        from lib.watchdog.config import BACKOFF_RESET_SEC

        now = time.monotonic()
        self.consecutive_fail += 1
        self.fail_count += 1
        self.last_fail_ts = now

        old = self.state
        if self.consecutive_fail >= 5:
            self._transition(ComponentState.DOWN)
        elif self.consecutive_fail >= 3:
            self._transition(ComponentState.UNHEALTHY)
            # Circuit breaker OPEN
            from lib.watchdog.config import CIRCUIT_BREAKER_TIMEOUT
            self.circuit_open_until = now + CIRCUIT_BREAKER_TIMEOUT
        else:
            self._transition(ComponentState.DEGRADED)

        return old != self.state

    def can_retry(self) -> bool:
        """Circuit breaker: check if OPEN."""
        if self.circuit_open_until == 0.0:
            return True
        if time.monotonic() >= self.circuit_open_until:
            self.circuit_open_until = 0.0
            return True  # HALF_OPEN → allow one test
        return False

    def is_degraded(self) -> bool:
        return self.state in (ComponentState.DEGRADED,
                              ComponentState.UNHEALTHY,
                              ComponentState.DOWN)

    def can_alert(self, dedup_sec: int = 300) -> bool:
        now = time.monotonic()
        if now - self.last_alert_ts >= dedup_sec:
            self.last_alert_ts = now
            return True
        return False

    def _transition(self, new_state: ComponentState):
        if self.state != new_state:
            self.state = new_state
            self.last_state_change = time.monotonic()

    def backoff_sec(self) -> int:
        """Return current backoff delay based on attempt count (CrashLoopBackOff)."""
        from lib.watchdog.config import BACKOFF_SCHEDULE
        idx = min(self.consecutive_fail, len(BACKOFF_SCHEDULE) - 1)
        return BACKOFF_SCHEDULE[idx]

    def summary(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "fail_count": self.fail_count,
            "consecutive_fail": self.consecutive_fail,
            "circuit_open": self.circuit_open_until > time.monotonic(),
        }


class WatchdogState:
    """Aggregate state for all tracked components."""

    def __init__(self):
        self._components: dict[str, ComponentTracker] = {}
        self._mode = "day"
        self._last_heartbeat_ts = 0.0
        self._events: list[dict] = []  # rolling buffer, max 1000
        # Trend trackers for predictive monitoring
        self.disk_trend = TrendTracker()
        self.mem_trend = TrendTracker()
        self._last_pipeline_state: dict[str, float] = {}  # state → first_observed_ts
        self._slot_state: dict[str, dict] = {}  # port → {slot_id: {task_id, processed, stuck_count}}
        self._token_stagnation: dict[str, dict] = {}  # port → {total_prev, stagnation_count, processing_prev}

    def get(self, name: str) -> ComponentTracker:
        if name not in self._components:
            self._components[name] = ComponentTracker(name)
        return self._components[name]

    def set_mode(self, mode: str):
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def should_heartbeat(self, interval: int = 1800) -> bool:
        now = time.monotonic()
        if now - self._last_heartbeat_ts >= interval:
            utc_now = datetime.now(timezone.utc)
            if utc_now.minute % 30 == 15:
                self._last_heartbeat_ts = now
                return True
        return False

    def add_event(self, component: str, event_type: str, detail: str,
                  from_state: str = "", to_state: str = "", fail_count: int = 0):
        self._events.append({
            "timestamp": time.monotonic(),
            "component": component,
            "type": event_type,
            "detail": detail,
        })
        # Trim to 1000
        if len(self._events) > 1000:
            self._events = self._events[-1000:]

        # Persist to DB (best-effort, non-blocking)
        try:
            from lib.db import psql_ok, esc_sql
            c = esc_sql(component)
            et = esc_sql(event_type)
            d = esc_sql(detail)
            from_st = esc_sql(from_state)
            to_st = esc_sql(to_state)
            psql_ok(
                f"INSERT INTO catchdog_events "
                f"(component, event_type, from_state, to_state, detail, fail_count) "
                f"VALUES ('{c}', '{et}', NULLIF('{from_st}', ''), NULLIF('{to_st}', ''), "
                f"NULLIF('{d}', ''), {fail_count})",
                timeout=5,
            )
        except Exception:
            pass  # best-effort — DB down shouldn't crash watchdog

    def events_since(self, sec: int) -> list[dict]:
        cutoff = time.monotonic() - sec
        return [e for e in self._events if e["timestamp"] > cutoff]

    def all_summaries(self) -> list[dict]:
        return [t.summary() for t in self._components.values()]

    def degraded_count(self) -> int:
        return sum(1 for t in self._components.values() if t.is_degraded())

    def update_liveness(self) -> None:
        """Update watchdog_main liveness timestamp in DB (dead man's switch)."""
        try:
            from lib.db import psql_ok
            psql_ok(
                "INSERT INTO watchdog_liveness (component, liveness_ts) "
                "VALUES ('watchdog_main', now()) "
                "ON CONFLICT (component) DO UPDATE SET liveness_ts = now()",
                timeout=5,
            )
        except Exception:
            pass  # best-effort — DB down shouldn't crash watchdog

    # ── Pipeline state stuck detection ─────────────────────────────────

    def check_pipeline_stuck(self, stale_sec: int = 3600) -> list[dict]:
        """Detect pipeline_state stagnation. Returns list of stuck states.

        Compares current state counts against previous cycle first-observation.
        States unchanged for >stale_sec are reported as stuck. Default 3600s (~MAX_CYCLE_SEC).
        """
        from lib.db import psql_json
        try:
            rows = psql_json(
                "SELECT pipeline_state, count(*)::int AS cnt, "
                "  EXTRACT(EPOCH FROM (now() - MIN(created_at)))::int AS min_age_sec "
                "FROM turns "
                "WHERE pipeline_state NOT IN ('pending', 'verified') "
                "GROUP BY pipeline_state ORDER BY pipeline_state"
            )
        except Exception:
            return []

        now = time.monotonic()
        current: dict[str, float] = {}
        stuck: list[dict] = []

        for r in (rows or []):
            state = r.get("pipeline_state", "")
            cnt = r.get("cnt", 0)
            if cnt == 0:
                continue
            current[state] = now

            # Check first observation time
            first_seen = self._last_pipeline_state.get(state)
            if first_seen is None:
                continue  # first cycle seeing this state
            age = now - first_seen
            if age > stale_sec and r.get("min_age_sec", 0) > stale_sec:
                stuck.append({
                    "state": state,
                    "cnt": cnt,
                    "stuck_sec": int(age),
                    "min_age": r.get("min_age_sec", 0),
                })

        # Track states that disappeared
        for prev_state in self._last_pipeline_state:
            if prev_state not in current:
                pass  # resolved, no alert needed

        self._last_pipeline_state = current
        return stuck

    # ── Pipeline intermediate state stuck detection ─────────────────────

    def check_intermediate_stuck(self) -> list[dict]:
        """Detect stale intermediate pipeline states (worker crash mid-batch).

        Queries DB for turns stuck in extracting/enriching/verifying beyond
        the configured stale threshold. Returns list of recoverable states:
            [{state, cnt, to_state, min_age_sec}]
        """
        from lib.db import psql_json

        states = {}
        try:
            from lib.watchdog.config import PIPELINE_INTERMEDIATE_STATES
            states = PIPELINE_INTERMEDIATE_STATES
        except Exception:
            return []

        stuck: list[dict] = []
        for intermediate_state, cfg in states.items():
            try:
                rows = psql_json(
                    f"SELECT count(*)::int AS cnt, "
                    f"EXTRACT(EPOCH FROM (now() - MIN(created_at)))::int AS min_age_sec "
                    f"FROM turns "
                    f"WHERE pipeline_state = '{intermediate_state}'",
                    timeout=5,
                )
                if not rows or not rows[0].get("cnt", 0):
                    continue
                cnt = rows[0]["cnt"]
                min_age = rows[0].get("min_age_sec", 0)
                if min_age >= cfg["stale_sec"]:
                    stuck.append({
                        "state": intermediate_state,
                        "cnt": cnt,
                        "to_state": cfg["to_state"],
                        "min_age_sec": min_age,
                        "stale_sec": cfg["stale_sec"],
                    })
            except Exception:
                continue
        return stuck

    # ── Token stagnation detection (aggregate "코인 증가량") ────────────

    def update_token_metrics(self, port: str, metrics: dict):
        """Register aggregate token totals for stagnation detection.

        Tracks total_prompt + total_gen per port across cycles.
        If processing > 0 but aggregate total doesn't advance for
        TOKEN_STAGNATION_THRESHOLD consecutive cycles → hang confirmed.
        """
        processing = metrics.get("processing", 0)
        total = metrics.get("total_prompt", 0) + metrics.get("total_gen", 0)
        prev = self._token_stagnation.get(port, {})

        stagnation_count = 0
        if processing > 0 and prev.get("processing_prev", 0) > 0:
            if total == prev.get("total_prev", 0):
                stagnation_count = prev.get("stagnation_count", 0) + 1

        self._token_stagnation[port] = {
            "total_prev": total,
            "processing_prev": processing,
            "stagnation_count": stagnation_count,
        }

    def check_token_stagnation(self) -> list[dict]:
        """Return list of stagnated ports (processing but no aggregate token growth).

        Complements slot-level deadlock detection: slot-level checks per-task_id
        progress, but cont-batching can cycle task_ids without generating tokens.
        This aggregate check catches that case.
        """
        from lib.watchdog.config import TOKEN_STAGNATION_THRESHOLD

        stagnated: list[dict] = []
        for port, state in self._token_stagnation.items():
            if state.get("stagnation_count", 0) >= TOKEN_STAGNATION_THRESHOLD:
                stagnated.append({
                    "port": port,
                    "stagnation_count": state["stagnation_count"],
                    "total_prev": state.get("total_prev", 0),
                })
        return stagnated

    # ── Slot stuck detection ─────────────────────────────────────────
    # Only alerts when ALL active slots on a port are simultaneously stuck.
    # Single-slot stuck = slow prompt (normal, up to 10+ min for large prompts).
    # All-slots stuck = cont-batching deadlock (llama.cpp known issue).

    SLOT_STUCK_THRESHOLD = 3  # consecutive cycles all-slots-stuck before alert (3 min @ 60s; 60s = ~300-900 tokens, which must progress)  # noqa

    def update_slots(self, port: str, slot_list: list[dict]):
        """Register current slot state for stuck detection. Call each cycle.

        Stuck detection tracks both prefill (n_prompt_tokens_processed) and
        decode (n_decoded) progress. A slot is "making progress" if either
        counter advances — prevents false deadlock on long decode-only phases
        (extract 5-10 min, where prefill finishes quickly then decodes slowly).
        """
        port_key = f"slots:{port}"
        prev = self._slot_state.get(port_key, {})
        current: dict[int, dict] = {}

        for s in slot_list:
            sid = s.get("id", 0)
            task_id = s.get("id_task", 0)
            processed = s.get("n_prompt_tokens_processed", 0)
            is_proc = s.get("is_processing", False)
            decoded = (s.get("next_token") or [{}])[0].get("n_decoded", 0)
            prev_slot = prev.get(sid)

            if not is_proc:
                current[sid] = {"task_id": 0, "processed": 0, "decoded": 0,
                                "stuck_count": 0, "is_processing": False}
                continue

            stuck_count = 0
            if prev_slot and prev_slot.get("is_processing"):
                task_unchanged = (task_id == prev_slot["task_id"] and task_id > 0)
                no_progress = (processed == prev_slot["processed"] and
                               decoded == prev_slot.get("decoded", 0))
                if task_unchanged and no_progress:
                    stuck_count = prev_slot.get("stuck_count", 0) + 1

            current[sid] = {
                "task_id": task_id,
                "processed": processed,
                "decoded": decoded,
                "prev_n_prompt": s.get("n_prompt_tokens", 0),
                "stuck_count": stuck_count,
                "is_processing": True,
            }

        self._slot_state[port_key] = current

    def check_slots_stuck(self) -> list[dict]:
        """Return list of deadlocked ports (ALL processing slots stuck for >= threshold cycles).

        A single slot stuck in processing is normal (slow prompt). Only when
        every processing slot on a port makes no progress for several consecutive
        cycles is it a confirmed deadlock (cont-batching slot livelock).
        """
        threshold = self.SLOT_STUCK_THRESHOLD
        stuck_ports: list[dict] = []

        for port_key, slots in self._slot_state.items():
            port = port_key.replace("slots:", "")
            processing = {sid: info for sid, info in slots.items()
                          if info.get("is_processing") and info.get("task_id", 0) > 0}
            if not processing:
                continue

            # Single slot stuck = slow prompt (normal, up to 10+ min).
            # Deadlock requires ≥2 processing slots all stuck simultaneously.
            if len(processing) < 2:
                continue

            # All processing slots must be stuck → deadlock confirmed
            all_stuck = all(info.get("stuck_count", 0) >= threshold
                            for info in processing.values())
            if all_stuck:
                slot_ids = ",".join(str(sid) for sid in processing)
                min_stuck = min(info["stuck_count"] for info in processing.values())
                total_processing = len(processing)
                stuck_ports.append({
                    "port": port,
                    "slots": slot_ids,
                    "total_processing": total_processing,
                    "min_stuck_checks": min_stuck,
                })

        return stuck_ports

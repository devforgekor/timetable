#!/usr/bin/env python3
# Status: experimental
# Path: imported by — scripts/tests/test_phase1_14b.py
"""TestRunner — Devin-like self-healing test framework for LLM evaluation.

Usage:
    from lib.test_runner import PodBClient, RetryConfig, TestResult, Health

    client = PodBClient("devforge-inference", internal_port=8081)
    client.health_check(timeout=10)
    result = client.call_llm(messages, schema, max_tokens=1500)

    if result.error:
        result = client.retry(client.call_llm, messages, schema,
                                retry=RetryConfig(max_retries=3, backoff=15))
"""

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# ── Configuration ────────────────────────────────────────────────────────


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff: float = 15.0  # seconds between retries (squared: 15, 60, 135)
    backoff_squared: bool = True


# ── Health ───────────────────────────────────────────────────────────────


@dataclass
class Health:
    ok: bool
    latency: float = 0.0
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


# ── Test Result (standardized output for all tests) ──────────────────────


@dataclass
class TestResult:
    label: str
    error: bool = False
    error_detail: str = ""
    elapsed: float = 0.0
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    num_facts: int = 0
    status: str = "FAIL"  # GOOD | MIXED | FAIL | ERROR
    facts: List[Dict[str, str]] = field(default_factory=list)
    bad_predicates: List[str] = field(default_factory=list)
    duplicate_evidence: bool = False
    raw_content: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_good(self) -> bool:
        return self.status == "GOOD"

    @property
    def is_usable(self) -> bool:
        return self.status in ("GOOD", "MIXED")

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "error": self.error,
            "error_detail": self.error_detail,
            "elapsed": round(self.elapsed, 1),
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "num_facts": self.num_facts,
            "status": self.status,
            "bad_predicates": self.bad_predicates,
            "duplicate_evidence": self.duplicate_evidence,
        }

    def summary(self) -> str:
        if self.error:
            return f"[{self.label}] ERROR: {self.error_detail}"
        return (
            f"[{self.label}] {self.status} | {self.elapsed:.0f}s | "
            f"{self.prompt_tokens}P+{self.completion_tokens}C | "
            f"{self.num_facts} facts | bad={self.bad_predicates} dup={self.duplicate_evidence}"
        )

    def facts_table(self, banned: set = set()) -> List[str]:
        lines = []
        for f in self.facts:
            p = f.get("predicate", "?")
            marker = "⛔" if p in banned else "  "
            lines.append(
                f"  {marker} [{p}] s={f.get('subject', '')[:25]} | o={f.get('object', '')[:35]}"
            )
            lines.append(f'    "{f.get("evidence", "")[:80]}"')
        return lines


# ── Common Banned Predicates ─────────────────────────────────────────────

BANNED_PREDICATES = {"equals", "exists", "is", "has", "does", "was"}


# ── PodBClient ───────────────────────────────────────────────────────────


class PodBClient:
    """LLM test client using podman exec curl for internal container access.

    Avoids port exposure issues by calling curl inside the container.
    Supports health check, retry, and structured test execution.
    """

    def __init__(
        self,
        container: str = "devforge-inference",
        internal_port: int = 8081,
        base_url: str = "http://127.0.0.1",
        timeout: int = 600,
    ):
        self.container = container
        self.internal_port = internal_port
        self.base_url = base_url
        self.timeout = timeout

    def _podman_curl(
        self, payload: str, _method: str = "POST", timeout: Optional[int] = None
    ) -> Tuple[int, str, str]:
        """Execute curl inside the container via podman exec.

        Returns (returncode, stdout, stderr).
        """
        url = f"{self.base_url}:{self.internal_port}/v1/chat/completions"
        t = timeout or self.timeout
        cmd = [
            "podman",
            "exec",
            "-i",
            self.container,
            "curl",
            "-s",
            "-m",
            str(t),
            url,
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=t + 30)
        return r.returncode, r.stdout, r.stderr

    def health_check(self, timeout: int = 15) -> Health:
        """Verify the model endpoint is responsive."""
        t0 = time.monotonic()
        rc, stdout, stderr = self._podman_curl(
            '{"model":"test","messages":[{"role":"user","content":"ping"}],"max_tokens":1}',
            timeout=timeout,
        )
        latency = time.monotonic() - t0
        if rc != 0:
            return Health(ok=False, latency=latency, message=f"curl exit={rc}: {stderr[:100]}")
        try:
            data = json.loads(stdout)
            if "error" in data:
                return Health(ok=False, latency=latency, message=f"API error: {data['error']}")
            return Health(ok=True, latency=latency, message="ok")
        except json.JSONDecodeError as e:
            return Health(ok=False, latency=latency, message=f"JSON parse fail: {e}")

    def call_llm(
        self,
        messages: List[Dict],
        schema: Optional[dict] = None,
        max_tokens: int = 1500,
        temperature: float = 0.0,
        label: str = "test",
    ) -> TestResult:
        """Execute a single LLM call with structured result.

        Args:
            messages: [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
            schema: JSON Schema for response_format (or None)
            max_tokens: max completion tokens
            temperature: sampling temperature
            label: result label for identification
        Returns:
            TestResult with parsed facts and quality metrics
        """
        body: dict = {
            "model": "default",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema:
            body["response_format"] = schema

        payload = json.dumps(body, ensure_ascii=False)

        t0 = time.monotonic()
        rc, stdout, stderr = self._podman_curl(payload)
        elapsed = time.monotonic() - t0

        result = TestResult(label=label, elapsed=elapsed)

        if rc != 0:
            result.error = True
            result.error_detail = f"curl exit={rc}: {stderr[:200]}"
            result.status = "ERROR"
            return result

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            result.error = True
            result.error_detail = f"JSON parse: {e}"
            result.raw_content = stdout[:500]
            result.status = "ERROR"
            return result

        if "error" in data:
            result.error = True
            result.error_detail = str(data["error"])[:300]
            result.status = "ERROR"
            return result

        # Parse response
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        result.raw_content = content
        result.finish_reason = choice.get("finish_reason", "")
        usage = data.get("usage", {})
        result.prompt_tokens = usage.get("prompt_tokens", 0)
        result.completion_tokens = usage.get("completion_tokens", 0)

        # Parse extraction JSON
        try:
            parsed = json.loads(content)
            result.facts = parsed.get("extractions", [])
        except json.JSONDecodeError:
            result.facts = []

        result.num_facts = len(result.facts)

        # Quality checks
        bad_preds = [
            f.get("predicate", "")
            for f in result.facts
            if f.get("predicate", "") in BANNED_PREDICATES
        ]
        result.bad_predicates = bad_preds

        evidences = [f.get("evidence", "") for f in result.facts if "evidence" in f]
        result.duplicate_evidence = len(evidences) != len(set(evidences))

        # Status classification
        if result.num_facts > 0 and not bad_preds and not result.duplicate_evidence:
            result.status = "GOOD"
        elif result.num_facts > 0:
            result.status = "MIXED"
        else:
            result.status = "FAIL"

        return result

    def retry(
        self,
        fn: Callable,
        *args,
        retry: Optional[RetryConfig] = None,
        health_check: bool = True,
        **kwargs,
    ) -> TestResult:
        """Call fn(*args, **kwargs) with retry + health check loop.

        On error/FAIL, waits backoff seconds (squared), checks health, retries.
        """
        config = retry or RetryConfig()

        for attempt in range(1, config.max_retries + 1):
            result = fn(*args, **kwargs)

            if not result.error and result.status != "FAIL":
                return result  # success

            if attempt == config.max_retries:
                return result  # exhausted

            # Calculate backoff
            wait = config.backoff * (attempt**2) if config.backoff_squared else config.backoff
            print(
                f"[retry:{result.label}] attempt {attempt}/{config.max_retries} "
                f"status={result.status} error={result.error} "
                f"waiting {wait:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(wait)

            if health_check:
                h = self.health_check()
                if not h.ok:
                    print(
                        f"[retry:{result.label}] health check FAILED: {h.message}. "
                        f"Waiting extra {wait:.0f}s...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)

        # Should not reach here, but satisfy type checker
        return TestResult(
            label=kwargs.get("label", "unknown"), error=True, error_detail="retry exhausted"
        )


# ── Quality Analysis ─────────────────────────────────────────────────────


def diff_results(a: TestResult, b: TestResult) -> dict:
    """Compare two test results side by side."""
    return {
        "a": a.to_dict(),
        "b": b.to_dict(),
        "delta": {
            "facts": b.num_facts - a.num_facts,
            "good": b.is_good - a.is_good,
            "usable": b.is_usable - a.is_usable,
            "tokens": b.total_tokens - a.total_tokens,
        },
        "a_bad_preds": a.bad_predicates,
        "b_bad_preds": b.bad_predicates,
    }


def print_comparison(a: TestResult, b: TestResult, title: str = "COMPARISON"):
    """Pretty-print side-by-side comparison."""
    print(f"\n{'=' * 60}")
    print(f"{title}")
    print(f"{'=' * 60}")
    for r in [a, b]:
        print(f"\n{r.summary()}")
        if not r.error:
            for line in r.facts_table(BANNED_PREDICATES):
                print(line)

    d = diff_results(a, b)
    print("\n--- Delta ---")
    print(f"  facts:  {d['delta']['facts']:+d}")
    print(f"  good:   {d['delta']['good']:+d}")
    print(f"  usable: {d['delta']['usable']:+d}")
    print(f"  tokens: {d['delta']['tokens']:+d}")

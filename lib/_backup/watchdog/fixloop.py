# Status: experimental
# Path: called by — watchdog.py fix loop
"""Auto-fix loop — error 감지 → LLM 수정 요청 → 임시 podman 검증 → 적용.

출처:
  - AlphaKhulnasoft: error→LLM fix→sandbox verify (80% pass rate)
  - BugZero: LangGraph state machine + search-and-replace patch
  - Repeton: ReAct-guided patch-and-test cycles
  - Podman SDK: containers.run(remove=True) for sandbox
  - AWS Architecture Blog: exponential backoff + full jitter for retry

흐름:
  1. subprocess 실행 → stderr/stdout 캡처
  2. exit code 분석 → fix 필요 판단
  3. LLM에 수정 요청 (error log + context) — 지수 백오프 + jitter 적용
  4. 생성된 패치를 임시 podman에서 실행 검증
  5. 성공 → 적용, 실패 → 유형 분류 → 재시도/중단
"""

import json
import os
import random
import subprocess
import time
import urllib.request
from typing import Optional

from lib.watchdog.config import SANDBOX_IMAGE, SANDBOX_MEM_LIMIT, SANDBOX_TIMEOUT

FIX_SCRIPT_DIR = "/opt/projects/server/scripts"
SYSTEM_FIX_PROMPT = """You are a code repair assistant. Given an error log and the relevant code,
generate a minimal fix. Return ONLY a JSON with:
{
  "files": [{"path": "relative/file.py", "old": "exact string to replace", "new": "replacement string"}],
  "rationale": "one sentence why this fix works"
}
Do NOT include any other text."""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] [fixloop] {msg}", flush=True)


def _exponential_backoff(
    attempt: int,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
) -> float:
    """Full jitter exponential backoff: delay = random(0, min(cap, base * 2^attempt)).

    AWS Architecture Blog (Marc Brooker, 2015-2023):
      Exponential backoff alone still causes synchronized retry waves.
      Full jitter spreads retries across the window, preventing thundering herd.

    Formula: sleep = random.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
    """
    cap = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return random.random() * cap


def _classify_failure(phase: str, detail: str = "") -> str:
    """Classify fix failure into retryable vs non-retryable.

    MatrixTrak (2026): "Classify first, retry second."
    - LLM errors (JSON parse, empty response) = transient, retryable
    - Sandbox errors (SyntaxError) = deterministic, non-retryable (bad patch)
    - Apply errors (old string not found) = non-retryable (file changed)
    - Sandbox timeouts = transient, retryable

    Returns: 'retryable', 'non-retryable', or 'unknown'
    """
    dl = detail.lower()
    if phase == "llm":
        # LLM sampling noise — usually transient
        return "retryable"
    if phase == "sandbox":
        if "timeout" in dl:
            return "retryable"
        if "syntaxerror" in dl:
            return "non-retryable"
        return "retryable"
    if phase == "apply":
        # old string not found → file changed underneath us
        return "non-retryable"
    return "unknown"


def _capture_run(cmd: list[str], timeout: int = 60, cwd: str = FIX_SCRIPT_DIR) -> dict:
    """Run command, return {ok, exit_code, stdout, stderr}."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": r.stdout[-1000:],
            "stderr": r.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "TIMEOUT"}
    except Exception as e:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(e)}


def _sandbox_verify(code_patch: dict) -> dict:
    """Run generated fix in temporary podman container. Returns {ok, output}."""
    test_code = "import sys\n"
    for f in code_patch.get("files", []):
        test_code += f"\n# Verify: {f['path']}\n"
        test_code += (
            f"try:\n    compile({f['new']!r}, '{f['path']}', 'exec')\n"
            f"    print('OK: {f['path']}')\n"
            f"except SyntaxError as e:\n    print('FAIL: {f['path']}', e)\n    sys.exit(1)\n"
        )
    try:
        result = subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--memory",
                SANDBOX_MEM_LIMIT,
                "--network",
                "none",
                "--read-only",
                SANDBOX_IMAGE,
                "python",
                "-c",
                test_code,
            ],
            capture_output=True,
            text=True,
            timeout=SANDBOX_TIMEOUT,
        )
        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "SANDBOX TIMEOUT"}
    except Exception as e:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(e)}


def _apply_patch(code_patch: dict) -> bool:
    """Apply verified patch to filesystem."""
    for f in code_patch.get("files", []):
        fpath = os.path.join(FIX_SCRIPT_DIR, f["path"])
        if not os.path.exists(fpath):
            log(f"  SKIP (not found): {f['path']}")
            continue
        try:
            content = open(fpath).read()
            if f["old"] in content:
                content = content.replace(f["old"], f["new"])
                with open(fpath, "w") as fw:
                    fw.write(content)
                log(f"  PATCHED: {f['path']}")
            else:
                log(f"  SKIP (old string not found): {f['path']}")
                return False
        except Exception as e:
            log(f"  FAIL: {f['path']}: {e}")
            return False
    return True


def _call_llm_fix(prompt: str, llm_port: int = 8080) -> Optional[dict]:
    """Call local LLM for fix suggestion. Returns parsed JSON or None."""
    body = json.dumps(
        {
            "messages": [
                {"role": "system", "content": SYSTEM_FIX_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 4096,
            "temperature": 0.2,
            "stream": False,
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{llm_port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
            log(f"  LLM response not JSON: {content[:200]}")
            return None
    except Exception as e:
        log(f"  LLM call failed: {e}")
        return None


def run_fix_loop(
    error_log: str,
    context: str,
    llm_port: int = 8080,
    max_attempts: int = 3,
) -> dict:
    """Main fix loop: error -> LLM -> sandbox -> apply.

    Each consecutive attempt applies exponential backoff + full jitter.
    Non-retryable failures (patch apply, SyntaxError) short-circuit immediately.

    Args:
        error_log: stderr from failed pipeline
        context: code context or script name that failed
        llm_port: which LLM to use for fix (day=8082, night=8081)
        max_attempts: max retry count (default 3)

    Returns:
        {"fixed": bool, "attempts": int, "detail": str,
         "failure_type": str, "failure_phase": str}
    """
    result = {
        "fixed": False,
        "attempts": 0,
        "detail": "",
        "failure_type": "",
        "failure_phase": "",
    }

    for attempt in range(1, max_attempts + 1):
        log(f"fix attempt {attempt}/{max_attempts} (LLM :{llm_port})")

        # 1. Exponential backoff + full jitter before retry (not first attempt)
        if attempt > 1:
            delay = _exponential_backoff(attempt - 1)
            log(f"  backoff {delay:.1f}s before attempt {attempt}")
            time.sleep(delay)

        # 2. Call LLM for fix
        prompt = (
            f"## Error Log\n```\n{error_log[-2000:]}\n```\n\n"
            f"## Context\n{context}\n\n## Task\nGenerate a fix."
        )
        patch = _call_llm_fix(prompt, llm_port)
        if not patch:
            result["detail"] = "LLM returned no valid fix"
            result["failure_type"] = _classify_failure("llm", result["detail"])
            result["failure_phase"] = "llm"
            log(f"  {result['failure_type']}: {result['detail']}")
            if result["failure_type"] == "non-retryable":
                break
            continue

        # 3. Sandbox verify
        sandbox = _sandbox_verify(patch)
        if not sandbox["ok"]:
            result["detail"] = f"sandbox verify FAILED (exit {sandbox['exit_code']})"
            result["failure_type"] = _classify_failure("sandbox", sandbox.get("stderr", ""))
            result["failure_phase"] = "sandbox"
            log(f"  {result['failure_type']}: {result['detail']}")
            error_log = sandbox["stderr"]  # feed sandbox error back
            if result["failure_type"] == "non-retryable":
                break
            continue

        # 4. Apply patch
        if _apply_patch(patch):
            result["fixed"] = True
            result["attempts"] = attempt
            result["failure_type"] = "none"
            result["failure_phase"] = ""
            result["detail"] = f"fixed in {attempt} attempts via {patch.get('rationale', '')[:100]}"
            log(f"  FIXED: {result['detail']}")
            return result

        # Apply failure — short-circuit (file changed, retry useless)
        result["detail"] = f"patch application failed (attempt {attempt})"
        result["failure_type"] = "non-retryable"
        result["failure_phase"] = "apply"
        log(f"  {result['failure_type']}: {result['detail']}")
        break  # non-retryable, no point continuing

    log(f"  FAILED after {result['attempts']} attempts (type={result['failure_type']})")
    return result

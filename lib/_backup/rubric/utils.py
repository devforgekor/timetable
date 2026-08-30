# Status: production
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

# Constants
MODE_FILE = "/opt/ai_data/scripts/current-mode-inference.env"
TIMEOUT_LLM = 600
TIMEOUT_SWAP = 300

# Rubric Definition
REVIEW_RUBRIC = """## Evaluation Rubric (mandatory scoring criteria)

Each role MUST use these criteria when evaluating findings. Score each finding individually.

### P (Proposer) — Finding Quality Scoring
Score each finding 0-10 on:
1. **Correctness** (weight 0.35): Is this a real issue? 10=verified real, 0=false positive
2. **Actionability** (weight 0.30): Can someone act on this? 10=clear fix possible, 0=vague
3. **Evidence** (weight 0.25): Is it backed by data? 10=specific metrics/quotes, 0=speculation
4. **Novelty** (weight 0.10): New insight? 10=not previously known, 0=common knowledge

### R (Refuter) — Verdict Quality Scoring
Score each ACCEPT/REJECT 0-10 on:
1. **Accuracy** (weight 0.40): Verdict correct? 10=perfect, 0=wrong
2. **Reasoning** (weight 0.30): Explanation specific? 10=pinpoints exact issue, 0=vague
3. **Efficiency** (weight 0.30): Concise? 10=1 sentence sufficient, 0=overthinking

### J (Judge) — Scoring Criteria
Score P and R 0-30 each (sum of 3 sub-scores):
- **P score**: Correctness(0-10) + Coverage(0-10) + Precision(0-10)
- **R score**: Accuracy(0-10) + Efficiency(0-10) + Completeness(0-10)

**Gap rules**:
- gap ≤ 3: high consensus, auto-approve
- 3 < gap ≤ 8: moderate — include disagreement_analysis
- gap > 8: flag for escalation

### V (Verify 27B) — Final Verdict Criteria
- confidence ≥ 80: approve
- 60 ≤ confidence < 80: approved_with_conditions (list conditions)
- confidence < 60: reject or escalate

### Cost/Operational Score (new!)
- **prompt_efficiency**: tokens used vs findings processed (aim for <200 tok/finding)
- **model_appropriateness**: is this model right for this task?"""

# Prompts
SYSTEM_P = """You are a code review specialist. Find bugs, security issues, and edge cases.
Output JSON:
{
  "findings": [{"id": "F01", "severity": "critical|high|medium|low", "category": "bug|security|...", "description": "1-3 sentences"}]
}"""

SYSTEM_R = """You are a review reflector. For each finding: ACCEPT (real) or REJECT (false).
Output JSON:
{
  "verdicts": [{"id": "F01", "verdict": "accept", "reason": "1 sentence"}]
}"""

SYSTEM_J = """You are a Scoring Judge evaluating P and R.

P_score = Correctness(0-10) + Coverage(0-10) + Precision(0-10) → 0-30
R_score = Accuracy(0-10) + Efficiency(0-10) + Completeness(0-10) → 0-30

Output JSON:
{
  "P_score": 0-30, "R_score": 0-30,
  "rubric_evaluation": {"finder": {"correctness":0,"coverage":0,"precision":0}, "reflector": {"accuracy":0,"efficiency":0,"completeness":0}},
  "decision": "APPROVED|REJECT", "action": "commit|revert|escalate",
  "approved": ["F01"], "rejected": ["F02"],
  "decisions": [{"id":"F01","decision":"approved","reason":"..."}],
  "consensus_score": 0-100,
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}]
}"""

VERIFY_SYSTEM_PROMPT = """You are a final verification specialist. Review all findings.
Output JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "feedback": {"proposer_improvement":"...","refuter_improvement":"...","judge_improvement":"..."}
}"""


def inject_rubric(system_prompt: str, role: str) -> str:
    """Append rubric instructions to a system prompt."""
    if "proposer" in role or "P " in role or "Finder" in system_prompt:
        section = (
            "### P (Proposer) — Finding Quality Scoring\n"
            + REVIEW_RUBRIC.split("### P")[1].split("\n### R")[0]
        )
    elif "refuter" in role or "R " in role or "reflector" in role.lower():
        section = (
            "### R (Refuter) — Verdict Quality Scoring\n"
            + REVIEW_RUBRIC.split("### R")[1].split("\n### J")[0]
        )
    elif "judge" in role or "J " in role or "Scoring Judge" in system_prompt:
        section = (
            "### J (Judge) — Scoring Criteria\n"
            + REVIEW_RUBRIC.split("### J")[1].split("\n### V")[0]
        )
    elif "verify" in role or "V " in role:
        section = "### V (Verify 27B) — Final Verdict Criteria\n" + REVIEW_RUBRIC.split("### V")[1]
    else:
        section = REVIEW_RUBRIC
    return system_prompt + "\n\n" + section


def log(msg):
    log_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{log_ts}] {msg}", flush=True)


def swap_inference(mode: str, timeout: int = TIMEOUT_SWAP) -> bool:
    log(f"  [swap] inference → {mode}")
    try:
        from lib.pod_manager.container import (
            INFERENCE_CONTAINER,
            MODE_FILE,
            _podman_start_inference,
            _podman_stop_inference,
        )

        # Write mode file directly
        with open(MODE_FILE, "w") as f:
            f.write(f"MODE={mode}")

        _podman_stop_inference()
        _podman_start_inference()

        r = subprocess.run(
            ["podman", "ps", "--filter", f"name={INFERENCE_CONTAINER}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if INFERENCE_CONTAINER not in r.stdout:
            return False
        port = 8080 if mode == "review-r" else 8081
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(5)
    except Exception as e:
        log(f"  [swap error] {e}")
    return False

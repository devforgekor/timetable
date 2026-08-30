# Status: production
from typing import Any, Dict, List, Optional
from lib.llm_client import call_llm
from lib.token_budget import TokenBudget
from lib.verify import utils

def build_findings_from_turn(turn: Dict, extractions: List[Dict]) -> List[Dict]:
    """Combine turn text and extractions into a findings list for the LLM."""
    findings = []
    # Simplified version for modular structure
    for idx, ex in enumerate(extractions):
        findings.append({
            "id": f"f{idx}",
            "type": ex.get("fact_type"),
            "evidence": ex.get("evidence"),
            "action": ex.get("fact_action")
        })
    return findings

def findings_to_context(findings: List[Dict], user_turn: str, thinking: str, text: str) -> str:
    """Format findings and turn content for the LLM prompt."""
    budget = TokenBudget("day_verify")
    parts = ["=== INPUT: turn START ==="]
    if budget.add_section("user", user_turn or "", priority=10):
        parts.append(f"USER: {user_turn}")
    if budget.add_section("thinking", thinking or "", priority=3):
        parts.append(f"THINKING: {thinking}")
    if budget.add_section("text", text or "", priority=8):
        parts.append(f"TEXT: {text}")
    parts.append("=== INPUT: turn END ===")

    parts.append("\n=== CONTEXT: findings START ===")
    for f in findings:
        line = f"[{f['id']}] ({f['type']}) {f['evidence'][:200]}"
        if budget.add_section(f"finding_{f['id']}", line, priority=7):
            parts.append(line)
    parts.append("=== CONTEXT: findings END ===")
    
    return "\n".join(parts)

def call_verifier(context: str, model_label: str = "day_verify", dry_run: bool = False) -> Optional[Dict]:
    if dry_run:
        return {
            "final_verdict": "approved",
            "confidence": 100,
            "summary": "Dry run mock result",
            "reasoning": "Mocking for testing structure",
            "verification_items": []
        }
    
    # In real day_verify.py, it calls call_llm with model mapping
    # Here we simplify for the example
    meta = call_llm(
        [{"role": "system", "content": utils.VERIFIER_SYSTEM_PROMPT},
         {"role": "user", "content": context}],
        model=model_label,
        max_tokens=utils.VERIFY_MAX_TOKENS,
        temperature=utils.VERIFY_TEMP,
        timeout=utils.VERIFY_TIMEOUT,
        json_mode=True
    )
    return meta

# Status: production
from typing import Any, Dict, List, Optional

# Constants from original day_verify.py
BATCH_LIMIT = 50
MAX_BUDGET = 1500
BUFFER_MIN = 180
CHUNK_SIZE = 6
STARVATION_LIMIT = 3
VERIFY_TIMEOUT = 480
VERIFY_MAX_TOKENS = 1024
VERIFY_TEMP = 0.1

VERIFIER_SYSTEM_PROMPT = """You are a code review verifier. Examine all findings and decide for each:
- approved: correct, can proceed
- rejected: incorrect or not actionable
- needs_review: requires deeper analysis

Classify each finding into a category:
- bug: actual logic error or incorrect behavior
- security: vulnerability or unsafe pattern
- performance: efficiency or resource issue
- quality: maintainability, style, or documentation
- data_loss: missing or dropped information
- hallucination: extracted content NOT supported by the original conversation turn (made up, exaggerated, or contradictory)

IMPORTANT — Faithfulness check: Each finding has an "evidence" field extracted from the
conversation. Verify that the evidence actually appears in or is directly supported by the
turn. If the evidence is fabricated, exaggerated, or contradicts the turn context, mark
it as "hallucination" category with result "fail".

Output STRICT JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "confidence": 0-100,
  "summary": "1-sentence overall assessment",
  "reasoning": "2-3 sentence analysis",
  "verification_items": [
    {"check": "...", "result": "pass|fail|partial", "detail": "...", "category": "bug|security|performance|quality|data_loss|hallucination"}
  ]
}"""

def build_category_summary(verification_items: List[Dict], total_findings: int) -> str:
    """Flattened categorical breakdown for quick analysis."""
    if not verification_items:
        return "none"
    counts = {}
    for item in verification_items:
        cat = item.get("category", "other")
        counts[cat] = counts.get(cat, 0) + 1
    
    parts = []
    for cat in sorted(counts.keys()):
        parts.append(f"{cat}:{counts[cat]}")
    
    summary = f"total:{total_findings} | " + " ".join(parts)
    return summary

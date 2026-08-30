# Status: production
import json
from typing import List, Dict
from lib.llm_client import call_llm
from lib.llm.json_parser import parse_llm_json
from lib.worklog import utils

def review_flagged(flagged_entries: List[Dict], turns: List[Dict]) -> tuple:
    source_text = "\n".join(
        f"[{t['created_at']}] {t['user_turn'][:500]}\n{t['text'][:500]}"
        for t in turns[:utils.BATCH_SIZE]
    )
    entries_json = json.dumps([
        {"index": i, "title": e.get("title", ""), "evidence": e.get("evidence", "")}
        for i, e in enumerate(flagged_entries)
    ], ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": utils.REVIEW_SYSTEM},
        {"role": "user", "content": f"Source turn text:\n{source_text[:2000]}\n\nFlagged entries:\n{entries_json}"},
    ]
    raw = call_llm(messages, model="proposer", max_tokens=512)
    result = parse_llm_json(raw) if raw else None
    if not result:
        return [], "escalate", 0
    return (
        result.get("reviews", []),
        result.get("action", "escalate"),
        int(result.get("consensus_score", 0) or 0),
    )

#!/usr/bin/env python3
# Status: production
"""Pattern → few-shot message conversion for feedback injection.

Converts gold_standard and edge_case patterns into alternating
user/assistant message pairs for LLM context injection.
"""

from typing import Any, Dict, List


SEVERITY_ORDER: Dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _patterns_to_messages(
    patterns: List[Dict[str, Any]],
    max_gold: int = 2,
    max_edge: int = 2,
) -> List[Dict[str, str]]:
    if not patterns:
        return []

    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for p in patterns:
        key = p["issue"][:100]
        if key not in seen:
            seen.add(key)
            unique.append(p)

    gold = [p for p in unique if p.get("classification") == "gold_standard"]
    edge = [p for p in unique if p.get("classification") != "gold_standard"]

    gold.sort(key=lambda p: SEVERITY_ORDER.get(p.get("severity", "medium"), 2))
    edge.sort(key=lambda p: SEVERITY_ORDER.get(p.get("severity", "medium"), 2))

    gold = gold[:max_gold]
    edge = edge[:max_edge]

    messages: List[Dict[str, str]] = []

    for p in gold:
        messages.append({
            "role": "user",
            "content": (
                f"[GOLD STANDARD] {p['issue']}\n"
                f"This is the correct approach. Apply this standard to similar cases."
            ),
        })
        messages.append({
            "role": "assistant",
            "content": f"Standard confirmed: {p['fix']}",
        })

    for p in edge:
        messages.append({
            "role": "user",
            "content": (
                f"[EDGE CASE CHALLENGE] {p['issue']}\n"
                f"Probe this case for weaknesses. If the model's logic is sound, "
                f"confirm it. If a flaw is found, explain how to fix it."
            ),
        })
        messages.append({
            "role": "assistant",
            "content": f"Challenge assessed. {p['fix']}",
        })

    return messages

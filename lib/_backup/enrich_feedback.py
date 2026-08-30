#!/usr/bin/env python3
# Status: production
# Path: imported by — enrich.py (dynamic few-shot), watchdog.py (no longer called directly)
"""Enrich Feedback — dynamic few-shot retrieval for enrich pipeline.

Architecture::

    enrich.py (_generate_enrich_fields)
      → get_dynamic_few_shot(turn_text)
        1. Embed turn text via :8081 → vector
        2. pgvector ANN: feedback_examples ORDER BY embedding <=> $1
           CONFIRM → good example, REJECT → bad example
        3. Supplementary: verify_result UNGROUNDED/GROUNDED from review_facts
        4. Return formatted few-shot text block

No file-based storage. No pre-collection. Everything is queried at enrich time
using pgvector similarity search on user-curated feedback_examples.
"""

import json
import time
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

from lib.db import psql_json, esc_sql

EMBED_URL = "http://127.0.0.1:8081/v1/embeddings"
EMBED_TIMEOUT = 60
MAX_EXAMPLES_DB = 4      # max feedback_examples (primary)
MAX_EXAMPLES_VERIFY = 3   # max verify_result examples (supplementary)
LOOKBACK_HOURS = 72       # verify_result lookback


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] [enrich_feedback] {msg}", flush=True)


# ── Embedding ────────────────────────────────────────────────────────

def _embed_text(text: str) -> Optional[List[float]]:
    """Embed a single text via qwen3-embed-8b on :8081.

    Returns vector or None on failure (embedder not available = expected
    during enrich phase when inference runs day-enrich on :8082, not embed mode).
    """
    import json as _json
    # Quick health check — embedder is often down during enrich phase
    # when inference has been swapped to day-enrich on :8082.
    try:
        hreq = Request("http://127.0.0.1:8081/health", method="GET")
        with urlopen(hreq, timeout=2) as hresp:
            if hresp.status != 200:
                return None
    except Exception:
        return None

    body = _json.dumps({"input": [text], "model": "default"}).encode()
    req = Request(EMBED_URL, data=body,
                  headers={"Content-Type": "application/json"},
                  method="POST")
    try:
        with urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = _json.loads(resp.read().decode())
        return data["data"][0]["embedding"]
    except Exception:
        return None


# ── Primary: pgvector dynamic retrieval from feedback_examples ───────

def _search_feedback_examples(
    vector: List[float], max_examples: int = MAX_EXAMPLES_DB,
) -> Dict[str, List[Dict]]:
    """pgvector ANN search on feedback_examples.

    Returns {"bad": [REJECT examples], "good": [CONFIRM examples]}.
    CONFIRM examples are weighted slightly higher in sort order
    (margin multiplier) so good examples are preferred when similar.
    """
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    rows = psql_json(
        f"SELECT evidence_text, source_text, fact_type, verdict, "
        f"  1 - (embedding <=> '{vec_str}'::vector) AS similarity "
        f"FROM feedback_examples "
        f"WHERE embedding IS NOT NULL "
        f"ORDER BY embedding <=> '{vec_str}'::vector "
        f"LIMIT {max_examples + 2}"
    )
    if not rows:
        return {"bad": [], "good": []}

    bad: List[Dict] = []
    good: List[Dict] = []
    for r in rows:
        verdict = r.get("verdict", "")
        evidence = (r.get("evidence_text") or "")[:200]
        sim = r.get("similarity", 0)
        if verdict == "REJECT":
            bad.append({
                "type": "bad",
                "evidence": evidence,
                "similarity": round(sim, 3),
                "lesson": f"Previously rejected: \"{evidence}\". Avoid similar patterns.",
            })
        elif verdict == "CONFIRM":
            good.append({
                "type": "good",
                "evidence": evidence,
                "similarity": round(sim, 3),
                "lesson": f"Previously confirmed: \"{evidence}\". Follow this pattern.",
            })

    # Sort: highest similarity first — dedup by evidence_text
    seen: set = set()
    for key in ("bad", "good"):
        deduped = []
        for ex in (bad if key == "bad" else good):
            dedup_key = ex["evidence"][:80]
            if dedup_key not in seen:
                seen.add(dedup_key)
                deduped.append(ex)
        if key == "bad":
            bad = deduped[:max_examples]
        else:
            good = deduped[:max_examples]

    return {"bad": bad, "good": good}


# ── Supplementary: verify_result (UNGROUNDED/GROUNDED) ───────────────

UNGROUNDED_VALUES = {"UNGROUNDED", "AMBIGUOUS"}
GOOD_SCORE_MIN = 0.8
BAD_SCORE_MAX = 0.4


def _fetch_verify_feedback() -> Dict[str, List[Dict]]:
    """Fetch recent verify_result for supplementary few-shot.

    Only includes turns with pipeline_state = 'verified'.
    Returns {"bad": [UNGROUNDED examples], "good": [GROUNDED examples]}.
    """
    sql = (
        "SELECT rf.evidence::text AS evidence_str "
        "FROM review_facts rf "
        "JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.fact_type = 'verify_result' "
        "  AND t.pipeline_state = 'verified' "
        f"  AND rf.created_at > now() - interval '{LOOKBACK_HOURS} hours' "
        "ORDER BY rf.created_at DESC "
        "LIMIT 50"
    )
    rows = psql_json(sql) or []

    bad: List[Dict] = []
    good: List[Dict] = []
    seen_entities: set = set()

    for row in rows:
        try:
            verify = json.loads(row.get("evidence_str", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue

        faithfulness = verify.get("faithfulness", {})
        if not isinstance(faithfulness, dict):
            continue

        entity_types = ["files", "technologies", "functions", "mentioned_users"]
        for etype in entity_types:
            items = faithfulness.get(etype, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                entity = str(item.get("entity", "")).strip()
                score = item.get("score")
                if not entity or score is None:
                    continue
                grounding = str(item.get("grounding", ""))
                dedup_key = (etype, entity[:60])
                if dedup_key in seen_entities:
                    continue
                seen_entities.add(dedup_key)

                if grounding in UNGROUNDED_VALUES and score < BAD_SCORE_MAX:
                    bad.append({
                        "type": "bad",
                        "entity_type": etype,
                        "entity": entity[:80],
                        "score": score,
                        "grounding": grounding,
                    })
                elif grounding == "GROUNDED" and score >= GOOD_SCORE_MIN:
                    good.append({
                        "type": "good",
                        "entity_type": etype,
                        "entity": entity[:80],
                        "score": score,
                        "grounding": grounding,
                    })

        # tldr
        tldr = faithfulness.get("tldr", {})
        if isinstance(tldr, dict):
            tscore = tldr.get("score")
            tgrounding = str(tldr.get("grounding", ""))
            if tscore is not None:
                key = ("tldr", str(tldr.get("text", ""))[:60])
                if key not in seen_entities:
                    seen_entities.add(key)
                    if tgrounding in UNGROUNDED_VALUES and tscore < BAD_SCORE_MAX:
                        bad.append({
                            "type": "bad", "entity_type": "tldr",
                            "entity": str(tldr.get("text", ""))[:80],
                            "score": tscore, "grounding": tgrounding,
                        })
                    elif tgrounding == "GROUNDED" and tscore >= GOOD_SCORE_MIN:
                        good.append({
                            "type": "good", "entity_type": "tldr",
                            "entity": str(tldr.get("text", ""))[:80],
                            "score": tscore, "grounding": tgrounding,
                        })

    return {
        "bad": bad[:MAX_EXAMPLES_VERIFY],
        "good": good[:MAX_EXAMPLES_VERIFY],
    }


# ── Format ───────────────────────────────────────────────────────────

def _format_few_shot(
    db_examples: Dict[str, List[Dict]],
    verify_examples: Dict[str, List[Dict]],
) -> str:
    """Format both sources into a single few-shot text block.

    Returns empty string if nothing available.
    """
    parts: List[str] = []

    db_bad = db_examples.get("bad", [])
    db_good = db_examples.get("good", [])
    v_bad = verify_examples.get("bad", [])
    v_good = verify_examples.get("good", [])

    if not db_bad and not db_good and not v_bad and not v_good:
        return ""

    # Primary: feedback_examples
    if db_bad:
        parts.append("### [FEEDBACK — Known errors to avoid]")
        for ex in db_bad:
            parts.append(f"- REJECTED: {ex['evidence']} (sim={ex['similarity']})")
        parts.append("")

    if db_good:
        parts.append("### [FEEDBACK — Known correct patterns to follow]")
        for ex in db_good:
            parts.append(f"- CONFIRMED: {ex['evidence']} (sim={ex['similarity']})")
        parts.append("")

    # Supplementary: verify_result
    if v_bad:
        parts.append("### [VERIFY FEEDBACK — Recent ungrounded entities]")
        for ex in v_bad:
            parts.append(
                f"- {ex['entity_type']}=\"{ex['entity']}\" "
                f"score={ex['score']} {ex['grounding']} — not in source"
            )
        parts.append("")

    if v_good:
        parts.append("### [VERIFY FEEDBACK — Recently verified correct entities]")
        for ex in v_good:
            parts.append(
                f"- {ex['entity_type']}=\"{ex['entity']}\" "
                f"score={ex['score']} {ex['grounding']} — good pattern"
            )
        parts.append("")

    return "\n".join(parts)


# ── Public API ───────────────────────────────────────────────────────

def _has_feedback_examples() -> bool:
    """Quick EXISTS check — avoids embed call when table is empty."""
    return bool(psql_json("SELECT 1 FROM feedback_examples LIMIT 1"))


def get_dynamic_few_shot(turn_text: str, max_examples: int = MAX_EXAMPLES_DB) -> str:
    """Dynamic few-shot retrieval for enrich.

    Early-returns "" if feedback_examples is empty (common during enrich
    phase when inference runs day-enrich on :8082, not embed on :8081).

    1. Quick EXISTS check on feedback_examples
    2. Embed turn_text via :8081
    3. pgvector ANN on feedback_examples (primary)
    4. Verify_result supplementary (no embed needed)
    5. Format and return

    Returns empty string if nothing available or embed fails.
    """
    if not _has_feedback_examples():
        # feedback_examples empty — no point embedding or querying
        return ""

    vector = _embed_text(turn_text)
    if vector is None:
        verify = _fetch_verify_feedback()
        if verify.get("bad") or verify.get("good"):
            return _format_few_shot({"bad": [], "good": []}, verify)
        return ""

    db_examples = _search_feedback_examples(vector, max_examples)
    verify_examples = _fetch_verify_feedback()

    return _format_few_shot(db_examples, verify_examples)

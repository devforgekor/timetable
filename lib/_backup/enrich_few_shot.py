#!/usr/bin/env python3
# Status: experimental
# Path: imported by — enrich.py (load_few_shot), weekly_enrich_rebuild.sh (rebuild)
"""Enrich Few-Shot — static diversity-first few-shot examples for enrich pipeline.

Architecture::

    weekly_enrich_rebuild.sh (weekly systemd timer)
      → rebuild()
        1. Load current config from YAML
        2. Diversity-first pick from feedback_examples (CONFIRM only)
        3. Update YAML config (append or rotate)

    enrich.py (each turn, via _generate_enrich_fields)
      → load_few_shot()
        1. Read YAML
        2. Format as text block → appended to SYSTEM_DAY_ENRICH

Design rationale — why static instead of dynamic embed retrieval:
  1. feedback_examples is empty during normal enrich phase (inference on :8082, not :8081)
  2. Non-monotonic few-shot curve: embedding-similar examples cause gradient sensitivity collapse
  3. Diversity-first: max-min distance picks examples from different intent/category quadrants
  4. KV cache: static system prompt → llama.cpp prefix cache hit across section-major batches
"""

import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

import yaml

from lib.db import psql_json

_CONFIG_PATH = os.path.join(SCRIPTS_DIR, "config", "enrich_few_shot.yaml")
_MAX_EXAMPLES = 4


# ── YAML I/O ──────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load YAML config. Returns default if file missing or corrupted."""
    if not os.path.exists(_CONFIG_PATH):
        return {"cycle": 0, "examples": []}
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            return {"cycle": 0, "examples": []}
        cfg.setdefault("cycle", 0)
        cfg.setdefault("examples", [])
        return cfg
    except Exception:
        return {"cycle": 0, "examples": []}


def _save_config(cfg: dict) -> None:
    """Write YAML config atomically."""
    tmp = _CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, _CONFIG_PATH)


# ── Public: called by enrich.py each turn ─────────────────────────────

def load_few_shot() -> str:
    """Read static few-shot examples from YAML config.

    Returns formatted text block (empty string if no examples).
    KV cache friendly: same YAML = same output string = same prefix.
    """
    cfg = _load_config()
    examples = cfg.get("examples", [])
    if not examples:
        return ""

    parts = ["### [CONFIRMED enrichment patterns — from user feedback]"]
    for i, ex in enumerate(examples, 1):
        text = (ex.get("text") or "").strip()
        if text:
            parts.append(f"Example {i}: {text}")
    return "\n\n".join(parts)


# ── Vector ops ────────────────────────────────────────────────────────

def _parse_vector(s: str) -> List[float]:
    """Parse pgvector text representation to float list."""
    return json.loads(s)


def _cosine_sim(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _cosine_dist(a: List[float], b: List[float]) -> float:
    """Cosine distance = 1 - cosine similarity."""
    return 1.0 - _cosine_sim(a, b)


# ── Diversity pick ────────────────────────────────────────────────────

def _pick_diverse(fresh_rows: List[dict],
                  existing_examples: List[dict]) -> Optional[dict]:
    """Pick the most diverse candidate from fresh vs existing.

    Max-min diversity: among fresh candidates, pick the one with the
    largest minimum cosine distance to any existing example.

    If existing_examples is empty: pick the most recent CONFIRM.
    """
    if not fresh_rows:
        return None

    # Parse all embeddings once
    fresh_embeds = [_parse_vector(r["embedding_str"]) for r in fresh_rows]

    if not existing_examples:
        # First pick: most recent (already sorted by created_at DESC)
        return fresh_rows[0]

    # Build existing embedding list from stored example embeddings
    # (re-queried from DB to handle deletion)
    existing_ids = {e["source_id"] for e in existing_examples if e.get("source_id")}

    # Max-min: pick fresh candidate farthest from all existing + fresh-picked ones
    best_row = None
    best_dist = -1.0

    for i, row in enumerate(fresh_rows):
        # Min distance to existing config examples
        min_to_existing = 1.0  # max possible distance = 1.0
        for e in existing_examples:
            e_emb = e.get("_embedding")
            if e_emb:
                dist = _cosine_dist(fresh_embeds[i], e_emb)
                if dist < min_to_existing:
                    min_to_existing = dist

        if min_to_existing > best_dist:
            best_dist = min_to_existing
            best_row = row

    return best_row


# ── Quality check ─────────────────────────────────────────────────────

def quality_check() -> Optional[bool]:
    """Compare CONTRADICTION rate week-over-week.

    Queries enrich_meta facts:
      - Current week (last 7 days)
      - Previous week (7-14 days ago)

    Returns:
      True  — rate stable or improved → safe to update
      False — rate worsened → skip update
      None  — insufficient data (skip check)
    """
    sql = """
    WITH current AS (
        SELECT
            COUNT(*) FILTER (WHERE rf.evidence::jsonb->>'nli_verdict' = 'CONTRADICTION') AS contradicted,
            COUNT(*) AS total
        FROM review_facts rf
        WHERE rf.fact_type = 'enrich_meta'
          AND rf.created_at > now() - interval '7 days'
    ),
    previous AS (
        SELECT
            COUNT(*) FILTER (WHERE rf.evidence::jsonb->>'nli_verdict' = 'CONTRADICTION') AS contradicted,
            COUNT(*) AS total
        FROM review_facts rf
        WHERE rf.fact_type = 'enrich_meta'
          AND rf.created_at BETWEEN now() - interval '14 days' AND now() - interval '7 days'
    )
    SELECT
        current.contradicted AS cur_bad, current.total AS cur_total,
        previous.contradicted AS prev_bad, previous.total AS prev_total
    FROM current, previous
    """
    rows = psql_json(sql)
    if not rows:
        return None
    row = rows[0]
    cur_total = row.get("cur_total", 0) or 0
    prev_total = row.get("prev_total", 0) or 0
    # Need at least 10 samples in current week to judge
    if cur_total < 10:
        return None
    if prev_total < 10:
        # No prior baseline → accept if current rate < 5%
        cur_rate = (row.get("cur_bad", 0) or 0) / max(cur_total, 1)
        return cur_rate < 0.05

    cur_rate = (row.get("cur_bad", 0) or 0) / max(cur_total, 1)
    prev_rate = (row.get("prev_bad", 0) or 0) / max(prev_total, 1)

    # Worsened by more than 5 percentage points → reject
    if cur_rate > prev_rate + 0.05:
        return False
    return True


# ── Public: called by weekly_enrich_rebuild.sh ────────────────────────

def rebuild(dry_run: bool = False) -> dict:
    """Diversity-first pick from feedback_examples → YAML update.

    Called weekly by systemd timer.
    Picks 1 diverse example, updates config YAML.

    Accumulation (cycle=0, <4 examples): appends new example.
    Rotation (cycle>=1 or 4 examples): replaces oldest slot.

    Returns dict with action taken.
    """
    # 1. Load all CONFIRM rows with embeddings from feedback_examples
    rows = psql_json(
        "SELECT id, evidence_text, source_text, verdict, "
        "  embedding::text AS embedding_str "
        "FROM feedback_examples "
        "WHERE verdict = 'CONFIRM' AND embedding IS NOT NULL "
        "ORDER BY created_at DESC"
    )
    if not rows:
        return {"action": "skip", "reason": "no CONFIRM examples in feedback_examples"}

    # 2. Load config
    cfg = _load_config()
    existing = cfg.get("examples", [])

    # 3. Filter out examples whose source_id no longer exists
    valid_source_ids = {r["id"] for r in rows}
    existing = [e for e in existing if e.get("source_id") in valid_source_ids]

    # 4. Dedup: only consider fresh rows not already in config
    existing_ids = {e["source_id"] for e in existing if e.get("source_id")}
    fresh = [r for r in rows if r["id"] not in existing_ids]

    if not fresh:
        return {"action": "skip", "reason": "no new unreplicated examples"}

    # 5. Compute existing example embeddings for distance calculation
    existing_with_emb = []
    for e in existing:
        matching = [r for r in rows if r["id"] == e.get("source_id")]
        if matching:
            e["_embedding"] = _parse_vector(matching[0]["embedding_str"])
            existing_with_emb.append(e)

    # 6. Pick one diverse candidate
    pick = _pick_diverse(fresh, existing_with_emb)
    if not pick:
        return {"action": "skip", "reason": "diversity pick returned None"}

    pick_embedding_str = pick.get("embedding_str", "")
    pick_embedding = _parse_vector(pick_embedding_str) if pick_embedding_str else None

    # 7. Determine action: accumulate or rotate
    cycle = cfg.get("cycle", 0)
    slot_index = 0

    if len(existing) < _MAX_EXAMPLES:
        # Accumulation phase
        action = "append"
        slot_index = len(existing)
    else:
        # Rotation phase: cycle >= 1
        action = "rotate"
        slot_index = cfg.get("next_slot", 0)

    # 8. Build example entry
    # text: compact nature of the confirmed example
    evidence = (pick.get("evidence_text") or "")[:300]
    source = (pick.get("source_text") or "")[:100]
    try:
        ev = json.loads(evidence) if evidence.startswith("{") else {}
        summary_parts = []
        if ev.get("intent"):
            summary_parts.append(f"intent={ev['intent']}")
        if ev.get("category"):
            summary_parts.append(f"category={ev['category']}")
        ents = ev.get("entities", {})
        if ents:
            techs = ents.get("technologies", [])[:3]
            if techs:
                summary_parts.append(f"tech=[{', '.join(techs)}]")
        text = " | ".join(summary_parts) if summary_parts else evidence[:150]
    except Exception:
        text = evidence[:150]

    entry = {
        "source_id": pick["id"],
        "text": text,
        "week": len(existing) + 1 if action == "append" else (cfg.get("week_counter", 0) + 1),
    }
    if pick_embedding:
        entry["_embedding"] = pick_embedding

    # 9. Update config
    if action == "append":
        existing.append(entry)
    else:
        existing[slot_index] = entry
        slot_index = (slot_index + 1) % _MAX_EXAMPLES

    new_cycle = 1 if len(existing) >= _MAX_EXAMPLES else 0

    cfg["cycle"] = new_cycle
    cfg["next_slot"] = slot_index
    cfg["week_counter"] = cfg.get("week_counter", 0) + 1
    cfg["examples"] = existing

    # Strip _embedding before saving (too large for YAML, only kept in memory)
    serializable = []
    for e in existing:
        entry_clean = {k: v for k, v in e.items() if k != "_embedding"}
        serializable.append(entry_clean)
    cfg["examples"] = serializable

    if not dry_run:
        _save_config(cfg)
        print(f"[enrich_few_shot] {action}: slot={slot_index}, "
              f"source={pick['id'][:8]} text=\"{text[:60]}\"", flush=True)
    else:
        print(f"[DRY RUN] Would {action}: slot={slot_index}, "
              f"source={pick['id'][:8]} text=\"{text[:60]}\"", flush=True)

    return {
        "action": action,
        "slot": slot_index,
        "source_id": pick["id"],
        "text": text,
        "cycle": new_cycle,
        "total_examples": len(existing),
        "dry_run": dry_run,
    }

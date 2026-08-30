#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""enrich_consumer — read enrichment metadata from review_facts and prepare for MCP tools.

Pre-build for Phase 2 (MCP tool integration). Reads fact_type='enrich_meta' facts,
formats them for MCP tools (mem_save, context injection), and marks as processed.

Flow:
  1. SELECT unprocessed enrich_meta facts
  2. Parse enrichment fields (tldr, intent, entities, tags, verified)
  3. Format as MCP-ready structured data
  4. Update verdict to 'enrich_processed'

Usage:
  from lib.enrich_consumer import consume_enrich
  results = consume_enrich(limit=50)

CLI:
  python3 -m lib.enrich_consumer --limit 50 --json
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import esc_sql, psql_json, psql_ok

BATCH_LIMIT = 50
ENRICH_FIELDS = (
    "tldr",
    "intent",
    "entities",
    "tags",
    "verified",
    "sentiment",
    "sentiment_intensity",
    "confidence",
    "schema_version",
    "provenance",
)


def fetch_unprocessed_enrich(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Fetch review_facts rows where fact_type='enrich_meta' and verdict='pending'."""
    sql = (
        "SELECT rf.id, rf.turn_id, rf.evidence, rf.extract_model, "
        "  rf.created_at, rf.fact_confidence AS faithfulness_score, "
        "  t.seq, t.conversation_id "
        "FROM review_facts rf "
        "LEFT JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.fact_type = 'enrich_meta' "
        "  AND rf.verdict = 'pending' "
        "ORDER BY rf.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []

    items = []
    for row in rows:
        try:
            evidence = json.loads(row.get("evidence") or "{}")
        except (json.JSONDecodeError, TypeError):
            evidence = {}
        items.append(
            {
                "id": row["id"],
                "turn_id": row["turn_id"],
                "evidence": evidence,
                "extract_model": row.get("extract_model", ""),
                "created_at": row.get("created_at", ""),
                "seq": int(row["seq"]) if row.get("seq") else 0,
                "conversation_id": row.get("conversation_id", ""),
                "faithfulness_score": row.get("faithfulness_score"),
            }
        )
    return items


def format_enrich_output(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a review_facts enrich_meta row to MCP-ready structured data."""
    ev = item.get("evidence", {})
    enrich_fields = {k: ev.get(k) for k in ENRICH_FIELDS if k in ev}

    return {
        "source": "extract_pipeline",
        "turn_id": item["turn_id"],
        "conversation_id": item["conversation_id"],
        "seq": item["seq"],
        "extract_model": item["extract_model"],
        "enrich": enrich_fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def mark_processed(fact_id: str) -> bool:
    """Mark a review_fact as processed by enrich consumer."""
    sql = f"UPDATE review_facts SET verdict = 'enrich_processed' WHERE id = {esc_sql(fact_id)}"
    return psql_ok(sql)


def consume_enrich(limit: int = BATCH_LIMIT, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Fetch unprocessed enrichment facts, format for MCP tools, mark processed.

    Returns list of MCP-ready dicts.
    """
    items = fetch_unprocessed_enrich(limit)
    if not items:
        print("[enrich_consumer] No unprocessed enrichment facts")
        return []

    print(f"[enrich_consumer] Processing {len(items)} enrichment fact(s)")
    results = []
    for item in items:
        enrich_output = format_enrich_output(item)
        results.append(enrich_output)

        tldr = enrich_output["enrich"].get("tldr", "?")
        print(f"  [{item['turn_id'][:8]}] tldr={tldr}")

        if not dry_run:
            mark_processed(item["id"])

    print(f"[enrich_consumer] {len(results)} formatted ({'dry-run' if dry_run else 'processed'})")
    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="MCP Consumer — read and format MCP metadata")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true", help="Read only, no verdict update")
    parser.add_argument("--json", action="store_true", help="Output as JSON lines")
    args = parser.parse_args()

    results = consume_enrich(limit=args.limit, dry_run=args.dry_run)

    if args.json:
        for r in results:
            print(json.dumps(r, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()

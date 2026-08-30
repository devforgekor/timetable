# Status: production
# Path: imported by — cli.py (fact subcommand)
"""CLI fact commands — user feedback on NEUTRAL review_facts."""

from lib.db import esc_sql
from lib.db import psql as _sql
from lib.db import psql_json as _pj


def cmd_fact_list(args):
    """List review_facts with optional NEUTRAL pending filter."""
    where = "WHERE 1=1"
    if getattr(args, "pending", False):
        where = "WHERE nli_llm = 'NEUTRAL' AND user_verdict IS NULL"
    limit = getattr(args, "limit", 20)
    sql = f"""SELECT id, turn_id, fact_index, left(evidence, 120) AS evidence, fact_type,
       nli_llm, user_verdict, source, created_at
    FROM review_facts {where}
    ORDER BY created_at DESC LIMIT {limit}"""
    rows = _pj(sql)
    if not rows:
        print("(no matching facts)")
        return
    print(f"{'ID':<38} {'Type':<12} {'NLI':<10} {'User':<8} {'Evidence':<60} {'Created'}")
    print("-" * 140)
    for r in rows:
        uid = str(r["id"])[:36]
        etype = (r.get("fact_type") or "")[:10]
        nli = (r.get("nli_llm") or "")[:8]
        uv = (r.get("user_verdict") or "-")[:6]
        ev = (r.get("evidence") or "")[:58]
        print(
            f"{uid:<38} {etype:<12} {nli:<10} {uv:<8} {ev:<60} {str(r.get('created_at', ''))[:19]}"
        )


def _try_embed_feedback(feedback_id: str, text: str) -> None:
    """Embed feedback example text via :8081 for pgvector search.

    Silently skips if embed server is unavailable (CLI should not block).
    """
    import json as _json
    from urllib.request import Request, urlopen

    body = _json.dumps({"input": [text], "model": "default"}).encode()
    req = Request(
        "http://127.0.0.1:8081/v1/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode())
        vec = data["data"][0]["embedding"]
        vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
        from lib.db import psql_ok

        psql_ok(
            f"UPDATE feedback_examples SET embedding = '{vec_str}'::vector WHERE id = '{feedback_id}'::uuid"
        )
        print("  (embedded for pgvector similarity search)")
    except Exception:
        pass  # embed server unavailable — non-critical


def cmd_fact_confirm(args):
    """Set user_verdict=CONFIRM for a fact UUID and store in feedback_examples."""
    fid = esc_sql(args.id)
    fact = _pj(f"""SELECT rf.id, rf.evidence, rf.fact_type,
       CASE rf.fact_type
         WHEN 'user' THEN t.user_turn
         WHEN 'thinking' THEN t.thinking
         WHEN 'text' THEN t.text
       END AS source_text
    FROM review_facts rf
    JOIN turns t ON t.id = rf.turn_id
    WHERE rf.id = '{fid}'""")
    if not fact:
        print(f"  ERROR: fact {args.id[:12]}... not found")
        return
    if not _sql(
        f"UPDATE review_facts SET user_verdict='CONFIRM', user_verdict_at=NOW() WHERE id='{fid}'"
    ):
        print(f"  ERROR: failed to confirm fact {args.id}")
        return
    r = fact[0]
    ev = esc_sql(r.get("evidence", ""))
    src = esc_sql(r.get("source_text", ""))
    ft = esc_sql(r.get("fact_type", ""))
    fb_id = _pj(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'CONFIRM') RETURNING id::text""")
    fb_uuid = (fb_id[0]["id"] if fb_id else "").strip()
    print(f"  Confirmed: {r['id'][:12]}... — {(r.get('evidence') or '')[:60]}")
    if fb_uuid:
        _try_embed_feedback(fb_uuid, r.get("evidence", ""))
    print("  Stored as feedback example for few-shot NLI")


def cmd_fact_reject(args):
    """Set user_verdict=REJECT for a fact UUID and store in feedback_examples."""
    fid = esc_sql(args.id)
    fact = _pj(f"""SELECT rf.id, rf.evidence, rf.fact_type,
       CASE rf.fact_type
         WHEN 'user' THEN t.user_turn
         WHEN 'thinking' THEN t.thinking
         WHEN 'text' THEN t.text
       END AS source_text
    FROM review_facts rf
    JOIN turns t ON t.id = rf.turn_id
    WHERE rf.id = '{fid}'""")
    if not fact:
        print(f"  ERROR: fact {args.id[:12]}... not found")
        return
    if not _sql(
        f"UPDATE review_facts SET user_verdict='REJECT', user_verdict_at=NOW() WHERE id='{fid}'"
    ):
        print(f"  ERROR: failed to reject fact {args.id}")
        return
    r = fact[0]
    ev = esc_sql(r.get("evidence", ""))
    src = esc_sql(r.get("source_text", ""))
    ft = esc_sql(r.get("fact_type", ""))
    fb_id = _pj(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'REJECT') RETURNING id::text""")
    fb_uuid = (fb_id[0]["id"] if fb_id else "").strip()
    print(f"  Rejected: {r['id'][:12]}... — {(r.get('evidence') or '')[:60]}")
    if fb_uuid:
        _try_embed_feedback(fb_uuid, r.get("evidence", ""))
    print("  Stored as feedback example for few-shot NLI")

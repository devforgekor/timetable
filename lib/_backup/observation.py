#!/usr/bin/env python3
# Status: production
# Path: imported by — auto_log.py (hook), mcp_server.py (MCP), cli.py (command), scripts
"""Unified observation API — recording and querying the observations table.

Usage:
    from lib.observation import observe, observe_insight, observe_decision, obs_search

    # Record
    oid = observe("something happened", category="insight", source="script:analyze")
    observe_insight("detected slot deadlock in inference", tags={"domain": ["mcp"]})

    # Query
    rows = obs_search(category="insight", limit=20)

────────────────────────────────────────────────────────────────
Future — split traces into separate table when:
  - observations > ~10M rows (currently <10K)
  - reasoning traces need different retention (e.g. keep 30d vs insight forever)
  - trace-only indexes become a size concern vs general observation queries
Until then: single observations table, category='reasoning' + tags + trace_id in context.
────────────────────────────────────────────────────────────────
"""

import json
import uuid as _uuid
from typing import Any, Dict, List, Optional

from lib.db import esc_sql, psql, psql_json

_OBSERVE_SQL = "INSERT INTO observations (observation, category, source, context, tags) VALUES "

# Recommended categories
CATEGORIES = frozenset(
    {
        "test_result",  # test run outcome
        "edit",  # file edit
        "error",  # error/failure
        "insight",  # learning/discovery
        "decision",  # design decision
        "reasoning",  # analysis chain (deep dive)
        "reference",  # reference info (search results)
        "db_result",  # DB query result
        "config",  # config change
        "general",  # uncategorized
    }
)

# Per-trace_id step counter (in-memory, resets per process)
_trace_counter: Dict[str, int] = {}


def _normalize_tags(tags: Optional[Dict]) -> str:
    """Convert tags dict to JSONB-safe string for SQL insertion."""
    if not tags:
        return "'{}'::jsonb"
    return f"$JSON${json.dumps(tags, ensure_ascii=False)}$JSON$::jsonb"


def observe(
    observation: str,
    category: str = "general",
    source: str = "auto",
    context: Optional[Dict] = None,
    tags: Optional[Dict] = None,
) -> Optional[str]:
    """Record an observation. Returns UUID string on success, None on error."""
    obs = observation.strip()
    if not obs:
        return None

    ctx_json = json.dumps(context or {}, ensure_ascii=False, default=str)
    tags_sql = _normalize_tags(tags)
    obs_escaped = obs.replace("'", "''")
    cat_escaped = esc_sql(category or "general") or "general"
    src_escaped = esc_sql(source or "auto") or "auto"

    sql = (
        f"{_OBSERVE_SQL}("
        f"'{obs_escaped}', '{cat_escaped}', '{src_escaped}', "
        f"$JSON${ctx_json}$JSON$::jsonb, "
        f"{tags_sql}"
        f") RETURNING id::text"
    )
    result = psql(sql)
    if result and result.strip():
        return result.strip()
    return None


# ── Convenience helpers ─────────────────────────────────────


def observe_insight(
    observation: str,
    tags: Optional[Dict] = None,
    **context: Any,
) -> Optional[str]:
    """Record a discovery/insight (category=insight)."""
    merged_tags = {"tier": ["insight"], **(tags or {})}
    return observe(observation, category="insight", context=context or None, tags=merged_tags)


def observe_decision(
    observation: str,
    rationale: str = "",
    tags: Optional[Dict] = None,
    **context: Any,
) -> Optional[str]:
    """Record a design decision (category=decision)."""
    ctx = dict(context)
    if rationale:
        ctx["rationale"] = rationale
    merged_tags = {"tier": ["decision"], **(tags or {})}
    return observe(observation, category="decision", context=ctx or None, tags=merged_tags)


def observe_reasoning(
    observation: str,
    step: Optional[int] = None,
    trace_id: Optional[str] = None,
    tags: Optional[Dict] = None,
    **context: Any,
) -> Optional[str]:
    """Record a reasoning/analysis step (category=reasoning).

    Auto-generates trace_id and step number for Devin-style trace chains.
    Same trace_id within one deep dive → sequential steps queryable via obs_trace().

    Args:
        observation: The reasoning text
        step: Optional explicit step number. Auto-increments per trace_id if omitted.
        trace_id: Optional trace ID. Auto-generated UUID if omitted.
        tags: Optional object-of-arrays tags (tier + domain auto-merged)
    """
    tid = trace_id or str(_uuid.uuid4())
    if step is None:
        _trace_counter[tid] = _trace_counter.get(tid, 0) + 1
        step = _trace_counter[tid]
    ctx: Dict = {"step": step, "trace_id": tid}
    ctx.update(context)
    merged_tags = {"tier": ["reasoning"], **(tags or {})}
    return observe(
        observation,
        category="reasoning",
        source="deep-dive:yggdrasil",
        context=ctx,
        tags=merged_tags,
    )


def observe_error(
    observation: str,
    tags: Optional[Dict] = None,
    **context: Any,
) -> Optional[str]:
    """Record an error (category=error)."""
    merged_tags = {"tier": ["error"], **(tags or {})}
    return observe(observation, category="error", context=context or None, tags=merged_tags)


# ── Query API ─────────────────────────────────────────────


def obs_search(
    category: Optional[str] = None,
    source: Optional[str] = None,
    tags: Optional[Dict] = None,
    query: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """Search observations. All filters are optional.

    Args:
        category: Filter by category (e.g. 'insight', 'error')
        source: Filter by source (e.g. 'hook:PostToolUse', 'mcp:obs_write')
        tags: Filter by tags containment (e.g. {"domain": ["mcp"]})
        query: Full-text search on observation text (pg_trgm ILIKE)
        limit: Max rows (default 50, cap 200)
    """
    limit = max(1, min(limit, 200))
    conds = []
    if category:
        conds.append(f"category = '{esc_sql(category)}'")
    if source:
        conds.append(f"source = '{esc_sql(source)}'")
    if tags:
        conds.append(f"tags @> $JSON${json.dumps(tags, ensure_ascii=False)}$JSON$::jsonb")
    if query:
        q = esc_sql(query)
        conds.append(f"observation ILIKE '%{q}%'")
    where = " AND ".join(conds) if conds else "TRUE"

    rows = psql_json(f"""
        SELECT id::text, observation, category, source, context::text, tags::text, created_at::text
        FROM observations
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT {limit}
    """)
    return rows or []


def obs_trace(trace_id: str) -> List[Dict]:
    """Retrieve all reasoning steps for a trace, ordered by step number."""
    rows = psql_json(f"""
        SELECT id::text, observation, category, source, context::text, tags::text, created_at::text
        FROM observations
        WHERE category = 'reasoning'
          AND context->>'trace_id' = '{esc_sql(trace_id)}'
        ORDER BY (context->>'step')::int ASC
    """)
    return rows or []


def obs_stats(days: int = 7) -> List[Dict]:
    """Return category counts for recent observations."""
    rows = psql_json(f"""
        SELECT category, COUNT(*) AS count
        FROM observations
        WHERE created_at > NOW() - INTERVAL '{days} days'
        GROUP BY category
        ORDER BY count DESC
    """)
    return rows or []

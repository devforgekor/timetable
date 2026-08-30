#!/usr/bin/env python3
# Status: production
# Path: imported by — cli.py (reflex command), auto_fix.py (match), hooks, pipeline scripts
"""Reflex rule CRUD and lifecycle management.

Part of Pattern 2+4 auto-fix system. Shares PostgreSQL with observations.
Reflex rules are mined from observations, then promoted/culled by lifecycle.

Usage:
    from lib.reflex_rules import (
        rule_create, rule_update, rule_delete,
        rule_get, rule_search, rule_match,
        rule_promote_all, rule_decay_all, rule_report
    )

    # Create a rule
    rid = rule_create(
        trigger_category="error",
        trigger_pattern="timeout",
        action_type="auto_fix",
        action_params={"function": "raise_timeout", "args": {"mcp_name": "test"}},
        description="Auto-raise timeout on MCP timeout errors",
    )

    # Match an observation against approved rules
    matches = rule_match("MCP search-proxy timeout", category="error")

    # Lifecycle
    promoted = rule_promote_all()
    decayed = rule_decay_all()
"""

import json
from typing import Any, Dict, List, Optional

from lib.db import esc_sql, psql, psql_json, psql_ok

_RULE_COLS = (
    "id::text, trigger_category, trigger_source, trigger_tags::text, "
    "trigger_pattern, trigger_min_count, trigger_window_hours, "
    "action_type, action_params::text, "
    "confidence, status, description, rationale, "
    "observation_count, last_matched_at::text, last_applied_at::text, "
    "supersedes, created_at::text, updated_at::text"
)


def _row_to_dict(row: dict) -> dict:
    """Normalize a rule row from psql_json into a clean dict."""
    result = dict(row)
    # Parse JSONB strings back to dicts
    for key in ("trigger_tags", "action_params"):
        if isinstance(result.get(key), str):
            try:
                result[key] = json.loads(result[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return result


# ── CRUD ──────────────────────────────────────────────────────


def rule_create(
    trigger_category: Optional[str] = None,
    trigger_source: Optional[str] = None,
    trigger_tags: Optional[Dict] = None,
    trigger_pattern: Optional[str] = None,
    trigger_min_count: int = 1,
    trigger_window_hours: int = 24,
    action_type: str = "notify",
    action_params: Optional[Dict] = None,
    confidence: float = 0.0,
    status: str = "candidate",
    description: Optional[str] = None,
    rationale: Optional[str] = None,
) -> Optional[str]:
    """Create a new reflex rule. Returns UUID string on success."""
    cols = []
    vals = []

    if trigger_category is not None:
        cols.append("trigger_category")
        vals.append(f"'{esc_sql(trigger_category)}'")
    if trigger_source is not None:
        cols.append("trigger_source")
        vals.append(f"'{esc_sql(trigger_source)}'")

    tags_json = json.dumps(trigger_tags or {}, ensure_ascii=False)
    cols.append("trigger_tags")
    vals.append(f"$JSON${tags_json}$JSON$::jsonb")

    if trigger_pattern is not None:
        cols.append("trigger_pattern")
        vals.append(f"'{esc_sql(trigger_pattern)}'")
    cols.append("trigger_min_count")
    vals.append(str(trigger_min_count))
    cols.append("trigger_window_hours")
    vals.append(str(trigger_window_hours))
    cols.append("action_type")
    vals.append(f"'{esc_sql(action_type)}'")

    params_json = json.dumps(action_params or {}, ensure_ascii=False)
    cols.append("action_params")
    vals.append(f"$JSON${params_json}$JSON$::jsonb")
    cols.append("confidence")
    vals.append(f"{confidence:.6f}")
    cols.append("status")
    vals.append(f"'{esc_sql(status)}'")
    if description is not None:
        cols.append("description")
        vals.append(f"'{esc_sql(description)}'")
    if rationale is not None:
        cols.append("rationale")
        vals.append(f"'{esc_sql(rationale)}'")

    sql = (
        f"INSERT INTO reflex_rules ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)}) RETURNING id::text"
    )
    result = psql(sql)
    if result and result.strip():
        return result.strip()
    return None


def rule_update(
    rule_id: str,
    **kwargs: Any,
) -> bool:
    """Update rule fields. Pass field=value for any column.

    Supports: trigger_category, trigger_source, trigger_pattern,
    trigger_min_count, trigger_window_hours, action_type, action_params (dict),
    confidence, status, description, rationale.
    """
    sets = []
    for key, val in kwargs.items():
        if key in ("trigger_tags", "action_params") and isinstance(val, dict):
            sets.append(f"{key} = $JSON${json.dumps(val, ensure_ascii=False)}$JSON$::jsonb")
        elif key in ("confidence",) and isinstance(val, (int, float)):
            sets.append(f"{key} = {val:.6f}")
        elif key in ("trigger_min_count", "trigger_window_hours"):
            sets.append(f"{key} = {int(val)}")
        elif isinstance(val, str):
            sets.append(f"{key} = '{esc_sql(val)}'")
        else:
            sets.append(f"{key} = '{esc_sql(str(val))}'")
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    sql = f"UPDATE reflex_rules SET {', '.join(sets)} WHERE id = '{esc_sql(rule_id)}'::uuid"
    return psql_ok(sql)


def rule_delete(rule_id: str) -> bool:
    """Delete a rule by ID."""
    return psql_ok(f"DELETE FROM reflex_rules WHERE id = '{esc_sql(rule_id)}'::uuid")


def rule_get(rule_id: str) -> Optional[dict]:
    """Get a single rule by ID."""
    rows = psql_json(f"SELECT {_RULE_COLS} FROM reflex_rules WHERE id = '{esc_sql(rule_id)}'::uuid")
    if rows:
        return _row_to_dict(rows[0])
    return None


def rule_search(
    status: Optional[str] = None,
    action_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict]:
    """Search rules. All filters optional. Ordered by confidence DESC."""
    conds = []
    if status:
        conds.append(f"status = '{esc_sql(status)}'")
    if action_type:
        conds.append(f"action_type = '{esc_sql(action_type)}'")
    where = " AND ".join(conds) if conds else "TRUE"

    rows = psql_json(f"""
        SELECT {_RULE_COLS}
        FROM reflex_rules
        WHERE {where}
        ORDER BY confidence DESC, observation_count DESC
        LIMIT {limit} OFFSET {offset}
    """)
    return [_row_to_dict(r) for r in rows]


# ── Matching ────────────────────────────────────────────────────


def rule_match(
    observation_text: str,
    category: Optional[str] = None,
    tags: Optional[Dict] = None,
    source: Optional[str] = None,
) -> List[Dict]:
    """Find approved rules matching an observation via SQL function.

    Returns rules ordered by confidence DESC.
    Also updates last_matched_at and observation_count atomically in the DB.
    """
    cat_arg = f"'{esc_sql(category)}'" if category else "NULL"
    tags_arg = f"$JSON${json.dumps(tags or {}, ensure_ascii=False)}$JSON$::jsonb"
    obs_arg = f"'{esc_sql(observation_text)}'"
    src_arg = f"'{esc_sql(source)}'" if source else "NULL"
    sql = f"SELECT rule_id, description, action_type, action_params::text, confidence FROM match_rule({obs_arg}, {cat_arg}, {tags_arg}, {src_arg})"
    rows = psql_json(sql)
    if not rows:
        return []
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("action_params"), str):
            try:
                d["action_params"] = json.loads(d["action_params"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


# ── Lifecycle ──────────────────────────────────────────────────


def rule_promote_all(
    min_confidence: float = 0.5, min_observations: int = 3, window_hours: int = 48
) -> List[Dict]:
    """Auto-promote candidate rules to approved. Returns list of promoted rules."""
    sql = f"SELECT rule_id, old_status, new_status, confidence FROM promote_rules({min_confidence:.6f}, {min_observations}, {window_hours})"
    rows = psql_json(sql)
    return rows or []


def rule_decay_all(dormant_days: int = 30) -> List[Dict]:
    """Demote unused approved/candidate rules to dormant. Returns list of decayed rules."""
    sql = (
        f"SELECT rule_id, old_status, new_status, days_since_match FROM decay_rules({dormant_days})"
    )
    rows = psql_json(sql)
    return rows or []


def rule_detect_patterns(since_hours: int = 24, min_occurrences: int = 3) -> List[Dict]:
    """Detect frequent patterns from recent observations.

    Returns candidate clusters with occurrence count.
    """
    sql = f"SELECT pattern_category, pattern_source, pattern_observation, occurrence_count, distinct_sources, sample_observation FROM detect_patterns({since_hours}, {min_occurrences})"
    rows = psql_json(sql)
    return rows or []


def rule_report() -> List[Dict]:
    """Generate daily rule activity report from SQL function."""
    sql = "SELECT section, line FROM daily_rule_report()"
    rows = psql_json(sql)
    return rows or []


def rule_auto_candidate(pattern: dict) -> Optional[str]:
    """Auto-create a candidate rule from a detected pattern.

    Called by detect_patterns → user approves → promote.
    Or directly for high-confidence patterns.
    """
    cat = pattern.get("pattern_category")
    src = pattern.get("pattern_source")
    obs = pattern.get("pattern_observation", "")
    count = pattern.get("occurrence_count", 1)

    # Build a sensible description
    desc_parts = [f"Pattern detected: {count}x"]
    if cat:
        desc_parts.append(f"category={cat}")
    if src:
        desc_parts.append(f"source={src}")
    desc = " ".join(desc_parts)

    # Determine action based on category
    action = "notify"
    params: dict = {}
    if cat == "error":
        action = "escalate"
        params["suggested_fix"] = f"Auto-detected: {obs[:80]}"

    return rule_create(
        trigger_category=cat,
        trigger_source=src,
        trigger_pattern=obs[:80],
        trigger_min_count=count,
        action_type=action,
        action_params=params,
        confidence=min(0.3 + count * 0.1, 0.9),
        description=desc,
        rationale=f"Auto-mined from observations: {obs[:200]}",
    )

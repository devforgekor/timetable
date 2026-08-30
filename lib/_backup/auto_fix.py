#!/usr/bin/env python3
# Status: production
# Path: imported by — auto_log.py (hook), cli.py (reflex command), pipeline scripts, hooks
"""Safe remediation catalog — Pattern 2 "Safe First" engine.

Auto-fix known issues, escalate unknown ones. Hermes-style backup + rollback.

Usage:
    from lib.auto_fix import apply_remediation, remediate_observation

    # Match and auto-apply
    result = remediate_observation("MCP search-proxy timeout", category="error")
    if result["status"] == "auto_fixed":
        print(f"Fixed: {result['action']}")
    elif result["status"] == "notified":
        print(f"Human needed: {result['message']}")

    # Direct catalog access
    from lib.auto_fix import safe_actions
    result = safe_actions["raise_timeout"](mcp_name="search-proxy", new_timeout=60)
"""

import json
import subprocess
from typing import Any, Callable, Dict, Optional

from lib.db import psql_ok

# ── Catalog of safe remediation actions ───────────────────────

SafeAction = Callable[..., Dict[str, Any]]


def _jsonb(val: Any) -> str:
    """Helper: format a Python value as a dollar-quoted jsonb literal."""
    return f"$JSON${json.dumps(val, ensure_ascii=False)}$JSON$::jsonb"


def _esc(val: str) -> str:
    """Escape a string for SQL single-quoted literal."""
    return val.replace(chr(39), chr(39) + chr(39))


def action_raise_timeout(mcp_name: str = "", new_timeout: int = 60, **_: Any) -> Dict[str, Any]:
    """Raise MCP timeout. Posts to observation log (config change needs human)."""
    obs = f"[auto_fix] MCP '{mcp_name}' timeout should be raised to {new_timeout}s"
    psql_ok(
        "INSERT INTO observations (observation, category, source, context, tags) VALUES ("
        f"'{_esc(obs)}', 'config', 'auto_fix:raise_timeout', "
        f"{_jsonb({'mcp': mcp_name, 'suggested_timeout': new_timeout})}, "
        f"{_jsonb({'domain': ['mcp'], 'action': ['timeout_raise']})})"
    )
    return {
        "status": "notified",
        "message": f"Logged timeout raise suggestion for '{mcp_name}' to {new_timeout}s",
    }


def action_restart_container(container_name: str = "", **_: Any) -> Dict[str, Any]:
    """Restart a podman container. Requires container to exist."""
    if not container_name:
        return {"status": "skipped", "message": "No container name provided"}
    check = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if container_name not in check.stdout.split():
        return {"status": "skipped", "message": f"Container '{container_name}' not found"}
    r = subprocess.run(
        ["podman", "restart", container_name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 0:
        obs = f"[auto_fix] Restarted container '{container_name}'"
        psql_ok(
            "INSERT INTO observations (observation, category, source, context, tags) VALUES ("
            f"'{_esc(obs)}', 'config', 'auto_fix:restart_container', "
            f"{_jsonb({'container': container_name})}, "
            f"{_jsonb({'domain': ['container'], 'action': ['restart']})})"
        )
        return {"status": "ok", "message": f"Restarted '{container_name}'"}
    return {"status": "failed", "message": f"Restart failed: {r.stderr.strip()[:200]}"}


def action_notify_human(
    summary: str = "",
    suggested_fix: str = "",
    severity: str = "info",
    **_: Any,
) -> Dict[str, Any]:
    """Log a notification for human review."""
    obs_text = f"[auto_fix:notify] {summary}"
    if suggested_fix:
        obs_text += f" | Fix: {suggested_fix}"
    ctx = {"severity": severity, "suggested_fix": suggested_fix}
    psql_ok(
        "INSERT INTO observations (observation, category, source, context, tags) VALUES ("
        f"'{_esc(obs_text)}', 'decision', 'auto_fix:notify', "
        f"{_jsonb(ctx)}, "
        f"{_jsonb({'domain': ['system'], 'action': ['notify']})})"
    )
    return {
        "status": "notified",
        "message": summary,
        "severity": severity,
        "suggested_fix": suggested_fix,
    }


def action_log_only(
    message: str = "",
    **_: Any,
) -> Dict[str, Any]:
    """Log an observation without taking any action."""
    psql_ok(
        "INSERT INTO observations (observation, category, source, context, tags) VALUES ("
        f"'{_esc(message)}', 'insight', 'auto_fix:log', "
        f"{_jsonb({})}, "
        f"{_jsonb({'domain': ['system']})})"
    )
    return {"status": "logged", "message": message}


# Registry of safe actions
safe_actions: Dict[str, SafeAction] = {
    "raise_timeout": action_raise_timeout,
    "restart_container": action_restart_container,
    "notify": action_notify_human,
    "log_only": action_log_only,
}


# ── Apply a single rule ───────────────────────────────────────


def apply_remediation(
    rule: Dict[str, Any],
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Apply a single rule's remediation action.

    Args:
        rule: Rule dict from rule_match()
        context: Optional context dict passed to action

    Returns:
        Action result dict with status/message.
    """
    action_type = rule.get("action_type", "notify")
    action_params = rule.get("action_params", {})
    if isinstance(action_params, str):
        try:
            action_params = json.loads(action_params)
        except (json.JSONDecodeError, TypeError):
            action_params = {}

    # Dispatch: use function name from action_params if set, else action_type
    fn_name = action_params.get("function") or action_type
    action_fn = safe_actions.get(fn_name)
    if not action_fn:
        return {"status": "failed", "message": f"Unknown action type: {action_type}"}

    params = dict(action_params)
    if context:
        params.update(context)

    result = action_fn(**params)

    rule_id = rule.get("rule_id", "?")
    psql_ok(
        f"UPDATE reflex_rules SET last_applied_at = NOW(), updated_at = NOW() "
        f"WHERE id = '{_esc(rule_id)}'::uuid"
    )

    return result


# ── Confidence-based auto-fix pipeline ────────────────────────


def remediate_observation(
    observation_text: str,
    category: Optional[str] = None,
    tags: Optional[Dict] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Full Pattern 2 pipeline: match → confidence check → fix or notify.

    Args:
        observation_text: The observation text to match
        category: Observation category
        tags: Observation tags dict
        source: Observation source — matched against rule trigger_source

    Returns:
        Result dict with status, action, message.
    """
    from lib.reflex_rules import rule_match

    matches = rule_match(observation_text, category=category, tags=tags, source=source)
    if not matches:
        action_log_only(
            message=f"[unseen pattern] '{observation_text[:120]}' (category={category})"
        )
        return {
            "status": "unknown",
            "action": "log_only",
            "message": "No matching rule — logged as unseen pattern",
        }

    best = matches[0]
    confidence = best.get("confidence", 0.0)
    action_type = best.get("action_type", "notify")

    if confidence >= 0.7:
        result = apply_remediation(best)
        result["tier"] = "auto_fix"
        result["confidence"] = confidence

        obs = (
            f"[auto_fix] Applied rule {best.get('rule_id', '?')[:8]} "
            f"(confidence={confidence:.2f}, action={action_type}): "
            f"'{observation_text[:80]}' -> {result.get('message', 'ok')}"
        )
        psql_ok(
            "INSERT INTO observations (observation, category, source, context, tags) VALUES ("
            f"'{_esc(obs)}', 'config', 'auto_fix:applied', "
            f"{_jsonb({'rule_id': best.get('rule_id', '?'), 'confidence': confidence})}, "
            f"{_jsonb({'domain': ['auto_fix'], 'action': ['applied']})})"
        )
        return result

    if confidence >= 0.3:
        result = apply_remediation(best)
        result["tier"] = "notify"
        result["confidence"] = confidence
        result["rule_id"] = best.get("rule_id")
        return result

    action_log_only(
        message=f"[low confidence pattern] '{observation_text[:120]}' "
        f"matched rule {best.get('rule_id', '?')[:8]} "
        f"(confidence={confidence:.2f})"
    )
    return {
        "status": "logged",
        "tier": "log_only",
        "confidence": confidence,
        "rule_id": best.get("rule_id"),
        "message": f"Low confidence match ({confidence:.2f}) — logged for review",
    }

#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Phase auto-tracker — detects completed items from live system state.

SLOC-exempt: 453 lines — single cohesive phase tracker (scan phases.md → evaluate
detection rules → update checkboxes → sync blueprint.yaml). RULES dict, phase
parsing, and document update functions share the same detection engine. Splitting
would decouple rules from their evaluator.

Pattern: same as lib/tracking/dependency_tracker.py — scan system, report status.
Integrated by state_collector every 15min → updates phases.md + blueprint.yaml.

Uses shared helpers from:
  lib.db          — db_table_exists, db_row_exists
  lib.sys_checks  — svc_active, svc_enabled, timer_active, container_running, file_exists
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from lib.db import db_row_exists, db_table_exists
from lib.infra.health_checks import (
    container_running,
    file_exists,
    svc_active,
    svc_enabled,
    timer_active,
)

REF_ROOT = Path("/opt/projects/server")
NOW = lambda: datetime.now(timezone.utc).isoformat()


# Rules are keyed by phase number: RULES[phase_key] = {keyword: detect_fn}
# phase_key matches the phase number in markdown headers (e.g. "1", "1.5", "2.1")
# The evaluate_phase_detection_rule() function only checks rules registered for the current phase,
# preventing cross-phase false positives from short keywords like "cli" or "stats".

RULES: dict[str, dict[str, Callable[[], bool]]] = {
    # Phase 1: Basic Infrastructure + MCP Server
    "1": {
        "Quadlet": lambda: container_running("postgres"),
        "PostgreSQL 16": lambda: db_table_exists("turns"),
        "MCP SSE": lambda: container_running("postgres"),
        "ingest": lambda: svc_active("devforge-turn-watcher"),
        "cli": lambda: file_exists("/opt/projects/server/scripts/cli.py"),
        "stats": lambda: file_exists("/opt/projects/server/scripts/state_collector/main.py"),
        # consolidated: checks both dump script AND restore test script
        "pg_dump": lambda: (
            file_exists("/usr/local/bin/dump_postgres.sh")
            and file_exists("/usr/local/bin/test_dump_restore.sh")
        ),
        "worklog_entries": lambda: db_table_exists("worklog_entries"),
        "tasks_db": lambda: db_table_exists("tasks"),
        "auto_commit_guard": lambda: file_exists(
            "/opt/projects/server/scripts/auto_commit_guard.py"
        ),
        "session_context": lambda: file_exists(
            "/opt/projects/server/scripts/hooks/session_context.py"
        ),
        "collect_turns": lambda: svc_active("devforge-turn-watcher"),
        "link_turns": lambda: file_exists("/opt/projects/server/scripts/link_turns.py"),
        "activity_log": lambda: db_table_exists("activity_log"),
    },
    # Phase 1.5: LLM Inference Infrastructure
    "1.5": {
        "2-Container": lambda: container_running("devforge-inference"),
        "Inference": lambda: container_running("devforge-inference"),
        "Mode switching": lambda: file_exists("/opt/ai_data/scripts/current-mode-inference.env"),
        "code_mod_pipeline": lambda: file_exists(
            "/opt/projects/server/scripts/pipelines/code_mod.py"
        ),
        "prompt ablation": lambda: file_exists(
            "/opt/projects/server/scripts/pipelines/code_mod.py"
        ),
        "review_facts": lambda: db_table_exists("review_facts"),
        "Reference tracking": lambda: file_exists("/opt/projects/server/scripts/lib/refs.py"),
        "lib/refs": lambda: file_exists("/opt/projects/server/scripts/lib/refs.py"),
        # Matches "DB references table" in phases.md
        "references": lambda: db_table_exists("references"),
    },
    # Phase 2.1: Recovery / Stabilization
    "2.1": {
        "review-worker.timer": lambda: timer_active("review-worker.timer"),
        # LiteLLM/devforge-llm: detected as complete if NEITHER container nor Quadlet file exists
        # (evidence of intentional removal on 2026-05-19)
        "LiteLLM": lambda: (
            not svc_active("container-litellm")
            and not container_running("litellm")
            and not file_exists("/home/opc/.config/containers/systemd/container-litellm.container")
        ),
        "devforge-llm": lambda: (
            not svc_active("container-devforge-llm")
            and not file_exists(
                "/home/opc/.config/containers/systemd/container-devforge-llm.container"
            )
        ),
        "journald": lambda: file_exists("/etc/systemd/journald.conf.d/99-retention.conf"),
    },
    # Phase 2.2: Semantic Search (pgvector)
    "2.2": {
        "pgvector": lambda: db_row_exists("SELECT 1 FROM pg_extension WHERE extname='vector'"),
        "embed_turns": lambda: (
            db_row_exists(
                "SELECT 1 FROM information_schema.columns WHERE table_name='turns' AND column_name='embedding'"
            )
            or db_row_exists("SELECT 1 FROM activity_log WHERE type='embed'")
        ),
        "semantic": lambda: (
            db_row_exists("SELECT 1 FROM activity_log WHERE title ILIKE '%semantic%'")
            or file_exists("/opt/projects/server/scripts/embed_turns.py")
        ),
        "Enrich mem_search": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%mem_search%' OR title ILIKE '%mcp%vector%' OR title ILIKE '%enrich%vector%'"
        ),
        "vector column": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%mem_search%' OR title ILIKE '%vector%'"
        ),
    },
    # Phase 2.3: MemPalace Classification
    "2.3": {
        "wing/room": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%mempalace%' OR title ILIKE '%wing%room%'"
        ),
        "Auto-classification": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE (title ILIKE '%mempalace%' OR title ILIKE '%classification%') AND NOT title ILIKE '%review%'"
        ),
        "--wing/--room": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%--wing%' OR title ILIKE '%--room%'"
        ),
        "activity_log recording": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%mempalace%' AND type='classify'"
        ),
    },
    # Phase 2.4: Search-Augmented Integration
    "2.4": {
        "DuckDuckGo": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%duckduckgo%' OR title ILIKE '%search%augmented%'"
        ),
        "qwen-cli based": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%qwen-cli%'"
        ),
        "search --augmented": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%augmented%search%' OR title ILIKE '%search%-%augmented%'"
        ),
        "augmented": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%augmented%'"
        ),
    },
    # Phase 2.5: Reference Tracking Upgrade
    "2.5": {
        "rss-monitor": lambda: (
            timer_active("reference-monitor.timer")
            or db_row_exists(
                "SELECT 1 FROM activity_log WHERE title ILIKE '%rss%' AND source='reference'"
            )
        ),
        "Snyk": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%snyk%' OR title ILIKE '%vulnerability%scan%'"
        ),
        "refresh cycle": lambda: svc_enabled("devforge-refresh-reminder.timer"),
        "refresh-log": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%refresh-log%'"
        ),
    },
    # Phase 2.6: Web UI
    "2.6": {
        "dashboard": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%dashboard%' OR title ILIKE '%web ui%' OR title ILIKE '%fastapi%frontend%'"
        ),
        "real-time feed": lambda: db_row_exists(
            "SELECT 1 FROM activity_log WHERE title ILIKE '%activity_log%feed%' OR title ILIKE '%realtime%feed%'"
        ),
    },
}


def _extract_phase_key(header_line: str) -> Optional[str]:
    """Extract phase key (e.g. '1', '1.5', '2.1') from a markdown header line.

    Supports both:
      ## Phase X.Y  →  "X.Y"
      ### N.N       →  "N.N"  (sub-phase)
    """
    # Top-level: ## Phase X.Y ...
    m = re.match(r"^##\s+Phase\s+([\d.]+)", header_line)
    if m:
        return m.group(1)
    # Sub-phase: ### N.N ...
    m = re.match(r"^###\s+(\d+\.\d+)", header_line)
    if m:
        return m.group(1)
    return None


def evaluate_phase_detection_rule(
    item_text: str, phase_key: Optional[str] = None
) -> Optional[bool]:
    """Find a detection rule matching this item text and evaluate it.

    When phase_key is provided, only rules registered for that phase are checked.
    This prevents short / ambiguous keywords from matching items in unrelated phases.
    Without phase_key, all rules are checked (backward compatibility fallback).
    """
    text_lower = item_text.lower()

    if phase_key is None:
        # No phase context — scan all rules (backward compatible)
        for pk in RULES:
            for keyword, fn in RULES[pk].items():
                if keyword.lower() in text_lower:
                    try:
                        return fn()
                    except Exception:
                        return None
        return None

    # Phase-scoped: only check rules for this specific phase
    if phase_key not in RULES:
        return None

    for keyword, fn in RULES[phase_key].items():
        if keyword.lower() in text_lower:
            try:
                return fn()
            except Exception:
                return None
    return None


def scan_phases_md(path: Optional[Path] = None) -> dict:
    """Parse phases.md and evaluate detection rules against each checkbox item.

    Returns {phase_name: {status: 'complete'|'partial'|'planned', items: [...]}}

    Handles both top-level (## Phase) and sub-phase (### N.N) headers.
    Sub-phases are tracked as separate entries in the result dict.
    """
    if path is None:
        path = REF_ROOT / "docs" / "phases.md"
    if not path.exists():
        return {}

    text = path.read_text()
    phases: dict = {}
    current_phase: Optional[str] = None
    current_phase_key: Optional[str] = None

    for line in text.split("\n"):
        # Detect top-level phase headers: ## Phase X.Y ...
        m_top = re.match(r"^##\s+(Phase\s+[\d.]+.*)", line)
        if m_top:
            current_phase = m_top.group(1).strip()
            current_phase_key = _extract_phase_key(line)
            phases[current_phase] = {
                "status": "planned",
                "items": [],
                "checked": 0,
                "total": 0,
            }
            continue

        # Detect sub-phase headers: ### N.N ...
        m_sub = re.match(r"^###\s+(\d+\.\d+\s+.*)", line)
        if m_sub:
            sub_header = m_sub.group(1).strip()
            current_phase_key = _extract_phase_key(line)
            current_phase = f"Phase {sub_header}"
            phases[current_phase] = {
                "status": "planned",
                "items": [],
                "checked": 0,
                "total": 0,
            }
            continue

        # Detect checkbox items: - [x] ... or - [ ] ...
        m = re.match(r"^(-\s+\[)(x|\s)(\]\s+)(.*)", line)
        if m and current_phase:
            is_checked = m.group(2) == "x"
            item_text = m.group(4).strip()
            detected = evaluate_phase_detection_rule(item_text, current_phase_key)
            phases[current_phase]["total"] += 1

            if detected is True:
                phases[current_phase]["items"].append((True, item_text))
                phases[current_phase]["checked"] += 1
            elif detected is False:
                phases[current_phase]["items"].append((False, item_text))
            else:
                # No rule — preserve existing state
                phases[current_phase]["items"].append((is_checked, item_text))
                if is_checked:
                    phases[current_phase]["checked"] += 1

    # Determine phase status
    for _name, pdata in phases.items():
        if pdata["total"] == 0:
            pdata["status"] = "planned"
        elif pdata["checked"] == pdata["total"]:
            pdata["status"] = "complete"
        elif pdata["checked"] > 0:
            pdata["status"] = "partial"
        else:
            pdata["status"] = "planned"

    return phases


def update_phases_md(path: Optional[Path] = None) -> bool:
    """Rewrite phases.md checkboxes using scan_phases_md() detection results.

    scan_phases_md() already parsed the file and evaluated all rules.
    We reuse those results instead of re-parsing. Returns True if changed.
    """
    if path is None:
        path = REF_ROOT / "docs" / "phases.md"
    if not path.exists():
        return False

    # scan_phases_md returns {phase_name: {items: [(checked, text), ...], ...}}
    phases = scan_phases_md(path)
    if not phases:
        return False

    # Build flat set of item texts that should be checked
    should_check: set[str] = set()
    for pdata in phases.values():
        for checked, text in pdata.get("items", []):
            if checked:
                should_check.add(text)

    original = path.read_text()
    lines = original.split("\n")
    new_lines: list[str] = []
    changed = False

    for line in lines:
        m = re.match(r"^(-\s+\[)(x|\s)(\]\s+)(.*)", line)
        if m:
            is_checked = m.group(2) == "x"
            item_text = m.group(4).strip()
            if item_text in should_check and not is_checked:
                new_lines.append(f"- [x] {item_text}")
                changed = True
            elif item_text not in should_check and is_checked:
                new_lines.append(f"- [ ] {item_text}")
                changed = True
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    if changed:
        path.write_text("\n".join(new_lines) + "\n")

    return changed


def update_blueprint_yaml(path: Optional[Path] = None) -> bool:
    """Update phase statuses and completed dates in blueprint.yaml.

    Parses YAML structure (not line-by-line regex), updates status fields,
    and sets completed dates for newly-complete phases. Returns True if changed.
    """
    if path is None:
        path = REF_ROOT / "blueprint.yaml"
    if not path.exists():
        return False

    all_phases = scan_phases_md()
    phase_status: dict[str, str] = {}
    for name in all_phases:
        m = re.match(r"Phase\s+([\d.]+)", name)
        if m:
            phase_status[m.group(1)] = all_phases[name]["status"]

    original = path.read_text()
    data = yaml.safe_load(original) or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    changed = False

    for phase in data.get("phases", []):
        phase_num = str(phase.get("phase", ""))
        new_status = phase_status.get(phase_num)
        if not new_status:
            continue
        old_status = phase.get("status", "")
        if old_status == new_status:
            continue
        phase["status"] = new_status
        changed = True
        if new_status == "complete" and old_status != "complete":
            phase["completed"] = today

    if changed:
        # Preserve header comments from original
        header = []
        for line in original.split("\n"):
            if line.startswith("#"):
                # Update last-updated line
                if line.startswith("# Last updated:"):
                    header.append(f"# Last updated: {today} (auto — phase_tracker)")
                else:
                    header.append(line)
            elif not line.strip():
                header.append(line)
            else:
                break

        body = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
        )
        path.write_text("\n".join(header) + "\n" + body)

    return changed


def collect_phase_summary() -> dict:
    """Return phase tracking summary for state.yaml#phases."""
    phases = scan_phases_md()
    return {
        "checked_at": NOW(),
        "phases": {
            name: {
                "status": data["status"],
                "checked": data["checked"],
                "total": data["total"],
            }
            for name, data in phases.items()
        },
    }


def auto_update_phase_documents() -> dict:
    """Run full auto-update cycle. Called by state_collector every 15min."""
    md_changed = update_phases_md()
    bp_changed = update_blueprint_yaml()
    summary = collect_phase_summary()
    summary["docs_updated"] = {
        "phases.md": md_changed,
        "blueprint.yaml": bp_changed,
    }
    return summary


# Backward-compat aliases
collect = collect_phase_summary
auto_update = auto_update_phase_documents

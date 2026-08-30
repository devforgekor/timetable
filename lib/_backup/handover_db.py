#!/usr/bin/env python3
# Status: production
"""handover_db.py — DB read/write operations for update_handover.py."""

import ast
import json
import re
from pathlib import Path
from typing import Optional

import yaml

from lib.db import psql as _psql, psql_json as _pj, esc_sql

SERVER_DIR = Path("/opt/projects/server")
HANDOVER_FILE = SERVER_DIR / "handover.yaml"


def parse_str_dict(text: str):
    """Parse a str(dict) like "{'id': 'nd05-01', 'detail': '...'}" even with
    unescaped quotes inside detail. Falls back to regex if ast.literal_eval fails."""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError):
        pass
    m = re.match(r"\{'id':\s*'([^']+)',\s*'detail':\s*'(.*)'\s*\}", text, re.DOTALL)
    if m:
        return {"id": m.group(1), "detail": m.group(2)}
    return None


def clean_log_text(text: str) -> str:
    """Extract human-readable text from str(dict) completed_log entries.
    Handles format like {'session DATE': 'TEXT...'} → TEXT."""
    m = re.match(r"\{'[^']+':\s*'(.+)'\s*\}", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def db_write_checkpoint(checkpoint: dict, data: dict, skip_checkpoint: bool = False):
    """Write handover checkpoint data to DB tables, then regenerate YAML.
    If skip_checkpoint=True, only write decisions/issues/logs without creating a new checkpoint row."""
    cp_id = _insert_checkpoint(checkpoint) if not skip_checkpoint else None

    _write_decisions(data.get("decisions", []), cp_id)
    _write_known_issues(data.get("known_issues", []), cp_id)
    _write_completed_log(data.get("completed_log", []), cp_id)

    regenerate_handover_yaml()


def _insert_checkpoint(checkpoint: dict) -> Optional[int]:
    summary = esc_sql(checkpoint.get("summary", ""))
    total_files = checkpoint.get("total_files", 0)
    recent_json = json.dumps(checkpoint.get("recent_files", {}))
    task = esc_sql(checkpoint.get("task") or "")

    cp_sql = f"""INSERT INTO session_checkpoints (summary, total_files, recent_files, task)
    VALUES ('{summary}', {total_files}, '{recent_json}'::jsonb, NULLIF('{task}', ''))
    RETURNING id"""
    cp_id_str = _psql(cp_sql)
    if not cp_id_str or not cp_id_str.strip():
        print("  DB checkpoint write FAILED")
        return None
    cp_id = int(cp_id_str.strip())
    print(f"  DB: session_checkpoint id={cp_id}")
    return cp_id


def _write_decisions(decisions: list, cp_id: Optional[int]):
    for dec in decisions:
        if isinstance(dec, dict):
            did = esc_sql(dec.get("id", ""))
            dtl = esc_sql(dec.get("detail", ""))
            st = esc_sql(dec.get("status", "open"))
            exists = _pj(f"SELECT 1 FROM decisions WHERE decision_id = '{did}' LIMIT 1")
            if not exists:
                _psql(
                    f"INSERT INTO decisions (checkpoint_id, decision_id, detail, status) "
                    f"VALUES ({cp_id}, '{did}', '{dtl}', '{st}')"
                )
        else:
            txt = str(dec)
            parsed = parse_str_dict(txt)
            if parsed:
                did = esc_sql(parsed.get("id", ""))
                dtl = esc_sql(parsed.get("detail", "")[:5000])
                st = esc_sql(parsed.get("status", "open"))
                exists = _pj(f"SELECT 1 FROM decisions WHERE decision_id = '{did}' LIMIT 1")
                if not exists:
                    _psql(
                        f"INSERT INTO decisions (checkpoint_id, decision_id, detail, status) "
                        f"VALUES ({cp_id}, '{did}', '{dtl}', '{st}')"
                    )
            else:
                dt = esc_sql(txt)
                exists = _pj(f"SELECT 1 FROM decisions WHERE decision_text = '{dt}' LIMIT 1")
                if not exists:
                    _psql(f"INSERT INTO decisions (checkpoint_id, decision_text) VALUES ({cp_id}, '{dt}')")


def _write_known_issues(issues: list, cp_id: Optional[int]):
    for iss in issues:
        if isinstance(iss, dict):
            iid = esc_sql(iss.get("id", iss.get("issue_id", "")))
            dtl = esc_sql(iss.get("detail", iss.get("text", ""))[:5000])
            resolved = "true" if iss.get("resolved") else "false"
            if iid:
                exists = _pj(f"SELECT 1 FROM known_issues WHERE issue_id = '{iid}' LIMIT 1")
            else:
                exists = _pj(f"SELECT 1 FROM known_issues WHERE issue_text = '{dtl}' LIMIT 1")
            if not exists:
                if iid:
                    _psql(
                        f"INSERT INTO known_issues (checkpoint_id, issue_id, detail, resolved) "
                        f"VALUES ({cp_id}, '{iid}', '{dtl}', {resolved})"
                    )
                else:
                    _psql(
                        f"INSERT INTO known_issues (checkpoint_id, issue_text, resolved) "
                        f"VALUES ({cp_id}, '{dtl}', {resolved})"
                    )
        else:
            txt = str(iss)
            parsed = parse_str_dict(txt)
            if parsed:
                iid = esc_sql(parsed.get("id", ""))
                dtl = esc_sql(parsed.get("detail", txt)[:5000])
                resolved = "true" if parsed.get("resolved") else "false"
                exists = _pj(f"SELECT 1 FROM known_issues WHERE issue_id = '{iid}' LIMIT 1") if iid else []
                if not exists:
                    if iid:
                        _psql(
                            f"INSERT INTO known_issues (checkpoint_id, issue_id, detail, resolved) "
                            f"VALUES ({cp_id}, '{iid}', '{dtl}', {resolved})"
                        )
                    else:
                        _psql(f"INSERT INTO known_issues (checkpoint_id, issue_text) VALUES ({cp_id}, '{dtl}')")
            else:
                it = esc_sql(txt)
                exists = _pj(f"SELECT 1 FROM known_issues WHERE issue_text = '{it}' LIMIT 1")
                if not exists:
                    _psql(f"INSERT INTO known_issues (checkpoint_id, issue_text) VALUES ({cp_id}, '{it}')")


def _write_completed_log(log_entries: list, cp_id: Optional[int]):
    for log_entry in log_entries:
        clean = clean_log_text(log_entry if isinstance(log_entry, str) else str(log_entry))
        lt = esc_sql(clean)
        exists = _pj(f"SELECT 1 FROM completed_log WHERE log_text = '{lt}' LIMIT 1")
        if not exists:
            _psql(f"INSERT INTO completed_log (checkpoint_id, log_text) VALUES ({cp_id}, '{lt}')")


def regenerate_handover_yaml():
    """Regenerate handover.yaml from DB (flat-file backup)."""
    latest = _pj("SELECT * FROM session_checkpoints ORDER BY id DESC LIMIT 1")
    if not latest:
        return
    cp = latest[0]
    cp_id = cp["id"]

    decisions = _pj(
        f"SELECT decision_id, detail, status, decision_text FROM decisions "
        f"WHERE (status IS NULL OR status != 'archived') "
        f"ORDER BY id"
    )
    issues = _pj(
        f"SELECT DISTINCT ON (issue_text) issue_text, issue_id, detail, resolved FROM known_issues "
        f"WHERE NOT resolved ORDER BY issue_text, id DESC"
    )
    completed = _pj(
        "SELECT log_text FROM ("
        "SELECT DISTINCT ON (log_text) log_text, id FROM completed_log "
        "ORDER BY log_text, id DESC"
        ") sub ORDER BY id ASC LIMIT 50"
    )

    def _fmt_dec(d):
        if d.get("decision_id"):
            return {"id": d["decision_id"], "detail": d["detail"], "status": d.get("status", "open")}
        return d["decision_text"]

    def _fmt_iss(i):
        if i.get("issue_id"):
            return {"id": i["issue_id"], "detail": i["detail"], "resolved": i["resolved"]}
        return {"text": i["issue_text"], "resolved": i["resolved"]}

    data = {
        "last_checkpoint": {
            "time": str(cp["created_at"]),
            "summary": cp.get("summary", ""),
            "total_files": cp.get("total_files", 0),
            "recent_files": cp.get("recent_files", {}),
            "git": cp.get("git_state", {}),
            "task": cp.get("task"),
        },
        "decisions": [_fmt_dec(d) for d in decisions],
        "known_issues": [_fmt_iss(i) for i in issues],
        "completed_log": [l["log_text"] for l in completed],
    }

    HANDOVER_FILE.write_text(yaml.dump(
        data, default_flow_style=False, allow_unicode=True,
        sort_keys=False, width=120))
    print(f"  Regenerated handover.yaml from DB (cp#{cp_id})")

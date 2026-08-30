#!/usr/bin/env python3
# Status: production
"""Review fetching and pattern extraction for feedback.

Queries completed reviews from activity_log and extracts
gold_standard / edge_case patterns from findings, quality notes,
verification items, and analysis results.
"""

import csv
import io
import json
import subprocess as sp
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


MAX_CONTENT_LENGTH = 300


def _fetch_review_results(since_hours: int = 48) -> List[Dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    sql = (
        f"SELECT id, type, title, body::text, model "
        f"FROM activity_log "
        f"WHERE queue_status = 'done' "
        f"  AND type IN ('review', 'verify_result', 'debate_result', 'extract_result')"
        f"  AND created_at > '{cutoff}'::timestamptz "
        f"ORDER BY created_at DESC "
        f"LIMIT 30"
    )
    r = sp.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--csv", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []

    results: List[Dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(r.stdout)):
        if not row:
            continue
        try:
            body = json.loads(row["body"]) if row.get("body") else {}
        except (json.JSONDecodeError, KeyError):
            body = {}
        results.append({
            "id": row.get("id", "").strip(),
            "type": row.get("type", "").strip(),
            "title": row.get("title", "").strip(),
            "body": body,
            "model": row.get("model", "").strip(),
        })
    return results


def _extract_findings(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    patterns: List[Dict[str, Any]] = []
    for r in results:
        body = r.get("body", {})
        if not body:
            continue

        for key in ("findings", "approved_findings"):
            findings = body.get(key, [])
            if not isinstance(findings, list):
                continue
            for f in findings:
                if not isinstance(f, dict):
                    continue
                desc = f.get("description", "") or f.get("summary", "")
                fix = f.get("fix", "") or f.get("solution", "") or f.get("diff", "")
                severity = f.get("severity", "medium")
                category = f.get("category", "general")
                if desc:
                    patterns.append({
                        "issue": desc[:MAX_CONTENT_LENGTH],
                        "fix": fix[:MAX_CONTENT_LENGTH] if fix else "Applied fix as described.",
                        "severity": severity,
                        "category": category,
                        "classification": "edge_case",
                    })

        for key in ("quality_notes", "feedback_notes"):
            notes = body.get(key, [])
            if not isinstance(notes, list):
                continue
            for note in notes:
                if isinstance(note, dict) and note.get("issue"):
                    patterns.append({
                        "issue": note["issue"][:MAX_CONTENT_LENGTH],
                        "fix": note.get("fix", "Addressed.")[:MAX_CONTENT_LENGTH],
                        "severity": "low",
                        "category": "quality",
                        "classification": "edge_case",
                    })

        if body.get("issue") and body.get("resolution"):
            patterns.append({
                "issue": body["issue"][:MAX_CONTENT_LENGTH],
                "fix": body["resolution"][:MAX_CONTENT_LENGTH],
                "severity": body.get("severity", "medium"),
                "category": body.get("category", "general"),
                "classification": "edge_case",
            })

        vi_sources = [body.get("verification_items", [])]
        verify_result = body.get("verify_result", {})
        if isinstance(verify_result, dict):
            vi_sources.append(verify_result.get("verification_items", []))
        for items in vi_sources:
            if not isinstance(items, list):
                continue
            for vi in items:
                if not isinstance(vi, dict):
                    continue
                check = vi.get("check", "") or vi.get("description", "")
                detail = vi.get("detail", "") or vi.get("explanation", "")
                result = vi.get("result", "partial")
                severity_map = {"pass": "low", "fail": "critical", "partial": "medium"}
                severity = severity_map.get(result, "medium")
                if check:
                    patterns.append({
                        "issue": check[:MAX_CONTENT_LENGTH],
                        "fix": detail[:MAX_CONTENT_LENGTH] if detail else "Verified.",
                        "severity": severity,
                        "category": "verification",
                        "classification": "gold_standard" if result == "pass" else "edge_case",
                    })

        deepseek_audit = body.get("deepseek_audit", {})
        if isinstance(deepseek_audit, dict):
            for fi in deepseek_audit.get("feedback_items", []):
                if not isinstance(fi, dict):
                    continue
                check = fi.get("check", "") or fi.get("description", "")
                reason = fi.get("audit_reason", "")
                audit_result = fi.get("audit_result", "agree")
                severity = "high" if audit_result == "disagree" else "medium"
                category = "deepseek_audit"
                if check:
                    patterns.append({
                        "issue": check[:MAX_CONTENT_LENGTH],
                        "fix": (
                            reason[:MAX_CONTENT_LENGTH]
                            if reason
                            else "Addressed in re-verification."
                        ),
                        "severity": severity,
                        "category": category,
                        "classification": "gold_standard" if audit_result == "agree" else "edge_case",
                    })

        analysis_result = body.get("analysis_result", {})
        if isinstance(analysis_result, dict):
            for fi in analysis_result.get("findings", []):
                if not isinstance(fi, dict):
                    continue
                issue = fi.get("issue", "") or fi.get("problem", "")
                fix = fi.get("corrected_fix", "")
                if issue:
                    patterns.append({
                        "issue": issue[:MAX_CONTENT_LENGTH],
                        "fix": fix[:MAX_CONTENT_LENGTH] if fix else "Addressed in corrected patterns.",
                        "severity": "high",
                        "category": "analysis_result",
                        "classification": "edge_case",
                    })
            for vp in analysis_result.get("verified_patterns", []):
                if not isinstance(vp, dict):
                    continue
                issue = vp.get("issue", "")
                fix = vp.get("fix", "")
                if issue:
                    patterns.append({
                        "issue": issue[:MAX_CONTENT_LENGTH],
                        "fix": fix[:MAX_CONTENT_LENGTH] if fix else "Confirmed standard.",
                        "severity": "low",
                        "category": "analysis_result",
                        "classification": "gold_standard",
                    })

    return patterns

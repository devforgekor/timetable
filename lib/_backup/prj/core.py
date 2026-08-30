# Status: production
import builtins
import hashlib
import json
import os
import sys

# Add scripts dir to sys.path
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from lib.common import log
from lib.db import escape_sql_string, psql_json
from lib.llm_client import resolve_model
from lib.pipeline_common import *
from lib.pod_manager import *

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

# EXPLICIT IMPORT instead of *
import lib.prj.utils as utils

# Constants
PROPOSER_MODEL = "night_proposer"
REFLECTOR_MODEL = "night_reflector"
JUDGE_MODEL = "night_judge"

PROPOSER_SYSTEM_PROMPT = """You are a code review specialist. Analyze the evaluation findings below. Identify bugs, security issues, data loss risks, and edge cases."""
REFLECTOR_SYSTEM_PROMPT = (
    """You are a reflector. Review the proposer's suggestions and provide feedback."""
)
JUDGE_SYSTEM_PROMPT = """You are a judge. Decide which suggestions are valid based on proposer and reflector inputs."""

if not hasattr(builtins, "DRY_RUN"):
    builtins.DRY_RUN = False


def call_one(model_name, sys_prompt, user_text, tag_label, max_tok=2048):
    if builtins.DRY_RUN:
        log(f"  [DRY] call_one({model_name}) → mock response")
        return {
            "P_score": 5,
            "R_score": 5,
            "consensus": 1,
            "decision": "APPROVED",
            "approved": [],
            "rejected": [],
        }
    return llm_call(model_name, sys_prompt, user_text, max_tok, tag_label)


def rubric_evaluate_findings(findings, tag):
    """Phase 2: Evaluate each finding against rubric criteria (day_verify)."""
    log("\n--- Phase 2: Rubric Evaluation (finding-level) ---")
    if not findings:
        log("  No findings to evaluate — skipping rubric evaluation")
        return []

    # Build finding text for evaluation
    finding_lines = []
    for f in findings:
        fid = f.get("fid", f.get("id", "?"))
        desc = f.get("description", "").replace("\n", " ")[:200]
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        finding_lines.append(f"  [{sev}/{cat}] {fid}: {desc}")

    user_text = "Evaluate these findings against the rubric:\n\n" + "\n".join(finding_lines[:20])
    resp = call_one("day_verify", RUBRIC_SYSTEM_PROMPT, user_text, f"rubric_{tag}", max_tok=4096)
    rubrics = (resp or {}).get("result", {}).get("rubric_evaluations", [])

    # Build a lookup for quick access
    rubric_by_id = {r["id"]: r for r in rubrics if "id" in r}
    for f in findings:
        fid = f.get("fid", f.get("id", ""))
        if fid in rubric_by_id:
            f["rubric"] = rubric_by_id[fid]

    avg_score = 0.0
    if rubrics:
        scores = [
            r.get("weighted_score", 0) for r in rubrics if r.get("weighted_score") is not None
        ]
        avg_score = sum(scores) / len(scores) if scores else 0.0

    log(f"  Evaluated {len(rubrics)} findings, avg weighted_score={avg_score:.2f}")
    return rubrics


def python_verify(data, tag):
    log("\n--- Phase 0: Python 구조 검증 ---")
    findings_list = data.get("findings", [])
    total = len(findings_list)
    issues = []

    ids = [f.get("id", f.get("fid", f"idx_{i}")) for i, f in enumerate(findings_list)]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        issues.append(
            {"check": "id_duplicates", "severity": "error", "detail": f"Duplicate IDs: {dupes}"}
        )
        log(f"  {FAIL} ID duplicates: {dupes}")
    else:
        log(f"  {PASS} All {total} IDs unique")

    REQUIRED = {"id", "severity", "category", "description"}
    missing = []
    for i, f in enumerate(findings_list):
        m = REQUIRED - set(f.keys())
        if m:
            missing.append((ids[i], m))
    if missing:
        issues.append(
            {
                "check": "missing_fields",
                "severity": "error",
                "detail": f"{len(missing)} findings missing fields: {missing}",
            }
        )
        log(f"  {FAIL} {len(missing)} findings missing required fields")
    else:
        log(f"  {PASS} All {total} findings have required fields")

    VALID_SEV = {"critical", "high", "medium", "low", "pass", "fail", "partial"}
    invalid_severity = [
        (ids[i], f.get("severity", "?"))
        for i, f in enumerate(findings_list)
        if f.get("severity", "").lower() not in VALID_SEV
    ]
    if invalid_severity:
        issues.append(
            {"check": "invalid_severity", "severity": "warn", "detail": str(invalid_severity)}
        )
        log(f"  {WARN} Invalid severities: {invalid_severity}")
    else:
        log(f"  {PASS} All severities valid")

    empty = [
        (ids[i], f.get("description", "")[:50])
        for i, f in enumerate(findings_list)
        if not f.get("description", "").strip()
    ]
    if empty:
        issues.append(
            {
                "check": "empty_description",
                "severity": "error",
                "detail": f"{len(empty)} empty descriptions",
            }
        )
        log(f"  {FAIL} {len(empty)} empty descriptions")
    else:
        log(f"  {PASS} All descriptions non-empty")

    expected = data.get("total_findings", 0)
    if expected and expected != total:
        issues.append(
            {
                "check": "count_mismatch",
                "severity": "error",
                "detail": f"meta={expected} actual={total}",
            }
        )
        log(f"  {FAIL} Count mismatch: meta={expected} actual={total}")
    else:
        log(f"  {PASS} Finding count matches metadata ({total})")

    without_source = [ids[i] for i, f in enumerate(findings_list) if not f.get("source_file")]
    if without_source:
        log(f"  {WARN} {len(without_source)} findings missing source_file")

    severity_dist = {}
    for f in findings_list:
        s = f.get("severity", "unknown").lower()
        severity_dist[s] = severity_dist.get(s, 0) + 1
    log(f"  Severity distribution: {severity_dist}")

    result = {
        "total_findings": total,
        "issues_found": len(issues),
        "issues": issues,
        "severity_distribution": severity_dist,
    }
    save(f"pyverify_{tag}", tag, result)
    log(f"  -> {len(issues)} issues, {total} findings checked")
    return result


MOCK_RESULT = {
    "result": {
        "findings": [
            {
                "id": "F001",
                "severity": "critical",
                "category": "bug",
                "description": "Mock finding for dry-run test",
                "file": "mock.py",
            },
            {
                "id": "F002",
                "severity": "high",
                "category": "security",
                "description": "Another mock finding",
                "file": "mock.py",
            },
        ],
        "rubric_evaluations": [
            {
                "id": "F001",
                "correctness": 8,
                "correctness_justification": "Real issue",
                "actionability": 7,
                "actionability_justification": "Clear fix",
                "evidence": 9,
                "evidence_justification": "Code evidence present",
                "novelty": 6,
                "novelty_justification": "Known pattern",
                "weighted_score": 7.65,
            },
            {
                "id": "F002",
                "correctness": 5,
                "correctness_justification": "Unclear",
                "actionability": 4,
                "actionability_justification": "No mitigation",
                "evidence": 6,
                "evidence_justification": "Partial evidence",
                "novelty": 3,
                "novelty_justification": "Well known",
                "weighted_score": 4.75,
            },
        ],
        "verdicts": [
            {"id": "F001", "verdict": "accept", "reason": "Valid dry-run finding"},
            {"id": "F002", "verdict": "reject", "reason": "Not reproducible in dry-run"},
        ],
        "P_score": 25,
        "R_score": 22,
        "P_rubric": {"correctness": 8, "coverage": 9, "precision": 8},
        "R_rubric": {"accuracy": 7, "efficiency": 8, "completeness": 7},
        "decision": "APPROVED",
        "consensus_score": 85,
        "approved": ["F001"],
        "rejected": ["F002"],
        "rubric_evaluation": {
            "fairness": 8,
            "fairness_justification": "Balanced scoring",
            "clarity": 7,
            "clarity_justification": "Clear report",
            "consistency": 8,
            "consistency_justification": "Consistent decisions",
        },
        "report": {
            "summary": "Mock dry-run report summary",
            "top_issues": ["F001: critical bug in mock.py"],
            "quality_notes": {"strengths": ["Good coverage"], "weaknesses": ["Limited data"]},
            "recommendation": "Commit after review",
        },
        "handoff": {
            "source": "dry_run_mock",
            "executive_summary": "Dry-run test handoff",
            "approved": [
                {
                    "id": "F001",
                    "severity": "critical",
                    "category": "bug",
                    "finding": "dry-run",
                    "approval_rationale": "test",
                }
            ],
            "rejected": [
                {
                    "id": "F002",
                    "severity": "high",
                    "category": "security",
                    "finding": "dry-run",
                    "rejection_rationale": "test",
                }
            ],
            "critical_remaining": [],
            "unresolved_count": 0,
            "verifier_priority": ["Check F001"],
            "quality_red_flags": [],
            "p_score": 25,
            "r_score": 22,
            "j_consensus": 85,
        },
        "handoff_comparison": {
            "better_handoff": "equal",
            "reason": "Both sources agree in dry-run",
            "llm_r_strengths": ["Rich descriptions"],
            "python_strengths": ["Deterministic counts"],
        },
        "final_verdict": "approved_with_conditions",
        "action": "commit",
        "confidence": 80,
        "summary": "Dry-run verification passed with conditions",
        "reasoning": "Mock reasoning for dry-run test",
        "verification_items": [
            {"check": "All findings verified", "result": "pass", "detail": "Mock verification"}
        ],
        "feedback": {
            "P": {
                "model": "night_proposer",
                "role": "proposer",
                "score": 80,
                "strengths": ["Good coverage"],
                "weaknesses": ["Needs more detail"],
                "improvements": ["Add more context"],
            },
            "R": {
                "model": "night_reflector",
                "role": "reflector",
                "score": 75,
                "strengths": ["Accurate"],
                "weaknesses": ["Brief reasoning"],
                "improvements": ["Elaborate on rejections"],
            },
            "J": {
                "model": "night_judge",
                "role": "judge",
                "score": 85,
                "strengths": ["Fair"],
                "weaknesses": ["Could be more detailed"],
                "improvements": ["Add more rationale"],
            },
        },
    },
    "usage": {"prompt_tokens": 500, "completion_tokens": 200},
    "timings": {"prompt_per_second": 10, "predicted_per_second": 5},
    "elapsed_ms": 1500,
}

PROPOSER_MODEL = "night_proposer"
REFLECTOR_MODEL = "night_reflector"
JUDGE_MODEL = "night_judge"


def call_one(model_name, sys_prompt, user_text, tag_label, max_tok=2048):
    """ensure_model -> LLM call. Day models skip restart if already healthy."""
    physical = resolve_model(model_name)
    if DRY_RUN:
        log(f"  [DRY] call_one({model_name}) → mock response")
        return MOCK_RESULT
    # Day models (reviewer/extractor) skip restart — inference stays running
    # Night models (proposer/reflector/judge/verifier) always restart for mode swap
    skip_if_healthy = physical not in NIGHT_MODELS
    ok = ensure_model(physical, skip_if_healthy=skip_if_healthy)
    if not ok:
        abort("컨테이너 시작 실패", model_name, f"{model_name} 컨테이너가 300s 내에 준비되지 않음")
    return llm_call(
        [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_text}],
        physical,
        max_tokens=max_tok,
        label=tag_label,
    )


def compile_handoff_single(r, round_num, with_rubric):
    """Python-compiled handoff from P-R-J result data."""
    n_approved = len(r.get("approved", []))
    n_rejected = len(r.get("rejected", []))
    handoff = {
        "source": "python_compiled",
        "p_model": r["p_model"],
        "r_model": r["r_model"],
        "j_model": r["j_model"],
        "round": round_num,
        "with_rubric": with_rubric,
        "P_score": r["P_score"],
        "R_score": r["R_score"],
        "consensus": r["consensus"],
        "decision": r["decision"],
        "approved_count": n_approved,
        "rejected_count": n_rejected,
        "approved_ids": sorted(r.get("approved", [])),
        "rejected_ids": sorted(r.get("rejected", [])),
        "report_summary": r.get("report_summary", ""),
        "r_rejected_findings": r.get("r_rejected_findings", []),
        "schema_version": 1,
    }
    handoff["checksum"] = hashlib.sha256(
        json.dumps(handoff, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return handoff


def compile_handoff(prj_results, round_num, with_rubric):
    """Consolidated handoff from P-R-J results."""
    first_prj_result = prj_results[0] if prj_results else {}
    handoff = {
        "source": "python_consolidated",
        "round": round_num,
        "with_rubric": with_rubric,
        "P_score": first_prj_result.get("P_score", 0),
        "R_score": first_prj_result.get("R_score", 0),
        "consensus": first_prj_result.get("consensus", 0),
        "decision": first_prj_result.get("decision", ""),
        "total_approved": len(first_prj_result.get("approved", [])),
        "total_rejected": len(first_prj_result.get("rejected", [])),
        "all_approved_ids": sorted(first_prj_result.get("approved", [])),
        "all_rejected_ids": sorted(first_prj_result.get("rejected", [])),
        "report_summary": first_prj_result.get("report_summary", ""),
        "top_issues": first_prj_result.get("report_top_issues", [])[:5],
        "schema_version": 1,
    }
    handoff["checksum"] = hashlib.sha256(
        json.dumps(handoff, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return handoff


def run_propose_review_judge(state, tag, rubric_append):
    """P-R-J 1회 패스. P → R → J → state 저장."""

    log("\n--- Night Debate — Proposer (P) ---")
    handoff_fragment = {}

    # P — gets findings by severity + P context
    p_max = 4096
    proposer_output = call_one(
        PROPOSER_MODEL,
        PROPOSER_SYSTEM_PROMPT + rubric_append,
        state.build_context("prj_proposer"),
        f"P_{tag}",
        max_tok=p_max,
    )
    save(f"p_{tag}", tag, proposer_output)
    p_findings = (proposer_output or {}).get("result", {}).get("findings", [])
    prev_count = len(p_findings)
    p_findings = utils._dedup_findings(p_findings)
    if len(p_findings) < prev_count:
        log(
            f"  Dedup: {prev_count} → {len(p_findings)} findings ({prev_count - len(p_findings)} removed)"
        )

    # R
    r_max = 2048
    reflector_output = call_one(
        REFLECTOR_MODEL,
        REFLECTOR_SYSTEM_PROMPT + rubric_append,
        f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}",
        f"R_{tag}",
        max_tok=r_max,
    )
    save(f"r_{tag}", tag, reflector_output)
    r_verdicts = (reflector_output or {}).get("result", {}).get("verdicts", [])
    r_rejected = (reflector_output or {}).get("result", {}).get("rejected_findings", [])
    if r_rejected and len(r_rejected) > 0:
        log(f"  R rejected {len(r_rejected)} findings — stored in audit trail")

    # J
    j_max = 2048
    judge_output = call_one(
        JUDGE_MODEL,
        JUDGE_SYSTEM_PROMPT + rubric_append,
        state.build_context("prj_judge", {"rotation_index": 0})
        + "\n\n### P findings:\n"
        + json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000]
        + "\n\n### R verdicts:\n"
        + json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
        f"J_{tag}",
        max_tok=j_max,
    )
    save(f"j_{tag}", tag, judge_output)

    judge_result = (judge_output or {}).get("result", {})
    j_report = judge_result.get("report", {})
    j_handoff = judge_result.get("handoff", {})
    handoff_fragment = j_handoff
    prj_result = {
        "p_model": PROPOSER_MODEL,
        "r_model": REFLECTOR_MODEL,
        "j_model": JUDGE_MODEL,
        "P_score": judge_result.get("P_score", 0),
        "R_score": judge_result.get("R_score", 0),
        "consensus": judge_result.get("consensus_score", 0),
        "decision": judge_result.get("decision", ""),
        "approved": judge_result.get("approved", []),
        "rejected": judge_result.get("rejected", []),
        "p_count": len(p_findings),
        "r_count": len(r_verdicts),
        "r_rejected_findings": r_rejected,
        "p_elapsed_ms": (proposer_output or {}).get("elapsed_ms", 0),
        "r_elapsed_ms": (reflector_output or {}).get("elapsed_ms", 0),
        "j_elapsed_ms": (judge_output or {}).get("elapsed_ms", 0),
        "report_summary": j_report.get("summary", ""),
        "report_top_issues": j_report.get("top_issues", []),
        "report_recommendation": j_report.get("recommendation", ""),
    }
    state.add_prj_rotation(prj_result)
    ps = prj_result["P_score"]
    rs = prj_result["R_score"]
    cs = prj_result["consensus"]
    slack_msg = (
        f"[P-R-J] *Round {state.round_num}*\n"
        f"P={PROPOSER_MODEL}→{ps} | R={REFLECTOR_MODEL}→{rs} | J={JUDGE_MODEL}→consensus={cs}\n"
    )
    if j_report.get("summary"):
        slack_msg += f"> {j_report['summary'][:120]}"
    slack_send(slack_msg)
    return prj_result, handoff_fragment, p_findings, r_verdicts


HANDOFF_SYSTEM_PROMPT = """You are a senior reviewer (R) writing the final handoff document after a complete P-R-J review cycle.

The full cycle is complete:
- **P (night_proposer)**: Proposed findings with severity/category
- **You (R, night_reflector)**: Reviewed each finding — accepted or rejected
- **J (night_judge)**: Final scoring and consolidated decision

Your job: synthesize ALL of the above into a comprehensive handoff for the **final verifier (night_verify)**.

Grounding rules:
- ALL finding IDs must come from the actual data below. Do NOT fabricate.
- Approved items = accepted by R AND approved by J
- Rejected items = rejected by R OR rejected by J
- "critical_remaining" = IDs of rejected findings with severity critical/high
- Include specific severity, category, and rationale for each finding

Return ONLY valid JSON — no markdown, no commentary.

Schema:
{
  "handoff": {
    "source": "R_handoff",
    "executive_summary": "1-2 sentence overview of the full P-R-J cycle including key decisions",
    "approved": [
      {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|...",
       "finding": "brief description (under 150 chars)",
       "approval_rationale": "why this was accepted by both R and J"}
    ],
    "rejected": [
      {"id": "F002", "severity": "...", "category": "...",
       "finding": "brief description",
       "rejection_rationale": "why this was rejected"}
    ],
    "critical_remaining": [],
    "unresolved_count": 0,
    "verifier_priority": [
      "specific item for verifier to double-check (with concrete reason and finding ID)"
    ],
    "quality_red_flags": ["systemic concern across multiple findings"],
    "p_score": 0-30,
    "r_score": 0-30,
    "j_consensus": 0-100,
    "rubric_evaluation": {
      "completeness": "0-10",
      "completeness_justification": "...",
      "accuracy": "0-10",
      "accuracy_justification": "...",
      "clarity": "0-10",
      "clarity_justification": "..."
    }
  }
}"""


def _get_turn(turn_id):
    """Query turns table by UUID."""
    sql = (
        "SELECT id, user_turn, thinking, text, source_message_id, "
        "  created_at, conversation_id, seq "
        f"FROM turns WHERE id = '{escape_sql_string(turn_id)}'::uuid"
    )
    rows = psql_json(sql)
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r["id"],
        "user_turn": r["user_turn"],
        "thinking": r.get("thinking") or None,
        "text": r["text"],
        "source_message_id": r.get("source_message_id", ""),
        "created_at": r["created_at"],
        "conversation_id": r["conversation_id"],
        "seq": r.get("seq", 0) or 0,
    }


def _get_facts(turn_id):
    """Query extracted facts for a turn from review_facts (non-marker)."""
    sql = (
        "SELECT fact_index, fact_type, evidence, extract_model, verdict "
        f"FROM review_facts "
        f"WHERE turn_id = '{escape_sql_string(turn_id)}'::uuid "
        f"  AND verdict != 'system' "
        "ORDER BY fact_index ASC"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    facts = []
    for row in rows:
        facts.append(
            {
                "fact_index": row.get("fact_index", 0) or 0,
                "fact_type": row.get("fact_type", ""),
                "evidence": row.get("evidence", ""),
                "extract_model": row.get("extract_model", ""),
                "verdict": row.get("verdict", ""),
            }
        )
    return facts


def _read_pending_items(limit=5):
    """Read pending extract_results from activity_log, with optional day_review reference."""
    sql = (
        "SELECT al.id, al.body, al.title, al.summary, al.created_at, "
        "  dr.body as day_review_body "
        "FROM activity_log al "
        "LEFT JOIN LATERAL ("
        "  SELECT body FROM activity_log "
        "  WHERE type='day_review' "
        "    AND body->>'turn_id' = al.body->>'turn_id' "
        "  LIMIT 1"
        ") dr ON true "
        "WHERE al.queue_status='pending' AND al.type='extract_result' "
        "ORDER BY al.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    items = []
    for row in rows:
        body = row.get("body")
        if not isinstance(body, dict):
            continue
        items.append(
            {
                "log_id": row.get("id", 0) or 0,
                "body": body,
                "title": row.get("title", ""),
                "summary": row.get("summary", ""),
                "created_at": row.get("created_at", ""),
                "day_review": row.get("day_review_body"),
            }
        )
    return items


def _build_p_context(turn, facts, body, day_review=None):
    """Build P context from turn + facts + MCP metadata + optional day_review."""
    parts = [
        "=== INPUT: turn START ===",
        f"User: {turn.get('user_turn', '')[:2000]}",
        f"Thinking: {(turn.get('thinking') or '')[:2000]}",
        f"Response: {(turn.get('text') or '')[:2000]}",
        "=== INPUT: turn END ===",
        "",
        f"=== CONTEXT: facts ({len(facts)}) START ===",
    ]
    for f in facts:
        parts.append(f"  [{f.get('fact_type', '?')}] {f.get('evidence', '')[:300]}")
    parts.append("=== CONTEXT: facts END ===")
    enrich_data = body.get("enrich") or body.get("mcp", {})
    if enrich_data:
        parts.extend(
            [
                "",
                "=== CONTEXT: enrich START ===",
                f"  tldr: {enrich_data.get('tldr', '')}",
                f"  intent: {enrich_data.get('intent', '')}",
            ]
        )
        ents = enrich_data.get("entities", {})
        if ents:
            files = ents.get("files", [])[:5]
            funcs = ents.get("functions", [])[:5]
            parts.append(
                f"  entities: files={len(ents.get('files', []))}, "
                f"funcs={len(ents.get('functions', []))}"
            )
            if files:
                for f in files:
                    parts.append(f"    file: {f}")
            if funcs:
                for f in funcs:
                    parts.append(f"    func: {f}")
        tags = enrich_data.get("tags", [])
        if tags:
            parts.append(f"  tags: {tags[:10]}")
        parts.append("=== CONTEXT: enrich END ===")

    if day_review:
        jr = day_review.get("J_results", {})
        dr_findings = day_review.get("P_results", [])
        dr_verdicts = day_review.get("R_results", [])
        parts.extend(
            [
                "",
                "=== CONTEXT: review START ===",
                "  [note: day review by day_p+day_r, may contain hallucinations]",
                f"  P_score={jr.get('P_score', '?')} R_score={jr.get('R_score', '?')}",
                f"  decision={jr.get('decision', '?')}",
                f"  approved={jr.get('approved', [])}",
                f"  rejected={jr.get('rejected', [])}",
            ]
        )
        if dr_findings:
            parts.append(f"  Day findings ({len(dr_findings)}):")
            for f in dr_findings[:5]:
                parts.append(f"    [{f.get('severity', '?')}] {f.get('description', '')[:120]}")
        if dr_verdicts:
            parts.append(f"  Day verdicts ({len(dr_verdicts)}):")
            for v in dr_verdicts[:5]:
                parts.append(f"    {v.get('id', '?')}: {v.get('verdict', '?')}")
        parts.extend(
            [
                "",
                "Perform your OWN independent review. Day results are reference only.",
                "Do NOT rely on day findings — verify everything yourself.",
                "=== CONTEXT: review END ===",
            ]
        )
    return "\n".join(parts)


def _batch_p(items, rubric_append):
    """P batch: load once, review all items."""
    log(f"\n--- P Batch Review ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock P batch")
        MOCK = [
            {
                "id": "M001",
                "severity": "medium",
                "category": "quality",
                "description": "Dry-run P finding for extract review",
                "file": "extract",
            }
        ]
        return [MOCK for _ in items]

    ok = ensure_model(PROPOSER_MODEL)
    if not ok:
        log(f"  FAILED to load {PROPOSER_MODEL}")
        return [[] for _ in items]

    results = []
    for idx, item in enumerate(items):
        turn_id = item["body"].get("turn_id", "")
        turn = _get_turn(turn_id)
        if not turn:
            log(f"  [{idx + 1}/{len(items)}] Turn not found: {turn_id[:8]}")
            results.append([])
            continue
        facts = _get_facts(turn_id)
        log(f"  [{idx + 1}/{len(items)}] {turn_id[:8]}: {len(facts)} facts")
        ctx = _build_p_context(turn, facts, item["body"], day_review=item.get("day_review"))
        resp = llm_call(
            [
                {"role": "system", "content": PROPOSER_SYSTEM_PROMPT + rubric_append},
                {"role": "user", "content": ctx},
            ],
            model=PROPOSER_MODEL,
            max_tokens=4096,
            label=f"P_queue_{idx}",
        )
        findings = resp.get("result", {}).get("findings", [])
        log(f"    P: {len(findings)} findings")
        results.append(findings)
    return results


def _batch_r(items, p_results, rubric_append):
    """R(night_reflector) batch: load once, reflect on all P findings."""
    log(f"\n--- R(night_reflector) Batch Reflection ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock R batch")
        MOCK = [{"id": "M001", "verdict": "accept", "reason": "Dry-run R verdict"}]
        return [MOCK for _ in items]

    ok = ensure_model(REFLECTOR_MODEL)
    if not ok:
        log(f"  FAILED to load {REFLECTOR_MODEL}")
        return [[] for _ in items]

    results = []
    for idx, (item, p_findings) in enumerate(zip(items, p_results)):
        if not p_findings:
            results.append([])
            continue
        ctx = f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}"
        resp = llm_call(
            [
                {"role": "system", "content": REFLECTOR_SYSTEM_PROMPT + rubric_append},
                {"role": "user", "content": ctx},
            ],
            model=REFLECTOR_MODEL,
            max_tokens=2048,
            label=f"R_queue_{idx}",
        )
        verdicts = resp.get("result", {}).get("verdicts", [])
        log(f"    R: {len(verdicts)} verdicts")
        results.append(verdicts)
    return results


def _batch_j(items, p_results, r_results, rubric_append):
    """J(night_judge) batch: load once, score all P-R pairs."""
    log(f"\n--- J(night_judge) Batch Scoring ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock J batch")
        MOCK = {
            "P_score": 25,
            "R_score": 22,
            "decision": "APPROVED",
            "consensus_score": 85,
            "approved": ["M001"],
            "rejected": [],
        }
        return [MOCK for _ in items]

    ok = ensure_model(JUDGE_MODEL)
    if not ok:
        log(f"  FAILED to load {JUDGE_MODEL}")
        return [None for _ in items]

    results = []
    for idx, (item, p_findings, r_verdicts) in enumerate(zip(items, p_results, r_results)):
        ctx_parts = [
            f"P findings ({len(p_findings)}):\n",
            json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000],
            f"\nR verdicts ({len(r_verdicts)}):\n",
            json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
        ]
        resp = llm_call(
            [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT + rubric_append},
                {"role": "user", "content": "\n".join(ctx_parts)},
            ],
            model=JUDGE_MODEL,
            max_tokens=2048,
            label=f"J_queue_{idx}",
        )
        jr = resp.get("result", {})
        log(
            f"    J: P_score={jr.get('P_score', '?')} R_score={jr.get('R_score', '?')} "
            f"decision={jr.get('decision', '?')}"
        )
        results.append(jr if jr.get("decision") in ("APPROVED", "REJECT") else None)
    return results

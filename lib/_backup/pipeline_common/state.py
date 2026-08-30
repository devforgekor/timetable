#!/usr/bin/env python3
# Status: production
"""PipelineState — append-only phase results blackboard."""

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone

from lib.common import log
from lib.token_budget import TokenBudget
from lib.pipeline_common.schema import _log_schema_warnings

PIPELINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "..", "data", "pipeline_run")
EVENTS_DIR = os.path.join(PIPELINE_DIR, "events")
os.makedirs(PIPELINE_DIR, exist_ok=True)
os.makedirs(EVENTS_DIR, exist_ok=True)


def compile_handoff_single(r, round_num, with_rubric):
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


class PipelineState:
    """Blackboard: append-only phase results in 1 JSON file."""

    def __init__(self, round_num, with_rubric, input_data, existing_data=None):
        self.round_num = round_num
        self.with_rubric = with_rubric
        self.tag = f"r{round_num}_{'rubric' if with_rubric else 'norubric'}"
        self.path = os.path.join(PIPELINE_DIR, f"pipeline_state_{self.tag}.json")
        self.events_dir_path = os.path.join(PIPELINE_DIR, "events")

        if existing_data:
            self.data = existing_data
            self.save()
            return

        findings = input_data.get("findings", [])
        sev = {}
        src_files = {}
        for f in findings:
            s = f.get("severity", "unknown").lower()
            sev[s] = sev.get(s, 0) + 1
            sf = f.get("source_file", "unknown")
            src_files[sf] = src_files.get(sf, 0) + 1

        ext_input = input_data.get("extract", {})
        extract_models = ext_input.get("models", [])

        self.data = {
            "meta": {"round": round_num, "with_rubric": with_rubric,
                     "created_at": datetime.now(timezone.utc).isoformat()},
            "extract": {"models": extract_models,
                        "total_models": len(extract_models),
                        "description": input_data.get("description", ""),
                        "total_files_merged": input_data.get("total_files_merged", 0)},
            "input": {"total_findings": len(findings),
                      "severity_distribution": sev,
                      "source_files": src_files,
                      "findings": findings},
            "python_verify": {},
            "day_verify": {},
            "rubric_evaluation": {},
            "prj": [],
            "handoffs": [],
            "final_verify": {},
        }
        self.save()

    def save(self):
        os.makedirs(PIPELINE_DIR, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _append_event(self, kind: str, value: dict, event_type: str = "phase") -> None:
        event = {
            "event_id": uuid.uuid4().hex[:12],
            "event_type": event_type,
            "ts": time.time(),
            "kind": kind,
            "value": value,
            "schema_version": 1,
        }
        ev_path = os.path.join(EVENTS_DIR, f"events_{self.tag}.jsonl")
        with open(ev_path, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def add_phase(self, key, value):
        self.data[key] = value
        self.save()
        self._append_event("phase", {"key": key, "value": value}, event_type=key)

    def add_prj_rotation(self, rotation_result):
        self.data["prj"].append(rotation_result)
        self.save()
        self._append_event("prj_rotation", rotation_result, event_type="prj_rotation")

    def build_context(self, phase, extra=None):
        budget = TokenBudget(phase)
        parts = []
        inp = self.data["input"]
        findings = inp.get("findings", [])
        sev = inp.get("severity_distribution", {})
        day_verify_data = self.data.get("day_verify")

        def _maybe_append(text: str, priority: int = 5) -> bool:
            ok = budget.add_section(text.split("\n")[0][:60], text, priority)
            if ok:
                parts.append(text)
            return ok

        rl = f"Round {self.round_num}" + (" (with rubric)" if self.with_rubric else "")
        _maybe_append(f"=== CONTEXT: pipeline ({rl}) START ===\n", priority=10)

        line = f"[INPUT] {inp['total_findings']} findings ({', '.join(f'{k}={v}' for k,v in sorted(sev.items()) if v > 0)})"
        srcs = inp.get("source_files", {})
        if srcs:
            line += f"\n  Sources: {', '.join(s.split('/')[-1]+'='+str(c) for s,c in sorted(srcs.items()))}"
        _maybe_append(line, priority=9)

        if phase not in ("day_verify", "prj_proposer"):
            ext = self.data.get("extract", {})
            ext_models = ext.get("models", [])
            if ext_models:
                ext_lines = [f"[EXTRACT] {len(ext_models)} models x 15 turns"]
                for m in ext_models:
                    fth = f"{m.get('faithfulness_rate',0)*100:.0f}%"
                    sec = m.get('elapsed_seconds', 0)
                    ext_count = m.get('total_extractions', 0)
                    ext_lines.append(f"  {m['model']}: faith={fth} ({m.get('total_faithful',0)}/{ext_count}), "
                                     f"time={sec:.0f}s ({m.get('avg_time_per_turn','?')}s/turn), ok={m.get('turns_ok',0)}/{m.get('turns_total',0)}")
                _maybe_append("\n".join(ext_lines), priority=5)

        pv = self.data.get("python_verify")
        if pv and pv.get("total_findings") and phase not in ("python_verify", "prj_proposer"):
            status = "PASS" if pv.get("issues_found", 0) == 0 else f"{pv['issues_found']} ISSUES"
            py_text = f"[PYTHON VERIFY] {status}"
            for iss in pv.get("issues", [])[:3]:
                py_text += f"\n  - {iss['check']}: {iss.get('detail','')[:100]}"
            _maybe_append(py_text, priority=7)

        if day_verify_data and day_verify_data.get("final_verdict") and phase not in ("python_verify", "day_verify", "prj_proposer"):
            v_text = f"[VERIFY] {day_verify_data['final_verdict']} (confidence={day_verify_data.get('confidence','?')})"
            for item in day_verify_data.get("verification_items", [])[:5]:
                v_text += f"\n  [{item.get('result','?')}] {item.get('check','')}"
            rsn = day_verify_data.get("reasoning", "")
            if rsn:
                v_text += f"\n  Reasoning: {rsn[:200]}"
            _maybe_append(v_text, priority=6)

        prj = self.data.get("prj", [])
        if prj and phase in ("handoff", "final_verify", "night_verify"):
            prj_text = f"[P-R-J] {len(prj)} rotations (last 2 shown):"
            for r in prj[-2:]:
                prj_text += f"\n  {r.get('rotation','?')}: P={r.get('p_model','?')}({r.get('P_score','?')}) R={r.get('r_model','?')}({r.get('R_score','?')}) J={r.get('j_model','?')} → score={r.get('consensus','?')} {r.get('decision','?')}"
            _maybe_append(prj_text, priority=7)

        if phase != "python_verify":
            sev_priority = {"critical": 9, "high": 7, "medium": 5, "low": 3, "partial": 2, "fail": 1}
            for sev_name in ("critical", "high", "medium", "low", "partial", "fail"):
                finding_list = [f for f in findings if f.get("severity", "").lower() == sev_name]
                if not finding_list:
                    continue
                sp = sev_priority.get(sev_name, 5)
                f_text = f"\n[{sev_name.upper()}] ({len(finding_list)}):"
                for f in finding_list:
                    fid = f.get("fid", f.get("id", "?"))
                    desc = f.get("description", "").replace("\n", " ")[:120]
                    f_text += f"\n  {fid}: {desc}"
                if not _maybe_append(f_text, priority=sp):
                    brief = f"\n[{sev_name.upper()}] ({len(finding_list)} total — list omitted, budget)"
                    _maybe_append(brief, priority=sp - 1)

            if phase == "prj_proposer" and day_verify_data:
                items = day_verify_data.get("verification_items", [])
                if items:
                    vi_text = f"\n[VERIFIED — do not re-review these items]"
                    for item in items:
                        vi_text += f"\n  [{item.get('result','?')}] {item.get('check','')}: {item.get('detail','')[:100]}"
                    if not _maybe_append(vi_text, priority=5):
                        brief = f"\n[VERIFIED] {len(items)} items — list omitted, budget"
                        _maybe_append(brief, priority=4)

        rub = self.data.get("rubric_evaluation", {})
        rub_evals = rub.get("evaluations", [])
        if rub_evals and phase in ("prj_proposer", "prj_judge", "final_verify"):
            rub_text = "\n[RUBRIC EVALUATION — finding-level scores]"
            low_scorers = [r for r in rub_evals if r.get("weighted_score", 10) < 5.0]
            for r in rub_evals:
                fid = r.get("id", "?")
                ws = r.get("weighted_score", 0)
                c = r.get("correctness", 0)
                a = r.get("actionability", 0)
                e = r.get("evidence", 0)
                n = r.get("novelty", 0)
                rub_text += f"\n  {fid}: weighted={ws:.1f} C={c} A={a} E={e} N={n}"
            if low_scorers:
                rub_text += f"\n  LOW SCORERS (<5.0): {len(low_scorers)} findings — prioritize review"
            _maybe_append(rub_text, priority=4)

        if phase == "prj_judge":
            ri = (extra or {}).get("rotation_index", 0)
            j_text = f"[JUDGE ROTATION {ri+1}/3]"
            if ri > 0 and len(prj) > 0:
                prev = prj[-1]
                j_text += f"\n  Previous: {prev.get('rotation','?')} consensus={prev.get('consensus','?')} decision={prev.get('decision','?')}"
                if prev.get("report_summary"):
                    j_text += f"\n  Summary: {prev['report_summary'][:150]}"
            _maybe_append(j_text, priority=8)

        if phase == "final_verify":
            _maybe_append("\n[FINAL VERIFY] 7 handoff documents below (3 LLM-R + 3 Python + 1 consolidated)", priority=7)

        log(budget.summary())
        parts.append(f"=== CONTEXT: pipeline ({rl}) END ===")
        return "\n".join(parts)

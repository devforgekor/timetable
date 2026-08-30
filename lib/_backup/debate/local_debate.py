#!/usr/bin/env python3
# Status: experimental
# Path: lib/debate/local_debate.py — imported by cli.py, orchestrator.py, cooperative_debate.py
"""LocalDebate — multi-agent debate using local inference + inference.

debate mode (v6.0, 2-person):
  inference (:8081): Qwen3-30B — Proposer + Judge + DRAG + Summary + Synthesis
  inference (:8082): Qwen2.5-Coder-7B — Refuter

review mode (v1.0, 2-person):
  inference (:8081): Qwen3-30B — Proposer + DRAG + Synthesis
  inference (:8082): Reviewer (Refuter + Judge combined)

SLOC exception (~640 lines, limit 400):
  Two debate classes (LocalDebate + LocalDebateReview) share the same file.
  round_1_to_4_dart in both classes cannot be split further without breaking cohesion.
  Proposer → Refuter → Judge form a single atomic DART cycle with shared state.
  Decision: 2026-05-28, review mode added as subclass.
"""

import json
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from lib import scoring as _sc

from .debate_data import MODELS, SESSIONS_DIR
from .debate_llm import (
    _build_messages,
    _extract_file_path,
    _poll_health,
    _read_file_content,
    call_llm,
    call_llm_json,
    check_early_exit,
    format_trend,
    write_report,
)


class LocalDebate:
    """Multi-agent debate orchestrator — 2 resident models, no switching."""

    def __init__(
        self,
        question: str,
        method: str = "drag",
        skip_drag: bool = False,
        dry_run: bool = False,
    ):
        self.question = question
        self.method = method
        self.skip_drag = skip_drag
        self.dry_run = dry_run
        self.mode = "debate"
        self.resident = True  # always-on models, no switch_file

        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.state_dir = SESSIONS_DIR / self.session_id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.current_round: int = 0
        self.consensus_scores: List[int] = []
        self.winner_map: List[Dict] = []
        self.drag_context: str = ""
        self._tunnels_open: set = set()

        # Resident model assignments — fixed ports, always-on
        self.drag_model = "qwen3-30b-a3b-local"  # inference :8081
        self.proposer_model = "qwen3-30b-a3b-local"  # inference :8081
        self.refuter_model = "qwen2.5-coder-7b"  # inference :8082
        self.judge_model = "qwen3-30b-a3b-local"  # inference :8081
        self.summary_model = "qwen3-30b-a3b-local"  # inference :8081
        self.synthesizer_model = "qwen3-30b-a3b-local"  # inference :8081

    # ── Persistence ────────────────────────────────────────────────────

    def _save_state(self, entry: Dict) -> None:
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("round", self.current_round)
        path = self.state_dir / "state.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Model ready check (resident — health poll only) ────────────────

    def switch_model(self, model_id: str) -> bool:
        """Verify resident model is healthy on its fixed port. No switch_file."""
        if self.dry_run:
            port = MODELS[model_id].get("local_port", MODELS[model_id]["port"])
            print(f"  [dry-run] health check {model_id} on :{port}")
            return True
        port = MODELS[model_id].get("local_port", MODELS[model_id]["port"])
        return _poll_health(port=port, timeout=10)  # always-on, fast check

    # ── Anonymize ──────────────────────────────────────────────────────

    def _shuffle_proposals(self, prop_a: dict, prop_b: dict) -> Tuple[dict, dict, str, str]:
        """Randomly assign Alpha/Beta labels for blind judging."""
        if random.random() < 0.5:
            labeled = [("alpha", prop_a), ("beta", prop_b)]
            mapping = {"alpha": self.proposer_model, "beta": self.refuter_model}
        else:
            labeled = [("alpha", prop_b), ("beta", prop_a)]
            mapping = {"alpha": self.refuter_model, "beta": self.proposer_model}

        self._save_state(
            {
                "type": "winner_map",
                "alpha": mapping["alpha"],
                "beta": mapping["beta"],
            }
        )
        self.winner_map.append(
            {
                "round": self.current_round,
                "alpha": mapping["alpha"],
                "beta": mapping["beta"],
            }
        )

        return (
            {"label": labeled[0][0], **labeled[0][1]},
            {"label": labeled[1][0], **labeled[1][1]},
            mapping["alpha"],
            mapping["beta"],
        )

    def _resolve_winner(self, verdict: dict) -> str:
        if not self.winner_map:
            return verdict.get("winner", "unknown")
        wm = self.winner_map[-1]
        winner_label = verdict.get("winner", "tie")
        if winner_label in ("alpha", "beta"):
            return wm[winner_label]
        return winner_label

    # ── Early exit ─────────────────────────────────────────────────────

    def _check_early_exit(self) -> Optional[str]:
        return check_early_exit(self.consensus_scores)

    # ── Scoring & Veto (delegates to lib/scoring.py) ──────────

    def _check_veto(self, verdict: dict) -> bool:
        """Server-side veto: delegates to shared lib/scoring."""
        return _sc.check_veto(verdict)

    def _should_continue_round(self, verdict: dict, round_num: int) -> bool:
        """Confidence-based early exit with logging.

        Delegates to lib/scoring.should_continue_round().
        Prints reason for gap≤5 and gap≥15 terminations.
        """
        if round_num >= 3:
            return False

        gap = _sc.compute_gap(verdict)

        if gap <= 5:
            print(f"  [judge] Gap {gap} ≤ 5 — high confidence consensus, terminating early")
            return False
        if gap >= 15:
            print(f"  [judge] Gap {gap} ≥ 15 — irreconcilable divergence, terminating early")
            return False

        threshold = _sc.THRESHOLDS.get(round_num, 7)
        return gap <= threshold

    def _inject_runtime_metrics(self, verdict: dict, round_num: int) -> dict:
        """Server-injected observability. Delegates to shared."""
        prev_gaps = (
            [getattr(self, "_last_gap")] if getattr(self, "_last_gap", None) is not None else []
        )
        return _sc.inject_runtime_metrics(verdict, round_num, prev_gaps)

    def _convergence_trend(self, current_gap: int) -> str:
        """narrowing | stable | diverging — delegates to shared."""
        prev_gaps = (
            [getattr(self, "_last_gap")] if getattr(self, "_last_gap", None) is not None else []
        )
        return _sc.convergence_trend(current_gap, prev_gaps)

    # ═══════════════════════════════════════════════════════════════════
    # Round handlers
    # ═══════════════════════════════════════════════════════════════════

    def round_0_drag(self) -> bool:
        """DRAG: Qwen7B analyzes target file and sets debate context. Returns False if skipped."""
        if self.skip_drag:
            print("\n─── Round 0 (DRAG) SKIPPED (--skip-drag) ───\n")
            self._save_state({"type": "round_skip", "reason": "--skip-drag flag"})
            if not self.switch_model(self.drag_model):
                print("  [ERROR] Drag model failed to load on inference (skip_drag path)")
                return False
            return False

        print(f"\n{'=' * 60}")
        print(f"Round 0: DRAG — Context Analysis ({self.drag_model})")
        print(f"{'=' * 60}\n")
        self._save_state({"type": "round_start", "phase": "drag"})

        if not self.switch_model(self.drag_model):
            print("  [ERROR] Drag model failed to load on inference")
            return False

        file_path = _extract_file_path(self.question)
        file_content = _read_file_content(file_path) if file_path else None
        if not file_content:
            print(f"  [WARN] Could not read file: {file_path}")
            file_content = f"# File not found: {file_path}\n# Proceeding with question only."

        print(f"  [drag] Analyzing {file_path} ({len(file_content)} chars)...")

        analysis = call_llm_json(
            "drag_lite",
            self.drag_model,
            dry_run=self.dry_run,
            question=self.question,
            file_content=file_content[-12000:],
        )
        if not analysis:
            print("  [ERROR] DRAG analysis failed")
            return False

        self.drag_context = json.dumps(analysis, indent=2)
        self._save_state(
            {
                "type": "llm_response",
                "phase": "drag_analysis",
                "model": self.drag_model,
                "output": analysis,
                "file_path": file_path,
            }
        )

        decision_points = len(analysis.get("decision_points", []))
        print(f"\n  [drag] Analysis complete: {decision_points} decision points identified")
        print("  [drag] DRAG analysis complete — Judge model will be loaded for verdict + summary")
        return True

    def round_1_to_4_dart(self) -> bool:
        """DART: Rounds 1-4 — Proposer → Refuter → Judge on inference sequentially."""
        proposer_output = None
        refuter_output = None
        last_disagreement = "N/A (first round)"
        consecutive_failures = 0

        for rnd in range(1, 4):
            self.current_round = rnd

            reason = self._check_early_exit()
            if reason:
                print(f"\n─── Early exit at Round {rnd}: {reason} ───")
                self._save_state({"type": "early_exit", "reason": reason})
                return True

            print(f"\n{'=' * 60}")
            print(f"Round {rnd}: DART Debate")
            print(f"{'=' * 60}\n")
            self._save_state({"type": "round_start", "phase": "dart"})

            history_summary = json.dumps(
                {
                    "round": rnd,
                    "prior_consensus": self.consensus_scores,
                }
            )
            drag_ctx = self.drag_context or json.dumps(
                {"note": "DRAG skipped, no pre-debate context"}
            )

            # A — Proposer
            if not self.switch_model(self.proposer_model):
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures")
                    return False
                continue
            proposer_output = call_llm_json(
                "dart_proposer",
                self.proposer_model,
                dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                refuter_last_output=json.dumps(refuter_output, indent=2)
                if refuter_output
                else "N/A (first round)",
            )
            if not proposer_output:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures")
                    return False
                continue
            self._save_state(
                {
                    "type": "llm_response",
                    "phase": "dart_proposer",
                    "model": self.proposer_model,
                    "output": proposer_output,
                }
            )

            # B — Refuter
            if not self.switch_model(self.refuter_model):
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures")
                    return False
                continue
            refuter_output = call_llm_json(
                "dart_refuter",
                self.refuter_model,
                dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                proposer_last_output=json.dumps(proposer_output, indent=2),
            )
            if not refuter_output:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures")
                    return False
                continue
            self._save_state(
                {
                    "type": "llm_response",
                    "phase": "dart_refuter",
                    "model": self.refuter_model,
                    "output": refuter_output,
                }
            )

            # C — Judge
            if not self.switch_model(self.judge_model):
                print("  [ERROR] judge model switch failed")
                continue
            alpha, beta, _, _ = self._shuffle_proposals(proposer_output, refuter_output)
            judge_output = call_llm_json(
                "dart_judge",
                self.judge_model,
                dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                proposal_a_anonymized=json.dumps(alpha, indent=2),
                proposal_b_anonymized=json.dumps(beta, indent=2),
            )
            if not judge_output:
                print("  [ERROR] judge failed")
                continue

            winner = self._resolve_winner(judge_output)
            score = judge_output.get("consensus_score", 0)
            self.consensus_scores.append(score)
            last_disagreement = judge_output.get(
                "disagreement_analysis", "no specific disagreements"
            )

            # ── : server-side veto + gap + runtime_metrics ──────────
            is_veto = self._check_veto(judge_output)
            p_score = judge_output.get("P_score", 0)
            r_score = judge_output.get("R_score", 0)
            gap = abs(p_score - r_score)
            runtime_metrics = self._inject_runtime_metrics(judge_output, self.current_round)
            if is_veto:
                print(
                    f"  [judge] VETO triggered: P={p_score} R={r_score} decision={judge_output.get('decision')}"
                )
            print(
                f"  [judge] Gap: {gap} (P={p_score}, R={r_score}) | "
                f"Threshold: {_sc.THRESHOLDS.get(self.current_round)}"
            )

            consecutive_failures = 0
            self._save_state(
                {
                    "type": "judge_verdict",
                    "consensus_score": score,
                    "winner_label": judge_output.get("winner"),
                    "winner_model": winner,
                    "output": judge_output,
                    "is_veto": is_veto,
                    "gap": gap,
                    "runtime_metrics": runtime_metrics,
                }
            )

            print(
                f"  Consensus: {score}% | Winner: {winner} | "
                f"Trend: {format_trend(self.consensus_scores)}"
            )

            # ── early termination ─────────────────────────────────
            if is_veto:
                print("  [judge] Veto upheld — terminating debate")
                return True
            if not self._should_continue_round(judge_output, self.current_round):
                print(
                    f"  [judge] Terminating — gap {gap} exceeds round {self.current_round} threshold"
                )
                return True

    def round_5_synthesis(self) -> Optional[dict]:
        """Synthesis: summary + final code on inference."""
        print(f"\n{'=' * 60}")
        print(
            f"Round 5: Synthesis ({self.summary_model} summary + {self.synthesizer_model} synthesis)"
        )
        print(f"{'=' * 60}\n")
        self.current_round = 5
        self._save_state({"type": "round_start", "phase": "synthesis"})

        state_path = self.state_dir / "state.jsonl"
        full_history = state_path.read_text() if state_path.exists() else ""
        consensus_trend = format_trend(self.consensus_scores)

        # Step 1: Summary
        print(f"  [summary] {self.summary_model} writing debate summary...")
        if not self.switch_model(self.summary_model):
            print("  [ERROR] Summary model switch failed")
            return None
        summary_raw = call_llm(
            _build_messages(
                "history_summary",
                self.summary_model,
                question=self.question,
                full_history=full_history[-8000:],
                consensus_trend=consensus_trend,
            ),
            self.summary_model,
            dry_run=self.dry_run,
        )
        if summary_raw:
            if len(summary_raw) > 3000:
                history_summary = summary_raw[:1798] + "\n...\n" + summary_raw[-1197:]
            else:
                history_summary = summary_raw
        else:
            history_summary = full_history[-3000:]
        self._save_state(
            {"type": "history_summary", "model": self.summary_model, "content": history_summary}
        )

        # Post-summary hook (CooperativeDebate closes Judge/Gemma tunnel here)
        self._post_summary_hook()

        # Step 2: Final synthesis (Qwen3-30B, already resident on inference :8081)
        if not self.switch_model(self.synthesizer_model):
            print("  [ERROR] Synthesizer health check failed")
            return None

        drag_ctx = self.drag_context or json.dumps({"note": "no DRAG context"})
        final = call_llm_json(
            "final_synthesis",
            self.synthesizer_model,
            dry_run=self.dry_run,
            question=self.question,
            history_summary=history_summary,
            consensus_trend=consensus_trend,
            drag_context=drag_ctx,
        )
        if not final:
            cfg = MODELS.get(self.synthesizer_model, {})
            print(
                f"  [ERROR] synthesis failed (max_tokens={cfg.get('max_tokens', '?')}, "
                f"bench_toks={cfg.get('bench_toks', '?')})"
            )
            return None

        self._save_state(
            {"type": "final_synthesis", "model": self.synthesizer_model, "output": final}
        )

        print(f"\n  [done] Final synthesis complete: confidence={final.get('confidence', 'N/A')}")
        return final

    # ── Extension hooks (overridden by CooperativeDebate) ──────────────

    def _pre_dart_hook(self) -> bool:
        """Called after DRAG, before DART rounds. Return False to abort."""
        return True

    def _post_dart_hook(self) -> None:
        """Called after DART rounds, before synthesis."""

    def _post_summary_hook(self) -> None:
        """Called after summary, before final synthesis."""

    def _cleanup_hook(self) -> None:
        """Called after synthesis for cleanup (tunnels, spot VMs)."""

    def _enqueue_for_review(self, final: dict) -> None:
        """Enqueue debate result to activity_log for night batch review (14B→27B)."""
        try:
            from lib.queue_writer import enqueue_review

            enqueue_review(
                entry_type="debate_result",
                source="local_debate",
                title=f"debate: {self.question[:80]}",
                summary=f"consensus={self.consensus_scores[-1] if self.consensus_scores else '?'}%, "
                f"confidence={final.get('confidence', '?')}, "
                f"rounds={len(self.consensus_scores)}",
                body={
                    "session_id": self.session_id,
                    "question": self.question[:200],
                    "method": self.method,
                    "mode": self.mode,
                    "rounds": len(self.consensus_scores),
                    "consensus_scores": self.consensus_scores,
                    "consensus_trend": format_trend(self.consensus_scores),
                    "final_diff": final.get("diff", "")[:5000],
                    "decision_summary": final.get("decision_summary", "")[:1000],
                    "security_notes": final.get("security_notes", []),
                    "performance_notes": final.get("performance_notes", []),
                    "action": final.get("action", "escalate"),
                    "confidence": final.get("confidence"),
                },
                model=self.synthesizer_model,
                tags=["debate", self.mode],
            )
        except Exception as e:
            print(f"  [queue] Failed to enqueue debate result: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # Main loop
    # ═══════════════════════════════════════════════════════════════════

    def _print_header(self) -> None:
        print(f"\n{'█' * 60}")
        print(f"█ DevForge Multi-Agent LLM Debate v6.0 ({self.mode}, resident)")
        print(f"█ Session: {self.session_id}")
        print(f"█ Method: {self.method} | Dry-run: {self.dry_run}")
        print("█ Inference (:8081): Qwen3-30B — Proposer + Judge + DRAG + Synthesis")
        print("█ Inference (:8082): Qwen2.5-Coder-7B — Refuter")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█' * 60}")

    def run_session(self) -> Optional[dict]:
        self._print_header()

        self._save_state(
            {
                "type": "session_start",
                "question": self.question,
                "method": self.method,
                "skip_drag": self.skip_drag,
                "mode": self.mode,
            }
        )

        # Ensure inference container is running (Qwen3-30B Judge/DRAG/Summary/Synthesis)
        if not self.dry_run:
            from lib.pod_manager.container import _podman_start_inference

            _podman_start_inference()
            print("  [pod] inference start requested (Qwen3-30B :8081)")
            # Brief wait for container init, then health check
            time.sleep(5)
            if not _poll_health(port=8081, timeout=30):
                print("  [WARN] inference :8081 health check failed — continuing anyway")

        # Round 0: DRAG
        self.current_round = 0
        if not self.round_0_drag() and not self.skip_drag:
            print("\n[ABORT] DRAG analysis failed — cannot proceed without context")
            return None

        # Pre-DART hook (spot VM provisioning in cooperative mode)
        if not self._pre_dart_hook():
            print("\n[ABORT] Pre-DART hook failed")
            return None

        # Rounds 1-4: DART
        dart_ok = self.round_1_to_4_dart()

        # Post-DART hook (spot VM termination in cooperative mode)
        self._post_dart_hook()

        # Round 5: Synthesis
        if not dart_ok:
            print("\n[DART aborted — skipping synthesis]")
        final = self.round_5_synthesis() if dart_ok else None

        # Cleanup hook (close tunnels)
        self._cleanup_hook()

        # Write report + upload
        if final:
            report_path = write_report(
                self.state_dir,
                self.session_id,
                self.question,
                self.method,
                self.consensus_scores,
                final,
            )
            print(f"\n{'█' * 60}")
            print("█ DEBATE COMPLETE")
            print(f"█ Session: {self.session_id}")
            print(f"█ Confidence: {final.get('confidence', '?')}")
            print(f"█ Local: {report_path}")

            # Enqueue for night batch review (14B → 27B)
            self._enqueue_for_review(final)

            try:
                from lib.blob_uploader import upload_review_bundle

                url = upload_review_bundle(
                    content=report_path.read_text(),
                    pipeline="debate_v3",
                    session_id=self.session_id,
                    metadata={
                        "question": self.question[:100],
                        "method": self.method,
                        "rounds": str(len(self.consensus_scores)),
                        "consensus_trend": format_trend(self.consensus_scores),
                        "confidence": str(final.get("confidence", "?")),
                    },
                )
                print(f"█ Review: {url}")
            except Exception as e:
                print(f"█ Upload skipped: {e}")

            print(f"{'█' * 60}")

        return final


class LocalDebateReview(LocalDebate):
    """2-person debate: Proposer (Qwen3-30B) ↔ Reviewer (Qwen7B).

    Review mode — both models always-on, no switching. The Reviewer combines
    Refuter + Judge roles in a single call, eliminating the separate Judge step.
    """

    def __init__(
        self,
        question: str,
        method: str = "drag",
        skip_drag: bool = False,
        dry_run: bool = False,
    ):
        super().__init__(question=question, method=method, skip_drag=skip_drag, dry_run=dry_run)
        self.mode = "review"

        # 2-person model assignments — inference :8081 + :8082
        self.proposer_model = "qwen3-30b-a3b-local"  # inference :8081 — Proposer + DRAG + Synthesis
        self.reviewer_model = "qwen2.5-coder-7b"  # inference :8082 — Reviewer (Refuter + Judge)

        # Synthesis/Summary still on inference
        self.drag_model = "qwen3-30b-a3b-local"
        self.summary_model = "qwen3-30b-a3b-local"
        self.synthesizer_model = "qwen3-30b-a3b-local"

    def _print_header(self) -> None:
        print(f"\n{'█' * 60}")
        print(f"█ DevForge 2-Person Debate v1.0 ({self.mode})")
        print(f"█ Session: {self.session_id}")
        print(f"█ Method: {self.method} | Dry-run: {self.dry_run}")
        print("█ inference (:8081): Qwen3-30B — Proposer + DRAG + Synthesis")
        print("█ inference (:8081): Reviewer (Refuter + Judge combined)")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█' * 60}")

    def round_1_to_4_dart(self) -> bool:
        """2-person DART: Proposer → Reviewer (refutes + scores in single call)."""
        proposer_output = None
        reviewer_output = None
        last_disagreement = "N/A (first round)"
        consecutive_failures = 0

        for rnd in range(1, 4):
            self.current_round = rnd

            reason = self._check_early_exit()
            if reason:
                print(f"\n─── Early exit at Round {rnd}: {reason} ───")
                self._save_state({"type": "early_exit", "reason": reason})
                return True

            print(f"\n{'=' * 60}")
            print(f"Round {rnd}: 2-Person DART (Proposer → Reviewer)")
            print(f"{'=' * 60}\n")
            self._save_state({"type": "round_start", "phase": "dart"})

            history_summary = json.dumps(
                {
                    "round": rnd,
                    "prior_consensus": self.consensus_scores,
                }
            )
            drag_ctx = self.drag_context or json.dumps(
                {"note": "DRAG skipped, no pre-debate context"}
            )

            # A — Proposer (inference :8081)
            if not self.switch_model(self.proposer_model):
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures")
                    return False
                continue
            proposer_output = call_llm_json(
                "dart_proposer",
                self.proposer_model,
                dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                refuter_last_output=json.dumps(reviewer_output, indent=2)
                if reviewer_output
                else "N/A (first round)",
            )
            if not proposer_output:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures")
                    return False
                continue
            self._save_state(
                {
                    "type": "llm_response",
                    "phase": "dart_proposer",
                    "model": self.proposer_model,
                    "output": proposer_output,
                }
            )

            # B — Reviewer (inference :8081) — refutes + judges in single call
            if not self.switch_model(self.reviewer_model):
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures")
                    return False
                continue
            reviewer_output = call_llm_json(
                "dart_reviewer",
                self.reviewer_model,
                dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                proposer_last_output=json.dumps(proposer_output, indent=2),
            )
            if not reviewer_output:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures")
                    return False
                continue

            score = reviewer_output.get("consensus_score", 0)
            winner = reviewer_output.get("winner", "tie")
            self.consensus_scores.append(score)
            last_disagreement = reviewer_output.get(
                "disagreement_analysis", "no specific disagreements"
            )

            consecutive_failures = 0
            self._save_state(
                {
                    "type": "reviewer_verdict",
                    "consensus_score": score,
                    "winner": winner,
                    "output": reviewer_output,
                }
            )

            print(
                f"  Consensus: {score}% | Winner: {winner} | "
                f"Trend: {format_trend(self.consensus_scores)}"
            )

        return True

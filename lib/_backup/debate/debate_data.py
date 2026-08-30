#!/usr/bin/env python3
# Status: experimental
# Path: lib/debate/debate_data.py — imported by cooperative_debate.py, local_debate.py, debate_llm.py, cooperative_remote.py
"""Model catalogue, remote hosts, prompt templates — pure data, no logic."""
from pathlib import Path
from typing import Any, Dict

# ── Paths ──────────────────────────────────────────────────────────────────
SWITCH_FILE = "/opt/ai_data/debate/switch/model-switch.json"
SESSIONS_DIR = Path("/opt/ai_data/debate_sessions")

# ── Model catalogue (MoE lineup) ──────────────────────────────────────────
MODELS: Dict[str, Dict[str, Any]] = {
    "qwen3-30b-a3b": {
        "filename": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "host": "azureqwen", "port": 400, "local_port": 8086,
        "model_name": "qwen",
        "ctx": 4096, "threads": 2, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 10, "bench_toks": 6.0,
        "cache_ram": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    "nemotron3-nano-30b": {
        "filename": "Nemotron-3-Nano-30B-A3B-IQ4_XS.gguf",
        "host": "azurenemo", "port": 400, "local_port": 8085,
        "model_name": "nemotron",
        "ctx": 4096, "threads": 2, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.6, "top_p": 0.95,
        "system_prompt_support": True,
        "bench_load_s": 10, "bench_toks": 5.5,
        "cache_ram": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    "gemma-4-26b": {
        "filename": "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
        "host": "azuregemma", "port": 8080, "local_port": 8087,
        "model_name": "gemma",
        "ctx": 4096, "threads": 2, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1, "top_p": 0.9,
        "system_prompt_support": True,
        "bench_load_s": 10, "bench_toks": 10.0,
        "cache_ram": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    "qwen-14b": {
        "filename": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 1024,
        "bench_load_s": 240, "bench_toks": 3.0,
    },
    "qwen3-30b-a3b-local": {
        "filename": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        "port": 8080, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 1024,
        "bench_load_s": 70, "bench_toks": 10.5,
    },
    "qwen2.5-coder-7b": {
        "filename": "Qwen2.5-Coder-7B-Instruct-Q8_0.gguf",
        "port": 8082, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 512,
        "bench_load_s": 30, "bench_toks": 18.0,
    },
    "qwen2.5-coder-7b": {
        "filename": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        "port": 8081, "ctx": 8192, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 1024,
        "bench_load_s": 45, "bench_toks": 12.0,
    },
}

# ── Remote host catalogue ─────────────────────────────────────────────────
REMOTE_HOSTS: Dict[str, Dict[str, str]] = {
    "azureqwen": {
        "ssh_host": "azureqwen",
        "model_dir": "/models",
        "description": "Azure spot VM — Qwen3-30B-A3B, 41GB RAM, 2-core x86_64",
    },
    "azurenemo": {
        "ssh_host": "azurenemo",
        "model_dir": "/models",
        "description": "Azure spot VM — Nemotron-3-Nano-30B-A3B MoE, 41GB RAM, 2-core x86_64",
    },
    "azuregemma": {
        "ssh_host": "azuregemma",
        "model_dir": "/opt/models",
        "description": "Azure VM — Gemma 4 26B-A4B MoE, 31GB RAM, 4-core x86_64",
    },
}

# ── Prompt templates ───────────────────────────────────────────────────────

PROMPTS = {
    # ── Lite DRAG (Level 1) ──
    "drag_lite": {
        "system": (
            "Extract only the essential context for a code modification task. "
            "Keep output under 500 characters. Be precise."
        ),
        "user": (
            "Task: {question}\n\n"
            "Target file content:\n```python\n{file_content}\n```\n\n"
            "Output STRICT JSON:\n"
            '{{"target_file": "...", '
            '"key_functions": ["..."], '
            '"change_location": "...", '
            '"constraints": ["..."]}}'
        ),
    },

    # ── DART Proposer ──
    "dart_proposer": {
        "system": (
            "You are a solution PROPOSER in a code debate. Your role:\n"
            "1. Build on your previous arguments using the debate context.\n"
            "2. Clearly state what you AGREE and DISAGREE with in the refuter's last response.\n"
            "3. Strengthen weak points. Abandon positions that evidence contradicts.\n"
            "4. Ground your proposal in the DRAG analysis framework."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context (from pre-debate analysis):\n{drag_context}\n\n"
            "Round History:\n{history_summary}\n\n"
            "Judge's last assessment: consensus={consensus_score}%\n"
            "Disagreements: {disagreement_points}\n\n"
            "Refuter's last argument:\n{refuter_last_output}\n\n"
            "Your task: Respond with a strengthened proposal.\n"
            "Output STRICT JSON (no extra text):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"]}}'
        ),
    },

    # ── DART Refuter ──
    "dart_refuter": {
        "system": (
            "You are a CRITICAL REFUTER in a code debate. Find weaknesses, "
            "propose alternatives, and challenge assumptions. Be constructive — "
            "every critique must come with an alternative suggestion."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context (from pre-debate analysis):\n{drag_context}\n\n"
            "Round History:\n{history_summary}\n\n"
            "Judge's last assessment: consensus={consensus_score}%\n"
            "Disagreements: {disagreement_points}\n\n"
            "Proposer's latest argument:\n{proposer_last_output}\n\n"
            "Your task:\n"
            "1. Identify logical flaws, missing edge cases, or performance issues.\n"
            "2. Propose a concrete alternative for each weakness found.\n\n"
            "Output STRICT JSON (no extra text, no markdown wrapper):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"]}}'
        ),
    },

    # ── DART Judge (v7.2 rubric) ──
    "dart_judge": {
        "system": (
            "You are a 3-person jury panel for a code debate:\n"
            "- Juror 1: Security expert\n"
            "- Juror 2: Performance optimization expert\n"
            "- Juror 3: Code readability/maintainability expert\n\n"
            "Scoring Rubric (DISCRETE INTEGER per criterion, max 10 each):\n"
            "  0  = Fundamentally wrong / dangerous. Reject outright.\n"
            "  4  = Has merit but contains significant flaws.\n"
            "  7  = Mostly correct, minor issues only.\n"
            "  10 = Production-ready. No issues found.\n\n"
            "Rules:\n"
            "- You see two ANONYMIZED proposals (Draft Alpha, Draft Beta). Order is random.\n"
            "- Score each draft on 3 criteria: Correctness, Efficiency, Security.\n"
            "- IGNORE: response length, comment style, politeness, formatting verbosity.\n"
            "- JUDGE ONLY: logical correctness, code integrity, factual accuracy.\n"
            "- If both scores are close (gap <= 10), state what information would resolve the dispute.\n"
            "- Produce 2-4 verification_items capturing the most critical checks performed "
            "(input validation, edge case handling, security boundary, performance bottleneck).\n"
            "- Set action: commit=fully approved, revert=rejected with no salvage, "
            "escalate=needs human review (hallucination suspected or edge case unclear)."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context:\n{drag_context}\n\n"
            "Draft Alpha:\n{proposal_a_anonymized}\n\n"
            "Draft Beta:\n{proposal_b_anonymized}\n\n"
            "Score each draft on 3 criteria (Correctness, Efficiency, Security).\n"
            "Each criterion: 0 | 4 | 7 | 10 (see rubric).\n"
            "P_score = sum(Alpha's 3 criteria). R_score = sum(Beta's 3 criteria).\n\n"
            "Output STRICT JSON:\n"
            '{{'
            '"P_score": 0-30, '
            '"R_score": 0-30, '
            '"rubric_evaluation": {{'
            '  "alpha": {{"correctness": 0-10, "efficiency": 0-10, "security": 0-10}},'
            '  "beta": {{"correctness": 0-10, "efficiency": 0-10, "security": 0-10}}'
            '}}, '
            '"decision": "APPROVED|REJECT", '
            '"action": "commit|revert|escalate", '
            '"winner": "alpha|beta|tie", '
            '"hallucination_flag": true|false, '
            '"verification_items": ['
            '  {{"check": "description of check", "result": "pass|fail|partial", "detail": "explanation"}}'
            '], '
            '"jury_opinions": {{"security": "...", "performance": "...", "readability": "..."}}, '
            '"consensus_score": 0-100, '
            '"disagreement_analysis": "1-sentence summary of key unresolved issues", '
            '"machine_summary": {{'
            '  "decision": "APPROVED|REJECT", '
            '  "scores": {{"P": 0-30, "R": 0-30}}, '
            '  "gap": 0-30, '
            '  "veto_triggered": true|false, '
            '  "critical_findings": ["..."], '
            '  "verification_focus": "what to check next round", '
            '  "next_state": "completed|round3_pending"'
            '}}'
            '}}'
        ),
    },

    # ── Synthesis: History Summary ──
    "history_summary": {
        "system": (
            "You are a debate historian. Condense a multi-round code debate into "
            "a structured summary suitable for the final synthesizer."
        ),
        "user": (
            "Task: {question}\n\n"
            "Full Debate History:\n{full_history}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "Summarize the debate. Include:\n"
            "1. Key arguments from Proposer and Refuter\n"
            "2. Points that were resolved vs. still disputed\n"
            "3. Recommended approach for the final synthesizer\n\n"
            "Output as plain text (no JSON, no markdown). Max 500 words."
        ),
    },

    # ── DART Reviewer (2-person debate: combined Refuter + Judge) ──
    "dart_reviewer": {
        "system": (
            "You are a CODE REVIEWER in a 2-person debate. You serve as both "
            "Refuter AND Judge simultaneously. Your responsibilities:\n"
            "1. REFUTE: Identify logical flaws, edge cases, performance issues, "
            "or security concerns in the Proposer's solution.\n"
            "2. JUDGE: Score the debate consensus (0-100) and declare a winner.\n\n"
            "Rules:\n"
            "- Every critique must include a concrete alternative.\n"
            "- Build on prior rounds — acknowledge when the Proposer has addressed "
            "your previous feedback.\n"
            "- Score reflects overall alignment: 100 = perfect agreement, "
            "0 = fundamental disagreement.\n"
            "- If score < 70, explain what specific information would resolve the dispute.\n"
            "- Produce 2-3 verification_items for the most critical checks performed.\n"
            "- Set action: commit=approved, revert=rejected, escalate=needs human review."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context (from pre-debate analysis):\n{drag_context}\n\n"
            "Round History:\n{history_summary}\n\n"
            "Prior consensus: {consensus_score}%\n"
            "Prior disagreements: {disagreement_points}\n\n"
            "Proposer's latest argument:\n{proposer_last_output}\n\n"
            "Your task:\n"
            "1. Critique the proposal — find weaknesses, missing edge cases, "
            "performance issues.\n"
            "2. Propose concrete alternatives for each weakness found.\n"
            "3. Provide a consensus score (0-100) reflecting alignment.\n"
            "4. Declare a winner: \"reviewer\" if the proposal needs significant "
            "revision, \"proposer\" if the proposal is sound, \"tie\" if balanced.\n\n"
            "Output STRICT JSON (no extra text, no markdown wrapper):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"], '
            '"verification_items": ['
            '  {{"check": "description of check", "result": "pass|fail|partial", "detail": "explanation"}}'
            '], '
            '"consensus_score": 0-100, '
            '"winner": "proposer|reviewer|tie", '
            '"action": "commit|revert|escalate", '
            '"disagreement_analysis": "1-sentence summary of key unresolved issues"}}'
        ),
    },

    # ── Synthesis: Final ──
    "final_synthesis": {
        "system": (
            "You are the final synthesizer for a multi-agent code debate. "
            "You have access to the complete debate record and the debate summary. "
            "Your job: produce the definitive, executable final code modification.\n\n"
            "Set action: commit=diff is safe and ready to apply, "
            "revert=proposal should be discarded, "
            "escalate=needs human review before applying."
        ),
        "user": (
            "Task: {question}\n\n"
            "Debate Summary:\n{history_summary}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "DRAG Context:\n{drag_context}\n\n"
            "Produce the final answer:\n"
            "1. Executable final code diff (complete, unified diff format).\n"
            "2. Decision summary — why this solution was chosen.\n"
            "3. Security concerns note.\n"
            "4. Performance considerations note.\n\n"
            "Output STRICT JSON:\n"
            '{{"diff": "--- a/file\\n+++ b/file\\n@@ ...", '
            '"decision_summary": "3-5 sentence reasoning", '
            '"security_notes": ["note1", "note2"], '
            '"performance_notes": ["note1", "note2"], '
            '"action": "commit|revert|escalate", '
            '"confidence": 0-100}}'
        ),
    },
}


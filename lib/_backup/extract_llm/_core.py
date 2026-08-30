# Status: production
# Path: imported by — lib/extract_llm (extraction subpackage)
"""Core extraction functions: constants, prompts, 8082 recovery, embed management, _extract_edcr_freeform."""

import json
import math
import os
import subprocess as _sp
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Dict, Generator, List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys

sys.path.insert(0, SCRIPTS_DIR)

from lib.llm_client import call_llm
from lib.model_registry import MODEL_METADATA
from lib.pod_manager import ensure_model as _ensure_model_pod
from lib.watchdog.messenger import heartbeat

from .chunking import _split_atomic
from .edc import _fix_status_hallucination, _normalize_freeform_pipeline
from .parser import _parse_json

# ── 8082 Auto-Recovery ──────────────────────────────────────────

_8082_RECOVERY_LOCK = threading.Lock()

_CONNECTION_ERROR_SUBSTRINGS = (
    "Remote end closed",
    "Connection reset",
    "Connection refused",
    "Broken pipe",
    "RemoteDisconnected",
)


def _is_8082_connection_error(e: Exception) -> bool:
    err = str(e)
    if "8082" not in err and "extractor" not in err:
        return False
    return any(s in err for s in _CONNECTION_ERROR_SUBSTRINGS)


def _recover_8082() -> None:
    if not _8082_RECOVERY_LOCK.acquire(blocking=False):
        print("  [recovery] Another recovery in progress, waiting...", flush=True)
        _8082_RECOVERY_LOCK.acquire(blocking=True)
        print("  [recovery] Recovery finished by other thread", flush=True)
        _8082_RECOVERY_LOCK.release()
        return
    try:
        print("  [recovery] Reloading 8082...", flush=True)
        _ensure_model_pod("day-extractor", skip_if_healthy=False)
        print("  [recovery] 8082 ready", flush=True)
    except Exception as recover_err:
        print(f"  [recovery] 8082 reload failed: {recover_err}", flush=True)
    finally:
        _8082_RECOVERY_LOCK.release()


def _call_with_8082_retry(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if _is_8082_connection_error(e):
            print(f"  [recovery] 8082 error: {type(e).__name__}", flush=True)
            _recover_8082()
            return fn(*args, **kwargs)
        raise


# ── Constants ────────────────────────────────────────────────────

TIMEOUT_EXTRACT = 900
MAX_TOKENS_BASE = 512  # 8B Q8 generates concise JSON; 30B MoE needed 768
TOKENS_PER_300CH = 50  # 300ch당 약 1 fact 추가, ceil 적용
TEMP_EXTRACT = 0.0
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.2
TIMEOUT_PER_TOK = 1.2  # ~0.83 tok/s decode (20% safety margin)
GEN_TIME_BUF = 90  # spike/GC/swap buffer
CAP = 1800  # hard cap (절대 초과 금지)
MIN_USEFUL_TOKENS = 256  # 이 미만이면 명시적 거절
MAX_CHARS_SOLO = 5000
MAX_INPUT_CHARS_ADVERTISED = 6500  # API 에러 메시지용 광고 한계 (실제 6,714 여유)

_SIGTERM_RECEIVED = threading.Event()


def _sigterm_handler(signum, frame):
    try:
        pid = os.getpid()
        chain = []
        for _ in range(5):
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("Name:"):
                            chain.append(line.split(":", 1)[1].strip())
                        elif line.startswith("PPid:"):
                            pid = int(line.split(":", 1)[1].strip())
                            break
            except (OSError, ValueError):
                break
        print(f"\n  [SIGTERM] from parent chain: {' > '.join(chain)}", flush=True)
    except Exception:
        print("\n  [SIGTERM] (source chain unavailable)", flush=True)
    _SIGTERM_RECEIVED.set()


# ── Optimized prompts ────────────────────────────────────────
# _SYSTEM_*_EXTRACT_FREE → 4B (simple, reduced rules)
# _SYSTEM_*_EXTRACT_FREE_8B → 8B (more specific, richer examples)

_SYSTEM_USER_EXTRACT_FREE = """\
Extract factual triples from the USER MESSAGE. Each fact: (subject, predicate=snake_case, object).

RULES:
1. Max 4 facts. Fewer clean facts > many noisy ones.
2. Predicate is snake_case (2-5 words). NO: empty, stative verbs like "has"/"is".
   YES: "deploys_on_port", "requires_version", "configures_timeout_to".
3. Object = extracted value. NOT a raw copy of evidence (anti-tautology).
4. Evidence = direct quote ending with period.
5. Self-contained: resolve pronouns.
6. Skip: flow markers, greetings, speculation, reasoning steps.

Output: {"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"...","predicate":"snake_case","object":"...","source_context":"..."}]}

Empty: {"extractions":[]}. Noise/gibberish: {"skip_verdict":"skip"}."""

_SYSTEM_TEXT_EXTRACT_FREE = """\
Extract factual triples from the ASSISTANT RESPONSE. Each fact: (subject, predicate=snake_case, object).

RULES:
1. Max 4 facts. Fewer clean facts > many noisy ones.
2. Predicate is snake_case (2-5 words). NO: empty, stative verbs like "has"/"is".
   YES: "deploys_on_port", "increases_to", "writes_log_to".
3. Object = extracted value. NOT a raw copy of evidence.
4. Evidence = direct quote from source ending with period.
5. Self-contained: resolve pronouns.
6. Skip: reasoning steps, speculation, flow markers.

Output: {"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"...","predicate":"snake_case","object":"...","source_context":"..."}]}

Empty: {"extractions":[]}."""

# ── 8B-specific prompts ──────────────────────────────────────
# 8B has higher capacity — use richer guidance for quality.
# Production (day-extractor) uses 8B Q8 → SYSTEM_DAY_EXTRACT uses these.

_SYSTEM_USER_EXTRACT_FREE_8B = """\
You are a system architect reviewing a message. Extract all concrete, explicitly stated facts about the infrastructure described. The message may be in Korean or English. Look for facts about: system status, model assignments, resource consumption (RAM, disk), performance metrics, configuration settings, and dependencies. File paths (e.g. /opt/ai_data, /mnt/lv_db) and mount points are valid subjects — extract their size and purpose.

Extract each distinct entity independently. Names that differ by a single character (e.g. "Pod A" vs "Pod B", "v2" vs "v3") are DIFFERENT entities. Do NOT merge or confuse them. Verify every attribute belongs to its correct entity. A factual claim about the current state of an entity remains valid even if the speaker also mentions future plans nearby.

When a sentence gives multiple attributes of the same entity (e.g. "22Gi total RAM with 16Gi available"), extract ALL attributes. When a sentence covers multiple entities (e.g. "Pod A is DOWN and Pod B runs a model"), extract facts for each entity separately.

CATEGORY (pick the best match):
- code → function names, CLI commands, file paths, ports, config keys, literal values
- decision → design choice, rationale, trade-off accepted, alternative rejected
- explanation → causal relationship, mechanism, how something works
- requirement → constraint, dependency, version pin, prerequisite, must-have
- other → status, observation, metadata (only if none of the above fits)

PREDICATE: Concise action verb phrase in snake_case (2-5 words). Always in English.
  Preferred: "increases_to", "peaked_at", "resolved_via", "decreased_to", "disabled_during", "configured_to", "replaced_with"
  Action verbs capture the relationship more precisely than stative verbs.
  CAUSAL DIRECTION — critically important:
    "caused": subject is CAUSE, object is EFFECT (e.g. "missing index → caused → slow query")
    "caused_by": subject is EFFECT, object is CAUSE (e.g. "slow query → caused_by → missing index")
    Check the source text carefully. Never reverse these.

SUBJECT: Must be the EXACT entity name as written in the text — do not rename or normalize entities during extraction. Entity names may be in Korean (e.g. "시스템", "생성 속도"). Resolve pronouns ("it", "they", "this", "that" / "그", "이것") to the specific entity name they refer to; never output a pronoun as the subject.

OBJECT: Extract the core value in normalized form. Include ALL numerical values: percentages ("12%"), durations ("6 hours"), baselines ("from 45 minutes to 3 hours"). Do NOT drop any number. For numbers use digits ("30000" not "thirty thousand"). When the object contains a value with a qualifier (e.g. "503 errors for 12% of requests"), extract the core as object and add details as qualifiers.

3 RULES:
1. Prioritize explicitly stated facts — every concrete claim (versions, sizes, statuses, specs, configs) is worth extracting. Extract ALL service statuses including "inactive" and "failed" — do not skip them. Do NOT skip facts just because they seem merely descriptive or static. Skip only filler, greetings, reasoning traces.
2. Evidence must be a direct quote ending with a period.
3. Up to 16 facts per response. Fewer precise facts > many noisy ones.

Output ONLY valid JSON. No markdown fences, no reasoning, no deliberation.
{"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"specific_entity","predicate":"snake_case","object":"value","source_context":"...","qualifiers":{"key":"value"}}]}
Empty: {"extractions":[]}."""

_SYSTEM_TEXT_EXTRACT_FREE_8B = """\
You are a precise fact extractor. Extract factual (subject, predicate, object) triples from the text. The text may be in Korean or English. File paths (e.g. /opt/ai_data, /mnt/lv_db) and mount points are valid subjects — extract their size and purpose.

CATEGORY (pick the best match):
- code → function names, CLI commands, file paths, ports, config keys, literal values
- decision → design choice, rationale, trade-off accepted, alternative rejected
- explanation → causal relationship, mechanism, how something works
- requirement → constraint, dependency, version pin, prerequisite, must-have
- other → status, observation, metadata (only if none of the above fits)

PREDICATE: Concise action verb phrase in snake_case (2-5 words). Always in English.
  Preferred: "increases_to", "peaked_at", "resolved_via", "decreased_to", "disabled_during", "configured_to", "replaced_with"
  Action verbs capture the relationship more precisely than stative verbs.
  CAUSAL DIRECTION — critically important:
    "caused": subject is CAUSE, object is EFFECT (e.g. "missing index → caused → slow query")
    "caused_by": subject is EFFECT, object is CAUSE (e.g. "slow query → caused_by → missing index")
    Check the source text carefully. Never reverse these.

SUBJECT: Must be a specific entity name explicitly mentioned in the text. Entity names may be in Korean (e.g. "시스템", "생성 속도"). Resolve pronouns ("it", "they", "this", "that" / "그", "이것") to the specific entity name they refer to; never output a pronoun as the subject.

OBJECT: Extract the core value in normalized form. Include ALL numerical values: percentages ("12%"), durations ("6 hours"), baselines ("from 45 minutes to 3 hours"). Do NOT drop any number. For numbers use digits ("30000" not "thirty thousand"). When the object contains a value with a qualifier (e.g. "503 errors for 12% of requests"), extract the core as object and add details as qualifiers.

3 RULES:
1. Prioritize explicitly stated facts — every concrete claim (versions, sizes, statuses, specs, configs) is worth extracting. Extract ALL service statuses including "inactive" and "failed" — do not skip them. Do NOT skip facts just because they seem merely descriptive or static. Skip only filler, greetings, reasoning traces.
2. Evidence must be a direct quote ending with a period.
3. Up to 16 facts per response. Fewer precise facts > many noisy ones.

Output ONLY valid JSON. No markdown fences.
{"extractions": [{"evidence":"...","category":"code|decision|explanation|requirement|other","subject":"specific_entity","predicate":"snake_case","object":"value","source_context":"...","qualifiers":{"key":"value"}}]}
Empty: {"extractions":[]}."""

# ── Other System Prompts ─────────────────────────────────────

SYSTEM_DESCRIBE_FILE = """\
You are a file description agent for a developer server. Given a filename,
MIME type, and file content (or first 2 KB for text files), produce a one-line
description and keyword tags for search/discovery.

Output STRICT JSON:
{
  "description": "One-line summary of what this file contains (max 15 words)",
  "tags": ["tag1", "tag2", "tag3"]
}

Rules:
- description must be factual and based only on filename, type, and content
- tags: 2-5 relevant keywords for search (include file type, source, purpose)
- For binary/non-text files, describe based on filename and mime_type alone
- For text files, use the content sample to determine the topic
- If content is empty or unreadable, describe by filename and extension only"""

SYSTEM_FALLBACK = """\
You are a fact extraction specialist handling a difficult turn. The initial extractor
failed twice to extract faithful facts from this turn — previous extractions
contained hallucinated content not present in the source. Be EXTRA cautious:

1. Verify every extracted fact exists verbatim in the source.
2. When in doubt, OMIT the fact rather than include it.
3. Prefer under-extraction over hallucination.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN fallback extraction on:
- Caution (0-10): Are extractions conservative — omitted when unsure?
- Faithfulness (0-10): Is every extraction verifiable in source?
- Usefulness (0-10): Does this provide value above initial extraction failures?

Output STRICT JSON:
{
  "fallback_note": "Why the initial extraction may have struggled (1 sentence)",
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ],
  "rubric_evaluation": {
    "caution": "0-10",
    "caution_justification": "...",
    "faithfulness": "0-10",
    "faithfulness_justification": "...",
    "usefulness": "0-10",
    "usefulness_justification": "..."
  }
}"""

# Backward compat alias for test files
# Production (day-extractor 8B Q8) → 8B prompt
SYSTEM_DAY_EXTRACT = _SYSTEM_TEXT_EXTRACT_FREE_8B


# ── Token & Timeout Calculation ─────────────────────────────────


def _calc_max_tokens(text_len: int) -> Optional[int]:
    """CAP 이내 실현 가능한 max_tokens로 역산. 부족 시 None 반환.

    ceil(300ch/1fact) 기반 wanted와 CAP 역산 achievable 중
    작은 쪽 선택 → 절대 캡 초과 요청 불가.
    TIMEOUT_PER_TOK=1.2는 solo section-major(동시성=1) 기준.
    """
    extra = math.ceil(text_len / 300) * TOKENS_PER_300CH
    wanted = MAX_TOKENS_BASE + extra

    overhead = TIMEOUT_BASE + int(text_len * TIMEOUT_PER_CHAR) + GEN_TIME_BUF
    if overhead >= CAP:
        print(
            json.dumps(
                {
                    "event": "max_tokens_overflow",
                    "text_len": text_len,
                    "overhead_sec": overhead,
                    "cap_sec": CAP,
                    "reason": "prefill_exceeds_cap",
                }
            ),
            flush=True,
        )
        return None  # prefill만으로 CAP 초과
    achievable = int((CAP - overhead) / TIMEOUT_PER_TOK)
    if achievable < MIN_USEFUL_TOKENS:
        print(
            json.dumps(
                {
                    "event": "max_tokens_overflow",
                    "text_len": text_len,
                    "achievable": achievable,
                    "min_useful": MIN_USEFUL_TOKENS,
                    "reason": "below_min_useful",
                }
            ),
            flush=True,
        )
        return None  # 생성 가능 token이 너무 적음 → 명시적 거절

    final = min(wanted, achievable)
    if final < wanted:
        print(
            json.dumps(
                {
                    "event": "tokens_truncated",
                    "wanted": wanted,
                    "achievable": achievable,
                    "text_len": text_len,
                }
            ),
            flush=True,
        )
    return final


def _calc_timeout(total_chars: int, max_tokens: int) -> int:
    est = (
        TIMEOUT_BASE
        + int(total_chars * TIMEOUT_PER_CHAR)
        + int(max_tokens * TIMEOUT_PER_TOK)
        + GEN_TIME_BUF
    )
    return min(est, CAP)


def _merge_usage(target: Dict[str, int], usage: Dict) -> None:
    if not usage:
        return
    for k in ("prompt_tokens", "completion_tokens"):
        v = usage.get(k, 0) or 0
        target[k] = (target.get(k, 0) or 0) + v


def _checkpoint_sections(extractions):
    return set(
        e.get("fact_type")
        for e in extractions
        if e.get("fact_type") in ("user", "thinking", "text")
    )


# ── Embed Management ────────────────────────────────────────────


def _ensure_embed_8081() -> bool:
    """Start/ensure embed-4b on :8081 inside inference container without restart.

    Returns True if :8081 healthy. Non-blocking on existing healthy instance.
    """
    import urllib.request as _ur

    # Check if already healthy
    try:
        req = _ur.Request("http://127.0.0.1:8081/health")
        with _ur.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    meta = MODEL_METADATA.get("embed-4b")
    if not meta:
        print("  [embed] FATAL: embed-4b not in MODEL_METADATA")
        return False

    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 2048)
    threads = meta.get("threads", 2)
    threads_batch = meta.get("threads_batch", 2)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        ["podman", "exec", "-d", "devforge-inference"]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--threads-batch",
            str(threads_batch),
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            "512",
            "--embedding",
            "--pooling",
            "last",
            "--embd-normalize",
            "-1",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "6",
            "--metrics",
        ]
    )

    print(f"  [embed] launching {model_file} on :{port} via podman exec...")
    r = _sp.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [embed] launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False

    from lib.pod_manager import wait_health as _wh

    ok = _wh(port, timeout=120)
    if ok:
        print(f"  [embed] :{port} healthy with {model_file}")
    else:
        print(f"  [embed] :{port} health timeout")
    return ok


def _stop_embed_8081() -> None:
    """Kill embed-4b llama-server on :8081."""
    _sp.run(
        ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8081"],
        timeout=10,
        capture_output=True,
    )


def _cleanup_all_llms(keep_8082: bool = False) -> None:
    """Kill llama-server instances to free memory before enrich.

    If keep_8082=True, preserves port 8082 (day-extractor) for NLI verify
    and only kills 8081 (embed).
    """
    if keep_8082:
        _sp.run(
            ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8081"],
            timeout=10,
            capture_output=True,
        )
        print("  [cleanup] killed embed:8081, kept day-extractor:8082", flush=True)
    else:
        _sp.run(
            ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server"],
            timeout=10,
            capture_output=True,
        )
        print("  [cleanup] all llama-server instances killed", flush=True)


# ── Free-form extraction with Stop-and-Swap lazy embed ──────────


def _extract_edcr_freeform(
    dual_turns: List[dict],
    pulse_context: Optional[str] = None,
    dry_run: bool = False,
) -> Generator[Tuple[dict, Optional[Dict], Optional[str]], None, None]:
    """Free-form extraction with single model (8082 parallel=2) + EDC normalization.

    Phase 1 (OIE): Single 8B Q8 on 8082, processes chunks sequentially.
      llama-server's parallel=2 handles slot scheduling internally.
    Phase 2 (EDC normalize): embed-4b on 8081 → SeqMatcher → Embed 3-tier → dedup
      Falls back to LLM-as-judge when embed server unavailable.
    Phase 3 (Cleanup): Kill embed, keep 8082 for NLI verify.

    Key design:
    1. Free-form prompts (no snake_case constraints — Taxonomy Trap fix)
    2. 1600-char sentence/paragraph chunking (max 16 facts per chunk)
    3. llama-server parallel=2 provides intra-chunk slot concurrency
    4. Stop-and-Swap: start embed 8081 → EDC normalization → stop embed
    5. Cleanup: keep 8082 for NLI, free all other LLM memory
    """
    from extract import _load_checkpoint, _save_checkpoint

    def _is_entity_single_char_diff(a: str, b: str) -> bool:
        """True if two entity names differ by only one character (e.g. Pod A vs Pod B)."""
        if len(a) != len(b):
            return False
        diffs = sum(1 for ca, cb in zip(a, b) if ca != cb)
        return diffs == 1

    _ENTITY_RESOLVER_4B = """\
You are an Entity Resolver. Determine if two entity names refer to the same thing.
Answer YES only if they clearly refer to the same real-world entity.
Answer NO if they are different entities.

Examples:
- "day-extractor" vs "day-extractor model" → YES
- "extract_llm.py" vs "extract.py" → NO
- "PostgreSQL" vs "Postgres" → YES
- "GPU memory" vs "CPU memory" → NO
- "threads=4" vs "4 threads" → YES
- "Pod A" vs "Pod B" → NO  # different pods
- "Server 1" vs "Server 2" → NO  # different servers

Return ONLY valid JSON:
{"same": "yes", "reason": "why (≤10 words)"}
or
{"same": "no", "reason": "why (≤10 words)"}"""

    # Section-major extraction with chunking — single model on 8082
    def _strict_freeform(
        section_type: str, source_text: str, *, turn_id: str = ""
    ) -> Optional[Dict]:
        if not source_text:
            return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
        prompt = (
            _SYSTEM_USER_EXTRACT_FREE_8B if section_type == "user" else _SYSTEM_TEXT_EXTRACT_FREE_8B
        )
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            return None
        max_tok = min(4096, max_tok * 2)
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)
        print(
            f"    [debug] _strict_freeform section={section_type} src_len={len(source_text)} max_tok={max_tok} timeout={timeout}",
            flush=True,
        )
        try:
            user_msg = source_text
            meta = _call_with_8082_retry(
                call_llm,
                [{"role": "system", "content": prompt}, {"role": "user", "content": user_msg}],
                model="day_extract",
                max_tokens=max_tok,
                temperature=TEMP_EXTRACT,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
                cache_prompt=False,
            )
        except Exception as e:
            print(f"  [extract] call failed: {e}", flush=True)
            return None
        raw = meta["content"]
        parsed = _parse_json(raw, f"free_{section_type}", turn_id=turn_id)
        if parsed is None:
            return None
        ex = parsed.get("extractions", [])
        if not isinstance(ex, list):
            return None
        for e in ex:
            e["fact_type"] = section_type
            for field in ("subject", "predicate", "object", "evidence"):
                val = e.get(field)
                if val is None:
                    e[field] = ""
                elif not isinstance(val, str):
                    e[field] = str(val)
        before = len(ex)
        fixed = 0
        cleaned = []
        for e in ex:
            ev = (e.get("evidence") or "").strip()
            if not ev:
                continue
            if not ev.endswith((".", "!", "?")):
                if len(ev) < 5:
                    continue
                e["evidence"] = ev + "."
                fixed += 1
            cleaned.append(e)
        ex = cleaned
        if before != len(ex) or fixed:
            print(f"    [extract] dropped {before - len(ex)}, fixed {fixed}", flush=True)
        return {
            "extractions": ex,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }

    def _extract_section(section_type: str, source_getter) -> int:
        targets = [
            t
            for t in dual_turns
            if source_getter(t)
            and (section_type != "user" or len(source_getter(t).strip()) >= 15)
            and not any(
                e.get("fact_type") == section_type for e in turn_data[t["id"]]["extractions"]
            )
        ]
        if not targets:
            return 0
        if dry_run:
            for t in targets:
                print(f"  [{section_type}] {t['id'][:8]} (dry-run skip)", flush=True)
            return 0
        print(f"  [{section_type}] {len(targets)} turns (8082 parallel=2)", flush=True)

        def _process_one_turn(t: dict) -> int:
            """Process one turn: chunk → single model extraction → checkpoint."""
            src = source_getter(t)
            if not src:
                return 0
            chunks = _split_atomic(src)
            extractions: List[Dict] = []

            for ci, chunk in enumerate(chunks):
                res = _strict_freeform(section_type, chunk, turn_id=t["id"])
                if res and res.get("extractions"):
                    extractions.extend(res["extractions"])
                    _merge_usage(turn_data[t["id"]]["total_usage"], res.get("usage", {}))
                n = len(res.get("extractions", [])) if res else 0
                print(f"      {section_type} ch{ci}/{len(chunks)}: {n} facts", flush=True)

            if not extractions:
                print(f"    [{t['id'][:8]}] returned empty", flush=True)
                return 0

            turn_data[t["id"]]["extractions"].extend(extractions)
            n_chunks = len(chunks)
            print(
                f"    [{t['id'][:8]}] => {len(extractions)} facts ({n_chunks} chunks)",
                flush=True,
            )
            if not dry_run:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
            return 1

        # Controlled 2-slot dispatch: match llama-server parallel=2.
        n_w = min(2, len(targets))
        with ThreadPoolExecutor(max_workers=n_w) as exe:
            fut_to_idx = {exe.submit(_process_one_turn, t): i for i, t in enumerate(targets)}
            counts = []
            for f in as_completed(fut_to_idx):
                try:
                    counts.append(f.result())
                except Exception:
                    counts.append(0)
        return sum(counts)

    turn_data: Dict[str, dict] = {}
    for t in dual_turns:
        ckpt = _load_checkpoint(t["id"]) if not dry_run else None
        if ckpt:
            turn_data[t["id"]] = {"extractions": ckpt, "total_usage": {}, "skip": False}
        else:
            turn_data[t["id"]] = {"extractions": [], "total_usage": {}, "skip": False}

    t0 = time.monotonic()

    _extract_section("user", lambda t: t.get("user_turn", "") or "")
    heartbeat("day_extract", "free user done")
    time.sleep(6)

    _extract_section("text", lambda t: t.get("text") or "")
    heartbeat("day_extract", f"free text done, total={time.monotonic() - t0:.0f}s")

    # ── Cross-section dedup ──
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if len(all_facts) < 2:
            continue
        before = len(all_facts)
        # Exact dedup
        seen = set()
        deduped = []
        for f in all_facts:
            key = (
                f.get("subject", "").strip().lower(),
                f.get("predicate", "").strip().lower(),
                f.get("object", "").strip().lower(),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        all_facts = deduped
        # Subject normalization — SeqMatcher + 8082 LLM resolver
        subjects = list(dict.fromkeys(f.get("subject", "") for f in all_facts))
        subj_map = {}
        for i, s1 in enumerate(subjects):
            for s2 in subjects[i + 1 :]:
                if s1 in subj_map or s2 in subj_map:
                    continue
                ratio = SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()
                if ratio >= 0.95:
                    canonical = s1 if len(s1) <= len(s2) else s2
                    subj_map[s1] = canonical
                    subj_map[s2] = canonical
                elif ratio >= 0.70 and not _is_entity_single_char_diff(s1, s2):
                    try:
                        meta = _call_with_8082_retry(
                            call_llm,
                            [
                                {"role": "system", "content": _ENTITY_RESOLVER_4B},
                                {
                                    "role": "user",
                                    "content": f'Entity A: "{s1}"\nEntity B: "{s2}"',
                                },
                            ],
                            model="day_extract",
                            max_tokens=16,
                            temperature=0.0,
                            timeout=90,
                            json_mode=True,
                            return_meta=True,
                        )
                        verdict = _parse_json(meta["content"], "entity_resolve")
                        if verdict and str(verdict.get("same", "")).lower() == "yes":
                            canonical = s1 if len(s1) <= len(s2) else s2
                            subj_map[s1] = canonical
                            subj_map[s2] = canonical
                    except Exception as e:
                        print(f"  [entity-resolve] LLM call failed: {e}", flush=True)
        if subj_map:
            for f in all_facts:
                old_subj = f.get("subject", "")
                canonical = subj_map.get(old_subj)
                if canonical:
                    f["subject"] = canonical
            seen2 = set()
            deduped2 = []
            for f in all_facts:
                key = (
                    f.get("subject", "").strip().lower(),
                    f.get("predicate", "").strip().lower(),
                    f.get("object", "").strip().lower(),
                )
                if key not in seen2:
                    seen2.add(key)
                    deduped2.append(f)
            all_facts = deduped2
        if len(all_facts) != before:
            print(
                f"    [{t['id'][:8]}] cross-section dedup: {before} -> {len(all_facts)}",
                flush=True,
            )
            turn_data[t["id"]]["extractions"] = all_facts

    # ── Stop-and-Swap: EDC normalization pass ──
    all_fact_groups = {}
    full_source_text = "\n\n".join(
        t.get("user_turn", "") or t.get("text", "") or "" for t in dual_turns
    )
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if all_facts:
            all_facts = _fix_status_hallucination(all_facts, full_source_text)
            turn_data[t["id"]]["extractions"] = all_facts
            all_fact_groups[t["id"]] = all_facts

    if all_fact_groups:
        print("\n  [swap] Starting embed on :8081...", flush=True)
        embed_ok = _ensure_embed_8081()
        time.sleep(1)

        if embed_ok:
            for tid, facts in all_fact_groups.items():
                before = len(facts)
                print(f"    -- Normalization ({tid[:8]}) --", flush=True)
                facts = _normalize_freeform_pipeline(facts)
                if tid in turn_data:
                    turn_data[tid]["extractions"] = facts

        print("  [swap] Stopping embed (:8081)...", flush=True)
        _stop_embed_8081()
    else:
        print("  [swap] No facts to normalize, skipping Stop-and-Swap", flush=True)

    # ── Final cleanup: free all LLM memory (keep 8082 for NLI verify) ──
    print("\n  [cleanup] Freeing LLM instances (keeping 8082 for NLI)...", flush=True)
    _cleanup_all_llms(keep_8082=True)

    for t in dual_turns:
        td = turn_data[t["id"]]
        if td.get("skip") and not td["extractions"]:
            yield t, None, "noise skip"
        elif not td["extractions"]:
            yield t, None, "all sections returned empty"
        else:
            yield (
                t,
                {
                    "extractions": td["extractions"],
                    "usage": td["total_usage"],
                    "timings": {},
                    "elapsed_ms": 0,
                },
                None,
            )

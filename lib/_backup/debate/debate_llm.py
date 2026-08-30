#!/usr/bin/env python3
# Status: experimental
# Path: lib/debate/debate_llm.py — imported by cooperative_debate.py, local_debate.py, cooperative_remote.py
"""LLM calling, JSON parsing, model switching — shared infrastructure.

Pure functions with no class dependency. Imported by both LocalDebate and CooperativeDebate.
"""

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.llm_client import MODEL_REGISTRY

from .debate_data import MODELS, PROMPTS, SWITCH_FILE

# Map debate_data model_id → MODEL_REGISTRY key (for unified config access)
_DEBATE_TO_REGISTRY: Dict[str, str] = {
    "qwen3-30b-a3b-local": "proposer",
    "qwen2.5-coder-7b": "extractor",
}


# ── JSON parsing ────────────────────────────────────────────────────────────


def _parse_json(raw: str) -> Optional[dict]:
    """Thin wrapper — delegates to shared Recovery Ladder in lib.llm.json_parser."""
    from lib.llm.json_parser import parse_llm_json

    return parse_llm_json(raw)


# ── Message building ────────────────────────────────────────────────────────


def _build_messages(prompt_key: str, model_id: str, **kwargs) -> List[Dict[str, str]]:
    """Build chat messages respecting system_prompt_support flag."""
    template = PROMPTS[prompt_key]
    model_cfg = MODELS[model_id]
    messages = []

    if model_cfg["system_prompt_support"] and template["system"]:
        messages.append({"role": "system", "content": template["system"]})

    user_content = template["user"].format(**kwargs)
    messages.append({"role": "user", "content": user_content})
    return messages


# ── Supervisor switch file ──────────────────────────────────────────────────


def _write_switch_file(model_id: str) -> None:
    """Write model-switch.json for supervisor on :8081 to detect."""
    cfg = MODELS[model_id]
    data = {
        "model_file": cfg["filename"],
        "port": cfg["port"],
        "ctx": cfg["ctx"],
        "threads": cfg["threads"],
        "mlock": cfg["mlock"],
        "cache_ram": cfg.get("cache_ram", 0),
    }
    os.makedirs(os.path.dirname(SWITCH_FILE), exist_ok=True)
    with open(SWITCH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [switch] wrote {cfg['filename']} (mlock={cfg['mlock']})")


# ── Page cache eviction ─────────────────────────────────────────────────────


def _evict_file_cache(filepath: str) -> bool:
    """Evict a file's pages from kernel page cache via posix_fadvise(DONTNEED).

    Targeted eviction — only affects this file, not the entire cache.
    Returns True on success, False if file not found or eviction failed.
    """
    try:
        fd = os.open(filepath, os.O_RDONLY)
        try:
            st_size = os.fstat(fd).st_size
            os.posix_fadvise(fd, 0, st_size, os.POSIX_FADV_DONTNEED)
            print(f"  [evict] {os.path.basename(filepath)}: {st_size // (1024 * 1024)}MB evicted")
            return True
        finally:
            os.close(fd)
    except FileNotFoundError:
        print(f"  [evict] file not found: {filepath}")
        return False
    except Exception as e:
        print(f"  [evict] failed: {e}")
        return False


# ── Health polling ──────────────────────────────────────────────────────────


def _poll_health(port: int, timeout: int = 240, backoff_base: float = 2.0) -> bool:
    """Poll :<port>/health with exponential backoff."""
    health_url = f"http://127.0.0.1:{port}/health"
    print(f"  [health] waiting for :{port} (timeout={timeout}s)...")
    start = time.monotonic()
    attempt = 0
    while time.monotonic() - start < timeout:
        try:
            req = urllib.request.Request(health_url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    if data.get("status") == "ok":
                        elapsed = time.monotonic() - start
                        print(f"  [health] :{port} ready after {elapsed:.0f}s")
                        return True
        except Exception:
            pass
        attempt += 1
        delay = min(backoff_base * (2**attempt), 8.0)
        time.sleep(delay)
    print(f"  [health] :{port} TIMEOUT after {timeout}s")
    return False


# ── Error classification ────────────────────────────────────────────────────


def _is_retryable(err_msg: str) -> bool:
    """Check if error is a transient connection issue worth retrying."""
    retryable = ("timed out", "Remote end closed", "Connection reset", "Connection aborted")
    return any(p in err_msg for p in retryable)


# ── File I/O ────────────────────────────────────────────────────────────────


def _read_file_content(file_path: str) -> Optional[str]:
    """Read target file for DRAG analysis. Returns None if file not found."""
    try:
        return Path(file_path).read_text()
    except FileNotFoundError:
        alt = Path("/opt/projects/server") / file_path.lstrip("/")
        try:
            return alt.read_text()
        except Exception:
            return None
    except Exception:
        return None


def _extract_file_path(question: str) -> Optional[str]:
    """Extract file path from question format: 'File: /path/to/file.py\nTask: ...'"""
    m = re.search(r"File:\s*(.+?\.py)", question)
    return m.group(1).strip() if m else None


# ── LLM calling ─────────────────────────────────────────────────────────────


def call_llm(
    messages: List[Dict], model_id: str, dry_run: bool = False, max_tokens: Optional[int] = None
) -> Optional[str]:
    """Call llama-server and return raw text response.

    Timeout = max_tokens / bench_toks + 600s buffer.
    On connection error, retries once with halved max_tokens.
    """
    cfg = MODELS[model_id]
    port = cfg.get("local_port", cfg["port"])
    llm_url = f"http://127.0.0.1:{port}/v1/chat/completions"

    # Inject feedback few-shot (self-reinforcing loop from activity_log)
    registry_key = _DEBATE_TO_REGISTRY.get(model_id)
    if registry_key:
        from lib.llm_client import _inject_feedback

        messages = _inject_feedback(messages, registry_key)

    mt = max_tokens if max_tokens is not None else cfg["max_tokens"]
    body: Dict[str, Any] = {
        "messages": messages,
        "temperature": cfg["temperature"],
        "max_tokens": mt,
    }
    if "model_name" in cfg:
        body["model"] = cfg["model_name"]
    if "top_p" in cfg:
        body["top_p"] = cfg["top_p"]
    if "chat_template_kwargs" in cfg:
        body["chat_template_kwargs"] = cfg["chat_template_kwargs"]

    if dry_run:
        print(
            f"  [dry-run] LLM call :{port}: {len(body['messages'])} msgs, "
            f"max_tokens={body['max_tokens']}"
        )
        return '{"dry_run": true}'

    for attempt in range(2):
        gen_rate = cfg.get("bench_toks", 2.0)
        timeout = int(body["max_tokens"] / gen_rate) + 600
        print(
            f"  [llm] calling {model_id} on :{port} (max_tokens={body['max_tokens']}, timeout={timeout}s"
            f"{', retry' if attempt > 0 else ''})..."
        )
        t_start = time.monotonic()
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                llm_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                elapsed = time.monotonic() - t_start
                content = result["choices"][0]["message"]["content"] or ""
                if not content.strip():
                    reasoning = result["choices"][0]["message"].get("reasoning_content", "")
                    if reasoning:
                        content = reasoning
                        print(
                            f"  [llm] response in {elapsed:.1f}s ({len(content)} chars, from reasoning_content)"
                        )
                    else:
                        print(f"  [llm] response in {elapsed:.1f}s (0 chars, empty)")
                        if attempt == 0 and body["max_tokens"] > 256:
                            body["max_tokens"] = max(body["max_tokens"] // 2, 256)
                            body["temperature"] = min(body["temperature"], 0.1)
                            print(f"  [llm] retrying with max_tokens={body['max_tokens']}...")
                            time.sleep(3)
                            continue
                        return None
                else:
                    print(f"  [llm] response in {elapsed:.1f}s ({len(content)} chars)")
                return content
        except Exception as e:
            err_msg = str(e)
            print(f"  [llm] ERROR: {e}")
            if attempt == 0 and _is_retryable(err_msg) and body["max_tokens"] > 256:
                body["max_tokens"] = max(body["max_tokens"] // 2, 256)
                body["temperature"] = min(body["temperature"], 0.1)
                print(f"  [llm] retrying with max_tokens={body['max_tokens']}...")
                time.sleep(3)
                continue
            return None

    return None


def call_llm_json(
    prompt_key: str, model_id: str, dry_run: bool = False, retry: int = 1, **kwargs
) -> Optional[dict]:
    """Call LLM and parse JSON response. Retry once with strict prompt on failure."""
    messages = _build_messages(prompt_key, model_id, **kwargs)
    raw = call_llm(messages, model_id, dry_run=dry_run)
    if raw is None:
        return None

    parsed = _parse_json(raw)
    if parsed is not None:
        return parsed

    if retry > 0:
        print("  [json] parse failed, retrying with strict prompt...")
        strict_msg = (
            "Your previous response was not valid JSON. "
            "Output STRICT JSON ONLY. No extra text, no markdown."
        )
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": strict_msg})
        raw2 = call_llm(messages, model_id, dry_run=dry_run)
        if raw2:
            parsed2 = _parse_json(raw2)
            if parsed2 is not None:
                return parsed2
        combined = (raw or "") + "\n" + (raw2 or "")
        parsed = _parse_json(combined)

    return parsed


# ── Trend formatting ────────────────────────────────────────────────────────


def format_trend(consensus_scores: list) -> str:
    """Format consensus scores as a visual trend bar string."""
    if not consensus_scores:
        return "(no data)"
    parts = []
    for i, s in enumerate(consensus_scores):
        filled = s // 10
        bar = "█" * filled + "░" * (10 - filled)
        parts.append(f"R{i + 1}: {bar} {s}%")
    return " | ".join(parts)


# ── Report writing ──────────────────────────────────────────────────────────


def write_report(
    state_dir: Path,
    session_id: str,
    question: str,
    method: str,
    consensus_scores: list,
    final: dict,
) -> Path:
    """Write final_report.md from debate results."""
    path = state_dir / "final_report.md"
    trend = format_trend(consensus_scores)

    diff_value = final.get("diff", "(no diff)")
    if isinstance(diff_value, dict):
        diff_value = json.dumps(diff_value, indent=2, ensure_ascii=False)
    elif not isinstance(diff_value, str):
        diff_value = str(diff_value)

    lines = [
        f"# Debate Report — {session_id}",
        "",
        f"**Question:** {question}",
        f"**Method:** {method}",
        f"**Rounds:** {len(consensus_scores)} debate + synthesis",
        f"**Consensus Trend:** {trend}",
        f"**Final Confidence:** {final.get('confidence', '?')}",
        "",
        "## Decision Summary",
        final.get("decision_summary", "(no summary)"),
        "",
        "## Final Diff",
        f"```diff\n{diff_value}\n```",
        "",
        "## Security Notes",
        *[f"- {n}" for n in final.get("security_notes", [])],
        "",
        "## Performance Notes",
        *[f"- {n}" for n in final.get("performance_notes", [])],
        "",
        "---",
        "*Generated by DevForge Debate Orchestrator v6.0*",
        f"*Session: {session_id}*",
    ]
    path.write_text("\n".join(lines))
    print(f"  [report] {path}")
    return path


# ── Local model switching ───────────────────────────────────────────────────


def switch_local_model(model_id: str, dry_run: bool = False) -> bool:
    """Switch model on inference container via supervisor (:8081/:8082/:8083).

    Port 8080 (reranker) is always-on — just verify health.
    Other ports use switch-file protocol — write model-switch.json, wait for supervisor.
    """
    cfg = MODELS[model_id]
    port = cfg["port"]

    if dry_run:
        print(f"  [dry-run] switch to {model_id} ({cfg['filename']}) on :{port}")
        return True

    if port == 8080:
        return _poll_health(port=8080, timeout=cfg.get("bench_load_s", 30) + 30)

    # Inference container (:8081+) — supervisor-managed
    same_model = False
    try:
        if os.path.exists(SWITCH_FILE):
            with open(SWITCH_FILE) as f:
                prev = json.load(f)
            if prev.get("model_file") == cfg["filename"]:
                same_model = True
    except Exception:
        pass

    if same_model:
        return _poll_health(
            port=MODEL_REGISTRY["proposer"]["port"], timeout=cfg.get("bench_load_s", 120) + 60
        )

    _write_switch_file(model_id)
    time.sleep(5)
    for _ in range(12):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{MODEL_REGISTRY['proposer']['port']}/health"
            )
            with urllib.request.urlopen(req, timeout=3):
                pass
            time.sleep(5)
        except Exception:
            break
    return _poll_health(
        port=MODEL_REGISTRY["proposer"]["port"], timeout=cfg.get("bench_load_s", 120) + 60
    )


# ── Early exit check ────────────────────────────────────────────────────────


def check_early_exit(consensus_scores: list) -> Optional[str]:
    """Check if debate should exit early based on consensus scores."""
    if not consensus_scores:
        return None
    latest = consensus_scores[-1]
    if latest >= 90:
        return f"consensus >= 90% ({latest}%)"
    if len(consensus_scores) >= 2:
        improvement = latest - consensus_scores[-2]
        if improvement < 5:
            return f"stagnation: improvement < 5% ({improvement}%)"
    return None

#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Shared utilities for code modification pipelines.

Used by both code_mod_pipeline.py and hybrid_pipeline.py.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SERVER_DIR = Path("/opt/projects/server")
TASKS_FILE = SERVER_DIR / "code_mod_test_tasks.yaml"
OUTPUT_DIR = Path("/var/tmp/code_mod_tests")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
from lib.llm_client import MODEL_REGISTRY
LLAMA_ENDPOINT = f"http://127.0.0.1:{MODEL_REGISTRY['verifier']['port']}"


def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def extract_json_from_llm_response(llm_result: tuple) -> dict:
    """Extract JSON from LLM response tuple (status, body)."""
    status, body = llm_result
    if status != 200:
        return {"error": body.get("error", f"HTTP {status}"), "body": body}

    content = ""
    try:
        choices = body.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
    except Exception:
        pass

    result = {"status": status, "raw_content": content, "body": {}}

    if content:
        try:
            result["body"] = json.loads(content)
        except json.JSONDecodeError:
            for marker in ("```json", "```"):
                if marker in content:
                    start = content.find(marker) + len(marker)
                    end = content.find("```", start)
                    if end > start:
                        try:
                            result["body"] = json.loads(content[start:end].strip())
                        except json.JSONDecodeError:
                            pass
                        break
            if not result["body"]:
                result["body"] = {"text": content}

    prompt_tokens = body.get("usage", {}).get("prompt_tokens", 0)
    completion_tokens = body.get("usage", {}).get("completion_tokens", 0)
    result["tokens"] = {"prompt": prompt_tokens, "completion": completion_tokens}

    return result


def save_result(data: dict, prefix: str, task_id: int, suffix: str = ""):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    utc_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    name = f"{prefix}_task{task_id:02d}{suffix}_{utc_ts}.json"
    with open(OUTPUT_DIR / name, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return name


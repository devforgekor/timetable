#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Recovery Ladder: stdlib json.loads → json_repair for LLM output.

Single shared implementation — used by code_mod_pipeline, debate, review_worker.

DLQ: parse failures are saved to {EXPER_DIR}/dlq/ for later debugging.
Schema validation: optional JSON Schema check with field-level errors.
"""

import json, os, hashlib, time
from typing import Optional


DLQ_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "dlq")


def _dlq_path(stage: str = "unknown") -> str:
    """Return append-only JSONL path for a given stage/model."""
    safe = stage.replace("/", "_").replace(" ", "_")
    os.makedirs(DLQ_DIR, exist_ok=True)
    return os.path.join(DLQ_DIR, f"{safe}.jsonl")


def save_dlq(raw: str, stage: str = "unknown", model: str = "",
             error: str = "", attempt: int = 1,
             checkpoint: str = "", turn_id: str = "") -> None:
    """Append parse failure to DLQ for offline review.

    Args:
        raw:        Raw LLM output text that failed to parse.
        stage:      Pipeline phase or context label (e.g. ``"P_round1"``).
        model:      Physical model name that produced the output.
        error:      Error message from the failed parse attempt.
        attempt:    Which retry attempt this was (1-based).
        checkpoint: Checkpoint or phase key for resubmission support.
        turn_id:    UUID of the turn being processed (for recoverability).
    """
    entry = {
        "ts": time.time(),
        "stage": stage,
        "model": model,
        "attempt": attempt,
        "error": error[:200],
        "raw_len": len(raw),
        "raw_preview": raw[:5000],
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()[:16],
    }
    if checkpoint:
        entry["checkpoint"] = checkpoint
    if turn_id:
        entry["turn_id"] = turn_id
    path = _dlq_path(stage)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def validate_schema(data: dict, schema: dict) -> list[str]:
    """Validate *data* against *schema* (JSON Schema subset).

    Checks:
      - Required keys are present.
      - Type of each present key matches ``type`` in schema.
      - ``additionalProperties: False`` — no unknown keys.

    Returns list of error messages (empty = valid).
    """
    errors: list[str] = []
    required = schema.get("required", [])
    props = schema.get("properties", {})

    # Required keys
    for key in required:
        if key not in data:
            errors.append(f"'{key}': required but missing")

    # Present keys: type check + enum check + unknown key check
    additional_ok = schema.get("additionalProperties", True)
    for key, val in data.items():
        if key not in props:
            if not additional_ok:
                errors.append(f"'{key}': unknown key (additionalProperties=False)")
            continue
        p = props[key]
        expected = p.get("type")
        if expected and not _type_matches(val, expected):
            errors.append(f"'{key}': expected {expected}, got {type(val).__name__}")
        # Enum check
        enum_vals = p.get("enum")
        if enum_vals is not None and val not in enum_vals:
            errors.append(f"'{key}': value '{val}' not in enum {enum_vals}")
        # Recurse into nested objects
        if expected == "object" and isinstance(val, dict):
            nested = props[key].get("properties", {})
            if nested:
                errors.extend(
                    f"{key}.{e}" for e in validate_schema(val, {
                        "required": props[key].get("required", []),
                        "properties": nested,
                        "additionalProperties": props[key].get("additionalProperties", True),
                    })
                )
    return errors


def _type_matches(val, expected: str) -> bool:
    """Check Python type against JSON Schema type string."""
    mapping = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "array": list, "object": dict, "null": type(None),
    }
    pytype = mapping.get(expected)
    return pytype is None or isinstance(val, pytype)


def parse_llm_json(text: str) -> Optional[dict]:
    """Parse possibly-malformed JSON from LLM output.

    Rung 1: stdlib json.loads (95% of cases)
    Rung 2: json_repair — trailing commas, unclosed braces, single quotes, md fences, etc.

    Strips <think>...</think> tags (reasoning-format none output).
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    # Strip markdown code fences (json_repair handles these too, but stdlib doesn't)
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        cleaned = "\n".join(lines).strip()
    # Rung 1
    try:
        result = json.loads(cleaned)
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict):
            return result[0]
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    # Rung 2
    try:
        from json_repair import repair_json
        result = repair_json(cleaned, return_objects=True)
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict):
            return result[0]
        return result if isinstance(result, dict) else None
    except Exception:
        return None


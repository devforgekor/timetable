# Status: production
# Path: imported by — lib/extract_llm (extraction subpackage)
"""LLM extraction JSON parser with error recovery.

Handles common 8B Q8 model JSON failure patterns: extra braces, missing
openers, truncated arrays, nested wrapping, control character escaping.
"""

import json
import re
from typing import Any, Dict, Optional

from lib.common import strip_think
from lib.llm.json_parser import parse_llm_json, save_dlq


def _clean_extraction_json(raw: str) -> str:
    """Fix common JSON formatting errors from the 8B Q8 extraction model.

    The model produces several distinct failure patterns:
      A. Extra closing brace before comma in array: ``'...}}}, {'`` → ``'...}}, {'``
      B. Extra closing bracket at end of array: ``'}]]}'`` → ``'}]}'``
      C. Multiple sequential ``{"extractions":[...]}`` objects separated by newlines
         → merged into a single array
      E. Quoted opening brace: ``, "{"evidence"`` → ``, {"evidence"``
      F. Nested array wrapping: ``[{"evidence":...}], [{`` → ``, {``
      G. Missing opening brace: ``}, "evidence"`` → ``}, {"evidence"``
    """
    if not raw:
        return raw
    raw = raw.strip()

    # Pattern C: merge multiple sequential {"extractions":[...]} objects
    # Use raw_decode to extract each valid top-level object, then merge arrays.
    decoder = json.JSONDecoder()
    idx = 0
    all_items: list[dict] = []
    count = 0
    while idx < len(raw):
        try:
            obj, end = decoder.raw_decode(raw, idx)
            if (
                isinstance(obj, dict)
                and "extractions" in obj
                and isinstance(obj["extractions"], list)
            ):
                for item in obj["extractions"]:
                    if isinstance(item, dict):
                        all_items.append(item)
                count += 1
            idx = end
            while idx < len(raw) and raw[idx] in " \n\r\t":
                idx += 1
        except (json.JSONDecodeError, ValueError):
            break

    if count > 1:
        return json.dumps({"extractions": all_items}, ensure_ascii=False)

    # Pattern E: remove extra quote before opening brace of a fact object
    # e.g. ..., "{"evidence": -> , {"evidence":
    raw = re.sub(r"""(,\s*)"(\s*\{)""", r"\1\2", raw)

    # Pattern F: unwrap nested array wrapping
    # e.g. }], [{ -> }, {  (model wraps individual facts in extra [] pairs)
    raw = re.sub(r"\}\],\s*\[(\{)", r"}, \1", raw)

    # Pattern G: add missing opening brace before known fact keys
    # when it follows an object close, e.g. }, "evidence" -> }, {"evidence"
    raw = re.sub(
        r'\}\s*,\s*("(?:evidence|subject|predicate|object|category|source_context|qualifiers)")',
        r"}, {\1",
        raw,
    )

    # Pattern A: remove extra closing brace before comma in array
    # e.g. }}}, -> },
    raw = re.sub(r"}}},(\s*\{)", r"}},\1", raw)

    # Pattern B: remove extra closing bracket at end of JSON
    if raw.rstrip().endswith("}]]}"):
        raw = raw.rstrip()[:-4] + "}]}"

    # Pattern H: convert key-value qualifiers {"key":"k","value":"v"} → {"k":"v"}
    raw = re.sub(
        r'"qualifiers"\s*:\s*\{\s*"key"\s*:\s*"([^"]*)"\s*,\s*"value"\s*:\s*"([^"]*)"\s*\}',
        r'"qualifiers":{"\1":"\2"}',
        raw,
    )
    # Also handle reversed: {"key":"89.8M": "value"} → {"89.8M":"value"}
    raw = re.sub(
        r'"qualifiers"\s*:\s*\{\s*"key"\s*:\s*"([^"]*)"\s*:\s*"value"\s*\}',
        r'"qualifiers":{"\1":"value"}',
        raw,
    )
    # Pattern H2: missing comma in qualifier when "value" is used as both key and value
    # e.g. {"key":"value":"actual"} → {"key":"value","value":"actual"}
    # e.g. {"key":"alternative":"reasoning"} → {"key":"alternative","value":"reasoning"}
    raw = re.sub(
        r'"qualifiers"\s*:\s*\{\s*"key"\s*:\s*"([^"]+)"\s*:\s*("(?:[^"]*)")',
        r'"qualifiers":{"key":"\1","value":\2',
        raw,
    )

    # Pattern J: premature extractions array closing before a fact object
    # e.g. ...,"qualifiers":{}}],{"evidence":"..." → ...,"qualifiers":{}}, {"evidence":"..."
    raw = re.sub(
        r'\}\]\s*,\s*(\{)("(?:evidence|subject|predicate|object|category))', r"}, \1\2", raw
    )
    # Pattern J variant: }}],[{ (no comma, ] directly before [)
    # e.g. ..."qualifiers":{}}],[{"evidence":"..." → ..."qualifiers":{}}, {"evidence":"..."
    raw = re.sub(
        r'\}\]\s*\[\s*(\{)("(?:evidence|subject|predicate|object|category))', r"}, \1\2", raw
    )

    # Pattern K: escape bare control characters in string values
    # JSON does not allow literal \n, \t, \r inside strings
    raw = re.sub(r"(?<=[^\\])\n", r"\\n", raw)

    # Pattern I: recover truncated JSON — find last complete fact and close
    # When the last fact is cut off mid-field, the JSON is invalid.
    # Find the last complete fact in the extractions array and close.
    if not _try_parse(raw):
        match = re.search(r'("extractions"\s*:\s*\[)', raw)
        if match:
            array_start = match.end() - 1
            depth = 0
            in_array = False
            last_fact_end = -1
            for i in range(array_start, len(raw)):
                if raw[i] == "[":
                    in_array = True
                    depth = 1
                elif raw[i] == "{":
                    if in_array:
                        depth += 1
                elif raw[i] == "}":
                    if in_array:
                        depth -= 1
                        if depth == 1:
                            last_fact_end = i + 1
                elif raw[i] == "]":
                    if in_array and depth == 1:
                        last_fact_end = i + 1
                        break
            if last_fact_end > 0:
                truncated = raw[:last_fact_end]
                if not truncated.endswith("]}") and not truncated.endswith("}]}"):
                    truncated = truncated.rstrip().rstrip(",") + "]}\n"
                if _try_parse(truncated):
                    raw = truncated
        if not _try_parse(raw):
            # Last resort: try raw_decode loop to extract any complete facts
            # Skips both whitespace and commas between top-level objects
            decoder = json.JSONDecoder()
            idx = 0
            all_items: list[dict] = []
            count = 0
            while idx < len(raw):
                try:
                    obj, end = decoder.raw_decode(raw, idx)
                    if isinstance(obj, dict):
                        if "extractions" in obj and isinstance(obj["extractions"], list):
                            for item in obj["extractions"]:
                                if isinstance(item, dict):
                                    all_items.append(item)
                            count += 1
                        elif "evidence" in obj:
                            # A bare fact object at top level — add as extractions[0]
                            all_items.append(obj)
                            count += 1
                    idx = end
                    while idx < len(raw) and raw[idx] in " \n\r\t,":
                        idx += 1
                except (json.JSONDecodeError, ValueError):
                    break
            if count > 0:
                raw = json.dumps({"extractions": all_items}, ensure_ascii=False)

    return raw


def _try_parse(raw: str) -> bool:
    """Quick check if raw is parseable JSON with an extractions key."""
    try:
        obj = json.loads(raw)
        return isinstance(obj, dict) and "extractions" in obj
    except (json.JSONDecodeError, ValueError, TypeError):
        return False


def _parse_json(
    raw: str, label: str = "LLM", attempt: int = 1, turn_id: str = ""
) -> Optional[Dict[str, Any]]:
    cleaned = strip_think(raw)
    cleaned = _clean_extraction_json(cleaned)
    result = parse_llm_json(cleaned)
    if result is None:
        # Fallback: try raw_decode — handles trailing garbage after valid JSON
        if cleaned.strip():
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(cleaned.strip())
                if isinstance(obj, dict):
                    result = obj
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    if result is None:
        save_dlq(
            raw,
            stage=f"extract_{label}",
            error="parse_llm_json returned None",
            attempt=attempt,
            turn_id=turn_id,
        )
    return result

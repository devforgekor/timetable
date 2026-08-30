#!/usr/bin/env python3
# Status: production
"""Text sanitization helpers for Anthropic/DeepSeek proxy.

Strips billing headers, flattens system blocks, and normalizes
message content for stable prefix caching.
"""

import json
import re
import sys
from typing import Any, Dict, List, Tuple

_BILLING_HEADER_RE = re.compile(
    r'^x-anthropic-billing-header:\s*.*$',
    re.IGNORECASE | re.MULTILINE,
)


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_flatten_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), (str, list, dict)):
            return _flatten_text(value["content"])
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _strip_billing_header(text: str) -> str:
    return _BILLING_HEADER_RE.sub("", text).strip()


def _strip_system_billing_header(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    system = payload.get("system")
    if system is None:
        return payload, False

    changed = False

    if isinstance(system, str):
        cleaned = _strip_billing_header(system)
        if cleaned != system:
            payload = dict(payload)
            payload["system"] = cleaned
            print("[anthropic_proxy] stripped billing header from system string", file=sys.stderr)
            return payload, True
        return payload, False

    if isinstance(system, list):
        new_system: list = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                cleaned = _strip_billing_header(block["text"])
                if cleaned != block["text"]:
                    block = dict(block)
                    block["text"] = cleaned
                    changed = True
            new_system.append(block)
        if changed:
            payload = dict(payload)
            payload["system"] = new_system
            print("[anthropic_proxy] stripped billing header from system content block", file=sys.stderr)

    return payload, changed


def _flatten_system_blocks(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    system = payload.get("system")
    if system is None or isinstance(system, str):
        return payload, False

    if not isinstance(system, list):
        return payload, False

    parts: list[str] = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
            if normalized:
                parts.append(normalized)

    merged = "\n\n".join(parts)
    if not merged:
        return payload, False

    payload = dict(payload)
    payload["system"] = merged
    return payload, True


def _sanitize_messages(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, False

    system_parts: List[str] = []
    cleaned_messages: List[Dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            cleaned_messages.append(message)
            continue
        if message.get("role") == "system":
            text = _flatten_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        cleaned_messages.append(message)

    if not system_parts:
        return payload, False

    updated = dict(payload)
    updated["messages"] = cleaned_messages
    existing_system = _flatten_text(updated.get("system"))
    merged_system = "\n\n".join(part for part in [existing_system, "\n\n".join(system_parts)] if part)
    cleaned_system = _strip_billing_header(merged_system)
    if cleaned_system != merged_system:
        print("[anthropic_proxy] stripped billing header from system prompt", file=sys.stderr)
    if cleaned_system:
        updated["system"] = cleaned_system
    elif "system" in updated:
        updated.pop("system", None)
    return updated, True

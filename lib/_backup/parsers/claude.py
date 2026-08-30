#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""parser_claude.py — extract turns from Claude Code session JSONL."""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30


def parse(path: Path, session_id: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse a Claude Code session JSONL.
    Returns (turns, model, is_active).
    """
    mtime = path.stat().st_mtime
    is_active = (time.time() - mtime) < ACTIVE_THRESHOLD_S

    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)

    turns = []
    current_user = None
    model = None
    acc_reasoning: List[str] = []
    acc_answer: List[str] = []
    first_assistant_uuid: Optional[str] = None
    first_assistant_ts: Optional[str] = None

    def _flush_turn():
        nonlocal current_user, acc_reasoning, acc_answer
        nonlocal first_assistant_uuid, first_assistant_ts
        if current_user is not None:
            reasoning = "\n".join(acc_reasoning) if acc_reasoning else None
            answer = "\n".join(acc_answer) if acc_answer else ""
            turns.append({
                "user_turn": current_user,
                "thinking": reasoning,
                "text": answer,
                "source_message_id": first_assistant_uuid,
                "created_at": first_assistant_ts,
            })
        current_user = None
        acc_reasoning = []
        acc_answer = []
        first_assistant_uuid = None
        first_assistant_ts = None

    for ev in events:
        if ev.get("isSidechain"):
            continue
        t = ev.get("type")
        if t == "user":
            content = ev.get("message", {}).get("content", "")
            # tool_result events arrive as list-type user events — skip without flushing
            if isinstance(content, list):
                continue
            _flush_turn()
            current_user = content
        elif t == "assistant":
            if current_user is None:
                continue
            msg = ev.get("message", {})
            model = msg.get("model", model)
            if first_assistant_uuid is None:
                first_assistant_uuid = ev.get("uuid")
                first_assistant_ts = ev.get("timestamp")
            content_blocks = msg.get("content", [])
            if not isinstance(content_blocks, list):
                content_blocks = []
            for block in content_blocks:
                if block.get("type") == "thinking":
                    acc_reasoning.append(block.get("thinking", ""))
                elif block.get("type") == "text":
                    acc_answer.append(block.get("text", ""))

    # Finalize last turn
    _flush_turn()

    # Active session: drop last turn if assistant was writing (no user follow-up)
    if is_active and turns:
        last = turns[-1]
        if not last["text"]:
            turns.pop()

    return (turns if turns else None), model, is_active


#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts, turn_watcher.py
"""parser_copilot.py — extract turns from Copilot events.jsonl.

Copilot CLI emits ONE interaction per user question, but breaks its response
into MANY assistant.turn_start/turn_end pairs (turnId 0..N) that all share the
same interactionId. Sub-agent (sidekick) activity is also interleaved in the
same stream using a distinct interactionId while the main turn is in flight.

This parser therefore groups assistant messages by interactionId and only
flushes a turn when the interaction closes (next user message / new
interaction's turn_start / EOF) — never at the first turn_end.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30


def parse(path: Path, session_id: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse a Copilot events.jsonl.
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

    turns: List[Dict[str, Any]] = []
    bucket: Optional[Dict[str, Any]] = None
    in_flight = False
    model: Optional[str] = None

    def _new_bucket(iid: str, user: str, created: Optional[str]) -> Dict[str, Any]:
        return {
            "iid": iid,
            "user": user,
            "thinking": [],
            "answer": [],
            "smid": iid,
            "created": created,
        }

    def _flush():
        nonlocal bucket
        if bucket is None:
            return
        if bucket["user"] or bucket["answer"] or bucket["thinking"]:
            turns.append({
                "user_turn": bucket["user"],
                "thinking": "\n".join(bucket["thinking"]) or None,
                "text": "\n".join(bucket["answer"]) or "",
                "source_message_id": bucket["smid"],
                "created_at": bucket["created"],
            })
        bucket = None

    for ev in events:
        t = ev.get("type")
        d = ev.get("data", {}) or {}

        if t == "user.message":
            iid = d.get("interactionId")
            content = d.get("content") or ""
            # Empty (sub-agent context injection) or in-flight (sidekick)
            # messages never start a real user turn.
            if not content or not iid or in_flight:
                continue
            if bucket is not None and bucket["iid"] != iid:
                _flush()
            if bucket is None or bucket["iid"] != iid:
                bucket = _new_bucket(iid, content, ev.get("timestamp"))
            else:
                bucket["user"] = content

        elif t == "assistant.turn_start":
            iid = d.get("interactionId") or in_flight
            if iid is None:
                continue
            in_flight = True
            if bucket is not None and bucket["iid"] != iid:
                _flush()
            if bucket is None or bucket["iid"] != iid:
                bucket = _new_bucket(iid, "", None)

        elif t == "assistant.message":
            iid = d.get("interactionId")
            amodel = d.get("model")
            if amodel:
                model = amodel
            if bucket is not None and (iid is None or iid == bucket["iid"]):
                if amodel:
                    bucket["model"] = amodel
                rt = d.get("reasoningText")
                if rt:
                    bucket["thinking"].append(rt)
                content = d.get("content") or ""
                if content:
                    bucket["answer"].append(content)

        elif t == "assistant.turn_end":
            in_flight = False

    # EOF flush: keep the last turn unless the assistant was still writing
    # (active session with no completed answer yet).
    if bucket is not None and (not is_active or bucket["answer"] or bucket["thinking"]):
        _flush()

    return (turns if turns else None), model, is_active

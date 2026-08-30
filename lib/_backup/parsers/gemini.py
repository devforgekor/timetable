#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""parser_gemini.py — extract turns from Gemini CLI session JSONL."""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30


def parse(path: Path, session_id: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse a Gemini CLI chat JSONL.

    Format:
      Line 1: session header (sessionId, startTime, kind)
      Alternating: message events ({id, timestamp, type, content, [thoughts]})
                    and $set events (ignored).

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
            if "$set" in ev:
                continue
            events.append(ev)

    if len(events) < 2:
        return None, None, is_active

    # First event is session header
    header = events[0]
    model = header.get("kind", "gemini")
    msgs = events[1:]

    turns = []
    current_user = None

    for ev in msgs:
        t = ev.get("type")
        content = ev.get("content", "")
        thoughts = ev.get("thoughts") or []

        if t == "user":
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict)]
                current_user = "\n".join(texts)
            elif isinstance(content, str):
                current_user = content
        elif t == "gemini":
            if isinstance(content, list):
                answer = "\n".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            else:
                answer = content or ""

            reasoning = "\n".join(
                th.get("description", "") for th in thoughts
                if isinstance(th, dict)
            ) or None

            if current_user is not None:
                token_meta = ev.get("tokens") or {}
                token_total = token_meta.get("total")
                token_count = None
                try:
                    if token_total is not None:
                        token_count = int(token_total)
                except (TypeError, ValueError):
                    token_count = None
                turns.append({
                    "user_turn": current_user,
                    "thinking": reasoning,
                    "text": answer,
                    "source_message_id": ev.get("id"),
                    "created_at": ev.get("timestamp"),
                    "tokens": token_count,
                })
                current_user = None

    # Active session: drop last turn if assistant just finished
    if is_active and turns and current_user is None:
        turns.pop()

    return (turns if turns else None), model, is_active


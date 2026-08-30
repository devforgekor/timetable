#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""parser_aider.py — extract turns from Aider chat history (.aider.chat.history.md)."""
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30

_SESSION_RE = re.compile(r"^# aider chat started at (.+)$")
_MODEL_RE = re.compile(r"^> Model: (.+)$")
_THINKING_RE = re.compile(r"^<thinking-content-[^>]+>$", re.MULTILINE)
_END_THINKING_RE = re.compile(r"^</thinking-content-[^>]+>$", re.MULTILINE)


def parse(path: Path, session_id: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse an Aider chat history file.

    Returns (turns, model, is_active).
    """
    mtime = path.stat().st_mtime
    is_active = (time.time() - mtime) < ACTIVE_THRESHOLD_S

    content = path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    turns = []
    sessions = _split_sessions(lines)
    for session_ts, session_lines in sessions:
        model = _extract_model(session_lines)
        session_turns = _extract_turns(session_lines)
        for t in session_turns:
            t["created_at"] = session_ts
        turns.extend(session_turns)

    # Determine model from last session that had one
    final_model = None
    for session_ts, session_lines in reversed(sessions):
        m = _extract_model(session_lines)
        if m:
            final_model = m
            break

    if not turns:
        return None, final_model, is_active

    return turns, final_model, is_active


def _split_sessions(lines):
    sessions = []
    current_ts = None
    current_lines = []

    for line in lines:
        m = _SESSION_RE.match(line)
        if m:
            if current_ts is not None and current_lines:
                sessions.append((current_ts, current_lines))
            current_ts = m.group(1).strip()
            current_lines = []
        elif current_ts is not None:
            current_lines.append(line)

    if current_ts is not None and current_lines:
        sessions.append((current_ts, current_lines))

    return sessions


def _extract_model(lines):
    for line in lines:
        m = _MODEL_RE.match(line)
        if m:
            model = m.group(1).strip()
            # Extract base model name (strip " with whole edit format" etc.)
            model = model.split(" with ")[0]
            return model
    return None


def _extract_turns(lines):
    """Extract Q&A pairs. User questions start with '#### ', answers follow."""
    turns = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("#### "):
            question = line[5:].strip()
            # Skip echoed commands and errors
            if question.startswith("/") or "litellm." in question.lower() or question.startswith("Warning"):
                i += 1
                continue

            answer_lines = []
            in_thinking = False
            i += 1

            while i < len(lines):
                ln = lines[i]
                if ln.startswith("#### ") or ln.startswith("# aider chat started"):
                    break
                if _THINKING_RE.match(ln):
                    in_thinking = True
                    i += 1
                    continue
                if _END_THINKING_RE.match(ln):
                    in_thinking = False
                    i += 1
                    continue
                if not in_thinking and ln.strip():
                    # Skip error lines and token metadata
                    if ln.startswith("> litellm.") or ln.startswith("> Tokens:"):
                        i += 1
                        continue
                    answer_lines.append(ln)
                i += 1

            answer = "\n".join(answer_lines).strip()
            # Skip if answer is just echoed output or error
            if answer and question and not answer.startswith("> "):
                turns.append({
                    "user_turn": question,
                    "text": answer,
                    "source_message_id": None,
                })
        else:
            i += 1

    return turns


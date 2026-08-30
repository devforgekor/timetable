#!/usr/bin/env python3
# Status: production
# Path: imported by turn_watcher.py — production scripts
"""parser_opencode.py — extract turns from the OpenCode session sqlite DB.

OpenCode persists sessions in a single SQLite DB (~/.local/share/opencode/opencode.db).
The watcher opens it read-only; WAL mode allows concurrent reads while opencode writes.

Schema (read-only views):
  session(messages): id, title, agent, model(JSON), time_updated
  message(messages): id, session_id, time_created, data(JSON role: user|assistant)
  part(message parts): id, message_id, session_id, data(JSON type: text|reasoning|tool|...)

Turn model mirrors the other parsers:
  {user_turn, thinking, text, source_message_id(=user msg id), created_at(ISO)}
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB = Path("/home/opc/.local/share/opencode/opencode.db")
ACTIVE_THRESHOLD_S = 30


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _iso(ms: Optional[int]) -> Optional[str]:
    """epoch ms -> ISO-8601 UTC, or None."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def list_sessions(path: Path = None) -> List[Dict[str, Any]]:
    """Return every session with the fields the watcher needs for checkpointing."""
    con = _connect(path or DEFAULT_DB)
    try:
        rows = con.execute(
            "SELECT s.id, s.title, s.time_updated, "
            "(SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS msg_count "
            "FROM session s ORDER BY s.time_updated ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def session_mtime(path: Path, session_id: str) -> float:
    """session.time_updated (epoch ms) as float seconds — drives mtime skip."""
    con = _connect(path or DEFAULT_DB)
    try:
        row = con.execute(
            "SELECT time_updated FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        return (row["time_updated"] or 0) / 1000.0 if row else 0.0
    finally:
        con.close()


def session_title(path: Path, session_id: str) -> Optional[str]:
    con = _connect(path or DEFAULT_DB)
    try:
        row = con.execute(
            "SELECT title FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        return row["title"] if row else None
    finally:
        con.close()


def _model_of(meta: Optional[str]) -> Optional[str]:
    try:
        m = json.loads(meta or "{}")
    except (TypeError, ValueError):
        return None
    mid = m.get("id") or m.get("modelID")
    return mid or None


def parse(path: Path, session_id: str = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse one OpenCode session. Returns (turns, model, is_active)."""
    con = _connect(path or DEFAULT_DB)
    try:
        meta = con.execute(
            "SELECT id, title, model, time_updated FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if meta is None:
            return None, None, False

        is_active = (time.time() * 1000 - meta["time_updated"]) < ACTIVE_THRESHOLD_S * 1000
        model = _model_of(meta["model"])

        msgs = con.execute(
            "SELECT id, time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created ASC, id ASC",
            (session_id,),
        ).fetchall()

        parts_by_msg: Dict[str, List[Dict]] = {}
        for p in con.execute(
            "SELECT message_id, data FROM part "
            "WHERE session_id = ? ORDER BY message_id ASC, time_created ASC",
            (session_id,),
        ).fetchall():
            try:
                parts_by_msg.setdefault(p["message_id"], []).append(json.loads(p["data"] or "{}"))
            except (TypeError, ValueError):
                pass
    finally:
        con.close()

    turns: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for m in msgs:
        try:
            d = json.loads(m["data"] or "{}")
        except (TypeError, ValueError):
            continue
        role = d.get("role")
        if role == "user":
            if current is not None:
                turns.append(current)
            texts = []
            for p in parts_by_msg.get(m["id"], []):
                if p.get("type") == "text":
                    t = p.get("text", "")
                    if t:
                        texts.append(t)
            user_turn = "\n".join(texts)
            current = {
                "user_turn": user_turn,
                "thinking": None,
                "text": "",
                "source_message_id": m["id"],
                "created_at": _iso(m["time_created"]),
            }
        elif role == "assistant" and current is not None:
            model = d.get("modelID") or model
            for p in parts_by_msg.get(m["id"], []):
                pt = p.get("type")
                if pt == "reasoning":
                    idea = (p.get("text") or "").strip()
                    if idea:
                        current["thinking"] = (
                            current["thinking"] + "\n" + idea
                            if current["thinking"]
                            else idea
                        )
                elif pt == "text":
                    t = p.get("text", "")
                    if t:
                        current["text"] = (
                            current["text"] + "\n" + t if current["text"] else t
                        )

    if current is not None:
        turns.append(current)

    # Active session: drop the last turn if the assistant was still writing
    # (current user message without a completed text answer yet).
    if is_active and turns and not turns[-1]["text"]:
        turns.pop()

    return (turns if turns else None), model, is_active

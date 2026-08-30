#!/usr/bin/env python3
# Status: production
"""Tool-use/tool-result adjacency repair for DeepSeek proxy.

DeepSeek strictly enforces tool_use/tool_result adjacency, which
Claude's auto-compaction can violate. These functions detect and
repair such cases.
"""

import sys
from typing import Any, Dict, List, Set, Tuple


def _fix_orphan_tool_results(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) < 1:
        return payload, False

    def _content_has_tool_use(msg: Any, tid: str) -> bool:
        if msg is None:
            return False
        content = msg.get("content", []) if isinstance(msg, dict) else []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tid:
                    return True
        return False

    def _strip_orphans(content: Any, prev_msg: Any) -> Tuple[Any, bool]:
        if not isinstance(content, list):
            return content, False
        new_content = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid and not _content_has_tool_use(prev_msg, tid):
                    print(
                        f"[anthropic_proxy] dropping orphan tool_result id={tid} "
                        f"(no matching tool_use in previous assistant message)",
                        file=sys.stderr,
                    )
                    changed = True
                    continue
            new_content.append(block)
        return new_content, changed

    changed = False
    new_messages: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            prev_msg = messages[i - 1] if i > 0 else None
            new_content, c = _strip_orphans(msg.get("content", []), prev_msg)
            if c:
                changed = True
                msg = dict(msg)
                msg["content"] = new_content
                if not new_content:
                    print(
                        f"[anthropic_proxy] dropping empty user message at index {i} after orphan tool_result removal",
                        file=sys.stderr,
                    )
                    continue
        new_messages.append(msg)

    if not changed:
        return payload, False

    updated = dict(payload)
    updated["messages"] = new_messages
    return updated, True


def _collect_referenced_tool_use_ids(obj: Any) -> set:
    ids = set()
    if isinstance(obj, dict):
        if obj.get("type") == "tool_result" and "tool_use_id" in obj:
            ids.add(obj["tool_use_id"])
        for v in obj.values():
            ids |= _collect_referenced_tool_use_ids(v)
    elif isinstance(obj, list):
        for it in obj:
            ids |= _collect_referenced_tool_use_ids(it)
    return ids


def _collect_tool_use_ids_present(obj: Any) -> set:
    ids = set()
    if isinstance(obj, dict):
        if obj.get("type") == "tool_use" and "id" in obj:
            ids.add(obj["id"])
        for v in obj.values():
            ids |= _collect_tool_use_ids_present(v)
    elif isinstance(obj, list):
        for it in obj:
            ids |= _collect_tool_use_ids_present(it)
    return ids


def _cleanup(obj: Any, kept_tool_use_ids: set) -> Any:
    if isinstance(obj, dict):
        if obj.get("type") == "tool_result" and "tool_use_id" in obj:
            if obj["tool_use_id"] not in kept_tool_use_ids:
                return None
            return obj
        changed = False
        newd = {}
        for k, v in obj.items():
            cv = _cleanup(v, kept_tool_use_ids)
            if cv is None:
                continue
            newd[k] = cv
            if cv is not v:
                changed = True
        return newd if changed else obj
    if isinstance(obj, list):
        newlist = []
        changed = False
        for it in obj:
            cv = _cleanup(it, kept_tool_use_ids)
            if cv is None:
                changed = True
                continue
            newlist.append(cv)
            if cv is not it:
                changed = True
        return newlist if changed else obj
    return obj


def _prev_has_tool_use(prev_msg: Any, tid: str) -> bool:
    if prev_msg is None:
        return False
    return tid in _collect_tool_use_ids_present(prev_msg)


def _remove_adjacent_orphans(msgs: list) -> list:
    if not msgs:
        return msgs

    def _rem(o, _prev, _idx):
        if isinstance(o, dict):
            if o.get("type") == "tool_result" and "tool_use_id" in o:
                if not _prev_has_tool_use(_prev, o["tool_use_id"]):
                    print(
                        f"[anthropic_proxy] dropping orphan tool_result "
                        f"{o['tool_use_id']} at message index {_idx}",
                        file=sys.stderr,
                    )
                    return None
                return o
            newd = {}
            for k, v in o.items():
                cv = _rem(v, _prev, _idx)
                if cv is None:
                    continue
                newd[k] = cv
            return newd
        if isinstance(o, list):
            nl = []
            for it in o:
                cv = _rem(it, _prev, _idx)
                if cv is None:
                    continue
                nl.append(cv)
            return nl
        return o

    out = []
    for i, m in enumerate(msgs):
        prev = msgs[i - 1] if i > 0 else None
        cleaned = _rem(m, prev, i)
        if cleaned is not None:
            out.append(cleaned)
    return out


def _strict_tool_adjacency_fix(msgs: list) -> list:
    if not msgs:
        return msgs

    changed = False
    out: list = []

    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue

        prev = msgs[i - 1] if i > 0 else None
        prev_has_tool_use = set()
        if isinstance(prev, dict) and prev.get("role") == "assistant":
            prev_has_tool_use = _collect_tool_use_ids_present(prev)

        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid and tid not in prev_has_tool_use:
                    _why = "no preceding message" if prev is None else \
                           f"preceding msg role={prev.get('role')} (expected assistant)" if not isinstance(prev, dict) or prev.get("role") != "assistant" else \
                           f"preceding assistant lacks tool_use id={tid}"
                    print(
                        f"[anthropic_proxy] strict: removing tool_result {tid} at msg[{i}] "
                        f"({_why})",
                        file=sys.stderr,
                    )
                    changed = True
                    continue
            new_content.append(block)

        if changed and not new_content:
            if m.get("role") in ("user", "assistant"):
                new_content = [{"type": "text", "text": "[tool results removed during context repair]"}]
                changed = True
                print(f"[anthropic_proxy] strict: replaced empty {m.get('role')} msg[{i}] with placeholder", file=sys.stderr)
            else:
                changed = True
                print(f"[anthropic_proxy] strict: dropping empty {m.get('role')} msg[{i}]", file=sys.stderr)
                continue

        if changed:
            m = dict(m)
            m["content"] = new_content
        out.append(m)

    if not changed:
        return msgs

    for i, m in enumerate(out):
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            c = m.get("content", [])
            if isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "tool_result":
                out[i] = dict(m)
                out[i]["content"] = [{"type": "text", "text": "[Context restored after truncation]"}] + c
                print(f"[anthropic_proxy] strict: prefixed tool_result-first msg[{i}] with filler", file=sys.stderr)
            break

    return out


def _message_has_nonempty_content(m: Any) -> bool:
    if not isinstance(m, dict):
        return False
    c = m.get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        for el in c:
            if isinstance(el, str) and el.strip():
                return True
            if isinstance(el, dict):
                if (
                    el.get("type") == "text"
                    and isinstance(el.get("text"), str)
                    and el["text"].strip()
                ):
                    return True
                if el.get("type") in ("tool_use", "tool_result"):
                    return True
                if isinstance(el.get("content"), str) and el["content"].strip():
                    return True
                if isinstance(el.get("content"), list) and el["content"]:
                    return True
        return False
    return False

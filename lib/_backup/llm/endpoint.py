#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Raw HTTP client for any OpenAI-compatible endpoint.

Callers pass an explicit endpoint URL.  This module handles:
- TCP keepalive to prevent podman from dropping idle connections
- DeepSeek workaround (rewrite system role to user role)
- HTTPS and HTTP connections

For model-alias-based local pod calls, use lib.llm_client instead.
"""

import http.client
import json
import socket
from typing import Tuple


def _enable_keepalive(sock: socket.socket) -> None:
    """Enable aggressive TCP keepalive to prevent idle connection drops by podman.
    Probes start at 10s idle, every 10s — keeps connection alive during long prompt eval."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 127)
    except (OSError, AttributeError):
        pass


def _normalize_messages(messages: list, endpoint: str) -> list:
    """Convert ``system`` role to ``user`` when targeting DeepSeek API.

    DeepSeek's /v1/chat/completions compatibility layer does not accept
    ``role: "system"`` (allowed values: ``"user"`` or ``"assistant"``).
    Prefix the original content with a marker to preserve the instruction
    semantics.
    """
    if "deepseek" not in endpoint:
        return messages
    out = []
    for m in messages:
        if m.get("role") == "system":
            out.append({"role": "user", "content": "[System instruction] " + m["content"]})
        else:
            out.append(m)
    return out


def call_llm_endpoint(
    endpoint: str,
    messages: list,
    api_key: str = "",
    model: str = "",
    timeout: int = 1200,
    max_tokens: int = 2048,
) -> Tuple[int, dict]:
    """Call any OpenAI-compatible chat completions endpoint. Returns (status, body).

    Uses TCP keepalive to prevent podman from dropping idle connections
    during long prompt evaluation on ARM CPU.

    Note:
        Messages with ``role: "system"`` are rewritten to ``role: "user"``
        when the endpoint belongs to DeepSeek, because the DeepSeek
        compatibility layer only accepts ``"user"`` or ``"assistant"``.
    """
    # -- DeepSeek workaround -----------------------------------------------------------------
    messages = _normalize_messages(messages, endpoint)
    # ---------------------------------------------------------------------------------------

    body = {"messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
    if model:
        body["model"] = model

    data = json.dumps(body).encode()
    u = endpoint
    if u.endswith("/"):
        u = u[:-1]
    path = "/v1/chat/completions"
    base = u
    if u.endswith("/v1/chat/completions"):
        base = u[: -len(path)]
        path = "/v1/chat/completions"

    host = base.replace("https://", "").replace("http://", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    conn = None
    try:
        if endpoint.startswith("https://"):
            import ssl

            conn = http.client.HTTPSConnection(
                host, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, timeout=timeout)

        conn.connect()
        _enable_keepalive(conn.sock)
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        if status == 200:
            return status, json.loads(raw)
        return status, {"error": raw.decode()[:500]}
    except Exception as e:
        return 0, {"error": str(e)}
    finally:
        if conn is not None:
            conn.close()

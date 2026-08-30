#!/usr/bin/env python3
# Status: experimental
# Path: systemd:container-devforge-tg-webhook.service — Telegram callback handler
"""Minimal Telegram webhook for NEUTRAL fact CONFIRM/REJECT buttons.

Runs on :8001 (inference container). Caddy routes /devforge/tg-webhook here.
"""

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import esc_sql, psql_ok


def _load_token() -> str:
    sf = Path.home() / ".config/devforge/secrets.env"
    if sf.exists():
        for line in sf.read_text().split("\n"):
            line = line.strip()
            if line.startswith("TELEGRAM_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


TG_TOKEN = _load_token()
TG_BASE = f"https://api.telegram.org/bot{TG_TOKEN}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default stderr logging

    def _tg_post(self, method: str, data: dict) -> bool:
        try:
            body = json.dumps(data).encode()
            req = urllib.request.Request(
                f"{TG_BASE}/{method}",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            return False

    def do_POST(self):
        if not TG_TOKEN:
            self._respond(500, b'{"ok":false}')
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        try:
            payload = json.loads(body)
        except Exception:
            self._respond(200, b'{"ok":true}')
            return

        cb = payload.get("callback_query")
        if not cb:
            self._respond(200, b'{"ok":true}')
            return

        data = cb.get("data", "")
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        cb_id = cb.get("id")
        msg_id = msg.get("message_id")

        if not data or not chat_id or not cb_id or not msg_id:
            self._respond(200, b'{"ok":true}')
            return

        try:
            action, fact_id = data.split(":", 1)
        except ValueError:
            self._respond(200, b'{"ok":true}')
            return

        if action == "c":
            psql_ok(
                f"UPDATE review_facts SET user_verdict = 'GROUNDED' WHERE id = '{esc_sql(fact_id)}'::uuid"
            )
            verdict = "GROUNDED"
            badge = "✅ GROUNDED"
        elif action == "r":
            psql_ok(
                f"UPDATE review_facts SET user_verdict = 'UNGROUNDED' WHERE id = '{esc_sql(fact_id)}'::uuid"
            )
            verdict = "UNGROUNDED"
            badge = "❌ UNGROUNDED"
        elif action == "nc":
            psql_ok(
                f"UPDATE review_facts SET user_verdict = 'CONFIRM' WHERE id = '{esc_sql(fact_id)}'::uuid"
            )
            badge = "🗑 noise confirmed"
        elif action == "nr":
            psql_ok(
                f"UPDATE review_facts SET user_verdict = 'REJECT' WHERE id = '{esc_sql(fact_id)}'::uuid"
            )
            badge = "🔄 re-extract"
        else:
            self._respond(200, b'{"ok":true}')
            return

        # Answer callback (remove loading spinner)
        self._tg_post(
            "answerCallbackQuery",
            {
                "callback_query_id": cb_id,
                "text": badge,
                "show_alert": False,
            },
        )

        # Update message — append verdict next to the matching line
        lines = (msg.get("text") or "").split("\n")
        updated = []
        for line in lines:
            if f":{fact_id[:12]}" in line and f"→ {badge}" not in line:
                updated.append(f"{line}  →  {badge}")
            else:
                updated.append(line)
        self._tg_post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": "\n".join(updated),
                "parse_mode": "HTML",
            },
        )

        self._respond(200, json.dumps({"ok": True}).encode())

    def _respond(self, code: int, data: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Telegram webhook handler")
    parser.add_argument("--port", type=int, default=8001, help="Listen port")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host")
    args = parser.parse_args()

    svr = HTTPServer((args.host, args.port), Handler)
    print(f"[tg_webhook] Listening on {args.host}:{args.port}", flush=True)
    try:
        svr.serve_forever()
    except KeyboardInterrupt:
        svr.shutdown()


if __name__ == "__main__":
    main()

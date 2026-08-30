#!/usr/bin/env python3
# Status: experimental
"""Slack interactive button handler — receives block_actions and processes fact confirm/reject."""

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

from lib.db import psql_ok, esc_sql, psql_json

SIGNING_SECRET = ""
BOT_TOKEN = ""
SLACK_CHANNEL = ""


def init(secret: str, token: str, channel: str = ""):
    global SIGNING_SECRET, BOT_TOKEN, SLACK_CHANNEL
    SIGNING_SECRET = secret
    BOT_TOKEN = token
    SLACK_CHANNEL = channel


def _verify_signature(timestamp: str, body: str, signature: str) -> bool:
    if not SIGNING_SECRET:
        return True
    basestring = f"v0:{timestamp}:{body}".encode()
    sig = "v0=" + hmac.new(SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, signature)


def _slack_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = Request(
        f"https://slack.com/api/{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [slack] {path} failed: {e}")
        return {"ok": False}


def _confirm_fact(fact_id: str) -> bool:
    fact = psql_json(f"""SELECT rf.id, rf.evidence,
       CASE rf.fact_type
         WHEN 'user' THEN t.user_turn
         WHEN 'thinking' THEN t.thinking
         WHEN 'text' THEN t.text
       END AS source_text,
       rf.fact_type
    FROM review_facts rf
    JOIN turns t ON t.id = rf.turn_id
    WHERE rf.id = '{esc_sql(fact_id)}'""")
    if not fact:
        return False
    if not psql_ok(f"UPDATE review_facts SET user_verdict='CONFIRM', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"):
        return False
    r = fact[0]
    ev = esc_sql(r.get('evidence', ''))
    src = esc_sql(r.get('source_text', ''))
    ft = esc_sql(r.get('fact_type', ''))
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'CONFIRM')""")
    return True


def _reject_fact(fact_id: str) -> bool:
    fact = psql_json(f"""SELECT rf.id, rf.evidence,
       CASE rf.fact_type
         WHEN 'user' THEN t.user_turn
         WHEN 'thinking' THEN t.thinking
         WHEN 'text' THEN t.text
       END AS source_text,
       rf.fact_type
    FROM review_facts rf
    JOIN turns t ON t.id = rf.turn_id
    WHERE rf.id = '{esc_sql(fact_id)}'""")
    if not fact:
        return False
    if not psql_ok(f"UPDATE review_facts SET user_verdict='REJECT', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"):
        return False
    r = fact[0]
    ev = esc_sql(r.get('evidence', ''))
    src = esc_sql(r.get('source_text', ''))
    ft = esc_sql(r.get('fact_type', ''))
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', 'REJECT')""")
    return True


def _skip_turn(turn_id: str) -> bool:
    turn = psql_json(f"""SELECT id::text,
       COALESCE(user_turn, '') AS user_turn,
       COALESCE(text, '') AS text
    FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid""")
    if not turn:
        return False
    r = turn[0]
    ev = esc_sql((r.get('user_turn') or '').strip()[:200])
    src = esc_sql((r.get('text') or '').strip()[:200])
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', 'turn', 'REJECT')
       ON CONFLICT DO NOTHING""")
    return True


def _admit_turn(turn_id: str) -> bool:
    turn = psql_json(f"""SELECT id::text,
       COALESCE(user_turn, '') AS user_turn,
       COALESCE(text, '') AS text
    FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid""")
    if not turn:
        return False
    r = turn[0]
    ev = esc_sql((r.get('user_turn') or '').strip()[:200])
    src = esc_sql((r.get('text') or '').strip()[:200])
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', 'turn', 'CONFIRM')
       ON CONFLICT DO NOTHING""")
    psql_ok(f"UPDATE turns SET pipeline_state = 'scanned' WHERE id = '{esc_sql(turn_id)}'::uuid")
    return True


def _build_resolved_block(original_section: list, verdict: str, fact_id: str) -> dict:
    resolved_text = f"*{':white_check_mark:' if verdict == 'CONFIRM' else ':x:'} {verdict}* ({fact_id[:12]}...)"
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": resolved_text}]}


class SlackActionHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")

        ts = self.headers.get("X-Slack-Request-Timestamp", "")
        sig = self.headers.get("X-Slack-Signature", "")
        if not _verify_signature(ts, body, sig):
            self._respond(401, "invalid signature")
            return

        params = parse_qs(body)
        payload_str = params.get("payload", [None])[0]
        if not payload_str:
            self._respond(400, "missing payload")
            return

        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            self._respond(400, "invalid json")
            return

        if payload.get("type") != "block_actions":
            self._respond(200, "ok")
            return

        actions = payload.get("actions", [])
        if not actions:
            self._respond(200, "ok")
            return

        action = actions[0]
        action_id = action.get("action_id", "")
        value = action.get("value", "")
        fact_id = value.split(":", 1)[-1] if ":" in value else value

        channel = payload.get("channel", {}).get("id", "")
        msg_ts = payload.get("message", {}).get("ts", "")
        original_blocks = payload.get("message", {}).get("blocks", [])

        verdict = None
        if action_id == "fact_confirm":
            if _confirm_fact(fact_id):
                verdict = "CONFIRM"
                print(f"  CONFIRMED {fact_id[:12]}... via Slack")
        elif action_id == "fact_reject":
            if _reject_fact(fact_id):
                verdict = "REJECT"
                print(f"  REJECTED {fact_id[:12]}... via Slack")
        elif action_id == "noise_skip":
            target = value.split(":", 1)[-1]
            if _skip_turn(target):
                verdict = "SKIP"
                print(f"  SKIP turn {target[:12]}... via Slack")
        elif action_id == "noise_admit":
            target = value.split(":", 1)[-1]
            if _admit_turn(target):
                verdict = "ADMIT"
                print(f"  ADMIT turn {target[:12]}... via Slack")

        if verdict and channel and msg_ts:
            updated_blocks = []
            for block in original_blocks:
                if block.get("type") == "actions" and any(
                    e.get("value", "").endswith(fact_id)
                    for e in block.get("elements", [])
                ):
                    updated_blocks.append(
                        _build_resolved_block(block, verdict, fact_id)
                    )
                else:
                    updated_blocks.append(block)
            _slack_post("chat.update", {
                "channel": channel, "ts": msg_ts,
                "text": f"Fact {verdict}: {fact_id[:12]}...",
                "blocks": updated_blocks,
            })

        self._respond(200, "ok")

    def _respond(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

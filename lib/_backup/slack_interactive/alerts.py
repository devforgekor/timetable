#!/usr/bin/env python3
# Status: experimental
"""Slack alert senders — NEUTRAL facts, noise markers, extract fail reports."""

import json
from pathlib import Path

from lib.db import psql_ok, psql_json
from lib.slack_interactive.handler import _slack_post, SLACK_CHANNEL


_EXTRACT_FAIL_REPORT = Path("/var/tmp/extract_fail_report.json")


def send_neutral_alert():
    thought_ids = psql_json(
        "SELECT id::text FROM review_facts "
        "WHERE source='extract_pipeline' AND nli_llm='NEUTRAL' AND user_verdict IS NULL "
        "  AND fact_type = 'thinking' "
        "  AND created_at > now() - interval '24 hours'",
        timeout=10,
    )
    if thought_ids:
        for r in thought_ids:
            psql_ok(
                f"UPDATE review_facts SET user_verdict='REJECT', user_verdict_at=NOW() "
                f"WHERE id = '{r['id']}'::uuid AND user_verdict IS NULL"
            )
        print(f"[slack_interactive] Auto-rejected {len(thought_ids)} thinking facts")

    rows = psql_json(
        "SELECT id::text, evidence, fact_type, "
        "  to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS kst "
        "FROM review_facts "
        "WHERE source='extract_pipeline' AND nli_llm='NEUTRAL' AND user_verdict IS NULL "
        "  AND fact_type IN ('text', 'user') "
        "  AND created_at > now() - interval '24 hours' "
        "ORDER BY created_at DESC LIMIT 10",
        timeout=10,
    )
    if not rows:
        print("[slack_interactive] No pending NEUTRAL facts (text/user)")
        return

    sent = 0
    for r in rows:
        ev = (r.get("evidence") or "")
        ft = r.get("fact_type", "?")
        fid = r["id"]
        kst = r.get("kst", "??")

        src_label = "\uc0ac\uc6a9\uc790 \uba54\uc2dc\uc9c0" if ft == "user" else "AI \uc751\ub2f5"
        explanation = (
            f"*[{ft}]*  _{kst}_  `:{fid[:12]}`\n"
            f"*\ucd9c\ucc98:* {src_label}\uc5d0\uc11c \ucd94\ucd9c\ub41c fact\uc785\ub2c8\ub2e4. evidence\uac00 \uc6d0\ubb38\uacfc \uc77c\uce58\ud558\ub294\uc9c0 \ud655\uc778 \ud6c4 CONFIRM/REJECT\ub97c \uc120\ud0dd\ud558\uc138\uc694.\n\n"
            f"> {ev}"
        )

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": explanation},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "CONFIRM", "emoji": True},
                        "style": "primary",
                        "value": f"c:{fid}",
                        "action_id": "fact_confirm",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "REJECT", "emoji": True},
                        "style": "danger",
                        "value": f"r:{fid}",
                        "action_id": "fact_reject",
                    },
                ],
            },
        ]
        result = _slack_post("chat.postMessage", {
            "channel": SLACK_CHANNEL,
            "text": f"NEUTRAL [{ft}] {ev[:80]}",
            "blocks": blocks,
        })
        if result.get("ok"):
            sent += 1
        else:
            print(f"[slack_interactive] Failed: {fid[:12]} - {result.get('error', '?')}")

    print(f"[slack_interactive] Sent: {sent}/{len(rows)} NEUTRAL facts individually")


def send_noise_alert():
    rows = psql_json(
        "SELECT id::text, turn_id::text, "
        "  substring(t.text, 1, 200) AS turn_preview, "
        "  substring(t.user_turn, 1, 200) AS user_preview, "
        "  to_char(rf.created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS kst "
        "FROM review_facts rf "
        "JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.fact_type = 'noise_marker' AND rf.user_verdict IS NULL "
        "  AND rf.telegram_notified_at IS NULL "
        "  AND rf.created_at > now() - interval '24 hours' "
        "ORDER BY rf.created_at DESC LIMIT 10",
        timeout=10,
    )
    if not rows:
        print("[slack_interactive] No pending noise markers")
        return

    sent = 0
    for r in rows:
        preview = r.get("turn_preview") or r.get("user_preview", "") or ""
        tid = r["turn_id"]
        kst = r.get("kst", "??")
        explanation = (
            f"*[NOISE]*  _{kst}_  `:{tid[:12]}`\n"
            f"*\uc548\ub0b4:* LLM\uc774 \uc774 turn\uc744 noise(gibberish/API error)\ub85c \ud310\ub2e8\ud588\uc2b5\ub2c8\ub2e4. "
            f"\uc2e4\uc81c\ub85c \uc758\ubbf8 \uc5c6\ub294 \uba54\uc2dc\uc9c0\uba74 Skip, \uc758\ubbf8 \uc788\ub294 \ub0b4\uc6a9\uc774\uba74 Admit(\uc7ac\ucd94\ucd9c)\uc744 \uc120\ud0dd\ud558\uc138\uc694.\n\n"
            f"> {preview[:200]}"
        )
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": explanation},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Skip (noise)", "emoji": True},
                        "style": "danger",
                        "value": f"s:{tid}",
                        "action_id": "noise_skip",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Admit (\uc7ac\ucd94\ucd9c)", "emoji": True},
                        "style": "primary",
                        "value": f"a:{tid}",
                        "action_id": "noise_admit",
                    },
                ],
            },
        ]
        result = _slack_post("chat.postMessage", {
            "channel": SLACK_CHANNEL,
            "text": f"Noise: {preview[:80]}",
            "blocks": blocks,
        })
        if result.get("ok"):
            sent += 1
        else:
            print(f"[slack_interactive] Noise send failed: {tid[:12]} - {result.get('error', '?')}")

    if sent > 0:
        for r in rows:
            psql_ok(
                f"UPDATE review_facts SET telegram_notified_at = NOW() "
                f"WHERE id = '{r['id']}'::uuid AND telegram_notified_at IS NULL"
            )

    print(f"[slack_interactive] Sent: {sent}/{len(rows)} noise markers individually")


def send_extract_fail_alert():
    if not _EXTRACT_FAIL_REPORT.exists():
        print("[slack_interactive] No extract fail report found")
        return

    report = json.loads(_EXTRACT_FAIL_REPORT.read_text())
    fails = report.get("failures", [])
    noise = report.get("noise", [])
    if not fails and not noise:
        print("[slack_interactive] Extract fail report empty")
        return

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Extract \uacb0\uacfc: {len(fails)}\uac74 \uc2e4\ud328 / {len(noise)}\uac74 noise", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\uc2e4\ud328\ud55c turn\ub4e4\uc744 \uac80\ud1a0 \ud6c4 Skip(noise)/Admit(\uc7ac\ucd94\ucd9c)\uc744 \uc120\ud0dd\ud558\uc138\uc694.\nAdmit \uc120\ud0dd \uc2dc \ub2e4\uc74c cycle\uc5d0\uc11c \uc7ac\ucd94\ucd9c\ub429\ub2c8\ub2e4.",
            },
        },
        {"type": "divider"},
    ]

    for entry in (fails + noise)[:8]:
        tid = entry.get("turn_id", "?")
        reason = entry.get("reason", "?")
        preview = entry.get("preview", "")[:100]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*[`{tid[:12]}`]* {reason}\n> {preview}"},
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Skip (noise)", "emoji": True},
                    "style": "danger",
                    "value": f"s:{tid}",
                    "action_id": "noise_skip",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Admit (\uc7ac\ucd94\ucd9c)", "emoji": True},
                    "style": "primary",
                    "value": f"a:{tid}",
                    "action_id": "noise_admit",
                },
            ],
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"\ucd1d {len(fails)+len(noise)}\uac74 \uc911 \uc0c1\uc704 8\uac1c \ud45c\uc2dc | \uc2e4\ud328 \ubcf4\uace0\uc11c: {_EXTRACT_FAIL_REPORT}"}],
    })

    _slack_post("chat.postMessage", {
        "channel": SLACK_CHANNEL,
        "text": f"Extract: {len(fails)} failed, {len(noise)} noise",
        "blocks": blocks,
    })

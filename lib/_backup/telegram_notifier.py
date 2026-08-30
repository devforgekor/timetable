#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh:380, mcp_server.py — Telegram notification tools
"""Telegram notifier for pipeline alerts.

Sends pending NEUTRAL facts to Telegram grouped by turn, with InlineKeyboardMarkup.
Dedup via telegram_notified_at.

CLI: python3 lib/telegram_notifier.py --send-neutral
"""

import json
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok
from telegram_send import send_text


def send_neutral_alert() -> bool:
    """Send one Telegram message per turn with NEUTRAL facts + inline buttons."""
    rows = psql_json(
        "SELECT rf.id::text, left(rf.evidence, 200) AS evidence, rf.fact_type, "
        "  rf.turn_id::text, "
        "  left(t.text, 80) AS turn_preview, "
        "  to_char(rf.created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS kst "
        "FROM review_facts rf "
        "JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.source='extract_pipeline' AND rf.nli_llm='NEUTRAL' AND rf.user_verdict IS NULL "
        "  AND rf.nli_verdict = 'AMBIGUOUS' "
        "  AND rf.telegram_notified_at IS NULL "
        "  AND rf.created_at > now() - interval '24 hours' "
        "ORDER BY rf.created_at DESC",
        timeout=10,
    )
    if not rows:
        return False

    # Group by turn_id
    by_turn: dict = {}
    for r in rows:
        tid = r["turn_id"]
        if tid not in by_turn:
            by_turn[tid] = {"preview": r.get("turn_preview", "")[:60], "facts": []}
        by_turn[tid]["facts"].append(r)

    sent_count = 0
    for turn_id, group in by_turn.items():
        facts = group["facts"]
        preview = group["preview"]
        kst = facts[0].get("kst", "??")

        msg_lines = []
        if preview:
            msg_lines.append(f"`{preview}`")
        msg_lines.append(f"[NEUTRAL] Turn {turn_id[:8]}  _{kst}_")
        msg_lines.append("")
        msg_lines.append(f"{len(facts)}건:")

        for f in facts:
            ev = (f.get("evidence") or "")[:100]
            ft = f.get("fact_type", "?")
            fid = f["id"][:12]
            msg_lines.append(f"• [{ft}] {ev}")

        text = "\n".join(msg_lines)

        # Inline buttons per fact
        inline_keyboard = []
        for f in facts:
            fid = f["id"]
            inline_keyboard.append([
                {"text": f"✅ {f.get('fact_type','?')}", "callback_data": f"c:{fid}"},
                {"text": f"❌ {f.get('fact_type','?')}", "callback_data": f"r:{fid}"},
            ])

        ok = send_text(text, reply_markup={"inline_keyboard": inline_keyboard})
        if not ok:
            print(f"[telegram_notifier] Failed to send turn {turn_id[:8]}", flush=True)
            continue

        # Mark sent
        for f in facts:
            psql_ok(
                f"UPDATE review_facts SET telegram_notified_at = NOW() "
                f"WHERE id = '{f['id']}'::uuid AND telegram_notified_at IS NULL"
            )
        sent_count += 1

    print(f"[telegram_notifier] Sent: {sent_count}/{len(by_turn)} turns ({len(rows)} facts)")
    return sent_count > 0


def send_noise_alert() -> bool:
    """Send pending noise markers to Telegram with confirm/reject buttons."""
    rows = psql_json(
        "SELECT rf.id::text, rf.turn_id::text, "
        "  substring(t.text, 1, 80) AS turn_preview, "
        "  substring(t.user_turn, 1, 80) AS user_preview, "
        "  to_char(rf.created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS kst "
        "FROM review_facts rf "
        "JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.fact_type = 'noise_marker' AND rf.user_verdict IS NULL "
        "  AND rf.telegram_notified_at IS NULL "
        "  AND rf.created_at > now() - interval '24 hours' "
        "ORDER BY rf.created_at DESC",
        timeout=10,
    )
    if not rows:
        return False

    sent_count = 0
    for r in rows:
        preview = r.get("turn_preview") or r.get("user_preview", "") or ""
        kst = r.get("kst", "??")
        fid = r["id"][:12]

        msg_lines = [
            f"`{preview[:60]}`",
            f"[NOISE] Turn {r['turn_id'][:8]}  _{kst}_",
            "",
            "LLM 판단: 이 턴은 noise(gibberish/API error)입니다.",
            "아니라면 re-extract 후 다음 cycle에 포함합니다.",
        ]
        text = "\n".join(msg_lines)

        inline_keyboard = [[
            {"text": "noise (skip)", "callback_data": f"nc:{r['id']}"},
            {"text": "fact (re-extract)", "callback_data": f"nr:{r['id']}"},
        ]]

        ok = send_text(text, reply_markup={"inline_keyboard": inline_keyboard})
        if not ok:
            print(f"[telegram_notifier] Noise send failed: turn {r['turn_id'][:8]}", flush=True)
            continue

        psql_ok(
            f"UPDATE review_facts SET telegram_notified_at = NOW() "
            f"WHERE id = '{r['id']}'::uuid AND telegram_notified_at IS NULL"
        )
        sent_count += 1

    print(f"[telegram_notifier] Noise sent: {sent_count}/{len(rows)}")
    return sent_count > 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Telegram notifier for pipeline alerts")
    parser.add_argument("--send-neutral", action="store_true", help="Send pending NEUTRAL facts by turn")
    parser.add_argument("--send-noise", action="store_true", help="Send pending noise markers")
    args = parser.parse_args()

    if args.send_neutral:
        send_neutral_alert()
    elif args.send_noise:
        send_noise_alert()
    else:
        parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()

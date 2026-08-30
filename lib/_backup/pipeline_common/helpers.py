#!/usr/bin/env python3
# Status: production
"""Helper functions for pipeline_common."""

import json
import sys
import urllib.request
from pathlib import Path

from lib.common import log


_SF = Path.home() / ".config/devforge/secrets.env"
_SLACK_TOKEN = ""
_SLACK_CHANNEL = ""
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "SLACK_BOT_TOKEN":
                _SLACK_TOKEN = _v.strip().strip('"').strip("'")
            elif _k.strip() == "SLACK_CHANNEL":
                _SLACK_CHANNEL = _v.strip().strip('"').strip("'")


def slack_send(text):
    if not _SLACK_TOKEN:
        return
    payload = json.dumps({"channel": _SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {_SLACK_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log(f"Slack API error: {result.get('error', '?')}")
    except Exception as e:
        log(f"Slack send failed: {e}")


def abort(phase, label, detail):
    log(f"\n{'='*60}")
    log(f"!! ABORT: {phase} — {label}")
    log(f"!! Detail: {detail}")
    log(f"{'='*60}")
    slack_send(
        f":no_entry: *P-R-J experiment aborted* — {phase}\n"
        f"> {label}: {detail[:200]}"
    )
    sys.exit(1)


def strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        elif text.endswith("``"):
            text = text[:-2].rstrip()
    return text.strip()

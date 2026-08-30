#!/usr/bin/env python3
# Status: experimental
"""Slack Interactive Handler — receives Slack block_actions and processes NEUTRAL fact confirm/reject."""

import os
import sys
from http.server import HTTPServer

from lib.slack_interactive.handler import SlackActionHandler, init, SLACK_CHANNEL
from lib.slack_interactive.alerts import send_neutral_alert, send_noise_alert, send_extract_fail_alert

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, SCRIPTS_DIR)

PORT = 8087

_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
_SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "U0APJGD8CBW")

init(_SIGNING_SECRET, _BOT_TOKEN, _SLACK_CHANNEL)


def run_server():
    server = HTTPServer(("127.0.0.1", PORT), SlackActionHandler)
    print(f"[slack_interactive] Listening on :{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[slack_interactive] Shutting down")
        server.server_close()


if __name__ == "__main__":
    if "--send-alert" in sys.argv:
        send_neutral_alert()
    elif "--send-noise-alert" in sys.argv:
        send_noise_alert()
    elif "--send-extract-fail" in sys.argv:
        send_extract_fail_alert()
    else:
        run_server()

#!/usr/bin/env python3.11
# Status: experimental
# Path: lib/notify.py — imported by app.py, telegram_send.py, mcp_server.py
"""Unified notification module — Apprise (Telegram, Email) + native Slack.

Usage:
    from lib.notify import Notifier
    n = Notifier(secrets_dict)
    n.send_all("Title", "Body")       # Telegram + Email
    n.send_telegram("Message")         # Telegram only
    n.send_slack("C123", "Text")       # Slack channel/user DM
"""

import json
import logging
import urllib.request
from typing import Any

import apprise

logger = logging.getLogger("devforge-notify")


class Notifier:
    """Apprise-based notification hub.

    Telegram and Email use Apprise (unified, extensible).
    Slack uses native API because Apprise's slack plugin has a bug:
    it passes @user_id to Slack API as-is (e.g. "@U123") instead of
    stripping the "@" prefix, which causes channel_not_found.
    The "#channel" and "+encoded_id" modes work fine, but we need DM
    for interactive confirm/reject buttons, so native API it is.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self._apprise = self._build_apprise()

    # ── Apprise (Telegram + Email) ──────────────────────────────

    def _build_apprise(self) -> apprise.Apprise:
        a_obj = apprise.Apprise()

        # Telegram
        tg_token = self._secrets.get("TELEGRAM_TOKEN", "")
        tg_chat = self._secrets.get("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            a_obj.add(f"tgram://{tg_token}/{tg_chat}")
            logger.info("notify: Telegram loaded")

        # Email (Gmail SMTP via SSL)
        smtp_user = self._secrets.get("SMTP_USER", "")
        smtp_pass = self._secrets.get("SMTP_PASSWORD", "").replace(" ", "%20")
        if smtp_user and smtp_pass:
            a_obj.add(
                f"mailto://{smtp_user}:{smtp_pass}@smtp.gmail.com:465?from={smtp_user}&mode=ssl"
            )
            logger.info("notify: Email loaded")

        return a_obj

    def send_all(self, title: str, body: str, body_format: int = apprise.NotifyFormat.TEXT) -> bool:
        """Send to all Apprise-configured channels (Telegram + Email)."""
        return self._apprise.notify(title=title, body=body, body_format=body_format)

    def send_telegram(self, text: str) -> bool:
        """Send a text message via Telegram only."""
        return self._apprise.notify(title="", body=text)

    # ── Slack (native) ──────────────────────────────────────────

    def send_slack(self, channel: str, text_or_data: Any) -> bool:
        """Send a Slack message via chat.postMessage.

        Args:
            channel: Slack channel/user ID or "chat.update" for update.
            text_or_data: Plain text string or dict payload for chat.update.
        """
        bot_token = self._secrets.get("SLACK_BOT_TOKEN", "")
        if not bot_token:
            logger.error("No SLACK_BOT_TOKEN, cannot send Slack")
            return False

        if isinstance(text_or_data, str):
            payload_data = {"channel": channel, "text": text_or_data, "mrkdwn": True}
        else:
            payload_data = text_or_data

        payload = json.dumps(payload_data).encode()
        api_url = (
            "https://slack.com/api/chat.update"
            if channel == "chat.update"
            else "https://slack.com/api/chat.postMessage"
        )
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    logger.error("Slack API error: %s", result.get("error", "?"))
                    return False
                return True
        except Exception as e:
            logger.error("Slack post failed: %s", e)
            return False

    # ── Convenience ─────────────────────────────────────────────

    def slack_post(self, channel: str, text: str) -> bool:
        """Alias: send plain text to a Slack channel."""
        return self.send_slack(channel, text)

    def slack_update(self, channel: str, ts: str, text: str, blocks: list) -> bool:
        """Update a Slack message (chat.update)."""
        return self.send_slack(
            "chat.update",
            {
                "channel": channel,
                "ts": ts,
                "text": text,
                "blocks": blocks,
            },
        )

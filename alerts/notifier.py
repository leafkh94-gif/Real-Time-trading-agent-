"""
Telegram and null notifiers.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._token   = token
        self._chat_id = chat_id

    def send_html(self, html: str) -> None:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": html, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning("Telegram HTTP %d: %s", r.status_code, r.text[:200])
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)

    def send(self, text: str) -> None:
        self.send_html(text)


class NullNotifier:
    def send_html(self, html: str) -> None:
        logger.info("[NullNotifier] %s", html[:300])

    def send(self, text: str) -> None:
        logger.info("[NullNotifier] %s", text[:300])

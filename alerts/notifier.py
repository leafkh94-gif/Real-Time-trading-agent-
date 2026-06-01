"""
Abstract notification interface with Telegram and ntfy.sh implementations.
Telegram messages are stored on Telegram servers — use only for non-sensitive alerts.
For private alerts (balances, account data) prefer ntfy.sh or a self-hosted webhook.
"""
import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, message: str) -> None: ...


def _escape_html(text: str) -> str:
    """Escape the three HTML-special characters required by Telegram HTML mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramNotifier(Notifier):
    """
    Sends plain-text alerts to a Telegram chat via the Bot API (HTML mode).

    Plain text passed to send() is auto-escaped so characters like <, >, &
    never break the message. For pre-formatted HTML (bold, code blocks, etc.)
    call send_html() directly.

    Handles HTTP 429 by honouring the retry_after value from the response body
    (up to _MAX_RETRIES attempts). Failures are logged but never propagate —
    a notification error must never block or crash trade execution.
    """

    _MAX_RETRIES = 3
    _RETRY_BASE_S = 1.0

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, message: str) -> None:
        self.send_html(_escape_html(message))

    def send_html(self, html: str) -> None:
        """Send a pre-formatted HTML message. Caller is responsible for escaping."""
        import requests

        payload = {
            "chat_id": self._chat_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(self._MAX_RETRIES):
            try:
                r = requests.post(self._url, json=payload, timeout=10)
                if r.status_code == 429:
                    retry_after = (
                        r.json().get("parameters", {}).get("retry_after", self._RETRY_BASE_S)
                    )
                    logger.warning("Telegram 429 — waiting %ss before retry", retry_after)
                    time.sleep(float(retry_after) + 0.1)
                    continue
                if r.status_code != 200:
                    logger.warning("Telegram alert HTTP %s: %s", r.status_code, r.text[:200])
                return
            except Exception as exc:
                logger.warning("Telegram alert failed (attempt %d/%d): %s",
                               attempt + 1, self._MAX_RETRIES, exc)
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(self._RETRY_BASE_S)


class NtfyNotifier(Notifier):
    """
    ntfy.sh notifier. Self-hostable — preferred for private alerts.
    topic_url: e.g. https://ntfy.sh/your-private-topic  or  http://your-host/topic
    """

    def __init__(self, topic_url: str, token: str | None = None):
        self._url = topic_url
        self._token = token

    def send(self, message: str) -> None:
        import urllib.request

        headers = {"Content-Type": "text/plain"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        req = urllib.request.Request(
            self._url,
            data=message.encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201):
                    logger.warning("ntfy alert failed: HTTP %s", resp.status)
        except Exception as exc:
            logger.warning("ntfy alert failed: %s", exc)


class NullNotifier(Notifier):
    """No-op notifier for testing and development."""

    def send(self, message: str) -> None:
        logger.debug("NullNotifier: %s", message)


def build_notifier() -> Notifier:
    """
    Build the appropriate notifier from secrets. Falls back to NullNotifier
    if no alert credentials are configured.
    """
    from config import secrets

    try:
        token = secrets.get("TELEGRAM_BOT_TOKEN")
        chat_id = secrets.get("TELEGRAM_CHAT_ID")
        return TelegramNotifier(token, chat_id)
    except RuntimeError:
        pass

    logger.warning(
        "No alert credentials configured — using NullNotifier. "
        "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or configure ntfy.sh."
    )
    return NullNotifier()

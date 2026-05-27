"""
Abstract notification interface with Telegram and ntfy.sh implementations.
Telegram messages are stored on Telegram servers — use only for non-sensitive alerts.
For private alerts (balances, account data) prefer ntfy.sh or a self-hosted webhook.
"""
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, message: str) -> None: ...


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id

    def send(self, message: str) -> None:
        import urllib.request
        import urllib.parse
        import json

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = json.dumps({"chat_id": self._chat_id, "text": message}).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("Telegram alert failed: HTTP %s", resp.status)
        except Exception as exc:
            logger.warning("Telegram alert failed: %s", exc)


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

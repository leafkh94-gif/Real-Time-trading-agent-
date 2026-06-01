"""
Logging filter that redacts known secret values before any write to disk or handlers.
Uses value-based redaction so secrets are caught even without a label prefix.
"""
import logging
from pathlib import Path

# Substring patterns (matched against the UPPERCASED key name) that mark a value
# as sensitive. Anything matching has its value redacted from logs. Non-matching
# config (e.g. ENVIRONMENT, AWS_REGION) is left intact so logs stay readable.
_SENSITIVE_KEY_PATTERNS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE", "ACCOUNT",
)


def _is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    if upper.endswith("_ID") or upper == "ID":
        return True
    return any(pattern in upper for pattern in _SENSITIVE_KEY_PATTERNS)


def sensitive_values(secrets: dict[str, str]) -> list[str]:
    """
    Select only the values whose key names look sensitive. This catches every real
    secret (BROKER_API_KEY, *_SECRET, *_TOKEN, *_ACCOUNT_ID, *_CHAT_ID, ...) while
    avoiding over-redaction of harmless config values that would otherwise turn
    debugging output into a wall of ***REDACTED***.
    """
    return [v for k, v in secrets.items() if _is_sensitive_key(k)]


class SecretRedactor(logging.Filter):
    REDACTED = "***REDACTED***"

    def __init__(self, secret_values: list[str]):
        super().__init__()
        # Only redact values long enough to be meaningful secrets (avoids clobbering short env vars)
        self._secrets = sorted(
            [s for s in secret_values if s and len(s) > 6],
            key=len,
            reverse=True,  # longest first so substring matches don't partially redact
        )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, self.REDACTED)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> None:
    """
    Configures the root logger with a sanitizing file handler.
    Call once at process startup, before any other logging calls.
    """
    from config import secrets as _secrets

    Path(log_dir).mkdir(exist_ok=True)

    redactor = SecretRedactor(sensitive_values(_secrets.get_secrets()))

    root = logging.getLogger()
    root.setLevel(level)

    # File handler — sanitized
    file_handler = logging.FileHandler(
        f"{log_dir}/bot.log", encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # Console handler — also sanitized
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    console_handler.addFilter(redactor)
    root.addHandler(console_handler)

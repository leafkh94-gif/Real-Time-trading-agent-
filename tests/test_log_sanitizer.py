"""Tests for core/log_sanitizer.py"""
import logging
import pytest
from core.log_sanitizer import SecretRedactor, sensitive_values, _is_sensitive_key


def make_logger(secrets: list[str]) -> tuple[logging.Logger, list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(f"test_{id(records)}")
    logger.setLevel(logging.DEBUG)
    cap = Capture()
    cap.addFilter(SecretRedactor(secrets))
    logger.addHandler(cap)
    logger.propagate = False
    return logger, records


# ── Redaction ─────────────────────────────────────────────────────────────────

def test_secret_is_redacted():
    logger, records = make_logger(["supersecretkey123"])
    logger.info("connecting with key supersecretkey123")
    assert "supersecretkey123" not in records[-1].getMessage()
    assert "***REDACTED***" in records[-1].getMessage()


def test_multiple_secrets_all_redacted():
    logger, records = make_logger(["key_abc123456", "token_xyz789012"])
    logger.info("key=%s token=%s", "key_abc123456", "token_xyz789012")
    msg = records[-1].getMessage()
    assert "key_abc123456" not in msg
    assert "token_xyz789012" not in msg
    assert msg.count("***REDACTED***") == 2


def test_non_secret_text_passes_through():
    logger, records = make_logger(["secretvalue12345"])
    logger.info("opening XAUUSD long at 2300.50")
    assert records[-1].getMessage() == "opening XAUUSD long at 2300.50"


def test_short_values_not_redacted():
    """Values ≤ 6 chars are excluded to avoid clobbering env vars like 'INFO'."""
    logger, records = make_logger(["abc"])
    logger.info("value abc is here")
    assert "abc" in records[-1].getMessage()


def test_secret_embedded_in_url():
    """Catches a secret even when it appears inside a longer string."""
    secret = "my_secret_token_abc123"
    logger, records = make_logger([secret])
    logger.info("https://api.example.com/auth?token=%s&foo=bar", secret)
    assert secret not in records[-1].getMessage()
    assert "***REDACTED***" in records[-1].getMessage()


def test_empty_secrets_list():
    logger, records = make_logger([])
    logger.info("hello world")
    assert records[-1].getMessage() == "hello world"


def test_none_values_in_list_ignored():
    logger, records = make_logger([None, "", "realkey12345678"])  # type: ignore[list-item]
    logger.info("key is realkey12345678")
    assert "realkey12345678" not in records[-1].getMessage()


def test_longest_secret_matched_first():
    """Prevent partial replacement when one secret is a prefix of another."""
    short = "abc1234567"
    long_secret = "abc1234567extra"
    logger, records = make_logger([short, long_secret])
    logger.info("secret is %s", long_secret)
    msg = records[-1].getMessage()
    # Should be fully redacted, not partially
    assert long_secret not in msg
    assert "***REDACTED***" in msg


# ── Sensitive-key selection (avoids over-redacting config) ────────────────────

@pytest.mark.parametrize("key", [
    "BROKER_API_KEY", "BROKER_API_SECRET", "BROKER_ACCOUNT_ID",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DB_PASSWORD", "AWS_CREDENTIAL",
])
def test_sensitive_keys_detected(key):
    assert _is_sensitive_key(key) is True


@pytest.mark.parametrize("key", ["ENVIRONMENT", "AWS_REGION", "LOG_LEVEL", "VALIDATION"])
def test_non_sensitive_keys_ignored(key):
    assert _is_sensitive_key(key) is False


def test_sensitive_values_filters_config():
    secrets = {
        "BROKER_API_KEY": "sk_live_abc1234567",
        "TELEGRAM_BOT_TOKEN": "telegram_tok_987654",
        "ENVIRONMENT": "development",
        "AWS_REGION": "us-east-1",
    }
    values = sensitive_values(secrets)
    assert "sk_live_abc1234567" in values
    assert "telegram_tok_987654" in values
    # Non-secret config must NOT be redacted, so it must NOT be in the value list
    assert "development" not in values
    assert "us-east-1" not in values


def test_config_value_not_redacted_end_to_end():
    """Regression: harmless config like 'us-east-1' stays readable in logs."""
    secrets = {"BROKER_API_KEY": "sk_live_realsecret99", "AWS_REGION": "us-east-1"}
    logger, records = make_logger(sensitive_values(secrets))
    logger.info("region us-east-1 connecting with sk_live_realsecret99")
    msg = records[-1].getMessage()
    assert "us-east-1" in msg                 # config preserved
    assert "sk_live_realsecret99" not in msg  # secret redacted
    assert "***REDACTED***" in msg

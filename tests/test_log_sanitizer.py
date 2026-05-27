"""Tests for core/log_sanitizer.py"""
import logging
import pytest
from core.log_sanitizer import SecretRedactor


@pytest.fixture
def handler():
    """In-memory log handler to capture records."""
    h = logging.handlers.MemoryHandler(capacity=100, flushLevel=logging.CRITICAL)
    return h


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


# logging.handlers import needed for MemoryHandler in the fixture hint
import logging.handlers

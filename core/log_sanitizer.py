"""
Logging setup — strips secrets from log output via a sanitising filter.
"""
from __future__ import annotations

import logging
import os
import re


class _SecretFilter(logging.Filter):
    """Redact Capital.com credentials if they appear in log messages."""
    _PATTERNS = [
        re.compile(r'(password["\']?\s*[:=]\s*)[^\s,}"]+', re.I),
        re.compile(r'(X-CAP-API-KEY["\']?\s*[:=]\s*)[^\s,}"]+', re.I),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in self._PATTERNS:
            msg = pat.sub(r'\1***', msg)
        record.msg  = msg
        record.args = ()
        return True


def setup_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().addFilter(_SecretFilter())

"""Tests for config/secrets.py"""
import os
import pytest
from unittest.mock import patch
from functools import lru_cache


def fresh_secrets():
    """Import secrets with a cleared lru_cache so each test is isolated."""
    import config.secrets as s
    s.get_secrets.cache_clear()
    return s


def test_get_returns_env_var(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MY_TEST_KEY_ABC", "test_value_123")
    s = fresh_secrets()
    assert s.get("MY_TEST_KEY_ABC") == "test_value_123"


def test_get_raises_on_missing_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    s = fresh_secrets()
    s.get_secrets.cache_clear()
    with pytest.raises(RuntimeError, match="Missing secret"):
        s.get("DEFINITELY_NOT_SET_KEY_XYZ_9999")


def test_get_secrets_returns_dict(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    s = fresh_secrets()
    result = s.get_secrets()
    assert isinstance(result, dict)


def test_production_env_calls_ssm(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = fresh_secrets()
    s.get_secrets.cache_clear()

    with patch.object(s, "_load_ssm", return_value={"FOO": "bar"}) as mock_ssm:
        result = s.get_secrets()
        mock_ssm.assert_called_once()
        assert result == {"FOO": "bar"}


def test_development_env_calls_local(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    s = fresh_secrets()
    s.get_secrets.cache_clear()

    with patch.object(s, "_load_local", return_value={"BAR": "baz"}) as mock_local:
        result = s.get_secrets()
        mock_local.assert_called_once()
        assert result == {"BAR": "baz"}

"""
Tests for ClaudeSignalFilter.

All tests mock the Anthropic client so no real API calls are made.
We verify prompt construction, response parsing, and error fallback.
"""
import math
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from execution.models import Signal
from strategy.base import Candle
from strategy.claude_filter import ClaudeSignalFilter, _build_prompt


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flat_candles(n: int, price: float = 2300.0) -> list[Candle]:
    return [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00Z",
               open=price, high=price + 2, low=price - 2, close=price)
        for i in range(n)
    ]


def _make_text_block(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_thinking_block():
    block = MagicMock()
    block.type = "thinking"
    return block


def _mock_client(response_text: str):
    """Return a mocked Anthropic client whose messages.create() returns response_text."""
    client = MagicMock()
    response = MagicMock()
    response.content = [_make_thinking_block(), _make_text_block(response_text)]
    client.messages.create.return_value = response
    return client


# ── _build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_no_candles():
    sig = Signal(direction="buy", lots=0.05)
    prompt = _build_prompt(sig, [])
    assert "BUY" in prompt
    assert "No price history" in prompt


def test_build_prompt_includes_direction_and_lots():
    sig = Signal(direction="sell", lots=0.10)
    prompt = _build_prompt(sig, _flat_candles(30))
    assert "SELL" in prompt
    assert "0.10" in prompt


def test_build_prompt_includes_price_fields():
    candles = _flat_candles(30)
    sig = Signal(direction="buy", lots=0.05)
    prompt = _build_prompt(sig, candles)
    assert "EMA-20" in prompt
    assert "ATR-14" in prompt
    assert "Last 5 closes" in prompt


def test_build_prompt_few_candles_no_crash():
    sig = Signal(direction="buy", lots=0.05)
    prompt = _build_prompt(sig, _flat_candles(3))
    assert "BUY" in prompt  # at minimum must contain the signal


# ── ClaudeSignalFilter.accept() ───────────────────────────────────────────────

def _filter_with_response(text: str, fallback: bool = True) -> ClaudeSignalFilter:
    flt = ClaudeSignalFilter.__new__(ClaudeSignalFilter)
    flt._client = _mock_client(text)
    flt._model = "claude-opus-4-8"
    flt._fallback_accept = fallback
    return flt


def test_accept_returns_true_on_accept_response():
    flt = _filter_with_response("ACCEPT")
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is True


def test_accept_returns_false_on_reject_response():
    flt = _filter_with_response("REJECT")
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is False


def test_accept_case_insensitive_accept():
    flt = _filter_with_response("accept")
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is True


def test_accept_case_insensitive_reject():
    flt = _filter_with_response("reject")
    assert flt.accept(Signal("sell", 0.05), _flat_candles(30)) is False


def test_accept_with_thinking_block_uses_last_text_block():
    """Thinking blocks must be ignored; decision comes from the text block."""
    flt = _filter_with_response("REJECT")
    result = flt.accept(Signal("buy", 0.05), _flat_candles(30))
    assert result is False


# ── Fallback on error ─────────────────────────────────────────────────────────

def test_api_error_falls_back_to_true_by_default():
    import anthropic as _anthropic
    flt = ClaudeSignalFilter.__new__(ClaudeSignalFilter)
    flt._client = MagicMock()
    flt._model = "claude-opus-4-8"
    flt._fallback_accept = True
    flt._client.messages.create.side_effect = _anthropic.APIConnectionError(request=MagicMock())
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is True


def test_api_error_falls_back_to_false_when_configured():
    import anthropic as _anthropic
    flt = ClaudeSignalFilter.__new__(ClaudeSignalFilter)
    flt._client = MagicMock()
    flt._model = "claude-opus-4-8"
    flt._fallback_accept = False
    flt._client.messages.create.side_effect = _anthropic.APIConnectionError(request=MagicMock())
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is False


def test_unexpected_error_falls_back():
    flt = ClaudeSignalFilter.__new__(ClaudeSignalFilter)
    flt._client = MagicMock()
    flt._model = "claude-opus-4-8"
    flt._fallback_accept = True
    flt._client.messages.create.side_effect = ValueError("unexpected")
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is True


def test_no_text_block_falls_back():
    flt = ClaudeSignalFilter.__new__(ClaudeSignalFilter)
    flt._client = MagicMock()
    flt._model = "claude-opus-4-8"
    flt._fallback_accept = True
    response = MagicMock()
    response.content = [_make_thinking_block()]  # no text block
    flt._client.messages.create.return_value = response
    assert flt.accept(Signal("buy", 0.05), _flat_candles(30)) is True


# ── API call parameters ───────────────────────────────────────────────────────

def test_messages_create_called_with_correct_model():
    flt = _filter_with_response("ACCEPT")
    flt.accept(Signal("buy", 0.05), _flat_candles(30))
    call_kwargs = flt._client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-opus-4-8"


def test_messages_create_uses_adaptive_thinking():
    flt = _filter_with_response("ACCEPT")
    flt.accept(Signal("buy", 0.05), _flat_candles(30))
    call_kwargs = flt._client.messages.create.call_args[1]
    assert call_kwargs["thinking"] == {"type": "adaptive"}


def test_messages_create_sends_user_message():
    flt = _filter_with_response("ACCEPT")
    flt.accept(Signal("buy", 0.05), _flat_candles(30))
    call_kwargs = flt._client.messages.create.call_args[1]
    assert call_kwargs["messages"][0]["role"] == "user"


# ── Integration with GoldStrategy ────────────────────────────────────────────

def test_gold_strategy_accepts_claude_filter():
    """GoldStrategy must accept any SignalFilter implementation."""
    from strategy.gold_strategy import GoldStrategy
    flt = _filter_with_response("ACCEPT")
    strat = GoldStrategy(signal_filter=flt)
    assert strat.signal_filter is flt

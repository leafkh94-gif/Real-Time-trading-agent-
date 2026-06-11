"""
ClaudeSignalFilter — uses Claude AI (claude-opus-4-8) to evaluate trade signals.

Sends a compact market-context summary to Claude and returns a binary
ACCEPT / REJECT decision. Falls back to True (accept) on any API error
so a transient API outage never halts all trading.
"""
import logging
import math
from typing import Sequence

import anthropic

from execution.models import Signal
from strategy.base import Candle
from strategy.indicators import atr, ema
from strategy.signal_filter import SignalFilter

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 1024  # adaptive thinking needs headroom; text reply is still one word
_CONTEXT_BARS = 20

_SYSTEM = (
    "You are a disciplined risk analyst reviewing liquidity-sweep reversal "
    "signals on Gold, S&P 500, Nasdaq, and Dow Jones. "
    "\n\n"
    "IMPORTANT — how this strategy works: each signal is a LIQUIDITY SWEEP, "
    "a deliberate counter-trend reversal setup. A 'sell' fires after price "
    "spiked above a recent swing high and was rejected (closed back below) — "
    "so price being ABOVE the EMA in an uptrend is EXPECTED and is NOT a "
    "reason to reject. A 'buy' fires after price pierced a swing low and "
    "snapped back up. Do NOT reject a signal merely for being counter-trend; "
    "that is the entire point of the setup. "
    "\n\n"
    "Your job is to judge the QUALITY of the sweep: reject only if the price "
    "action looks like a genuine breakout with no rejection, if volatility is "
    "so extreme the setup is noise, or if recent closes show the reversal has "
    "clearly already failed. When in doubt on a clean sweep, ACCEPT. "
    "\n\n"
    "Reply with exactly one word — ACCEPT or REJECT — and nothing else."
)


class ClaudeSignalFilter(SignalFilter):
    """
    Claude-backed signal filter.

    Builds a short market-context prompt from recent candles, sends it to
    Claude with adaptive thinking enabled, and parses the single-word reply.

    Parameters
    ----------
    api_key:
        Anthropic API key. If None the SDK reads ANTHROPIC_API_KEY from the
        environment (the recommended approach — never hard-code keys).
    model:
        Claude model ID. Defaults to claude-opus-4-8.
    fallback_accept:
        What to return when the API call fails. True (accept all) keeps the
        bot trading during transient outages; False (reject all) is safer
        for production. Default: True.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _MODEL,
        fallback_accept: bool = True,
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._fallback_accept = fallback_accept

    # ── Public interface ──────────────────────────────────────────────────────

    def accept(self, signal: Signal, candles: Sequence[Candle]) -> bool:
        try:
            return self._query_claude(signal, candles)
        except anthropic.APIError as exc:
            logger.warning("ClaudeSignalFilter API error — fallback=%s: %s", self._fallback_accept, exc)
            return self._fallback_accept
        except Exception as exc:
            logger.warning("ClaudeSignalFilter unexpected error — fallback=%s: %s", self._fallback_accept, exc)
            return self._fallback_accept

    # ── Internal ──────────────────────────────────────────────────────────────

    def _query_claude(self, signal: Signal, candles: Sequence[Candle]) -> bool:
        prompt = _build_prompt(signal, candles)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        # Extract the last text block (thinking blocks come first when present)
        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            logger.warning("ClaudeSignalFilter: no text block in response — fallback=%s", self._fallback_accept)
            return self._fallback_accept

        decision = text_blocks[-1].text.strip().upper()
        logger.info("ClaudeSignalFilter decision=%s (signal=%s %.2f lots)", decision, signal.direction, signal.lots)
        return decision.startswith("ACCEPT")


# ── Prompt builder (module-level, pure) ──────────────────────────────────────

def _build_prompt(signal: Signal, candles: Sequence[Candle]) -> str:
    tail = list(candles[-_CONTEXT_BARS:]) if candles else []
    if not tail:
        return (
            f"Liquidity-sweep {signal.direction.upper()} reversal signal.\n"
            "No price history available.\n"
            "ACCEPT or REJECT?"
        )

    closes = [c.close for c in tail]
    ema_vals = ema(closes, period=min(20, len(closes)))
    atr_vals = atr(tail, period=min(14, len(tail)))

    last = tail[-1]

    # Latest valid EMA value
    valid_emas = [v for v in ema_vals if not math.isnan(v)]
    ema_str = f"{valid_emas[-1]:.2f}" if valid_emas else "n/a"
    trend_rel = "above" if (valid_emas and last.close > valid_emas[-1]) else "below"

    # Average ATR (excluding NaN padding)
    valid_atrs = [v for v in atr_vals if not math.isnan(v)]
    avg_atr = sum(valid_atrs) / len(valid_atrs) if valid_atrs else 0.0

    recent_closes = ", ".join(f"{c.close:.2f}" for c in tail[-5:])

    sweep_desc = (
        "price swept above a swing high and was rejected (bearish reversal)"
        if signal.direction == "sell"
        else "price swept below a swing low and snapped back up (bullish reversal)"
    )
    return (
        f"Liquidity-sweep {signal.direction.upper()} reversal signal "
        f"({sweep_desc}).\n"
        f"Current price : {last.close:.2f}\n"
        f"EMA-20        : {ema_str} (price is {trend_rel} EMA-20)\n"
        f"ATR-14 avg    : {avg_atr:.2f}\n"
        f"Last 5 closes : {recent_closes}\n"
        f"Last bar H/L  : {last.high:.2f} / {last.low:.2f}\n"
        f"\nJudge the quality of this sweep. ACCEPT or REJECT?"
    )

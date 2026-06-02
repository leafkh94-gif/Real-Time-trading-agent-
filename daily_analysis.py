"""
daily_analysis.py — sends a structured market analysis report to Telegram.
Runs 3x/day via GitHub Actions. Uses Claude to generate the analysis from
live Yahoo Finance data (price, RSI, MACD, ATR, support/resistance).

Required env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY
"""
import logging
import os
import math

import anthropic
from dotenv import load_dotenv

load_dotenv()

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.base import TF_H1, TF_H4
from strategy.indicators import atr as _atr, ema, swing_highs, swing_lows
from strategy.yahoo_feed import YahooFinanceFeed

WATCHLIST = [
    ("GOLD",  "Gold (XAU/USD)"),
    ("US500", "S&P 500"),
    ("US100", "Nasdaq 100"),
    ("US30",  "Dow Jones"),
]


def _rsi(closes: list[float], period: int = 14) -> float:
    """Compute the most recent RSI value."""
    if len(closes) < period + 1:
        return float("nan")
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes: list[float]) -> tuple[float, float]:
    """Return (macd_line, signal_line) for most recent bar."""
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    if not fast or not slow:
        return float("nan"), float("nan")
    macd_line = [f - s for f, s in zip(fast, slow) if not (math.isnan(f) or math.isnan(s))]
    if not macd_line:
        return float("nan"), float("nan")
    signal = ema(macd_line, 9)
    if not signal:
        return macd_line[-1], float("nan")
    return macd_line[-1], signal[-1]


def _sma(closes: list[float], period: int) -> float:
    valid = [c for c in closes[-period:] if not math.isnan(c)]
    return sum(valid) / len(valid) if valid else float("nan")


def _build_market_data(epic: str, name: str) -> dict | None:
    try:
        candles = YahooFinanceFeed(epic).get_candles()
    except Exception as exc:
        logging.getLogger(__name__).warning("%s: feed error: %s", epic, exc)
        return None

    h1 = candles.get(TF_H1, [])
    h4 = candles.get(TF_H4, [])
    if len(h1) < 50:
        return None

    closes_h1 = [c.close for c in h1]
    current_price = closes_h1[-1]
    prev_close    = closes_h1[-2] if len(closes_h1) > 1 else current_price
    daily_change  = ((current_price - prev_close) / prev_close) * 100

    rsi_val    = _rsi(closes_h1)
    macd_val, signal_val = _macd(closes_h1)
    atr_vals   = [v for v in _atr(h1) if not math.isnan(v)]
    current_atr = atr_vals[-1] if atr_vals else 0
    sma20 = _sma(closes_h1, 20)
    sma50 = _sma(closes_h1, 50)
    ema9_list  = ema(closes_h1, 9)
    ema9  = ema9_list[-1] if ema9_list else float("nan")

    # Swing highs/lows for support/resistance
    sh = [v for v in swing_highs(h1, lookback=5) if v is not None]
    sl = [v for v in swing_lows(h1,  lookback=5) if v is not None]
    resistance_levels = sorted(set(round(v, 2) for v in sh[-5:]), reverse=True)
    support_levels    = sorted(set(round(v, 2) for v in sl[-5:]),  reverse=True)

    # H4 trend
    h4_trend = "neutral"
    if len(h4) >= 20:
        h4_closes = [c.close for c in h4]
        h4_sma20  = _sma(h4_closes, 20)
        if h4[-1].close > h4_sma20 * 1.002:
            h4_trend = "bullish"
        elif h4[-1].close < h4_sma20 * 0.998:
            h4_trend = "bearish"

    return {
        "name":            name,
        "price":           current_price,
        "daily_change_pct": daily_change,
        "rsi":             rsi_val,
        "macd":            macd_val,
        "macd_signal":     signal_val,
        "atr":             current_atr,
        "sma20":           sma20,
        "sma50":           sma50,
        "ema9":            ema9,
        "h4_trend":        h4_trend,
        "resistance":      resistance_levels[:3],
        "support":         support_levels[:3],
    }


def _ask_claude(market_snapshots: list[dict]) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    data_block = ""
    for m in market_snapshots:
        data_block += f"""
{m['name']}
  Price: {m['price']:,.2f}  ({m['daily_change_pct']:+.2f}% vs prev H1 bar)
  RSI(14): {m['rsi']:.1f}
  MACD: {m['macd']:.4f}  Signal: {m['macd_signal']:.4f}
  ATR(14): {m['atr']:.2f}
  SMA20: {m['sma20']:,.2f}  SMA50: {m['sma50']:,.2f}  EMA9: {m['ema9']:,.2f}
  H4 trend: {m['h4_trend']}
  Resistance: {', '.join(str(r) for r in m['resistance']) or 'n/a'}
  Support:    {', '.join(str(s) for s in m['support']) or 'n/a'}
"""

    prompt = f"""You are a professional market analyst. Based on the live technical data below, write a concise daily market briefing for a retail trader.

LIVE MARKET DATA:
{data_block}

Write the briefing in this exact structure (use plain text, no markdown):

DAILY MARKET BRIEFING

For each instrument, cover in 3-4 sentences:
- Overall bias (bullish / bearish / neutral) and why
- Key levels to watch (support and resistance)
- What RSI and MACD tell you right now
- One actionable insight (e.g. "watch for a break above X", "oversold bounce possible near Y")

End with a 2-sentence OVERALL MARKET MOOD summary.

Be specific with price levels. Keep total length under 600 words."""

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b.text for b in response.content if b.type == "text")


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
    notifier  = TelegramNotifier(bot_token, chat_id) if (bot_token and chat_id) else NullNotifier()

    log.info("Collecting market data for daily briefing...")
    snapshots = []
    for epic, name in WATCHLIST:
        data = _build_market_data(epic, name)
        if data:
            snapshots.append(data)
            log.info("%s: price=%.2f  RSI=%.1f  H4=%s", epic, data["price"], data["rsi"], data["h4_trend"])
        else:
            log.warning("%s: skipped (insufficient data)", epic)

    if not snapshots:
        log.error("No market data available — aborting briefing")
        return

    log.info("Asking Claude for market analysis...")
    try:
        analysis = _ask_claude(snapshots)
    except Exception as exc:
        log.error("Claude API error: %s", exc)
        return

    # Format for Telegram
    header = "📊 <b>DAILY MARKET BRIEFING</b>\n\n"
    body   = analysis.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Restore our own HTML tags
    body   = body.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    message = header + body

    if hasattr(notifier, "send_html"):
        notifier.send_html(message)
    else:
        notifier.send(analysis)

    log.info("Daily briefing sent.")


if __name__ == "__main__":
    main()

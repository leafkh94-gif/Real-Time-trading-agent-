"""
main_alerts.py — multi-market alert bot (no execution).

Watches Gold, S&P 500, Nasdaq 100, and Dow Jones (US30) via Capital.com.
When GoldStrategy detects a setup it sends a Telegram message with
entry price, take profit, and stop loss — no trades are placed.

Usage:
  python main_alerts.py

Required .env keys:
  CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Optional:
  ENVIRONMENT=production   (uses live Capital.com API; default is demo)
"""
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.base import TF_H1
from strategy.capital_feed import CapitalComFeed
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import atr as _atr

# ── Configuration ─────────────────────────────────────────────────────────────

SCAN_INTERVAL_S = 5 * 60       # seconds between full watchlist scans
ALERT_COOLDOWN_S = 60 * 60     # minimum seconds between alerts for the same instrument
TP_ATR_MULT = 2.5              # take-profit = entry ± (ATR × 2.5)
SL_ATR_MULT = 1.5              # stop-loss   = entry ± (ATR × 1.5)


@dataclass
class _Instrument:
    epic: str        # Capital.com epic code
    name: str        # human-readable label
    _last_alert: float = field(default=0.0, init=False, repr=False)

    def on_cooldown(self) -> bool:
        return time.time() - self._last_alert < ALERT_COOLDOWN_S

    def mark_alerted(self) -> None:
        self._last_alert = time.time()


WATCHLIST: list[_Instrument] = [
    _Instrument("GOLD",  "Gold (XAU/USD)"),
    _Instrument("US500", "S&P 500"),
    _Instrument("US100", "Nasdaq 100"),
    _Instrument("US30",  "Dow Jones (US30)"),
]

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_message(instr: _Instrument, direction: str,
                   entry: float, tp: float, sl: float) -> tuple[str, str]:
    """Return (html, plain) alert strings for the given setup."""
    emoji = "🟢" if direction == "buy" else "🔴"
    dir_label = "BUY" if direction == "buy" else "SELL"
    risk = abs(entry - sl)
    reward = abs(entry - tp)
    rr = reward / risk if risk > 0 else 0.0
    tp_pct = (reward / entry) * 100
    sl_pct = (risk / entry) * 100

    html_lines = [
        f"{emoji} <b>TRADE SETUP — {instr.name}</b>",
        "",
        f"Direction:    <b>{dir_label}</b>",
        f"Entry:        <b>{entry:,.2f}</b>",
        f"Take Profit:  <b>{tp:,.2f}</b>  (+{tp_pct:.1f}%)",
        f"Stop Loss:    <b>{sl:,.2f}</b>  (-{sl_pct:.1f}%)",
        f"R:R Ratio:    1 : {rr:.1f}",
        "",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    plain_lines = [
        f"{emoji} TRADE SETUP — {instr.name}",
        "",
        f"Direction:    {dir_label}",
        f"Entry:        {entry:,.2f}",
        f"Take Profit:  {tp:,.2f}  (+{tp_pct:.1f}%)",
        f"Stop Loss:    {sl:,.2f}  (-{sl_pct:.1f}%)",
        f"R:R Ratio:    1 : {rr:.1f}",
        "",
        "Alert only — always confirm before trading.",
    ]
    return "\n".join(html_lines), "\n".join(plain_lines)


def _notify(notifier, html: str, plain: str) -> None:
    if hasattr(notifier, "send_html"):
        notifier.send_html(html)
    else:
        notifier.send(plain)


def _scan_one(instr: _Instrument, feed: CapitalComFeed,
              strategy: GoldStrategy, notifier, logger: logging.Logger) -> None:
    if instr.on_cooldown():
        logger.debug("%s: cooldown active — skipping", instr.epic)
        return

    try:
        candles = feed.get_candles()
    except Exception as exc:
        logger.error("%s: feed error: %s", instr.epic, exc)
        return

    h1 = candles.get(TF_H1, [])
    if not h1:
        logger.debug("%s: no H1 candles returned", instr.epic)
        return

    sig = strategy.evaluate(candles)
    if sig is None:
        logger.debug("%s: no signal", instr.epic)
        return

    # ── ATR-based TP/SL ───────────────────────────────────────────────────
    atr_series = _atr(h1, period=14)
    valid = [v for v in atr_series if v == v]  # drop leading NaN
    if not valid:
        logger.warning("%s: ATR unavailable — cannot compute TP/SL, skipping", instr.epic)
        return

    current_atr = valid[-1]
    entry = h1[-1].close

    if sig.direction == "buy":
        tp = entry + TP_ATR_MULT * current_atr
        sl = entry - SL_ATR_MULT * current_atr
    else:
        tp = entry - TP_ATR_MULT * current_atr
        sl = entry + SL_ATR_MULT * current_atr

    html, plain = _build_message(instr, sig.direction, entry, tp, sl)
    _notify(notifier, html, plain)
    instr.mark_alerted()
    logger.info("Alert sent: %s %s  entry=%.2f  tp=%.2f  sl=%.2f  atr=%.2f",
                instr.epic, sig.direction.upper(), entry, tp, sl, current_atr)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    api_key    = os.getenv("CAPITAL_API_KEY", "")
    identifier = os.getenv("CAPITAL_IDENTIFIER", "")
    password   = os.getenv("CAPITAL_PASSWORD", "")
    demo       = os.getenv("ENVIRONMENT", "development").lower() != "production"

    if not (api_key and identifier and password):
        logger.error(
            "Missing Capital.com credentials — set CAPITAL_API_KEY, "
            "CAPITAL_IDENTIFIER, and CAPITAL_PASSWORD in .env"
        )
        sys.exit(1)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        notifier = TelegramNotifier(bot_token, chat_id)
        logger.info("Telegram notifier ready (chat_id=%s)", chat_id)
    else:
        notifier = NullNotifier()
        logger.warning("No Telegram credentials — alerts will be logged only. "
                       "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    # Connect one feed per instrument, skip instruments that fail to connect.
    feeds: dict[str, CapitalComFeed] = {}
    logger.info("Connecting Capital.com feeds (%s)...", "DEMO" if demo else "LIVE")
    for instr in WATCHLIST:
        try:
            feeds[instr.epic] = CapitalComFeed(
                api_key=api_key, identifier=identifier, password=password,
                epic=instr.epic, demo=demo,
            )
            logger.info("  %-6s connected", instr.epic)
        except Exception as exc:
            logger.error("  %-6s FAILED (%s) — skipping", instr.epic, exc)

    if not feeds:
        logger.error("No feeds connected — check credentials and network, then retry")
        sys.exit(1)

    strategy = GoldStrategy()
    env_label = "DEMO" if demo else "LIVE"
    active = [i.epic for i in WATCHLIST if i.epic in feeds]
    logger.info(
        "Alert bot running [%s] — watching %s, scanning every %ds",
        env_label, ", ".join(active), SCAN_INTERVAL_S,
    )

    while _running:
        for instr in WATCHLIST:
            if not _running:
                break
            feed = feeds.get(instr.epic)
            if feed is None:
                continue
            _scan_one(instr, feed, strategy, notifier, logger)
            time.sleep(0.5)  # pace requests within Capital.com rate limit

        if _running:
            logger.debug("Scan complete — sleeping %ds until next cycle", SCAN_INTERVAL_S)
            time.sleep(SCAN_INTERVAL_S)

    logger.info("Alert bot stopped cleanly.")


if __name__ == "__main__":
    main()

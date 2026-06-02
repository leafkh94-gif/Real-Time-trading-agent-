"""
scan_once.py — scans all markets once and exits.
Called by GitHub Actions every 5 minutes.
"""
import logging
import os
import time

from dotenv import load_dotenv
load_dotenv()

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.base import TF_H1
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import atr as _atr
from strategy.yahoo_feed import YahooFinanceFeed

WATCHLIST = [
    ("GOLD",  "Gold (XAU/USD)"),
    ("US500", "S&P 500"),
    ("US100", "Nasdaq 100"),
    ("US30",  "Dow Jones (US30)"),
]

TP_ATR_MULT = 2.5
SL_ATR_MULT = 1.5


def _build_html(name: str, direction: str, entry: float, tp: float, sl: float) -> str:
    emoji     = "🟢" if direction == "buy" else "🔴"
    dir_label = "BUY" if direction == "buy" else "SELL"
    risk      = abs(entry - sl)
    reward    = abs(entry - tp)
    rr        = reward / risk if risk > 0 else 0.0
    tp_pct    = (reward / entry) * 100
    sl_pct    = (risk   / entry) * 100
    return "\n".join([
        f"{emoji} <b>TRADE SETUP — {name}</b>",
        "",
        f"Direction:    <b>{dir_label}</b>",
        f"Entry:        <b>{entry:,.2f}</b>",
        f"Take Profit:  <b>{tp:,.2f}</b>  (+{tp_pct:.1f}%)",
        f"Stop Loss:    <b>{sl:,.2f}</b>  (-{sl_pct:.1f}%)",
        f"R:R Ratio:    1 : {rr:.1f}",
        "",
        "<i>Alert only — always confirm before trading.</i>",
    ])


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
    notifier  = TelegramNotifier(bot_token, chat_id) if (bot_token and chat_id) else NullNotifier()

    strategy     = GoldStrategy()
    alerts_sent  = 0

    for epic, name in WATCHLIST:
        try:
            candles = YahooFinanceFeed(epic).get_candles()
            h1      = candles.get(TF_H1, [])
            h4      = candles.get("H4", [])
            log.info("%s: %d H1 candles, %d H4 candles", epic, len(h1), len(h4))
            if not h1:
                continue

            sig = strategy.evaluate(candles)
            if sig is None:
                continue

            atr_vals = [v for v in _atr(h1, period=14) if v == v]
            if not atr_vals:
                continue

            cur_atr = atr_vals[-1]
            entry   = h1[-1].close
            if sig.direction == "buy":
                tp, sl = entry + TP_ATR_MULT * cur_atr, entry - SL_ATR_MULT * cur_atr
            else:
                tp, sl = entry - TP_ATR_MULT * cur_atr, entry + SL_ATR_MULT * cur_atr

            html = _build_html(name, sig.direction, entry, tp, sl)
            if hasattr(notifier, "send_html"):
                notifier.send_html(html)
            else:
                notifier.send(html)

            alerts_sent += 1
            log.info("Alert sent: %s %s entry=%.2f tp=%.2f sl=%.2f",
                     epic, sig.direction.upper(), entry, tp, sl)

        except Exception as exc:
            log.error("%s: %s", epic, exc)

        time.sleep(3)   # stagger requests to avoid Yahoo Finance rate limits

    log.info("Scan complete — %d alert(s) sent.", alerts_sent)


if __name__ == "__main__":
    main()

"""
main_alerts.py — US index alert bot.

Monitors S&P 500, Nasdaq 100, and Dow Jones via Yahoo Finance (free, no auth).
When the 3-gate strategy detects a liquidity sweep setup, sends a Telegram
alert with entry, take profit, stop loss, and R:R.

No broker connection. No trade execution. Human confirms before trading.

Gate 1 — Data sufficiency  (H4 ≥ 65 candles, H1 ≥ 28 candles)
Gate 2 — Regime filter     (H4 ATR/close > 1.8% → VOLATILE → skip)
Gate 3 — Liquidity sweep   (H1 wick pierces swing level + closes back)

Usage:
  python main_alerts.py

Required .env keys:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.base import TF_H1, TF_H4
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import atr as _atr
from strategy.market_hours import is_tradeable
from strategy.yahoo_feed import YahooFinanceFeed

# ── Configuration ──────────────────────────────────────────────────────────────

SCAN_INTERVAL_S  = 20 * 60   # scan every 20 minutes
ALERT_COOLDOWN_S = 60 * 60   # 1-hour cooldown per instrument
TP_ATR_MULT      = 2.5       # take profit = entry ± ATR × 2.5
SL_ATR_MULT      = 1.5       # stop loss   = entry ∓ ATR × 1.5
COOLDOWN_FILE    = os.getenv("COOLDOWN_FILE", ".alert_cooldown.json")


@dataclass
class _Instrument:
    epic: str
    name: str
    _last_alert: float = field(default=0.0, init=False, repr=False)

    def on_cooldown(self) -> bool:
        return time.time() - self._last_alert < ALERT_COOLDOWN_S

    def mark_alerted(self) -> None:
        self._last_alert = time.time()


WATCHLIST: list[_Instrument] = [
    _Instrument("US500", "S&P 500"),
    _Instrument("US100", "Nasdaq 100"),
    _Instrument("US30",  "Dow Jones (US30)"),
]

# ── Cooldown persistence ───────────────────────────────────────────────────────

def _load_cooldowns(instruments: list) -> None:
    try:
        with open(COOLDOWN_FILE) as f:
            data = json.load(f)
        for instr in instruments:
            ts = data.get(instr.epic, 0.0)
            if ts:
                instr._last_alert = float(ts)
        logging.getLogger(__name__).info("Cooldown state restored from %s", COOLDOWN_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_cooldown(instr) -> None:
    try:
        try:
            with open(COOLDOWN_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[instr.epic] = instr._last_alert
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(data, f)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not save cooldown state: %s", exc)


# ── Graceful shutdown ──────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Alert formatting ───────────────────────────────────────────────────────────

def _build_message(instr: _Instrument, direction: str,
                   entry: float, tp: float, sl: float) -> tuple[str, str]:
    """Return (html, plain) alert strings."""
    import datetime
    now       = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    emoji     = "🟢" if direction == "buy" else "🔴"
    dir_label = "BUY"  if direction == "buy" else "SELL"
    risk      = abs(entry - sl)
    reward    = abs(entry - tp)
    rr        = reward / risk if risk > 0 else 0.0
    tp_pct    = (reward / entry) * 100
    sl_pct    = (risk   / entry) * 100

    html_lines = [
        f"{emoji} <b>TRADE SETUP — {instr.name}</b>",
        f"<i>Signal detected: {now}</i>",
        "",
        f"Direction:    <b>{dir_label}</b>",
        f"Entry:        <b>{entry:,.2f}</b>",
        f"Take Profit:  <b>{tp:,.2f}</b>  (+{tp_pct:.1f}%)",
        f"Stop Loss:    <b>{sl:,.2f}</b>  (-{sl_pct:.1f}%)",
        f"R:R Ratio:    1 : {rr:.1f}",
        "",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    plain_lines = [line.replace("<b>", "").replace("</b>", "")
                       .replace("<i>", "").replace("</i>", "")
                   for line in html_lines]
    return "\n".join(html_lines), "\n".join(plain_lines)


def _notify(notifier, html: str, plain: str) -> None:
    if hasattr(notifier, "send_html"):
        notifier.send_html(html)
    else:
        notifier.send(plain)


# ── Heartbeat ──────────────────────────────────────────────────────────────────

_last_heartbeat: float = 0.0
_HEARTBEAT_INTERVAL_S = 24 * 60 * 60


def _maybe_send_heartbeat(notifier, instruments: list, logger: logging.Logger) -> None:
    global _last_heartbeat
    if time.time() - _last_heartbeat < _HEARTBEAT_INTERVAL_S:
        return
    if any(time.time() - i._last_alert < _HEARTBEAT_INTERVAL_S for i in instruments):
        _last_heartbeat = time.time()
        return
    markets = ", ".join(i.name for i in instruments)
    html  = ("🤖 <b>Alert bot — daily check-in</b>\n"
             f"<i>Watching: {markets}</i>\n"
             "No trade setups in the last 24h — bot is running normally.")
    plain = f"Alert bot — daily check-in. Watching {markets}. No setups in 24h."
    _notify(notifier, html, plain)
    _last_heartbeat = time.time()
    logger.info("Daily heartbeat sent")


# ── Per-instrument scan ────────────────────────────────────────────────────────

def _evaluate_one(instr: _Instrument, feed: YahooFinanceFeed,
                  strategy: GoldStrategy, logger: logging.Logger):
    """
    Run the 3-gate pipeline for one instrument.
    Returns (direction, entry, tp, sl) if all gates pass, else None.

    Gate order:
      1. Cooldown     — skip if alerted in the last hour
      2. Market hours — skip outside official trading session
      3. Strategy     — Gate 1 (data) + Gate 2 (regime H4) + Gate 3 (sweep H1)
    """
    if instr.on_cooldown():
        logger.debug("%s: cooldown active — skipping", instr.epic)
        return None

    if not is_tradeable(instr.epic):
        logger.info("%s: outside trading hours — skipping", instr.epic)
        return None

    try:
        candles = feed.get_candles()
        h1 = candles.get(TF_H1, [])

        if not h1:
            logger.debug("%s: no H1 candles returned", instr.epic)
            return None

        sig = strategy.evaluate(candles)
        if sig is None:
            return None

        # Compute ATR-based TP and SL
        atr_series = _atr(h1, period=14)
        valid_atr  = [v for v in atr_series if v == v]
        if not valid_atr:
            logger.debug("%s: ATR unavailable — skipping", instr.epic)
            return None

        current_atr = valid_atr[-1]
        entry = h1[-1].close

        if sig.direction == "buy":
            tp = entry + TP_ATR_MULT * current_atr
            sl = entry - SL_ATR_MULT * current_atr
        else:
            tp = entry - TP_ATR_MULT * current_atr
            sl = entry + SL_ATR_MULT * current_atr

        rr = abs(tp - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0.0
        logger.info(
            "%s: signal %s  entry=%.2f  tp=%.2f  sl=%.2f  R:R=1:%.1f  atr=%.2f",
            instr.epic, sig.direction.upper(), entry, tp, sl, rr, current_atr,
        )
        return sig.direction, entry, tp, sl

    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        return None


def _send_alert(instr: _Instrument, direction: str, entry: float,
                tp: float, sl: float, notifier, logger: logging.Logger) -> None:
    try:
        html, plain = _build_message(instr, direction, entry, tp, sl)
        _notify(notifier, html, plain)
        instr.mark_alerted()
        _save_cooldown(instr)
        logger.info("Alert sent: %s %s  entry=%.2f  tp=%.2f  sl=%.2f",
                    instr.epic, direction.upper(), entry, tp, sl)
    except Exception as exc:
        logger.error("%s: alert error: %s", instr.epic, exc)


# ── Market-hours sleep ────────────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = dtime(9, 30)
_MARKET_CLOSE = dtime(15, 30)   # 30-min buffer before 16:00 close


def _next_market_open_et() -> datetime:
    """Return the next NYSE open as a tz-aware ET datetime."""
    now = datetime.now(tz=_ET)
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)

    # If we're already past the cutoff today, roll to next day
    if now.time() >= _MARKET_CLOSE:
        candidate += timedelta(days=1)

    # Advance past weekends
    while candidate.weekday() >= 5:   # 5=Sat, 6=Sun
        candidate += timedelta(days=1)

    return candidate


def _wait_for_market_open(logger: logging.Logger) -> None:
    """
    If all markets are currently closed, sleep until the next NYSE open
    (09:30 ET on the next trading day) rather than spinning every 20 min.
    Wakes up 2 minutes early so the first scan fires right at open.
    Respects MAX_RUNTIME_S — exits cleanly if runtime would be exceeded.
    """
    if is_tradeable("US500"):   # representative — all three share the same hours
        return

    next_open = _next_market_open_et()
    wake_time = next_open - timedelta(minutes=2)
    now       = datetime.now(tz=_ET)
    wait_s    = (wake_time - now).total_seconds()

    if wait_s <= 0:
        return

    logger.info(
        "Markets closed — sleeping %.0f min until %s ET  (next NYSE open)",
        wait_s / 60,
        next_open.strftime("%H:%M"),
    )

    # Sleep in 60-second chunks so SIGTERM is handled promptly
    deadline = time.time() + wait_s
    while _running and time.time() < deadline:
        time.sleep(min(60, deadline - time.time()))


# ── Health server (keeps Render / cloud host alive) ────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass


def _start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.getLogger(__name__).info("Health server listening on port %d", port)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    _start_health_server()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    if bot_token and chat_id:
        notifier = TelegramNotifier(bot_token, chat_id)
        logger.info("Telegram notifier ready (chat_id=%s)", chat_id)
    else:
        notifier = NullNotifier()
        logger.warning(
            "No Telegram credentials found — alerts will be logged only.\n"
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to your .env file."
        )

    feeds: dict[str, YahooFinanceFeed] = {
        instr.epic: YahooFinanceFeed(instr.epic) for instr in WATCHLIST
    }

    _load_cooldowns(WATCHLIST)

    import datetime as _dt
    _startup_time = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _notify(notifier,
            f"🟡 <b>Alert bot started</b> — <i>{_startup_time}</i>\n"
            "Watching S&amp;P 500, Nasdaq 100, Dow Jones. Scanning every 20 min.",
            f"Alert bot started {_startup_time}. Watching S&P 500, Nasdaq 100, Dow Jones. Scanning every 20 min.")
    logger.info("Startup notification sent")

    strategy  = GoldStrategy()
    epic_list = ", ".join(i.epic for i in WATCHLIST)

    max_runtime_s = int(os.getenv("MAX_RUNTIME_S", "0"))
    start_time    = time.time()

    logger.info("Alert bot running — watching %s, scanning every %ds%s",
                epic_list, SCAN_INTERVAL_S,
                f", max runtime {max_runtime_s}s" if max_runtime_s else "")

    while _running:
        _wait_for_market_open(logger)   # sleep until 09:30 ET if markets are closed

        for instr in WATCHLIST:
            if not _running:
                break
            result = _evaluate_one(instr, feeds[instr.epic], strategy, logger)
            if result is not None:
                direction, entry, tp, sl = result
                _send_alert(instr, direction, entry, tp, sl, notifier, logger)
            time.sleep(3)

        _maybe_send_heartbeat(notifier, WATCHLIST, logger)

        if max_runtime_s and (time.time() - start_time) >= max_runtime_s:
            logger.info("Max runtime reached — exiting cleanly for handoff.")
            break

        if _running:
            logger.debug("Scan complete — sleeping %ds", SCAN_INTERVAL_S)
            time.sleep(SCAN_INTERVAL_S)

    logger.info("Alert bot stopped cleanly.")


if __name__ == "__main__":
    main()

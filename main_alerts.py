"""
main_alerts.py — multi-market alert bot (no execution, no broker login).

Watches Gold, S&P 500, Nasdaq 100, and Dow Jones via Yahoo Finance (free).
When GoldStrategy detects a setup it sends a Telegram message with
entry price, take profit, and stop loss — no trades are placed.

Usage:
  python main_alerts.py

Required .env keys:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

No Capital.com or TradingView account required.
"""
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from execution.models import Signal
from strategy.base import TF_H1, TF_H4
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import atr as _atr, ema as _ema, swing_highs, swing_lows
from strategy.market_hours import is_tradeable
from strategy.momentum_detector import MomentumDetector
from strategy.news_filter import high_impact_news_within
from strategy.yahoo_feed import YahooFinanceFeed

# ── Configuration ─────────────────────────────────────────────────────────────

SCAN_INTERVAL_S       = 30 * 60       # seconds between full watchlist scans
ALERT_COOLDOWN_S      = 0             # no cooldown — CHOCH/BOS confirmation prevents repeats
HEARTBEAT_INTERVAL_S  = 24 * 60 * 60  # daily liveness ping if no alerts fired
SL_ATR_MULT           = 1.5           # stop-loss   = entry ± (ATR × 1.5)
MIN_RR_RATIO          = 2.0           # minimum reward:risk — skip if swing TP gives less
EMA_TREND_STRENGTH_PCT = 1.5          # % spread between EMA20/EMA50 that defines a rocket trend
NEWS_BLOCK_WINDOW_MIN = 30            # block alerts within ±30 min of high-impact USD news
COOLDOWN_FILE         = os.getenv("COOLDOWN_FILE", ".alert_cooldown.json")


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
    _Instrument("GOLD",  "Gold (XAU/USD)"),
    _Instrument("US500", "S&P 500"),
    _Instrument("US100", "Nasdaq 100"),
    _Instrument("US30",  "Dow Jones (US30)"),
]

# ── Cooldown persistence ─────────────────────────────────────────────────────

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


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Alert formatting ──────────────────────────────────────────────────────────

def _build_message(instr: _Instrument, direction: str,
                   entry: float, tp: float, sl: float) -> tuple[str, str]:
    """Return (html, plain) alert strings."""
    import datetime
    now       = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
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


# ── Heartbeat ─────────────────────────────────────────────────────────────────

_last_heartbeat: float = 0.0


def _maybe_send_heartbeat(notifier, instruments: list, logger: logging.Logger) -> None:
    """Send a 24h liveness ping only when no trade alert has fired recently."""
    global _last_heartbeat
    if time.time() - _last_heartbeat < HEARTBEAT_INTERVAL_S:
        return
    # Skip heartbeat if a real alert fired in the last 24h — not needed
    if any(time.time() - i._last_alert < HEARTBEAT_INTERVAL_S for i in instruments):
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


# ── US index consensus ────────────────────────────────────────────────────────

_US_INDEX_EPICS = frozenset({"US500", "US100", "US30"})


# ── Swing-based TP helper ─────────────────────────────────────────────────────

def _nearest_swing_tp(h1: list, entry: float, direction: str) -> float | None:
    """
    Return the nearest H1 swing high (for BUY) or swing low (for SELL)
    beyond the entry price — the first structural target the market must reach.
    Returns None if no clear swing level exists in the direction of the trade.
    """
    sh = swing_highs(h1, lookback=5)
    sl = swing_lows( h1, lookback=5)
    if direction == "buy":
        candidates = [v for v in sh if v is not None and v > entry]
        return min(candidates) if candidates else None   # nearest swing high above entry
    else:
        candidates = [v for v in sl if v is not None and v < entry]
        return max(candidates) if candidates else None   # nearest swing low below entry


# ── Per-instrument scan ───────────────────────────────────────────────────────

def _evaluate_one(instr: _Instrument, feed: YahooFinanceFeed,
                  strategy: GoldStrategy, logger: logging.Logger,
                  momentum_detector: MomentumDetector | None = None):
    """
    Fetch candles and run the full filter pipeline.
    Returns (candles, direction, swing_tp) if all gates pass, else None.

    Gate order (cheapest first):
      1. Cooldown        — skip if alerted in the last 4 hours
      2. Market hours    — skip outside trading session
      3. News window     — skip ±30 min around high-impact USD events
      4a. Strategy       — sweep (wick quality) + BOS / CHOCH confirmation + EMA regime
      4b. Momentum       — fallback: big ATR-sized move fires counter-trend signal
      5. EMA distance    — suppress counter-trend signals in rocket-trend markets
                           (skipped for momentum signals — they ARE the counter-trend)
      6. R:R             — skip if nearest swing target gives less than 1:2
    """
    if instr.on_cooldown():
        logger.debug("%s: cooldown active — skipping", instr.epic)
        return None

    if not is_tradeable(instr.epic):
        logger.info("%s: outside trading hours — skipping", instr.epic)
        return None

    if high_impact_news_within(NEWS_BLOCK_WINDOW_MIN):
        logger.info("%s: high-impact USD news within %dm — skipping",
                    instr.epic, NEWS_BLOCK_WINDOW_MIN)
        return None

    try:
        candles = feed.get_candles()
        h1 = candles.get(TF_H1, [])
        h4 = candles.get(TF_H4, [])

        if not h1:
            logger.debug("%s: no H1 candles returned", instr.epic)
            return None

        # Gate 4a — Strategy: sweep + BOS + EMA regime filter (inside GoldStrategy)
        sig = strategy.evaluate(candles)
        is_momentum_signal = False

        # Gate 4b — Momentum fallback: big ATR-sized directional move
        if sig is None and momentum_detector is not None and h1:
            direction = momentum_detector.detect(h1)
            if direction is not None:
                sig = Signal(direction=direction, lots=strategy.lots, symbol=instr.epic)
                is_momentum_signal = True
                logger.info("%s: momentum signal → %s", instr.epic, direction.upper())

        if sig is None:
            logger.debug("%s: no signal", instr.epic)
            return None

        # Gate 5 — EMA trend strength: suppress counter-trend in rocket markets.
        # Skipped for momentum signals — they are intentionally counter-trend
        # (big up move → SELL, big down move → BUY).
        if not is_momentum_signal and h4:
            closes = [c.close for c in h4]
            e20 = _ema(closes, 20)
            e50 = _ema(closes, 50)
            if (not math.isnan(e20[-1]) and not math.isnan(e50[-1]) and e50[-1] > 0):
                spread_pct = (e20[-1] - e50[-1]) / e50[-1] * 100
                if abs(spread_pct) > EMA_TREND_STRENGTH_PCT:
                    is_bull = spread_pct > 0
                    if (is_bull and sig.direction == "sell") or \
                       (not is_bull and sig.direction == "buy"):
                        logger.info(
                            "%s: EMA20/50 spread %.2f%% — suppressing counter-trend "
                            "%s in rocket market",
                            instr.epic, spread_pct, sig.direction,
                        )
                        return None

        # Gate 6 — R:R: swing-based TP must give at least MIN_RR_RATIO
        atr_series = _atr(h1, period=14)
        valid_atr  = [v for v in atr_series if v == v]
        if not valid_atr:
            logger.debug("%s: ATR unavailable — skipping", instr.epic)
            return None
        current_atr = valid_atr[-1]
        entry       = h1[-1].close

        swing_tp = _nearest_swing_tp(h1, entry, sig.direction)
        if swing_tp is None:
            logger.info("%s: no clear swing target in trade direction — skipping", instr.epic)
            return None

        sl     = entry - SL_ATR_MULT * current_atr if sig.direction == "buy" \
                 else entry + SL_ATR_MULT * current_atr
        risk   = abs(entry - sl)
        reward = abs(swing_tp - entry)
        rr     = reward / risk if risk > 0 else 0.0

        if rr < MIN_RR_RATIO:
            logger.info(
                "%s: R:R %.2f < %.1f (entry %.2f → tp %.2f, sl %.2f) — skipping",
                instr.epic, rr, MIN_RR_RATIO, entry, swing_tp, sl,
            )
            return None

        logger.info(
            "%s: signal %s  entry=%.2f  tp=%.2f  sl=%.2f  R:R=1:%.1f",
            instr.epic, sig.direction.upper(), entry, swing_tp, sl, rr,
        )
        return candles, sig.direction, swing_tp

    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        return None


def _send_alert(instr: _Instrument, candles, direction: str,
                tp: float, notifier, logger: logging.Logger) -> None:
    """Compute ATR-based SL, format the message, and send the Telegram alert."""
    try:
        h1 = candles.get(TF_H1, [])
        atr_series = _atr(h1, period=14)
        valid_atr  = [v for v in atr_series if v == v]
        if not valid_atr:
            logger.warning("%s: ATR unavailable — skipping alert", instr.epic)
            return

        current_atr = valid_atr[-1]
        entry       = h1[-1].close
        sl = entry - SL_ATR_MULT * current_atr if direction == "buy" \
             else entry + SL_ATR_MULT * current_atr

        html, plain = _build_message(instr, direction, entry, tp, sl)
        _notify(notifier, html, plain)
        instr.mark_alerted()
        _save_cooldown(instr)
        logger.info("Alert sent: %s %s  entry=%.2f  tp=%.2f  sl=%.2f  atr=%.2f",
                    instr.epic, direction.upper(), entry, tp, sl, current_atr)
    except Exception as exc:
        logger.error("%s: alert error: %s", instr.epic, exc)


# ── Health server (keeps Render free tier alive) ──────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass  # silence access logs


def _start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.getLogger(__name__).info("Health server listening on port %d", port)


# ── Entry point ───────────────────────────────────────────────────────────────

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

    # One feed per instrument (Yahoo Finance — no auth required)
    feeds: dict[str, YahooFinanceFeed] = {
        instr.epic: YahooFinanceFeed(instr.epic) for instr in WATCHLIST
    }

    _load_cooldowns(WATCHLIST)

    # Startup confirmation — lets you know the cloud run picked up cleanly
    import datetime as _dt
    _startup_time = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _notify(notifier,
            f"🟡 <b>Alert bot started</b> — <i>{_startup_time}</i>\n"
            "Watching Gold, S&amp;P 500, Nasdaq 100, Dow Jones. Scanning every 30 min.",
            f"Alert bot started {_startup_time}. Watching Gold, S&P 500, Nasdaq, Dow. Scanning every 30 min.")
    logger.info("Startup notification sent")

    strategy           = GoldStrategy()
    momentum_detector  = MomentumDetector()   # fires SELL on big up, BUY on big down
    epic_list = ", ".join(i.epic for i in WATCHLIST)

    # Optional bounded runtime (used by the cloud runner so each job exits
    # cleanly and the next queued run takes over). 0/unset = run forever.
    max_runtime_s = int(os.getenv("MAX_RUNTIME_S", "0"))
    start_time    = time.time()

    logger.info("Alert bot running — watching %s, scanning every %ds%s",
                epic_list, SCAN_INTERVAL_S,
                f", max runtime {max_runtime_s}s" if max_runtime_s else "")

    while _running:
        # ── Phase 1: evaluate every instrument, collect pending signals ────────
        pending: dict[str, tuple] = {}   # epic -> (instr, candles, direction, swing_tp)
        for instr in WATCHLIST:
            if not _running:
                break
            result = _evaluate_one(instr, feeds[instr.epic], strategy, logger, momentum_detector)
            if result is not None:
                candles, direction, swing_tp = result
                pending[instr.epic] = (instr, candles, direction, swing_tp)
            time.sleep(3)   # stagger requests to avoid Yahoo Finance rate limits

        # ── Phase 2: US index consensus — suppress the lone contradicting signal
        us_pending = {e: v for e, v in pending.items() if e in _US_INDEX_EPICS}
        if len(us_pending) >= 2:
            buy_count  = sum(1 for _, _, d, _ in us_pending.values() if d == "buy")
            sell_count = sum(1 for _, _, d, _ in us_pending.values() if d == "sell")
            if buy_count != sell_count:   # tie → no consensus, send both
                consensus = "buy" if buy_count > sell_count else "sell"
                for epic in list(pending.keys()):
                    if epic in _US_INDEX_EPICS and pending[epic][2] != consensus:
                        logger.info(
                            "%s: suppressed — contradicts US index consensus "
                            "(%d buy / %d sell → %s)", epic, buy_count, sell_count, consensus
                        )
                        del pending[epic]

        # ── Phase 3: send all approved alerts ────────────────────────────────
        for epic, (instr, candles, direction, swing_tp) in pending.items():
            _send_alert(instr, candles, direction, swing_tp, notifier, logger)

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

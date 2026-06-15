"""
main_alerts.py — multi-market alert bot (no execution, no broker login).

Watches Gold, S&P 500, Nasdaq 100, and Dow Jones via the Capital.com API.
When SmartTradingBotStrategy detects a setup it sends a Telegram message with
entry price, take profit, and stop loss — no trades are placed.

Usage:
  python main_alerts.py

Required .env keys:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  CAPITAL_API_KEY
  CAPITAL_IDENTIFIER   (your Capital.com login email)
  CAPITAL_PASSWORD

Optional:
  CAPITAL_DEMO=false   (default true — demo endpoint; prices are identical)
"""
import datetime as _dt
import json
import logging
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
from core.scalping_signal_manager import ScalpingSignalManager
from strategy.base import TF_H1
from strategy.gold_strategy import SmartTradingBotStrategy
from strategy.indicators import atr as _atr, swing_highs, swing_lows
from strategy.market_hours import is_tradeable
from strategy.capital_feed import CapitalComFeed
from strategy.scalping_config import PLAN_B_CONFIG
from strategy.scalping_strategy import ScalpingStrategy


def _utcnow() -> _dt.datetime:
    """Naive UTC datetime — avoids DeprecationWarning from utcnow()."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ── Configuration ────────────────────────────────────────────────────────

SCAN_INTERVAL_S       = 20 * 60
ALERT_COOLDOWN_S      = 60 * 60
SETUP_COOLDOWN_S      = 2 * 60 * 60
HEARTBEAT_INTERVAL_S  = 24 * 60 * 60
TP2_ATR_MULT          = 2.5
SL_ATR_MULT           = 0.5
COOLDOWN_FILE         = os.getenv("COOLDOWN_FILE", ".alert_cooldown.json")


@dataclass
class _Instrument:
    epic: str
    name: str
    _last_alert:       float = field(default=0.0, init=False, repr=False)
    _last_setup_alert: float = field(default=0.0, init=False, repr=False)

    def on_cooldown(self) -> bool:
        return time.time() - self._last_alert < ALERT_COOLDOWN_S

    def setup_on_cooldown(self) -> bool:
        return time.time() - self._last_setup_alert < SETUP_COOLDOWN_S

    def mark_alerted(self) -> None:
        self._last_alert = time.time()

    def mark_setup_alerted(self) -> None:
        self._last_setup_alert = time.time()


WATCHLIST: list[_Instrument] = [
    _Instrument("GOLD",  "Gold (XAU/USD)"),
    _Instrument("US500", "S&P 500"),
    _Instrument("US100", "Nasdaq 100"),
    _Instrument("US30",  "Dow Jones (US30)"),
]

# ── Cooldown persistence ───────────────────────────────────────────────

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


# ── Graceful shutdown ────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Alert formatting ───────────────────────────────────────────────────

def _strip(s: str) -> str:
    return s.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>","")


def _build_setup_message(instr: _Instrument, direction: str,
                         watch_level: float, comment: str = "") -> tuple[str, str]:
    t         = _utcnow().strftime("%H:%M UTC")
    dir_label = "BUY" if direction == "buy" else "SELL"
    sep       = "━━━━━━━━━━━━━━━━━━"
    lines = [
        f"🟡 <b>SETUP FORMING — {instr.name}</b>",
        sep,
        f"Direction : <b>{dir_label}</b>  (sweep detected)",
        f"Watch BOS : <b>{watch_level:,.2f}</b>",
        sep,
        f"🕐 {t}",
        "<i>Not yet confirmed — wait for BOS before entering.</i>",
    ]
    if comment:
        lines.append(f"<i>{comment}</i>")
    html  = "\n".join(lines)
    plain = "\n".join(_strip(l) for l in lines)
    return html, plain


def _build_confirmed_message(instr: _Instrument, direction: str,
                              entry: float, sl: float,
                              tp1: float | None, tp2: float,
                              comment: str = "") -> tuple[str, str]:
    t         = _utcnow().strftime("%H:%M UTC")
    emoji     = "🟢" if direction == "buy" else "🔴"
    dir_label = "BUY" if direction == "buy" else "SELL"
    sep       = "━━━━━━━━━━━━━━━━━━"

    risk  = abs(entry - sl)
    r_tp1 = abs(tp1 - entry) if tp1 else None
    r_tp2 = abs(tp2 - entry)
    rr1   = f"1 : {r_tp1/risk:.1f}" if (tp1 and risk > 0) else "—"
    rr2   = f"1 : {r_tp2/risk:.1f}" if risk > 0 else "—"

    tp1_line = (f"TP1    : <b>{tp1:,.2f}</b>  (R:R {rr1})" if tp1
                else "TP1    : — (no swing target found)")

    lines = [
        f"{emoji} <b>✅ CONFIRMED — {instr.name}</b>",
        sep,
        f"Direction : <b>{dir_label}</b>",
        f"Entry     : <b>{entry:,.2f}</b>",
        f"SL        : <b>{sl:,.2f}</b>",
        tp1_line,
        f"TP2       : <b>{tp2:,.2f}</b>  (R:R {rr2})",
        sep,
        f"🕐 {t}",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    if comment:
        lines.append(f"<i>{comment}</i>")
    html  = "\n".join(lines)
    plain = "\n".join(_strip(l) for l in lines)
    return html, plain


def _notify(notifier, html: str, plain: str) -> None:
    if hasattr(notifier, "send_html"):
        notifier.send_html(html)
    else:
        notifier.send(plain)


# ── Heartbeat ────────────────────────────────────────────────────────────

_last_heartbeat: float = 0.0


def _maybe_send_heartbeat(notifier, instruments: list, logger: logging.Logger) -> None:
    global _last_heartbeat
    if time.time() - _last_heartbeat < HEARTBEAT_INTERVAL_S:
        return
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


# ── US index consensus ─────────────────────────────────────────────────

_US_INDEX_EPICS = frozenset({"US500", "US100", "US30"})

# ── Per-instrument scan ────────────────────────────────────────────────

def _sweep_sl(h1: list, direction: str, current_atr: float) -> float:
    recent = h1[-3:] if len(h1) >= 3 else h1
    if direction == "buy":
        return min(c.low for c in recent) - SL_ATR_MULT * current_atr
    return max(c.high for c in recent) + SL_ATR_MULT * current_atr


def _nearest_swing_tp(h1: list, entry: float, direction: str) -> float | None:
    window = h1[-60:] if len(h1) >= 60 else h1
    if direction == "buy":
        levels = [v for v in swing_highs(window, lookback=3) if v is not None and v > entry]
        return min(levels) if levels else None
    levels = [v for v in swing_lows(window, lookback=3) if v is not None and v < entry]
    return max(levels) if levels else None


def _evaluate_one(instr: _Instrument, feed: CapitalComFeed,
                   strategy: SmartTradingBotStrategy, logger: logging.Logger):
    if instr.on_cooldown() and instr.setup_on_cooldown():
        logger.debug("%s: both cooldowns active — skipping", instr.epic)
        return None
    if not is_tradeable(instr.epic):
        logger.info("%s: outside market hours (FYI) — still scanning", instr.epic)
    try:
        candles = feed.get_candles()
        h1 = candles.get(TF_H1, [])
        if not h1:
            logger.debug("%s: no H1 candles returned", instr.epic)
            return None
        sig = strategy.evaluate(candles)
        if sig is None:
            logger.debug("%s: no signal", instr.epic)
            return None
        return candles, sig.direction, sig.confirmed, sig.comment
    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        return None


def _send_alert(instr: _Instrument, candles, direction: str, confirmed: bool,
                 notifier, logger: logging.Logger, comment: str = "") -> None:
    try:
        h1 = candles.get(TF_H1, [])
        atr_series = _atr(h1, period=14)
        valid_atr  = [v for v in atr_series if v == v]
        if not valid_atr:
            logger.warning("%s: ATR unavailable — skipping alert", instr.epic)
            return
        current_atr = valid_atr[-1]
        entry       = h1[-1].close

        if not instr.setup_on_cooldown():
            if direction == "buy":
                watch_levels = [v for v in swing_highs(h1[-30:], lookback=3) if v is not None]
                watch = watch_levels[-1] if watch_levels else entry
            else:
                watch_levels = [v for v in swing_lows(h1[-30:], lookback=3) if v is not None]
                watch = watch_levels[-1] if watch_levels else entry
            html, plain = _build_setup_message(instr, direction, watch, comment)
            _notify(notifier, html, plain)
            instr.mark_setup_alerted()
            logger.info("Setup alert sent: %s %s  watch=%.2f",
                        instr.epic, direction.upper(), watch)

        if confirmed and not instr.on_cooldown():
            sl  = _sweep_sl(h1, direction, current_atr)
            tp1 = _nearest_swing_tp(h1, entry, direction)
            tp2 = (entry + TP2_ATR_MULT * current_atr if direction == "buy"
                   else entry - TP2_ATR_MULT * current_atr)
            html, plain = _build_confirmed_message(instr, direction, entry, sl, tp1, tp2, comment)
            _notify(notifier, html, plain)
            instr.mark_alerted()
            _save_cooldown(instr)
            logger.info(
                "Confirmed alert sent: %s %s  entry=%.2f  sl=%.2f  tp1=%s  tp2=%.2f  atr=%.2f",
                instr.epic, direction.upper(), entry, sl,
                f"{tp1:.2f}" if tp1 else "—", tp2, current_atr,
            )
    except Exception as exc:
        logger.error("%s: alert error: %s", instr.epic, exc)


# ── Plan B alert formatting ───────────────────────────────────────────────

def _build_plan_b_message(instr: _Instrument, result) -> tuple[str, str]:
    t     = _utcnow().strftime("%H:%M UTC")
    emoji = "🟢" if result.signal == "BUY" else "🔴"
    sep   = "━━━━━━━━━━━━━━━━━━"
    risk  = abs(result.entry - result.stop_loss) if result.stop_loss else 0
    rr1   = f"1 : {abs(result.tp1 - result.entry) / risk:.1f}" if risk > 0 else "—"
    rr2   = f"1 : {abs(result.tp2 - result.entry) / risk:.1f}" if risk > 0 else "—"
    lines = [
        f"{emoji} <b>[PLAN B] {instr.name}</b> — {result.signal} (Scalp/M15)",
        sep,
        f"H1 Bias  : <b>{result.h1_bias}</b>",
        f"Entry    : <b>{result.entry:,.2f}</b>",
        f"SL       : <b>{result.stop_loss:,.2f}</b>",
        f"TP1      : <b>{result.tp1:,.2f}</b>  (R:R {rr1})",
        f"TP2      : <b>{result.tp2:,.2f}</b>  (R:R {rr2})",
        f"ATR(M15) : {result.atr_m15:.2f}",
        sep,
        f"🕐 {t}",
        "<i>⏱️ Time stop: review/close after 2h if TP1 not hit.</i>",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    if not result.in_session:
        lines.append("<i>⚠️ Outside London/NY session — FYI only.</i>")
    html  = "\n".join(lines)
    plain = "\n".join(_strip(l) for l in lines)
    return html, plain


# ── Plan B background loop ──────────────────────────────────────────────

def _plan_b_loop(feeds: dict, notifier, logger: logging.Logger) -> None:
    scalping = ScalpingStrategy(PLAN_B_CONFIG)
    signals  = ScalpingSignalManager(PLAN_B_CONFIG)

    while _running:
        now = _utcnow()

        for instr in WATCHLIST:
            if not _running:
                break
            if not is_tradeable(instr.epic):
                logger.info("[Plan B] %s: outside market hours (FYI) — still scanning", instr.epic)
            try:
                h1_df, m15_df = feeds[instr.epic].get_plan_b_candles()
                if h1_df.empty or m15_df.empty:
                    logger.debug("[Plan B] %s: empty candles", instr.epic)
                    continue

                last_price = float(m15_df["close"].iloc[-1])
                ts_msg = signals.check_time_stop(instr.epic, last_price, now)
                if ts_msg:
                    _notify(notifier,
                            f"⏱️ <b>[Plan B] {instr.name}</b>\n{ts_msg}",
                            f"[Plan B] {instr.name}: {ts_msg}")

                if not signals.can_alert(instr.epic, now):
                    continue

                result = scalping.run(h1_df, m15_df, now_utc=now)
                if result.signal is None:
                    logger.debug("[Plan B] %s: %s", instr.epic, result.reason)
                    continue

                html, plain = _build_plan_b_message(instr, result)
                _notify(notifier, html, plain)
                signals.register_alert(
                    instr.epic, result.signal, result.entry, result.tp1, now
                )
                logger.info("[Plan B] Alert: %s %s entry=%.2f sl=%.2f tp1=%.2f rr=%.2f",
                            instr.epic, result.signal, result.entry,
                            result.stop_loss, result.tp1, result.rr)

            except Exception as exc:
                logger.error("[Plan B] %s: error: %s", instr.epic, exc)

            time.sleep(2)

        if _running:
            time.sleep(PLAN_B_CONFIG.scan_interval_s)


# ── Health server ─────────────────────────────────────────────────────────────

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


# ── Entry point ────────────────────────────────────────────────────────────

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

    cap_key      = os.getenv("CAPITAL_API_KEY", "")
    cap_id       = os.getenv("CAPITAL_IDENTIFIER", "")
    cap_password = os.getenv("CAPITAL_PASSWORD", "")
    cap_demo     = os.getenv("CAPITAL_DEMO", "true").lower() != "false"

    if not (cap_key and cap_id and cap_password):
        logger.error(
            "Missing Capital.com credentials — set CAPITAL_API_KEY, "
            "CAPITAL_IDENTIFIER and CAPITAL_PASSWORD in .env / GitHub secrets."
        )
        _notify(notifier,
                "🔴 <b>Alert bot stopped</b> — missing Capital.com credentials.",
                "Alert bot stopped — missing Capital.com credentials.")
        sys.exit(1)

    try:
        feeds: dict[str, CapitalComFeed] = {
            instr.epic: CapitalComFeed(cap_key, cap_id, cap_password,
                                       epic=instr.epic, demo=cap_demo)
            for instr in WATCHLIST
        }
    except Exception as exc:
        logger.error("Capital.com login failed: %s", exc)
        _notify(notifier,
                f"🔴 <b>Alert bot stopped</b> — Capital.com login failed (demo={'on' if cap_demo else 'off'}).",
                "Alert bot stopped — Capital.com login failed.")
        sys.exit(1)

    _load_cooldowns(WATCHLIST)

    if PLAN_B_CONFIG.enabled:
        threading.Thread(
            target=_plan_b_loop, args=(feeds, notifier, logger),
            daemon=True, name="plan-b-scalping",
        ).start()
        logger.info("Plan B scalping loop started (scan every %ds)", PLAN_B_CONFIG.scan_interval_s)

    _startup_time = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _notify(notifier,
            f"🟡 <b>Alert bot started</b> — <i>{_startup_time}</i>\n"
            "Watching Gold, S&amp;P 500, Nasdaq 100, Dow Jones. Scanning every 20 min.",
            f"Alert bot started {_startup_time}. Watching Gold, S&P 500, Nasdaq, Dow.")
    logger.info("Startup notification sent")

    strategies: dict[str, SmartTradingBotStrategy] = {
        instr.epic: SmartTradingBotStrategy(epic=instr.epic)
        for instr in WATCHLIST
    }
    epic_list     = ", ".join(i.epic for i in WATCHLIST)
    max_runtime_s = int(os.getenv("MAX_RUNTIME_S", "0"))
    start_time    = time.time()

    logger.info("Alert bot running — watching %s, scanning every %ds%s",
                epic_list, SCAN_INTERVAL_S,
                f", max runtime {max_runtime_s}s" if max_runtime_s else "")

    while _running:
        pending: dict[str, tuple] = {}
        for instr in WATCHLIST:
            if not _running:
                break
            result = _evaluate_one(instr, feeds[instr.epic], strategies[instr.epic], logger)
            if result is not None:
                candles, direction, confirmed, comment = result
                pending[instr.epic] = (instr, candles, direction, confirmed, comment)
            time.sleep(3)

        us_pending = {e: v for e, v in pending.items() if e in _US_INDEX_EPICS}
        if len(us_pending) >= 2:
            buy_count  = sum(1 for _, _, d, _, _ in us_pending.values() if d == "buy")
            sell_count = sum(1 for _, _, d, _, _ in us_pending.values() if d == "sell")
            if buy_count != sell_count:
                consensus = "buy" if buy_count > sell_count else "sell"
                for epic in list(pending.keys()):
                    if epic in _US_INDEX_EPICS and pending[epic][2] != consensus:
                        logger.info("%s: suppressed — contradicts consensus (%d buy / %d sell → %s)",
                                    epic, buy_count, sell_count, consensus)
                        del pending[epic]

        for epic, (instr, candles, direction, confirmed, comment) in pending.items():
            _send_alert(instr, candles, direction, confirmed, notifier, logger, comment)

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

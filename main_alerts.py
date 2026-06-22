"""
main_alerts.py — multi-market alert bot (no execution, no broker login).

Watches US100, US500, US30 via the Capital.com API using a unified 2-gate
strategy on H1+M15. Sends a Telegram alert when a setup is confirmed.
No trades are placed.

Usage:
  python main_alerts.py

Required .env keys:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  CAPITAL_API_KEY
  CAPITAL_IDENTIFIER   (your Capital.com login email)
  CAPITAL_PASSWORD

Optional:
  CAPITAL_DEMO=false   (default true — demo endpoint)
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
from strategy.market_hours import is_tradeable
from strategy.capital_feed import CapitalComFeed
from strategy.gold_strategy import SmartTradingBotStrategy


def _utcnow() -> _dt.datetime:
    """Naive UTC datetime — avoids DeprecationWarning from utcnow()."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ── Configuration ─────────────────────────────────────────────────────────────

SCAN_INTERVAL_S      = 5 * 60        # scan every 5 min
ALERT_COOLDOWN_S     = 60 * 60       # 60-min cooldown per market
HEARTBEAT_INTERVAL_S = 24 * 60 * 60
COOLDOWN_FILE        = os.getenv("COOLDOWN_FILE", ".alert_cooldown.json")


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


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Alert formatting ───────────────────────────────────────────────────────────

def _strip(s: str) -> str:
    return s.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")


def _parse_tf_status(comment: str) -> tuple[bool, bool]:
    """Parse 'h1=1;m15=0' style comment → (h1_ok, m15_ok)."""
    parts = {}
    for chunk in comment.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts.get("h1", "0") == "1", parts.get("m15", "0") == "1"


def _build_confirmed_message(
    instr: _Instrument, sig, correlated_with: list[str] | None = None
) -> tuple[str, str]:
    t         = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    direction = sig.direction
    entry     = sig.entry
    sl        = sig.stop_loss
    tp1       = sig.take_profit
    tp2       = sig.take_profit2
    emoji     = "🔵" if direction == "buy" else "🔴"
    dir_label = "BUY" if direction == "buy" else "SELL"
    sep       = "━━━━━━━━━━━━━━━━━━━━━━"

    h1_ok, m15_ok = _parse_tf_status(sig.comment or "")
    h1_icon  = "✅" if h1_ok  else "❌"
    m15_icon = "✅" if m15_ok else "❌"
    tf_label = "+".join(t for t, v in [("H1", h1_ok), ("M15", m15_ok)] if v)

    risk = abs(entry - sl) if (entry and sl) else 0
    rr1  = f"{abs(tp1 - entry) / risk:.1f}" if (tp1 and risk > 0) else "—"

    lines = [
        f"{emoji} <b>{dir_label} — {instr.name}</b>   [{tf_label}]",
        sep,
        f"H1  → Sweep + BOS {h1_icon}",
        f"M15 → Sweep + BOS {m15_icon}",
        sep,
        f"Entry : <b>{entry:,.2f}</b>",
        f"SL    : <b>{sl:,.2f}</b>",
    ]
    if tp1:
        lines.append(f"TP1   : <b>{tp1:,.2f}</b>  ← M15 short target")
    if tp2:
        lines.append(f"TP2   : <b>{tp2:,.2f}</b>  ← H1 long target")
    lines += [
        f"R:R   : {rr1}",
        sep,
        f"🕐 {t}",
        "<i>Alert only — always confirm before trading.</i>",
    ]
    if correlated_with:
        others = " / ".join(correlated_with)
        lines.append(
            f"<i>⚠️ Also firing on {others} — US indices are correlated. "
            f"Treat all three as one exposure, not independent trades.</i>"
        )
    html  = "\n".join(lines)
    plain = "\n".join(_strip(l) for l in lines)
    return html, plain


def _notify(notifier, html: str, plain: str) -> None:
    if hasattr(notifier, "send_html"):
        notifier.send_html(html)
    else:
        notifier.send(plain)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

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


# ── US index consensus ─────────────────────────────────────────────────────────

_US_INDEX_EPICS = frozenset({"US500", "US100", "US30"})

# ── Per-instrument scan ────────────────────────────────────────────────────────

def _evaluate_one(instr: _Instrument, feed: CapitalComFeed,
                  strategy: SmartTradingBotStrategy, logger: logging.Logger):
    if instr.on_cooldown():
        logger.debug("%s: cooldown active — skipping", instr.epic)
        return None
    if not is_tradeable(instr.epic):
        logger.info("%s: outside market hours (FYI) — still scanning", instr.epic)
    try:
        candles = feed.get_h1_m15_candles()
        sig = strategy.evaluate(candles)
        if sig is None:
            logger.debug("%s: no signal", instr.epic)
            return None
        return candles, sig
    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        return None


def _send_alert(
    instr: _Instrument, sig, notifier, logger: logging.Logger,
    correlated_with: list[str] | None = None,
) -> None:
    try:
        html, plain = _build_confirmed_message(instr, sig, correlated_with)
        _notify(notifier, html, plain)
        instr.mark_alerted()
        _save_cooldown(instr)
        logger.info(
            "Alert sent: %s %s entry=%.2f sl=%.2f tp1=%s tp2=%s%s",
            instr.epic, sig.direction.upper(),
            sig.entry or 0, sig.stop_loss or 0,
            f"{sig.take_profit:.2f}" if sig.take_profit else "—",
            f"{sig.take_profit2:.2f}" if sig.take_profit2 else "—",
            f" [correlated with {correlated_with}]" if correlated_with else "",
        )
    except Exception as exc:
        logger.error("%s: alert error: %s", instr.epic, exc)


# ── Health server ──────────────────────────────────────────────────────────────

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
                f"🔴 <b>Alert bot stopped</b> — Capital.com login failed "
                f"(demo={'on' if cap_demo else 'off'}).",
                "Alert bot stopped — Capital.com login failed.")
        sys.exit(1)

    _load_cooldowns(WATCHLIST)

    _startup_time = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _notify(notifier,
            f"🟡 <b>Alert bot started</b> — <i>{_startup_time}</i>\n"
            "Watching S&amp;P 500, Nasdaq 100, Dow Jones. Scanning every 5 min.",
            f"Alert bot started {_startup_time}. Watching S&P 500, Nasdaq, Dow.")
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
        pending: dict[str, tuple] = {}   # epic → (instr, candles, sig)
        for instr in WATCHLIST:
            if not _running:
                break
            result = _evaluate_one(instr, feeds[instr.epic], strategies[instr.epic], logger)
            if result is not None:
                candles, sig = result
                pending[instr.epic] = (instr, candles, sig)
            time.sleep(3)

        # US index consensus — suppress lone contradicting signal
        us_pending = {e: v for e, v in pending.items() if e in _US_INDEX_EPICS}
        if len(us_pending) >= 2:
            buy_count  = sum(1 for _, _, s in us_pending.values() if s.direction == "buy")
            sell_count = sum(1 for _, _, s in us_pending.values() if s.direction == "sell")
            if buy_count != sell_count:
                consensus = "buy" if buy_count > sell_count else "sell"
                for epic in list(pending.keys()):
                    if epic in _US_INDEX_EPICS and pending[epic][2].direction != consensus:
                        logger.info("%s: suppressed — contradicts consensus (%d buy / %d sell → %s)",
                                    epic, buy_count, sell_count, consensus)
                        del pending[epic]

        # Build per-epic correlation map: if ≥2 US indices fire same direction,
        # warn in each alert that they're correlated (one exposure, not many).
        us_aligned = {
            e for e, (_, _, s) in pending.items()
            if e in _US_INDEX_EPICS
        }
        for epic, (instr, _candles, sig) in pending.items():
            corr = sorted(us_aligned - {epic}) if epic in _US_INDEX_EPICS and len(us_aligned) >= 2 else None
            _send_alert(instr, sig, notifier, logger, correlated_with=corr)

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

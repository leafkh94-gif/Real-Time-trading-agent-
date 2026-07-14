"""
main_alerts.py — multi-market alert bot (no execution, no broker login).

Watches US500, US100, US30 and BTCUSD via the Capital.com API and applies the
scoring strategy from the strategy document (5 patterns -> 0-100 score ->
WATCH / A+). Sends a Telegram alert when a setup clears the threshold.
No trades are placed.

Required .env keys:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD
Optional:
  CAPITAL_DEMO=false        (default true — demo endpoint)
  STATE_FILE=.bot_state.json
"""
import datetime as _dt
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.capital_feed import CapitalComFeed
from strategy import strategy_config as C
from strategy.scoring_strategy import ScoringStrategy, MarketData
from strategy.market_sessions import index_market_open, in_news_blackout


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ── Configuration ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_S      = 5 * 60         # scan every 5 min (15m candles update slower)
ALERT_COOLDOWN_S     = 90 * 60        # 90-min cooldown per market (v3)
INTER_ALERT_GAP_S    = 30 * 60        # minimum 30 min between any two alerts (v3)
HEARTBEAT_INTERVAL_S = 24 * 60 * 60
STATE_FILE           = os.getenv("STATE_FILE", ".bot_state.json")

# Watchlist is driven by the strategy config (single source of truth).
WATCHLIST = list(C.INSTRUMENTS.keys())          # US500, US30, US100, BTCUSD
_US_INDEX_GROUP = {e for e, c in C.INSTRUMENTS.items() if c["correlated_group"] == "us_indices"}


@dataclass
class _Instrument:
    epic: str
    name: str
    _last_alert: float = field(default=0.0, init=False, repr=False)

    def on_cooldown(self) -> bool:
        return time.time() - self._last_alert < ALERT_COOLDOWN_S

    def mark_alerted(self) -> None:
        self._last_alert = time.time()


INSTRUMENTS = [_Instrument(e, C.INSTRUMENTS[e]["name"]) for e in WATCHLIST]


# ── Persistent bot state (daily caps + adaptive threshold) ─────────────────────
@dataclass
class BotState:
    day: str = ""
    a_plus_today: int = 0
    watch_today: int = 0
    a_plus_threshold: float = C.A_PLUS_BASE
    no_signal_streak: int = 0          # consecutive days with zero signals
    cooldowns: dict = field(default_factory=dict)

    def roll_day(self, today: str, logger: logging.Logger) -> None:
        """Apply adaptive-threshold logic when the UTC day changes."""
        if self.day == today:
            return
        if self.day:   # not first run
            had_signals = (self.a_plus_today + self.watch_today) > 0
            if had_signals:
                self.no_signal_streak = 0
                if self.a_plus_today >= C.MAX_A_PLUS_PER_DAY:        # very active day -> raise bar
                    self.a_plus_threshold = min(C.A_PLUS_CEIL, self.a_plus_threshold + C.ADAPT_STEP_UP)
            else:
                self.no_signal_streak += 1
                if self.no_signal_streak >= C.ADAPT_NO_SIGNAL_DAYS:
                    self.a_plus_threshold = max(C.A_PLUS_FLOOR, self.a_plus_threshold - C.ADAPT_STEP_DOWN)
            logger.info("Day roll %s->%s | A+ threshold now %.0f | no-signal streak %d",
                        self.day, today, self.a_plus_threshold, self.no_signal_streak)
        self.day = today
        self.a_plus_today = 0
        self.watch_today = 0

    def can_send(self, tier: str) -> bool:
        if tier == "A+":
            return self.a_plus_today < C.MAX_A_PLUS_PER_DAY
        if C.MAX_WATCH_PER_DAY is None:
            return True
        return self.watch_today < C.MAX_WATCH_PER_DAY

    def record(self, tier: str) -> None:
        if tier == "A+":
            self.a_plus_today += 1
        else:
            self.watch_today += 1


def _load_state(logger: logging.Logger) -> BotState:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        st = BotState(**{k: data.get(k, getattr(BotState(), k)) for k in BotState().__dict__})
        for instr in INSTRUMENTS:
            instr._last_alert = float(st.cooldowns.get(instr.epic, 0.0))
        logger.info("State restored from %s (A+ threshold %.0f)", STATE_FILE, st.a_plus_threshold)
        return st
    except (FileNotFoundError, json.JSONDecodeError):
        return BotState()


def _save_state(st: BotState) -> None:
    st.cooldowns = {i.epic: i._last_alert for i in INSTRUMENTS}
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(st), f)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not save state: %s", exc)


# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True


def _handle_shutdown(sig, frame):  # noqa: ARG001
    global _running
    logging.getLogger(__name__).info("Shutdown signal — stopping alert loop")
    _running = False


# ── Market data adapter ───────────────────────────────────────────────────────
# The scoring engine needs M15 (signal/entry timeframe) + DAILY (bias) candles.
# >>> INTEGRATION POINT <<<
# Your CapitalComFeed must expose a method that returns an OHLCV DataFrame with
# columns: open, high, low, close, volume (oldest -> newest). The Capital.com
# prices endpoint supports this: GET /prices/{epic}?resolution=...&max=...
# Resolutions used here: "MINUTE_15" and "DAY".
# If your feed already has get_h1_m15_candles(), add a sibling get_candles().
def _fetch_df(feed: CapitalComFeed, resolution: str, count: int):
    """Return an OHLCV DataFrame using the feed's existing internals.

    CapitalComFeed already has _fetch(resolution, max) -> list[Candle] and
    _to_df(candles) -> DataFrame(open,high,low,close,volume). We reuse those
    rather than its public get_candles() (which returns H4+H1 and takes no args).
    """
    candles = feed._fetch(resolution, count)
    return feed._to_df(candles)


def _load_market_data(feed: CapitalComFeed, epic: str, now: _dt.datetime) -> MarketData:
    m15 = _fetch_df(feed, "MINUTE_15", 220)
    daily = _fetch_df(feed, "DAY", 260)
    return MarketData(epic=epic, m15=m15, daily=daily, now_utc=now)


# ── Alert formatting (Arabic + English technical terms) ────────────────────────
_SEP = "━━━━━━━━━━━━━━━━━━━━"


def _fmt(epic: str, price: float) -> str:
    dec = 1 if C.INSTRUMENTS[epic]["asset"] == "crypto" else 2
    return f"{price:,.{dec}f}"


def _entry_label(pattern: str) -> str:
    return ("50% retrace limit" if C.PATTERNS[pattern]["type"] == "breakout"
            else "limit at structure — wait for retest")


def _build_message(sig) -> tuple[str, str]:
    emoji = "🔵" if sig.direction == "buy" else "🔴"
    dir_label = "BUY" if sig.direction == "buy" else "SELL"
    tier_icon = "🟢 A+" if sig.tier == "A+" else "⚡ WATCH"
    f = lambda p: _fmt(sig.epic, p)

    lines = [
        f"{tier_icon}  |  {emoji} <b>{dir_label} — {sig.name}</b>  |  score {sig.score}",
        _SEP,
        f"Pattern : {sig.pattern_label}",
        f"Entry   : <b>{f(sig.entry)}</b>  ({_entry_label(sig.pattern)})",
        f"SL      : <b>{f(sig.stop_loss)}</b>",
        f"TP1     : <b>{f(sig.take_profit)}</b>   (R:R {sig.rr:.1f})",
        f"TP2     : <b>{f(sig.take_profit2)}</b>",
        _SEP,
        "<b>الأسباب:</b>",
    ]
    lines += [f"• {r}" for r in sig.reasons]
    if sig.expiry_utc:
        lines += [_SEP, f"🕐 صلاحية الإعداد حتى {sig.expiry_utc.strftime('%H:%M UTC')}"]
    lines.append("<i>تنبيه فقط — أكّدي قبل التنفيذ.</i>")

    html = "\n".join(lines)
    plain = html.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    return html, plain


def _notify(notifier, html: str, plain: str) -> None:
    if hasattr(notifier, "send_html"):
        notifier.send_html(html)
    else:
        notifier.send(plain)


# ── Heartbeat ─────────────────────────────────────────────────────────────────
_last_heartbeat: float = 0.0


def _maybe_send_heartbeat(notifier, instruments, logger) -> None:
    global _last_heartbeat
    if time.time() - _last_heartbeat < HEARTBEAT_INTERVAL_S:
        return
    if any(time.time() - i._last_alert < HEARTBEAT_INTERVAL_S for i in instruments):
        _last_heartbeat = time.time()
        return
    markets = ", ".join(i.name for i in instruments)
    html = ("🤖 <b>Alert bot — daily check-in</b>\n"
            f"<i>Watching: {markets}</i>\nNo setups in the last 24h — running normally.")
    _notify(notifier, html, html.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    _last_heartbeat = time.time()
    logger.info("Daily heartbeat sent")


# ── Per-instrument scan ────────────────────────────────────────────────────────
def _evaluate_one(instr, feed, strategy, logger, now):
    if instr.on_cooldown():
        logger.debug("%s: cooldown — skipping", instr.epic)
        return None
    cfg = C.INSTRUMENTS[instr.epic]
    if not cfg["always_open"] and not index_market_open(now):
        logger.debug("%s: index market closed — skipping", instr.epic)
        return None
    try:
        md = _load_market_data(feed, instr.epic, now)
        strategy.a_plus_threshold = _CURRENT_THRESHOLD     # keep engine in sync with adaptive state
        return strategy.evaluate(md)
    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        return None


def _send_alert(instr, sig, notifier, logger) -> None:
    global _last_any_alert
    try:
        html, plain = _build_message(sig)
        _notify(notifier, html, plain)
        instr.mark_alerted()
        _last_any_alert = time.time()
        logger.info("Alert sent: %s %s %s score=%d entry=%s sl=%s",
                    instr.epic, sig.tier, sig.direction.upper(), sig.score,
                    _fmt(instr.epic, sig.entry), _fmt(instr.epic, sig.stop_loss))
    except Exception as exc:
        logger.error("%s: alert error: %s", instr.epic, exc)


# ── Health server ──────────────────────────────────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass


def _start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.getLogger(__name__).info("Health server listening on port %d", port)


# adaptive threshold shared with the engine each cycle
_CURRENT_THRESHOLD: float = C.A_PLUS_BASE
# timestamp of the last alert sent (any instrument) — enforces inter-alert gap
_last_any_alert: float = 0.0


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    global _CURRENT_THRESHOLD
    setup_logging()
    logger = logging.getLogger(__name__)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    _start_health_server()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        notifier = TelegramNotifier(bot_token, chat_id)
        logger.info("Telegram notifier ready (chat_id=%s)", chat_id)
    else:
        notifier = NullNotifier()
        logger.warning("No Telegram credentials — alerts will be logged only.")

    cap_key = os.getenv("CAPITAL_API_KEY", "")
    cap_id = os.getenv("CAPITAL_IDENTIFIER", "")
    cap_password = os.getenv("CAPITAL_PASSWORD", "")
    cap_demo = os.getenv("CAPITAL_DEMO", "true").lower() != "false"
    if not (cap_key and cap_id and cap_password):
        logger.error("Missing Capital.com credentials.")
        _notify(notifier, "🔴 <b>Alert bot stopped</b> — missing Capital.com credentials.",
                "Alert bot stopped — missing Capital.com credentials.")
        sys.exit(1)

    try:
        feeds = {e: CapitalComFeed(cap_key, cap_id, cap_password, epic=e, demo=cap_demo)
                 for e in WATCHLIST}
    except Exception as exc:
        logger.error("Capital.com login failed: %s", exc)
        _notify(notifier, "🔴 <b>Alert bot stopped</b> — Capital.com login failed.",
                "Alert bot stopped — Capital.com login failed.")
        sys.exit(1)

    state = _load_state(logger)
    _CURRENT_THRESHOLD = state.a_plus_threshold
    strategies = {e: ScoringStrategy(e, a_plus_threshold=state.a_plus_threshold) for e in WATCHLIST}

    started = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _notify(notifier,
            f"🟡 <b>Alert bot started</b> — <i>{started}</i>\n"
            "Watching S&amp;P 500, Nasdaq 100, Dow Jones, Bitcoin. Scanning every 5 min.",
            f"Alert bot started {started}. Watching US500, US100, US30, BTCUSD.")
    logger.info("Startup notification sent — watching %s", ", ".join(WATCHLIST))

    max_runtime_s = int(os.getenv("MAX_RUNTIME_S", "0"))
    start_time = time.time()

    while _running:
        now = _utcnow()
        state.roll_day(now.strftime("%Y-%m-%d"), logger)
        _CURRENT_THRESHOLD = state.a_plus_threshold

        # News blackout: scan but don't emit (Section V)
        blackout = in_news_blackout(now)

        candidates = {}      # epic -> signal
        for instr in INSTRUMENTS:
            if not _running:
                break
            sig = _evaluate_one(instr, feeds[instr.epic], strategies[instr.epic], logger, now)
            if sig is not None:
                candidates[instr.epic] = sig
            time.sleep(2)

        if blackout and candidates:
            logger.info("News blackout active — suppressing %d candidate(s)", len(candidates))
            candidates = {}

        # Correlation filter: among US indices, keep only the strongest; BTC exempt.
        us_hits = {e: s for e, s in candidates.items() if e in _US_INDEX_GROUP}
        if len(us_hits) > 1:
            best = max(us_hits, key=lambda e: us_hits[e].score)
            for e in list(us_hits):
                if e != best:
                    logger.info("%s: suppressed by correlation filter (kept %s)", e, best)
                    candidates.pop(e, None)

        # Emit, honoring daily caps and inter-alert gap
        instr_by_epic = {i.epic: i for i in INSTRUMENTS}
        for epic, sig in sorted(candidates.items(), key=lambda kv: -kv[1].score):
            if not state.can_send(sig.tier):
                logger.info("%s: %s daily cap reached — skipping", epic, sig.tier)
                continue
            if time.time() - _last_any_alert < INTER_ALERT_GAP_S:
                logger.info("%s: inter-alert gap active — skipping", epic)
                continue
            _send_alert(instr_by_epic[epic], sig, notifier, logger)
            state.record(sig.tier)

        _save_state(state)
        _maybe_send_heartbeat(notifier, INSTRUMENTS, logger)

        if max_runtime_s and (time.time() - start_time) >= max_runtime_s:
            logger.info("Max runtime reached — exiting cleanly.")
            break
        if _running:
            time.sleep(SCAN_INTERVAL_S)

    _save_state(state)
    logger.info("Alert bot stopped cleanly.")


if __name__ == "__main__":
    main()

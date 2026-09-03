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
from collections import Counter
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from alerts.notifier import NullNotifier, TelegramNotifier
from core.log_sanitizer import setup_logging
from strategy.capital_feed import CapitalComFeed
from strategy import journal
from strategy import strategy_config as C
from strategy.scoring_strategy import ScoringStrategy, MarketData, funnel_report
from strategy.market_sessions import index_market_open, in_news_blackout


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ── Configuration ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_S      = 30 * 60        # scan every 30 min (H1 candles form every 60 min)
ALERT_COOLDOWN_S     = 4 * 60 * 60   # 4-hour cooldown per market — H1 trades need room to develop
HEARTBEAT_INTERVAL_S = 24 * 60 * 60
# The bot restarts every 90 min (MAX_RUNTIME_S) so the journal artifact uploads.
# Only announce a restart this often — a chained handoff is not news.
STARTUP_NOTIFY_INTERVAL_S = 12 * 60 * 60
STATE_FILE           = os.getenv("STATE_FILE", ".bot_state.json")

# Watchlist is driven by the strategy config (single source of truth).
WATCHLIST = list(C.INSTRUMENTS.keys())          # US500, US30, US100, BTCUSD


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

# Gates that live outside the scoring engine: they reject a whole scan of an
# instrument rather than a bar's candidate, so they are counted separately and
# reported without a percentage. Drained into BotState after every scan.
_LIVE_GATES: Counter = Counter()


# ── Persistent bot state (daily caps + adaptive threshold) ─────────────────────
@dataclass
class BotState:
    day: str = ""
    week: str = ""                     # ISO year-week of last weekly stats report
    a_plus_today: int = 0
    watch_today: int = 0
    a_plus_threshold: float = C.A_PLUS_BASE
    no_signal_streak: int = 0          # consecutive days with zero signals
    cooldowns: dict = field(default_factory=dict)
    # Notification bookkeeping. These MUST live in persisted state, not in
    # module globals: the bot now exits every 90 min so the journal uploads,
    # and anything kept in memory resets on each self-chain handoff — which
    # turned one startup message per ~6h into one per 90 min.
    last_start_notify: float = 0.0     # epoch of last "bot started" message
    last_heartbeat: float = 0.0        # epoch of last daily check-in
    # Today's rejection funnel. Persisted for the same reason as the two fields
    # above: the bot exits every 90 minutes so the journal can upload, and a
    # counter held in memory would restart from zero on every self-chain
    # handoff — reporting a fraction of the day and reading as a quiet market.
    funnel: dict = field(default_factory=dict)

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
        self.funnel = {}

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
# The scoring engine needs H1 (signal/entry timeframe) + DAILY (bias) candles.
# >>> INTEGRATION POINT <<<
# Your CapitalComFeed must expose a method that returns an OHLCV DataFrame with
# columns: open, high, low, close, volume (oldest -> newest). The Capital.com
# prices endpoint supports this: GET /prices/{epic}?resolution=...&max=...
# Resolutions used here: "HOUR" and "DAY".
def _fetch_df(feed: CapitalComFeed, resolution: str, count: int):
    """Return an OHLCV DataFrame using the feed's existing internals.

    CapitalComFeed already has _fetch(resolution, max) -> list[Candle] and
    _to_df(candles) -> DataFrame(open,high,low,close,volume). We reuse those
    rather than its public get_candles() (which returns H4+H1 and takes no args).
    """
    candles = feed._fetch(resolution, count)
    return feed._to_df(candles)


def _closed_h1(feed: CapitalComFeed, count: int = 201):
    """Closed H1 candles only — Capital.com includes the still-forming candle,
    and a pattern 'confirmed' mid-bar can vanish by candle close (repaint)."""
    df = _fetch_df(feed, "HOUR", count)
    return df.iloc[:-1].reset_index(drop=True) if len(df) else df


def _load_market_data(feed: CapitalComFeed, epic: str, now: _dt.datetime) -> MarketData:
    # Fetch one extra bar and drop the forming one on both timeframes so
    # signals only ever fire on closed candles.
    h1    = _closed_h1(feed)               # ~8 trading days of closed H1 candles
    daily = _fetch_df(feed, "DAY", 261)
    if len(daily):
        daily = daily.iloc[:-1].reset_index(drop=True)
    return MarketData(epic=epic, h1=h1, daily=daily, now_utc=now)


# ── Alert formatting (Arabic + English technical terms) ────────────────────────
_SEP = "━━━━━━━━━━━━━━━━━━━━"


def _fmt(epic: str, price: float) -> str:
    dec = 1 if C.INSTRUMENTS[epic]["asset"] == "crypto" else 2
    return f"{price:,.{dec}f}"


def _entry_label(epic: str, pattern: str) -> str:
    if C.INSTRUMENTS[epic].get("entry_mode", "retrace_limit") == "bos_close":
        return "market — enter now at BOS close"
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
        f"Entry   : <b>{f(sig.entry)}</b>  ({_entry_label(sig.epic, sig.pattern)})",
        f"SL      : <b>{f(sig.stop_loss)}</b>",
    ]
    if getattr(sig, "breakeven_at", None) is not None:
        lines.append(f"BE      : <b>{f(sig.breakeven_at)}</b>   "
                     f"(move SL to entry once reached)")
    lines += [
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


# ── Rejection funnel ──────────────────────────────────────────────────────────
def _drain_funnels(strategies, state, logger) -> None:
    """Fold this scan's counters into the persisted daily funnel.

    The engine counters are drained (not copied) so each strategy object holds
    one scan at a time while BotState accumulates the day — otherwise every
    scan would re-add the running total.
    """
    for strat in strategies.values():
        for stage, n in strat.funnel.items():
            state.funnel[stage] = state.funnel.get(stage, 0) + n
        strat.funnel.clear()
    for stage, n in _LIVE_GATES.items():
        state.funnel[stage] = state.funnel.get(stage, 0) + n
    _LIVE_GATES.clear()
    logger.info("Funnel today:\n%s", funnel_report(state.funnel))


# ── Heartbeat ─────────────────────────────────────────────────────────────────
def _maybe_send_heartbeat(notifier, instruments, logger, state) -> None:
    """Daily 'still running' check-in. Timing lives in persisted state so it
    survives the 90-minute self-chain handoff — as a module global it fired
    once per run instead of once per day."""
    if time.time() - state.last_heartbeat < HEARTBEAT_INTERVAL_S:
        return
    if any(time.time() - i._last_alert < HEARTBEAT_INTERVAL_S for i in instruments):
        state.last_heartbeat = time.time()
        return
    markets = ", ".join(i.name for i in instruments)
    # "No setups" on its own is indistinguishable from a broken bot. Naming the
    # gate that rejected the most candidates makes a quiet day readable: a
    # market with no patterns is a different situation from a market full of
    # patterns that all failed one filter.
    html = ("🤖 <b>Alert bot — daily check-in</b>\n"
            f"<i>Watching: {markets}</i>\nNo setups in the last 24h — running normally.")
    detail = funnel_report(state.funnel, indent="")
    if state.funnel:
        html += f"\n\n<b>Why nothing fired today</b>\n<code>{detail}</code>"
    _notify(notifier, html, html.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    state.last_heartbeat = time.time()
    logger.info("Daily heartbeat sent")


# ── Per-instrument scan ────────────────────────────────────────────────────────
def _evaluate_one(instr, feed, strategy, logger, now):
    if instr.on_cooldown():
        logger.debug("%s: cooldown — skipping", instr.epic)
        _LIVE_GATES["cooldown"] += 1
        return None
    cfg = C.INSTRUMENTS[instr.epic]
    if not cfg["always_open"] and not index_market_open(now):
        logger.debug("%s: index market closed — skipping", instr.epic)
        _LIVE_GATES["market_closed"] += 1
        return None
    try:
        md = _load_market_data(feed, instr.epic, now)
        strategy.a_plus_threshold = _CURRENT_THRESHOLD     # keep engine in sync with adaptive state
        return strategy.evaluate(md)
    except Exception as exc:
        logger.error("%s: evaluation error: %s", instr.epic, exc)
        _LIVE_GATES["eval_error"] += 1
        return None


def _send_alert(instr, sig, notifier, logger) -> None:
    global _last_any_alert
    try:
        html, plain = _build_message(sig)
        _notify(notifier, html, plain)
        instr.mark_alerted()
        _last_any_alert = time.time()
        try:
            journal.record_signal(sig, _utcnow())
        except Exception as exc:
            logger.warning("Journal record failed: %s", exc)
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

    # Announce a genuine (re)start, not a routine self-chain handoff. The bot
    # exits every 90 min so the journal uploads, so notifying unconditionally
    # meant ~16 identical "started" messages a day. A cold start after a real
    # outage is still worth knowing about, hence the interval rather than
    # silence.
    started = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if time.time() - state.last_start_notify >= STARTUP_NOTIFY_INTERVAL_S:
        _notify(notifier,
                f"🟡 <b>Alert bot started</b> — <i>{started}</i>\n"
                "Watching S&amp;P 500, Nasdaq 100, Dow Jones, Bitcoin. Scanning every 30 min on H1 candles.",
                f"Alert bot started {started}. Watching US500, US100, US30, BTCUSD.")
        state.last_start_notify = time.time()
        _save_state(state)
        logger.info("Startup notification sent — watching %s", ", ".join(WATCHLIST))
    else:
        logger.info("Startup at %s — notification suppressed (chained handoff); "
                    "watching %s", started, ", ".join(WATCHLIST))

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
            _LIVE_GATES["news_blackout"] += len(candidates)
            candidates = {}

        # Correlation filter and inter-alert gap removed by request. Every
        # qualifying setup is now emitted, subject only to the per-instrument
        # cooldown and the daily caps. Note this means the three US indices can
        # alert simultaneously; they are highly correlated, so taking all three
        # is closer to one position at triple size than to three independent
        # trades. Position sizing is the user's call.
        instr_by_epic = {i.epic: i for i in INSTRUMENTS}
        for epic, sig in sorted(candidates.items(), key=lambda kv: -kv[1].score):
            if not state.can_send(sig.tier):
                logger.info("%s: %s daily cap reached — skipping", epic, sig.tier)
                _LIVE_GATES["daily_cap"] += 1
                continue
            _send_alert(instr_by_epic[epic], sig, notifier, logger)
            state.record(sig.tier)

        _drain_funnels(strategies, state, logger)

        # Resolve outcomes of past alerts against closed candles (the feedback loop)
        try:
            journal.update_outcomes(lambda epic: _closed_h1(feeds[epic]), now)
        except Exception as exc:
            logger.warning("Journal outcome update failed: %s", exc)

        # Weekly stats report (fires on the first scan of each new ISO week).
        # Reports the 7-day cohort AND cumulative: the recent window is biased
        # downward because losers resolve at 1R while winners need 2-3R.
        iso_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if state.week != iso_week:
            if state.week:                       # skip the very first run ever
                try:
                    entries = journal.load()
                    if entries:
                        last7 = journal.stats(entries, since=now - _dt.timedelta(days=7))
                        cum   = journal.stats(entries)
                        report = ("<b>Last 7 days</b>\n" + journal.format_stats(last7)
                                  + "\n\n<b>All time</b>\n" + journal.format_stats(cum))
                        _notify(notifier, "📊 <b>Weekly signal report</b>\n" + report,
                                "Weekly signal report\n" + report)
                        logger.info("Weekly stats report sent")

                        # Per-trade detail. The aggregate hides which trades ran
                        # into profit and by how much, and the journal file is
                        # otherwise trapped on the runner — send it and log it.
                        rows = journal.trade_rows(entries, statuses={"sl_hit"})
                        if rows:
                            table = journal.format_trade_table(
                                rows, "Losses that moved into profit:")
                            _notify(notifier,
                                    "🔍 <b>Loss detail</b>\n<pre>" + table + "</pre>",
                                    "Loss detail\n" + table)
                            logger.info("Per-trade loss table:\n%s", table)
                except Exception as exc:
                    logger.warning("Weekly stats failed: %s", exc)
            state.week = iso_week

        _save_state(state)
        _maybe_send_heartbeat(notifier, INSTRUMENTS, logger, state)

        if max_runtime_s and (time.time() - start_time) >= max_runtime_s:
            logger.info("Max runtime reached — exiting cleanly.")
            break
        if _running:
            time.sleep(SCAN_INTERVAL_S)

    _save_state(state)
    logger.info("Alert bot stopped cleanly.")


if __name__ == "__main__":
    main()

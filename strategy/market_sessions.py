"""
market_sessions.py — session-timing score (Factor 4), news blackout, and the
index market-hours guard.

News blackout uses fixed UTC windows from v3 (12:25–13:05 and 13:25–14:05 UTC).
BTC US-overlap session is DST-aware (anchored to 09:30–16:00 ET via zoneinfo).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from . import strategy_config as C
from .strategy_config import (
    BTC_US_OVERLAP_BONUS, BTC_EUROPE_BONUS,
    BTC_ASIA_BONUS, BTC_WEEKEND_PENALTY,
)


def _tables():
    """Session weights in force — read at call time, not import time, so a
    backtest can switch modes without reimporting the module."""
    return (C.SESSION_TABLES_MEASURED if C.SESSION_WEIGHTS_MODE == "measured"
            else C.SESSION_TABLES)

_ET = ZoneInfo("America/New_York")


def _in_window(t: dt.time, start: tuple[int, int], end: tuple[int, int]) -> bool:
    s = dt.time(*start)
    e = dt.time(*end)
    if s <= e:
        return s <= t < e
    return t >= s or t < e          # wraps midnight


def session_score(epic_session: str, now_utc: dt.datetime) -> int:
    """Factor 4 score for the given instrument session profile."""
    t  = now_utc.time()
    wd = now_utc.weekday()          # 0=Mon … 6=Sun

    if epic_session == "btc":
        if wd >= 5:                 # Sat/Sun
            return BTC_WEEKEND_PENALTY
        # US equity cash session — DST-aware (09:30–16:00 ET)
        if _et_between(now_utc, (9, 30), (16, 0)):
            return BTC_US_OVERLAP_BONUS
        if _in_window(t, (7, 0), (13, 30)):
            return BTC_EUROPE_BONUS
        return BTC_ASIA_BONUS

    table = _tables().get(epic_session, [])
    for start, end, pts in table:
        if _in_window(t, start, end):
            return pts
    return 0


def session_label(epic_session: str, now_utc: dt.datetime) -> str:
    """Name of the session that `session_score` matched — for diagnostics.

    Mirrors session_score's branching exactly. A 3-4 level label is analysable;
    the raw UTC hour is a 24-level dimension that needs far more samples.
    """
    t  = now_utc.time()
    wd = now_utc.weekday()

    if epic_session == "btc":
        if wd >= 5:
            return "btc_weekend"
        if _et_between(now_utc, (9, 30), (16, 0)):
            return "btc_us_overlap"
        if _in_window(t, (7, 0), (13, 30)):
            return "btc_europe"
        return "btc_asia"

    names = {(12, 30): "new_york", (7, 0): "london", (0, 0): "asia"}
    for start, end, _pts in _tables().get(epic_session, []):
        if _in_window(t, start, end):
            return names.get(tuple(start), f"{start[0]:02d}{start[1]:02d}")
    return "off_hours"


def _et_between(now_utc: dt.datetime, start_et: tuple[int, int], end_et: tuple[int, int]) -> bool:
    et = now_utc.replace(tzinfo=dt.timezone.utc).astimezone(_ET)
    return dt.time(*start_et) <= et.time() < dt.time(*end_et)


# ── News blackout — fixed UTC windows (v3) ───────────────────────────────────
# 12:25–13:05 UTC covers the 08:30 ET / 12:30 UTC US data release.
# 13:25–14:05 UTC covers the 09:30/10:00 ET / 13:30/14:00 UTC releases.
_NEWS_BLACKOUTS_UTC = [
    (dt.time(12, 25), dt.time(13, 5)),
    (dt.time(13, 25), dt.time(14, 5)),
]


def in_news_blackout(now_utc: dt.datetime) -> bool:
    if now_utc.weekday() >= 5:      # weekends only — no US data releases
        return False
    t = now_utc.time()
    for start, end in _NEWS_BLACKOUTS_UTC:
        if start <= t < end:
            return True
    return False


# ── Index market-hours guard ──────────────────────────────────────────────────
# Indices: open Sun 22:00 UTC → Fri 21:00 UTC, daily maintenance 21:00–22:00 UTC.
# v3: also block 15 min before daily close (20:45 UTC) — "hard flat" rule.
# BTC (always_open) bypasses this entirely.
def index_market_open(now_utc: dt.datetime) -> bool:
    wd = now_utc.weekday()
    t  = now_utc.time()
    if wd == 5:                                     # Saturday — closed
        return False
    if wd == 6:                                     # Sunday — opens 22:00 UTC
        return t >= dt.time(22, 0)
    if wd == 4 and t >= dt.time(21, 0):             # Friday close
        return False
    if dt.time(20, 45) <= t < dt.time(22, 0):       # pre-close + maintenance
        return False
    return True

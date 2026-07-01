"""
market_sessions.py — session-timing score (Factor 5), news blackout, and the
index market-hours guard.

The index session bonuses use the literal UTC windows from the doc. The news
blackout and BTC US-overlap are anchored to US Eastern time and converted with
zoneinfo, so they stay correct across the EDT/EST switch.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .strategy_config import (
    SESSION_TABLES, BTC_US_OVERLAP_BONUS, BTC_EUROPE_BONUS,
    BTC_ASIA_BONUS, BTC_WEEKEND_PENALTY,
)

_ET = ZoneInfo("America/New_York")


def _in_window(t: dt.time, start: tuple[int, int], end: tuple[int, int]) -> bool:
    s = dt.time(*start)
    e = dt.time(*end)
    if s <= e:
        return s <= t < e
    return t >= s or t < e          # wraps midnight


def session_score(epic_session: str, now_utc: dt.datetime) -> int:
    """Factor 5 score for the given instrument session profile."""
    t  = now_utc.time()
    wd = now_utc.weekday()          # 0=Mon … 6=Sun

    if epic_session == "btc":
        if wd >= 5:                 # Sat/Sun
            return BTC_WEEKEND_PENALTY
        if _et_between(now_utc, (9, 30), (16, 0)):
            return BTC_US_OVERLAP_BONUS
        if _in_window(t, (7, 0), (13, 30)):
            return BTC_EUROPE_BONUS
        return BTC_ASIA_BONUS

    table = SESSION_TABLES.get(epic_session, [])
    for start, end, pts in table:
        if _in_window(t, start, end):
            return pts
    return 0


def _et_between(now_utc: dt.datetime, start_et: tuple[int, int], end_et: tuple[int, int]) -> bool:
    et = now_utc.replace(tzinfo=dt.timezone.utc).astimezone(_ET)
    return dt.time(*start_et) <= et.time() < dt.time(*end_et)


# ── News blackout — anchored to Eastern release times ──────────────────────────
_NEWS_BLACKOUTS_ET = [
    ((8, 25),  (9, 5)),     # around the 08:30 ET release
    ((9, 25),  (10, 5)),    # around the 09:30/10:00 ET releases
]


def in_news_blackout(now_utc: dt.datetime) -> bool:
    et = now_utc.replace(tzinfo=dt.timezone.utc).astimezone(_ET)
    if et.weekday() >= 5:
        return False
    for start, end in _NEWS_BLACKOUTS_ET:
        if dt.time(*start) <= et.time() < dt.time(*end):
            return True
    return False


# ── Index market-hours guard ────────────────────────────────────────────────
# Indices: open Sun 22:00 UTC → Fri 21:00 UTC, daily maintenance 21:00–22:00 UTC.
def index_market_open(now_utc: dt.datetime) -> bool:
    wd = now_utc.weekday()
    t  = now_utc.time()
    if wd == 5:                                    # Saturday
        return False
    if wd == 6:                                    # Sunday: opens 22:00 UTC
        return t >= dt.time(22, 0)
    if wd == 4 and t >= dt.time(21, 0):            # Friday close
        return False
    if dt.time(21, 0) <= t < dt.time(22, 0):       # daily maintenance
        return False
    return True

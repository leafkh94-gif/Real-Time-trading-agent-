"""
Market-hours guard for US index and commodity watchlist instruments.

All watchlist instruments (US500, US100, US30, GOLD) trade near-24h:
  Open:  Sunday 18:00 ET  → Friday 17:00 ET
  Daily maintenance break: 17:00–18:00 ET every day
  Fully closed: all day Saturday, Sunday before 18:00 ET

A 30-minute pre-close buffer (from 16:30 ET) blocks alerts right before the
daily 17:00 ET close — too little time to execute cleanly.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_DAILY_CLOSE    = time(17, 0)   # 5:00 PM ET — daily close / start of maintenance
_REOPEN         = time(18, 0)   # 6:00 PM ET — session reopen
_PRECLOSE_GUARD = time(16, 30)  # 30-min buffer before the daily close


def is_tradeable(epic: str | None = None, now_utc: datetime | None = None) -> bool:
    """
    Return True when it is safe to send a trade alert.

    All watchlist instruments share the near-24h futures schedule:
      blocked all Saturday, Sunday before 18:00 ET, Friday from 16:30 ET,
      and the daily 16:30–18:00 ET window (pre-close buffer + maintenance).
    The *epic* argument is accepted for API compatibility but unused.
    """
    if now_utc is None:
        now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_et  = now_utc.astimezone(_ET)
    weekday = now_et.weekday()   # 0=Mon … 5=Sat … 6=Sun
    t       = now_et.time()

    if weekday == 5:                               # Saturday: fully closed
        return False
    if weekday == 6 and t < _REOPEN:              # Sunday before 18:00 ET
        return False
    if weekday == 4 and t >= _PRECLOSE_GUARD:     # Friday: stop 30 min before close
        return False
    if _PRECLOSE_GUARD <= t < _REOPEN:            # daily pre-close buffer + maintenance
        return False
    return True

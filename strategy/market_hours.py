"""
Market-hours guard — blocks alerts outside trading sessions, within
30 minutes of close, and on US market holidays.

US equity indices (US500, US100, US30): Mon-Fri 09:30-16:00 ET
  → no alert after 15:30 ET (30-min pre-close buffer)
  → closed on all dates in _HOLIDAYS
Gold futures (GC=F / GOLD): near-24h, Sun 18:00 – Fri 17:00 ET
  → 1-hour daily maintenance break 17:00-18:00 ET, fully closed Saturday
  → CME observes US bank holidays — also blocked on _HOLIDAYS dates
"""
import datetime
from datetime import time, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_EQUITY_OPEN   = time(9, 30)
_EQUITY_CLOSE  = time(16, 0)
_CLOSE_BUFFER  = timedelta(minutes=30)

_EQUITY_EPICS  = frozenset({"US500", "US100", "US30"})

# US market holidays 2026 — NYSE and CME fully closed.
# Jul 4 is a Saturday → observed closure is Fri Jul 3 (both listed for safety).
_HOLIDAYS: frozenset[str] = frozenset({
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day observed (Jul 4 falls on Saturday)
    "2026-07-04",  # Independence Day (Saturday — equities already skip weekends)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
})


def is_tradeable(epic: str, now_utc: datetime.datetime | None = None) -> bool:
    """
    Return True when it is safe to send a trade alert for *epic*.

    US indices: must be Mon-Fri AND between 09:30 and 15:30 ET,
                AND not a US market holiday.
    Gold/other: blocked on Saturday, Sunday before 18:00 ET,
                Friday from 17:00 ET, the daily 17:00-18:00 ET break,
                AND US market holidays (CME observes reduced trading).
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(tz=ZoneInfo("UTC"))
    now_et  = now_utc.astimezone(_ET)
    weekday = now_et.weekday()   # 0=Mon … 6=Sun
    t       = now_et.time()
    date_s  = now_et.strftime("%Y-%m-%d")

    # Block on known holidays for all instruments
    if date_s in _HOLIDAYS:
        return False

    if epic in _EQUITY_EPICS:
        if weekday >= 5:          # weekend
            return False
        cutoff = (
            datetime.datetime.combine(now_et.date(), _EQUITY_CLOSE, tzinfo=_ET)
            - _CLOSE_BUFFER
        ).time()                  # 15:30 ET
        return _EQUITY_OPEN <= t <= cutoff

    # Gold and any other near-24h instrument
    if weekday == 5:              # Saturday: fully closed
        return False
    if weekday == 6 and t < time(18, 0):   # Sunday before session open
        return False
    if weekday == 4 and t >= time(17, 0):  # Friday: session ends 17:00 ET
        return False
    if time(17, 0) <= t < time(18, 0):     # daily maintenance break
        return False
    return True

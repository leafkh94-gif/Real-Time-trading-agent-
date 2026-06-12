"""
Market-hours guard — blocks alerts outside trading sessions or within
30 minutes of close, where execution is poor and setups often gap away.

US equity indices (US500, US100, US30): Mon-Fri 09:30-16:00 ET
  → no alert after 15:30 ET (30-min pre-close buffer)
Gold futures (GC=F / GOLD): near-24h, Sun 18:00 – Fri 17:00 ET
  → 1-hour daily maintenance break 17:00-18:00 ET, fully closed Saturday
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_EQUITY_OPEN   = time(9, 30)
_EQUITY_CLOSE  = time(16, 0)
_CLOSE_BUFFER  = timedelta(minutes=30)

_EQUITY_EPICS  = frozenset({"US500", "US100", "US30"})


def is_tradeable(epic: str, now_utc: datetime | None = None) -> bool:
    """
    Return True when it is safe to send a trade alert for *epic*.

    US indices: must be Mon-Fri AND between 09:30 and 15:30 ET.
    Gold/other: blocked on Saturday, Sunday before 18:00 ET,
                Friday from 17:00 ET, and the daily 17:00-18:00 ET break.
    """
    if now_utc is None:
        now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_et  = now_utc.astimezone(_ET)
    weekday = now_et.weekday()   # 0=Mon … 6=Sun
    t       = now_et.time()

    if epic in _EQUITY_EPICS:
        if weekday >= 5:          # weekend
            return False
        cutoff = (
            datetime.combine(now_et.date(), _EQUITY_CLOSE, tzinfo=_ET)
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

"""
Economic news filter — blocks alerts around high-impact USD calendar events.

Uses the ForexFactory public JSON feed (no API key required).
The calendar is cached for 4 hours to avoid hammering the endpoint.

Fails open: if the calendar cannot be fetched, alerts are NOT blocked.
This prevents a network blip from silently suppressing all signals.
"""
import datetime
import logging
import time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_CACHE_TTL_S = 4 * 60 * 60   # re-fetch calendar every 4 hours
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_cached_events: list[dict] = []
_cache_fetched_at: float = 0.0


def _fetch_calendar() -> list[dict]:
    import requests
    try:
        resp = requests.get(_FF_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("NewsFilter: calendar fetch failed (%s) — filter disabled", exc)
        return []


def _get_events() -> list[dict]:
    global _cached_events, _cache_fetched_at
    if time.time() - _cache_fetched_at > _CACHE_TTL_S:
        events = _fetch_calendar()
        if events:
            _cached_events = events
            _cache_fetched_at = time.time()
            logger.info("NewsFilter: loaded %d calendar events", len(events))
    return _cached_events


def _parse_event_dt(event: dict) -> datetime.datetime | None:
    """Parse an event's date + time into a tz-aware datetime (ET)."""
    try:
        date_str = event.get("date", "")
        time_str = event.get("time", "").strip().lower()

        # Date may arrive as "2026-06-06" or ISO "2026-06-06T00:00:00-04:00"
        if "t" in date_str.lower():
            date_str = date_str[:10]

        # Skip all-day or tentative events — no specific time to block around
        if not time_str or time_str in ("tentative", "all day", ""):
            return None

        dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M%p")
        return dt.replace(tzinfo=_ET)
    except Exception:
        return None


def high_impact_news_within(minutes: int = 30) -> bool:
    """
    Returns True if any high-impact USD news event falls within
    `minutes` of now (before or after the release time).

    Returns False (don't block) if the calendar cannot be fetched.
    """
    events = _get_events()
    if not events:
        return False   # fail open

    window = datetime.timedelta(minutes=minutes)
    now = datetime.datetime.now(tz=_ET)

    for event in events:
        if event.get("impact", "").lower() != "high":
            continue
        if event.get("country", "").upper() not in ("USD", "US"):
            continue

        event_dt = _parse_event_dt(event)
        if event_dt is None:
            continue

        if abs(event_dt - now) <= window:
            logger.info(
                "NewsFilter: '%s' at %s ET is within %dm — blocking alerts",
                event.get("title", "?"),
                event_dt.strftime("%Y-%m-%d %H:%M"),
                minutes,
            )
            return True

    return False

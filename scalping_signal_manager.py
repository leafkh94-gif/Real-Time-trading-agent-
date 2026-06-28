"""
Signal/state manager for Plan B.

Tracks, per instrument:
  - last alert time            -> enforces the 30-minute cooldown
  - open trade entry/time/dir   -> enforces the 2-hour "time stop"

Drop into core/scalping_signal_manager.py. Pairs with
strategy/scalping_strategy.py + strategy/scalping_config.py.

This is intentionally simple (in-memory dict). If your bot already has
core/state_store.py for persistence, swap the dict for that store so
state survives restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional

from strategy.scalping_config import ScalpingConfig


@dataclass
class OpenTrade:
    direction: str          # "BUY" or "SELL"
    entry: float
    tp1: float
    opened_at: datetime


class ScalpingSignalManager:
    def __init__(self, config: ScalpingConfig):
        self.cfg = config
        self._last_alert: Dict[str, datetime] = {}
        self._open_trades: Dict[str, OpenTrade] = {}

    # ------------------------------------------------------------------
    # Cooldown (alert lock)
    # ------------------------------------------------------------------
    def can_alert(self, instrument: str, now_utc: Optional[datetime] = None) -> bool:
        """30-minute lock: True if enough time has passed since the last alert."""
        now_utc = now_utc or datetime.utcnow()
        last = self._last_alert.get(instrument)
        if last is None:
            return True
        return (now_utc - last) >= timedelta(seconds=self.cfg.alert_cooldown_s)

    def register_alert(
        self,
        instrument: str,
        direction: str,
        entry: float,
        tp1: float,
        now_utc: Optional[datetime] = None,
    ) -> None:
        """Call this right after sending a Plan B alert."""
        now_utc = now_utc or datetime.utcnow()
        self._last_alert[instrument] = now_utc
        self._open_trades[instrument] = OpenTrade(
            direction=direction, entry=entry, tp1=tp1, opened_at=now_utc
        )

    # ------------------------------------------------------------------
    # Time stop (2 hours, "TP1 not hit" check)
    # ------------------------------------------------------------------
    def check_time_stop(
        self,
        instrument: str,
        current_price: float,
        now_utc: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Returns a "time expired" message if the open trade for `instrument`
        has run past PLAN_B_TIME_STOP_S without reaching TP1, otherwise None.

        Clears the tracked trade once TP1 is hit OR the time stop fires,
        so this should be called once per scan cycle for instruments with
        an open Plan B trade.
        """
        now_utc = now_utc or datetime.utcnow()
        trade = self._open_trades.get(instrument)
        if trade is None:
            return None

        tp1_hit = (
            current_price >= trade.tp1 if trade.direction == "BUY"
            else current_price <= trade.tp1
        )
        if tp1_hit:
            self._open_trades.pop(instrument, None)
            return None

        elapsed = (now_utc - trade.opened_at).total_seconds()
        if elapsed >= self.cfg.time_stop_s:
            self._open_trades.pop(instrument, None)
            return (
                f"\u23f1\ufe0f TIME STOP - {instrument} {trade.direction} "
                f"opened {int(elapsed // 60)}min ago has not reached TP1. "
                f"Consider closing manually."
            )

        return None

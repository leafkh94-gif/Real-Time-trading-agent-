"""
Per-instrument state manager for Plan B.

Tracks:
  - last alert time   → enforces the 30-minute alert cooldown
  - open trade info   → enforces the 2-hour “TP1 not hit” time-stop
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from strategy.scalping_config import ScalpingConfig


def _now() -> datetime:
    """Naive UTC datetime (avoids DeprecationWarning from utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class _OpenTrade:
    direction: str     # "BUY" or "SELL"
    entry:     float
    tp1:       float
    opened_at: datetime


class ScalpingSignalManager:
    def __init__(self, config: ScalpingConfig):
        self.cfg          = config
        self._last_alert:  Dict[str, datetime] = {}
        self._open_trades: Dict[str, _OpenTrade] = {}

    # ── Cooldown ──────────────────────────────────────────────────────

    def can_alert(self, instrument: str, now_utc: Optional[datetime] = None) -> bool:
        now_utc = now_utc or _now()
        last    = self._last_alert.get(instrument)
        if last is None:
            return True
        return (now_utc - last) >= timedelta(seconds=self.cfg.alert_cooldown_s)

    def register_alert(
        self,
        instrument: str,
        direction:  str,
        entry:      float,
        tp1:        float,
        now_utc:    Optional[datetime] = None,
    ) -> None:
        now_utc = now_utc or _now()
        self._last_alert[instrument]  = now_utc
        self._open_trades[instrument] = _OpenTrade(
            direction=direction, entry=entry, tp1=tp1, opened_at=now_utc
        )

    # ── Time stop ─────────────────────────────────────────────────────

    def check_time_stop(
        self,
        instrument:    str,
        current_price: float,
        now_utc:       Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Returns a warning message if the tracked trade has exceeded PLAN_B_TIME_STOP_S
        without reaching TP1. Clears the trade record on TP1 hit or time stop.
        """
        now_utc = now_utc or _now()
        trade   = self._open_trades.get(instrument)
        if trade is None:
            return None

        tp1_hit = (current_price >= trade.tp1 if trade.direction == "BUY"
                   else current_price <= trade.tp1)
        if tp1_hit:
            self._open_trades.pop(instrument, None)
            return None

        elapsed = (now_utc - trade.opened_at).total_seconds()
        if elapsed >= self.cfg.time_stop_s:
            self._open_trades.pop(instrument, None)
            return (
                f"⏱️ TIME STOP — {instrument} {trade.direction} "
                f"opened {int(elapsed // 60)}min ago, TP1 not yet hit. "
                "Consider closing manually."
            )
        return None

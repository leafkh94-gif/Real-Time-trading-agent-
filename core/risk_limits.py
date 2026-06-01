"""
Hard risk limits enforced before every order.
Reads daily PnL from state_store so restarts cannot reset running totals.
Automatically trips the kill switch when the daily loss limit is reached.
"""
from dataclasses import dataclass, field
from core.kill_switch import KillSwitch, kill_switch as _default_kill_switch
from core.state_store import StateStore, state_store as _default_state_store


@dataclass
class RiskLimits:
    max_position_size_lots: float = 0.10
    max_daily_loss_usd: float = 100.0
    max_open_positions: int = 1
    max_trades_per_day: int = 5
    min_risk_reward_ratio: float = 1.5


class RiskGuard:
    def __init__(
        self,
        limits: RiskLimits | None = None,
        store: StateStore | None = None,
        switch: KillSwitch | None = None,
    ):
        self.limits = limits or RiskLimits()
        self._store = store or _default_state_store
        self._switch = switch or _default_kill_switch

    def can_trade(self, proposed_lots: float, open_positions: int) -> tuple[bool, str]:
        """
        Returns (True, 'ok') when the trade is allowed.
        Returns (False, reason) and optionally trips the kill switch when rejected.
        Must be called before every order submission — no exceptions.
        """
        if self._switch.check():
            return False, f"kill switch active: {self._switch.reason}"

        if proposed_lots <= 0:
            return False, "proposed lots must be positive"

        if proposed_lots > self.limits.max_position_size_lots:
            return (
                False,
                f"size {proposed_lots} lots exceeds max {self.limits.max_position_size_lots}",
            )

        if open_positions >= self.limits.max_open_positions:
            return False, f"max open positions ({self.limits.max_open_positions}) reached"

        stats = self._store.get_today()

        if stats.trades >= self.limits.max_trades_per_day:
            return False, f"daily trade count limit ({self.limits.max_trades_per_day}) reached"

        if stats.pnl <= -self.limits.max_daily_loss_usd:
            self._switch.trip(f"daily loss limit hit: {stats.pnl:.2f} USD")
            return False, f"daily loss limit breached ({stats.pnl:.2f}) — kill switch tripped"

        return True, "ok"

    def record_pnl(self, amount: float) -> None:
        """Call after a position is closed with the realised PnL (negative for losses)."""
        self._store.add_pnl(amount)
        stats = self._store.get_today()
        if stats.pnl <= -self.limits.max_daily_loss_usd and not self._switch.check():
            self._switch.trip(f"daily loss limit hit after close: {stats.pnl:.2f} USD")

    def record_trade(self) -> None:
        """Call when a new order is confirmed open."""
        self._store.add_trade()

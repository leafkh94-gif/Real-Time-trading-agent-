"""
Abstract broker adapter. All safety gates (kill switch + risk limits) are enforced
inside the base place_order() — subclasses implement _submit_order() only.
This makes it structurally impossible to bypass the gates in any concrete adapter.
"""
import logging
from abc import ABC, abstractmethod
from typing import final

from alerts.notifier import Notifier
from core.kill_switch import KillSwitch
from core.risk_limits import RiskGuard
from core.state_store import StateStore
from execution.models import Order, Signal

logger = logging.getLogger(__name__)


class BrokerAdapter(ABC):
    """
    Base class for all broker integrations.

    Subclasses must implement:
      _submit_order(signal)  — broker-specific wire protocol
      reconcile()            — fetch live positions on startup, sync to state_store
      close_position(id)     — close a position, return realised PnL
      open_position_count()  — current live count (not from state_store cache)
      connect()              — authenticate / establish session
    """

    def __init__(
        self,
        guard: RiskGuard,
        switch: KillSwitch,
        store: StateStore,
        notifier: Notifier,
    ):
        self._guard = guard
        self._switch = switch
        self._store = store
        self._notifier = notifier

    # ── Public API ────────────────────────────────────────────────────────────

    @final
    def place_order(self, signal: Signal) -> Order:
        """
        Template method. Marked @final so type checkers reject any subclass that
        overrides it — the kill-switch and risk gates below cannot be bypassed.
        """
        # Gate 1 — kill switch (checked first; cheapest)
        if self._switch.check():
            self._reject(f"kill switch active: {self._switch.reason}")

        # Gate 2 — risk limits (reads persistent daily state)
        ok, reason = self._guard.can_trade(
            proposed_lots=signal.lots,
            open_positions=self.open_position_count(),
        )
        if not ok:
            self._reject(reason)

        # Submit to the broker (subclass responsibility)
        order = self._submit_order(signal)

        # Post-fill bookkeeping
        self._guard.record_trade()
        self._store.add_position(
            position_id=order.order_id,
            symbol=order.symbol,
            lots=order.lots,
            direction=order.direction,
            open_price=order.price,
            opened_at=order.opened_at,
        )
        msg = (
            f"opened {signal.direction} {signal.lots} lots "
            f"{signal.symbol} @ {order.price:.2f}"
        )
        logger.info(msg)
        self._notifier.send(msg)
        return order

    # ── Subclass interface ────────────────────────────────────────────────────

    @abstractmethod
    def _submit_order(self, signal: Signal) -> Order:
        """Broker-specific submission. Called only after all gates pass."""
        ...

    @abstractmethod
    def reconcile(self) -> list[Order]:
        """
        Fetch live open positions from the broker and sync to state_store.
        Must be called once on startup before the main loop begins.
        """
        ...

    @abstractmethod
    def close_position(self, position_id: str) -> float:
        """Close a position. Returns realised PnL in account currency."""
        ...

    @abstractmethod
    def open_position_count(self) -> int:
        """Return live count of open positions from the broker (not just the local cache)."""
        ...

    @abstractmethod
    def connect(self) -> None:
        """Authenticate and establish the broker session."""
        ...

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _reject(self, reason: str) -> None:
        msg = f"order rejected — {reason}"
        logger.warning(msg)
        self._notifier.send(msg)
        raise RuntimeError(msg)

"""
Paper trading broker. No real API calls — all operations are local.
Safe for development and 2-week demo validation.

Raises RuntimeError on instantiation if ENVIRONMENT=production, preventing
accidental use of a paper broker with a live account.
"""
import logging
import os
from typing import Callable, Optional

from alerts.notifier import Notifier
from core.kill_switch import KillSwitch
from core.risk_limits import RiskGuard
from core.state_store import StateStore
from execution.broker import BrokerAdapter
from execution.models import Order, Signal

logger = logging.getLogger(__name__)

# Default simulated fill price for XAUUSD — override via price_feed for realism
_DEFAULT_PRICE = 2300.0
# XAU/USD standard contract size (troy oz per lot)
_CONTRACT_SIZE_OZ = 100


class PaperBroker(BrokerAdapter):
    """
    Paper broker. Fills are simulated at the current price from price_feed()
    (or a fixed simulated_price if no feed is provided).

    On startup: call connect() then reconcile().
    """

    def __init__(
        self,
        guard: RiskGuard,
        switch: KillSwitch,
        store: StateStore,
        notifier: Notifier,
        simulated_price: float = _DEFAULT_PRICE,
        price_feed: Optional[Callable[[], float]] = None,
    ):
        if os.getenv("ENVIRONMENT") == "production":
            raise RuntimeError(
                "PaperBroker cannot be used in a production environment. "
                "Instantiate a real broker adapter instead."
            )
        super().__init__(guard=guard, switch=switch, store=store, notifier=notifier)
        self.simulated_price = simulated_price
        self._price_feed = price_feed
        self._positions: dict[str, Order] = {}

    # ── BrokerAdapter interface ───────────────────────────────────────────────

    def connect(self) -> None:
        logger.info("PaperBroker: connected (paper trading — no real funds)")

    def reconcile(self) -> list[Order]:
        """
        Paper reconcile: local in-memory positions ARE the broker state.
        Writes them into state_store so the rest of the system sees a consistent view.
        On a fresh start with an empty _positions dict this is a no-op.
        """
        for order in self._positions.values():
            self._store.add_position(
                position_id=order.order_id,
                symbol=order.symbol,
                lots=order.lots,
                direction=order.direction,
                open_price=order.price,
                opened_at=order.opened_at,
            )
        logger.info("PaperBroker: reconciled %d open positions", len(self._positions))
        return list(self._positions.values())

    def _submit_order(self, signal: Signal) -> Order:
        price = self._current_price()
        order = Order.new(
            symbol=signal.symbol,
            direction=signal.direction,
            lots=signal.lots,
            price=price,
        )
        self._positions[order.order_id] = order
        logger.debug(
            "PaperBroker: filled %s %s %s lots @ %.2f",
            signal.direction, signal.symbol, signal.lots, price,
        )
        return order

    def close_position(self, position_id: str) -> float:
        """
        Close a paper position. Returns realised PnL in USD.
        PnL = direction_multiplier * (close - open) * lots * contract_size
        """
        if position_id not in self._positions:
            raise KeyError(f"Position {position_id!r} not found in paper broker")

        order = self._positions.pop(position_id)
        close_price = self._current_price()

        multiplier = 1.0 if order.direction == "buy" else -1.0
        pnl = multiplier * (close_price - order.price) * order.lots * _CONTRACT_SIZE_OZ

        self._store.remove_position(position_id)
        self._guard.record_pnl(pnl)

        msg = (
            f"closed {order.direction} {order.lots} lots {order.symbol} "
            f"@ {close_price:.2f} | PnL: {pnl:+.2f} USD"
        )
        logger.info(msg)
        self._notifier.send(msg)
        return pnl

    def open_position_count(self) -> int:
        return len(self._positions)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _current_price(self) -> float:
        if self._price_feed is not None:
            return self._price_feed()
        return self.simulated_price

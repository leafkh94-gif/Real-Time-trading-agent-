"""
Data types shared between strategy and execution layers.
Strategy produces Signal objects — execution consumes them and returns Orders.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


@dataclass
class Signal:
    direction: str          # 'buy' | 'sell'
    lots: float
    symbol: str = "XAUUSD"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    comment: str = ""

    def __post_init__(self):
        if self.direction not in ("buy", "sell"):
            raise ValueError(f"direction must be 'buy' or 'sell', got {self.direction!r}")
        if self.lots <= 0:
            raise ValueError(f"lots must be positive, got {self.lots}")


@dataclass
class Order:
    order_id: str
    symbol: str
    direction: str
    lots: float
    price: float
    opened_at: str

    @staticmethod
    def new(symbol: str, direction: str, lots: float, price: float) -> "Order":
        return Order(
            order_id=str(uuid.uuid4()),
            symbol=symbol,
            direction=direction,
            lots=lots,
            price=price,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )

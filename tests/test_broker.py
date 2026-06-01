"""
Phase 2 broker adapter tests.
Covers: every reject path, happy path, close_position PnL, reconcile,
and the production environment guard on PaperBroker.
"""
import pytest
from unittest.mock import MagicMock

from alerts.notifier import NullNotifier
from core.kill_switch import KillSwitch
from core.risk_limits import RiskGuard, RiskLimits
from core.state_store import StateStore
from execution.models import Signal
from execution.paper_broker import PaperBroker


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def kill(tmp_path):
    return KillSwitch(kill_file=tmp_path / "KILL")


@pytest.fixture
def store(tmp_path):
    return StateStore(db_path=tmp_path / "bot.db")


@pytest.fixture
def guard(store, kill):
    return RiskGuard(
        limits=RiskLimits(
            max_position_size_lots=0.10,
            max_daily_loss_usd=100.0,
            max_open_positions=2,
            max_trades_per_day=5,
        ),
        store=store,
        switch=kill,
    )


@pytest.fixture
def notifier():
    n = MagicMock(spec=NullNotifier)
    return n


@pytest.fixture
def broker(guard, kill, store, notifier, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    return PaperBroker(
        guard=guard, switch=kill, store=store, notifier=notifier,
        simulated_price=2300.0,
    )


# ── Signal helpers ─────────────────────────────────────────────────────────────

def buy(lots=0.05):
    return Signal(direction="buy", lots=lots)


def sell(lots=0.05):
    return Signal(direction="sell", lots=lots)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_place_order_returns_order(broker):
    order = broker.place_order(buy())
    assert order.direction == "buy"
    assert order.lots == 0.05
    assert order.price == 2300.0
    assert order.order_id


def test_place_order_sends_opened_alert(broker, notifier):
    broker.place_order(buy())
    notifier.send.assert_called_once()
    assert "opened" in notifier.send.call_args[0][0].lower()


def test_place_order_records_trade(broker, store):
    broker.place_order(buy())
    assert store.get_today().trades == 1


def test_place_order_syncs_position_to_store(broker, store):
    broker.place_order(buy())
    assert store.count_open_positions() == 1


def test_open_position_count_increments(broker):
    broker.place_order(buy())
    broker.place_order(sell())
    assert broker.open_position_count() == 2


# ── Reject: kill switch active ────────────────────────────────────────────────

def test_rejects_when_kill_switch_active(broker, kill, notifier):
    kill.trip("manual")
    with pytest.raises(RuntimeError, match="kill switch"):
        broker.place_order(buy())
    notifier.send.assert_called_once()
    assert "rejected" in notifier.send.call_args[0][0].lower()


# ── Reject: position size too large ──────────────────────────────────────────

def test_rejects_oversized_lots(broker, notifier):
    with pytest.raises(RuntimeError, match="rejected"):
        broker.place_order(buy(lots=0.50))
    notifier.send.assert_called_once()


# ── Reject: max open positions ────────────────────────────────────────────────

def test_rejects_when_max_open_positions_reached(broker, notifier):
    broker.place_order(buy())
    broker.place_order(buy())  # fills the max_open_positions=2 limit
    with pytest.raises(RuntimeError, match="rejected"):
        broker.place_order(buy())
    # 2 successful opens + 1 rejection
    assert notifier.send.call_count == 3


# ── Reject: daily trade count ─────────────────────────────────────────────────

def test_rejects_when_daily_trade_count_reached(broker, store, notifier):
    for _ in range(5):
        store.add_trade()
    with pytest.raises(RuntimeError, match="rejected"):
        broker.place_order(buy())
    notifier.send.assert_called_once()


# ── Reject: daily loss limit (also trips kill switch) ────────────────────────

def test_rejects_when_daily_loss_limit_hit(broker, store, kill, notifier):
    store.add_pnl(-100.0)
    with pytest.raises(RuntimeError, match="rejected"):
        broker.place_order(buy())
    assert kill.is_tripped is True
    notifier.send.assert_called_once()


def test_rejection_does_not_record_trade(broker, store, kill):
    kill.trip("manual")
    with pytest.raises(RuntimeError):
        broker.place_order(buy())
    assert store.get_today().trades == 0


# ── Close position ────────────────────────────────────────────────────────────

def test_close_position_removes_from_store(broker, store):
    order = broker.place_order(buy())
    broker.close_position(order.order_id)
    assert store.count_open_positions() == 0
    assert broker.open_position_count() == 0


def test_close_position_sends_closed_alert(broker, notifier):
    order = broker.place_order(buy())
    notifier.reset_mock()
    broker.close_position(order.order_id)
    notifier.send.assert_called_once()
    assert "closed" in notifier.send.call_args[0][0].lower()


def test_close_buy_profit(broker):
    order = broker.place_order(buy(lots=0.05))
    broker.simulated_price = 2310.0
    pnl = broker.close_position(order.order_id)
    # (2310 - 2300) * 0.05 * 100 = +50.0
    assert pnl == pytest.approx(50.0)


def test_close_buy_loss(broker):
    order = broker.place_order(buy(lots=0.05))
    broker.simulated_price = 2290.0
    pnl = broker.close_position(order.order_id)
    # (2290 - 2300) * 0.05 * 100 = -50.0
    assert pnl == pytest.approx(-50.0)


def test_close_sell_profit(broker):
    order = broker.place_order(sell(lots=0.05))
    broker.simulated_price = 2290.0
    pnl = broker.close_position(order.order_id)
    # sell at 2300, close at 2290 → (2300 - 2290) * 0.05 * 100 = +50
    assert pnl == pytest.approx(50.0)


def test_close_sell_loss(broker):
    order = broker.place_order(sell(lots=0.05))
    broker.simulated_price = 2310.0
    pnl = broker.close_position(order.order_id)
    assert pnl == pytest.approx(-50.0)


def test_close_nonexistent_position_raises(broker):
    with pytest.raises(KeyError):
        broker.close_position("nonexistent-id")


def test_close_loss_trips_kill_switch_when_limit_reached(broker, store, kill):
    store.add_pnl(-90.0)  # already -90, one more -15 will breach -100
    order = broker.place_order(buy(lots=0.05))
    broker.simulated_price = 2285.0  # loss = (2285-2300)*0.05*100 = -75 → total -165
    broker.close_position(order.order_id)
    assert kill.is_tripped is True


# ── Reconcile ─────────────────────────────────────────────────────────────────

def test_reconcile_returns_empty_list_when_no_positions(broker):
    assert broker.reconcile() == []


def test_reconcile_returns_current_positions(broker):
    order = broker.place_order(buy())
    positions = broker.reconcile()
    assert len(positions) == 1
    assert positions[0].order_id == order.order_id


def test_reconcile_syncs_to_state_store(broker, store):
    order = broker.place_order(buy())
    store.remove_position(order.order_id)  # simulate state_store being cleared
    assert store.count_open_positions() == 0
    broker.reconcile()
    assert store.count_open_positions() == 1


# ── Price feed callback ───────────────────────────────────────────────────────

def test_price_feed_used_when_provided(guard, kill, store, notifier, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    prices = iter([2350.0, 2360.0])
    b = PaperBroker(
        guard=guard, switch=kill, store=store, notifier=notifier,
        price_feed=lambda: next(prices),
    )
    order = b.place_order(buy())
    assert order.price == 2350.0
    pnl = b.close_position(order.order_id)
    # (2360 - 2350) * 0.05 * 100 = +50
    assert pnl == pytest.approx(50.0)


# ── Signal validation ─────────────────────────────────────────────────────────

def test_signal_rejects_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        Signal(direction="hold", lots=0.05)


def test_signal_rejects_zero_lots():
    with pytest.raises(ValueError, match="lots"):
        Signal(direction="buy", lots=0.0)


def test_signal_rejects_negative_lots():
    with pytest.raises(ValueError, match="lots"):
        Signal(direction="buy", lots=-0.01)


# ── Production guard ──────────────────────────────────────────────────────────

def test_paper_broker_blocked_in_production(guard, kill, store, notifier, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="production"):
        PaperBroker(guard=guard, switch=kill, store=store, notifier=notifier)

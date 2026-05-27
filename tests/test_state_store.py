"""Tests for core/state_store.py"""
import pytest
from datetime import date
from pathlib import Path
from core.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(db_path=tmp_path / "test.db")


def test_get_today_returns_zeros_initially(store):
    stats = store.get_today()
    assert stats.pnl == 0.0
    assert stats.trades == 0


def test_add_pnl_accumulates(store):
    store.add_pnl(50.0)
    store.add_pnl(25.0)
    assert store.get_today().pnl == pytest.approx(75.0)


def test_add_pnl_negative(store):
    store.add_pnl(-30.0)
    assert store.get_today().pnl == pytest.approx(-30.0)


def test_add_trade_increments(store):
    store.add_trade()
    store.add_trade()
    assert store.get_today().trades == 2


def test_pnl_and_trades_independent(store):
    store.add_pnl(100.0)
    store.add_trade()
    stats = store.get_today()
    assert stats.pnl == pytest.approx(100.0)
    assert stats.trades == 1


def test_persists_across_reconnect(tmp_path):
    """Restart simulation: close and reopen the DB file."""
    db = tmp_path / "test.db"
    s1 = StateStore(db_path=db)
    s1.add_pnl(-80.0)
    s1.add_trade()
    s1.close()

    s2 = StateStore(db_path=db)
    stats = s2.get_today()
    assert stats.pnl == pytest.approx(-80.0)
    assert stats.trades == 1


def test_open_positions_count(store):
    assert store.count_open_positions() == 0
    store.add_position("p1", "XAUUSD", 0.1, "buy", 2300.0, "2024-01-01T00:00:00Z")
    assert store.count_open_positions() == 1
    store.add_position("p2", "XAUUSD", 0.1, "sell", 2305.0, "2024-01-01T00:01:00Z")
    assert store.count_open_positions() == 2


def test_remove_position(store):
    store.add_position("p1", "XAUUSD", 0.1, "buy", 2300.0, "2024-01-01T00:00:00Z")
    store.remove_position("p1")
    assert store.count_open_positions() == 0


def test_remove_nonexistent_position_is_safe(store):
    store.remove_position("nonexistent")  # must not raise


def test_add_position_upserts(store):
    store.add_position("p1", "XAUUSD", 0.1, "buy", 2300.0, "2024-01-01T00:00:00Z")
    store.add_position("p1", "XAUUSD", 0.2, "buy", 2301.0, "2024-01-01T00:01:00Z")
    assert store.count_open_positions() == 1

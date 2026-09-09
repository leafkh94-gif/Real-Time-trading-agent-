"""
One H1 candle can contain both the stop and the target.

OHLC cannot say which came first, and the resolver has always answered "the
stop". That is the safe answer but a systematically pessimistic one — and the
stop is the CLOSER level, so these candles are not rare. Every such trade is
booked as a full loss.

These tests pin that the ambiguity is now RECORDED, so a report can bracket it
rather than present the pessimistic end as fact.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from strategy import journal
from strategy import strategy_config as C

T0 = dt.datetime(2026, 7, 1)


def _entry(**over):
    e = {
        "id": "T-1", "epic": "US500", "direction": "buy", "pattern": "flag",
        "entry": 100.0, "stop_loss": 90.0, "take_profit": 120.0,
        "take_profit2": 130.0, "status": "pending",
        "alert_utc": T0.strftime(journal._ISO), "expiry_utc": None,
        "filled_utc": None, "resolved_utc": None,
        "mfe_r": 0.0, "mfe_r_optimistic": 0.0, "mae_r": 0.0,
        "bars_since_alert": 0, "bars_to_fill": None, "bars_to_resolve": None,
        "ambiguous_exit": False, "last_bar_utc": None, "r_realized": None,
        "be_armed": False, "breakeven_at": None,
    }
    e.update(over)
    return e


def _bars(rows):
    return pd.DataFrame(
        [{"time": T0 + dt.timedelta(hours=i + 1), "open": o, "high": h,
          "low": l, "close": c} for i, (o, h, l, c) in enumerate(rows)])


def test_a_candle_holding_both_stop_and_target_is_flagged(monkeypatch):
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    # Fill at 100, then one candle spanning 85..125 — both 90 and 120 inside.
    e = journal.resolve(_entry(), _bars([(100, 101, 99, 100), (100, 125, 85, 100)]))
    assert e["status"] == "sl_hit", e["status"]
    assert e["ambiguous_exit"] is True


def test_a_clean_stop_is_not_flagged(monkeypatch):
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    e = journal.resolve(_entry(), _bars([(100, 101, 99, 100), (99, 99, 85, 88)]))
    assert e["status"] == "sl_hit"
    assert e["ambiguous_exit"] is False


def test_a_clean_win_is_not_flagged(monkeypatch):
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    e = journal.resolve(_entry(), _bars([(100, 101, 99, 100), (101, 122, 99, 121)]))
    assert e["status"] == "tp1_hit"
    assert e["ambiguous_exit"] is False


def test_the_pessimistic_verdict_is_unchanged(monkeypatch):
    """The flag records uncertainty; it must not silently change any outcome."""
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    e = journal.resolve(_entry(), _bars([(100, 101, 99, 100), (100, 125, 85, 100)]))
    assert e["r_realized"] == pytest.approx(-1.0)


def test_legacy_entries_without_the_field_still_resolve(monkeypatch):
    """Journals written before this change must not crash on load."""
    monkeypatch.setattr(C, "BREAKEVEN_ENABLED", False)
    old = _entry()
    del old["ambiguous_exit"]
    e = journal.resolve(old, _bars([(100, 101, 99, 100), (100, 125, 85, 100)]))
    assert e["ambiguous_exit"] is True

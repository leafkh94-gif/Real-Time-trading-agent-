"""
Opening Range Breakout.

Every other detector reads chart shape. This one reads the clock: the range is
whatever the market printed in its first hour, and the signal is price leaving
it. It is the only rule in the engine tied to the market open — the gap the
session measurements kept pointing at — and the only one with no judgement in
it, so it either measures well or it does not.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from strategy import indicators as ind
from strategy import strategy_config as C
from strategy.scoring_strategy import _detect_orb

# September: New York is UTC-4, so ET 09:00 is 13:00 UTC.
ET_OPEN_UTC = 13


def _session(bars, day=dt.date(2026, 9, 8)):
    """bars: list of (high, low, close). First bar starts at the ET open."""
    rows = []
    for i, (h, l, c) in enumerate(bars):
        rows.append({
            "time": dt.datetime(day.year, day.month, day.day, ET_OPEN_UTC + i),
            "open": c, "high": h, "low": l, "close": c, "volume": 100.0,
        })
    # Pad with prior-day bars so ATR and length checks have material.
    pre = [{"time": dt.datetime(day.year, day.month, day.day - 1, 13 + i),
            "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100.0}
           for i in range(8)]
    return pd.DataFrame(pre + rows)


def _atr(df):
    return ind.atr(df)


def test_a_close_above_the_opening_hour_is_a_buy():
    df = _session([(110, 100, 105), (112, 104, 111)])   # range 100-110, close 111
    d = _detect_orb(df, _atr(df))
    assert d is not None and d["direction"] == "buy", d
    assert d["broken_level"] == 110.0
    assert d["pattern"] == "orb"


def test_a_close_below_the_opening_hour_is_a_sell():
    df = _session([(110, 100, 105), (106, 97, 98)])
    d = _detect_orb(df, _atr(df))
    assert d is not None and d["direction"] == "sell", d
    assert d["broken_level"] == 100.0


def test_price_still_inside_the_range_is_not_a_signal():
    df = _session([(110, 100, 105), (109, 101, 106)])
    assert _detect_orb(df, _atr(df)) is None


def test_the_breakout_must_be_the_current_bar():
    """Broke out an hour ago and drifted back inside: the move already
    happened, and entering now is chasing."""
    df = _session([(110, 100, 105), (115, 104, 114), (113, 106, 108)])
    assert _detect_orb(df, _atr(df)) is None


def test_a_break_late_in_the_session_is_not_an_opening_move():
    bars = [(110, 100, 105)] + [(109, 101, 105)] * C.ORB_VALID_BARS + [(120, 104, 119)]
    assert _detect_orb(_session(bars), _atr(_session(bars))) is None


def test_a_range_too_tight_to_mean_anything_is_ignored(monkeypatch):
    monkeypatch.setattr(C, "ORB_MIN_RANGE_ATR", 5.0)
    df = _session([(110, 100, 105), (112, 104, 111)])
    assert _detect_orb(df, _atr(df)) is None


def test_the_opening_hour_alone_gives_nothing_to_break_out_of():
    df = _session([(110, 100, 105)])
    assert _detect_orb(df, _atr(df)) is None


def test_a_frame_without_timestamps_is_refused():
    df = pd.DataFrame({"open": [1] * 10, "high": [2] * 10,
                       "low": [0] * 10, "close": [1] * 10})
    assert _detect_orb(df, pd.Series([1.0] * 10)) is None


def test_it_ships_disabled():
    """Published write-ups disagree about whether ORB still works on indices,
    so it earns its place from our own measurement or not at all."""
    assert C.PATTERNS["orb"]["enabled"] is False

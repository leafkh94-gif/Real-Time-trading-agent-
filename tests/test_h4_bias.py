"""
Which timeframe decides the trend.

The measured problem: 84% of every trade was a buy, and `counter_trend`
rejected 30% of all candidates the detectors found. The cause is the read
itself — EMA200 on a DAILY chart still reports "up" weeks into a real decline,
so bearish setups are banned throughout every pullback.

H4 EMA20/50 spans roughly the same calendar window as daily EMA3/8. These tests
pin the one claim that justifies the switch: the faster pair turns while the
slower pair is still facing the wrong way.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from strategy import strategy_config as C
from strategy.scoring_strategy import (
    MarketData, _bias_from, _resample_h4, _trend_bias,
)

T0 = dt.datetime(2026, 7, 1)


def _h1(n, drift):
    close = 5000 + np.cumsum(np.full(n, drift, dtype=float))
    return pd.DataFrame({
        "time":  [T0 + dt.timedelta(hours=i) for i in range(n)],
        "open":  close, "high": close + 5, "low": close - 5,
        "close": close, "volume": np.full(n, 100.0),
    })


def _rise_then_fall(up_bars, down_bars):
    a = _h1(up_bars, 1.0)
    b = _h1(down_bars, -3.0)
    b["close"] = a["close"].iloc[-1] + np.cumsum(np.full(down_bars, -3.0))
    b["open"] = b["high"] = b["low"] = b["close"]
    b["high"] = b["close"] + 5
    b["low"] = b["close"] - 5
    b["time"] = [a["time"].iloc[-1] + dt.timedelta(hours=i + 1) for i in range(down_bars)]
    return pd.concat([a, b], ignore_index=True)


# ── resampling ───────────────────────────────────────────────────────────────
def test_h4_aggregates_four_hourly_bars_into_one():
    h4 = _resample_h4(_h1(16, 1.0))
    assert len(h4) == 4
    src = _h1(16, 1.0)
    first = src.iloc[:4]
    assert h4["open"].iloc[0] == first["open"].iloc[0]
    assert h4["close"].iloc[0] == first["close"].iloc[-1]
    assert h4["high"].iloc[0] == first["high"].max()
    assert h4["low"].iloc[0] == first["low"].min()


def test_resample_refuses_a_frame_too_short_to_be_meaningful():
    assert _resample_h4(_h1(4, 1.0)) is None
    assert _resample_h4(pd.DataFrame({"close": [1, 2, 3]})) is None


# ── the load-bearing claim ───────────────────────────────────────────────────
def test_the_h4_read_turns_while_the_daily_read_still_says_up():
    """One price path, both frames derived from it — the faithful comparison.

    300 days of uptrend then a two-week decline. Daily EMA50/200 spans months
    and cannot have turned yet; H4 EMA20/50 spans about 3 and 8 days and has.
    If the daily pair turned this quickly too, the switch would buy nothing.
    """
    up_days, down_days = 300, 10
    daily_close = np.concatenate([
        5000 + np.cumsum(np.full(up_days, 10.0)),
        5000 + up_days * 10.0 + np.cumsum(np.full(down_days, -60.0)),
    ])
    daily = pd.DataFrame({"close": daily_close})

    # The same decline seen hourly: the last 40 days at 24 bars a day.
    tail_days = 40
    hourly = np.repeat(daily_close[-tail_days:], 24) + \
        np.tile(np.linspace(0, 1, 24), tail_days)
    h1 = pd.DataFrame({
        "time":  [T0 + dt.timedelta(hours=i) for i in range(len(hourly))],
        "open":  hourly, "high": hourly + 5, "low": hourly - 5,
        "close": hourly, "volume": np.full(len(hourly), 100.0),
    })
    h4 = _resample_h4(h1)
    assert len(h4) >= C.EMA_SLOW_H4 + 10, len(h4)

    _, daily_state = _bias_from(daily["close"], "sell",
                                C.EMA_FAST_BIAS, C.EMA_SLOW_BIAS, C.EMA_MEDIUM_BIAS)
    _, h4_state = _bias_from(h4["close"], "sell",
                             C.EMA_FAST_H4, C.EMA_SLOW_H4, C.EMA_MEDIUM_H4)
    assert daily_state == "counter-trend", daily_state
    assert h4_state == "aligned-down", h4_state


def test_a_sell_blocked_on_daily_is_allowed_on_h4(monkeypatch):
    h1 = _rise_then_fall(800, 120)
    daily = pd.DataFrame({"close": 5000 + np.cumsum(np.full(300, 1.0))})
    md = MarketData(epic="US500", h1=h1, daily=daily, now_utc=T0)

    monkeypatch.setattr(C, "BIAS_TIMEFRAME", "daily")
    assert _trend_bias(md, "sell")[1] == "counter-trend"

    monkeypatch.setattr(C, "BIAS_TIMEFRAME", "h4")
    pts, state, src = _trend_bias(md, "sell")
    assert src == "h4" and state == "aligned-down", (src, state)


# ── honesty about which source spoke ─────────────────────────────────────────
def test_short_h4_history_falls_back_to_daily_and_says_so(monkeypatch):
    """A report that cannot tell which timeframe decided would make the whole
    comparison meaningless."""
    monkeypatch.setattr(C, "BIAS_TIMEFRAME", "h4")
    md = MarketData(epic="US500", h1=_h1(40, 1.0),          # 10 H4 bars only
                    daily=pd.DataFrame({"close": 5000 + np.cumsum(np.full(300, 1.0))}),
                    now_utc=T0)
    assert _trend_bias(md, "buy")[2] == "daily"


def test_daily_mode_never_consults_h4(monkeypatch):
    monkeypatch.setattr(C, "BIAS_TIMEFRAME", "daily")
    md = MarketData(epic="US500", h1=_rise_then_fall(800, 120),
                    daily=pd.DataFrame({"close": 5000 + np.cumsum(np.full(300, 1.0))}),
                    now_utc=T0)
    assert _trend_bias(md, "buy")[2] == "daily"


def test_default_config_is_unchanged():
    """This ships as a switch, not as a change."""
    assert C.BIAS_TIMEFRAME == "daily"

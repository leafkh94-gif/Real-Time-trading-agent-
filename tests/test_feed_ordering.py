"""
The feed must GUARANTEE oldest-first ordering, not assume it.

Capital.com documents no ordering for /prices. Every detector reads .iloc[-1]
as the latest bar and main_alerts drops .iloc[-1] as the still-forming candle,
so a reversed or shuffled response would invert the entire read silently: the
bot would analyse the oldest bar as "now" and drop a real closed candle.
"""
from __future__ import annotations

import pandas as pd
import pytest

from strategy.capital_feed import CapitalComFeed


def _price(hour: int, close: float, *, ts: str | None = None) -> dict:
    def px(v):
        return {"bid": v - 0.5, "ask": v + 0.5}
    return {
        "snapshotTimeUTC": ts if ts is not None else f"2026-07-15T{hour:02d}:00:00",
        "openPrice": px(close), "highPrice": px(close + 5),
        "lowPrice": px(close - 5), "closePrice": px(close),
        "lastTradedVolume": 100,
    }


@pytest.fixture
def feed():
    """A feed instance without touching the network."""
    return CapitalComFeed.__new__(CapitalComFeed)


def test_reversed_response_is_corrected(feed):
    feed._epic = "US500"
    prices = [_price(h, 5000 + h) for h in range(10, 4, -1)]     # newest first
    df = feed._to_df(prices)
    assert list(df["time"]) == sorted(df["time"])
    assert df["close"].iloc[-1] == 5010          # latest bar really is latest
    assert df["close"].iloc[0] == 5005


def test_shuffled_response_is_corrected(feed):
    feed._epic = "US500"
    order = [3, 7, 1, 9, 5, 2]
    df = feed._to_df([_price(h, 5000 + h) for h in order])
    assert list(df["close"]) == [5001, 5002, 5003, 5005, 5007, 5009]


def test_already_sorted_is_unchanged(feed):
    feed._epic = "US500"
    df = feed._to_df([_price(h, 5000 + h) for h in range(5, 11)])
    assert list(df["close"]) == [5005, 5006, 5007, 5008, 5009, 5010]


def test_index_is_reset_so_positional_access_is_safe(feed):
    """iloc[:-1] and iloc[-1] must line up with the sorted order."""
    feed._epic = "US500"
    df = feed._to_df([_price(h, 5000 + h) for h in range(10, 4, -1)])
    assert list(df.index) == list(range(len(df)))
    assert df.iloc[-1]["close"] == 5010


def test_unparseable_timestamp_row_is_dropped(feed):
    """A row that cannot be placed in time would sort last and masquerade as
    the newest bar — drop it rather than let it become 'now'."""
    feed._epic = "US500"
    prices = [_price(5, 5005), _price(0, 9999, ts="not-a-date"), _price(6, 5006)]
    df = feed._to_df(prices)
    assert len(df) == 2
    assert 9999 not in list(df["close"])
    assert df["close"].iloc[-1] == 5006


def test_all_timestamps_unusable_returns_empty(feed):
    """Going quiet is safer than trading on a series we cannot order."""
    feed._epic = "US500"
    df = feed._to_df([_price(1, 5001, ts="junk"), _price(2, 5002, ts="junk")])
    assert df.empty
    assert list(df.columns) == ["time", "open", "high", "low", "close",
                                "volume", "spread"]


def test_empty_response(feed):
    feed._epic = "US500"
    assert feed._to_df([]).empty


def test_mid_price_and_columns_preserved(feed):
    feed._epic = "US500"
    df = feed._to_df([_price(5, 5005)])
    assert df["close"].iloc[0] == pytest.approx(5005.0)   # (bid+ask)/2
    assert df["high"].iloc[0] == pytest.approx(5010.0)
    assert list(df.columns) == ["time", "open", "high", "low", "close",
                                "volume", "spread"]


def test_spread_is_kept_not_discarded(feed):
    """OHLC are mid prices; the cost of crossing the book must survive."""
    feed._epic = "US500"
    df = feed._to_df([_price(5, 5005)])           # bid 5004.5 / ask 5005.5
    assert df["spread"].iloc[0] == pytest.approx(1.0)


def test_dropping_the_forming_candle_targets_the_newest_bar(feed):
    """End-to-end guard on the interaction that matters: reversed input must
    still leave main_alerts dropping the LATEST bar, not the oldest."""
    feed._epic = "US500"
    df = feed._to_df([_price(h, 5000 + h) for h in range(10, 4, -1)])
    closed = df.iloc[:-1].reset_index(drop=True)
    assert closed["close"].iloc[-1] == 5009      # 5010 (forming) removed
    assert closed["close"].iloc[0] == 5005       # oldest retained

"""
Tests for strategy layer:
  indicators, regime_filter, liquidity_sweep, signal_filter, gold_strategy.
"""
import math
import pytest

from strategy.base import Candle, MarketRegime, TF_H1, TF_H4
from strategy.indicators import atr, ema, swing_highs, swing_lows, true_range
from strategy.regime_filter import RegimeFilter
from strategy.liquidity_sweep import LiquiditySweepDetector
from strategy.signal_filter import MLSignalFilter
from strategy.gold_strategy import GoldStrategy
from execution.models import Signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def flat_candles(n: int, price: float = 2300.0) -> list[Candle]:
    return [
        Candle(timestamp=f"2024-01-01T{i:02d}:00:00Z", open=price, high=price + 1,
               low=price - 1, close=price)
        for i in range(n)
    ]


def trending_candles(n: int, start: float = 2200.0, step: float = 2.0) -> list[Candle]:
    candles = []
    p = start
    for i in range(n):
        candles.append(Candle(
            timestamp=f"2024-01-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
            open=p, high=p + 3, low=p - 1, close=p + step,
        ))
        p += step
    return candles


def volatile_candles(n: int, base: float = 2300.0, swing: float = 60.0) -> list[Candle]:
    import math
    candles = []
    for i in range(n):
        high = base + swing * (1 + math.sin(i))
        low = base - swing * (1 + math.cos(i))
        close = (high + low) / 2
        candles.append(Candle(
            timestamp=f"2024-01-01T{i % 24:02d}:00:00Z",
            open=base, high=high, low=low, close=close,
        ))
    return candles


# ── EMA ───────────────────────────────────────────────────────────────────────

def test_ema_returns_empty_when_insufficient_data():
    assert ema([1.0, 2.0], period=5) == []


def test_ema_length_matches_input():
    prices = [float(i) for i in range(1, 21)]
    result = ema(prices, period=5)
    assert len(result) == len(prices)


def test_ema_first_valid_value_equals_sma():
    prices = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    result = ema(prices, period=5)
    assert not math.isnan(result[4])
    assert result[4] == pytest.approx(3.0)  # SMA of first 5


def test_ema_leading_values_are_nan():
    result = ema([1.0] * 10, period=5)
    assert all(math.isnan(v) for v in result[:4])
    assert not math.isnan(result[4])


def test_ema_constant_series_returns_constant():
    result = ema([5.0] * 20, period=5)
    valid = [v for v in result if not math.isnan(v)]
    assert all(v == pytest.approx(5.0) for v in valid)


# ── ATR ───────────────────────────────────────────────────────────────────────

def test_atr_returns_empty_when_insufficient():
    candles = flat_candles(5)
    assert atr(candles, period=14) == []


def test_atr_length_matches_input():
    candles = flat_candles(50)
    result = atr(candles, period=14)
    assert len(result) == len(candles)


def test_atr_non_negative():
    candles = flat_candles(50)
    valid = [v for v in atr(candles, 14) if not math.isnan(v)]
    assert all(v >= 0 for v in valid)


def test_true_range_flat_candles_equals_high_minus_low():
    candles = flat_candles(5, price=2300.0)
    tr = true_range(candles)
    # high=2301, low=2299 → TR = 2 (first bar has no prev close)
    assert tr[0] == pytest.approx(2.0)


# ── Swing pivots ──────────────────────────────────────────────────────────────

def test_swing_highs_detects_peak():
    prices = [10, 11, 15, 11, 10, 11, 10]
    candles = [Candle(f"t{i}", p, p + 0.5, p - 0.5, p) for i, p in enumerate(prices)]
    sh = swing_highs(candles, lookback=2)
    assert sh[2] == 15.5  # high at index 2 is the pivot (high = price + 0.5 = 15.5)


def test_swing_lows_detects_trough():
    prices = [10, 9, 5, 9, 10, 9, 10]
    candles = [Candle(f"t{i}", p, p + 0.5, p - 0.5, p) for i, p in enumerate(prices)]
    sl = swing_lows(candles, lookback=2)
    assert sl[2] == 4.5  # low = price - 0.5 = 4.5


# ── Regime filter ─────────────────────────────────────────────────────────────

def test_regime_insufficient_data_returns_ranging():
    rf = RegimeFilter()
    result = rf.classify(flat_candles(10))
    assert result == MarketRegime.RANGING


def test_regime_volatile_candles():
    rf = RegimeFilter(volatile_atr_pct=0.005)  # tight threshold to force VOLATILE
    candles = volatile_candles(200)
    result = rf.classify(candles)
    assert result == MarketRegime.VOLATILE


def test_regime_flat_candles_not_volatile():
    rf = RegimeFilter()
    candles = flat_candles(200)
    result = rf.classify(candles)
    assert result != MarketRegime.VOLATILE


def test_regime_strong_uptrend():
    rf = RegimeFilter(volatile_atr_pct=0.05)  # generous threshold so trend is detected
    candles = trending_candles(200, step=3.0)
    result = rf.classify(candles)
    assert result == MarketRegime.TRENDING_UP


# ── Liquidity sweep ───────────────────────────────────────────────────────────

def test_sweep_insufficient_data_returns_none():
    det = LiquiditySweepDetector()
    assert det.detect(flat_candles(5)) is None


def test_sweep_flat_market_returns_none():
    det = LiquiditySweepDetector()
    assert det.detect(flat_candles(100)) is None


def _build_sweep_candles(direction: str, lookback: int = 20, sweep_lookback: int = 5) -> list[Candle]:
    """
    Build a candle series containing a confirmed liquidity sweep.

    Layout (N = total candles needed):
      [0 .. N-sweep_lookback-3]   — background flat bars
      [N-sweep_lookback-2]        — the pivot bar (extreme high or low)
      [N-sweep_lookback-1 .. N-2] — post-pivot recovery bars
      [N-1]                       — the signal bar (sweep + close-back-inside)

    The pivot must sit at least sweep_lookback bars from each end of the
    window slice passed to swing_highs/swing_lows, otherwise it can't be
    confirmed as a pivot.
    """
    base = 2300.0
    # Total needed: lookback + sweep_lookback*2 + 2 (detector minimum) + sweep_lookback extra
    n = lookback + sweep_lookback * 3 + 5
    candles: list[Candle] = []

    pivot_idx = n - sweep_lookback - 2  # pivot sits sweep_lookback bars before the signal

    for i in range(n - 1):
        if direction == "buy" and i == pivot_idx:
            # A confirmed swing low: low much lower than neighbours
            c = Candle(timestamp=f"t{i}", open=base, high=base + 2, low=base - 30, close=base)
        elif direction == "sell" and i == pivot_idx:
            # A confirmed swing high: high much higher than neighbours
            c = Candle(timestamp=f"t{i}", open=base, high=base + 30, low=base - 2, close=base)
        else:
            c = Candle(timestamp=f"t{i}", open=base, high=base + 5, low=base - 5, close=base)
        candles.append(c)

    # Signal bar: sweeps the pivot level but closes back inside
    if direction == "buy":
        pivot_low = base - 30
        candles.append(Candle(
            timestamp=f"t{n-1}",
            open=base,
            high=base + 2,
            low=pivot_low - 10,   # swept below the pivot low
            close=base + 1,       # closed back above it
        ))
    else:
        pivot_high = base + 30
        candles.append(Candle(
            timestamp=f"t{n-1}",
            open=base,
            high=pivot_high + 10,  # swept above the pivot high
            low=base - 2,
            close=base - 1,        # closed back below it
        ))
    return candles


def test_sweep_buy_detected():
    det = LiquiditySweepDetector(lookback=20, sweep_lookback=5)
    candles = _build_sweep_candles("buy")
    result = det.detect(candles)
    assert result == "buy"


def test_sweep_sell_detected():
    det = LiquiditySweepDetector(lookback=20, sweep_lookback=5)
    candles = _build_sweep_candles("sell")
    result = det.detect(candles)
    assert result == "sell"


# ── ML signal filter ──────────────────────────────────────────────────────────

def test_ml_filter_accepts_all_by_default():
    flt = MLSignalFilter()
    sig = Signal(direction="buy", lots=0.05)
    candles = flat_candles(50)
    assert flt.accept(sig, candles) is True


# ── GoldStrategy ──────────────────────────────────────────────────────────────

def _make_candles(n_h4: int = 200, n_h1: int = 200) -> dict:
    return {TF_H4: flat_candles(n_h4), TF_H1: flat_candles(n_h1)}


def test_strategy_returns_none_when_not_enough_h4():
    strat = GoldStrategy()
    result = strat.evaluate({TF_H4: flat_candles(5), TF_H1: flat_candles(200)})
    assert result is None


def test_strategy_returns_none_when_not_enough_h1():
    strat = GoldStrategy()
    result = strat.evaluate({TF_H4: flat_candles(200), TF_H1: flat_candles(5)})
    assert result is None


def test_strategy_returns_none_on_volatile_regime():
    rf = RegimeFilter(volatile_atr_pct=0.001)  # force VOLATILE
    strat = GoldStrategy(regime_filter=rf)
    result = strat.evaluate({TF_H4: volatile_candles(200), TF_H1: flat_candles(200)})
    assert result is None


def test_strategy_returns_none_when_no_sweep():
    strat = GoldStrategy()
    # flat candles → no sweep detected
    result = strat.evaluate({TF_H4: flat_candles(200), TF_H1: flat_candles(200)})
    assert result is None


def test_strategy_returns_none_when_ml_filter_rejects():
    class RejectAll(MLSignalFilter):
        def _predict(self, signal, candles):
            return False

    det = LiquiditySweepDetector(lookback=20, sweep_lookback=5)
    strat = GoldStrategy(signal_filter=RejectAll(), sweep_detector=det)
    h1 = _build_sweep_candles("buy")
    result = strat.evaluate({TF_H4: flat_candles(200), TF_H1: h1})
    assert result is None


def test_strategy_signal_never_touches_broker():
    """Strategy evaluate() must return a Signal (or None) — no side effects."""
    strat = GoldStrategy()
    candles = _make_candles()
    result = strat.evaluate(candles)
    assert result is None or isinstance(result, Signal)


def test_strategy_signal_direction_aligns_with_trending_up():
    """In a trending-up regime a sell sweep must be suppressed."""
    rf = RegimeFilter(volatile_atr_pct=0.05)
    det = LiquiditySweepDetector(lookback=20, sweep_lookback=5)
    strat = GoldStrategy(regime_filter=rf, sweep_detector=det, lots=0.05)

    h4 = trending_candles(200, step=3.0)  # strongly trending up
    h1_sell = _build_sweep_candles("sell")
    result = strat.evaluate({TF_H4: h4, TF_H1: h1_sell})
    assert result is None  # misaligned direction must be dropped

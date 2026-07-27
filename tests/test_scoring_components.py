"""
Invariants on the score breakdown.

`components` must always reconstruct `score` exactly, and the `additional`
bucket must equal the sum of its five sub-keys. If either drifts, any analysis
built on the journal silently becomes wrong.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from strategy.scoring_strategy import ScoringStrategy, MarketData

TOP_KEYS = ("pattern", "confirmation", "daily_bias", "session", "additional")
SUB_KEYS = ("round_number", "volume_confirm", "anchored_vwap",
            "volume_profile", "choppy")

T0 = dt.datetime(2026, 7, 15, 14, 0)


def _synthetic(seed: int, n: int = 220, drift: float = 0.4, vol: float = 9.0):
    """Random-walk OHLCV with a 'time' column, shaped like the live feed."""
    rng   = np.random.default_rng(seed)
    close = 5000 + np.cumsum(rng.normal(drift, vol, n))
    high  = close + np.abs(rng.normal(5, 3, n))
    low   = close - np.abs(rng.normal(5, 3, n))
    return pd.DataFrame({
        "time":   [T0 + dt.timedelta(hours=i) for i in range(n)],
        "open":   close + rng.normal(0, 2, n),
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": rng.integers(100, 5000, n).astype(float),
    })


def _signals(seeds=range(12)):
    """Collect signals by walking each series bar-by-bar, as the backtest does.

    Evaluating only the final bar yields roughly one signal per hundred series;
    walking the window exercises every detector and gives the invariants a
    sample worth asserting on.

    The engine needs >= 60 daily rows as well as >= 60 H1 rows, so the daily
    frame is generated independently rather than resampled from the H1 one.
    """
    out = []
    for s in seeds:
        for drift, vol in ((0.4, 9.0), (1.5, 12.0), (-1.5, 12.0), (0.0, 6.0)):
            df    = _synthetic(s, drift=drift, vol=vol)
            daily = _synthetic(s + 9_000, n=120, drift=drift, vol=vol * 3)
            for epic in ("US500", "BTCUSD"):
                strat = ScoringStrategy(epic)
                for i in range(80, len(df), 3):
                    sig = strat.evaluate(MarketData(
                        epic=epic, h1=df.iloc[:i + 1], daily=daily,
                        now_utc=T0 + dt.timedelta(hours=i)))
                    if sig is not None:
                        out.append(sig)
    return out


@pytest.fixture(scope="module")
def signals():
    sigs = _signals()
    if not sigs:
        pytest.skip("no signals generated from the synthetic series")
    return sigs


def test_components_reconstruct_score(signals):
    for sig in signals:
        total = sum(sig.components[k] for k in TOP_KEYS)
        assert sig.score == int(round(total)), sig.components


def test_additional_equals_sum_of_subkeys(signals):
    for sig in signals:
        assert sig.components["additional"] == sum(
            sig.components[k] for k in SUB_KEYS), sig.components


def test_all_subkeys_always_present(signals):
    """Absent vs zero must be distinguishable — every sub-key is unconditional."""
    for sig in signals:
        for k in TOP_KEYS + SUB_KEYS:
            assert k in sig.components, f"{k} missing from {sig.components}"


def test_context_populated(signals):
    required = ("bias_state", "adx", "atr", "atr_pct", "session_label",
                "sl_clip", "raw_sl_dist_atr", "entry_dist_atr",
                "avwap_state", "vp_state", "confirm_count")
    for sig in signals:
        for k in required:
            assert k in sig.context, f"{k} missing from context"
        assert sig.context["sl_clip"] in ("none", "min", "max")
        assert sig.context["avwap_state"] in ("aligned", "opposed", "unavailable")
        assert sig.context["vp_state"] in ("aligned", "opposed", "unavailable")


def test_sl_clip_reports_the_atr_band(signals):
    """sl_clip must agree with where raw_sl_dist_atr sits relative to the band."""
    from strategy import strategy_config as C
    for sig in signals:
        cfg = C.INSTRUMENTS[sig.epic]
        raw = sig.context["raw_sl_dist_atr"]
        expected = ("min" if raw < cfg["atr_min"] - 1e-6 else
                    "max" if raw > cfg["atr_max"] + 1e-6 else "none")
        assert sig.context["sl_clip"] == expected, (raw, cfg["atr_min"], cfg["atr_max"])

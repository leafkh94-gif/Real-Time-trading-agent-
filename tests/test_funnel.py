"""
Rejection-funnel accounting.

The engine had nine silent `return None` exits and no record of which one
fired, so "the bot is not sending" could not be answered — a market with no
patterns and a market full of patterns that all failed one filter looked
identical from outside. These tests pin the counters that make the two
distinguishable.

The load-bearing test is `test_every_evaluation_is_counted_exactly_once`: it
is what stops a future exit being added without a counter, which would
silently under-report the funnel and make it lie by omission.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from strategy import strategy_config as C
from strategy.scoring_strategy import (
    REJECT_STAGES, MarketData, ScoringStrategy, funnel_report,
)

T0 = dt.datetime(2026, 7, 15, 14, 0)


def _synthetic(seed: int, n: int = 220, drift: float = 0.4, vol: float = 9.0):
    rng   = np.random.default_rng(seed)
    close = 5000 + np.cumsum(rng.normal(drift, vol, n))
    return pd.DataFrame({
        "time":   [T0 + dt.timedelta(hours=i) for i in range(n)],
        "open":   close + rng.normal(0, 2, n),
        "high":   close + np.abs(rng.normal(5, 3, n)),
        "low":    close - np.abs(rng.normal(5, 3, n)),
        "close":  close,
        "volume": rng.integers(100, 5000, n).astype(float),
    })


def _walk(seeds=range(6)):
    """Walk several regimes bar by bar, returning (strategy, n_calls) pairs."""
    out = []
    for s in seeds:
        for drift, vol in ((0.4, 9.0), (1.5, 12.0), (-1.5, 12.0), (0.0, 6.0)):
            df    = _synthetic(s, drift=drift, vol=vol)
            daily = _synthetic(s + 9_000, n=120, drift=drift, vol=vol * 3)
            for epic in ("US500", "BTCUSD"):
                strat = ScoringStrategy(epic)
                calls = 0
                for i in range(80, len(df), 3):
                    strat.evaluate(MarketData(
                        epic=epic, h1=df.iloc[:i + 1], daily=daily,
                        now_utc=T0 + dt.timedelta(hours=i)))
                    calls += 1
                out.append((strat, calls))
    return out


@pytest.fixture(scope="module")
def walked():
    return _walk()


def test_every_evaluation_is_counted_exactly_once(walked):
    """One evaluate() call -> exactly one funnel increment.

    This is the invariant that keeps the funnel honest. An exit added later
    without a counter shows up here as a shortfall, rather than as a quietly
    wrong percentage in a report nobody can check.
    """
    for strat, calls in walked:
        assert sum(strat.funnel.values()) == calls, (
            f"{strat.epic}: {sum(strat.funnel.values())} counted vs {calls} calls")


def test_only_known_stages_are_recorded(walked):
    for strat, _ in walked:
        assert set(strat.funnel) <= set(REJECT_STAGES)


def test_the_funnel_is_not_degenerate(walked):
    """Across mixed regimes more than one exit must fire, or the counter is
    measuring nothing useful."""
    seen: set[str] = set()
    for strat, _ in walked:
        seen |= {k for k, v in strat.funnel.items() if v}
    assert len(seen) >= 3, f"only {seen} ever fired"
    assert "no_pattern" in seen


def test_last_reject_tracks_the_latest_call():
    df    = _synthetic(3)
    daily = _synthetic(9_003, n=120)
    strat = ScoringStrategy("US500")
    # Too little history is the first gate in the pipeline.
    assert strat.evaluate(MarketData(epic="US500", h1=df.iloc[:10],
                                     daily=daily, now_utc=T0)) is None
    assert strat.last_reject == "insufficient_history"
    assert strat.funnel["insufficient_history"] == 1


def test_signal_clears_last_reject(monkeypatch):
    """A produced signal must reset last_reject, or a stale stage name would
    be reported against a bar that in fact alerted."""
    monkeypatch.setattr(C, "WATCH_MIN", 0)      # force the gate open
    for seed in range(40):
        df    = _synthetic(seed, drift=1.5, vol=12.0)
        daily = _synthetic(seed + 9_000, n=120, drift=1.5, vol=36.0)
        strat = ScoringStrategy("US500")
        for i in range(80, len(df), 3):
            sig = strat.evaluate(MarketData(epic="US500", h1=df.iloc[:i + 1],
                                            daily=daily,
                                            now_utc=T0 + dt.timedelta(hours=i)))
            if sig is not None:
                assert strat.last_reject is None
                assert strat.funnel["signal"] >= 1
                return
    pytest.fail("no signal produced with the score gate open")


def test_below_threshold_is_reachable_and_threshold_moves_it():
    """Raising WATCH_MIN must move candidates into below_threshold and nowhere
    else — the gate is a filter on score, not on anything upstream."""
    df    = _synthetic(5, drift=1.5, vol=12.0)
    daily = _synthetic(9_005, n=120, drift=1.5, vol=36.0)

    def run(watch_min):
        original = C.WATCH_MIN
        C.WATCH_MIN = watch_min
        try:
            strat = ScoringStrategy("US500")
            for i in range(80, len(df), 3):
                strat.evaluate(MarketData(epic="US500", h1=df.iloc[:i + 1],
                                          daily=daily,
                                          now_utc=T0 + dt.timedelta(hours=i)))
            return dict(strat.funnel)
        finally:
            C.WATCH_MIN = original

    loose, tight = run(0), run(999)
    assert tight.get("below_threshold", 0) > loose.get("below_threshold", 0)
    # Stages before the score gate are untouched by it.
    for stage in ("insufficient_history", "bad_atr", "volatile_regime",
                  "no_pattern", "counter_trend"):
        assert loose.get(stage, 0) == tight.get(stage, 0), stage


def test_report_shows_live_gates_without_percentages():
    """Live-bot gates reject whole scans, not per-bar candidates, so they must
    print as bare counts rather than borrow the candidate denominator."""
    txt = funnel_report({"no_pattern": 900, "below_threshold": 30,
                         "signal": 10, "cooldown": 55})
    assert "cooldown" in txt
    assert "% of candidates" in txt
    cooldown_line = [l for l in txt.splitlines() if "cooldown" in l][0]
    assert "%" not in cooldown_line


def test_report_survives_an_empty_funnel():
    assert "nothing evaluated" in funnel_report({})

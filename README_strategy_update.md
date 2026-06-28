# Strategy update — scoring engine (US500 / US100 / US30 / BTCUSD)

Replaces the old H1+M15 two-gate sweep+BOS logic with the scoring model from the
strategy document. Gold is removed; **BTCUSD is added**. Alert only — no execution.

## New / changed files
- `strategy/strategy_config.py` — every tunable number (thresholds, pattern scores,
  factor weights, session windows, ATR multipliers, per-instrument config).
- `strategy/indicators.py` — EMA/SMA/RSI/MACD/ATR + swing detection.
- `strategy/market_sessions.py` — session score (Factor 5), **DST-aware** news
  blackout + BTC US-overlap, and the index market-hours guard.
- `strategy/scoring_strategy.py` — the engine: 5 pattern detectors → Factors 1–5 +
  additional → WATCH/A+ → entry/SL/TP per pattern type and instrument ATR.
- `main_alerts.py` — wires it in: BTC added, correlation filter (BTC exempt),
  A+ daily cap (4) / no WATCH cap, adaptive threshold, news blackout, Arabic alerts.

## One integration point you must wire
The engine needs **M15** (signals) and **DAILY** (bias) candles. `main_alerts.py`
calls `feed.get_candles(resolution, count)` with `"MINUTE_15"` and `"DAY"`,
expecting an OHLCV DataFrame (`open, high, low, close, volume`, oldest→newest).
Your `CapitalComFeed` already fetches H1+M15; add a sibling `get_candles()` backed
by Capital.com `GET /prices/{epic}?resolution=...&max=...`. The old
`get_h1_m15_candles()` and `gold_strategy.py` are no longer used.

## Honest caveats
- Pattern detectors are documented **heuristics**, not a proven edge. Backtest and
  tune `strategy_config.py` before trusting live alerts.
- BTC ATR multipliers (2.2× / 0.60×) and session weights are **starting points** —
  calibrate on real BTC data.
- Index session-bonus windows are kept as the doc's literal UTC values; the news
  blackout and BTC US-overlap are DST-corrected. If you want the index sessions
  DST-corrected too, say so.
- State (daily caps, adaptive threshold, cooldowns) persists in `STATE_FILE`. On
  GitHub Actions, persist it across runs (cache or commit) or counts reset each run.

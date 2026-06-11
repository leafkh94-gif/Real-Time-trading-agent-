#!/usr/bin/env python3
"""
backtest.py — Historical backtester for the liquidity-sweep strategy.

Downloads 1 year of H1 data for Gold, S&P 500, Nasdaq 100, and Dow Jones,
then simulates the full strategy pipeline on a rolling window:

  Gate 1 — Regime filter    (H4 EMA20/50 + ATR volatility check)
  Gate 2 — Liquidity sweep  (sweep + CHOCH/BOS confirmation)
  Gate 3 — EMA distance     (suppress counter-trend in rocket markets)
  Gate 4 — R:R check        (swing TP must give ≥ 2.0 reward:risk)

For each confirmed signal, looks forward FORWARD_BARS H1 candles to
determine whether TP or SL was hit first.

Output: win rate, signals/month, avg R:R — per instrument and overall.

Usage:
  python backtest.py
  python backtest.py --forward-bars 72   # use 72h forward window
  python backtest.py --save-csv          # also write backtest_results.csv
"""
import argparse
import csv
import math
import sys
from collections import defaultdict

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    sys.exit("ERROR: yfinance and pandas required.  Run: pip install yfinance pandas")

from strategy.base import Candle, TF_H1, TF_H4
from strategy.gold_strategy import GoldStrategy
from strategy.indicators import atr as _atr, ema as _ema, swing_highs, swing_lows


# ── Configuration ─────────────────────────────────────────────────────────────

INSTRUMENTS = [
    ("GOLD",  "GC=F"),
    ("US500", "^GSPC"),
    ("US100", "^NDX"),
    ("US30",  "^DJI"),
]

SL_ATR_MULT            = 1.5
MIN_RR_RATIO           = 2.0
EMA_TREND_STRENGTH_PCT = 1.5

H1_WINDOW            = 400   # rolling H1 bars fed to strategy (enough for ATR + pivots)
H4_WINDOW            = 150   # rolling H4 bars fed to strategy (enough for EMA-50)
MIN_SIGNAL_SPACING   = 8     # minimum H1 bars between signals (suppresses duplicates)
DEFAULT_FORWARD_BARS = 48    # H1 bars forward to check TP/SL (48 = 2 days)


# ── Data fetching ──────────────────────────────────────────────────────────────

def _df_to_candles(df: "pd.DataFrame") -> list:
    candles = []
    for ts, row in df.iterrows():
        o = float(row.get("Open",   row.get("open",   0) or 0))
        h = float(row.get("High",   row.get("high",   0) or 0))
        l = float(row.get("Low",    row.get("low",    0) or 0))
        c = float(row.get("Close",  row.get("close",  0) or 0))
        v = float(row.get("Volume", row.get("volume", 0) or 0))
        if c <= 0 or math.isnan(c):
            continue
        candles.append(Candle(timestamp=str(ts), open=o, high=h, low=l, close=c, volume=v))
    return candles


def fetch_data(ticker: str) -> tuple[list, list]:
    """Download 1 year of H1 data; return (h1_candles, h4_candles)."""
    raw = yf.download(ticker, period="1y", interval="1h", progress=False, auto_adjust=True)
    if raw.empty:
        return [], []
    # Flatten MultiIndex columns (yfinance sometimes nests on multi-ticker downloads)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    h1 = _df_to_candles(raw)

    raw_h4 = raw.resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Close"])
    h4 = _df_to_candles(raw_h4)

    return h1, h4


# ── Helpers ────────────────────────────────────────────────────────────────────

def _nearest_swing_tp(h1: list, entry: float, direction: str) -> float | None:
    sh = swing_highs(h1, lookback=5)
    sl = swing_lows( h1, lookback=5)
    if direction == "buy":
        candidates = [v for v in sh if v is not None and v > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [v for v in sl if v is not None and v < entry]
        return max(candidates) if candidates else None


def _check_outcome(h1_all: list, signal_idx: int,
                   entry: float, tp: float, sl: float,
                   forward_bars: int) -> str:
    """Returns 'win', 'loss', or 'open' (neither hit within forward_bars)."""
    end    = min(signal_idx + 1 + forward_bars, len(h1_all))
    is_buy = tp > entry
    for i in range(signal_idx + 1, end):
        bar = h1_all[i]
        if is_buy:
            if bar.high >= tp:  return "win"
            if bar.low  <= sl:  return "loss"
        else:
            if bar.low  <= tp:  return "win"
            if bar.high >= sl:  return "loss"
    return "open"


# ── Core backtest loop ─────────────────────────────────────────────────────────

def run_backtest(epic: str, h1_all: list, h4_all: list,
                 strategy: GoldStrategy, forward_bars: int) -> list:
    """
    Walk through every H1 bar, run the full strategy pipeline with a
    fixed-size rolling window (no look-ahead), collect confirmed signals,
    then check whether each signal hit TP or SL within forward_bars candles.
    """
    trades: list[dict] = []
    n_h1, n_h4        = len(h1_all), len(h4_all)
    last_signal_idx   = -MIN_SIGNAL_SPACING - 1
    h4_ptr            = 0   # single-pass pointer through H4 array

    for i in range(H1_WINDOW, n_h1 - forward_bars):
        bar_ts = h1_all[i].timestamp

        # Advance H4 pointer: include all H4 bars whose start <= current H1 bar
        while h4_ptr + 1 < n_h4 and h4_all[h4_ptr + 1].timestamp <= bar_ts:
            h4_ptr += 1

        # Fixed-size rolling windows (avoids O(n²) memory growth)
        h1_win = h1_all[max(0, i + 1 - H1_WINDOW): i + 1]
        h4_win = h4_all[max(0, h4_ptr + 1 - H4_WINDOW): h4_ptr + 1]

        if len(h4_win) < strategy.regime_filter.min_candles:
            continue
        if len(h1_win) < strategy.sweep_detector.min_candles:
            continue

        # Minimum spacing guard (CHOCH/BOS confirmation already reduces stacking,
        # but a confirmed BOS can span several consecutive bars — guard removes dups)
        if i - last_signal_idx < MIN_SIGNAL_SPACING:
            continue

        candles = {TF_H1: h1_win, TF_H4: h4_win}

        try:
            sig = strategy.evaluate(candles)
        except Exception:
            continue

        if sig is None:
            continue

        # Gate: EMA trend-strength (mirror of main_alerts.py Gate 5)
        if h4_win:
            closes = [c.close for c in h4_win]
            e20 = _ema(closes, 20)
            e50 = _ema(closes, 50)
            if (e20 and e50
                    and not math.isnan(e20[-1]) and not math.isnan(e50[-1])
                    and e50[-1] > 0):
                spread_pct = (e20[-1] - e50[-1]) / e50[-1] * 100
                if abs(spread_pct) > EMA_TREND_STRENGTH_PCT:
                    is_bull = spread_pct > 0
                    if (is_bull and sig.direction == "sell") or \
                       (not is_bull and sig.direction == "buy"):
                        continue

        # Gate: R:R (mirror of main_alerts.py Gate 6)
        atr_series = _atr(h1_win, period=14)
        valid_atr  = [v for v in atr_series if v == v]
        if not valid_atr:
            continue
        current_atr = valid_atr[-1]
        entry       = h1_win[-1].close

        swing_tp = _nearest_swing_tp(h1_win, entry, sig.direction)
        if swing_tp is None:
            continue

        sl     = (entry - SL_ATR_MULT * current_atr if sig.direction == "buy"
                  else entry + SL_ATR_MULT * current_atr)
        risk   = abs(entry - sl)
        reward = abs(swing_tp - entry)
        rr     = reward / risk if risk > 0 else 0.0

        if rr < MIN_RR_RATIO:
            continue

        last_signal_idx = i

        outcome = _check_outcome(h1_all, i, entry, swing_tp, sl, forward_bars)
        month   = str(bar_ts)[:7]

        trades.append({
            "epic":      epic,
            "timestamp": str(bar_ts),
            "month":     month,
            "direction": sig.direction,
            "entry":     round(entry,    4),
            "tp":        round(swing_tp, 4),
            "sl":        round(sl,       4),
            "rr":        round(rr,       2),
            "outcome":   outcome,
        })

        outcome_sym = "✅ WIN" if outcome == "win" else ("❌ LOSS" if outcome == "loss" else "⏳ OPEN")
        print(f"  [{month}] {sig.direction.upper():<4}  "
              f"entry={entry:>10.2f}  tp={swing_tp:>10.2f}  sl={sl:>10.2f}  "
              f"R:R=1:{rr:.1f}  {outcome_sym}")

    return trades


# ── Reporting ──────────────────────────────────────────────────────────────────

def _pct(num: int, denom: int) -> str:
    return f"{num / denom * 100:.1f}%" if denom else "–"


def print_report(all_trades: list) -> None:
    W = 68
    SEP = "═" * W
    sep = "─" * W

    print(f"\n{SEP}")
    print("  BACKTEST SUMMARY — Last 12 months")
    print("  Gates: Regime filter → Liquidity Sweep + CHOCH/BOS → EMA distance → R:R ≥ 2.0")
    print(f"  Forward window: {DEFAULT_FORWARD_BARS} H1 bars (≈ 2 trading days)")
    print(SEP)

    if not all_trades:
        print("  No signals generated — check Yahoo Finance connectivity.")
        print(SEP)
        return

    by_epic = defaultdict(list)
    for t in all_trades:
        by_epic[t["epic"]].append(t)

    for epic, trades in sorted(by_epic.items()):
        wins   = [t for t in trades if t["outcome"] == "win"]
        losses = [t for t in trades if t["outcome"] == "loss"]
        opens  = [t for t in trades if t["outcome"] == "open"]
        closed = wins + losses

        print(f"\n  {epic}")
        print(f"  {sep}")
        print(f"  {'Total signals':<22} {len(trades):>5}")
        print(f"  {'Wins':<22} {len(wins):>5}  ({_pct(len(wins),   len(closed))} of closed)")
        print(f"  {'Losses':<22} {len(losses):>5}  ({_pct(len(losses), len(closed))} of closed)")
        print(f"  {'Still open':<22} {len(opens):>5}")

        months   = sorted(set(t["month"] for t in trades))
        n_months = len(months) or 1
        print(f"  {'Signals/month':<22} {len(trades) / n_months:>5.1f}")

        if closed:
            avg_rr = sum(t["rr"] for t in closed) / len(closed)
            print(f"  {'Avg R:R':<22} {'1:' + f'{avg_rr:.2f}':>5}")

        by_month = defaultdict(list)
        for t in trades:
            by_month[t["month"]].append(t)

        print(f"\n  {'Month':<12} {'Sigs':>5}  {'W':>4}  {'L':>4}  {'Win%':>6}")
        for m in months:
            mt = by_month[m]
            mw = sum(1 for t in mt if t["outcome"] == "win")
            ml = sum(1 for t in mt if t["outcome"] == "loss")
            mc = mw + ml
            print(f"  {m:<12} {len(mt):>5}  {mw:>4}  {ml:>4}  {_pct(mw, mc):>6}")

    # Overall
    wins   = [t for t in all_trades if t["outcome"] == "win"]
    losses = [t for t in all_trades if t["outcome"] == "loss"]
    closed = wins + losses
    opens  = [t for t in all_trades if t["outcome"] == "open"]
    months = len(set(t["month"] for t in all_trades)) or 1

    print(f"\n{SEP}")
    print("  OVERALL — all instruments combined")
    print(f"  {sep}")
    print(f"  {'Total signals':<22} {len(all_trades):>5}")
    print(f"  {'Win rate':<22} {_pct(len(wins), len(closed)):>5}  "
          f"({len(wins)} wins / {len(closed)} closed trades)")
    print(f"  {'Open (pending)':<22} {len(opens):>5}")
    print(f"  {'Signals/month':<22} {len(all_trades) / months:>5.1f}")
    if closed:
        avg_rr = sum(t["rr"] for t in closed) / len(closed)
        print(f"  {'Avg R:R':<22} {'1:' + f'{avg_rr:.2f}':>5}")
    print(f"{SEP}\n")


def save_csv(trades: list, path: str) -> None:
    if not trades:
        return
    fields = ["epic", "timestamp", "month", "direction", "entry", "tp", "sl", "rr", "outcome"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)
    print(f"Trade log saved → {path}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the liquidity-sweep strategy")
    parser.add_argument(
        "--forward-bars", type=int, default=DEFAULT_FORWARD_BARS,
        help=f"H1 bars to check after signal for TP/SL hit (default: {DEFAULT_FORWARD_BARS})",
    )
    parser.add_argument(
        "--save-csv", action="store_true",
        help="Write every trade to backtest_results.csv",
    )
    args = parser.parse_args()

    strategy   = GoldStrategy()
    all_trades = []

    for epic, ticker in INSTRUMENTS:
        print(f"\n{'─'*50}")
        print(f"  {epic}  ({ticker})")
        print(f"{'─'*50}")
        print("  Downloading 1 year of H1 data ...")
        h1, h4 = fetch_data(ticker)
        if not h1:
            print(f"  ERROR: no data returned for {ticker}")
            continue
        print(f"  {len(h1)} H1 bars  |  {len(h4)} H4 bars")
        print(f"  Running backtest (forward={args.forward_bars} bars) ...")

        trades = run_backtest(epic, h1, h4, strategy, args.forward_bars)
        print(f"\n  → {len(trades)} signals total")
        all_trades.extend(trades)

    print_report(all_trades)

    if args.save_csv:
        save_csv(all_trades, "backtest_results.csv")


if __name__ == "__main__":
    main()

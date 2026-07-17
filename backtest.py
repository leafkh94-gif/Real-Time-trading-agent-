"""
backtest.py — walk-forward historical validation of the scoring strategy.

Replays the last ~1000 closed H1 candles per instrument through the exact
same ScoringStrategy.evaluate() the live bot uses, then resolves each signal
with the exact same fill/TP/SL logic as the live journal (journal.resolve).
Prints fill rate, win rate and expectancy overall and per pattern.

No parameter is tuned automatically — this is an evidence report, not an
optimizer.

Usage:
    python backtest.py                 # all instruments
    python backtest.py US500 BTCUSD    # subset

Requires the same CAPITAL_* env keys as the live bot (demo by default).
"""
from __future__ import annotations

import datetime as dt
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import pandas as pd

from strategy import journal
from strategy import strategy_config as C
from strategy.capital_feed import CapitalComFeed
from strategy.scoring_strategy import ScoringStrategy, MarketData

WARMUP_BARS   = 80   # bars of history before the first evaluation
COOLDOWN_BARS = 4    # mirror the live 4-hour per-market cooldown


def backtest_epic(feed: CapitalComFeed, epic: str) -> list[dict]:
    h1    = feed.get_candles("HOUR", 1000)
    daily = feed.get_candles("DAY", 400)
    if len(h1) < WARMUP_BARS + 10 or len(daily) < 70:
        print(f"  {epic}: not enough history ({len(h1)} H1 / {len(daily)} D)")
        return []
    # Drop the still-forming candle on both timeframes (same as live).
    h1    = h1.iloc[:-1].reset_index(drop=True)
    daily = daily.iloc[:-1].reset_index(drop=True)

    strat   = ScoringStrategy(epic)
    entries: list[dict] = []
    last_sig_i = -(10 ** 9)

    for i in range(WARMUP_BARS, len(h1) - 1):
        if i - last_sig_i < COOLDOWN_BARS:
            continue
        bar_t = h1["time"].iloc[i]
        if pd.isna(bar_t):
            continue
        # Decision moment = close of bar i (bar times are candle-open times).
        now = bar_t.to_pydatetime() + dt.timedelta(hours=1)
        dwin = daily[daily["time"] < now]
        if len(dwin) < 60:
            continue
        sig = strat.evaluate(MarketData(epic=epic, h1=h1.iloc[: i + 1],
                                        daily=dwin, now_utc=now))
        if sig is None:
            continue
        last_sig_i = i
        entry = journal.entry_from_signal(sig, now)
        journal.resolve(entry, h1.iloc[i + 1:])          # future candles only
        entries.append(entry)
    return entries


def _expectancy(bucket: dict, rows: list[dict]) -> float | None:
    """Average R multiple per decided trade (TP1 = +2R, TP2 = +3R, SL = -1R)."""
    tp1 = sum(1 for r in rows if r["status"] == "tp1_hit")
    tp2 = sum(1 for r in rows if r["status"] == "tp2_hit")
    sl  = sum(1 for r in rows if r["status"] == "sl_hit")
    decided = tp1 + tp2 + sl
    if not decided:
        return None
    return round((tp1 * C.MIN_RR + tp2 * (C.MIN_RR + 1) - sl * 1.0) / decided, 2)


def print_report(all_entries: list[dict]) -> None:
    if not all_entries:
        print("\nNo signals generated over the tested window.")
        return
    s = journal.stats(all_entries)
    print("\n" + "=" * 64)
    print("OVERALL")
    o = s["overall"]
    print(f"  signals {o['total']}  filled {o['filled']}  wins {o['wins']}  "
          f"losses {o['losses']}  expired {o['expired']}  open {o['open']}")
    print(f"  fill rate {o['fill_rate']}  win rate {o['win_rate']}  "
          f"expectancy {_expectancy(o, all_entries)}R per decided trade")
    print("-" * 64)
    print(f"  {'pattern':<14}{'signals':>8}{'wins':>6}{'losses':>8}"
          f"{'expired':>9}{'win rate':>10}{'expect':>8}")
    for pat, b in s["per_pattern"].items():
        rows = [e for e in all_entries if e["pattern"] == pat]
        exp  = _expectancy(b, rows)
        print(f"  {pat:<14}{b['total']:>8}{b['wins']:>6}{b['losses']:>8}"
              f"{b['expired']:>9}{str(b['win_rate']):>10}{str(exp):>8}")
    print("=" * 64)


def main() -> None:
    epics = [e for e in sys.argv[1:] if e in C.INSTRUMENTS] or list(C.INSTRUMENTS)
    cap_key, cap_id, cap_pw = (os.getenv("CAPITAL_API_KEY", ""),
                               os.getenv("CAPITAL_IDENTIFIER", ""),
                               os.getenv("CAPITAL_PASSWORD", ""))
    if not (cap_key and cap_id and cap_pw):
        sys.exit("Missing CAPITAL_* credentials in environment.")
    demo = os.getenv("CAPITAL_DEMO", "true").lower() != "false"

    all_entries: list[dict] = []
    for epic in epics:
        print(f"Backtesting {epic} ...")
        feed = CapitalComFeed(cap_key, cap_id, cap_pw, epic=epic, demo=demo)
        entries = backtest_epic(feed, epic)
        print(f"  {epic}: {len(entries)} signals")
        all_entries.extend(entries)
    print_report(all_entries)


if __name__ == "__main__":
    main()

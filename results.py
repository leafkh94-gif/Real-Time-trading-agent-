"""
results.py — print a summary of bot activity from state/bot.db

Usage:
  python results.py          # today's summary + open positions
  python results.py --all    # full trade history
"""
import sqlite3
import sys
from pathlib import Path

DB = Path("state/bot.db")

if not DB.exists():
    print("No state/bot.db found — has the bot run yet?")
    sys.exit(0)

conn = sqlite3.connect(str(DB))
all_history = "--all" in sys.argv

# ── Daily summary ─────────────────────────────────────────────────────────────
print("=" * 50)
print("DAILY P&L SUMMARY")
print("=" * 50)
rows = conn.execute(
    "SELECT day, pnl, trades FROM daily ORDER BY day DESC"
    + ("" if all_history else " LIMIT 7")
).fetchall()

if rows:
    print(f"{'Date':<12} {'PnL (USD)':>12} {'Trades':>8}")
    print("-" * 34)
    for day, pnl, trades in rows:
        sign = "+" if pnl >= 0 else ""
        print(f"{day:<12} {sign}{pnl:>11.2f} {trades:>8}")
    total_pnl = sum(r[1] for r in rows)
    total_trades = sum(r[2] for r in rows)
    print("-" * 34)
    sign = "+" if total_pnl >= 0 else ""
    print(f"{'TOTAL':<12} {sign}{total_pnl:>11.2f} {total_trades:>8}")
else:
    print("  No daily records yet.")

# ── Open positions ────────────────────────────────────────────────────────────
print()
print("=" * 50)
print("OPEN POSITIONS")
print("=" * 50)
positions = conn.execute(
    "SELECT position_id, symbol, direction, lots, open_price, opened_at "
    "FROM open_positions ORDER BY opened_at DESC"
).fetchall()

if positions:
    for pid, sym, direction, lots, price, opened_at in positions:
        short_id = pid[:8] + "..." if len(pid) > 8 else pid
        print(f"  {direction.upper():4s}  {lots:.2f} lots  {sym}  @ {price:.2f}"
              f"  [{short_id}]  {opened_at[:19]}")
else:
    print("  No open positions.")

print("=" * 50)
conn.close()

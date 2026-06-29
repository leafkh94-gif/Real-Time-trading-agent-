"""
╔══════════════════════════════════════════════════════════════════╗
║          LONG-TERM SMART MONEY BOT — US30 / US100 / US500       ║
║                                                                  ║
║  INDICATORS:                                                     ║
║    Classic  → EMA50, EMA200, RSI, MACD, ATR                     ║
║    SMC/ICT  → Market Structure (BOS/CHoCH), FVG, Liquidity      ║
║                                                                  ║
║  TIMEFRAMES:  Weekly → Daily → H4                               ║
║  SENDS ALERT: Score 70+ only (quality over quantity)            ║
║  SCANS EVERY: 4 hours                                            ║
╚══════════════════════════════════════════════════════════════════╝

INSTALL:
  pip install yfinance pandas-ta requests schedule python-dotenv

CREDENTIALS:
  Telegram credentials are read from environment variables (or a .env file):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  This keeps secrets out of source control and matches how the bot is
  deployed (Render / GitHub Actions secrets).
"""

import os

import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import schedule
import time
import csv
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars may be set directly

# ─── YOUR SETTINGS ────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "1000"))   # your account in USD
RISK_PCT     = float(os.getenv("RISK_PCT", "0.01"))       # risk 1% per trade
LOG_FILE     = os.getenv("LOG_FILE", "signals_log.csv")
ENTER_SCORE  = int(os.getenv("ENTER_SCORE", "70"))        # send alert only if score >= 70

# ─── INSTRUMENTS ──────────────────────────────────────────────────────────────
SYMBOLS = {
    "US30":  "^DJI",
    "US100": "^NDX",
    "US500": "^GSPC",
}


# ══════════════════════════════════════════════════════════════════════════════
#  DATA & INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def get_data(ticker, period, interval):
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        df.dropna(inplace=True)
        if len(df) < 50:
            return None
        return df
    except Exception as e:
        print(f"  ⚠️  Data error {ticker} {interval}: {e}")
        return None


def add_indicators(df):
    if df is None or len(df) < 60:
        return None
    c = df["Close"].squeeze()
    h = df["High"].squeeze()
    l = df["Low"].squeeze()

    df["EMA50"]       = ta.ema(c, length=50)
    df["EMA200"]      = ta.ema(c, length=200)
    df["RSI"]         = ta.rsi(c, length=14)
    df["ATR"]         = ta.atr(h, l, c, length=14)

    macd = ta.macd(c, fast=12, slow=26, signal=9)
    if macd is not None:
        df["MACD"]        = macd.iloc[:, 0]
        df["MACD_Signal"] = macd.iloc[:, 1]
        df["MACD_Hist"]   = macd.iloc[:, 2]

    df.dropna(inplace=True)
    return df if len(df) > 10 else None


# ══════════════════════════════════════════════════════════════════════════════
#  SMC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def find_swing_highs_lows(df, window=5):
    """Find swing highs and lows (pivot points)"""
    highs, lows = [], []
    hi = df["High"].squeeze().values
    lo = df["Low"].squeeze().values

    for i in range(window, len(df) - window):
        # Swing High: highest point in window
        if hi[i] == max(hi[i - window: i + window + 1]):
            highs.append((i, hi[i]))
        # Swing Low: lowest point in window
        if lo[i] == min(lo[i - window: i + window + 1]):
            lows.append((i, lo[i]))

    return highs, lows


def check_bos(df, direction, lookback=30):
    """
    BOS = Break of Structure
    LONG:  price breaks above a recent swing high → bullish BOS
    SHORT: price breaks below a recent swing low  → bearish BOS
    Returns: True/False, level broken
    """
    highs, lows = find_swing_highs_lows(df, window=5)
    current_close = float(df["Close"].iloc[-1])

    if direction == "LONG" and len(highs) >= 2:
        # Get swing highs from last `lookback` candles (excluding last 5)
        recent_highs = [h for h in highs if h[0] >= len(df) - lookback - 5
                        and h[0] < len(df) - 5]
        if recent_highs:
            last_high = recent_highs[-1][1]
            if current_close > last_high:
                return True, last_high

    elif direction == "SHORT" and len(lows) >= 2:
        recent_lows = [l for l in lows if l[0] >= len(df) - lookback - 5
                       and l[0] < len(df) - 5]
        if recent_lows:
            last_low = recent_lows[-1][1]
            if current_close < last_low:
                return True, last_low

    return False, None


def check_choch(df, direction, lookback=20):
    """
    CHoCH = Change of Character
    Confirms the trend has actually flipped — strongest BOS signal
    LONG:  was making lower highs, now breaks above one → CHoCH
    SHORT: was making higher lows, now breaks below one → CHoCH
    """
    highs, lows = find_swing_highs_lows(df, window=5)
    current_close = float(df["Close"].iloc[-1])

    if direction == "LONG" and len(highs) >= 3:
        # Check if previous highs were descending (downtrend) then broke up
        recent = [h[1] for h in highs if h[0] >= len(df) - lookback - 5
                  and h[0] < len(df) - 5]
        if len(recent) >= 2:
            was_downtrend = recent[-1] < recent[-2]  # lower highs = was down
            if was_downtrend and current_close > recent[-1]:
                return True  # broke above → CHoCH bullish

    elif direction == "SHORT" and len(lows) >= 3:
        recent = [l[1] for l in lows if l[0] >= len(df) - lookback - 5
                  and l[0] < len(df) - 5]
        if len(recent) >= 2:
            was_uptrend = recent[-1] > recent[-2]  # higher lows = was up
            if was_uptrend and current_close < recent[-1]:
                return True  # broke below → CHoCH bearish

    return False


def find_fvg(df, direction, lookback=30):
    """
    FVG = Fair Value Gap
    Bullish FVG: High of candle[i] < Low of candle[i+2]  → gap above
    Bearish FVG: Low of candle[i] > High of candle[i+2] → gap below
    Returns: list of active FVGs [(top, bottom)]
    """
    hi = df["High"].squeeze().values
    lo = df["Low"].squeeze().values
    cl = df["Close"].squeeze().values
    fvgs = []

    start = max(0, len(df) - lookback - 3)
    current_price = cl[-1]

    for i in range(start, len(df) - 2):
        c1_high = hi[i]
        c1_low  = lo[i]
        c3_high = hi[i + 2]
        c3_low  = lo[i + 2]

        if direction == "LONG":
            # Bullish FVG: gap between C1 high and C3 low
            if c1_high < c3_low:
                fvg_bottom = c1_high
                fvg_top    = c3_low
                # Active if price is near or inside this FVG
                if fvg_bottom <= current_price * 1.02:  # within 2%
                    fvgs.append((fvg_top, fvg_bottom))

        elif direction == "SHORT":
            # Bearish FVG: gap between C1 low and C3 high
            if c1_low > c3_high:
                fvg_top    = c1_low
                fvg_bottom = c3_high
                if fvg_top >= current_price * 0.98:
                    fvgs.append((fvg_top, fvg_bottom))

    return fvgs


def check_liquidity_sweep(df, direction, lookback=20):
    """
    Liquidity Sweep = price wicks above recent highs (BSL) then reverses
                      or wicks below recent lows (SSL) then reverses
    LONG signal:  SSL swept (wick below low, close back above) → buyers came in
    SHORT signal: BSL swept (wick above high, close back above) → sellers came in
    """
    hi = df["High"].squeeze().values
    lo = df["Low"].squeeze().values
    op = df["Open"].squeeze().values
    cl = df["Close"].squeeze().values

    # Look at last `lookback` candles
    start = max(5, len(df) - lookback)

    for i in range(start, len(df) - 1):
        if direction == "LONG":
            # SSL sweep: candle wicks below a previous low then closes above it
            prev_low = min(lo[max(0, i-10):i])
            if lo[i] < prev_low and cl[i] > prev_low:
                return True, prev_low  # swept and recovered

        elif direction == "SHORT":
            # BSL sweep: candle wicks above a previous high then closes below it
            prev_high = max(hi[max(0, i-10):i])
            if hi[i] > prev_high and cl[i] < prev_high:
                return True, prev_high  # swept and rejected

    return False, None


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ANALYSIS & SCORING
# ══════════════════════════════════════════════════════════════════════════════

def analyze(name, ticker):
    print(f"\n  Analyzing {name}...")

    # Fetch data
    df_w = add_indicators(get_data(ticker, "3y",  "1wk"))
    df_d = add_indicators(get_data(ticker, "1y",  "1d"))
    df_h = add_indicators(get_data(ticker, "60d", "4h"))

    if df_w is None or df_d is None or df_h is None:
        print(f"  ⚠️  Not enough data")
        return None

    # Latest values
    w = df_w.iloc[-1]
    d = df_d.iloc[-1]
    h = df_h.iloc[-1]

    price     = float(d["Close"])
    w_close   = float(w["Close"])
    w_ema50   = float(w["EMA50"])
    w_ema200  = float(w["EMA200"])
    d_ema50   = float(d["EMA50"])
    d_ema200  = float(d["EMA200"])
    d_atr     = float(d["ATR"])
    h_rsi     = float(h["RSI"])
    h_macd    = float(h["MACD"])
    h_signal  = float(h["MACD_Signal"])
    h_hist    = float(h["MACD_Hist"])
    h_ema50   = float(h["EMA50"])
    h_ema200  = float(h["EMA200"])
    h_close   = float(h["Close"])

    score  = 0
    notes  = []
    direction = None

    # ──────────────────────────────────────────────────────────────
    #  1. WEEKLY TREND — 25 pts
    #     The foundation. Everything must agree with weekly.
    # ──────────────────────────────────────────────────────────────
    if w_close > w_ema200 and w_ema50 > w_ema200:
        score += 25
        notes.append("Weekly Bull (EMA50>EMA200) +25")
        direction = "LONG"
    elif w_close < w_ema200 and w_ema50 < w_ema200:
        score += 25
        notes.append("Weekly Bear (EMA50<EMA200) +25")
        direction = "SHORT"
    elif w_close > w_ema200:
        score += 12
        notes.append("Weekly mild Bull +12")
        direction = "LONG"
    elif w_close < w_ema200:
        score += 12
        notes.append("Weekly mild Bear +12")
        direction = "SHORT"

    if direction is None:
        return None

    # ──────────────────────────────────────────────────────────────
    #  2. BOS ON DAILY — 20 pts
    #     Structure is broken → trend confirmed
    # ──────────────────────────────────────────────────────────────
    bos_found, bos_level = check_bos(df_d, direction, lookback=40)
    if bos_found:
        score += 20
        notes.append(f"Daily BOS at {bos_level:,.2f} +20")
    else:
        notes.append("No Daily BOS ❌")

    # ──────────────────────────────────────────────────────────────
    #  3. CHoCH ON DAILY — BONUS +10 pts
    #     Even stronger than BOS — full structure flip
    # ──────────────────────────────────────────────────────────────
    choch_found = check_choch(df_d, direction, lookback=30)
    if choch_found:
        score += 10
        notes.append("Daily CHoCH confirmed (structure flip) +10")

    # ──────────────────────────────────────────────────────────────
    #  4. FAIR VALUE GAP — 20 pts
    #     Price has an unfilled FVG = magnet for price
    # ──────────────────────────────────────────────────────────────
    fvgs = find_fvg(df_d, direction, lookback=40)
    if fvgs:
        best_fvg = fvgs[-1]  # most recent FVG
        fvg_mid  = (best_fvg[0] + best_fvg[1]) / 2
        score   += 20
        notes.append(f"Daily FVG found {best_fvg[1]:,.2f}–{best_fvg[0]:,.2f} +20")
    else:
        best_fvg = None
        notes.append("No Daily FVG ❌")

    # ──────────────────────────────────────────────────────────────
    #  5. LIQUIDITY SWEEP — 15 pts
    #     Smart money swept stops before reversing = high confidence
    # ──────────────────────────────────────────────────────────────
    sweep_found, sweep_level = check_liquidity_sweep(df_d, direction, lookback=25)
    if sweep_found:
        score += 15
        notes.append(f"Liquidity Sweep at {sweep_level:,.2f} +15")
    else:
        notes.append("No Liquidity Sweep ❌")

    # ──────────────────────────────────────────────────────────────
    #  6. H4 RSI — 7 pts
    # ──────────────────────────────────────────────────────────────
    if direction == "LONG":
        if 35 <= h_rsi <= 60:
            score += 7
            notes.append(f"H4 RSI {h_rsi:.0f} (bullish zone) +7")
        elif h_rsi < 35:
            score += 7
            notes.append(f"H4 RSI {h_rsi:.0f} (oversold = good long) +7")
    else:
        if 40 <= h_rsi <= 65:
            score += 7
            notes.append(f"H4 RSI {h_rsi:.0f} (bearish zone) +7")
        elif h_rsi > 65:
            score += 7
            notes.append(f"H4 RSI {h_rsi:.0f} (overbought = good short) +7")

    # ──────────────────────────────────────────────────────────────
    #  7. H4 MACD — 7 pts
    # ──────────────────────────────────────────────────────────────
    macd_bull = h_macd > h_signal and h_hist > 0
    macd_bear = h_macd < h_signal and h_hist < 0

    if direction == "LONG" and (macd_bull or h_macd > h_signal):
        score += 7
        notes.append("H4 MACD bullish +7")
    elif direction == "SHORT" and (macd_bear or h_macd < h_signal):
        score += 7
        notes.append("H4 MACD bearish +7")

    # ──────────────────────────────────────────────────────────────
    #  8. H4 EMA ALIGNMENT — 6 pts
    # ──────────────────────────────────────────────────────────────
    if direction == "LONG" and h_close > h_ema50:
        score += 6
        notes.append("H4 price > EMA50 +6")
    elif direction == "SHORT" and h_close < h_ema50:
        score += 6
        notes.append("H4 price < EMA50 +6")

    # ──────────────────────────────────────────────────────────────
    #  ENTRY PLAN
    # ──────────────────────────────────────────────────────────────
    # Best entry = FVG mid if available, else EMA50 Daily
    if best_fvg:
        entry = round((best_fvg[0] + best_fvg[1]) / 2, 2)
    else:
        entry = round(d_ema50, 2)

    atr2x = d_atr * 2.0
    if direction == "LONG":
        stop = round(entry - atr2x, 2)
        tp1  = round(entry + atr2x * 2, 2)   # 1:2
        tp2  = round(entry + atr2x * 4, 2)   # 1:4
    else:
        stop = round(entry + atr2x, 2)
        tp1  = round(entry - atr2x * 2, 2)
        tp2  = round(entry - atr2x * 4, 2)

    risk_pts = abs(entry - stop)
    risk_usd = ACCOUNT_SIZE * RISK_PCT
    lot_size = round(risk_usd / risk_pts, 2) if risk_pts > 0 else 0.01

    level = "✅ ENTER" if score >= ENTER_SCORE else "❌ SKIP"

    return {
        "name":       name,
        "direction":  direction,
        "score":      score,
        "level":      level,
        "price":      price,
        "entry":      entry,
        "stop":       stop,
        "tp1":        tp1,
        "tp2":        tp2,
        "risk_pts":   round(risk_pts, 2),
        "lot_size":   lot_size,
        "rsi":        round(h_rsi, 1),
        "fvg":        best_fvg,
        "bos":        bos_level,
        "sweep":      sweep_level,
        "choch":      choch_found,
        "notes":      notes,
        "atr":        round(d_atr, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def format_message(s):
    arrow = "📈" if s["direction"] == "LONG" else "📉"
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build WHY section
    why_parts = []
    for note in s["notes"]:
        if "+25" in note or "+12" in note: why_parts.append("Weekly Trend ✅")
        if "BOS"     in note and "+" in note: why_parts.append("BOS ✅")
        if "CHoCH"   in note and "+" in note: why_parts.append("CHoCH ✅")
        if "FVG"     in note and "+" in note: why_parts.append("FVG ✅")
        if "Sweep"   in note and "+" in note: why_parts.append("Liq. Sweep ✅")
        if "RSI"     in note and "+" in note: why_parts.append(f"RSI {s['rsi']} ✅")
        if "MACD"    in note and "+" in note: why_parts.append("MACD ✅")
        if "EMA50"   in note and "+" in note: why_parts.append("EMA ✅")

    why = "  |  ".join(dict.fromkeys(why_parts))

    # FVG zone line
    fvg_line = ""
    if s["fvg"]:
        fvg_line = f"\n🟩 FVG Zone:    {s['fvg'][1]:,.2f} – {s['fvg'][0]:,.2f}"

    msg = f"""
{s['level']} {arrow} {s['direction']} {s['name']} — {s['score']}/100
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 Price Now:  {s['price']:,.2f}
🎯 Entry:      {s['entry']:,.2f}{fvg_line}
🛑 Stop Loss:  {s['stop']:,.2f}  ({s['risk_pts']} pts / 2x ATR)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 TP1:        {s['tp1']:,.2f}  (1:2 R:R)
🏆 TP2:        {s['tp2']:,.2f}  (1:4 R:R)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Why: {why}
💼 Lot: {s['lot_size']} lots  (1% risk)
📅 Weekly → Daily → H4
⏰ {now}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Survival > Capital > Growth
""".strip()
    return msg


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM SENDER
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(message):
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" \
            or not CHAT_ID or CHAT_ID == "YOUR_TELEGRAM_CHAT_ID":
        print("  ⚠️  Telegram not configured (set TELEGRAM_BOT_TOKEN / "
              "TELEGRAM_CHAT_ID) — skipping send")
        return
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code == 200:
            print("  ✅ Telegram sent")
        else:
            print(f"  ⚠️  Telegram error: {r.text}")
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  CSV LOG
# ══════════════════════════════════════════════════════════════════════════════

def log_signal(s):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "datetime","symbol","direction","score",
            "entry","stop","tp1","tp2","rsi","lot_size",
            "bos","choch","fvg","sweep","result"
        ])
        if not exists:
            w.writeheader()
        w.writerow({
            "datetime":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "symbol":    s["name"],
            "direction": s["direction"],
            "score":     s["score"],
            "entry":     s["entry"],
            "stop":      s["stop"],
            "tp1":       s["tp1"],
            "tp2":       s["tp2"],
            "rsi":       s["rsi"],
            "lot_size":  s["lot_size"],
            "bos":       "YES" if s["bos"] else "NO",
            "choch":     "YES" if s["choch"] else "NO",
            "fvg":       "YES" if s["fvg"] else "NO",
            "sweep":     "YES" if s["sweep"] else "NO",
            "result":    "PENDING",
        })
    print(f"  📝 Logged to {LOG_FILE}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SCAN
# ══════════════════════════════════════════════════════════════════════════════

def run_scan():
    print(f"\n{'='*65}")
    print(f"  🔍 SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*65}")

    for name, ticker in SYMBOLS.items():
        result = analyze(name, ticker)

        if result is None:
            print(f"  ❌ {name}: skipped")
            continue

        print(f"  {name}: {result['score']}/100 → {result['level']}")

        if result["score"] >= ENTER_SCORE:
            msg = format_message(result)
            print(f"\n{msg}\n")
            send_telegram(msg)
            log_signal(result)
        else:
            print(f"  ⏭️  Score {result['score']} < 70, no alert")

    print(f"\n  ✅ Done. Next scan in 4 hours.\n")


# ══════════════════════════════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════╗
║   LONG-TERM SMART MONEY BOT — US30 / US100 / US500      ║
║                                                          ║
║   Checks:  Weekly Trend + BOS + CHoCH + FVG + Sweep     ║
║   Score:   70+  →  ✅ Alert sent                         ║
║            <70  →  ❌ Silence                            ║
║                                                          ║
║   Quality over quantity. 1 perfect trade > 10 average   ║
╚══════════════════════════════════════════════════════════╝
""")
    run_scan()
    schedule.every(4).hours.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(60)

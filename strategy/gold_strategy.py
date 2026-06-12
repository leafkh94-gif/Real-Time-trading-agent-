"""
Alert strategy — price-confirmation + multi-factor confirmation.

Three-gate pipeline:
  Gate 1 — Price confirms direction
            BUY:  close breaks above resistance OR strong bounce from support
            SELL: close breaks below support OR rejection from resistance
  Gate 2 — ≥2 of 3 confirmation conditions
            A. Indicators align  (RSI + MACD + MAs on H1)
            B. Multi-timeframe   (D1 EMA20 vs EMA50 trend + H1 confirms)
            C. Active session    (London 03:00–12:00 ET or NY 09:30–16:00 ET)
  Gate 3 — R:R ≥ 1.5  (TP = 2.5×ATR, SL = 1.5×ATR → 1:1.67)

Returns a Signal or None. Never touches the broker.
"""
import logging
import math
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import Signal
from strategy.base import MultiTimeframeCandles, StrategyBase, TF_D1, TF_H1
from strategy.indicators import atr, ema, macd, rsi, swing_highs, swing_lows

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_LONDON_OPEN  = time(3,  0)
_LONDON_CLOSE = time(12, 0)
_NY_OPEN      = time(9, 30)
_NY_CLOSE     = time(16, 0)

_MIN_H1     = 60       # minimum H1 candles required
_MIN_D1     = 50       # minimum D1 candles for MTF condition
_SR_WINDOW  = 60       # H1 bars scanned for swing support/resistance
_WICK_RATIO = 0.4      # wick must be ≥40% of candle range for bounce/rejection
_TP_MULT    = 2.5
_SL_MULT    = 1.5
_MIN_RR     = 1.5


def _in_active_session() -> bool:
    t = datetime.now(tz=_ET).time()
    return (_LONDON_OPEN <= t < _LONDON_CLOSE) or (_NY_OPEN <= t < _NY_CLOSE)


class GoldStrategy(StrategyBase):
    def __init__(self, lots: float = 0.05):
        self.lots = lots

    def evaluate(self, candles: MultiTimeframeCandles) -> Optional[Signal]:
        h1 = candles.get(TF_H1, [])
        d1 = candles.get(TF_D1, [])

        if len(h1) < _MIN_H1:
            logger.info("gate1 SKIP: not enough H1 candles (%d < %d)", len(h1), _MIN_H1)
            return None

        closes_h1 = [c.close for c in h1]

        rsi_vals = rsi(closes_h1, 14)
        macd_line, signal_line, _ = macd(closes_h1)
        ema20_h1 = ema(closes_h1, 20)
        ema50_h1 = ema(closes_h1, 50)
        atr_vals = atr(h1, 14)

        cur_rsi   = rsi_vals[-1]
        prev_rsi  = rsi_vals[-2] if len(rsi_vals) >= 2 else cur_rsi
        cur_macd  = macd_line[-1]
        cur_sig   = signal_line[-1]
        cur_ema20 = ema20_h1[-1]
        cur_ema50 = ema50_h1[-1]
        cur_atr   = atr_vals[-1]
        cur_close = h1[-1].close

        if any(math.isnan(v) for v in [cur_rsi, cur_macd, cur_sig, cur_ema20, cur_ema50, cur_atr]):
            logger.debug("gate1 SKIP: indicator data not ready")
            return None

        # ── Gate 1: price confirms direction ──────────────────────────────────
        direction = self._price_confirmation(h1)
        if direction is None:
            logger.info("gate1 SKIP: no price confirmation (breakout/bounce)")
            return None

        # ── Gate 2: ≥2 of 3 confirmation conditions ───────────────────────────
        conds = 0

        # Condition A — indicators align
        if direction == "buy":
            if cur_rsi > 50 and cur_rsi > prev_rsi and cur_macd > cur_sig \
                    and cur_close > cur_ema20 and cur_close > cur_ema50:
                conds += 1
                logger.debug("gate2: A (indicators bullish) ✓")
        else:
            if cur_rsi < 50 and cur_rsi < prev_rsi and cur_macd < cur_sig \
                    and cur_close < cur_ema20 and cur_close < cur_ema50:
                conds += 1
                logger.debug("gate2: A (indicators bearish) ✓")

        # Condition B — multi-timeframe alignment
        if len(d1) >= _MIN_D1:
            closes_d1 = [c.close for c in d1]
            d1_ema20 = ema(closes_d1, 20)
            d1_ema50 = ema(closes_d1, 50)
            de20, de50 = d1_ema20[-1], d1_ema50[-1]
            if not (math.isnan(de20) or math.isnan(de50)):
                if direction == "buy" and de20 > de50 \
                        and (cur_close > cur_ema20 or cur_rsi > 50):
                    conds += 1
                    logger.debug("gate2: B (MTF bullish) ✓")
                elif direction == "sell" and de20 < de50 \
                        and (cur_close < cur_ema20 or cur_rsi < 50):
                    conds += 1
                    logger.debug("gate2: B (MTF bearish) ✓")

        # Condition C — active trading session
        if _in_active_session():
            conds += 1
            logger.debug("gate2: C (active session) ✓")

        if conds < 2:
            logger.info("gate2 SKIP: only %d/3 conditions met (need ≥2)", conds)
            return None

        # ── Gate 3: R:R ≥ 1.5 ────────────────────────────────────────────────
        rr = _TP_MULT / _SL_MULT  # 2.5 / 1.5 = 1.67
        if rr < _MIN_RR:
            logger.info("gate3 SKIP: R:R %.2f < %.1f", rr, _MIN_RR)
            return None

        logger.info(
            "SIGNAL %s  rsi=%.1f  macd=%.3f/%.3f  conds=%d/3  rr=1:%.2f",
            direction.upper(), cur_rsi, cur_macd, cur_sig, conds, rr,
        )
        return Signal(direction=direction, lots=self.lots)

    def _price_confirmation(self, h1: list) -> str | None:
        """Return 'buy', 'sell', or None based on the latest bar's price action."""
        if len(h1) < 3:
            return None

        bar  = h1[-1]
        prev = h1[-2]

        window      = h1[-(_SR_WINDOW + 10):]
        sh          = swing_highs(window, lookback=5)
        sl_         = swing_lows( window, lookback=5)
        resistances = [v for v in sh  if v is not None]
        supports    = [v for v in sl_ if v is not None]

        bar_range  = bar.high - bar.low
        lower_wick = min(bar.close, bar.open) - bar.low
        upper_wick = bar.high - max(bar.close, bar.open)

        # BUY — close breaks above a resistance level
        buy_breakout = any(prev.close <= r <= bar.close for r in resistances)

        # BUY — strong bounce from support (wick dipped below, closed back above)
        buy_bounce = (
            bar_range > 0
            and lower_wick >= _WICK_RATIO * bar_range
            and any(bar.low < s < bar.close for s in supports)
        )

        # SELL — close breaks below a support level
        sell_breakdown = any(bar.close <= s <= prev.close for s in supports)

        # SELL — rejection from resistance (wick punched above, closed back below)
        sell_rejection = (
            bar_range > 0
            and upper_wick >= _WICK_RATIO * bar_range
            and any(bar.close < r < bar.high for r in resistances)
        )

        if buy_breakout or buy_bounce:
            return "buy"
        if sell_breakdown or sell_rejection:
            return "sell"
        return None

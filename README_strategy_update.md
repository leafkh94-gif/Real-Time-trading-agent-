# Strategy update — Long-Term Smart Money Bot (US30 / US100 / US500)

The active strategy is now **`longterm_bot.py`**, a self-contained long-term
SMC/ICT bot. It replaces the previous Capital.com scoring engine
(`main_alerts.py` + `strategy/scoring_strategy.py`), which is no longer wired
into any entrypoint.

## What it does
- **Data:** Yahoo Finance (`yfinance`) — no broker login required.
- **Instruments:** US30 (`^DJI`), US100 (`^NDX`), US500 (`^GSPC`).
- **Timeframes:** Weekly → Daily → H4 top-down.
- **Indicators:** EMA50/EMA200, RSI, MACD, ATR + SMC (BOS, CHoCH, FVG,
  Liquidity Sweep).
- **Scoring:** 0–100. Sends a Telegram alert only when score ≥ `ENTER_SCORE`
  (default 70). Alert only — no execution.
- **Cadence:** runs one scan on start, then every 4 hours.
- **Logging:** appends every alert to `signals_log.csv`.

## Run it
```bash
pip install -r requirements-alerts.txt
python longterm_bot.py
```

## Configuration (environment variables)
Credentials and tunables are read from the environment (or a `.env` file via
`python-dotenv`) so nothing sensitive is committed:

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token (required to send alerts) |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID (required to send alerts) |
| `ACCOUNT_SIZE` | `1000` | Account size in USD (position sizing) |
| `RISK_PCT` | `0.01` | Risk per trade (1%) |
| `ENTER_SCORE` | `70` | Minimum score to send an alert |
| `LOG_FILE` | `signals_log.csv` | CSV log path |

If Telegram credentials are not set, the bot still scans and prints results but
skips sending.

## Deployment
- **GitHub Actions** (`.github/workflows/alert_bot.yml`) runs `longterm_bot.py`
  continuously and self-chains across the runner timeout. Set `TELEGRAM_BOT_TOKEN`
  and `TELEGRAM_CHAT_ID` as repo secrets. `signals_log.csv` is cached between runs.
- **Render** (`render.yaml` / `Procfile`) runs `longterm_bot.py` as a web service.

## Honest caveats
- The SMC pattern detectors (BOS/CHoCH/FVG/sweep) are documented **heuristics**,
  not a proven edge. Backtest before trusting live alerts.
- `yfinance` 4h/weekly history can be sparse or rate-limited; an instrument is
  skipped when there isn't enough data.
- ATR multipliers (2× stop, 2×/4× targets) and the scoring weights are starting
  points — tune them to your own results.

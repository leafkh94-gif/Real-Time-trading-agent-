"""
test_telegram.py — sends a test message to confirm Telegram is connected.
Usage: python test_telegram.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

if not token or not chat_id:
    print("FAILED: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from .env")
    raise SystemExit(1)

host = "api.telegram.org"
url  = f"https://{host}/bot{token}/sendMessage"

r = requests.post(url, json={
    "chat_id":    chat_id,
    "text":       "✅ Gold Alert Bot — Telegram is connected and working!",
    "parse_mode": "HTML",
}, timeout=10)

if r.status_code == 200:
    print("SUCCESS — check your Telegram now.")
else:
    print(f"FAILED (HTTP {r.status_code}): {r.text}")

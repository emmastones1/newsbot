"""
Configuration for the Forex Factory News Bot (scheduled side).
Fill these in before running forex_news_bot.py.
"""

import os

# --- Telegram ---
BOT_TOKEN = os.environ.get("FF_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.environ.get("FF_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

# --- Timing ---
LOCAL_TZ = os.environ.get("FF_LOCAL_TZ", "UTC")
DAILY_DIGEST_TIME = os.environ.get("FF_DIGEST_TIME", "06:00")
ALERT_MINUTES_BEFORE = int(os.environ.get("FF_ALERT_MINUTES", "30"))

# Set to "true" to bypass all time/weekday checks and send everything
# immediately — useful for manually testing a deployment.
FORCE_SEND = os.environ.get("FF_FORCE_SEND", "false")

# --- Cloudflare KV (shared user data — written by the Worker, read here) ---
# Found in your Cloudflare dashboard: account_id in the sidebar, namespace_id
# when you create the KV namespace, api_token from My Profile -> API Tokens
# (needs "Workers KV Storage: Read" permission for that account).
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

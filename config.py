"""
Configuration for the Forex Factory News Bot.
Fill these in before running forex_news_bot.py.
"""

import os

# --- Telegram ---
# Get a token from @BotFather on Telegram.
# Get your chat_id by messaging your bot, then visiting:
# https://api.telegram.org/bot<TOKEN>/getUpdates
BOT_TOKEN = os.environ.get("FF_BOT_TOKEN", "8900372924:AAE5ZstFggoUk90W6ly6AG2rAGmkXE4e5uE")
CHAT_ID = os.environ.get("FF_CHAT_ID", "6444118431")

# --- Timing ---
# Your local timezone (IANA name), used for the daily digest schedule.
LOCAL_TZ = os.environ.get("FF_LOCAL_TZ", "UTC-1")

# Time (24h HH:MM, in LOCAL_TZ) the daily digest is sent.
DAILY_DIGEST_TIME = os.environ.get("FF_DIGEST_TIME", "06:00")

# How many minutes before each session opens to send the heads-up.
ALERT_MINUTES_BEFORE = int(os.environ.get("FF_ALERT_MINUTES", "30"))

# Forex Factory News Bot

Sends a daily Telegram digest of **high-impact** ForexFactory news, plus a
heads-up alert before each trading session (Asian, London, New York) listing
the high-impact events that fall inside that session's window.

Designed to run as a **Render Cron Job**: Render wakes the script up every
15 minutes, and the script itself decides whether it's actually time to send
anything (daily digest time, or one of the three session alert times). This
avoids paying for an always-on worker.

## 1. Create your Telegram bot

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you.
3. Message your new bot anything (so it can see your chat), then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find `"chat":{"id": ...}` in the response — that's your **chat_id**.

## 2. Deploy to Render

1. Push this folder to a GitHub repo.
2. In Render: **New → Blueprint**, point it at the repo. Render will read
   `render.yaml` and create a **Cron Job** service automatically.
   (No Blueprint? Create it manually: **New → Cron Job**, runtime Python,
   build command `pip install -r requirements.txt`, start command
   `python3 forex_news_bot.py`, schedule `*/15 * * * *`.)
3. In the service's **Environment** tab, set:
   - `FF_BOT_TOKEN` — your bot token
   - `FF_CHAT_ID` — your chat id
   - `FF_LOCAL_TZ` — your IANA timezone, e.g. `Africa/Lagos` (used for the daily digest time only)
   - `FF_DIGEST_TIME` — e.g. `06:00`
   - `FF_ALERT_MINUTES` — how many minutes before each session open to alert, e.g. `30`
4. Deploy. Render will invoke the script every 15 minutes from then on.

Render's own cron schedule field (`*/15 * * * *`) is always UTC — that's just
how often the script *wakes up to check*, not the actual send times. The
actual digest/session times are computed inside the script using real
timezones (`Africa/Lagos`, `Europe/London`, `America/New_York`, etc.), so
daylight saving time is handled automatically without touching the Render
schedule.

> Double-check Render's current cron job pricing/behavior in their dashboard
> before deploying — I can't verify live pricing here.

## 3. Run locally instead (optional)

You can still run it on your own machine as a repeating check instead of
Render:

```bash
pip install -r requirements.txt
export FF_BOT_TOKEN="..." FF_CHAT_ID="..." FF_LOCAL_TZ="Africa/Lagos"
watch -n 900 python3 forex_news_bot.py   # re-run every 15 min
```

Or just call `python3 forex_news_bot.py` from any external scheduler
(cron, Task Scheduler, another cloud cron service) every 10-15 minutes.

## Notes

- News data comes from ForexFactory's public calendar JSON feed
  (`nfs.faireconomy.media/ff_calendar_thisweek.json`). This is an unofficial,
  widely-used feed — no API key needed, but it isn't officially documented by
  ForexFactory and could change format or break without notice.
- "High impact" = ForexFactory's own red-folder classification.
- If no high-impact news falls in a given day/session, the bot still sends a
  short "nothing scheduled" message so you know it's alive and working.
- `TOLERANCE_MINUTES` in `forex_news_bot.py` (default 7) controls how close
  "now" must be to a target time to fire. Keep it under half your cron
  interval to avoid duplicate sends.

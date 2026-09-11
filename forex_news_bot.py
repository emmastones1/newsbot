"""
Forex Factory News Bot
-----------------------
Sends a daily Telegram digest of HIGH-impact ForexFactory news,
plus a pre-session heads-up before the Asian, London, and New York
sessions listing the high-impact events falling inside that session.

Designed to run as a Render Cron Job (or any scheduler that invokes
this script periodically, e.g. every 15 minutes). Each run is
stateless: it checks whether "now" falls within a small tolerance
window of any scheduled send time, and fires only those that match.
Because each target time is computed fresh from real timezones via
zoneinfo, DST is handled automatically — no manual UTC-offset upkeep.
"""

import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ff_bot")

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Session definitions: name -> (IANA timezone of the financial center, local open hour, local close hour)
SESSIONS = {
    "Asian": {"tz": "Asia/Tokyo", "open_hour": 9, "close_hour": 18},
    "London": {"tz": "Europe/London", "open_hour": 8, "close_hour": 17},
    "New York": {"tz": "America/New_York", "open_hour": 8, "close_hour": 17},
}


CHECK_BUTTON_LABEL = "🔍 Check News Now"
# Any of these (case-insensitive) trigger an on-demand check.
COMMAND_TRIGGERS = {CHECK_BUTTON_LABEL.lower(), "/check", "/news"}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str, with_keyboard: bool = True) -> None:
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if with_keyboard:
        # Attaches a persistent tappable button to the chat. Telegram keeps
        # showing it until a message explicitly changes/removes it, so
        # sending it here on every message is enough to keep it available.
        payload["reply_markup"] = {
            "keyboard": [[CHECK_BUTTON_LABEL]],
            "resize_keyboard": True,
            "is_persistent": True,
        }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send Telegram message: %s", e)


def poll_and_handle_commands() -> None:
    """
    Check for any '/check' style command sent since the last run, and
    respond immediately with an on-demand news scan if found.

    Uses Telegram's own update offset to track what's been read — no local
    state file needed, which matters since each run is a fresh, stateless
    container (GitHub Actions / Render Cron).
    """
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"timeout": 0}, timeout=15)
        r.raise_for_status()
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        log.error("Failed to poll Telegram for commands: %s", e)
        return

    if not updates:
        return

    triggered = False
    for u in updates:
        msg = u.get("message", {}) or {}
        text = str(msg.get("text", "")).strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id == str(config.CHAT_ID) and text in COMMAND_TRIGGERS:
            triggered = True

    if triggered:
        log.info("On-demand check command received — running scan now.")
        handle_on_demand_check()

    # Acknowledge everything up to the latest update_id so next run doesn't
    # reprocess these same messages.
    max_update_id = max(u["update_id"] for u in updates)
    try:
        requests.get(url, params={"offset": max_update_id + 1, "timeout": 0}, timeout=15)
    except requests.RequestException as e:
        log.error("Failed to acknowledge Telegram updates: %s", e)


# --------------------------------------------------------------------------
# ForexFactory calendar
# --------------------------------------------------------------------------
def fetch_calendar() -> list[dict]:
    """Fetch this week's ForexFactory calendar as a list of event dicts."""
    try:
        r = requests.get(FF_CALENDAR_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.error("Failed to fetch ForexFactory calendar: %s", e)
        return []
    except ValueError as e:
        log.error("Failed to parse ForexFactory calendar JSON: %s", e)
        return []


def parse_event_time(event: dict) -> datetime | None:
    """ForexFactory feed gives an ISO 8601 datetime string with offset in 'date'."""
    raw = event.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def is_high_impact(event: dict) -> bool:
    return str(event.get("impact", "")).strip().lower() == "high"


def get_high_impact_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        if is_high_impact(e):
            dt = parse_event_time(e)
            if dt:
                out.append({**e, "_dt": dt})
    out.sort(key=lambda e: e["_dt"])
    return out


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def format_event_line(event: dict, tz: ZoneInfo) -> str:
    local_dt = event["_dt"].astimezone(tz)
    time_str = local_dt.strftime("%H:%M")
    currency = event.get("country", "")
    title = event.get("title", "")
    forecast = event.get("forecast", "") or "-"
    previous = event.get("previous", "") or "-"
    return f"🔴 {time_str} | <b>{currency}</b> — {title}\n     Forecast: {forecast} | Previous: {previous}"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def send_daily_digest():
    log.info("Running daily digest job")
    tz = ZoneInfo(config.LOCAL_TZ)
    today = datetime.now(tz).date()

    events = get_high_impact_events(fetch_calendar())
    todays_events = [e for e in events if e["_dt"].astimezone(tz).date() == today]

    if not todays_events:
        send_telegram_message(f"📅 <b>{today.strftime('%A, %d %b %Y')}</b>\nNo high-impact news scheduled today.")
        return

    lines = [f"📅 <b>High-Impact News — {today.strftime('%A, %d %b %Y')}</b>\n"]
    lines += [format_event_line(e, tz) for e in todays_events]
    send_telegram_message("\n\n".join(lines))


def send_session_alert(session_name: str):
    log.info("Running session alert job: %s", session_name)
    session = SESSIONS[session_name]
    tz = ZoneInfo(session["tz"])
    now_local = datetime.now(tz)

    session_open = now_local.replace(hour=session["open_hour"], minute=0, second=0, microsecond=0)
    session_close = now_local.replace(hour=session["close_hour"], minute=0, second=0, microsecond=0)

    events = get_high_impact_events(fetch_calendar())
    session_events = [
        e for e in events
        if session_open <= e["_dt"].astimezone(tz) <= session_close
    ]

    if not session_events:
        send_telegram_message(
            f"🌍 <b>{session_name} session</b> opens in {config.ALERT_MINUTES_BEFORE} min — "
            f"no high-impact news scheduled during this session."
        )
        return

    lines = [
        f"🌍 <b>{session_name} session</b> opens in {config.ALERT_MINUTES_BEFORE} min — "
        f"key news for this session:\n"
    ]
    lines += [format_event_line(e, tz) for e in session_events]
    send_telegram_message("\n\n".join(lines))


def handle_on_demand_check():
    log.info("Running on-demand check")
    tz = ZoneInfo(config.LOCAL_TZ)
    today = datetime.now(tz).date()

    events = get_high_impact_events(fetch_calendar())
    todays_events = [e for e in events if e["_dt"].astimezone(tz).date() == today]

    if not todays_events:
        send_telegram_message("🔍 Checked now — no high-impact news scheduled for today.")
        return

    lines = [f"🔍 <b>On-demand check — {today.strftime('%A, %d %b %Y')}</b>\n"]
    lines += [format_event_line(e, tz) for e in todays_events]
    send_telegram_message("\n\n".join(lines))


# --------------------------------------------------------------------------
# Stateless "is it time yet?" checks
# --------------------------------------------------------------------------
# How close "now" must be to a target time to count as a match. Keep this
# comfortably smaller than half your cron interval (default cron: every 15
# min -> tolerance of 6-7 min guarantees exactly one run fires per target
# per day, with no duplicate-send tracking needed).
TOLERANCE_MINUTES = 7


def _within_tolerance(now: datetime, target: datetime) -> bool:
    return abs((now - target).total_seconds()) <= TOLERANCE_MINUTES * 60


def digest_due(now_utc: datetime) -> bool:
    tz = ZoneInfo(config.LOCAL_TZ)
    now_local = now_utc.astimezone(tz)
    hour, minute = map(int, config.DAILY_DIGEST_TIME.split(":"))
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return _within_tolerance(now_local, target_local)


def session_alert_due(session_name: str, now_utc: datetime) -> bool:
    session = SESSIONS[session_name]
    tz = ZoneInfo(session["tz"])
    now_local = now_utc.astimezone(tz)
    target_local = now_local.replace(
        hour=session["open_hour"], minute=0, second=0, microsecond=0
    ) - timedelta(minutes=config.ALERT_MINUTES_BEFORE)
    return _within_tolerance(now_local, target_local)


# --------------------------------------------------------------------------
# Entry point — run once per invocation (called by Render Cron Job)
# --------------------------------------------------------------------------
def main():
    now_utc = datetime.now(ZoneInfo("UTC"))
    log.info("Run check at %s UTC", now_utc.isoformat())

    poll_and_handle_commands()

    force_send = str(config.FORCE_SEND).strip().lower() in ("1", "true", "yes")
    if force_send:
        log.info("FF_FORCE_SEND is set — sending digest and all session alerts now, ignoring time checks.")
        send_daily_digest()
        for session_name in SESSIONS:
            send_session_alert(session_name)
        return

    if digest_due(now_utc):
        send_daily_digest()

    for session_name in SESSIONS:
        if session_alert_due(session_name, now_utc):
            send_session_alert(session_name)


if __name__ == "__main__":
    main()

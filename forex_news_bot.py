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
START_TRIGGERS = {"/start"}

WELCOME_TEXT = (
    "👋 <b>Welcome to FX Pulse.</b>\n\n"
    "I track high-impact ForexFactory news and ping you about it. "
    "Tap the button below anytime to check what's on today, right now."
)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str, chat_id=None, with_keyboard: bool = True) -> None:
    """Send a message. Defaults to the owner's chat (for scheduled digests/
    alerts); pass chat_id explicitly to reply to whoever messaged the bot."""
    target_chat = chat_id if chat_id is not None else config.CHAT_ID
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
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
        log.error("Failed to send Telegram message to %s: %s", target_chat, e)


def poll_and_handle_commands() -> None:
    """
    Check for any new messages sent to the bot since the last run — from
    ANYONE, not just the owner — and respond immediately:
      - '/start' (first time a person opens the bot) -> welcome + button
      - '/check', '/news', or the button tap -> on-demand news scan

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

    for u in updates:
        msg = u.get("message", {}) or {}
        text = str(msg.get("text", "")).strip().lower()
        text = text.split("@")[0]  # strip a possible "@yourbotname" suffix Telegram may append to commands
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            continue

        if text in START_TRIGGERS:
            log.info("New /start from chat %s — sending welcome.", chat_id)
            send_telegram_message(WELCOME_TEXT, chat_id=chat_id)
        elif text in COMMAND_TRIGGERS:
            log.info("On-demand check requested by chat %s.", chat_id)
            handle_on_demand_check(chat_id=chat_id)

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


def get_session_status(session_name: str, now_utc: datetime) -> dict:
    """
    Determine a session's REAL current state relative to now — not an
    assumption. Returns:
      status: "upcoming" | "active" | "passed"
      minutes: minutes until open (upcoming), since close (passed), or None (active)
      session_open / session_close: today's localized open/close datetimes
      tz: the session's ZoneInfo
    """
    session = SESSIONS[session_name]
    tz = ZoneInfo(session["tz"])
    now_local = now_utc.astimezone(tz)
    session_open = now_local.replace(hour=session["open_hour"], minute=0, second=0, microsecond=0)
    session_close = now_local.replace(hour=session["close_hour"], minute=0, second=0, microsecond=0)

    if now_local < session_open:
        status, minutes = "upcoming", int((session_open - now_local).total_seconds() // 60)
    elif now_local > session_close:
        status, minutes = "passed", int((now_local - session_close).total_seconds() // 60)
    else:
        status, minutes = "active", None

    return {"status": status, "minutes": minutes, "session_open": session_open,
            "session_close": session_close, "tz": tz}


def send_session_alert(session_name: str):
    log.info("Running session alert job: %s", session_name)
    now_utc = datetime.now(ZoneInfo("UTC"))
    info = get_session_status(session_name, now_utc)
    tz = info["tz"]

    events = get_high_impact_events(fetch_calendar())
    session_events = [
        e for e in events
        if info["session_open"] <= e["_dt"].astimezone(tz) <= info["session_close"]
    ]

    if info["status"] == "upcoming":
        header = f"🌍 <b>{session_name} session</b> opens in {info['minutes']} min"
    elif info["status"] == "active":
        header = f"🌍 <b>{session_name} session</b> is currently open"
    else:
        header = f"🌍 <b>{session_name} session</b> has already closed for today"

    if not session_events:
        send_telegram_message(f"{header} — no high-impact news scheduled for this session.")
        return

    lines = [f"{header} — key news for this session:\n"]
    lines += [format_event_line(e, tz) for e in session_events]
    send_telegram_message("\n\n".join(lines))


def handle_on_demand_check(chat_id=None):
    log.info("Running on-demand check")
    tz = ZoneInfo(config.LOCAL_TZ)
    now_utc = datetime.now(ZoneInfo("UTC"))
    today = now_utc.astimezone(tz).date()

    events = get_high_impact_events(fetch_calendar())
    todays_events = [e for e in events if e["_dt"].astimezone(tz).date() == today]

    active_session = next(
        (name for name in SESSIONS if get_session_status(name, now_utc)["status"] == "active"),
        None,
    )
    session_line = (
        f"🕒 Currently in the <b>{active_session}</b> session.\n"
        if active_session else "🕒 No major session currently active.\n"
    )

    if not todays_events:
        send_telegram_message(f"🔍 Checked now — {session_line}No high-impact news scheduled for today.", chat_id=chat_id)
        return

    lines = [f"🔍 <b>On-demand check — {today.strftime('%A, %d %b %Y')}</b>\n{session_line}"]
    for e in todays_events:
        marker = "✅ (already out)" if e["_dt"] < now_utc else "⏳ (upcoming)"
        lines.append(f"{marker}\n{format_event_line(e, tz)}")
    send_telegram_message("\n\n".join(lines), chat_id=chat_id)


# --------------------------------------------------------------------------
# Stateless "is it time yet?" checks
# --------------------------------------------------------------------------
# How close "now" must be to a target time to count as a match. Keep this
# comfortably smaller than half your cron interval (default cron: every 10
# min -> tolerance of 4 min guarantees exactly one run fires per target
# per day, with no duplicate-send tracking needed).
TOLERANCE_MINUTES = 4


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

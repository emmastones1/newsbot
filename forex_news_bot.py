"""
Forex Factory News Bot — scheduled side
-----------------------------------------
Sends the daily digest and pre-session alerts (Asian/London/New York),
broadcasting to everyone who has said /start to the bot.

Instant, live interactions (/start, /check, /setrules) are handled
separately by a Cloudflare Worker (see cloudflare-worker/worker.js), which
responds within milliseconds instead of waiting for this script's next
scheduled run. Both sides share the same Cloudflare KV namespace for user
data — this script only READS it (to build the broadcast list + each
person's saved rules); only the Worker writes to it.

Designed to run periodically (every ~10 min) via GitHub Actions.
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

SESSIONS = {
    "Asian": {"tz": "Asia/Tokyo", "open_hour": 9, "close_hour": 18},
    "London": {"tz": "Europe/London", "open_hour": 8, "close_hour": 17},
    "New York": {"tz": "America/New_York", "open_hour": 8, "close_hour": 17},
}

CHECK_BUTTON_LABEL = "🔍 Check News Now"

WEEKEND_REMINDERS = [
    "📖 Weekend check-in: review this week's trades. What worked, what didn't?",
    "🧘 Markets are closed — a good day to step away from the charts and rest.",
    "📚 Study idea: pick one setup from this week and break down why it worked (or didn't).",
    "🚶 Go outside, get some fresh air, come back sharper on Monday.",
    "📏 Weekend reminder: re-read your own trading rules before Monday's open.",
    "🧠 Overtrading usually starts with under-resting. Take today off from the charts.",
]


# --------------------------------------------------------------------------
# Shared user data — read-only here (Cloudflare Worker owns the writes)
# --------------------------------------------------------------------------
def load_users() -> dict:
    """Fetch every registered user + their saved rules from Cloudflare KV."""
    base = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{config.CF_KV_NAMESPACE_ID}"
    )
    headers = {"Authorization": f"Bearer {config.CF_API_TOKEN}"}

    try:
        r = requests.get(f"{base}/keys", headers=headers, timeout=15)
        r.raise_for_status()
        keys = [k["name"] for k in r.json().get("result", [])]
    except (requests.RequestException, ValueError) as e:
        log.error("Failed to list Cloudflare KV keys: %s", e)
        return {}

    users = {}
    for key in keys:
        if not key.startswith("user:"):
            continue
        chat_id = key[len("user:"):]
        try:
            vr = requests.get(f"{base}/values/{key}", headers=headers, timeout=15)
            vr.raise_for_status()
            users[chat_id] = vr.json()
        except (requests.RequestException, ValueError) as e:
            log.error("Failed to read Cloudflare KV key %s: %s", key, e)
    return users


def with_rules(text: str, users: dict, chat_id) -> str:
    rules = users.get(str(chat_id), {}).get("rules", "")
    if rules:
        return f"{text}\n\n📋 <b>Your rules:</b>\n{rules}"
    return text


def get_recipients(users: dict) -> set:
    return set(users.keys()) | {str(config.CHAT_ID)}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str, chat_id=None, with_keyboard: bool = True) -> None:
    target_chat = chat_id if chat_id is not None else config.CHAT_ID
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if with_keyboard:
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


# --------------------------------------------------------------------------
# ForexFactory calendar
# --------------------------------------------------------------------------
def fetch_calendar() -> list[dict]:
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


def get_weekend_reminder_text(today) -> str:
    idx = today.toordinal() % len(WEEKEND_REMINDERS)
    return f"🗓️ <b>{today.strftime('%A, %d %b %Y')}</b>\n{WEEKEND_REMINDERS[idx]}"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def send_daily_digest():
    log.info("Running daily digest job")
    tz = ZoneInfo(config.LOCAL_TZ)
    now_local = datetime.now(tz)
    today = now_local.date()

    users = load_users()
    recipients = get_recipients(users)

    if now_local.weekday() >= 5:  # Saturday=5, Sunday=6
        base_text = get_weekend_reminder_text(today)
        for chat_id in recipients:
            send_telegram_message(with_rules(base_text, users, chat_id), chat_id=chat_id)
        return

    events = get_high_impact_events(fetch_calendar())
    todays_events = [e for e in events if e["_dt"].astimezone(tz).date() == today]

    if not todays_events:
        base_text = f"📅 <b>{today.strftime('%A, %d %b %Y')}</b>\nNo high-impact news scheduled today."
    else:
        lines = [f"📅 <b>High-Impact News — {today.strftime('%A, %d %b %Y')}</b>\n"]
        lines += [format_event_line(e, tz) for e in todays_events]
        base_text = "\n\n".join(lines)

    for chat_id in recipients:
        send_telegram_message(with_rules(base_text, users, chat_id), chat_id=chat_id)


def get_session_status(session_name: str, now_utc: datetime) -> dict:
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
        base_text = f"{header} — no high-impact news scheduled for this session."
    else:
        lines = [f"{header} — key news for this session:\n"]
        lines += [format_event_line(e, tz) for e in session_events]
        base_text = "\n\n".join(lines)

    users = load_users()
    for chat_id in get_recipients(users):
        send_telegram_message(with_rules(base_text, users, chat_id), chat_id=chat_id)


# --------------------------------------------------------------------------
# Stateless "is it time yet?" checks
# --------------------------------------------------------------------------
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
    """
    True only if it's actually time to alert AND the FX market is realistically
    open: no alerts at all on Saturday; only the Asian session on Sunday
    (the real start of the trading week).
    """
    session = SESSIONS[session_name]
    tz = ZoneInfo(session["tz"])
    now_local = now_utc.astimezone(tz)

    weekday = now_local.weekday()  # Monday=0 ... Saturday=5, Sunday=6
    if weekday == 5:
        return False
    if weekday == 6 and session_name != "Asian":
        return False

    target_local = now_local.replace(
        hour=session["open_hour"], minute=0, second=0, microsecond=0
    ) - timedelta(minutes=config.ALERT_MINUTES_BEFORE)
    return _within_tolerance(now_local, target_local)


# --------------------------------------------------------------------------
# Entry point — run once per invocation
# --------------------------------------------------------------------------
def main():
    now_utc = datetime.now(ZoneInfo("UTC"))
    log.info("Run check at %s UTC", now_utc.isoformat())

    force_send = str(config.FORCE_SEND).strip().lower() in ("1", "true", "yes")
    if force_send:
        log.info("FF_FORCE_SEND is set — sending digest and all session alerts now, ignoring time/weekday checks.")
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

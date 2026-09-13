"""
Forex Factory News Bot
-----------------------
Sends a daily Telegram digest of HIGH-impact ForexFactory news, plus a
pre-session heads-up before the Asian, London, and New York sessions.
Also supports:
  - Weekday-awareness: no session alerts fire on Saturday; only the Asian
    session fires on Sunday (the real start of the FX trading week).
  - Weekend reminders in place of the digest on Sat/Sun.
  - Per-user personal trading rules (/setrules, /myrules, /clearrules),
    persisted in data/users.json and appended to that user's messages.
  - Broadcasting the digest/session alerts to everyone who has said
    /start to the bot, not just the original owner.

Designed to run periodically (every ~10 min) via GitHub Actions or any
external scheduler. Each run is stateless except for data/users.json,
which the workflow commits back to the repo when it changes.
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet, InvalidToken

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
COMMAND_TRIGGERS = {"/check", "/news"}
START_TRIGGERS = {"/start"}

WELCOME_TEXT = (
    "👋 <b>Welcome to FX Pulse.</b>\n\n"
    "I track high-impact ForexFactory news — a daily digest, plus a heads-up "
    "before the Asian, London, and New York sessions.\n\n"
    "<b>Commands:</b>\n"
    "🔍 Check News Now — see what's on today, right now\n"
    "/setrules &lt;text&gt; — save your own trading rules; I'll remind you of them\n"
    "/myrules — see your saved rules\n"
    "/clearrules — clear them"
)

WEEKEND_REMINDERS = [
    "📖 Weekend check-in: review this week's trades. What worked, what didn't?",
    "🧘 Markets are closed — a good day to step away from the charts and rest.",
    "📚 Study idea: pick one setup from this week and break down why it worked (or didn't).",
    "🚶 Go outside, get some fresh air, come back sharper on Monday.",
    "📏 Weekend reminder: re-read your own trading rules before Monday's open.",
    "🧠 Overtrading usually starts with under-resting. Take today off from the charts.",
]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.enc")  # encrypted — never stored as plain JSON


# --------------------------------------------------------------------------
# Persistent per-user data (registered users + their saved rules)
# --------------------------------------------------------------------------
def _get_fernet() -> Fernet:
    key = config.DATA_KEY
    if not key:
        raise RuntimeError(
            "FF_DATA_KEY is not set — can't read/write user data. "
            "Generate a key and add it as a GitHub secret named FF_DATA_KEY."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def load_users() -> dict:
    try:
        with open(USERS_FILE, "rb") as f:
            ciphertext = f.read()
    except FileNotFoundError:
        return {}

    if not ciphertext:
        return {}

    try:
        plaintext = _get_fernet().decrypt(ciphertext)
        return json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as e:
        log.error("Could not decrypt/parse user data — starting fresh. (%s)", e)
        return {}


def save_users(users: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    plaintext = json.dumps(users, ensure_ascii=False).encode("utf-8")
    ciphertext = _get_fernet().encrypt(plaintext)
    with open(USERS_FILE, "wb") as f:
        f.write(ciphertext)


def with_rules(text: str, users: dict, chat_id) -> str:
    rules = users.get(str(chat_id), {}).get("rules", "")
    if rules:
        return f"{text}\n\n📋 <b>Your rules:</b>\n{rules}"
    return text


def get_recipients(users: dict) -> set:
    """Everyone who has ever interacted, plus the owner's chat as a permanent fallback."""
    return set(users.keys()) | {str(config.CHAT_ID)}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str, chat_id=None, with_keyboard: bool = True) -> None:
    """Send a message. Defaults to the owner's chat; pass chat_id explicitly
    to target a specific user (on-demand replies, broadcasts, etc.)."""
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


def poll_and_handle_commands() -> None:
    """
    Check for any new messages sent to the bot since the last run — from
    ANYONE, not just the owner — and respond immediately. Uses Telegram's
    own update offset to track what's been read, so no local state file
    is needed for this part (data/users.json is separate, persistent state).
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

    users = load_users()
    changed = False

    for u in updates:
        msg = u.get("message", {}) or {}
        raw_text = str(msg.get("text", "")).strip()
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None or not raw_text:
            continue
        chat_key = str(chat_id)

        parts = raw_text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in START_TRIGGERS:
            if chat_key not in users:
                users[chat_key] = {"rules": ""}
                changed = True
                log.info("New user registered: %s", chat_key)
            send_telegram_message(WELCOME_TEXT, chat_id=chat_id)

        elif cmd in COMMAND_TRIGGERS or raw_text.lower() == CHECK_BUTTON_LABEL.lower():
            handle_on_demand_check(chat_id=chat_id)

        elif cmd == "/setrules":
            if chat_key not in users:
                users[chat_key] = {"rules": ""}
            if arg:
                users[chat_key]["rules"] = arg
                changed = True
                send_telegram_message(
                    "✅ Your trading rules are saved. I'll include them with your checks and alerts.",
                    chat_id=chat_id,
                )
            else:
                send_telegram_message(
                    "Send your rules right after the command, e.g.:\n"
                    "/setrules Never risk more than 1% per trade. No trading the first 5 min after NFP.",
                    chat_id=chat_id,
                )

        elif cmd == "/myrules":
            rules = users.get(chat_key, {}).get("rules", "")
            if rules:
                send_telegram_message(f"📋 <b>Your saved rules:</b>\n{rules}", chat_id=chat_id)
            else:
                send_telegram_message("You haven't saved any rules yet. Use /setrules to add some.", chat_id=chat_id)

        elif cmd == "/clearrules":
            if chat_key in users and users[chat_key].get("rules"):
                users[chat_key]["rules"] = ""
                changed = True
            send_telegram_message("🗑️ Your saved rules were cleared.", chat_id=chat_id)

    if changed:
        save_users(users)

    max_update_id = max(u["update_id"] for u in updates)
    try:
        requests.get(url, params={"offset": max_update_id + 1, "timeout": 0}, timeout=15)
    except requests.RequestException as e:
        log.error("Failed to acknowledge Telegram updates: %s", e)


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

    if now_local.weekday() >= 5:  # Saturday=5, Sunday=6 -> weekend, market closed
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
        base_text = f"🔍 Checked now — {session_line}No high-impact news scheduled for today."
    else:
        lines = [f"🔍 <b>On-demand check — {today.strftime('%A, %d %b %Y')}</b>\n{session_line}"]
        for e in todays_events:
            marker = "✅ (already out)" if e["_dt"] < now_utc else "⏳ (upcoming)"
            lines.append(f"{marker}\n{format_event_line(e, tz)}")
        base_text = "\n\n".join(lines)

    users = load_users()
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

    poll_and_handle_commands()

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

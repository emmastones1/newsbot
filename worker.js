/**
 * FX Pulse — instant Telegram webhook handler.
 *
 * Handles everything a person does LIVE: /start, the "Check News Now"
 * button, /check, /setrules, /myrules, /clearrules. Responds within
 * milliseconds, since Telegram calls this the moment a message arrives
 * (no polling/waiting involved).
 *
 * The scheduled daily digest and session alerts stay on the GitHub
 * Actions side (forex_news_bot.py) — those don't need instant response,
 * just reliable timing. Both sides read/write the same Cloudflare KV
 * namespace, so a person who /start's here shows up in the scheduled
 * broadcasts too.
 *
 * Required bindings/secrets (set in the Worker's Settings):
 *   - KV namespace bound as: USERS_KV
 *   - Secret variable: BOT_TOKEN  (your Telegram bot token)
 */

const CHECK_BUTTON_LABEL = "🔍 Check News Now";
const FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";
const LOCAL_TZ = "Africa/Lagos"; // used for "today" in on-demand checks

const SESSIONS = {
  "Asian": { tz: "Asia/Tokyo", open: 9, close: 18 },
  "London": { tz: "Europe/London", open: 8, close: 17 },
  "New York": { tz: "America/New_York", open: 8, close: 17 },
};

const WELCOME_TEXT =
  "👋 <b>Welcome to FX Pulse.</b>\n\n" +
  "I track high-impact ForexFactory news — a daily digest, plus a heads-up " +
  "before the Asian, London, and New York sessions.\n\n" +
  "<b>Commands:</b>\n" +
  "🔍 Check News Now — see what's on today, right now\n" +
  "/setrules &lt;text&gt; — save your own trading rules; I'll remind you of them\n" +
  "/myrules — see your saved rules\n" +
  "/clearrules — clear them";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("FX Pulse webhook is running.", { status: 200 });
    }
    let update;
    try {
      update = await request.json();
    } catch (e) {
      return new Response("bad request", { status: 400 });
    }
    // Ack Telegram immediately; do the actual work in the background so
    // Telegram never sees a slow response (ctx.waitUntil keeps the worker
    // alive after the response is sent).
    ctx.waitUntil(handleUpdate(update, env));
    return new Response("ok", { status: 200 });
  },
};

async function handleUpdate(update, env) {
  const msg = update.message;
  if (!msg || !msg.text) return;

  const chatId = msg.chat.id;
  const rawText = msg.text.trim();
  const firstWord = rawText.split(/\s+/)[0] || "";
  const cmd = firstWord.split("@")[0].toLowerCase(); // strip a possible @BotName suffix
  const arg = rawText.slice(firstWord.length).trim();

  try {
    if (cmd === "/start") {
      const existing = await getUser(env, chatId);
      if (!existing) await setUser(env, chatId, { rules: "" });
      await sendMessage(env, chatId, WELCOME_TEXT, true);
      return;
    }

    if (cmd === "/check" || cmd === "/news" || rawText.toLowerCase() === CHECK_BUTTON_LABEL.toLowerCase()) {
      await handleCheck(env, chatId);
      return;
    }

    if (cmd === "/setrules") {
      const user = (await getUser(env, chatId)) || { rules: "" };
      if (arg) {
        user.rules = arg;
        await setUser(env, chatId, user);
        await sendMessage(env, chatId, "✅ Your trading rules are saved. I'll include them with your checks and alerts.", true);
      } else {
        await sendMessage(
          env, chatId,
          "Send your rules right after the command, e.g.:\n/setrules Never risk more than 1% per trade.",
          true
        );
      }
      return;
    }

    if (cmd === "/myrules") {
      const user = await getUser(env, chatId);
      if (user && user.rules) {
        await sendMessage(env, chatId, `📋 <b>Your saved rules:</b>\n${user.rules}`, true);
      } else {
        await sendMessage(env, chatId, "You haven't saved any rules yet. Use /setrules to add some.", true);
      }
      return;
    }

    if (cmd === "/clearrules") {
      const user = await getUser(env, chatId);
      if (user) {
        user.rules = "";
        await setUser(env, chatId, user);
      }
      await sendMessage(env, chatId, "🗑️ Your saved rules were cleared.", true);
      return;
    }
  } catch (e) {
    // Never let an error here leave the person without any reply.
    await sendMessage(env, chatId, "⚠️ Something went wrong handling that — try again in a moment.", true);
  }
}

// --------------------------------------------------------------------------
// KV storage (one key per chat: "user:<chatId>" -> {"rules": "..."})
// --------------------------------------------------------------------------
async function getUser(env, chatId) {
  const raw = await env.USERS_KV.get(`user:${chatId}`);
  return raw ? JSON.parse(raw) : null;
}

async function setUser(env, chatId, data) {
  await env.USERS_KV.put(`user:${chatId}`, JSON.stringify(data));
}

// --------------------------------------------------------------------------
// Telegram
// --------------------------------------------------------------------------
async function sendMessage(env, chatId, text, withKeyboard) {
  const payload = {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  };
  if (withKeyboard) {
    payload.reply_markup = {
      keyboard: [[CHECK_BUTTON_LABEL]],
      resize_keyboard: true,
      is_persistent: true,
    };
  }
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// --------------------------------------------------------------------------
// Timezone helpers (no external libraries — Workers' V8 runtime has full
// Intl/ICU support, which is enough to do IANA timezone math by hand).
// --------------------------------------------------------------------------
function getPartsInTZ(date, timeZone) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone, hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const map = {};
  for (const p of fmt.formatToParts(date)) map[p.type] = p.value;
  let hour = parseInt(map.hour, 10);
  if (hour === 24) hour = 0;
  return {
    year: parseInt(map.year, 10), month: parseInt(map.month, 10), day: parseInt(map.day, 10),
    hour, minute: parseInt(map.minute, 10),
  };
}

function zonedWallTimeToUtc(y, m, d, hh, mm, timeZone) {
  // Two-pass approximation: guess, see how far off the guess reads in the
  // target zone, then correct by that difference. Accurate except within
  // the same hour as a DST transition, which doesn't matter for our
  // fixed-hour session boundaries.
  const guess = new Date(Date.UTC(y, m - 1, d, hh, mm, 0));
  const p = getPartsInTZ(guess, timeZone);
  const guessedWallAsUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, 0);
  const intendedWallAsUtc = Date.UTC(y, m - 1, d, hh, mm, 0);
  return new Date(guess.getTime() + (intendedWallAsUtc - guessedWallAsUtc));
}

function sessionStatus(sessionName, now) {
  const s = SESSIONS[sessionName];
  const p = getPartsInTZ(now, s.tz);
  const open = zonedWallTimeToUtc(p.year, p.month, p.day, s.open, 0, s.tz);
  const close = zonedWallTimeToUtc(p.year, p.month, p.day, s.close, 0, s.tz);
  if (now < open) return { status: "upcoming", minutes: Math.round((open - now) / 60000) };
  if (now > close) return { status: "passed", minutes: Math.round((now - close) / 60000) };
  return { status: "active", minutes: null };
}

// --------------------------------------------------------------------------
// ForexFactory calendar
// --------------------------------------------------------------------------
function isHighImpact(ev) {
  return String(ev.impact || "").trim().toLowerCase() === "high";
}

function formatEventLine(ev, timeZone) {
  const p = getPartsInTZ(new Date(ev.date), timeZone);
  const hh = String(p.hour).padStart(2, "0");
  const mm = String(p.minute).padStart(2, "0");
  const forecast = ev.forecast || "-";
  const previous = ev.previous || "-";
  return `🔴 ${hh}:${mm} | <b>${ev.country || ""}</b> — ${ev.title || ""}\n     Forecast: ${forecast} | Previous: ${previous}`;
}

async function handleCheck(env, chatId) {
  const now = new Date();

  let events = [];
  try {
    const resp = await fetch(FF_CALENDAR_URL, { headers: { "User-Agent": "Mozilla/5.0" } });
    events = await resp.json();
  } catch (e) {
    events = [];
  }

  const highImpact = events.filter(isHighImpact);
  const todayParts = getPartsInTZ(now, LOCAL_TZ);
  const todays = highImpact.filter((ev) => {
    const p = getPartsInTZ(new Date(ev.date), LOCAL_TZ);
    return p.year === todayParts.year && p.month === todayParts.month && p.day === todayParts.day;
  });

  let activeSession = null;
  for (const name of Object.keys(SESSIONS)) {
    if (sessionStatus(name, now).status === "active") {
      activeSession = name;
      break;
    }
  }
  const sessionLine = activeSession
    ? `🕒 Currently in the <b>${activeSession}</b> session.\n`
    : "🕒 No major session currently active.\n";

  const dateLabel = now.toLocaleDateString("en-GB", {
    timeZone: LOCAL_TZ, weekday: "long", day: "2-digit", month: "short", year: "numeric",
  });

  let text;
  if (todays.length === 0) {
    text = `🔍 Checked now — ${sessionLine}No high-impact news scheduled for today.`;
  } else {
    const lines = [`🔍 <b>On-demand check — ${dateLabel}</b>\n${sessionLine}`];
    for (const ev of todays) {
      const evDt = new Date(ev.date);
      const marker = evDt < now ? "✅ (already out)" : "⏳ (upcoming)";
      lines.push(`${marker}\n${formatEventLine(ev, LOCAL_TZ)}`);
    }
    text = lines.join("\n\n");
  }

  const user = await getUser(env, chatId);
  if (user && user.rules) {
    text += `\n\n📋 <b>Your rules:</b>\n${user.rules}`;
  }

  await sendMessage(env, chatId, text, true);
}

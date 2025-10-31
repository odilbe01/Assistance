# bot.py
import os
import re
import time
import json
import asyncio
import logging
from typing import Dict, Optional, Set, Tuple, List
from datetime import datetime, timedelta

from telegram import Update, Message, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    ChatMemberHandler, CallbackQueryHandler, filters
)

# =========================
# Env & basic configuration
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env is required")

MAIN_GROUP_ID_ENV = os.getenv("MAIN_GROUP_ID", "").strip()
MAIN_GROUP_ID: Optional[int] = int(MAIN_GROUP_ID_ENV) if MAIN_GROUP_ID_ENV else None

TEAM_USERNAMES_ENV = os.getenv("TEAM_USERNAMES", "").strip()
TEAM_USER_IDS_ENV = os.getenv("TEAM_USER_IDS", "").strip()
OWNER_IDS_ENV = os.getenv("OWNER_IDS", "").strip()

ALERT_DELAY_SECONDS = int(os.getenv("ALERT_DELAY_SECONDS", "90") or 90)
DUP_TTL_SECONDS = int(os.getenv("DUP_TTL_SECONDS", str(24 * 60 * 60)) or 86400)  # 24h TTL
WARN_ON_DUP = os.getenv("WARN_ON_DUP", "1").strip() not in ("0", "false", "False")

# ===== Persistence files =====
STATS_FILE = os.getenv("STATS_FILE", "reply_stats.json").strip()
MAX_REPLY_WINDOW_SEC = int(os.getenv("MAX_REPLY_WINDOW_SEC", "300") or 300)  # 5 minutes
GROUPS_FILE = os.getenv("GROUPS_FILE", "groups.json").strip()
INACTIVE_FILE = os.getenv("INACTIVE_FILE", "inactive_groups.json").strip()
ANALYZE_TEAM_FILE = os.getenv("ANALYZE_TEAM_FILE", "analyze_team.json").strip()

TEAM_USERNAMES: Set[str] = {
    u.strip().lower().lstrip("@")
    for u in TEAM_USERNAMES_ENV.split(",") if u.strip()
}
TEAM_USER_IDS: Set[int] = set(
    int(x.strip()) for x in TEAM_USER_IDS_ENV.split(",") if x.strip().isdigit()
)
OWNER_IDS: Set[int] = set(
    int(x.strip()) for x in OWNER_IDS_ENV.split(",") if x.strip().isdigit()
)

# =================
# Logging
# =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("watchdog-90s")

# ============
# Globals
# ============
# (chat_id -> (task, last_msg))
PENDING: Dict[int, Tuple[asyncio.Task, Message]] = {}

# Duplicate detector (PIN-LOAD-ID asosida)
# Kalit: "PIN:<LOAD_ID>" -> (first_seen_epoch, first_group_id, first_group_title)
DUP_SEEN: Dict[str, Tuple[float, int, str]] = {}
# Qaysi (kalit, group_id) bo‘yicha allaqachon alert qilingan — shuni eslab qolamiz
DUP_ALERTED: Set[Tuple[str, int]] = set()

# ---------------- PIN-only extractor ----------------
_PIN_LOAD_RE = re.compile(
    r"(?mi)^\s*(?:📍\s*)?(\d+)\s*#\s*[:：-]?\s*([A-Z0-9]{6,20})\b"
)

# --------- Reply-time analytics ---------
# Har bir chat uchun oxirgi driver xabari va vaqti (sekund epoch)
LAST_DRIVER_TS: Dict[int, float] = {}

# Stats: list of dicts
# {"ts": 1730256000.0, "ym": "2025-10", "user_id": 123, "username": "alex", "name": "Alex", "seconds": 42.0}
STATS: List[dict] = []

# Group registry and activation flags
KNOWN_GROUPS: Dict[int, str] = {}          # chat_id -> title
INACTIVE_GROUPS: Set[int] = set()          # chat_id that are paused

# Team-only analysis whitelist (lowercase usernames without @)
ANALYZE_TEAM: Set[str] = set()

# ============ Persistence helpers ============

def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning("Could not load %s: %s", path, e)
    return default


def _save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


def _load_stats() -> None:
    global STATS
    STATS = _load_json(STATS_FILE, []) or []


def _save_stats() -> None:
    _save_json(STATS_FILE, STATS)


def _record_reply(user_id: int, username: str, name: str, seconds: float, ts: Optional[float] = None):
    if ts is None:
        ts = time.time()
    ym = datetime.fromtimestamp(ts).strftime("%Y-%m")
    STATS.append({
        "ts": ts,
        "ym": ym,
        "user_id": int(user_id),
        "username": (username or "").lower(),
        "name": name or "",
        "seconds": float(seconds)
    })
    _save_stats()


def _load_groups() -> None:
    global KNOWN_GROUPS, INACTIVE_GROUPS, ANALYZE_TEAM
    KNOWN_GROUPS = _load_json(GROUPS_FILE, {}) or {}
    INACTIVE_GROUPS = set(_load_json(INACTIVE_FILE, []) or [])
    ANALYZE_TEAM = set((u or "").lower().lstrip("@") for u in (_load_json(ANALYZE_TEAM_FILE, []) or []))


def _save_groups() -> None:
    _save_json(GROUPS_FILE, KNOWN_GROUPS)
    _save_json(INACTIVE_FILE, list(sorted(INACTIVE_GROUPS)))
    _save_json(ANALYZE_TEAM_FILE, list(sorted(ANALYZE_TEAM)))


# ============ Helpers ============

def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def is_main(chat_id: int) -> bool:
    return MAIN_GROUP_ID is not None and chat_id == MAIN_GROUP_ID


def is_team_user(update: Update) -> bool:
    u = update.effective_user
    if not u or u.is_bot:
        return False
    if u.id in TEAM_USER_IDS:
        return True
    uname = (u.username or "").lower()
    return uname in TEAM_USERNAMES


def is_owner(user_id: Optional[int]) -> bool:
    # OWNER_IDS bo'sh bo'lsa, hech kim owner emas.
    return bool(user_id and OWNER_IDS and user_id in OWNER_IDS)


async def cancel_pending(chat_id: int):
    pair = PENDING.pop(chat_id, None)
    if pair:
        task, _ = pair
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def schedule_alert(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg: Message):
    # Agar guruh PAUSED bo'lsa — hech narsa qilmaymiz
    if chat_id in INACTIVE_GROUPS:
        return

    # har safar yangidan taymer — oxirgi xabar bo‘yicha
    await cancel_pending(chat_id)

    async def _job():
        try:
            await asyncio.sleep(ALERT_DELAY_SECONDS)
            if MAIN_GROUP_ID is None:
                log.warning("MAIN_GROUP_ID not set; skipping alert")
                return
            group_title = msg.chat.title or "(no title)"
            sender = msg.from_user.full_name if msg.from_user else "(unknown)"
            snippet = msg.text or msg.caption or "(non-text message)"
            header = (
                f"🚨 *No team reply in {ALERT_DELAY_SECONDS} sec*\n"
                f"👥 *Group:* {group_title}\n"
                f"👤 *From:* {sender}\n\n"
                f"{snippet[:4000]}"
            )
            await context.bot.send_message(
                chat_id=MAIN_GROUP_ID,
                text=header,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        finally:
            PENDING.pop(chat_id, None)

    task = asyncio.create_task(_job())
    PENDING[chat_id] = (task, msg)


def _purge_expired_duplicates() -> None:
    if not DUP_SEEN:
        return
    cutoff = time.time() - DUP_TTL_SECONDS
    to_del = [k for k, (ts, _, _) in DUP_SEEN.items() if ts < cutoff]
    for k in to_del:
        DUP_SEEN.pop(k, None)
    if DUP_ALERTED:
        to_keep = {(key, gid) for (key, gid) in DUP_ALERTED if key in DUP_SEEN}
        DUP_ALERTED.clear()
        DUP_ALERTED.update(to_keep)


def _extract_pin_load_ids(text: str) -> Set[str]:
    """Faqat 📍 <n># : <LOAD_ID> formatidan ID qaytaradi. Kalit: PIN:<LOAD_ID>"""
    ids: Set[str] = set()
    if not text:
        return ids
    for _, load_id in _PIN_LOAD_RE.findall(text):
        lid = load_id.upper()
        ids.add(f"PIN:{lid}")
    return ids


async def process_pin_duplicate_forward(context: ContextTypes.DEFAULT_TYPE, msg: Message):
    """
    Bir xil 📍 <n># : <LOAD_ID> ikki xil driver guruhida ko‘rilsa:
      - agar WARN_ON_DUP=1 bo‘lsa → MAIN’ga WARNING yuboriladi
      - forward QILINMAYDI (faqat warning)
    (Har bir (key, group_id) uchun faqat bir marta).
    """
    if not msg or not msg.chat or not MAIN_GROUP_ID:
        return
    chat = msg.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if chat.id == MAIN_GROUP_ID:
        return

    _purge_expired_duplicates()

    text = msg.text or msg.caption or ""
    keys = _extract_pin_load_ids(text)
    if not keys:
        return

    log.info("PINFWD: found keys=%s in group_id=%s", keys, chat.id)

    for key in keys:
        first = DUP_SEEN.get(key)
        if not first:
            DUP_SEEN[key] = (
                time.time(),
                chat.id,
                (chat.title or f"id:{chat.id}")
            )
            continue

        _, first_gid, first_title = first
        if first_gid == chat.id:
            # shu guruh ichida ko‘rildi — e’tibor bermaymiz
            continue

        mark = (key, chat.id)
        if mark in DUP_ALERTED:
            continue
        DUP_ALERTED.add(mark)

        # Warning (faqat xabar, forward yo‘q)
        if WARN_ON_DUP:
            load_id = key.split(":", 1)[1]

            group1 = first_title or "(no title)"
            group2 = chat.title or "(no title)"
            sender_name = "(unknown)"
            try:
                if msg.forward_from:
                    sender_name = msg.forward_from.full_name
                elif msg.forward_from_chat:
                    sender_name = (
                        msg.forward_from_chat.title
                        or ("@" + (msg.forward_from_chat.username or "unknown"))
                    )
                elif msg.from_user:
                    sender_name = msg.from_user.full_name
            except Exception:
                pass

            try:
                await context.bot.send_message(
                    chat_id=MAIN_GROUP_ID,
                    text=(
                        "⚠️ WARNING: POSSIBLE DUPLICATE IN MULTIPLE DRIVER GROUPS\n"
                        f"📦 Load ID: {load_id}\n"
                        f"👥 Group 1: {group1}\n"
                        f"👥 Group 2: {group2}\n"
                        f"👤 Sender: {sender_name}\n"
                        "Immediate action required: verify assignment to prevent double-booking, penalties, and pay conflicts."
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("PINFWD: warning send failed for %s", key)


# ================ Commands ================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Watchdog is running.\n"
        "• Use /setmaingroup in your MAIN group (owner only).\n"
        "• /addteam @u1 @u2 or numeric IDs (owner only).\n"
        f"• Current delay: {ALERT_DELAY_SECONDS} seconds."
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_text(
        "Main: {}\nTeam usernames: {}\nTeam IDs: {}\nDelay: {}s\nWarnOnDup: {}\nTTL: {}s\nPaused groups: {}".format(
            MAIN_GROUP_ID,
            ", ".join(sorted(TEAM_USERNAMES)) or "(none)",
            ", ".join(map(str, sorted(TEAM_USER_IDS))) or "(none)",
            ALERT_DELAY_SECONDS,
            WARN_ON_DUP,
            DUP_TTL_SECONDS,
            ", ".join(str(g) for g in sorted(INACTIVE_GROUPS)) or "(none)",
        )
    )


async def set_main_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text("Run this *inside* your MAIN group.", parse_mode=ParseMode.MARKDOWN)
        return
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return
    global MAIN_GROUP_ID
    MAIN_GROUP_ID = update.effective_chat.id
    await update.message.reply_text(f"MAIN group set to: {MAIN_GROUP_ID}")


async def add_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    added = []
    for token in context.args:
        t = token.strip()
        if not t:
            continue
        if t.startswith("@"):
            t = t[1:]
        if t.isdigit():
            TEAM_USER_IDS.add(int(t)); added.append(t)
        else:
            TEAM_USERNAMES.add(t.lower()); added.append("@"+t.lower())
    await update.message.reply_text("Added to TEAM: " + (", ".join(added) or "(none)"))


async def remove_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    removed = []
    for token in context.args:
        t = token.strip()
        if not t:
            continue
        if t.startswith("@"):
            t = t[1:]
        if t.isdigit():
            if int(t) in TEAM_USER_IDS:
                TEAM_USER_IDS.remove(int(t)); removed.append(t)
        else:
            tl = t.lower()
            if tl in TEAM_USERNAMES:
                TEAM_USERNAMES.remove(tl); removed.append("@"+tl)
    await update.message.reply_text("Removed from TEAM: " + (", ".join(removed) or "(none)"))


async def list_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_text(
        f"TEAM usernames: {', '.join(sorted(TEAM_USERNAMES)) or '(none)'}\n"
        f"TEAM ids: {', '.join(map(str, sorted(TEAM_USER_IDS))) or '(none)'}"
    )


async def clearseen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    DUP_SEEN.clear()
    DUP_ALERTED.clear()
    await update.message.reply_text("Duplicate cache cleared.")


# ---------- Running groups registry & toggles ----------
async def _ensure_group_registered(chat) -> None:
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if chat.id not in KNOWN_GROUPS:
        KNOWN_GROUPS[chat.id] = chat.title or f"id:{chat.id}"
        _save_groups()


async def running_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    # Human-friendly list
    if not KNOWN_GROUPS:
        await update.message.reply_text("No groups registered yet.")
        return
    lines = ["📋 *Running groups (known by bot):*", "(Active by default; 'Paused' means watchdog disabled)"]
    for gid, title in sorted(KNOWN_GROUPS.items(), key=lambda x: x[1].lower()):
        state = "Paused" if gid in INACTIVE_GROUPS else "Active"
        mark = "⏸️" if state == "Paused" else "▶️"
        lines.append(f"{mark} {title} — `{gid}` — {state}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _toggle_group_active(update: Update, context: ContextTypes.DEFAULT_TYPE, active: bool):
    if not is_owner(update.effective_user.id):
        return
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Run this inside a *group*.", parse_mode=ParseMode.MARKDOWN)
        return
    await _ensure_group_registered(chat)
    if active:
        INACTIVE_GROUPS.discard(chat.id)
        await update.message.reply_text("This group is now *Active*. Watchdog enabled.", parse_mode=ParseMode.MARKDOWN)
    else:
        INACTIVE_GROUPS.add(chat.id)
        # cancel any pending timers for this chat
        await cancel_pending(chat.id)
        await update.message.reply_text("This group is now *Paused*. Watchdog disabled.", parse_mode=ParseMode.MARKDOWN)
    _save_groups()


# Accept both exact and numbered variants (e.g., /inactivategroup1)
async def inactivate_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_group_active(update, context, active=False)


async def activate_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_group_active(update, context, active=True)


# ---------- ANALYSIS: UI & logic ----------

def _month_buttons(n_months: int = 6, prefix: str = "ANALYZE") -> InlineKeyboardMarkup:
    today = datetime.now()
    months = []
    cur = datetime(today.year, today.month, 1)
    for _ in range(n_months):
        label = cur.strftime("%Y-%m")
        months.append(label)
        # previous month
        prev_month = cur.replace(day=1) - timedelta(days=1)
        cur = datetime(prev_month.year, prev_month.month, 1)
    rows = [months[i:i+3] for i in range(0, len(months), 3)]
    keyboard = [[InlineKeyboardButton(m, callback_data=f"{prefix}:{m}") for m in row] for row in rows]
    return InlineKeyboardMarkup(keyboard)


async def analiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not is_main(update.effective_chat.id):
        await update.message.reply_text("Please run in MAIN group.")
        return
    # Two keyboards: All team vs MyTeam-only (if configured)
    parts = ["Select a month for response-time analysis:"]
    await update.message.reply_text(
        "\n".join(parts + ["\n*All Team:*"]),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_month_buttons(6, prefix="ANALYZE")
    )
    if ANALYZE_TEAM:
        await update.message.reply_text(
            "*MyTeam Only:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_month_buttons(6, prefix="ANALYZE_MY")
        )
    else:
        await update.message.reply_text(
            "No MyTeam set. Use `/myteamanalizsetup alex steve` to configure.",
            parse_mode=ParseMode.MARKDOWN
        )


async def _build_analysis_text(month: str, only_myteam: bool = False) -> str:
    # Aggregate
    per_user: Dict[int, Dict[str, float]] = {}

    def _is_allowed_username(u: str) -> bool:
        if not only_myteam:
            return True
        if not ANALYZE_TEAM:
            return False
        return (u or "").lower() in ANALYZE_TEAM

    for rec in STATS:
        if rec.get("ym") != month:
            continue
        uname = (rec.get("username") or "").lower()
        if not _is_allowed_username(uname):
            continue
        uid = int(rec.get("user_id", 0))
        per_user.setdefault(uid, {
            "name": rec.get("name") or rec.get("username") or str(uid),
            "sum": 0.0,
            "count": 0.0,
        })
        per_user[uid]["sum"] += float(rec.get("seconds", 0.0))
        per_user[uid]["count"] += 1.0

    if not per_user:
        scope = "MyTeam" if only_myteam else "All Team"
        return f"No data for {month} ({scope})."

    # Sort by avg asc, then count desc
    rows = []
    for uid, d in per_user.items():
        avg = (d["sum"] / d["count"]) if d["count"] else 0.0
        rows.append((avg, d["count"], d["name"]))
    rows.sort(key=lambda x: (x[0], -x[1]))

    scope = "MyTeam" if only_myteam else "All Team"
    lines = [f"📊 *Reply-time analysis for {month}* — _{scope}_ (lower is better)"]
    rank = 1
    for avg, cnt, name in rows:
        lines.append(f"{rank}. {name} — avg {int(round(avg))}s (n={int(cnt)})")
        rank += 1
    return "\n".join(lines)


async def analiz_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    cq = update.callback_query
    if not is_owner(cq.from_user.id):
        await cq.answer("Not authorized.", show_alert=True)
        return
    if not cq.data or not (cq.data.startswith("ANALYZE:") or cq.data.startswith("ANALYZE_MY:")):
        return

    only_myteam = cq.data.startswith("ANALYZE_MY:")
    month = cq.data.split(":", 1)[1]  # "YYYY-MM"

    text = await _build_analysis_text(month, only_myteam=only_myteam)
    await cq.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------- MyTeam-only analysis setup ----------
async def myteam_setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    # Expect a list of usernames or @usernames
    newset: Set[str] = set()
    for tok in context.args:
        t = tok.strip().lower().lstrip("@")
        if t:
            newset.add(t)
    global ANALYZE_TEAM
    ANALYZE_TEAM = newset
    _save_groups()
    if ANALYZE_TEAM:
        await update.message.reply_text(
            "MyTeam for analysis set to: " + ", ".join(sorted(ANALYZE_TEAM))
        )
    else:
        await update.message.reply_text("MyTeam cleared. Using All Team by default.")


# =================== Message handlers ===================
async def driver_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    # Register group title/id for /runninggroups
    await _ensure_group_registered(chat)

    # If MAIN group — skip watchdog and analytics
    if MAIN_GROUP_ID is not None and chat.id == MAIN_GROUP_ID:
        return

    # If group is paused — do nothing (no analytics, no watchdog)
    if chat.id in INACTIVE_GROUPS:
        return

    if not msg:
        return

    # 1) PIN-only duplicate check (bot xabarlari ham)
    try:
        await process_pin_duplicate_forward(context, msg)
    except Exception as e:
        log.warning("pin duplicate check failed: %s", e)

    # 2) Analytics: driver xabari / team javobi matching
    now = time.time()
    if msg.from_user and not msg.from_user.is_bot:
        if is_team_user(update):
            # team reply
            last_ts = LAST_DRIVER_TS.get(chat.id)
            if last_ts:
                diff = now - last_ts
                if 0 <= diff <= MAX_REPLY_WINDOW_SEC:
                    user = msg.from_user
                    _record_reply(
                        user_id=user.id,
                        username=(user.username or "").lower(),
                        name=user.full_name or "",
                        seconds=diff,
                        ts=now,
                    )
                # Bir driverga faqat birinchi team javobi sanalsin:
                LAST_DRIVER_TS.pop(chat.id, None)
        else:
            # driver message — yangi boshlanish
            LAST_DRIVER_TS[chat.id] = now

    # 3) Agar xabarni bot yuborgan bo‘lsa, shu yerda to‘xtaymiz (watchdog yo‘q)
    if msg.from_user and msg.from_user.is_bot:
        return

    # 4) TEAM a’zosi yozsa → 90s taymerni bekor qilamiz
    if is_team_user(update):
        await cancel_pending(chat.id)
        return

    # 5) Oddiy foydalanuvchi xabari → 90s watchdog
    await schedule_alert(context, chat.id, msg)


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    # auto-register when added to a group
    await _ensure_group_registered(chat)

    status = update.my_chat_member.new_chat_member.status
    if status in (ChatMember.KICKED, ChatMember.LEFT):
        await cancel_pending(chat.id)


# ============ Entrypoint ============

def main():
    _load_stats()
    _load_groups()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setmaingroup", set_main_cmd))
    app.add_handler(CommandHandler("addteam", add_team_cmd))
    app.add_handler(CommandHandler("removeteam", remove_team_cmd))
    app.add_handler(CommandHandler("listteam", list_team_cmd))
    app.add_handler(CommandHandler("clearseen", clearseen_cmd))

    # Running groups
    app.add_handler(CommandHandler("runninggroups", running_groups_cmd))

    # Accept both exact and numbered variants (e.g., /inactivategroup1)
    app.add_handler(CommandHandler(["inactivategroup", "inactivategroup1", "inactivategroup2", "inactivategroup3"], inactivate_group_cmd))
    app.add_handler(CommandHandler(["activategroup", "activategroup1", "activategroup2", "activategroup3"], activate_group_cmd))

    # Analysis
    app.add_handler(CommandHandler("analiz", analiz_cmd))
    app.add_handler(CallbackQueryHandler(analiz_cb, pattern=r"^(ANALYZE|ANALYZE_MY):"))
    app.add_handler(CommandHandler("myteamanalizsetup", myteam_setup_cmd))

    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, driver_message_handler))

    log.info(
        "Watchdog 90s started. MAIN=%s Delay=%ss DUP_TTL=%ss WARN_ON_DUP=%s",
        MAIN_GROUP_ID, ALERT_DELAY_SECONDS, DUP_TTL_SECONDS, WARN_ON_DUP
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

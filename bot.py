# bot.py
import os
import re
import time
import json
import asyncio
import logging
from typing import Dict, Optional, Set, Tuple, List
from datetime import datetime, timedelta, timezone

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

# Javob vaqti analitikasi uchun fayl
STATS_FILE = os.getenv("STATS_FILE", "reply_stats.json").strip()
MAX_REPLY_WINDOW_SEC = int(os.getenv("MAX_REPLY_WINDOW_SEC", "300") or 300)  # 5 minut

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
# Qaysi (kalit, group_id) bo‘yicha allaqachon alert/forward qilingan — shuni eslab qolamiz
DUP_ALERTED: Set[Tuple[str, int]] = set()

# ---------------- PIN-only extractor ----------------
_PIN_LOAD_RE = re.compile(
    r"(?mi)^\s*(?:📍\s*)?(\d+)\s*#\s*[:：-]?\s*([A-Z0-9]{6,20})\b"
)

# --------- Reply-time analytics ---------
# Har bir chat uchun oxirgi driver xabari va vaqti (sekund epoch)
LAST_DRIVER_TS: Dict[int, float] = {}   # chat_id -> last driver epoch seconds

# Stats yozuvlari: list of dicts
# {"ts": 1730256000.0, "ym": "2025-10", "user_id": 123, "username": "alex", "name": "Alex", "seconds": 42.0}
STATS: List[dict] = []

def _load_stats() -> None:
    global STATS
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                STATS = data
            else:
                STATS = []
        else:
            STATS = []
    except Exception as e:
        log.warning("Could not load stats: %s", e)
        STATS = []

def _save_stats() -> None:
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATS, f, ensure_ascii=False)
        os.replace(tmp, STATS_FILE)
    except Exception as e:
        log.warning("Could not save stats: %s", e)

def _record_reply(user_id: int, username: str, name: str, seconds: float, ts: Optional[float] = None):
    if ts is None:
        ts = time.time()
    ym = datetime.fromtimestamp(ts).strftime("%Y-%m")
    STATS.append({
        "ts": ts,
        "ym": ym,
        "user_id": int(user_id),
        "username": username or "",
        "name": name or "",
        "seconds": float(seconds)
    })
    _save_stats()

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
    # OWNER_IDS bo'sh bo'lsa, hech kim owner emas (xavfsiz variant).
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
        "Main: {}\nTeam usernames: {}\nTeam IDs: {}\nDelay: {}s\nWarnOnDup: {}\nTTL: {}s".format(
            MAIN_GROUP_ID,
            ", ".join(sorted(TEAM_USERNAMES)) or "(none)",
            ", ".join(map(str, sorted(TEAM_USER_IDS))) or "(none)",
            ALERT_DELAY_SECONDS,
            WARN_ON_DUP,
            DUP_TTL_SECONDS,
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

# ---------- ANALYSIS: UI & logic ----------
def _month_buttons(n_months: int = 6) -> InlineKeyboardMarkup:
    today = datetime.now()
    months = []
    cur = datetime(today.year, today.month, 1)
    for _ in range(n_months):
        label = cur.strftime("%Y-%m")
        months.append(label)
        # previous month
        prev_month = cur.replace(day=1) - timedelta(days=1)
        cur = datetime(prev_month.year, prev_month.month, 1)
    # 2 qator: 3 tadan
    rows = [months[i:i+3] for i in range(0, len(months), 3)]
    keyboard = [[InlineKeyboardButton(m, callback_data=f"ANALYZE:{m}")] for r in rows for m in r]
    # yuqoridagi comprehension bir qatorda chiqaradi; pastdagisi 3 tadan qilib beradi:
    keyboard = [ [InlineKeyboardButton(m, callback_data=f"ANALYZE:{m}") for m in row] for row in rows ]
    return InlineKeyboardMarkup(keyboard)

async def analiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not is_main(update.effective_chat.id):
        await update.message.reply_text("Please run in MAIN group.")
        return
    await update.message.reply_text(
        "Select a month for response-time analysis:",
        reply_markup=_month_buttons(6)
    )

async def analiz_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    cq = update.callback_query
    if not is_owner(cq.from_user.id):
        await cq.answer("Not authorized.", show_alert=True)
        return
    if not cq.data or not cq.data.startswith("ANALYZE:"):
        return
    month = cq.data.split(":", 1)[1]  # "YYYY-MM"
    # Aggregate
    per_user = {}
    for rec in STATS:
        if rec.get("ym") == month:
            uid = rec.get("user_id")
            per_user.setdefault(uid, {"name": rec.get("name") or rec.get("username") or str(uid),
                                      "sum": 0.0, "count": 0})
            per_user[uid]["sum"] += float(rec.get("seconds", 0))
            per_user[uid]["count"] += 1

    if not per_user:
        await cq.edit_message_text(f"No data for {month}.")
        return

    # Sort by average ascending
    rows = []
    for uid, d in per_user.items():
        avg = (d["sum"] / d["count"]) if d["count"] else 0.0
        rows.append((avg, d["count"], d["name"]))
    rows.sort(key=lambda x: (x[0], -x[1]))  # avg asc, then count desc

    lines = [f"📊 *Reply-time analysis for {month}* (lower is better)"]
    rank = 1
    for avg, cnt, name in rows:
        lines.append(f"{rank}. {name} — avg {int(round(avg))}s (n={cnt})")
        rank += 1

    await cq.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# =================== Message handlers ===================
async def driver_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if MAIN_GROUP_ID is not None and chat.id == MAIN_GROUP_ID:
        return
    if not msg:
        return

    # 1) PIN-only duplicate check (bot xabarlari ham)
    try:
        await process_pin_duplicate_forward(context, msg)
    except Exception as e:
        log.warning("pin duplicate check failed: %s", e)

    # 2) Analytics: driver xabari / team javobi matching
    #   - Team emas bo'lsa: driver deb belgilaymiz
    #   - Team bo'lsa: agar oxirgi driver xabari bor va diff <= 300s -> yozib qo'yamiz
    now = time.time()
    if msg.from_user and not msg.from_user.is_bot:
        if is_team_user(update):
            # team reply
            last_ts = LAST_DRIVER_TS.get(chat.id)
            if last_ts:
                diff = now - last_ts
                if 0 <= diff <= MAX_REPLY_WINDOW_SEC:
                    # record
                    user = msg.from_user
                    _record_reply(
                        user_id=user.id,
                        username=user.username or "",
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
    status = update.my_chat_member.new_chat_member.status
    if status in (ChatMember.KICKED, ChatMember.LEFT):
        await cancel_pending(chat.id)

# ============ Entrypoint ============
def main():
    _load_stats()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setmaingroup", set_main_cmd))
    app.add_handler(CommandHandler("addteam", add_team_cmd))
    app.add_handler(CommandHandler("removeteam", remove_team_cmd))
    app.add_handler(CommandHandler("listteam", list_team_cmd))
    app.add_handler(CommandHandler("clearseen", clearseen_cmd))

    # Analysis
    app.add_handler(CommandHandler("analiz", analiz_cmd))
    app.add_handler(CallbackQueryHandler(analiz_cb, pattern=r"^ANALYZE:"))

    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, driver_message_handler))

    log.info(
        "Watchdog 90s started. MAIN=%s Delay=%ss DUP_TTL=%ss WARN_ON_DUP=%s",
        MAIN_GROUP_ID, ALERT_DELAY_SECONDS, DUP_TTL_SECONDS, WARN_ON_DUP
    )
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

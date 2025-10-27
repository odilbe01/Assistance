# bot.py
import os
import re
import time
import asyncio
import logging
from typing import Dict, Optional, Set, Tuple

from telegram import Update, Message, ChatMember
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    ChatMemberHandler, filters
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
# Kalit: "PIN:<LOAD_ID>" -> (first_seen_epoch, first_group_id)
DUP_SEEN: Dict[str, Tuple[float, int]] = {}
# Qaysi (kalit, group_id) bo‘yicha allaqachon alert/forward qilingan — shuni eslab qolamiz
DUP_ALERTED: Set[Tuple[str, int]] = set()

# ---------------- PIN-only extractor ----------------
# Qattiq format: satr boshida (yoki bo'shliqlardan so‘ng)
#   📍 2#: 111X58WPC
# emoji ixtiyoriy; ":" o‘rniga "：" yoki "-" ham bo‘lishi mumkin
_PIN_LOAD_RE = re.compile(
    r"(?mi)^\s*(?:📍\s*)?(\d+)\s*#\s*[:：-]?\s*([A-Z0-9]{6,20})\b"
)

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
            try:
                await msg.forward(chat_id=MAIN_GROUP_ID)
            except Exception as e:
                log.warning("Forward failed: %s", e)
        finally:
            PENDING.pop(chat_id, None)

    task = asyncio.create_task(_job())
    PENDING[chat_id] = (task, msg)

def _purge_expired_duplicates() -> None:
    if not DUP_SEEN:
        return
    cutoff = time.time() - DUP_TTL_SECONDS
    to_del = [k for k, (ts, _) in DUP_SEEN.items() if ts < cutoff]
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
      - agar WARN_ON_DUP=1 bo‘lsa → MAIN’ga warning matni yuboriladi
      - har holda → asl xabar MAIN’ga forward qilinadi
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
            DUP_SEEN[key] = (time.time(), chat.id)
            continue

        _, first_gid = first
        if first_gid == chat.id:
            # shu guruh ichida ko‘rildi — e’tibor bermaymiz
            continue

        mark = (key, chat.id)
        if mark in DUP_ALERTED:
            continue
        DUP_ALERTED.add(mark)

        # Warning (ixtiyoriy)
        if WARN_ON_DUP:
            load_id = key.split(":", 1)[1]
            try:
                await context.bot.send_message(
                    chat_id=MAIN_GROUP_ID,
                    text=(
                        "⚠️ *POSSIBLE DUPLICATE IN MULTIPLE DRIVER GROUPS*\n"
                        f"📦 *Load ID:* `{load_id}`"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("PINFWD: warning send failed for %s", key)

        # Faqat forward (asl xabar)
        try:
            await msg.forward(chat_id=MAIN_GROUP_ID)
            log.info("PINFWD: forwarded duplicate %s from group_id=%s", key, chat.id)
        except Exception:
            # forward yiqilsa, keyinchalik yana urinishi uchun mark ni olib tashlaymiz
            DUP_ALERTED.discard(mark)
            log.exception("PINFWD: forward failed for %s", key)

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

    # 1) PIN-only duplicate forward — (bot xabarlari ham)
    try:
        await process_pin_duplicate_forward(context, msg)
    except Exception as e:
        log.warning("pin duplicate check failed: %s", e)

    # 2) Agar xabarni bot yuborgan bo‘lsa, shu yerda to‘xtaymiz (watchdog yo‘q)
    if msg.from_user and msg.from_user.is_bot:
        return

    # 3) TEAM a’zosi yozsa → 90s taymerni bekor qilamiz
    if is_team_user(update):
        await cancel_pending(chat.id)
        return

    # 4) Oddiy foydalanuvchi xabari → 90s watchdog
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
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setmaingroup", set_main_cmd))
    app.add_handler(CommandHandler("addteam", add_team_cmd))
    app.add_handler(CommandHandler("removeteam", remove_team_cmd))
    app.add_handler(CommandHandler("listteam", list_team_cmd))
    app.add_handler(CommandHandler("clearseen", clearseen_cmd))

    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, driver_message_handler))

    log.info(
        "Watchdog 90s started. MAIN=%s Delay=%ss DUP_TTL=%ss WARN_ON_DUP=%s",
        MAIN_GROUP_ID, ALERT_DELAY_SECONDS, DUP_TTL_SECONDS, WARN_ON_DUP
    )
    # drop_pending_updates=True  -> eski navbatni tashlab yuboradi
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

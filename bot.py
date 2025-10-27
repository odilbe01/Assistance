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

# ⬇️ DEFAULT ENDILIKDA 90 SEKUND
ALERT_DELAY_SECONDS = int(os.getenv("ALERT_DELAY_SECONDS", "90") or 90)

TEAM_USERNAMES: Set[str] = {u.strip().lower().lstrip("@") for u in TEAM_USERNAMES_ENV.split(",") if u.strip()}
TEAM_USER_IDS: Set[int] = set(int(x.strip()) for x in TEAM_USER_IDS_ENV.split(",") if x.strip().isdigit())
OWNER_IDS: Set[int] = set(int(x.strip()) for x in OWNER_IDS_ENV.split(",") if x.strip().isdigit())

# --- Duplicate detector settings ---
DUP_TTL_SECONDS = int(os.getenv("DUP_TTL_SECONDS", str(24 * 60 * 60)) or 86400)  # 24h TTL

# LOAD_ID -> (first_seen_epoch, first_group_id)
DUP_SEEN: Dict[str, Tuple[float, int]] = {}
# already alerted for (LOAD_ID, group_id) pairs to avoid spam
DUP_ALERTED: Set[Tuple[str, int]] = set()

# =================
# Logging
# =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("watchdog-90s")

# ============
# Globals
# ============
PENDING: Dict[int, Tuple[asyncio.Task, Message]] = {}

# ============
# Helpers
# ============
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
    return bool(user_id and (not OWNER_IDS or user_id in OWNER_IDS))

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

# ---------------- NEW: Only-📍pin-load extractor ----------------
# pattern: "📍 1#: 111X58WPC"  (emoji optional spaces) <num> # optional spaces : optional -  <LOADID>
_PIN_LOAD_RE = re.compile(
    r"(?:📍\s*)?(\d+)\s*#\s*[:：-]?\s*([A-Z0-9]{6,20})\b",
    re.IGNORECASE,
)

def _purge_expired_duplicates() -> None:
    if not DUP_SEEN:
        return
    cutoff = time.time() - DUP_TTL_SECONDS
    to_del = [k for k, (ts, _) in DUP_SEEN.items() if ts < cutoff]
    for k in to_del:
        DUP_SEEN.pop(k, None)
    if DUP_ALERTED:
        to_keep = {(lid, gid) for (lid, gid) in DUP_ALERTED if lid in DUP_SEEN}
        DUP_ALERTED.clear()
        DUP_ALERTED.update(to_keep)

def _extract_load_ids_from_pins(text: str) -> Set[str]:
    """Only return LOAD IDs that appear after the numbered 📍 pin lines."""
    if not text:
        return set()
    ids: Set[str] = set()
    for _, load_id in _PIN_LOAD_RE.findall(text):
        up = load_id.upper()
        # must be alphanumeric (mix recommended), but allow all as given by format
        ids.add(up)
    return ids

async def process_duplicate_check(context: ContextTypes.DEFAULT_TYPE, msg: Message):
    """
    Bir xil 📍 <n># : <LOAD_ID> ikki xil driver guruhida ko‘rilsa → MAIN ga faqat forward.
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
    found = _extract_load_ids_from_pins(text)
    if not found:
        return

    log.info("PIN-DUPCHK: found pin-load IDs=%s in group_id=%s", found, chat.id)

    for lid in found:
        first = DUP_SEEN.get(lid)
        if not first:
            DUP_SEEN[lid] = (time.time(), chat.id)
            continue

        _, first_gid = first
        if first_gid == chat.id:
            # shu guruhda ko‘rildi — e’tibor bermaymiz
            continue

        key = (lid, chat.id)
        if key in DUP_ALERTED:
            continue
        DUP_ALERTED.add(key)

        # Faqat forward qilish
        try:
            await msg.forward(chat_id=MAIN_GROUP_ID)
            log.info("PIN-DUPCHK: forwarded duplicate LOAD_ID=%s from group_id=%s to MAIN", lid, chat.id)
        except Exception:
            DUP_ALERTED.discard(key)
            log.exception("PIN-DUPCHK: forward failed for LOAD_ID=%s", lid)

# ================
# Commands
# ================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Watchdog is running.\n"
        "• Use /setmaingroup in your MAIN group.\n"
        "• /addteam @u1 @u2 or numeric IDs.\n"
        f"• Current delay: {ALERT_DELAY_SECONDS} seconds."
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_text(
        "Main: {}\nTeam usernames: {}\nTeam IDs: {}\nDelay: {}s".format(
            MAIN_GROUP_ID,
            ", ".join(sorted(TEAM_USERNAMES)) or "(none)",
            ", ".join(map(str, sorted(TEAM_USER_IDS))) or "(none)",
            ALERT_DELAY_SECONDS,
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
        if not t: continue
        if t.startswith("@"):
            t = t[1:]
        if t.isdigit():
            if int(t) in TEAM_USER_IDS: TEAM_USER_IDS.remove(int(t)); removed.append(t)
        else:
            tl = t.lower()
            if tl in TEAM_USERNAMES: TEAM_USERNAMES.remove(tl); removed.append("@"+tl)
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

# ===================
# Message handlers
# ===================
async def driver_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if MAIN_GROUP_ID is not None and chat.id == MAIN_GROUP_ID:
        return
    if not msg:
        return

    # 1) PIN-LOAD DUPLICATE CHECK — har doim (bot xabarlari ham)
    try:
        await process_duplicate_check(context, msg)
    except Exception as e:
        log.warning("duplicate check failed: %s", e)

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

# ============
# Entrypoint
# ============
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
    log.info("Watchdog 90s started. MAIN=%s Delay=%ss DUP_TTL=%ss", MAIN_GROUP_ID, ALERT_DELAY_SECONDS, DUP_TTL_SECONDS)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

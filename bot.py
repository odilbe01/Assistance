# Driver Response Watchdog Bot — 3‑minute escalation to a main group
# ---------------------------------------------------------------
# Monitors driver groups. If a NON‑TEAM member posts and no TEAM reply
# arrives in ALERT_DELAY_SECONDS (default 180s), it alerts the MAIN group
# with a header and forwards the original message.
#
# Env:
#   BOT_TOKEN            — required
#   MAIN_GROUP_ID        — optional (or use /setmaingroup)
#   TEAM_USERNAMES       — optional comma-separated, without @
#   TEAM_USER_IDS        — optional comma-separated numeric ids
#   OWNER_IDS            — optional comma-separated numeric ids
#   ALERT_DELAY_SECONDS  — optional, default 180
#
# Commands:
#   /start, /status, /setmaingroup, /addteam, /removeteam, /listteam
#
import os
import asyncio
import logging
from typing import Dict, Optional, Set, Tuple

from telegram import Update, Chat, Message, ChatMember
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    ChatMemberHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAIN_GROUP_ID_ENV = os.getenv("MAIN_GROUP_ID", "").strip()
TEAM_USERNAMES_ENV = os.getenv("TEAM_USERNAMES", "").strip()
TEAM_USER_IDS_ENV = os.getenv("TEAM_USER_IDS", "").strip()
OWNER_IDS_ENV = os.getenv("OWNER_IDS", "").strip()
ALERT_DELAY_SECONDS = int(os.getenv("ALERT_DELAY_SECONDS", "180") or 180)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env is required")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("watchdog-bot")

MAIN_GROUP_ID: Optional[int] = int(MAIN_GROUP_ID_ENV) if MAIN_GROUP_ID_ENV else None
TEAM_USERNAMES: Set[str] = {u.strip().lower() for u in TEAM_USERNAMES_ENV.split(",") if u.strip()}
TEAM_USER_IDS: Set[int] = set()
if TEAM_USER_IDS_ENV:
    for token in TEAM_USER_IDS_ENV.split(","):
        token = token.strip()
        if token.isdigit():
            TEAM_USER_IDS.add(int(token))

OWNER_IDS: Set[int] = set()
if OWNER_IDS_ENV:
    for token in OWNER_IDS_ENV.split(","):
        token = token.strip()
        if token.isdigit():
            OWNER_IDS.add(int(token))

# pending[(chat_id)] = (asyncio.Task, message)
PENDING: Dict[int, Tuple[asyncio.Task, Message]] = {}

def is_group(chat) -> bool:
    return chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

def is_main_group(chat_id: int) -> bool:
    return MAIN_GROUP_ID is not None and chat_id == MAIN_GROUP_ID

def username_key(name: Optional[str]) -> Optional[str]:
    return name.lower() if name else None

def is_team_member(update: Update) -> bool:
    user = update.effective_user
    if not user or user.is_bot:
        return False
    if user.id in TEAM_USER_IDS:
        return True
    ukey = username_key(user.username)
    if ukey and ukey in TEAM_USERNAMES:
        return True
    return False

def is_owner(user_id: Optional[int]) -> bool:
    return bool(user_id and (user_id in OWNER_IDS or not OWNER_IDS))

async def cancel_pending(chat_id: int):
    pair = PENDING.pop(chat_id, None)
    if pair:
        task, _ = pair
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

async def schedule_alert(context: ContextTypes.DEFAULT_TYPE, driver_chat, msg: Message):
    await cancel_pending(driver_chat.id)

    async def _job():
        try:
            await asyncio.sleep(ALERT_DELAY_SECONDS)
            if MAIN_GROUP_ID is None:
                logger.warning("MAIN_GROUP_ID not set; skipping alert.")
                return

            title = driver_chat.title or "(no title)"
            sender_name = msg.from_user.full_name if msg.from_user else "(unknown)"
            snippet = msg.text or msg.caption or "(non-text message)"
            header = f"🔔 *No team reply in {ALERT_DELAY_SECONDS // 60} minutes*\\n*{title}*\\nFrom: {sender_name}\\n\\n{snippet}"

            await context.bot.send_message(
                chat_id=MAIN_GROUP_ID,
                text=header,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            try:
                await msg.forward(chat_id=MAIN_GROUP_ID)
            except Exception as e:
                logger.warning("Forward failed: %s", e)
        finally:
            PENDING.pop(driver_chat.id, None)

    task = asyncio.create_task(_job())
    PENDING[driver_chat.id] = (task, msg)

# --- Handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Watchdog bot is running.\\n"
        "Add me to driver groups and to your MAIN group.\\n"
        "Use /setmaingroup in your MAIN group to save it.\\n"
        "Use /addteam @user1 @user2 to add responders."
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    team_usernames = ", ".join(sorted(TEAM_USERNAMES)) or "(none)"
    team_ids = ", ".join(str(x) for x in sorted(TEAM_USER_IDS)) or "(none)"
    await update.message.reply_text(
        f"MAIN_GROUP_ID: {MAIN_GROUP_ID}\\n"
        f"TEAM_USERNAMES: {team_usernames}\\n"
        f"TEAM_USER_IDS: {team_ids}\\n"
        f"ALERT_DELAY_SECONDS: {ALERT_DELAY_SECONDS}"
    )

async def set_main_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        await update.message.reply_text("Run this inside your MAIN group.")
        return
    if not is_owner(user.id):
        await update.message.reply_text("Not authorized.")
        return
    global MAIN_GROUP_ID
    MAIN_GROUP_ID = chat.id
    await update.message.reply_text(f"MAIN group set to: {MAIN_GROUP_ID}")

async def add_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    added = []
    for token in context.args:
        tok = token.strip()
        if not tok:
            continue
        if tok.startswith("@"):
            tok = tok[1:]
        if tok.isdigit():
            TEAM_USER_IDS.add(int(tok))
            added.append(tok)
        else:
            TEAM_USERNAMES.add(tok.lower())
            added.append("@" + tok)
    await update.message.reply_text("Added to TEAM: " + (", ".join(added) if added else "(none)"))

async def remove_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    removed = []
    for token in context.args:
        tok = token.strip()
        if not tok:
            continue
        if tok.startswith("@"):
            tok = tok[1:]
        if tok.isdigit():
            try:
                TEAM_USER_IDS.remove(int(tok)); removed.append(tok)
            except KeyError:
                pass
        else:
            try:
                TEAM_USERNAMES.remove(tok.lower()); removed.append("@" + tok)
            except KeyError:
                pass
    await update.message.reply_text("Removed from TEAM: " + (", ".join(removed) if removed else "(none)"))

async def list_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    team_usernames = ", ".join(sorted(TEAM_USERNAMES)) or "(none)"
    team_ids = ", ".join(str(x) for x in sorted(TEAM_USER_IDS)) or "(none)"
    await update.message.reply_text(f"TEAM usernames: {team_usernames}\\nTEAM ids: {team_ids}")

async def driver_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        return
    if is_main_group(chat.id):
        return
    if not msg or not user or user.is_bot:
        return
    if is_team_member(update):
        await cancel_pending(chat.id)
        return
    await schedule_alert(context, chat, msg)

async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    status = update.my_chat_member.new_chat_member.status
    if status in (ChatMember.KICKED, ChatMember.LEFT):
        await cancel_pending(chat.id)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setmaingroup", set_main_group_cmd))
    app.add_handler(CommandHandler("addteam", add_team_cmd))
    app.add_handler(CommandHandler("removeteam", remove_team_cmd))
    app.add_handler(CommandHandler("listteam", list_team_cmd))
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, driver_message_handler))
    logger.info("Watchdog bot started. Main group: %s", MAIN_GROUP_ID)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

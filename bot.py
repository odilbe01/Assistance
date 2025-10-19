
import os
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_GROUP_ID = int(os.getenv("MAIN_GROUP_ID", 0))
TEAM_USERNAMES = [x.lower().replace("@", "") for x in os.getenv("TEAM_USERNAMES", "").split(",") if x]
ALERT_DELAY_SECONDS = int(os.getenv("ALERT_DELAY_SECONDS", 120))

pending_messages = {}

async def setmaingroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAIN_GROUP_ID
    MAIN_GROUP_ID = update.effective_chat.id
    await update.message.reply_text(f"✅ Main group set to: {MAIN_GROUP_ID}")

async def addteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TEAM_USERNAMES
    TEAM_USERNAMES = [x.lower().replace("@", "") for x in context.args]
    await update.message.reply_text(f"✅ Team updated: {', '.join(TEAM_USERNAMES)}")

async def listteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"👥 Team: {', '.join(TEAM_USERNAMES) or 'empty'}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user.username.lower() if update.message.from_user.username else None
    chat_id = update.message.chat_id
    text = update.message.text or "(no text)"
    if user and user not in TEAM_USERNAMES:
        msg_key = f"{chat_id}:{update.message.message_id}"
        pending_messages[msg_key] = datetime.now()
        await asyncio.sleep(ALERT_DELAY_SECONDS)
        last_msgs = pending_messages.get(msg_key)
        if last_msgs and (datetime.now() - last_msgs).total_seconds() >= ALERT_DELAY_SECONDS:
            if MAIN_GROUP_ID:
                await context.bot.send_message(
                    MAIN_GROUP_ID,
                    f"📢 From group: {update.effective_chat.title}\nUser @{user}:\n{text}"
                )
            pending_messages.pop(msg_key, None)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setmaingroup", setmaingroup))
    app.add_handler(CommandHandler("addteam", addteam))
    app.add_handler(CommandHandler("listteam", listteam))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("✅ Watchdog bot started.")
    # IMPORTANT: use synchronous run_polling to avoid 'event loop is already running'
    app.run_polling()

if __name__ == "__main__":
    main()

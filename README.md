
# Driver Watchdog Bot

A Telegram bot that monitors driver group messages.  
If a non-team member sends a message and no team member replies within 3 minutes, the bot forwards it to the main group.

## Setup (Render)
1. Push this repo to GitHub.
2. Create a **Background Worker** service in Render.
3. Environment variables:
   - `BOT_TOKEN`: your Telegram bot token
   - `MAIN_GROUP_ID`: Telegram group ID where alerts are sent
   - `TEAM_USERNAMES`: comma-separated usernames (no @)
   - `ALERT_DELAY_SECONDS`: seconds before forwarding (default 180)
4. Deploy. Done!

## Commands
- `/setmaingroup` → set current chat as main alert group.
- `/addteam @user1 @user2 ...` → set your team.
- `/listteam` → show team list.

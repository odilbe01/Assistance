# Driver Response Watchdog Bot (Telegram)

Monitors driver groups. If a non-team member posts and no team reply appears within 3 minutes,
the bot alerts the MAIN group and forwards the message.

## Quick Start (local)

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN=123456:ABC...                 # or set in your shell
python bot.py
```

In the MAIN group chat, run `/setmaingroup`, then add team:
```
/addteam @user1 @user2 123456789
```

## Deploy to Render

1. Push this repo to GitHub.
2. In Render: **New +** → **Blueprint** → connect repo → it will read `render.yaml`.
3. Add environment variables in Render dashboard:
   - `BOT_TOKEN` (required)
   - `MAIN_GROUP_ID` (optional; or use /setmaingroup after deploy)
   - `TEAM_USERNAMES`, `TEAM_USER_IDS`, `OWNER_IDS` (optional)
   - `ALERT_DELAY_SECONDS` = `180` (optional)
4. Deploy. Logs should show `Watchdog bot started ...`.

## Commands
- `/start` — basic help
- `/status` — print config (owners only)
- `/setmaingroup` — run inside MAIN group to save its chat id
- `/addteam`, `/removeteam`, `/listteam` — manage team

## Notes
- The bot ignores the MAIN group for triggers.
- Messages by a TEAM user cancel pending alerts for that driver chat.
- Works with text and non-text (forward is attempted).

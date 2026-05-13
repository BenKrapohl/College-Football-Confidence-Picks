# CFCP Discord Bot — Setup Guide

## Requirements
- Python 3.11 or later (download from python.org — check "Add to PATH" during install)
- A Discord bot application with a token

---

## First-time setup

### 1. Create your Discord bot
1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it "CFCP Bot"
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - Server Members Intent
   - Message Content Intent
5. Click **Reset Token** → copy the token (you'll need this)
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Manage Channels`, `Send Messages`, `Embed Links`,
     `Read Message History`, `Add Reactions`, `Manage Messages`, `Pin Messages`
7. Copy the generated URL → open in browser → invite bot to your server

### 2. Enable Developer Mode in Discord
User Settings → Advanced → Developer Mode: ON
This lets you right-click to copy IDs.

### 3. Get your IDs
- **Guild ID**: Right-click your server name → Copy Server ID
- **Admin Role ID**: Right-click your admin role in Server Settings → Copy Role ID

### 4. Install Python dependencies
Open Command Prompt in the `cfcp_bot` folder and run:
```
pip install -r requirements.txt
```

### 5. Configure your .env file
Copy `.env.example` to `.env` and fill in your three values:
```
DISCORD_TOKEN=your_token_here
GUILD_ID=123456789012345678
ADMIN_ROLE_ID=123456789012345678
```

### 6. Run the bot
```
python bot.py
```

The bot will:
- Initialize the SQLite database (`data/cfcp.db`)
- Create a **CFCP** category in your server with all five channels
- Post and pin the panel messages in each channel
- Log all activity to `data/cfcp_bot.log`

---

## Running the bot persistently on Windows 10
To keep the bot running after you close the terminal, use Windows Task Scheduler
or create a simple batch file:

**run_bot.bat**
```batch
@echo off
cd /d D:\path\to\cfcp_bot
python bot.py
pause
```

For a proper background service, install `NSSM` (Non-Sucking Service Manager)
from https://nssm.cc and register the bot as a Windows service.

---

## Project structure
```
cfcp_bot/
├── bot.py              ← Entry point
├── config.py           ← All constants and settings
├── database.py         ← Schema, helpers, DB access
├── requirements.txt
├── .env                ← Your secrets (never commit this)
├── data/
│   ├── cfcp.db         ← SQLite database (auto-created)
│   └── cfcp_bot.log    ← Log file (auto-created)
├── utils/
│   ├── espn.py         ← ESPN API client
│   ├── embeds.py       ← Discord embed builders
│   └── time_utils.py   ← ET timezone helpers
└── cogs/
    └── setup.py        ← Channel/panel initialization (Milestone 1)
    # admin.py          ← Week management, results (Milestone 2)
    # picks.py          ← Pick submission flow (Milestone 3)
    # scoring.py        ← Auto-scoring, forfeiture (Milestone 4)
    # notifications.py  ← DM scheduler (Milestone 5)
    # stats.py          ← History, streaks, exports (Milestone 6)
```

---

## Channel overview
| Channel | Visible to | Purpose |
|---|---|---|
| `#cfcp-admin` | Admins only | Admin control panel |
| `#cfcp-logs` | Admins only | Bot logs and automated events |
| `#cfcp-picks` | Everyone | Submit/edit picks, register |
| `#cfcp-games` | Everyone (read-only) | Live per-matchup embeds |
| `#cfcp-standings` | Everyone (read-only) | Season and weekly leaderboard |

import os
from dotenv import load_dotenv

load_dotenv()

# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN", "")
GUILD_ID            = int(os.getenv("GUILD_ID", "0"))
ADMIN_ROLE_ID       = int(os.getenv("ADMIN_ROLE_ID", "0"))

# ── Channel names (bot creates these on first run) ────────────────────────────
CHANNEL_ADMIN       = "cfcp-admin"
CHANNEL_LOGS        = "cfcp-logs"
CHANNEL_PICKS       = "cfcp-picks"
CHANNEL_GAMES       = "cfcp-games"
CHANNEL_STANDINGS   = "cfcp-standings"

# ── Backups ───────────────────────────────────────────────────────────────────
# Where the bot will locally store database copies
BACKUP_DIR          = os.path.join(os.path.dirname(__file__), "backups")

# ── ESPN API ──────────────────────────────────────────────────────────────────
ESPN_SCOREBOARD_URL = (
    "http://site.api.espn.com/apis/site/v2/sports/football/"
    "college-football/scoreboard?limit=1000"
)
ESPN_RANKINGS_URL   = (
    "http://site.api.espn.com/apis/site/v2/sports/football/"
    "college-football/rankings"
)

# ── Poll types ────────────────────────────────────────────────────────────────
POLL_AP             = "ap"
POLL_CFP            = "cfp"
POLL_AP_LABEL       = "AP Top 25"
POLL_CFP_LABEL      = "CFP Rankings"

# ── Season ────────────────────────────────────────────────────────────────────
SEASON_YEAR         = 2026

# ── Refresh intervals (seconds) ──────────────────────────────────────────────
REFRESH_INTERVAL = 900      # scheduler tick every 15 min

# ── Bot behavior ──────────────────────────────────────────────────────────────
PICKS_REVEAL_DEFAULT    = True   # show who picked what after lock
# FIX #26: Single source of truth — was duplicated in scoring.py
ESPN_FINAL_GRACE_SECS   = 180    # wait 3 min after 'post' before scoring
STALE_WEEK_HOURS        = 48     # log warning if week not loaded

# ── Colors (Discord embed accent colors as integers) ─────────────────────────
COLOR_PURPLE        = 0x7C3AED
COLOR_TEAL          = 0x0F6E56
COLOR_AMBER         = 0xBA7517
COLOR_CORAL         = 0x993C1D
COLOR_GRAY          = 0x5F5E5A
COLOR_GREEN         = 0x3B6D11
COLOR_RED           = 0xA32D2D
COLOR_BLUE          = 0x185FA5

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE            = "America/New_York"


# ── FIX #17: Startup validation ───────────────────────────────────────────────
def validate_config() -> list[str]:
    """
    Returns a list of error strings. Empty list = config is valid.
    Call this before starting the bot so misconfigurations fail fast.
    """
    errors = []
    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN is not set in .env")
    if GUILD_ID == 0:
        errors.append("GUILD_ID is not set or invalid in .env")
    return errors
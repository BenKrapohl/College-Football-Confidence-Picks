"""
FIX #25: Centralized shared helpers.

Previously _log(), _is_admin(), and _latest_week() were copy-pasted
verbatim into picks.py, scoring.py, stats.py, admin.py, and
notifications.py. All cogs now import from here instead.
"""
from __future__ import annotations
import discord
from discord.ext import commands
import logging

from config import ADMIN_ROLE_ID
from database import config_get, get_active_season, get_latest_week
from utils.embeds import log_embed

log = logging.getLogger(__name__)


# ── Admin check ───────────────────────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    """Return True if the interaction user holds the configured admin role."""
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)


# ── Logging helper ────────────────────────────────────────────────────────────

async def log_to_channel(
    bot: commands.Bot,
    description: str,
    title: str = "CFCP",
    level: str = "info",
) -> None:
    """Send a formatted log embed to #cfcp-logs."""
    ch_id = config_get("channel_logs")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=log_embed(title, description, level))
        except discord.HTTPException as exc:
            log.warning(f"Could not send log embed: {exc}")


# ── Current week resolution ───────────────────────────────────────────────────

def resolve_current_week():
    """
    FIX #7: Returns (season, week) using date-range matching so the bot
    stays on the correct week even when a future week is loaded early.
    Falls back to the latest loaded week if no date-range match.
    """
    from database import get_current_week
    season = get_active_season()
    if not season:
        return None, None
    week = get_current_week(season["id"])
    return season, week


def resolve_latest_week():
    """
    Returns (season, week) using the most recently inserted week.
    Used by admin operations that always want to act on the last-loaded week.
    """
    season = get_active_season()
    if not season:
        return None, None
    week = get_latest_week(season["id"])
    return season, week


# ── DM helper ─────────────────────────────────────────────────────────────────

async def send_dm(bot: commands.Bot, discord_id: str, message: str) -> bool:
    """
    Send a DM. Returns True on success, False if DMs are closed.
    Centralised here so all cogs share the same error-handling logic.
    """
    try:
        user = await bot.fetch_user(int(discord_id))
        await user.send(message)
        return True
    except discord.Forbidden:
        return False
    except Exception as exc:
        log.warning(f"DM to {discord_id} failed unexpectedly: {exc}")
        return False

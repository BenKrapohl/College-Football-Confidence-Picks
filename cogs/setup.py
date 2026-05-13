from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands
from config import (
    CHANNEL_ADMIN, CHANNEL_LOGS, CHANNEL_PICKS,
    CHANNEL_GAMES, CHANNEL_STANDINGS,
    ADMIN_ROLE_ID, COLOR_GRAY,
)
from database import config_get, config_set, get_active_season
from utils.embeds import (admin_panel_embed, picks_hub_embed,
                          standings_season_embed, standings_week_embed,
                          log_embed)
import logging

log = logging.getLogger(__name__)


async def get_or_create_channel(
    guild: discord.Guild,
    name: str,
    category: Optional[discord.CategoryChannel] = None,
    overwrites: Optional[dict] = None,
    topic: str = "",
) -> discord.TextChannel:
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing:
        return existing
    channel = await guild.create_text_channel(
        name=name,
        category=category,
        overwrites=overwrites or {},
        topic=topic,
    )
    log.info(f"Created channel #{name}")
    return channel


async def setup_channels(bot: commands.Bot, guild: discord.Guild):
    """
    Ensure all five CFCP channels exist and have pinned panel messages.
    Stores message IDs in bot_config for later editing.
    Called once on startup and idempotent thereafter.
    """
    admin_role  = guild.get_role(ADMIN_ROLE_ID)
    everyone    = guild.default_role

    # Permission overwrites
    admin_only = {
        everyone:   discord.PermissionOverwrite(read_messages=False),
        guild.me:   discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if admin_role:
        admin_only[admin_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True
        )

    read_only_public = {
        everyone:  discord.PermissionOverwrite(read_messages=True, send_messages=False),
        guild.me:  discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }

    picks_public = {
        everyone:  discord.PermissionOverwrite(read_messages=True, send_messages=False),
        guild.me:  discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }

    # Find or create a CFCP category
    category = discord.utils.get(guild.categories, name="CFCP")
    if not category:
        category = await guild.create_category("CFCP")

    # Create channels
    admin_ch    = await get_or_create_channel(guild, CHANNEL_ADMIN,    category, admin_only,       "Admin controls — do not chat here")
    logs_ch     = await get_or_create_channel(guild, CHANNEL_LOGS,     category, admin_only,       "Bot logs and automated events")
    picks_ch    = await get_or_create_channel(guild, CHANNEL_PICKS,    category, picks_public,     "Submit and manage your picks here")
    games_ch    = await get_or_create_channel(guild, CHANNEL_GAMES,    category, read_only_public, "Live matchup tracker")
    standing_ch = await get_or_create_channel(guild, CHANNEL_STANDINGS,category, read_only_public, "Season and weekly leaderboards")

    # Store channel IDs
    config_set("channel_admin",    str(admin_ch.id))
    config_set("channel_logs",     str(logs_ch.id))
    config_set("channel_picks",    str(picks_ch.id))
    config_set("channel_games",    str(games_ch.id))
    config_set("channel_standings",str(standing_ch.id))

    # ── Admin panel message ──────────────────────────────────────────────────
    from cogs.admin import AdminPanelView
    await _ensure_pinned_message(
        bot, admin_ch, "panel_msg_admin",
        embed=admin_panel_embed(),
        view=AdminPanelView(),
    )

    # ── Picks hub message ────────────────────────────────────────────────────
    from cogs.picks import PicksHubView
    await _ensure_pinned_message(
        bot, picks_ch, "panel_msg_picks",
        embed=picks_hub_embed(),
        view=PicksHubView(),
    )

    # ── Standings messages ───────────────────────────────────────────────────
    season = get_active_season()
    year   = season["year"] if season else 2026

    await _ensure_pinned_message(
        bot, standing_ch, "panel_msg_standings_season",
        embed=standings_season_embed([], year),
    )
    await _ensure_pinned_message(
        bot, standing_ch, "panel_msg_standings_week",
        embed=standings_week_embed([], 1),
    )

    log.info("All CFCP channels and panel messages are ready.")
    await _log(bot, "Bot started — all channels verified.", level="success")


async def _ensure_pinned_message(
    bot: commands.Bot,
    channel: discord.TextChannel,
    config_key: str,
    embed: discord.Embed,
    view: Optional[discord.ui.View] = None,
) -> discord.Message:
    """
    Find or create the panel message for this channel.
    Priority:
      1. Message ID stored in DB — fetch and edit it.
      2. DB wiped but message still exists — scan pinned messages for one
         sent by the bot, reuse it and save its ID back to DB.
      3. Nothing found — create a new message and pin it.
    """
    msg_id = config_get(config_key)

    # ── 1. Try stored ID ──────────────────────────────────────────────────────
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return msg
        except discord.NotFound:
            config_set(config_key, "")

    # ── 2. Scan pinned messages for an existing bot panel ─────────────────────
    try:
        pins = await channel.pins()
        for pinned in pins:
            if pinned.author == channel.guild.me and pinned.embeds:
                # Found a pinned bot message — reuse it
                await pinned.edit(embed=embed, view=view)
                config_set(config_key, str(pinned.id))
                log.info(
                    f"Reconnected to existing panel in #{channel.name} "
                    f"(msg {pinned.id})"
                )
                return pinned
    except discord.Forbidden:
        pass

    # ── 3. Create fresh message ───────────────────────────────────────────────
    if view is not None:
        msg = await channel.send(embed=embed, view=view)
    else:
        msg = await channel.send(embed=embed)
    try:
        await msg.pin()
        # Delete the "pinned a message" system notification Discord auto-posts
        async for m in channel.history(limit=5):
            if m.type == discord.MessageType.pins_add:
                await m.delete()
                break
    except discord.Forbidden:
        pass
    config_set(config_key, str(msg.id))
    log.info(f"Created new panel message in #{channel.name}")
    return msg


async def _log(bot: commands.Bot, description: str, title: str = "System", level: str = "info"):
    log_ch_id = config_get("channel_logs")
    if not log_ch_id:
        return
    ch = bot.get_channel(int(log_ch_id))
    if isinstance(ch, discord.TextChannel):
        await ch.send(embed=log_embed(title, description, level))


# ── REFRESH HELPERS (called by other cogs) ────────────────────────────────────

async def refresh_admin_panel(bot: commands.Bot, guild: discord.Guild,
                              week=None, season=None, poll_type: str = "ap",
                              picks_reveal: bool = True) -> None:
    ch_id  = config_get("channel_admin")
    msg_id = config_get("panel_msg_admin")
    if not ch_id or not msg_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return
    from cogs.admin import AdminPanelView
    try:
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(
            embed=admin_panel_embed(week, season, poll_type, picks_reveal),
            view=AdminPanelView(),
        )
    except discord.NotFound:
        config_set("panel_msg_admin", "")


async def refresh_picks_hub(bot: commands.Bot, week=None,
                            games: Optional[list] = None,
                            players: Optional[list] = None) -> None:
    ch_id  = config_get("channel_picks")
    msg_id = config_get("panel_msg_picks")
    if not ch_id or not msg_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return
    from cogs.picks import PicksHubView
    try:
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(
            embed=picks_hub_embed(week, games, players),
            view=PicksHubView(),
        )
    except discord.NotFound:
        config_set("panel_msg_picks", "")


async def refresh_standings(bot: commands.Bot, season_rows: list,
                            week_rows: list, season_year: int,
                            week_number: int) -> None:
    ch_id       = config_get("channel_standings")
    season_id   = config_get("panel_msg_standings_season")
    week_msg_id = config_get("panel_msg_standings_week")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return
    for msg_id, embed in [
        (season_id,   standings_season_embed(season_rows, season_year)),
        (week_msg_id, standings_week_embed(week_rows, week_number)),
    ]:
        if not msg_id:
            continue
        try:
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
        except discord.NotFound:
            pass


# ── STUB VIEWS (full implementations in their own cogs) ───────────────────────

async def setup(bot: commands.Bot) -> None:
    pass

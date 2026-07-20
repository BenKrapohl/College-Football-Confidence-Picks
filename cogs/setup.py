from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands
import logging

from config import (CHANNEL_ADMIN, CHANNEL_LOGS, CHANNEL_PICKS,
                    CHANNEL_GAMES, CHANNEL_STANDINGS, ADMIN_ROLE_ID,
                    POLL_AP)
from database import config_get, config_set, get_active_season, get_latest_week

log = logging.getLogger(__name__)

async def _get_or_create_channel(guild: discord.Guild, name: str,
                                 admin_only: bool = False) -> discord.TextChannel:
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing:
        return existing

    overwrites = {}
    if admin_only:
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=False
        )
        admin_role = guild.get_role(ADMIN_ROLE_ID)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

    ch = await guild.create_text_channel(name, overwrites=overwrites)
    log.info(f"Created channel #{name} in {guild.name}")
    return ch

async def setup_channels(bot: commands.Bot, guild: discord.Guild) -> None:
    admin_ch     = await _get_or_create_channel(guild, CHANNEL_ADMIN, admin_only=True)
    logs_ch      = await _get_or_create_channel(guild, CHANNEL_LOGS, admin_only=True)
    picks_ch     = await _get_or_create_channel(guild, CHANNEL_PICKS)
    games_ch     = await _get_or_create_channel(guild, CHANNEL_GAMES)
    standings_ch = await _get_or_create_channel(guild, CHANNEL_STANDINGS)

    await config_set("channel_admin",     str(admin_ch.id))
    await config_set("channel_logs",      str(logs_ch.id))
    await config_set("channel_picks",     str(picks_ch.id))
    await config_set("channel_games",     str(games_ch.id))
    await config_set("channel_standings", str(standings_ch.id))

    if not await config_get("poll_type"):
        await config_set("poll_type", POLL_AP)
    if not await config_get("picks_reveal"):
        await config_set("picks_reveal", "1")

    await _ensure_admin_panel(bot, admin_ch)
    await _ensure_picks_hub(bot, picks_ch)
    await _ensure_standings_panels(bot, standings_ch)


async def _ensure_admin_panel(bot: commands.Bot, channel: discord.TextChannel) -> None:
    from cogs.admin import AdminPanelView
    from utils.embeds import admin_panel_embed

    msg_id = await config_get("admin_panel_msg_id")
    season = await get_active_season()
    week   = await get_latest_week(season["id"]) if season else None
    poll_type = await config_get("poll_type", POLL_AP)
    picks_reveal = await config_get("picks_reveal", "1") == "1"

    embed = admin_panel_embed(
        week=dict(week) if week else None,
        season=dict(season) if season else None,
        poll_type=poll_type,
        picks_reveal=picks_reveal,
    )
    view = AdminPanelView()

    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await channel.send(embed=embed, view=view)
    await config_set("admin_panel_msg_id", str(msg.id))


async def refresh_admin_panel(bot: commands.Bot) -> None:
    ch_id = await config_get("channel_admin")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if isinstance(ch, discord.TextChannel):
        await _ensure_admin_panel(bot, ch)


async def _ensure_picks_hub(bot: commands.Bot, channel: discord.TextChannel) -> None:
    from cogs.picks import PicksHubView
    from utils.embeds import picks_hub_embed

    msg_id = await config_get("picks_hub_msg_id")
    season = await get_active_season()
    week   = await get_latest_week(season["id"]) if season else None

    embed = picks_hub_embed(week=dict(week) if week else None)
    view  = PicksHubView()

    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await channel.send(embed=embed, view=view)
    await config_set("picks_hub_msg_id", str(msg.id))


async def refresh_picks_hub(bot: commands.Bot, week: Optional[dict] = None,
                            games: Optional[list] = None,
                            players: Optional[list] = None) -> None:
    ch_id = await config_get("channel_picks")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    from cogs.picks import PicksHubView
    from utils.embeds import picks_hub_embed

    msg_id = await config_get("picks_hub_msg_id")
    embed = picks_hub_embed(week=week, games=games, players=players)
    view  = PicksHubView()

    if msg_id:
        try:
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await ch.send(embed=embed, view=view)
    await config_set("picks_hub_msg_id", str(msg.id))


async def _ensure_standings_panels(bot: commands.Bot,
                                   channel: discord.TextChannel) -> None:
    from utils.embeds import standings_week_embed, standings_season_embed
    from database import (get_active_season, get_season_leaderboard,
                          get_week_leaderboard, get_latest_week)

    season = await get_active_season()
    season_rows = await get_season_leaderboard(season["id"]) if season else []
    season_embed = standings_season_embed(
        season_rows, season["year"] if season else 0
    )

    week_embed = standings_week_embed([], 0)
    if season:
        week = await get_latest_week(season["id"])
        if week:
            week_rows = await get_week_leaderboard(week["id"])
            week_embed = standings_week_embed(week_rows, week["week_number"])

    week_msg_id   = await config_get("standings_week_msg_id")
    season_msg_id = await config_get("standings_season_msg_id")

    if week_msg_id:
        try:
            msg = await channel.fetch_message(int(week_msg_id))
            await msg.edit(embed=week_embed)
        except (discord.NotFound, discord.HTTPException):
            msg = await channel.send(embed=week_embed)
            await config_set("standings_week_msg_id", str(msg.id))
    else:
        msg = await channel.send(embed=week_embed)
        await config_set("standings_week_msg_id", str(msg.id))

    if season_msg_id:
        try:
            msg = await channel.fetch_message(int(season_msg_id))
            await msg.edit(embed=season_embed)
        except (discord.NotFound, discord.HTTPException):
            msg = await channel.send(embed=season_embed)
            await config_set("standings_season_msg_id", str(msg.id))
    else:
        msg = await channel.send(embed=season_embed)
        await config_set("standings_season_msg_id", str(msg.id))


async def refresh_standings(bot: commands.Bot, week_embed: discord.Embed,
                            season_embed: discord.Embed) -> None:
    ch_id = await config_get("channel_standings")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    week_msg_id   = await config_get("standings_week_msg_id")
    season_msg_id = await config_get("standings_season_msg_id")

    if week_msg_id:
        try:
            msg = await ch.fetch_message(int(week_msg_id))
            await msg.edit(embed=week_embed)
        except (discord.NotFound, discord.HTTPException):
            msg = await ch.send(embed=week_embed)
            await config_set("standings_week_msg_id", str(msg.id))
    else:
        msg = await ch.send(embed=week_embed)
        await config_set("standings_week_msg_id", str(msg.id))

    if season_msg_id:
        try:
            msg = await ch.fetch_message(int(season_msg_id))
            await msg.edit(embed=season_embed)
        except (discord.NotFound, discord.HTTPException):
            msg = await ch.send(embed=season_embed)
            await config_set("standings_season_msg_id", str(msg.id))
    else:
        msg = await ch.send(embed=season_embed)
        await config_set("standings_season_msg_id", str(msg.id))


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
    log.info("SetupCog loaded.")
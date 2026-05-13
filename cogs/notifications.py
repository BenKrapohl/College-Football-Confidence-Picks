from __future__ import annotations
import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from database import (get_db, config_get, get_active_season,
                      get_all_active_players, get_unpicked_games_for_player)
from utils.embeds import log_embed
from utils.time_utils import (now_et, group_games_by_day,
                               first_kickoff_of_day, format_time_et,
                               seconds_until, countdown_label)

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _log(bot: commands.Bot, description: str,
               title: str = "Notifications", level: str = "info") -> None:
    ch_id = config_get("channel_logs")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if isinstance(ch, discord.TextChannel):
        await ch.send(embed=log_embed(title, description, level))


def _notif_already_sent(player_id: int, week_id: int,
                        game_day: str, notif_type: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """SELECT id FROM notifications_sent
               WHERE player_id=? AND week_id=? AND game_day=? AND notif_type=?""",
            (player_id, week_id, game_day, notif_type)
        ).fetchone()
    return row is not None


def _mark_sent(player_id: int, week_id: int,
               game_day: str, notif_type: str) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO notifications_sent
               (player_id, week_id, game_day, notif_type)
               VALUES (?,?,?,?)""",
            (player_id, week_id, game_day, notif_type)
        )


def _get_current_week():
    season = get_active_season()
    if not season:
        return None, None
    with get_db() as conn:
        week = conn.execute(
            "SELECT * FROM weeks WHERE season_id=? ORDER BY week_number DESC LIMIT 1",
            (season["id"],)
        ).fetchone()
    return season, week


async def _send_dm(bot: commands.Bot, discord_id: str,
                   message: str) -> bool:
    """Send a DM. Returns True on success, False if DMs are disabled."""
    try:
        user = await bot.fetch_user(int(discord_id))
        await user.send(message)
        return True
    except discord.Forbidden:
        return False
    except Exception as exc:
        log.warning(f"DM to {discord_id} failed: {exc}")
        return False


# ── Notification: week open ────────────────────────────────────────────────────

async def notify_week_open(bot: commands.Bot, week_id: int,
                           week_number: int, game_count: int) -> None:
    """
    Fire once when admin loads a new week.
    Sends a single DM to all active players with DM notifications enabled.
    """
    players = get_all_active_players()
    sent = 0

    with get_db() as conn:
        games = conn.execute(
            "SELECT kickoff_time FROM games WHERE week_id=? ORDER BY kickoff_time",
            (week_id,)
        ).fetchall()

    first_kick = ""
    if games:
        first_kick = format_time_et(
            datetime.fromisoformat(games[0]["kickoff_time"])
        )

    for player in players:
        if not player["dm_notifications"]:
            continue
        if _notif_already_sent(player["id"], week_id, "all", "week_open"):
            continue

        msg = (
            f"🏈 **Week {week_number} picks are now open!**\n\n"
            f"**{game_count} games** are available to pick this week.\n"
            f"First kickoff: **{first_kick}**\n\n"
            f"Picks lock at each game's individual kickoff time — "
            f"head to #cfcp-picks to submit yours!"
        )
        success = await _send_dm(bot, player["discord_id"], msg)
        if success:
            _mark_sent(player["id"], week_id, "all", "week_open")
            sent += 1

    await _log(
        bot,
        f"Week {week_number} open notification sent to {sent} players.",
        title="Week open DMs", level="info",
    )


# ── Notification: 24hr and 30min warnings ─────────────────────────────────────

async def run_notification_check(bot: commands.Bot) -> None:
    """
    Called every 15 minutes by the scheduler.
    For each player, for each calendar day group of games:
      - If within 24hr of first kickoff that day AND not yet sent → send 24hr DM
      - If within 30min of first kickoff that day AND not yet sent → send 30min DM
    Only fires if the player has unpicked games in that day group.
    """
    season, week = _get_current_week()
    if not week:
        return

    with get_db() as conn:
        all_games = conn.execute(
            """SELECT * FROM games WHERE week_id=? AND status='scheduled'
               ORDER BY kickoff_time""",
            (week["id"],)
        ).fetchall()

    if not all_games:
        return

    # Group all scheduled games by ET calendar day
    day_groups = group_games_by_day([dict(g) for g in all_games])
    players    = get_all_active_players()

    for player in players:
        if not player["dm_notifications"]:
            continue
        if player["status"] != "active":
            continue

        # Get this player's unpicked scheduled games
        unpicked = get_unpicked_games_for_player(player["id"], week["id"])
        unpicked_ids = {g["id"] for g in unpicked}

        for day_str, day_games in day_groups.items():
            # Filter to only games this player hasn't picked yet
            unpicked_today = [g for g in day_games if g["id"] in unpicked_ids]
            if not unpicked_today:
                continue

            first_kick   = first_kickoff_of_day(day_games)
            secs_to_kick = seconds_until(first_kick.isoformat())

            # ── 24hr window ──────────────────────────────────────────────────
            if 0 < secs_to_kick <= 86400:
                if not _notif_already_sent(
                    player["id"], week["id"], day_str, "24hr"
                ):
                    msg = _build_24hr_message(
                        week["week_number"], day_str,
                        unpicked_today, first_kick
                    )
                    success = await _send_dm(bot, player["discord_id"], msg)
                    if success:
                        _mark_sent(player["id"], week["id"], day_str, "24hr")
                        log.info(
                            f"24hr DM sent to {player['display_name']} "
                            f"for {day_str}"
                        )

            # ── 30min window ─────────────────────────────────────────────────
            if 0 < secs_to_kick <= 1800:
                if not _notif_already_sent(
                    player["id"], week["id"], day_str, "30min"
                ):
                    msg = _build_30min_message(
                        week["week_number"], day_str,
                        unpicked_today, day_games, first_kick
                    )
                    success = await _send_dm(bot, player["discord_id"], msg)
                    if success:
                        _mark_sent(player["id"], week["id"], day_str, "30min")
                        log.info(
                            f"30min DM sent to {player['display_name']} "
                            f"for {day_str}"
                        )


def _build_24hr_message(week_number: int, day_str: str,
                        unpicked_today: list, first_kick: datetime) -> str:
    day_label = first_kick.strftime("%A %b ") + str(first_kick.day)
    count     = len(unpicked_today)
    game_lines = []
    for g in unpicked_today:
        kick = format_time_et(
            datetime.fromisoformat(g["kickoff_time"]), include_date=False
        )
        hr = f"#{g['home_rank']} " if g.get("home_rank") else ""
        ar = f"#{g['away_rank']} " if g.get("away_rank") else ""
        game_lines.append(
            f"• {hr}{g['home_team']} vs {ar}{g['away_team']} — {kick}"
        )

    return (
        f"⏰ **24 hours to kickoff — Week {week_number}**\n\n"
        f"You have **{count} unpicked game{'s' if count != 1 else ''}** "
        f"on **{day_label}**:\n"
        + "\n".join(game_lines)
        + f"\n\nPicks lock at each game's kickoff time. "
        f"Head to #cfcp-picks to submit yours!"
    )


def _build_30min_message(week_number: int, day_str: str,
                         unpicked_today: list, all_day_games: list,
                         first_kick: datetime) -> str:
    day_label = first_kick.strftime("%A %b ") + str(first_kick.day)

    # Split unpicked into imminent (kickoff ≤ 30min) vs later today
    imminent = []
    later    = []
    for g in unpicked_today:
        secs = seconds_until(g["kickoff_time"])
        if secs <= 1800:
            imminent.append(g)
        else:
            later.append(g)

    lines = ["**Last call — picks locking soon:**\n"]

    if imminent:
        for g in imminent:
            kick    = format_time_et(
                datetime.fromisoformat(g["kickoff_time"]), include_date=False
            )
            cd      = countdown_label(g["kickoff_time"])
            hr = f"#{g['home_rank']} " if g.get("home_rank") else ""
            ar = f"#{g['away_rank']} " if g.get("away_rank") else ""
            lines.append(
                f"🔴 **{hr}{g['home_team']} vs {ar}{g['away_team']}** "
                f"— {kick} *(kicks off in {cd})*"
            )

    if later:
        lines.append("\n**Still to pick later today:**")
        for g in later:
            kick = format_time_et(
                datetime.fromisoformat(g["kickoff_time"]), include_date=False
            )
            hr = f"#{g['home_rank']} " if g.get("home_rank") else ""
            ar = f"#{g['away_rank']} " if g.get("away_rank") else ""
            lines.append(
                f"• {hr}{g['home_team']} vs {ar}{g['away_team']} — {kick}"
            )

    lines.append("\nHead to #cfcp-picks to get your picks in!")

    return (
        f"🚨 **Week {week_number} — last call ({day_label})**\n\n"
        + "\n".join(lines)
    )


# ── Cog ────────────────────────────────────────────────────────────────────────

class NotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.notif_scheduler.start()

    def cog_unload(self) -> None:
        self.notif_scheduler.cancel()

    @tasks.loop(minutes=15)
    async def notif_scheduler(self) -> None:
        try:
            await run_notification_check(self.bot)
        except Exception as exc:
            log.error(f"notif_scheduler error: {exc}", exc_info=True)

    @notif_scheduler.before_loop
    async def before_notif_scheduler(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NotificationsCog(bot))
    log.info("NotificationsCog loaded.")

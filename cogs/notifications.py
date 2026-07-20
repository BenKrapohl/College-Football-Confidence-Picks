from __future__ import annotations
import discord
from discord.ext import commands
from discord.ext import tasks
import logging

from config import NOTIF_24HR, NOTIF_30MIN, NOTIF_CHECK_INTERVAL
from database import get_db, get_all_active_players
from utils.helpers import resolve_current_week, send_dm, log_to_channel
from utils.time_utils import (format_time_et, parse_iso,
                              group_games_by_day, first_kickoff_of_day,
                              seconds_until)

log = logging.getLogger(__name__)

async def _notify_missing_picks(bot: commands.Bot, week: dict,
                                game_day: str, games_on_day: list,
                                notif_type: str, hours_label: str) -> None:
    players = await get_all_active_players()
    game_ids_today = {g["id"] for g in games_on_day}

    for player in players:
        if not player["dm_notifications"]:
            continue

        async with get_db() as conn:
            async with conn.execute(
                """SELECT 1 FROM notifications_sent
                   WHERE player_id=? AND week_id=? AND game_day=? AND notif_type=?""",
                (player["id"], week["id"], game_day, notif_type)
            ) as cursor:
                already_sent = await cursor.fetchone()
                
            if already_sent:
                continue

            async with conn.execute(
                """SELECT game_id FROM picks
                   WHERE player_id=? AND is_forfeit=0
                   AND game_id IN (SELECT id FROM games WHERE week_id=?)""",
                (player["id"], week["id"])
            ) as cursor:
                rows = await cursor.fetchall()
                picked_ids = {r["game_id"] for r in rows}
            
            async with conn.execute(
                "SELECT COUNT(*) as c FROM picks WHERE player_id=? AND week_id=? AND is_forfeit=0",
                (player["id"], week["id"])
            ) as cursor:
                week_picks_count = (await cursor.fetchone())["c"]

        missing = game_ids_today - picked_ids
        if not missing:
            async with get_db() as conn:
                await conn.execute(
                    """INSERT OR IGNORE INTO notifications_sent
                       (player_id, week_id, game_day, notif_type)
                       VALUES (?,?,?,?)""",
                    (player["id"], week["id"], game_day, notif_type)
                )
            continue

        missing_games = [g for g in games_on_day if g["id"] in missing]
        lines = []
        for g in missing_games:
            hr = f"#{g['home_rank']} " if g["home_rank"] else ""
            ar = f"#{g['away_rank']} " if g["away_rank"] else ""
            kickoff_str = format_time_et(parse_iso(g["kickoff_time"]))
            lines.append(f"• {hr}{g['home_team']} vs {ar}{g['away_team']} — {kickoff_str}")

        message = (
            f"🏈 **CFCP reminder — {hours_label} until kickoff!**\n\n"
            f"📊 **Week Progress:** You have locked in **{week_picks_count}/{week['game_count']}** picks for the week.\n\n"
            f"⚠️ You still need to pick **{len(missing_games)}** game(s) kicking off soon:\n"
            + "\n".join(lines) +
            "\n\nHead to #cfcp-picks and use **Submit picks** to lock them in before time runs out!"
        )

        sent = await send_dm(bot, player["discord_id"], message)

        async with get_db() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO notifications_sent
                   (player_id, week_id, game_day, notif_type)
                   VALUES (?,?,?,?)""",
                (player["id"], week["id"], game_day, notif_type)
            )

        if not sent:
            picks_ch_id = None
            async with get_db() as conn:
                async with conn.execute(
                    "SELECT value FROM bot_config WHERE key='channel_picks'"
                ) as cursor:
                    row = await cursor.fetchone()
                    picks_ch_id = row["value"] if row else None
            if picks_ch_id:
                ch = bot.get_channel(int(picks_ch_id))
                if isinstance(ch, discord.TextChannel):
                    await ch.send(
                        f"<@{player['discord_id']}> — reminder: you have "
                        f"**{len(missing_games)}** unpicked game(s) "
                        f"kicking off in {hours_label}! "
                        f"*(DMs are off — enable them to get these privately.)*",
                        delete_after=3600,
                    )


async def send_weekly_recap(bot: commands.Bot, week_id: int) -> None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM weeks WHERE id=?", (week_id,)) as cursor:
            week = await cursor.fetchone()
    if not week:
        return

    players = await get_all_active_players()

    for player in players:
        if not player["dm_notifications"]:
            continue

        already = None
        async with get_db() as conn:
            async with conn.execute(
                """SELECT 1 FROM notifications_sent
                   WHERE player_id=? AND week_id=? AND game_day='recap'
                   AND notif_type='recap'""",
                (player["id"], week_id)
            ) as cursor:
                already = await cursor.fetchone()
                
        if already:
            continue

        async with get_db() as conn:
            async with conn.execute(
                "SELECT * FROM weekly_scores WHERE player_id=? AND week_id=?",
                (player["id"], week_id)
            ) as cursor:
                ws = await cursor.fetchone()
                
            async with conn.execute(
                """SELECT pk.*, g.home_team, g.away_team, g.winner, g.home_score,
                          g.away_score
                   FROM picks pk JOIN games g ON pk.game_id = g.id
                   WHERE pk.player_id=? AND g.week_id=?
                   ORDER BY pk.confidence_points DESC""",
                (player["id"], week_id)
            ) as cursor:
                picks = await cursor.fetchall()

        if not ws:
            continue

        lines = []
        for p in picks:
            if p["is_forfeit"]:
                icon = "⛔"
                detail = "missed pick — forfeited"
            elif p["winner"] is None:
                icon = "🤝"
                detail = (
                    f"{p['home_team']} {p['home_score']}-{p['away_score']} "
                    f"{p['away_team']} (tie)"
                )
            elif p["is_correct"] == 1:
                icon = "✅"
                detail = f"{p['picked_team']} won"
            else:
                icon = "❌"
                detail = f"{p['picked_team']} lost"
            lines.append(f"`{p['confidence_points']:>2}` {icon} {detail}")

        rank_str = f" — ranked **#{ws['weekly_rank']}**" if ws["weekly_rank"] else ""

        message = (
            f"📊 **Week {week['week_number']} Recap**{rank_str}\n\n"
            f"Score: **{ws['points_earned']}/{ws['total_possible']}** points  "
            f"({ws['correct_picks']} correct"
            + (f", {ws['forfeited_picks']} forfeited" if ws["forfeited_picks"] else "")
            + ")\n\n"
            + "\n".join(lines) +
            "\n\nCheck #cfcp-standings for the full leaderboard!"
        )

        sent = await send_dm(bot, player["discord_id"], message)

        async with get_db() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO notifications_sent
                   (player_id, week_id, game_day, notif_type)
                   VALUES (?,?,'recap','recap')""",
                (player["id"], week_id)
            )

        if not sent:
            await log_to_channel(
                bot,
                f"Could not DM weekly recap to **{player['display_name']}** "
                "— DMs disabled.",
                title="DM failed", level="warning",
            )


class NotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.notification_checker.start()

    def cog_unload(self) -> None:
        self.notification_checker.cancel()

    @tasks.loop(seconds=NOTIF_CHECK_INTERVAL)
    async def notification_checker(self) -> None:
        try:
            season, week = await resolve_current_week()
            if not week:
                return

            async with get_db() as conn:
                async with conn.execute(
                    """SELECT * FROM games
                       WHERE week_id=? AND status='scheduled'""",
                    (week["id"],)
                ) as cursor:
                    games = await cursor.fetchall()

            if not games:
                return

            games_dicts = [dict(g) for g in games]
            day_groups  = group_games_by_day(games_dicts)

            for game_day, day_games in day_groups.items():
                first_kick = first_kickoff_of_day(day_games)
                secs_to_first = seconds_until(first_kick)

                if NOTIF_24HR - NOTIF_CHECK_INTERVAL <= secs_to_first <= NOTIF_24HR:
                    await _notify_missing_picks(
                        self.bot, week, game_day, day_games, "24hr", "24 hours"
                    )

                if NOTIF_30MIN - NOTIF_CHECK_INTERVAL <= secs_to_first <= NOTIF_30MIN:
                    await _notify_missing_picks(
                        self.bot, week, game_day, day_games, "30min", "30 minutes"
                    )

        except Exception as exc:
            log.error(f"notification_checker error: {exc}", exc_info=True)

    @notification_checker.before_loop
    async def before_notification_checker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NotificationsCog(bot))
    log.info("NotificationsCog loaded.")
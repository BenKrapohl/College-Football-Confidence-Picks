from __future__ import annotations
from discord.ext import commands
from discord.ext import tasks
import logging

from config import ESPN_FINAL_GRACE_SECS
from database import (get_db, config_get, get_all_scoreable_players,
                      get_week_leaderboard, grace_start, grace_elapsed_secs,
                      grace_clear)
from utils.helpers import log_to_channel, resolve_current_week
from utils.espn import fetch_game_status
from utils.embeds import standings_week_embed, standings_season_embed

log = logging.getLogger(__name__)


# ── Single game scoring ─────────────────────────────────────────────────────

async def score_single_game(bot: commands.Bot, game_id: int) -> None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM games WHERE id=?", (game_id,)) as cursor:
            game = await cursor.fetchone()
            
        if not game or game["status"] != "final":
            return

        async with conn.execute(
            "SELECT * FROM picks WHERE game_id=?", (game_id,)
        ) as cursor:
            picks = await cursor.fetchall()

        winner = game["winner"]  

        for pick in picks:
            if pick["is_forfeit"]:
                is_correct = 0
            elif winner is None:
                is_correct = None
            else:
                is_correct = 1 if pick["picked_team"] == winner else 0

            await conn.execute(
                "UPDATE picks SET is_correct=?, scored_at=datetime('now') WHERE id=?",
                (is_correct, pick["id"])
            )

    await recalculate_weekly_scores(bot, game["week_id"])

    if winner is None:
        log.info(f"Game {game_id} ended in a tie — picks marked, no winner credited.")


# ── Weekly score recalculation ───────────────────────────────────────────────

async def recalculate_weekly_scores(bot: commands.Bot, week_id: int) -> None:
    players = await get_all_scoreable_players()

    async with get_db() as conn:
        for player in players:
            async with conn.execute(
                """SELECT pk.confidence_points, pk.is_correct, pk.is_forfeit,
                          g.status as g_status, g.winner as g_winner
                   FROM picks pk
                   JOIN games g ON pk.game_id = g.id
                   WHERE pk.player_id=? AND g.week_id=?""",
                (player["id"], week_id)
            ) as cursor:
                rows = await cursor.fetchall()

            points_earned   = 0
            correct_picks   = 0
            wrong_picks     = 0
            forfeited_picks = 0
            total_possible  = 0

            for r in rows:
                pts = r["confidence_points"]

                if r["is_forfeit"]:
                    forfeited_picks += 1
                    if r["g_status"] == "final":
                        total_possible += pts
                    continue

                if r["g_status"] != "final":
                    continue  

                total_possible += pts

                if r["g_winner"] is None:
                    continue

                if r["is_correct"] == 1:
                    points_earned += pts
                    correct_picks += 1
                else:
                    wrong_picks += 1

            await conn.execute("""
                INSERT INTO weekly_scores(player_id, week_id, points_earned,
                    correct_picks, wrong_picks, forfeited_picks, total_possible)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(player_id, week_id) DO UPDATE SET
                    points_earned   = excluded.points_earned,
                    correct_picks   = excluded.correct_picks,
                    wrong_picks     = excluded.wrong_picks,
                    forfeited_picks = excluded.forfeited_picks,
                    total_possible  = excluded.total_possible
            """, (player["id"], week_id, points_earned, correct_picks,
                  wrong_picks, forfeited_picks, total_possible))

        async with conn.execute("""
            SELECT id, player_id, points_earned, correct_picks
            FROM weekly_scores WHERE week_id=?
            ORDER BY points_earned DESC, correct_picks DESC
        """, (week_id,)) as cursor:
            scores = await cursor.fetchall()

        rank = 0
        prev_key = None
        for s in scores:
            key = (s["points_earned"], s["correct_picks"])
            if key != prev_key:
                rank += 1
                prev_key = key
            await conn.execute(
                "UPDATE weekly_scores SET weekly_rank=? WHERE id=?",
                (rank, s["id"])
            )

    await _refresh_standings(bot, week_id)


# ── Standings refresh ────────────────────────────────────────────────────────

async def _refresh_standings(bot: commands.Bot, week_id: int) -> None:
    from cogs.setup import refresh_standings
    from database import get_active_season, get_season_leaderboard

    async with get_db() as conn:
        async with conn.execute("SELECT * FROM weeks WHERE id=?", (week_id,)) as cursor:
            week = await cursor.fetchone()
            
    if not week:
        return

    season = await get_active_season()
    if not season:
        return

    week_rows   = await get_week_leaderboard(week_id)
    season_rows = await get_season_leaderboard(season["id"])

    week_embed   = standings_week_embed(week_rows, week["week_number"])
    season_embed = standings_season_embed(season_rows, season["year"])

    await refresh_standings(bot, week_embed, season_embed)


# ── Game-day scoring poll ────────────────────────────────────────────────────

class ScoringCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # FIX #5: API Polling Failsafe state tracking
        self.consecutive_failures = 0 
        self.score_poller.start()

    def cog_unload(self) -> None:
        self.score_poller.cancel()

    @tasks.loop(seconds=90)
    async def score_poller(self) -> None:
        try:
            season, week = await resolve_current_week()
            if not week:
                return

            async with get_db() as conn:
                async with conn.execute(
                    """SELECT * FROM games
                       WHERE week_id=? AND status != 'final'
                       AND is_manually_added=0""",
                    (week["id"],)
                ) as cursor:
                    games = await cursor.fetchall()

            from cogs.admin import update_single_game_embed

            for game in games:
                result = await fetch_game_status(game["espn_game_id"])
                if not result:
                    continue

                espn_status = result["status"]

                if espn_status == "final":
                    elapsed = await grace_elapsed_secs(game["id"])
                    if elapsed is None:
                        await grace_start(game["id"])
                        async with get_db() as conn:
                            await conn.execute(
                                """UPDATE games SET home_score=?, away_score=?
                                   WHERE id=?""",
                                (result["home_score"], result["away_score"], game["id"])
                            )
                        continue

                    if elapsed < ESPN_FINAL_GRACE_SECS:
                        continue

                    async with get_db() as conn:
                        await conn.execute(
                            """UPDATE games SET status='final', home_score=?,
                               away_score=?, winner=? WHERE id=?""",
                            (result["home_score"], result["away_score"],
                             result["winner"], game["id"])
                        )
                    await grace_clear(game["id"])
                    await update_single_game_embed(self.bot, game["id"])
                    await score_single_game(self.bot, game["id"])

                    g = dict(game)
                    tie_note = " (tie)" if result["winner"] is None else ""
                    await log_to_channel(
                        self.bot,
                        f"**{g['home_team']} {result['home_score']} — "
                        f"{result['away_score']} {g['away_team']}** final"
                        f"{tie_note} — scores updated.",
                        title="Game final", level="success",
                    )

                elif espn_status == "in_progress" and game["status"] != "in_progress":
                    async with get_db() as conn:
                        await conn.execute(
                            """UPDATE games SET status='in_progress', home_score=?,
                               away_score=? WHERE id=?""",
                            (result["home_score"], result["away_score"], game["id"])
                        )
                    await update_single_game_embed(self.bot, game["id"])

                elif espn_status == "in_progress":
                    async with get_db() as conn:
                        await conn.execute(
                            "UPDATE games SET home_score=?, away_score=? WHERE id=?",
                            (result["home_score"], result["away_score"], game["id"])
                        )
                    await update_single_game_embed(self.bot, game["id"])

            await self._check_week_complete(week["id"])
            
            # If the loop finished successfully, reset the failure counter and polling interval
            if self.consecutive_failures > 0:
                self.consecutive_failures = 0
                self.score_poller.change_interval(seconds=90)
                await log_to_channel(
                    self.bot, 
                    "✅ ESPN API connection successfully restored.", 
                    title="API Restored", level="success"
                )

        except Exception as exc:
            self.consecutive_failures += 1
            log.error(f"score_poller error (attempt {self.consecutive_failures}): {exc}", exc_info=True)
            
            # Exponential backoff: 90s, 180s, 360s, maxing out at 1 hour (3600 seconds)
            new_interval = min(90 * (2 ** (self.consecutive_failures - 1)), 3600)
            self.score_poller.change_interval(seconds=new_interval)
            
            if self.consecutive_failures == 3:
                await log_to_channel(
                    self.bot,
                    f"🚨 **ESPN API Polling has failed 3 consecutive times.**\n"
                    f"The internal polling task has temporarily backed off to prevent rate limiting.\n"
                    f"Checking again in **{int(new_interval/60)} minutes**.\n"
                    f"Error: `{exc}`",
                    title="API Failure Alert", level="error"
                )

    @score_poller.before_loop
    async def before_score_poller(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_week_complete(self, week_id: int) -> None:
        async with get_db() as conn:
            async with conn.execute("SELECT * FROM weeks WHERE id=?", (week_id,)) as cursor:
                week = await cursor.fetchone()
                
            if not week or week["recap_sent"]:
                return

            async with conn.execute(
                "SELECT COUNT(*) as c FROM games WHERE week_id=?", (week_id,)
            ) as cursor:
                total = (await cursor.fetchone())["c"]
                
            async with conn.execute(
                "SELECT COUNT(*) as c FROM games WHERE week_id=? AND status='final'",
                (week_id,)
            ) as cursor:
                final = (await cursor.fetchone())["c"]

            async with conn.execute(
                """SELECT COUNT(*) as c FROM games
                   WHERE week_id=? AND is_manually_added=1 AND status != 'final'""",
                (week_id,)
            ) as cursor:
                pending_manual = (await cursor.fetchone())["c"]

        if total == 0:
            return

        if final == total:
            from cogs.notifications import send_weekly_recap
            await send_weekly_recap(self.bot, week_id)
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE weeks SET recap_sent=1, is_scored=1 WHERE id=?",
                    (week_id,)
                )
            await log_to_channel(
                self.bot,
                f"Week {week['week_number']} complete — all {total} games final. Recap sent.",
                title="Week complete", level="success",
            )
        elif pending_manual > 0 and final == (total - pending_manual):
            reminder_key = f"manual_pending_reminder_week_{week_id}"
            reminder_val = await config_get(reminder_key)
            if not reminder_val:
                from database import config_set
                await config_set(reminder_key, "1")
                await log_to_channel(
                    self.bot,
                    f"⚠️ Week {week['week_number']}: all ESPN-tracked games are "
                    f"final, but **{pending_manual}** manually-added game(s) "
                    f"still need a result entered via **Set results** before "
                    f"the weekly recap can be sent.",
                    title="Action needed", level="warning",
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScoringCog(bot))
    log.info("ScoringCog loaded.")
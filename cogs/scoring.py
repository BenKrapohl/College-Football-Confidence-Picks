from __future__ import annotations
from discord.ext import commands
from discord.ext import tasks
import logging

# FIX #26: single source of truth, imported from config — no local redefinition
from config import ESPN_FINAL_GRACE_SECS
from database import (get_db, config_get, get_all_scoreable_players,
                      get_week_leaderboard, grace_start, grace_elapsed_secs,
                      grace_clear)
# FIX #25: shared helpers
from utils.helpers import log_to_channel, resolve_current_week
from utils.espn import fetch_game_status
from utils.embeds import standings_week_embed, standings_season_embed

log = logging.getLogger(__name__)


# ── Single game scoring ─────────────────────────────────────────────────────

async def score_single_game(bot: commands.Bot, game_id: int) -> None:
    """
    Score every player's pick on this single game.
    Called once a game's status flips to 'final'.

    FIX #9: handles ties — if winner is None, no one is "correct" but no one
    is "wrong" either; the pick contributes to total_possible only.
    FIX #10: uses get_all_scoreable_players() so withdrawn players' weekly
    scores stay accurate.
    """
    with get_db() as conn:
        game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if not game or game["status"] != "final":
            return

        picks = conn.execute(
            "SELECT * FROM picks WHERE game_id=?", (game_id,)
        ).fetchall()

        winner = game["winner"]  # may be None on a tie

        for pick in picks:
            if pick["is_forfeit"]:
                is_correct = 0
            elif winner is None:
                # FIX #9: tie — pick neither right nor wrong, scored_at still set
                is_correct = None
            else:
                is_correct = 1 if pick["picked_team"] == winner else 0

            conn.execute(
                "UPDATE picks SET is_correct=?, scored_at=datetime('now') WHERE id=?",
                (is_correct, pick["id"])
            )

    await recalculate_weekly_scores(bot, game["week_id"])

    if winner is None:
        log.info(f"Game {game_id} ended in a tie — picks marked, no winner credited.")


# ── Weekly score recalculation ───────────────────────────────────────────────

async def recalculate_weekly_scores(bot: commands.Bot, week_id: int) -> None:
    """
    Recompute weekly_scores for every player who has any picks in this week.

    FIX #10: iterate over get_all_scoreable_players() (active + withdrawn)
    instead of get_all_active_players(), so a withdrawn player's weekly
    total doesn't go stale after they leave.
    """
    players = get_all_scoreable_players()

    with get_db() as conn:
        for player in players:
            # FIX #9: join the game's status/winner so we can correctly
            # distinguish "not yet scored" (game still in progress, exclude
            # from totals entirely) from "tie" (final, no winner, counts
            # toward total_possible only).
            rows = conn.execute(
                """SELECT pk.confidence_points, pk.is_correct, pk.is_forfeit,
                          g.status as g_status, g.winner as g_winner
                   FROM picks pk
                   JOIN games g ON pk.game_id = g.id
                   WHERE pk.player_id=? AND g.week_id=?""",
                (player["id"], week_id)
            ).fetchall()

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
                    continue  # game not yet decided — excluded from totals

                total_possible += pts

                if r["g_winner"] is None:
                    # FIX #9: tie — counts toward total_possible only,
                    # neither correct nor wrong
                    continue

                if r["is_correct"] == 1:
                    points_earned += pts
                    correct_picks += 1
                else:
                    wrong_picks += 1

            conn.execute("""
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

        # Compute weekly ranks
        scores = conn.execute("""
            SELECT id, player_id, points_earned, correct_picks
            FROM weekly_scores WHERE week_id=?
            ORDER BY points_earned DESC, correct_picks DESC
        """, (week_id,)).fetchall()

        rank = 0
        prev_key = None
        for s in scores:
            key = (s["points_earned"], s["correct_picks"])
            if key != prev_key:
                rank += 1
                prev_key = key
            conn.execute(
                "UPDATE weekly_scores SET weekly_rank=? WHERE id=?",
                (rank, s["id"])
            )

    await _refresh_standings(bot, week_id)


# ── Standings refresh ────────────────────────────────────────────────────────

async def _refresh_standings(bot: commands.Bot, week_id: int) -> None:
    from cogs.setup import refresh_standings
    from database import get_active_season, get_season_leaderboard

    with get_db() as conn:
        week = conn.execute("SELECT * FROM weeks WHERE id=?", (week_id,)).fetchone()
    if not week:
        return

    season = get_active_season()
    if not season:
        return

    week_rows   = get_week_leaderboard(week_id)
    season_rows = get_season_leaderboard(season["id"])

    week_embed   = standings_week_embed(week_rows, week["week_number"])
    season_embed = standings_season_embed(season_rows, season["year"])

    await refresh_standings(bot, week_embed, season_embed)


# ── Game-day scoring poll ────────────────────────────────────────────────────

class ScoringCog(commands.Cog):
    """
    FIX #11: Replaces the in-memory `self._pending_finals` dict with the
    persisted `scoring_grace` table (via grace_start/grace_elapsed_secs/
    grace_clear in database.py). If the bot restarts mid-grace-window, the
    game is picked back up on the next poll instead of being stuck forever.
    """
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.score_poller.start()

    def cog_unload(self) -> None:
        self.score_poller.cancel()

    @tasks.loop(seconds=90)
    async def score_poller(self) -> None:
        try:
            season, week = resolve_current_week()
            if not week:
                return

            with get_db() as conn:
                games = conn.execute(
                    """SELECT * FROM games
                       WHERE week_id=? AND status != 'final'
                       AND is_manually_added=0""",
                    (week["id"],)
                ).fetchall()

            from cogs.admin import update_single_game_embed

            for game in games:
                result = fetch_game_status(game["espn_game_id"])
                if not result:
                    continue

                espn_status = result["status"]

                if espn_status == "final":
                    # FIX #11: persist grace-period start instead of in-memory dict
                    elapsed = grace_elapsed_secs(game["id"])
                    if elapsed is None:
                        grace_start(game["id"])
                        # Update status to in_progress→final transition is
                        # written immediately but scoring waits for the grace.
                        with get_db() as conn:
                            conn.execute(
                                """UPDATE games SET home_score=?, away_score=?
                                   WHERE id=?""",
                                (result["home_score"], result["away_score"], game["id"])
                            )
                        continue

                    if elapsed < ESPN_FINAL_GRACE_SECS:
                        continue

                    # Grace period elapsed — commit final status and score
                    with get_db() as conn:
                        conn.execute(
                            """UPDATE games SET status='final', home_score=?,
                               away_score=?, winner=? WHERE id=?""",
                            (result["home_score"], result["away_score"],
                             result["winner"], game["id"])
                        )
                    grace_clear(game["id"])
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
                    with get_db() as conn:
                        conn.execute(
                            """UPDATE games SET status='in_progress', home_score=?,
                               away_score=? WHERE id=?""",
                            (result["home_score"], result["away_score"], game["id"])
                        )
                    await update_single_game_embed(self.bot, game["id"])

                elif espn_status == "in_progress":
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE games SET home_score=?, away_score=? WHERE id=?",
                            (result["home_score"], result["away_score"], game["id"])
                        )
                    await update_single_game_embed(self.bot, game["id"])

            await self._check_week_complete(week["id"])

        except Exception as exc:
            log.error(f"score_poller error: {exc}", exc_info=True)

    @score_poller.before_loop
    async def before_score_poller(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_week_complete(self, week_id: int) -> None:
        """
        Trigger recap when all games in the week are final.

        FIX #20: Previously, a manually-added game stuck in 'scheduled'
        would silently prevent the recap forever. Now: if every
        *non-manual* game is final AND every manual game has had results
        entered (status='final' via Set results), recap fires. If manual
        games remain unscored while everything else is final, we log a
        one-time reminder so the admin knows to use Set results.
        """
        with get_db() as conn:
            week = conn.execute("SELECT * FROM weeks WHERE id=?", (week_id,)).fetchone()
            if not week or week["recap_sent"]:
                return

            total = conn.execute(
                "SELECT COUNT(*) as c FROM games WHERE week_id=?", (week_id,)
            ).fetchone()["c"]
            final = conn.execute(
                "SELECT COUNT(*) as c FROM games WHERE week_id=? AND status='final'",
                (week_id,)
            ).fetchone()["c"]

            pending_manual = conn.execute(
                """SELECT COUNT(*) as c FROM games
                   WHERE week_id=? AND is_manually_added=1 AND status != 'final'""",
                (week_id,)
            ).fetchone()["c"]

        if total == 0:
            return

        if final == total:
            from cogs.notifications import send_weekly_recap
            await send_weekly_recap(self.bot, week_id)
            with get_db() as conn:
                conn.execute(
                    "UPDATE weeks SET recap_sent=1, is_scored=1 WHERE id=?",
                    (week_id,)
                )
            await log_to_channel(
                self.bot,
                f"Week {week['week_number']} complete — all {total} games final. "
                f"Recap sent.",
                title="Week complete", level="success",
            )
        elif pending_manual > 0 and final == (total - pending_manual):
            # Only manual games remain — remind once per check by writing
            # a flag so we don't spam every 90 seconds.
            reminder_key = f"manual_pending_reminder_week_{week_id}"
            if not config_get(reminder_key):
                from database import config_set
                config_set(reminder_key, "1")
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

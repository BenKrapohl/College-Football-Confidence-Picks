from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands, tasks
import logging

from config import ADMIN_ROLE_ID, COLOR_GREEN, COLOR_RED, COLOR_AMBER
from database import (get_db, config_get, get_active_season,
                      get_all_active_players, get_season_leaderboard,
                      get_week_leaderboard)
from utils.embeds import log_embed
from utils.time_utils import now_et

log = logging.getLogger(__name__)

ESPN_FINAL_GRACE_SECS = 180   # wait 3 min after 'post' before scoring


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)


async def _log(bot: commands.Bot, description: str,
               title: str = "Scoring", level: str = "info") -> None:
    ch_id = config_get("channel_logs")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if isinstance(ch, discord.TextChannel):
        await ch.send(embed=log_embed(title, description, level))


def _latest_week(season_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM weeks WHERE season_id=? ORDER BY week_number DESC LIMIT 1",
            (season_id,)
        ).fetchone()


# ── Core scoring logic ─────────────────────────────────────────────────────────

def score_game(game_id: int, winner: str) -> dict:
    """
    Score all picks for a single game.
    Returns summary dict with counts.
    """
    summary = {"correct": 0, "wrong": 0, "forfeit": 0, "total": 0}

    with get_db() as conn:
        picks = conn.execute(
            "SELECT * FROM picks WHERE game_id=?", (game_id,)
        ).fetchall()

        for pick in picks:
            summary["total"] += 1
            if pick["is_forfeit"]:
                summary["forfeit"] += 1
                conn.execute(
                    "UPDATE picks SET is_correct=0, scored_at=datetime('now','localtime') "
                    "WHERE id=?",
                    (pick["id"],)
                )
            elif pick["picked_team"] == winner:
                summary["correct"] += 1
                conn.execute(
                    "UPDATE picks SET is_correct=1, scored_at=datetime('now','localtime') "
                    "WHERE id=?",
                    (pick["id"],)
                )
            else:
                summary["wrong"] += 1
                conn.execute(
                    "UPDATE picks SET is_correct=0, scored_at=datetime('now','localtime') "
                    "WHERE id=?",
                    (pick["id"],)
                )

        conn.execute(
            "UPDATE games SET winner=?, status='final' WHERE id=?",
            (winner, game_id)
        )

    return summary


def recalculate_weekly_scores(week_id: int) -> None:
    """
    Recalculate and upsert weekly_scores for every player for this week.
    Called after any game is scored or a score is corrected.
    """
    players = get_all_active_players()

    with get_db() as conn:
        week = conn.execute(
            "SELECT * FROM weeks WHERE id=?", (week_id,)
        ).fetchone()
        if not week:
            return

        total_possible = sum(
            range(1, week["game_count"] + 1)
        )

        for player in players:
            picks = conn.execute(
                """SELECT pk.confidence_points, pk.is_correct, pk.is_forfeit
                   FROM picks pk
                   JOIN games g ON pk.game_id = g.id
                   WHERE pk.player_id=? AND g.week_id=?
                     AND pk.is_correct IS NOT NULL""",
                (player["id"], week_id)
            ).fetchall()

            points_earned  = sum(
                p["confidence_points"] for p in picks
                if p["is_correct"] and not p["is_forfeit"]
            )
            correct_picks  = sum(1 for p in picks if p["is_correct"] and not p["is_forfeit"])
            wrong_picks    = sum(1 for p in picks if not p["is_correct"] and not p["is_forfeit"])
            forfeited      = sum(1 for p in picks if p["is_forfeit"])

            conn.execute("""
                INSERT INTO weekly_scores
                    (player_id, week_id, points_earned, correct_picks,
                     wrong_picks, forfeited_picks, total_possible)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(player_id, week_id) DO UPDATE SET
                    points_earned   = excluded.points_earned,
                    correct_picks   = excluded.correct_picks,
                    wrong_picks     = excluded.wrong_picks,
                    forfeited_picks = excluded.forfeited_picks,
                    total_possible  = excluded.total_possible
            """, (player["id"], week_id, points_earned, correct_picks,
                  wrong_picks, forfeited, total_possible))

        # Assign weekly ranks (points DESC, correct_picks DESC for ties)
        scores = conn.execute(
            """SELECT id, points_earned, correct_picks
               FROM weekly_scores WHERE week_id=?
               ORDER BY points_earned DESC, correct_picks DESC""",
            (week_id,)
        ).fetchall()

        rank = 1
        for i, row in enumerate(scores):
            if i > 0:
                prev = scores[i - 1]
                if (row["points_earned"] != prev["points_earned"] or
                        row["correct_picks"] != prev["correct_picks"]):
                    rank = i + 1
            conn.execute(
                "UPDATE weekly_scores SET weekly_rank=? WHERE id=?",
                (rank, row["id"])
            )


def recalculate_ranks_only(week_id: int) -> None:
    """Re-sort weekly ranks without touching points or correct pick counts."""
    with get_db() as conn:
        scores = conn.execute(
            """SELECT id, points_earned, correct_picks
               FROM weekly_scores WHERE week_id=?
               ORDER BY points_earned DESC, correct_picks DESC""",
            (week_id,)
        ).fetchall()

        rank = 1
        for i, row in enumerate(scores):
            if i > 0:
                prev = scores[i - 1]
                if (row["points_earned"] != prev["points_earned"] or
                        row["correct_picks"] != prev["correct_picks"]):
                    rank = i + 1
            conn.execute(
                "UPDATE weekly_scores SET weekly_rank=? WHERE id=?",
                (rank, row["id"])
            )


async def refresh_standings_panels(bot: commands.Bot) -> None:
    """Push updated leaderboard data to both standings embeds."""
    from cogs.setup import refresh_standings
    season = get_active_season()
    if not season:
        return
    week = _latest_week(season["id"])
    if not week:
        return

    season_rows = get_season_leaderboard(season["id"])
    week_rows   = get_week_leaderboard(week["id"])

    await refresh_standings(
        bot,
        season_rows=[dict(r) for r in season_rows],
        week_rows=[dict(r) for r in week_rows],
        season_year=season["year"],
        week_number=week["week_number"],
    )


async def check_and_send_recap(bot: commands.Bot, week_id: int) -> None:
    """
    If all games in the week are final and recap hasn't been sent,
    fire the weekly recap DM to all players.
    """
    with get_db() as conn:
        week = conn.execute(
            "SELECT * FROM weeks WHERE id=?", (week_id,)
        ).fetchone()
        if not week or week["recap_sent"]:
            return

        unfinished = conn.execute(
            "SELECT COUNT(*) as c FROM games "
            "WHERE week_id=? AND status != 'final'",
            (week_id,)
        ).fetchone()["c"]

    if unfinished > 0:
        return

    # All games final — send recap DMs
    await _send_weekly_recap(bot, week_id)

    with get_db() as conn:
        conn.execute(
            "UPDATE weeks SET recap_sent=1, is_scored=1 WHERE id=?",
            (week_id,)
        )


async def _send_weekly_recap(bot: commands.Bot, week_id: int) -> None:
    season = get_active_season()
    if not season:
        return

    with get_db() as conn:
        week = conn.execute(
            "SELECT * FROM weeks WHERE id=?", (week_id,)
        ).fetchone()

    week_rows   = get_week_leaderboard(week_id)
    season_rows = get_season_leaderboard(season["id"])

    # Build week winner string
    if week_rows:
        top_pts     = week_rows[0]["points_earned"]
        top_correct = week_rows[0]["correct_picks"]
        winners = [
            r["display_name"] for r in week_rows
            if r["points_earned"] == top_pts
            and r["correct_picks"] == top_correct
        ]
        winner_str = " & ".join(winners)
    else:
        winner_str = "N/A"

    # Season standings blurb
    season_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(season_rows[:5]):
        icon = medals[i] if i < 3 else f"{i+1}."
        season_lines.append(
            f"{icon} {r['display_name']} — {r['total_points']} pts"
        )

    players = get_all_active_players()

    for player in players:
        if not player["dm_notifications"]:
            continue

        with get_db() as conn:
            my_picks = conn.execute(
                """SELECT pk.picked_team, pk.confidence_points,
                          pk.is_correct, pk.is_forfeit,
                          g.home_team, g.away_team, g.winner
                   FROM picks pk
                   JOIN games g ON pk.game_id = g.id
                   WHERE pk.player_id=? AND g.week_id=?
                   ORDER BY pk.confidence_points DESC""",
                (player["id"], week_id)
            ).fetchall()

            my_score = conn.execute(
                "SELECT * FROM weekly_scores WHERE player_id=? AND week_id=?",
                (player["id"], week_id)
            ).fetchone()

        if not my_score:
            continue

        pct = (
            my_score["correct_picks"] / week["game_count"] * 100
            if week["game_count"] else 0
        )

        pick_lines = []
        for p in my_picks:
            if p["is_forfeit"]:
                pick_lines.append(
                    f"❌ MISSED — {p['home_team']} vs {p['away_team']} "
                    f"(forfeited `{p['confidence_points']}` pts)"
                )
            elif p["is_correct"]:
                pick_lines.append(
                    f"✅ {p['picked_team']} — `+{p['confidence_points']}` pts"
                )
            else:
                pick_lines.append(
                    f"❌ {p['picked_team']} — `0` pts "
                    f"*(winner: {p['winner']})*"
                )

        recap_msg = (
            f"📊 **Week {week['week_number']} recap — {player['display_name']}**\n\n"
            f"**Your score:** {my_score['points_earned']} pts · "
            f"{my_score['correct_picks']}/{week['game_count']} correct "
            f"({pct:.0f}%)"
            + (f" · {my_score['forfeited_picks']} missed" if my_score["forfeited_picks"] else "")
            + f"\n**Week rank:** #{my_score['weekly_rank']}\n"
            f"**Week winner:** {winner_str}\n\n"
            f"**Your picks:**\n" + "\n".join(pick_lines[:20])
            + (f"\n*…and {len(pick_lines)-20} more*" if len(pick_lines) > 20 else "")
            + f"\n\n**Season standings (top 5):**\n" + "\n".join(season_lines)
        )

        try:
            user = await bot.fetch_user(int(player["discord_id"]))
            await user.send(recap_msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await _log(
        bot,
        f"Week {week['week_number']} recap DMs sent to all active players. "
        f"Week winner: **{winner_str}**.",
        title="Weekly recap sent", level="success",
    )


# ── Score fix modal ────────────────────────────────────────────────────────────

class ScoreFixSelectMenu(discord.ui.Select):
    def __init__(self, players: list, bot: commands.Bot, week_id: int):
        self.bot     = bot
        self.week_id = week_id
        options = [
            discord.SelectOption(
                label=p["display_name"][:100],
                value=str(p["id"]),
                description=p["discord_username"][:100],
            )
            for p in players[:25]
        ]
        super().__init__(placeholder="Select player to adjust…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        player_id = int(self.values[0])
        with get_db() as conn:
            player = conn.execute(
                "SELECT * FROM players WHERE id=?", (player_id,)
            ).fetchone()
            score = conn.execute(
                "SELECT * FROM weekly_scores WHERE player_id=? AND week_id=?",
                (player_id, self.week_id)
            ).fetchone()

        if not player:
            await interaction.response.send_message(
                "Player not found.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ScoreFixModal(self.bot, dict(player),
                          self.week_id, dict(score) if score else None)
        )


class ScoreFixSelectView(discord.ui.View):
    def __init__(self, players: list, bot: commands.Bot, week_id: int):
        super().__init__(timeout=60)
        self.add_item(ScoreFixSelectMenu(players, bot, week_id))


class ScoreFixModal(discord.ui.Modal, title="Fix weekly score"):
    points = discord.ui.TextInput(
        label="Points earned (override)",
        placeholder="e.g. 142",
        max_length=5,
    )
    correct = discord.ui.TextInput(
        label="Correct picks (override)",
        placeholder="e.g. 10",
        max_length=3,
    )
    note = discord.ui.TextInput(
        label="Reason for adjustment",
        placeholder="e.g. ESPN had wrong winner for OSU game",
        max_length=200,
        required=False,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: commands.Bot, player: dict,
                 week_id: int, current_score: Optional[dict]):
        super().__init__()
        self.bot           = bot
        self.player        = player
        self.week_id       = week_id
        self.current_score = current_score

        if current_score:
            self.points.default  = str(current_score["points_earned"])
            self.correct.default = str(current_score["correct_picks"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            new_points  = int(self.points.value.strip())
            new_correct = int(self.correct.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Points and correct picks must be whole numbers.", ephemeral=True
            )
            return

        note = self.note.value.strip()

        with get_db() as conn:
            conn.execute("""
                INSERT INTO weekly_scores
                    (player_id, week_id, points_earned, correct_picks)
                VALUES (?,?,?,?)
                ON CONFLICT(player_id, week_id) DO UPDATE SET
                    points_earned = excluded.points_earned,
                    correct_picks = excluded.correct_picks
            """, (self.player["id"], self.week_id, new_points, new_correct))

        # Only recalculate ranks — don't overwrite the manual points values
        recalculate_ranks_only(self.week_id)
        await refresh_standings_panels(self.bot)

        await _log(
            self.bot,
            f"Score manually adjusted for **{self.player['display_name']}** "
            f"— {new_points} pts, {new_correct} correct."
            + (f"\nReason: {note}" if note else ""),
            title="Score fixed", level="warning",
        )
        await interaction.response.send_message(
            f"✅ Score updated for **{self.player['display_name']}**.",
            ephemeral=True,
        )


# ── Scoring poller task ────────────────────────────────────────────────────────

class ScoringCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._pending_finals: dict[int, float] = {}
        self.scoring_poller.start()

    def cog_unload(self) -> None:
        self.scoring_poller.cancel()

    @tasks.loop(seconds=90)
    async def scoring_poller(self) -> None:
        """
        Poll ESPN for live/final game statuses.
        When a game goes final, wait the grace period then score it.
        After all games are final, trigger recap DMs.
        """
        try:
            from utils.espn import fetch_game_status
            from cogs.admin import update_single_game_embed

            season = get_active_season()
            if not season:
                return

            week = _latest_week(season["id"])
            if not week:
                return

            with get_db() as conn:
                active_games = conn.execute(
                    """SELECT * FROM games
                       WHERE week_id=? AND status IN ('scheduled','in_progress')
                         AND (espn_game_id IS NOT NULL)
                         AND espn_game_id NOT LIKE 'manual_%'""",
                    (week["id"],)
                ).fetchall()

            if not active_games:
                return

            import time
            now = time.time()

            for game in active_games:
                game_dict = dict(game)
                result    = fetch_game_status(game_dict["espn_game_id"])
                if not result:
                    continue

                new_status = result["status"]

                # Update live score in DB and refresh embed
                with get_db() as conn:
                    conn.execute(
                        """UPDATE games SET status=?, home_score=?,
                           away_score=?, winner=? WHERE id=?""",
                        (new_status, result["home_score"],
                         result["away_score"], result["winner"],
                         game_dict["id"])
                    )
                await update_single_game_embed(self.bot, game_dict["id"])

                if new_status == "final" and result.get("winner"):
                    gid = game_dict["id"]
                    if gid not in self._pending_finals:
                        self._pending_finals[gid] = now
                        log.info(
                            f"Game {gid} final — grace period started."
                        )
                    elif now - self._pending_finals[gid] >= ESPN_FINAL_GRACE_SECS:
                        # Grace period elapsed — score it
                        already_scored = False
                        with get_db() as conn:
                            sample = conn.execute(
                                "SELECT is_correct FROM picks "
                                "WHERE game_id=? AND is_correct IS NOT NULL LIMIT 1",
                                (gid,)
                            ).fetchone()
                            already_scored = sample is not None

                        if not already_scored:
                            summary = score_game(gid, result["winner"])
                            recalculate_weekly_scores(week["id"])
                            await refresh_standings_panels(self.bot)
                            await update_single_game_embed(self.bot, gid)

                            with get_db() as conn:
                                g = conn.execute(
                                    "SELECT home_team, away_team FROM games WHERE id=?",
                                    (gid,)
                                ).fetchone()

                            await _log(
                                self.bot,
                                f"**{g['home_team']} vs {g['away_team']}** final — "
                                f"winner: **{result['winner']}** · "
                                f"{summary['correct']} correct, "
                                f"{summary['wrong']} wrong, "
                                f"{summary['forfeit']} forfeits.",
                                title="Game scored", level="success",
                            )

                        self._pending_finals.pop(gid, None)
                        await check_and_send_recap(self.bot, week["id"])

        except Exception as exc:
            log.error(f"scoring_poller error: {exc}", exc_info=True)

    @scoring_poller.before_loop
    async def before_poller(self) -> None:
        await self.bot.wait_until_ready()


# ── Score fix button wired up here (called from admin panel) ──────────────────

async def open_score_fix(interaction: discord.Interaction,
                         bot: commands.Bot) -> None:
    season = get_active_season()
    if not season:
        await interaction.response.send_message(
            "No active season.", ephemeral=True
        )
        return
    week = _latest_week(season["id"])
    if not week:
        await interaction.response.send_message(
            "No week loaded.", ephemeral=True
        )
        return

    players = get_all_active_players()
    if not players:
        await interaction.response.send_message(
            "No active players.", ephemeral=True
        )
        return

    view = ScoreFixSelectView(
        [dict(p) for p in players], bot, week["id"]
    )
    await interaction.response.send_message(
        f"**Week {week['week_number']} — select a player to adjust their score:**",
        view=view, ephemeral=True,
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScoringCog(bot))
    log.info("ScoringCog loaded.")

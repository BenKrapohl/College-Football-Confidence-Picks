from __future__ import annotations
import discord
from discord.ext import commands
import logging

from config import COLOR_PURPLE, COLOR_TEAL
from database import (get_db, get_player_by_discord_id, get_active_season,
                      get_season_leaderboard, get_player_picks_for_week)

log = logging.getLogger(__name__)


# ── My stats ──────────────────────────────────────────────────────────────────

async def open_my_stats(interaction: discord.Interaction) -> None:
    player = get_player_by_discord_id(str(interaction.user.id))
    if not player:
        await interaction.response.send_message(
            "You're not registered yet. Use the **Register** button to join!",
            ephemeral=True,
        )
        return

    season = get_active_season()
    if not season:
        await interaction.response.send_message(
            "No active season.", ephemeral=True
        )
        return

    leaderboard = get_season_leaderboard(season["id"])
    my_row = next((r for r in leaderboard if r["id"] == player["id"]), None)
    my_rank = None
    for i, r in enumerate(leaderboard, start=1):
        if r["id"] == player["id"]:
            my_rank = i
            break

    e = discord.Embed(
        title=f"📈  {player['display_name']}'s Stats — {season['year']}",
        color=COLOR_TEAL,
    )

    if not my_row or my_row["weeks_played"] == 0:
        e.description = "No scored weeks yet this season."
    else:
        pct = (my_row["total_correct"] / my_row["total_possible"] * 100
               if my_row["total_possible"] else 0)
        e.add_field(name="Season rank", value=f"#{my_rank}", inline=True)
        e.add_field(name="Total points", value=str(my_row["total_points"]), inline=True)
        e.add_field(name="Weeks played", value=str(my_row["weeks_played"]), inline=True)
        e.add_field(
            name="Correct picks",
            value=f"{my_row['total_correct']} / {my_row['total_correct'] + my_row['total_wrong']} "
                  f"({pct:.1f}%)",
            inline=True,
        )
        e.add_field(name="Best week", value=f"{my_row['best_week']} pts", inline=True)
        if my_row["total_forfeits"]:
            e.add_field(
                name="Forfeited picks", value=str(my_row["total_forfeits"]), inline=True
            )
        if player["status"] == "withdrawn":
            e.add_field(
                name="Status", value="⚠️ Withdrawn (scores preserved)", inline=False
            )

    # Per-week breakdown
    with get_db() as conn:
        weekly = conn.execute("""
            SELECT w.week_number, ws.points_earned, ws.correct_picks,
                   ws.total_possible, ws.weekly_rank, ws.forfeited_picks
            FROM weekly_scores ws
            JOIN weeks w ON ws.week_id = w.id
            WHERE ws.player_id=? AND w.season_id=?
            ORDER BY w.week_number
        """, (player["id"], season["id"])).fetchall()

    if weekly:
        lines = []
        for w in weekly:
            rank_str = f"#{w['weekly_rank']}" if w["weekly_rank"] else "—"
            forfeit_str = f" ({w['forfeited_picks']} missed)" if w["forfeited_picks"] else ""
            lines.append(
                f"Week {w['week_number']}: **{w['points_earned']}**/"
                f"{w['total_possible']} pts  ·  rank {rank_str}{forfeit_str}"
            )
        e.add_field(
            name="Weekly breakdown",
            value="\n".join(lines[-10:]) +
                  (f"\n*…and {len(lines)-10} earlier weeks*" if len(lines) > 10 else ""),
            inline=False,
        )

    await interaction.response.send_message(embed=e, ephemeral=True)


# ── Pick history ──────────────────────────────────────────────────────────────

async def open_pick_history(interaction: discord.Interaction) -> None:
    player = get_player_by_discord_id(str(interaction.user.id))
    if not player:
        await interaction.response.send_message(
            "You're not registered yet.", ephemeral=True
        )
        return

    season = get_active_season()
    if not season:
        await interaction.response.send_message(
            "No active season.", ephemeral=True
        )
        return

    with get_db() as conn:
        weeks = conn.execute(
            "SELECT * FROM weeks WHERE season_id=? ORDER BY week_number DESC",
            (season["id"],)
        ).fetchall()

    if not weeks:
        await interaction.response.send_message(
            "No weeks loaded yet this season.", ephemeral=True
        )
        return

    view = PickHistoryView(player["id"], [dict(w) for w in weeks])
    embed = view.build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PickHistoryView(discord.ui.View):
    def __init__(self, player_id: int, weeks: list):
        super().__init__(timeout=180)
        self.player_id = player_id
        self.weeks      = weeks
        self.index      = 0  # 0 = most recent (weeks sorted DESC)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.clear_items()

        prev_btn = discord.ui.Button(
            label="◀ Newer", style=discord.ButtonStyle.secondary,
            disabled=(self.index == 0),
        )
        prev_btn.callback = self._go_newer
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(
            label="Older ▶", style=discord.ButtonStyle.secondary,
            disabled=(self.index >= len(self.weeks) - 1),
        )
        next_btn.callback = self._go_older
        self.add_item(next_btn)

        close_btn = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.secondary,
        )
        close_btn.callback = self._close
        self.add_item(close_btn)

    def build_embed(self) -> discord.Embed:
        week = self.weeks[self.index]
        picks = get_player_picks_for_week(self.player_id, week["id"])

        e = discord.Embed(
            title=f"Week {week['week_number']} — Pick history",
            color=COLOR_PURPLE,
        )

        if not picks:
            e.description = "No picks made this week."
            return e

        lines = []
        for p in picks:
            hr = f"#{p['home_rank']} " if p["home_rank"] else ""
            ar = f"#{p['away_rank']} " if p["away_rank"] else ""
            matchup = f"{hr}{p['home_team']} vs {ar}{p['away_team']}"

            if p["is_forfeit"]:
                icon = "⛔"
                pick_str = "*(forfeited — no pick made)*"
            elif p["game_status"] != "final":
                icon = "⏳"
                pick_str = f"picked **{p['picked_team']}**  ·  {p['game_status']}"
            elif p["winner"] is None:
                icon = "🤝"
                pick_str = f"picked **{p['picked_team']}**  ·  tie"
            elif p["is_correct"] == 1:
                icon = "✅"
                pick_str = f"picked **{p['picked_team']}**  ·  correct!"
            else:
                icon = "❌"
                pick_str = f"picked **{p['picked_team']}**  ·  incorrect"

            lines.append(
                f"`{p['confidence_points']:>2}` {icon} {matchup}\n　{pick_str}"
            )

        e.description = "\n\n".join(lines[:12])
        if len(lines) > 12:
            e.set_footer(text=f"…and {len(lines)-12} more games this week")

        with get_db() as conn:
            ws = conn.execute(
                "SELECT * FROM weekly_scores WHERE player_id=? AND week_id=?",
                (self.player_id, week["id"])
            ).fetchone()
        if ws:
            e.add_field(
                name="Week result",
                value=f"**{ws['points_earned']}**/{ws['total_possible']} pts"
                      + (f"  ·  rank #{ws['weekly_rank']}" if ws["weekly_rank"] else ""),
                inline=False,
            )

        return e

    async def _go_newer(self, interaction: discord.Interaction) -> None:
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _go_older(self, interaction: discord.Interaction) -> None:
        self.index = min(len(self.weeks) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Closed.", embed=None, view=None
        )
        self.stop()


# ── Cog ────────────────────────────────────────────────────────────────────────

class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
    log.info("StatsCog loaded.")

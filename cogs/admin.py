from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (ADMIN_ROLE_ID, GUILD_ID, POLL_AP, POLL_CFP)
from database import (get_db, config_get, config_set,
                      get_active_season, get_all_active_players)
from utils.espn import fetch_rankings, fetch_week_games
from utils.embeds import admin_panel_embed, picks_hub_embed, game_embed, log_embed
from utils.time_utils import format_time_et

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)


async def _log(bot: commands.Bot, description: str,
               title: str = "Admin action", level: str = "info") -> None:
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


# ── Game embed management ──────────────────────────────────────────────────────

async def post_game_embeds(bot: commands.Bot, week_id: int) -> None:
    """Clear #cfcp-games and post one embed per game, storing message IDs."""
    ch_id = config_get("channel_games")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    with get_db() as conn:
        games = conn.execute(
            "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
            (week_id,)
        ).fetchall()

    if not games:
        return

    try:
        await ch.purge(limit=100)
    except discord.Forbidden:
        pass

    picks_reveal   = config_get("picks_reveal", "1") == "1"
    total_players  = len(get_all_active_players())

    for game in games:
        embed = game_embed(dict(game), picks_reveal=picks_reveal,
                           total_players=total_players)
        msg = await ch.send(embed=embed)
        with get_db() as conn:
            conn.execute(
                "UPDATE games SET discord_message_id=? WHERE id=?",
                (str(msg.id), game["id"])
            )


async def update_single_game_embed(bot: commands.Bot, game_id: int) -> None:
    """Edit the existing Discord message for one game."""
    with get_db() as conn:
        game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if not game or not game["discord_message_id"]:
        return
    ch_id = config_get("channel_games")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return
    try:
        msg = await ch.fetch_message(int(game["discord_message_id"]))
        picks_reveal  = config_get("picks_reveal", "1") == "1"
        total_players = len(get_all_active_players())
        await msg.edit(embed=game_embed(
            dict(game), picks_reveal=picks_reveal, total_players=total_players
        ))
    except discord.NotFound:
        pass


async def _refresh_all_panels(bot: commands.Bot) -> None:
    """Convenience: refresh admin + picks hub after any week mutation."""
    from cogs.setup import refresh_admin_panel, refresh_picks_hub
    season = get_active_season()
    if not season:
        return
    week = _latest_week(season["id"])
    guild = bot.get_guild(GUILD_ID)
    poll_type    = config_get("active_poll", POLL_AP)
    picks_reveal = config_get("picks_reveal", "1") == "1"

    await refresh_admin_panel(
        bot, guild,
        week=dict(week) if week else None,
        season=dict(season),
        poll_type=poll_type,
        picks_reveal=picks_reveal,
    )

    if week:
        with get_db() as conn:
            game_rows = conn.execute(
                "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
                (week["id"],)
            ).fetchall()
        players = get_all_active_players()
        # Determine submitted status for each player
        player_list = []
        with get_db() as conn:
            for p in players:
                count = conn.execute(
                    """SELECT COUNT(*) as c FROM picks pk
                       JOIN games g ON pk.game_id = g.id
                       WHERE pk.player_id=? AND g.week_id=? AND pk.is_forfeit=0""",
                    (p["id"], week["id"])
                ).fetchone()["c"]
                player_list.append({**dict(p), "submitted": count == week["game_count"]})

        await refresh_picks_hub(
            bot,
            week=dict(week),
            games=[dict(g) for g in game_rows],
            players=player_list,
        )


# ── Modals ─────────────────────────────────────────────────────────────────────

class LoadWeekModal(discord.ui.Modal, title="Load week"):
    week_number = discord.ui.TextInput(
        label="Week number", placeholder="1", max_length=2, min_length=1
    )
    start_date = discord.ui.TextInput(
        label="Start date (YYYYMMDD)", placeholder="20260829",
        max_length=8, min_length=8
    )
    end_date = discord.ui.TextInput(
        label="End date (YYYYMMDD)", placeholder="20260901",
        max_length=8, min_length=8
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            week_num = int(self.week_number.value.strip())
        except ValueError:
            await interaction.followup.send(
                "Week number must be a whole number.", ephemeral=True
            )
            return

        start = self.start_date.value.strip()
        end   = self.end_date.value.strip()
        try:
            datetime.strptime(start, "%Y%m%d")
            datetime.strptime(end,   "%Y%m%d")
        except ValueError:
            await interaction.followup.send(
                "Invalid date — use YYYYMMDD format (e.g. 20260829).", ephemeral=True
            )
            return

        season = get_active_season()
        if not season:
            await interaction.followup.send("No active season found.", ephemeral=True)
            return

        poll_type = config_get("active_poll", POLL_AP)

        await interaction.followup.send(
            f"Fetching Week {week_num} from ESPN ({start}–{end})…", ephemeral=True
        )

        try:
            ranked_teams = fetch_rankings(poll_type)
        except Exception as exc:
            await interaction.followup.send(
                f"Rankings fetch failed: {exc}", ephemeral=True
            )
            return

        if not ranked_teams:
            await interaction.followup.send(
                "Rankings came back empty — ESPN API may be down or the poll "
                "isn't published yet. Try again or swap to AP.", ephemeral=True
            )
            return

        try:
            games = fetch_week_games(start, end, ranked_teams)
        except Exception as exc:
            await interaction.followup.send(
                f"Schedule fetch failed: {exc}", ephemeral=True
            )
            return

        if not games:
            await interaction.followup.send(
                f"No ranked games found for {start}–{end}. "
                "Check the dates — CFB weeks sometimes start Thursday.", ephemeral=True
            )
            return

        # ── Write to DB ──────────────────────────────────────────────────────
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM weeks WHERE season_id=? AND week_number=?",
                (season["id"], week_num)
            ).fetchone()

            if existing:
                week_id = existing["id"]
                conn.execute("""
                    UPDATE weeks
                    SET start_date=?, end_date=?, game_count=?,
                        loaded_at=datetime('now','localtime'), is_scored=0
                    WHERE id=?
                """, (start, end, len(games), week_id))
            else:
                conn.execute("""
                    INSERT INTO weeks(season_id, week_number, start_date,
                                     end_date, game_count, loaded_at)
                    VALUES (?,?,?,?,?,datetime('now','localtime'))
                """, (season["id"], week_num, start, end, len(games)))
                week_id = conn.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]

            for g in games:
                conn.execute("""
                    INSERT INTO games(week_id, espn_game_id, home_team, away_team,
                        home_rank, away_rank, spread, over_under, kickoff_time,
                        channel, espn_link, status, home_score, away_score, winner)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(week_id, espn_game_id) DO UPDATE SET
                        home_rank    = excluded.home_rank,
                        away_rank    = excluded.away_rank,
                        spread       = excluded.spread,
                        over_under   = excluded.over_under,
                        kickoff_time = excluded.kickoff_time,
                        channel      = excluded.channel,
                        status       = excluded.status,
                        home_score   = excluded.home_score,
                        away_score   = excluded.away_score,
                        winner       = excluded.winner
                """, (
                    week_id, g["espn_game_id"], g["home_team"], g["away_team"],
                    g["home_rank"], g["away_rank"], g["spread"], g["over_under"],
                    g["kickoff_time"], g["channel"], g["espn_link"],
                    g["status"], g["home_score"], g["away_score"], g["winner"]
                ))

        # ── Post embeds & refresh panels ────────────────────────────────────
        await post_game_embeds(self.bot, week_id)
        await _refresh_all_panels(self.bot)

        # ── Notify all players that picks are open ───────────────────────────
        from cogs.notifications import notify_week_open
        await notify_week_open(self.bot, week_id, week_num, len(games))

        await _log(
            self.bot,
            f"Week {week_num} loaded — **{len(games)} games** · "
            f"Dates: {start}–{end} · Poll: {poll_type.upper()}",
            title="Week loaded", level="success",
        )
        await interaction.followup.send(
            f"✅ Week {week_num} loaded — {len(games)} games posted to #cfcp-games.",
            ephemeral=True,
        )


class EditGameModal(discord.ui.Modal, title="Edit game"):
    home_rank = discord.ui.TextInput(
        label="Home rank (number, or blank = NR)",
        required=False, max_length=3, placeholder="e.g. 5"
    )
    away_rank = discord.ui.TextInput(
        label="Away rank (number, or blank = NR)",
        required=False, max_length=3, placeholder="e.g. 12"
    )
    kickoff = discord.ui.TextInput(
        label="Kickoff time (YYYY-MM-DD HH:MM ET)",
        required=False, max_length=20, placeholder="e.g. 2026-09-05 12:00"
    )
    spread = discord.ui.TextInput(
        label="Spread", required=False,
        max_length=30, placeholder="e.g. OHIO -14.5"
    )
    channel = discord.ui.TextInput(
        label="Broadcast channel", required=False,
        max_length=30, placeholder="e.g. ESPN"
    )

    def __init__(self, bot: commands.Bot, game: dict):
        super().__init__()
        self.bot  = bot
        self.game = game

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        updates: dict = {}

        # Home rank — blank string means "set to NR (NULL)"
        hr = self.home_rank.value.strip()
        if hr:
            try:
                updates["home_rank"] = int(hr)
            except ValueError:
                await interaction.followup.send(
                    "Home rank must be a number.", ephemeral=True
                )
                return
        elif self.home_rank.value != "":
            updates["home_rank"] = None

        # Away rank
        ar = self.away_rank.value.strip()
        if ar:
            try:
                updates["away_rank"] = int(ar)
            except ValueError:
                await interaction.followup.send(
                    "Away rank must be a number.", ephemeral=True
                )
                return
        elif self.away_rank.value != "":
            updates["away_rank"] = None

        # Kickoff
        k = self.kickoff.value.strip()
        if k:
            try:
                dt = datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
                updates["kickoff_time"] = dt.isoformat()
            except ValueError:
                await interaction.followup.send(
                    "Invalid kickoff — use YYYY-MM-DD HH:MM.", ephemeral=True
                )
                return

        if self.spread.value.strip():
            updates["spread"] = self.spread.value.strip()
        if self.channel.value.strip():
            updates["channel"] = self.channel.value.strip()

        if not updates:
            await interaction.followup.send("No changes submitted.", ephemeral=True)
            return

        set_clause = ", ".join(f"{k}=?" for k in updates)
        with get_db() as conn:
            conn.execute(
                f"UPDATE games SET {set_clause} WHERE id=?",
                list(updates.values()) + [self.game["id"]]
            )

        await update_single_game_embed(self.bot, self.game["id"])
        await _log(
            self.bot,
            f"Game edited: **{self.game['home_team']} vs {self.game['away_team']}** "
            f"— updated: {', '.join(updates.keys())}",
            title="Game edited",
        )
        await interaction.followup.send("✅ Game updated.", ephemeral=True)


class AddGameModal(discord.ui.Modal, title="Add game manually"):
    home_team = discord.ui.TextInput(
        label="Home team name", max_length=50, placeholder="e.g. Ohio State"
    )
    away_team = discord.ui.TextInput(
        label="Away team name", max_length=50, placeholder="e.g. Michigan"
    )
    kickoff = discord.ui.TextInput(
        label="Kickoff time (YYYY-MM-DD HH:MM ET)",
        max_length=20, placeholder="e.g. 2026-09-05 12:00"
    )
    spread = discord.ui.TextInput(
        label="Spread (optional)", required=False,
        max_length=30, placeholder="e.g. OSU -7.5"
    )
    channel = discord.ui.TextInput(
        label="Channel (optional)", required=False,
        max_length=30, placeholder="e.g. FOX"
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        season = get_active_season()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return

        week = _latest_week(season["id"])
        if not week:
            await interaction.followup.send(
                "No week loaded yet — load a week first.", ephemeral=True
            )
            return

        k = self.kickoff.value.strip()
        try:
            dt = datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            kickoff_iso = dt.isoformat()
        except ValueError:
            await interaction.followup.send(
                "Invalid kickoff — use YYYY-MM-DD HH:MM.", ephemeral=True
            )
            return

        home    = self.home_team.value.strip()
        away    = self.away_team.value.strip()
        spread  = self.spread.value.strip()
        channel = self.channel.value.strip()
        manual_id = f"manual_{int(datetime.now().timestamp())}"

        with get_db() as conn:
            conn.execute("""
                INSERT INTO games(week_id, espn_game_id, home_team, away_team,
                    kickoff_time, spread, channel, status, is_manually_added)
                VALUES (?,?,?,?,?,?,?,'scheduled',1)
            """, (week["id"], manual_id, home, away, kickoff_iso, spread, channel))
            conn.execute(
                "UPDATE weeks SET game_count = game_count + 1 WHERE id=?",
                (week["id"],)
            )

        await post_game_embeds(self.bot, week["id"])
        await _refresh_all_panels(self.bot)

        await _log(
            self.bot,
            f"Manual game added: **{home} vs {away}** · {kickoff_iso}",
            title="Game added manually",
        )
        await interaction.followup.send(
            f"✅ Manual game added: {home} vs {away}.", ephemeral=True
        )


# ── Edit game select flow ──────────────────────────────────────────────────────

class EditGameSelectMenu(discord.ui.Select):
    def __init__(self, games: list, bot: commands.Bot):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"{g['home_team']} vs {g['away_team']}"[:100],
                value=str(g["id"]),
                description=(
                    format_time_et(
                        datetime.fromisoformat(g["kickoff_time"]),
                        include_date=True
                    )[:100]
                    if g.get("kickoff_time") else ""
                ),
            )
            for g in games[:25]
        ]
        super().__init__(placeholder="Select a game to edit…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        game_id = int(self.values[0])
        with get_db() as conn:
            game = conn.execute(
                "SELECT * FROM games WHERE id=?", (game_id,)
            ).fetchone()
        if not game:
            await interaction.response.send_message("Game not found.", ephemeral=True)
            return
        await interaction.response.send_modal(
            EditGameModal(self.bot, dict(game))
        )


class EditGameSelectView(discord.ui.View):
    def __init__(self, games: list, bot: commands.Bot):
        super().__init__(timeout=60)
        self.add_item(EditGameSelectMenu(games, bot))


# ── Lock confirmation ──────────────────────────────────────────────────────────

class LockConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, week_id: int, week_num: int):
        super().__init__(timeout=30)
        self.bot      = bot
        self.week_id  = week_id
        self.week_num = week_num

    @discord.ui.button(label="Confirm lock", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        with get_db() as conn:
            conn.execute(
                "UPDATE weeks SET is_locked=1 WHERE id=?", (self.week_id,)
            )
        await _refresh_all_panels(self.bot)
        await _log(
            self.bot,
            f"Week {self.week_num} picks locked — no further submissions accepted.",
            title="Picks locked", level="warning",
        )
        await interaction.followup.send(
            f"🔒 Week {self.week_num} picks are now locked.", ephemeral=True
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


# ── Admin panel view ───────────────────────────────────────────────────────────

class AdminPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ── Row 0: Week management ───────────────────────────────────────────────
    @discord.ui.button(label="Load week", style=discord.ButtonStyle.primary,
                       custom_id="admin:load_week", row=0)
    async def load_week(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(LoadWeekModal(interaction.client))

    @discord.ui.button(label="Lock picks", style=discord.ButtonStyle.danger,
                       custom_id="admin:lock_picks", row=0)
    async def lock_picks(self, interaction: discord.Interaction,
                         button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season = get_active_season()
        if not season:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return
        week = _latest_week(season["id"])
        if not week:
            await interaction.response.send_message(
                "No week loaded.", ephemeral=True
            )
            return
        if week["is_locked"]:
            await interaction.response.send_message(
                f"Week {week['week_number']} is already locked.", ephemeral=True
            )
            return
        view = LockConfirmView(
            interaction.client, week["id"], week["week_number"]
        )
        await interaction.response.send_message(
            f"⚠️ Lock all picks for **Week {week['week_number']}**? "
            "Players will not be able to submit or edit after this.",
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="Unlock picks", style=discord.ButtonStyle.secondary,
                       custom_id="admin:unlock_picks", row=0)
    async def unlock_picks(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season = get_active_season()
        if not season:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return
        week = _latest_week(season["id"])
        if not week:
            await interaction.response.send_message("No week loaded.", ephemeral=True)
            return
        with get_db() as conn:
            conn.execute(
                "UPDATE weeks SET is_locked=0 WHERE id=?", (week["id"],)
            )
        await _refresh_all_panels(interaction.client)
        await _log(
            interaction.client,
            f"Week {week['week_number']} picks **unlocked** (emergency override).",
            title="Picks unlocked", level="warning",
        )
        await interaction.response.send_message(
            f"🔓 Week {week['week_number']} picks unlocked.", ephemeral=True
        )

    # ── Row 1: Game management ───────────────────────────────────────────────
    @discord.ui.button(label="Edit game", style=discord.ButtonStyle.secondary,
                       custom_id="admin:edit_game", row=1)
    async def edit_game(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season = get_active_season()
        if not season:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return
        week = _latest_week(season["id"])
        if not week:
            await interaction.response.send_message("No week loaded.", ephemeral=True)
            return
        with get_db() as conn:
            games = conn.execute(
                "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
                (week["id"],)
            ).fetchall()
        if not games:
            await interaction.response.send_message(
                "No games this week.", ephemeral=True
            )
            return
        view = EditGameSelectView([dict(g) for g in games], interaction.client)
        await interaction.response.send_message(
            f"**Week {week['week_number']} — select a game to edit:**",
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="Add game", style=discord.ButtonStyle.secondary,
                       custom_id="admin:add_game", row=1)
    async def add_game(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(AddGameModal(interaction.client))

    @discord.ui.button(label="Set results", style=discord.ButtonStyle.secondary,
                       custom_id="admin:set_results", row=1)
    async def set_results(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Result entry is coming in Milestone 4 (auto-scoring).", ephemeral=True
        )

    # ── Row 2: Settings ──────────────────────────────────────────────────────
    @discord.ui.button(label="Swap poll", style=discord.ButtonStyle.secondary,
                       custom_id="admin:swap_poll", row=2)
    async def swap_poll(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        current  = config_get("active_poll", POLL_AP)
        new_poll = POLL_CFP if current == POLL_AP else POLL_AP
        config_set("active_poll", new_poll)
        label = "CFP Rankings" if new_poll == POLL_CFP else "AP Top 25"
        season = get_active_season()
        if season:
            with get_db() as conn:
                conn.execute(
                    "UPDATE seasons SET poll_type=? WHERE id=?",
                    (new_poll, season["id"])
                )
        await _refresh_all_panels(interaction.client)
        await _log(
            interaction.client,
            f"Poll switched to **{label}**.",
            title="Poll changed",
        )
        await interaction.response.send_message(
            f"✅ Active poll switched to **{label}**.", ephemeral=True
        )

    @discord.ui.button(label="Toggle pick reveal", style=discord.ButtonStyle.secondary,
                       custom_id="admin:toggle_reveal", row=2)
    async def toggle_reveal(self, interaction: discord.Interaction,
                            button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        current = config_get("picks_reveal", "1")
        new_val = "0" if current == "1" else "1"
        config_set("picks_reveal", new_val)
        state = "ON" if new_val == "1" else "OFF"
        await _refresh_all_panels(interaction.client)
        await _log(
            interaction.client,
            f"Pick reveal toggled **{state}** — "
            "game embeds will {'show' if new_val=='1' else 'hide'} individual picks after lock.",
            title="Pick reveal changed",
        )
        await interaction.response.send_message(
            f"Pick reveal is now **{state}**.", ephemeral=True
        )

    # ── Row 3: Tools ─────────────────────────────────────────────────────────
    @discord.ui.button(label="Fix score", style=discord.ButtonStyle.secondary,
                       custom_id="admin:fix_score", row=3)
    async def fix_score(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        from cogs.scoring import open_score_fix
        await open_score_fix(interaction, interaction.client)

    @discord.ui.button(label="Pending approvals", style=discord.ButtonStyle.secondary,
                       custom_id="admin:approvals", row=3)
    async def approvals(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        with get_db() as conn:
            pending = conn.execute(
                """SELECT rr.id, rr.discord_id, rr.discord_username,
                          rr.display_name, rr.requested_at
                   FROM registration_requests rr
                   WHERE rr.status = 'pending'
                   ORDER BY rr.requested_at ASC"""
            ).fetchall()

        if not pending:
            await interaction.response.send_message(
                "No pending registration requests.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Pending registrations ({len(pending)})",
            color=0x7C3AED,
        )
        lines = []
        for r in pending:
            lines.append(
                f"**{r['display_name']}** · `{r['discord_username']}` · "
                f"<@{r['discord_id']}> · requested {r['requested_at'][:16]}"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(
            text="Approve or deny each request using the buttons in #cfcp-logs."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Export week", style=discord.ButtonStyle.secondary,
                       custom_id="admin:export_week", row=3)
    async def export_week(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        from cogs.stats import export_week
        await export_week(interaction, interaction.client)

    # ── Row 4: Player management ─────────────────────────────────────────────
    @discord.ui.button(label="Remove player", style=discord.ButtonStyle.danger,
                       custom_id="admin:remove_player", row=4)
    async def remove_player(self, interaction: discord.Interaction,
                            button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        with get_db() as conn:
            players = conn.execute(
                """SELECT id, display_name, discord_username, status
                   FROM players ORDER BY display_name"""
            ).fetchall()

        if not players:
            await interaction.response.send_message(
                "No players registered yet.", ephemeral=True
            )
            return

        view = RemovePlayerSelectView(players, interaction.client)
        await interaction.response.send_message(
            "⚠️ Select a player to **permanently remove**. "
            "All their picks, scores, and data will be deleted.",
            view=view, ephemeral=True,
        )


# ── Remove player flow ────────────────────────────────────────────────────────

class RemovePlayerSelectMenu(discord.ui.Select):
    def __init__(self, players: list, bot: commands.Bot):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=f"{p['display_name']}"[:100],
                value=str(p["id"]),
                description=f"{p['discord_username']} · {p['status']}"[:100],
            )
            for p in players[:25]
        ]
        super().__init__(placeholder="Select a player to remove…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        player_id = int(self.values[0])
        with get_db() as conn:
            player = conn.execute(
                "SELECT * FROM players WHERE id=?", (player_id,)
            ).fetchone()
        if not player:
            await interaction.response.send_message(
                "Player not found.", ephemeral=True
            )
            return
        view = RemovePlayerConfirmView(
            self.bot, dict(player)
        )
        await interaction.response.edit_message(
            content=(
                f"⚠️ Are you sure you want to permanently remove "
                f"**{player['display_name']}** (`{player['discord_username']}`)?\n\n"
                f"This will delete **all** their picks, scores, and data. "
                f"This cannot be undone."
            ),
            view=view,
        )


class RemovePlayerSelectView(discord.ui.View):
    def __init__(self, players: list, bot: commands.Bot):
        super().__init__(timeout=60)
        self.add_item(RemovePlayerSelectMenu(players, bot))


class RemovePlayerConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player: dict):
        super().__init__(timeout=30)
        self.bot    = bot
        self.player = player

    @discord.ui.button(label="Yes, remove permanently",
                       style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        pid  = self.player["id"]
        name = self.player["display_name"]

        with get_db() as conn:
            conn.execute(
                "DELETE FROM notifications_sent WHERE player_id=?", (pid,)
            )
            conn.execute(
                "DELETE FROM pick_slots WHERE player_id=?", (pid,)
            )
            conn.execute(
                "DELETE FROM picks WHERE player_id=?", (pid,)
            )
            conn.execute(
                "DELETE FROM weekly_scores WHERE player_id=?", (pid,)
            )
            conn.execute(
                "DELETE FROM registration_requests WHERE discord_id=?",
                (self.player["discord_id"],)
            )
            conn.execute(
                "DELETE FROM players WHERE id=?", (pid,)
            )

        from cogs.setup import refresh_picks_hub
        await refresh_picks_hub(self.bot)

        await _log(
            self.bot,
            f"Player **{name}** (`{self.player['discord_username']}`) was "
            f"permanently removed by {interaction.user}. "
            "All picks, scores, and data deleted.",
            title="Player removed", level="warning",
        )
        await interaction.response.edit_message(
            content=f"✅ **{name}** has been permanently removed.",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Cancelled — no changes made.", view=None
        )
        self.stop()


# ── Cog ────────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
    bot.add_view(AdminPanelView())
    log.info("AdminCog loaded and AdminPanelView registered.")

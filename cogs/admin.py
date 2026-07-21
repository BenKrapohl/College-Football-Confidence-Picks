from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import POLL_AP, POLL_CFP, SEASON_YEAR
from database import (get_db, config_get, config_set, get_active_season,
                      create_season, end_active_season)
from utils.helpers import is_admin, log_to_channel, resolve_latest_week
from utils.embeds import game_embed
from utils.espn import fetch_rankings, fetch_week_games, fetch_game_status
from utils.time_utils import now_et

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


# ── Pick split data helper (FIX #4) ────────────────────────────────────────────

async def _get_player_picks_for_game(game_id: int) -> list[dict]:
    """
    Centralized helper to fetch all non-forfeit picks for a game,
    joined with player display names. Used by post_game_embeds() and
    update_single_game_embed() so the pick-split bar / reveal actually renders.
    """
    async with get_db() as conn:
        async with conn.execute(
            """SELECT pk.picked_team, pk.is_forfeit, pl.display_name
               FROM picks pk
               JOIN players pl ON pk.player_id = pl.id
               WHERE pk.game_id=? AND pl.status IN ('active','withdrawn')""",
            (game_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows if r["picked_team"] is not None]


async def _get_total_active_players() -> int:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT COUNT(*) as c FROM players WHERE status IN ('active','withdrawn')"
        ) as cursor:
            row = await cursor.fetchone()
    return row["c"] if row else 0


# ── Game embed posting / updating ──────────────────────────────────────────────

async def post_game_embeds(bot: commands.Bot, week: dict) -> None:
    """Post one embed per game to #cfcp-games for the given week."""
    ch_id = await config_get("channel_games")
    if not ch_id:
        log.warning("channel_games not configured — skipping game embed post.")
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    picks_reveal_raw = await config_get("picks_reveal", "1")
    picks_reveal     = picks_reveal_raw == "1"
    total_players    = await _get_total_active_players()

    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
            (week["id"],)
        ) as cursor:
            games = await cursor.fetchall()

    for game in games:
        game_dict = dict(game)
        player_picks = await _get_player_picks_for_game(game_dict["id"])
        embed = game_embed(
            game_dict,
            player_picks=player_picks,
            picks_reveal=picks_reveal,
            total_players=total_players,
        )
        msg = await ch.send(embed=embed)
        async with get_db() as conn:
            await conn.execute(
                "UPDATE games SET discord_message_id=? WHERE id=?",
                (str(msg.id), game_dict["id"])
            )

    await log_to_channel(
        bot,
        f"Posted {len(games)} game embeds to #cfcp-games for Week {week['week_number']}.",
        title="Games posted",
        level="success",
    )


async def update_single_game_embed(bot: commands.Bot, game_id: int) -> None:
    """Re-render a single game's embed (e.g. after a score update or kickoff lock)."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM games WHERE id=?", (game_id,)) as cursor:
            game = await cursor.fetchone()
    if not game:
        return

    ch_id = await config_get("channel_games")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    if not game["discord_message_id"]:
        return

    try:
        msg = await ch.fetch_message(int(game["discord_message_id"]))
    except (discord.NotFound, discord.HTTPException):
        return

    picks_reveal_raw = await config_get("picks_reveal", "1")
    picks_reveal     = picks_reveal_raw == "1"
    total_players    = await _get_total_active_players()

    game_dict     = dict(game)
    player_picks  = await _get_player_picks_for_game(game_dict["id"])
    embed = game_embed(
        game_dict,
        player_picks=player_picks,
        picks_reveal=picks_reveal,
        total_players=total_players,
    )
    try:
        await msg.edit(embed=embed)
    except discord.HTTPException as exc:
        log.warning(f"Failed to edit game embed {game_id}: {exc}")


async def update_all_game_embeds(bot: commands.Bot, week_id: int) -> None:
    """Refresh all game embeds for a week — e.g. after toggling pick reveal."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT id FROM games WHERE week_id=?", (week_id,)
        ) as cursor:
            games = await cursor.fetchall()
    for g in games:
        await update_single_game_embed(bot, g["id"])


# ── Load Week flow ───────────────────────────────────────────────────────────

class LoadWeekModal(discord.ui.Modal, title="Load a week"):
    week_number = discord.ui.TextInput(
        label="Week number",
        placeholder="e.g. 1",
        min_length=1, max_length=2,
    )
    start_date = discord.ui.TextInput(
        label="Start date (YYYYMMDD)",
        placeholder="e.g. 20260829",
        min_length=8, max_length=8,
    )
    end_date = discord.ui.TextInput(
        label="End date (YYYYMMDD)",
        placeholder="e.g. 20260901",
        min_length=8, max_length=8,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            wk_num = int(self.week_number.value.strip())
        except ValueError:
            await interaction.response.send_message(
                f"`{self.week_number.value}` is not a valid week number. "
                "Please enter a whole number (e.g. `1`).",
                ephemeral=True,
            )
            return

        start_raw = self.start_date.value.strip()
        end_raw   = self.end_date.value.strip()

        try:
            start_dt = datetime.strptime(start_raw, "%Y%m%d")
            end_dt   = datetime.strptime(end_raw, "%Y%m%d")
        except ValueError:
            await interaction.response.send_message(
                "Dates must be in `YYYYMMDD` format, e.g. `20260829`.",
                ephemeral=True,
            )
            return

        if end_dt < start_dt:
            await interaction.response.send_message(
                "End date must be on or after the start date.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        season = await get_active_season()
        if not season:
            await create_season(SEASON_YEAR, POLL_AP)
            season = await get_active_season()

        poll_type = await config_get("poll_type", season["poll_type"] or POLL_AP)

        try:
            ranked_teams = await fetch_rankings(poll_type)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Failed to fetch rankings from ESPN: `{exc}`\n"
                "Try again in a moment.",
                ephemeral=True,
            )
            return

        if not ranked_teams:
            await interaction.followup.send(
                "⚠️ ESPN returned no ranked teams for the current poll. "
                "This can happen during preseason. Try again later or "
                "check ESPN's rankings page.",
                ephemeral=True,
            )
            return

        try:
            games = await fetch_week_games(
                start_dt.strftime("%Y%m%d"),
                end_dt.strftime("%Y%m%d"),
                ranked_teams,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Failed to fetch schedule from ESPN: `{exc}`", ephemeral=True
            )
            return

        if not games:
            await interaction.followup.send(
                f"No games involving ranked teams found between "
                f"`{start_raw}` and `{end_raw}`. Double check the date range.",
                ephemeral=True,
            )
            return

        async with get_db() as conn:
            async with conn.execute(
                "SELECT * FROM weeks WHERE season_id=? AND week_number=?",
                (season["id"], wk_num)
            ) as cursor:
                existing_week = await cursor.fetchone()

            if existing_week:
                week_id = existing_week["id"]
                await conn.execute(
                    """UPDATE weeks SET start_date=?, end_date=?,
                       game_count=?, loaded_at=datetime('now'), recap_sent=0
                       WHERE id=?""",
                    (start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"),
                     len(games), week_id)
                )
            else:
                async with conn.execute(
                    """INSERT INTO weeks(season_id, week_number, start_date,
                       end_date, game_count, loaded_at)
                       VALUES (?,?,?,?,?,datetime('now'))""",
                    (season["id"], wk_num, start_dt.strftime("%Y-%m-%d"),
                     end_dt.strftime("%Y-%m-%d"), len(games))
                ) as cursor:
                    week_id = cursor.lastrowid

            # ── FIX: STALE GAME DELETION PENALTY PATCH ──
            new_espn_ids = {g["espn_game_id"] for g in games}
            async with conn.execute(
                """SELECT id, espn_game_id, home_team, away_team, status
                   FROM games
                   WHERE week_id=? AND is_manually_added=0
                     AND status != 'final'""",
                (week_id,)
            ) as cursor:
                stale_games = await cursor.fetchall()
            
            async with conn.execute("SELECT game_count FROM weeks WHERE id=?", (week_id,)) as cursor:
                old_gc_row = await cursor.fetchone()
                old_game_count = old_gc_row["game_count"] if old_gc_row else len(games)

            removed_stale = []
            for sg in stale_games:
                if sg["espn_game_id"] not in new_espn_ids:
                    game_id = sg["id"]
                    
                    async with conn.execute(
                        "SELECT player_id, confidence_points, game_id FROM picks WHERE game_id IN (SELECT id FROM games WHERE week_id=?)",
                        (week_id,)
                    ) as cursor:
                        all_picks = await cursor.fetchall()
                        
                    player_picks = {}
                    for p in all_picks:
                        player_picks.setdefault(p["player_id"], []).append(dict(p))
                        
                    await conn.execute("DELETE FROM pick_slots WHERE game_id=?", (game_id,))
                    await conn.execute("DELETE FROM picks WHERE game_id=?", (game_id,))
                    await conn.execute("DELETE FROM games WHERE id=?", (game_id,))
                    
                    for pid, p_picks in player_picks.items():
                        voided_pick = next((p for p in p_picks if p["game_id"] == game_id), None)
                        used_slots = {p["confidence_points"] for p in p_picks}
                        
                        if voided_pick:
                            x = voided_pick["confidence_points"]
                        else:
                            empty_slots = [s for s in range(1, old_game_count + 1) if s not in used_slots]
                            x = max(empty_slots) if empty_slots else old_game_count
                            
                        picks_to_shift = [p for p in p_picks if p["game_id"] != game_id and p["confidence_points"] > x]
                        picks_to_shift.sort(key=lambda p: p["confidence_points"]) 
                        
                        for p in picks_to_shift:
                            old_pts = p["confidence_points"]
                            new_pts = old_pts - 1
                            await conn.execute("UPDATE picks SET confidence_points=? WHERE player_id=? AND game_id=?", (new_pts, pid, p["game_id"]))
                            await conn.execute("DELETE FROM pick_slots WHERE player_id=? AND week_id=? AND confidence_points=?", (pid, week_id, old_pts))
                            await conn.execute("INSERT INTO pick_slots (player_id, week_id, confidence_points, game_id) VALUES (?, ?, ?, ?)", (pid, week_id, new_pts, p["game_id"]))

                    old_game_count -= 1
                    removed_stale.append(f"{sg['home_team']} vs {sg['away_team']}")
            # ─────────────────────────────────────────────

            for g in games:
                await conn.execute("""
                    INSERT INTO games(week_id, espn_game_id, home_team, away_team,
                        home_rank, away_rank, spread, over_under, kickoff_time,
                        channel, espn_link, status, home_score, away_score, winner)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(week_id, espn_game_id) DO UPDATE SET
                        home_rank   = excluded.home_rank,
                        away_rank   = excluded.away_rank,
                        spread      = excluded.spread,
                        over_under  = excluded.over_under,
                        kickoff_time= excluded.kickoff_time,
                        channel     = excluded.channel,
                        status      = excluded.status,
                        home_score  = excluded.home_score,
                        away_score  = excluded.away_score,
                        winner      = excluded.winner
                """, (
                    week_id, g["espn_game_id"], g["home_team"], g["away_team"],
                    g["home_rank"], g["away_rank"], g["spread"], g["over_under"],
                    g["kickoff_time"], g["channel"], g["espn_link"], g["status"],
                    g["home_score"], g["away_score"], g["winner"],
                ))

            async with conn.execute(
                "SELECT COUNT(*) as c FROM games WHERE week_id=?", (week_id,)
            ) as cursor:
                total_count = (await cursor.fetchone())["c"]
                
            await conn.execute(
                "UPDATE weeks SET game_count=? WHERE id=?",
                (total_count, week_id)
            )

            async with conn.execute("SELECT * FROM weeks WHERE id=?", (week_id,)) as cursor:
                week = await cursor.fetchone()

        await post_game_embeds(interaction.client, dict(week))

        from cogs.setup import refresh_admin_panel
        await refresh_admin_panel(interaction.client)

        from cogs.picks import _refresh_picks_hub
        await _refresh_picks_hub(interaction.client)

        result_msg = (
            f"✅ **Week {wk_num}** loaded with **{len(games)}** ranked games "
            f"(`{start_raw}` → `{end_raw}`)."
        )
        if removed_stale:
            result_msg += (
                f"\n\n⚠️ Removed **{len(removed_stale)}** game(s) no longer "
                f"in ESPN's schedule for this week: "
                + ", ".join(removed_stale)
            )
        await interaction.followup.send(result_msg, ephemeral=True)

        await log_to_channel(
            interaction.client,
            f"**Week {wk_num}** loaded by {interaction.user.mention} — "
            f"{len(games)} games" +
            (f", {len(removed_stale)} stale game(s) removed (grids mathematically shifted)" if removed_stale else ""),
            title="Week loaded",
            level="success",
        )


# ── Add Manual Game flow ────────────────────────────────────────────────────────

class AddManualGameModal(discord.ui.Modal, title="Add a game manually"):
    home_team = discord.ui.TextInput(label="Home team", max_length=64)
    away_team = discord.ui.TextInput(label="Away team", max_length=64)
    home_rank = discord.ui.TextInput(
        label="Home rank (blank if unranked)", required=False, max_length=2
    )
    away_rank = discord.ui.TextInput(
        label="Away rank (blank if unranked)", required=False, max_length=2
    )
    kickoff = discord.ui.TextInput(
        label="Kickoff (YYYY-MM-DD HH:MM, 24h, ET)",
        placeholder="e.g. 2026-09-06 19:30",
        max_length=16,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        season, week = await resolve_latest_week()
        if not week:
            await interaction.response.send_message(
                "No week loaded — load a week first.", ephemeral=True
            )
            return

        try:
            kickoff_dt = datetime.strptime(
                self.kickoff.value.strip(), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=ET)
        except ValueError:
            await interaction.response.send_message(
                "Kickoff must be in `YYYY-MM-DD HH:MM` format (24-hour, ET).",
                ephemeral=True,
            )
            return

        def _parse_rank(v: str) -> Optional[int]:
            v = v.strip()
            if not v:
                return None
            try:
                r = int(v)
                return r if 1 <= r <= 25 else None
            except ValueError:
                return None

        home_rank = _parse_rank(self.home_rank.value)
        away_rank = _parse_rank(self.away_rank.value)

        manual_id = f"manual_{week['id']}_{int(now_et().timestamp())}"

        async with get_db() as conn:
            await conn.execute("""
                INSERT INTO games(week_id, espn_game_id, home_team, away_team,
                    home_rank, away_rank, kickoff_time, status, is_manually_added)
                VALUES (?,?,?,?,?,?,?, 'scheduled', 1)
            """, (
                week["id"], manual_id,
                self.home_team.value.strip(), self.away_team.value.strip(),
                home_rank, away_rank, kickoff_dt.isoformat(),
            ))
            
            async with conn.execute(
                "SELECT id FROM games WHERE espn_game_id=?", (manual_id,)
            ) as cursor:
                new_game_id = (await cursor.fetchone())["id"]

            await conn.execute(
                "UPDATE weeks SET game_count = game_count + 1 WHERE id=?",
                (week["id"],)
            )
            
            async with conn.execute("SELECT * FROM weeks WHERE id=?", (week["id"],)) as cursor:
                week = await cursor.fetchone()
                
            async with conn.execute("SELECT * FROM games WHERE id=?", (new_game_id,)) as cursor:
                game = await cursor.fetchone()

        ch_id = await config_get("channel_games")
        if ch_id:
            ch = interaction.client.get_channel(int(ch_id))
            if isinstance(ch, discord.TextChannel):
                player_picks  = await _get_player_picks_for_game(game["id"])
                picks_reveal  = await config_get("picks_reveal", "1") == "1"
                total_players = await _get_total_active_players()
                embed = game_embed(
                    dict(game), player_picks=player_picks,
                    picks_reveal=picks_reveal, total_players=total_players,
                )
                msg = await ch.send(embed=embed)
                async with get_db() as conn:
                    await conn.execute(
                        "UPDATE games SET discord_message_id=? WHERE id=?",
                        (str(msg.id), game["id"])
                    )

        from cogs.setup import refresh_admin_panel
        await refresh_admin_panel(interaction.client)
        from cogs.picks import _refresh_picks_hub
        await _refresh_picks_hub(interaction.client)

        await interaction.response.send_message(
            f"✅ Added **{self.home_team.value} vs {self.away_team.value}** "
            f"to Week {week['week_number']}.\n\n"
            f"⚠️ **Note:** Manual games don't auto-score from ESPN. "
            f"You'll need to use **Set results** to enter the final score "
            f"once this game finishes.",
            ephemeral=True,
        )

        await log_to_channel(
            interaction.client,
            f"Manual game added by {interaction.user.mention}: "
            f"**{self.home_team.value} vs {self.away_team.value}** "
            f"(Week {week['week_number']})",
            title="Manual game added",
        )


# ── Phase 3: Void Game flow (Admin Contingency) ──────────────────────────────

class VoidGameSelectView(discord.ui.View):
    """Lets an admin void a game, perfectly refunding and shifting
    point slots to keep everyone's confidence grid mathematically valid."""
    def __init__(self, bot: commands.Bot, week: dict, games: list):
        super().__init__(timeout=120)
        self.bot  = bot
        self.week = week

        options = []
        for g in games[:25]:
            hr = f"#{g['home_rank']} " if g["home_rank"] else ""
            ar = f"#{g['away_rank']} " if g["away_rank"] else ""
            tag = " (manual)" if g["is_manually_added"] else ""
            options.append(discord.SelectOption(
                label=f"{hr}{g['home_team']} vs {ar}{g['away_team']}{tag}"[:100],
                value=str(g["id"]),
                description=g["status"],
            ))

        select = discord.ui.Select(
            placeholder="Select a matchup to void...", options=options
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        game_id = int(interaction.data["values"][0])
        
        async with get_db() as conn:
            async with conn.execute("SELECT * FROM games WHERE id=?", (game_id,)) as cursor:
                game = await cursor.fetchone()
            if not game:
                await interaction.response.send_message("Game not found.", ephemeral=True)
                return

            # Grab all picks for the entire week to mathematically shift the grid
            async with conn.execute(
                "SELECT player_id, confidence_points, game_id FROM picks WHERE game_id IN (SELECT id FROM games WHERE week_id=?)",
                (self.week["id"],)
            ) as cursor:
                all_picks = await cursor.fetchall()

            player_picks = {}
            for p in all_picks:
                pid = p["player_id"]
                if pid not in player_picks:
                    player_picks[pid] = []
                player_picks[pid].append(dict(p))

            async with conn.execute("SELECT COUNT(*) as c FROM picks WHERE game_id=?", (game_id,)) as cursor:
                has_picks = (await cursor.fetchone())["c"]

            # 1. Delete the voided game and its exact picks
            await conn.execute("DELETE FROM pick_slots WHERE game_id=?", (game_id,))
            await conn.execute("DELETE FROM picks WHERE game_id=?", (game_id,))
            await conn.execute("DELETE FROM games WHERE id=?", (game_id,))
            
            # 2. Update the week's total game count to maintain the 1-to-N rule
            await conn.execute(
                "UPDATE weeks SET game_count = game_count - 1 WHERE id=?",
                (self.week["id"],)
            )

            old_game_count = self.week["game_count"]

            # 3. Shift the points to close the gap for every player
            for pid, picks in player_picks.items():
                voided_pick = next((p for p in picks if p["game_id"] == game_id), None)
                used_slots = {p["confidence_points"] for p in picks}

                # If they picked the voided game, the gap is where their pick was.
                # If they didn't, the gap is their highest unassigned slot.
                if voided_pick:
                    x = voided_pick["confidence_points"]
                else:
                    empty_slots = [s for s in range(1, old_game_count + 1) if s not in used_slots]
                    x = max(empty_slots) if empty_slots else old_game_count

                # Identify all picks HIGHER than the gap, and prepare to shift them down 1 point.
                picks_to_shift = [p for p in picks if p["game_id"] != game_id and p["confidence_points"] > x]
                
                # Sort ascending (x+1, x+2, x+3) so the updates safely cascade into the empty slot 
                # without violating the unique database constraints.
                picks_to_shift.sort(key=lambda p: p["confidence_points"]) 
                
                for p in picks_to_shift:
                    old_pts = p["confidence_points"]
                    new_pts = old_pts - 1
                    
                    await conn.execute(
                        "UPDATE picks SET confidence_points=? WHERE player_id=? AND game_id=?",
                        (new_pts, pid, p["game_id"])
                    )
                    await conn.execute(
                        "DELETE FROM pick_slots WHERE player_id=? AND week_id=? AND confidence_points=?",
                        (pid, self.week["id"], old_pts)
                    )
                    await conn.execute(
                        "INSERT INTO pick_slots (player_id, week_id, confidence_points, game_id) VALUES (?, ?, ?, ?)",
                        (pid, self.week["id"], new_pts, p["game_id"])
                    )

        ch_id = await config_get("channel_games")
        if ch_id and game["discord_message_id"]:
            ch = interaction.client.get_channel(int(ch_id))
            if isinstance(ch, discord.TextChannel):
                try:
                    msg = await ch.fetch_message(int(game["discord_message_id"]))
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

        from cogs.setup import refresh_admin_panel
        await refresh_admin_panel(interaction.client)
        from cogs.picks import _refresh_picks_hub
        await _refresh_picks_hub(interaction.client)

        warn = (
            f"\n\n♻️ **{has_picks}** player(s) had points on this game. Their lower-confidence picks "
            "have been mathematically shifted up by 1 point to close the grid gap, ensuring valid boards."
            if has_picks else "\n\n♻️ The weekly game count has been reduced and all players' available points adjusted."
        )
        
        await interaction.response.edit_message(
            content=(
                f"🛑 **Matchup Voided:** {game['home_team']} vs {game['away_team']} "
                f"has been successfully removed from Week {self.week['week_number']}.{warn}"
            ),
            view=None,
        )

        await log_to_channel(
            interaction.client,
            f"Matchup voided by {interaction.user.mention}: "
            f"**{game['home_team']} vs {game['away_team']}**"
            + (f" ({has_picks} pick(s) shifted/refunded)" if has_picks else ""),
            title="Game Voided", level="warning",
        )


# ── Set Results flow (FIX #18) ──────────────────────────────────────────────────

class SetResultsGameSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, week: dict, games: list):
        super().__init__(timeout=180)
        self.bot  = bot
        self.week = week

        options = []
        for g in games[:25]:
            hr  = f"#{g['home_rank']} " if g["home_rank"] else ""
            ar  = f"#{g['away_rank']} " if g["away_rank"] else ""
            tag = " (manual)" if g["is_manually_added"] else ""
            cur = (
                f"current: {g['home_score']}-{g['away_score']}"
                if g["home_score"] is not None else g["status"]
            )
            options.append(discord.SelectOption(
                label=f"{hr}{g['home_team']} vs {ar}{g['away_team']}{tag}"[:100],
                value=str(g["id"]),
                description=cur[:100],
            ))

        if not options:
            self.add_item(discord.ui.Button(
                label="No games to score", disabled=True,
                style=discord.ButtonStyle.secondary,
            ))
            return

        select = discord.ui.Select(
            placeholder="Select a game to enter results for…", options=options
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        game_id = int(interaction.data["values"][0])
        async with get_db() as conn:
            async with conn.execute("SELECT * FROM games WHERE id=?", (game_id,)) as cursor:
                game = await cursor.fetchone()
                
        if not game:
            await interaction.response.send_message("Game not found.", ephemeral=True)
            return
        await interaction.response.send_modal(SetResultModal(self.bot, dict(game)))


class SetResultModal(discord.ui.Modal, title="Enter final score"):
    home_score = discord.ui.TextInput(label="Home score", max_length=3)
    away_score = discord.ui.TextInput(label="Away score", max_length=3)

    def __init__(self, bot: commands.Bot, game: dict):
        super().__init__()
        self.bot  = bot
        self.game = game
        self.title = (
            f"{game['home_team']} vs {game['away_team']}"
        )[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            h = int(self.home_score.value.strip())
            a = int(self.away_score.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Scores must be whole numbers.", ephemeral=True
            )
            return
        if h < 0 or a < 0:
            await interaction.response.send_message(
                "Scores can't be negative.", ephemeral=True
            )
            return

        if h > a:
            winner = self.game["home_team"]
        elif a > h:
            winner = self.game["away_team"]
        else:
            winner = None

        async with get_db() as conn:
            await conn.execute(
                """UPDATE games SET home_score=?, away_score=?, winner=?,
                   status='final' WHERE id=?""",
                (h, a, winner, self.game["id"])
            )

        await update_single_game_embed(self.bot, self.game["id"])

        from cogs.scoring import score_single_game
        await score_single_game(self.bot, self.game["id"])

        tie_note = " *(tie — no winner credited)*" if winner is None else ""
        await interaction.response.send_message(
            f"✅ **{self.game['home_team']} {h} — {a} {self.game['away_team']}** "
            f"recorded and scored.{tie_note}",
            ephemeral=True,
        )

        await log_to_channel(
            self.bot,
            f"Manual result entered by {interaction.user.mention}: "
            f"**{self.game['home_team']} {h} — {a} {self.game['away_team']}**"
            f"{tie_note}",
            title="Result entered",
        )


# ── End Season flow (FIX #21) ──────────────────────────────────────────────────

class EndSeasonConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, season: dict):
        super().__init__(timeout=120)
        self.bot    = bot
        self.season = season

    @discord.ui.button(label="Yes, end the season", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        from database import get_season_leaderboard
        from utils.embeds import standings_season_embed

        rows = await get_season_leaderboard(self.season["id"])

        # Export to CSV
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "rank", "display_name", "status", "total_points", "total_correct",
            "total_wrong", "total_forfeits", "total_possible", "weeks_played",
            "best_week"
        ])
        for i, r in enumerate(rows, start=1):
            writer.writerow([
                i, r["display_name"], r["status"], r["total_points"],
                r["total_correct"], r["total_wrong"], r["total_forfeits"],
                r["total_possible"], r["weeks_played"], r["best_week"]
            ])
        buf.seek(0)
        file = discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            filename=f"cfcp_{self.season['year']}_final_standings.csv",
        )

        embed = standings_season_embed(rows, self.season["year"])
        embed.title = f"🏆  {self.season['year']} Final Standings — Season Complete!"

        if rows:
            champ = rows[0]
            embed.add_field(
                name="🎉 Champion",
                value=f"**{champ['display_name']}** with {champ['total_points']} points!",
                inline=False,
            )

        std_ch_id = await config_get("channel_standings")
        if std_ch_id:
            ch = self.bot.get_channel(int(std_ch_id))
            if isinstance(ch, discord.TextChannel):
                await ch.send(embed=embed, file=file)

        await end_active_season()

        await interaction.followup.send(
            f"🏁 **{self.season['year']} season ended.** Final standings posted "
            f"to the standings channel. Use **Load week** with a new week number "
            f"to start a fresh season — you'll be prompted to confirm the new year.",
            ephemeral=True,
        )

        from cogs.setup import refresh_admin_panel
        await refresh_admin_panel(self.bot)

        await log_to_channel(
            self.bot,
            f"**{self.season['year']} season ended** by {interaction.user.mention}.",
            title="Season ended", level="success",
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


# ── Player management ────────────────────────────────────────────────────────

class PlayerManageSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, players: list):
        super().__init__(timeout=120)
        self.bot = bot

        options = [
            discord.SelectOption(
                label=f"{p['display_name']} ({p['status']})"[:100],
                value=str(p["id"]),
            )
            for p in players[:25]
        ]
        if not options:
            self.add_item(discord.ui.Button(
                label="No players found", disabled=True,
                style=discord.ButtonStyle.secondary,
            ))
            return

        select = discord.ui.Select(
            placeholder="Select a player…", options=options
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        player_id = int(interaction.data["values"][0])
        async with get_db() as conn:
            async with conn.execute(
                "SELECT * FROM players WHERE id=?", (player_id,)
            ) as cursor:
                player = await cursor.fetchone()
                
        if not player:
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        view = PlayerActionView(self.bot, dict(player))
        await interaction.response.edit_message(
            content=(
                f"**{player['display_name']}** "
                f"(`{player['discord_username']}`)\n"
                f"Status: `{player['status']}`  ·  "
                f"DMs: {'on' if player['dm_notifications'] else 'off'}"
            ),
            view=view,
        )


class PlayerActionView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player: dict):
        super().__init__(timeout=120)
        self.bot    = bot
        self.player = player

        if player["status"] != "withdrawn":
            withdraw_btn = discord.ui.Button(
                label="Withdraw player", style=discord.ButtonStyle.danger
            )
            withdraw_btn.callback = self._withdraw
            self.add_item(withdraw_btn)
        else:
            reactivate_btn = discord.ui.Button(
                label="Re-activate player", style=discord.ButtonStyle.success
            )
            reactivate_btn.callback = self._reactivate
            self.add_item(reactivate_btn)

        rename_btn = discord.ui.Button(
            label="Rename", style=discord.ButtonStyle.secondary
        )
        rename_btn.callback = self._rename
        self.add_item(rename_btn)

    async def _withdraw(self, interaction: discord.Interaction) -> None:
        async with get_db() as conn:
            await conn.execute(
                "UPDATE players SET status='withdrawn', dm_notifications=0 WHERE id=?",
                (self.player["id"],)
            )
        await interaction.response.edit_message(
            content=f"**{self.player['display_name']}** withdrawn.", view=None
        )
        from cogs.picks import _refresh_picks_hub
        await _refresh_picks_hub(self.bot)
        await log_to_channel(
            self.bot,
            f"**{self.player['display_name']}** withdrawn by "
            f"{interaction.user.mention}.",
            title="Player withdrawn", level="warning",
        )

    async def _reactivate(self, interaction: discord.Interaction) -> None:
        async with get_db() as conn:
            await conn.execute(
                "UPDATE players SET status='active', dm_notifications=1 WHERE id=?",
                (self.player["id"],)
            )
        await interaction.response.edit_message(
            content=f"**{self.player['display_name']}** re-activated.", view=None
        )
        from cogs.picks import _refresh_picks_hub
        await _refresh_picks_hub(self.bot)
        await log_to_channel(
            self.bot,
            f"**{self.player['display_name']}** re-activated by "
            f"{interaction.user.mention}.",
            title="Player re-activated", level="success",
        )

    async def _rename(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RenamePlayerModal(self.bot, self.player))


class RenamePlayerModal(discord.ui.Modal, title="Rename player"):
    new_name = discord.ui.TextInput(label="New display name", max_length=32)

    def __init__(self, bot: commands.Bot, player: dict):
        super().__init__()
        self.bot    = bot
        self.player = player

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new = self.new_name.value.strip()
        async with get_db() as conn:
            async with conn.execute(
                "SELECT id FROM players WHERE LOWER(display_name)=LOWER(?) AND id!=?",
                (new, self.player["id"])
            ) as cursor:
                taken = await cursor.fetchone()
                
        if taken:
            await interaction.response.send_message(
                f"`{new}` is already in use by another player.", ephemeral=True
            )
            return
            
        async with get_db() as conn:
            await conn.execute(
                "UPDATE players SET display_name=? WHERE id=?",
                (new, self.player["id"])
            )
            
        await interaction.response.send_message(
            f"Renamed **{self.player['display_name']}** → **{new}**.", ephemeral=True
        )
        from cogs.picks import _refresh_picks_hub
        await _refresh_picks_hub(self.bot)


# ── Admin panel view ─────────────────────────────────────────────────────────

class AdminPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Load week", style=discord.ButtonStyle.primary,
                       custom_id="admin:load_week", row=0)
    async def load_week(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(LoadWeekModal())

    @discord.ui.button(label="Add manual game", style=discord.ButtonStyle.secondary,
                       custom_id="admin:add_game", row=0)
    async def add_manual_game(self, interaction: discord.Interaction,
                              button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season, week = await resolve_latest_week()
        if not week:
            await interaction.response.send_message(
                "No week loaded — load a week first.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AddManualGameModal())

    @discord.ui.button(label="Void matchup", style=discord.ButtonStyle.danger,
                       custom_id="admin:void_game", row=0)
    async def void_game(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season, week = await resolve_latest_week()
        if not week:
            await interaction.response.send_message(
                "No week loaded.", ephemeral=True
            )
            return
            
        async with get_db() as conn:
            async with conn.execute(
                "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
                (week["id"],)
            ) as cursor:
                games = await cursor.fetchall()
                
        if not games:
            await interaction.response.send_message(
                "No games to remove this week.", ephemeral=True
            )
            return
        view = VoidGameSelectView(interaction.client, dict(week), [dict(g) for g in games])
        await interaction.response.send_message(
            "Select a matchup to void:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Set results", style=discord.ButtonStyle.secondary,
                       custom_id="admin:set_results", row=1)
    async def set_results(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season, week = await resolve_latest_week()
        if not week:
            await interaction.response.send_message(
                "No week loaded.", ephemeral=True
            )
            return
            
        async with get_db() as conn:
            async with conn.execute(
                """SELECT * FROM games WHERE week_id=? AND status != 'final'
                   ORDER BY kickoff_time""",
                (week["id"],)
            ) as cursor:
                games = await cursor.fetchall()
                
        if not games:
            await interaction.response.send_message(
                "All games for the current week are already final. "
                "If you need to *correct* a final score, use **Force re-fetch** "
                "first to reset, or contact a developer for a manual DB fix.",
                ephemeral=True,
            )
            return
        view = SetResultsGameSelectView(interaction.client, dict(week), [dict(g) for g in games])
        await interaction.response.send_message(
            "Select a game to enter a final score for:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Force re-fetch scores", style=discord.ButtonStyle.secondary,
                       custom_id="admin:refetch", row=1)
    async def refetch(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        season, week = await resolve_latest_week()
        if not week:
            await interaction.response.send_message("No week loaded.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        async with get_db() as conn:
            async with conn.execute(
                """SELECT * FROM games WHERE week_id=?
                   AND is_manually_added=0""",
                (week["id"],)
            ) as cursor:
                games = await cursor.fetchall()

        updated = 0
        for game in games:
            result = await fetch_game_status(game["espn_game_id"])
            if not result:
                continue
                
            async with get_db() as conn:
                await conn.execute(
                    """UPDATE games SET status=?, home_score=?, away_score=?,
                       winner=? WHERE id=?""",
                    (result["status"], result["home_score"], result["away_score"],
                     result["winner"], game["id"])
                )
            await update_single_game_embed(interaction.client, game["id"])
            updated += 1

        from cogs.scoring import recalculate_weekly_scores
        await recalculate_weekly_scores(interaction.client, week["id"])

        await interaction.followup.send(
            f"🔄 Re-fetched and updated **{updated}** game(s), and recalculated scores.",
            ephemeral=True,
        )

    @discord.ui.button(label="Toggle pick reveal", style=discord.ButtonStyle.secondary,
                       custom_id="admin:toggle_reveal", row=1)
    async def toggle_reveal(self, interaction: discord.Interaction,
                            button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
            
        current = await config_get("picks_reveal", "1")
        new_val = "0" if current == "1" else "1"
        await config_set("picks_reveal", new_val)

        season, week = await resolve_latest_week()
        if week:
            await update_all_game_embeds(interaction.client, week["id"])

        from cogs.setup import refresh_admin_panel
        await refresh_admin_panel(interaction.client)

        await interaction.response.send_message(
            f"Pick reveal is now **{'ON' if new_val == '1' else 'OFF'}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Switch poll (AP/CFP)", style=discord.ButtonStyle.secondary,
                       custom_id="admin:switch_poll", row=2)
    async def switch_poll(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
            
        current = await config_get("poll_type", POLL_AP)
        new_val = POLL_CFP if current == POLL_AP else POLL_AP
        await config_set("poll_type", new_val)

        season = await get_active_season()
        if season:
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE seasons SET poll_type=? WHERE id=?",
                    (new_val, season["id"])
                )

        from cogs.setup import refresh_admin_panel
        await refresh_admin_panel(interaction.client)

        label = "CFP Rankings" if new_val == POLL_CFP else "AP Top 25"
        await interaction.response.send_message(
            f"Active poll switched to **{label}**. "
            f"This takes effect on the next **Load week**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Manage players", style=discord.ButtonStyle.secondary,
                       custom_id="admin:manage_players", row=2)
    async def manage_players(self, interaction: discord.Interaction,
                             button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
            
        async with get_db() as conn:
            async with conn.execute(
                "SELECT * FROM players WHERE status != 'denied' "
                "ORDER BY display_name"
            ) as cursor:
                players = await cursor.fetchall()
                
        view = PlayerManageSelectView(interaction.client, [dict(p) for p in players])
        await interaction.response.send_message(
            "Select a player to manage:", view=view, ephemeral=True
        )

    @discord.ui.button(label="End season", style=discord.ButtonStyle.danger,
                       custom_id="admin:end_season", row=2)
    async def end_season(self, interaction: discord.Interaction,
                         button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
            
        season = await get_active_season()
        if not season:
            await interaction.response.send_message(
                "No active season to end.", ephemeral=True
            )
            return
            
        view = EndSeasonConfirmView(interaction.client, dict(season))
        await interaction.response.send_message(
            f"Are you sure you want to end the **{season['year']}** season? "
            f"This will post final standings and cannot be undone.",
            view=view, ephemeral=True,
        )


# ── Cog ────────────────────────────────────────────────────────────────────────

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
    bot.add_view(AdminPanelView())
    log.info("AdminCog loaded and AdminPanelView registered.")
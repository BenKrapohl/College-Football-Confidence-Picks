from __future__ import annotations
from typing import Optional
import discord
from discord.ext import commands
from discord.ext import tasks
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import COLOR_PURPLE, COLOR_GREEN
from database import (get_db, config_get, get_all_active_players,
                      get_player_by_discord_id, get_used_slots_for_week)
from utils.helpers import is_admin, log_to_channel, resolve_current_week
from utils.time_utils import format_time_et, seconds_until_iso

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_player(discord_id: str):
    return get_player_by_discord_id(str(discord_id))


def _game_is_locked(game: dict) -> bool:
    if game.get("status") in ("in_progress", "final"):
        return True
    try:
        secs = seconds_until_iso(game["kickoff_time"])
        return secs <= 0
    except Exception:
        return False


async def _refresh_picks_hub(bot: commands.Bot) -> None:
    from cogs.setup import refresh_picks_hub
    season, week = resolve_current_week()
    if not week:
        await refresh_picks_hub(bot)
        return
    with get_db() as conn:
        game_rows = conn.execute(
            "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
            (week["id"],)
        ).fetchall()
    players     = get_all_active_players()
    player_list = []
    with get_db() as conn:
        for p in players:
            count = conn.execute(
                """SELECT COUNT(*) as c FROM picks pk
                   JOIN games g ON pk.game_id = g.id
                   WHERE pk.player_id=? AND g.week_id=? AND pk.is_forfeit=0""",
                (p["id"], week["id"])
            ).fetchone()["c"]
            player_list.append({
                **dict(p),
                "submitted": count >= week["game_count"]
            })
    await refresh_picks_hub(
        bot,
        week=dict(week),
        games=[dict(g) for g in game_rows],
        players=player_list,
    )


# ── Registration ───────────────────────────────────────────────────────────────

class RegistrationModal(discord.ui.Modal, title="Join CFCP"):
    display_name = discord.ui.TextInput(
        label="Display name (shown on leaderboards)",
        placeholder="e.g. Ben",
        min_length=2, max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        discord_id = str(interaction.user.id)
        username   = str(interaction.user)
        name       = self.display_name.value.strip()

        existing = _get_player(discord_id)
        if existing:
            status = existing["status"]
            if status == "active":
                await interaction.response.send_message(
                    "You're already registered and active!", ephemeral=True
                )
                return
            if status == "withdrawn":
                with get_db() as conn:
                    conn.execute(
                        "UPDATE players SET status='active', display_name=? WHERE discord_id=?",
                        (name, discord_id)
                    )
                await interaction.response.send_message(
                    f"Welcome back, **{name}**! You've been re-activated. "
                    "Your previous scores are still on record.",
                    ephemeral=True,
                )
                await _refresh_picks_hub(interaction.client)
                return
            if status == "pending":
                await interaction.response.send_message(
                    "Your registration is already pending admin approval.",
                    ephemeral=True,
                )
                return

        with get_db() as conn:
            pending = conn.execute(
                "SELECT id FROM registration_requests "
                "WHERE discord_id=? AND status='pending'",
                (discord_id,)
            ).fetchone()

        if pending:
            await interaction.response.send_message(
                "You already have a pending registration request.", ephemeral=True
            )
            return

        with get_db() as conn:
            taken = conn.execute(
                "SELECT id FROM players WHERE LOWER(display_name)=LOWER(?)",
                (name,)
            ).fetchone()
        if taken:
            await interaction.response.send_message(
                f"The display name **{name}** is already taken. "
                "Please choose a different one.",
                ephemeral=True,
            )
            return

        with get_db() as conn:
            conn.execute(
                """INSERT INTO registration_requests
                   (discord_id, discord_username, display_name, status)
                   VALUES (?,?,?,'pending')""",
                (discord_id, username, name),
            )
            conn.execute(
                """INSERT OR IGNORE INTO players
                   (discord_id, discord_username, display_name, status)
                   VALUES (?,?,?,'pending')""",
                (discord_id, username, name),
            )

        await interaction.response.send_message(
            f"✅ Registration submitted for **{name}**! "
            "An admin will review your request shortly. "
            "You'll receive a DM once approved.",
            ephemeral=True,
        )

        bot: commands.Bot = interaction.client
        await _notify_admins_registration(bot, discord_id, username, name)


async def _notify_admins_registration(bot: commands.Bot, discord_id: str,
                                      username: str, display_name: str) -> None:
    ch_id = config_get("channel_logs")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return

    with get_db() as conn:
        req = conn.execute(
            "SELECT id FROM registration_requests "
            "WHERE discord_id=? ORDER BY id DESC LIMIT 1",
            (discord_id,)
        ).fetchone()
    if not req:
        return

    embed = discord.Embed(
        title="New registration request",
        description=(
            f"**{display_name}** (`{username}`) wants to join CFCP.\n"
            f"Discord ID: `{discord_id}`"
        ),
        color=COLOR_PURPLE,
    )
    view = ApprovalView(req["id"], discord_id, display_name, bot)
    await ch.send(embed=embed, view=view)


class ApprovalView(discord.ui.View):
    def __init__(self, request_id: int, discord_id: str,
                 display_name: str, bot: commands.Bot):
        super().__init__(timeout=None)
        self.request_id   = request_id
        self.discord_id   = discord_id
        self.display_name = display_name
        self.bot          = bot

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        season, week = resolve_current_week()
        joined_week  = week["week_number"] if week else None

        with get_db() as conn:
            conn.execute(
                """UPDATE players SET status='active', joined_week=?
                   WHERE discord_id=?""",
                (joined_week, self.discord_id),
            )
            conn.execute(
                """UPDATE registration_requests SET status='approved',
                   reviewed_at=datetime('now'), reviewed_by=?
                   WHERE id=?""",
                (str(interaction.user), self.request_id),
            )

        for item in self.children:
            item.disabled = True  
        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f"✅ **{self.display_name}** approved.", ephemeral=True
        )

        try:
            user = await self.bot.fetch_user(int(self.discord_id))
            await user.send(
                "🏈 You've been approved for **College Football Confidence Picks**!\n"
                "Head to the server and use the **Submit picks** button "
                "in #cfcp-picks to get started."
            )
        except discord.Forbidden:
            await log_to_channel(
                self.bot,
                f"Could not DM **{self.display_name}** — they have DMs disabled. "
                "A mention has been posted in #cfcp-picks instead.",
                title="DM failed", level="warning",
            )
            picks_ch_id = config_get("channel_picks")
            if picks_ch_id:
                picks_ch = self.bot.get_channel(int(picks_ch_id))
                if isinstance(picks_ch, discord.TextChannel):
                    await picks_ch.send(
                        f"<@{self.discord_id}> — you've been approved for CFCP! "
                        f"Use the **Submit picks** button above to get started.\n\n"
                        f"📬 **Action required — enable DMs:**\n"
                        f"This bot sends important notifications via DM throughout "
                        f"the season. To enable them:\n"
                        f"1. Right-click the server name → **Privacy Settings**\n"
                        f"2. Turn on **Direct Messages**\n\n"
                        f"*(This message will delete itself in 5 minutes.)*",
                        delete_after=300,
                    )
        except Exception as exc:
            await log_to_channel(
                self.bot,
                f"Unexpected error DMing **{self.display_name}**: {exc}",
                title="DM failed", level="error",
            )

        await _refresh_picks_hub(self.bot)
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction,
                   button: discord.ui.Button) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        with get_db() as conn:
            conn.execute(
                """UPDATE players SET status='denied'
                   WHERE discord_id=? AND status='pending'""",
                (self.discord_id,),
            )
            conn.execute(
                """UPDATE registration_requests SET status='denied',
                   reviewed_at=datetime('now'), reviewed_by=?
                   WHERE id=?""",
                (str(interaction.user), self.request_id),
            )

        for item in self.children:
            item.disabled = True  
        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f"**{self.display_name}** denied. They can reapply at any time.",
            ephemeral=True,
        )

        try:
            user = await self.bot.fetch_user(int(self.discord_id))
            await user.send(
                "Your CFCP registration request was not approved at this time. "
                "You're welcome to apply again whenever you'd like."
            )
        except discord.Forbidden:
            await log_to_channel(
                self.bot,
                f"Could not DM **{self.display_name}** denial notice — DMs disabled.",
                title="DM failed", level="warning",
            )
        except Exception as exc:
            await log_to_channel(
                self.bot,
                f"Unexpected error DMing **{self.display_name}**: {exc}",
                title="DM failed", level="error",
            )

        self.stop()


# ── Picks submission flow ──────────────────────────────────────────────────────

def _build_picks_embed(player_id: int, week: dict,
                       games: list, title: str = "Your picks") -> discord.Embed:
    with get_db() as conn:
        existing = conn.execute(
            """SELECT pk.game_id, pk.picked_team, pk.confidence_points,
                      g.home_team, g.away_team, g.kickoff_time, g.home_rank,
                      g.away_rank, g.status as game_status
               FROM picks pk
               JOIN games g ON pk.game_id = g.id
               WHERE pk.player_id=? AND g.week_id=? AND pk.is_forfeit=0
               ORDER BY pk.confidence_points DESC""",
            (player_id, week["id"])
        ).fetchall()

    picked_ids = {r["game_id"] for r in existing}
    remaining  = [g for g in games if g["id"] not in picked_ids
                  and not _game_is_locked(g)]

    e = discord.Embed(title=f"🏈  Week {week['week_number']} — {title}",
                      color=COLOR_PURPLE)

    if existing:
        pick_lines = []
        for r in existing:
            hr        = f"#{r['home_rank']} " if r["home_rank"] else ""
            ar        = f"#{r['away_rank']} " if r["away_rank"] else ""
            locked    = r["game_status"] in ("in_progress", "final")
            lock_icon = "🔒 " if locked else ""
            pick_lines.append(
                f"`{r['confidence_points']:>2}` {lock_icon}{r['picked_team']}  "
                f"*({hr}{r['home_team']} vs {ar}{r['away_team']})*"
            )
        e.add_field(
            name=f"Confirmed picks ({len(existing)}/{week['game_count']})",
            value="\n".join(pick_lines),
            inline=False,
        )

    if remaining:
        rem_lines = []
        for g in remaining:
            hr      = f"#{g['home_rank']} " if g.get("home_rank") else ""
            ar      = f"#{g['away_rank']} " if g.get("away_rank") else ""
            kickoff = format_time_et(
                datetime.fromisoformat(g["kickoff_time"]), include_date=True
            )
            rem_lines.append(
                f"• {hr}{g['home_team']} vs {ar}{g['away_team']}  —  {kickoff}"
            )
        e.add_field(
            name=f"Still to pick ({len(remaining)})",
            value="\n".join(rem_lines[:15]) +
                  (f"\n*…and {len(rem_lines)-15} more*" if len(rem_lines) > 15 else ""),
            inline=False,
        )
    elif not existing:
        e.description = "No games available to pick this week."
    else:
        e.add_field(name="All done!", value="You've made all your picks.", inline=False)

    e.set_footer(text="Picks lock at each game's kickoff time.")
    return e


class PicksHubEphemeralView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player_id: int,
                 week: dict, games: list):
        super().__init__(timeout=300)
        self.bot       = bot
        self.player_id = player_id
        self.week      = week
        self.games     = games
        self._rebuild_select()

    def _rebuild_select(self) -> None:
        self.clear_items()

        with get_db() as conn:
            picked_ids = {
                r["game_id"] for r in conn.execute(
                    """SELECT game_id FROM picks
                       WHERE player_id=? AND is_forfeit=0
                       AND game_id IN (SELECT id FROM games WHERE week_id=?)""",
                    (self.player_id, self.week["id"])
                ).fetchall()
            }

        available = [g for g in self.games
                     if g["id"] not in picked_ids and not _game_is_locked(g)]

        if available:
            options = []
            for g in available[:25]:
                hr      = f"#{g['home_rank']} " if g.get("home_rank") else ""
                ar      = f"#{g['away_rank']} " if g.get("away_rank") else ""
                kickoff = format_time_et(
                    datetime.fromisoformat(g["kickoff_time"]), include_date=True
                )
                options.append(discord.SelectOption(
                    label=f"{hr}{g['home_team']} vs {ar}{g['away_team']}"[:100],
                    value=str(g["id"]),
                    description=kickoff[:100],
                ))
            select = discord.ui.Select(
                placeholder="Select a game to pick…",
                options=options,
                custom_id="picks_hub_select",
            )
            select.callback = self._on_game_select
            self.add_item(select)

        close_btn = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.secondary,
            custom_id="picks_hub_close",
        )
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _on_game_select(self, interaction: discord.Interaction) -> None:
        game_id = int(interaction.data["values"][0])  
        game    = next((g for g in self.games if g["id"] == game_id), None)
        if not game:
            await interaction.response.send_message(
                "Game not found.", ephemeral=True
            )
            return
        view  = TeamSelectView(self.bot, self.player_id, self.week, self.games, game)
        embed = _build_team_embed(game)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Picks session closed.", embed=None, view=None
        )
        self.stop()


def _build_team_embed(game: dict) -> discord.Embed:
    hr      = f"#{game['home_rank']} " if game.get("home_rank") else ""
    ar      = f"#{game['away_rank']} " if game.get("away_rank") else ""
    kickoff = format_time_et(
        datetime.fromisoformat(game["kickoff_time"]), include_date=True
    )
    e = discord.Embed(
        title=f"{hr}{game['home_team']}  vs  {ar}{game['away_team']}",
        color=COLOR_PURPLE,
    )
    e.add_field(name="Kickoff", value=kickoff, inline=True)
    if game.get("channel"):
        e.add_field(name="Channel", value=game["channel"], inline=True)
    if game.get("spread"):
        e.add_field(name="Spread", value=game["spread"], inline=True)
    if game.get("over_under"):
        e.add_field(name="O/U", value=game["over_under"], inline=True)
    if game.get("espn_link"):
        e.add_field(name="ESPN", value=f"[Game page]({game['espn_link']})", inline=True)
    e.set_footer(text="Which team do you think will win?")
    return e


class TeamSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player_id: int,
                 week: dict, games: list, game: dict):
        super().__init__(timeout=300)
        self.bot       = bot
        self.player_id = player_id
        self.week      = week
        self.games     = games
        self.game      = game

        hr = f"#{game['home_rank']} " if game.get("home_rank") else ""
        ar = f"#{game['away_rank']} " if game.get("away_rank") else ""

        home_btn = discord.ui.Button(
            label=f"{hr}{game['home_team']}"[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"team_home_{game['id']}",
        )
        home_btn.callback = self._pick_home
        self.add_item(home_btn)

        away_btn = discord.ui.Button(
            label=f"{ar}{game['away_team']}"[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"team_away_{game['id']}",
        )
        away_btn.callback = self._pick_away
        self.add_item(away_btn)

        back_btn = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
            custom_id=f"team_back_{game['id']}",
        )
        back_btn.callback = self._go_back
        self.add_item(back_btn)

    async def _pick_home(self, interaction: discord.Interaction) -> None:
        await self._pick_team(interaction, self.game["home_team"])

    async def _pick_away(self, interaction: discord.Interaction) -> None:
        await self._pick_team(interaction, self.game["away_team"])

    async def _pick_team(self, interaction: discord.Interaction,
                         team: str) -> None:
        if _game_is_locked(self.game):
            await interaction.response.send_message(
                "This game has already kicked off — picks are locked.",
                ephemeral=True,
            )
            return
        view  = SlotSelectView(
            self.bot, self.player_id, self.week,
            self.games, self.game, team
        )
        embed = _build_slot_embed(self.player_id, self.week, self.game, team)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _go_back(self, interaction: discord.Interaction) -> None:
        hub_view = PicksHubEphemeralView(
            self.bot, self.player_id, self.week, self.games
        )
        embed = _build_picks_embed(self.player_id, self.week, self.games)
        await interaction.response.edit_message(embed=embed, view=hub_view)


def _build_slot_embed(player_id: int, week: dict,
                      game: dict, picked_team: str) -> discord.Embed:
    total_games = week["game_count"]

    e = discord.Embed(
        title=f"You picked: {picked_team}",
        description="Assign a confidence value to this pick.\n"
                    "Higher = more confident. Each value can only be used once.",
        color=COLOR_GREEN,
    )

    with get_db() as conn:
        slot_map = {
            r["confidence_points"]: r
            for r in conn.execute(
                """SELECT ps.confidence_points, g.home_team, g.away_team,
                          pk.picked_team, g.status as game_status
                   FROM pick_slots ps
                   JOIN games g ON ps.game_id = g.id
                   JOIN picks pk ON pk.player_id = ps.player_id
                                AND pk.game_id   = ps.game_id
                   WHERE ps.player_id=? AND ps.week_id=?""",
                (player_id, week["id"])
            ).fetchall()
        }

    lines = []
    for slot in range(total_games, 0, -1):
        if slot in slot_map:
            r         = slot_map[slot]
            lock_icon = "🔒 " if r["game_status"] in ("in_progress", "final") else ""
            lines.append(
                f"`{slot:>2}` ✅ {lock_icon}{r['picked_team']} "
                f"*({r['home_team']} vs {r['away_team']})*"
            )
        else:
            lines.append(f"`{slot:>2}` — *empty*")

    e.add_field(
        name="Current slot assignments",
        value="\n".join(lines[:20]) +
              (f"\n*…and {len(lines)-20} more*" if len(lines) > 20 else ""),
        inline=False,
    )
    e.set_footer(text="Select a slot number below.")
    return e


class SlotSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player_id: int, week: dict,
                 games: list, game: dict, picked_team: str):
        super().__init__(timeout=300)
        self.bot         = bot
        self.player_id   = player_id
        self.week        = week
        self.games       = games
        self.game        = game
        self.picked_team = picked_team

        used_slots  = get_used_slots_for_week(player_id, week["id"])
        total_games = week["game_count"]

        with get_db() as conn:
            existing = conn.execute(
                "SELECT confidence_points FROM picks "
                "WHERE player_id=? AND game_id=?",
                (player_id, game["id"])
            ).fetchone()
        self.current_slot: Optional[int] = (
            existing["confidence_points"] if existing else None
        )

        options = []
        for slot in range(total_games, 0, -1):
            if slot in used_slots and slot != self.current_slot:
                with get_db() as conn:
                    occupant = conn.execute(
                        """SELECT g.home_team, g.away_team, pk.picked_team
                           FROM pick_slots ps
                           JOIN games g ON ps.game_id = g.id
                           JOIN picks pk ON pk.player_id=ps.player_id
                                        AND pk.game_id=ps.game_id
                           WHERE ps.player_id=? AND ps.week_id=?
                             AND ps.confidence_points=?""",
                        (player_id, week["id"], slot)
                    ).fetchone()
                desc = (
                    f"Override: {occupant['picked_team']}"
                    f" ({occupant['home_team']} vs {occupant['away_team']})"[:100]
                    if occupant else "Override existing pick"
                )
                options.append(discord.SelectOption(
                    label=f"{slot} pts  ⚠ occupied",
                    value=f"override_{slot}",
                    description=desc,
                ))
            else:
                label = f"{slot} pts"
                if slot == self.current_slot:
                    label += "  (current)"
                options.append(discord.SelectOption(
                    label=label, value=str(slot)
                ))

        select = discord.ui.Select(
            placeholder="Choose confidence points…",
            options=options[:25],
            custom_id="slot_select",
        )
        select.callback = self._on_slot_select
        self.add_item(select)

        back_btn = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
            custom_id="slot_back",
        )
        back_btn.callback = self._go_back
        self.add_item(back_btn)

    async def _on_slot_select(self, interaction: discord.Interaction) -> None:
        value = interaction.data["values"][0]  

        if value.startswith("override_"):
            slot = int(value.split("_")[1])
            view = OverrideConfirmView(
                self.bot, self.player_id, self.week,
                self.games, self.game, self.picked_team, slot
            )
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Override existing pick?",
                    description=(
                        f"Slot **{slot}** is already assigned to another game.\n"
                        f"Assigning it to **{self.picked_team}** will remove "
                        "the other game's pick and put it back in your remaining list."
                    ),
                    color=0xd97706,
                ),
                view=view,
            )
        else:
            slot = int(value)
            await _save_pick(
                interaction, self.bot, self.player_id, self.week,
                self.games, self.game, self.picked_team, slot
            )

    async def _go_back(self, interaction: discord.Interaction) -> None:
        view  = TeamSelectView(
            self.bot, self.player_id, self.week, self.games, self.game
        )
        embed = _build_team_embed(self.game)
        await interaction.response.edit_message(embed=embed, view=view)


class OverrideConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player_id: int, week: dict,
                 games: list, game: dict, picked_team: str, slot: int):
        super().__init__(timeout=120)
        self.bot         = bot
        self.player_id   = player_id
        self.week        = week
        self.games       = games
        self.game        = game
        self.picked_team = picked_team
        self.slot        = slot

    @discord.ui.button(label="Yes, override", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        await _save_pick(
            interaction, self.bot, self.player_id, self.week,
            self.games, self.game, self.picked_team, self.slot,
            override=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        view  = SlotSelectView(
            self.bot, self.player_id, self.week,
            self.games, self.game, self.picked_team
        )
        embed = _build_slot_embed(
            self.player_id, self.week, self.game, self.picked_team
        )
        await interaction.response.edit_message(embed=embed, view=view)


async def _save_pick(interaction: discord.Interaction, bot: commands.Bot,
                     player_id: int, week: dict, games: list,
                     game: dict, picked_team: str, slot: int,
                     override: bool = False) -> None:
    if _game_is_locked(game):
        await interaction.response.send_message(
            "This game has kicked off — pick is now locked.", ephemeral=True
        )
        return

    with get_db() as conn:
        if override:
            displaced = conn.execute(
                """SELECT ps.game_id FROM pick_slots ps
                   WHERE ps.player_id=? AND ps.week_id=? AND ps.confidence_points=?""",
                (player_id, week["id"], slot)
            ).fetchone()
            if displaced:
                conn.execute(
                    "DELETE FROM picks WHERE player_id=? AND game_id=?",
                    (player_id, displaced["game_id"])
                )
                conn.execute(
                    "DELETE FROM pick_slots WHERE player_id=? AND week_id=? "
                    "AND confidence_points=?",
                    (player_id, week["id"], slot)
                )

        existing_same_game = conn.execute(
            "SELECT id, confidence_points FROM picks "
            "WHERE player_id=? AND game_id=?",
            (player_id, game["id"])
        ).fetchone()

        if existing_same_game:
            old_slot = existing_same_game["confidence_points"]
            conn.execute(
                "UPDATE picks SET picked_team=?, confidence_points=?, "
                "submitted_at=datetime('now') "
                "WHERE player_id=? AND game_id=?",
                (picked_team, slot, player_id, game["id"])
            )
            conn.execute(
                "DELETE FROM pick_slots WHERE player_id=? AND week_id=? "
                "AND confidence_points=?",
                (player_id, week["id"], old_slot)
            )
        else:
            conn.execute(
                """INSERT INTO picks(player_id, game_id, picked_team,
                   confidence_points, submitted_at)
                   VALUES (?,?,?,?,datetime('now'))""",
                (player_id, game["id"], picked_team, slot)
            )

        conn.execute(
            """INSERT INTO pick_slots(player_id, week_id, confidence_points, game_id)
               VALUES (?,?,?,?)
               ON CONFLICT(player_id, week_id, confidence_points)
               DO UPDATE SET game_id=excluded.game_id""",
            (player_id, week["id"], slot, game["id"])
        )

    hub_view = PicksHubEphemeralView(bot, player_id, week, games)
    embed    = _build_picks_embed(player_id, week, games)
    await interaction.response.edit_message(embed=embed, view=hub_view)
    await _refresh_picks_hub(bot)


# ── Forfeit assignment ─────────────────────────────────────────────────────────

async def assign_forfeits(bot: commands.Bot, game: dict, week_id: int) -> None:
    """
    Assign forfeit picks using a highly optimized bulk insert to avoid
    database thrashing (N+1 query problem).
    """
    from database import get_all_scoreable_players
    players = get_all_scoreable_players()

    with get_db() as conn:
        week = conn.execute("SELECT game_count FROM weeks WHERE id=?", (week_id,)).fetchone()
        total = week["game_count"] if week else 1

        all_slots = conn.execute(
            "SELECT player_id, confidence_points FROM pick_slots WHERE week_id=?", 
            (week_id,)
        ).fetchall()
        
        used_slots_map = {}
        for r in all_slots:
            used_slots_map.setdefault(r["player_id"], set()).add(r["confidence_points"])

        all_picks = conn.execute(
            "SELECT player_id FROM picks WHERE game_id=?", 
            (game["id"],)
        ).fetchall()
        picked_players = {r["player_id"] for r in all_picks}

        insert_picks = []
        insert_slots = []

        for player in players:
            pid = player["id"]
            
            if pid in picked_players:
                continue

            used = used_slots_map.get(pid, set())
            
            forfeit_slot = None
            for slot in range(1, total + 1):
                if slot not in used:
                    forfeit_slot = slot
                    break

            if forfeit_slot is not None:
                insert_picks.append((pid, game["id"], forfeit_slot))
                insert_slots.append((pid, week_id, forfeit_slot, game["id"]))

        if insert_picks:
            conn.executemany(
                """INSERT OR IGNORE INTO picks(player_id, game_id, picked_team,
                   confidence_points, is_forfeit, submitted_at)
                   VALUES (?,?,NULL,?,1,datetime('now'))""",
                insert_picks
            )
            conn.executemany(
                """INSERT INTO pick_slots(player_id, week_id, confidence_points, game_id)
                   VALUES (?,?,?,?)
                   ON CONFLICT(player_id, week_id, confidence_points)
                   DO UPDATE SET game_id=excluded.game_id""",
                insert_slots
            )

    if insert_picks:
        await log_to_channel(
            bot,
            f"Picks locked for **{game['home_team']} vs {game['away_team']}** — "
            f"forfeits assigned to {len(insert_picks)} players.",
            title="Game locked",
            level="info",
        )


# ── Current picks view ────────────────────────────────────────────────────────

class CurrentPicksView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player_id: int):
        super().__init__(timeout=120)
        self.bot       = bot
        self.player_id = player_id

    @discord.ui.button(label="View pick history", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        from cogs.stats import open_pick_history
        await open_pick_history(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Closed.", embed=None, view=None
        )
        self.stop()


# ── Hub panel view (public, in #cfcp-picks) ────────────────────────────────────

class PicksHubView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _get_active_player(self, interaction: discord.Interaction):
        player = _get_player(str(interaction.user.id))
        if not player:
            await interaction.response.send_message(
                "You're not registered yet. Use the **Register** button to join!",
                ephemeral=True,
            )
            return None
        if player["status"] == "pending":
            await interaction.response.send_message(
                "Your registration is pending admin approval.", ephemeral=True
            )
            return None
        if player["status"] == "withdrawn":
            await interaction.response.send_message(
                "You're currently withdrawn. Re-register using the Register button.",
                ephemeral=True,
            )
            return None
        if player["status"] == "denied":
            await interaction.response.send_message(
                "Your registration was not approved. Contact an admin if you "
                "believe this is an error.",
                ephemeral=True,
            )
            return None
        return player

    async def _open_picks(self, interaction: discord.Interaction) -> None:
        player = await self._get_active_player(interaction)
        if not player:
            return

        season, week = resolve_current_week()
        if not week:
            await interaction.response.send_message(
                "No week is currently active. Check back soon!",
                ephemeral=True,
            )
            return

        with get_db() as conn:
            games = conn.execute(
                "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
                (week["id"],)
            ).fetchall()

        games_list = [dict(g) for g in games]
        embed = _build_picks_embed(player["id"], dict(week), games_list)
        view  = PicksHubEphemeralView(
            interaction.client, player["id"], dict(week), games_list
        )
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    @discord.ui.button(label="Submit picks", style=discord.ButtonStyle.primary,
                       custom_id="picks:submit", emoji="🏈", row=0)
    async def submit_picks(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        await self._open_picks(interaction)

    @discord.ui.button(label="Edit picks", style=discord.ButtonStyle.secondary,
                       custom_id="picks:edit", row=0)
    async def edit_picks(self, interaction: discord.Interaction,
                         button: discord.ui.Button) -> None:
        await self._open_picks(interaction)

    @discord.ui.button(label="My picks", style=discord.ButtonStyle.secondary,
                       custom_id="picks:view", row=0)
    async def view_picks(self, interaction: discord.Interaction,
                         button: discord.ui.Button) -> None:
        player = await self._get_active_player(interaction)
        if not player:
            return
        season, week = resolve_current_week()
        if not week:
            from cogs.stats import open_pick_history
            await open_pick_history(interaction)
            return
        with get_db() as conn:
            games = conn.execute(
                "SELECT * FROM games WHERE week_id=? ORDER BY kickoff_time",
                (week["id"],)
            ).fetchall()
        embed = _build_picks_embed(
            player["id"], dict(week), [dict(g) for g in games],
            title="Current picks"
        )
        view = CurrentPicksView(interaction.client, player["id"])
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    @discord.ui.button(label="My stats", style=discord.ButtonStyle.secondary,
                       custom_id="picks:stats", row=0)
    async def my_stats(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        from cogs.stats import open_my_stats
        await open_my_stats(interaction)

    @discord.ui.button(label="Register", style=discord.ButtonStyle.success,
                       custom_id="picks:register", row=1)
    async def register(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        player = _get_player(str(interaction.user.id))
        if player and player["status"] == "active":
            await interaction.response.send_message(
                "You're already registered!", ephemeral=True
            )
            return
        if player and player["status"] == "pending":
            await interaction.response.send_message(
                "Your registration is already pending approval.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RegistrationModal())

    @discord.ui.button(label="Withdraw", style=discord.ButtonStyle.danger,
                       custom_id="picks:withdraw", row=1)
    async def withdraw(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        player = _get_player(str(interaction.user.id))
        if not player or player["status"] != "active":
            await interaction.response.send_message(
                "You're not currently an active player.", ephemeral=True
            )
            return
        view = WithdrawConfirmView(interaction.client, player["id"],
                                   player["display_name"])
        await interaction.response.send_message(
            "Are you sure you want to withdraw from CFCP? Your scores will be "
            "preserved and you can rejoin at any time.",
            view=view, ephemeral=True,
        )


class WithdrawConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, player_id: int, display_name: str):
        super().__init__(timeout=60)
        self.bot          = bot
        self.player_id    = player_id
        self.display_name = display_name

    @discord.ui.button(label="Yes, withdraw", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE players SET status='withdrawn', dm_notifications=0 "
                "WHERE id=?",
                (self.player_id,)
            )
        await _refresh_picks_hub(self.bot)
        await log_to_channel(
            self.bot,
            f"**{self.display_name}** has withdrawn from the competition.",
            title="Player withdrawn",
        )
        await interaction.response.send_message(
            "You've been withdrawn. Your scores are preserved and you can "
            "rejoin at any time using the Register button.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


# ── Cog ────────────────────────────────────────────────────────────────────────

class PicksCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.lock_checker.start()

    def cog_unload(self) -> None:
        self.lock_checker.cancel()

    @tasks.loop(minutes=1)
    async def lock_checker(self) -> None:
        try:
            season, week = resolve_current_week()
            if not week:
                return

            with get_db() as conn:
                games = conn.execute(
                    """SELECT * FROM games
                       WHERE week_id=? AND status='scheduled'""",
                    (week["id"],)
                ).fetchall()

            from utils.espn import fetch_game_status
            from cogs.admin import update_single_game_embed

            for game in games:
                game_dict = dict(game)
                secs = seconds_until_iso(game_dict["kickoff_time"])

                if secs <= 0 and secs > -300:
                    await assign_forfeits(self.bot, game_dict, week["id"])

                if secs <= 0 and game_dict.get("espn_game_id") and \
                   not game_dict["espn_game_id"].startswith("manual_"):
                    result = await fetch_game_status(game_dict["espn_game_id"])
                    if result:
                        with get_db() as conn:
                            conn.execute(
                                """UPDATE games SET status=?, home_score=?,
                                   away_score=?, winner=?
                                   WHERE id=?""",
                                (result["status"], result["home_score"],
                                 result["away_score"], result["winner"],
                                 game_dict["id"])
                            )
                        await update_single_game_embed(self.bot, game_dict["id"])

        except Exception as exc:
            log.error(f"lock_checker error: {exc}", exc_info=True)

    @lock_checker.before_loop
    async def before_lock_checker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PicksCog(bot))
    bot.add_view(PicksHubView())
    log.info("PicksCog loaded and PicksHubView registered.")
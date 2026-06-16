from __future__ import annotations
from typing import Optional
import discord
from datetime import datetime, timezone
from config import (
    COLOR_PURPLE, COLOR_AMBER, COLOR_CORAL,
    COLOR_GRAY, COLOR_GREEN, COLOR_RED, COLOR_BLUE,
)
from utils.time_utils import format_time_et, countdown_label


# ── ADMIN PANEL ───────────────────────────────────────────────────────────────

def admin_panel_embed(week=None, season=None, poll_type="ap",
                      picks_reveal: bool = True) -> discord.Embed:
    e = discord.Embed(
        title="CFCP Admin Panel",
        color=COLOR_CORAL,
    )
    if season:
        e.add_field(name="Season", value=str(season["year"]), inline=True)
    if week:
        e.add_field(name="Current week",
                    value=f"Week {week['week_number']}", inline=True)
        e.add_field(name="Games loaded",
                    value=str(week["game_count"]), inline=True)
        lock_status = "🔒 Locked" if week["is_locked"] else "🟢 Open"
        e.add_field(name="Picks status", value=lock_status, inline=True)
        scored = "✅ Yes" if week["is_scored"] else "⏳ Pending"
        e.add_field(name="Scored", value=scored, inline=True)
    else:
        e.add_field(name="Status", value="No week loaded", inline=False)

    poll_label = "CFP Rankings" if poll_type == "cfp" else "AP Top 25"
    e.add_field(name="Active poll", value=poll_label, inline=True)
    e.add_field(name="Pick reveal",
                value="🟢 On" if picks_reveal else "🔴 Off", inline=True)
    e.set_footer(text="Use the buttons below to manage the competition.")
    return e


# ── PICKS HUB PANEL ───────────────────────────────────────────────────────────

def picks_hub_embed(week=None, games: Optional[list] = None,
                    players: Optional[list] = None) -> discord.Embed:
    if not week:
        e = discord.Embed(
            title="🏈  College Football Confidence Picks",
            description="No week is currently active. Check back soon!",
            color=COLOR_GRAY,
        )
        return e

    game_count = week["game_count"]
    e = discord.Embed(
        title=f"🏈  CFCP — Week {week['week_number']}",
        color=COLOR_PURPLE,
    )

    status_str = "🔒 Picks locked" if week["is_locked"] else "🟢 Picks open"
    e.add_field(name="Status",  value=status_str,      inline=True)
    e.add_field(name="Games",   value=str(game_count), inline=True)

    if games:
        first_kick = min(g["kickoff_time"] for g in games)
        e.add_field(
            name="First kickoff",
            value=format_time_et(datetime.fromisoformat(first_kick)),
            inline=True,
        )

    if players:
        submitted = [p["display_name"] for p in players if p.get("submitted")]
        missing   = [p["display_name"] for p in players if not p.get("submitted")]
        sub_str   = " · ".join(f"✅ {n}" for n in submitted) if submitted else "—"
        miss_str  = " · ".join(f"❌ {n}" for n in missing)  if missing  else "—"
        e.add_field(
            name=f"Submitted ({len(submitted)}/{len(players)})",
            value=sub_str or "—",
            inline=False,
        )
        if missing:
            e.add_field(name="Still needed", value=miss_str, inline=False)

    e.set_footer(text=(
        "Picks lock at each game's kickoff time  ·  "
        "Use the buttons below to submit or edit your picks"
    ))
    return e


# ── GAME EMBED (one per matchup in #cfcp-games) ───────────────────────────────

def game_embed(game, player_picks: Optional[list] = None,
               picks_reveal: bool = True,
               total_players: int = 0) -> discord.Embed:
    status = game["status"]
    home   = game["home_team"]
    away   = game["away_team"]
    h_rank = f"#{game['home_rank']} " if game["home_rank"] else ""
    a_rank = f"#{game['away_rank']} " if game["away_rank"] else ""

    if status == "final":
        color = COLOR_GRAY
        title = f"Final  ·  {h_rank}{home}  vs  {a_rank}{away}"
    elif status == "in_progress":
        color = COLOR_GREEN
        title = f"🔴 Live  ·  {h_rank}{home}  vs  {a_rank}{away}"
    else:
        color = COLOR_BLUE
        title = f"{h_rank}{home}  vs  {a_rank}{away}"

    e = discord.Embed(title=title, color=color)

    if status in ("final", "in_progress") and game.get("home_score") is not None:
        score_line = f"{game['home_score']}  —  {game['away_score']}"
        if status == "final" and game.get("winner"):
            w = game["winner"]
            if w == home:
                score_line = f"**{game['home_score']}** — {game['away_score']}  · {home} wins"
            else:
                score_line = f"{game['home_score']} — **{game['away_score']}**  · {away} wins"
        elif status == "final" and game.get("winner") is None:
            score_line = f"{game['home_score']} — {game['away_score']}  · *Tie*"
        e.add_field(name="Score", value=score_line, inline=False)
    else:
        e.add_field(
            name="Kickoff",
            value=(
                f"{format_time_et(datetime.fromisoformat(game['kickoff_time']))}  "
                f"· _{countdown_label(game['kickoff_time'])}_"
            ),
            inline=False,
        )

    meta_parts = []
    if game.get("spread"):
        meta_parts.append(f"Spread: {game['spread']}")
    if game.get("over_under"):
        meta_parts.append(f"O/U: {game['over_under']}")
    if game.get("channel"):
        meta_parts.append(game["channel"])
    if meta_parts:
        e.add_field(
            name="Lines & broadcast",
            value="  ·  ".join(meta_parts),
            inline=False,
        )

    # FIX #4: pick split now only renders when player_picks is actually provided
    if player_picks is not None and total_players > 0:
        locked       = status != "scheduled"
        home_pickers = [p for p in player_picks if p.get("picked_team") == home]
        away_pickers = [p for p in player_picks if p.get("picked_team") == away]
        home_pct     = round(len(home_pickers) / total_players * 100) if total_players else 0
        away_pct     = 100 - home_pct

        if locked and picks_reveal:
            home_names = ", ".join(p["display_name"] for p in home_pickers) or "—"
            away_names = ", ".join(p["display_name"] for p in away_pickers) or "—"
            e.add_field(name=f"{home} ({home_pct}%)", value=home_names, inline=True)
            e.add_field(name=f"{away} ({away_pct}%)", value=away_names, inline=True)
        else:
            bar_filled = round(home_pct / 10)
            bar = "█" * bar_filled + "░" * (10 - bar_filled)
            e.add_field(
                name="Pick split",
                value=(
                    f"{home_pct}%  {bar}  {away_pct}%\n"
                    f"{home} vs {away}"
                ),
                inline=False,
            )

    if game.get("espn_link"):
        e.add_field(
            name="ESPN",
            value=f"[Game page]({game['espn_link']})",
            inline=True,
        )

    return e


# ── SEASON STANDINGS EMBED ────────────────────────────────────────────────────

def standings_season_embed(rows: list, season_year: int) -> discord.Embed:
    e = discord.Embed(
        title=f"🏆  {season_year} Season Standings",
        color=COLOR_AMBER,
    )
    if not rows:
        e.description = "No scores yet."
        return e

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, r in enumerate(rows):
        rank_icon = medals[i] if i < 3 else f"`{i+1}.`"
        pct       = (r["total_correct"] / r["total_possible"] * 100
                     if r["total_possible"] else 0)
        withdrawn = " *(withdrawn)*" if r["status"] == "withdrawn" else ""
        lines.append(
            f"{rank_icon} **{r['display_name']}**{withdrawn}  "
            f"— **{r['total_points']}** pts  "
            f"· {r['total_correct']} correct ({pct:.1f}%)"
        )
    e.description = "\n".join(lines)
    e.set_footer(text="Updates automatically after each game result.")
    return e


# ── WEEKLY STANDINGS EMBED ────────────────────────────────────────────────────

def standings_week_embed(rows: list, week_number: int) -> discord.Embed:
    e = discord.Embed(
        title=f"📊  Week {week_number} Results",
        color=COLOR_PURPLE,
    )
    if not rows:
        e.description = "Results not yet finalized."
        return e

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, r in enumerate(rows):
        if r["total_possible"] == 0:
            continue
        rank_icon = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(
            f"{rank_icon} **{r['display_name']}**  "
            f"— **{r['points_earned']}** pts  "
            f"· {r['correct_picks']} correct"
            + (f" · {r['forfeited_picks']} missed" if r["forfeited_picks"] else "")
        )

    if rows:
        top_pts     = rows[0]["points_earned"]
        top_correct = rows[0]["correct_picks"]
        winners = [
            r["display_name"] for r in rows
            if r["points_earned"] == top_pts
            and r["correct_picks"] == top_correct
        ]
        if winners:
            win_str = " & ".join(winners)
            e.add_field(
                name=f"🏅  Week {week_number} winner",
                value=win_str,
                inline=False,
            )

    e.description = "\n".join(lines) if lines else "No results yet."
    return e


# ── LOG MESSAGE ───────────────────────────────────────────────────────────────

def log_embed(title: str, description: str, level: str = "info") -> discord.Embed:
    color_map = {
        "info":    COLOR_BLUE,
        "success": COLOR_GREEN,
        "warning": COLOR_AMBER,
        "error":   COLOR_RED,
    }
    e = discord.Embed(
        title=title,
        description=description,
        color=color_map.get(level, COLOR_GRAY),
        # FIX: use timezone-aware UTC datetime instead of deprecated utcnow()
        timestamp=datetime.now(tz=timezone.utc),
    )
    return e

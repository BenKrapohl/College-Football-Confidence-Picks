import aiohttp
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from config import ESPN_SCOREBOARD_URL, ESPN_RANKINGS_URL, POLL_AP

log = logging.getLogger(__name__)
ET  = ZoneInfo("America/New_York")

_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    """Maintain a single global ClientSession to prevent socket exhaustion."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def _get(url: str, params: Optional[dict] = None) -> dict:
    session = await get_session()
    async with session.get(url, params=params, timeout=10) as resp:
        resp.raise_for_status()
        return await resp.json()


# ── RANKINGS ──────────────────────────────────────────────────────────────────

async def fetch_rankings(poll_type: str = POLL_AP) -> dict[str, int]:
    """
    Returns {team_location: rank} for the requested poll.
    poll_type: 'ap' or 'cfp'
    """
    data = await _get(ESPN_RANKINGS_URL)
    target_name = "AP Top 25" if poll_type == POLL_AP else "College Football Playoff"

    for poll in data.get("rankings", []):
        if target_name.lower() in poll.get("name", "").lower():
            ranks = {}
            for entry in poll.get("ranks", []):
                team_loc = entry["team"]["location"]
                ranks[team_loc] = entry["current"]
            return ranks

    return {}


# ── SCHEDULE ──────────────────────────────────────────────────────────────────

async def fetch_week_games(
    start_date: str,
    end_date: str,
    ranked_teams: dict[str, int],
) -> list[dict]:
    """
    Fetch all games in date range, filter to those involving at least one
    ranked team, and return structured game dicts.
    """
    params = {"dates": f"{start_date}-{end_date}"}
    data   = await _get(ESPN_SCOREBOARD_URL, params=params)

    games         = []
    seen_espn_ids = set()

    for event in data.get("events", []):
        espn_id = event["id"]
        if espn_id in seen_espn_ids:
            continue

        comp        = event["competitions"][0]
        competitors = comp["competitors"]

        home = next((c for c in competitors if c["homeAway"] == "home"), competitors[0])
        away = next((c for c in competitors if c["homeAway"] == "away"), competitors[1])

        home_name = home["team"]["location"]
        away_name = away["team"]["location"]

        home_rank = ranked_teams.get(home_name)
        away_rank = ranked_teams.get(away_name)

        if home_rank is None and away_rank is None:
            continue

        raw_time = event.get("date", "")
        try:
            kickoff_utc = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            kickoff_et  = kickoff_utc.astimezone(ET)
            kickoff_iso = kickoff_et.isoformat()
        except (ValueError, AttributeError):
            kickoff_iso = raw_time

        try:
            broadcast = comp["broadcasts"][0]["names"][0]
        except (KeyError, IndexError):
            broadcast = "TBD"

        odds       = comp.get("odds", [])
        spread     = ""
        over_under = ""
        if odds:
            for o in odds:
                if "details" in o and not spread:
                    spread = o["details"]
                if "overUnder" in o and not over_under:
                    over_under = str(o["overUnder"])

        status_name = event["status"]["type"]["name"]
        status_map  = {
            "STATUS_SCHEDULED":   "scheduled",
            "STATUS_IN_PROGRESS": "in_progress",
            "STATUS_FINAL":       "final",
        }
        status = status_map.get(status_name, "scheduled")

        try:
            home_score = int(home.get("score", 0) or 0)
            away_score = int(away.get("score", 0) or 0)
        except (TypeError, ValueError):
            home_score = away_score = None

        winner = None
        if status == "final" and home_score is not None and away_score is not None:
            if home_score > away_score:
                winner = home_name
            elif away_score > home_score:
                winner = away_name
            else:
                winner = None
                log.warning(
                    f"Game {espn_id} ({home_name} vs {away_name}) ended in a tie. Winner left as None."
                )

        games.append({
            "espn_game_id": espn_id,
            "home_team":    home_name,
            "away_team":    away_name,
            "home_rank":    home_rank,
            "away_rank":    away_rank,
            "spread":       spread,
            "over_under":   over_under,
            "kickoff_time": kickoff_iso,
            "channel":      broadcast,
            "espn_link":    f"https://www.espn.com/college-football/game?gameId={espn_id}",
            "status":       status,
            "home_score":   home_score,
            "away_score":   away_score,
            "winner":       winner,
        })

        seen_espn_ids.add(espn_id)

    games.sort(key=lambda g: g["kickoff_time"])
    return games


# ── LIVE SCORE UPDATE ─────────────────────────────────────────────────────────

async def fetch_game_status(espn_game_id: str) -> dict | None:
    """
    Fetch current status for a single game by ESPN ID.
    """
    try:
        url = (
            f"http://site.api.espn.com/apis/site/v2/sports/football/"
            f"college-football/summary?event={espn_game_id}"
        )
        data   = await _get(url)
        header = data.get("header", {})
        comps  = header.get("competitions", [{}])
        comp   = comps[0] if comps else {}

        status_obj    = comp.get("status", {})
        status_type   = status_obj.get("type", {})
        status_name   = status_type.get("name", "STATUS_SCHEDULED")
        status_detail = status_type.get("shortDetail", "")

        status_map = {
            "STATUS_SCHEDULED":   "scheduled",
            "STATUS_IN_PROGRESS": "in_progress",
            "STATUS_FINAL":       "final",
        }
        status = status_map.get(status_name, "scheduled")

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        try:
            home_score = int(home.get("score", 0) or 0)
            away_score = int(away.get("score", 0) or 0)
        except (TypeError, ValueError):
            home_score = away_score = 0

        home_name = home.get("team", {}).get("location", "")
        away_name = away.get("team", {}).get("location", "")

        winner = None
        if status == "final":
            if home_score > away_score:
                winner = home_name
            elif away_score > home_score:
                winner = away_name
            else:
                winner = None

        return {
            "status":        status,
            "home_score":    home_score,
            "away_score":    away_score,
            "winner":        winner,
            "status_detail": status_detail,
        }

    except Exception as exc:
        log.debug(f"fetch_game_status({espn_game_id}) failed: {exc}")
        return None
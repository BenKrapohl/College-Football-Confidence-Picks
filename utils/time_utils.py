from datetime import datetime, date
from zoneinfo import ZoneInfo
from config import TIMEZONE

ET  = ZoneInfo(TIMEZONE)
UTC = ZoneInfo("UTC")


def now_et() -> datetime:
    return datetime.now(tz=ET)

def today_et() -> date:
    return now_et().date()

def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)

def parse_iso(s: str) -> datetime:
    """Parse an ISO8601 string, returning ET-aware datetime."""
    dt = datetime.fromisoformat(s)
    return to_et(dt)

def format_time_et(dt: datetime, include_date: bool = True) -> str:
    """Format a datetime for display, e.g. 'Sat Sep 6 · 12:00 PM ET'."""
    dt = to_et(dt)
    day  = dt.strftime("%a %b ").rstrip() + " " + str(dt.day)
    hour = str(dt.hour % 12 or 12)
    mins = dt.strftime("%M")
    ampm = dt.strftime("%p")
    if include_date:
        return f"{day} · {hour}:{mins} {ampm} ET"
    return f"{hour}:{mins} {ampm} ET"

def format_date_et(dt: datetime) -> str:
    dt = to_et(dt)
    return dt.strftime("%a %b ") + str(dt.day)

def seconds_until(dt: datetime) -> float:
    """
    FIX #1 / #3: Accepts a datetime object only.
    Callers that have an ISO string must call parse_iso() first.
    """
    return (to_et(dt) - now_et()).total_seconds()

def seconds_until_iso(iso_str: str) -> float:
    """Convenience wrapper — parse ISO string then compute seconds."""
    return seconds_until(parse_iso(iso_str))

def game_day_key(kickoff_iso: str) -> str:
    """Return YYYY-MM-DD string for grouping games by calendar day (ET)."""
    return str(to_et(parse_iso(kickoff_iso)).date())

def group_games_by_day(games) -> dict[str, list]:
    """Group a list of game rows by their ET calendar day."""
    groups: dict[str, list] = {}
    for g in games:
        key = game_day_key(g["kickoff_time"])
        groups.setdefault(key, []).append(g)
    return groups

def first_kickoff_of_day(games_on_day: list) -> datetime:
    kickoffs = [parse_iso(g["kickoff_time"]) for g in games_on_day]
    return min(kickoffs)

def countdown_label(kickoff_iso: str) -> str:
    """Human-readable countdown, e.g. '2h 34m' or '3 days'."""
    secs = seconds_until_iso(kickoff_iso)
    if secs <= 0:
        return "started"
    minutes = int(secs // 60)
    hours   = minutes // 60
    days    = hours   // 24
    if days >= 2:
        return f"{days} days"
    if hours >= 1:
        remaining_mins = minutes % 60
        return f"{hours}h {remaining_mins}m"
    return f"{minutes}m"

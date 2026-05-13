import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "cfcp.db")

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            -- ── PLAYERS ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS players (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id          TEXT    NOT NULL UNIQUE,
                discord_username    TEXT    NOT NULL,
                display_name        TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'active',
                -- active | withdrawn | pending | denied
                dm_notifications    INTEGER NOT NULL DEFAULT 1,
                picks_visible       INTEGER NOT NULL DEFAULT 1,
                joined_week         INTEGER,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

            -- ── SEASONS ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS seasons (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                year                INTEGER NOT NULL UNIQUE,
                poll_type           TEXT    NOT NULL DEFAULT 'ap',
                -- ap | cfp
                is_active           INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

            -- ── WEEKS ────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS weeks (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id           INTEGER NOT NULL REFERENCES seasons(id),
                week_number         INTEGER NOT NULL,
                start_date          TEXT    NOT NULL,
                end_date            TEXT    NOT NULL,
                is_locked           INTEGER NOT NULL DEFAULT 0,
                is_scored           INTEGER NOT NULL DEFAULT 0,
                game_count          INTEGER NOT NULL DEFAULT 0,
                recap_sent          INTEGER NOT NULL DEFAULT 0,
                loaded_at           TEXT,
                UNIQUE(season_id, week_number)
            );

            -- ── GAMES ────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS games (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                week_id             INTEGER NOT NULL REFERENCES weeks(id),
                espn_game_id        TEXT,
                home_team           TEXT    NOT NULL,
                away_team           TEXT    NOT NULL,
                home_rank           INTEGER,
                away_rank           INTEGER,
                spread              TEXT,
                over_under          TEXT,
                kickoff_time        TEXT    NOT NULL,
                -- stored as ISO8601 in US/Eastern
                channel             TEXT,
                espn_link           TEXT,
                status              TEXT    NOT NULL DEFAULT 'scheduled',
                -- scheduled | in_progress | final
                home_score          INTEGER,
                away_score          INTEGER,
                winner              TEXT,
                discord_message_id  TEXT,
                -- message ID in #cfcp-games
                is_manually_added   INTEGER NOT NULL DEFAULT 0,
                UNIQUE(week_id, espn_game_id)
            );

            -- ── PICKS ────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS picks (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id           INTEGER NOT NULL REFERENCES players(id),
                game_id             INTEGER NOT NULL REFERENCES games(id),
                picked_team         TEXT,
                confidence_points   INTEGER NOT NULL,
                is_correct          INTEGER,
                -- NULL until scored, 1 correct, 0 wrong
                is_forfeit          INTEGER NOT NULL DEFAULT 0,
                submitted_at        TEXT,
                scored_at           TEXT,
                UNIQUE(player_id, game_id),
                UNIQUE(player_id, game_id, confidence_points)
            );

            -- Separate unique constraint: one confidence value per player per week
            CREATE TABLE IF NOT EXISTS pick_slots (
                player_id           INTEGER NOT NULL REFERENCES players(id),
                week_id             INTEGER NOT NULL REFERENCES weeks(id),
                confidence_points   INTEGER NOT NULL,
                game_id             INTEGER REFERENCES games(id),
                PRIMARY KEY (player_id, week_id, confidence_points)
            );

            -- ── WEEKLY SCORES ─────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS weekly_scores (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id           INTEGER NOT NULL REFERENCES players(id),
                week_id             INTEGER NOT NULL REFERENCES weeks(id),
                points_earned       INTEGER NOT NULL DEFAULT 0,
                correct_picks       INTEGER NOT NULL DEFAULT 0,
                wrong_picks         INTEGER NOT NULL DEFAULT 0,
                forfeited_picks     INTEGER NOT NULL DEFAULT 0,
                total_possible      INTEGER NOT NULL DEFAULT 0,
                weekly_rank         INTEGER,
                UNIQUE(player_id, week_id)
            );

            -- ── NOTIFICATIONS ─────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS notifications_sent (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id           INTEGER NOT NULL REFERENCES players(id),
                week_id             INTEGER NOT NULL REFERENCES weeks(id),
                game_day            TEXT    NOT NULL,
                -- YYYY-MM-DD of the day group
                notif_type          TEXT    NOT NULL,
                -- week_open | 24hr | 30min | recap
                sent_at             TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(player_id, week_id, game_day, notif_type)
            );

            -- ── REGISTRATION REQUESTS ─────────────────────────────────
            CREATE TABLE IF NOT EXISTS registration_requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id          TEXT    NOT NULL,
                discord_username    TEXT    NOT NULL,
                display_name        TEXT    NOT NULL,
                status              TEXT    NOT NULL DEFAULT 'pending',
                -- pending | approved | denied
                requested_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                reviewed_at         TEXT,
                reviewed_by         TEXT
            );

            -- ── BOT CONFIG ────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS bot_config (
                key                 TEXT    PRIMARY KEY,
                value               TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

            -- ── INDEXES ───────────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_picks_player   ON picks(player_id);
            CREATE INDEX IF NOT EXISTS idx_picks_game     ON picks(game_id);
            CREATE INDEX IF NOT EXISTS idx_games_week     ON games(week_id);
            CREATE INDEX IF NOT EXISTS idx_games_status   ON games(status);
            CREATE INDEX IF NOT EXISTS idx_weekly_player  ON weekly_scores(player_id);
            CREATE INDEX IF NOT EXISTS idx_weekly_week    ON weekly_scores(week_id);
            CREATE INDEX IF NOT EXISTS idx_notif_player   ON notifications_sent(player_id, week_id);
        """)
    print(f"Database initialized at {DB_PATH}")


# ── CONFIG HELPERS ───────────────────────────────────────────────────────────

def config_get(key: str, default=None):
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

def config_set(key: str, value: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO bot_config(key, value, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at
        """, (key, value))


# ── SEASON HELPERS ────────────────────────────────────────────────────────────

def get_active_season():
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM seasons WHERE is_active = 1 ORDER BY year DESC LIMIT 1"
        ).fetchone()

def get_current_week(season_id: int):
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM weeks
            WHERE season_id = ?
              AND start_date <= date('now','localtime')
              AND end_date   >= date('now','localtime')
            LIMIT 1
        """, (season_id,)).fetchone()

def get_week_by_number(season_id: int, week_number: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM weeks WHERE season_id = ? AND week_number = ?",
            (season_id, week_number)
        ).fetchone()


# ── PLAYER HELPERS ────────────────────────────────────────────────────────────

def get_player_by_discord_id(discord_id: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
        ).fetchone()

def get_all_active_players():
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE status = 'active' ORDER BY display_name"
        ).fetchall()


# ── PICK HELPERS ──────────────────────────────────────────────────────────────

def get_player_picks_for_week(player_id: int, week_id: int):
    with get_db() as conn:
        return conn.execute("""
            SELECT p.*, g.home_team, g.away_team, g.home_rank, g.away_rank,
                   g.kickoff_time, g.channel, g.spread, g.status as game_status,
                   g.winner, g.espn_link
            FROM picks p
            JOIN games g ON p.game_id = g.id
            WHERE p.player_id = ? AND g.week_id = ?
            ORDER BY p.confidence_points DESC
        """, (player_id, week_id)).fetchall()

def get_used_slots_for_week(player_id: int, week_id: int):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT confidence_points FROM pick_slots
            WHERE player_id = ? AND week_id = ?
        """, (player_id, week_id)).fetchall()
        return {r["confidence_points"] for r in rows}

def get_unpicked_games_for_player(player_id: int, week_id: int):
    with get_db() as conn:
        return conn.execute("""
            SELECT g.* FROM games g
            WHERE g.week_id = ?
              AND g.status = 'scheduled'
              AND g.id NOT IN (
                  SELECT game_id FROM picks
                  WHERE player_id = ? AND is_forfeit = 0
              )
            ORDER BY g.kickoff_time
        """, (week_id, player_id)).fetchall()


# ── SCORING HELPERS ───────────────────────────────────────────────────────────

def get_season_leaderboard(season_id: int):
    with get_db() as conn:
        return conn.execute("""
            SELECT
                pl.id,
                pl.display_name,
                pl.status,
                COALESCE(SUM(ws.points_earned), 0)    AS total_points,
                COALESCE(SUM(ws.correct_picks), 0)    AS total_correct,
                COALESCE(SUM(ws.wrong_picks), 0)      AS total_wrong,
                COALESCE(SUM(ws.forfeited_picks), 0)  AS total_forfeits,
                COALESCE(SUM(ws.total_possible), 0)   AS total_possible,
                COUNT(ws.week_id)                     AS weeks_played,
                COALESCE(MAX(ws.points_earned), 0)    AS best_week,
                COALESCE(MIN(CASE WHEN ws.total_possible > 0
                             THEN ws.points_earned END), 0) AS worst_week
            FROM players pl
            LEFT JOIN weekly_scores ws ON ws.player_id = pl.id
            LEFT JOIN weeks w ON ws.week_id = w.id AND w.season_id = ?
            WHERE pl.status IN ('active', 'withdrawn')
            GROUP BY pl.id
            ORDER BY total_points DESC, total_correct DESC
        """, (season_id,)).fetchall()

def get_week_leaderboard(week_id: int):
    with get_db() as conn:
        return conn.execute("""
            SELECT
                pl.display_name,
                pl.status,
                COALESCE(ws.points_earned, 0)   AS points_earned,
                COALESCE(ws.correct_picks, 0)   AS correct_picks,
                COALESCE(ws.forfeited_picks, 0) AS forfeited_picks,
                COALESCE(ws.total_possible, 0)  AS total_possible,
                ws.weekly_rank
            FROM players pl
            LEFT JOIN weekly_scores ws
                   ON ws.player_id = pl.id AND ws.week_id = ?
            WHERE pl.status IN ('active', 'withdrawn')
            ORDER BY points_earned DESC, correct_picks DESC
        """, (week_id,)).fetchall()

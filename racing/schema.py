from __future__ import annotations

import sqlite3
from textwrap import dedent


RACECARD_RACE_INFO_TABLE = "racecard_race_info"
RACECARD_STARTERS_TABLE = "racecard_starters"
RACECARD_STARTERS_BASE_COLUMNS: tuple[str, ...] = (
    "id",
    "race_date",
    "racecourse",
    "race_no",
    "後備",
    "horse_id",
)
RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS: tuple[str, ...] = (
    "馬號",
    "馬名",
    "檔位",
    "騎師",
    "練馬師",
    "負磅",
    "評分",
    "馬匹體重",
    "配備",
)


def qident(identifier: str) -> str:
    """Quote SQLite identifier safely."""
    return '"' + identifier.replace('"', '""') + '"'


def core_schema_sql() -> str:
    """DDL for core racing tables/indexes."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS race_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          horse_id TEXT NOT NULL,
          race_date TEXT NOT NULL,
          venue TEXT NOT NULL,
          race_no TEXT NOT NULL,
          course TEXT NOT NULL,
          distance TEXT NOT NULL,
          going TEXT NOT NULL,
          class_band TEXT NOT NULL,
          draw TEXT NOT NULL,
          rating TEXT NOT NULL,
          jockey TEXT NOT NULL,
          trainer TEXT NOT NULL,
          finish_pos TEXT NOT NULL,
          running_pos TEXT NOT NULL DEFAULT '',
          win_odds TEXT NOT NULL DEFAULT '',
          time TEXT NOT NULL,
          prize TEXT NOT NULL,
          handicap TEXT NOT NULL DEFAULT '',
          place_odds TEXT NOT NULL DEFAULT '',
          sectional_time TEXT NOT NULL DEFAULT '',
          UNIQUE(
            horse_id, race_date, venue, race_no, course, distance, going, class_band, draw,
            rating, jockey, trainer, finish_pos, running_pos, win_odds, time, prize
          )
        );
        CREATE INDEX IF NOT EXISTS idx_race_records_horse_id
          ON race_records(horse_id);

        CREATE TABLE IF NOT EXISTS horses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          horse_id TEXT NOT NULL UNIQUE,
          brand_no TEXT,
          name_zh TEXT,
          name_en TEXT,
          status TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_horses_name_zh
          ON horses(name_zh);
        CREATE INDEX IF NOT EXISTS idx_horses_name_en
          ON horses(name_en);
        """
    )


def racecard_base_schema_sql(race_info_table: str = RACECARD_RACE_INFO_TABLE) -> str:
    """DDL for racecard meta + race info table/index."""
    race_info = qident(race_info_table)
    return dedent(
        f"""
        CREATE TABLE IF NOT EXISTS racecard_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS {race_info} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL,
            racecourse TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            event_name TEXT,
            calendar_date TEXT,
            weekday TEXT,
            meeting_venue TEXT,
            post_time TEXT,
            race_course_area TEXT,
            track_name TEXT,
            distance TEXT,
            prize_money TEXT,
            rating_range TEXT,
            race_class TEXT,
            UNIQUE(race_date, racecourse, race_no)
        );
        CREATE INDEX IF NOT EXISTS idx_racecard_race_info_meeting
          ON {race_info}(race_date, racecourse, race_no);
        """
    )


def football_schema_sql() -> str:
    """DDL for HKJC football matches and odds."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS football_matches (
          match_id TEXT PRIMARY KEY,
          front_end_id TEXT,
          match_date TEXT,
          kick_off_time TEXT,
          status TEXT,
          tournament_code TEXT,
          tournament_name TEXT,
          home_team TEXT,
          away_team TEXT,
          venue TEXT,
          home_score TEXT DEFAULT '',
          away_score TEXT DEFAULT '',
          home_ht_score TEXT DEFAULT '',
          away_ht_score TEXT DEFAULT '',
          neutral_venue TEXT DEFAULT '',
          odds_home TEXT DEFAULT '',
          odds_draw TEXT DEFAULT '',
          odds_away TEXT DEFAULT '',
          corners TEXT DEFAULT '',
          updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_football_matches_date
          ON football_matches(match_date);

        CREATE TABLE IF NOT EXISTS football_odds (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          match_id TEXT NOT NULL,
          odds_type TEXT NOT NULL,
          pool_name TEXT,
          inplay INTEGER NOT NULL DEFAULT 0,
          line_condition TEXT NOT NULL DEFAULT '',
          main_line INTEGER NOT NULL DEFAULT 0,
          selection TEXT NOT NULL,
          odds TEXT,
          status TEXT,
          updated_at TEXT,
          UNIQUE(match_id, odds_type, line_condition, selection, inplay)
        );
        CREATE INDEX IF NOT EXISTS idx_football_odds_match
          ON football_odds(match_id);
        """
    )


def ai_schema_sql() -> str:
    """DDL for the local AI assistant: saved prompts, chat, and private documents."""
    return dedent(
        """
        CREATE TABLE IF NOT EXISTS AI_COMMAND (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            CMD_TITLE TEXT,
            CMD_PROMPT TEXT NOT NULL,
            CREATED_AT TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS AI_CHAT (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            ROLE TEXT NOT NULL,
            CONTENT TEXT NOT NULL,
            CREATED_AT TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS AI_DOCUMENT (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            FILENAME TEXT NOT NULL,
            CONTENT_TEXT TEXT,
            CREATED_AT TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS AI_META (
            KEY TEXT PRIMARY KEY,
            VALUE TEXT
        );
        """
    )


def racecard_starters_schema_sql(
    starters_table: str = RACECARD_STARTERS_TABLE,
    predicted_odds_col: str = "預測賠率",
    immediate_odds_col: str = "即時賠率",
    place_odds_col: str = "即時位置賠率",
) -> str:
    """DDL for racecard starters table/index."""
    starters = qident(starters_table)
    predicted = qident(predicted_odds_col)
    immediate = qident(immediate_odds_col)
    place = qident(place_odds_col)
    return dedent(
        f"""
        CREATE TABLE IF NOT EXISTS {starters} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL,
            racecourse TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            後備 TEXT NOT NULL DEFAULT '否',
            horse_id TEXT,
            "馬號" TEXT,
            "馬名" TEXT,
            "檔位" TEXT,
            "騎師" TEXT,
            "練馬師" TEXT,
            "負磅" TEXT,
            "評分" TEXT,
            "馬匹體重" TEXT,
            "配備" TEXT,
            {predicted} TEXT,
            {immediate} TEXT,
            {place} TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_racecard_starters_meeting
          ON {starters}(race_date, racecourse, race_no);
        """
    )


def racecard_starters_expected_columns(
    display_fields: list[str] | tuple[str, ...] = RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS,
    predicted_odds_col: str = "預測賠率",
    immediate_odds_col: str = "即時賠率",
    place_odds_col: str = "即時位置賠率",
) -> set[str]:
    """Return canonical racecard_starters columns expected by app."""
    return {
        *RACECARD_STARTERS_BASE_COLUMNS,
        *display_fields,
        predicted_odds_col,
        immediate_odds_col,
        place_odds_col,
    }


def _read_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({qident(table)});").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["name"]) for row in rows}


def ensure_racecard_starters_columns(
    conn: sqlite3.Connection,
    starters_table: str = RACECARD_STARTERS_TABLE,
    display_fields: list[str] | tuple[str, ...] = RACECARD_STARTERS_DEFAULT_DISPLAY_FIELDS,
    predicted_odds_col: str = "預測賠率",
    immediate_odds_col: str = "即時賠率",
    place_odds_col: str = "即時位置賠率",
) -> None:
    """
    Ensure racecard_starters structure is compatible with current app.

    Compatibility strategy (kept from legacy UI logic):
    - If table has unexpected columns, rebuild table to canonical schema.
    - If table is missing expected columns, add them via ALTER TABLE.
    """
    expected = racecard_starters_expected_columns(
        display_fields=display_fields,
        predicted_odds_col=predicted_odds_col,
        immediate_odds_col=immediate_odds_col,
        place_odds_col=place_odds_col,
    )
    actual = _read_table_columns(conn, starters_table)
    unexpected = actual - expected
    if actual and unexpected:
        conn.execute(f"DROP TABLE IF EXISTS {qident(starters_table)};")
        actual = set()

    if not actual:
        conn.executescript(
            racecard_starters_schema_sql(
                starters_table=starters_table,
                predicted_odds_col=predicted_odds_col,
                immediate_odds_col=immediate_odds_col,
                place_odds_col=place_odds_col,
            )
        )
        return

    missing = expected - actual
    for col in missing:
        conn.execute(
            f"ALTER TABLE {qident(starters_table)} ADD COLUMN {qident(col)} TEXT;"
        )

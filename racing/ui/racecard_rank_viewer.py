from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, default_config
from ..db import fetch_horse_by_id, fetch_race_records, get_connection, get_horse_status, init_db
from ..download_sources import odds_graphql_url, racecard_base_url
from ..hkjc_graphql import get_race_odds_pools
from ..race_record_grid import RACE_RECORD_GRID_HEADERS, race_record_grid_values
from .grid_nav import install_grid_keyboard_nav
from .theme import (
    apply_multi_column_sort,
    configure_data_grid,
    make_table_item,
    resize_grid_columns_to_content,
    show_copyable_message,
)
from ..schema import (
    ensure_racecard_starters_columns,
    racecard_base_schema_sql,
)


RACECARD_BASE_URL = "https://racing.hkjc.com/zh-hk/local/information/racecard"


def _racecard_url(config: AppConfig | None = None) -> str:
    if config is None:
        return RACECARD_BASE_URL
    return racecard_base_url(config.data_dir)

# 每場賽事摘要（由排位頁面上方資訊區解析；與馬匹列表分表儲存）
RACECARD_RACE_INFO_TABLE = "racecard_race_info"


COURSE_NAME_TO_CODE: dict[str, str] = {
    "沙田": "ST",
    "跑馬地": "HV",
    "從化": "FC",
}

# 場地代碼 → 顯示名稱（查閱過往賽日下拉選單用）
COURSE_CODE_TO_DISPLAY: dict[str, str] = {code: name for name, code in COURSE_NAME_TO_CODE.items()}

# 只保留「出馬表 table」需要的欄位（不把其它欄位存進 DB）。
RACECARD_DISPLAY_FIELDS: list[str] = [
    "馬號",
    "馬名",
    "檔位",
    "騎師",
    "練馬師",
    "負磅",
    "評分",
    "馬匹體重",
    "配備",
]

# 預測賠率（本欄會顯示在 grid，但目前不做抓取解析：寫入空值）。
RACECARD_PREDICTED_ODDS_COLUMN = "預測賠率"

# 隔夜賠率：預設跟隨即時賠率；按「確認隔夜賠率」後鎖定，不再隨即時變更。
RACECARD_OVERNIGHT_ODDS_COLUMN = "隔夜賠率"

# 即時賠率：初始會先用模型填一份；抓到排位資料後會再用 GraphQL 的 winOdds 重新更新（只更新既有且非後備）。
RACECARD_IMMEDIATE_ODDS_COLUMN = "即時賠率"
RACECARD_PLACE_ODDS_COLUMN = "即時位置賠率"
# 顯示用差值（不入庫）：即時賠率 − 預測賠率。
RACECARD_ODDS_DIFF_COLUMN = "即時賠率-預測賠率"

RACECARD_DISPLAY_COLUMNS: list[str] = (
    RACECARD_DISPLAY_FIELDS
    + [
        RACECARD_PREDICTED_ODDS_COLUMN,
        RACECARD_OVERNIGHT_ODDS_COLUMN,
        RACECARD_IMMEDIATE_ODDS_COLUMN,
        RACECARD_ODDS_DIFF_COLUMN,
        RACECARD_PLACE_ODDS_COLUMN,
        "後備",
    ]
)

# 實際存於 racecard_starters 的顯示欄（不含計算欄）。
RACECARD_DB_DISPLAY_COLUMNS: list[str] = [
    c for c in RACECARD_DISPLAY_COLUMNS if c != RACECARD_ODDS_DIFF_COLUMN
]

# 馬匹 ID：用於用 `race_records` 計算預測賠率（不顯示在 grid）。
RACECARD_HIDDEN_HORSE_ID_COLUMN = "horse_id"

# Map HKJC header label -> fixed DB/UI field.
# Use substring match because HKJC may include extra text.
_FIELD_KEYWORDS: dict[str, list[str]] = {
    "馬號": ["馬號"],
    "馬名": ["馬名"],
    "檔位": ["檔位"],
    "騎師": ["騎師", "騎手"],
    "練馬師": ["練馬師"],
    "負磅": ["負磅"],
    "評分": ["評分"],
    # Reserve table sometimes uses 排位體重 for the weight-related column.
    "馬匹體重": ["馬匹體重", "排位體重", "體重"],
    "配備": ["配備"],
}


def _map_header_label_to_field(label: str) -> str | None:
    s = (label or "").strip()
    if not s:
        return None
    for field, keywords in _FIELD_KEYWORDS.items():
        if any(kw in s for kw in keywords):
            return field
    return None


def _extract_racecard_display_fields(
    headers: list[str],
    values: list[str],
) -> dict[str, str | None]:
    out: dict[str, str | None] = {f: None for f in RACECARD_DISPLAY_FIELDS}

    # HKJC racecard 的第一個欄位通常是「馬號」（出場排序）。
    # 但由於 header label 在不同編碼下可能無法穩定匹配到「馬號」關鍵字，
    # 所以改用位置 + 數字外觀做保底。
    if values:
        v0 = (values[0] or "").strip()
        horse_no_field = RACECARD_DISPLAY_FIELDS[0]
        if v0 and re.fullmatch(r"\d{1,2}", v0):
            out[horse_no_field] = v0

    for idx, (h, v) in enumerate(zip(headers, values)):
        if idx == 0:
            continue
        field = _map_header_label_to_field(h)
        if not field:
            continue
        vv = (v or "").strip()
        out[field] = vv if vv else None
    return out


def _finish_pos_to_win_flag(finish_pos: str | None) -> bool | None:
    """
    Convert HKJC finish_pos to win flag.

    DB example: '01'..'14', 'WV', 'WV-A'...
    - numeric position 01 == win
    - WV / non-numeric => not-counted (return None)
    """
    fp = str(finish_pos or "").strip().upper()
    if not fp:
        return None
    if fp.startswith("WV"):
        return None
    m = re.fullmatch(r"0*(\d{1,2})", fp)
    if not m:
        return None
    return int(m.group(1)) == 1


def _finish_pos_to_start_flag(finish_pos: str | None) -> bool:
    """Whether finish_pos represents a numeric finishing position."""
    fp = str(finish_pos or "").strip().upper()
    if not fp:
        return False
    if fp.startswith("WV"):
        return False
    return re.fullmatch(r"0*\d{1,2}", fp) is not None


def _format_odds(odds_decimal: float) -> str:
    """Format odds with up to 2 decimals, trimming trailing zeros."""
    v = round(float(odds_decimal), 2)
    s = f"{v:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _parse_odds_number(raw: Any) -> float | None:
    """Parse odds text to float; return None if missing/invalid."""
    s = str(raw or "").strip().replace(",", "")
    if not s or s in {"-", "—", "N/A", "n/a", "SCR", "scr"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def odds_diff_display(immediate: Any, predicted: Any) -> str:
    """即時賠率 − 預測賠率；缺任一值則空白。"""
    imm = _parse_odds_number(immediate)
    pred = _parse_odds_number(predicted)
    if imm is None or pred is None:
        return ""
    return _format_odds(imm - pred)


def _estimate_raw_win_probability(
    conn: sqlite3.Connection,
    horse_id: str,
    meeting_race_date_yyyy_mm_dd: str,
    lookback_races: int = 12,
    weight_decay: float = 0.9,
    alpha: float = 1.0,
    beta: float = 9.0,
) -> float:
    """
    Estimate win probability from past performance.

    Heuristic:
    - consider last `lookback_races` (by race_records.id desc)
    - weight recency by `weight_decay ** idx`
    - starts = sum(weights for numeric finish_pos)
    - wins = sum(weights for finish_pos indicating win (01))
    - apply beta-like smoothing: (wins + alpha) / (starts + alpha + beta)
    - return raw probability (will be normalized among horses in the same race)
    """
    if not horse_id:
        return 1.0 / (alpha + beta)

    rows = conn.execute(
        """
        SELECT finish_pos, race_date
        FROM race_records
        WHERE horse_id = ?
        ORDER BY id DESC
        LIMIT ?;
        """,
        (horse_id, lookback_races),
    ).fetchall()

    starts_w = 0.0
    wins_w = 0.0
    for idx, r in enumerate(rows):
        w = weight_decay**idx
        fp = r["finish_pos"]
        if not _finish_pos_to_start_flag(fp):
            continue
        starts_w += w
        if _finish_pos_to_win_flag(fp):
            wins_w += w

    return (wins_w + alpha) / (starts_w + alpha + beta)


def _fill_predicted_win_odds_for_race(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
) -> None:
    horse_rows = conn.execute(
        f"""
        SELECT DISTINCT {RACECARD_HIDDEN_HORSE_ID_COLUMN} AS horse_id
        FROM racecard_starters
        WHERE race_date = ? AND racecourse = ? AND race_no = ?
          AND 後備 = '否'
          AND {RACECARD_HIDDEN_HORSE_ID_COLUMN} IS NOT NULL
          AND TRIM({RACECARD_HIDDEN_HORSE_ID_COLUMN}) <> '';
        """,
        (race_date, racecourse, race_no),
    ).fetchall()

    horse_ids = [r["horse_id"] for r in horse_rows if r["horse_id"]]
    if not horse_ids:
        return

    # 1) Estimate raw probabilities
    p_raw_by_hid: dict[str, float] = {}
    for hid in horse_ids:
        p_raw_by_hid[hid] = _estimate_raw_win_probability(
            conn, hid, meeting_race_date_yyyy_mm_dd=race_date
        )

    p_sum = sum(p_raw_by_hid.values()) or 1.0

    # 2) Normalize within the same race, convert to odds
    # Model 預測只更新「預測賠率」，「即時賠率」由 GraphQL winOdds 更新。
    updates: list[tuple[str, str, str, int, str]] = []
    for hid in horse_ids:
        p_norm = p_raw_by_hid[hid] / p_sum
        odds = 1.0 / max(p_norm, 1e-12)
        odds_str = _format_odds(odds)
        updates.append((odds_str, race_date, racecourse, race_no, hid))

    conn.executemany(
        f"""
        UPDATE racecard_starters
        SET \"{RACECARD_PREDICTED_ODDS_COLUMN}\" = ?
        WHERE race_date = ?
          AND racecourse = ?
          AND race_no = ?
          AND {RACECARD_HIDDEN_HORSE_ID_COLUMN} = ?
          AND 後備 = '否';
        """,
        updates,
    )


def _fill_predicted_win_odds_for_meeting(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
) -> None:
    race_nos = [
        int(r["race_no"])
        for r in conn.execute(
            """
            SELECT DISTINCT race_no
            FROM racecard_starters
            WHERE race_date = ? AND racecourse = ?
            ORDER BY race_no ASC;
            """,
            (race_date, racecourse),
        ).fetchall()
    ]

    for rn in race_nos:
        _fill_predicted_win_odds_for_race(conn, race_date, racecourse, rn)


def overnight_odds_confirmed_meta_key(race_date: str, racecourse: str) -> str:
    return f"overnight_odds_confirmed:{race_date}|{racecourse}"


def is_overnight_odds_confirmed(
    conn: sqlite3.Connection, race_date: str, racecourse: str
) -> bool:
    key = overnight_odds_confirmed_meta_key(race_date, racecourse)
    try:
        row = conn.execute(
            "SELECT value FROM racecard_meta WHERE key = ? LIMIT 1;",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    val = str(row["value"] if hasattr(row, "keys") else row[0] or "").strip().lower()
    if not val or val in {"0", "false", "no", "n", "off"}:
        return False
    # 值可為 1／confirmed，或確認當下的時間字串
    return True


def clear_overnight_odds_confirmed(
    conn: sqlite3.Connection, race_date: str, racecourse: str
) -> None:
    """重抓排位時清除鎖定，隔夜賠率恢復跟隨即時。"""
    key = overnight_odds_confirmed_meta_key(race_date, racecourse)
    try:
        conn.execute("DELETE FROM racecard_meta WHERE key = ?;", (key,))
    except sqlite3.OperationalError:
        return


def set_overnight_odds_confirmed(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    *,
    confirmed: bool = True,
) -> None:
    key = overnight_odds_confirmed_meta_key(race_date, racecourse)
    if not confirmed:
        clear_overnight_odds_confirmed(conn, race_date, racecourse)
        return
    now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO racecard_meta(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        """,
        (key, now_txt),
    )


def sync_overnight_odds_from_immediate(
    conn: sqlite3.Connection, race_date: str, racecourse: str
) -> int:
    """未確認時：隔夜賠率 ← 即時賠率。"""
    cur = conn.execute(
        f"""
        UPDATE racecard_starters
           SET "{RACECARD_OVERNIGHT_ODDS_COLUMN}" = "{RACECARD_IMMEDIATE_ODDS_COLUMN}"
         WHERE race_date = ?
           AND racecourse = ?
           AND 後備 = '否';
        """,
        (race_date, racecourse),
    )
    return int(cur.rowcount or 0)


def confirm_overnight_odds_for_meeting(
    conn: sqlite3.Connection, race_date: str, racecourse: str
) -> int:
    """
    確認隔夜賠率：以目前即時賠率寫入隔夜並鎖定，之後即時更新不再改隔夜。
    回傳更新列數。
    """
    n = sync_overnight_odds_from_immediate(conn, race_date, racecourse)
    set_overnight_odds_confirmed(conn, race_date, racecourse, confirmed=True)
    return n


def update_win_odds(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    馬號: str,
    win_odds: Any,
    *,
    sync_overnight: bool = True,
) -> None:
    if sync_overnight:
        sql = f"""
        UPDATE racecard_starters
           SET "{RACECARD_IMMEDIATE_ODDS_COLUMN}" = ?,
               "{RACECARD_OVERNIGHT_ODDS_COLUMN}" = ?
         WHERE race_date = ?
           AND racecourse = ?
           AND race_no = ?
           AND 後備 = '否'
           AND (
                TRIM("馬號") = ?
                OR (
                    TRIM(COALESCE("馬號", '')) GLOB '[0-9]*'
                    AND CAST(TRIM("馬號") AS INTEGER) = CAST(? AS INTEGER)
                )
           );
        """
        params = (win_odds, win_odds, race_date, racecourse, race_no, 馬號, 馬號)
    else:
        sql = f"""
        UPDATE racecard_starters
           SET "{RACECARD_IMMEDIATE_ODDS_COLUMN}" = ?
         WHERE race_date = ?
           AND racecourse = ?
           AND race_no = ?
           AND 後備 = '否'
           AND (
                TRIM("馬號") = ?
                OR (
                    TRIM(COALESCE("馬號", '')) GLOB '[0-9]*'
                    AND CAST(TRIM("馬號") AS INTEGER) = CAST(? AS INTEGER)
                )
           );
        """
        params = (win_odds, race_date, racecourse, race_no, 馬號, 馬號)
    cur = conn.execute(sql, params)
    if cur.rowcount == 0:
        # 只 update 已存在紀錄；找不到就略過並提示。
        print(f"[略過] 未找到匹配：{race_date} {racecourse} 第{race_no}場 馬號={馬號} 後備=否")


def update_place_odds(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    馬號: str,
    place_odds: Any,
) -> None:
    try:
        cur = conn.execute(
            """
            UPDATE racecard_starters
               SET "即時位置賠率" = ?
             WHERE race_date = ?
               AND racecourse = ?
               AND race_no = ?
               AND 後備 = '否'
               AND (
                    TRIM("馬號") = ?
                    OR (
                        TRIM(COALESCE("馬號", '')) GLOB '[0-9]*'
                        AND CAST(TRIM("馬號") AS INTEGER) = CAST(? AS INTEGER)
                    )
               );
            """,
            (place_odds, race_date, racecourse, race_no, 馬號, 馬號),
        )
    except sqlite3.OperationalError:
        return
    if cur.rowcount == 0:
        print(
            f"[略過] 未找到匹配（位置賠率）：{race_date} {racecourse} "
            f"第{race_no}場 馬號={馬號} 後備=否"
        )


def _update_immediate_odds_for_meeting_from_graphql(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse_code: str,
    *,
    config: AppConfig | None = None,
) -> None:
    """
    以白名單內 horseOddsQuery（operationName=racing）更新獨贏／位置賠率。

    注意：舊的 raceMeetings 大查詢目前會回 WHITELIST_ERROR，不可再依賴 runners.winOdds。
    """
    gql = odds_graphql_url(config.data_dir) if config is not None else None
    try:
        race_nos = [
            int(r[0])
            for r in conn.execute(
                """
                SELECT DISTINCT race_no
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC;
                """,
                (race_date, racecourse_code),
            ).fetchall()
        ]
    except Exception:  # noqa: BLE001
        race_nos = []
    if not race_nos:
        # 尚無排位列時，嘗試 1–14 場（馬會未開售則 oddsNodes 為空）
        race_nos = list(range(1, 15))

    wrote_win = 0
    wrote_place = 0
    saw_pool = False
    sync_overnight = not is_overnight_odds_confirmed(conn, race_date, racecourse_code)
    for race_no in race_nos:
        try:
            pools = get_race_odds_pools(
                race_date,
                racecourse_code,
                int(race_no),
                odds_types=["WIN", "PLA"],
                graphql_url=gql,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[即時賠率] 第{race_no}場 GraphQL 失敗（略過）：{exc}")
            continue
        if pools:
            saw_pool = True
        for pool in pools:
            odds_type = str(pool.get("oddsType") or "").strip().upper()
            nodes = pool.get("oddsNodes") or []
            for node in nodes:
                comb = str(node.get("combString") or "").strip()
                odds_val = str(node.get("oddsValue") or "").strip()
                if not comb or not odds_val:
                    continue
                馬號 = comb.lstrip("0") or "0"
                if odds_type == "WIN":
                    update_win_odds(
                        conn=conn,
                        race_date=race_date,
                        racecourse=racecourse_code,
                        race_no=int(race_no),
                        馬號=馬號,
                        win_odds=odds_val,
                        sync_overnight=sync_overnight,
                    )
                    wrote_win += 1
                elif odds_type == "PLA":
                    update_place_odds(
                        conn=conn,
                        race_date=race_date,
                        racecourse=racecourse_code,
                        race_no=int(race_no),
                        馬號=馬號,
                        place_odds=odds_val,
                    )
                    wrote_place += 1

    # 未確認時再整場同步一次，補齊未開出獨贏／新欄位空白列
    if sync_overnight:
        sync_overnight_odds_from_immediate(conn, race_date, racecourse_code)

    if saw_pool and wrote_win == 0:
        print("[即時賠率] 馬會尚未開出獨贏賠率，保留現有數值。")
    elif wrote_win or wrote_place:
        locked = "（隔夜已鎖定）" if not sync_overnight else "（隔夜同步即時）"
        print(
            f"[即時賠率] 已更新獨贏 {wrote_win}／位置 {wrote_place} "
            f"（{race_date} {racecourse_code}）{locked}"
        )


def _race_no_to_label(race_no: int) -> str:
    """
    Convert 1 -> '第一場', 2 -> '第二場', 10 -> '第十場', ...
    """
    cn_digit = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
    }
    if race_no in cn_digit:
        return f"第{cn_digit[race_no]}場"
    if 10 <= race_no <= 19:
        if race_no == 10:
            return "第十場"
        return f"第十{cn_digit.get(race_no % 10, '')}場"
    return f"第 {race_no} 場"


def _normalize_post_time(raw: object) -> str | None:
    """從『13:00』／『13:00草地』等字串抽出開跑時間 HH:mm。"""
    text = str(raw or "").strip()
    if not text:
        return None
    matched = re.search(r"(\d{1,2}):(\d{2})", text)
    if not matched:
        return text
    return f"{int(matched.group(1)):02d}:{matched.group(2)}"


def _race_tab_label(race_no: int, post_time: object = None) -> str:
    """場次分頁標題：第一場 13:00（有開跑時間時附上）。"""
    base = _race_no_to_label(race_no)
    t = _normalize_post_time(post_time)
    return f"{base} {t}" if t else base


def _clean_cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _extract_horse_id_from_row(tr: Any) -> str | None:
    # In the row there are usually <a href="...horse?horseid=HK_xxxx">...</a>.
    for a in tr.find_all("a", href=True):
        href = str(a["href"])
        m = re.search(r"[?&]horseid=([^&\s]+)", href, re.IGNORECASE)
        if m:
            hid = m.group(1).strip()
            if hid:
                return hid
    return None


def _parse_latest_meeting_from_base_racecard(html: str) -> tuple[str, str] | None:
    """
    Return (racedate_yyyy_mm_dd, racecourse_code).

    We heuristically parse the first occurrence like:
      第 1 場 - ... 2026年3月22日, 星期日, 沙田, 13:00草地, ...
    """
    # Date
    m_date = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", html)
    if not m_date:
        return None
    yyyy, mm, dd = m_date.groups()
    racedate = f"{int(yyyy):04d}/{int(mm):02d}/{int(dd):02d}"

    # Course name shortly after date
    tail = html[m_date.end() : m_date.end() + 200]
    m_course = re.search(r"(沙田|跑馬地|從化)", tail)
    if not m_course:
        return None
    course_name = m_course.group(1)
    code = COURSE_NAME_TO_CODE.get(course_name)
    if not code:
        return None
    return racedate, code


def _extract_race_nos_from_base_racecard(html: str) -> list[int]:
    """
    Extract race numbers from the base racecard page.

    We look for pattern like: "第 1 場" and return sorted unique integers.
    """
    # Limit search window to reduce false matches.
    m_date = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", html)
    window = html
    if m_date:
        window = html[m_date.end() : m_date.end() + 8000]

    nos = [int(m.group(1)) for m in re.finditer(r"第\s*(\d+)\s*場", window)]
    uniq = sorted(set(nos))
    return uniq


def _race_finished(race_date: str, race_no: int, racecourse_code: str) -> bool:
    """
    Determine whether a race already has result rows.

    Note:
    HKJC 的 `localresults` 目前似乎會忽略/覆蓋傳入的 `racedate` 參數，
    導致用它判斷「該期是否已開跑/已完成」會誤判，進而跳過所有 racecard 入庫。

    在這版中改採簡單且穩定的規則：
    - `race_date` 小於今天 => 視為已完成，跳過重新抓取
    - `race_date` 等於或大於今天 => 不跳過，確保能抓到資料
    """
    # race_date expected format: YYYY/MM/DD
    try:
        dt = datetime.strptime(race_date.replace("-", "/"), "%Y/%m/%d").date()
    except Exception:
        # 如果 racedate 解析失敗，就不要跳過，避免漏抓。
        return False

    today = datetime.now().date()
    return dt < today


def _fetch_racecard_html(
    race_date: str,
    race_no: int,
    racecourse_code: str,
    *,
    base_url: str | None = None,
) -> str:
    url = (base_url or RACECARD_BASE_URL).strip() or RACECARD_BASE_URL
    resp = requests.get(
        url,
        params={"racedate": race_date, "RaceNo": race_no, "Racecourse": racecourse_code},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def _parse_race_event_panel_from_racecard_html(
    html: str, race_no: int
) -> dict[str, str | None]:
    """
    從 HKJC 排位頁（單場）上方資訊區解析場次資料。

    典型結構（zh-hk）：
      第 N 場 - {場事名稱}</span><br>
      {YYYY年M月D日}, {星期}, {馬場}, {HH:MM}<br>
      {草地|全天候}, "{賽道}" 賽道, {途程}<br>
      獎金: ..., 評分: ..., {班次}</div>

    若頁面為「沒有相關資料」錯誤頁，回傳全部 None。
    """
    empty: dict[str, str | None] = {
        "event_name": None,
        "calendar_date": None,
        "weekday": None,
        "meeting_venue": None,
        "post_time": None,
        "race_course_area": None,
        "track_name": None,
        "distance": None,
        "prize_money": None,
        "rating_range": None,
        "race_class": None,
    }
    if not html:
        return empty
    if "沒有相關資料" in html and "errorContainer" in html:
        return empty

    m = re.search(
        rf"第\s*{race_no}\s*場\s*-\s*([^<]+)</span><br>"
        r"([^<]+)<br>"
        r"([^<]+)<br>"
        r"([^<]+)</div>",
        html,
    )
    if not m:
        return empty

    event_name = _clean_cell_text(m.group(1))
    line1 = _clean_cell_text(m.group(2))
    line2 = _clean_cell_text(m.group(3))
    line3 = _clean_cell_text(m.group(4))

    # 容許「13:00草地」等接在時間後的字樣
    m1 = re.match(
        r"^(\d{4}年\d{1,2}月\d{1,2}日)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*(\d{1,2}:\d{2})",
        line1,
    )
    calendar_date = weekday = meeting_venue = post_time = None
    if m1:
        calendar_date, weekday, meeting_venue, post_time = (
            _clean_cell_text(m1.group(1)),
            _clean_cell_text(m1.group(2)),
            _clean_cell_text(m1.group(3)),
            _normalize_post_time(m1.group(4)),
        )

    m2 = re.match(r"^(.+?)\s*,\s*\"([^\"]+)\"\s*賽道\s*,\s*(.+)\s*$", line2)
    race_course_area = track_name = distance = None
    if m2:
        race_course_area = _clean_cell_text(m2.group(1))
        track_name = _clean_cell_text(m2.group(2))
        distance = _clean_cell_text(m2.group(3))

    m3 = re.match(
        r"^獎金:\s*(.+?)\s*,\s*評分:\s*(.+?)\s*,\s*(.+)\s*$",
        line3,
    )
    prize_money = rating_range = race_class = None
    if m3:
        prize_money = _clean_cell_text(m3.group(1))
        rating_range = _clean_cell_text(m3.group(2))
        race_class = _clean_cell_text(m3.group(3))

    out = dict(empty)
    out.update(
        {
            "event_name": event_name or None,
            "calendar_date": calendar_date,
            "weekday": weekday,
            "meeting_venue": meeting_venue,
            "post_time": post_time,
            "race_course_area": race_course_area,
            "track_name": track_name,
            "distance": distance,
            "prize_money": prize_money,
            "rating_range": rating_range,
            "race_class": race_class,
        }
    )
    return out


def _upsert_race_event_info(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    info: dict[str, str | None],
) -> None:
    if not any(v for v in info.values() if v):
        return
    conn.execute(
        f"""
        INSERT INTO {RACECARD_RACE_INFO_TABLE} (
            race_date, racecourse, race_no,
            event_name, calendar_date, weekday, meeting_venue, post_time,
            race_course_area, track_name, distance,
            prize_money, rating_range, race_class
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(race_date, racecourse, race_no) DO UPDATE SET
            event_name=excluded.event_name,
            calendar_date=excluded.calendar_date,
            weekday=excluded.weekday,
            meeting_venue=excluded.meeting_venue,
            post_time=excluded.post_time,
            race_course_area=excluded.race_course_area,
            track_name=excluded.track_name,
            distance=excluded.distance,
            prize_money=excluded.prize_money,
            rating_range=excluded.rating_range,
            race_class=excluded.race_class;
        """,
        (
            race_date,
            racecourse,
            race_no,
            info.get("event_name"),
            info.get("calendar_date"),
            info.get("weekday"),
            info.get("meeting_venue"),
            info.get("post_time"),
            info.get("race_course_area"),
            info.get("track_name"),
            info.get("distance"),
            info.get("prize_money"),
            info.get("rating_range"),
            info.get("race_class"),
        ),
    )


def _build_race_info_header_widget(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
) -> QFrame:
    """每個分頁頂部：場次摘要；每行排 4 組「標籤＋內容」（約佔 3～4 欄資料寬）。"""
    frame = QFrame()
    frame.setStyleSheet(
        """
        QFrame#raceInfoFrame {
            background: #f4f7fc;
            border: 1px solid #b8c9e0;
            border-radius: 6px;
            padding: 6px 8px;
        }
        """
    )
    frame.setObjectName("raceInfoFrame")

    row = conn.execute(
        f"""
        SELECT event_name, calendar_date, weekday, meeting_venue, post_time,
               race_course_area, track_name, distance,
               prize_money, rating_range, race_class
        FROM {RACECARD_RACE_INFO_TABLE}
        WHERE race_date = ? AND racecourse = ? AND race_no = ?
        LIMIT 1;
        """,
        (race_date, racecourse, race_no),
    ).fetchone()

    grid = QGridLayout(frame)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(2)
    grid.setContentsMargins(4, 4, 4, 4)
    # 每行 4 組，每組佔 2 欄（標籤 | 內容）→ 共 8 欄；內容欄平均分配剩餘寬度
    pairs_per_row = 4
    for j in range(pairs_per_row):
        grid.setColumnStretch(j * 2 + 1, 1)

    pairs: list[tuple[str, str]] = [
        ("場事名稱", "event_name"),
        ("日子", "calendar_date"),
        ("星期", "weekday"),
        ("比賽場地", "meeting_venue"),
        ("時間", "post_time"),
        ("場地", "race_course_area"),
        ("賽道", "track_name"),
        ("途程", "distance"),
        ("獎金", "prize_money"),
        ("評分範圍", "rating_range"),
        ("班次", "race_class"),
        ("標準完成時間", "standard_finish_time"),
    ]

    if not row:
        tip = QLabel("（此場尚無場次摘要：請按「更新資料」重新抓取，或該日賽事頁面暫無資料）")
        tip.setWordWrap(True)
        grid.addWidget(tip, 0, 0, 1, pairs_per_row * 2)
        frame.setProperty("post_time", "")
        return frame

    from racing.standard_times import attach_standard_finish_time

    rdict = attach_standard_finish_time(dict(row)) or {}
    frame.setProperty("post_time", _normalize_post_time(rdict.get("post_time")) or "")
    for i, (cn, key) in enumerate(pairs):
        row_idx = i // pairs_per_row
        slot = i % pairs_per_row
        base_col = slot * 2

        lk = QLabel(f"{cn}：")
        lk.setStyleSheet("color: #1a3a5c; font-weight: 600;")
        lk.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val = rdict.get(key)
        if key == "post_time":
            val = _normalize_post_time(val)
        if (
            key == "track_name"
            and val is not None
            and str(val).strip()
            and "賽道" not in str(val)
        ):
            val = f'{str(val).strip()} 賽道'
        vv = QLabel("—" if val is None or str(val).strip() == "" else str(val))
        vv.setWordWrap(True)
        vv.setStyleSheet("color: #222;")
        vv.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(lk, row_idx, base_col, alignment=Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(vv, row_idx, base_col + 1)

    return frame


def _parse_racecard_tables(
    html: str,
) -> tuple[list[str], list[tuple[str | None, list[str]]], list[str], list[tuple[str | None, list[str]]]] | None:
    """
    Returns:
      main_headers, main_rows_values, reserve_headers, reserve_rows_values

    Each rows_values is aligned to its corresponding headers order.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return None

    def table_headers(table: Any) -> list[str]:
        tr = table.find("tr")
        if not tr:
            return []
        cells = tr.find_all(["th", "td"])
        if not cells:
            return []
        return [_clean_cell_text(c.get_text(" ", strip=True)) for c in cells]

    def looks_like_main(headers: list[str]) -> bool:
        joined = " ".join(headers)
        return ("馬名" in joined and ("檔位" in joined or "負磅" in joined)) or "檔位" in headers

    def looks_like_reserve(headers: list[str]) -> bool:
        joined = " ".join(headers)
        return ("排位體重" in joined and "優先參賽次序" in joined) or ("後備" in joined)

    main_table = None
    reserve_table = None
    main_headers: list[str] = []
    reserve_headers: list[str] = []

    for t in tables:
        headers = table_headers(t)
        if not headers:
            continue
        if main_table is None and looks_like_main(headers):
            main_table = t
            main_headers = headers
        elif reserve_table is None and looks_like_reserve(headers):
            reserve_table = t
            reserve_headers = headers

    if main_table is None:
        return None

    def rows_from_table(table: Any, headers: list[str]) -> list[tuple[str | None, list[str]]]:
        rows: list[tuple[str | None, list[str]]] = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if not tds:
                continue
            values = [_clean_cell_text(td.get_text(" ", strip=True)) for td in tds]
            horse_id = _extract_horse_id_from_row(tr)
            # Align to header length (ignore extra columns).
            if len(values) >= len(headers):
                rows.append((horse_id, values[: len(headers)]))
            else:
                # If fewer cells, pad.
                padded = values + [""] * (len(headers) - len(values))
                rows.append((horse_id, padded))
        return rows

    main_rows = rows_from_table(main_table, main_headers)
    reserve_rows: list[tuple[str | None, list[str]]] = []
    if reserve_table is not None and reserve_headers:
        reserve_rows = rows_from_table(reserve_table, reserve_headers)

    return main_headers, main_rows, reserve_headers, reserve_rows


@dataclass(frozen=True, slots=True)
class LatestMeeting:
    racedate: str
    racecourse_code: str


def _upsert_racecard_for_race(
    conn: sqlite3.Connection,
    config: AppConfig,
    race_date: str,
    race_no: int,
    racecourse_code: str,
) -> int:
    html = _fetch_racecard_html(
        race_date, race_no, racecourse_code, base_url=_racecard_url(config)
    )
    info = _parse_race_event_panel_from_racecard_html(html, race_no)
    _upsert_race_event_info(conn, race_date, racecourse_code, race_no, info)
    parsed = _parse_racecard_tables(html)
    if not parsed:
        return 0

    main_headers, main_rows, reserve_headers, reserve_rows = parsed
    reserve_headers = reserve_headers or []

    # 重抓該場出馬表：隔夜改回跟隨即時（整場賽日鎖定一併解除）
    clear_overnight_odds_confirmed(conn, race_date, racecourse_code)

    # Delete previous rows for the race (both reserve/main).
    conn.execute(
        """
        DELETE FROM racecard_starters
        WHERE race_date = ? AND racecourse = ? AND race_no = ?;
        """,
        (race_date, racecourse_code, race_no),
    )

    def qident(col: str) -> str:
        return '"' + col.replace('"', '""') + '"'

    fixed_cols = list(RACECARD_DISPLAY_FIELDS)
    insert_cols = [
        "race_date",
        "racecourse",
        "race_no",
        "後備",
        RACECARD_HIDDEN_HORSE_ID_COLUMN,
        *fixed_cols,
        RACECARD_PREDICTED_ODDS_COLUMN,
        RACECARD_OVERNIGHT_ODDS_COLUMN,
        RACECARD_IMMEDIATE_ODDS_COLUMN,
    ]
    insert_cols_sql = [qident(c) for c in insert_cols]
    placeholders = ",".join(["?"] * len(insert_cols_sql))
    insert_sql = (
        f"INSERT INTO racecard_starters ({','.join(insert_cols_sql)}) VALUES ({placeholders});"
    )

    def insert_row(
        reserve_flag: str,
        horse_id: str | None,
        headers: list[str],
        values: list[str],
    ) -> None:
        fields = _extract_racecard_display_fields(headers, values)
        # Keep empty strings as NULL for cleaner display/sorting.
        row_vals: list[Any] = [race_date, racecourse_code, race_no, reserve_flag, horse_id]
        row_vals.extend(fields.get(c) for c in fixed_cols)
        # 預測／隔夜／即時先空值；抓賠率後即時寫入，未確認時隔夜跟隨即時。
        row_vals.append(None)
        row_vals.append(None)
        row_vals.append(None)
        conn.execute(insert_sql, row_vals)

    inserted = 0
    # Main rows.
    for _hid, values in main_rows:
        insert_row("否", _hid, main_headers, values)
        inserted += 1

    # Reserve rows.
    if reserve_rows and reserve_headers:
        for _hid, values in reserve_rows:
            insert_row("是", _hid, reserve_headers, values)
            inserted += 1

    return inserted


def init_racecard_db(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        # Schema is centrally managed in racing.schema; UI only ensures DB is initialized.
        conn.executescript(racecard_base_schema_sql(RACECARD_RACE_INFO_TABLE))
        # racecard_starters 欄位定義與兼容修復集中在 racing.schema。
        ensure_racecard_starters_columns(
            conn=conn,
            starters_table="racecard_starters",
            display_fields=RACECARD_DISPLAY_FIELDS,
            predicted_odds_col=RACECARD_PREDICTED_ODDS_COLUMN,
            overnight_odds_col=RACECARD_OVERNIGHT_ODDS_COLUMN,
            immediate_odds_col=RACECARD_IMMEDIATE_ODDS_COLUMN,
            place_odds_col=RACECARD_PLACE_ODDS_COLUMN,
        )
    finally:
        conn.close()


def list_stored_racecard_meetings(config: AppConfig) -> list[tuple[str, str]]:
    """
    資料庫內已有排位資料的 (賽事日期, 場地代碼)，新至舊排序。
    日期格式與庫內一致：YYYY/MM/DD。
    """
    init_racecard_db(config.db_path)
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT race_date, racecourse
            FROM racecard_starters
            ORDER BY race_date DESC, racecourse ASC;
            """
        ).fetchall()
        return [(str(r["race_date"]), str(r["racecourse"])) for r in rows]
    finally:
        conn.close()


def update_immediate_odds_for_latest_meeting(config: AppConfig) -> LatestMeeting | None:
    """只更新資料庫中最新賽日的即時賠率（GraphQL winOdds），不重抓排位頁。"""
    stored = list_stored_racecard_meetings(config)
    if not stored:
        return None
    race_date, racecourse = stored[0]
    init_racecard_db(config.db_path)
    conn = get_connection(config.db_path)
    try:
        conn.execute("SAVEPOINT sp_immediate_odds_only;")
        try:
            _update_immediate_odds_for_meeting_from_graphql(
                conn=conn,
                race_date=race_date,
                racecourse_code=racecourse,
                config=config,
            )
            conn.execute("RELEASE sp_immediate_odds_only;")
        except Exception:
            conn.execute("ROLLBACK TO sp_immediate_odds_only;")
            conn.execute("RELEASE sp_immediate_odds_only;")
            raise
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value)
            VALUES('last_odds_update_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (now_txt,),
        )
        conn.commit()
        return LatestMeeting(racedate=race_date, racecourse_code=racecourse)
    finally:
        conn.close()


def update_racecard_meeting_from_web(
    config: AppConfig, race_date: str, racecourse_code: str
) -> LatestMeeting | None:
    """
    依指定賽日 + 場地從 HKJC 下載該賽馬日各場排位並寫入 SQLite（不套用「已賽畢則跳過」邏輯，方便補抓過往）。

    race_date: YYYY/MM/DD；racecourse_code: ST / HV / FC
    """
    init_racecard_db(config.db_path)
    base = _racecard_url(config)
    resp = requests.get(
        base,
        params={"racedate": race_date, "Racecourse": racecourse_code},
        timeout=25,
    )
    resp.raise_for_status()
    html = resp.text
    if "沒有相關資料" in html and "errorContainer" in html:
        return None

    race_nos = _extract_race_nos_from_base_racecard(html)
    if not race_nos:
        race_nos = list(range(1, 15))

    meeting = LatestMeeting(racedate=race_date, racecourse_code=racecourse_code)
    conn = get_connection(config.db_path)
    try:
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        race_nos_csv = ",".join(str(n) for n in race_nos)
        for race_no in race_nos:
            _upsert_racecard_for_race(conn, config, race_date, race_no, racecourse_code)
            conn.commit()

        _fill_predicted_win_odds_for_meeting(conn, race_date, racecourse_code)

        try:
            conn.execute("SAVEPOINT sp_immediate_odds_graphql;")
            _update_immediate_odds_for_meeting_from_graphql(
                conn=conn,
                race_date=race_date,
                racecourse_code=racecourse_code,
                config=config,
            )
            conn.execute("RELEASE sp_immediate_odds_graphql;")
        except Exception as e:
            conn.execute("ROLLBACK TO sp_immediate_odds_graphql;")
            conn.execute("RELEASE sp_immediate_odds_graphql;")
            print(f"[即時賠率] GraphQL 更新失敗（略過）：{e}")

        conn.execute(
            """
            INSERT INTO racecard_meta(key, value)
            VALUES('last_update_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (now_txt,),
        )
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value) VALUES
              ('last_source_race_date', ?),
              ('last_source_racecourse_code', ?),
              ('last_source_race_nos', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (race_date, racecourse_code, race_nos_csv),
        )
        conn.commit()
        return meeting
    finally:
        conn.close()


def format_racecard_source_urls_for_meeting(
    config: AppConfig, meeting: LatestMeeting
) -> str:
    """依目前資料庫內該賽日場次組出來源 URL 字串（供標籤顯示）。"""
    base = _racecard_url(config)
    conn = get_connection(config.db_path)
    try:
        race_nos = [
            str(r["race_no"])
            for r in conn.execute(
                """
                SELECT DISTINCT race_no
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC;
                """,
                (meeting.racedate, meeting.racecourse_code),
            ).fetchall()
        ]
        if not race_nos:
            return f"來源 URL：{base}"
        urls: list[str] = []
        for rn in race_nos[:8]:
            urls.append(
                f"{base}?racedate={meeting.racedate}&RaceNo={rn}&Racecourse={meeting.racecourse_code}"
            )
        tail = ""
        if len(race_nos) > 8:
            tail = f"…（另 {len(race_nos) - 8} 場）"
        return "來源 URL：" + "\n".join(urls) + tail
    finally:
        conn.close()


def update_latest_racecard_data(config: AppConfig) -> LatestMeeting | None:
    """
    Auto-update latest meeting:
    - infer latest meeting from base racecard page
    - fetch racecards for races in that meeting until we stop finding data
    - skip races that already have results
    """
    init_racecard_db(config.db_path)
    base = _racecard_url(config)
    base_html = requests.get(base, timeout=20).text
    latest = _parse_latest_meeting_from_base_racecard(base_html)
    if not latest:
        return None
    race_date, racecourse_code = latest
    meeting = LatestMeeting(racedate=race_date, racecourse_code=racecourse_code)

    race_nos = _extract_race_nos_from_base_racecard(base_html)
    if not race_nos:
        # Fallback to typical range.
        race_nos = list(range(1, 15))

    conn = get_connection(config.db_path)
    try:
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        race_nos_csv = ",".join(str(n) for n in race_nos)
        for race_no in race_nos:
            before = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ? AND race_no = ?;
                """,
                (race_date, racecourse_code, race_no),
            ).fetchone()["c"]

            # Only skip if finished AND we already have data for this race.
            # Additionally, if 馬號 欄位是空的，代表我們上次的解析/映射不完整，需要重抓補齊。
            horse_no_col = RACECARD_DISPLAY_FIELDS[0]
            qident = '"' + horse_no_col.replace('"', '""') + '"'
            missing_horse_no = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ? AND race_no = ?
                  AND ({qident} IS NULL OR TRIM({qident}) = '');
                """,
                (race_date, racecourse_code, race_no),
            ).fetchone()["c"]

            horse_id_col = RACECARD_HIDDEN_HORSE_ID_COLUMN
            missing_horse_id = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ? AND race_no = ?
                  AND ({horse_id_col} IS NULL OR TRIM({horse_id_col}) = '');
                """,
                (race_date, racecourse_code, race_no),
            ).fetchone()["c"]

            if (
                _race_finished(race_date, race_no, racecourse_code)
                and before
                and before > 0
                and missing_horse_no == 0
                and missing_horse_id == 0
            ):
                continue

            inserted = _upsert_racecard_for_race(
                conn, config, race_date, race_no, racecourse_code
            )
            conn.commit()
            _ = before  # keep for possible future logic
            _ = inserted

        # Fill predicted win odds for this meeting (only where 後備='否').
        _fill_predicted_win_odds_for_meeting(conn, race_date, racecourse_code)

        # After we have racecard rows, update "即時賠率" from HKJC GraphQL (winOdds).
        # If GraphQL is blocked/fails, keep existing odds (currently predicted).
        try:
            conn.execute("SAVEPOINT sp_immediate_odds_graphql;")
            _update_immediate_odds_for_meeting_from_graphql(
                conn=conn,
                race_date=race_date,
                racecourse_code=racecourse_code,
                config=config,
            )
            conn.execute("RELEASE sp_immediate_odds_graphql;")
        except Exception as e:
            conn.execute("ROLLBACK TO sp_immediate_odds_graphql;")
            conn.execute("RELEASE sp_immediate_odds_graphql;")
            print(f"[即時賠率] GraphQL 更新失敗（略過）：{e}")

        # Always update "last update time" when we have determined a meeting.
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value)
            VALUES('last_update_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (now_txt,),
        )
        conn.commit()

        # Save sources attempted (for UI display / debugging).
        conn.execute(
            """
            INSERT INTO racecard_meta(key, value) VALUES
              ('last_source_race_date', ?),
              ('last_source_racecourse_code', ?),
              ('last_source_race_nos', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value;
            """,
            (race_date, racecourse_code, race_nos_csv),
        )
        conn.commit()
        return meeting
    finally:
        conn.close()


def confirm_overnight_odds(
    config: AppConfig, race_date: str, racecourse: str
) -> dict[str, Any]:
    """確認目前賽日之隔夜賠率（寫入並鎖定）。"""
    init_racecard_db(config.db_path)
    conn = get_connection(config.db_path)
    try:
        count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM racecard_starters
            WHERE race_date = ? AND racecourse = ? AND 後備 = '否';
            """,
            (race_date, racecourse),
        ).fetchone()["c"]
        if not count:
            return {
                "ok": False,
                "error": "no_starters",
                "message": "此賽日尚無排位，無法確認隔夜賠率。",
            }
        updated = confirm_overnight_odds_for_meeting(conn, race_date, racecourse)
        conn.commit()
        return {
            "ok": True,
            "updated": updated,
            "overnight_confirmed": True,
            "confirmed_at": get_racecard_meta_value(
                config, overnight_odds_confirmed_meta_key(race_date, racecourse)
            ),
            "message": f"已確認隔夜賠率（{updated} 匹），之後即時賠率變更不會再改隔夜。",
        }
    finally:
        conn.close()


def get_overnight_odds_confirmed(
    config: AppConfig, race_date: str, racecourse: str
) -> bool:
    init_racecard_db(config.db_path)
    conn = get_connection(config.db_path)
    try:
        return is_overnight_odds_confirmed(conn, race_date, racecourse)
    finally:
        conn.close()


def get_last_update_at(config: AppConfig) -> str:
    conn = get_connection(config.db_path)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM racecard_meta WHERE key='last_update_at' LIMIT 1;"
            ).fetchone()
            return str(row["value"]) if row else ""
        except sqlite3.OperationalError:
            # Table not created yet.
            return ""
    finally:
        conn.close()


def get_racecard_meta_value(config: AppConfig, key: str) -> str:
    conn = get_connection(config.db_path)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM racecard_meta WHERE key = ? LIMIT 1;",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else ""
        except sqlite3.OperationalError:
            return ""
    finally:
        conn.close()


class RacecardPanel(QWidget):
    """
    排位（Racecard）內容區：可嵌入主視窗，亦可由 `RacecardRankWindow` 包成獨立視窗。
    """
    horse_double_clicked = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._meeting: LatestMeeting | None = None

        root = QVBoxLayout()
        # Ensure tables exist before reading meta.
        init_racecard_db(self._config.db_path)
        self._last_update_label = QLabel("上次更新時間：")
        self._overnight_label = QLabel("隔夜賠率：跟隨即時")
        self._update_btn = QPushButton("更新資料")
        self._confirm_overnight_btn = QPushButton("確認隔夜賠率")
        self._confirm_overnight_btn.setToolTip(
            "把目前即時賠率寫入隔夜並鎖定；之後即時更新不會再改隔夜賠率"
        )
        style = self.style()
        if style is not None:
            self._update_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            self._confirm_overnight_btn.setIcon(
                style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
            )
        self._update_btn.clicked.connect(self._on_update_clicked)
        self._confirm_overnight_btn.clicked.connect(self._on_confirm_overnight_clicked)

        # 可選擇資料庫內已下載的賽事日期，顯示該日排位
        self._history_combo = QComboBox()
        self._history_combo.setMinimumWidth(280)
        self._history_combo.setToolTip("選擇資料庫內已有的賽事日期／場地，即時顯示該日排位")
        self._history_combo.currentIndexChanged.connect(self._on_history_combo_changed)

        top = QHBoxLayout()
        top.addWidget(self._last_update_label, 1)
        top.addWidget(self._overnight_label, 0)
        top.addWidget(self._confirm_overnight_btn, 0)
        top.addWidget(self._update_btn, 0)
        root.addLayout(top)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("賽事日期："))
        date_row.addWidget(self._history_combo, 1)
        root.addLayout(date_row)

        self._tabs = QTabWidget()
        # 排位約 60%、下方往績約 40%
        root.addWidget(self._tabs, 3)

        self._form_title = QLabel("往績（請選擇上方馬匹）")
        self._form_title.setStyleSheet("font-weight: 700;")
        self._form_table = QTableWidget(0, len(RACE_RECORD_GRID_HEADERS))
        self._form_table.setHorizontalHeaderLabels(list(RACE_RECORD_GRID_HEADERS))
        configure_data_grid(
            self._form_table,
            fixed_viewport=False,
            visible_rows=6,
            multi_select=False,
            multi_sort=True,
            multi_sort_headers=list(RACE_RECORD_GRID_HEADERS),
        )
        install_grid_keyboard_nav(self._form_table)
        root.addWidget(self._form_title, 0)
        root.addWidget(self._form_table, 2)

        self.setLayout(root)
        self._form_horse_id = ""

        self._load_latest_stored_meeting()

    def reload_stored(self) -> None:
        """重新讀取資料庫裡已有的排位；盡量保留目前選取的賽日。"""
        keep = self._meeting
        self._refresh_last_update_label()
        self._refresh_history_combo(select_meeting=keep)
        if self._history_combo.count() <= 0:
            self._meeting = None
            self._tabs.clear()
            self._clear_form()
            return
        # 若先前賽日仍在清單，維持；否則改最新
        data = self._history_combo.currentData()
        if data and isinstance(data, tuple) and len(data) == 2:
            self._meeting = LatestMeeting(racedate=str(data[0]), racecourse_code=str(data[1]))
        elif keep is None:
            self._load_latest_stored_meeting()
            return
        self._load_grid()

    def _load_latest_stored_meeting(self) -> None:
        stored = list_stored_racecard_meetings(self._config)
        if stored:
            race_date, racecourse = stored[0]
            self._meeting = LatestMeeting(racedate=race_date, racecourse_code=racecourse)
        else:
            self._meeting = None
        self._refresh_last_update_label()
        self._refresh_history_combo(select_meeting=self._meeting)
        self._load_grid()

    def _history_combo_display(self, race_date: str, racecourse: str) -> str:
        disp = COURSE_CODE_TO_DISPLAY.get(racecourse, racecourse)
        return f"{race_date}  {disp}（{racecourse}）"

    def _refresh_history_combo(self, select_meeting: LatestMeeting | None = None) -> None:
        self._history_combo.blockSignals(True)
        self._history_combo.clear()
        pairs = list_stored_racecard_meetings(self._config)
        for rd, rc in pairs:
            self._history_combo.addItem(self._history_combo_display(rd, rc), (rd, rc))
        pick = select_meeting or self._meeting
        if pick and pairs:
            target = (pick.racedate, pick.racecourse_code)
            for i in range(self._history_combo.count()):
                if self._history_combo.itemData(i) == target:
                    self._history_combo.setCurrentIndex(i)
                    break
        elif pairs:
            self._history_combo.setCurrentIndex(0)
        self._history_combo.blockSignals(False)

    def _on_history_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        data = self._history_combo.itemData(index)
        if not data or not isinstance(data, tuple) or len(data) != 2:
            return
        rd, rc = str(data[0]), str(data[1])
        self._meeting = LatestMeeting(racedate=rd, racecourse_code=rc)
        self._load_grid()

    def _refresh_last_update_label(self) -> None:
        last = get_last_update_at(self._config)
        if last:
            self._last_update_label.setText(f"上次更新時間：{last}")
        else:
            self._last_update_label.setText("上次更新時間：尚未更新")
        self._refresh_overnight_status()

    def _refresh_overnight_status(self) -> None:
        if self._meeting is None:
            self._overnight_label.setText("隔夜賠率：—")
            self._confirm_overnight_btn.setEnabled(False)
            return
        confirmed = get_overnight_odds_confirmed(
            self._config, self._meeting.racedate, self._meeting.racecourse_code
        )
        if confirmed:
            at = get_racecard_meta_value(
                self._config,
                overnight_odds_confirmed_meta_key(
                    self._meeting.racedate, self._meeting.racecourse_code
                ),
            )
            tip = f"（{at}）" if at else ""
            self._overnight_label.setText(f"隔夜賠率：已確認{tip}")
            self._confirm_overnight_btn.setEnabled(True)
            self._confirm_overnight_btn.setText("再次確認隔夜賠率")
        else:
            self._overnight_label.setText("隔夜賠率：跟隨即時")
            self._confirm_overnight_btn.setEnabled(True)
            self._confirm_overnight_btn.setText("確認隔夜賠率")

    def _on_confirm_overnight_clicked(self) -> None:
        if self._meeting is None:
            QMessageBox.information(self, "確認隔夜賠率", "尚無排位賽日。")
            return
        reply = QMessageBox.question(
            self,
            "確認隔夜賠率",
            "將以目前「即時賠率」寫入「隔夜賠率」並鎖定。\n"
            "之後下載／更新即時賠率時，隔夜賠率不會再變更。\n\n確定？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = confirm_overnight_odds(
            self._config, self._meeting.racedate, self._meeting.racecourse_code
        )
        if not result.get("ok"):
            QMessageBox.warning(
                self, "確認隔夜賠率", str(result.get("message") or "確認失敗")
            )
            return
        self._refresh_overnight_status()
        self._load_grid()
        QMessageBox.information(
            self, "確認隔夜賠率", str(result.get("message") or "已確認。")
        )

    def _on_update_clicked(self) -> None:
        win = self.window()
        start = getattr(win, "_start_download_with_progress", None)
        if callable(start):
            start(do_horses=False, do_racecard=True, title="更新排位資料")
            return
        QMessageBox.information(self, "更新資料", "請從主視窗工具列的「下載」更新排位。")

    def _load_grid(self) -> None:
        conn = get_connection(self._config.db_path)
        try:
            display_cols = list(RACECARD_DISPLAY_COLUMNS)
            db_cols = list(RACECARD_DB_DISPLAY_COLUMNS)
            display_cols_sql = [f'"{c}"' for c in db_cols]

            # Filter by current meeting if we have it.
            if self._meeting:
                race_date = self._meeting.racedate
                racecourse = self._meeting.racecourse_code
                # 未確認：開啟畫面時把隔夜對齊即時（相容舊資料空白欄）
                if not is_overnight_odds_confirmed(conn, race_date, racecourse):
                    sync_overnight_odds_from_immediate(conn, race_date, racecourse)
                    conn.commit()
            else:
                # Fallback: take the latest race_date and its first course.
                row = conn.execute(
                    """
                    SELECT race_date, racecourse
                    FROM racecard_starters
                    ORDER BY race_date DESC
                    LIMIT 1;
                    """
                ).fetchone()
                if not row:
                    self._tabs.clear()
                    return
                race_date = str(row["race_date"])
                racecourse = str(row["racecourse"])

            race_nos = [
                int(rn["race_no"])
                for rn in conn.execute(
                    """
                    SELECT DISTINCT race_no
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ?
                    ORDER BY race_no ASC;
                    """,
                    (race_date, racecourse),
                ).fetchall()
            ]

            self._tabs.clear()

            if not race_nos:
                show_copyable_message(
                    self,
                    "沒有資料",
                    "沒有抓到任何排位資料可顯示（可能是 racecard 解析失敗或該期尚未開跑）。",
                )
                return

            order_sql = "CASE WHEN 後備 = '否' THEN 0 ELSE 1 END"
            select_cols_sql = ",".join(display_cols_sql)
            horse_id_col = RACECARD_HIDDEN_HORSE_ID_COLUMN
            for race_no in race_nos:
                table = QTableWidget()
                table.setColumnCount(len(display_cols))
                table.setHorizontalHeaderLabels(display_cols)
                configure_data_grid(
                    table,
                    multi_select=True,
                    enable_copy=True,
                    fixed_viewport=False,
                    multi_sort=True,
                    multi_sort_headers=list(display_cols),
                )
                table.setShowGrid(True)
                table.setStyleSheet(
                    """
                    QTableWidget {
                        gridline-color: #7a96b8;
                        border: 1px solid #7a96b8;
                    }
                    QTableWidget::item {
                        border: 1px solid #9eb6d9;
                        padding: 2px 4px;
                    }
                    QHeaderView::section {
                        border: 1px solid #7a96b8;
                    }
                    """
                )
                table.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
                )

                rows = conn.execute(
                    f"""
                    SELECT {select_cols_sql}, {horse_id_col} AS __horse_id
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ? AND race_no = ?
                    ORDER BY {order_sql}, id ASC;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchall()

                if not rows:
                    continue

                table.setRowCount(0)
                for r in rows:
                    row_idx = table.rowCount()
                    table.insertRow(row_idx)
                    row_horse_id = str(r["__horse_id"] or "").strip()
                    for c_idx, name in enumerate(display_cols):
                        if name == RACECARD_ODDS_DIFF_COLUMN:
                            val = odds_diff_display(
                                r[RACECARD_IMMEDIATE_ODDS_COLUMN],
                                r[RACECARD_PREDICTED_ODDS_COLUMN],
                            )
                        else:
                            val = r[name]
                        item = make_table_item("" if val is None else str(val))
                        if c_idx == 0:
                            # 將該列 horse_id 掛在第 1 欄，供雙擊事件轉跳主畫面往績。
                            item.setData(Qt.UserRole, row_horse_id)
                        table.setItem(row_idx, c_idx, item)

                resize_grid_columns_to_content(table)
                table.itemSelectionChanged.connect(
                    lambda t=table: self._on_starter_selection_changed(t)
                )
                table.itemDoubleClicked.connect(self._on_table_item_double_clicked)
                install_grid_keyboard_nav(table)

                tab_page = QWidget()
                tab_layout = QVBoxLayout(tab_page)
                tab_layout.setContentsMargins(4, 4, 4, 4)
                tab_layout.setSpacing(8)
                header = _build_race_info_header_widget(
                    conn, race_date, racecourse, race_no
                )
                header.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
                )
                tab_layout.addWidget(header, 0)
                tab_layout.addWidget(table, 1)
                self._tabs.addTab(
                    tab_page,
                    _race_tab_label(race_no, header.property("post_time")),
                )
            self._clear_form()
            # 預設選取第一場第一列，載入往績
            if self._tabs.count() > 0:
                first_page = self._tabs.widget(0)
                if first_page is not None:
                    first_table = first_page.findChild(QTableWidget)
                    if first_table is not None and first_table.rowCount() > 0:
                        first_table.selectRow(0)
                        self._on_starter_selection_changed(first_table)
            self._refresh_overnight_status()
        finally:
            conn.close()

    def _clear_form(self) -> None:
        self._form_horse_id = ""
        self._form_title.setText("往績（請選擇上方馬匹）")
        self._form_table.setRowCount(0)

    def _on_starter_selection_changed(self, table: QTableWidget) -> None:
        """選取排位列時，在下方顯示該馬往績。"""
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if not rows:
            return
        first = table.item(rows[0].row(), 0)
        if first is None:
            return
        horse_id = str(first.data(Qt.UserRole) or "").strip()
        if not horse_id:
            return
        horse_name = ""
        # 嘗試從「馬名」欄讀取顯示名稱
        headers = [
            table.horizontalHeaderItem(i).text() if table.horizontalHeaderItem(i) else ""
            for i in range(table.columnCount())
        ]
        if "馬名" in headers:
            name_item = table.item(rows[0].row(), headers.index("馬名"))
            if name_item is not None:
                horse_name = str(name_item.text() or "").strip()
        self._load_form_for_horse(horse_id, horse_name)

    def _on_table_item_double_clicked(self, item: QTableWidgetItem) -> None:
        """雙擊排位中的馬匹：切至主畫面馬匹／往績。"""
        if item is None:
            return
        table = item.tableWidget()
        if table is None:
            return
        first = table.item(item.row(), 0)
        if first is None:
            return
        horse_id = str(first.data(Qt.UserRole) or "").strip()
        if not horse_id:
            return
        self.horse_double_clicked.emit(horse_id)

    def _load_form_for_horse(self, horse_id: str, horse_name: str = "", limit: int | None = None) -> None:
        """下方往績 GRID：與「馬匹／往績」同一套欄位與全部往績。"""
        hid = str(horse_id or "").strip()
        if not hid:
            self._clear_form()
            return
        if hid == self._form_horse_id and self._form_table.rowCount() > 0:
            return
        self._form_horse_id = hid
        rows = fetch_race_records(self._config.db_path, hid)
        if limit is not None and limit > 0:
            rows = rows[:limit]
        name = str(horse_name or "").strip()
        if not name:
            hr = fetch_horse_by_id(self._config.db_path, hid)
            if hr is not None:
                name = str(hr["name_zh"] or hr["name_en"] or "").strip()
        title = f"{name}（{hid}）｜往績" if name else f"{hid}｜往績"
        self._form_title.setText(title)

        flag_status = get_horse_status(self._config.db_path, hid)
        numeric_cols = {2, 4, 7, 8, 11, 12, 15, 17}
        prev_keys = list(getattr(self._form_table, "_multi_sort_keys", []) or [])
        self._form_table.setRowCount(0)
        for r in rows:
            r_idx = self._form_table.rowCount()
            self._form_table.insertRow(r_idx)
            values = race_record_grid_values(r, flag_status)
            for c_idx, val in enumerate(values):
                self._form_table.setItem(
                    r_idx,
                    c_idx,
                    make_table_item(str(val), numeric=c_idx in numeric_cols),
                )
        resize_grid_columns_to_content(self._form_table)
        if prev_keys:
            apply_multi_column_sort(
                self._form_table,
                prev_keys,
                base_headers=list(RACE_RECORD_GRID_HEADERS),
            )
        if not rows:
            self._form_title.setText(f"{title}（暫無往績）")


class RacecardRankWindow(QMainWindow):
    """獨立視窗：僅包一層 `RacecardPanel`（與主程式內嵌同一套 UI）。"""

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setWindowTitle("Racing｜馬匹排位（Racecard）")
        self.resize(1400, 800)
        self.setCentralWidget(RacecardPanel(config, parent=self))


def main() -> int:
    config = default_config()
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    w = RacecardRankWindow(config)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())


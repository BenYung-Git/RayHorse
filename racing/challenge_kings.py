"""從 HKJC「騎師王／練馬師王」賽日積分頁下載並寫入資料庫。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import requests
from bs4 import BeautifulSoup

from .db import get_connection, init_db
from .download_sources import jockey_king_base_url, trainer_king_base_url
from .web_utils import normalize_date_yyyy_mm_dd

ChallengeType = Literal["jockey", "trainer"]

TYPE_JOCKEY: ChallengeType = "jockey"
TYPE_TRAINER: ChallengeType = "trainer"

_TYPE_LABEL = {
    TYPE_JOCKEY: "騎師王",
    TYPE_TRAINER: "練馬師王",
}


@dataclass(frozen=True, slots=True)
class ChallengeDownloadStats:
    challenge_type: ChallengeType
    race_date: str
    rows: int
    written: int
    dates: int = 1


def season_start_yyyy_mm_dd(race_date: str) -> str:
    """港馬賽季約自每年 9 月起；回傳該賽日所屬賽季起點 YYYY/MM/DD。"""
    text = _normalize_race_date(race_date)
    parts = text.split("/")
    if len(parts) != 3:
        return ""
    try:
        y, m = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if m >= 9:
        return f"{y:04d}/09/01"
    return f"{(y - 1):04d}/09/01"


def _points_expr(column: str = "day_points") -> str:
    return (
        f"CAST(REPLACE(REPLACE(TRIM(COALESCE({column}, '')), ',', ''), ' ', '') AS REAL)"
    )


def challenge_kings_schema_sql() -> str:
    return """
        CREATE TABLE IF NOT EXISTS challenge_standings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          challenge_type TEXT NOT NULL,
          race_date TEXT NOT NULL,
          entry_no TEXT NOT NULL DEFAULT '',
          person_name TEXT NOT NULL,
          day_points TEXT NOT NULL DEFAULT '',
          wins TEXT NOT NULL DEFAULT '',
          seconds TEXT NOT NULL DEFAULT '',
          thirds TEXT NOT NULL DEFAULT '',
          fourths TEXT NOT NULL DEFAULT '',
          remark TEXT NOT NULL DEFAULT '',
          is_other INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT '',
          UNIQUE(challenge_type, race_date, entry_no, person_name, is_other)
        );
        CREATE INDEX IF NOT EXISTS idx_challenge_standings_meeting
          ON challenge_standings(challenge_type, race_date);
    """


def _fetch_html(url: str, params: dict[str, str] | None = None) -> str:
    resp = requests.get(
        url,
        params=params,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RacingApp/1.0)"},
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _normalize_race_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    # select value 多為 2026/10/19；頁面文字多為 19/10/2026
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
        d, m, y = text.split("/")
        return f"{int(y):04d}/{int(m):02d}/{int(d):02d}"
    return normalize_date_yyyy_mm_dd(text) or text.replace("-", "/")


def _extract_all_race_dates_from_html(html: str) -> list[str]:
    """由頁面賽日下拉／文字列出可選賽日（新→舊，去重）。"""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        rd = _normalize_race_date(raw)
        if rd and rd not in seen:
            seen.add(rd)
            found.append(rd)

    for sel in soup.select("select"):
        for opt in sel.select("option"):
            val = str(opt.get("value") or "").strip()
            if re.search(r"\d{4}/\d{1,2}/\d{1,2}", val):
                _add(val)
                continue
            label = opt.get_text(strip=True)
            if re.search(r"\d{1,2}/\d{1,2}/\d{4}", label):
                _add(label)
    if not found:
        text = soup.get_text("\n", strip=True)
        for m in re.finditer(
            r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}/\d{1,2}/\d{1,2})",
            text,
        ):
            _add(m.group(1))
    found.sort(reverse=True)
    return found


def _extract_race_date_from_html(html: str) -> str:
    dates = _extract_all_race_dates_from_html(html)
    return dates[0] if dates else ""


def _parse_standings_rows(html: str) -> list[dict[str, str | int]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    out: list[dict[str, str | int]] = []
    in_other = False
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not any(cells):
            continue
        joined = " ".join(cells)
        if "編號" in joined and ("騎師" in joined or "練馬師" in joined):
            continue
        if "實際數目" in joined:
            continue
        # 分段標題：# 其他騎師／# 其他練馬師（整段略過，不入庫）
        if len(cells) <= 2 and re.search(r"#?\s*其他", joined):
            in_other = True
            continue
        if in_other:
            continue
        if len(cells) < 3:
            continue
        # 正規列：編號, 姓名, 積分, 冠, 亞, 季, 殿, 備註?
        entry_no = cells[0].strip()
        name = cells[1].strip() if len(cells) > 1 else ""
        if not name and len(cells) >= 3 and not entry_no:
            continue
        # 彙總列「# 其他騎師／# 其他練馬師」不寫入
        if re.search(r"#?\s*其他", name) or name.startswith("其他"):
            continue
        if not name:
            continue
        points = cells[2] if len(cells) > 2 else ""
        wins = cells[3] if len(cells) > 3 else ""
        seconds = cells[4] if len(cells) > 4 else ""
        thirds = cells[5] if len(cells) > 5 else ""
        fourths = cells[6] if len(cells) > 6 else ""
        remark = cells[7] if len(cells) > 7 else ""
        out.append(
            {
                "entry_no": entry_no,
                "person_name": name,
                "day_points": points,
                "wins": wins,
                "seconds": seconds,
                "thirds": thirds,
                "fourths": fourths,
                "remark": remark,
                "is_other": 0,
            }
        )
    return out


def _upsert_standings(
    db_path: Path,
    *,
    challenge_type: ChallengeType,
    race_date: str,
    rows: list[dict[str, str | int]],
) -> int:
    init_db(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    with get_connection(db_path) as conn:
        conn.executescript(challenge_kings_schema_sql())
        # 清掉舊版曾寫入的「其他騎師／其他練馬師」列
        conn.execute(
            """
            DELETE FROM challenge_standings
            WHERE is_other = 1
               OR TRIM(COALESCE(person_name, '')) LIKE '其他%'
            """
        )
        conn.execute(
            """
            DELETE FROM challenge_standings
            WHERE challenge_type = ? AND race_date = ?
            """,
            (challenge_type, race_date),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO challenge_standings(
                  challenge_type, race_date, entry_no, person_name,
                  day_points, wins, seconds, thirds, fourths, remark,
                  is_other, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    challenge_type,
                    race_date,
                    str(row.get("entry_no") or ""),
                    str(row.get("person_name") or ""),
                    str(row.get("day_points") or ""),
                    str(row.get("wins") or ""),
                    str(row.get("seconds") or ""),
                    str(row.get("thirds") or ""),
                    str(row.get("fourths") or ""),
                    str(row.get("remark") or ""),
                    int(row.get("is_other") or 0),
                    now,
                ),
            )
            written += 1
        conn.commit()
    return written


def download_challenge_standings(
    db_path: Path,
    challenge_type: ChallengeType,
    *,
    data_dir: Path | None = None,
    racedate: str | None = None,
    include_season: bool = True,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ChallengeDownloadStats:
    """下載指定類型積分榜。

    預設一併下載今季初至今各賽日（供「今季總分」加總）；
    ``include_season=False`` 時只下載單一賽日。
    """
    if challenge_type == TYPE_JOCKEY:
        base = jockey_king_base_url(data_dir)
    else:
        base = trainer_king_base_url(data_dir)
    label = _TYPE_LABEL.get(challenge_type, challenge_type)
    if progress:
        progress(f"正在下載{label}…")
    if should_cancel and should_cancel():
        return ChallengeDownloadStats(challenge_type, "", 0, 0, 0)

    params: dict[str, str] | None = None
    if racedate:
        params = {"racedate": _normalize_race_date(racedate)}
    html = _fetch_html(base, params)
    all_dates = _extract_all_race_dates_from_html(html)
    race_date = all_dates[0] if all_dates else ""
    if not race_date and racedate:
        race_date = _normalize_race_date(racedate)
    if not race_date:
        return ChallengeDownloadStats(challenge_type, "", 0, 0, 0)
    if should_cancel and should_cancel():
        return ChallengeDownloadStats(challenge_type, race_date, 0, 0, 0)

    season_start = season_start_yyyy_mm_dd(race_date)
    if include_season and all_dates and season_start:
        target_dates = sorted(
            [d for d in all_dates if season_start <= d <= race_date]
        )
    else:
        target_dates = [race_date]

    total_written = 0
    last_rows = 0
    for i, rd in enumerate(target_dates):
        if should_cancel and should_cancel():
            break
        if progress:
            progress(f"正在下載{label} {rd}（{i + 1}/{len(target_dates)}）…")
        if rd == race_date and (not racedate or _normalize_race_date(racedate) == rd):
            day_html = html
        else:
            day_html = _fetch_html(base, {"racedate": rd})
        rows = _parse_standings_rows(day_html)
        last_rows = len(rows)
        total_written += _upsert_standings(
            db_path,
            challenge_type=challenge_type,
            race_date=rd,
            rows=rows,
        )
    return ChallengeDownloadStats(
        challenge_type,
        race_date,
        last_rows,
        total_written,
        dates=len(target_dates),
    )


def download_jockey_king(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    racedate: str | None = None,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ChallengeDownloadStats:
    return download_challenge_standings(
        db_path,
        TYPE_JOCKEY,
        data_dir=data_dir,
        racedate=racedate,
        progress=progress,
        should_cancel=should_cancel,
    )


def download_trainer_king(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    racedate: str | None = None,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ChallengeDownloadStats:
    return download_challenge_standings(
        db_path,
        TYPE_TRAINER,
        data_dir=data_dir,
        racedate=racedate,
        progress=progress,
        should_cancel=should_cancel,
    )


def format_challenge_stats(stats: ChallengeDownloadStats) -> str:
    label = _TYPE_LABEL.get(stats.challenge_type, stats.challenge_type)
    if not stats.race_date:
        return f"{label}：找不到可下載的賽日"
    if stats.dates > 1:
        return (
            f"{label}完成：今季至 {stats.race_date}｜賽日 {stats.dates}"
            f"｜寫入 {stats.written}"
        )
    return f"{label}完成：{stats.race_date}｜列 {stats.rows}｜寫入 {stats.written}"


def list_stored_challenge_dates(
    db_path: Path,
    challenge_type: ChallengeType | None = None,
) -> list[str]:
    """回傳已入庫的賽日清單（新→舊）。未指定類型時合併騎師／練馬師。"""
    init_db(db_path)
    with get_connection(db_path) as conn:
        if challenge_type is None:
            rows = conn.execute(
                """
                SELECT DISTINCT race_date FROM challenge_standings
                ORDER BY race_date DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT race_date FROM challenge_standings
                WHERE challenge_type = ?
                ORDER BY race_date DESC
                """,
                (challenge_type,),
            ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def fetch_season_point_totals(
    db_path: Path,
    challenge_type: ChallengeType,
    through_date: str,
) -> dict[str, float]:
    """今季初至今各人賽日積分加總（依姓名）。"""
    through = _normalize_race_date(through_date)
    start = season_start_yyyy_mm_dd(through)
    if not through or not start:
        return {}
    init_db(db_path)
    expr = _points_expr("day_points")
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT person_name, SUM({expr}) AS total_points
            FROM challenge_standings
            WHERE challenge_type = ?
              AND race_date >= ?
              AND race_date <= ?
              AND COALESCE(is_other, 0) = 0
              AND TRIM(COALESCE(person_name, '')) <> ''
              AND person_name NOT LIKE '其他%'
            GROUP BY person_name
            """,
            (challenge_type, start, through),
        ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        name = str(r["person_name"] or "").strip()
        if not name:
            continue
        try:
            out[name] = float(r["total_points"] or 0)
        except (TypeError, ValueError):
            out[name] = 0.0
    return out


def _format_points_value(value: object) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.1f}".rstrip("0").rstrip(".")


def fetch_challenge_standings(
    db_path: Path,
    challenge_type: ChallengeType,
    race_date: str,
) -> list[dict[str, object]]:
    """讀取指定類型／賽日的積分榜列。"""
    init_db(db_path)
    race_date = _normalize_race_date(race_date)
    expr = _points_expr("day_points")
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT entry_no, person_name, day_points, wins, seconds, thirds,
                   fourths, remark, is_other, updated_at
            FROM challenge_standings
            WHERE challenge_type = ? AND race_date = ?
              AND COALESCE(is_other, 0) = 0
              AND person_name NOT LIKE '其他%'
            ORDER BY {expr} DESC,
                     CASE WHEN entry_no GLOB '[0-9]*' THEN CAST(entry_no AS INTEGER) ELSE 9999 END,
                     entry_no ASC,
                     person_name ASC
            """,
            (challenge_type, race_date),
        ).fetchall()
    out: list[dict[str, object]] = []
    for r in rows:
        out.append(
            {
                "entry_no": r["entry_no"] or "",
                "person_name": r["person_name"] or "",
                "day_points": r["day_points"] or "",
                "wins": r["wins"] or "",
                "seconds": r["seconds"] or "",
                "thirds": r["thirds"] or "",
                "fourths": r["fourths"] or "",
                "remark": r["remark"] or "",
                "is_other": 0,
                "updated_at": r["updated_at"] or "",
            }
        )
    return out


def fetch_season_standings(
    db_path: Path,
    challenge_type: ChallengeType,
    through_date: str,
) -> list[dict[str, object]]:
    """今季初至今總分榜（依總分由大至小；含冠／亞／季／殿累計）。"""
    through = _normalize_race_date(through_date)
    start = season_start_yyyy_mm_dd(through)
    if not through or not start:
        return []
    init_db(db_path)
    expr = _points_expr("day_points")
    wins_expr = _points_expr("wins")
    seconds_expr = _points_expr("seconds")
    thirds_expr = _points_expr("thirds")
    fourths_expr = _points_expr("fourths")
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT person_name,
                   SUM({expr}) AS total_points,
                   SUM({wins_expr}) AS total_wins,
                   SUM({seconds_expr}) AS total_seconds,
                   SUM({thirds_expr}) AS total_thirds,
                   SUM({fourths_expr}) AS total_fourths,
                   COUNT(DISTINCT race_date) AS meeting_count
            FROM challenge_standings
            WHERE challenge_type = ?
              AND race_date >= ?
              AND race_date <= ?
              AND COALESCE(is_other, 0) = 0
              AND TRIM(COALESCE(person_name, '')) <> ''
              AND person_name NOT LIKE '其他%'
            GROUP BY person_name
            ORDER BY total_points DESC, person_name ASC
            """,
            (challenge_type, start, through),
        ).fetchall()
    out: list[dict[str, object]] = []
    for i, r in enumerate(rows, start=1):
        out.append(
            {
                "rank": str(i),
                "person_name": r["person_name"] or "",
                "season_points": _format_points_value(r["total_points"] or 0),
                "wins": _format_points_value(r["total_wins"] or 0),
                "seconds": _format_points_value(r["total_seconds"] or 0),
                "thirds": _format_points_value(r["total_thirds"] or 0),
                "fourths": _format_points_value(r["total_fourths"] or 0),
                "meeting_count": str(int(r["meeting_count"] or 0)),
            }
        )
    return out


def challenge_type_label(challenge_type: ChallengeType) -> str:
    return _TYPE_LABEL.get(challenge_type, challenge_type)

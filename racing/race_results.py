"""從 HKJC「賽果／localresults」下載當日各場賽馬結果並寫入 race_records。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .db import get_connection, init_db, upsert_race_records
from .download_sources import results_base_url
from .models import RaceRecord
from .web_utils import format_two_digit_99

COURSE_CODE_TO_NAME = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}
COURSE_NAME_TO_CODE = {name: code for code, name in COURSE_CODE_TO_NAME.items()}

# 馬會「分段時間及位置」頁（段速）
SECTIONAL_TIME_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/displaysectionaltime"
)
_SECTION_TIME_RE = re.compile(r"\d+\.\d{2}")


def infer_venue_code(venue: str, course: str = "") -> str:
    """從 venue／course 推斷 ST/HV/FC。"""
    v = (venue or "").strip().upper()
    if v in COURSE_CODE_TO_NAME:
        return v
    text = f"{venue or ''} {course or ''}"
    for name, code in COURSE_NAME_TO_CODE.items():
        if name in text:
            return code
    for code in COURSE_CODE_TO_NAME:
        if code in text.upper():
            return code
    return ""


def list_stored_results_meetings(db_path: Path) -> list[tuple[str, str]]:
    """已入庫賽果的 (race_date yyyy/MM/dd, venue code)，新至舊。"""
    init_db(db_path)
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT race_date, venue, course
            FROM race_records
            WHERE race_date GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]'
            """
        ).fetchall()
    for row in rows:
        rd = _normalize_meeting_date(str(row["race_date"] or ""))
        code = infer_venue_code(str(row["venue"] or ""), str(row["course"] or ""))
        if not rd or not code:
            continue
        key = (rd, code)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return out


def _race_no_sort_key(raw: object) -> int:
    s = str(raw or "").strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    return 10**9


def _season_race_to_meeting_map(race_nos: list[str]) -> dict[str, str]:
    """馬季場次（099、100…）依當日順序對應成 01、02、03…。"""
    season = sorted(
        {n for n in race_nos if re.fullmatch(r"\d{3}", n)},
        key=_race_no_sort_key,
    )
    return {n: f"{i:02d}" for i, n in enumerate(season, start=1)}


def _display_meeting_race_no(
    raw_race_no: object,
    racecard_race_no: object,
    season_map: dict[str, str],
) -> str:
    """賽馬結果一律顯示當日場次（01–14），不用馬季場次。"""
    if racecard_race_no is not None and str(racecard_race_no).strip() != "":
        return format_two_digit_99(racecard_race_no)
    raw = str(raw_race_no or "").strip()
    if raw in season_map:
        return season_map[raw]
    if re.fullmatch(r"\d{1,2}", raw) and 1 <= int(raw) <= 14:
        return f"{int(raw):02d}"
    return format_two_digit_99(raw)


def _format_form_rating(raw: object) -> str:
    """與馬匹往績相同：純數字評分去掉小數位／前導零。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+(\.0+)?", text):
        return str(int(float(text)))
    return text


def _lookup_form_rating(
    conn,
    *,
    horse_id: str,
    race_date: str,
    venue_code: str = "",
) -> str:
    """從 race_records（馬匹往績）取該馬該賽日評分；日期格式差異可對上。"""
    from .web_utils import normalize_date_yyyy_mm_dd

    hid = (horse_id or "").strip()
    want = normalize_date_yyyy_mm_dd(race_date)
    if not hid or not want:
        return ""
    code = (venue_code or "").strip().upper()
    rows = conn.execute(
        """
        SELECT race_date, venue, course, rating
        FROM race_records
        WHERE horse_id = ?
          AND rating IS NOT NULL
          AND TRIM(rating) != '';
        """,
        (hid,),
    ).fetchall()
    for row in rows:
        if normalize_date_yyyy_mm_dd(row["race_date"]) != want:
            continue
        if code:
            inferred = infer_venue_code(str(row["venue"] or ""), str(row["course"] or ""))
            if inferred and inferred != code:
                continue
        return _format_form_rating(row["rating"])
    return ""


def fetch_results_rows(
    db_path: Path, race_date: str, racecourse_code: str
) -> list[dict[str, str]]:
    """讀取指定賽日／場地的賽果列（含馬名／馬號）；場次以當日 01、02… 顯示，場次由大至小。

    「評分」一律取馬匹往績 race_records.rating，不用排位表評分。
    """
    init_db(db_path)
    rd = _normalize_meeting_date(race_date)
    code = (racecourse_code or "").strip().upper()
    rd_dash = rd.replace("/", "-") if rd else ""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              rr.race_no, rr.finish_pos, rr.draw, rr.time, rr.win_odds, rr.distance,
              rr.jockey, rr.trainer, rr.horse_id, rr.venue, rr.course,
              COALESCE(rr.class_band, '') AS class_band,
              COALESCE(rr.rating, '') AS rating,
              COALESCE(rr.handicap, '') AS handicap,
              COALESCE(rr.running_pos, '') AS running_pos,
              COALESCE(rr.place_odds, '') AS place_odds,
              COALESCE(rr.sectional_time, '') AS sectional_time,
              COALESCE(h.name_zh, '') AS name_zh,
              COALESCE(h.brand_no, '') AS brand_no,
              (
                SELECT rs.race_no
                FROM racecard_starters AS rs
                WHERE rs.horse_id = rr.horse_id
                  AND (rs.race_date = rr.race_date OR rs.race_date = ?)
                  AND UPPER(TRIM(rs.racecourse)) = ?
                ORDER BY rs.race_no
                LIMIT 1
              ) AS meeting_race_no,
              (
                SELECT COALESCE(rs."馬號", '')
                FROM racecard_starters AS rs
                WHERE rs.horse_id = rr.horse_id
                  AND (rs.race_date = rr.race_date OR rs.race_date = ?)
                  AND UPPER(TRIM(rs.racecourse)) = ?
                ORDER BY rs.race_no
                LIMIT 1
              ) AS horse_no
            FROM race_records AS rr
            LEFT JOIN horses AS h ON h.horse_id = rr.horse_id
            WHERE rr.race_date = ?
            ORDER BY rr.id
            """,
            (rd_dash, code, rd_dash, code, rd),
        ).fetchall()
        filtered = []
        for r in rows:
            inferred = infer_venue_code(str(r["venue"] or ""), str(r["course"] or ""))
            if inferred != code:
                continue
            filtered.append(r)
        season_map = _season_race_to_meeting_map(
            [str(r["race_no"] or "").strip() for r in filtered]
        )
        out: list[dict[str, str]] = []
        for r in filtered:
            race_disp = _display_meeting_race_no(
                r["race_no"], r["meeting_race_no"], season_map
            )
            horse_no = str(r["horse_no"] or "").strip()
            class_band = str(r["class_band"] or "").strip()
            rating = _format_form_rating(r["rating"])
            if not rating:
                rating = _lookup_form_rating(
                    conn,
                    horse_id=str(r["horse_id"] or ""),
                    race_date=rd,
                    venue_code=code,
                )
            out.append(
                {
                    "race_no": race_disp,
                    "finish_pos": format_two_digit_99(r["finish_pos"]),
                    "horse_no": format_two_digit_99(horse_no) if horse_no else "",
                    "name_zh": str(r["name_zh"] or ""),
                    "brand_no": str(r["brand_no"] or ""),
                    "jockey": str(r["jockey"] or ""),
                    "trainer": str(r["trainer"] or ""),
                    "class_band": class_band,
                    "rating": rating,
                    "handicap": str(r["handicap"] or "").strip(),
                    "draw": format_two_digit_99(r["draw"]),
                    "running_pos": str(r["running_pos"] or "").strip(),
                    "time": str(r["time"] or ""),
                    "win_odds": str(r["win_odds"] or ""),
                    "place_odds": str(r["place_odds"] or ""),
                    "sectional_time": str(r["sectional_time"] or "").strip(),
                    "distance": str(r["distance"] or ""),
                    "horse_id": str(r["horse_id"] or ""),
                }
            )
    # 場次由大至小；同場內按名次由小至大
    out.sort(
        key=lambda row: (
            -_race_no_sort_key(row["race_no"]),
            _race_no_sort_key(row["finish_pos"]),
            _race_no_sort_key(row["horse_no"]),
            row["horse_id"],
        )
    )
    # 舊賽果未存段速時，自動補抓一次並寫回
    if out and not any(str(r.get("sectional_time") or "").strip() for r in out):
        try:
            _enrich_sectional_times_for_meeting(db_path, rd, code, out)
        except Exception:
            pass
    return out


def _enrich_sectional_times_for_meeting(
    db_path: Path,
    race_date: str,
    racecourse_code: str,
    rows: list[dict[str, str]],
) -> None:
    """為當日賽果補段速（記憶體列＋資料庫）。"""
    by_race: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rn = format_two_digit_99(row.get("race_no"))
        if not rn:
            continue
        by_race.setdefault(rn, []).append(row)
    updates: list[tuple[str, str, str, str]] = []
    for rn, race_rows in by_race.items():
        sec_map = fetch_sectional_times_for_race(race_date, racecourse_code, rn)
        if not sec_map:
            continue
        for row in race_rows:
            hno = str(row.get("horse_no") or "").strip()
            if not re.fullmatch(r"\d{1,2}", hno):
                continue
            sec = sec_map.get(str(int(hno)), "")
            if not sec:
                continue
            row["sectional_time"] = sec
            hid = str(row.get("horse_id") or "").strip()
            if hid:
                updates.append((sec, hid, race_date, racecourse_code))
    if not updates:
        return
    with get_connection(db_path) as conn:
        for sec, hid, rd, venue in updates:
            conn.execute(
                """
                UPDATE race_records
                SET sectional_time = ?
                WHERE horse_id = ?
                  AND race_date = ?
                  AND UPPER(TRIM(venue)) = ?
                  AND (sectional_time IS NULL OR TRIM(sectional_time) = '');
                """,
                (sec, hid, rd, venue.upper()),
            )


def list_results_race_nos(rows: list[dict[str, str]]) -> list[str]:
    """由賽果列抽出當日場次（01…），由小至大。"""
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        raw = str(row.get("race_no") or "").strip()
        if not raw or not re.fullmatch(r"\d+", raw):
            continue
        rn = format_two_digit_99(raw)
        if not rn or rn in seen:
            continue
        seen.add(rn)
        out.append(rn)
    out.sort(key=_race_no_sort_key)
    return out


def _finish_place_int(raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 99 else None


# 負磅分組（磅）
_HANDICAP_GROUP_DEFS: list[tuple[str, Callable[[float], bool]]] = [
    ("<110磅", lambda x: x < 110),
    (">=110及<120磅", lambda x: 110 <= x < 120),
    (">=120及<130磅", lambda x: 120 <= x < 130),
    (">=130磅", lambda x: x >= 130),
]

# 賠率分組（獨贏）。需求文案寫「4組」但列出五個區間（第2組重複編號），依區間實作。
_ODDS_GROUP_DEFS: list[tuple[str, Callable[[float], bool]]] = [
    ("<=3", lambda x: x <= 3),
    (">3及<=10", lambda x: 3 < x <= 10),
    (">10及<=20", lambda x: 10 < x <= 20),
    (">20及<=40", lambda x: 20 < x <= 40),
    (">40", lambda x: x > 40),
]


def _parse_positive_number(raw: object) -> float | None:
    """從負磅／賠率字串取出第一個正數；無法解析則 None。"""
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if val < 0:
        return None
    return val


def _handicap_group_label(raw: object) -> str | None:
    lbs = _parse_positive_number(raw)
    if lbs is None:
        return None
    for label, pred in _HANDICAP_GROUP_DEFS:
        if pred(lbs):
            return label
    return None


def _odds_group_label(raw: object) -> str | None:
    odds = _parse_positive_number(raw)
    if odds is None:
        return None
    for label, pred in _ODDS_GROUP_DEFS:
        if pred(odds):
            return label
    return None


def summarize_top3_stats(
    rows: list[dict[str, str]],
) -> dict[str, list[dict[str, str | int]]]:
    """
    當天跑入 1–3 名的統計：練馬師／騎師／檔位／負磅組／賠率組。
    回傳各表列含 name、wins、seconds、thirds；全部按第1名場次（wins）由大至小。
    """

    def _bucket(key_field: str) -> dict[str, dict[str, int]]:
        acc: dict[str, dict[str, int]] = {}
        for row in rows:
            place = _finish_place_int(row.get("finish_pos"))
            if place is None or place > 3:
                continue
            name = str(row.get(key_field) or "").strip()
            if not name:
                continue
            item = acc.setdefault(name, {"wins": 0, "seconds": 0, "thirds": 0})
            if place == 1:
                item["wins"] += 1
            elif place == 2:
                item["seconds"] += 1
            else:
                item["thirds"] += 1
        return acc

    def _bucket_by_label(
        label_fn, *, preset_labels: list[str]
    ) -> dict[str, dict[str, int]]:
        acc: dict[str, dict[str, int]] = {
            label: {"wins": 0, "seconds": 0, "thirds": 0} for label in preset_labels
        }
        for row in rows:
            place = _finish_place_int(row.get("finish_pos"))
            if place is None or place > 3:
                continue
            name = label_fn(row)
            if not name:
                continue
            item = acc.setdefault(name, {"wins": 0, "seconds": 0, "thirds": 0})
            if place == 1:
                item["wins"] += 1
            elif place == 2:
                item["seconds"] += 1
            else:
                item["thirds"] += 1
        return acc

    def _sorted_rows(
        acc: dict[str, dict[str, int]],
        *,
        numeric_name: bool = False,
        fixed_order: list[str] | None = None,
    ) -> list[dict[str, str | int]]:
        items = list(acc.items())
        order_index = (
            {name: i for i, name in enumerate(fixed_order)} if fixed_order else {}
        )
        items.sort(
            key=lambda kv: (
                -kv[1]["wins"],
                -kv[1]["seconds"],
                -kv[1]["thirds"],
                order_index.get(kv[0], 999) if fixed_order else 0,
                _race_no_sort_key(kv[0]) if numeric_name else 0,
                kv[0],
            )
        )
        out: list[dict[str, str | int]] = []
        for name, c in items:
            out.append(
                {
                    "name": name,
                    "wins": c["wins"],
                    "seconds": c["seconds"],
                    "thirds": c["thirds"],
                }
            )
        return out

    handicap_labels = [label for label, _ in _HANDICAP_GROUP_DEFS]
    odds_labels = [label for label, _ in _ODDS_GROUP_DEFS]

    return {
        "trainers": _sorted_rows(_bucket("trainer")),
        "jockeys": _sorted_rows(_bucket("jockey")),
        "draws": _sorted_rows(_bucket("draw"), numeric_name=True),
        "handicaps": _sorted_rows(
            _bucket_by_label(
                lambda r: _handicap_group_label(r.get("handicap")),
                preset_labels=handicap_labels,
            ),
            fixed_order=handicap_labels,
        ),
        "odds": _sorted_rows(
            _bucket_by_label(
                lambda r: _odds_group_label(r.get("win_odds")),
                preset_labels=odds_labels,
            ),
            fixed_order=odds_labels,
        ),
    }


@dataclass(frozen=True, slots=True)
class ResultsMeeting:
    racedate: str  # YYYY/MM/DD
    racecourse_code: str


@dataclass(frozen=True, slots=True)
class ResultsDownloadStats:
    meeting: ResultsMeeting | None
    races: int
    rows: int
    inserted: int
    skipped_no_horse: int


def _normalize_meeting_date(raw: str) -> str:
    """轉成 yyyy/MM/dd；無法辨識則原樣。"""
    from .web_utils import normalize_date_yyyy_mm_dd

    return normalize_date_yyyy_mm_dd(raw)


def _normalize_finish_time(raw: str) -> str:
    """1:11.00 / 1.11.00 → 1.11.00（對齊往績格式）。"""
    t = (raw or "").strip()
    if not t or t == "---":
        return ""
    t = t.replace("：", ":")
    if re.fullmatch(r"\d+:\d{2}\.\d+", t):
        return t.replace(":", ".", 1)
    return t


def _parse_horse_id_from_href(href: str) -> str:
    if not href:
        return ""
    qs = parse_qs(urlparse(href).query)
    for key in ("horseid", "HorseId", "HorseID"):
        vals = qs.get(key) or []
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    m = re.search(r"(?:horseid|HorseId)=([^&]+)", href, re.I)
    return m.group(1).strip() if m else ""


def _brand_from_name(name_cell: str) -> tuple[str, str]:
    """『輕功猛男 (J513)』→ (輕功猛男, J513)。"""
    text = (name_cell or "").strip()
    m = re.search(r"^(.*?)\s*[\(（]\s*([A-Za-z]\d{2,4})\s*[\)）]\s*$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip().upper()
    return text, ""


def _resolve_horse_id(db_path: Path, *, horse_id: str, brand_no: str, name_zh: str) -> str:
    if horse_id:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT horse_id FROM horses WHERE horse_id = ? LIMIT 1;",
                (horse_id,),
            ).fetchone()
            if row:
                return str(row["horse_id"])
        return horse_id
    with get_connection(db_path) as conn:
        if brand_no:
            row = conn.execute(
                """
                SELECT horse_id FROM horses
                WHERE brand_no = ? OR horse_id LIKE ?
                ORDER BY CASE WHEN brand_no = ? THEN 0 ELSE 1 END
                LIMIT 1;
                """,
                (brand_no, f"%_{brand_no}", brand_no),
            ).fetchone()
            if row:
                return str(row["horse_id"])
        if name_zh:
            row = conn.execute(
                """
                SELECT horse_id FROM horses
                WHERE name_zh = ? OR name_zh LIKE ?
                LIMIT 1;
                """,
                (name_zh, f"%{name_zh}%"),
            ).fetchone()
            if row:
                return str(row["horse_id"])
    return ""


def _extract_meeting_from_html(html: str, fallback_date: str, fallback_course: str) -> ResultsMeeting:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    date_raw = fallback_date
    m = re.search(r"賽事日期\s*[:：]?\s*(\d{4}/\d{1,2}/\d{1,2})", text)
    if m:
        date_raw = m.group(1)
    else:
        m = re.search(r"賽事日期\s*[:：]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", text)
        if m:
            date_raw = m.group(1)
        else:
            m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
            if m:
                date_raw = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    course = fallback_course
    for name, code in COURSE_NAME_TO_CODE.items():
        if name in text:
            course = code
            break
    if not course:
        m2 = re.search(r"Racecourse=([A-Z]{2})", html, re.I)
        if m2:
            course = m2.group(1).upper()
    return ResultsMeeting(_normalize_meeting_date(date_raw), course or "ST")


def _extract_race_nos(html: str, base_race_no: int) -> list[int]:
    nos = {base_race_no} if base_race_no > 0 else set()
    for m in re.finditer(r"RaceNo=(\d+)", html, re.I):
        nos.add(int(m.group(1)))
    for m in re.finditer(r"第\s*(\d+)\s*場", html):
        n = int(m.group(1))
        if 1 <= n <= 14:
            nos.add(n)
    return sorted(nos)


def _parse_race_meta(html: str, race_no: int) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    meta = {
        "distance": "",
        "going": "",
        "course": "",
        "class_band": "",
        "prize": "",
    }
    for table in soup.find_all("table"):
        txt = table.get_text(" | ", strip=True)
        if f"第 {race_no} 場" not in txt and f"第{race_no}場" not in txt:
            continue
        dm = re.search(r"(\d{3,4})\s*米", txt)
        if dm:
            meta["distance"] = dm.group(1)
        gm = re.search(r"場地狀況\s*[:：]\s*([^\s|]+)", txt)
        if gm:
            meta["going"] = gm.group(1)
        cm = re.search(r"賽道\s*[:：]\s*([^|]+)", txt)
        if cm:
            meta["course"] = cm.group(1).strip()
        cl = re.search(r"(第[一二三四五]班|[A-E]\s*班)", txt)
        if cl:
            meta["class_band"] = cl.group(1).strip()
        pm = re.search(r"HK\$\s*([\d,]+)", txt)
        if pm:
            meta["prize"] = pm.group(1).replace(",", "")
        break
    return meta


def _dividend_to_odds(div_text: str) -> str:
    """派彩金額（HK$）→ 與獨贏賠率相同刻度（÷10）。"""
    s = (div_text or "").replace(",", "").replace("$", "").replace("HK", "").strip()
    if not s:
        return ""
    try:
        val = float(s) / 10.0
    except ValueError:
        return s
    text = f"{val:.2f}".rstrip("0").rstrip(".")
    return text


def _parse_place_dividends(html: str) -> dict[str, str]:
    """
    從賽果「派彩」表解析位置派彩。
    回傳：馬號（去前導零字串）→ 位置賠率（派彩÷10）。
    注意：位置列常有 rowspan，後續列只有「馬號／派彩」兩格。
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        texts0 = [
            c.get_text(" ", strip=True).replace(" ", "")
            for c in rows[0].find_all(["th", "td"])
        ]
        blob0 = "".join(texts0)
        # 標題列可能是「派彩」或直接「彩池／勝出組合／派彩」
        if "派彩" not in blob0 and "彩池" not in blob0:
            continue
        current_pool = ""
        found_pool_header = False
        for tr in rows:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if cells[0].strip() == "彩池" or (
                len(cells) >= 2 and "勝出組合" in "".join(cells)
            ):
                found_pool_header = True
                continue
            if not found_pool_header and "派彩" in "".join(cells) and len(cells) <= 2:
                # 外層標題「派彩」
                continue
            combo = ""
            dividend = ""
            pool_cell = cells[0].strip()
            # rowspan 延續列：只有兩格（組合、派彩）
            if len(cells) == 2 and current_pool == "位置":
                combo, dividend = cells[0].strip(), cells[1].strip()
            elif len(cells) >= 3:
                if pool_cell:
                    current_pool = pool_cell
                combo = cells[1].strip()
                dividend = cells[2].strip()
            else:
                continue
            if current_pool != "位置":
                continue
            # 勝出組合為單一馬號（8 / 10 / 12）
            if not re.fullmatch(r"\d{1,2}", combo):
                continue
            odds = _dividend_to_odds(dividend)
            if odds:
                out[str(int(combo))] = odds
        if out:
            break
    return out


def _parse_result_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    place_by_no = _parse_place_dividends(html)
    out: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(" ", strip=True).replace(" ", "") for c in rows[0].find_all(["th", "td"])]
        if not headers or "名次" not in headers[0]:
            continue
        # 標準欄：名次 馬號 馬名 騎師 練馬師 實際負磅 排位體重 檔位 頭馬距離 沿途走位 完成時間 獨贏賠率
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            finish = texts[0].strip()
            if not finish or not re.match(r"^\d+", finish):
                continue
            horse_name_cell = texts[2] if len(texts) > 2 else ""
            name_zh, brand = _brand_from_name(horse_name_cell)
            href = ""
            for a in cells[2].find_all("a", href=True) if len(cells) > 2 else []:
                href = a.get("href") or ""
                if "horse" in href.lower():
                    break
            horse_no_raw = texts[1].strip() if len(texts) > 1 else ""
            horse_no_key = ""
            if re.fullmatch(r"\d{1,2}", horse_no_raw):
                horse_no_key = str(int(horse_no_raw))
            out.append(
                {
                    "finish_pos": format_two_digit_99(finish),
                    "horse_no": horse_no_raw,
                    "name_zh": name_zh,
                    "brand_no": brand,
                    "horse_id_hint": _parse_horse_id_from_href(href),
                    "jockey": texts[3] if len(texts) > 3 else "",
                    "trainer": texts[4] if len(texts) > 4 else "",
                    "handicap": texts[5] if len(texts) > 5 else "",
                    "draw": format_two_digit_99(texts[7] if len(texts) > 7 else ""),
                    "running_pos": texts[9] if len(texts) > 9 else "",
                    "time": _normalize_finish_time(texts[10] if len(texts) > 10 else ""),
                    "win_odds": texts[11] if len(texts) > 11 else "",
                    "place_odds": place_by_no.get(horse_no_key, ""),
                }
            )
        if out:
            break
    return out


def _meeting_date_dd_mm_yyyy(race_date: str) -> str:
    """yyyy/MM/dd → dd/MM/yyyy（displaysectionaltime 參數格式）。"""
    from .web_utils import normalize_date_yyyy_mm_dd

    nd = normalize_date_yyyy_mm_dd(race_date)
    m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})", nd)
    if not m:
        return ""
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"


def _section_cell_primary_time(cell: str) -> str:
    """分段格取主段時間（第一個 xx.xx；後面為 200 米細分）。"""
    times = _SECTION_TIME_RE.findall(str(cell or ""))
    return times[0] if times else ""


def _parse_sectional_times_by_horse_no(html: str) -> dict[str, str]:
    """
    解析 displaysectionaltime：馬號 → 段速字串（各段主時間以「 / 」連接）。
    例：23.57 / 22.15 / 22.69
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue
        header_blob = " ".join(
            c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])
        )
        if "馬號" not in header_blob or "完成時間" not in header_blob:
            continue
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 5:
                continue
            horse_no = str(cells[1] or "").strip()
            if not re.fullmatch(r"\d{1,2}", horse_no):
                continue
            # 欄：次序、馬號、馬名、第1–6段、完成時間
            section_cells = cells[3:-1] if len(cells) >= 10 else cells[3:]
            parts = [
                _section_cell_primary_time(c)
                for c in section_cells
                if str(c or "").strip()
            ]
            parts = [p for p in parts if p]
            if not parts:
                continue
            out[str(int(horse_no))] = " / ".join(parts)
        if out:
            break
    return out


def fetch_sectional_times_for_race(
    race_date: str,
    racecourse_code: str,
    race_no: int | str,
) -> dict[str, str]:
    """抓取一場分段時間；回傳馬號（去前導零）→ 段速。"""
    dd = _meeting_date_dd_mm_yyyy(race_date)
    code = (racecourse_code or "").strip().upper()
    try:
        rn = int(str(race_no).strip())
    except ValueError:
        return {}
    if not dd or not code or rn <= 0:
        return {}
    try:
        html = _fetch_html(
            SECTIONAL_TIME_URL,
            {"racedate": dd, "Racecourse": code, "RaceNo": rn},
        )
    except Exception:
        return {}
    return _parse_sectional_times_by_horse_no(html)


def _fetch_html(url: str, params: dict[str, str | int] | None = None) -> str:
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def discover_latest_results_meeting(
    *,
    base_url: str | None = None,
    data_dir: Path | None = None,
) -> ResultsMeeting | None:
    """開啟賽果首頁，解析目前顯示的賽日／場地。"""
    url = (base_url or results_base_url(data_dir)).rstrip("?&")
    try:
        html = _fetch_html(url)
    except Exception:
        return None
    meeting = _extract_meeting_from_html(html, "", "")
    if not meeting.racedate:
        return None
    return meeting


def meeting_from_db(db_path: Path) -> ResultsMeeting | None:
    """優先：資料庫裡最近一個不大於今天的排位賽日。"""
    init_db(db_path)
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection(db_path) as conn:
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT race_date, racecourse
                FROM racecard_starters
                ORDER BY race_date DESC;
                """
            ).fetchall()
        except Exception:
            return None
    best: ResultsMeeting | None = None
    best_key = ""
    for row in rows:
        raw = str(row["race_date"] or "")
        course = str(row["racecourse"] or "").strip().upper()
        nd = _normalize_meeting_date(raw)
        nk = nd.replace("/", "-") if re.fullmatch(r"\d{4}/\d{2}/\d{2}", nd) else ""
        if not nk or nk > today:
            continue
        if nk >= best_key and course in COURSE_CODE_TO_NAME:
            best_key = nk
            best = ResultsMeeting(nd, course)
    return best


def download_race_results(
    db_path: Path,
    *,
    data_dir: Path | None = None,
    racedate: str | None = None,
    racecourse: str | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ResultsDownloadStats:
    """
    下載指定（或最近）賽日全部場次賽果，寫入 race_records。
    progress(current, total, message)
    """
    init_db(db_path)
    base = results_base_url(data_dir)
    meeting: ResultsMeeting | None = None
    if racedate and racecourse:
        meeting = ResultsMeeting(_normalize_meeting_date(racedate), racecourse.upper())
    if meeting is None:
        meeting = meeting_from_db(db_path)
    if meeting is None:
        meeting = discover_latest_results_meeting(base_url=base, data_dir=data_dir)
    if meeting is None:
        return ResultsDownloadStats(None, 0, 0, 0, 0)

    # 先抓第 1 場取得場次清單
    first_html = _fetch_html(
        base,
        {
            "racedate": meeting.racedate,
            "Racecourse": meeting.racecourse_code,
            "RaceNo": 1,
        },
    )
    meeting = _extract_meeting_from_html(first_html, meeting.racedate, meeting.racecourse_code)
    race_nos = _extract_race_nos(first_html, 1)
    if not race_nos:
        race_nos = list(range(1, 12))

    all_records: list[RaceRecord] = []
    skipped = 0
    total_steps = len(race_nos)
    # 預載該賽日往績評分，供賽果寫入時帶入（賽果頁本身無評分欄）
    form_rating_by_horse: dict[str, str] = {}
    try:
        with get_connection(db_path) as conn:
            from .web_utils import normalize_date_yyyy_mm_dd

            want = normalize_date_yyyy_mm_dd(meeting.racedate)
            for fr in conn.execute(
                """
                SELECT horse_id, race_date, venue, course, rating
                FROM race_records
                WHERE rating IS NOT NULL AND TRIM(rating) != '';
                """
            ):
                if normalize_date_yyyy_mm_dd(fr["race_date"]) != want:
                    continue
                inferred = infer_venue_code(
                    str(fr["venue"] or ""), str(fr["course"] or "")
                )
                if inferred and inferred != meeting.racecourse_code:
                    continue
                hid0 = str(fr["horse_id"] or "").strip()
                if hid0 and hid0 not in form_rating_by_horse:
                    form_rating_by_horse[hid0] = _format_form_rating(fr["rating"])
    except Exception:
        form_rating_by_horse = {}
    for idx, race_no in enumerate(race_nos, start=1):
        if should_cancel and should_cancel():
            break
        if progress:
            progress(idx - 1, total_steps, f"下載賽果：第 {race_no} 場")
        try:
            html = (
                first_html
                if race_no == 1
                else _fetch_html(
                    base,
                    {
                        "racedate": meeting.racedate,
                        "Racecourse": meeting.racecourse_code,
                        "RaceNo": race_no,
                    },
                )
            )
        except Exception:
            continue
        # 若頁面不是該場，略過
        if f"第 {race_no} 場" not in html and f"第{race_no}場" not in html:
            # 有時空白頁
            if "沒有相關資料" in html or "no information" in html.lower():
                continue
        meta = _parse_race_meta(html, race_no)
        rows = _parse_result_rows(html)
        sectional_by_no = fetch_sectional_times_for_race(
            meeting.racedate, meeting.racecourse_code, race_no
        )
        for row in rows:
            hid = _resolve_horse_id(
                db_path,
                horse_id=row["horse_id_hint"],
                brand_no=row["brand_no"],
                name_zh=row["name_zh"],
            )
            if not hid:
                skipped += 1
                continue
            # 勝出馬匹獎金：僅第一名寫入總獎金（其餘留空）
            prize = meta["prize"] if str(row["finish_pos"]).lstrip("0") == "1" or row["finish_pos"] == "1" else ""
            hno_key = ""
            hno_raw = str(row.get("horse_no") or "").strip()
            if re.fullmatch(r"\d{1,2}", hno_raw):
                hno_key = str(int(hno_raw))
            all_records.append(
                RaceRecord(
                    horse_id=hid,
                    race_date=meeting.racedate,
                    venue=meeting.racecourse_code,
                    race_no=str(race_no),
                    course=meta["course"],
                    distance=meta["distance"],
                    going=meta["going"],
                    class_band=meta["class_band"],
                    draw=row["draw"],
                    rating=form_rating_by_horse.get(hid, ""),
                    jockey=row["jockey"],
                    trainer=row["trainer"],
                    finish_pos=row["finish_pos"],
                    running_pos=row["running_pos"],
                    win_odds=row["win_odds"],
                    time=row["time"],
                    prize=prize,
                    handicap=row.get("handicap", ""),
                    place_odds=row.get("place_odds", ""),
                    sectional_time=sectional_by_no.get(hno_key, ""),
                )
            )
        if progress:
            progress(idx, total_steps, f"已解析第 {race_no} 場（{len(rows)} 匹）")

    inserted = upsert_race_records(db_path, all_records) if all_records else 0
    if progress:
        progress(total_steps, total_steps, "賽果寫入完成")
    return ResultsDownloadStats(
        meeting=meeting,
        races=len(race_nos),
        rows=len(all_records),
        inserted=inserted,
        skipped_no_horse=skipped,
    )

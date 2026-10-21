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


def fetch_results_rows(
    db_path: Path, race_date: str, racecourse_code: str
) -> list[dict[str, str]]:
    """讀取指定賽日／場地的賽果列（含馬名／馬號）；場次以當日 01、02… 顯示，場次由大至小。"""
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
              ) AS horse_no,
              (
                SELECT COALESCE(rs."評分", '')
                FROM racecard_starters AS rs
                WHERE rs.horse_id = rr.horse_id
                  AND (rs.race_date = rr.race_date OR rs.race_date = ?)
                  AND UPPER(TRIM(rs.racecourse)) = ?
                ORDER BY rs.race_no
                LIMIT 1
              ) AS card_rating
            FROM race_records AS rr
            LEFT JOIN horses AS h ON h.horse_id = rr.horse_id
            WHERE rr.race_date = ?
            ORDER BY rr.id
            """,
            (rd_dash, code, rd_dash, code, rd_dash, code, rd),
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
        rating = str(r["rating"] or "").strip() or str(r["card_rating"] or "").strip()
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
    return out


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
                    rating="",
                    jockey=row["jockey"],
                    trainer=row["trainer"],
                    finish_pos=row["finish_pos"],
                    running_pos=row["running_pos"],
                    win_odds=row["win_odds"],
                    time=row["time"],
                    prize=prize,
                    handicap=row.get("handicap", ""),
                    place_odds=row.get("place_odds", ""),
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

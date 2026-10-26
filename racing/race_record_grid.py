"""馬匹往績 GRID 欄位與顯示列（馬匹／往績與排位下方往績共用）。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .models import RaceRecord
from .web_utils import format_race_date_display, format_two_digit_99

# 欄位順序與 Windows「馬匹／往績」往績 Grid、WEB racesGrid 完全一致
RACE_RECORD_GRID_HEADERS: list[str] = [
    "日期",
    "場地",
    "場次",
    "跑道/途程",
    "途程",
    "場地狀況",
    "班/級別",
    "檔",
    "評分",
    "騎師",
    "練馬師",
    "負磅",
    "名次",
    "沿途走位",
    "段速",
    "獨贏賠率",
    "時間",
    "獎金",
    "Flag",
]


def _format_integer(value: object) -> str:
    if value is None or str(value).strip() == "":
        return ""
    text = str(value).strip().replace(",", "")
    try:
        num = Decimal(text)
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{int(num):,}"


def _format_money(value: object) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        num = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{num:,.2f}"


def race_record_grid_values(record: RaceRecord, flag_status: str = "") -> list[str]:
    """回傳與 RACE_RECORD_GRID_HEADERS 對齊的顯示字串列。"""

    r = record
    dist = str(r.distance or "")
    if dist.replace(".", "", 1).isdigit():
        dist_text = _format_integer(dist)
    else:
        dist_text = dist
    handicap_raw = str(r.handicap or "").strip()
    handicap_text = (
        _format_integer(r.handicap) if handicap_raw else (r.handicap or "")
    )
    prize_raw = str(r.prize or "").strip()
    prize_text = _format_money(r.prize) if prize_raw else ""
    return [
        format_race_date_display(r.race_date),
        r.venue or "",
        _format_integer(r.race_no) if str(r.race_no or "").strip() else "",
        r.course or "",
        dist_text,
        r.going or "",
        r.class_band or "",
        format_two_digit_99(r.draw),
        _format_integer(r.rating) if str(r.rating or "").strip() else "",
        r.jockey or "",
        r.trainer or "",
        handicap_text,
        format_two_digit_99(r.finish_pos),
        r.running_pos or "",
        str(getattr(r, "sectional_time", "") or ""),
        str(r.win_odds or ""),
        r.time or "",
        prize_text,
        flag_status or "",
    ]


def race_record_grid_row(record: RaceRecord, flag_status: str = "") -> dict[str, str]:
    """回傳中文欄名 → 顯示字串（供 WEB JSON）。"""

    values = race_record_grid_values(record, flag_status)
    return dict(zip(RACE_RECORD_GRID_HEADERS, values, strict=True))

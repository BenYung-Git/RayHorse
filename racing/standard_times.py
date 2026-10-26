"""香港賽馬會跑道標準完成時間（好地場地頭馬預期時間）。

資料來源：https://www.hkjc.com/chinese/racinginfo/racing_course_time.htm
（頁面標註 2018-08-27 更新）
"""

from __future__ import annotations

import re
from typing import Mapping

# (途程米, 班次鍵) -> 標準完成時間；班次鍵：group / 1..5 / maiden
_ST_TURF: dict[tuple[int, str], str] = {
    (1000, "group"): "0.55.95",
    (1000, "1"): "0.55.95",
    (1000, "2"): "0.56.10",
    (1000, "3"): "0.56.45",
    (1000, "4"): "0.56.85",
    (1000, "5"): "0.57.05",
    (1000, "maiden"): "0.57.25",
    (1200, "group"): "1.08.50",
    (1200, "1"): "1.08.65",
    (1200, "2"): "1.08.95",
    (1200, "3"): "1.09.25",
    (1200, "4"): "1.09.55",
    (1200, "5"): "1.09.75",
    (1200, "maiden"): "1.10.30",
    (1400, "group"): "1.21.25",
    (1400, "1"): "1.21.40",
    (1400, "2"): "1.21.65",
    (1400, "3"): "1.21.80",
    (1400, "4"): "1.22.25",
    (1400, "5"): "1.22.45",
    (1600, "group"): "1.33.95",
    (1600, "1"): "1.34.25",
    (1600, "2"): "1.34.45",
    (1600, "3"): "1.34.65",
    (1600, "4"): "1.35.30",
    (1600, "5"): "1.35.45",
    (1800, "group"): "1.47.20",
    (1800, "1"): "1.47.20",
    (1800, "2"): "1.47.40",
    (1800, "3"): "1.47.60",
    (1800, "4"): "1.48.35",
    (1800, "5"): "1.48.55",
    (2000, "group"): "2.01.20",
    (2000, "1"): "2.02.10",
    (2000, "2"): "2.02.30",
    (2000, "3"): "2.02.50",
    (2000, "4"): "2.03.20",
    (2000, "5"): "2.03.45",
    (2200, "2"): "2.13.50",
    (2200, "3"): "2.16.25",
    (2200, "4"): "2.16.40",
    (2200, "5"): "2.16.95",
    (2400, "group"): "2.26.90",
}

_HV_TURF: dict[tuple[int, str], str] = {
    (1000, "2"): "0.56.70",
    (1000, "3"): "0.57.05",
    (1000, "4"): "0.57.25",
    (1000, "5"): "0.57.45",
    (1200, "1"): "1.09.25",
    (1200, "2"): "1.09.45",
    (1200, "3"): "1.10.00",
    (1200, "4"): "1.10.20",
    (1200, "5"): "1.10.40",
    (1650, "1"): "1.39.95",
    (1650, "2"): "1.40.10",
    (1650, "3"): "1.40.30",
    (1650, "4"): "1.40.65",
    (1650, "5"): "1.40.85",
    (1800, "group"): "1.49.00",
    (1800, "1"): "1.49.00",
    (1800, "2"): "1.49.15",
    (1800, "3"): "1.49.65",
    (1800, "4"): "1.50.50",
    (1800, "5"): "1.50.80",
    (2200, "3"): "2.16.85",
    (2200, "4"): "2.17.65",
    (2200, "5"): "2.18.10",
}

_ST_AWT: dict[tuple[int, str], str] = {
    (1200, "1"): "1.08.30",
    (1200, "2"): "1.08.45",
    (1200, "3"): "1.08.65",
    (1200, "4"): "1.09.05",
    (1200, "5"): "1.09.35",
    (1650, "2"): "1.37.90",
    (1650, "3"): "1.38.15",
    (1650, "4"): "1.38.75",
    (1650, "5"): "1.39.40",
    (1800, "3"): "1.47.65",
    (1800, "4"): "1.48.40",
    (1800, "5"): "1.49.30",
}

_TABLES: dict[str, Mapping[tuple[int, str], str]] = {
    "ST_TURF": _ST_TURF,
    "HV_TURF": _HV_TURF,
    "ST_AWT": _ST_AWT,
}


def _parse_distance_m(raw: object) -> int | None:
    text = str(raw or "")
    matched = re.search(r"(\d{3,4})", text)
    if not matched:
        return None
    return int(matched.group(1))


def _parse_class_key(raw: object) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if re.search(r"新馬", text):
        return "maiden"
    if re.search(
        r"分級|精英|一級|二級|三級|Group|G\s*[123]|國際",
        text,
        re.IGNORECASE,
    ):
        return "group"
    m = re.search(r"第\s*([一二三四五1-5])\s*班", text)
    if not m:
        m = re.search(r"Class\s*([1-5])", text, re.IGNORECASE)
    if not m:
        return None
    digit = m.group(1)
    cn = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5"}
    return cn.get(digit, digit)


def _track_key(meeting_venue: object, race_course_area: object) -> str | None:
    venue = str(meeting_venue or "")
    area = str(race_course_area or "")
    is_awt = "全天候" in area or "AWT" in area.upper()
    if "跑馬地" in venue or "Happy" in venue:
        return None if is_awt else "HV_TURF"
    if "沙田" in venue or "Sha" in venue:
        return "ST_AWT" if is_awt else "ST_TURF"
    # 場地代碼 ST / HV
    code = venue.strip().upper()
    if code in {"HV", "H"}:
        return None if is_awt else "HV_TURF"
    if code in {"ST", "S"}:
        return "ST_AWT" if is_awt else "ST_TURF"
    return None


def lookup_standard_finish_time(
    *,
    meeting_venue: object = None,
    race_course_area: object = None,
    distance: object = None,
    race_class: object = None,
) -> str | None:
    """依馬場／場地／途程／班次查標準完成時間；無資料則回傳 None。"""
    track = _track_key(meeting_venue, race_course_area)
    meters = _parse_distance_m(distance)
    class_key = _parse_class_key(race_class)
    if not track or meters is None or not class_key:
        return None
    table = _TABLES.get(track)
    if not table:
        return None
    return table.get((meters, class_key))


def attach_standard_finish_time(info: dict[str, object] | None) -> dict[str, object] | None:
    """在場次摘要 dict 加上 standard_finish_time（就地／淺拷貝皆可）。"""
    if info is None:
        return None
    out = dict(info)
    out["standard_finish_time"] = (
        lookup_standard_finish_time(
            meeting_venue=out.get("meeting_venue"),
            race_course_area=out.get("race_course_area"),
            distance=out.get("distance"),
            race_class=out.get("race_class"),
        )
        or ""
    )
    return out

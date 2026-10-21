from __future__ import annotations

import re


def normalize_date_yyyy_mm_dd(raw: object) -> str:
    """將日期字串正規成 yyyy/MM/dd；無法辨識則原樣（strip）。

    支援：
    - yyyy/MM/dd、yyyy-MM-dd
    - dd/MM/yyyy、dd/MM/yy（港馬慣用日/月/年）
    - yyyy年M月D日
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    s = s.replace("-", "/")
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}/{mo:02d}/{d:02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y <= 69 else 1900
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}/{mo:02d}/{d:02d}"
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}/{mo:02d}/{d:02d}"
    return s


def format_two_digit_99(raw: object) -> str:
    """名次／檔位：純數字轉成 99（如 8→08）；非數字原樣。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{1,2}", s):
        return f"{int(s):02d}"
    return s


def format_race_date_display(date_text: str) -> str:
    """顯示用日期：一律 yyyy/MM/dd。"""
    return normalize_date_yyyy_mm_dd(date_text)


def race_date_sort_key(date_text: str) -> int:
    """Return sortable yyyymmdd key for race date text."""
    nd = normalize_date_yyyy_mm_dd(date_text)
    m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})", nd)
    if m:
        return int(f"{m.group(1)}{m.group(2)}{m.group(3)}")
    return 0

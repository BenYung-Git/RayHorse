"""賽馬分析：組出 AI 合計分數指令（對齊 ai_engine 可解析句式）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONDITION_TYPES: tuple[tuple[str, str], ...] = (
    ("trainer_wins", "練馬師贏馬"),
    ("jockey_wins", "騎師贏馬"),
    ("speed_rank", "同途程速度名次分"),
    ("custom", "自訂"),
)

CONDITION_TYPE_LABELS: dict[str, str] = {k: v for k, v in CONDITION_TYPES}

DEFAULT_SCORING: dict[str, str] = {
    "trainer_wins": "截至上一場, 練馬師的贏馬得2分, 跑入位置得1分.",
    "jockey_wins": "截至上一場, 騎師贏馬得2分, 跑入位置得1分.",
    "speed_rank": "相同途程的最佳速度, 最快得4分,速度第4快得1分,第5快及以後得0分. 要顯示每一條件的分數. 退出馬匹不要顯示",
    "custom": "",
}

# 輸出欄位（顯示名＝寫入 prompt；以「／」分隔輸入）
DISPLAY_FIELD_OPTIONS: tuple[str, ...] = (
    "場次",
    "馬號",
    "馬匹名稱",
    "練馬師",
    "騎師",
    "日期",
    "最快時間",
    "場地狀況",
    "當天負磅",
    "當天檔位",
    "當天走位",
    "場地",
    "今天負磅",
    "今天檔位",
    "今天走位",
    "當天賠率",
    "今天賠率",
    "預測賠率",
    "隔夜賠率",
    "即時賠率",
    "即時賠率-預測賠率",
    "即時位置賠率",
    "當天名次",
    "今天名次",
    "今天時間",
    "練馬師分數",
    "騎師分數",
    "速度分數",
    "負磅分數",
    "檔位分數",
    "時間分數",
    "賠率分數",
    "合計分數",
)

DEFAULT_DISPLAY_FIELDS: tuple[str, ...] = DISPLAY_FIELD_OPTIONS


def format_columns_text(columns: list[str] | tuple[str, ...] | None) -> str:
    """以「／」串接；欄位較多時拆成兩行，方便完整顯示。"""
    cols = [str(c).strip() for c in (columns or DEFAULT_DISPLAY_FIELDS) if str(c).strip()]
    if not cols:
        cols = list(DEFAULT_DISPLAY_FIELDS)
    if len(cols) <= 1:
        return cols[0]
    # 依字元長度對半拆行，避免單行過長被裁切
    lengths = [len(c) + 1 for c in cols]  # +1 for ／
    total = sum(lengths) - 1
    target = total / 2
    acc = 0
    mid = len(cols)
    for i, n in enumerate(lengths):
        acc += n
        if acc >= target:
            mid = i + 1
            break
    mid = max(1, min(mid, len(cols) - 1))
    # 換行前後保留「／」，避免相鄰欄位被黏在一起
    return "／".join(cols[:mid]) + "／\n" + "／".join(cols[mid:])


def parse_columns_text(text: str) -> list[str]:
    """解析「場次／馬號／…」型式輸入；亦相容空白、換行或逗號分隔。"""
    raw = (text or "").replace("\r", "\n").strip()
    if not raw:
        return list(DEFAULT_DISPLAY_FIELDS)
    # 兩行顯示的換行視同分隔（相容舊資料漏「／」）
    raw = re.sub(r"\s*\n\s*", "／", raw)
    # 優先以全形／半形斜線分隔
    if "／" in raw or "/" in raw:
        parts = re.split(r"[／/]+", raw)
    else:
        parts = re.split(r"[,，\s]+", raw)
    cols = [p.strip() for p in parts if p and p.strip()]
    return cols if cols else list(DEFAULT_DISPLAY_FIELDS)


@dataclass
class ConditionRow:
    enabled: bool = True
    kind: str = "custom"
    scoring: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        # 介面已取消備註欄，prefs 不寫入 note
        return {
            "enabled": self.enabled,
            "kind": self.kind,
            "scoring": self.scoring,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ConditionRow:
        raw = raw or {}
        kind = str(raw.get("kind") or "custom").strip() or "custom"
        if kind not in CONDITION_TYPE_LABELS:
            kind = "custom"
        scoring = str(raw.get("scoring") or "").strip()
        if not scoring and kind in DEFAULT_SCORING:
            scoring = DEFAULT_SCORING[kind]
        return cls(
            enabled=bool(raw.get("enabled", True)),
            kind=kind,
            scoring=scoring,
            note=str(raw.get("note") or "").strip(),
        )


def default_condition_rows() -> list[ConditionRow]:
    return [
        ConditionRow(True, "trainer_wins", DEFAULT_SCORING["trainer_wins"], ""),
        ConditionRow(True, "jockey_wins", DEFAULT_SCORING["jockey_wins"], ""),
        ConditionRow(True, "speed_rank", DEFAULT_SCORING["speed_rank"], ""),
    ]


def prefs_path(data_dir: Path) -> Path:
    return Path(data_dir) / "race_analysis_prefs.json"


def templates_path(data_dir: Path) -> Path:
    return Path(data_dir) / "race_analysis_templates.json"


def load_prefs(data_dir: Path) -> dict[str, Any]:
    path = prefs_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_prefs(data_dir: Path, prefs: dict[str, Any]) -> None:
    path = prefs_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _new_template_id() -> str:
    import time
    import uuid

    return f"t{int(time.time())}_{uuid.uuid4().hex[:8]}"


def _normalize_template(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    tid = str(raw.get("id") or "").strip() or _new_template_id()
    columns_text = str(raw.get("columns_text") or "").strip()
    columns = raw.get("columns")
    if columns_text:
        cols = parse_columns_text(columns_text)
        columns_text = format_columns_text(cols)
    elif isinstance(columns, list) and columns:
        cols = [str(c).strip() for c in columns if str(c).strip()]
        columns_text = format_columns_text(cols)
    else:
        cols = list(DEFAULT_DISPLAY_FIELDS)
        columns_text = format_columns_text(cols)
    raw_conds = raw.get("conditions")
    conditions: list[dict[str, Any]] = []
    if isinstance(raw_conds, list):
        for item in raw_conds:
            if isinstance(item, dict):
                conditions.append(ConditionRow.from_dict(item).to_dict())
    if not conditions:
        conditions = [r.to_dict() for r in default_condition_rows()]
    return {
        "id": tid,
        "name": name,
        "columns": cols,
        "columns_text": columns_text,
        "sort_field": str(raw.get("sort_field") or "合計分數").strip() or "合計分數",
        "sort_ascending": bool(raw.get("sort_ascending")),
        "all_races": bool(raw.get("all_races", True)),
        "conditions": conditions,
    }


def load_templates(data_dir: Path) -> list[dict[str, Any]]:
    path = templates_path(data_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("templates") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        norm = _normalize_template(item if isinstance(item, dict) else None)
        if norm:
            out.append(norm)
    return out


def _write_templates(data_dir: Path, templates: list[dict[str, Any]]) -> None:
    path = templates_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"templates": templates}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_template(data_dir: Path, template_id: str) -> dict[str, Any] | None:
    tid = str(template_id or "").strip()
    if not tid:
        return None
    for item in load_templates(data_dir):
        if item.get("id") == tid:
            return item
    return None


def save_template(
    data_dir: Path,
    *,
    name: str,
    payload: dict[str, Any],
    template_id: str | None = None,
) -> dict[str, Any]:
    """新增或覆寫同名／同 id 範本；回傳已儲存的範本。"""
    templates = load_templates(data_dir)
    name = str(name or "").strip()
    if not name:
        raise ValueError("請輸入範本名稱。")
    tid = str(template_id or "").strip()
    body = dict(payload or {})
    body["name"] = name
    if tid:
        body["id"] = tid
    # 同 id 優先；否則同名覆寫
    idx = -1
    if tid:
        for i, item in enumerate(templates):
            if item.get("id") == tid:
                idx = i
                break
    if idx < 0:
        for i, item in enumerate(templates):
            if str(item.get("name") or "") == name:
                idx = i
                body["id"] = item.get("id")
                break
    if idx < 0:
        body["id"] = tid or _new_template_id()
    norm = _normalize_template(body)
    if norm is None:
        raise ValueError("範本內容無效。")
    if idx >= 0:
        templates[idx] = norm
    else:
        templates.append(norm)
    _write_templates(data_dir, templates)
    return norm


def delete_template(data_dir: Path, template_id: str) -> bool:
    tid = str(template_id or "").strip()
    if not tid:
        return False
    templates = load_templates(data_dir)
    kept = [t for t in templates if t.get("id") != tid]
    if len(kept) == len(templates):
        return False
    _write_templates(data_dir, kept)
    return True


def template_as_prefs(template: dict[str, Any]) -> dict[str, Any]:
    """範本轉成可套用到面板的 prefs 結構（不含賽日）。"""
    return {
        "columns": list(template.get("columns") or []),
        "columns_text": str(template.get("columns_text") or ""),
        "sort_field": str(template.get("sort_field") or "合計分數"),
        "sort_ascending": bool(template.get("sort_ascending")),
        "all_races": bool(template.get("all_races", True)),
        "conditions": list(template.get("conditions") or []),
    }


def _race_phrase(race_no: int | None, *, all_races: bool) -> str:
    if all_races or race_no is None:
        return "每一場"
    return f"第{int(race_no)}場"


def _sort_phrase(sort_field: str, ascending: bool) -> str:
    field = (sort_field or "合計分數").strip() or "合計分數"
    order = "由小至大" if ascending else "由大至小"
    if field == "合計分數":
        return f"{order}排列出"
    return f"按{field}{order}排列出"


def build_analysis_prompt(
    *,
    race_date: str,
    race_no: int | None = None,
    all_races: bool = False,
    columns: list[str] | None = None,
    sort_field: str = "合計分數",
    sort_ascending: bool = False,
    conditions: list[ConditionRow] | None = None,
) -> str:
    """組成 ai_engine 合計分數路徑可解析的自然語言指令。"""
    date_txt = (race_date or "").strip() or "今天"
    cols = [c.strip() for c in (columns or list(DEFAULT_DISPLAY_FIELDS)) if c and str(c).strip()]
    if not cols:
        cols = list(DEFAULT_DISPLAY_FIELDS)
    if "合計分數" not in cols:
        cols.append("合計分數")
    rows = conditions if conditions is not None else default_condition_rows()
    enabled = [r for r in rows if r.enabled and (r.scoring or "").strip()]

    race_txt = _race_phrase(race_no, all_races=all_races)
    sort_txt = _sort_phrase(sort_field, sort_ascending)
    col_txt = "/".join(cols)
    lines = [
        f"按以下條件及計分方法, 計算出{date_txt}{race_txt}每一馬匹的總分數，"
        f"{sort_txt}{col_txt}",
    ]
    if not enabled:
        enabled = default_condition_rows()
    for i, row in enumerate(enabled, start=1):
        text = (row.scoring or "").strip()
        if text:
            lines.append(f"{i}. {text}")
    return "\n".join(lines)


def suggest_command_title(
    *,
    race_date: str,
    race_no: int | None = None,
    all_races: bool = False,
) -> str:
    date_txt = (race_date or "").strip() or "賽日"
    if all_races or race_no is None:
        return f"賽馬分析 {date_txt} 各場合計"
    return f"賽馬分析 {date_txt} 第{int(race_no)}場合計"


def analysis_meta_payload(
    *,
    meetings: list[dict[str, Any]],
    race_nos: list[int] | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """供 WEB／面板初始化的共用 meta。"""
    templates = load_templates(data_dir) if data_dir is not None else []
    return {
        "condition_types": [{"id": k, "label": v} for k, v in CONDITION_TYPES],
        "default_scoring": dict(DEFAULT_SCORING),
        "display_fields": list(DISPLAY_FIELD_OPTIONS),
        "default_display_fields": list(DEFAULT_DISPLAY_FIELDS),
        "default_display_fields_text": format_columns_text(DEFAULT_DISPLAY_FIELDS),
        "default_conditions": [r.to_dict() for r in default_condition_rows()],
        "default_sort_field": "合計分數",
        "default_sort_ascending": False,
        "meetings": meetings,
        "race_nos": list(race_nos or []),
        "templates": templates,
    }

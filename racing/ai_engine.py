"""本機 AI 助手：賽馬資料查詢、文件檢索、統計報表，以及系統操作意圖。"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .db import get_connection, init_db

_DATA_HINTS = ("最佳途程", "賠率", "速度", "排列", "統計", "報表", "相關資料", "分析", "計算")
_HORSE_PLACEHOLDERS = {"馬名", "馬名或編號", "關鍵字", "編號"}
_DEFAULT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("最佳途程", "【馬名】的最佳途程"),
    ("贏馬賠率", "【馬名】的贏的賠率"),
    ("同途程速度", "第1場相同途程的最快速度排列"),
    ("今日騎師", "今天贏馬的騎師排列"),
    ("今日練馬師", "今天贏馬的練馬師排列"),
    (
        "今日各場合計分數",
        "按以下條件及計分方法, 計算出今天每一場每一馬匹的總分數，"
        "由大至小排列出馬號/馬匹名稱/練馬師/騎師/日期/最快時間/當天負磅/今天負磅/"
        "當天賠率/預測賠率/即時賠率/即時位置賠率/當天名次/今天名次/今天時間/合計分數\n"
        "1. 練馬師的贏馬紀錄為分數.\n"
        "2. 騎師的贏馬紀錄為分數.\n"
        "3. 相同途程的最佳速度, 最快得4分,速度第4快得1分,第5快及以後得0分. 要顯示每一條件的分數. 退出馬匹不要顯示",
    ),
    (
        "排位及賽馬結果",
        "列出2026/10/14第五場的排位及賽馬結果，按名次排列",
    ),
    ("關鍵字查詢", "查詢【馬名或編號】的相關資料"),
    ("下載全部", "下載資料"),
    ("下載排位", "下載排位"),
    ("下載馬匹往績", "下載馬匹及往績"),
    ("下載即時賠率", "下載即時賠率"),
    ("下載賽馬結果", "下載賽馬結果"),
    ("間隔下載賠率", "每隔 5 分鐘下載即時賠率"),
    ("開啟下載", "開啟下載"),
)


@dataclass
class AiSettings:
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"


@dataclass
class AiAnswer:
    text: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    action: str | None = None
    action_arg: str | None = None
    handled: bool = True


def settings_path(db_path: Path) -> Path:
    return db_path.parent / "ai_settings.json"


def load_ai_settings(db_path: Path) -> AiSettings:
    path = settings_path(db_path)
    if not path.is_file():
        return AiSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AiSettings()
    return AiSettings(
        base_url=str(raw.get("base_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        model=str(raw.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
    )


def save_ai_settings(db_path: Path, settings: AiSettings) -> None:
    path = settings_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "base_url": settings.base_url,
                "api_key": settings.api_key,
                "model": settings.model,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


_COMMANDS_PROMPT_META = "commands_prompt_v5"


def ensure_default_commands(db_path: Path) -> None:
    """首次建立時寫入範例指令；之後只補缺少的標題，不覆寫使用者已改過的內容。"""
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT VALUE FROM AI_META WHERE KEY = 'commands_seeded';").fetchone()
        if not row or str(row["VALUE"]) != "1":
            count = conn.execute("SELECT COUNT(*) AS c FROM AI_COMMAND;").fetchone()["c"]
            if int(count) == 0:
                conn.executemany(
                    "INSERT INTO AI_COMMAND(CMD_TITLE, CMD_PROMPT) VALUES(?, ?);",
                    list(_DEFAULT_COMMANDS),
                )
            conn.execute(
                """
                INSERT INTO AI_META(KEY, VALUE) VALUES('commands_seeded', '1')
                ON CONFLICT(KEY) DO UPDATE SET VALUE = excluded.VALUE;
                """
            )
        # 既有資料庫只補缺少的預設指令標題（不覆寫已有 CMD_PROMPT）
        for title, prompt in _DEFAULT_COMMANDS:
            exists = conn.execute(
                "SELECT 1 FROM AI_COMMAND WHERE CMD_TITLE = ? LIMIT 1;",
                (title,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO AI_COMMAND(CMD_TITLE, CMD_PROMPT) VALUES(?, ?);",
                (title, prompt),
            )
        # 舊標題「第五場合計分數」改為今日各場；若新標題已存在則刪舊標題避免重複
        old_score = conn.execute(
            "SELECT ID FROM AI_COMMAND WHERE CMD_TITLE = '第五場合計分數' LIMIT 1;"
        ).fetchone()
        new_score = conn.execute(
            "SELECT ID FROM AI_COMMAND WHERE CMD_TITLE = '今日各場合計分數' LIMIT 1;"
        ).fetchone()
        if old_score and new_score:
            conn.execute(
                "DELETE FROM AI_COMMAND WHERE CMD_TITLE = '第五場合計分數';"
            )
        elif old_score and not new_score:
            conn.execute(
                "UPDATE AI_COMMAND SET CMD_TITLE = ?, CMD_PROMPT = ? WHERE CMD_TITLE = ?;",
                (
                    "今日各場合計分數",
                    dict(_DEFAULT_COMMANDS).get("今日各場合計分數", ""),
                    "第五場合計分數",
                ),
            )
        # 一次性把預設合計分數／排位指令提示文升級到最新版（不重複覆寫）
        meta = conn.execute(
            "SELECT VALUE FROM AI_META WHERE KEY = ?;",
            (_COMMANDS_PROMPT_META,),
        ).fetchone()
        if not meta or str(meta["VALUE"]) != "1":
            for title in ("今日各場合計分數", "排位及賽馬結果"):
                prompt = dict(_DEFAULT_COMMANDS).get(title)
                if not prompt:
                    continue
                conn.execute(
                    "UPDATE AI_COMMAND SET CMD_PROMPT = ? WHERE CMD_TITLE = ?;",
                    (prompt, title),
                )
            conn.execute(
                """
                INSERT INTO AI_META(KEY, VALUE) VALUES(?, '1')
                ON CONFLICT(KEY) DO UPDATE SET VALUE = excluded.VALUE;
                """,
                (_COMMANDS_PROMPT_META,),
            )


def list_commands(db_path: Path) -> list[sqlite3.Row]:
    init_db(db_path)
    ensure_default_commands(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT ID, CMD_TITLE, CMD_PROMPT, CREATED_AT
                FROM AI_COMMAND
                ORDER BY ID ASC;
                """
            ).fetchall()
        )


def save_command(db_path: Path, prompt: str, title: str = "") -> int:
    text = prompt.strip()
    if not text:
        raise ValueError("指令內容是空的")
    init_db(db_path)
    ensure_default_commands(db_path)
    label = title.strip() or text[:24]
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO AI_COMMAND(CMD_TITLE, CMD_PROMPT) VALUES(?, ?);",
            (label, text),
        )
        return int(cur.lastrowid or 0)


def delete_command(db_path: Path, command_id: int) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM AI_COMMAND WHERE ID = ?;", (int(command_id),))


def append_chat(db_path: Path, role: str, content: str) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO AI_CHAT(ROLE, CONTENT) VALUES(?, ?);",
            (role, content),
        )


def load_chat(db_path: Path, limit: int = 200) -> list[sqlite3.Row]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = list(
            conn.execute(
                """
                SELECT ROLE, CONTENT
                FROM AI_CHAT
                ORDER BY ID DESC
                LIMIT ?;
                """,
                (int(limit),),
            ).fetchall()
        )
    rows.reverse()
    return rows


def user_prompts(db_path: Path, limit: int = 200) -> list[str]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = list(
            conn.execute(
                """
                SELECT CONTENT
                FROM AI_CHAT
                WHERE ROLE = 'user'
                ORDER BY ID DESC
                LIMIT ?;
                """,
                (int(limit),),
            ).fetchall()
        )
    return [str(r["CONTENT"]) for r in reversed(rows)]


def clear_chat(db_path: Path) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM AI_CHAT;")


def add_document(db_path: Path, filename: str, content: str) -> int:
    init_db(db_path)
    text = content
    note = ""
    if len(text) > 2_000_000:
        text = text[:2_000_000]
        note = "\n（內容過長，只保存前 200 萬字）"
    with get_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO AI_DOCUMENT(FILENAME, CONTENT_TEXT) VALUES(?, ?);",
            (filename, text),
        )
        doc_id = int(cur.lastrowid or 0)
    if note:
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE AI_DOCUMENT SET CONTENT_TEXT = CONTENT_TEXT || ? WHERE ID = ?;",
                (note, doc_id),
            )
    return doc_id


def list_documents(db_path: Path) -> list[sqlite3.Row]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT ID, FILENAME, LENGTH(COALESCE(CONTENT_TEXT, '')) AS N, CREATED_AT
                FROM AI_DOCUMENT
                ORDER BY ID DESC;
                """
            ).fetchall()
        )


def read_uploaded_file(path: Path) -> str:
    """讀取私人文件為純文字。支援文字、網頁、CSV 與 Excel。"""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".log", ".json", ".tsv"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    if suffix in {".xlsx", ".xls"}:
        try:
            import pandas as pd

            frame = pd.read_excel(path)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("讀取 Excel 失敗。請先把工作表另存成 CSV 再上傳。") from exc
        return frame.to_csv(index=False)
    raise ValueError("目前支援 txt、md、csv、json、html、xlsx。")


def normalize_date_key(value: str) -> str:
    """把常見賽期字串轉成 YYYY-MM-DD，無法辨識時回傳空字串。"""
    s = (value or "").strip().replace(".", "/").replace("-", "/")
    parts = [p for p in s.split("/") if p]
    if len(parts) != 3:
        return ""
    a, b, c = parts
    if len(a) == 4 and a.isdigit():
        year, month, day = a, b, c
    elif len(c) == 4 and c.isdigit():
        day, month, year = a, b, c
    elif len(c) == 2 and c.isdigit():
        day, month, year = a, b, "20" + c
    else:
        return ""
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return ""
    month_n, day_n = int(month), int(day)
    if not (1 <= month_n <= 12 and 1 <= day_n <= 31):
        return ""
    return f"{int(year):04d}-{month_n:02d}-{day_n:02d}"


def parse_race_time_seconds(value: str) -> float | None:
    """HKJC 時間：1.09.99 或 1:09.99 為分.秒；57.21 為秒。"""
    s = (value or "").strip().replace(" ", "")
    if not s or s in {"-", "—", "--", "---"}:
        return None

    def frac(text: str | None) -> float:
        if not text:
            return 0.0
        return float("0." + text)

    matched = re.fullmatch(r"(\d+):(\d{1,2})(?:\.(\d+))?", s)
    if matched:
        return int(matched.group(1)) * 60 + int(matched.group(2)) + frac(matched.group(3))
    matched = re.fullmatch(r"(\d+)\.(\d{2})\.(\d+)", s)
    if matched:
        return int(matched.group(1)) * 60 + int(matched.group(2)) + frac(matched.group(3))
    matched = re.fullmatch(r"(\d+)(?:\.(\d+))?", s)
    if matched:
        return int(matched.group(1)) + frac(matched.group(2))
    return None


def parse_distance_m(value: str) -> int | None:
    matched = re.search(r"(\d{3,4})", value or "")
    if not matched:
        return None
    meters = int(matched.group(1))
    if meters < 800 or meters > 3600:
        return None
    return meters


def _is_win(finish_pos: str | None) -> bool:
    text = str(finish_pos or "").strip().upper()
    if not text or text.startswith("WV"):
        return False
    matched = re.fullmatch(r"0*(\d{1,2})", text)
    return bool(matched and int(matched.group(1)) == 1)


def _finish_number(finish_pos: str | None) -> int | None:
    text = str(finish_pos or "").strip().upper()
    if not text or text.startswith("WV"):
        return None
    matched = re.fullmatch(r"0*(\d{1,2})", text)
    if not matched:
        return None
    return int(matched.group(1))


def _is_place_finish(finish_pos: str | None, *, top_n: int = 3) -> bool:
    """入位（不含冠軍）：名次 2…top_n。"""
    n = _finish_number(finish_pos)
    return n is not None and 2 <= n <= max(2, top_n)


def _parse_handicap_int(raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text or text in {"—", "-"}:
        return None
    matched = re.search(r"(\d{2,3})", text)
    if not matched:
        return None
    try:
        return int(matched.group(1))
    except ValueError:
        return None


def _parse_person_point_weights(question: str) -> tuple[int, int]:
    """
    解析「贏馬得N分／跑入位置得M分」。
    未寫明時維持舊行為：勝出 1 分、入位 0 分。
    """
    win_pts = 1
    place_pts = 0
    win_m = re.search(r"贏馬(?:得|為)\s*(\d+)\s*分", question)
    if win_m:
        win_pts = int(win_m.group(1))
    place_m = re.search(r"(?:跑入)?位置(?:得|為)\s*(\d+)\s*分", question)
    if place_m:
        place_pts = int(place_m.group(1))
    return win_pts, place_pts


def _parse_handicap_topn_bonus(question: str) -> tuple[int, int, int] | None:
    """
    解析「負磅>=130跑入前3名得1分」→ (門檻, 前N名, 分數)。
    """
    matched = re.search(
        r"負磅\s*[>≥＞]=?\s*(\d+)\s*.{0,12}前\s*(\d+)\s*名.{0,8}得\s*(\d+)\s*分",
        question,
    )
    if not matched:
        matched = re.search(
            r"負磅\s*[>≥＞]=?\s*(\d+)\s*.{0,12}(?:入|跑入).{0,4}前\s*(\d+)\s*.{0,8}得\s*(\d+)\s*分",
            question,
        )
    if not matched:
        return None
    return int(matched.group(1)), int(matched.group(2)), int(matched.group(3))


def _fmt_pct(numer: int, denom: int) -> str:
    if denom <= 0:
        return "—"
    return f"{(100.0 * numer / denom):.1f}%"


def _fmt_speed(meters: int | None, seconds: float | None) -> str:
    if not meters or not seconds or seconds <= 0:
        return "—"
    return f"{meters / seconds:.2f}"


def _fmt_odds(value: str | None) -> str:
    text = str(value or "").strip()
    return text or "—"


def _parse_odds(value: str | None) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _horse_label(row: sqlite3.Row | None, fallback: str = "") -> str:
    if row is None:
        return fallback
    zh = str(row["name_zh"] or "").strip()
    en = str(row["name_en"] or "").strip()
    brand = str(row["brand_no"] or "").strip()
    hid = str(row["horse_id"] or "").strip()
    name = zh or en or brand or hid or fallback
    extra = brand or hid
    if extra and extra != name:
        return f"{name}（{extra}）"
    return name


def _extract_quoted(text: str) -> str:
    matched = re.search(r"[「『\"'](.+?)[」』\"']", text)
    if matched:
        return matched.group(1).strip()
    matched = re.search(r"[【\[](.+?)[】\]]", text)
    if matched:
        inner = matched.group(1).strip()
        if inner not in _HORSE_PLACEHOLDERS:
            return inner
    return ""


def _clean_subject(text: str) -> str:
    subject = text.strip(" ：:，,。．")
    subject = re.sub(r"^(請|幫我|查一下|查詢|找出|看看|這隻馬名|這隻馬|這匹馬|馬名)+", "", subject)
    return subject.strip(" ：:，,。．的")


def extract_horse_keyword(question: str) -> str:
    quoted = _extract_quoted(question)
    if quoted:
        return quoted
    for marker in (
        "的最佳途程",
        "最佳途程",
        "的贏的賠率",
        "的贏馬賠率",
        "的獨贏賠率",
        "贏的賠率",
        "贏馬賠率",
        "獨贏賠率",
        "的相關資料",
        "相關資料",
    ):
        if marker in question:
            left = _clean_subject(question.split(marker, 1)[0])
            if left and left not in _HORSE_PLACEHOLDERS:
                return left
    return ""


def _needs_placeholder(question: str) -> bool:
    return bool(re.search(r"[【\[]\s*(馬名|馬名或編號|關鍵字|編號)\s*[】\]]", question))


def _resolve_horses(conn: sqlite3.Connection, keyword: str) -> list[sqlite3.Row]:
    kw = keyword.strip()
    if not kw:
        return []
    like = f"%{kw}%"
    return list(
        conn.execute(
            """
            SELECT horse_id, brand_no, name_zh, name_en, status
            FROM horses
            WHERE horse_id = ?
               OR UPPER(TRIM(COALESCE(brand_no, ''))) = UPPER(?)
               OR COALESCE(name_zh, '') = ?
               OR LOWER(COALESCE(name_en, '')) = LOWER(?)
               OR horse_id LIKE ?
               OR COALESCE(brand_no, '') LIKE ?
               OR COALESCE(name_zh, '') LIKE ?
               OR COALESCE(name_en, '') LIKE ?
            ORDER BY
              CASE
                WHEN horse_id = ? THEN 0
                WHEN UPPER(TRIM(COALESCE(brand_no, ''))) = UPPER(?) THEN 1
                WHEN COALESCE(name_zh, '') = ? OR LOWER(COALESCE(name_en, '')) = LOWER(?) THEN 2
                ELSE 3
              END,
              name_zh
            LIMIT 30;
            """,
            (kw, kw, kw, kw, like, like, like, like, kw, kw, kw, kw),
        ).fetchall()
    )


def _pick_horse(rows: list[sqlite3.Row], keyword: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    if not rows:
        return None, []
    exact = []
    kw = keyword.strip().lower()
    for row in rows:
        values = {
            str(row["horse_id"] or "").strip().lower(),
            str(row["brand_no"] or "").strip().lower(),
            str(row["name_zh"] or "").strip().lower(),
            str(row["name_en"] or "").strip().lower(),
        }
        if kw in values:
            exact.append(row)
    if len(exact) == 1:
        return exact[0], rows
    if len(rows) == 1:
        return rows[0], rows
    return None, rows


def _ambiguous(keyword: str, rows: list[sqlite3.Row]) -> AiAnswer:
    lines = [f"「{keyword}」對到多匹馬，請改用完整馬名、烙號或 HorseId："]
    table = []
    for row in rows[:20]:
        lines.append(f"- {_horse_label(row)}")
        table.append(
            [
                str(row["horse_id"] or ""),
                str(row["brand_no"] or ""),
                str(row["name_zh"] or ""),
                str(row["name_en"] or ""),
            ]
        )
    return AiAnswer(
        "\n".join(lines),
        columns=["HorseId", "烙號", "中文名", "英文名"],
        rows=table,
    )


def _horse_ids(row: sqlite3.Row) -> list[str]:
    ids = []
    for key in ("horse_id", "brand_no"):
        value = str(row[key] or "").strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def _name_map(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT horse_id, brand_no, name_zh, name_en
        FROM horses
        WHERE horse_id IN ({marks}) OR COALESCE(brand_no, '') IN ({marks});
        """,
        [*ids, *ids],
    ).fetchall()
    mapping: dict[str, str] = {}
    for row in rows:
        label = str(row["name_zh"] or row["name_en"] or row["brand_no"] or row["horse_id"])
        for key in ("horse_id", "brand_no"):
            value = str(row[key] or "").strip()
            if value:
                mapping[value] = label
    return mapping


def _distinct_race_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT race_date FROM race_records;").fetchall()
    return [str(r["race_date"]) for r in rows if str(r["race_date"] or "").strip()]


def _win_date_keys(conn: sqlite3.Connection) -> set[str]:
    keys: set[str] = set()
    for row in conn.execute("SELECT race_date, finish_pos FROM race_records;"):
        if not _is_win(row["finish_pos"]):
            continue
        key = normalize_date_key(str(row["race_date"] or ""))
        if key:
            keys.add(key)
    return keys


def _target_date_key(
    question: str,
    conn: sqlite3.Connection,
    *,
    require_win: bool = False,
) -> tuple[str, str, bool]:
    """回傳 (YYYY-MM-DD, 說明, 是否為使用者指定或真正的今天)。"""
    matched = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", question)
    if not matched:
        matched = re.search(r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", question)
    if matched:
        key = normalize_date_key(matched.group(1))
        if key:
            return key, key, True
    today = datetime.now().strftime("%Y-%m-%d")
    if require_win:
        win_dates = _win_date_keys(conn)
        if today in win_dates:
            return today, "今天", True
        if not win_dates:
            return today, "今天", False
        latest = max(win_dates)
        return latest, f"最近有勝出紀錄的賽日 {latest}（資料庫沒有今天的賽果）", False
    dates = _distinct_race_dates(conn)
    if any(normalize_date_key(item) == today for item in dates):
        return today, "今天", True
    keyed = [normalize_date_key(item) for item in dates]
    keyed = [key for key in keyed if key]
    if not keyed:
        return today, "今天", False
    latest = max(keyed)
    return latest, f"最近賽日 {latest}（資料庫沒有今天的賽果）", False


def _dates_for_key(conn: sqlite3.Connection, key: str) -> list[str]:
    return [item for item in _distinct_race_dates(conn) if normalize_date_key(item) == key]


def _help_text() -> str:
    return (
        "我可以在本機資料庫和你上傳的文件裡找資料、做統計，並操作這個系統。\n"
        "賽馬例子：\n"
        "- 金鎗六十的最佳途程\n"
        "- 金鎗六十的贏的賠率\n"
        "- 第3場相同途程的最快速度排列\n"
        "- 今天贏馬的騎師排列\n"
        "- 今天贏馬的練馬師排列\n"
        "- 計算今天每一場合計分數（顯示練馬師／騎師／速度各條件分數＋預測／即時賠率＋合計）\n"
        "- 列出 2026/10/14 第五場的排位及賽馬結果，按名次排列\n"
        "- 查詢 H123 或馬名的相關資料\n"
        "下載操作：\n"
        "- 下載資料／下載一次（馬匹往績＋排位＋即時賠率）\n"
        "- 下載馬匹及往績、下載排位、下載即時賠率\n"
        "- 指定時間下載 23:00\n"
        "- 每隔 5 分鐘下載即時賠率\n"
        "- 開啟下載（打開下載設定視窗）\n"
        "其他系統操作：開啟排位、開啟馬匹、更新馬匹資料、任務設定。\n"
        "文件：按「上傳文件」，再問「在文件中找……」或「統計文件」。\n"
        "上／下鍵可叫回上一句或下一句提問。左邊「指令」可從 AI_COMMAND 選 CMD_PROMPT。"
        "日常開放式問題可在「模型設定」填入相容 OpenAI 的服務；留空則只使用本機資料。"
    )


def _download_scope(question: str) -> tuple[bool, bool, bool, bool]:
    """回傳 (馬匹及往績, 排位, 即時賠率, 賽馬結果)。未指明項目時前三者下載。"""
    horses = bool(re.search(r"馬匹|往績", question))
    racecard = "排位" in question
    odds = bool(re.search(r"即時賠率|賠率", question)) and "贏的賠率" not in question
    results = bool(re.search(r"賽馬結果|賽果|localresults", question))
    if horses or racecard or odds or results:
        if results and not horses and not racecard and not odds:
            return False, False, False, True
        if odds and not horses and not racecard and not results:
            return False, False, True, False
        return horses, racecard, odds, results
    return True, True, True, False


def _download_time(question: str) -> str | None:
    matched = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", question)
    if not matched:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _download_interval_minutes(question: str) -> int | None:
    matched = re.search(r"每隔\s*(\d{1,3})\s*分", question)
    if matched:
        return max(1, int(matched.group(1)))
    matched = re.search(r"每\s*(\d{1,3})\s*分", question)
    if matched:
        return max(1, int(matched.group(1)))
    return None


def _detect_action(question: str) -> AiAnswer | None:
    # 下載／系統操作優先；其餘含「賠率」「排列」等字樣的視為資料查詢。
    is_download_cmd = bool(re.search(r"下載|更新排位|更新馬匹|開啟下載|任務設定", question))
    if (not is_download_cmd) and any(hint in question for hint in _DATA_HINTS):
        return None

    if re.search(r"(開啟|打開).{0,4}下載|下載設定|下載視窗", question):
        return AiAnswer("正在開啟下載設定。", action="open_download")

    interval_minutes = _download_interval_minutes(question)
    if interval_minutes is not None and ("下載" in question or "更新" in question):
        horses, racecard, odds, results = _download_scope(question)
        if not horses and not racecard and not odds and not results:
            odds = True
        scope = []
        if horses:
            scope.append("horses")
        if racecard:
            scope.append("racecard")
        if odds:
            scope.append("odds")
        if results:
            scope.append("results")
        arg = f"{interval_minutes}|{','.join(scope) or 'odds'}"
        return AiAnswer(
            f"正在設定每隔 {interval_minutes} 分鐘下載。",
            action="download_interval",
            action_arg=arg,
        )

    schedule_like = bool(
        re.search(r"指定時間|定時下載|每日.*下載|每天.*下載", question)
        or ("下載" in question and _download_time(question) and "一次" not in question)
    )
    if schedule_like and ("下載" in question or "更新" in question):
        when = _download_time(question) or "23:00"
        horses, racecard, odds, results = _download_scope(question)
        scope = []
        if horses:
            scope.append("horses")
        if racecard:
            scope.append("racecard")
        if odds:
            scope.append("odds")
        if results:
            scope.append("results")
        arg = f"{when}|{','.join(scope) or 'horses,racecard,odds'}"
        return AiAnswer(
            f"正在設定每日 {when} 下載。",
            action="download_schedule",
            action_arg=arg,
        )

    if re.search(
        r"下載一次|下載資料|開始下載|全部下載|下載全部|下載全部資料",
        question,
    ) or (question.strip() in {"下載", "下載。"}):
        horses, racecard, odds, results = _download_scope(question)
        arg = ",".join(
            part
            for part, enabled in (
                ("horses", horses),
                ("racecard", racecard),
                ("odds", odds),
                ("results", results),
            )
            if enabled
        )
        return AiAnswer(
            "正在下載資料。",
            action="download_once",
            action_arg=arg or "horses,racecard,odds",
        )

    if re.search(r"下載.{0,8}(賽馬結果|賽果)", question):
        return AiAnswer("正在下載賽馬結果。", action="download_once", action_arg="results")
    if re.search(r"下載.{0,8}(即時)?賠率", question) and "馬匹" not in question and "排位" not in question:
        return AiAnswer("正在下載即時賠率。", action="download_once", action_arg="odds")
    if re.search(r"下載.{0,8}(馬匹|往績)", question) and "排位" not in question:
        return AiAnswer("正在下載馬匹資料及往績。", action="download_once", action_arg="horses")
    if re.search(r"下載.{0,6}排位", question):
        return AiAnswer("正在下載排位。", action="download_once", action_arg="racecard,odds")
    if re.search(r"更新.{0,6}馬匹", question) and "排位" not in question and "往績" not in question:
        return AiAnswer("正在更新馬匹資料。", action="update_horses")
    if re.search(r"(更新|下載).{0,6}排位", question):
        return AiAnswer("正在下載排位。", action="download_once", action_arg="racecard,odds")
    if "任務設定" in question:
        return AiAnswer("正在開啟任務設定。", action="task_settings")
    if "抓取" in question and "往績" in question:
        keyword = extract_horse_keyword(question)
        return AiAnswer("正在抓取往績。", action="fetch_records", action_arg=keyword or None)
    if re.search(r"(開啟|打開|顯示).{0,8}排位", question):
        return AiAnswer("正在開啟排位。", action="open_racecard")
    if re.search(r"(選擇|選取|打開馬|開啟馬).+", question):
        keyword = extract_horse_keyword(question) or _clean_subject(
            re.sub(r"(請|幫我|選擇|選取|打開|開啟|馬匹|這隻|這匹)", "", question)
        )
        if keyword:
            return AiAnswer(f"正在開啟馬匹：{keyword}", action="select_horse", action_arg=keyword)
    if re.search(r"(開啟|打開|顯示).{0,8}(往績|馬匹)", question):
        return AiAnswer("正在開啟馬匹／往績。", action="open_horses")
    return None


def _best_distance(conn: sqlite3.Connection, horse: sqlite3.Row) -> AiAnswer:
    ids = _horse_ids(horse)
    marks = ",".join("?" * len(ids))
    records = conn.execute(
        f"""
        SELECT distance, finish_pos, time
        FROM race_records
        WHERE horse_id IN ({marks});
        """,
        ids,
    ).fetchall()
    buckets: dict[int, dict[str, object]] = {}
    for record in records:
        meters = parse_distance_m(str(record["distance"] or ""))
        if meters is None:
            continue
        bucket = buckets.setdefault(
            meters,
            {"starts": 0, "wins": 0, "finishes": [], "best_sec": None, "best_time": ""},
        )
        finish = _finish_number(record["finish_pos"])
        if finish is None:
            continue
        bucket["starts"] = int(bucket["starts"]) + 1
        bucket["finishes"].append(finish)  # type: ignore[union-attr]
        if finish == 1:
            bucket["wins"] = int(bucket["wins"]) + 1
        seconds = parse_race_time_seconds(str(record["time"] or ""))
        best = bucket["best_sec"]
        if seconds is not None and (best is None or seconds < float(best)):
            bucket["best_sec"] = seconds
            bucket["best_time"] = str(record["time"] or "")

    if not buckets:
        return AiAnswer(f"{_horse_label(horse)} 沒有可用的途程往績。")

    ranked = []
    for meters, bucket in buckets.items():
        starts = int(bucket["starts"])
        wins = int(bucket["wins"])
        finishes = list(bucket["finishes"])  # type: ignore[arg-type]
        avg = sum(finishes) / len(finishes) if finishes else 99
        ranked.append((wins, starts and wins / starts, -avg, meters, bucket))
    ranked.sort(reverse=True)
    rows = []
    for _wins, _rate, _avg, meters, bucket in ranked:
        starts = int(bucket["starts"])
        wins = int(bucket["wins"])
        finishes = list(bucket["finishes"])  # type: ignore[arg-type]
        avg = sum(finishes) / len(finishes) if finishes else 0
        rows.append(
            [
                str(meters),
                str(starts),
                str(wins),
                _fmt_pct(wins, starts),
                f"{avg:.2f}" if finishes else "—",
                str(bucket["best_time"] or "—"),
                _fmt_speed(meters, bucket["best_sec"] if isinstance(bucket["best_sec"], float) else None),
            ]
        )
    top = rows[0]
    text = (
        f"{_horse_label(horse)} 的最佳途程是 {top[0]} 米"
        f"（勝出 {top[2]} / 出賽 {top[1]}，勝率 {top[3]}）。"
        "排序先看勝出次數，再看勝率與平均名次。"
    )
    return AiAnswer(
        text,
        columns=["途程(米)", "出賽", "勝出", "勝率", "平均名次", "最快時間", "最快速度(m/s)"],
        rows=rows,
    )


def _win_odds(conn: sqlite3.Connection, horse: sqlite3.Row) -> AiAnswer:
    ids = _horse_ids(horse)
    marks = ",".join("?" * len(ids))
    records = conn.execute(
        f"""
        SELECT race_date, venue, race_no, distance, jockey, trainer, finish_pos, win_odds
        FROM race_records
        WHERE horse_id IN ({marks});
        """,
        ids,
    ).fetchall()
    wins = [r for r in records if _is_win(r["finish_pos"])]
    wins.sort(key=lambda r: normalize_date_key(str(r["race_date"] or "")), reverse=True)
    if not wins:
        return AiAnswer(f"{_horse_label(horse)} 在往績裡沒有勝出紀錄。")
    rows = []
    odds_values = []
    for record in wins:
        odds = _parse_odds(record["win_odds"])
        if odds is not None:
            odds_values.append(odds)
        rows.append(
            [
                str(record["race_date"] or ""),
                str(record["venue"] or ""),
                str(record["race_no"] or ""),
                str(record["distance"] or ""),
                str(record["jockey"] or ""),
                str(record["trainer"] or ""),
                _fmt_odds(record["win_odds"]),
            ]
        )
    if odds_values:
        summary = (
            f"勝出 {len(wins)} 場，其中 {len(odds_values)} 場有賠率。"
            f"平均 {sum(odds_values) / len(odds_values):.2f}，"
            f"最低 {min(odds_values):.2f}，最高 {max(odds_values):.2f}。"
        )
    else:
        summary = f"勝出 {len(wins)} 場，往績裡沒有寫入獨贏賠率。"
    return AiAnswer(
        f"{_horse_label(horse)} 的贏馬賠率：{summary}",
        columns=["日期", "場地", "場次", "途程", "騎師", "練馬師", "獨贏賠率"],
        rows=rows,
    )


def _parse_venue(question: str) -> str:
    if "跑馬地" in question or re.search(r"\bHV\b", question, re.I):
        return "HV"
    if "從化" in question or re.search(r"\bFC\b", question, re.I):
        return "FC"
    if "沙田" in question or re.search(r"\bST\b", question, re.I):
        return "ST"
    return ""


def _cn_race_no(text: str) -> int | None:
    mapping = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
        "十三": 13,
        "十四": 14,
    }
    return mapping.get(text.strip())


def _parse_race_token(token: str) -> int | None:
    """解析『1』或『五』為場次數字。"""
    t = (token or "").strip()
    if not t:
        return None
    if t.isdigit():
        n = int(t)
        return n if 1 <= n <= 14 else None
    return _cn_race_no(t)


def _parse_race_no(question: str) -> int | None:
    nos = _parse_race_nos(question)
    return nos[0] if nos else None


def _wants_all_races(question: str) -> bool:
    """問題是否要求今天／該賽日每一場（全部場次）。"""
    text = question or ""
    if re.search(r"每一場|每一個場|所有場|全部場|各場次|各場|全場次", text):
        return True
    if re.search(r"今天.{0,8}(全部|所有).{0,4}場", text):
        return True
    return False


def _parse_race_nos(question: str) -> list[int]:
    """
    解析場次清單／範圍。
    支援：第5場；第1場至第8場；第1至第8場；第1-8場；第一場到第八場。
    「每一場／所有場」不在此展開（需對照排位後由呼叫端處理）。
    """
    text = question or ""
    if _wants_all_races(text):
        return []
    # 範圍：第1場至第8場 / 第1至第8場 / 第1-8場
    matched = re.search(
        r"第\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*場?"
        r"\s*(?:至|到|～|~|-|—|－)\s*"
        r"第?\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*場",
        text,
    )
    if matched:
        a = _parse_race_token(matched.group(1))
        b = _parse_race_token(matched.group(2))
        if a is not None and b is not None:
            lo, hi = (a, b) if a <= b else (b, a)
            return list(range(lo, hi + 1))

    nos: list[int] = []
    for m in re.finditer(r"第\s*(\d{1,2})\s*場", text):
        n = int(m.group(1))
        if 1 <= n <= 14 and n not in nos:
            nos.append(n)
    for m in re.finditer(r"第\s*([一二三四五六七八九十]{1,3})\s*場", text):
        n = _cn_race_no(m.group(1))
        if n is not None and n not in nos:
            nos.append(n)
    return nos


def _meeting_race_nos(
    conn: sqlite3.Connection, race_date: str, racecourse: str
) -> list[int]:
    """排位中該賽日／場地的全部場次（由小至大）。"""
    nos: list[int] = []
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT CAST(race_no AS INTEGER) AS n
            FROM racecard_starters
            WHERE race_date = ? AND racecourse = ?
            ORDER BY n;
            """,
            (race_date, racecourse),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        try:
            n = int(row["n"])
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 14 and n not in nos:
            nos.append(n)
    if nos:
        return nos
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT CAST(race_no AS INTEGER) AS n
            FROM racecard_race_info
            WHERE race_date = ? AND racecourse = ?
            ORDER BY n;
            """,
            (race_date, racecourse),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    for row in rows:
        try:
            n = int(row["n"])
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 14 and n not in nos:
            nos.append(n)
    return nos


def _is_combined_win_score_question(question: str) -> bool:
    """合計／總分數計分（可含練馬師、騎師、同途程速度名次分）。"""
    if "按以下條件及計分方法" in (question or ""):
        return True
    if any(word in question for word in ("合計分數", "總分數", "計分方法", "計分")):
        return True
    has_race = bool(_parse_race_nos(question)) or _wants_all_races(question)
    has_score = any(word in question for word in ("分數", "得分", "計分"))
    has_people = ("練馬師" in question or "練馬" in question) and ("騎師" in question)
    has_win = any(word in question for word in ("贏馬", "勝出", "贏馬紀錄"))
    has_speed_pts = ("速度" in question or "途程" in question) and has_score
    return bool(has_race and has_score and (has_people or has_win or has_speed_pts))


def _wants_speed_rank_points(question: str) -> bool:
    """問題是否要求把相同途程最佳速度名次化成分數。"""
    if any(word in question for word in ("速度", "最快", "途程")) and any(
        word in question for word in ("分", "計分", "名次", "第1名", "第１名")
    ):
        return True
    if re.search(r"第\s*1\s*名\s*得\s*\d+\s*分", question):
        return True
    return False


def _speed_place_points(place: int | None) -> int:
    """相同途程最佳速度：第1→4、第2→3、第3→2、第4→1、第5及以後→0。"""
    if place is None or place <= 0 or place >= 5:
        return 0
    return 5 - place


# 最快速度紀錄：
# (秒數, 時間, 賽日, 當天負磅, 當天名次, 當天賠率, 當天檔位, 場地, 場地狀況, 當天走位)
BestSpeedInfo = tuple[float, str, str, str, str, str, str, str, str, str]

_VENUE_ZH = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}


def _venue_display(code: str) -> str:
    raw = (code or "").strip()
    if not raw:
        return "—"
    return _VENUE_ZH.get(raw.upper(), raw)


def _meeting_race_no_int(raw_race_no: object, season_map: dict[str, str]) -> int | None:
    """把 race_records 的場次（含馬季 108）轉成當日場次整數。"""
    raw = str(raw_race_no or "").strip()
    if not raw:
        return None
    if raw in season_map:
        try:
            return int(season_map[raw])
        except ValueError:
            return None
    # 當日已有馬季場次對照時，略過 1–2 位數雜訊（往績誤寫同日）
    if season_map:
        return None
    if re.fullmatch(r"\d{1,2}", raw) and 1 <= int(raw) <= 14:
        return int(raw)
    return None


def _best_speed_info_by_horse(
    conn: sqlite3.Connection, horse_ids: list[str], distance_m: int
) -> dict[str, BestSpeedInfo]:
    """各馬在指定途程的最快紀錄。"""
    from .web_utils import format_two_digit_99

    out: dict[str, BestSpeedInfo] = {}
    if not horse_ids or not distance_m:
        return out
    marks = ",".join("?" * len(horse_ids))
    try:
        rows = conn.execute(
            f"""
            SELECT horse_id, distance, time, race_date, venue,
                   COALESCE(handicap, '') AS handicap,
                   COALESCE(finish_pos, '') AS finish_pos,
                   COALESCE(win_odds, '') AS win_odds,
                   COALESCE(draw, '') AS draw,
                   COALESCE(going, '') AS going,
                   COALESCE(running_pos, '') AS running_pos
            FROM race_records
            WHERE horse_id IN ({marks});
            """,
            horse_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            f"""
            SELECT horse_id, distance, time, race_date, venue, '' AS handicap,
                   COALESCE(finish_pos, '') AS finish_pos,
                   COALESCE(win_odds, '') AS win_odds,
                   COALESCE(draw, '') AS draw,
                   COALESCE(going, '') AS going,
                   COALESCE(running_pos, '') AS running_pos
            FROM race_records
            WHERE horse_id IN ({marks});
            """,
            horse_ids,
        ).fetchall()
    for record in rows:
        if parse_distance_m(str(record["distance"] or "")) != distance_m:
            continue
        seconds = parse_race_time_seconds(str(record["time"] or ""))
        if seconds is None:
            continue
        hid = str(record["horse_id"])
        prev = out.get(hid)
        if prev is None or seconds < prev[0]:
            out[hid] = (
                seconds,
                str(record["time"] or "").strip() or "—",
                str(record["race_date"] or "").strip() or "—",
                str(record["handicap"] or "").strip(),
                format_two_digit_99(record["finish_pos"]) or "",
                str(record["win_odds"] or "").strip(),
                format_two_digit_99(record["draw"]) or "",
                str(record["venue"] or "").strip(),
                str(record["going"] or "").strip(),
                str(record["running_pos"] or "").strip(),
            )
    return out


def _best_seconds_by_horse(
    conn: sqlite3.Connection, horse_ids: list[str], distance_m: int
) -> dict[str, float]:
    """各馬在指定途程的最快完成秒數。"""
    return {
        hid: info[0]
        for hid, info in _best_speed_info_by_horse(conn, horse_ids, distance_m).items()
    }


def _collect_starter_horse_ids(
    conn: sqlite3.Connection, starters: list[sqlite3.Row]
) -> tuple[list[str], list[str]]:
    """回傳 (每位 starter 對應的主 horse_id, 全部可查詢 id 清單)。"""
    id_list: list[str] = []
    starter_ids: list[str] = []
    for starter in starters:
        hid = str(starter["horse_id"] or "").strip()
        starter_ids.append(hid)
        if hid and hid not in id_list:
            id_list.append(hid)
        name = str(starter["horse_name"] or "").strip()
        if name:
            horses = _resolve_horses(conn, name)
            exact, _all = _pick_horse(horses, name)
            if exact:
                for item in _horse_ids(exact):
                    if item not in id_list:
                        id_list.append(item)
    return starter_ids, id_list


def _is_scratched_starter(starter: sqlite3.Row) -> bool:
    """退出／劃出馬匹（名稱含「退出」等）。"""
    name = str(starter["horse_name"] or "").strip()
    return bool(re.search(r"退出|劃出|刪除", name))


def _resolve_best_for_starter(
    conn: sqlite3.Connection,
    starter: sqlite3.Row,
    hid: str,
    best: dict[str, BestSpeedInfo],
) -> tuple[str, BestSpeedInfo | None]:
    """為一匹排位馬找到最佳速度紀錄與實際 horse_id。"""
    info = best.get(hid) if hid else None
    resolved = hid
    if info is None:
        name = str(starter["horse_name"] or "").strip()
        if name:
            horses = _resolve_horses(conn, name)
            exact, _all = _pick_horse(horses, name)
            if exact:
                for item in _horse_ids(exact):
                    if item in best:
                        return item, best[item]
    return resolved, info


def _speed_places_for_starters(
    conn: sqlite3.Connection,
    starters: list[sqlite3.Row],
    distance_m: int,
) -> dict[str, int]:
    """
    以 horse_id 為鍵回傳同途程最快速度名次（1 起）。
    無時間者不入榜；並列時共用名次（與速度列表相同邏輯）。
    """
    starter_ids, id_list = _collect_starter_horse_ids(conn, starters)
    best = _best_speed_info_by_horse(conn, id_list, distance_m)
    timed: list[tuple[float, str]] = []
    for starter, hid in zip(starters, starter_ids, strict=False):
        resolved, info = _resolve_best_for_starter(conn, starter, hid, best)
        if info is None or not resolved:
            continue
        timed.append((info[0], resolved))
    timed.sort(key=lambda item: item[0])

    places: dict[str, int] = {}
    rank = 0
    last_sec: float | None = None
    for index, (seconds, hid) in enumerate(timed, start=1):
        if last_sec is None or abs(seconds - last_sec) > 1e-6:
            rank = index
            last_sec = seconds
        places[hid] = rank
    return places


_SCORE_COLUMN_ALIAS: dict[str, str] = {
    "馬匹名稱": "馬匹名稱",
    "馬名": "馬匹名稱",
    "名稱": "馬匹名稱",
    "練馬師": "練馬師",
    "騎師": "騎師",
    "日期": "日期",
    "最快日期": "日期",
    "速度": "速度",
    "速度(m/s)": "速度",
    "最快時間": "最快時間",
    # 「時間」易與「今天時間」混淆；單獨「時間」仍指最快時間
    "負磅": "今天負磅",
    "磅": "今天負磅",
    "今天負磅": "今天負磅",
    "今日負磅": "今天負磅",
    "當天負磅": "當天負磅",
    "當日負磅": "當天負磅",
    "實際負磅": "當天負磅",
    "預測賠率": "預測賠率",
    "即時賠率": "即時賠率",
    "賠率": "即時賠率",
    "獨贏賠率": "即時賠率",
    "賽果賠率": "即時賠率",
    "排位賠率": "即時賠率",
    "當天賠率": "當天賠率",
    "當日賠率": "當天賠率",
    "當天獨贏賠率": "當天賠率",
    "即時位置賠率": "即時位置賠率",
    "位置賠率": "即時位置賠率",
    "今天時間": "今天時間",
    "今日時間": "今天時間",
    "完成時間": "今天時間",
    "合計分數": "合計分數",
    "總分數": "合計分數",
    # 單獨「分數」易與「練馬師分數」等混淆，不在此對應
    "練馬師勝場": "練馬師分數",
    "練馬師分數": "練馬師分數",
    "練馬師得分": "練馬師分數",
    "騎師勝場": "騎師分數",
    "騎師分數": "騎師分數",
    "騎師得分": "騎師分數",
    "速度分": "速度分數",
    "速度分數": "速度分數",
    "速度得分": "速度分數",
    "速度名次": "速度名次",
    "名次": "今天名次",
    "賽果名次": "今天名次",
    "今天名次": "今天名次",
    "今日名次": "今天名次",
    "當天名次": "當天名次",
    "當日名次": "當天名次",
    "排名": "排名",
    "場次": "場次",
    "場": "場次",
    "馬號": "馬號",
    "檔位": "今天檔位",
    "今天檔位": "今天檔位",
    "今日檔位": "今天檔位",
    "當天檔位": "當天檔位",
    "當日檔位": "當天檔位",
    "場地": "場地",
    "馬場": "場地",
    "賽場": "場地",
    "場地狀況": "場地狀況",
    "地況": "場地狀況",
    "評分": "評分",
    "走位": "今天走位",
    "沿途走位": "今天走位",
    "今天走位": "今天走位",
    "今日走位": "今天走位",
    "當天走位": "當天走位",
    "當日走位": "當天走位",
    "負磅分數": "負磅分數",
}


def _normalize_person_name(name: str) -> str:
    """去掉騎師減磅標記，如『袁幸堯 (-7)』→『袁幸堯』。"""
    s = (name or "").strip()
    return re.sub(r"\s*[\(（]\s*-?\d+\s*[\)）]\s*$", "", s).strip()


def _wants_each_condition_scores(question: str) -> bool:
    """指令要求顯示每一計分條件的分數。"""
    return bool(
        re.search(r"每一條件|各條件|每項條件|條件的分數|各項分數|分項分數", question)
        or re.search(r"顯示.{0,6}(條件|分項).{0,4}分數", question)
    )


def _inject_condition_score_columns(
    columns: list[str],
    *,
    include_speed: bool,
    include_handicap_bonus: bool = False,
) -> list[str]:
    """在合計分數前插入各條件分數欄（若尚未列入）。"""
    # 顯示名稱用「…分數」；同時相容舊稱「…勝場／速度分」
    extras = ["練馬師分數", "騎師分數"]
    if include_speed:
        extras.append("速度分數")
    if include_handicap_bonus:
        extras.append("負磅分數")
    # 若指令已用舊欄名，不再重複插入
    legacy = {
        "練馬師分數": "練馬師勝場",
        "騎師分數": "騎師勝場",
        "速度分數": "速度分",
    }
    out = list(columns)
    for col in extras:
        if col in out or legacy.get(col, "") in out:
            continue
        if "合計分數" in out:
            out.insert(out.index("合計分數"), col)
        else:
            out.append(col)
    return out


def _parse_score_display_columns(question: str) -> list[str] | None:
    """
    從「排列出 A/B/C」或「列出 A、B、C」解析要顯示的欄位順序。
    欄位增刪會完全依指令內容；找不到明確清單時回傳 None。
    """
    blob = ""
    # 取「排列出／列出…」之後到換行或句號為止（勿用字元白名單，否則新欄位如「負磅」會被截斷）
    matched = re.search(
        r"(?:排列出|列出|顯示|輸出)\s*(.+?)(?=\n|$|。|．|\.|；|;)",
        question,
        flags=re.DOTALL,
    )
    if matched:
        blob = matched.group(1).strip()
    else:
        matched = re.search(
            r"(馬匹名稱|馬名)\s*/\s*(練馬師)\s*/\s*(騎師)(?:\s*/\s*[^\n。．.；;]+)?",
            question,
        )
        if matched:
            blob = matched.group(0).strip()
    if not blob:
        return None

    # 去掉開頭贅詞
    blob = re.sub(r"^(?:由大至小|由小至大|按分數)?\s*", "", blob)
    parts = re.split(r"[/／、，,]+", blob)
    cols: list[str] = []
    for part in parts:
        key = part.strip()
        if not key:
            continue
        # 去掉尾隨「等」「為準」等
        key = re.sub(r"(等|為準|欄位)$", "", key).strip()
        mapped = _SCORE_COLUMN_ALIAS.get(key)
        if mapped is None:
            # 容忍「馬匹名稱 」等空白；也允許未知欄位原樣顯示（資料缺則 —）
            mapped = _SCORE_COLUMN_ALIAS.get(key.replace(" ", ""))
        if mapped is None and key in _SCORE_COLUMN_ALIAS.values():
            mapped = key
        if mapped is None and len(key) <= 8 and re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9()（）]+", key):
            mapped = key
        if mapped and mapped not in cols:
            cols.append(mapped)
    if "馬匹名稱" in cols or "合計分數" in cols:
        if "合計分數" not in cols and any(w in question for w in ("總分數", "合計分數", "分數")):
            cols.append("合計分數")
        return cols
    return None


def _parse_date_in_question(question: str) -> str | None:
    """從問題取出 YYYY-MM-DD（支援 2026/10/14、2026-10-14 等）。"""
    matched = re.search(
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
        question,
    )
    if not matched:
        return None
    key = normalize_date_key(matched.group(1))
    return key or None


def _racecard_meetings_all(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """排位賽日清單（race_info ∪ starters）。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for source in (_racecard_dates(conn),):
        for item in source:
            if item not in seen and item[0]:
                seen.add(item)
                out.append(item)
    try:
        rows = conn.execute(
            "SELECT DISTINCT race_date, racecourse FROM racecard_starters;"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        item = (str(row["race_date"] or ""), str(row["racecourse"] or ""))
        if item[0] and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _latest_racecard_meeting(
    conn: sqlite3.Connection, venue: str | None = None
) -> tuple[str, str] | None:
    meetings = _racecard_meetings_all(conn)
    if not meetings:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    today_hits = [
        item
        for item in meetings
        if normalize_date_key(item[0]) == today and (not venue or item[1] == venue)
    ]
    if today_hits:
        return today_hits[-1]
    best: tuple[str, str] | None = None
    best_key = ""
    for raw_date, course in meetings:
        if venue and course != venue:
            continue
        key = normalize_date_key(raw_date)
        if key >= best_key:
            best_key = key
            best = (raw_date, course)
    return best


def _racecard_meeting_for_date(
    conn: sqlite3.Connection,
    date_key: str,
    venue: str | None = None,
) -> tuple[str, str] | None:
    """依正規化日期（YYYY-MM-DD）找排位賽日＋場地。"""
    meetings = _racecard_meetings_all(conn)
    hits = [
        item
        for item in meetings
        if normalize_date_key(item[0]) == date_key and (not venue or item[1] == venue)
    ]
    if not hits:
        return None
    # 同一日多場地時：有指定場地已篩過；否則優先 ST，再取最後一筆
    if venue:
        return hits[-1]
    st = [item for item in hits if item[1] == "ST"]
    if st:
        return st[-1]
    return hits[-1]


def _unique_day_finishes(
    conn: sqlite3.Connection, date_key: str
) -> list[tuple[str, str, str, str, str]]:
    """
    指定賽日有名次的列（已去重）。
    回傳 [(jockey, trainer, horse_id, venue, finish_pos), ...]。
    同一馬同一場地同日只計一次（避免往績 race_no 與賽果 race_no 重複入庫）。
    """
    raw_dates = _dates_for_key(conn, date_key)
    if not raw_dates:
        return []
    marks = ",".join("?" * len(raw_dates))
    rows = conn.execute(
        f"""
        SELECT jockey, trainer, horse_id, venue, finish_pos
        FROM race_records
        WHERE race_date IN ({marks});
        """,
        raw_dates,
    ).fetchall()
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        if _finish_number(row["finish_pos"]) is None:
            continue
        jockey = _normalize_person_name(str(row["jockey"] or ""))
        trainer = _normalize_person_name(str(row["trainer"] or ""))
        hid = str(row["horse_id"] or "").strip()
        venue = str(row["venue"] or "").strip().upper()
        finish = str(row["finish_pos"] or "").strip()
        # 以馬＋場地去重；無 horse_id 時退回人＋場地＋騎／練組合避免全吞
        dedupe_key = (hid or f"{jockey}|{trainer}", venue, hid or jockey or trainer)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append((jockey, trainer, hid, venue, finish))
    return out


def _unique_day_wins(
    conn: sqlite3.Connection, date_key: str
) -> list[tuple[str, str, str, str]]:
    """指定賽日的勝出列（已去重）。回傳 [(jockey, trainer, horse_id, venue), ...]。"""
    return [
        (j, t, h, v)
        for j, t, h, v, finish in _unique_day_finishes(conn, date_key)
        if _is_win(finish)
    ]


def _person_win_counts_on_date(
    conn: sqlite3.Connection, date_key: str
) -> tuple[dict[str, int], dict[str, int]]:
    """回傳 (騎師勝場, 練馬師勝場)，以今天／指定賽日 race_records 勝出計算。"""
    return _person_score_counts_on_date(conn, date_key, win_pts=1, place_pts=0)


def _person_score_counts_on_date(
    conn: sqlite3.Connection,
    date_key: str,
    *,
    win_pts: int = 1,
    place_pts: int = 0,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    回傳 (騎師分數, 練馬師分數)。
    贏馬累加 win_pts、入位（2–3 名）累加 place_pts。
    """
    jockey_scores: dict[str, int] = {}
    trainer_scores: dict[str, int] = {}
    for jockey, trainer, _hid, _venue, finish in _unique_day_finishes(conn, date_key):
        pts = 0
        if _is_win(finish):
            pts = win_pts
        elif place_pts and _is_place_finish(finish, top_n=3):
            pts = place_pts
        if pts <= 0:
            continue
        if jockey:
            jockey_scores[jockey] = jockey_scores.get(jockey, 0) + pts
        if trainer:
            trainer_scores[trainer] = trainer_scores.get(trainer, 0) + pts
    return jockey_scores, trainer_scores


def _is_list_racecard_results_question(question: str) -> bool:
    """列出／顯示某場「排位」與「賽馬結果」。"""
    if re.search(r"下載|更新", question):
        return False
    has_rc = "排位" in question
    has_res = bool(re.search(r"賽馬結果|賽果", question))
    return has_rc and has_res


def _fmt_display_date(raw: str) -> str:
    from .web_utils import normalize_date_yyyy_mm_dd

    return normalize_date_yyyy_mm_dd(raw) or (raw or "")


def _fmt99(raw: object) -> str:
    from .web_utils import format_two_digit_99

    return format_two_digit_99(raw)


def _results_meeting_for_date(
    conn: sqlite3.Connection,
    date_key: str,
    venue: str | None = None,
) -> tuple[str, str] | None:
    """從 race_records 找 (race_date, venue) 對應指定日期。"""
    from .race_results import infer_venue_code
    from .web_utils import normalize_date_yyyy_mm_dd

    try:
        rows = conn.execute(
            """
            SELECT race_date, venue, course
            FROM race_records
            WHERE race_date GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        rd = normalize_date_yyyy_mm_dd(str(row["race_date"] or ""))
        if normalize_date_key(rd) != date_key:
            continue
        code = infer_venue_code(str(row["venue"] or ""), str(row["course"] or ""))
        if not code:
            continue
        if venue and code != venue:
            continue
        key = (rd, code)
        if key not in seen:
            seen.add(key)
            hits.append(key)
    if not hits:
        return None
    st = [h for h in hits if h[1] == "ST"]
    if st and not venue:
        return st[0]
    return hits[0]


def _list_racecard_and_results(conn: sqlite3.Connection, question: str) -> AiAnswer:
    """列出指定賽日某場的排位，並併入該場賽馬結果。"""
    race_no = _parse_race_no(question)
    if race_no is None:
        return AiAnswer("請指定場次，例如「列出 2026/10/14 第五場的排位及賽馬結果」。")
    venue = _parse_venue(question) or None
    asked_date_key = _parse_date_in_question(question)
    race_date = ""
    racecourse = ""
    if asked_date_key:
        meeting = _racecard_meeting_for_date(conn, asked_date_key, venue)
        if meeting is None:
            meeting = _results_meeting_for_date(conn, asked_date_key, venue)
        if meeting is None:
            return AiAnswer(
                f"找不到 {_fmt_display_date(asked_date_key.replace('-', '/'))} 的排位或賽馬結果，"
                "請確認已下載該日資料。"
            )
        race_date, racecourse = meeting
    else:
        meeting = _latest_racecard_meeting(conn, venue)
        if meeting is None:
            return AiAnswer("資料庫尚無排位資料，請先下載排位，或在指令中寫明日期（yyyy/MM/dd）。")
        race_date, racecourse = meeting

    date_label = _fmt_display_date(race_date)
    venue_label = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}.get(racecourse, racecourse)

    starters: list[sqlite3.Row] = []
    try:
        starters = list(
            conn.execute(
                """
                SELECT COALESCE("馬名", '') AS horse_name,
                       COALESCE("練馬師", '') AS trainer,
                       COALESCE("騎師", '') AS jockey,
                       COALESCE("馬號", '') AS horse_no,
                       COALESCE("負磅", '') AS handicap,
                       COALESCE("檔位", '') AS draw,
                       COALESCE("評分", '') AS rating,
                       COALESCE("即時賠率", COALESCE("預測賠率", '')) AS card_odds,
                       COALESCE(horse_id, '') AS horse_id,
                       COALESCE(後備, '否') AS reserve
                FROM racecard_starters
                WHERE race_date = ?
                  AND racecourse = ?
                  AND race_no = ?
                  AND COALESCE(後備, '否') = '否'
                ORDER BY CAST(COALESCE("馬號", '0') AS INTEGER), id;
                """,
                (race_date, racecourse, race_no),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        # 舊庫可能沒有即時／預測賠率欄
        try:
            starters = list(
                conn.execute(
                    """
                    SELECT COALESCE("馬名", '') AS horse_name,
                           COALESCE("練馬師", '') AS trainer,
                           COALESCE("騎師", '') AS jockey,
                           COALESCE("馬號", '') AS horse_no,
                           COALESCE("負磅", '') AS handicap,
                           COALESCE("檔位", '') AS draw,
                           COALESCE("評分", '') AS rating,
                           '' AS card_odds,
                           COALESCE(horse_id, '') AS horse_id,
                           COALESCE(後備, '否') AS reserve
                    FROM racecard_starters
                    WHERE race_date = ?
                      AND racecourse = ?
                      AND race_no = ?
                      AND COALESCE(後備, '否') = '否'
                    ORDER BY CAST(COALESCE("馬號", '0') AS INTEGER), id;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchall()
            )
        except sqlite3.OperationalError:
            starters = []

    result_by_hid: dict[str, sqlite3.Row] = {}
    result_rows: list[sqlite3.Row] = []
    try:
        result_rows = list(
            conn.execute(
                """
                SELECT horse_id, finish_pos, draw, jockey, trainer, time, win_odds, distance,
                       running_pos
                FROM race_records
                WHERE race_date = ?
                  AND UPPER(TRIM(venue)) = ?
                  AND CAST(race_no AS INTEGER) = ?
                ORDER BY CAST(finish_pos AS INTEGER), id;
                """,
                (date_label if date_label else race_date, racecourse, race_no),
            ).fetchall()
        )
        # 若 race_date 字串與正規化後不同，再試原始 race_date
        if not result_rows and race_date != date_label:
            result_rows = list(
                conn.execute(
                    """
                    SELECT horse_id, finish_pos, draw, jockey, trainer, time, win_odds, distance,
                           running_pos
                    FROM race_records
                    WHERE race_date = ?
                      AND UPPER(TRIM(venue)) = ?
                      AND CAST(race_no AS INTEGER) = ?
                    ORDER BY CAST(finish_pos AS INTEGER), id;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchall()
            )
    except sqlite3.OperationalError:
        result_rows = []

    for row in result_rows:
        hid = str(row["horse_id"] or "").strip()
        if hid:
            result_by_hid[hid] = row

    columns = [
        "馬號",
        "馬名",
        "檔位",
        "騎師",
        "練馬師",
        "負磅",
        "評分",
        "排位賠率",
        "名次",
        "完成時間",
        "賽果賠率",
        "途程",
    ]
    rows: list[list[str]] = []
    used_hids: set[str] = set()

    if starters:
        for s in starters:
            hid = str(s["horse_id"] or "").strip()
            res = result_by_hid.get(hid) if hid else None
            if res is not None and hid:
                used_hids.add(hid)
            rows.append(
                [
                    _fmt99(s["horse_no"]),
                    str(s["horse_name"] or "").strip() or "—",
                    _fmt99(s["draw"]) or "—",
                    str(s["jockey"] or "").strip() or "—",
                    str(s["trainer"] or "").strip() or "—",
                    str(s["handicap"] or "").strip() or "—",
                    str(s["rating"] or "").strip() or "—",
                    str(s["card_odds"] or "").strip() or "—",
                    _fmt99(res["finish_pos"]) if res else "—",
                    str(res["time"] or "").strip() if res else "—",
                    str(res["win_odds"] or "").strip() if res else "—",
                    str(res["distance"] or "").strip() if res else "—",
                ]
            )
        # 排位沒對上、但仍有賽果的馬
        for hid, res in result_by_hid.items():
            if hid in used_hids:
                continue
            name = ""
            try:
                hr = conn.execute(
                    "SELECT name_zh FROM horses WHERE horse_id = ? LIMIT 1", (hid,)
                ).fetchone()
                if hr:
                    name = str(hr["name_zh"] or "")
            except sqlite3.OperationalError:
                name = ""
            rows.append(
                [
                    "—",
                    name or hid,
                    _fmt99(res["draw"]) or "—",
                    str(res["jockey"] or "").strip() or "—",
                    str(res["trainer"] or "").strip() or "—",
                    "—",
                    "—",
                    "—",
                    _fmt99(res["finish_pos"]) or "—",
                    str(res["time"] or "").strip() or "—",
                    str(res["win_odds"] or "").strip() or "—",
                    str(res["distance"] or "").strip() or "—",
                ]
            )
    elif result_rows:
        names = _name_map(conn, [str(r["horse_id"] or "") for r in result_rows])
        for res in result_rows:
            hid = str(res["horse_id"] or "")
            rows.append(
                [
                    "—",
                    names.get(hid, hid) or "—",
                    _fmt99(res["draw"]) or "—",
                    str(res["jockey"] or "").strip() or "—",
                    str(res["trainer"] or "").strip() or "—",
                    "—",
                    "—",
                    "—",
                    _fmt99(res["finish_pos"]) or "—",
                    str(res["time"] or "").strip() or "—",
                    str(res["win_odds"] or "").strip() or "—",
                    str(res["distance"] or "").strip() or "—",
                ]
            )
    else:
        return AiAnswer(
            f"{date_label} {venue_label}（{racecourse}）第{race_no}場：找不到排位或賽馬結果。"
        )

    sort_by_finish = bool(
        re.search(r"(按|依|以|照)\s*名次\s*(排列|排序|排|由小到大)?", question)
        or re.search(r"名次\s*(排列|排序)", question)
    )
    sort_note = ""
    if sort_by_finish:
        # 名次欄 index=8；無賽果（—／非數字）排最後，其餘由小到大
        def _finish_sort_key(row: list[str]) -> tuple[int, int, str]:
            pos = str(row[8] if len(row) > 8 else "").strip()
            if not pos or pos in {"—", "-", "---"} or not pos.isdigit():
                return (1, 999, str(row[0] if row else ""))
            return (0, int(pos), str(row[0] if row else ""))

        rows.sort(key=_finish_sort_key)
        sort_note = "｜已按名次排列"

    n_res = sum(1 for r in rows if r[8] != "—")
    return AiAnswer(
        f"{date_label} {venue_label}（{racecourse}）第{race_no}場｜"
        f"排位 {len(starters) if starters else 0} 匹｜已對上賽果 {n_res} 匹{sort_note}。",
        columns=columns,
        rows=rows,
    )


def _race_distance_m(
    conn: sqlite3.Connection, race_date: str, racecourse: str, race_no: int
) -> int | None:
    try:
        row = conn.execute(
            """
            SELECT distance FROM racecard_race_info
            WHERE race_date = ? AND racecourse = ? AND race_no = ?
            LIMIT 1;
            """,
            (race_date, racecourse, race_no),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return parse_distance_m(str(row["distance"] or ""))


def _score_starters_for_race(
    conn: sqlite3.Connection,
    *,
    race_date: str,
    racecourse: str,
    race_no: int,
    jockey_wins: dict[str, int],
    trainer_wins: dict[str, int],
    include_speed: bool,
    handicap_bonus: tuple[int, int, int] | None = None,
) -> tuple[list[dict[str, str | int]], int | None]:
    """計算單場正選馬合計分數列；回傳 (scored_rows, distance_m)。"""
    from .web_utils import format_two_digit_99, normalize_date_yyyy_mm_dd

    try:
        starters = list(
            conn.execute(
                """
                SELECT COALESCE("馬名", '') AS horse_name,
                       COALESCE("練馬師", '') AS trainer,
                       COALESCE("騎師", '') AS jockey,
                       COALESCE("馬號", '') AS horse_no,
                       COALESCE("負磅", '') AS handicap,
                       COALESCE("檔位", '') AS draw,
                       COALESCE("評分", '') AS rating,
                       COALESCE("即時賠率", '') AS live_odds,
                       COALESCE("預測賠率", '') AS pred_odds,
                       COALESCE("即時位置賠率", '') AS place_odds,
                       COALESCE(horse_id, '') AS horse_id,
                       COALESCE(後備, '否') AS reserve
                FROM racecard_starters
                WHERE race_date = ?
                  AND racecourse = ?
                  AND race_no = ?
                  AND COALESCE(後備, '否') = '否'
                ORDER BY CAST(COALESCE("馬號", '0') AS INTEGER), id;
                """,
                (race_date, racecourse, race_no),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        try:
            starters = list(
                conn.execute(
                    """
                    SELECT COALESCE("馬名", '') AS horse_name,
                           COALESCE("練馬師", '') AS trainer,
                           COALESCE("騎師", '') AS jockey,
                           COALESCE("馬號", '') AS horse_no,
                           COALESCE("負磅", '') AS handicap,
                           COALESCE("檔位", '') AS draw,
                           COALESCE("評分", '') AS rating,
                           COALESCE("即時賠率", '') AS live_odds,
                           COALESCE("預測賠率", '') AS pred_odds,
                           '' AS place_odds,
                           COALESCE(horse_id, '') AS horse_id,
                           COALESCE(後備, '否') AS reserve
                    FROM racecard_starters
                    WHERE race_date = ?
                      AND racecourse = ?
                      AND race_no = ?
                      AND COALESCE(後備, '否') = '否'
                    ORDER BY CAST(COALESCE("馬號", '0') AS INTEGER), id;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchall()
            )
        except sqlite3.OperationalError:
            return [], None
    # 退出馬匹不計分、不顯示
    starters = [s for s in starters if not _is_scratched_starter(s)]
    if not starters:
        return [], _race_distance_m(conn, race_date, racecourse, race_no)

    distance_m = _race_distance_m(conn, race_date, racecourse, race_no) if include_speed else None
    speed_places: dict[str, int] = {}
    best_info: dict[str, BestSpeedInfo] = {}
    if include_speed and distance_m:
        starter_ids, id_list = _collect_starter_horse_ids(conn, starters)
        best_info = _best_speed_info_by_horse(conn, id_list, distance_m)
        speed_places = _speed_places_for_starters(conn, starters, distance_m)
    else:
        starter_ids = [str(s["horse_id"] or "").strip() for s in starters]

    finish_by_hid: dict[str, str] = {}
    odds_by_hid: dict[str, str] = {}
    time_by_hid: dict[str, str] = {}
    place_odds_by_hid: dict[str, str] = {}
    draw_by_hid: dict[str, str] = {}
    running_by_hid: dict[str, str] = {}
    going_by_hid: dict[str, str] = {}
    rd_norm = normalize_date_yyyy_mm_dd(race_date) or race_date
    from .race_results import _season_race_to_meeting_map

    try:
        day_rows = list(
            conn.execute(
                """
                SELECT horse_id, race_no, finish_pos, win_odds, time, draw,
                       COALESCE(place_odds, '') AS place_odds,
                       COALESCE(running_pos, '') AS running_pos,
                       COALESCE(going, '') AS going
                FROM race_records
                WHERE (race_date = ? OR race_date = ?)
                  AND UPPER(TRIM(venue)) = ?
                """,
                (race_date, rd_norm, racecourse),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        try:
            day_rows = list(
                conn.execute(
                    """
                    SELECT horse_id, race_no, finish_pos, win_odds, time, draw,
                           '' AS place_odds,
                           COALESCE(running_pos, '') AS running_pos,
                           COALESCE(going, '') AS going
                    FROM race_records
                    WHERE (race_date = ? OR race_date = ?)
                      AND UPPER(TRIM(venue)) = ?
                    """,
                    (race_date, rd_norm, racecourse),
                ).fetchall()
            )
        except sqlite3.OperationalError:
            day_rows = []
    season_map = _season_race_to_meeting_map(
        [str(fr["race_no"] or "").strip() for fr in day_rows]
    )
    for fr in day_rows:
        meeting_no = _meeting_race_no_int(fr["race_no"], season_map)
        if meeting_no is None or meeting_no != race_no:
            continue
        hid_f = str(fr["horse_id"] or "").strip()
        if not hid_f:
            continue
        finish_by_hid[hid_f] = format_two_digit_99(fr["finish_pos"]) or "—"
        wo = str(fr["win_odds"] or "").strip()
        if wo:
            odds_by_hid[hid_f] = wo
        tm = str(fr["time"] or "").strip()
        if tm:
            time_by_hid[hid_f] = tm
        po = str(fr["place_odds"] or "").strip()
        if po:
            place_odds_by_hid[hid_f] = po
        dw = format_two_digit_99(fr["draw"]) or ""
        if dw:
            draw_by_hid[hid_f] = dw
        rp = str(fr["running_pos"] or "").strip()
        if rp:
            running_by_hid[hid_f] = rp
        gg = str(fr["going"] or "").strip()
        if gg:
            going_by_hid[hid_f] = gg

    scored: list[dict[str, str | int]] = []
    for row, hid0 in zip(starters, starter_ids, strict=False):
        name = str(row["horse_name"] or "").strip() or "（無名）"
        trainer_raw = str(row["trainer"] or "").strip() or "（空白）"
        jockey_raw = str(row["jockey"] or "").strip() or "（空白）"
        trainer_key = _normalize_person_name(trainer_raw) or trainer_raw
        jockey_key = _normalize_person_name(jockey_raw) or jockey_raw
        hid = hid0
        resolved, info = (
            _resolve_best_for_starter(conn, row, hid, best_info)
            if include_speed
            else (hid, None)
        )
        if resolved:
            hid = resolved
        t_wins = trainer_wins.get(trainer_key, 0)
        j_wins = jockey_wins.get(jockey_key, 0)
        place = speed_places.get(hid) if hid else None
        s_pts = _speed_place_points(place) if include_speed else 0
        horse_no = 0
        try:
            horse_no = int(str(row["horse_no"] or "0").strip() or "0")
        except ValueError:
            horse_no = 0
        seconds = info[0] if info else None
        best_date = "—"
        if info and info[2]:
            best_date = normalize_date_yyyy_mm_dd(info[2]) or str(info[2])
        day_handicap = (info[3] if info and len(info) > 3 else "") or "—"
        day_finish = (info[4] if info and len(info) > 4 else "") or "—"
        day_odds = (info[5] if info and len(info) > 5 else "") or "—"
        day_draw = (info[6] if info and len(info) > 6 else "") or "—"
        day_venue = _venue_display(info[7] if info and len(info) > 7 else "")
        day_going = (info[8] if info and len(info) > 8 else "") or "—"
        day_running = (info[9] if info and len(info) > 9 else "") or "—"
        today_handicap = str(row["handicap"] or "").strip() or "—"
        today_draw = str(row["draw"] or "").strip() or "—"
        if today_draw == "—":
            if hid and hid in draw_by_hid:
                today_draw = draw_by_hid[hid]
            elif hid0 and hid0 in draw_by_hid:
                today_draw = draw_by_hid[hid0]
        today_finish = "—"
        today_time = "—"
        today_running = "—"
        if hid and hid in finish_by_hid:
            today_finish = finish_by_hid[hid]
        elif hid0 and hid0 in finish_by_hid:
            today_finish = finish_by_hid[hid0]
        if hid and hid in time_by_hid:
            today_time = time_by_hid[hid]
        elif hid0 and hid0 in time_by_hid:
            today_time = time_by_hid[hid0]
        if hid and hid in running_by_hid:
            today_running = running_by_hid[hid]
        elif hid0 and hid0 in running_by_hid:
            today_running = running_by_hid[hid0]
        # 場地狀況：優先最快速度當日；否則用今日賽果
        going_val = day_going if day_going and day_going != "—" else "—"
        if going_val == "—":
            if hid and hid in going_by_hid:
                going_val = going_by_hid[hid]
            elif hid0 and hid0 in going_by_hid:
                going_val = going_by_hid[hid0]
        live = str(row["live_odds"] or "").strip()
        pred = str(row["pred_odds"] or "").strip()
        live_place = ""
        try:
            live_place = str(row["place_odds"] or "").strip()
        except (KeyError, IndexError):
            live_place = ""
        result_odds = odds_by_hid.get(hid, "") if hid else ""
        result_place = ""
        if hid and hid in place_odds_by_hid:
            result_place = place_odds_by_hid[hid]
        elif hid0 and hid0 in place_odds_by_hid:
            result_place = place_odds_by_hid[hid0]
        live_val = live or result_odds or "—"
        pred_val = pred or "—"
        place_val = live_place or result_place or "—"
        h_pts = 0
        if handicap_bonus is not None:
            thr, top_n, bonus = handicap_bonus
            hc = _parse_handicap_int(today_handicap)
            fn = _finish_number(today_finish)
            if hc is not None and fn is not None and hc >= thr and 1 <= fn <= top_n:
                h_pts = bonus
        total = t_wins + j_wins + s_pts + h_pts
        scored.append(
            {
                "total": total,
                "horse_no": horse_no,
                "場次": f"{race_no:02d}",
                "race_no_int": race_no,
                "馬匹名稱": name,
                "練馬師": trainer_raw,
                "騎師": jockey_raw,
                "馬號": str(row["horse_no"] or "").strip(),
                "今天負磅": today_handicap,
                "當天負磅": day_handicap,
                "負磅": today_handicap,
                "檔位": today_draw,
                "今天檔位": today_draw,
                "當天檔位": day_draw if day_draw else "—",
                "場地": day_venue,
                "場地狀況": going_val,
                "當天走位": day_running if day_running else "—",
                "今天走位": today_running,
                "走位": today_running,
                "評分": str(row["rating"] or "").strip() or "—",
                "當天賠率": day_odds if day_odds else "—",
                "預測賠率": pred_val,
                "即時賠率": live_val,
                "即時位置賠率": place_val,
                "賠率": live_val if live_val != "—" else pred_val,
                "練馬師分數": t_wins,
                "騎師分數": j_wins,
                "速度分數": s_pts,
                "負磅分數": h_pts,
                "練馬師勝場": t_wins,
                "騎師勝場": j_wins,
                "速度分": s_pts,
                "速度名次": place if place else "—",
                "日期": best_date,
                "最快時間": (info[1] if info else "—"),
                "今天時間": today_time,
                "速度": _fmt_speed(distance_m, seconds) if include_speed else "—",
                "當天名次": day_finish,
                "今天名次": today_finish,
                "名次": today_finish,
                "合計分數": total,
            }
        )
    return scored, distance_m


def _race_combined_win_score(conn: sqlite3.Connection, question: str) -> AiAnswer:
    """
    指定場次（可為第1場至第8場等多場／每一場）每匹馬合計分數（由大至小）：
    1) 該賽日練馬師贏馬次數
    2) 該賽日騎師贏馬次數
    3)（若問題提到速度／途程計分）相同途程最佳速度名次分
    """
    venue = _parse_venue(question) or None
    asked_date_key = _parse_date_in_question(question)
    if asked_date_key:
        meeting = _racecard_meeting_for_date(conn, asked_date_key, venue)
        if meeting is None:
            return AiAnswer(
                f"排位中找不到 {asked_date_key.replace('-', '/')} 的賽日資料"
                + (f"（場地 {venue}）" if venue else "")
                + "，請確認已下載該日排位。"
            )
    else:
        meeting = _latest_racecard_meeting(conn, venue)
    if meeting is None:
        return AiAnswer("資料庫尚無排位資料，請先下載排位後再計算合計分數。")
    race_date, racecourse = meeting
    date_key = normalize_date_key(race_date) or datetime.now().strftime("%Y-%m-%d")
    date_label = race_date
    if asked_date_key:
        date_label = asked_date_key.replace("-", "/")
    elif date_key == datetime.now().strftime("%Y-%m-%d"):
        date_label = f"今天（{race_date}）"
    race_nos = _parse_race_nos(question)
    if not race_nos or _wants_all_races(question):
        race_nos = _meeting_race_nos(conn, race_date, racecourse)
    if not race_nos:
        return AiAnswer(
            f"排位中找不到 {race_date} {racecourse} 的場次，請確認已下載排位。"
        )
    include_speed = _wants_speed_rank_points(question) or (
        ("速度" in question or "最快" in question)
        and any(word in question for word in ("分", "計分", "總分", "合計", "條件"))
    )
    want_cols = _parse_score_display_columns(question)
    if want_cols and any(
        c in want_cols for c in ("日期", "速度", "最快時間", "速度分", "速度名次", "速度分數")
    ):
        include_speed = True
    win_pts, place_pts = _parse_person_point_weights(question)
    handicap_bonus = _parse_handicap_topn_bonus(question)

    jockey_wins, trainer_wins = _person_score_counts_on_date(
        conn, date_key, win_pts=win_pts, place_pts=place_pts
    )
    scored: list[dict[str, str | int]] = []
    missing: list[int] = []
    distances: list[str] = []
    for race_no in race_nos:
        part, distance_m = _score_starters_for_race(
            conn,
            race_date=race_date,
            racecourse=racecourse,
            race_no=race_no,
            jockey_wins=jockey_wins,
            trainer_wins=trainer_wins,
            include_speed=include_speed,
            handicap_bonus=handicap_bonus,
        )
        if not part:
            missing.append(race_no)
            continue
        if distance_m:
            distances.append(f"第{race_no}場 {distance_m}米")
        scored.extend(part)

    if not scored:
        rng = (
            f"第{race_nos[0]}場至第{race_nos[-1]}場"
            if len(race_nos) > 1
            else f"第{race_nos[0]}場"
        )
        return AiAnswer(
            f"排位中找不到 {race_date} {racecourse} {rng} 的正選馬匹，請確認已下載該場排位。"
        )

    multi = len(race_nos) > 1
    ascending = bool(re.search(r"由小至大|由低至高|升序", question))
    score_sign = 1 if ascending else -1
    scored.sort(
        key=lambda item: (
            int(item.get("race_no_int", 0)),
            score_sign * int(item["total"]),
            int(item["horse_no"]),
            str(item["馬匹名稱"]),
        )
        if multi
        else (
            score_sign * int(item["total"]),
            int(item["horse_no"]),
            str(item["馬匹名稱"]),
        )
    )
    for index, item in enumerate(scored, start=1):
        item["排名"] = index

    if want_cols:
        # 嚴格依指令欄位順序；條件分數插在合計分數前
        columns = list(want_cols)
    elif include_speed:
        columns = ["馬匹名稱", "練馬師", "騎師", "日期", "速度", "合計分數"]
    else:
        columns = ["馬匹名稱", "練馬師", "騎師", "合計分數"]

    columns = _inject_condition_score_columns(
        columns,
        include_speed=include_speed,
        include_handicap_bonus=handicap_bonus is not None,
    )
    # 多場時若指令未寫「場次」，加在最前以便辨識
    if multi and "場次" not in columns:
        columns = ["場次", *columns]

    rows: list[list[str]] = []
    for item in scored:
        rows.append([str(item.get(col, "—")) for col in columns])

    win_day = date_label
    if len(race_nos) == 1:
        race_txt = f"第{race_nos[0]}場"
    else:
        race_txt = f"第{race_nos[0]}場至第{race_nos[-1]}場"
    miss_txt = f"；缺排位：{','.join(f'第{n}場' for n in missing)}" if missing else ""
    dist_txt = "、".join(distances) if distances else "（未找到途程，速度分數以 0 計）"
    person_rule = f"練馬師／騎師：贏馬{win_pts}分"
    if place_pts:
        person_rule += f"、入位{place_pts}分"
    parts = [person_rule]
    if include_speed:
        parts.append("速度分數（同途程最快→4、第2→3、第3→2、第4→1、第5快起→0）")
    if handicap_bonus is not None:
        thr, top_n, bonus = handicap_bonus
        parts.append(f"負磅>={thr}且今日前{top_n}名→{bonus}分")
    score_cols = ["練馬師分數", "騎師分數"]
    if include_speed:
        score_cols.append("速度分數")
    if handicap_bonus is not None:
        score_cols.append("負磅分數")
    text = (
        f"{win_day} {racecourse} {race_txt}"
        + (f"，途程 {dist_txt}" if include_speed else "")
        + f"{miss_txt}。\n"
        f"合計分數 = {' + '.join(score_cols)}（{ '；'.join(parts) }），"
        f"{'各場內' if multi else ''}由大至小排列（共 {len(rows)} 匹）。\n"
        f"各條件分數欄：{'／'.join(score_cols)}。\n"
        f"顯示欄位：{'／'.join(columns)}"
    )
    return AiAnswer(text, columns=columns, rows=rows)


def _racecard_dates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT race_date, racecourse FROM racecard_race_info;"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(r["race_date"] or ""), str(r["racecourse"] or "")) for r in rows]


def _speed_ranking(conn: sqlite3.Connection, question: str) -> AiAnswer:
    race_no = _parse_race_no(question)
    venue = _parse_venue(question)
    date_in_q = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", question)
    meetings = _racecard_dates(conn)
    race_date = ""
    racecourse = venue
    if date_in_q:
        key = normalize_date_key(date_in_q.group(1))
        matched = [item for item in meetings if normalize_date_key(item[0]) == key and (not venue or item[1] == venue)]
        if matched:
            race_date, course = matched[-1]
            racecourse = racecourse or course
        else:
            race_date = date_in_q.group(1)
    elif meetings:
        latest_key = ""
        for raw_date, course in meetings:
            key = normalize_date_key(raw_date)
            if venue and course != venue:
                continue
            if key >= latest_key:
                latest_key = key
                race_date, racecourse = raw_date, course

    distance_m = None
    distance_match = re.search(r"途程\s*(\d{3,4})", question)
    if distance_match:
        distance_m = int(distance_match.group(1))

    starters: list[sqlite3.Row] = []
    if race_date:
        sql = """
            SELECT s."馬號" AS horse_no, s."馬名" AS horse_name, s.horse_id,
                   s.race_no, s.racecourse, s.race_date, i.distance
            FROM racecard_starters s
            LEFT JOIN racecard_race_info i
              ON i.race_date = s.race_date AND i.racecourse = s.racecourse AND i.race_no = s.race_no
            WHERE s.race_date = ?
              AND COALESCE(s.後備, '否') = '否'
        """
        params: list[object] = [race_date]
        if racecourse:
            sql += " AND s.racecourse = ?"
            params.append(racecourse)
        if race_no is not None:
            sql += " AND s.race_no = ?"
            params.append(race_no)
        try:
            starters = list(conn.execute(sql, params).fetchall())
        except sqlite3.OperationalError:
            starters = []

    assumed = ""
    if starters and race_no is None:
        numbers = sorted({int(s["race_no"]) for s in starters if str(s["race_no"] or "").isdigit()})
        if numbers:
            race_no = numbers[0]
            starters = [s for s in starters if int(s["race_no"]) == race_no]
            assumed = f"未指定場次，先用第{race_no}場。"

    if starters and distance_m is None:
        for starter in starters:
            distance_m = parse_distance_m(str(starter["distance"] or ""))
            if distance_m:
                break

    if starters and distance_m:
        result = _speed_for_starters(conn, starters, distance_m, race_date, racecourse, race_no)
        if assumed:
            result.text = assumed + result.text
        return result

    if distance_m is None:
        return AiAnswer("請指定場次（例如「第3場相同途程的最快速度排列」）或寫明途程米數。")
    return _speed_for_distance(conn, distance_m)


def _speed_for_starters(
    conn: sqlite3.Connection,
    starters: list[sqlite3.Row],
    distance_m: int,
    race_date: str,
    racecourse: str,
    race_no: int | None,
) -> AiAnswer:
    id_set: list[str] = []
    for starter in starters:
        hid = str(starter["horse_id"] or "").strip()
        if hid and hid not in id_set:
            id_set.append(hid)
        horses = _resolve_horses(conn, str(starter["horse_name"] or "")) if starter["horse_name"] else []
        exact, _all = _pick_horse(horses, str(starter["horse_name"] or ""))
        if exact:
            for item in _horse_ids(exact):
                if item not in id_set:
                    id_set.append(item)
    records = []
    if id_set:
        marks = ",".join("?" * len(id_set))
        records = list(
            conn.execute(
                f"""
                SELECT horse_id, race_date, venue, race_no, distance, time
                FROM race_records
                WHERE horse_id IN ({marks});
                """,
                id_set,
            ).fetchall()
        )
    by_horse: dict[str, tuple[float, str, str]] = {}
    for record in records:
        if parse_distance_m(str(record["distance"] or "")) != distance_m:
            continue
        seconds = parse_race_time_seconds(str(record["time"] or ""))
        if seconds is None:
            continue
        hid = str(record["horse_id"])
        prev = by_horse.get(hid)
        if prev is None or seconds < prev[0]:
            by_horse[hid] = (seconds, str(record["time"] or ""), str(record["race_date"] or ""))

    ranked_rows = []
    for starter in starters:
        hid = str(starter["horse_id"] or "")
        best = by_horse.get(hid)
        if best is None:
            horses = _resolve_horses(conn, str(starter["horse_name"] or ""))
            exact, _all = _pick_horse(horses, str(starter["horse_name"] or ""))
            if exact:
                for item in _horse_ids(exact):
                    if item in by_horse:
                        best = by_horse[item]
                        break
        seconds = best[0] if best else None
        ranked_rows.append((seconds if seconds is not None else 10**9, starter, best))
    ranked_rows.sort(key=lambda item: item[0])

    rows = []
    rank = 0
    last_sec: float | None = None
    for index, (seconds, starter, best) in enumerate(ranked_rows, start=1):
        if seconds >= 10**9:
            show_rank = "—"
        else:
            if last_sec is None or abs(seconds - last_sec) > 1e-6:
                rank = index
                last_sec = seconds
            show_rank = str(rank)
        rows.append(
            [
                show_rank,
                str(starter["horse_no"] or ""),
                str(starter["horse_name"] or ""),
                best[1] if best else "—",
                _fmt_speed(distance_m, None if seconds >= 10**9 else seconds),
                best[2] if best else "—",
            ]
        )
    where = f"{race_date} {racecourse}".strip()
    race_text = f"第{race_no}場" if race_no else "各場"
    return AiAnswer(
        f"{where} {race_text}，途程 {distance_m} 米。以下按各駒在相同途程的最快速度排列（米/秒，愈高愈快）。",
        columns=["名次", "馬號", "馬名", "最快時間", "速度(m/s)", "最快日期"],
        rows=rows,
    )


def _speed_for_distance(conn: sqlite3.Connection, distance_m: int) -> AiAnswer:
    records = conn.execute(
        "SELECT horse_id, race_date, venue, distance, time FROM race_records;"
    ).fetchall()
    best: dict[str, tuple[float, str, str, str]] = {}
    for record in records:
        if parse_distance_m(str(record["distance"] or "")) != distance_m:
            continue
        seconds = parse_race_time_seconds(str(record["time"] or ""))
        if seconds is None:
            continue
        hid = str(record["horse_id"])
        prev = best.get(hid)
        if prev is None or seconds < prev[0]:
            best[hid] = (seconds, str(record["time"] or ""), str(record["race_date"] or ""), str(record["venue"] or ""))
    if not best:
        return AiAnswer(f"找不到途程 {distance_m} 米的完成時間。")
    names = _name_map(conn, list(best))
    ordered = sorted(best.items(), key=lambda item: item[1][0])[:30]
    rows = []
    for index, (hid, (seconds, time_text, race_date, venue)) in enumerate(ordered, start=1):
        rows.append(
            [
                str(index),
                names.get(hid, hid),
                time_text,
                _fmt_speed(distance_m, seconds),
                race_date,
                venue,
            ]
        )
    return AiAnswer(
        f"資料庫中 {distance_m} 米最快速度前 {len(rows)} 名（沒有指定排位場次，改用全部往績）。",
        columns=["名次", "馬匹", "最快時間", "速度(m/s)", "日期", "場地"],
        rows=rows,
    )


def _person_ranking(conn: sqlite3.Connection, question: str, kind: str) -> AiAnswer:
    title = "騎師" if kind == "jockey" else "練馬師"
    date_key, date_label, _exact = _target_date_key(question, conn, require_win=True)
    wins = _unique_day_wins(conn, date_key)
    if not wins:
        return AiAnswer(f"{date_label}沒有勝出紀錄，無法排列{title}。")
    grouped: dict[str, list[str]] = {}
    horse_ids: list[str] = []
    for jockey, trainer, hid, _venue in wins:
        person = jockey if kind == "jockey" else trainer
        person = person or "（空白）"
        grouped.setdefault(person, []).append(hid)
        if hid:
            horse_ids.append(hid)
    if not grouped:
        return AiAnswer(f"{date_label}沒有勝出紀錄，無法排列{title}。")
    names = _name_map(conn, horse_ids)
    ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    rows = []
    rank = 0
    last_count = None
    for index, (person, horses) in enumerate(ordered, start=1):
        count = len(horses)
        if last_count != count:
            rank = index
            last_count = count
        # 勝出馬匹名單也去重（保序）
        seen_h: set[str] = set()
        labels: list[str] = []
        for hid in horses:
            key = hid or ""
            if key in seen_h:
                continue
            seen_h.add(key)
            labels.append(names.get(hid, hid) if hid else "—")
        rows.append([str(rank), person, str(count), "、".join(labels)])
    return AiAnswer(
        f"{date_label}贏馬的{title}排列（按勝出次數）。",
        columns=["名次", title, "勝出次數", "勝出馬匹"],
        rows=rows,
    )


def _stats(conn: sqlite3.Connection, question: str) -> AiAnswer | None:
    if not any(word in question for word in ("統計", "報表", "計算", "分析")):
        return None
    if "文件" in question:
        return None
    if "騎師" in question:
        return _career_person(conn, "jockey", "騎師")
    if "練馬師" in question:
        return _career_person(conn, "trainer", "練馬師")
    if "途程" in question:
        return _career_distance(conn)
    if "場地" in question or "馬場" in question:
        return _career_venue(conn)
    horses = conn.execute("SELECT COUNT(*) AS c FROM horses;").fetchone()["c"]
    races = conn.execute("SELECT COUNT(*) AS c FROM race_records;").fetchone()["c"]
    return AiAnswer(
        f"資料庫現有馬匹 {horses} 匹、往績 {races} 筆。"
        "可以再問：統計騎師勝出、統計練馬師、統計途程、統計場地。"
    )


def _career_person(conn: sqlite3.Connection, column: str, title: str) -> AiAnswer:
    records = conn.execute(
        f"SELECT {column} AS person, finish_pos FROM race_records;"
    ).fetchall()
    grouped: dict[str, list[int]] = {}
    for record in records:
        person = str(record["person"] or "").strip()
        if not person:
            continue
        grouped.setdefault(person, [0, 0])
        finish = _finish_number(record["finish_pos"])
        if finish is None:
            continue
        grouped[person][0] += 1
        if finish == 1:
            grouped[person][1] += 1
    ordered = sorted(grouped.items(), key=lambda item: (-item[1][1], -item[1][0], item[0]))[:30]
    rows = [
        [str(i), name, str(wins), str(starts), _fmt_pct(wins, starts)]
        for i, (name, (starts, wins)) in enumerate(ordered, start=1)
    ]
    return AiAnswer(
        f"{title}勝出統計（前 {len(rows)} 名，以勝出次數排序）。",
        columns=["名次", title, "勝出", "出賽", "勝率"],
        rows=rows,
    )


def _career_distance(conn: sqlite3.Connection) -> AiAnswer:
    records = conn.execute("SELECT distance, finish_pos FROM race_records;").fetchall()
    grouped: dict[int, list[int]] = {}
    for record in records:
        meters = parse_distance_m(str(record["distance"] or ""))
        if meters is None:
            continue
        bucket = grouped.setdefault(meters, [0, 0])
        finish = _finish_number(record["finish_pos"])
        if finish is None:
            continue
        bucket[0] += 1
        if finish == 1:
            bucket[1] += 1
    rows = [
        [str(meters), str(starts), str(wins)]
        for meters, (starts, wins) in sorted(grouped.items())
    ]
    return AiAnswer("各途程出賽與勝出次數。", columns=["途程(米)", "出賽", "勝出"], rows=rows)


def _career_venue(conn: sqlite3.Connection) -> AiAnswer:
    records = conn.execute("SELECT venue, finish_pos FROM race_records;").fetchall()
    grouped: dict[str, list[int]] = {}
    for record in records:
        venue = str(record["venue"] or "").strip() or "（空白）"
        bucket = grouped.setdefault(venue, [0, 0])
        finish = _finish_number(record["finish_pos"])
        if finish is None:
            continue
        bucket[0] += 1
        if finish == 1:
            bucket[1] += 1
    rows = [[name, str(starts), str(wins)] for name, (starts, wins) in sorted(grouped.items())]
    return AiAnswer("各場地出賽與勝出次數。", columns=["場地", "出賽", "勝出"], rows=rows)


def _lookup(conn: sqlite3.Connection, keyword: str) -> AiAnswer:
    horses = _resolve_horses(conn, keyword)
    horse, all_rows = _pick_horse(horses, keyword)
    if horse is None and len(all_rows) > 1:
        return _ambiguous(keyword, all_rows)
    if horse is not None:
        ids = _horse_ids(horse)
        marks = ",".join("?" * len(ids))
        records = conn.execute(
            f"""
            SELECT race_date, venue, race_no, distance, finish_pos, jockey, trainer, win_odds, time
            FROM race_records
            WHERE horse_id IN ({marks})
            ORDER BY id DESC
            LIMIT 200;
            """,
            ids,
        ).fetchall()
        records = sorted(records, key=lambda r: normalize_date_key(str(r["race_date"] or "")), reverse=True)
        rows = [
            [
                str(r["race_date"] or ""),
                str(r["venue"] or ""),
                str(r["race_no"] or ""),
                str(r["distance"] or ""),
                str(r["finish_pos"] or ""),
                str(r["jockey"] or ""),
                str(r["trainer"] or ""),
                _fmt_odds(r["win_odds"]),
                str(r["time"] or ""),
            ]
            for r in records
        ]
        wins = sum(1 for r in records if _is_win(r["finish_pos"]))
        return AiAnswer(
            f"{_horse_label(horse)}｜狀態 {horse['status'] or '—'}｜"
            f"這次列出 {len(rows)} 筆往績，其中勝出 {wins} 場。",
            columns=["日期", "場地", "場次", "途程", "名次", "騎師", "練馬師", "獨贏賠率", "時間"],
            rows=rows,
        )

    like = f"%{keyword}%"
    people = conn.execute(
        """
        SELECT race_date, venue, race_no, distance, horse_id, jockey, trainer, finish_pos, win_odds
        FROM race_records
        WHERE jockey LIKE ? OR trainer LIKE ? OR horse_id LIKE ?
        ORDER BY id DESC
        LIMIT 80;
        """,
        (like, like, like),
    ).fetchall()
    if not people:
        return AiAnswer(f"資料庫找不到與「{keyword}」相關的馬匹、騎師或練馬師。", handled=True)
    ids = [str(r["horse_id"]) for r in people if r["horse_id"]]
    names = _name_map(conn, ids)
    rows = [
        [
            str(r["race_date"] or ""),
            str(r["venue"] or ""),
            str(r["race_no"] or ""),
            names.get(str(r["horse_id"]), str(r["horse_id"] or "")),
            str(r["jockey"] or ""),
            str(r["trainer"] or ""),
            str(r["finish_pos"] or ""),
            _fmt_odds(r["win_odds"]),
        ]
        for r in people
    ]
    return AiAnswer(
        f"沒有唯一馬匹符合「{keyword}」，以下是騎師、練馬師或編號相關的往績。",
        columns=["日期", "場地", "場次", "馬匹", "騎師", "練馬師", "名次", "獨贏賠率"],
        rows=rows,
    )


def _doc_keyword(question: str) -> str:
    text = question
    for token in ("在文件中找", "在文件裡找", "文件中找", "文件裡找", "搜尋文件", "查找文件", "統計文件", "分析文件"):
        text = text.replace(token, " ")
    text = re.sub(r"(請|幫我|找出|查詢|文件|上傳|統計|分析|計算|平均|合計)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ：:，,。")


def _search_documents(conn: sqlite3.Connection, question: str) -> AiAnswer | None:
    docs = list(conn.execute("SELECT ID, FILENAME, CONTENT_TEXT FROM AI_DOCUMENT ORDER BY ID DESC;").fetchall())
    if not docs:
        if "文件" in question:
            return AiAnswer("還沒有上傳文件。請按「上傳文件」。")
        return None
    if any(word in question for word in ("文件清單", "已上傳", "我的文件")):
        rows = [[str(d["ID"]), str(d["FILENAME"]), str(len(d["CONTENT_TEXT"] or ""))] for d in docs]
        return AiAnswer(f"已上傳 {len(docs)} 份文件。", columns=["編號", "檔名", "字數"], rows=rows)

    wants_doc = "文件" in question
    keyword = _doc_keyword(question) if wants_doc else ""
    if wants_doc and any(word in question for word in ("統計", "分析", "平均", "合計", "計算")):
        return _analyze_documents(docs, keyword)

    if not wants_doc:
        return None
    if not keyword:
        rows = [[str(d["ID"]), str(d["FILENAME"])] for d in docs]
        return AiAnswer("請說明要在文件中找的關鍵字。", columns=["編號", "檔名"], rows=rows)

    hits = []
    for doc in docs:
        content = str(doc["CONTENT_TEXT"] or "")
        count = content.lower().count(keyword.lower())
        if count:
            hits.append((count, doc, content))
    hits.sort(key=lambda item: item[0], reverse=True)
    if not hits:
        return AiAnswer(f"已搜尋 {len(docs)} 份文件，沒有找到「{keyword}」。")
    rows = []
    for count, doc, content in hits[:8]:
        index = content.lower().find(keyword.lower())
        start = max(0, index - 40)
        end = min(len(content), index + len(keyword) + 80)
        snippet = content[start:end].replace("\n", " ")
        rows.append([str(doc["FILENAME"]), str(count), snippet])
    return AiAnswer(
        f"在文件中找到「{keyword}」，共 {len(hits)} 份檔案有相符內容。",
        columns=["檔名", "出現次數", "摘錄"],
        rows=rows,
    )


def _analyze_documents(docs: list[sqlite3.Row], keyword: str) -> AiAnswer:
    chosen = docs
    if keyword:
        filtered = [d for d in docs if keyword.lower() in str(d["FILENAME"]).lower() or keyword.lower() in str(d["CONTENT_TEXT"] or "").lower()]
        if filtered:
            chosen = filtered
    frames: list[tuple[str, list[str], list[list[str]]]] = []
    for doc in chosen[:5]:
        parsed = _parse_table_text(str(doc["CONTENT_TEXT"] or ""))
        if parsed is not None:
            header, body = parsed
            frames.append((str(doc["FILENAME"]), header, body))
    if not frames:
        return _search_documents_fallback(chosen, keyword)

    blocks = []
    rows = []
    for name, header, body in frames:
        numeric_columns = []
        for index, column in enumerate(header):
            numbers = _column_numbers([row[index] if index < len(row) else "" for row in body])
            if numbers:
                numeric_columns.append((column, numbers))
        if not numeric_columns:
            blocks.append(f"{name}：{len(body)} 列，沒有可計算的數字欄。")
            continue
        blocks.append(f"{name}：{len(body)} 列。")
        for column, numbers in numeric_columns:
            rows.append(
                [
                    name,
                    column,
                    str(len(numbers)),
                    f"{sum(numbers):.2f}",
                    f"{(sum(numbers) / len(numbers)):.2f}",
                    f"{min(numbers):.2f}",
                    f"{max(numbers):.2f}",
                ]
            )
    text = "文件數字統計：\n" + "\n".join(blocks)
    if not rows:
        return AiAnswer(text)
    return AiAnswer(
        text,
        columns=["檔名", "欄位", "筆數", "合計", "平均", "最小", "最大"],
        rows=rows,
    )


def _parse_table_text(content: str) -> tuple[list[str], list[list[str]]] | None:
    sample = content[:2000]
    if "," not in sample and "\t" not in sample:
        return None
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = [
        row
        for row in csv.reader(io.StringIO(content), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if len(rows) < 2:
        return None
    width = max(len(row) for row in rows)
    header = [(cell.strip() or f"欄{index + 1}") for index, cell in enumerate(rows[0])]
    while len(header) < width:
        header.append(f"欄{len(header) + 1}")
    body = []
    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        body.append(padded[:width])
    return header, body


def _column_numbers(values: list[str]) -> list[float]:
    numbers: list[float] = []
    for value in values:
        text = value.strip().replace(",", "")
        if not text:
            continue
        try:
            numbers.append(float(text))
        except ValueError:
            return []
    return numbers


def _search_documents_fallback(docs: list[sqlite3.Row], keyword: str) -> AiAnswer:
    if not keyword:
        return AiAnswer("這份文件不是表格，請改問「在文件中找某個詞」。")
    rows = []
    for doc in docs:
        content = str(doc["CONTENT_TEXT"] or "")
        count = content.lower().count(keyword.lower())
        if not count:
            continue
        index = content.lower().find(keyword.lower())
        snippet = content[max(0, index - 30) : index + 80].replace("\n", " ")
        rows.append([str(doc["FILENAME"]), str(count), snippet])
    if not rows:
        return AiAnswer("文件裡沒有可計算的數字欄，也找不到這個關鍵字。")
    return AiAnswer("文件不是表格，改以關鍵字摘錄。", columns=["檔名", "出現次數", "摘錄"], rows=rows)


def _local_answer(db_path: Path, question: str) -> AiAnswer:
    text = question.strip()
    if not text:
        return AiAnswer("請輸入問題或指令。")
    if text in {"幫助", "說明", "你可以做什麼", "help", "?"}:
        return AiAnswer(_help_text())
    if _needs_placeholder(text):
        return AiAnswer("請把【馬名】或【馬名或編號】換成真正的馬名、烙號或 HorseId，再送出。")

    init_db(db_path)
    with get_connection(db_path) as conn:
        action = _detect_action(text)
        if action is not None and action.action == "select_horse" and action.action_arg:
            horses = _resolve_horses(conn, action.action_arg)
            horse, many = _pick_horse(horses, action.action_arg)
            if horse is None and len(many) > 1:
                return _ambiguous(action.action_arg, many)
            if horse is None:
                return AiAnswer(f"找不到馬匹「{action.action_arg}」，無法選取。")
            action.action_arg = str(horse["horse_id"])
            action.text = f"正在開啟 {_horse_label(horse)}。"
            return action
        if action is not None and action.action == "fetch_records" and action.action_arg:
            horses = _resolve_horses(conn, action.action_arg)
            horse, _many = _pick_horse(horses, action.action_arg)
            if horse is not None:
                action.action_arg = str(horse["horse_id"])
            return action
        if action is not None:
            return action

        if "最佳途程" in text:
            return _answer_for_horse(conn, text, _best_distance)
        # 列出排位＋賽馬結果／合計分數須優先於「賠率」查詢
        # （合計指令常含「贏馬紀錄」+「賠率」欄，勿誤判成查獨贏賠率）
        if _is_list_racecard_results_question(text):
            return _list_racecard_and_results(conn, text)
        if _is_combined_win_score_question(text):
            return _race_combined_win_score(conn, text)
        if any(word in text for word in ("贏的賠率", "贏馬賠率", "獨贏賠率")) or (
            "賠率" in text
            and "贏" in text
            and not any(w in text for w in ("計分", "合計", "總分數", "條件"))
        ):
            return _answer_for_horse(conn, text, _win_odds)
        if "速度" in text or ("最快" in text and "途程" in text):
            return _speed_ranking(conn, text)
        if "騎師" in text and any(word in text for word in ("排列", "排名", "贏")):
            return _person_ranking(conn, text, "jockey")
        if "練馬師" in text and any(word in text for word in ("排列", "排名", "贏")):
            return _person_ranking(conn, text, "trainer")

        stats = _stats(conn, text)
        if stats is not None:
            return stats

        docs = _search_documents(conn, text)
        if docs is not None:
            return docs

        keyword = extract_horse_keyword(text)
        if keyword:
            return _lookup(conn, keyword)
        if len(text) <= 40 and "\n" not in text and not any(word in text for word in ("什麼", "如何", "為什麼", "怎麼")):
            found = _lookup(conn, text)
            if found.rows or "找不到" not in found.text:
                return found
        return AiAnswer(
            "這句沒有對上賽馬資料、已上傳文件或系統操作。\n" + _help_text(),
            handled=False,
        )


def _answer_for_horse(conn: sqlite3.Connection, question: str, fn) -> AiAnswer:  # noqa: ANN001
    keyword = extract_horse_keyword(question)
    if not keyword:
        return AiAnswer("請寫出馬名、烙號或 HorseId，例如「金鎗六十的最佳途程」。")
    horses = _resolve_horses(conn, keyword)
    horse, many = _pick_horse(horses, keyword)
    if horse is None and len(many) > 1:
        return _ambiguous(keyword, many)
    if horse is None:
        return AiAnswer(f"找不到馬匹「{keyword}」。")
    return fn(conn, horse)


def _llm_answer(db_path: Path, question: str, local: AiAnswer) -> AiAnswer | None:
    settings = load_ai_settings(db_path)
    if not settings.api_key:
        return None
    base = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"
    context = local.text
    if local.rows:
        header = " | ".join(local.columns)
        preview = [" | ".join(row) for row in local.rows[:30]]
        context += "\n" + header + "\n" + "\n".join(preview)
    payload = {
        "model": settings.model or "gpt-4o-mini",
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是賽馬資料系統 Racing 的助手，請用繁體中文回答。"
                    "只能根據提供的資料庫結果與文件摘錄作答；沒有的數字不要編造。"
                    "若資料不足，請直接說明缺少什麼。"
                ),
            },
            {
                "role": "user",
                "content": f"問題：{question}\n\n本機查到的資料：\n{context or '（沒有對應的本機資料）'}",
            },
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"模型服務回覆 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"無法連線到模型服務：{exc.reason}") from exc
    try:
        content = str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型回覆格式無法讀取。") from exc
    return AiAnswer(content, columns=local.columns, rows=local.rows, handled=True)


def answer(db_path: Path, question: str) -> AiAnswer:
    """回答一句提問。賽馬查詢在本機完成；只有開放式問題才會呼叫已設定的模型。"""
    local = _local_answer(db_path, question)
    if local.handled or local.action:
        return local
    try:
        remote = _llm_answer(db_path, question, local)
    except RuntimeError as exc:
        return AiAnswer(f"{local.text}\n\n模型呼叫失敗：{exc}", handled=True)
    return remote or local


def export_rows(columns: list[str], rows: list[list[str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            if columns:
                writer.writerow(columns)
            writer.writerows(rows)
        return
    import xlsxwriter

    workbook = xlsxwriter.Workbook(str(path))
    sheet = workbook.add_worksheet("報表")
    for col, name in enumerate(columns):
        sheet.write(0, col, name)
    for row_index, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            sheet.write(row_index, col, value)
    workbook.close()

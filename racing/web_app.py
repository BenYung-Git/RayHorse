

from __future__ import annotations
print(">>> USING WEB_APP FROM:", __file__)

from pathlib import Path
from typing import Any
import io
import json
import re
import threading
from urllib.parse import urlencode
from datetime import date, datetime, timedelta
import time

from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from racing import reports

from racing.config import default_config
from racing.db import (
    count_horses,
    count_race_records,
    fetch_horse_by_id,
    fetch_horses,
    fetch_race_records,
    get_recent_form_for_horse,
    get_horse_status,
    get_connection,
    init_db,
    sqlite3,
    search_horses_by_keyword,
    search_horses_by_name,
)
from racing.pp_preset import (
    find_preset,
    load_pp_preset_list,
    save_pp_preset_default,
    save_pp_preset_list,
    slug_preset_id,
)
from racing.web_utils import format_race_date_display, race_date_sort_key

BASE_DIR = Path(__file__).resolve().parent



# 與 horses_list 往績 sorters 欄位一致（save_pp_preset 驗證用）
HORSES_RECORD_SORT_KEYS: frozenset[str] = frozenset(
    {
        "race_date",
        "venue",
        "race_no",
        "course",
        "distance",
        "going",
        "class_band",
        "draw",
        "rating",
        "jockey",
        "trainer",
        "finish_pos",
        "running_pos",
        "win_odds",
        "time",
        "prize",
        "flag",
    }
)
TEMPLATE_DIR = BASE_DIR / "web_templates"
STATIC_DIR = BASE_DIR / "web_static"

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR),
    static_url_path="/web_static",
)
# 開發時改模板後不必手動重啟（仍建議重啟以避免多程序混淆）
app.config["TEMPLATES_AUTO_RELOAD"] = True
# 與 Windows 介面同時執行時允許多執行緒處理請求
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
cfg = default_config()
DB_PATH = cfg.db_path
RANKINGS_UI_VERSION = "rankings-ui-v3"
_scheduler_heartbeat = datetime.now()


@app.before_request
def _ensure_scheduler_before_request() -> None:
    """不論以 web／both／嵌入方式啟動，都確保間隔下載排程有跑。"""
    _ensure_download_scheduler()


@app.get("/ping")
def ping():
    return "pong"



def _compute_web_build_tag() -> str:
    """
    產生可見版本戳記：
    - 任何 web_app.py / web_templates/*.html 更新後，標記會改變
    - 方便前端快速判斷是否載入新版本
    """
    targets = [Path(__file__), *TEMPLATE_DIR.glob("*.html")]
    latest_ts = 0.0
    for p in targets:
        try:
            ts = p.stat().st_mtime
        except OSError:
            continue
        if ts > latest_ts:
            latest_ts = ts
    if latest_ts <= 0:
        return "web-unknown"
    return "web-" + datetime.fromtimestamp(latest_ts).strftime("%Y%m%d-%H%M%S")


WEB_BUILD_TAG = _compute_web_build_tag()


@app.get("/api/debug/web_app_info")
def api_debug_web_app_info() -> Any:
    """診斷：確認目前 process 載入嘅 web_app / 模板係邊個檔案（排查仍見舊版）。"""
    rankings_path = TEMPLATE_DIR / "rankings.html"
    has_stamp = False
    try:
        has_stamp = "build-stamp" in rankings_path.read_text(encoding="utf-8")
    except OSError:
        pass
    return jsonify(
        {
            "ok": True,
            "web_app_py": str(Path(__file__).resolve()),
            "template_dir": str(TEMPLATE_DIR.resolve()),
            "rankings_html": str(rankings_path.resolve()),
            "rankings_template_has_build_stamp": has_stamp,
            "RANKINGS_UI_VERSION": RANKINGS_UI_VERSION,
            "WEB_BUILD_TAG": WEB_BUILD_TAG,
            "db_path": str(DB_PATH),
        }
    )


@app.context_processor
def inject_web_build_tag() -> dict[str, str]:
    return {"web_build_tag": WEB_BUILD_TAG}


@app.after_request
def add_version_header(resp):
    resp.headers["X-Racing-Web-Build"] = WEB_BUILD_TAG
    return resp


def _race_meeting_snapshot() -> dict[str, Any]:
    race_date = "—"
    racecourse_code = "—"
    race_count = 0
    stored_meetings = 0

    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            meta_rows = conn.execute(
                """
                SELECT key, value FROM racecard_meta
                WHERE key IN (
                    'last_source_race_date',
                    'last_source_racecourse_code'
                );
                """
            ).fetchall()
    except Exception:
        meta_rows = []

    meta = {str(r["key"]): str(r["value"]) for r in meta_rows}
    race_date = meta.get("last_source_race_date", "—").strip() or "—"
    racecourse_code = meta.get("last_source_racecourse_code", "—").strip() or "—"

    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if race_date != "—" and racecourse_code != "—":
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT race_no) AS c
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ?;
                    """,
                    (race_date, racecourse_code),
                ).fetchone()
                race_count = int(row["c"]) if row else 0

            row2 = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT race_date, racecourse
                    FROM racecard_starters
                    GROUP BY race_date, racecourse
                ) x;
                """
            ).fetchone()
            stored_meetings = int(row2["c"]) if row2 else 0
    except Exception:
        pass

    return {
        "race_date": race_date,
        "racecourse": racecourse_code,
        "race_count": race_count,
        "stored_meetings": stored_meetings,
    }


def _racecourse_display(code: str) -> str:
    mapping = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}
    c = str(code or "").strip().upper()
    return mapping.get(c, c or "—")


def _message_board_images() -> list[str]:
    assets_dir = BASE_DIR / "assets" / "message_board"
    if not assets_dir.is_dir():
        return []
    files = [
        p.name for p in assets_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif")
    ]
    files.sort()
    return files


def _latest_rankings_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "race_date": "—",
        "racecourse": "—",
        "race_count": 0,
        "races": [],
    }
    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            meeting = conn.execute(
                """
                SELECT race_date, racecourse
                FROM racecard_starters
                GROUP BY race_date, racecourse
                ORDER BY race_date DESC, racecourse ASC
                LIMIT 1;
                """
            ).fetchone()
            if not meeting:
                return payload

            race_date = str(meeting["race_date"] or "")
            racecourse = str(meeting["racecourse"] or "")
            rows = conn.execute(
                """
                SELECT
                    race_no,
                    horse_id,
                    "馬號" AS horse_no,
                    "馬名" AS horse_name,
                    "檔位" AS draw,
                    "騎師" AS jockey,
                    "練馬師" AS trainer,
                    "評分" AS rating,
                    "負磅" AS handicap,
                    "後備" AS reserve
                FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC, CAST("馬號" AS INTEGER) ASC, "馬號" ASC;
                """,
                (race_date, racecourse),
            ).fetchall()
    except Exception:
        return payload

    races_map: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        race_no = int(row["race_no"] or 0)
        races_map.setdefault(race_no, []).append(
            {
                "horse_id": str(row["horse_id"] or ""),
                "horse_no": str(row["horse_no"] or ""),
                "horse_name": str(row["horse_name"] or ""),
                "draw": str(row["draw"] or ""),
                "jockey": str(row["jockey"] or ""),
                "trainer": str(row["trainer"] or ""),
                "rating": str(row["rating"] or ""),
                "handicap": str(row["handicap"] or ""),
                "reserve": str(row["reserve"] or ""),
            }
        )

    races = [
        {
            "race_no": race_no,
            "title": f"第 {race_no} 場｜{_racecourse_display(racecourse)}｜距離：—｜班次：—",
            "rows": races_map[race_no],
        }
        for race_no in sorted(races_map.keys())
    ]
    return {
        "race_date": race_date,
        "racecourse": _racecourse_display(racecourse),
        "race_count": len(races),
        "races": races,
    }


@app.get("/assets/message_board/<path:filename>")
def message_image(filename: str):
    folder = BASE_DIR / "assets" / "message_board"
    return send_from_directory(str(folder), filename)


@app.get("/")
def index():
    """首頁與 Windows「WEB 介面」相同，避免其他電腦看到舊 Dashboard。"""
    return redirect(url_for("desktop_app_shell"))


@app.get("/dashboard")
def legacy_dashboard() -> str:
    init_db(DB_PATH)
    try:
        with get_connection(DB_PATH) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM race_records;").fetchone()
            race_records_total = int(row["c"]) if row else 0
    except Exception:
        race_records_total = 0
    snap = _race_meeting_snapshot()
    stats = {
        "active_horses": count_horses(DB_PATH),
        "race_records_total": race_records_total,
        "race_date": snap["race_date"],
        "racecourse": _racecourse_display(snap["racecourse"]),
        "race_count": snap["race_count"],
        "stored_meetings": snap["stored_meetings"],
    }
    return render_template(
        "dashboard.html",
        stats=stats,
        images=_message_board_images(),
        db_path=str(DB_PATH),
    )


@app.get("/rankings")
def rankings() -> str:
    init_db(DB_PATH)
    payload = _latest_rankings_payload()
    rankings_path = (TEMPLATE_DIR / "rankings.html").resolve()
    html = render_template(
        "rankings.html",
        payload=payload,
        ui_version=RANKINGS_UI_VERSION,
        debug_web_app_py=str(Path(__file__).resolve()),
        debug_rankings_html=str(rankings_path),
    )
    resp = make_response(html)
    # 避免瀏覽器長期快取排位頁，導致仍顯示舊版（無 Build 區塊）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/api/horse/<horse_id>/recent_form")
def api_horse_recent_form(horse_id: str):
    init_db(DB_PATH)
    limit_raw = (request.args.get("limit") or "").strip()
    try:
        limit = int(limit_raw) if limit_raw else 10
    except ValueError:
        limit = 10
    if limit <= 0 or limit > 50:
        limit = 10
    # 排位彈窗往績固定用「最新在前」：race_date DESC
    sort_seq_for_recent: list[tuple[str, str]] = [("race_date", "desc")]
    try:
        rows = get_recent_form_for_horse(
            DB_PATH,
            horse_id,
            limit=limit,
            sort_seq=sort_seq_for_recent,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    records: list[dict[str, str]] = []
    for r in rows:
        records.append(
            {
                "race_date": str(r["race_date"] or ""),
                "venue": str(r["venue"] or ""),
                "race_no": str(r["race_no"] or ""),
                "course": str(r["course"] or ""),
                "distance": str(r["distance"] or ""),
                "going": str(r["going"] or ""),
                "class_band": str(r["class_band"] or ""),
                "draw": str(r["draw"] or ""),
                "rating": str(r["rating"] or ""),
                "jockey": str(r["jockey"] or ""),
                "trainer": str(r["trainer"] or ""),
                "finish_pos": str(r["finish_pos"] or ""),
                "running_pos": str(r["running_pos"] or ""),
                "win_odds": str(r["win_odds"] or ""),
                "time": str(r["time"] or ""),
                "prize": str(r["prize"] or ""),
            }
        )
    return jsonify({"ok": True, "records": records})


@app.get("/reports/export")
def export_report():
    # pandas/numpy 僅匯出時才載入，避免 WEB 主殼因 ABI 不相容而無法啟動
    try:
        import pandas as pd
    except ImportError as exc:
        abort(500, description=f"無法載入 pandas/numpy（請重裝相容套件）：{exc}")
    report_code = (request.args.get("report") or "").strip()
    if not report_code:
        abort(400, description="missing report code")
    try:
        report_def = reports.get_report_definition(report_code)
    except KeyError:
        abort(404, description=f"unknown report: {report_code}")
    df, filename_prefix = report_def.generate()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name=report_def.sheet_name)
    output.seek(0)
    today_str = date.today().strftime("%Y%m%d")
    filename = f"{filename_prefix}-{today_str}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/horses")
def horses_list() -> str:
    init_db(DB_PATH)
    q = (request.args.get("q") or "").strip()
    horse_id_query = (request.args.get("horse_id") or "").strip()
    panel_state = (request.args.get("panel") or "").strip().lower()
    def _to_number(text: str) -> float:
        t = str(text or "").strip()
        if not t:
            return float("-inf")
        m = re.search(r"-?\d+(?:\.\d+)?", t.replace(",", ""))
        return float(m.group(0)) if m else float("-inf")

    sorters = {
        "race_date": ("Race Date", lambda x: race_date_sort_key(x["race_date"])),
        "venue": ("Venue", lambda x: str(x["venue"] or "").lower()),
        "race_no": ("Race No.", lambda x: _to_number(x["race_no"])),
        "course": ("Course", lambda x: str(x["course"] or "").lower()),
        "distance": ("Distance", lambda x: _to_number(x["distance"])),
        "going": ("Going", lambda x: str(x["going"] or "").lower()),
        "class_band": ("Class", lambda x: str(x["class_band"] or "").lower()),
        "draw": ("Draw", lambda x: _to_number(x["draw"])),
        "rating": ("Rating", lambda x: _to_number(x["rating"])),
        "jockey": ("Jockey", lambda x: str(x["jockey"] or "").lower()),
        "trainer": ("Trainer", lambda x: str(x["trainer"] or "").lower()),
        "finish_pos": ("Finish Pos", lambda x: _to_number(x["finish_pos"])),
        "running_pos": ("Running Pos", lambda x: _to_number(x["running_pos"])),
        "win_odds": ("Win Odds", lambda x: _to_number(x["win_odds"])),
        "time": ("Time", lambda x: str(x["time"] or "")),
        "prize": ("Prize", lambda x: _to_number(x["prize"])),
        "flag": ("Flag", lambda x: str(x["flag"] or "").lower()),
    }

    pp_presets = load_pp_preset_list()
    pp_preset_id_param = (request.args.get("pp_preset_id") or "").strip()
    active_preset = find_preset(pp_presets, pp_preset_id_param) or pp_presets[0]
    pp_active_preset_id = str(active_preset["id"])
    active_sort_source = [
        (sk, dk)
        for sk, dk in (active_preset.get("sort_seq") or [])
        if sk in sorters and dk in ("asc", "desc")
    ][:3]
    active_show_source = [k for k in (active_preset.get("show_cols") or []) if k in sorters]
    pp_visible_request = (request.args.get("pp_visible") or "").strip()

    raw_seq: list[tuple[str, str]] = []
    for i in (1, 2, 3):
        sk = (request.args.get(f"sort{i}") or "").strip()
        dk = (request.args.get(f"dir{i}") or "asc").strip().lower()
        if dk not in ("asc", "desc"):
            dk = "asc"
        if sk and sk in sorters:
            raw_seq.append((sk, dk))
    # dedupe keys, keep first occurrence, max 3 keys
    seen: set[str] = set()
    sort_seq: list[tuple[str, str]] = []
    for key, direction in raw_seq:
        if key in seen:
            continue
        seen.add(key)
        sort_seq.append((key, direction))
        if len(sort_seq) >= 3:
            break
    if not sort_seq:
        seen_pp: set[str] = set()
        for sk, dk in active_sort_source:
            if sk not in sorters or dk not in ("asc", "desc"):
                continue
            if sk in seen_pp:
                continue
            seen_pp.add(sk)
            sort_seq.append((sk, dk))
            if len(sort_seq) >= 3:
                break
        if not sort_seq:
            sort_seq = [("race_date", "desc")]

    clicked_col = (request.args.get("clicked_col") or "").strip()
    selected_col = (request.args.get("selected_col") or (sort_seq[0][0] if sort_seq else "")).strip()
    if clicked_col and clicked_col in sorters:
        idx = next((i for i, (k, _d) in enumerate(sort_seq) if k == clicked_col), -1)
        if idx != -1:
            k, d = sort_seq[idx]
            sort_seq[idx] = (k, "desc" if d == "asc" else "asc")
        else:
            default_dir = "desc" if clicked_col == "race_date" else "asc"
            # 新欄位接在排序鍵列表**末尾**（先點的欄位維持在較前），避免「永遠插在主鍵後面」
            # 把先前點的副鍵（如 jockey）擠到後面。
            insert_pos = len(sort_seq)
            sort_seq.insert(insert_pos, (clicked_col, default_dir))
            # 最多 3 鍵：保留主鍵 sort_seq[0]，超出則從索引 1 開始移除最舊的副鍵
            while len(sort_seq) > 3:
                sort_seq.pop(1)

    rows = fetch_horses(DB_PATH, limit=None)

    def _resolve_query_to_horse_id(keyword: str) -> str:
        kw = keyword.strip()
        if not kw:
            return ""
        kw_up = kw.upper()
        # 優先：HorseId / Brand No. 精準匹配
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            brand = str(r["brand_no"] or "").strip()
            if hid.upper() == kw_up:
                return hid
            if brand.upper() == kw_up:
                return hid
        # 次優先：中文名包含
        for r in rows:
            zh = str(r["name_zh"] or "").strip()
            if kw and kw in zh:
                return str(r["horse_id"] or "")
        # 次優先：英文名包含（不分大小寫）
        for r in rows:
            en = str(r["name_en"] or "").strip().upper()
            if kw_up and kw_up in en:
                return str(r["horse_id"] or "")
        # 最後 fallback：沿用既有搜尋函式的第一筆
        hit = search_horses_by_keyword(DB_PATH, kw, limit=1)
        if hit:
            return str(hit[0]["horse_id"] or "")
        return ""

    selected_horse_id = ""
    if rows:
        valid_ids = {str(r["horse_id"] or "") for r in rows}
        resolved_by_q = _resolve_query_to_horse_id(q) if q else ""
        # 有輸入關鍵字時，優先用 q 定位；找不到才回退到 horse_id 參數。
        if resolved_by_q and resolved_by_q in valid_ids:
            selected_horse_id = resolved_by_q
        elif horse_id_query and horse_id_query in valid_ids:
            selected_horse_id = horse_id_query
        else:
            selected_horse_id = str(rows[0]["horse_id"] or "")

    records_view: list[dict[str, str]] = []
    selected_status = ""
    if selected_horse_id:
        selected_status = get_horse_status(DB_PATH, selected_horse_id)
        selected_records = fetch_race_records(DB_PATH, selected_horse_id)
        rows_unsorted = [
            {
                "race_date": format_race_date_display(r.race_date),
                "venue": r.venue,
                "race_no": r.race_no,
                "course": r.course,
                "distance": r.distance,
                "going": r.going,
                "class_band": r.class_band,
                "draw": r.draw,
                "rating": r.rating,
                "jockey": r.jockey,
                "trainer": r.trainer,
                "finish_pos": r.finish_pos,
                "running_pos": r.running_pos,
                "win_odds": r.win_odds,
                "time": r.time,
                "prize": r.prize,
                "flag": selected_status,
            }
            for r in selected_records
        ]
        records_view = list(rows_unsorted)
        # stable multi-key sort: apply from lowest priority to highest
        for key, direction in reversed(sort_seq):
            records_view.sort(key=sorters[key][1], reverse=(direction == "desc"))

    base_header_params: dict[str, str] = {"q": q, "horse_id": selected_horse_id}
    if pp_active_preset_id:
        base_header_params["pp_preset_id"] = pp_active_preset_id
    if panel_state == "open":
        base_header_params["panel"] = "open"
    if sort_seq:
        base_header_params["selected_col"] = sort_seq[0][0]
    for idx, (sk, dk) in enumerate(sort_seq, start=1):
        base_header_params[f"sort{idx}"] = sk
        base_header_params[f"dir{idx}"] = dk
    sort_labels = {k: v[0] for k, v in sorters.items()}
    sort_summary = "  ".join(
        [f"{i+1}. {sort_labels[k]}({'↑' if d == 'asc' else '↓'})" for i, (k, d) in enumerate(sort_seq)]
    )
    sort_state: dict[str, dict[str, Any]] = {}
    for i, (k, d) in enumerate(sort_seq, start=1):
        sort_state[k] = {"order": i, "dir": d, "selected": True}
    for k in sorters.keys():
        if k not in sort_state:
            sort_state[k] = {"order": 999, "dir": "asc", "selected": False}

    pp_show_cols_default = list(active_show_source)
    if not pp_show_cols_default:
        pp_show_cols_default = list(sorters.keys())
    if pp_visible_request:
        parsed_vis: list[str] = []
        seen_vis: set[str] = set()
        for part in pp_visible_request.split(","):
            vk = part.strip()
            if vk in sorters and vk not in seen_vis:
                seen_vis.add(vk)
                parsed_vis.append(vk)
        if parsed_vis:
            pp_show_cols_default = parsed_vis
    pp_sort_seq_default = [
        (sk, dk)
        for sk, dk in active_sort_source
        if sk in sorters and dk in ("asc", "desc")
    ][:3]

    pp_presets_meta = [{"id": p["id"], "name": p["name"]} for p in pp_presets]

    pp_visible_param = ""
    if set(pp_show_cols_default) != set(active_show_source):
        pp_visible_param = ",".join(pp_show_cols_default)
    if pp_visible_param:
        base_header_params["pp_visible"] = pp_visible_param
    base_header_qs = urlencode(base_header_params)

    return render_template(
        "horses_list.html",
        horses=rows,
        q=q,
        selected_horse_id=selected_horse_id,
        selected_status=selected_status,
        records=records_view,
        sort_seq=sort_seq,
        sort_labels=sort_labels,
        sort_summary=sort_summary,
        sort_state=sort_state,
        base_header_qs=base_header_qs,
        panel_state=panel_state,
        pp_presets_meta=pp_presets_meta,
        pp_active_preset_id=pp_active_preset_id,
        pp_visible_param=pp_visible_param,
        pp_show_cols_default=pp_show_cols_default,
        pp_sort_seq_default=pp_sort_seq_default,
        sortable_fields=[
            ("race_date", "Race Date"),
            ("jockey", "Jockey"),
            ("trainer", "Trainer"),
            ("class_band", "Class"),
            ("rating", "Rating"),
            ("win_odds", "Odds"),
            ("draw", "Draw"),
            ("race_no", "Race No."),
            ("distance", "Distance"),
            ("finish_pos", "Finish Pos"),
            ("running_pos", "Running Pos"),
            ("time", "Time"),
            ("prize", "Prize"),
            ("venue", "Venue"),
            ("course", "Course"),
            ("going", "Going"),
            ("flag", "Flag"),
        ],
    )


@app.post("/horses/save_pp_preset")
def save_pp_preset() -> tuple[Any, int] | Any:
    """建立／更新往績 preset 列表；無 mode 時相容第 2 階段（覆寫 default）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    show_raw = data.get("show_cols")
    sort_raw = data.get("sort_seq")
    if not isinstance(show_raw, list):
        show_raw = []
    if not isinstance(sort_raw, list):
        sort_raw = []

    show_cols: list[str] = []
    seen_show: set[str] = set()
    for k in show_raw:
        if not isinstance(k, str) or k not in HORSES_RECORD_SORT_KEYS:
            continue
        if k in seen_show:
            continue
        seen_show.add(k)
        show_cols.append(k)

    sort_seq_tuples: list[tuple[str, str]] = []
    seen_sort: set[str] = set()
    for item in sort_raw[:10]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        sk = str(item[0]).strip()
        dk = str(item[1]).strip().lower()
        if sk not in HORSES_RECORD_SORT_KEYS or dk not in ("asc", "desc"):
            continue
        if sk in seen_sort:
            continue
        seen_sort.add(sk)
        sort_seq_tuples.append((sk, dk))
        if len(sort_seq_tuples) >= 3:
            break

    if not show_cols:
        return jsonify({"ok": False, "error": "no_show_cols"}), 400
    if not sort_seq_tuples:
        sort_seq_tuples = [("race_date", "desc")]

    mode = str(data.get("mode") or "").lower()

    try:
        if mode == "create":
            name = (data.get("name") or "").strip()
            if not name:
                return jsonify({"ok": False, "error": "name_required"}), 400
            presets = load_pp_preset_list()
            existing_ids = {str(p["id"]) for p in presets}
            new_id = slug_preset_id(name, existing_ids)
            presets.append(
                {
                    "id": new_id,
                    "name": name,
                    "is_builtin": False,
                    "show_cols": list(show_cols),
                    "sort_seq": list(sort_seq_tuples),
                }
            )
            save_pp_preset_list(presets)
            return jsonify({"ok": True, "id": new_id})

        if mode == "update":
            preset_id = (data.get("id") or "").strip()
            if not preset_id:
                return jsonify({"ok": False, "error": "id_required"}), 400
            presets = load_pp_preset_list()
            target = find_preset(presets, preset_id)
            if target is None:
                return jsonify({"ok": False, "error": "preset_not_found"}), 404
            target["show_cols"] = list(show_cols)
            target["sort_seq"] = list(sort_seq_tuples)
            if preset_id == "default":
                save_pp_preset_default(
                    {"show_cols": list(show_cols), "sort_seq": list(sort_seq_tuples)}
                )
            else:
                save_pp_preset_list(presets)
            return jsonify({"ok": True, "id": preset_id})

        save_pp_preset_default(
            {"show_cols": list(show_cols), "sort_seq": list(sort_seq_tuples)}
        )
    except OSError as e:
        return jsonify({"ok": False, "error": "write_failed", "detail": str(e)}), 500
    return jsonify({"ok": True})


@app.get("/horses/<horse_id>")
def horse_detail(horse_id: str) -> str:
    init_db(DB_PATH)
    horse = fetch_horse_by_id(DB_PATH, horse_id)
    if horse is None:
        abort(404)

    records = fetch_race_records(DB_PATH, horse_id)
    status = get_horse_status(DB_PATH, horse_id)
    records_view = [
        {
            "race_date": format_race_date_display(r.race_date),
            "venue": r.venue,
            "race_no": r.race_no,
            "course": r.course,
            "distance": r.distance,
            "going": r.going,
            "class_band": r.class_band,
            "draw": r.draw,
            "rating": r.rating,
            "jockey": r.jockey,
            "trainer": r.trainer,
            "handicap": r.handicap,
            "finish_pos": r.finish_pos,
            "running_pos": r.running_pos,
            "win_odds": r.win_odds,
            "time": r.time,
            "prize": r.prize,
            "flag": status,
        }
        for r in records
    ]
    return render_template(
        "horse_detail.html",
        horse=horse,
        status=status,
        records=records_view,
        records_total=count_race_records(DB_PATH, horse_id),
    )


@app.get("/app")
def desktop_app_shell() -> str:
    """與 Windows 主視窗對齊的 WEB 主殼。"""
    init_db(DB_PATH)
    return render_template("desktop_shell.html")


@app.get("/api/app/horses")
def api_app_horses():
    init_db(DB_PATH)
    q = (request.args.get("q") or "").strip()
    # 與 Windows「過濾／搜尋」相同：依馬名關鍵字（中／英）
    if q:
        rows = search_horses_by_name(DB_PATH, q, limit=None)
    else:
        rows = fetch_horses(DB_PATH, limit=None)
    out = []
    for r in rows:
        name_zh = str(r["name_zh"] or "").strip()
        if not name_zh:
            continue
        out.append(
            {
                "horse_id": str(r["horse_id"] or ""),
                "brand_no": str(r["brand_no"] or ""),
                "name_en": str(r["name_en"] or ""),
                "name_zh": name_zh,
                "status": str(r["status"] or ""),
            }
        )
    return jsonify(
        {
            "ok": True,
            "db_path": str(DB_PATH),
            "total": count_horses(DB_PATH),
            "rows": out,
        }
    )


@app.get("/api/app/horse/<horse_id>")
def api_app_horse(horse_id: str):
    init_db(DB_PATH)
    horse = fetch_horse_by_id(DB_PATH, horse_id)
    if horse is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    from racing.race_record_grid import RACE_RECORD_GRID_HEADERS, race_record_grid_row

    records = fetch_race_records(DB_PATH, horse_id)
    flag_status = get_horse_status(DB_PATH, horse_id) or str(horse["status"] or "")
    race_columns = list(RACE_RECORD_GRID_HEADERS)
    races = [race_record_grid_row(r, flag_status) for r in records]
    return jsonify(
        {
            "ok": True,
            "horse": {
                "horse_id": str(horse["horse_id"] or ""),
                "brand_no": str(horse["brand_no"] or ""),
                "name_en": str(horse["name_en"] or ""),
                "name_zh": str(horse["name_zh"] or ""),
                "status": str(horse["status"] or ""),
            },
            "race_columns": race_columns,
            "races": races,
            "records_total": count_race_records(DB_PATH, horse_id),
        }
    )


@app.get("/api/app/racecard")
def api_app_racecard():
    """排位資料欄位與 Windows RacecardPanel 相同；可用 ?date=&course= 選賽日。"""
    from racing.ui.racecard_rank_viewer import (
        COURSE_CODE_TO_DISPLAY,
        RACECARD_DB_DISPLAY_COLUMNS,
        RACECARD_DISPLAY_COLUMNS,
        RACECARD_HIDDEN_HORSE_ID_COLUMN,
        RACECARD_IMMEDIATE_ODDS_COLUMN,
        RACECARD_ODDS_DIFF_COLUMN,
        RACECARD_PREDICTED_ODDS_COLUMN,
        RACECARD_RACE_INFO_TABLE,
        _normalize_post_time,
        _race_tab_label,
        get_last_update_at,
        list_stored_racecard_meetings,
        odds_diff_display,
    )

    init_db(DB_PATH)
    last_update = get_last_update_at(cfg) or "尚未更新"
    meetings = list_stored_racecard_meetings(cfg)
    meeting_opts = [
        {
            "race_date": rd,
            "racecourse": rc,
            "label": f"{rd}  {COURSE_CODE_TO_DISPLAY.get(rc, rc)}（{rc}）",
        }
        for rd, rc in meetings
    ]
    if not meetings:
        return jsonify(
            {
                "ok": True,
                "last_update": last_update,
                "race_date": "—",
                "racecourse": "—",
                "racecourse_code": "",
                "meetings": [],
                "columns": list(RACECARD_DISPLAY_COLUMNS),
                "races": [],
            }
        )

    req_date = (request.args.get("date") or "").strip()
    req_course = (request.args.get("course") or "").strip().upper()
    race_date, racecourse = meetings[0]
    if req_date:
        hits = [
            (rd, rc)
            for rd, rc in meetings
            if rd == req_date and (not req_course or rc == req_course)
        ]
        if hits:
            race_date, racecourse = hits[0]
        else:
            # 容許不同斜線格式
            for rd, rc in meetings:
                if rd.replace("-", "/") == req_date.replace("-", "/") and (
                    not req_course or rc == req_course
                ):
                    race_date, racecourse = rd, rc
                    break

    columns = list(RACECARD_DISPLAY_COLUMNS)
    db_columns = list(RACECARD_DB_DISPLAY_COLUMNS)
    cols_sql = ",".join(f'"{c}"' for c in db_columns)
    horse_id_col = RACECARD_HIDDEN_HORSE_ID_COLUMN
    info_keys = (
        "event_name",
        "calendar_date",
        "weekday",
        "meeting_venue",
        "post_time",
        "race_course_area",
        "track_name",
        "distance",
        "prize_money",
        "rating_range",
        "race_class",
    )
    races: list[dict[str, Any]] = []
    try:
        with get_connection(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            race_nos = [
                int(r[0])
                for r in conn.execute(
                    """
                    SELECT DISTINCT race_no FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ?
                    ORDER BY race_no ASC;
                    """,
                    (race_date, racecourse),
                ).fetchall()
            ]
            for race_no in race_nos:
                rows_db = conn.execute(
                    f"""
                    SELECT {cols_sql}, {horse_id_col} AS __horse_id
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ? AND race_no = ?
                    ORDER BY CASE WHEN 後備 = '否' THEN 0 ELSE 1 END, id ASC;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchall()
                rows = []
                for r in rows_db:
                    item = {
                        c: ("" if r[c] is None else str(r[c])) for c in db_columns
                    }
                    item[RACECARD_ODDS_DIFF_COLUMN] = odds_diff_display(
                        item.get(RACECARD_IMMEDIATE_ODDS_COLUMN),
                        item.get(RACECARD_PREDICTED_ODDS_COLUMN),
                    )
                    item["__horse_id"] = str(r["__horse_id"] or "")
                    rows.append(item)
                info_row = conn.execute(
                    f"""
                    SELECT {", ".join(info_keys)}
                    FROM {RACECARD_RACE_INFO_TABLE}
                    WHERE race_date = ? AND racecourse = ? AND race_no = ?
                    LIMIT 1;
                    """,
                    (race_date, racecourse, race_no),
                ).fetchone()
                info: dict[str, str] | None = None
                post_time = None
                if info_row is not None:
                    from racing.standard_times import attach_standard_finish_time

                    raw_info: dict[str, object] = {}
                    for k in info_keys:
                        val: object = info_row[k]
                        if k == "post_time":
                            val = _normalize_post_time(val)
                            post_time = val
                        if (
                            k == "track_name"
                            and val is not None
                            and str(val).strip()
                            and "賽道" not in str(val)
                        ):
                            val = f"{str(val).strip()} 賽道"
                        raw_info[k] = "" if val is None else str(val)
                    attached = attach_standard_finish_time(raw_info) or {}
                    info = {k: ("" if v is None else str(v)) for k, v in attached.items()}
                races.append(
                    {
                        "race_no": race_no,
                        "label": _race_tab_label(race_no, post_time),
                        "post_time": post_time or "",
                        "info": info,
                        "rows": rows,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(
        {
            "ok": True,
            "last_update": last_update,
            "race_date": race_date,
            "racecourse": COURSE_CODE_TO_DISPLAY.get(racecourse, racecourse),
            "racecourse_code": racecourse,
            "meetings": meeting_opts,
            "columns": columns,
            "races": races,
        }
    )


@app.get("/api/app/results")
def api_app_results():
    """賽馬結果：可選不同賽日／場地／場次（與 Windows 賽馬結果分頁相同）。"""
    from racing.race_results import (
        COURSE_CODE_TO_NAME,
        fetch_results_rows,
        list_results_race_nos,
        list_stored_results_meetings,
        summarize_top3_stats,
    )
    from racing.web_utils import format_two_digit_99, normalize_date_yyyy_mm_dd

    init_db(DB_PATH)
    stored = list_stored_results_meetings(DB_PATH)
    meetings = [
        {
            "race_date": rd,
            "racecourse": rc,
            "label": f"{rd}  {COURSE_CODE_TO_NAME.get(rc, rc)}（{rc}）",
        }
        for rd, rc in stored
    ]
    if not meetings:
        return jsonify(
            {
                "ok": True,
                "race_date": "—",
                "racecourse": "—",
                "racecourse_code": "",
                "meetings": [],
                "race_nos": [],
                "rows": [],
                "top3_stats": {
                    "trainers": [],
                    "jockeys": [],
                    "draws": [],
                    "handicaps": [],
                    "odds": [],
                },
            }
        )

    req_date = normalize_date_yyyy_mm_dd(request.args.get("date") or "")
    req_course = (request.args.get("course") or "").strip().upper()
    req_race = (request.args.get("race") or "").strip()
    race_date, racecourse = stored[0]
    if req_date:
        for rd, rc in stored:
            if rd == req_date and (not req_course or rc == req_course):
                race_date, racecourse = rd, rc
                break

    all_rows = fetch_results_rows(DB_PATH, race_date, racecourse)
    race_nos = list_results_race_nos(all_rows)
    top3 = summarize_top3_stats(all_rows)
    rows = all_rows
    if req_race and req_race.lower() not in {"", "all", "*"}:
        want = format_two_digit_99(req_race)
        rows = [r for r in all_rows if format_two_digit_99(r.get("race_no")) == want]
    return jsonify(
        {
            "ok": True,
            "race_date": race_date,
            "racecourse": COURSE_CODE_TO_NAME.get(racecourse, racecourse),
            "racecourse_code": racecourse,
            "meetings": meetings,
            "race_nos": race_nos,
            "selected_race": req_race if req_race else "all",
            "rows": rows,
            "top3_stats": top3,
        }
    )


@app.get("/api/app/football")
def api_app_football():
    """足球賠率／賽果：依賽日列出賽事與盤口（scope=hk｜epl，與 Windows 分頁相同）。"""
    from racing.football import (
        FOOTBALL_SCOPE_HK,
        fetch_football_display_rows,
        list_football_dates,
        normalize_football_date,
        normalize_football_scope,
    )

    init_db(DB_PATH)
    scope = normalize_football_scope(request.args.get("scope") or FOOTBALL_SCOPE_HK)
    dates = list_football_dates(DB_PATH, scope=scope)
    meetings = [{"race_date": d, "label": d} for d in dates]
    if not meetings:
        return jsonify(
            {
                "ok": True,
                "scope": scope,
                "race_date": "",
                "meetings": [],
                "display_rows": [],
                "epl_rows": [],
                "result_rows": [],
                "odds_match_rows": [],
                "odds_rows": [],
                "selected_match_id": "",
            }
        )

    req_date = normalize_football_date(request.args.get("date") or "")
    race_date = dates[0]
    if req_date and req_date in dates:
        race_date = req_date

    display_rows = fetch_football_display_rows(DB_PATH, race_date, scope=scope)
    return jsonify(
        {
            "ok": True,
            "scope": scope,
            "race_date": race_date,
            "meetings": meetings,
            "display_rows": display_rows,
            # 相容舊前端欄位名稱
            "epl_rows": display_rows,
            "result_rows": display_rows,
            "odds_match_rows": [],
            "odds_rows": [],
            "selected_match_id": "",
        }
    )


@app.get("/api/app/kings")
def api_app_kings():
    """騎師王／練馬師王：賽日積分＋今季總分（與 Windows 分頁相同）。"""
    from racing.challenge_kings import (
        TYPE_JOCKEY,
        TYPE_TRAINER,
        fetch_challenge_standings,
        fetch_season_standings,
        list_stored_challenge_dates,
    )
    from racing.web_utils import normalize_date_yyyy_mm_dd

    init_db(DB_PATH)
    dates = list_stored_challenge_dates(DB_PATH)
    meetings = [{"race_date": rd, "label": rd} for rd in dates]
    if not meetings:
        return jsonify(
            {
                "ok": True,
                "race_date": "—",
                "meetings": [],
                "jockey_rows": [],
                "trainer_rows": [],
                "jockey_season_rows": [],
                "trainer_season_rows": [],
            }
        )

    req_date = normalize_date_yyyy_mm_dd(request.args.get("date") or "")
    # challenge_standings 賽日多為 YYYY/MM/DD
    race_date = dates[0]
    if req_date:
        req_slash = req_date.replace("-", "/")
        for rd in dates:
            if rd == req_date or rd == req_slash or rd.replace("/", "-") == req_date:
                race_date = rd
                break

    return jsonify(
        {
            "ok": True,
            "race_date": race_date,
            "meetings": meetings,
            "jockey_rows": fetch_challenge_standings(DB_PATH, TYPE_JOCKEY, race_date),
            "trainer_rows": fetch_challenge_standings(DB_PATH, TYPE_TRAINER, race_date),
            "jockey_season_rows": fetch_season_standings(DB_PATH, TYPE_JOCKEY, race_date),
            "trainer_season_rows": fetch_season_standings(DB_PATH, TYPE_TRAINER, race_date),
        }
    )


@app.get("/api/app/analysis/meta")
def api_app_analysis_meta():
    """賽馬分析面板：賽日、場次、欄位／條件預設。"""
    from racing.race_analysis import analysis_meta_payload, load_prefs
    from racing.ui.racecard_rank_viewer import (
        COURSE_CODE_TO_DISPLAY,
        list_stored_racecard_meetings,
    )

    init_db(DB_PATH)
    meetings = list_stored_racecard_meetings(cfg)
    meeting_opts = [
        {
            "race_date": rd,
            "racecourse": rc,
            "label": f"{rd}  {COURSE_CODE_TO_DISPLAY.get(rc, rc)}（{rc}）",
        }
        for rd, rc in meetings
    ]
    race_nos: list[int] = []
    date = str(request.args.get("date") or "").strip()
    course = str(request.args.get("course") or "").strip().upper()
    if not date and meeting_opts:
        date = meeting_opts[0]["race_date"]
        course = meeting_opts[0]["racecourse"]
    if date and course:
        conn = get_connection(DB_PATH)
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT race_no FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC;
                """,
                (date, course),
            ).fetchall()
            race_nos = [int(r["race_no"]) for r in rows]
        finally:
            conn.close()
    payload = analysis_meta_payload(
        meetings=meeting_opts, race_nos=race_nos, data_dir=cfg.data_dir
    )
    payload["prefs"] = load_prefs(cfg.data_dir)
    payload["selected"] = {"race_date": date, "racecourse": course}
    payload["ok"] = True
    return jsonify(payload)


@app.post("/api/app/analysis/templates")
def api_app_analysis_templates_save():
    """儲存賽馬分析範本（欄位／排序／條件）。"""
    from racing.race_analysis import (
        ConditionRow,
        format_columns_text,
        load_templates,
        parse_columns_text,
        save_template,
    )

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "請輸入範本名稱。"}), 400
    columns_text = str(payload.get("columns_text") or "").strip()
    columns = payload.get("columns") or []
    if columns_text:
        columns = parse_columns_text(columns_text)
    elif isinstance(columns, list):
        columns = [str(c).strip() for c in columns if str(c).strip()]
    else:
        columns = []
    if not columns:
        return jsonify({"ok": False, "message": "請輸入輸出欄位（以／分隔）。"}), 400
    columns_text = format_columns_text(columns)
    raw_conds = payload.get("conditions") or []
    conditions = [
        ConditionRow.from_dict(c) for c in raw_conds if isinstance(c, dict)
    ]
    body = {
        "columns": columns,
        "columns_text": columns_text,
        "sort_field": str(payload.get("sort_field") or "合計分數").strip() or "合計分數",
        "sort_ascending": bool(payload.get("sort_ascending")),
        "all_races": bool(payload.get("all_races", True)),
        "conditions": [c.to_dict() for c in conditions],
    }
    template_id = str(payload.get("id") or "").strip() or None
    overwrite = bool(payload.get("overwrite"))
    if not overwrite and not template_id:
        for item in load_templates(cfg.data_dir):
            if str(item.get("name") or "") == name:
                return jsonify(
                    {
                        "ok": False,
                        "exists": True,
                        "message": f"範本「{name}」已存在，是否覆寫？",
                        "id": item.get("id"),
                    }
                ), 409
        # 若前端確認覆寫會帶 overwrite；此處同名且未指定 id 時走新建路徑由 save_template 覆寫
    try:
        if overwrite and not template_id:
            for item in load_templates(cfg.data_dir):
                if str(item.get("name") or "") == name:
                    template_id = str(item.get("id") or "")
                    break
        saved = save_template(
            cfg.data_dir, name=name, payload=body, template_id=template_id
        )
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "template": saved,
            "templates": load_templates(cfg.data_dir),
            "message": f"已儲存範本：{saved.get('name')}",
        }
    )


@app.delete("/api/app/analysis/templates/<template_id>")
def api_app_analysis_templates_delete(template_id: str):
    from racing.race_analysis import delete_template, load_templates

    ok = delete_template(cfg.data_dir, template_id)
    if not ok:
        return jsonify({"ok": False, "message": "找不到該範本。"}), 404
    return jsonify(
        {
            "ok": True,
            "templates": load_templates(cfg.data_dir),
            "message": "已刪除範本。",
        }
    )


def _analysis_prefs_from_payload(payload: dict) -> dict:
    """從請求 JSON 正規化可寫入 race_analysis_prefs 的欄位。"""
    from racing.race_analysis import (
        ConditionRow,
        format_columns_text,
        parse_columns_text,
    )

    columns_text = str(payload.get("columns_text") or "").strip()
    columns = payload.get("columns") or []
    if columns_text:
        columns = parse_columns_text(columns_text)
    elif isinstance(columns, list):
        columns = [str(c).strip() for c in columns if str(c).strip()]
    else:
        columns = []
    if columns:
        columns_text = format_columns_text(columns)
    raw_conds = payload.get("conditions") or []
    conditions = [
        ConditionRow.from_dict(c).to_dict()
        for c in raw_conds
        if isinstance(c, dict)
    ]
    race_no = payload.get("race_no")
    try:
        race_no_i = None if race_no in (None, "") else int(race_no)
    except (TypeError, ValueError):
        race_no_i = None
    prefs: dict = {
        "columns": columns,
        "columns_text": columns_text,
        "sort_field": str(payload.get("sort_field") or "合計分數").strip() or "合計分數",
        "sort_ascending": bool(payload.get("sort_ascending")),
        "all_races": bool(payload.get("all_races", True)),
        "conditions": conditions,
        "race_date": str(payload.get("race_date") or "").strip(),
        "racecourse": str(payload.get("racecourse") or "").strip().upper(),
        "race_no": race_no_i,
        "title": str(payload.get("title") or "").strip(),
        "template_id": str(payload.get("template_id") or "").strip(),
        "preview": str(payload.get("preview") or ""),
    }
    return prefs


@app.post("/api/app/analysis/prefs")
def api_app_analysis_prefs_save():
    """離開賽馬分析分頁時保存目前設定（不含組成指令）。"""
    from racing.race_analysis import save_prefs

    payload = request.get_json(silent=True) or {}
    prefs = _analysis_prefs_from_payload(payload)
    save_prefs(cfg.data_dir, prefs)
    return jsonify({"ok": True})


@app.post("/api/app/analysis/build")
def api_app_analysis_build():
    """依面板參數組成 AI 指令（可選寫入 prefs）。"""
    from racing.race_analysis import (
        ConditionRow,
        build_analysis_prompt,
        format_columns_text,
        parse_columns_text,
        save_prefs,
        suggest_command_title,
    )

    payload = request.get_json(silent=True) or {}
    race_date = str(payload.get("race_date") or "").strip()
    if not race_date:
        return jsonify({"ok": False, "message": "請選擇賽事日期。"}), 400
    all_races = bool(payload.get("all_races", True))
    race_no = payload.get("race_no")
    try:
        race_no_i = None if all_races or race_no is None else int(race_no)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "場次無效。"}), 400
    columns_text = str(payload.get("columns_text") or "").strip()
    columns = payload.get("columns") or []
    if columns_text:
        columns = parse_columns_text(columns_text)
    elif isinstance(columns, list):
        columns = [str(c).strip() for c in columns if str(c).strip()]
    else:
        columns = []
    if not columns:
        return jsonify({"ok": False, "message": "請輸入輸出欄位（以／分隔）。"}), 400
    columns_text = format_columns_text(columns)
    sort_field = str(payload.get("sort_field") or "合計分數").strip() or "合計分數"
    sort_ascending = bool(payload.get("sort_ascending"))
    raw_conds = payload.get("conditions") or []
    conditions = [
        ConditionRow.from_dict(c) for c in raw_conds if isinstance(c, dict)
    ]
    prompt = build_analysis_prompt(
        race_date=race_date,
        race_no=race_no_i,
        all_races=all_races,
        columns=columns,
        sort_field=sort_field,
        sort_ascending=sort_ascending,
        conditions=conditions,
    )
    title = str(payload.get("title") or "").strip() or suggest_command_title(
        race_date=race_date,
        race_no=race_no_i,
        all_races=all_races,
    )
    if bool(payload.get("save_prefs", True)):
        prefs = _analysis_prefs_from_payload(payload)
        prefs["columns"] = columns
        prefs["columns_text"] = columns_text
        prefs["sort_field"] = sort_field
        prefs["sort_ascending"] = sort_ascending
        prefs["all_races"] = all_races
        prefs["conditions"] = [c.to_dict() for c in conditions]
        prefs["race_date"] = race_date
        save_prefs(cfg.data_dir, prefs)
    return jsonify({"ok": True, "prompt": prompt, "title": title})


def _ui_prefs_path() -> Path:
    return Path(cfg.data_dir) / "ui_prefs.json"


def _read_ui_prefs() -> dict[str, Any]:
    path = _ui_prefs_path()
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _write_ui_prefs(raw: dict[str, Any]) -> None:
    path = _ui_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


@app.get("/api/app/ui_prefs")
def api_app_ui_prefs_get():
    from racing.ui.theme import (
        FONT_SIZE_DEFAULT,
        FONT_SIZE_MAX,
        FONT_SIZE_MIN,
        clamp_font_size_pt,
        get_grid_header_pt,
        set_grid_font_size_pt,
    )

    raw = _read_ui_prefs()
    pt = clamp_font_size_pt(raw.get("font_size_pt", FONT_SIZE_DEFAULT))
    set_grid_font_size_pt(pt)
    return jsonify(
        {
            "ok": True,
            "font_size_pt": pt,
            "font_header_pt": get_grid_header_pt(),
            "font_size_min": FONT_SIZE_MIN,
            "font_size_max": FONT_SIZE_MAX,
            "font_size_default": FONT_SIZE_DEFAULT,
            "main_menu_style": str(raw.get("main_menu_style") or "treeview"),
        }
    )


@app.post("/api/app/ui_prefs")
def api_app_ui_prefs_save():
    from racing.ui.theme import (
        FONT_SIZE_DEFAULT,
        FONT_SIZE_MAX,
        FONT_SIZE_MIN,
        clamp_font_size_pt,
        get_grid_header_pt,
        set_grid_font_size_pt,
    )

    payload = request.get_json(silent=True) or {}
    raw = _read_ui_prefs()
    if "font_size_pt" in payload:
        pt = clamp_font_size_pt(payload.get("font_size_pt", FONT_SIZE_DEFAULT))
        raw["font_size_pt"] = pt
        set_grid_font_size_pt(pt)
    if "main_menu_style" in payload:
        style = str(payload.get("main_menu_style") or "").strip()
        if style:
            raw["main_menu_style"] = style
    _write_ui_prefs(raw)
    pt = clamp_font_size_pt(raw.get("font_size_pt", FONT_SIZE_DEFAULT))
    return jsonify(
        {
            "ok": True,
            "font_size_pt": pt,
            "font_header_pt": get_grid_header_pt(),
            "font_size_min": FONT_SIZE_MIN,
            "font_size_max": FONT_SIZE_MAX,
            "font_size_default": FONT_SIZE_DEFAULT,
            "main_menu_style": str(raw.get("main_menu_style") or "treeview"),
        }
    )


@app.post("/api/app/backup")
def api_app_backup():
    init_db(DB_PATH)
    backup_dir = DB_PATH.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"Racing_{stamp}.sqlite3"
    try:
        import shutil

        shutil.copy2(str(DB_PATH), str(dst))
        return jsonify({"ok": True, "message": f"備份完成：{dst}"})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"備份失敗：{exc}"}), 500


@app.post("/api/app/ai")
def api_app_ai():
    from racing.ai_engine import append_chat, answer

    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "message": "請輸入問題。"}), 400
    append_chat(DB_PATH, "user", question)
    ans = answer(DB_PATH, question)
    append_chat(DB_PATH, "assistant", ans.text or "")
    return jsonify(
        {
            "ok": True,
            "text": ans.text,
            "columns": ans.columns,
            "rows": ans.rows,
            "action": ans.action,
            "action_arg": ans.action_arg,
        }
    )


@app.get("/api/app/ai/chat")
def api_app_ai_chat_get():
    from racing.ai_engine import load_chat, user_prompts

    rows = load_chat(DB_PATH)
    welcome = (
        "可以直接問賽馬資料，例如：\n"
        "某馬名的最佳途程、某馬名的贏的賠率、第1場相同途程的最快速度排列、"
        "今天贏馬的騎師排列、今天贏馬的練馬師排列。\n"
        "輸入馬名、烙號或關鍵字，會列出相關往績。\n"
        "也可以說「開啟排位」「下載資料」「下載排位」「下載馬匹及往績」、"
        "「指定時間下載 23:00」「開啟下載」。\n"
        "私人文件按「上傳文件」後，再問「在文件中找……」或「統計文件」。"
    )
    return jsonify(
        {
            "ok": True,
            "welcome": welcome,
            "prompts": user_prompts(DB_PATH),
            "rows": [
                {"role": str(r["ROLE"]), "content": str(r["CONTENT"])} for r in rows
            ],
        }
    )


@app.delete("/api/app/ai/chat")
def api_app_ai_chat_clear():
    from racing.ai_engine import clear_chat

    clear_chat(DB_PATH)
    return jsonify({"ok": True, "message": "已清除對話"})


@app.get("/api/app/ai/commands")
def api_app_ai_commands_get():
    from racing.ai_engine import list_commands

    rows = list_commands(DB_PATH)
    return jsonify(
        {
            "ok": True,
            "commands": [
                {
                    "id": int(r["ID"]),
                    "title": str(r["CMD_TITLE"] or ""),
                    "prompt": str(r["CMD_PROMPT"] or ""),
                }
                for r in rows
            ],
        }
    )


@app.post("/api/app/ai/commands")
def api_app_ai_commands_save():
    from racing.ai_engine import save_command

    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    title = str(payload.get("title") or "").strip()
    try:
        cid = save_command(DB_PATH, prompt, title)
        return jsonify({"ok": True, "id": cid, "message": f"已儲存指令（#{cid}）"})
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.delete("/api/app/ai/commands/<int:command_id>")
def api_app_ai_commands_delete(command_id: int):
    from racing.ai_engine import delete_command

    delete_command(DB_PATH, command_id)
    return jsonify({"ok": True, "message": f"已刪除指令（#{command_id}）"})


def _task_settings_path() -> Path:
    return cfg.data_dir / "task_settings.json"


def _load_task_raw() -> dict[str, Any]:
    path = _task_settings_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_task_raw(raw: dict[str, Any]) -> None:
    path = _task_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _download_dialog_view(raw: dict[str, Any]) -> dict[str, Any]:
    """與 Windows DownloadDialog 開啟時的模式與勾選相同。"""
    if bool(raw.get("enable_interval_task")):
        mode = "interval"
        horses = bool(raw.get("interval_horses"))
        racecard = bool(raw.get("interval_racecard"))
        odds = bool(raw.get("interval_odds"))
        results = bool(raw.get("interval_results"))
        jockey_king = bool(raw.get("interval_jockey_king"))
        trainer_king = bool(raw.get("interval_trainer_king"))
        football_odds = bool(raw.get("interval_football_odds"))
        football_results = bool(raw.get("interval_football_results"))
        epl = bool(raw.get("interval_epl"))
    elif (
        raw.get("enable_horses_task")
        or raw.get("enable_racecard_task")
        or raw.get("enable_odds_task")
        or raw.get("enable_results_task")
        or raw.get("enable_jockey_king_task")
        or raw.get("enable_trainer_king_task")
        or raw.get("enable_football_odds_task")
        or raw.get("enable_football_results_task")
        or raw.get("enable_epl_task")
    ):
        mode = "schedule"
        horses = bool(raw.get("enable_horses_task"))
        racecard = bool(raw.get("enable_racecard_task"))
        odds = bool(raw.get("enable_odds_task") or raw.get("enable_racecard_task"))
        results = bool(raw.get("enable_results_task"))
        jockey_king = bool(raw.get("enable_jockey_king_task"))
        trainer_king = bool(raw.get("enable_trainer_king_task"))
        football_odds = bool(raw.get("enable_football_odds_task"))
        football_results = bool(raw.get("enable_football_results_task"))
        epl = bool(raw.get("enable_epl_task"))
    else:
        mode = "once"
        horses = bool(raw.get("run_horses", True))
        racecard = bool(raw.get("run_racecard", True))
        odds = bool(raw.get("run_odds", True))
        results = bool(raw.get("run_results", False))
        jockey_king = bool(raw.get("run_jockey_king", False))
        trainer_king = bool(raw.get("run_trainer_king", False))
        football_odds = bool(raw.get("run_football_odds", False))
        football_results = bool(raw.get("run_football_results", False))
        epl = bool(raw.get("run_epl", False))
    preset = (
        str(raw.get("odds_task_time") or "")
        or str(raw.get("racecard_task_time") or "")
        or str(raw.get("horses_task_time") or "")
        or "23:00"
    )
    try:
        minutes = max(1, min(180, int(raw.get("interval_minutes") or 5)))
    except (TypeError, ValueError):
        minutes = 5
    return {
        "mode": mode,
        "time": preset,
        "minutes": minutes,
        "horses": horses,
        "racecard": racecard,
        "odds": odds,
        "results": results,
        "jockey_king": jockey_king,
        "trainer_king": trainer_king,
        "football_odds": football_odds,
        "football_results": football_results,
        "epl": epl,
    }


def _download_scope_message(
    horses: bool,
    racecard: bool,
    odds: bool,
    results: bool,
    jockey_king: bool = False,
    trainer_king: bool = False,
    football_odds: bool = False,
    football_results: bool = False,
    epl: bool = False,
) -> str:
    parts: list[str] = []
    if horses:
        parts.append("馬匹及往績")
    if racecard:
        parts.append("排位")
    if odds and not racecard:
        parts.append("即時賠率")
    elif odds and racecard:
        parts.append("即時賠率（隨排位）")
    if results:
        parts.append("賽馬結果")
    if jockey_king:
        parts.append("騎師王")
    if trainer_king:
        parts.append("練馬師王")
    if football_odds:
        parts.append("足球賠率")
    if football_results:
        parts.append("足球賽果")
    if epl:
        parts.append("英超賽程/賽果")
    return "、".join(parts)


def _apply_download_settings(payload: dict[str, Any]) -> str:
    """寫入與 Windows「儲存設定／執行」相同的 task_settings 欄位。"""
    global _schedule_not_before

    mode = str(payload.get("mode") or "once").strip().lower()
    if mode not in {"once", "schedule", "interval"}:
        mode = "once"
    horses = bool(payload.get("horses"))
    racecard = bool(payload.get("racecard"))
    odds = bool(payload.get("odds"))
    results = bool(payload.get("results"))
    jockey_king = bool(payload.get("jockey_king"))
    trainer_king = bool(payload.get("trainer_king"))
    football_odds = bool(payload.get("football_odds"))
    football_results = bool(payload.get("football_results"))
    epl = bool(payload.get("epl"))
    when = str(payload.get("time") or "23:00").strip() or "23:00"
    if len(when) >= 5:
        when = when[:5]
    try:
        minutes = max(1, min(180, int(payload.get("minutes") or 5)))
    except (TypeError, ValueError):
        minutes = 5
    raw = _load_task_raw()
    scope = _download_scope_message(
        horses,
        racecard,
        odds,
        results,
        jockey_king,
        trainer_king,
        football_odds,
        football_results,
        epl,
    )
    if mode == "interval":
        # 立刻寫入 last_interval_at，避免儲存後排程在「首次手動執行」期間誤判到期
        now_iso = datetime.now().isoformat(timespec="seconds")
        raw.update(
            {
                "enable_interval_task": True,
                "interval_minutes": minutes,
                "interval_horses": horses,
                "interval_racecard": racecard,
                "interval_odds": odds,
                "interval_results": results,
                "interval_jockey_king": jockey_king,
                "interval_trainer_king": trainer_king,
                "interval_football_odds": football_odds,
                "interval_football_results": football_results,
                "interval_epl": epl,
                "last_interval_at": now_iso,
                "enable_horses_task": False,
                "enable_racecard_task": False,
                "enable_odds_task": False,
                "enable_results_task": False,
                "enable_jockey_king_task": False,
                "enable_trainer_king_task": False,
                "enable_football_odds_task": False,
                "enable_football_results_task": False,
                "enable_epl_task": False,
                "run_horses": horses,
                "run_racecard": racecard,
                "run_odds": odds,
                "run_results": results,
                "run_jockey_king": jockey_king,
                "run_trainer_king": trainer_king,
                "run_football_odds": football_odds,
                "run_football_results": football_results,
                "run_epl": epl,
            }
        )
        _schedule_not_before = datetime.now()
        message = (
            f"已設定每隔 {minutes} 分鐘下載{scope}。"
            f"按「執行」先下載一次，之後由伺服器自動重複（需保持開啟）。"
        )
    elif mode == "schedule":
        raw.update(
            {
                "enable_horses_task": horses,
                "enable_racecard_task": racecard,
                "enable_odds_task": odds and not racecard,
                "enable_results_task": results,
                "enable_jockey_king_task": jockey_king,
                "enable_trainer_king_task": trainer_king,
                "enable_football_odds_task": football_odds,
                "enable_football_results_task": football_results,
                "enable_epl_task": epl,
                "enable_interval_task": False,
                "run_horses": horses,
                "run_racecard": racecard,
                "run_odds": odds,
                "run_results": results,
                "run_jockey_king": jockey_king,
                "run_trainer_king": trainer_king,
                "run_football_odds": football_odds,
                "run_football_results": football_results,
                "run_epl": epl,
            }
        )
        if horses:
            raw["horses_task_time"] = when
        if racecard:
            raw["racecard_task_time"] = when
        if odds:
            raw["odds_task_time"] = when
        if results:
            raw["results_task_time"] = when
        if jockey_king:
            raw["jockey_king_task_time"] = when
        if trainer_king:
            raw["trainer_king_task_time"] = when
        if football_odds:
            raw["football_odds_task_time"] = when
        if football_results:
            raw["football_results_task_time"] = when
        if epl:
            raw["epl_task_time"] = when
        _schedule_not_before = datetime.now()
        message = f"已設定每日 {when} 下載{scope}。開啟程式時不會補抓。"
    else:
        raw.update(
            {
                "enable_interval_task": False,
                "enable_horses_task": False,
                "enable_racecard_task": False,
                "enable_odds_task": False,
                "enable_results_task": False,
                "enable_jockey_king_task": False,
                "enable_trainer_king_task": False,
                "enable_football_odds_task": False,
                "enable_football_results_task": False,
                "enable_epl_task": False,
                "run_horses": horses,
                "run_racecard": racecard,
                "run_odds": odds,
                "run_results": results,
                "run_jockey_king": jockey_king,
                "run_trainer_king": trainer_king,
                "run_football_odds": football_odds,
                "run_football_results": football_results,
                "run_epl": epl,
            }
        )
        message = (
            f"下載設定已儲存：{scope or '無'}。"
            "於下載視窗按「執行」才會開始下載。"
        )
    _save_task_raw(raw)
    _ensure_download_scheduler()
    return message


def _sources_payload(sources) -> list[dict[str, Any]]:
    return [
        {"kind": s.kind, "name": s.name, "url": s.url, "enabled": s.enabled}
        for s in sources
    ]


@app.get("/api/app/download_sources")
def api_app_download_sources_get():
    from racing.download_sources import KIND_HINTS, KIND_HORSE_PP, KIND_LABELS, load_download_sources

    sources = load_download_sources(cfg.data_dir)
    return jsonify(
        {
            "ok": True,
            "kinds": KIND_LABELS,
            "hints": KIND_HINTS,
            "horse_pp_kind": KIND_HORSE_PP,
            "settings": _download_dialog_view(_load_task_raw()),
            "sources": _sources_payload(sources),
        }
    )


@app.post("/api/app/download_sources")
def api_app_download_sources_save():
    from racing.download_sources import KIND_LABELS, DownloadSource, save_download_sources

    payload = request.get_json(silent=True) or {}
    raw = payload.get("sources") or []
    sources: list[DownloadSource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        url = str(item.get("url") or "").strip()
        name = str(item.get("name") or "").strip()
        if kind not in KIND_LABELS or not url:
            continue
        if not name:
            name = KIND_LABELS.get(kind, kind)
        sources.append(
            DownloadSource(
                kind=kind,
                name=name,
                url=url,
                enabled=bool(item.get("enabled", True)),
            )
        )
    if not sources:
        return jsonify({"ok": False, "message": "沒有可儲存的網站來源"}), 400
    save_download_sources(cfg.data_dir, sources)
    return jsonify({"ok": True, "message": f"已儲存 {len(sources)} 筆下載來源", "sources": [
        {"kind": s.kind, "name": s.name, "url": s.url, "enabled": s.enabled} for s in sources
    ]})


@app.post("/api/app/download_sources/reset")
def api_app_download_sources_reset():
    from racing.download_sources import default_download_sources, save_download_sources

    sources = default_download_sources()
    save_download_sources(cfg.data_dir, sources)
    return jsonify(
        {
            "ok": True,
            "message": "已還原預設網站清單",
            "sources": _sources_payload(sources),
        }
    )


@app.post("/api/app/download_settings")
def api_app_download_settings_save():
    payload = request.get_json(silent=True) or {}
    horses = bool(payload.get("horses"))
    racecard = bool(payload.get("racecard"))
    odds = bool(payload.get("odds"))
    results = bool(payload.get("results"))
    jockey_king = bool(payload.get("jockey_king"))
    trainer_king = bool(payload.get("trainer_king"))
    football_odds = bool(payload.get("football_odds"))
    football_results = bool(payload.get("football_results"))
    epl = bool(payload.get("epl"))
    if not (
        horses
        or racecard
        or odds
        or results
        or jockey_king
        or trainer_king
        or football_odds
        or football_results
        or epl
    ):
        return jsonify({"ok": False, "message": "請至少選擇一項要下載的資料。"}), 400
    message = _apply_download_settings(payload)
    return jsonify({"ok": True, "message": message})


def _norm_hhmm(value: object, default: str = "03:00") -> str:
    text = str(value or "").strip() or default
    if len(text) >= 5:
        text = text[:5]
    try:
        hh, mm = text.split(":", 1)
        h_i, m_i = int(hh), int(mm)
        if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
            return default
        return f"{h_i:02d}:{m_i:02d}"
    except (TypeError, ValueError):
        return default


def _task_settings_view(raw: dict[str, Any]) -> dict[str, Any]:
    """與 Windows TaskSettingsDialog 對齊的欄位。"""
    template = str(
        raw.get("backup_filename_template") or "Racing_%Y%m%d_%H%M.sqlite3"
    )
    backup_time = _norm_hhmm(raw.get("backup_time"), "02:55")
    try:
        hh_str, mm_str = backup_time.split(":")
        now_dt = datetime.now()
        backup_dt = datetime(
            now_dt.year, now_dt.month, now_dt.day, int(hh_str), int(mm_str)
        )
        backup_filename = backup_dt.strftime(template)
    except (TypeError, ValueError):
        backup_filename = template
    return {
        "enable_horses_task": bool(raw.get("enable_horses_task")),
        "horses_task_time": _norm_hhmm(raw.get("horses_task_time"), "03:00"),
        "enable_racecard_task": bool(raw.get("enable_racecard_task")),
        "racecard_task_time": _norm_hhmm(raw.get("racecard_task_time"), "04:00"),
        "enable_backup": bool(raw.get("enable_backup")),
        "backup_folder": str(raw.get("backup_folder") or ""),
        "backup_time": backup_time,
        "backup_filename_template": template,
        "backup_filename": backup_filename,
        "last_run_horses": str(raw.get("last_run_horses") or ""),
        "last_run_racecard": str(raw.get("last_run_racecard") or ""),
        "last_backup": str(raw.get("last_backup") or ""),
    }


@app.get("/api/app/task_settings")
def api_app_task_settings_get():
    return jsonify({"ok": True, "settings": _task_settings_view(_load_task_raw())})


@app.post("/api/app/task_settings")
def api_app_task_settings_save():
    """儲存系統設定 → 任務設定（對齊 Windows TaskSettingsDialog）。"""
    global _schedule_not_before

    payload = request.get_json(silent=True) or {}
    horses_time = _norm_hhmm(payload.get("horses_task_time"), "03:00")
    racecard_time = _norm_hhmm(payload.get("racecard_task_time"), "04:00")
    backup_time = _norm_hhmm(payload.get("backup_time"), "02:55")
    enable_horses = bool(payload.get("enable_horses_task"))
    enable_racecard = bool(payload.get("enable_racecard_task"))
    enable_backup = bool(payload.get("enable_backup"))
    backup_folder = str(payload.get("backup_folder") or "").strip()

    raw = _load_task_raw()
    raw.update(
        {
            "enable_horses_task": enable_horses,
            "horses_task_time": horses_time,
            "enable_racecard_task": enable_racecard,
            "racecard_task_time": racecard_time,
            "enable_backup": enable_backup,
            "backup_folder": backup_folder,
            "backup_time": backup_time,
        }
    )
    if "backup_filename_template" not in raw or not raw.get("backup_filename_template"):
        raw["backup_filename_template"] = "Racing_%Y%m%d_%H%M.sqlite3"
    # 變更每日排程後，不補抓已過時間
    if enable_horses or enable_racecard or enable_backup:
        _schedule_not_before = datetime.now()
    _save_task_raw(raw)
    view = _task_settings_view(raw)
    parts: list[str] = []
    if enable_horses:
        parts.append(f"馬匹/往績 {horses_time}")
    if enable_racecard:
        parts.append(f"排位 {racecard_time}")
    if enable_backup:
        parts.append(f"備份 {backup_time}")
    detail = "、".join(parts) if parts else "（皆未啟用自動任務）"
    return jsonify(
        {
            "ok": True,
            "message": f"任務設定已儲存：{detail}",
            "settings": view,
        }
    )


_download_job_lock = threading.Lock()
_download_cancel = threading.Event()
_download_running = False
_download_progress_lock = threading.Lock()
_download_progress: dict[str, Any] = {
    "message": "就緒",
    "current": 0,
    "total": 0,
    "last_result": "",
}
_schedule_not_before = datetime.now()
_scheduler_started = False
_scheduler_start_lock = threading.Lock()


def _download_cancelled() -> bool:
    return _download_cancel.is_set()


def _set_download_progress(
    message: str, *, current: int = 0, total: int = 0
) -> None:
    with _download_progress_lock:
        _download_progress["message"] = str(message or "")
        _download_progress["current"] = max(0, int(current))
        _download_progress["total"] = max(0, int(total))


def _snapshot_download_progress() -> dict[str, Any]:
    with _download_progress_lock:
        return {
            "message": str(_download_progress.get("message") or ""),
            "current": int(_download_progress.get("current") or 0),
            "total": int(_download_progress.get("total") or 0),
            "last_result": str(_download_progress.get("last_result") or ""),
        }


def _with_download_finished_at(message: str) -> str:
    """下載結束訊息末尾加上完成時間（可重複呼叫，會換新時間）。"""
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = str(message or "").strip() or "下載完成"
    msg = re.sub(
        r"(?:｜)?完成時間\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}",
        "",
        msg,
    ).strip("｜").strip()
    if not msg:
        msg = "下載完成"
    return f"{msg}｜完成時間 {when}"


def _set_download_last_result(message: str) -> None:
    stamped = _with_download_finished_at(message)
    with _download_progress_lock:
        _download_progress["last_result"] = stamped
        _download_progress["message"] = stamped
        _download_progress["current"] = 0
        _download_progress["total"] = 0


def _mark_interval_done() -> None:
    raw = _load_task_raw()
    raw["last_interval_at"] = datetime.now().isoformat(timespec="seconds")
    _save_task_raw(raw)


def _mark_schedule_done(
    *,
    horses: bool,
    racecard: bool,
    odds: bool,
    results: bool,
    jockey_king: bool = False,
    trainer_king: bool = False,
    football_odds: bool = False,
    football_results: bool = False,
    epl: bool = False,
) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    raw = _load_task_raw()
    if horses:
        raw["last_run_horses"] = today
    if racecard:
        raw["last_run_racecard"] = today
    if odds or racecard:
        raw["last_run_odds"] = today
    if results:
        raw["last_run_results"] = today
    if jockey_king:
        raw["last_run_jockey_king"] = today
    if trainer_king:
        raw["last_run_trainer_king"] = today
    if football_odds:
        raw["last_run_football_odds"] = today
    if football_results:
        raw["last_run_football_results"] = today
    if epl:
        raw["last_run_epl"] = today
    _save_task_raw(raw)


def _scheduled_moment(hhmm: str, now: datetime) -> datetime:
    hour_text, minute_text = hhmm.split(":") if ":" in hhmm else ("00", "00")
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        hour, minute = 0, 0
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _is_schedule_due(hhmm: str, last_run: str, now: datetime) -> bool:
    if (last_run or "").strip() == now.strftime("%Y-%m-%d"):
        return False
    moment = _scheduled_moment(str(hhmm or "00:00"), now)
    if moment < _schedule_not_before:
        return False
    return now >= moment


def _interval_due(raw: dict[str, Any], now: datetime) -> bool:
    if not bool(raw.get("enable_interval_task")):
        return False
    if not (
        raw.get("interval_horses")
        or raw.get("interval_racecard")
        or raw.get("interval_odds")
        or raw.get("interval_results")
        or raw.get("interval_jockey_king")
        or raw.get("interval_trainer_king")
        or raw.get("interval_football_odds")
        or raw.get("interval_football_results")
        or raw.get("interval_epl")
    ):
        return False
    try:
        minutes = max(1, int(raw.get("interval_minutes") or 5))
    except (TypeError, ValueError):
        minutes = 5
    last_text = str(raw.get("last_interval_at") or "").strip()
    if not last_text:
        return now >= _schedule_not_before
    try:
        last_at = datetime.fromisoformat(last_text)
    except ValueError:
        return True
    return now >= last_at + timedelta(minutes=minutes)


def _execute_download_jobs(
    *,
    do_horses: bool,
    do_racecard: bool,
    do_odds: bool,
    do_results: bool,
    do_jockey_king: bool = False,
    do_trainer_king: bool = False,
    do_football_odds: bool = False,
    do_football_results: bool = False,
    do_epl: bool = False,
) -> dict[str, Any]:
    """執行下載；回傳 {ok, cancelled?, message, status?}。呼叫端須已取得 _download_job_lock。"""
    from racing.challenge_kings import (
        download_jockey_king,
        download_trainer_king,
        format_challenge_stats,
    )
    from racing.db import fetch_horses, upsert_race_records
    from racing.download_sources import horse_pp_template
    from racing.football import (
        download_epl_schedule_results,
        download_football_odds,
        download_football_results,
    )
    from racing.race_results import download_race_results
    from racing.scraper import scrape_horse_past_performance, update_horses_table_from_web
    from racing.ui.racecard_rank_viewer import (
        update_immediate_odds_for_latest_meeting,
        update_latest_racecard_data,
    )

    parts: list[str] = []
    try:
        init_db(DB_PATH)
        if do_horses:
            if _download_cancelled():
                return {"ok": False, "cancelled": True, "message": "已取消下載。"}
            _set_download_progress("正在下載馬匹清單…")
            horses_stats = update_horses_table_from_web(DB_PATH)
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": f"已取消下載（馬匹清單已寫入 {horses_stats.written}）。",
                }
            rows = fetch_horses(DB_PATH, limit=None)
            success = failed = fetched_total = inserted_total = 0
            total = len(rows)
            template = horse_pp_template(cfg.data_dir, cfg.hkjc_horse_url_template)
            _set_download_progress("正在下載馬匹往績…", current=0, total=total)
            for index, row in enumerate(rows, start=1):
                if _download_cancelled():
                    parts.append(
                        f"馬匹寫入 {horses_stats.written}｜往績成功 {success}/{total}"
                        f"（失敗 {failed}，已取消）｜抓到 {fetched_total}｜新增 {inserted_total}"
                    )
                    return {
                        "ok": False,
                        "cancelled": True,
                        "message": "已取消下載｜" + "｜".join(parts),
                    }
                horse_id = str(row["horse_id"] or "").strip()
                if not horse_id:
                    _set_download_progress(
                        f"馬匹往績 {index}/{total}", current=index, total=total
                    )
                    continue
                _set_download_progress(
                    f"馬匹往績 {index}/{total}：{horse_id}",
                    current=index,
                    total=total,
                )
                try:
                    result = scrape_horse_past_performance(
                        horse_id, template, db_path=DB_PATH
                    )
                    inserted = upsert_race_records(DB_PATH, result.records)
                    fetched_total += len(result.records)
                    inserted_total += inserted
                    success += 1
                except Exception:  # noqa: BLE001
                    failed += 1
            parts.append(
                f"馬匹寫入 {horses_stats.written}｜往績成功 {success}/{total}"
                f"（失敗 {failed}）｜抓到 {fetched_total}｜新增 {inserted_total}"
            )
        if do_racecard:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            _set_download_progress("正在下載排位與即時賠率…")
            meeting = update_latest_racecard_data(cfg)
            if meeting is None:
                parts.append("排位：找不到可更新的賽馬日")
            else:
                parts.append(
                    f"排位完成：{meeting.racedate} {meeting.racecourse_code}（含即時賠率）"
                )
        if do_odds:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            _set_download_progress("正在下載即時賠率…")
            meeting = update_immediate_odds_for_latest_meeting(cfg)
            if meeting is None:
                parts.append("即時賠率：資料庫尚無排位，請先下載排位")
            else:
                parts.append(
                    f"即時賠率完成：{meeting.racedate} {meeting.racecourse_code}"
                )
        if do_results:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }

            def _results_progress(cur: int, total: int, msg: str) -> None:
                _set_download_progress(msg, current=cur, total=total)

            stats = download_race_results(
                DB_PATH,
                data_dir=cfg.data_dir,
                progress=_results_progress,
                should_cancel=_download_cancelled,
            )
            if _download_cancelled():
                parts.append("賽馬結果：已取消")
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + "｜".join(parts),
                }
            if stats.meeting is None:
                parts.append("賽馬結果：找不到可下載的賽日")
            else:
                parts.append(
                    f"賽果完成：{stats.meeting.racedate} {stats.meeting.racecourse_code}"
                    f"｜場次 {stats.races}｜列 {stats.rows}｜新增 {stats.inserted}"
                )
        if do_jockey_king:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            jk = download_jockey_king(
                DB_PATH,
                data_dir=cfg.data_dir,
                progress=lambda msg: _set_download_progress(msg),
                should_cancel=_download_cancelled,
            )
            parts.append(format_challenge_stats(jk))
        if do_trainer_king:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            tk = download_trainer_king(
                DB_PATH,
                data_dir=cfg.data_dir,
                progress=lambda msg: _set_download_progress(msg),
                should_cancel=_download_cancelled,
            )
            parts.append(format_challenge_stats(tk))
        if do_football_odds:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            fb_odds = download_football_odds(
                DB_PATH,
                data_dir=cfg.data_dir,
                progress=lambda msg: _set_download_progress(msg),
                should_cancel=_download_cancelled,
            )
            parts.append(fb_odds.message)
        if do_football_results:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            fb_res = download_football_results(
                DB_PATH,
                data_dir=cfg.data_dir,
                progress=lambda msg: _set_download_progress(msg),
                should_cancel=_download_cancelled,
            )
            parts.append(fb_res.message)
        if do_epl:
            if _download_cancelled():
                return {
                    "ok": False,
                    "cancelled": True,
                    "message": "已取消下載｜" + ("｜".join(parts) if parts else ""),
                }
            epl_stats = download_epl_schedule_results(
                DB_PATH,
                data_dir=cfg.data_dir,
                progress=lambda msg: _set_download_progress(msg),
                should_cancel=_download_cancelled,
            )
            parts.append(epl_stats.message)
        msg = "｜".join(parts) if parts else "沒有選擇要下載的項目。"
        return {"ok": True, "message": msg}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"下載失敗：{exc}", "status": 500}


def _try_run_download(
    *,
    do_horses: bool,
    do_racecard: bool,
    do_odds: bool,
    do_results: bool,
    do_jockey_king: bool = False,
    do_trainer_king: bool = False,
    do_football_odds: bool = False,
    do_football_results: bool = False,
    do_epl: bool = False,
) -> dict[str, Any] | None:
    """嘗試取得鎖並執行下載；忙碌時回傳 None。"""
    global _download_running
    if not (
        do_horses
        or do_racecard
        or do_odds
        or do_results
        or do_jockey_king
        or do_trainer_king
        or do_football_odds
        or do_football_results
        or do_epl
    ):
        return {"ok": False, "message": "沒有選擇要下載的項目。", "status": 400}
    if not _download_job_lock.acquire(blocking=False):
        return None
    _download_cancel.clear()
    _download_running = True
    _set_download_progress("背景下載開始…")
    try:
        result = _execute_download_jobs(
            do_horses=do_horses,
            do_racecard=do_racecard,
            do_odds=do_odds,
            do_results=do_results,
            do_jockey_king=do_jockey_king,
            do_trainer_king=do_trainer_king,
            do_football_odds=do_football_odds,
            do_football_results=do_football_results,
            do_epl=do_epl,
        )
        result["message"] = _with_download_finished_at(str(result.get("message") or ""))
        _set_download_last_result(str(result.get("message") or ""))
        return result
    finally:
        _download_running = False
        _download_job_lock.release()


def _check_scheduled_downloads() -> None:
    """背景排程：間隔下載／每日指定時間（與 Windows QTimer 行為對齊）。"""
    global _scheduler_heartbeat
    _scheduler_heartbeat = datetime.now()
    now = datetime.now()
    raw = _load_task_raw()
    if _interval_due(raw, now):
        print(
            f"[間隔下載] 開始（每 {raw.get('interval_minutes') or 5} 分）"
            f" @ {_scheduler_heartbeat.isoformat(timespec='seconds')}"
        )
        result = _try_run_download(
            do_horses=bool(raw.get("interval_horses")),
            do_racecard=bool(raw.get("interval_racecard")),
            do_odds=bool(raw.get("interval_odds")) and not bool(raw.get("interval_racecard")),
            do_results=bool(raw.get("interval_results")),
            do_jockey_king=bool(raw.get("interval_jockey_king")),
            do_trainer_king=bool(raw.get("interval_trainer_king")),
            do_football_odds=bool(raw.get("interval_football_odds")),
            do_football_results=bool(raw.get("interval_football_results")),
            do_epl=bool(raw.get("interval_epl")),
        )
        if result is None:
            # 忙碌時不推進 last_interval_at，下一個 tick 會再試
            print("[間隔下載] 略過：已有下載進行中（稍後重試）")
            return
        # 間隔從「實際跑完」起算（成功／取消／失敗都記，避免緊密重試）
        _mark_interval_done()
        if result.get("ok") and not result.get("cancelled"):
            print(f"[間隔下載] 完成：{result.get('message')}")
        elif result.get("cancelled"):
            print(f"[間隔下載] 已取消：{result.get('message')}")
        else:
            print(f"[間隔下載] 失敗：{result.get('message')}")
        return

    due_horses = bool(raw.get("enable_horses_task")) and _is_schedule_due(
        str(raw.get("horses_task_time") or "23:00"),
        str(raw.get("last_run_horses") or ""),
        now,
    )
    due_racecard = bool(raw.get("enable_racecard_task")) and _is_schedule_due(
        str(raw.get("racecard_task_time") or "23:00"),
        str(raw.get("last_run_racecard") or ""),
        now,
    )
    due_odds = bool(raw.get("enable_odds_task")) and _is_schedule_due(
        str(raw.get("odds_task_time") or "23:00"),
        str(raw.get("last_run_odds") or ""),
        now,
    )
    due_results = bool(raw.get("enable_results_task")) and _is_schedule_due(
        str(raw.get("results_task_time") or "23:00"),
        str(raw.get("last_run_results") or ""),
        now,
    )
    due_jockey = bool(raw.get("enable_jockey_king_task")) and _is_schedule_due(
        str(raw.get("jockey_king_task_time") or "23:00"),
        str(raw.get("last_run_jockey_king") or ""),
        now,
    )
    due_trainer = bool(raw.get("enable_trainer_king_task")) and _is_schedule_due(
        str(raw.get("trainer_king_task_time") or "23:00"),
        str(raw.get("last_run_trainer_king") or ""),
        now,
    )
    due_fb_odds = bool(raw.get("enable_football_odds_task")) and _is_schedule_due(
        str(raw.get("football_odds_task_time") or "23:00"),
        str(raw.get("last_run_football_odds") or ""),
        now,
    )
    due_fb_results = bool(raw.get("enable_football_results_task")) and _is_schedule_due(
        str(raw.get("football_results_task_time") or "23:00"),
        str(raw.get("last_run_football_results") or ""),
        now,
    )
    due_epl = bool(raw.get("enable_epl_task")) and _is_schedule_due(
        str(raw.get("epl_task_time") or "23:00"),
        str(raw.get("last_run_epl") or ""),
        now,
    )
    if not (
        due_horses
        or due_racecard
        or due_odds
        or due_results
        or due_jockey
        or due_trainer
        or due_fb_odds
        or due_fb_results
        or due_epl
    ):
        return
    result = _try_run_download(
        do_horses=due_horses,
        do_racecard=due_racecard,
        do_odds=due_odds and not due_racecard,
        do_results=due_results,
        do_jockey_king=due_jockey,
        do_trainer_king=due_trainer,
        do_football_odds=due_fb_odds,
        do_football_results=due_fb_results,
        do_epl=due_epl,
    )
    if result is None:
        return
    if result.get("ok") and not result.get("cancelled"):
        _mark_schedule_done(
            horses=due_horses,
            racecard=due_racecard,
            odds=due_odds,
            results=due_results,
            jockey_king=due_jockey,
            trainer_king=due_trainer,
            football_odds=due_fb_odds,
            football_results=due_fb_results,
            epl=due_epl,
        )
        print(f"[指定時間下載] {result.get('message')}")
    else:
        print(f"[指定時間下載] {result.get('message')}")


def _download_scheduler_loop() -> None:
    global _scheduler_heartbeat
    _scheduler_heartbeat = datetime.now()
    while True:
        time.sleep(10)
        try:
            _check_scheduled_downloads()
        except Exception as exc:  # noqa: BLE001
            print(f"[下載排程] {exc}")


def _ensure_download_scheduler() -> None:
    global _scheduler_started
    with _scheduler_start_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        threading.Thread(
            target=_download_scheduler_loop,
            name="download-scheduler",
            daemon=True,
        ).start()
        print("[下載排程] 已啟動（每 10 秒檢查間隔／指定時間任務）")


@app.post("/api/app/download_cancel")
def api_app_download_cancel():
    """即時取消所有進行中的下載（馬匹往績／排位／賠率／賽果）。"""
    global _download_running
    _download_cancel.set()
    if not _download_running:
        return jsonify({"ok": True, "message": "目前沒有進行中的下載。", "running": False})
    return jsonify(
        {
            "ok": True,
            "message": "已送出取消，正在停止所有進行中的下載…",
            "running": True,
        }
    )


@app.get("/api/app/download_status")
def api_app_download_status():
    _ensure_download_scheduler()
    snap = _snapshot_download_progress()
    current = int(snap["current"])
    total = int(snap["total"])
    message = str(snap["message"] or "")
    raw = _load_task_raw()
    interval_on = bool(raw.get("enable_interval_task"))
    try:
        minutes = max(1, int(raw.get("interval_minutes") or 5))
    except (TypeError, ValueError):
        minutes = 5
    last_text = str(raw.get("last_interval_at") or "").strip()
    next_interval_at = ""
    if interval_on and last_text:
        try:
            next_interval_at = (
                datetime.fromisoformat(last_text) + timedelta(minutes=minutes)
            ).isoformat(timespec="seconds")
        except ValueError:
            next_interval_at = ""
    elif interval_on:
        next_interval_at = datetime.now().isoformat(timespec="seconds")
    if bool(_download_running) and total > 0:
        display = f"下載：{message}（{current}/{total}）"
    elif bool(_download_running):
        display = f"下載：{message}" if message else "背景下載執行中…"
    elif interval_on and next_interval_at and not bool(_download_running):
        display = (
            str(snap["last_result"] or message or "就緒")
            + f"｜下次間隔下載約 {next_interval_at[11:16]}"
        )
    else:
        display = str(snap["last_result"] or message or "就緒")
    return jsonify(
        {
            "ok": True,
            "running": bool(_download_running),
            "message": message,
            "display": display,
            "current": current,
            "total": total,
            "last_result": snap["last_result"],
            "scheduler": bool(_scheduler_started),
            "scheduler_heartbeat": _scheduler_heartbeat.isoformat(timespec="seconds"),
            "interval_enabled": interval_on,
            "interval_minutes": minutes,
            "last_interval_at": last_text,
            "next_interval_at": next_interval_at,
        }
    )


@app.post("/api/app/download_run")
def api_app_download_run():
    """與 Windows「下載資料 → 執行」相同：依勾選項目下載（同步回傳結果；可被 cancel 中止）。"""
    _ensure_download_scheduler()
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "once").strip().lower()
    do_horses = bool(payload.get("horses"))
    do_racecard = bool(payload.get("racecard"))
    do_odds = bool(payload.get("odds")) and not do_racecard
    do_results = bool(payload.get("results"))
    do_jockey_king = bool(payload.get("jockey_king"))
    do_trainer_king = bool(payload.get("trainer_king"))
    do_football_odds = bool(payload.get("football_odds"))
    do_football_results = bool(payload.get("football_results"))
    do_epl = bool(payload.get("epl"))
    if not (
        do_horses
        or do_racecard
        or do_odds
        or do_results
        or do_jockey_king
        or do_trainer_king
        or do_football_odds
        or do_football_results
        or do_epl
    ):
        return jsonify({"ok": False, "message": "沒有選擇要下載的項目。"}), 400
    result = _try_run_download(
        do_horses=do_horses,
        do_racecard=do_racecard,
        do_odds=do_odds,
        do_results=do_results,
        do_jockey_king=do_jockey_king,
        do_trainer_king=do_trainer_king,
        do_football_odds=do_football_odds,
        do_football_results=do_football_results,
        do_epl=do_epl,
    )
    if result is None:
        return jsonify({"ok": False, "message": "已有下載進行中，請稍後再試。"}), 409
    status = int(result.pop("status", 200 if result.get("ok") else 500))
    if result.get("ok") and not result.get("cancelled"):
        if mode == "interval":
            _mark_interval_done()
            try:
                minutes = max(1, min(180, int(payload.get("minutes") or 5)))
            except (TypeError, ValueError):
                minutes = 5
            result["message"] = (
                f"{result.get('message')}｜之後每隔 {minutes} 分鐘自動下載"
                "（伺服器需保持開啟）"
            )
            result["message"] = _with_download_finished_at(str(result.get("message") or ""))
            _set_download_last_result(str(result.get("message") or ""))
        elif mode == "schedule":
            _mark_schedule_done(
                horses=do_horses,
                racecard=do_racecard,
                odds=do_odds,
                results=do_results,
                jockey_king=do_jockey_king,
                trainer_king=do_trainer_king,
                football_odds=do_football_odds,
                football_results=do_football_results,
                epl=do_epl,
            )
            when = str(payload.get("time") or "23:00")[:5]
            result["message"] = (
                f"{result.get('message')}｜已啟用每日 {when} 自動下載"
                "（伺服器需保持開啟；今日不會重跑）"
            )
            result["message"] = _with_download_finished_at(str(result.get("message") or ""))
            _set_download_last_result(str(result.get("message") or ""))
    return jsonify(result), status


def main() -> None:
    # threaded=True：可與 Windows 介面同時存取／回應多請求
    _ensure_download_scheduler()
    app.run(host="0.0.0.0", port=800, debug=False, threaded=True)


if __name__ == "__main__":
    main()

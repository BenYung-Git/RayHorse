"""下載資料來源網站：載入／儲存，供下載對話框與抓取流程共用。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import (
    HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN,
    HKJC_MCS_HORSES_CLAS_URL_EN,
)


KIND_HORSES_RATING = "horses_rating"
KIND_HORSE_PP = "horse_pp"
KIND_RACECARD = "racecard"
KIND_ODDS = "odds"
KIND_RESULTS = "results"
KIND_JOCKEY_KING = "jockey_king"
KIND_TRAINER_KING = "trainer_king"

KIND_LABELS: dict[str, str] = {
    KIND_HORSES_RATING: "馬匹清單",
    KIND_HORSE_PP: "往績",
    KIND_RACECARD: "排位",
    KIND_ODDS: "即時賠率",
    KIND_RESULTS: "賽馬結果",
    KIND_JOCKEY_KING: "騎師王",
    KIND_TRAINER_KING: "練馬師王",
}

KIND_HINTS: dict[str, str] = {
    KIND_HORSES_RATING: "馬匹評分／清單頁（依序嘗試直到成功）",
    KIND_HORSE_PP: "往績頁模板，網址需含 {horse_id}",
    KIND_RACECARD: "排位表基礎網址",
    KIND_ODDS: "即時賠率 GraphQL 端點",
    KIND_RESULTS: "賽果頁（localresults），依賽日／場地／場次抓取名次",
    KIND_JOCKEY_KING: "騎師王賽日積分頁（jkcresult）",
    KIND_TRAINER_KING: "練馬師王賽日積分頁（tncresult）",
}


@dataclass
class DownloadSource:
    kind: str
    name: str
    url: str
    enabled: bool = True


def default_download_sources() -> list[DownloadSource]:
    return [
        DownloadSource(
            KIND_HORSES_RATING,
            "Horse Rating List (MCS)",
            HKJC_MCS_HORSES_CLAS_URL_EN,
        ),
        DownloadSource(
            KIND_HORSES_RATING,
            "Horse Rating List (LatestOnHorse)",
            HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN,
        ),
        DownloadSource(
            KIND_HORSE_PP,
            "馬匹往績（中文）",
            "https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}",
        ),
        DownloadSource(
            KIND_RACECARD,
            "排位表",
            "https://racing.hkjc.com/zh-hk/local/information/racecard",
        ),
        DownloadSource(
            KIND_ODDS,
            "HKJC GraphQL（即時賠率）",
            "https://info.cld.hkjc.com/graphql/base/",
        ),
        DownloadSource(
            KIND_RESULTS,
            "賽馬結果（localresults）",
            "https://racing.hkjc.com/zh-hk/local/information/localresults",
        ),
        DownloadSource(
            KIND_JOCKEY_KING,
            "騎師王（jkcresult）",
            "https://racing.hkjc.com/zh-hk/local/information/jkcresult",
        ),
        DownloadSource(
            KIND_TRAINER_KING,
            "練馬師王（tncresult）",
            "https://racing.hkjc.com/zh-hk/local/information/tncresult",
        ),
    ]


def sources_path(data_dir: Path) -> Path:
    return Path(data_dir) / "download_sources.json"


def load_download_sources(data_dir: Path) -> list[DownloadSource]:
    path = sources_path(data_dir)
    defaults = default_download_sources()
    if not path.is_file() or path.stat().st_size == 0:
        # 檔案不存在或被清空時，寫回預設清單，避免「找不到／空白」
        try:
            save_download_sources(data_dir, defaults)
        except OSError:
            pass
        return defaults
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            save_download_sources(data_dir, defaults)
            return defaults
        raw = json.loads(text)
    except (OSError, json.JSONDecodeError):
        try:
            save_download_sources(data_dir, defaults)
        except OSError:
            pass
        return defaults
    if not isinstance(raw, list):
        save_download_sources(data_dir, defaults)
        return defaults
    out: list[DownloadSource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if kind not in KIND_LABELS or not url:
            continue
        if not name:
            name = KIND_LABELS.get(kind, kind)
        out.append(
            DownloadSource(
                kind=kind,
                name=name,
                url=url,
                enabled=bool(item.get("enabled", True)),
            )
        )
    if not out:
        save_download_sources(data_dir, defaults)
        return defaults
    # 補上預設清單中缺少的種類（例如新增「賽馬結果」）
    have_kinds = {s.kind for s in out}
    changed = False
    for src in defaults:
        if src.kind not in have_kinds:
            out.append(src)
            have_kinds.add(src.kind)
            changed = True
    if changed:
        try:
            save_download_sources(data_dir, out)
        except OSError:
            pass
    return out


def save_download_sources(data_dir: Path, sources: list[DownloadSource]) -> None:
    path = sources_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(s) for s in sources]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not text.strip():
        text = json.dumps(
            [asdict(s) for s in default_download_sources()],
            ensure_ascii=False,
            indent=2,
        )
    path.write_text(text + "\n", encoding="utf-8")


def urls_for_kind(
    data_dir: Path,
    kind: str,
    *,
    enabled_only: bool = True,
) -> list[str]:
    urls: list[str] = []
    for src in load_download_sources(data_dir):
        if src.kind != kind:
            continue
        if enabled_only and not src.enabled:
            continue
        u = (src.url or "").strip()
        if u:
            urls.append(u)
    return urls


def first_url_for_kind(data_dir: Path, kind: str, fallback: str) -> str:
    urls = urls_for_kind(data_dir, kind, enabled_only=True)
    return urls[0] if urls else fallback


def horse_pp_template(data_dir: Path, fallback: str) -> str:
    return first_url_for_kind(data_dir, KIND_HORSE_PP, fallback)


def horses_rating_urls(data_dir: Path) -> list[str]:
    urls = urls_for_kind(data_dir, KIND_HORSES_RATING, enabled_only=True)
    if urls:
        return urls
    return [
        HKJC_MCS_HORSES_CLAS_URL_EN,
        HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN,
    ]


def racecard_base_url(data_dir: Path | None = None) -> str:
    fallback = "https://racing.hkjc.com/zh-hk/local/information/racecard"
    if data_dir is None:
        return fallback
    return first_url_for_kind(data_dir, KIND_RACECARD, fallback)


def odds_graphql_url(data_dir: Path | None = None) -> str:
    fallback = "https://info.cld.hkjc.com/graphql/base/"
    if data_dir is None:
        return fallback
    return first_url_for_kind(data_dir, KIND_ODDS, fallback)


def results_base_url(data_dir: Path | None = None) -> str:
    fallback = "https://racing.hkjc.com/zh-hk/local/information/localresults"
    if data_dir is None:
        return fallback
    return first_url_for_kind(data_dir, KIND_RESULTS, fallback)


def jockey_king_base_url(data_dir: Path | None = None) -> str:
    fallback = "https://racing.hkjc.com/zh-hk/local/information/jkcresult"
    if data_dir is None:
        return fallback
    return first_url_for_kind(data_dir, KIND_JOCKEY_KING, fallback)


def trainer_king_base_url(data_dir: Path | None = None) -> str:
    fallback = "https://racing.hkjc.com/zh-hk/local/information/tncresult"
    if data_dir is None:
        return fallback
    return first_url_for_kind(data_dir, KIND_TRAINER_KING, fallback)


def display_lines(data_dir: Path) -> list[str]:
    """給 UI／標籤用的來源說明列。"""
    lines: list[str] = []
    for src in load_download_sources(data_dir):
        mark = "✓" if src.enabled else "×"
        label = KIND_LABELS.get(src.kind, src.kind)
        lines.append(f"[{mark}] {label}｜{src.name}｜{src.url}")
    return lines

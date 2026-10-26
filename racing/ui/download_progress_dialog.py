"""背景下載：馬匹／往績、排位、即時賠率、賽馬結果、騎師王／練馬師王、足球賠率／賽果（進度由呼叫端顯示於狀態列）。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..challenge_kings import (
    download_jockey_king,
    download_trainer_king,
    format_challenge_stats,
)
from ..config import AppConfig
from ..db import fetch_horses, upsert_race_records
from ..download_sources import horse_pp_template
from ..football import (
    download_epl_schedule_results,
    download_football_odds,
    download_football_results,
)
from ..race_results import download_race_results
from ..scraper import scrape_horse_past_performance, update_horses_table_from_web
from .racecard_rank_viewer import (
    update_immediate_odds_for_latest_meeting,
    update_latest_racecard_data,
)


class DownloadWorker(QObject):
    stage_changed = Signal(str)
    progress_changed = Signal(int, int)  # current, total；total=0 表示不確定進度
    finished_ok = Signal(str)
    finished_error = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        *,
        do_horses: bool,
        do_racecard: bool,
        do_odds: bool = False,
        do_results: bool = False,
        do_jockey_king: bool = False,
        do_trainer_king: bool = False,
        do_football_odds: bool = False,
        do_football_results: bool = False,
        do_epl: bool = False,
    ) -> None:
        super().__init__()
        self._config = config
        self._do_horses = do_horses
        self._do_racecard = do_racecard
        # 排位下載本身會帶即時賠率；只在未下載排位時單獨抓賠率。
        self._do_odds = do_odds and not do_racecard
        self._do_results = do_results
        self._do_jockey_king = do_jockey_king
        self._do_trainer_king = do_trainer_king
        self._do_football_odds = do_football_odds
        self._do_football_results = do_football_results
        self._do_epl = do_epl
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        parts: list[str] = []
        try:
            if self._do_horses:
                parts.append(self._download_horses())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_racecard:
                parts.append(self._download_racecard())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_odds:
                parts.append(self._download_odds())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_results:
                parts.append(self._download_results())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_jockey_king:
                parts.append(self._download_jockey_king())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_trainer_king:
                parts.append(self._download_trainer_king())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_football_odds:
                parts.append(self._download_football_odds())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_football_results:
                parts.append(self._download_football_results())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if self._do_epl:
                parts.append(self._download_epl())
                if self._cancelled:
                    self.finished_error.emit("已取消。")
                    return
            if not parts:
                self.finished_error.emit("沒有選擇要下載的項目。")
                return
            self.finished_ok.emit("｜".join(parts))
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))

    def _download_horses(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載馬匹清單…")
        horses_stats = update_horses_table_from_web(self._config.db_path)
        if self._cancelled:
            return "馬匹下載已取消"
        rows = fetch_horses(self._config.db_path, limit=None)
        success = failed = fetched_total = inserted_total = 0
        total = len(rows)
        template = horse_pp_template(
            self._config.data_dir, self._config.hkjc_horse_url_template
        )
        self.stage_changed.emit("正在下載馬匹往績…")
        for index, row in enumerate(rows, start=1):
            if self._cancelled:
                break
            self.progress_changed.emit(index, total)
            horse_id = str(row["horse_id"] or "").strip()
            if not horse_id:
                continue
            try:
                result = scrape_horse_past_performance(
                    horse_id, template, db_path=self._config.db_path
                )
                inserted = upsert_race_records(self._config.db_path, result.records)
                fetched_total += len(result.records)
                inserted_total += inserted
                success += 1
            except Exception:  # noqa: BLE001
                failed += 1
        return (
            f"馬匹寫入 {horses_stats.written}｜往績成功 {success}/{total}"
            f"（失敗 {failed}）｜抓到 {fetched_total}｜新增 {inserted_total}"
        )

    def _download_racecard(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載最新排位與即時賠率…")
        meeting = update_latest_racecard_data(self._config)
        if meeting is None:
            return "排位：找不到可更新的賽馬日"
        return f"排位完成：{meeting.racedate} {meeting.racecourse_code}（含即時賠率）"

    def _download_odds(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載即時賠率…")
        meeting = update_immediate_odds_for_latest_meeting(self._config)
        if meeting is None:
            return "即時賠率：資料庫尚無排位，請先下載排位"
        return f"即時賠率完成：{meeting.racedate} {meeting.racecourse_code}"

    def _download_results(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載賽馬結果…")

        def _progress(cur: int, total: int, msg: str) -> None:
            self.stage_changed.emit(msg)
            self.progress_changed.emit(cur, total)

        stats = download_race_results(
            self._config.db_path,
            data_dir=self._config.data_dir,
            progress=_progress,
            should_cancel=lambda: self._cancelled,
        )
        if stats.meeting is None:
            return "賽馬結果：找不到可下載的賽日"
        return (
            f"賽果完成：{stats.meeting.racedate} {stats.meeting.racecourse_code}"
            f"｜場次 {stats.races}｜列 {stats.rows}｜新增 {stats.inserted}"
            f"（無馬號對應略過 {stats.skipped_no_horse}）"
        )

    def _download_jockey_king(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載騎師王…")
        stats = download_jockey_king(
            self._config.db_path,
            data_dir=self._config.data_dir,
            progress=lambda msg: self.stage_changed.emit(msg),
            should_cancel=lambda: self._cancelled,
        )
        return format_challenge_stats(stats)

    def _download_trainer_king(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載練馬師王…")
        stats = download_trainer_king(
            self._config.db_path,
            data_dir=self._config.data_dir,
            progress=lambda msg: self.stage_changed.emit(msg),
            should_cancel=lambda: self._cancelled,
        )
        return format_challenge_stats(stats)

    def _download_football_odds(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載足球賠率…")
        stats = download_football_odds(
            self._config.db_path,
            data_dir=self._config.data_dir,
            progress=lambda msg: self.stage_changed.emit(msg),
            should_cancel=lambda: self._cancelled,
        )
        return stats.message

    def _download_football_results(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載足球賽果…")
        stats = download_football_results(
            self._config.db_path,
            data_dir=self._config.data_dir,
            progress=lambda msg: self.stage_changed.emit(msg),
            should_cancel=lambda: self._cancelled,
        )
        return stats.message

    def _download_epl(self) -> str:
        self.progress_changed.emit(0, 0)
        self.stage_changed.emit("正在下載英超賽程/賽果…")
        stats = download_epl_schedule_results(
            self._config.db_path,
            data_dir=self._config.data_dir,
            progress=lambda msg: self.stage_changed.emit(msg),
            should_cancel=lambda: self._cancelled,
        )
        return stats.message

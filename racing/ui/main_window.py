from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTime, QTimer, Signal, QStringListModel
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .grid_nav import install_grid_keyboard_nav
from .main_menu import (
    MAIN_MENU_STYLES,
    MainMenuController,
    main_menu_style_key,
)
from .theme import (
    THEMES,
    build_app_stylesheet,
    configure_data_grid,
    copy_table_selection,
    format_date_yyyy_mm_dd,
    format_integer,
    format_money,
    format_two_digit_99,
    make_form_label,
    make_table_item,
    resize_grid_columns_to_content,
    show_copyable_message,
    style_action_button,
    theme_by_index,
    theme_names,
    wrap_form_in_scroll,
)

from ..config import AppConfig, default_config
from ..download_sources import horse_pp_template
from ..db import (
    count_horses,
    count_race_records,
    fetch_horse_by_id,
    fetch_horses,
    fetch_race_records,
    get_horse_status,
    init_db,
    search_horses_by_keyword,
    search_horses_by_name,
)
from ..scraper import HorsesUpdateStats
from .download_progress_dialog import DownloadWorker
from .horses_update_dialog import HorsesUpdateProgressDialog
from .race_fetch_dialog import (
    RaceFetchAllProgressDialog,
    RaceFetchAllStats,
    RaceFetchProgressDialog,
    RaceFetchStats,
)
from .challenge_kings_viewer import ChallengeKingsPanel
from .race_analysis_panel import RaceAnalysisPanel
from .racecard_rank_viewer import RacecardPanel
from .race_results_viewer import RaceResultsPanel


@dataclass(frozen=True, slots=True)
class UiStrings:
    title: str = "Racing｜賽馬往績資料庫系統"
    horse_id_label: str = "HorseId："
    fetch_button: str = "抓取並入庫"
    load_button: str = "從資料庫載入"


@dataclass(frozen=True, slots=True)
class TaskSettings:
    enable_horses_task: bool = False
    horses_task_time: str = "03:00"
    enable_racecard_task: bool = False
    racecard_task_time: str = "04:00"
    enable_odds_task: bool = False
    odds_task_time: str = "04:00"
    last_run_horses: str = ""
    last_run_racecard: str = ""
    last_run_odds: str = ""
    # 每隔一段時間下載（程式開著才執行）
    enable_interval_task: bool = False
    interval_minutes: int = 5
    interval_horses: bool = False
    interval_racecard: bool = False
    interval_odds: bool = True
    last_interval_at: str = ""
    # 「執行」按鈕要下載的項目（下載視窗只負責設定）
    run_horses: bool = True
    run_racecard: bool = True
    run_odds: bool = True
    run_results: bool = True
    last_run_results: str = ""
    interval_results: bool = False
    enable_results_task: bool = False
    results_task_time: str = "05:00"
    run_jockey_king: bool = False
    run_trainer_king: bool = False
    interval_jockey_king: bool = False
    interval_trainer_king: bool = False
    enable_jockey_king_task: bool = False
    enable_trainer_king_task: bool = False
    jockey_king_task_time: str = "05:00"
    trainer_king_task_time: str = "05:00"
    last_run_jockey_king: str = ""
    last_run_trainer_king: str = ""
    # 任務執行前備份資料庫（避免 schema/資料更新影響測試回歸）
    enable_backup: bool = False
    backup_folder: str = ""
    # 使用 datetime.strftime 格式：Racing + 日期 + hhMM
    backup_filename_template: str = "Racing_%Y%m%d_%H%M.sqlite3"
    # 每日備份時間（用於自動先備份，再做下載）
    backup_time: str = "02:55"
    last_backup: str = ""


class _BackgroundTaskWorker(QObject):
    finished_ok = Signal(str)
    finished_error = Signal(str)

    def __init__(self, fn) -> None:  # noqa: ANN001
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            msg = str(self._fn())
            self.finished_ok.emit(msg)
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))


class TaskSettingsDialog(QDialog):
    """任務設定：每日定時下載（馬匹+往績、排位）。"""

    def __init__(self, parent, settings: TaskSettings) -> None:
        super().__init__(parent)
        self.setWindowTitle("任務設定")
        self.resize(560, 280)

        self._cb_horses = QCheckBox("啟用：每日自動下載馬匹資料及往績")
        self._cb_horses.setChecked(settings.enable_horses_task)
        self._time_horses = QTimeEdit()
        self._time_horses.setDisplayFormat("HH:mm")
        self._time_horses.setTime(QTime.fromString(settings.horses_task_time, "HH:mm"))

        self._cb_racecard = QCheckBox("啟用：每日自動下載排位資料")
        self._cb_racecard.setChecked(settings.enable_racecard_task)
        self._time_racecard = QTimeEdit()
        self._time_racecard.setDisplayFormat("HH:mm")
        self._time_racecard.setTime(QTime.fromString(settings.racecard_task_time, "HH:mm"))

        self._cb_backup = QCheckBox("啟用：任務執行前先備份資料庫")
        self._cb_backup.setChecked(settings.enable_backup)
        self._backup_folder = QLineEdit()
        self._backup_folder.setPlaceholderText("例如：data/backup")
        self._backup_folder.setText(settings.backup_folder or "")
        self._backup_folder.setMinimumWidth(260)
        self._backup_filename = QLineEdit()
        self._backup_filename.setReadOnly(True)
        # 備份檔名預覽：以「設定的備份時間」產生 hhMM
        backup_time = getattr(settings, "backup_time", "02:55") or "02:55"
        hh_str, mm_str = backup_time.split(":") if ":" in backup_time else ("02", "55")
        now_dt = datetime.now()
        backup_dt = datetime(
            now_dt.year, now_dt.month, now_dt.day, int(hh_str), int(mm_str)
        )
        self._backup_filename.setText(backup_dt.strftime(settings.backup_filename_template))

        self._time_backup = QTimeEdit()
        self._time_backup.setDisplayFormat("HH:mm")
        self._time_backup.setTime(QTime.fromString(getattr(settings, "backup_time", "02:55"), "HH:mm"))

        def _sync_backup_times_enabled() -> None:
            enabled = self._cb_backup.isChecked()
            self._time_backup.setEnabled(enabled)
            self._backup_folder.setEnabled(enabled)

        def _sync_backup_filename_preview() -> None:
            backup_time = self._time_backup.time().toString("HH:mm")
            hh_str, mm_str = (
                backup_time.split(":") if ":" in backup_time else ("02", "55")
            )
            now_dt = datetime.now()
            backup_dt = datetime(
                now_dt.year, now_dt.month, now_dt.day, int(hh_str), int(mm_str)
            )
            self._backup_filename.setText(
                backup_dt.strftime(settings.backup_filename_template)
            )

        self._cb_backup.toggled.connect(_sync_backup_times_enabled)
        self._time_backup.timeChanged.connect(_sync_backup_filename_preview)
        _sync_backup_times_enabled()
        _sync_backup_filename_preview()

        form = QFormLayout()
        form.addRow(self._cb_horses)
        form.addRow("馬匹/往績時間：", self._time_horses)
        form.addRow(self._cb_racecard)
        form.addRow("排位時間：", self._time_racecard)
        form.addRow(self._cb_backup)
        form.addRow("預備份資料夾：", self._backup_folder)
        form.addRow("備份檔案：", self._backup_filename)
        form.addRow("備份時間：", self._time_backup)

        note = QLabel("說明：任務會在背景執行，下載期間仍可操作畫面。")
        note.setStyleSheet("color: #666; font-size: 12px;")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addStretch()
        root.addWidget(buttons)

    def result_settings(self, old: TaskSettings) -> TaskSettings:
        return replace(
            old,
            enable_horses_task=self._cb_horses.isChecked(),
            horses_task_time=self._time_horses.time().toString("HH:mm"),
            enable_racecard_task=self._cb_racecard.isChecked(),
            racecard_task_time=self._time_racecard.time().toString("HH:mm"),
            enable_backup=self._cb_backup.isChecked(),
            backup_folder=self._backup_folder.text().strip(),
            backup_time=self._time_backup.time().toString("HH:mm"),
        )


class DownloadDialog(QDialog):
    """下載設定：排程／間隔／要下載的項目。網站來源請按「網站設定」。"""

    def __init__(self, parent, settings: TaskSettings, data_dir: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("下載設定")
        self.resize(520, 460)
        self._data_dir = Path(data_dir)
        self._run_now = False

        self._once = QRadioButton("手動執行（按「執行」才下載）")
        self._scheduled = QRadioButton("指定時間下載（每日）")
        self._interval = QRadioButton("每隔一段時間下載")
        if settings.enable_interval_task:
            self._interval.setChecked(True)
        elif settings.enable_horses_task or settings.enable_racecard_task or settings.enable_odds_task:
            self._scheduled.setChecked(True)
        else:
            self._once.setChecked(True)

        self._time = QTimeEdit()
        self._time.setDisplayFormat("HH:mm")
        preset = (
            settings.odds_task_time
            or settings.racecard_task_time
            or settings.horses_task_time
            or "23:00"
        )
        parsed = QTime.fromString(preset, "HH:mm")
        self._time.setTime(parsed if parsed.isValid() else QTime(23, 0))

        self._interval_minutes = QSpinBox()
        self._interval_minutes.setRange(1, 180)
        self._interval_minutes.setSuffix(" 分鐘")
        self._interval_minutes.setValue(max(1, int(settings.interval_minutes or 5)))

        self._cb_horses = QCheckBox("馬匹資料及往績")
        self._cb_racecard = QCheckBox("排位資料")
        self._cb_odds = QCheckBox("即時賠率")
        self._cb_results = QCheckBox("賽馬結果")
        self._cb_jockey_king = QCheckBox("騎師王")
        self._cb_trainer_king = QCheckBox("練馬師王")
        if settings.enable_interval_task:
            self._cb_horses.setChecked(settings.interval_horses)
            self._cb_racecard.setChecked(settings.interval_racecard)
            self._cb_odds.setChecked(settings.interval_odds)
            self._cb_results.setChecked(settings.interval_results)
            self._cb_jockey_king.setChecked(settings.interval_jockey_king)
            self._cb_trainer_king.setChecked(settings.interval_trainer_king)
        elif (
            settings.enable_horses_task
            or settings.enable_racecard_task
            or settings.enable_odds_task
            or settings.enable_results_task
            or settings.enable_jockey_king_task
            or settings.enable_trainer_king_task
        ):
            self._cb_horses.setChecked(settings.enable_horses_task)
            self._cb_racecard.setChecked(settings.enable_racecard_task)
            self._cb_odds.setChecked(settings.enable_odds_task or settings.enable_racecard_task)
            self._cb_results.setChecked(settings.enable_results_task)
            self._cb_jockey_king.setChecked(settings.enable_jockey_king_task)
            self._cb_trainer_king.setChecked(settings.enable_trainer_king_task)
        else:
            self._cb_horses.setChecked(settings.run_horses)
            self._cb_racecard.setChecked(settings.run_racecard)
            self._cb_odds.setChecked(settings.run_odds)
            self._cb_results.setChecked(settings.run_results)
            self._cb_jockey_king.setChecked(settings.run_jockey_king)
            self._cb_trainer_king.setChecked(settings.run_trainer_king)

        self._once.toggled.connect(self._sync_mode_enabled)
        self._scheduled.toggled.connect(self._sync_mode_enabled)
        self._interval.toggled.connect(self._sync_mode_enabled)
        self._sync_mode_enabled()

        # 單一欄垂直排列（排程選項與下載項目不並排）
        form = QVBoxLayout()
        form.setSpacing(6)
        form.addWidget(self._once)
        sched_row = QHBoxLayout()
        sched_row.addWidget(self._scheduled)
        sched_row.addWidget(self._time)
        sched_row.addStretch(1)
        form.addLayout(sched_row)
        interval_row = QHBoxLayout()
        interval_row.addWidget(self._interval)
        interval_row.addWidget(self._interval_minutes)
        interval_row.addStretch(1)
        form.addLayout(interval_row)
        form.addWidget(QLabel("要下載的資料："))
        form.addWidget(self._cb_horses)
        form.addWidget(self._cb_racecard)
        form.addWidget(self._cb_odds)
        form.addWidget(self._cb_results)
        form.addWidget(self._cb_jockey_king)
        form.addWidget(self._cb_trainer_king)

        note = QLabel(
            "「儲存設定」只保存，不會開始下載；要立刻下載請按「執行」。"
            "下載中可按「取消所有下載」或狀態列按鈕即時中止。"
            "指定時間／間隔下載只在程式開著時自動跑；開啟程式時不會補抓已過時間。"
            "勾選「排位」時會一併更新即時賠率；只抓賠率可不重抓排位頁。"
            "「賽馬結果」由 HKJC 賽果頁抓取名次／時間／賠率並寫入往績。"
            "「騎師王／練馬師王」抓取馬會賽日積分榜。"
            "資料來源網站請按「網站設定」管理。"
        )
        note.setWordWrap(True)

        sites_btn = QPushButton("網站設定")
        style_action_button(sites_btn, QStyle.StandardPixmap.SP_ComputerIcon)
        sites_btn.setToolTip("以 Grid 管理資料來源網站（新增／啟用／網址）")
        sites_btn.clicked.connect(self._open_sites_dialog)

        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("儲存設定", QDialogButtonBox.ButtonRole.AcceptRole)
        run_btn = buttons.addButton("執行", QDialogButtonBox.ButtonRole.ActionRole)
        cancel_all_btn = buttons.addButton(
            "取消所有下載", QDialogButtonBox.ButtonRole.ActionRole
        )
        close_btn = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        save_btn.clicked.connect(self._accept_save_only)
        run_btn.clicked.connect(self._accept_and_run)
        cancel_all_btn.clicked.connect(self._cancel_all_downloads)
        close_btn.clicked.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(sites_btn)
        root.addWidget(buttons)

    def _cancel_all_downloads(self) -> None:
        """即時取消主視窗進行中的背景下載。"""
        parent = self.parent()
        worker = getattr(parent, "_download_worker", None)
        thread = getattr(parent, "_download_thread", None)
        running = worker is not None and thread is not None and thread.isRunning()
        if not running:
            QMessageBox.information(self, "下載", "目前沒有進行中的下載。")
            return
        cancel = getattr(parent, "_cancel_background_download", None)
        if callable(cancel):
            cancel()
            QMessageBox.information(self, "下載", "已送出取消，正在停止所有進行中的下載。")
            return
        QMessageBox.information(self, "下載", "目前沒有可取消的下載工作。")

    def _sync_mode_enabled(self) -> None:
        self._time.setEnabled(self._scheduled.isChecked())
        self._interval_minutes.setEnabled(self._interval.isChecked())

    def _open_sites_dialog(self) -> None:
        from .download_sites_dialog import DownloadSitesDialog

        dlg = DownloadSitesDialog(self, self._data_dir)
        dlg.exec()

    def _validate_selection(self) -> bool:
        if (
            not self._cb_horses.isChecked()
            and not self._cb_racecard.isChecked()
            and not self._cb_odds.isChecked()
            and not self._cb_results.isChecked()
            and not self._cb_jockey_king.isChecked()
            and not self._cb_trainer_king.isChecked()
        ):
            QMessageBox.information(self, "下載設定", "請至少選擇一項要下載的資料。")
            return False
        return True

    def _accept_save_only(self) -> None:
        if not self._validate_selection():
            return
        self._run_now = False
        self.accept()

    def _accept_and_run(self) -> None:
        if not self._validate_selection():
            return
        self._run_now = True
        self.accept()

    def should_run(self) -> bool:
        return bool(self._run_now)

    def choice(self) -> tuple[str, bool, bool, bool, bool, bool, bool, str, int]:
        if self._interval.isChecked():
            mode = "interval"
        elif self._scheduled.isChecked():
            mode = "schedule"
        else:
            mode = "once"
        return (
            mode,
            self._cb_horses.isChecked(),
            self._cb_racecard.isChecked(),
            self._cb_odds.isChecked(),
            self._cb_results.isChecked(),
            self._cb_jockey_king.isChecked(),
            self._cb_trainer_king.isChecked(),
            self._time.time().toString("HH:mm"),
            int(self._interval_minutes.value()),
        )


class DateSortItem(QTableWidgetItem):
    """Date item that sorts by numeric yyyymmdd key."""

    def __lt__(self, other: object) -> bool:
        if isinstance(other, QTableWidgetItem):
            a = self.data(Qt.UserRole)
            b = other.data(Qt.UserRole)
            if isinstance(a, int) and isinstance(b, int):
                return a < b
        return super().__lt__(other)


def _headers_with_sort_arrow(
    base_headers: list[str], section: int, order: Qt.SortOrder
) -> list[str]:
    labels = list(base_headers)
    if 0 <= section < len(labels):
        labels[section] = f"{labels[section]} {'↑' if order == Qt.AscendingOrder else '↓'}"
    return labels


# 主殼分頁：標題底色（未選／已選）與圖示；啟動時只開 AI 助手，其餘按需加入且可關閉
_SHELL_TAB_DEFS: dict[str, dict[str, object]] = {
    "ai": {
        "title": "AI 助手",
        "bg": "#BFDBFE",
        "bg_sel": "#2563EB",
        "fg": "#1E3A8A",
        "fg_sel": "#FFFFFF",
        "icon": QStyle.StandardPixmap.SP_MessageBoxInformation,
    },
    "horses": {
        "title": "馬匹／往績",
        "bg": "#BBF7D0",
        "bg_sel": "#16A34A",
        "fg": "#14532D",
        "fg_sel": "#FFFFFF",
        "icon": QStyle.StandardPixmap.SP_DirHomeIcon,
    },
    "racecard": {
        "title": "排位",
        "bg": "#FDE68A",
        "bg_sel": "#D97706",
        "fg": "#78350F",
        "fg_sel": "#FFFFFF",
        "icon": QStyle.StandardPixmap.SP_FileDialogListView,
    },
    "results": {
        "title": "賽馬結果",
        "bg": "#FBCFE8",
        "bg_sel": "#DB2777",
        "fg": "#831843",
        "fg_sel": "#FFFFFF",
        "icon": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    },
    "kings": {
        "title": "騎師王-練馬師王",
        "bg": "#DDD6FE",
        "bg_sel": "#7C3AED",
        "fg": "#4C1D95",
        "fg_sel": "#FFFFFF",
        "icon": QStyle.StandardPixmap.SP_DialogApplyButton,
    },
    "analysis": {
        "title": "賽馬分析",
        "bg": "#BFDBFE",
        "bg_sel": "#2563EB",
        "fg": "#1E3A8A",
        "fg_sel": "#FFFFFF",
        "icon": QStyle.StandardPixmap.SP_FileDialogInfoView,
    },
}


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, *, auto_open_web: bool = False) -> None:
        super().__init__()
        self._config = config
        self._auto_open_web = auto_open_web
        self._ui = UiStrings()
        self._task_settings_path = self._config.data_dir / "task_settings.json"
        self._task_settings = self._load_task_settings()
        self._task_threads: list[QThread] = []
        self._running_jobs: set[str] = set()
        self._ai_panel = None
        self._ai_tab_index = -1
        self._horses_workspace: QWidget | None = None
        self._download_thread: QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._download_jobs: list[str] = []
        self._download_flags: tuple[bool, bool, bool, bool, bool] = (
            False,
            False,
            False,
            False,
            False,
        )
        self._web_server_thread: threading.Thread | None = None
        self._web_server = None
        self._web_server_error = ""

        init_db(self._config.db_path)

        self.setWindowTitle(self._ui.title)
        self.resize(1200, 720)
        self._build_menu_bar()

        self._horse_id_input = QComboBox()
        self._horse_id_input.setEditable(True)
        self._horse_id_input.lineEdit().setPlaceholderText(
            "HorseId、烙號（如 H123）或馬名關鍵字；可下拉或輸入後離開欄位自動對應"
        )
        self._horse_completion_map: dict[str, str] = {}
        le_horse = self._horse_id_input.lineEdit()
        self._horse_completer = QCompleter(le_horse)
        self._horse_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._horse_completer.setFilterMode(Qt.MatchContains)
        self._horse_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._horse_completer.setMaxVisibleItems(20)
        self._horse_completer.setModel(QStringListModel())
        self._horse_completer.activated[str].connect(self._on_horse_completer_activated)
        le_horse.setCompleter(self._horse_completer)
        le_horse.editingFinished.connect(self._on_horse_id_line_finished)
        self._horse_id_input.currentTextChanged.connect(self._on_horse_id_changed)
        # 縮窄 HorseId / Brand No. ComboBox 寬度，保留輸入與下拉功能
        self._horse_id_input.setMinimumWidth(260)
        self._horse_id_input.setMaximumWidth(360)
        self._all_horses_checkbox = QCheckBox("所有馬匹")
        self._all_horses_checkbox.toggled.connect(self._on_all_horses_toggled)

        self._race_year_combo = QComboBox()
        self._race_year_combo.addItem("全部年份", userData="")
        self._race_year_combo.setToolTip(
            "僅用於「抓取並入庫」時要寫入哪一年度的往績；下方「往績」表格一律顯示該馬全部紀錄。"
        )
        # 年份僅用於「抓取並入庫」篩選，不影響下方往績表格顯示
        self._all_years_checkbox = QCheckBox("所有年份")
        self._all_years_checkbox.setChecked(True)
        self._all_years_checkbox.setToolTip(
            "勾選：抓取時不限制年份（等同「全部年份」）。不影響下方往績表格顯示。"
        )
        self._all_years_checkbox.toggled.connect(self._on_all_years_toggled)

        self._horse_name_search_input = QLineEdit()
        self._horse_name_search_input.setPlaceholderText("輸入馬名關鍵字（中文/英文）")

        self._db_path_field = QLineEdit()
        self._db_path_field.setReadOnly(True)
        self._db_path_field.setText(str(self._config.db_path))

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(50, 50000)
        self._limit_spin.setValue(200)
        self._limit_spin.setSingleStep(50)
        # Grid 要顯示全部，不再套用 LIMIT。
        self._limit_spin.setVisible(False)

        self._show_all_btn = QPushButton("顯示全部")
        self._show_all_btn.clicked.connect(self._on_show_all_clicked)
        self._show_all_btn.setVisible(False)

        self._fetch_btn = QPushButton(self._ui.fetch_button)
        self._fetch_btn.clicked.connect(self._on_fetch_clicked)

        self._load_btn = QPushButton(self._ui.load_button)
        self._load_btn.clicked.connect(self._on_load_clicked)

        self._effect_combo = QComboBox()
        self._effect_combo.addItems(theme_names())
        self._effect_combo.setMinimumWidth(180)
        self._effect_combo.currentIndexChanged.connect(self._on_effect_changed)

        self._search_horse_btn = QPushButton("搜尋")
        style_action_button(self._search_horse_btn, QStyle.StandardPixmap.SP_FileDialogContentsView)
        self._search_horse_btn.clicked.connect(self._on_search_horses_clicked)

        self._filter_btn = QPushButton("過濾")
        style_action_button(self._filter_btn, QStyle.StandardPixmap.SP_FileDialogDetailedView)
        self._filter_btn.setToolTip("依馬名關鍵字過濾上方 Grid")
        self._filter_btn.clicked.connect(self._on_search_horses_clicked)

        self._select_horse_btn = QPushButton("選擇")
        style_action_button(self._select_horse_btn, QStyle.StandardPixmap.SP_DialogApplyButton)
        self._select_horse_btn.clicked.connect(self._on_select_horse_clicked)

        self._update_horses_btn = QPushButton("更新馬匹資料")
        style_action_button(self._update_horses_btn, QStyle.StandardPixmap.SP_BrowserReload)
        self._update_horses_btn.clicked.connect(self._on_update_horses_clicked)

        self._copy_grid_btn = QPushButton("複製")
        style_action_button(self._copy_grid_btn, QStyle.StandardPixmap.SP_FileDialogListView)
        self._copy_grid_btn.setToolTip("複製選取列（可多選）；無選取則複製全部。快捷鍵 Ctrl+C")
        self._copy_grid_btn.clicked.connect(self._copy_active_grid)

        # 馬匹-往績主畫面不顯示：HorseId／所有馬匹／抓取年份／抓取並入庫／從資料庫載入
        self._horse_id_input.hide()
        self._all_horses_checkbox.hide()
        self._race_year_combo.hide()
        self._all_years_checkbox.hide()
        self._fetch_btn.hide()
        self._load_btn.hide()

        self._horses_table = QTableWidget(0, 5)
        self._horses_table.setSortingEnabled(True)
        self._horses_headers = ["HorseId", "Brand No.", "Name EN", "Name ZH", "Status"]
        self._horses_table.setHorizontalHeaderLabels(self._horses_headers)
        configure_data_grid(self._horses_table, multi_select=True, enable_copy=True)
        # 預設以中文名稱（Name ZH）排序
        self._horses_table.horizontalHeader().setSortIndicator(3, Qt.AscendingOrder)
        self._horses_table.horizontalHeader().sortIndicatorChanged.connect(
            self._on_horses_sort_changed
        )
        self._horses_table.itemDoubleClicked.connect(self._on_horse_table_item_double_clicked)
        self._horses_table.itemSelectionChanged.connect(self._on_horses_selection_changed)
        install_grid_keyboard_nav(self._horses_table)

        # 搜尋列在 Grid 上一整行：過濾／搜尋在搜尋標題左邊
        # 不顯示：選擇／清除／複製／更新馬匹資料
        self._select_horse_btn.hide()
        self._copy_grid_btn.hide()
        self._update_horses_btn.hide()
        self._clear_btn = QPushButton("清除")
        style_action_button(self._clear_btn, QStyle.StandardPixmap.SP_DialogResetButton)
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        self._clear_btn.hide()

        search_row = QHBoxLayout()
        search_row.addWidget(self._filter_btn)
        search_row.addWidget(self._search_horse_btn)
        self._search_label = make_form_label("馬名搜尋：")
        search_row.addWidget(self._search_label)
        search_row.addWidget(self._horse_name_search_input, 1)
        self._display_count_label = QLabel("Grid Row 0/0")
        search_row.addWidget(self._display_count_label)

        self._limit_label = QLabel("顯示前")
        self._limit_label.setVisible(False)
        self._limit_unit_label = QLabel("筆")
        self._limit_unit_label.setVisible(False)
        search_row.addWidget(self._limit_label)
        search_row.addWidget(self._limit_spin)
        search_row.addWidget(self._limit_unit_label)
        search_row.addWidget(self._show_all_btn)

        db_row = QHBoxLayout()
        db_row.addWidget(make_form_label("DB："))
        db_row.addWidget(self._db_path_field, 1)

        self._race_headers = [
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
            "獨贏賠率",
            "時間",
            "獎金",
            "Flag",
        ]

        # Detail form (selected row fields).
        self._horse_id_detail = QLineEdit()
        self._horse_id_detail.setReadOnly(True)
        self._brand_no_detail = QLineEdit()
        self._brand_no_detail.setReadOnly(True)
        self._horse_name_en_detail = QLineEdit()
        self._horse_name_en_detail.setReadOnly(True)
        self._horse_name_zh_detail = QLineEdit()
        self._horse_name_zh_detail.setReadOnly(True)
        self._horse_status_detail = QLineEdit()
        self._horse_status_detail.setReadOnly(True)

        # 馬匹明細（分頁 1）：左標右欄，可捲動
        horse_form_host = QWidget()
        horse_form = QFormLayout(horse_form_host)
        horse_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        horse_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        for title, widget in (
            ("HorseId", self._horse_id_detail),
            ("Brand No.", self._brand_no_detail),
            ("Name EN", self._horse_name_en_detail),
            ("Name ZH", self._horse_name_zh_detail),
            ("Status", self._horse_status_detail),
        ):
            horse_form.addRow(make_form_label(title), widget)
        self._horse_detail_widget = wrap_form_in_scroll(horse_form_host)

        # 分頁 2：往績 Grid
        self._detail_race_table = QTableWidget(0, 18)
        self._detail_race_table.setSortingEnabled(True)
        self._detail_race_table.setHorizontalHeaderLabels(self._race_headers)
        configure_data_grid(
            self._detail_race_table,
            multi_select=True,
            enable_copy=True,
            fixed_viewport=False,
        )
        self._detail_race_table.horizontalHeader().sortIndicatorChanged.connect(
            self._on_detail_race_sort_changed
        )
        install_grid_keyboard_nav(self._detail_race_table)

        race_tab = QWidget()
        race_tab_layout = QVBoxLayout()
        race_tab_layout.setContentsMargins(0, 0, 0, 0)
        race_copy_row = QHBoxLayout()
        race_copy_btn = QPushButton("複製往績")
        style_action_button(race_copy_btn, QStyle.StandardPixmap.SP_FileDialogListView)
        race_copy_btn.clicked.connect(lambda: copy_table_selection(self._detail_race_table))
        race_copy_row.addWidget(race_copy_btn)
        race_copy_row.addStretch(1)
        race_tab_layout.addLayout(race_copy_row)
        race_tab_layout.addWidget(self._detail_race_table, 1)
        race_tab.setLayout(race_tab_layout)

        # 明細區容器：分頁（馬匹資料 / 往績 Grid）
        self._detail_tabs = QTabWidget()
        self._detail_tabs.addTab(self._horse_detail_widget, "馬匹資料")
        self._detail_tabs.addTab(race_tab, "往績")
        self._detail_tabs.currentChanged.connect(self._on_detail_tab_changed)

        root = QVBoxLayout()
        root.addLayout(db_row)
        root.addLayout(search_row)
        root.addWidget(self._horses_table, 1)
        root.addWidget(self._detail_tabs, 1)

        self._horses_workspace = QWidget()
        self._horses_workspace.setLayout(root)

        # 頂層分頁：啟動只開 AI 助手；其餘由選單／按鈕按需加入，可關閉
        self._main_shell_tabs = QTabWidget()
        self._main_shell_tabs.setObjectName("mainShellTabs")
        # 文件式分頁列在 Windows 上較穩定；並為 TabBar 設 objectName 供樣式選擇器使用
        # （勿用「QTabWidget > QTabBar」子選擇器，部分環境套不到，會導致分頁無法點選）
        self._main_shell_tabs.setDocumentMode(True)
        self._main_shell_tabs.setTabsClosable(True)
        self._main_shell_tabs.tabBar().setObjectName("mainShellTabBar")
        self._main_shell_tabs.tabCloseRequested.connect(self._on_shell_tab_close_requested)
        # 勿 parent=MainWindow：未加入分頁前會浮在 (0,0) 蓋住選單列（系統設定等標題）
        self._racecard_panel = RacecardPanel(self._config, parent=None)
        self._racecard_panel.hide()
        self._racecard_panel.horse_double_clicked.connect(self._on_racecard_horse_double_clicked)
        self._results_panel = RaceResultsPanel(self._config, parent=None)
        self._results_panel.hide()
        self._kings_panel = ChallengeKingsPanel(self._config, parent=None)
        self._kings_panel.hide()
        self._analysis_panel = RaceAnalysisPanel(
            self._config,
            parent=None,
            on_status=lambda m: self.statusBar().showMessage(m, 5000),
        )
        self._analysis_panel.hide()
        self._analysis_panel.send_to_ai.connect(self._on_analysis_send_to_ai)
        self._horses_workspace.hide()
        self._main_shell_tabs.currentChanged.connect(self._on_main_shell_tab_changed)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.addWidget(self._main_shell_tabs)

        # 主選單（左側 Tree/Image/List；MenuBar/ToolBar 另掛）+ 內容區
        self._content_host = QWidget()
        content_lay = QHBoxLayout(self._content_host)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(0)
        self._main_menu_controller = MainMenuController(
            self,
            handlers={
                "horses": self._menu_open_horses,
                "racecard": self._menu_open_racecard,
                "results": self._menu_open_results,
                "kings": self._menu_open_kings,
                "analysis": self._menu_open_analysis,
                "backup": self._menu_backup_database,
                "download": self._open_download_dialog,
            },
        )
        content_lay.addWidget(self._main_menu_controller.panel, 0)
        content_lay.addWidget(shell, 1)
        self.setCentralWidget(self._content_host)
        self._ui_prefs_path = self._config.data_dir / "ui_prefs.json"
        self._apply_main_menu_style(self._load_main_menu_style_key())

        tb = QToolBar("Main")
        tb.setMovable(False)
        # 置中：左右 Expanding 占位，中間再加入的 widget / action 會落在橫向正中
        sp_left = QWidget()
        sp_left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(sp_left)
        self._toolbar_center = QWidget()
        center_lay = QHBoxLayout(self._toolbar_center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(8)
        self._current_view_label = QLabel("目前開啟：AI 助手")
        self._current_view_label.setStyleSheet("font-weight: 600; padding: 2px 10px;")
        center_lay.addWidget(self._current_view_label)
        ai_btn = QPushButton("AI 助手")
        style_action_button(ai_btn, QStyle.StandardPixmap.SP_MessageBoxInformation)
        ai_btn.clicked.connect(self._open_ai_assistant)
        center_lay.addWidget(ai_btn)
        download_btn = QPushButton("下載資料")
        style_action_button(download_btn, QStyle.StandardPixmap.SP_ArrowDown)
        download_btn.setToolTip("開啟下載設定；於視窗內按「執行」才開始下載")
        download_btn.clicked.connect(self._open_download_dialog)
        center_lay.addWidget(download_btn)
        center_lay.addWidget(make_form_label("主題風格："))
        center_lay.addWidget(self._effect_combo)
        print_btn = QPushButton("列印")
        style_action_button(print_btn, QStyle.StandardPixmap.SP_FileDialogContentsView)
        print_btn.setToolTip("列印／報表：匯出馬匹往績 Excel 或預覽")
        print_btn.clicked.connect(self._open_print_dialog)
        center_lay.addWidget(print_btn)
        web_btn = QPushButton("WEB 介面")
        style_action_button(web_btn, QStyle.StandardPixmap.SP_ComputerIcon)
        web_btn.setToolTip("開啟瀏覽器 WEB 介面（與本 Windows 介面可隨時切換）")
        web_btn.clicked.connect(self._open_web_ui)
        center_lay.addWidget(web_btn)
        tb.addWidget(self._toolbar_center)
        sp_right = QWidget()
        sp_right.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(sp_right)
        exit_tb_btn = QPushButton("離開")
        style_action_button(exit_tb_btn, QStyle.StandardPixmap.SP_DialogCloseButton)
        exit_tb_btn.setToolTip("關閉程式")
        exit_tb_btn.clicked.connect(self.close)
        tb.addWidget(exit_tb_btn)
        self.addToolBar(tb)

        status = QStatusBar()
        self.setStatusBar(status)
        self._download_progress = QProgressBar()
        self._download_progress.setMaximumWidth(180)
        self._download_progress.setMaximumHeight(16)
        self._download_progress.setTextVisible(True)
        self._download_progress.setFormat("%v/%m")
        self._download_progress.hide()
        self._download_cancel_btn = QPushButton("取消所有下載")
        self._download_cancel_btn.setToolTip("即時取消所有進行中的下載")
        self._download_cancel_btn.hide()
        self._download_cancel_btn.clicked.connect(self._cancel_background_download)
        status.addPermanentWidget(self._download_progress)
        status.addPermanentWidget(self._download_cancel_btn)
        # 狀態列只顯示背景執行訊息
        status.showMessage("就緒")

        # 往績模式時，目前馬匹的總筆數（用於 顯示 N/M 的 M）
        self._race_records_total_for_horse: int = 0

        self._refresh_horse_id_combo()
        self._refresh_race_year_combo()
        self._on_search_clicked()
        self._refresh_sort_header_labels()
        self._on_effect_changed(0)
        # 啟動時只開啟 AI 助手分頁（馬匹／排位／賽果按需再開）
        self._open_ai_assistant()
        self._start_task_scheduler()
        # 啟動 Windows 後自動開啟 WEB（both 模式已由 __main__ 先啟 web_app，略過）
        if self._auto_open_web:
            QTimer.singleShot(500, self._open_web_ui)

    def _on_effect_changed(self, idx: int) -> None:
        """切換主題風格（套用至 Grid、表單與整體介面）。"""
        theme = theme_by_index(idx)
        qss = build_app_stylesheet(theme) + self._main_shell_tabs_style(idx)
        self.setStyleSheet(qss)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        # 排位／積分榜／分析面板一併套用
        if hasattr(self, "_racecard_panel"):
            self._racecard_panel.setStyleSheet(qss)
        if hasattr(self, "_kings_panel"):
            self._kings_panel.setStyleSheet(qss)
        if hasattr(self, "_analysis_panel"):
            self._analysis_panel.setStyleSheet(qss)
        self._apply_shell_tab_colors()

    def _build_menu_bar(self) -> None:
        bar = self.menuBar()
        from .main_menu import _menu_font, _set_menu_title_font

        bar.setFont(_menu_font(13, True))
        style = self.style() or QApplication.style()

        def _act(text: str, pixmap: QStyle.StandardPixmap, slot) -> QAction:
            action = QAction(text, self)
            action.setFont(_menu_font(12, True))
            if style is not None:
                action.setIcon(style.standardIcon(pixmap))
            action.triggered.connect(slot)
            return action

        # 「主選單」項目由 MainMenuController 依樣式動態加入（含 Icon、大字體）

        m_settings = bar.addMenu("系統設定")
        _set_menu_title_font(m_settings, 13)
        m_settings.addAction(
            _act("任務設定", QStyle.StandardPixmap.SP_FileDialogInfoView, self._open_task_settings_dialog)
        )
        m_theme = m_settings.addMenu("主題風格")
        _set_menu_title_font(m_theme, 12)
        for i, theme in enumerate(THEMES):
            a = QAction(theme.name, self)
            a.setFont(_menu_font(12, True))
            a.triggered.connect(lambda checked=False, idx=i: self._effect_combo.setCurrentIndex(idx))
            m_theme.addAction(a)

        m_menu_style = m_settings.addMenu("主選單樣式")
        _set_menu_title_font(m_menu_style, 12)
        for i, (_key, name) in enumerate(MAIN_MENU_STYLES):
            a = QAction(name, self)
            a.setFont(_menu_font(12, True))
            a.triggered.connect(lambda checked=False, idx=i: self._on_main_menu_style_chosen(idx))
            m_menu_style.addAction(a)

        m_ai = bar.addMenu("AI")
        _set_menu_title_font(m_ai, 13)
        m_ai.addAction(
            _act("AI 助手", QStyle.StandardPixmap.SP_MessageBoxInformation, self._open_ai_assistant)
        )

        m_iface = bar.addMenu("介面")
        _set_menu_title_font(m_iface, 13)
        m_iface.addAction(
            _act("WEB 介面", QStyle.StandardPixmap.SP_ComputerIcon, self._open_web_ui)
        )
        m_iface.addAction(
            _act("列印／報表", QStyle.StandardPixmap.SP_FileDialogContentsView, self._open_print_dialog)
        )
        m_iface.addSeparator()
        m_iface.addAction(
            _act("離開", QStyle.StandardPixmap.SP_DialogCloseButton, self.close)
        )

    def _load_main_menu_style_key(self) -> str:
        path = getattr(self, "_ui_prefs_path", self._config.data_dir / "ui_prefs.json")
        if not path.is_file():
            return "treeview"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            key = str(raw.get("main_menu_style", "treeview"))
            return key if key in {k for k, _ in MAIN_MENU_STYLES} else "treeview"
        except Exception:
            return "treeview"

    def _save_main_menu_style_key(self, key: str) -> None:
        path = getattr(self, "_ui_prefs_path", self._config.data_dir / "ui_prefs.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        raw: dict = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        raw["main_menu_style"] = key
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_main_menu_style(self, key: str) -> None:
        if not hasattr(self, "_main_menu_controller"):
            return
        self._main_menu_controller.apply_style(key)
        self._save_main_menu_style_key(key)
        name = next((n for k, n in MAIN_MENU_STYLES if k == key), key)
        pass  # UI 提示不寫入狀態列

    def _on_main_menu_style_chosen(self, index: int) -> None:
        self._apply_main_menu_style(main_menu_style_key(index))

    def _menu_open_horses(self) -> None:
        self._open_shell_tab("horses")
        pass  # UI 提示不寫入狀態列

    def _menu_open_racecard(self) -> None:
        self._open_shell_tab("racecard")
        pass  # UI 提示不寫入狀態列

    def _menu_open_results(self) -> None:
        self._open_shell_tab("results")
        pass  # UI 提示不寫入狀態列

    def _menu_open_kings(self) -> None:
        self._open_shell_tab("kings")

    def _menu_open_analysis(self) -> None:
        self._open_shell_tab("analysis")

    def _on_analysis_send_to_ai(self, prompt: str, auto_send: bool) -> None:
        self._open_shell_tab("ai")
        if self._ai_panel is not None:
            self._ai_panel.load_prompt(prompt, auto_send=auto_send)
        pass  # UI 提示不寫入狀態列

    def _menu_backup_database(self) -> None:
        try:
            msg = self._run_backup_job(force=True)
            show_copyable_message(self, "備份資料庫", msg)
            pass  # UI 提示不寫入狀態列
        except Exception as exc:  # noqa: BLE001
            show_copyable_message(self, "備份失敗", str(exc), level="error")

    def _web_ui_reachable(self, timeout: float = 0.4) -> bool:
        """檢查本機 WEB 伺服器是否已可連線。"""
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen("http://127.0.0.1:800/ping", timeout=timeout) as resp:
                return int(getattr(resp, "status", 200) or 200) < 500
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _ensure_web_server(self) -> bool:
        """確保背景 WEB 伺服器在跑（與 Windows 介面並存）。成功或已在運作為 True。"""
        import time

        if self._web_ui_reachable():
            return True
        if self._web_server_thread is not None and self._web_server_thread.is_alive():
            for _ in range(15):
                if self._web_ui_reachable():
                    return True
                time.sleep(0.1)
            return self._web_ui_reachable()

        self._web_server_error = ""
        ready = threading.Event()

        def _serve() -> None:
            try:
                from werkzeug.serving import make_server

                from racing.web_app import app as flask_app

                # threaded=True：WEB 請求與 Windows UI 可同時進行
                server = make_server("127.0.0.1", 800, flask_app, threaded=True)
                self._web_server = server
                ready.set()
                server.serve_forever()
            except OSError:
                # 埠已被其他程序占用：視為可用（稍後以 ping 確認）
                ready.set()
            except Exception as exc:  # noqa: BLE001
                self._web_server_error = str(exc)
                ready.set()

        t = threading.Thread(target=_serve, name="racing-web", daemon=True)
        self._web_server_thread = t
        t.start()
        ready.wait(timeout=3.0)
        if self._web_server_error:
            self._web_server_thread = None
            self._web_server = None
            return False
        for _ in range(15):
            if self._web_ui_reachable():
                return True
            time.sleep(0.1)
        return self._web_ui_reachable()

    def _open_web_ui(self) -> None:
        """啟動（或沿用）本機 Flask，並以瀏覽器開啟；與 Windows 介面同時執行。"""
        url = "http://127.0.0.1:800/app"
        self._set_background_status("正在啟動 WEB 介面…")

        def _worker() -> None:
            ok = self._ensure_web_server()
            err = self._web_server_error

            def _done() -> None:
                if not ok and err:
                    show_copyable_message(
                        self,
                        "WEB 介面啟動失敗",
                        err
                        + "\n\n若為 numpy／pandas 錯誤，請在虛擬環境執行：\n"
                        "pip install --force-reinstall numpy pandas",
                        level="error",
                    )
                    return
                if not ok:
                    show_copyable_message(
                        self,
                        "WEB 介面啟動失敗",
                        "無法連線 http://127.0.0.1:800 。\n"
                        "請確認埠 800 未被其他程式占用，或稍後再按「WEB 介面」。",
                        level="error",
                    )
                    return
                webbrowser.open(url)
                self._set_background_status(f"Windows＋WEB 同時執行中：{url}")

            QTimer.singleShot(0, _done)

        threading.Thread(target=_worker, name="racing-web-open", daemon=True).start()

    def _open_print_dialog(self) -> None:
        """列印／報表：按鈕列加高，與 WEB 列印列同一工作流程。"""
        from ..reports import get_report_definition

        dlg = QDialog(self)
        dlg.setWindowTitle("列印／報表")
        dlg.resize(720, 160)
        lay = QVBoxLayout(dlg)
        bar = QHBoxLayout()
        bar.setSpacing(10)

        def _export_horse_results() -> None:
            try:
                report = get_report_definition("horse_results")
                df, prefix = report.generate()
                default = str(self._config.data_dir / f"{prefix}-{datetime.now():%Y%m%d}.xlsx")
                path, _ = QFileDialog.getSaveFileName(
                    dlg, "匯出馬匹往績報表", default, "Excel (*.xlsx)"
                )
                if not path:
                    return
                df.to_excel(path, index=False, sheet_name=report.sheet_name)
                show_copyable_message(dlg, "列印／報表", f"已匯出：{path}")
                pass  # UI 提示不寫入狀態列
            except Exception as exc:  # noqa: BLE001
                show_copyable_message(dlg, "匯出失敗", str(exc), level="error")

        def _preview_grid() -> None:
            table = (
                self._detail_race_table
                if self._detail_tabs.currentIndex() == 1
                else self._horses_table
            )
            rows = table.rowCount()
            cols = table.columnCount()
            lines = ["\t".join(table.horizontalHeaderItem(c).text() if table.horizontalHeaderItem(c) else "" for c in range(cols))]
            for r in range(min(rows, 40)):
                lines.append(
                    "\t".join(
                        table.item(r, c).text() if table.item(r, c) else ""
                        for c in range(cols)
                    )
                )
            show_copyable_message(dlg, "預覽目前 Grid（最多 40 列）", "\n".join(lines))

        btn_report = QPushButton("馬匹往績報表")
        style_action_button(btn_report, QStyle.StandardPixmap.SP_FileDialogDetailedView)
        btn_report.clicked.connect(_export_horse_results)
        btn_preview = QPushButton("預覽目前 Grid")
        style_action_button(btn_preview, QStyle.StandardPixmap.SP_FileDialogContentsView)
        btn_preview.clicked.connect(_preview_grid)
        btn_web = QPushButton("WEB 列印頁")
        style_action_button(btn_web, QStyle.StandardPixmap.SP_ComputerIcon)
        btn_web.clicked.connect(lambda: (self._open_web_ui(), dlg.accept()))
        btn_close = QPushButton("關閉")
        style_action_button(btn_close, QStyle.StandardPixmap.SP_DialogCloseButton)
        btn_close.clicked.connect(dlg.reject)
        for b in (btn_report, btn_preview, btn_web, btn_close):
            b.setMinimumHeight(36)
            bar.addWidget(b)
        lay.addLayout(bar)
        tip = QLabel("工作流程與 WEB 相同：選報表 → 預覽／匯出 → 列印。按鈕列加高以便同一行顯示。")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        dlg.exec()

    def _copy_active_grid(self) -> None:
        if self._detail_tabs.currentIndex() == 1:
            copy_table_selection(self._detail_race_table)
            pass  # UI 提示不寫入狀態列
        else:
            copy_table_selection(self._horses_table)
            pass  # UI 提示不寫入狀態列

    def _load_task_settings(self) -> TaskSettings:
        default_backup_folder = str(self._config.data_dir / "backup")
        if not self._task_settings_path.is_file():
            return TaskSettings(
                enable_backup=False,
                backup_folder=default_backup_folder,
            )
        try:
            raw = json.loads(self._task_settings_path.read_text(encoding="utf-8"))
            return TaskSettings(
                enable_horses_task=bool(raw.get("enable_horses_task", False)),
                horses_task_time=str(raw.get("horses_task_time", "03:00")),
                enable_racecard_task=bool(raw.get("enable_racecard_task", False)),
                racecard_task_time=str(raw.get("racecard_task_time", "04:00")),
                enable_odds_task=bool(raw.get("enable_odds_task", False)),
                odds_task_time=str(raw.get("odds_task_time", raw.get("racecard_task_time", "04:00"))),
                last_run_horses=str(raw.get("last_run_horses", "")),
                last_run_racecard=str(raw.get("last_run_racecard", "")),
                last_run_odds=str(raw.get("last_run_odds", "")),
                enable_interval_task=bool(raw.get("enable_interval_task", False)),
                interval_minutes=max(1, int(raw.get("interval_minutes", 5) or 5)),
                interval_horses=bool(raw.get("interval_horses", False)),
                interval_racecard=bool(raw.get("interval_racecard", False)),
                interval_odds=bool(raw.get("interval_odds", True)),
                last_interval_at=str(raw.get("last_interval_at", "")),
                run_horses=bool(
                    raw["run_horses"]
                    if "run_horses" in raw
                    else (
                        raw.get("interval_horses", False)
                        if raw.get("enable_interval_task")
                        else raw.get("enable_horses_task", True)
                    )
                ),
                run_racecard=bool(
                    raw["run_racecard"]
                    if "run_racecard" in raw
                    else (
                        raw.get("interval_racecard", False)
                        if raw.get("enable_interval_task")
                        else raw.get("enable_racecard_task", True)
                    )
                ),
                run_odds=bool(
                    raw["run_odds"]
                    if "run_odds" in raw
                    else (
                        raw.get("interval_odds", True)
                        if raw.get("enable_interval_task")
                        else raw.get("enable_odds_task", True)
                        or raw.get("enable_racecard_task", False)
                    )
                ),
                run_results=bool(raw.get("run_results", True)),
                last_run_results=str(raw.get("last_run_results", "")),
                interval_results=bool(raw.get("interval_results", False)),
                enable_results_task=bool(raw.get("enable_results_task", False)),
                results_task_time=str(raw.get("results_task_time", "05:00")),
                run_jockey_king=bool(raw.get("run_jockey_king", False)),
                run_trainer_king=bool(raw.get("run_trainer_king", False)),
                interval_jockey_king=bool(raw.get("interval_jockey_king", False)),
                interval_trainer_king=bool(raw.get("interval_trainer_king", False)),
                enable_jockey_king_task=bool(raw.get("enable_jockey_king_task", False)),
                enable_trainer_king_task=bool(raw.get("enable_trainer_king_task", False)),
                jockey_king_task_time=str(raw.get("jockey_king_task_time", "05:00")),
                trainer_king_task_time=str(raw.get("trainer_king_task_time", "05:00")),
                last_run_jockey_king=str(raw.get("last_run_jockey_king", "")),
                last_run_trainer_king=str(raw.get("last_run_trainer_king", "")),
                enable_backup=bool(raw.get("enable_backup", False)),
                backup_folder=str(raw.get("backup_folder", default_backup_folder)),
                backup_filename_template=str(
                    raw.get("backup_filename_template", "Racing_%Y%m%d_%H%M.sqlite3")
                ),
                backup_time=str(
                    raw.get("backup_time")
                    or raw.get("horses_backup_time")
                    or "02:55"
                ),
                last_backup=str(
                    raw.get("last_backup")
                    or raw.get("last_backup_horses")
                    or ""
                ),
            )
        except Exception:
            return TaskSettings(
                enable_backup=False,
                backup_folder=default_backup_folder,
            )

    def _save_task_settings(self) -> None:
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "enable_horses_task": self._task_settings.enable_horses_task,
            "horses_task_time": self._task_settings.horses_task_time,
            "enable_racecard_task": self._task_settings.enable_racecard_task,
            "racecard_task_time": self._task_settings.racecard_task_time,
            "enable_odds_task": self._task_settings.enable_odds_task,
            "odds_task_time": self._task_settings.odds_task_time,
            "last_run_horses": self._task_settings.last_run_horses,
            "last_run_racecard": self._task_settings.last_run_racecard,
            "last_run_odds": self._task_settings.last_run_odds,
            "enable_interval_task": self._task_settings.enable_interval_task,
            "interval_minutes": self._task_settings.interval_minutes,
            "interval_horses": self._task_settings.interval_horses,
            "interval_racecard": self._task_settings.interval_racecard,
            "interval_odds": self._task_settings.interval_odds,
            "last_interval_at": self._task_settings.last_interval_at,
            "run_horses": self._task_settings.run_horses,
            "run_racecard": self._task_settings.run_racecard,
            "run_odds": self._task_settings.run_odds,
            "run_results": self._task_settings.run_results,
            "last_run_results": self._task_settings.last_run_results,
            "interval_results": self._task_settings.interval_results,
            "enable_results_task": self._task_settings.enable_results_task,
            "run_jockey_king": self._task_settings.run_jockey_king,
            "run_trainer_king": self._task_settings.run_trainer_king,
            "interval_jockey_king": self._task_settings.interval_jockey_king,
            "interval_trainer_king": self._task_settings.interval_trainer_king,
            "enable_jockey_king_task": self._task_settings.enable_jockey_king_task,
            "enable_trainer_king_task": self._task_settings.enable_trainer_king_task,
            "jockey_king_task_time": self._task_settings.jockey_king_task_time,
            "trainer_king_task_time": self._task_settings.trainer_king_task_time,
            "last_run_jockey_king": self._task_settings.last_run_jockey_king,
            "last_run_trainer_king": self._task_settings.last_run_trainer_king,
            "results_task_time": self._task_settings.results_task_time,
            "enable_backup": self._task_settings.enable_backup,
            "backup_folder": self._task_settings.backup_folder,
            "backup_filename_template": self._task_settings.backup_filename_template,
            "backup_time": self._task_settings.backup_time,
            "last_backup": self._task_settings.last_backup,
        }
        self._task_settings_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _open_task_settings_dialog(self) -> None:
        dlg = TaskSettingsDialog(self, self._task_settings)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._task_settings = dlg.result_settings(self._task_settings)
        self._schedule_not_before = datetime.now()
        self._save_task_settings()
        pass  # UI 提示不寫入狀態列

    def open_task_settings_dialog(self) -> None:
        """供外部（如 Dashboard）呼叫的任務設定入口。"""
        self._open_task_settings_dialog()

    def _shell_tab_key_for_widget(self, widget: QWidget | None) -> str | None:
        if widget is None:
            return None
        if self._ai_panel is not None and widget is self._ai_panel:
            return "ai"
        if self._horses_workspace is not None and widget is self._horses_workspace:
            return "horses"
        if hasattr(self, "_racecard_panel") and widget is self._racecard_panel:
            return "racecard"
        if hasattr(self, "_results_panel") and widget is self._results_panel:
            return "results"
        if hasattr(self, "_kings_panel") and widget is self._kings_panel:
            return "kings"
        if hasattr(self, "_analysis_panel") and widget is self._analysis_panel:
            return "analysis"
        return None

    def _ensure_shell_tab(self, key: str) -> int:
        """若分頁未開啟則加入主殼；回傳分頁索引（失敗為 -1）。"""
        meta = _SHELL_TAB_DEFS.get(key)
        if meta is None:
            return -1
        if key == "ai":
            from .ai_assistant_dialog import AiAssistantPanel

            if self._ai_panel is None:
                self._ai_panel = AiAssistantPanel(
                    self._main_shell_tabs,
                    self._config,
                    action_handler=self.handle_ai_action,
                )
            widget = self._ai_panel
        elif key == "horses":
            widget = self._horses_workspace
        elif key == "racecard":
            widget = self._racecard_panel
        elif key == "kings":
            widget = self._kings_panel
        elif key == "analysis":
            widget = self._analysis_panel
        else:
            widget = self._results_panel
        if widget is None:
            return -1
        index = self._main_shell_tabs.indexOf(widget)
        if index < 0:
            index = self._main_shell_tabs.addTab(widget, str(meta["title"]))
            style = self.style() or QApplication.style()
            icon = meta.get("icon")
            if style is not None and isinstance(icon, QStyle.StandardPixmap):
                self._main_shell_tabs.setTabIcon(index, style.standardIcon(icon))
            self._refresh_shell_tab_colors()
        if key == "ai":
            self._ai_tab_index = index
        return index

    def _open_shell_tab(self, key: str) -> None:
        index = self._ensure_shell_tab(key)
        if index < 0:
            return
        self._main_shell_tabs.setCurrentIndex(index)
        self._on_main_shell_tab_changed(index)
        # 開啟賽果分頁：只重載目前選取賽日資料，不重讀賽日清單
        if key == "results" and hasattr(self, "_results_panel"):
            self._results_panel.reload()
        if key == "kings" and hasattr(self, "_kings_panel"):
            self._kings_panel.reload()
        if key == "analysis" and hasattr(self, "_analysis_panel"):
            self._analysis_panel.reload()
        if key == "ai" and self._ai_panel is not None:
            self._ai_panel.focus_input()

    def _on_shell_tab_close_requested(self, index: int) -> None:
        """關閉主殼分頁（保留面板實體，之後可再開）。"""
        if index < 0 or index >= self._main_shell_tabs.count():
            return
        self._main_shell_tabs.removeTab(index)
        if self._main_shell_tabs.count() == 0:
            self._open_shell_tab("ai")
            return
        self._refresh_shell_tab_colors()
        cur = self._main_shell_tabs.currentIndex()
        if cur >= 0:
            self._on_main_shell_tab_changed(cur)

    def _refresh_shell_tab_colors(self) -> None:
        """依目前開啟的分頁重套標題底色。"""
        if not hasattr(self, "_effect_combo"):
            return
        self._on_effect_changed(self._effect_combo.currentIndex())

    def _open_ai_assistant(self) -> None:
        """開啟 AI 助手分頁並切過去。"""
        self._open_shell_tab("ai")
        self.show()
        self.raise_()
        self.activateWindow()

    def handle_ai_action(self, action: str, arg: str | None = None) -> str:
        """執行 AI 助手要求的系統操作。"""
        if action == "open_racecard":
            self.show_racecard_shell()
            return "已開啟排位。"
        if action == "open_horses":
            self.show_horses_shell()
            return "已開啟馬匹／往績。"
        if action == "select_horse":
            self.show_horses_shell()
            hid = (arg or "").strip()
            if hid:
                self._select_horse_id(hid)
                return f"已開啟馬匹並選取 {hid}。"
            return "已開啟馬匹／往績。"
        if action == "update_horses":
            self._on_update_horses_clicked()
            return "已開始更新馬匹資料。"
        if action == "update_racecard":
            self._start_download_with_progress(
                do_horses=False, do_racecard=True, do_odds=False, title="下載排位"
            )
            return "已開始下載排位。"
        if action == "download_once":
            horses, racecard, odds, results, jockey_king, trainer_king = (
                self._parse_download_scope_arg(arg)
            )
            self._start_download_with_progress(
                do_horses=horses,
                do_racecard=racecard,
                do_odds=odds,
                do_results=results,
                do_jockey_king=jockey_king,
                do_trainer_king=trainer_king,
                title="AI 下載資料",
            )
            parts = []
            if horses:
                parts.append("馬匹及往績")
            if racecard:
                parts.append("排位")
            if odds and not racecard:
                parts.append("即時賠率")
            if results:
                parts.append("賽馬結果")
            if jockey_king:
                parts.append("騎師王")
            if trainer_king:
                parts.append("練馬師王")
            return f"已開始下載：{'、'.join(parts) or '無'}。"
        if action == "download_schedule":
            when, horses, racecard, odds, results, jockey_king, trainer_king = (
                self._parse_download_schedule_arg(arg)
            )
            message = self._apply_download_schedule(
                horses=horses,
                racecard=racecard,
                odds=odds,
                results=results,
                jockey_king=jockey_king,
                trainer_king=trainer_king,
                when=when,
            )
            return message
        if action == "download_interval":
            minutes, horses, racecard, odds, results, jockey_king, trainer_king = (
                self._parse_download_interval_arg(arg)
            )
            message = self._apply_download_interval(
                horses=horses,
                racecard=racecard,
                odds=odds,
                results=results,
                jockey_king=jockey_king,
                trainer_king=trainer_king,
                minutes=minutes,
            )
            return message
        if action == "open_download":
            self._open_download_dialog()
            return "已開啟下載設定。"
        if action == "task_settings":
            self.open_task_settings_dialog()
            return "已開啟任務設定。"
        if action == "fetch_records":
            hid = (arg or "").strip()
            if not hid:
                return "請在指令裡寫出馬名或編號，例如「抓取往績 金鎗六十」。"
            self.show_horses_shell()
            self._all_horses_checkbox.setChecked(False)
            self._select_horse_id(hid)
            self._on_fetch_clicked()
            return "已開始抓取往績。"
        return ""

    def _parse_download_scope_arg(
        self, arg: str | None
    ) -> tuple[bool, bool, bool, bool, bool, bool]:
        text = (arg or "horses,racecard,odds").lower()
        horses = "horses" in text or "馬匹" in text or "往績" in text
        racecard = "racecard" in text or "排位" in text
        odds = "odds" in text or "賠率" in text
        results = "results" in text or "賽果" in text or "賽馬結果" in text
        jockey_king = "jockey_king" in text or "騎師王" in text
        trainer_king = "trainer_king" in text or "練馬師王" in text
        if not (
            horses or racecard or odds or results or jockey_king or trainer_king
        ):
            return True, True, True, False, False, False
        return horses, racecard, odds, results, jockey_king, trainer_king

    def _parse_download_schedule_arg(
        self, arg: str | None
    ) -> tuple[str, bool, bool, bool, bool, bool, bool]:
        text = (arg or "23:00|horses,racecard,odds").strip()
        when, _, scope = text.partition("|")
        when = when.strip() or "23:00"
        horses, racecard, odds, results, jockey_king, trainer_king = (
            self._parse_download_scope_arg(scope or "horses,racecard,odds")
        )
        return when, horses, racecard, odds, results, jockey_king, trainer_king

    def _parse_download_interval_arg(
        self, arg: str | None
    ) -> tuple[int, bool, bool, bool, bool, bool, bool]:
        text = (arg or "5|odds").strip()
        minutes_text, _, scope = text.partition("|")
        try:
            minutes = max(1, int(minutes_text.strip() or "5"))
        except ValueError:
            minutes = 5
        horses, racecard, odds, results, jockey_king, trainer_king = (
            self._parse_download_scope_arg(scope or "odds")
        )
        return minutes, horses, racecard, odds, results, jockey_king, trainer_king

    def _apply_download_schedule(
        self,
        *,
        horses: bool,
        racecard: bool,
        odds: bool,
        results: bool = False,
        jockey_king: bool = False,
        trainer_king: bool = False,
        when: str,
    ) -> str:
        self._task_settings = replace(
            self._task_settings,
            enable_horses_task=horses,
            horses_task_time=when if horses else self._task_settings.horses_task_time,
            enable_racecard_task=racecard,
            racecard_task_time=when if racecard else self._task_settings.racecard_task_time,
            enable_odds_task=odds and not racecard,
            odds_task_time=when if odds else self._task_settings.odds_task_time,
            enable_results_task=results,
            results_task_time=when if results else self._task_settings.results_task_time,
            enable_jockey_king_task=jockey_king,
            jockey_king_task_time=(
                when if jockey_king else self._task_settings.jockey_king_task_time
            ),
            enable_trainer_king_task=trainer_king,
            trainer_king_task_time=(
                when if trainer_king else self._task_settings.trainer_king_task_time
            ),
            enable_interval_task=False,
            run_horses=horses,
            run_racecard=racecard,
            run_odds=odds,
            run_results=results,
            run_jockey_king=jockey_king,
            run_trainer_king=trainer_king,
        )
        self._schedule_not_before = datetime.now()
        self._save_task_settings()
        parts = []
        if horses:
            parts.append("馬匹及往績")
        if racecard:
            parts.append("排位")
        if odds and not racecard:
            parts.append("即時賠率")
        if results:
            parts.append("賽馬結果")
        if jockey_king:
            parts.append("騎師王")
        if trainer_king:
            parts.append("練馬師王")
        message = f"已設定每日 {when} 下載{'、'.join(parts)}。開啟程式時不會補抓。"
        pass  # UI 提示不寫入狀態列
        return message

    def _apply_download_interval(
        self,
        *,
        horses: bool,
        racecard: bool,
        odds: bool,
        results: bool = False,
        jockey_king: bool = False,
        trainer_king: bool = False,
        minutes: int,
    ) -> str:
        minutes = max(1, int(minutes))
        self._task_settings = replace(
            self._task_settings,
            enable_interval_task=True,
            interval_minutes=minutes,
            interval_horses=horses,
            interval_racecard=racecard,
            interval_odds=odds,
            interval_results=results,
            interval_jockey_king=jockey_king,
            interval_trainer_king=trainer_king,
            last_interval_at="",
            enable_horses_task=False,
            enable_racecard_task=False,
            enable_odds_task=False,
            enable_results_task=False,
            enable_jockey_king_task=False,
            enable_trainer_king_task=False,
            run_horses=horses,
            run_racecard=racecard,
            run_odds=odds,
            run_results=results,
            run_jockey_king=jockey_king,
            run_trainer_king=trainer_king,
        )
        self._schedule_not_before = datetime.now()
        self._save_task_settings()
        parts = []
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
        message = f"已設定每隔 {minutes} 分鐘下載{'、'.join(parts)}。程式需保持開啟。"
        pass  # UI 提示不寫入狀態列
        return message

    def _apply_download_manual(
        self,
        *,
        horses: bool,
        racecard: bool,
        odds: bool,
        results: bool = False,
        jockey_king: bool = False,
        trainer_king: bool = False,
    ) -> str:
        self._task_settings = replace(
            self._task_settings,
            enable_interval_task=False,
            enable_horses_task=False,
            enable_racecard_task=False,
            enable_odds_task=False,
            enable_results_task=False,
            enable_jockey_king_task=False,
            enable_trainer_king_task=False,
            run_horses=horses,
            run_racecard=racecard,
            run_odds=odds,
            run_results=results,
            run_jockey_king=jockey_king,
            run_trainer_king=trainer_king,
        )
        self._save_task_settings()
        parts = []
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
        message = (
            f"下載設定已儲存：{'、'.join(parts) or '無'}。"
            "於下載視窗按「執行」才會開始下載。"
        )
        pass  # UI 提示不寫入狀態列
        return message

    def _open_download_dialog(self) -> None:
        dlg = DownloadDialog(self, self._task_settings, self._config.data_dir)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        (
            mode,
            horses,
            racecard,
            odds,
            results,
            jockey_king,
            trainer_king,
            when,
            minutes,
        ) = dlg.choice()
        if mode == "interval":
            self._apply_download_interval(
                horses=horses,
                racecard=racecard,
                odds=odds,
                results=results,
                jockey_king=jockey_king,
                trainer_king=trainer_king,
                minutes=minutes,
            )
        elif mode == "schedule":
            self._apply_download_schedule(
                horses=horses,
                racecard=racecard,
                odds=odds,
                results=results,
                jockey_king=jockey_king,
                trainer_king=trainer_king,
                when=when,
            )
        else:
            self._apply_download_manual(
                horses=horses,
                racecard=racecard,
                odds=odds,
                results=results,
                jockey_king=jockey_king,
                trainer_king=trainer_king,
            )
        if dlg.should_run():
            if mode == "interval":
                title = f"間隔下載（每 {minutes} 分）"
            elif mode == "schedule":
                title = f"指定時間下載（每日 {when}）"
            else:
                title = "執行下載"
            self._start_download_with_progress(
                do_horses=horses,
                do_racecard=racecard,
                do_odds=odds,
                do_results=results,
                do_jockey_king=jockey_king,
                do_trainer_king=trainer_king,
                title=title,
                # 間隔模式首次執行也要寫入 last_interval_at，避免 15 秒內再跑一次
                from_interval=(mode == "interval"),
            )

    def _start_task_scheduler(self) -> None:
        # 只在程式開啟之後到達指定時間才下載，不補抓開啟前已過的時間。
        self._schedule_not_before = datetime.now()
        self._task_timer = QTimer(self)
        self._task_timer.timeout.connect(self._check_scheduled_tasks)
        self._task_timer.start(15000)

    def _scheduled_moment(self, hhmm: str, now: datetime) -> datetime:
        hour_text, minute_text = hhmm.split(":") if ":" in hhmm else ("00", "00")
        return now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)

    def _is_due(self, hhmm: str, last_run: str, now: datetime) -> bool:
        if last_run == now.strftime("%Y-%m-%d"):
            return False
        moment = self._scheduled_moment(hhmm, now)
        if moment < self._schedule_not_before:
            return False
        return now >= moment

    def _interval_due(self, now: datetime) -> bool:
        if not self._task_settings.enable_interval_task:
            return False
        if not (
            self._task_settings.interval_horses
            or self._task_settings.interval_racecard
            or self._task_settings.interval_odds
            or self._task_settings.interval_results
            or self._task_settings.interval_jockey_king
            or self._task_settings.interval_trainer_king
        ):
            return False
        minutes = max(1, int(self._task_settings.interval_minutes or 5))
        last_text = (self._task_settings.last_interval_at or "").strip()
        if not last_text:
            return now >= self._schedule_not_before
        try:
            last_at = datetime.fromisoformat(last_text)
        except ValueError:
            return True
        return now >= last_at + timedelta(minutes=minutes)

    def _download_allowed(self, now: datetime) -> bool:
        if not self._task_settings.enable_backup:
            return True
        if self._task_settings.last_backup == now.strftime("%Y-%m-%d"):
            return True
        moment = self._scheduled_moment(self._task_settings.backup_time, now)
        if moment < self._schedule_not_before or now < moment:
            return True
        return False

    def _check_scheduled_tasks(self) -> None:
        try:
            self._check_scheduled_tasks_impl()
        except Exception as exc:  # noqa: BLE001
            self._set_background_status(f"排程任務錯誤：{exc}")

    def _check_scheduled_tasks_impl(self) -> None:
        now = datetime.now()

        if (
            self._task_settings.enable_backup
            and (
                self._task_settings.enable_horses_task
                or self._task_settings.enable_racecard_task
                or self._task_settings.enable_odds_task
                or self._task_settings.enable_interval_task
            )
            and self._is_due(self._task_settings.backup_time, self._task_settings.last_backup, now)
        ):
            self._run_background_job("backup")

        if not self._download_allowed(now):
            return

        if self._interval_due(now):
            self._start_download_with_progress(
                do_horses=self._task_settings.interval_horses,
                do_racecard=self._task_settings.interval_racecard,
                do_odds=self._task_settings.interval_odds,
                do_results=self._task_settings.interval_results,
                do_jockey_king=self._task_settings.interval_jockey_king,
                do_trainer_king=self._task_settings.interval_trainer_king,
                title=f"間隔下載（每 {self._task_settings.interval_minutes} 分）",
                from_interval=True,
            )
            return

        due_horses = self._task_settings.enable_horses_task and self._is_due(
            self._task_settings.horses_task_time, self._task_settings.last_run_horses, now
        )
        due_racecard = self._task_settings.enable_racecard_task and self._is_due(
            self._task_settings.racecard_task_time, self._task_settings.last_run_racecard, now
        )
        due_odds = self._task_settings.enable_odds_task and self._is_due(
            self._task_settings.odds_task_time, self._task_settings.last_run_odds, now
        )
        due_results = self._task_settings.enable_results_task and self._is_due(
            self._task_settings.results_task_time, self._task_settings.last_run_results, now
        )
        due_jockey = self._task_settings.enable_jockey_king_task and self._is_due(
            self._task_settings.jockey_king_task_time,
            self._task_settings.last_run_jockey_king,
            now,
        )
        due_trainer = self._task_settings.enable_trainer_king_task and self._is_due(
            self._task_settings.trainer_king_task_time,
            self._task_settings.last_run_trainer_king,
            now,
        )
        if (
            due_horses
            or due_racecard
            or due_odds
            or due_results
            or due_jockey
            or due_trainer
        ):
            self._start_download_with_progress(
                do_horses=due_horses,
                do_racecard=due_racecard,
                do_odds=due_odds,
                do_results=due_results,
                do_jockey_king=due_jockey,
                do_trainer_king=due_trainer,
                title="指定時間下載",
            )

    def _mark_download_done(
        self,
        *,
        horses: bool,
        racecard: bool,
        odds: bool = False,
        results: bool = False,
        jockey_king: bool = False,
        trainer_king: bool = False,
        interval: bool = False,
    ) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        now_iso = datetime.now().isoformat(timespec="seconds")
        self._task_settings = replace(
            self._task_settings,
            last_run_horses=today if horses else self._task_settings.last_run_horses,
            last_run_racecard=today if racecard else self._task_settings.last_run_racecard,
            last_run_odds=today if (odds or racecard) else self._task_settings.last_run_odds,
            last_run_results=today if results else self._task_settings.last_run_results,
            last_run_jockey_king=(
                today if jockey_king else self._task_settings.last_run_jockey_king
            ),
            last_run_trainer_king=(
                today if trainer_king else self._task_settings.last_run_trainer_king
            ),
            last_interval_at=now_iso if interval else self._task_settings.last_interval_at,
        )
        self._save_task_settings()

    def _set_background_status(self, message: str) -> None:
        """狀態列只顯示背景執行訊息。"""
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(str(message or "就緒"))

    def _cancel_background_download(self) -> None:
        worker = self._download_worker
        if worker is None:
            return
        self._download_cancel_btn.setEnabled(False)
        self._set_background_status("下載：取消中…")
        worker.cancel()

    def _set_download_status_ui(self, *, active: bool) -> None:
        self._download_progress.setVisible(active)
        self._download_cancel_btn.setVisible(active)
        self._download_cancel_btn.setEnabled(active)
        if not active:
            self._download_progress.setRange(0, 1)
            self._download_progress.setValue(0)
            self._download_progress.setFormat("%v/%m")

    def _on_download_stage(self, message: str) -> None:
        self._download_stage_msg = str(message or "")
        cur = getattr(self, "_download_progress_cur", 0)
        tot = getattr(self, "_download_progress_tot", 0)
        if tot > 0:
            self._set_background_status(
                f"下載：{self._download_stage_msg}（{cur}/{tot}）"
            )
        else:
            self._set_background_status(f"下載：{self._download_stage_msg}")

    def _on_download_progress(self, current: int, total: int) -> None:
        self._download_progress_cur = max(0, int(current))
        self._download_progress_tot = max(0, int(total))
        stage = getattr(self, "_download_stage_msg", "") or "進行中"
        if total <= 0:
            self._download_progress.setRange(0, 0)
            self._download_progress.setFormat("進行中")
            self._set_background_status(f"下載：{stage}")
            return
        self._download_progress.setRange(0, total)
        self._download_progress.setValue(max(0, min(current, total)))
        self._download_progress.setFormat("%v/%m")
        self._set_background_status(f"下載：{stage}（{current}/{total}）")

    def _start_download_with_progress(
        self,
        *,
        do_horses: bool,
        do_racecard: bool,
        do_odds: bool = False,
        do_results: bool = False,
        do_jockey_king: bool = False,
        do_trainer_king: bool = False,
        title: str = "下載資料",
        from_interval: bool = False,
    ) -> None:
        if not (
            do_horses
            or do_racecard
            or do_odds
            or do_results
            or do_jockey_king
            or do_trainer_king
        ):
            return
        jobs = []
        if do_horses:
            jobs.append("horses")
        if do_racecard:
            jobs.append("racecard")
        if do_odds and not do_racecard:
            jobs.append("odds")
        if do_results:
            jobs.append("results")
        if do_jockey_king:
            jobs.append("jockey_king")
        if do_trainer_king:
            jobs.append("trainer_king")
        if any(job in self._running_jobs for job in jobs):
            self._set_background_status("下載進行中，請稍候…")
            return
        if self._download_thread is not None and self._download_thread.isRunning():
            self._set_background_status("下載進行中，請稍候…")
            return

        for job in jobs:
            self._running_jobs.add(job)

        # 不要把 QThread 設成 MainWindow 的子物件，避免視窗或 deleteLater
        # 過早銷毀執行緒，造成「QThread: Destroyed while still running」後程式退出。
        thread = QThread()
        worker = DownloadWorker(
            self._config,
            do_horses=do_horses,
            do_racecard=do_racecard,
            do_odds=do_odds,
            do_results=do_results,
            do_jockey_king=do_jockey_king,
            do_trainer_king=do_trainer_king,
        )
        self._download_thread = thread
        self._download_worker = worker
        self._download_jobs = list(jobs)
        self._download_flags = (
            do_horses,
            do_racecard,
            do_odds,
            do_results,
            do_jockey_king,
            do_trainer_king,
            from_interval,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stage_changed.connect(self._on_download_stage, Qt.ConnectionType.QueuedConnection)
        worker.progress_changed.connect(self._on_download_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished_ok.connect(thread.quit)
        worker.finished_error.connect(thread.quit)
        worker.finished_ok.connect(self._on_download_finished_ok, Qt.ConnectionType.QueuedConnection)
        worker.finished_error.connect(self._on_download_finished_error, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(self._on_download_thread_finished, Qt.ConnectionType.QueuedConnection)

        self._set_download_status_ui(active=True)
        self._download_progress.setRange(0, 0)
        self._download_progress.setFormat("進行中")
        self._set_background_status(f"{title}：背景下載中…（可繼續操作）")
        thread.start()

    def _on_download_finished_ok(self, message: str) -> None:
        flags = getattr(self, "_download_flags", (False, False, False, False, False))
        do_jockey_king = False
        do_trainer_king = False
        if len(flags) == 4:
            do_horses, do_racecard, do_odds, from_interval = flags
            do_results = False
        elif len(flags) == 5:
            do_horses, do_racecard, do_odds, do_results, from_interval = flags
        else:
            (
                do_horses,
                do_racecard,
                do_odds,
                do_results,
                do_jockey_king,
                do_trainer_king,
                from_interval,
            ) = flags
        try:
            self._mark_download_done(
                horses=do_horses,
                racecard=do_racecard,
                odds=do_odds,
                results=do_results,
                jockey_king=do_jockey_king,
                trainer_king=do_trainer_king,
                interval=from_interval,
            )
            self._set_background_status(f"下載完成：{message}")
            if (do_racecard or do_odds) and hasattr(self, "_racecard_panel"):
                self._racecard_panel.reload_stored()
            if (do_jockey_king or do_trainer_king) and hasattr(self, "_kings_panel"):
                self._kings_panel.reload_stored()
            if do_horses:
                self._refresh_horse_id_combo()
                self._on_search_clicked()
        except Exception as exc:  # noqa: BLE001
            self._set_background_status(f"下載完成，但畫面刷新失敗：{exc}")
        finally:
            self._release_download_jobs()

    def _on_download_finished_error(self, message: str) -> None:
        try:
            if message == "已取消。":
                self._set_background_status("下載已取消。")
            else:
                self._set_background_status(f"下載失敗：{message}")
        finally:
            self._release_download_jobs()

    def _release_download_jobs(self) -> None:
        for job in getattr(self, "_download_jobs", []):
            self._running_jobs.discard(job)
        self._download_jobs = []
        self._set_download_status_ui(active=False)

    def _on_download_thread_finished(self) -> None:
        worker = self._download_worker
        thread = self._download_thread
        self._download_worker = None
        self._download_thread = None
        self._release_download_jobs()
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

    def closeEvent(self, event) -> None:  # noqa: N802
        worker = self._download_worker
        thread = self._download_thread
        if worker is not None:
            try:
                worker.cancel()
            except RuntimeError:
                pass
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(5000)
            except RuntimeError:
                pass
        ai_panel = getattr(self, "_ai_panel", None)
        if ai_panel is not None:
            try:
                ai_panel.shutdown()
            except RuntimeError:
                pass
        for t in list(getattr(self, "_task_threads", [])):
            try:
                if t.isRunning():
                    t.quit()
                    t.wait(2000)
            except RuntimeError:
                pass
        super().closeEvent(event)

    def _run_background_job(self, job: str) -> None:
        if job in {"horses", "racecard", "odds"}:
            self._start_download_with_progress(
                do_horses=job == "horses",
                do_racecard=job == "racecard",
                do_odds=job == "odds",
                title="下載資料",
            )
            return
        if job in self._running_jobs:
            return
        self._running_jobs.add(job)

        if job == "backup":
            fn = self._run_backup_job
        else:
            self._running_jobs.discard(job)
            return
        worker = _BackgroundTaskWorker(fn)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def on_ok(msg: str) -> None:
            today = datetime.now().strftime("%Y-%m-%d")
            self._task_settings = replace(
                self._task_settings,
                last_backup=today if job == "backup" else self._task_settings.last_backup,
            )
            self._save_task_settings()
            self._set_background_status(msg)

        def on_err(err: str) -> None:
            self._set_background_status(f"自動任務失敗（{job}）：{err}")

        def on_done() -> None:
            self._running_jobs.discard(job)
            thread.quit()

        worker.finished_ok.connect(on_ok)
        worker.finished_ok.connect(lambda _: on_done())
        worker.finished_error.connect(on_err)
        worker.finished_error.connect(lambda _: on_done())
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._task_threads.append(thread)

    def _run_backup_job(self, *, force: bool = False) -> str:
        """整個 DB 備份。排程每日一次；功能表手動備份可 force=True。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if not force and not self._task_settings.enable_backup:
            return "備份：已停用（請至任務設定啟用，或於功能表手動備份）"
        if not force and self._task_settings.last_backup == today:
            return "備份：跳過（今日已備份）"

        src_db = self._config.db_path
        if not src_db.exists():
            return "備份：跳過（DB 不存在）"

        folder_text = (self._task_settings.backup_folder or "").strip()
        if not folder_text:
            folder_text = str(self._config.data_dir / "backup")
        backup_dir = Path(folder_text)
        if not backup_dir.is_absolute():
            p = str(backup_dir).replace("\\", "/").lower()
            if p == "data" or p.startswith("data/"):
                backup_dir = self._config.data_dir.parent / backup_dir
            else:
                backup_dir = self._config.data_dir / backup_dir

        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            if force:
                fname = now.strftime("Racing_%Y%m%d_%H%M%S.sqlite3")
            else:
                backup_time = self._task_settings.backup_time or "00:00"
                hh_str, mm_str = backup_time.split(":") if ":" in backup_time else ("00", "00")
                backup_dt = datetime(now.year, now.month, now.day, int(hh_str), int(mm_str))
                fname = backup_dt.strftime(self._task_settings.backup_filename_template)
            dst_db = backup_dir / fname
            shutil.copy2(str(src_db), str(dst_db))
            self._task_settings = replace(self._task_settings, last_backup=today)
            self._save_task_settings()
            return f"備份完成：{dst_db}"
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(str(e)) from e

    # 兼容舊的函式名稱（現已改成唯一備份時間 + 單一 backup job）
    def _run_horses_backup_job(self) -> str:
        return self._run_backup_job()

    def _run_racecard_backup_job(self) -> str:
        return self._run_backup_job()

    def show_horses_shell(self) -> None:
        """顯示主視窗並切到「馬匹／往績」頂層分頁（供 Dashboard 選單等呼叫）。"""
        self._open_shell_tab("horses")
        self.show()
        self.raise_()
        self.activateWindow()

    def show_racecard_shell(self) -> None:
        """顯示主視窗並切到內嵌「排位」頂層分頁。"""
        self._open_shell_tab("racecard")
        self.show()
        self.raise_()
        self.activateWindow()

    def show_results_shell(self) -> None:
        """顯示主視窗並切到內嵌「賽馬結果」頂層分頁。"""
        self._open_shell_tab("results")
        self.show()
        self.raise_()
        self.activateWindow()

    def show_ai_shell(self) -> None:
        """顯示主視窗並切到「AI 助手」分頁。"""
        self._open_ai_assistant()

    def _on_main_shell_tab_changed(self, index: int) -> None:
        """切換主殼分頁時，明確顯示目前開啟項目。"""
        widget = self._main_shell_tabs.widget(index) if index >= 0 else None
        key = self._shell_tab_key_for_widget(widget)
        meta = _SHELL_TAB_DEFS.get(key or "")
        tab_name = str(meta["title"]) if meta else "—"
        if hasattr(self, "_current_view_label"):
            self._current_view_label.setText(f"目前開啟：{tab_name}")
        if self.statusBar() is not None:
            pass  # UI 提示不寫入狀態列
        # 更新選取分頁底色／文字色（不使用非法 QSS nth-child）
        self._refresh_shell_tab_colors()

    def _on_racecard_horse_double_clicked(self, horse_id: str) -> None:
        """在排位表雙擊馬匹時：切回主頁並自動開啟該馬往績分頁。"""
        hid = str(horse_id or "").strip()
        if not hid:
            return
        self._open_shell_tab("horses")
        self._select_horse_id(hid)
        self._detail_tabs.setCurrentIndex(1)
        pass  # UI 提示不寫入狀態列

    def _apply_shell_tab_colors(self) -> None:
        """以 setTabTextColor 上色（Qt Style Sheet 不支援 :nth-child）。"""
        from PySide6.QtGui import QColor

        if not hasattr(self, "_main_shell_tabs"):
            return
        bar = self._main_shell_tabs.tabBar()
        for i in range(self._main_shell_tabs.count()):
            key = self._shell_tab_key_for_widget(self._main_shell_tabs.widget(i))
            meta = _SHELL_TAB_DEFS.get(key or "")
            if not meta:
                continue
            # 選取時用較深色字／未選用定義色
            if i == self._main_shell_tabs.currentIndex():
                bar.setTabTextColor(i, QColor(str(meta["fg_sel"])))
            else:
                bar.setTabTextColor(i, QColor(str(meta["fg"])))

    def _main_shell_tabs_style(self, idx: int) -> str:
        """頂層分頁：邊框與分頁列（僅使用 Qt 支援的 QSS）。"""
        theme = theme_by_index(idx)
        border = theme.border
        # 依目前選取分頁套對應底色（避免 nth-child 導致整份 QSS 解析失敗）
        sel_bg = theme.header_bg
        sel_fg = theme.label
        if hasattr(self, "_main_shell_tabs"):
            cur = self._main_shell_tabs.currentIndex()
            if cur >= 0:
                key = self._shell_tab_key_for_widget(self._main_shell_tabs.widget(cur))
                meta = _SHELL_TAB_DEFS.get(key or "")
                if meta:
                    sel_bg = str(meta["bg_sel"])
                    sel_fg = str(meta["fg_sel"])
        return f"""
QTabWidget#mainShellTabs::pane {{
  border: 2px solid {border};
  border-radius: 10px;
  padding: 6px;
  background: transparent;
}}
QTabBar#mainShellTabBar::tab {{
  min-width: 120px;
  padding: 8px 18px 8px 12px;
  margin-right: 4px;
  color: {theme.text};
  background: {theme.header_bg};
  border: 1px solid {border};
  border-bottom: none;
  border-top-left-radius: 8px;
  border-top-right-radius: 8px;
  font-weight: 600;
}}
QTabBar#mainShellTabBar::tab:selected {{
  color: {sel_fg};
  background: {sel_bg};
}}
QTabBar#mainShellTabBar::close-button {{
  subcontrol-position: right;
  margin: 2px;
}}
QTabBar#mainShellTabBar::close-button:hover {{
  background: {theme.button_hover_start};
  border-radius: 3px;
}}
"""

    def _horse_id(self) -> str:
        """實際用於查詢的 HorseId：下拉選定用 userData；手打時僅用補全表／HK_ 格式（其餘離開欄位後才解析）。"""
        idx = self._horse_id_input.currentIndex()
        if idx >= 0:
            data = self._horse_id_input.itemData(idx)
            if data:
                return str(data).strip()
        text = self._horse_id_input.currentText().strip()
        if not text:
            return ""
        if text in self._horse_completion_map:
            return self._horse_completion_map[text]
        if re.fullmatch(r"HK_[A-Za-z0-9_]+", text, flags=re.IGNORECASE):
            return text
        return text

    def _resolve_input_to_horse_id(self, text: str) -> str:
        """將輸入對應為 HorseId；無法唯一對應時保留原字串（例如關鍵字過寬）。"""
        t = text.strip()
        if not t:
            return ""
        if t in self._horse_completion_map:
            return self._horse_completion_map[t]
        if re.fullmatch(r"HK_[A-Za-z0-9_]+", t, flags=re.IGNORECASE):
            return t
        if fetch_horse_by_id(self._config.db_path, t) is not None:
            return t
        rows = search_horses_by_keyword(self._config.db_path, t, limit=40)
        if not rows:
            return t
        if len(rows) == 1:
            return str(rows[0]["horse_id"])
        exact_brand = [r for r in rows if str(r["brand_no"] or "").strip().upper() == t.upper()]
        if len(exact_brand) == 1:
            return str(exact_brand[0]["horse_id"])
        exact_zh = [r for r in rows if str(r["name_zh"] or "").strip() == t]
        if len(exact_zh) == 1:
            return str(exact_zh[0]["horse_id"])
        exact_en = [r for r in rows if str(r["name_en"] or "").strip().upper() == t.upper()]
        if len(exact_en) == 1:
            return str(exact_en[0]["horse_id"])
        return t

    def _rebuild_horse_completion_map(self) -> None:
        """補全清單：HorseId、烙號、中英文名、與下拉顯示字串 -> horse_id。"""
        self._horse_completion_map.clear()
        rows = fetch_horses(self._config.db_path, limit=None)
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            if not hid:
                continue
            brand = str(r["brand_no"] or "").strip()
            zh = str(r["name_zh"] or "").strip()
            en = str(r["name_en"] or "").strip()
            display = "｜".join(p for p in [brand or hid, zh, en] if p)

            def add_key(k: str) -> None:
                k2 = k.strip()
                if not k2:
                    return
                if k2 in self._horse_completion_map and self._horse_completion_map[k2] != hid:
                    return
                self._horse_completion_map[k2] = hid

            add_key(hid)
            add_key(brand)
            add_key(zh)
            add_key(en)
            add_key(display)

        model = QStringListModel(sorted(self._horse_completion_map.keys()))
        self._horse_completer.setModel(model)

    def _on_horse_completer_activated(self, text: str) -> None:
        hid = self._horse_completion_map.get(text.strip())
        if hid:
            self._select_horse_id(hid)

    def _on_horse_id_line_finished(self) -> None:
        """離開輸入欄時：若可唯一解析為 HorseId，則正規化顯示並同步。"""
        if self._horse_id_input.signalsBlocked():
            return
        raw = self._horse_id_input.currentText().strip()
        if not raw:
            return
        resolved = self._resolve_input_to_horse_id(raw)
        if resolved and resolved != raw:
            self._select_horse_id(resolved)

    def _refresh_horse_id_combo(self) -> None:
        """Refresh HorseId combo items from horses table."""
        current = self._horse_id()
        rows = fetch_horses(self._config.db_path, limit=None)
        ids: list[str] = []
        seen: set[str] = set()
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            if not hid or hid in seen:
                continue
            ids.append(hid)
            seen.add(hid)

        self._horse_id_input.blockSignals(True)
        self._horse_id_input.clear()
        for r in rows:
            hid = str(r["horse_id"] or "").strip()
            if not hid:
                continue
            brand_no = str(r["brand_no"] or "").strip()
            name_zh = str(r["name_zh"] or "").strip()
            name_en = str(r["name_en"] or "").strip()
            # 顯示：Brand No. + 馬名；內部 userData 仍存 horse_id
            display = "｜".join(
                [p for p in [brand_no or hid, name_zh or "", name_en or ""] if p]
            )
            self._horse_id_input.addItem(display, userData=hid)
        if current:
            idx = self._horse_id_input.findData(current)
            if idx >= 0:
                self._horse_id_input.setCurrentIndex(idx)
            else:
                # 保留手動輸入值（可能不在 horses 清單內）
                self._horse_id_input.setEditText(current)
        self._horse_id_input.blockSignals(False)
        self._rebuild_horse_completion_map()

    def _refresh_race_year_combo(self) -> None:
        """重建「抓取年份」下拉（僅影響抓取／下載，不影響往績表格）。"""
        current_year = str(self._race_year_combo.currentData() or "")
        current = datetime.now().year
        # 允許預先選未來年份（例如來季/下季）
        years = [str(y) for y in range(current + 5, 1999, -1)]

        self._race_year_combo.blockSignals(True)
        self._race_year_combo.clear()
        self._race_year_combo.addItem("全部年份", userData="")
        for y in years:
            self._race_year_combo.addItem(f"{y}年", userData=y)

        if current_year and current_year in years:
            idx = self._race_year_combo.findData(current_year)
            if idx >= 0:
                self._race_year_combo.setCurrentIndex(idx)
        elif current_year:
            self._race_year_combo.addItem(f"{current_year}年", userData=current_year)
            self._race_year_combo.setCurrentIndex(self._race_year_combo.count() - 1)
        else:
            self._race_year_combo.setCurrentIndex(0)
        self._race_year_combo.blockSignals(False)

    def _selected_race_year(self) -> str:
        if self._all_years_checkbox.isChecked():
            return ""
        return str(self._race_year_combo.currentData() or "").strip()

    @staticmethod
    def _extract_year(date_text: str) -> str:
        """Extract year from race_date text (supports dd/mm/yy and yyyy)."""
        if not date_text:
            return ""
        # 1) direct 4-digit year
        m4 = re.search(r"(19|20)\d{2}", date_text)
        if m4:
            return m4.group(0)
        # 2) trailing 2-digit year in date like 01/03/26
        m2 = re.search(r"\b\d{1,2}/\d{1,2}/(\d{2})\b", date_text)
        if m2:
            yy = int(m2.group(1))
            return f"{2000 + yy:04d}" if yy <= 69 else f"{1900 + yy:04d}"
        return ""

    @staticmethod
    def _format_race_date_display(date_text: str) -> str:
        """顯示用日期：yyyy/MM/dd。"""
        from racing.web_utils import normalize_date_yyyy_mm_dd

        return normalize_date_yyyy_mm_dd(date_text)

    @staticmethod
    def _race_date_sort_key(date_text: str) -> int:
        """Return sortable yyyymmdd key for race date."""
        from racing.web_utils import race_date_sort_key

        return race_date_sort_key(date_text)

    def _on_all_years_toggled(self, checked: bool) -> None:
        self._race_year_combo.setEnabled(not checked)

    def _on_all_horses_toggled(self, checked: bool) -> None:
        # 「所有馬匹」只影響馬匹範圍，不強制年份；年份仍由右側 controls 決定。
        self._horse_id_input.setEnabled(not checked)
        self._all_years_checkbox.setEnabled(True)
        # 勾選「所有馬匹」時，預設改為可選年份（避免看起來被強制所有年份）
        if checked and self._all_years_checkbox.isChecked():
            self._all_years_checkbox.setChecked(False)
        self._race_year_combo.setEnabled(not self._all_years_checkbox.isChecked())

    def _select_horse_row_by_horse_id(self, horse_id: str) -> None:
        """Select the matching row in horses grid."""
        if not horse_id:
            return
        for row_idx in range(self._horses_table.rowCount()):
            item = self._horses_table.item(row_idx, 0)
            if item and item.text().strip() == horse_id:
                self._horses_table.setCurrentCell(row_idx, 0)
                self._horses_table.scrollToItem(item)
                break

    def _on_horse_id_changed(self, text: str) -> None:
        """Sync grid/details when HorseId combo changes."""
        _ = text
        horse_id = self._horse_id()
        self._refresh_race_year_combo()
        if not horse_id:
            self._sync_selected_horse_panels()
            self._update_display_count_label()
            return
        self._select_horse_row_by_horse_id(horse_id)
        self._sync_selected_horse_panels()
        self._update_display_count_label()

    def _on_horses_sort_changed(self, section: int, order: Qt.SortOrder) -> None:
        self._horses_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(self._horses_headers, section, order)
        )

    def _on_detail_race_sort_changed(self, section: int, order: Qt.SortOrder) -> None:
        self._detail_race_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(self._race_headers, section, order)
        )

    def _refresh_sort_header_labels(self) -> None:
        h1 = self._horses_table.horizontalHeader()
        self._horses_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(
                self._horses_headers, h1.sortIndicatorSection(), h1.sortIndicatorOrder()
            )
        )
        h2 = self._detail_race_table.horizontalHeader()
        self._detail_race_table.setHorizontalHeaderLabels(
            _headers_with_sort_arrow(
                self._race_headers, h2.sortIndicatorSection(), h2.sortIndicatorOrder()
            )
        )

    def _update_display_count_label(self) -> None:
        """更新「Grid Row N/M」：N = 目前選中列（current row），M = 總列數（row count）。"""
        cur = self._horses_table.currentRow()
        n = cur + 1 if cur >= 0 else "—"
        m = self._horses_table.rowCount()
        self._display_count_label.setText(f"Grid Row {n}/{m}")

    def _select_horse_id(self, horse_id: str) -> None:
        le = self._horse_id_input.lineEdit()
        self._horse_id_input.blockSignals(True)
        le.blockSignals(True)
        try:
            idx = self._horse_id_input.findData(horse_id)
            if idx >= 0:
                self._horse_id_input.setCurrentIndex(idx)
            else:
                self._horse_id_input.setEditText(horse_id)
        finally:
            le.blockSignals(False)
            self._horse_id_input.blockSignals(False)
        pass  # UI 提示不寫入狀態列
        self._refresh_race_year_combo()
        self._select_horse_row_by_horse_id(horse_id)
        self._sync_selected_horse_panels()
        self._update_display_count_label()

    def _on_search_horses_clicked(self) -> None:
        self._on_search_clicked()

    def _on_horses_selection_changed(self) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx >= 0:
            item = self._horses_table.item(row_idx, 0)
            if item:
                hid = item.text().strip()
                if hid and hid != self._horse_id():
                    self._select_horse_id(hid)
                    return
        self._sync_selected_horse_panels()
        self._update_display_count_label()

    def _on_select_horse_clicked(self) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx < 0:
            QMessageBox.warning(self, "未選擇", "請先在搜尋結果中選擇一筆。")
            return

        item = self._horses_table.item(row_idx, 0)
        if item is None:
            QMessageBox.warning(self, "資料錯誤", "未找到該筆的 horse_id。")
            return

        horse_id = item.text().strip()
        if horse_id is None:
            QMessageBox.warning(self, "資料錯誤", "該項目的 horse_id 取得失敗。")
            return

        self._select_horse_id(str(horse_id))

    def _on_horse_table_item_double_clicked(self, _item: QTableWidgetItem) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx < 0:
            return
        item = self._horses_table.item(row_idx, 0)
        if item is None:
            return
        self._select_horse_id(item.text().strip())

    def _on_update_horses_clicked(self) -> None:
        started_at = datetime.now()
        before_total = count_horses(self._config.db_path)
        self._set_background_status(f"更新馬匹資料中…（開始：{started_at:%Y-%m-%d %H:%M:%S}）")

        dlg = HorsesUpdateProgressDialog(self, self._config.db_path)

        def on_ok(obj: object) -> None:
            stats = obj if isinstance(obj, HorsesUpdateStats) else None
            if stats is None:
                QMessageBox.critical(self, "更新失敗", "更新回傳資料格式不正確。")
                return

            finished_at = datetime.now()
            after_total = count_horses(self._config.db_path)
            added = max(after_total - before_total, 0)
            self._set_background_status(
                f"更新馬匹完成：寫入 {stats.written}｜新增 {added}｜總數 {after_total}"
            )
            QMessageBox.information(
                self,
                "更新完成",
                "\n".join(
                    [
                        f"開始時間：{started_at:%Y-%m-%d %H:%M:%S}",
                        f"結束時間：{finished_at:%Y-%m-%d %H:%M:%S}",
                        f"總搜尋到：{stats.total_found} 筆",
                        f"寫入（INSERT OR REPLACE）：{stats.written} 筆",
                        f"新增（DB 總數差值）：{added} 筆",
                        f"現役馬匹總數 {after_total}",
                    ]
                ),
            )
            self._refresh_horse_id_combo()
            self._on_search_clicked()

        dlg.finished_ok.connect(on_ok)
        dlg.start()

    def _on_clear_clicked(self) -> None:
        self._horse_name_search_input.setText("")
        self._on_search_clicked()

    def _on_show_all_clicked(self) -> None:
        total = count_horses(self._config.db_path)
        # Allow 0 (empty) gracefully.
        self._limit_spin.setValue(max(total, 50) if total else 50)
        self._on_search_clicked()

    def _on_search_clicked(self) -> None:
        keyword = self._horse_name_search_input.text().strip()
        # Grid 顯示全部：不在 DB query 加入 LIMIT。
        limit: int | None = None

        self._horses_table.setSortingEnabled(False)
        rows = (
            search_horses_by_name(self._config.db_path, keyword, limit=limit)
            if keyword
            else fetch_horses(self._config.db_path, limit=limit)
        )
        rows = [r for r in rows if str(r["name_zh"] or "").strip()]
        self._horses_table.setRowCount(0)
        for r in rows:
            row_idx = self._horses_table.rowCount()
            self._horses_table.insertRow(row_idx)
            values = [
                str(r["horse_id"]),
                r["brand_no"] or "",
                r["name_en"] or "",
                r["name_zh"] or "",
                r["status"] or "",
            ]
            for col, v in enumerate(values):
                item = make_table_item(v, numeric=False)
                self._horses_table.setItem(row_idx, col, item)
        self._horses_table.setSortingEnabled(True)
        # 套用預設排序：Name ZH 升序。
        self._horses_table.sortItems(3, Qt.AscendingOrder)
        resize_grid_columns_to_content(self._horses_table)
        self._refresh_sort_header_labels()
        if rows:
            self._horses_table.selectRow(0)
        self._sync_selected_horse_panels()
        total = count_horses(self._config.db_path)
        if not rows:
            pass  # UI 提示不寫入狀態列
        else:
            pass  # UI 提示不寫入狀態列
        self._update_display_count_label()

    def _sync_selected_horse_panels(self) -> None:
        """選定馬匹時同步：分頁 1 馬匹表單 + 分頁 2 往績 Grid（不強制切換目前分頁）。"""
        self._update_horses_details()
        self._load_race_records_into_detail_table(self._horse_id())

    def _load_race_records_into_detail_table(self, horse_id: str) -> None:
        """依目前 HorseId 填入下方「往績」分頁 Grid（一律顯示全部往績，不依上方抓取年份篩選）。"""
        table = self._detail_race_table
        if not horse_id:
            table.setRowCount(0)
            self._race_records_total_for_horse = 0
            self._refresh_sort_header_labels()
            return

        rows = fetch_race_records(self._config.db_path, horse_id)

        table.setSortingEnabled(False)
        table.setRowCount(0)
        flag_status = get_horse_status(self._config.db_path, horse_id)

        for r in rows:
            row_idx = table.rowCount()
            table.insertRow(row_idx)
            date_text = format_date_yyyy_mm_dd(r.race_date)
            values = [
                date_text,
                r.venue or "",
                format_integer(r.race_no) if str(r.race_no or "").strip() else "",
                r.course or "",
                format_integer(r.distance) if str(r.distance or "").replace(".", "", 1).isdigit() else (r.distance or ""),
                r.going or "",
                r.class_band or "",
                format_two_digit_99(r.draw),
                format_integer(r.rating) if str(r.rating or "").strip() else "",
                r.jockey or "",
                r.trainer or "",
                format_integer(r.handicap) if str(r.handicap or "").strip() else (r.handicap or ""),
                format_two_digit_99(r.finish_pos),
                r.running_pos or "",
                str(r.win_odds or ""),
                r.time or "",
                format_money(r.prize) if str(r.prize or "").strip() else "",
                flag_status or "",
            ]
            numeric_cols = {2, 4, 7, 8, 11, 12, 14, 16}
            for col, v in enumerate(values):
                if col == 0:
                    item = DateSortItem(str(v))
                    item.setData(Qt.UserRole, self._race_date_sort_key(str(v)))
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                else:
                    item = make_table_item(str(v), numeric=col in numeric_cols)
                table.setItem(row_idx, col, item)

        resize_grid_columns_to_content(table)
        table.setSortingEnabled(True)
        self._refresh_sort_header_labels()
        if table.rowCount() > 0:
            table.selectRow(0)
        self._race_records_total_for_horse = count_race_records(self._config.db_path, horse_id)

    def _on_detail_tab_changed(self, index: int) -> None:
        """切換明細分頁時重新整理該頁資料（與目前選定馬匹同步）。"""
        if index == 0:
            self._update_horses_details()
        elif index == 1:
            self._load_race_records_into_detail_table(self._horse_id())

    def _clear_horse_detail_fields(self) -> None:
        self._horse_id_detail.setText("")
        self._brand_no_detail.setText("")
        self._horse_name_en_detail.setText("")
        self._horse_name_zh_detail.setText("")
        self._horse_status_detail.setText("")

    def _update_horses_details(self) -> None:
        row_idx = self._horses_table.currentRow()
        if row_idx < 0:
            self._clear_horse_detail_fields()
            return
        self._horse_id_detail.setText(self._horses_table.item(row_idx, 0).text() if self._horses_table.item(row_idx, 0) else "")
        self._brand_no_detail.setText(self._horses_table.item(row_idx, 1).text() if self._horses_table.item(row_idx, 1) else "")
        self._horse_name_en_detail.setText(self._horses_table.item(row_idx, 2).text() if self._horses_table.item(row_idx, 2) else "")
        self._horse_name_zh_detail.setText(self._horses_table.item(row_idx, 3).text() if self._horses_table.item(row_idx, 3) else "")
        self._horse_status_detail.setText(self._horses_table.item(row_idx, 4).text() if self._horses_table.item(row_idx, 4) else "")

    def _on_fetch_clicked(self) -> None:
        if self._all_horses_checkbox.isChecked():
            selected_year = self._selected_race_year()
            year_text = selected_year if selected_year else "所有年份"
            self._set_background_status(f"抓取中…（所有馬匹 / {year_text}）")
            dlg_all = RaceFetchAllProgressDialog(
                self,
                self._config.db_path,
                horse_pp_template(
                    self._config.data_dir, self._config.hkjc_horse_url_template
                ),
                selected_year,
            )

            def on_ok_all(obj: object) -> None:
                stats = obj if isinstance(obj, RaceFetchAllStats) else None
                if stats is None:
                    QMessageBox.critical(self, "抓取失敗", "抓取回傳資料格式不正確。")
                    return
                year_text2 = stats.selected_year if stats.selected_year else "所有年份"
                self._set_background_status(
                    f"抓取完成：全部馬匹／{year_text2}"
                    f"｜成功 {stats.success_horses}｜失敗 {stats.failed_horses}"
                )
                self._on_search_clicked()

            dlg_all.finished_ok.connect(on_ok_all)
            dlg_all.start()
            return

        horse_id = self._horse_id()
        if not horse_id:
            QMessageBox.warning(self, "需要 HorseId", "請先輸入 HorseId。")
            return

        selected_year = self._selected_race_year()
        self._set_background_status(f"抓取中…（{horse_id}）")
        dlg = RaceFetchProgressDialog(
            self,
            self._config.db_path,
            horse_id,
            horse_pp_template(
                self._config.data_dir, self._config.hkjc_horse_url_template
            ),
            selected_year,
        )

        def on_ok(obj: object) -> None:
            stats = obj if isinstance(obj, RaceFetchStats) else None
            if stats is None:
                QMessageBox.critical(self, "抓取失敗", "抓取回傳資料格式不正確。")
                return
            year_text = f"（年份：{stats.selected_year}）" if stats.selected_year else "（年份：全部）"
            self._set_background_status(
                f"抓取完成：{stats.horse_id}{year_text}｜抓到 {stats.fetched}｜寫入 {stats.saved}"
            )
            if stats.selected_year and stats.saved == 0:
                valid_years = [
                    y for y in stats.available_years if re.fullmatch(r"(19|20)\d{2}", str(y))
                ]
                years_txt = "、".join(valid_years) if valid_years else "（無可用年份）"
                tip = (
                    f"{stats.horse_id} 在 {stats.selected_year} 年沒有往績。"
                    if stats.fetched > 0
                    else f"{stats.horse_id} 目前抓不到任何往績資料。"
                )
                QMessageBox.information(
                    self,
                    "年份無資料",
                    f"{tip}\n可用年份：{years_txt}",
                )
            self._load_into_table(stats.horse_id)

        dlg.finished_ok.connect(on_ok)
        dlg.start()

    def _on_load_clicked(self) -> None:
        horse_id = self._horse_id()
        if not horse_id:
            QMessageBox.warning(self, "需要 HorseId", "請先輸入 HorseId。")
            return
        self._load_into_table(horse_id)

    def _load_into_table(self, horse_id: str) -> None:
        """從資料庫載入往績至「往績」分頁 Grid（與抓取完成後刷新相同）。"""
        self._load_race_records_into_detail_table(horse_id)
        n = self._detail_race_table.rowCount()
        pass  # UI 提示不寫入狀態列
        self._update_display_count_label()


def main() -> int:
    """供 `python -m racing.ui.main_window` 直接啟動主視窗（與 `python -m racing` 相同）。"""
    app = QApplication(sys.argv)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    w = MainWindow(default_config())
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""賽馬結果檢視：可選賽日／場地／場次；下方顯示當天冠亞季統計。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QTabWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig
from ..race_results import (
    COURSE_CODE_TO_NAME,
    fetch_results_rows,
    list_results_race_nos,
    list_stored_results_meetings,
    summarize_top3_stats,
)
from ..web_utils import format_two_digit_99
from .theme import (
    configure_data_grid,
    make_table_item,
    resize_grid_columns_to_content,
    style_action_button,
)


class RaceResultsPanel(QWidget):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._selected: tuple[str, str] | None = None
        self._all_rows: list[dict[str, str]] = []
        self._loading = False

        self._meeting_combo = QComboBox()
        self._meeting_combo.setMinimumWidth(280)
        self._meeting_combo.setToolTip("選擇資料庫內已有的賽事日期／場地，即時顯示該日賽果")
        self._meeting_combo.currentIndexChanged.connect(self._on_meeting_changed)

        self._race_combo = QComboBox()
        self._race_combo.setMinimumWidth(120)
        self._race_combo.setToolTip("選擇當天場次；全部則顯示全日")
        self._race_combo.currentIndexChanged.connect(self._on_race_changed)

        reload_btn = QPushButton("重新整理")
        style_action_button(reload_btn, QStyle.StandardPixmap.SP_BrowserReload)
        reload_btn.setToolTip("重新載入目前選取賽日的賽果（不重讀賽日清單）")
        reload_btn.clicked.connect(self.reload)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("賽事日期："))
        date_row.addWidget(self._meeting_combo, 1)
        date_row.addWidget(QLabel("場次："))
        date_row.addWidget(self._race_combo)
        date_row.addWidget(reload_btn)

        self._table = QTableWidget(0, 17)
        self._table.setHorizontalHeaderLabels(
            [
                "場次",
                "名次",
                "馬號",
                "馬名",
                "烙號",
                "班/級別",
                "評分",
                "騎師",
                "練馬師",
                "負磅",
                "檔位",
                "走位",
                "段速",
                "完成時間",
                "獨贏",
                "位置賠率",
                "途程",
            ]
        )
        # 與下方統計各佔約 50%：不鎖死可視列高，由 layout stretch 均分
        configure_data_grid(self._table, fixed_viewport=False, visible_rows=8)

        self._trainer_stats = self._make_stats_table("練馬師")
        self._jockey_stats = self._make_stats_table("騎師")
        self._draw_stats = self._make_stats_table("檔位")
        self._handicap_stats = self._make_stats_table("負磅")
        self._odds_stats = self._make_stats_table("賠率")

        tab_people = QWidget()
        people_row = QHBoxLayout(tab_people)
        people_row.setContentsMargins(0, 4, 0, 0)
        people_row.setSpacing(12)
        people_row.addLayout(
            self._wrap_stats("練馬師（1–3名）", self._trainer_stats), 1
        )
        people_row.addLayout(
            self._wrap_stats("騎師（1–3名）", self._jockey_stats), 1
        )
        people_row.addLayout(
            self._wrap_stats("檔位（1–3名）", self._draw_stats), 1
        )

        tab_bands = QWidget()
        bands_row = QHBoxLayout(tab_bands)
        bands_row.setContentsMargins(0, 4, 0, 0)
        bands_row.setSpacing(12)
        bands_row.addLayout(
            self._wrap_stats("負磅（1–3名）", self._handicap_stats), 1
        )
        bands_row.addLayout(self._wrap_stats("賠率（1–3名）", self._odds_stats), 1)

        self._stats_tabs = QTabWidget()
        self._stats_tabs.addTab(tab_people, "練馬師／騎師／檔位")
        self._stats_tabs.addTab(tab_bands, "負磅／賠率")
        self._stats_tabs.currentChanged.connect(self._on_stats_tab_changed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addLayout(date_row)
        lay.addWidget(self._table, 1)
        lay.addWidget(self._stats_tabs, 1)

        self._load_meeting_list()
        self._load_selected()

    def _on_stats_tab_changed(self, index: int) -> None:
        """切換統計分頁時重算欄寬（隱藏分頁初次顯示時寬度常為 0）。"""
        tables = (
            (self._trainer_stats, self._jockey_stats, self._draw_stats)
            if index == 0
            else (self._handicap_stats, self._odds_stats)
        )
        for table in tables:
            resize_grid_columns_to_content(table)

    def _make_stats_table(self, name_header: str) -> QTableWidget:
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels([name_header, "冠", "亞", "季"])
        configure_data_grid(table, fixed_viewport=False, visible_rows=6)
        return table

    def _wrap_stats(self, title: str, table: QTableWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        label = QLabel(title)
        label.setStyleSheet("font-weight: 700;")
        box.addWidget(label)
        box.addWidget(table, 1)
        return box

    def _meeting_label(self, race_date: str, venue: str) -> str:
        disp = COURSE_CODE_TO_NAME.get(venue, venue)
        return f"{race_date}  {disp}（{venue}）"

    def _load_meeting_list(self) -> None:
        keep = self._selected
        self._meeting_combo.blockSignals(True)
        self._meeting_combo.clear()
        for rd, venue in list_stored_results_meetings(self._config.db_path):
            self._meeting_combo.addItem(self._meeting_label(rd, venue), (rd, venue))
        target_idx = 0
        if keep is not None:
            for i in range(self._meeting_combo.count()):
                if self._meeting_combo.itemData(i) == keep:
                    target_idx = i
                    break
        if self._meeting_combo.count() > 0:
            self._meeting_combo.setCurrentIndex(target_idx)
            data = self._meeting_combo.currentData()
            self._selected = data if isinstance(data, tuple) else None
        else:
            self._selected = None
        self._meeting_combo.blockSignals(False)

    def reload(self) -> None:
        self._load_selected()

    def reload_stored(self) -> None:
        self._selected = None
        self._load_meeting_list()
        self._load_selected()
        if self._table.columnCount() > 0:
            resize_grid_columns_to_content(self._table)

    def _on_meeting_changed(self, index: int) -> None:
        data = self._meeting_combo.itemData(index)
        self._selected = data if isinstance(data, tuple) else None
        self._load_selected(reset_race=True)

    def _on_race_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._fill_results_table()

    def _load_selected(self, *, reset_race: bool = False) -> None:
        self._all_rows = []
        if not self._selected:
            self._fill_race_combo([])
            self._fill_results_table()
            self._fill_stats({})
            return
        race_date, venue = self._selected
        self._all_rows = fetch_results_rows(self._config.db_path, race_date, venue)
        keep_race = None if reset_race else self._race_combo.currentData()
        self._fill_race_combo(list_results_race_nos(self._all_rows), keep=keep_race)
        self._fill_results_table()
        self._fill_stats(summarize_top3_stats(self._all_rows))

    def _fill_race_combo(
        self, race_nos: list[str], *, keep: object | None = None
    ) -> None:
        self._loading = True
        self._race_combo.blockSignals(True)
        self._race_combo.clear()
        self._race_combo.addItem("全部場次", "all")
        for rn in race_nos:
            self._race_combo.addItem(f"第{int(rn)}場", rn)
        chosen = 0
        if keep and keep != "all":
            want = format_two_digit_99(keep)
            for i in range(self._race_combo.count()):
                if format_two_digit_99(self._race_combo.itemData(i)) == want:
                    chosen = i
                    break
        self._race_combo.setCurrentIndex(chosen)
        self._race_combo.blockSignals(False)
        self._loading = False

    def _filtered_rows(self) -> list[dict[str, str]]:
        race = self._race_combo.currentData()
        if not race or race == "all":
            return list(self._all_rows)
        want = format_two_digit_99(race)
        return [
            r
            for r in self._all_rows
            if format_two_digit_99(r.get("race_no")) == want
        ]

    def _fill_results_table(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        rows = self._filtered_rows()
        for row in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            values = [
                row["race_no"],
                row["finish_pos"],
                row.get("horse_no", ""),
                row["name_zh"],
                row["brand_no"],
                row.get("class_band", ""),
                row.get("rating", ""),
                row["jockey"],
                row["trainer"],
                row.get("handicap", ""),
                row["draw"],
                row.get("running_pos", ""),
                row.get("sectional_time", ""),
                row["time"],
                row["win_odds"],
                row.get("place_odds", ""),
                row["distance"],
            ]
            numeric = {0, 1, 2, 6, 9, 10, 14, 15, 16}
            for col, val in enumerate(values):
                self._table.setItem(r, col, make_table_item(val, numeric=col in numeric))
        resize_grid_columns_to_content(self._table)
        self._table.setSortingEnabled(False)

    def _fill_stats(self, stats: dict) -> None:
        self._fill_one_stats(self._trainer_stats, stats.get("trainers") or [])
        self._fill_one_stats(self._jockey_stats, stats.get("jockeys") or [])
        self._fill_one_stats(self._draw_stats, stats.get("draws") or [])
        self._fill_one_stats(self._handicap_stats, stats.get("handicaps") or [])
        self._fill_one_stats(self._odds_stats, stats.get("odds") or [])

    def _fill_one_stats(self, table: QTableWidget, rows: list[dict]) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            values = [
                row.get("name", ""),
                row.get("wins", ""),
                row.get("seconds", ""),
                row.get("thirds", ""),
            ]
            for col, val in enumerate(values):
                table.setItem(
                    r, col, make_table_item(val, numeric=col in {1, 2, 3})
                )
        resize_grid_columns_to_content(table)
        table.setSortingEnabled(False)

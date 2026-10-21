"""賽馬結果檢視：可選擇不同賽日／場地顯示已入庫賽果。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig
from ..race_results import (
    COURSE_CODE_TO_NAME,
    fetch_results_rows,
    list_stored_results_meetings,
)
from .theme import configure_data_grid, make_table_item, resize_grid_columns_to_content, style_action_button


class RaceResultsPanel(QWidget):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._selected: tuple[str, str] | None = None

        self._meeting_combo = QComboBox()
        self._meeting_combo.setMinimumWidth(280)
        self._meeting_combo.setToolTip("選擇資料庫內已有的賽事日期／場地，即時顯示該日賽果")
        self._meeting_combo.currentIndexChanged.connect(self._on_meeting_changed)

        reload_btn = QPushButton("重新整理")
        style_action_button(reload_btn, QStyle.StandardPixmap.SP_BrowserReload)
        reload_btn.setToolTip("重新載入目前選取賽日的賽果（不重讀賽日清單）")
        reload_btn.clicked.connect(self.reload)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("賽事日期："))
        date_row.addWidget(self._meeting_combo, 1)
        date_row.addWidget(reload_btn)

        self._table = QTableWidget(0, 16)
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
                "完成時間",
                "獨贏",
                "位置賠率",
                "途程",
            ]
        )
        configure_data_grid(self._table, fixed_viewport=False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addLayout(date_row)
        lay.addWidget(self._table, 1)

        # 賽日清單只在首次建立時讀取一次
        self._load_meeting_list()
        self._load_selected()

    def _meeting_label(self, race_date: str, venue: str) -> str:
        disp = COURSE_CODE_TO_NAME.get(venue, venue)
        return f"{race_date}  {disp}（{venue}）"

    def _load_meeting_list(self) -> None:
        """讀取賽日清單（僅初始化或明確需要時呼叫）。"""
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
        """重新載入目前選取賽日的賽果；不重讀賽日清單。"""
        self._load_selected()

    def _on_meeting_changed(self, index: int) -> None:
        data = self._meeting_combo.itemData(index)
        self._selected = data if isinstance(data, tuple) else None
        self._load_selected()

    def _load_selected(self) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        if not self._selected:
            return
        race_date, venue = self._selected
        rows = fetch_results_rows(self._config.db_path, race_date, venue)
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
                row["time"],
                row["win_odds"],
                row.get("place_odds", ""),
                row["distance"],
            ]
            numeric = {0, 1, 2, 6, 9, 10, 13, 14, 15}
            for col, val in enumerate(values):
                self._table.setItem(r, col, make_table_item(val, numeric=col in numeric))
        resize_grid_columns_to_content(self._table)
        # 預設已按場次由大至小；關閉即時排序以免點表頭打亂
        self._table.setSortingEnabled(False)

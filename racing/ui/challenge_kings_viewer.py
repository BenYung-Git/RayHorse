"""騎師王／練馬師王檢視：賽日積分與今季總分分兩個 Grid。"""

from __future__ import annotations

from PySide6.QtCore import Qt
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

from ..challenge_kings import (
    TYPE_JOCKEY,
    TYPE_TRAINER,
    ChallengeType,
    fetch_challenge_standings,
    fetch_season_standings,
    list_stored_challenge_dates,
)
from ..config import AppConfig
from .theme import configure_data_grid, make_table_item, resize_grid_columns_to_content, style_action_button

_DAY_COLUMNS = ("編號", "姓名", "賽日積分", "冠", "亞", "季", "殿", "備註")
_SEASON_COLUMNS = ("排名", "姓名", "今季總分", "冠", "亞", "季", "殿", "賽日數")
_VISIBLE_ROWS = 6


class ChallengeKingsPanel(QWidget):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._selected_date: str | None = None

        self._date_combo = QComboBox()
        self._date_combo.setMinimumWidth(220)
        self._date_combo.setToolTip("選擇資料庫內已有的賽事日期（今季總分截至該日）")
        self._date_combo.currentIndexChanged.connect(self._on_date_changed)

        reload_btn = QPushButton("重新整理")
        style_action_button(reload_btn, QStyle.StandardPixmap.SP_BrowserReload)
        reload_btn.setToolTip("重新載入目前選取賽日的積分榜（不重讀賽日清單）")
        reload_btn.clicked.connect(self.reload)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("賽事日期："))
        date_row.addWidget(self._date_combo, 1)
        date_row.addWidget(reload_btn)

        self._trainer_day = self._make_table(_DAY_COLUMNS)
        self._jockey_day = self._make_table(_DAY_COLUMNS)
        self._trainer_season = self._make_table(_SEASON_COLUMNS)
        self._jockey_season = self._make_table(_SEASON_COLUMNS)

        day_row = QHBoxLayout()
        day_row.setSpacing(10)
        day_row.addWidget(self._wrap_pane("練馬師王（賽日）", self._trainer_day), 1)
        day_row.addWidget(self._wrap_pane("騎師王（賽日）", self._jockey_day), 1)

        season_row = QHBoxLayout()
        season_row.setSpacing(10)
        season_row.addWidget(self._wrap_pane("練馬師王（今季總分）", self._trainer_season), 1)
        season_row.addWidget(self._wrap_pane("騎師王（今季總分）", self._jockey_season), 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addLayout(date_row)
        lay.addLayout(day_row, 0)
        lay.addLayout(season_row, 0)
        lay.addStretch(1)

        self._load_date_list()
        self._load_selected()

    def _wrap_pane(self, title: str, table: QTableWidget) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        label = QLabel(title)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-weight: 700; padding: 2px 0;")
        col.addWidget(label)
        col.addWidget(table)
        return box

    def _make_table(self, columns: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        configure_data_grid(table, fixed_viewport=True, visible_rows=_VISIBLE_ROWS)
        return table

    def _load_date_list(self) -> None:
        keep = self._selected_date
        self._date_combo.blockSignals(True)
        self._date_combo.clear()
        for rd in list_stored_challenge_dates(self._config.db_path):
            self._date_combo.addItem(rd, rd)
        target_idx = 0
        if keep:
            for i in range(self._date_combo.count()):
                if self._date_combo.itemData(i) == keep:
                    target_idx = i
                    break
        if self._date_combo.count() > 0:
            self._date_combo.setCurrentIndex(target_idx)
            data = self._date_combo.currentData()
            self._selected_date = str(data) if data else None
        else:
            self._selected_date = None
        self._date_combo.blockSignals(False)

    def reload(self) -> None:
        self._load_selected()

    def reload_stored(self) -> None:
        self._load_date_list()
        self._load_selected()

    def _on_date_changed(self, index: int) -> None:
        data = self._date_combo.itemData(index)
        self._selected_date = str(data) if data else None
        self._load_selected()

    def _load_selected(self) -> None:
        self._fill_day_table(self._trainer_day, TYPE_TRAINER)
        self._fill_day_table(self._jockey_day, TYPE_JOCKEY)
        self._fill_season_table(self._trainer_season, TYPE_TRAINER)
        self._fill_season_table(self._jockey_season, TYPE_JOCKEY)

    def _fill_day_table(self, table: QTableWidget, challenge_type: ChallengeType) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)
        if not self._selected_date:
            return
        rows = fetch_challenge_standings(
            self._config.db_path, challenge_type, self._selected_date
        )
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            name = str(row.get("person_name") or "")
            if int(row.get("is_other") or 0):
                name = f"（其他）{name}" if not name.startswith("其他") else name
            values = [
                row.get("entry_no") or "",
                name,
                row.get("day_points") or "",
                row.get("wins") or "",
                row.get("seconds") or "",
                row.get("thirds") or "",
                row.get("fourths") or "",
                row.get("remark") or "",
            ]
            for c, val in enumerate(values):
                table.setItem(r, c, make_table_item(str(val)))
        resize_grid_columns_to_content(table)

    def _fill_season_table(self, table: QTableWidget, challenge_type: ChallengeType) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)
        if not self._selected_date:
            return
        rows = fetch_season_standings(
            self._config.db_path, challenge_type, self._selected_date
        )
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            values = [
                row.get("rank") or "",
                row.get("person_name") or "",
                row.get("season_points") or "",
                row.get("wins") or "",
                row.get("seconds") or "",
                row.get("thirds") or "",
                row.get("fourths") or "",
                row.get("meeting_count") or "",
            ]
            for c, val in enumerate(values):
                table.setItem(r, c, make_table_item(str(val)))
        resize_grid_columns_to_content(table)

"""足球賠率／賽果檢視：香港／英超同一組欄位。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig
from ..football import (
    FOOTBALL_SCOPE_EPL,
    FOOTBALL_SCOPE_HK,
    fetch_football_display_rows,
    list_football_dates,
    normalize_football_scope,
)
from .theme import (
    char_width_px,
    configure_data_grid,
    make_table_item,
    style_action_button,
)

_DISPLAY_COLUMNS = (
    "聯賽",
    "日期",
    "時間",
    "主場隊",
    "客場隊",
    "是否中立場地",
    "入球比率",
    "半場比率",
    "主賠率",
    "和賠率",
    "客賠率",
)
_FOOTBALL_COL_MAX_CHARS = 36
_FOOTBALL_COL_MIN_CHARS = 6
_DISPLAY_DEFAULT_CHARS = (12, 10, 6, 12, 12, 8, 8, 8, 7, 7, 7)


class FootballPanel(QWidget):
    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        scope: str = FOOTBALL_SCOPE_HK,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._scope = normalize_football_scope(scope)
        self._selected_date: str | None = None
        self._loading = False

        self._date_combo = QComboBox()
        self._date_combo.setMinimumWidth(220)
        tip = (
            "選擇資料庫內已有的英超賽日"
            if self._scope == FOOTBALL_SCOPE_EPL
            else "選擇資料庫內已有的香港足球賽日"
        )
        self._date_combo.setToolTip(tip)
        self._date_combo.currentIndexChanged.connect(self._on_date_changed)

        reload_btn = QPushButton("重新整理")
        style_action_button(reload_btn, QStyle.StandardPixmap.SP_BrowserReload)
        reload_btn.setToolTip("重新載入目前選取賽日（不重讀賽日清單）")
        reload_btn.clicked.connect(self.reload)

        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("賽事日期："))
        date_row.addWidget(self._date_combo, 1)
        date_row.addWidget(reload_btn)

        self._table = self._make_table(
            _DISPLAY_COLUMNS, visible_rows=15, fixed_viewport=True
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addLayout(date_row)
        lay.addWidget(self._table, 1)

        self._apply_default_column_widths()
        self._load_date_list()
        self._load_selected()

    def _make_table(
        self,
        columns: tuple[str, ...],
        *,
        visible_rows: int = 12,
        fixed_viewport: bool = False,
    ) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        configure_data_grid(
            table, fixed_viewport=fixed_viewport, visible_rows=visible_rows
        )
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        header.setStretchLastSection(False)
        return table

    def _apply_default_column_widths(self) -> None:
        cw = char_width_px(self._table)
        for col, n in enumerate(_DISPLAY_DEFAULT_CHARS):
            if col >= self._table.columnCount():
                break
            self._table.setColumnWidth(col, max(48, n * cw + 16))

    def _resize_football_columns(self, table: QTableWidget) -> None:
        if table.columnCount() <= 0:
            return
        cw = char_width_px(table)
        min_w = _FOOTBALL_COL_MIN_CHARS * cw + 12
        max_w = _FOOTBALL_COL_MAX_CHARS * cw + 16
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.resizeColumnsToContents()
        for col in range(table.columnCount()):
            w = table.columnWidth(col)
            table.setColumnWidth(col, max(min_w, min(max_w, w)))

    def _load_date_list(self) -> None:
        keep = self._selected_date
        self._date_combo.blockSignals(True)
        self._date_combo.clear()
        dates = list_football_dates(self._config.db_path, scope=self._scope)
        for d in dates:
            self._date_combo.addItem(d, d)
        if keep:
            idx = self._date_combo.findData(keep)
            if idx >= 0:
                self._date_combo.setCurrentIndex(idx)
            elif dates:
                self._selected_date = dates[0]
                self._date_combo.setCurrentIndex(0)
            else:
                self._selected_date = None
        elif dates:
            self._selected_date = dates[0]
            self._date_combo.setCurrentIndex(0)
        else:
            self._selected_date = None
        self._date_combo.blockSignals(False)

    def _on_date_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        data = self._date_combo.currentData()
        self._selected_date = str(data) if data else None
        self._load_selected()

    def reload(self) -> None:
        self._load_selected()

    def reload_stored(self) -> None:
        self._load_date_list()
        self._load_selected()

    def _fill_rows(self, rows: list[dict[str, str]]) -> None:
        table = self._table
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row.get("league", ""),
                row.get("match_date", ""),
                row.get("kick_off_time", ""),
                row.get("home_team", ""),
                row.get("away_team", ""),
                row.get("neutral_venue", ""),
                row.get("ft_score", ""),
                row.get("ht_score", ""),
                row.get("odds_home", ""),
                row.get("odds_draw", ""),
                row.get("odds_away", ""),
            ]
            for c, text in enumerate(values):
                item = make_table_item(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.get("match_id", ""))
                table.setItem(r, c, item)
        self._resize_football_columns(table)

    def _load_selected(self) -> None:
        self._loading = True
        try:
            date = self._selected_date or ""
            if not date:
                self._table.setRowCount(0)
                return
            rows = fetch_football_display_rows(
                self._config.db_path, date, scope=self._scope
            )
            self._fill_rows(rows)
        finally:
            self._loading = False

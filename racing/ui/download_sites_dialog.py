"""資料來源網站設定：以 Grid 顯示／編輯下載來源。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..download_sources import (
    KIND_HINTS,
    KIND_HORSE_PP,
    KIND_LABELS,
    DownloadSource,
    default_download_sources,
    load_download_sources,
    save_download_sources,
)
from .theme import (
    GRID_ROW_HEIGHT,
    configure_data_grid,
    make_table_item,
    resize_grid_columns_to_content,
    style_action_button,
)

_COLUMNS = ("啟用", "類別", "名稱", "網址")


class DownloadSitesDialog(QDialog):
    """獨立網站設定視窗：資料來源以 Grid 管理。"""

    def __init__(self, parent: QWidget | None, data_dir: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("網站設定")
        # 獨立可縮放視窗（含最大化）；勿把表格最小高度鎖成全部列，否則無法縮小
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setSizeGripEnabled(True)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._data_dir = Path(data_dir)
        self._sources = load_download_sources(self._data_dir)
        self._loading = False
        self._fitted_once = False

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        configure_data_grid(
            self._table,
            fixed_viewport=False,
            multi_select=False,
            visible_rows=8,
        )
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.setTextElideMode(Qt.TextElideMode.ElideNone)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(40)
        hdr.setSectionsClickable(True)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        self._kind_combo = QComboBox()
        for kind, label in KIND_LABELS.items():
            self._kind_combo.addItem(label, kind)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("名稱（可選）")
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://…（往績類別請含 {horse_id}）")
        self._kind_hint = QLabel("")
        self._kind_hint.setWordWrap(True)
        self._kind_combo.currentIndexChanged.connect(self._sync_kind_hint)
        self._sync_kind_hint()

        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("類別"))
        edit_row.addWidget(self._kind_combo, 1)
        edit_row.addWidget(QLabel("名稱"))
        edit_row.addWidget(self._name_edit, 2)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("新增網站")
        style_action_button(add_btn, QStyle.StandardPixmap.SP_DialogApplyButton)
        add_btn.clicked.connect(self._add_site)
        remove_btn = QPushButton("刪除選取")
        style_action_button(remove_btn, QStyle.StandardPixmap.SP_DialogCancelButton)
        remove_btn.clicked.connect(self._remove_site)
        apply_btn = QPushButton("套用到選取列")
        style_action_button(apply_btn, QStyle.StandardPixmap.SP_DialogSaveButton)
        apply_btn.setToolTip("將上方類別／名稱／網址寫入目前選取的列")
        apply_btn.clicked.connect(self._apply_to_selected)
        reset_btn = QPushButton("還原預設")
        style_action_button(reset_btn, QStyle.StandardPixmap.SP_BrowserReload)
        reset_btn.clicked.connect(self._reset_sites)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.accept)

        note = QLabel(
            "在 Grid 勾選「啟用」即可開關來源；雙擊名稱／網址可直接編輯。"
            "同一類別至少保留一個啟用中的網站。"
        )
        note.setWordWrap(True)

        root = QVBoxLayout(self)
        root.addWidget(note)
        root.addWidget(self._table, 1)
        root.addLayout(edit_row)
        root.addWidget(self._url_edit)
        root.addWidget(self._kind_hint)
        root.addLayout(btn_row)
        root.addWidget(buttons)

        self._reload_grid()
        self._fit_window_to_content(initial=True)

    def _apply_column_widths(self) -> None:
        resize_grid_columns_to_content(self._table)
        hdr = self._table.horizontalHeader()
        # 保持可拖曳調整欄寬（勿改成 Fixed / Stretch）
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        fm = self._table.fontMetrics()
        pad = 28

        def text_w(*texts: str) -> int:
            return max((fm.horizontalAdvance(t or "") for t in texts), default=0) + pad

        self._table.setColumnWidth(0, max(48, min(56, self._table.columnWidth(0))))
        kind_labels = [KIND_LABELS.get(s.kind, s.kind) for s in self._sources] or ["類別"]
        self._table.setColumnWidth(1, min(max(text_w("類別", *kind_labels), 88), 200))
        # 名稱／網址依實際內容加寬，預設就要看得到完整文字
        names = [s.name for s in self._sources] or ["名稱"]
        urls = [s.url for s in self._sources] or ["網址"]
        self._table.setColumnWidth(2, min(max(text_w("名稱", *names), 140), 560))
        self._table.setColumnWidth(3, min(max(text_w("網址", *urls), 560), 1600))

    def _preferred_table_height(self, rows: int) -> int:
        header = self._table.horizontalHeader()
        header_h = max(header.sizeHint().height(), 36)
        frame_h = max(2, self._table.frameWidth() * 2)
        return GRID_ROW_HEIGHT * max(rows, 1) + header_h + frame_h + 12

    def _size_table_for_rows(self) -> None:
        # 最小高度只保留約 8 列，視窗才可縮小；全部資料靠初始視窗高度／捲動顯示
        self._table.setMinimumHeight(self._preferred_table_height(8))
        self._table.setMaximumHeight(16777215)

    def _chrome_height(self) -> tuple[int, int]:
        root = self.layout()
        chrome_h = 0
        side = 24
        if root is None:
            return 220, side
        m = root.contentsMargins()
        chrome_h += m.top() + m.bottom()
        side = m.left() + m.right()
        for i in range(root.count()):
            item = root.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is self._table:
                continue
            if w is not None:
                chrome_h += max(w.sizeHint().height(), w.minimumHeight()) + root.spacing()
            elif item.layout() is not None:
                chrome_h += item.layout().sizeHint().height() + root.spacing()
        return chrome_h, side

    def _fit_window_to_content(self, *, initial: bool = False) -> None:
        """預設視窗夠顯示全部列與網址；之後仍可手動調整大小。"""
        self._apply_column_widths()
        self._size_table_for_rows()
        if self.layout() is not None:
            self.layout().activate()

        cols_w = sum(
            self._table.columnWidth(c) for c in range(self._table.columnCount())
        )
        frame = max(2, self._table.frameWidth() * 2)
        table_w = cols_w + frame + 28
        table_h = self._preferred_table_height(max(len(self._sources), 1))
        chrome_h, side = self._chrome_height()

        want_w = table_w + side
        want_h = table_h + chrome_h + 8

        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            ag = screen.availableGeometry()
            want_w = min(want_w, max(640, ag.width() - 48))
            want_h = min(want_h, max(360, ag.height() - 48))
            if initial:
                self.setGeometry(
                    ag.x() + max(0, (ag.width() - want_w) // 2),
                    ag.y() + max(0, (ag.height() - want_h) // 2),
                    want_w,
                    want_h,
                )
                self._fitted_once = True
                return
        self.resize(want_w, want_h)
        self._fitted_once = True

    def _sync_kind_hint(self) -> None:
        kind = str(self._kind_combo.currentData() or "")
        self._kind_hint.setText(KIND_HINTS.get(kind, ""))

    def _persist(self) -> None:
        save_download_sources(self._data_dir, self._sources)

    def _reload_grid(self) -> None:
        self._loading = True
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        for src in self._sources:
            r = self._table.rowCount()
            self._table.insertRow(r)

            chk = QTableWidgetItem("")
            chk.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            chk.setCheckState(
                Qt.CheckState.Checked if src.enabled else Qt.CheckState.Unchecked
            )
            self._table.setItem(r, 0, chk)

            kind_item = make_table_item(KIND_LABELS.get(src.kind, src.kind))
            kind_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            kind_item.setData(Qt.ItemDataRole.UserRole, src.kind)
            self._table.setItem(r, 1, kind_item)

            # 勿用 make_table_item：它會清掉 ItemIsEditable
            name_item = QTableWidgetItem(src.name)
            name_item.setFlags(
                Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            name_item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self._table.setItem(r, 2, name_item)

            url_item = QTableWidgetItem(src.url)
            url_item.setFlags(
                Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            url_item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self._table.setItem(r, 3, url_item)

        self._apply_column_widths()
        self._size_table_for_rows()
        self._loading = False

    def _selected_row(self) -> int:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if rows:
            return int(rows[0].row())
        return self._table.currentRow()

    def _sync_form_row(self, row: int) -> None:
        if row < 0 or row >= len(self._sources):
            return
        src = self._sources[row]
        idx = self._kind_combo.findData(src.kind)
        if idx >= 0:
            self._kind_combo.setCurrentIndex(idx)
        self._name_edit.setText(src.name)
        self._url_edit.setText(src.url)

    def _on_selection_changed(self) -> None:
        self._sync_form_row(self._selected_row())

    def done(self, result: int) -> None:  # noqa: N802
        # 關閉前結束儲存格編輯，確保最後一次修改寫入
        index = self._table.currentIndex()
        editor = self._table.indexWidget(index) if index.isValid() else None
        if editor is not None:
            self._table.commitData(editor)
            self._table.closeEditor(
                editor, QAbstractItemDelegate.EndEditHint.SubmitModelCache
            )
        current = self._table.currentItem()
        if current is not None:
            self._table.closePersistentEditor(current)
        super().done(result)

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        if col in {2, 3}:
            self._table.editItem(self._table.item(row, col))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item is None:
            return
        row = item.row()
        col = item.column()
        if row < 0 or row >= len(self._sources):
            return
        src = self._sources[row]
        if col == 0:
            want_enabled = item.checkState() == Qt.CheckState.Checked
            if src.enabled and not want_enabled:
                enabled_same = [s for s in self._sources if s.kind == src.kind and s.enabled]
                if len(enabled_same) <= 1:
                    self._loading = True
                    item.setCheckState(Qt.CheckState.Checked)
                    self._loading = False
                    QMessageBox.information(
                        self,
                        "網站設定",
                        f"「{KIND_LABELS.get(src.kind, src.kind)}」至少保留一個啟用中的網站。",
                    )
                    return
            if src.enabled == want_enabled:
                return
            self._sources[row] = DownloadSource(
                kind=src.kind, name=src.name, url=src.url, enabled=want_enabled
            )
            self._persist()
            return
        if col == 2:
            name = item.text().strip() or KIND_LABELS.get(src.kind, src.kind)
            if name == src.name:
                return
            self._sources[row] = DownloadSource(
                kind=src.kind, name=name, url=src.url, enabled=src.enabled
            )
            self._persist()
            self._sync_form_row(row)
            return
        if col == 3:
            url = item.text().strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                self._loading = True
                item.setText(src.url)
                self._loading = False
                QMessageBox.information(self, "網站設定", "網址請以 http:// 或 https:// 開頭。")
                return
            if src.kind == KIND_HORSE_PP and "{horse_id}" not in url:
                self._loading = True
                item.setText(src.url)
                self._loading = False
                QMessageBox.information(self, "網站設定", "往績網站模板必須包含 {horse_id}。")
                return
            for i, other in enumerate(self._sources):
                if i != row and other.kind == src.kind and other.url == url:
                    self._loading = True
                    item.setText(src.url)
                    self._loading = False
                    QMessageBox.information(self, "網站設定", "此網站已存在於清單中。")
                    return
            if url == src.url:
                return
            self._sources[row] = DownloadSource(
                kind=src.kind, name=src.name, url=url, enabled=src.enabled
            )
            self._persist()
            self._sync_form_row(row)

    def _validate_new(self, kind: str, name: str, url: str) -> str | None:
        if not url:
            return "請輸入網站網址。"
        if not (url.startswith("http://") or url.startswith("https://")):
            return "網址請以 http:// 或 https:// 開頭。"
        if kind == KIND_HORSE_PP and "{horse_id}" not in url:
            return "往績網站模板必須包含 {horse_id}。"
        if any(s.kind == kind and s.url == url for s in self._sources):
            return "此網站已存在於清單中。"
        return None

    def _add_site(self) -> None:
        kind = str(self._kind_combo.currentData() or "")
        url = self._url_edit.text().strip()
        name = self._name_edit.text().strip() or KIND_LABELS.get(kind, kind)
        err = self._validate_new(kind, name, url)
        if err:
            QMessageBox.information(self, "網站設定", err)
            return
        self._sources.append(DownloadSource(kind=kind, name=name, url=url, enabled=True))
        self._persist()
        self._url_edit.clear()
        self._name_edit.clear()
        self._reload_grid()
        self._table.selectRow(len(self._sources) - 1)

    def _apply_to_selected(self) -> None:
        row = self._selected_row()
        if row < 0 or row >= len(self._sources):
            QMessageBox.information(self, "網站設定", "請先選取要修改的列。")
            return
        kind = str(self._kind_combo.currentData() or "")
        url = self._url_edit.text().strip()
        name = self._name_edit.text().strip() or KIND_LABELS.get(kind, kind)
        if not url:
            QMessageBox.information(self, "網站設定", "請輸入網站網址。")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.information(self, "網站設定", "網址請以 http:// 或 https:// 開頭。")
            return
        if kind == KIND_HORSE_PP and "{horse_id}" not in url:
            QMessageBox.information(self, "網站設定", "往績網站模板必須包含 {horse_id}。")
            return
        for i, other in enumerate(self._sources):
            if i != row and other.kind == kind and other.url == url:
                QMessageBox.information(self, "網站設定", "此網站已存在於清單中。")
                return
        old = self._sources[row]
        # 若改類別導致原類別沒有啟用來源，擋下
        if old.kind != kind and old.enabled:
            enabled_old = [s for s in self._sources if s.kind == old.kind and s.enabled]
            if len(enabled_old) <= 1:
                QMessageBox.information(
                    self,
                    "網站設定",
                    f"「{KIND_LABELS.get(old.kind, old.kind)}」至少保留一個啟用中的網站。",
                )
                return
        self._sources[row] = DownloadSource(
            kind=kind, name=name, url=url, enabled=old.enabled
        )
        self._persist()
        self._reload_grid()
        self._table.selectRow(row)

    def _remove_site(self) -> None:
        row = self._selected_row()
        if row < 0 or row >= len(self._sources):
            QMessageBox.information(self, "網站設定", "請先選取要刪除的網站。")
            return
        src = self._sources[row]
        same_kind = [s for s in self._sources if s.kind == src.kind]
        if len(same_kind) <= 1:
            QMessageBox.information(
                self,
                "網站設定",
                f"「{KIND_LABELS.get(src.kind, src.kind)}」至少保留一個網站。",
            )
            return
        del self._sources[row]
        self._persist()
        self._reload_grid()

    def _reset_sites(self) -> None:
        reply = QMessageBox.question(
            self,
            "網站設定",
            "還原為預設網站清單？目前自訂項目會被覆蓋。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._sources = default_download_sources()
        self._persist()
        self._reload_grid()

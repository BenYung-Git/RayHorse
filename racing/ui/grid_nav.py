"""Windows Grid 鍵盤導覽：Enter → 下一欄；方向鍵上下左右（含編輯中）。"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStyledItemDelegate,
    QTableWidget,
    QWidget,
)


def _move(table: QTableWidget, d_row: int, d_col: int) -> None:
    row = table.currentRow()
    col = table.currentColumn()
    rows = table.rowCount()
    cols = table.columnCount()
    if rows <= 0 or cols <= 0:
        return
    nr = max(0, min(rows - 1, row + d_row))
    nc = max(0, min(cols - 1, col + d_col))
    table.setCurrentCell(nr, nc)


def _move_enter(table: QTableWidget) -> None:
    row = table.currentRow()
    col = table.currentColumn()
    rows = table.rowCount()
    cols = table.columnCount()
    if rows <= 0 or cols <= 0:
        return
    if col + 1 < cols:
        table.setCurrentCell(row, col + 1)
    elif row + 1 < rows:
        table.setCurrentCell(row + 1, 0)


class _NavDelegate(QStyledItemDelegate):
    """編輯器內 Enter／方向鍵：結束編輯並移動儲存格。"""

    def __init__(self, table: QTableWidget, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._table = table

    def createEditor(
        self, parent: QWidget, option, index: QModelIndex  # noqa: ANN001
    ) -> QWidget:
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            editor.installEventFilter(self)
        return editor

    def eventFilter(self, obj: QObject, event) -> bool:  # noqa: ANN001, N802
        from PySide6.QtCore import QEvent

        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        assert isinstance(event, QKeyEvent)
        key = event.key()
        table = self._table
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commitData.emit(obj)  # type: ignore[arg-type]
            self.closeEditor.emit(obj, QStyledItemDelegate.EndEditHint.NoHint)  # type: ignore[arg-type]
            _move_enter(table)
            return True
        if key in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            self.commitData.emit(obj)  # type: ignore[arg-type]
            self.closeEditor.emit(obj, QStyledItemDelegate.EndEditHint.NoHint)  # type: ignore[arg-type]
            if key == Qt.Key.Key_Left:
                _move(table, 0, -1)
            elif key == Qt.Key.Key_Right:
                _move(table, 0, 1)
            elif key == Qt.Key.Key_Up:
                _move(table, -1, 0)
            else:
                _move(table, 1, 0)
            return True
        return super().eventFilter(obj, event)


class GridNavTable(QTableWidget):
    """支援 ENTER／方向鍵在儲存格間移動的 QTableWidget。"""

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if _handle_nav_key(self, event):
            return
        super().keyPressEvent(event)


def _handle_nav_key(table: QTableWidget, event: QKeyEvent) -> bool:
    key = event.key()
    rows = table.rowCount()
    cols = table.columnCount()
    if rows <= 0 or cols <= 0:
        return False
    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        _move_enter(table)
        event.accept()
        return True
    if key == Qt.Key.Key_Left:
        _move(table, 0, -1)
        event.accept()
        return True
    if key == Qt.Key.Key_Right:
        _move(table, 0, 1)
        event.accept()
        return True
    if key == Qt.Key.Key_Up:
        _move(table, -1, 0)
        event.accept()
        return True
    if key == Qt.Key.Key_Down:
        _move(table, 1, 0)
        event.accept()
        return True
    return False


def install_grid_keyboard_nav(table: QTableWidget, *, editable: bool = True) -> None:
    """為既有 QTableWidget 安裝鍵盤導覽與可編輯。"""
    if editable:
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )

    original = table.keyPressEvent

    def _nav(event: QKeyEvent, *, _table: QTableWidget = table, _orig=original) -> None:
        if _handle_nav_key(_table, event):
            return
        _orig(event)

    table.keyPressEvent = _nav  # type: ignore[method-assign]
    table.setTabKeyNavigation(True)
    delegate = _NavDelegate(table, table)
    table.setItemDelegate(delegate)
    table._racing_grid_nav_delegate = delegate  # type: ignore[attr-defined]

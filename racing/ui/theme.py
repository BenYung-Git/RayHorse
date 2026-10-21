"""Racing 介面主題、格式化與 Grid／表單共用樣式。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


GRID_ROW_HEIGHT = 24
GRID_VISIBLE_ROWS = 8
GRID_MIN_CHARS = 6
GRID_MAX_CHARS = 20
GRID_HEADER_PT = 11
GRID_DATA_PT = 10
FORM_LABEL_PT = 12
FORM_FIELD_PT = 11


@dataclass(frozen=True, slots=True)
class ThemeSpec:
    key: str
    name: str
    window_bg: str
    panel_bg: str
    input_bg: str
    border: str
    border_focus: str
    text: str
    label: str
    header_bg: str
    button_start: str
    button_end: str
    button_hover_start: str
    button_hover_end: str
    button_text: str
    select_row: str
    select_row_text: str
    current_cell: str
    current_cell_text: str
    alt_row: str
    status_bg: str


# 預設：Fusion 科技風 + 5 種淡淺色
THEMES: list[ThemeSpec] = [
    ThemeSpec(
        key="fusion_tech",
        name="① Fusion 科技風（預設）",
        window_bg="#eef5fc",
        panel_bg="#f7fbff",
        input_bg="#f3f7ff",
        border="#9eb6d9",
        border_focus="#3d7fd4",
        text="#1a2f4a",
        label="#14345c",
        header_bg="#d9e8fb",
        button_start="#9ec7f2",
        button_end="#4aa3e8",
        button_hover_start="#b7d8f8",
        button_hover_end="#63b4ef",
        button_text="#0d2744",
        select_row="#7eb6ea",
        select_row_text="#083156",
        current_cell="#2f7fd4",
        current_cell_text="#ffffff",
        alt_row="#f3f8ff",
        status_bg="#e3eefb",
    ),
    ThemeSpec(
        key="mint",
        name="② 薄荷清新",
        window_bg="#f0faf6",
        panel_bg="#f7fcfa",
        input_bg="#f2faf7",
        border="#9dcdb8",
        border_focus="#3fa97a",
        text="#1f3d32",
        label="#184836",
        header_bg="#d8f0e5",
        button_start="#9fd9bc",
        button_end="#4fba88",
        button_hover_start="#b6e5cd",
        button_hover_end="#68c79a",
        button_text="#12382a",
        select_row="#7ecfad",
        select_row_text="#0f3a29",
        current_cell="#2f9b6d",
        current_cell_text="#ffffff",
        alt_row="#f1faf6",
        status_bg="#e4f5ed",
    ),
    ThemeSpec(
        key="apricot",
        name="③ 暖杏米白",
        window_bg="#fbf6ef",
        panel_bg="#fffaf4",
        input_bg="#fff7ef",
        border="#d9bf9a",
        border_focus="#c48a45",
        text="#4a3520",
        label="#5a3d22",
        header_bg="#f3e4cf",
        button_start="#e8c89a",
        button_end="#d39a55",
        button_hover_start="#f0d5b0",
        button_hover_end="#dea66a",
        button_text="#3d2a16",
        select_row="#e0b87a",
        select_row_text="#3d2a16",
        current_cell="#c48a45",
        current_cell_text="#ffffff",
        alt_row="#fbf4ea",
        status_bg="#f5eadc",
    ),
    ThemeSpec(
        key="mist_blue",
        name="④ 霧灰藍",
        window_bg="#f2f5f8",
        panel_bg="#f8fafc",
        input_bg="#f4f7fa",
        border="#a8b6c6",
        border_focus="#5b7ea0",
        text="#2a3542",
        label="#243447",
        header_bg="#e1e8f0",
        button_start="#b7c7d8",
        button_end="#6f8fad",
        button_hover_start="#c8d5e3",
        button_hover_end="#82a0bb",
        button_text="#1d2b3a",
        select_row="#9bb0c6",
        select_row_text="#1d2b3a",
        current_cell="#5b7ea0",
        current_cell_text="#ffffff",
        alt_row="#f3f6f9",
        status_bg="#e8eef4",
    ),
    ThemeSpec(
        key="soft_green",
        name="⑤ 薄霧綠",
        window_bg="#f3f8f2",
        panel_bg="#f8fbf7",
        input_bg="#f4f9f3",
        border="#a9c4a4",
        border_focus="#5f9558",
        text="#2a3d28",
        label="#264224",
        header_bg="#ddeedd",
        button_start="#b3d0ae",
        button_end="#6fad68",
        button_hover_start="#c5dcc1",
        button_hover_end="#83ba7c",
        button_text="#1e331c",
        select_row="#98c492",
        select_row_text="#1e331c",
        current_cell="#5f9558",
        current_cell_text="#ffffff",
        alt_row="#f2f8f1",
        status_bg="#e6f0e5",
    ),
    ThemeSpec(
        key="rose_mist",
        name="⑥ 淡玫瑰粉",
        window_bg="#faf3f6",
        panel_bg="#fff8fb",
        input_bg="#fff5f9",
        border="#d4a8ba",
        border_focus="#c05d84",
        text="#4a2a38",
        label="#5a3042",
        header_bg="#f3dde7",
        button_start="#e3adc2",
        button_end="#c96b90",
        button_hover_start="#ecc0d0",
        button_hover_end="#d47fa1",
        button_text="#3d2030",
        select_row="#d992ae",
        select_row_text="#3d2030",
        current_cell="#c05d84",
        current_cell_text="#ffffff",
        alt_row="#fbf2f6",
        status_bg="#f5e6ec",
    ),
]


def theme_names() -> list[str]:
    return [t.name for t in THEMES]


def theme_by_index(index: int) -> ThemeSpec:
    if index < 0 or index >= len(THEMES):
        return THEMES[0]
    return THEMES[index]


def build_app_stylesheet(theme: ThemeSpec) -> str:
    return f"""
QWidget {{
    background: {theme.window_bg};
    color: {theme.text};
    font-size: 10pt;
}}
QMainWindow, QDialog, QScrollArea, QScrollArea > QWidget > QWidget {{
    background: {theme.window_bg};
    color: {theme.text};
}}
QLabel {{
    background: transparent;
    color: {theme.label};
    font-size: 11pt;
    font-weight: 700;
}}
QLineEdit, QComboBox, QSpinBox, QTimeEdit, QTextEdit, QPlainTextEdit, QListWidget {{
    background: {theme.input_bg};
    color: {theme.text};
    border: 1px solid {theme.border};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {theme.select_row};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTimeEdit:focus,
QTextEdit:focus, QPlainTextEdit:focus, QListWidget:focus {{
    border: 2px solid {theme.border_focus};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {theme.button_start}, stop:1 {theme.button_end});
    color: {theme.button_text};
    border: 1px solid {theme.border};
    border-radius: 4px;
    padding: 3px 10px;
    min-height: 22px;
    max-height: 28px;
    font-size: 10pt;
    font-weight: 600;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {theme.button_hover_start}, stop:1 {theme.button_hover_end});
}}
QPushButton:pressed {{
    background: {theme.border_focus};
    color: #ffffff;
}}
QPushButton:disabled {{
    background: #d7dee8;
    color: #8a93a3;
}}
QToolBar {{
    background: {theme.panel_bg};
    border-bottom: 1px solid {theme.border};
    spacing: 8px;
    padding: 4px;
}}
QStatusBar {{
    background: {theme.status_bg};
    color: {theme.text};
    border-top: 1px solid {theme.border};
}}
QMenuBar {{
    background: {theme.panel_bg};
    color: {theme.label};
    font-family: "Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI";
    font-size: 13pt;
    font-weight: 700;
    padding: 2px 6px;
    min-height: 32px;
}}
QMenuBar::item {{
    padding: 6px 14px;
    margin: 0 1px;
    color: {theme.label};
    font-family: "Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI";
    font-size: 13pt;
    font-weight: 700;
    background: transparent;
}}
QMenuBar::item:selected {{
    background: {theme.header_bg};
    color: {theme.label};
}}
QMenu {{
    background: {theme.panel_bg};
    color: {theme.text};
    border: 1px solid {theme.border};
    font-family: "Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI";
    font-size: 12pt;
    font-weight: 600;
}}
QMenu::item {{
    padding: 6px 18px;
    font-size: 12pt;
    color: {theme.text};
}}
QMenu::item:selected {{
    background: {theme.select_row};
    color: {theme.select_row_text};
}}
QTabWidget::pane {{
    border: 1px solid {theme.border};
    background: {theme.panel_bg};
}}
QTabBar::tab {{
    background: {theme.header_bg};
    color: {theme.label};
    border: 1px solid {theme.border};
    padding: 8px 16px;
    margin-right: 2px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background: {theme.button_end};
    color: #ffffff;
}}
QHeaderView::section {{
    background: {theme.header_bg};
    color: {theme.label};
    border: 1px solid {theme.border};
    padding: 4px 6px;
    font-size: {GRID_HEADER_PT}pt;
    font-weight: 700;
}}
QTableWidget, QTableView {{
    background: {theme.panel_bg};
    alternate-background-color: {theme.alt_row};
    color: {theme.text};
    gridline-color: {theme.border};
    border: 1px solid {theme.border};
    font-size: {GRID_DATA_PT}pt;
    selection-background-color: {theme.select_row};
    selection-color: {theme.select_row_text};
}}
QTableWidget::item:selected, QTableView::item:selected {{
    background: {theme.select_row};
    color: {theme.select_row_text};
}}
QTableWidget::item:focus, QTableView::item:focus {{
    background: {theme.current_cell};
    color: {theme.current_cell_text};
    border: 1px solid {theme.border_focus};
}}
QGroupBox {{
    background: {theme.panel_bg};
    border: 1px solid {theme.border};
    border-radius: 6px;
    margin-top: 10px;
    font-weight: 700;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {theme.label};
}}
QProgressBar {{
    border: 1px solid {theme.border};
    border-radius: 4px;
    background: {theme.input_bg};
    text-align: center;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {theme.button_start}, stop:1 {theme.button_end});
}}
QRadioButton, QCheckBox {{
    background: transparent;
    color: {theme.text};
    font-weight: 600;
}}
"""


def apply_fusion_style(app: QApplication) -> None:
    app.setStyle("Fusion")


def style_action_button(btn: QPushButton, icon: QStyle.StandardPixmap | None = None) -> None:
    btn.setMinimumHeight(22)
    btn.setMaximumHeight(28)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon is not None:
        style = btn.style() or QApplication.style()
        if style is not None:
            btn.setIcon(style.standardIcon(icon))


def configure_data_grid(
    table: QTableWidget,
    *,
    multi_select: bool = True,
    enable_copy: bool = True,
    fixed_viewport: bool = True,
    visible_rows: int | None = None,
) -> None:
    """套用 Grid 規格：列高 24px、可視列數、字型、多選、複製。"""
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    if multi_select:
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    else:
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(GRID_ROW_HEIGHT)
    table.verticalHeader().setMinimumSectionSize(GRID_ROW_HEIGHT)
    table.setShowGrid(True)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    font_h = QFont(table.font())
    font_h.setPointSize(GRID_HEADER_PT)
    font_h.setBold(True)
    header.setFont(font_h)
    font_d = QFont(table.font())
    font_d.setPointSize(GRID_DATA_PT)
    table.setFont(font_d)
    rows = GRID_VISIBLE_ROWS if visible_rows is None else max(1, int(visible_rows))
    # 資料不足也保留空間；超過可視列數則垂直捲動
    # 表頭／邊框預留需足夠，否則實際可見列會少於 visible_rows
    header_h = max(header.sizeHint().height(), 36)
    frame_h = max(2, table.frameWidth() * 2)
    min_h = GRID_ROW_HEIGHT * rows + header_h + frame_h + 12
    table.setMinimumHeight(min_h)
    if fixed_viewport:
        table.setMaximumHeight(min_h)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    if enable_copy:
        shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda t=table: copy_table_selection(t))


def char_width_px(table: QTableWidget) -> int:
    return max(7, table.fontMetrics().horizontalAdvance("0"))


def resize_grid_columns_to_content(table: QTableWidget) -> None:
    """欄寬依內容，限制約 6～20 字元寬。"""
    if table.columnCount() <= 0:
        return
    cw = char_width_px(table)
    min_w = GRID_MIN_CHARS * cw + 12
    max_w = GRID_MAX_CHARS * cw + 16
    table.resizeColumnsToContents()
    for col in range(table.columnCount()):
        w = table.columnWidth(col)
        table.setColumnWidth(col, max(min_w, min(max_w, w)))


def is_numeric_text(text: str) -> bool:
    s = (text or "").strip().replace(",", "")
    if not s:
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return True
    return False


def align_item_by_value(item: QTableWidgetItem) -> None:
    text = item.text()
    if is_numeric_text(text):
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    else:
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def make_table_item(text: str, *, numeric: bool | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text if text is not None else ""))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if numeric is None:
        align_item_by_value(item)
    elif numeric:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    else:
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return item


def format_date_yyyy_mm_dd(value: object) -> str:
    from racing.web_utils import normalize_date_yyyy_mm_dd

    return normalize_date_yyyy_mm_dd(value)


def format_two_digit_99(value: object) -> str:
    from racing.web_utils import format_two_digit_99 as _fmt

    return _fmt(value)


def format_money(value: object) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        num = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{num:,.2f}"


def format_rate(value: object) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        num = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{num:,.4f}"


def format_integer(value: object) -> str:
    if value is None or str(value).strip() == "":
        return ""
    text = str(value).strip().replace(",", "")
    try:
        num = Decimal(text)
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{int(num):,}"


def copy_table_selection(table: QTableWidget) -> None:
    indexes = table.selectionModel().selectedIndexes() if table.selectionModel() else []
    if not indexes:
        # 若無選取，複製全部
        rows = range(table.rowCount())
        cols = range(table.columnCount())
    else:
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes})
    lines: list[str] = []
    # 含欄位標題
    header = [table.horizontalHeaderItem(c).text() if table.horizontalHeaderItem(c) else str(c) for c in cols]
    lines.append("\t".join(header))
    for r in rows:
        cells = []
        for c in cols:
            item = table.item(r, c)
            cells.append(item.text() if item else "")
        lines.append("\t".join(cells))
    QGuiApplication.clipboard().setText("\n".join(lines))


class CopyableMessageDialog(QDialog):
    """訊息可全選複製。"""

    def __init__(self, parent: QWidget | None, title: str, message: str, *, level: str = "info") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 280)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setText(message)
        text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        copy_btn = buttons.addButton("複製全部", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(message))
        buttons.accepted.connect(self.accept)
        root = QVBoxLayout(self)
        tip = QLabel("提示：可拖曳選取文字，或按「複製全部」。")
        tip.setWordWrap(True)
        root.addWidget(tip)
        root.addWidget(text, 1)
        root.addWidget(buttons)
        _ = level


def show_copyable_message(
    parent: QWidget | None,
    title: str,
    message: str,
    *,
    level: str = "info",
) -> None:
    dlg = CopyableMessageDialog(parent, title, message, level=level)
    dlg.exec()


def make_form_label(text: str) -> QLabel:
    lab = QLabel(text)
    font = QFont(lab.font())
    font.setPointSize(FORM_LABEL_PT)
    font.setBold(True)
    lab.setFont(font)
    lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return lab


def wrap_form_in_scroll(form_widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(form_widget)
    return scroll


def build_labeled_form(
    rows: Iterable[tuple[str, QWidget]],
    *,
    save_callback=None,
    cancel_callback=None,
) -> QWidget:
    """標準輸入表單：左標右欄；Note/Remark/Memo 佔滿列；含儲存／取消。"""
    container = QWidget()
    layout = QVBoxLayout(container)
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(10)
    for title, widget in rows:
        key = title.strip().lower()
        is_note = any(x in key for x in ("note", "remark", "memo", "備註", "說明", "備考"))
        make_form_label(title)
        if hasattr(widget, "setFont"):
            f = QFont(widget.font())
            f.setPointSize(FORM_FIELD_PT)
            widget.setFont(f)
        if is_note:
            row = QWidget()
            row_l = QVBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            lab = make_form_label(title)
            lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            row_l.addWidget(lab)
            row_l.addWidget(widget)
            form.addRow(row)
        else:
            form.addRow(make_form_label(title), widget)
    layout.addLayout(form)
    if save_callback is not None or cancel_callback is not None:
        box = QDialogButtonBox()
        if save_callback is not None:
            save_btn = box.addButton("儲存", QDialogButtonBox.ButtonRole.AcceptRole)
            style_action_button(save_btn, QStyle.StandardPixmap.SP_DialogSaveButton)
            save_btn.clicked.connect(save_callback)
        if cancel_callback is not None:
            cancel_btn = box.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
            style_action_button(cancel_btn, QStyle.StandardPixmap.SP_DialogCancelButton)
            cancel_btn.clicked.connect(cancel_callback)
        layout.addWidget(box)
    return wrap_form_in_scroll(container)

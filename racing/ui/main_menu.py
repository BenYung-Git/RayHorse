"""可切換的主選單：5 種呈現方式（MenuBar / ToolBar / TreeView / ImageList / ListView）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMenuBar,
    QSizePolicy,
    QStyle,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)


MENU_FONT_PT = 13
# 側欄需容納最長項目「騎師王-練馬師王」完整顯示
_SIDE_MENU_ICON_PX = 28
_SIDE_MENU_PAD_PX = 56


@dataclass(frozen=True, slots=True)
class MainMenuItem:
    key: str
    title: str
    icon: QStyle.StandardPixmap


MAIN_MENU_ITEMS: tuple[MainMenuItem, ...] = (
    MainMenuItem("racecard", "排位", QStyle.StandardPixmap.SP_FileDialogListView),
    MainMenuItem("results", "賽馬結果", QStyle.StandardPixmap.SP_FileDialogDetailedView),
    MainMenuItem("kings", "騎師王-練馬師王", QStyle.StandardPixmap.SP_DialogApplyButton),
    MainMenuItem("analysis", "賽馬分析", QStyle.StandardPixmap.SP_FileDialogInfoView),
    MainMenuItem("horses", "馬匹-往績", QStyle.StandardPixmap.SP_DirHomeIcon),
    MainMenuItem("backup", "備份資料庫", QStyle.StandardPixmap.SP_DialogSaveButton),
    MainMenuItem("download", "下載資料", QStyle.StandardPixmap.SP_ArrowDown),
)

# 與 WEB／分頁色對齊：(文字色, 底色)
MAIN_MENU_ITEM_COLORS: dict[str, tuple[str, str]] = {
    "racecard": ("#78350f", "#fde68a"),
    "results": ("#831843", "#fbcfe8"),
    "kings": ("#4c1d95", "#ddd6fe"),
    "analysis": ("#1e3a8a", "#bfdbfe"),
    "horses": ("#14532d", "#bbf7d0"),
    "backup": ("#9a3412", "#fdba74"),
    "download": ("#155e75", "#a5f3fc"),
}


def _item_colors(key: str) -> tuple[str, str]:
    return MAIN_MENU_ITEM_COLORS.get(key, ("#1a2f4a", "#eef5fc"))


def _apply_item_colors(widget: QWidget, key: str) -> None:
    fg, bg = _item_colors(key)
    widget.setStyleSheet(
        f"background-color: {bg}; color: {fg}; border: 1px solid transparent; "
        f"border-radius: 6px; padding: 4px 10px; font-weight: 700;"
    )


def _side_menu_content_width(point_size: int = MENU_FONT_PT) -> int:
    """依最長選單標題估算側欄寬度（圖示＋內距），確保不截字。"""
    longest = max((item.title for item in MAIN_MENU_ITEMS), key=len, default="")
    try:
        font = _menu_font(point_size, True)
        text_w = QFontMetrics(font).horizontalAdvance(longest)
    except Exception:  # noqa: BLE001
        # 無 GUI／字型尚未就緒時，以字數粗估（中文粗體約 pt+4）
        text_w = len(longest) * (point_size + 4)
    return max(280, text_w + _SIDE_MENU_ICON_PX + _SIDE_MENU_PAD_PX)


MAIN_MENU_STYLES: tuple[tuple[str, str], ...] = (
    ("menubar", "① MenuBar 傳統選單"),
    ("toolbar", "② ToolBar 圖示列"),
    ("treeview", "③ TreeView 樹狀選單"),
    ("imagelist", "④ ImageList 圖示清單"),
    ("listview", "⑤ ListView 列表選單"),
)


def main_menu_style_names() -> list[str]:
    return [name for _, name in MAIN_MENU_STYLES]


def main_menu_style_key(index: int) -> str:
    if index < 0 or index >= len(MAIN_MENU_STYLES):
        return MAIN_MENU_STYLES[0][0]
    return MAIN_MENU_STYLES[index][0]


def main_menu_style_index(key: str) -> int:
    for i, (k, _) in enumerate(MAIN_MENU_STYLES):
        if k == key:
            return i
    return 0


def _std_icon(pixmap: QStyle.StandardPixmap) -> QIcon:
    style = QApplication.style()
    if style is None:
        return QIcon()
    return style.standardIcon(pixmap)


def _menu_font(point_size: int = MENU_FONT_PT, bold: bool = True) -> QFont:
    font = QFont()
    # 明確指定中文字型，避免 MenuBar 標題變成預設 9pt／亂碼感
    font.setFamilies(["Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI", "Sans Serif"])
    font.setPointSize(point_size)
    font.setBold(bold)
    return font


def _set_menu_title_font(menu: QMenu, point_size: int = MENU_FONT_PT) -> None:
    """設定 MenuBar 上的選單標題字型（與下拉內容字型分開）。"""
    act = menu.menuAction()
    act.setFont(_menu_font(point_size, True))
    act.setText(act.text())  # 觸發重繪
    menu.setFont(_menu_font(12, True))


class MainMenuPanel(QWidget):
    """左側／上方主選單容器（TreeView / ImageList / ListView / ToolBar 用）。"""

    activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._style_key = "treeview"
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(4, 4, 4, 4)
        self._root.setSpacing(6)
        self._title = QLabel("主選單")
        self._title.setFont(_menu_font(14, True))
        self._root.addWidget(self._title)
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._root.addLayout(self._body, 1)
        self._host: QWidget | None = None
        side_w = _side_menu_content_width()
        self.setMinimumWidth(side_w)

    def set_style(self, style_key: str) -> None:
        self._style_key = style_key
        self._rebuild()

    def _clear_body(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._host = None

    def _apply_side_width(self, *, imagelist: bool = False) -> None:
        side_w = _side_menu_content_width(12 if imagelist else MENU_FONT_PT)
        # imagelist 兩欄時略加寬；其餘單欄以最長文字為準
        if imagelist:
            side_w = max(side_w, 280)
        self.setMinimumWidth(side_w)
        self.setMaximumWidth(side_w + 40)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def _rebuild(self) -> None:
        self._clear_body()
        key = self._style_key
        if key == "toolbar":
            self._title.setVisible(False)
            host = self._build_toolbar()
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        elif key == "imagelist":
            self._title.setVisible(True)
            host = self._build_imagelist()
            self._apply_side_width(imagelist=True)
        elif key == "listview":
            self._title.setVisible(True)
            host = self._build_listview()
            self._apply_side_width()
        else:  # treeview default for side panel
            self._title.setVisible(True)
            host = self._build_treeview()
            self._apply_side_width()
        self._host = host
        self._body.addWidget(host, 1)

    def _emit(self, item_key: str) -> None:
        self.activated.emit(item_key)

    def _build_toolbar(self) -> QWidget:
        frame = QFrame()
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(8)
        for item in MAIN_MENU_ITEMS:
            btn = QToolButton()
            btn.setText(item.title)
            btn.setIcon(_std_icon(item.icon))
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.setFont(_menu_font(13, True))
            btn.setMinimumHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            _apply_item_colors(btn, item.key)
            btn.clicked.connect(lambda checked=False, k=item.key: self._emit(k))
            lay.addWidget(btn)
        lay.addStretch(1)
        return frame

    def _build_treeview(self) -> QWidget:
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setFont(_menu_font(13, True))
        tree.setIconSize(QSize(28, 28))
        tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tree.setUniformRowHeights(True)
        root = QTreeWidgetItem(["功能選單"])
        root.setFont(0, _menu_font(13, True))
        tree.addTopLevelItem(root)
        for item in MAIN_MENU_ITEMS:
            child = QTreeWidgetItem([item.title])
            child.setIcon(0, _std_icon(item.icon))
            child.setData(0, Qt.ItemDataRole.UserRole, item.key)
            child.setFont(0, _menu_font(13, True))
            child.setToolTip(0, item.title)
            fg, bg = _item_colors(item.key)
            child.setForeground(0, QBrush(QColor(fg)))
            child.setBackground(0, QBrush(QColor(bg)))
            root.addChild(child)
        root.setExpanded(True)

        def on_click(node: QTreeWidgetItem, _col: int) -> None:
            key = node.data(0, Qt.ItemDataRole.UserRole)
            if key:
                self._emit(str(key))

        tree.itemClicked.connect(on_click)
        return tree

    def _build_imagelist(self) -> QWidget:
        lst = QListWidget()
        lst.setViewMode(QListWidget.ViewMode.IconMode)
        lst.setMovement(QListWidget.Movement.Static)
        lst.setResizeMode(QListWidget.ResizeMode.Adjust)
        lst.setWordWrap(True)
        lst.setTextElideMode(Qt.TextElideMode.ElideNone)
        lst.setSpacing(10)
        lst.setFont(_menu_font(12, True))
        lst.setIconSize(QSize(48, 48))
        for item in MAIN_MENU_ITEMS:
            row = QListWidgetItem(_std_icon(item.icon), item.title)
            row.setData(Qt.ItemDataRole.UserRole, item.key)
            row.setFont(_menu_font(12, True))
            row.setToolTip(item.title)
            row.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            # 預留足夠格子寬度，避免「騎師王-練馬師王」被裁切
            row.setSizeHint(QSize(130, 100))
            fg, bg = _item_colors(item.key)
            row.setForeground(QBrush(QColor(fg)))
            row.setBackground(QBrush(QColor(bg)))
            lst.addItem(row)

        def on_click(row: QListWidgetItem) -> None:
            key = row.data(Qt.ItemDataRole.UserRole)
            if key:
                self._emit(str(key))

        lst.itemClicked.connect(on_click)
        return lst

    def _build_listview(self) -> QWidget:
        lst = QListWidget()
        lst.setViewMode(QListWidget.ViewMode.ListMode)
        lst.setFont(_menu_font(13, True))
        lst.setIconSize(QSize(28, 28))
        lst.setSpacing(4)
        lst.setTextElideMode(Qt.TextElideMode.ElideNone)
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        lst.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        for item in MAIN_MENU_ITEMS:
            row = QListWidgetItem(_std_icon(item.icon), item.title)
            row.setData(Qt.ItemDataRole.UserRole, item.key)
            row.setFont(_menu_font(13, True))
            row.setToolTip(item.title)
            fg, bg = _item_colors(item.key)
            row.setForeground(QBrush(QColor(fg)))
            row.setBackground(QBrush(QColor(bg)))
            lst.addItem(row)

        def on_click(row: QListWidgetItem) -> None:
            key = row.data(Qt.ItemDataRole.UserRole)
            if key:
                self._emit(str(key))

        lst.itemClicked.connect(on_click)
        return lst


class MainMenuController:
    """管理主選單 5 種樣式，並把項目接到對應 callback。"""

    def __init__(
        self,
        window,
        handlers: dict[str, Callable[[], None]],
    ) -> None:
        self._window = window
        self._handlers = handlers
        self._style_key = "treeview"
        self._panel = MainMenuPanel(window)
        self._panel.activated.connect(self._on_activated)
        self._menubar_actions: list[QAction] = []
        self._main_menu: QWidget | None = None  # QMenu under menubar
        self._toolbar: QToolBar | None = None

    @property
    def panel(self) -> MainMenuPanel:
        return self._panel

    @property
    def style_key(self) -> str:
        return self._style_key

    def apply_style(self, style_key: str) -> None:
        self._style_key = style_key if style_key in {k for k, _ in MAIN_MENU_STYLES} else "menubar"
        self._clear_menubar_items()
        self._clear_toolbar()
        if self._style_key == "menubar":
            self._panel.hide()
            self._install_menubar()
        elif self._style_key == "toolbar":
            self._panel.hide()
            self._install_toolbar()
        else:
            self._panel.show()
            self._panel.set_style(self._style_key)

    def _on_activated(self, key: str) -> None:
        fn = self._handlers.get(key)
        if fn is not None:
            fn()

    def _clear_menubar_items(self) -> None:
        bar: QMenuBar = self._window.menuBar()
        for action in list(self._menubar_actions):
            bar.removeAction(action)
            action.deleteLater()
        self._menubar_actions.clear()
        self._main_menu = None

    def _install_menubar(self) -> None:
        bar: QMenuBar = self._window.menuBar()
        bar.setFont(_menu_font(13, True))
        menu = QMenu("主選單", bar)
        _set_menu_title_font(menu, 13)
        self._main_menu = menu
        menu.setIconVisibleInMenu(True)
        menu_min_w = _side_menu_content_width()
        menu.setMinimumWidth(menu_min_w)
        for item in MAIN_MENU_ITEMS:
            btn = QToolButton(menu)
            btn.setText(item.title)
            btn.setIcon(_std_icon(item.icon))
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.setFont(_menu_font(13, True))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumWidth(menu_min_w - 16)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            _apply_item_colors(btn, item.key)
            btn.clicked.connect(lambda checked=False, k=item.key: self._on_activated(k))
            wrap = QWidgetAction(menu)
            wrap.setDefaultWidget(btn)
            menu.addAction(wrap)
        # 把「主選單」插到最左邊
        actions = bar.actions()
        if actions:
            bar.insertMenu(actions[0], menu)
        else:
            bar.addMenu(menu)
        self._menubar_actions.append(menu.menuAction())
        _set_menu_title_font(menu, 13)

    def _clear_toolbar(self) -> None:
        if self._toolbar is not None:
            self._window.removeToolBar(self._toolbar)
            self._toolbar.deleteLater()
            self._toolbar = None

    def _install_toolbar(self) -> None:
        tb = QToolBar("主選單", self._window)
        tb.setObjectName("mainMenuToolBar")
        tb.setMovable(False)
        tb.setIconSize(QSize(28, 28))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setFont(_menu_font(13, True))
        for item in MAIN_MENU_ITEMS:
            act = QAction(_std_icon(item.icon), item.title, self._window)
            act.setFont(_menu_font(13, True))
            act.setIconVisibleInMenu(True)
            act.triggered.connect(lambda checked=False, k=item.key: self._on_activated(k))
            tb.addAction(act)
            btn = tb.widgetForAction(act)
            if isinstance(btn, QToolButton):
                _apply_item_colors(btn, item.key)
        self._window.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self._toolbar = tb

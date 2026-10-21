"""賽馬分析面板：賽日／場次／欄位／排序／條件 GRID → 製成 AI 指令。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..ai_engine import save_command
from ..config import AppConfig
from ..db import get_connection
from ..race_analysis import (
    CONDITION_TYPES,
    DEFAULT_DISPLAY_FIELDS,
    DEFAULT_SCORING,
    DISPLAY_FIELD_OPTIONS,
    ConditionRow,
    build_analysis_prompt,
    default_condition_rows,
    delete_template,
    format_columns_text,
    get_template,
    load_prefs,
    load_templates,
    parse_columns_text,
    save_prefs,
    save_template,
    suggest_command_title,
    template_as_prefs,
)
from .racecard_rank_viewer import list_stored_racecard_meetings
from .theme import configure_data_grid, style_action_button

_GRID_COLS = ("啟用", "條件類型", "計分方法")


class RaceAnalysisPanel(QWidget):
    """賽馬分析：組 prompt → 存 AI 指令／送 AI 助手。"""

    send_to_ai = Signal(str, bool)  # prompt, auto_send

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._on_status = on_status
        self._loading = False

        self._meeting = QComboBox()
        self._meeting.currentIndexChanged.connect(self._on_meeting_changed)

        self._race_all = QRadioButton("全部場次")
        self._race_one = QRadioButton("指定場次")
        self._race_all.setChecked(True)
        self._race_group = QButtonGroup(self)
        self._race_group.addButton(self._race_all)
        self._race_group.addButton(self._race_one)
        self._race_no = QComboBox()
        self._race_no.setEnabled(False)
        self._race_all.toggled.connect(self._sync_race_enabled)

        self._fields = QTextEdit()
        self._fields.setPlainText(format_columns_text(DEFAULT_DISPLAY_FIELDS))
        self._fields.setPlaceholderText(
            "以／分隔，例如：場次／馬號／馬匹名稱／…／合計分數"
        )
        self._fields.setToolTip("輸出欄位以「／」分隔輸入（可兩行顯示）")
        self._fields.setAcceptRichText(False)
        self._fields.setMinimumHeight(56)
        self._fields.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._fields.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fields.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fields.textChanged.connect(self._fit_fields_height)

        self._sort_field = QComboBox()
        self._sort_field.setEditable(True)
        self._sort_field.addItems(list(DISPLAY_FIELD_OPTIONS))
        idx = self._sort_field.findText("合計分數")
        if idx >= 0:
            self._sort_field.setCurrentIndex(idx)
        self._sort_asc = QRadioButton("升序（由小至大）")
        self._sort_desc = QRadioButton("降序（由大至小）")
        self._sort_desc.setChecked(True)
        sort_group = QButtonGroup(self)
        sort_group.addButton(self._sort_asc)
        sort_group.addButton(self._sort_desc)

        self._table = QTableWidget(0, len(_GRID_COLS))
        self._table.setHorizontalHeaderLabels(list(_GRID_COLS))
        configure_data_grid(self._table, fixed_viewport=False, multi_select=False, visible_rows=6)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setWordWrap(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 48)
        self._table.setColumnWidth(1, 150)

        cond_btns = QHBoxLayout()
        add_btn = QPushButton("新增條件")
        style_action_button(add_btn, QStyle.StandardPixmap.SP_DialogApplyButton)
        add_btn.clicked.connect(self._add_condition_row)
        del_btn = QPushButton("刪除選取")
        style_action_button(del_btn, QStyle.StandardPixmap.SP_DialogCancelButton)
        del_btn.clicked.connect(self._remove_condition_row)
        reset_btn = QPushButton("還原預設條件")
        style_action_button(reset_btn, QStyle.StandardPixmap.SP_BrowserReload)
        reset_btn.clicked.connect(self._reset_conditions)
        cond_btns.addWidget(add_btn)
        cond_btns.addWidget(del_btn)
        cond_btns.addWidget(reset_btn)
        cond_btns.addStretch(1)

        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("按「預覽指令」產生 AI 指令內容")
        self._preview.setMaximumHeight(140)

        self._title = QLineEdit()
        self._title.setPlaceholderText("指令標題（製成 AI 指令時使用）")

        self._template = QComboBox()
        self._template.setMinimumWidth(200)
        self._template.setToolTip("選擇已儲存的分析範本後按「取出」")

        preview_btn = QPushButton("預覽指令")
        style_action_button(preview_btn, QStyle.StandardPixmap.SP_FileDialogContentsView)
        preview_btn.clicked.connect(self._preview_prompt)
        save_btn = QPushButton("製成 AI 指令")
        style_action_button(save_btn, QStyle.StandardPixmap.SP_DialogSaveButton)
        save_btn.clicked.connect(self._save_as_command)
        send_btn = QPushButton("送至 AI 助手")
        style_action_button(send_btn, QStyle.StandardPixmap.SP_ArrowForward)
        send_btn.setToolTip("填入 AI 助手並自動送出，產生報表／列表")
        send_btn.clicked.connect(lambda: self._send_to_ai(auto_send=True))
        fill_btn = QPushButton("放入 AI 助手")
        style_action_button(fill_btn, QStyle.StandardPixmap.SP_CommandLink)
        fill_btn.setToolTip("只填入指令，不自動送出")
        fill_btn.clicked.connect(lambda: self._send_to_ai(auto_send=False))
        load_tpl_btn = QPushButton("取出範本")
        style_action_button(load_tpl_btn, QStyle.StandardPixmap.SP_DialogOpenButton)
        load_tpl_btn.setToolTip("套用選取的範本（欄位／排序／條件）")
        load_tpl_btn.clicked.connect(self._load_selected_template)
        save_tpl_btn = QPushButton("儲存為範本")
        style_action_button(save_tpl_btn, QStyle.StandardPixmap.SP_DialogSaveButton)
        save_tpl_btn.setToolTip("把目前欄位、排序、條件存成範本")
        save_tpl_btn.clicked.connect(self._save_as_template)
        del_tpl_btn = QPushButton("刪除範本")
        style_action_button(del_tpl_btn, QStyle.StandardPixmap.SP_DialogCancelButton)
        del_tpl_btn.clicked.connect(self._delete_selected_template)

        note = QLabel(
            "條件以 GRID 管理；啟用列會寫入 AI 指令。"
            "預設三種計分可由本機合計分數引擎直接出表；自訂條件視內容可能走模型。"
            "範本只存欄位／排序／條件，不含賽日。"
        )
        note.setWordWrap(True)

        left = QVBoxLayout()
        tpl_row = QHBoxLayout()
        tpl_row.addWidget(QLabel("範本"))
        tpl_row.addWidget(self._template, 1)
        left.addLayout(tpl_row)
        left.addWidget(QLabel("賽事日期／場地"))
        left.addWidget(self._meeting)
        race_row = QHBoxLayout()
        race_row.addWidget(self._race_all)
        race_row.addWidget(self._race_one)
        race_row.addWidget(self._race_no, 1)
        left.addLayout(race_row)
        left.addWidget(QLabel("輸出欄位（以／分隔，可兩行）"))
        left.addWidget(self._fields)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("排序欄位"))
        sort_row.addWidget(self._sort_field, 1)
        sort_row.addWidget(self._sort_desc)
        sort_row.addWidget(self._sort_asc)
        left.addLayout(sort_row)
        left.addWidget(QLabel("條件及計分方法"))
        left.addWidget(self._table, 1)
        left.addLayout(cond_btns)
        left.addWidget(QLabel("指令標題"))
        left.addWidget(self._title)
        left.addWidget(QLabel("指令預覽"))
        left.addWidget(self._preview)
        left.addWidget(note)

        right = QVBoxLayout()
        right.setSpacing(8)
        for btn in (
            load_tpl_btn,
            save_tpl_btn,
            del_tpl_btn,
            preview_btn,
            save_btn,
            fill_btn,
            send_btn,
        ):
            btn.setMinimumWidth(128)
            right.addWidget(btn)
        right.addStretch(1)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addLayout(left, 1)
        body.addLayout(right, 0)

        root = QVBoxLayout(self)
        root.addLayout(body, 1)

        self._reload_meetings()
        self._reload_templates()
        self._load_prefs_into_ui()
        if self._table.rowCount() == 0:
            self._set_condition_rows(default_condition_rows())
        self._fit_fields_height()

    def reload(self) -> None:
        self._reload_meetings()
        self._reload_templates()

    def _reload_templates(self, *, select_id: str | None = None) -> None:
        keep = select_id or self._template.currentData()
        self._template.blockSignals(True)
        self._template.clear()
        self._template.addItem("（選擇範本）", None)
        for item in load_templates(self._config.data_dir):
            self._template.addItem(str(item.get("name") or item.get("id")), item.get("id"))
        if keep:
            for i in range(self._template.count()):
                if self._template.itemData(i) == keep:
                    self._template.setCurrentIndex(i)
                    break
        self._template.blockSignals(False)

    def _current_analysis_payload(self) -> dict:
        return {
            "columns": self._selected_columns(),
            "columns_text": self._fields.toPlainText().strip(),
            "sort_field": self._sort_field.currentText().strip() or "合計分數",
            "sort_ascending": self._sort_asc.isChecked(),
            "all_races": self._race_all.isChecked(),
            "conditions": [r.to_dict() for r in self._condition_rows_from_grid()],
        }

    def _apply_prefs_dict(self, prefs: dict) -> None:
        cols_text = str(prefs.get("columns_text") or "").strip()
        cols = prefs.get("columns")
        if cols_text:
            self._fields.setPlainText(cols_text)
            self._fit_fields_height()
        elif isinstance(cols, list) and cols:
            self._set_selected_columns([str(c) for c in cols])
        sort_field = str(prefs.get("sort_field") or "")
        if sort_field:
            idx = self._sort_field.findText(sort_field)
            if idx >= 0:
                self._sort_field.setCurrentIndex(idx)
            else:
                self._sort_field.setEditText(sort_field)
        if bool(prefs.get("sort_ascending")):
            self._sort_asc.setChecked(True)
        else:
            self._sort_desc.setChecked(True)
        if "all_races" in prefs:
            if bool(prefs.get("all_races")):
                self._race_all.setChecked(True)
            else:
                self._race_one.setChecked(True)
        self._sync_race_enabled()
        raw_conds = prefs.get("conditions")
        if isinstance(raw_conds, list) and raw_conds:
            rows = [
                ConditionRow.from_dict(c) for c in raw_conds if isinstance(c, dict)
            ]
            if rows:
                self._set_condition_rows(rows)

    def _load_selected_template(self) -> None:
        tid = self._template.currentData()
        if not tid:
            QMessageBox.information(self, "賽馬分析", "請先選擇要取出的範本。")
            return
        tpl = get_template(self._config.data_dir, str(tid))
        if not tpl:
            QMessageBox.warning(self, "賽馬分析", "找不到該範本。")
            self._reload_templates()
            return
        self._apply_prefs_dict(template_as_prefs(tpl))
        self._persist_prefs()
        self._status(f"已取出範本：{tpl.get('name')}")

    def _save_as_template(self) -> None:
        cur_name = ""
        tid = self._template.currentData()
        if tid:
            tpl = get_template(self._config.data_dir, str(tid))
            if tpl:
                cur_name = str(tpl.get("name") or "")
        name, ok = QInputDialog.getText(
            self,
            "儲存為範本",
            "範本名稱：",
            QLineEdit.EchoMode.Normal,
            cur_name or "賽馬分析範本",
        )
        if not ok:
            return
        name = str(name or "").strip()
        if not name:
            QMessageBox.information(self, "賽馬分析", "請輸入範本名稱。")
            return
        # 同名則覆寫；若目前選中同名範本則沿用 id
        overwrite_id = None
        for item in load_templates(self._config.data_dir):
            if str(item.get("name") or "") == name:
                overwrite_id = str(item.get("id") or "")
                break
        if overwrite_id:
            ans = QMessageBox.question(
                self,
                "賽馬分析",
                f"範本「{name}」已存在，是否覆寫？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        try:
            saved = save_template(
                self._config.data_dir,
                name=name,
                payload=self._current_analysis_payload(),
                template_id=overwrite_id,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "賽馬分析", f"儲存範本失敗：{exc}")
            return
        self._reload_templates(select_id=str(saved.get("id")))
        QMessageBox.information(self, "賽馬分析", f"已儲存範本：{saved.get('name')}")
        self._status(f"已儲存範本：{saved.get('name')}")

    def _delete_selected_template(self) -> None:
        tid = self._template.currentData()
        if not tid:
            QMessageBox.information(self, "賽馬分析", "請先選擇要刪除的範本。")
            return
        name = self._template.currentText()
        ans = QMessageBox.question(self, "賽馬分析", f"確定刪除範本「{name}」？")
        if ans != QMessageBox.StandardButton.Yes:
            return
        if delete_template(self._config.data_dir, str(tid)):
            self._reload_templates()
            self._status(f"已刪除範本：{name}")
        else:
            QMessageBox.warning(self, "賽馬分析", "刪除失敗。")

    def _status(self, text: str) -> None:
        if self._on_status:
            self._on_status(text)

    def _sync_race_enabled(self) -> None:
        self._race_no.setEnabled(self._race_one.isChecked())

    def _fit_fields_height(self) -> None:
        """輸出欄位依內容撐高，兩行起、不裁切。"""
        doc = self._fields.document()
        vw = max(40, self._fields.viewport().width())
        doc.setTextWidth(vw)
        margins = self._fields.contentsMargins()
        frame = max(2, self._fields.frameWidth() * 2)
        h = int(doc.size().height()) + margins.top() + margins.bottom() + frame + 8
        self._fields.setFixedHeight(max(56, min(h, 160)))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_fields_height()

    def _resize_condition_columns(self) -> None:
        """啟用／類型固定寬；計分方法伸展填滿，編輯框與欄寬一致。"""
        self._table.setColumnWidth(0, 48)
        self._table.setColumnWidth(1, 150)

    def _reload_meetings(self) -> None:
        self._loading = True
        cur = self._meeting.currentData()
        self._meeting.clear()
        for date, course in list_stored_racecard_meetings(self._config):
            label = f"{date}　{course}"
            self._meeting.addItem(label, (date, course))
        if cur is not None:
            for i in range(self._meeting.count()):
                if self._meeting.itemData(i) == cur:
                    self._meeting.setCurrentIndex(i)
                    break
        self._loading = False
        self._on_meeting_changed()
        if self._meeting.count() == 0:
            self._status("尚無排位賽日，請先下載排位資料")

    def _on_meeting_changed(self) -> None:
        if self._loading:
            return
        data = self._meeting.currentData()
        self._race_no.clear()
        if not data:
            return
        race_date, course = data
        conn = get_connection(self._config.db_path)
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT race_no FROM racecard_starters
                WHERE race_date = ? AND racecourse = ?
                ORDER BY race_no ASC;
                """,
                (race_date, course),
            ).fetchall()
            for r in rows:
                no = int(r["race_no"])
                self._race_no.addItem(f"第{no}場", no)
        finally:
            conn.close()
        self._title.setText(
            suggest_command_title(
                race_date=race_date,
                race_no=None,
                all_races=True,
            )
        )

    def _selected_columns(self) -> list[str]:
        return parse_columns_text(self._fields.toPlainText())

    def _set_selected_columns(self, names: list[str]) -> None:
        self._fields.setPlainText(format_columns_text(names))
        self._fit_fields_height()

    def _condition_rows_from_grid(self) -> list[ConditionRow]:
        rows: list[ConditionRow] = []
        for r in range(self._table.rowCount()):
            chk = self._table.item(r, 0)
            kind_w = self._table.cellWidget(r, 1)
            scoring_item = self._table.item(r, 2)
            kind = "custom"
            if isinstance(kind_w, QComboBox):
                kind = str(kind_w.currentData() or "custom")
            rows.append(
                ConditionRow(
                    enabled=chk is not None and chk.checkState() == Qt.CheckState.Checked,
                    kind=kind,
                    scoring=(scoring_item.text() if scoring_item else "").strip(),
                    note="",
                )
            )
        return rows

    def _set_condition_rows(self, rows: list[ConditionRow]) -> None:
        self._table.setRowCount(0)
        for row in rows:
            self._append_condition_row(row, resize=False)
        self._resize_condition_columns()

    def _append_condition_row(
        self, row: ConditionRow | None = None, *, resize: bool = True
    ) -> None:
        row = row or ConditionRow(True, "custom", "", "")
        r = self._table.rowCount()
        self._table.insertRow(r)
        chk = QTableWidgetItem("")
        chk.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        chk.setCheckState(Qt.CheckState.Checked if row.enabled else Qt.CheckState.Unchecked)
        self._table.setItem(r, 0, chk)

        combo = QComboBox()
        for kid, label in CONDITION_TYPES:
            combo.addItem(label, kid)
        idx = combo.findData(row.kind)
        combo.setCurrentIndex(idx if idx >= 0 else combo.findData("custom"))
        combo.currentIndexChanged.connect(lambda _i, rr=r: self._on_kind_changed(rr))
        self._table.setCellWidget(r, 1, combo)

        scoring = QTableWidgetItem(row.scoring or DEFAULT_SCORING.get(row.kind, ""))
        scoring.setFlags(
            Qt.ItemFlag.ItemIsEditable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        self._table.setItem(r, 2, scoring)
        if resize:
            self._resize_condition_columns()

    def _on_kind_changed(self, row: int) -> None:
        combo = self._table.cellWidget(row, 1)
        if not isinstance(combo, QComboBox):
            return
        kind = str(combo.currentData() or "custom")
        item = self._table.item(row, 2)
        if item is None:
            return
        # 切換類型時若內容仍是其他預設文案／空白，帶入該類型預設
        cur = item.text().strip()
        presets = set(DEFAULT_SCORING.values())
        if not cur or cur in presets:
            item.setText(DEFAULT_SCORING.get(kind, ""))

    def _add_condition_row(self) -> None:
        self._append_condition_row(ConditionRow(True, "custom", "", ""))

    def _remove_condition_row(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(self, "賽馬分析", "請先選取要刪除的條件列。")
            return
        self._table.removeRow(row)

    def _reset_conditions(self) -> None:
        self._set_condition_rows(default_condition_rows())

    def _current_prompt_args(self) -> dict | None:
        data = self._meeting.currentData()
        if not data:
            QMessageBox.information(self, "賽馬分析", "請先選擇賽事日期（需已下載排位）。")
            return None
        race_date, _course = data
        all_races = self._race_all.isChecked()
        race_no = None
        if not all_races:
            race_no = self._race_no.currentData()
            if race_no is None:
                QMessageBox.information(self, "賽馬分析", "請選擇場次，或改選「全部場次」。")
                return None
            race_no = int(race_no)
        cols = self._selected_columns()
        if not cols:
            QMessageBox.information(self, "賽馬分析", "請輸入輸出欄位（以／分隔）。")
            return None
        return {
            "race_date": race_date,
            "race_no": race_no,
            "all_races": all_races,
            "columns": cols,
            "sort_field": self._sort_field.currentText(),
            "sort_ascending": self._sort_asc.isChecked(),
            "conditions": self._condition_rows_from_grid(),
        }

    def _build_prompt(self) -> str | None:
        args = self._current_prompt_args()
        if not args:
            return None
        return build_analysis_prompt(**args)

    def _preview_prompt(self) -> None:
        prompt = self._build_prompt()
        if not prompt:
            return
        self._preview.setPlainText(prompt)
        args = self._current_prompt_args()
        if args:
            self._title.setText(
                suggest_command_title(
                    race_date=args["race_date"],
                    race_no=args["race_no"],
                    all_races=args["all_races"],
                )
            )
        self._persist_prefs()
        self._status("已預覽賽馬分析指令")

    def _save_as_command(self) -> None:
        prompt = self._build_prompt()
        if not prompt:
            return
        self._preview.setPlainText(prompt)
        title = self._title.text().strip() or suggest_command_title(
            race_date=(self._meeting.currentData() or ("賽日",))[0],
            all_races=self._race_all.isChecked(),
            race_no=None if self._race_all.isChecked() else self._race_no.currentData(),
        )
        cid = save_command(self._config.db_path, prompt, title=title)
        self._persist_prefs()
        QMessageBox.information(self, "賽馬分析", f"已製成 AI 指令（#{cid}）：{title}")
        self._status(f"已儲存 AI 指令 #{cid}")

    def _send_to_ai(self, *, auto_send: bool) -> None:
        prompt = self._build_prompt()
        if not prompt:
            return
        self._preview.setPlainText(prompt)
        self._persist_prefs()
        self.send_to_ai.emit(prompt, auto_send)
        self._status("已送至 AI 助手" if auto_send else "已放入 AI 助手")

    def _persist_prefs(self) -> None:
        save_prefs(self._config.data_dir, self._current_analysis_payload())

    def _load_prefs_into_ui(self) -> None:
        self._apply_prefs_dict(load_prefs(self._config.data_dir))

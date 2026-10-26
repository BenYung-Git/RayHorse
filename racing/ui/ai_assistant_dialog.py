"""AI 助手視窗：對話、指令表、文件與報表。"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..ai_engine import (
    AiAnswer,
    AiSettings,
    add_document,
    answer,
    append_chat,
    clear_chat,
    delete_command,
    ensure_default_commands,
    export_rows,
    list_commands,
    list_documents,
    load_ai_settings,
    load_chat,
    read_uploaded_file,
    save_ai_settings,
    save_command,
    user_prompts,
)
from ..config import AppConfig
from .theme import (
    configure_data_grid,
    install_multi_column_sort,
    make_table_item,
)


class _AnswerWorker(QObject):
    finished_ok = Signal()
    finished_error = Signal(str)

    def __init__(self, db_path: Path, question: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._question = question
        self.result: AiAnswer | None = None

    def run(self) -> None:
        try:
            self.result = answer(self._db_path, self._question)
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001
            self.finished_error.emit(str(exc))


class _PromptEdit(QPlainTextEdit):
    """五行對話框。游標在第一行按上鍵、最後一行按下鍵，切換上一／下一對話。"""

    _VISIBLE_LINES = 5

    def __init__(self, on_older, on_newer, on_submit) -> None:  # noqa: ANN001
        super().__init__()
        self._on_older = on_older
        self._on_newer = on_newer
        self._on_submit = on_submit
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(4)
        line = self.fontMetrics().lineSpacing()
        self.setFixedHeight(line * self._VISIBLE_LINES + 12)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802
        self.setPlainText(text)

    def _at_first_line(self) -> bool:
        return self.textCursor().blockNumber() == 0

    def _at_last_line(self) -> bool:
        cursor = self.textCursor()
        return cursor.blockNumber() >= self.document().blockCount() - 1

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Up and self._at_first_line():
            self._on_older()
            return
        if event.key() == Qt.Key.Key_Down and self._at_last_line():
            self._on_newer()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._on_submit()
            return
        super().keyPressEvent(event)


class AiAssistantPanel(QWidget):
    """本機 AI 助手面板，可嵌入主視窗分頁或獨立對話框。"""

    def __init__(
        self,
        parent: QWidget | None,
        config: AppConfig,
        action_handler: Callable[[str, str | None], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._action_handler = action_handler
        self._busy = False
        self._thread: QThread | None = None
        self._worker: _AnswerWorker | None = None
        self._history: list[str] = []
        self._commands: list = []
        self._cmd_index: int = -1
        self._cmd_draft: str = ""
        self._hist_index = 0
        self._draft = ""
        self._table_columns: list[str] = []
        self._table_rows: list[list[str]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        hint = QLabel(
            "對話框為五行。第一行按↑上一對話、最後一行按↓下一對話。Ctrl+Enter 送出。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._chat = QTextBrowser()
        self._chat.setOpenExternalLinks(False)
        self._chat.setMinimumHeight(160)
        root.addWidget(self._chat, 2)

        self._table_label = QLabel("報表／列表")
        root.addWidget(self._table_label)
        self._table = QTableWidget(0, 0)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setMinimumHeight(180)
        configure_data_grid(
            self._table,
            multi_select=True,
            enable_copy=True,
            fixed_viewport=False,
        )
        # 報表欄位動態產生：有資料後再啟用多欄排序
        root.addWidget(self._table, 3)

        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        button_col = QVBoxLayout()
        button_col.setSpacing(4)
        self._send_button = QPushButton("送出")
        self._send_button.clicked.connect(self._send)
        self._cmd_button = QPushButton("選取指令")
        self._cmd_button.setToolTip("LOOKUP AI_COMMAND，把 CMD_PROMPT 放入對話框")
        self._cmd_button.clicked.connect(self._lookup_command)
        self._cmd_prev_button = QPushButton("上一對話")
        self._cmd_prev_button.setToolTip("放入上一則已送出的對話（↑）")
        self._cmd_prev_button.clicked.connect(self._history_older)
        self._cmd_next_button = QPushButton("下一對話")
        self._cmd_next_button.setToolTip("放入下一則已送出的對話（↓）")
        self._cmd_next_button.clicked.connect(self._history_newer)
        button_col.addWidget(self._send_button)
        button_col.addWidget(self._cmd_button)
        button_col.addWidget(self._cmd_prev_button)
        button_col.addWidget(self._cmd_next_button)
        # 左側欄與「儲存指令」同寬（以欄內最寬按鈕為準）
        side_w = max(
            self._send_button.sizeHint().width(),
            self._cmd_button.sizeHint().width(),
            self._cmd_prev_button.sizeHint().width(),
            self._cmd_next_button.sizeHint().width(),
        )
        for b in (
            self._send_button,
            self._cmd_button,
            self._cmd_prev_button,
            self._cmd_next_button,
        ):
            b.setFixedWidth(side_w)
        self._input = _PromptEdit(self._history_older, self._history_newer, self._send)
        self._input.setPlaceholderText("輸入馬名、編號、問題，或要儲存的指令")
        input_row.addLayout(button_col, 0)
        input_row.addWidget(self._input, 1)
        root.addLayout(input_row)

        tools = QHBoxLayout()
        save_btn = QPushButton("儲存指令")
        save_btn.setToolTip("把對話框內容寫入 AI_COMMAND.CMD_PROMPT")
        save_btn.clicked.connect(self._save_command)
        save_btn.setFixedWidth(side_w)
        upload_btn = QPushButton("上傳文件")
        upload_btn.clicked.connect(self._upload_document)
        export_btn = QPushButton("匯出報表")
        export_btn.clicked.connect(self._export_table)
        clear_btn = QPushButton("清除對話")
        clear_btn.clicked.connect(self._clear_chat)
        settings_btn = QPushButton("模型設定")
        settings_btn.clicked.connect(self._edit_settings)
        for button in (save_btn, upload_btn, export_btn, clear_btn, settings_btn):
            tools.addWidget(button)
        tools.addStretch()
        root.addLayout(tools)

        self._status = QLabel("")
        root.addWidget(self._status)

        ensure_default_commands(self._config.db_path)
        self._reload_history()
        self._load_transcript()
        self._restore_last_table()
        self._input.setFocus()

    def _reload_history(self) -> None:
        self._history = user_prompts(self._config.db_path)
        self._hist_index = len(self._history)
        self._draft = ""

    def _load_transcript(self) -> None:
        self._chat.clear()
        rows = load_chat(self._config.db_path)
        if not rows:
            self._append("assistant", self._welcome())
            return
        for row in rows:
            self._append(str(row["ROLE"]), str(row["CONTENT"]), persist=False)

    def _welcome(self) -> str:
        return (
            "可以直接問賽馬資料，例如：\n"
            "某馬名的最佳途程、某馬名的贏的賠率、第1場相同途程的最快速度排列、"
            "今天贏馬的騎師排列、今天贏馬的練馬師排列。\n"
            "輸入馬名、烙號或關鍵字，會列出相關往績。\n"
            "也可以說「開啟排位」「下載資料」「下載排位」「下載馬匹及往績」、"
            "「指定時間下載 23:00」「開啟下載」。\n"
            "私人文件按「上傳文件」後，再問「在文件中找……」或「統計文件」。"
        )

    def _append(self, role: str, content: str, persist: bool = False) -> None:
        who = "你" if role == "user" else "AI"
        safe = html.escape(content).replace("\n", "<br>")
        self._chat.append(f"<p><b>{who}</b><br>{safe}</p>")
        cursor = self._chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._chat.setTextCursor(cursor)
        self._chat.ensureCursorVisible()
        if persist:
            append_chat(self._config.db_path, role, content)

    def _history_older(self) -> None:
        if not self._history:
            self._status.setText("尚無對話紀錄")
            return
        if self._hist_index <= 0:
            self._status.setText(f"對話 1/{len(self._history)}（已是第一則）")
            return
        if self._hist_index == len(self._history):
            self._draft = self._input.text()
        self._hist_index -= 1
        self._input.setText(self._history[self._hist_index])
        self._status.setText(f"對話 {self._hist_index + 1}/{len(self._history)}")

    def _history_newer(self) -> None:
        if not self._history:
            self._status.setText("尚無對話紀錄")
            return
        if self._hist_index >= len(self._history) - 1:
            self._hist_index = len(self._history)
            self._input.setText(self._draft)
            self._status.setText("已回到目前輸入")
            return
        self._hist_index += 1
        self._input.setText(self._history[self._hist_index])
        self._status.setText(f"對話 {self._hist_index + 1}/{len(self._history)}")

    def _reload_commands(self) -> None:
        self._commands = list(list_commands(self._config.db_path))
        if self._cmd_index >= len(self._commands):
            self._cmd_index = len(self._commands)

    def _apply_command_at(self, index: int) -> None:
        if index < 0 or index >= len(self._commands):
            return
        row = self._commands[index]
        self._input.setText(str(row["CMD_PROMPT"] or ""))
        self._input.setFocus()
        title = str(row["CMD_TITLE"] or "").strip()
        label = f"指令 {index + 1}/{len(self._commands)}"
        if title:
            label += f"：{title}"
        self._status.setText(label)

    def _command_older(self) -> None:
        self._reload_commands()
        if not self._commands:
            self._status.setText("尚無已儲存指令")
            return
        if self._cmd_index < 0 or self._cmd_index >= len(self._commands):
            self._cmd_draft = self._input.text()
            self._cmd_index = len(self._commands)
        if self._cmd_index <= 0:
            self._status.setText(f"指令 1/{len(self._commands)}（已是第一筆）")
            return
        self._cmd_index -= 1
        self._apply_command_at(self._cmd_index)

    def _command_newer(self) -> None:
        self._reload_commands()
        if not self._commands:
            self._status.setText("尚無已儲存指令")
            return
        if self._cmd_index < 0:
            self._cmd_index = -1
        if self._cmd_index >= len(self._commands) - 1:
            self._cmd_index = len(self._commands)
            self._input.setText(self._cmd_draft)
            self._input.setFocus()
            self._status.setText("已回到目前輸入")
            return
        self._cmd_index += 1
        self._apply_command_at(self._cmd_index)

    def _lookup_command(self) -> None:
        dlg = _CommandLookupDialog(self, self._config.db_path)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        prompt = dlg.selected_prompt()
        if not prompt:
            return
        self._input.setText(prompt)
        self._input.setFocus()
        self._reload_commands()
        for i, row in enumerate(self._commands):
            if str(row["CMD_PROMPT"] or "") == prompt:
                self._cmd_index = i
                break
        self._status.setText("已放入 AI_COMMAND.CMD_PROMPT")

    def _save_command(self) -> None:
        prompt = self._input.text().strip()
        if not prompt:
            QMessageBox.information(self, "儲存指令", "請先在對話框輸入要儲存的指令。")
            return
        title, ok = QInputDialog.getText(self, "儲存指令", "指令標題：", text=prompt[:24])
        if not ok:
            return
        try:
            cid = save_command(self._config.db_path, prompt, title)
        except ValueError as exc:
            QMessageBox.warning(self, "儲存指令", str(exc))
            return
        self._reload_commands()
        for i, row in enumerate(self._commands):
            if int(row["ID"]) == int(cid):
                self._cmd_index = i
                break
        self._status.setText("已儲存到 AI_COMMAND")

    def _upload_document(self) -> None:
        path_text, _selected = QFileDialog.getOpenFileName(
            self,
            "上傳私人文件",
            "",
            "文件 (*.txt *.md *.csv *.json *.html *.htm *.xlsx *.log);;所有檔案 (*.*)",
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            content = read_uploaded_file(path)
            add_document(self._config.db_path, path.name, content)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "上傳文件", str(exc))
            return
        count = len(list_documents(self._config.db_path))
        self._append("assistant", f"已儲存文件「{path.name}」，目前共 {count} 份。可問「在文件中找……」。", persist=True)
        self._status.setText(f"已上傳 {path.name}")

    def _export_table(self) -> None:
        if self._table.rowCount() <= 0 or self._table.columnCount() <= 0:
            QMessageBox.information(self, "匯出報表", "目前沒有可匯出的列表。")
            return
        # 依目前 Grid 顯示順序匯出（含多欄排序後）
        columns: list[str] = []
        for col in range(self._table.columnCount()):
            header = self._table.horizontalHeaderItem(col)
            raw = header.text() if header is not None else ""
            base = re.sub(r"\s*[↑↓]\d*\s*$", "", raw).strip()
            columns.append(base or f"欄{col + 1}")
        rows: list[list[str]] = []
        for r_index in range(self._table.rowCount()):
            rows.append(
                [
                    (
                        self._table.item(r_index, c_index).text()
                        if self._table.item(r_index, c_index) is not None
                        else ""
                    )
                    for c_index in range(self._table.columnCount())
                ]
            )
        default = str(self._config.data_dir / "ai_report.xlsx")
        path_text, _selected = QFileDialog.getSaveFileName(
            self,
            "匯出報表",
            default,
            "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() not in {".xlsx", ".csv"}:
            path = path.with_suffix(".xlsx")
        try:
            export_rows(columns, rows, path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "匯出報表", str(exc))
            return
        self._status.setText(f"已匯出 {path.name}")

    def _clear_chat(self) -> None:
        if (
            QMessageBox.question(self, "清除對話", "清除畫面上的對話紀錄？已儲存的指令和文件會保留。")
            != QMessageBox.StandardButton.Yes
        ):
            return
        clear_chat(self._config.db_path)
        self._chat.clear()
        self._append("assistant", self._welcome())
        self._history = []
        self._hist_index = 0
        self._draft = ""
        self._show_table([], [])
        self._status.setText("對話已清除")

    def _edit_settings(self) -> None:
        current = load_ai_settings(self._config.db_path)
        dlg = _SettingsDialog(self, current)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        save_ai_settings(self._config.db_path, dlg.settings())
        self._status.setText("模型設定已儲存。留空金鑰時只使用本機資料。")

    def _send(self) -> None:
        if self._busy:
            return
        text = self._input.text().strip()
        if not text:
            return
        self._busy = True
        self._send_button.setEnabled(False)
        self._append("user", text, persist=True)
        self._history.append(text)
        self._hist_index = len(self._history)
        self._draft = ""
        self._input.clear()
        self._status.setText("查詢中…")

        # 不將 QThread 設為子物件，避免面板銷毀時誤殺仍在跑的執行緒導致程式退出
        thread = QThread()
        worker = _AnswerWorker(self._config.db_path, text)
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished_ok.connect(self._on_answer, Qt.ConnectionType.QueuedConnection)
        worker.finished_error.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
        worker.finished_ok.connect(thread.quit)
        worker.finished_error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        thread.start()

    def _on_answer(self) -> None:
        self._busy = False
        self._send_button.setEnabled(True)
        result = self._worker.result if self._worker is not None else None
        ans = result if isinstance(result, AiAnswer) else AiAnswer("沒有取得回覆。")
        extra = ""
        if ans.action and self._action_handler is not None:
            try:
                extra = self._action_handler(ans.action, ans.action_arg) or ""
            except Exception as exc:  # noqa: BLE001
                extra = f"操作失敗：{exc}"
        text = ans.text if not extra else f"{ans.text}\n{extra}"
        self._append("assistant", text, persist=True)
        self._show_table(ans.columns, ans.rows)
        self._status.setText("完成")
        self._input.setFocus()

    def _on_error(self, message: str) -> None:
        self._busy = False
        self._send_button.setEnabled(True)
        self._append("assistant", f"查詢失敗：{message}", persist=True)
        self._status.setText("失敗")

    def _last_table_path(self) -> Path:
        return Path(self._config.data_dir) / "ai_last_table.json"

    def _persist_last_table(self) -> None:
        path = self._last_table_path()
        try:
            if self._table_rows:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {"columns": self._table_columns, "rows": self._table_rows},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            elif path.is_file():
                path.unlink()
        except Exception:
            pass

    def _restore_last_table(self) -> None:
        path = self._last_table_path()
        try:
            if not path.is_file():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            cols = raw.get("columns") or []
            rows = raw.get("rows") or []
            if isinstance(cols, list) and isinstance(rows, list) and rows:
                self._show_table([str(c) for c in cols], rows, persist=False)
        except Exception:
            pass

    def _show_table(
        self, columns: list[str], rows: list[list[str]], *, persist: bool = True
    ) -> None:
        self._table_columns = list(columns)
        self._table_rows = [list(row) for row in rows]
        prev_keys = list(getattr(self._table, "_multi_sort_keys", []) or [])
        prev_bases = self._table.property("_multi_sort_base_headers")
        same_cols = (
            isinstance(prev_bases, list)
            and [str(x) for x in prev_bases] == list(columns)
        )
        self._table.clear()
        self._table.setColumnCount(len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.setRowCount(len(rows))
        for r_index, row in enumerate(rows):
            for c_index, value in enumerate(row):
                text = "" if value is None else str(value)
                item = make_table_item(text)
                item.setToolTip(text)
                self._table.setItem(r_index, c_index, item)
        # 依內容調欄寬，不強制截成 20 字，完整顯示報表資料
        self._table.resizeColumnsToContents()
        header = self._table.horizontalHeader()
        for col in range(self._table.columnCount()):
            w = self._table.columnWidth(col)
            self._table.setColumnWidth(col, max(48, min(w + 8, 480)))
        header.setStretchLastSection(False)
        # 點表頭排序；Shift+點可多欄（與排位／馬匹 Grid 相同）
        if columns:
            install_multi_column_sort(
                self._table,
                base_headers=list(columns),
                initial_keys=prev_keys if same_cols else None,
            )
        self._table_label.setText(
            f"報表／列表（{len(rows)} 列 × {len(columns)} 欄）" if rows else "報表／列表"
        )
        if persist:
            self._persist_last_table()

    def focus_input(self) -> None:
        self._input.setFocus()

    def load_prompt(self, prompt: str, *, auto_send: bool = False) -> None:
        """由外部（如賽馬分析）放入指令；auto_send 時立即送出產生報表。"""
        text = (prompt or "").strip()
        if not text:
            return
        self._input.setText(text)
        self._input.setFocus()
        self._status.setText("已放入賽馬分析指令" + ("，正在送出…" if auto_send else ""))
        if auto_send:
            self._send()

    def shutdown(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
        except RuntimeError:
            # C++ QThread 可能已被 deleteLater 銷毀
            pass


class AiAssistantDialog(QDialog):
    """獨立視窗版 AI 助手（Dashboard 等仍可使用）。"""

    def __init__(
        self,
        parent: QWidget | None,
        config: AppConfig,
        action_handler: Callable[[str, str | None], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 助手")
        self.resize(980, 680)
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._panel = AiAssistantPanel(self, config, action_handler=action_handler)
        layout.addWidget(self._panel)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._panel.shutdown()
        super().closeEvent(event)


class _CommandLookupDialog(QDialog):
    def __init__(self, parent: QWidget, db_path: Path) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._selected = ""
        self.setWindowTitle("AI_COMMAND")
        self.resize(720, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("選擇一筆指令，CMD_PROMPT 會放入對話框。"))
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("篩選標題或指令")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["ID", "標題", "CMD_PROMPT"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self._table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        delete_btn = buttons.addButton("刪除", QDialogButtonBox.ButtonRole.ActionRole)
        delete_btn.clicked.connect(self._delete_current)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._reload()

    def _reload(self) -> None:
        self._rows = list_commands(self._db_path)
        self._apply_filter()

    def _apply_filter(self) -> None:
        keyword = self._filter.text().strip().lower()
        shown = []
        for row in self._rows:
            blob = f"{row['CMD_TITLE'] or ''} {row['CMD_PROMPT']}".lower()
            if keyword and keyword not in blob:
                continue
            shown.append(row)
        self._table.setRowCount(len(shown))
        for index, row in enumerate(shown):
            values = [str(row["ID"]), str(row["CMD_TITLE"] or ""), str(row["CMD_PROMPT"] or "")]
            for col, value in enumerate(values):
                self._table.setItem(index, col, QTableWidgetItem(value))
        self._shown = shown

    def _delete_current(self) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._shown):
            return
        command_id = int(self._shown[row]["ID"])
        delete_command(self._db_path, command_id)
        self._reload()

    def accept(self) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(getattr(self, "_shown", [])):
            QMessageBox.information(self, "AI_COMMAND", "請先選取一筆指令。")
            return
        self._selected = str(self._shown[row]["CMD_PROMPT"] or "")
        super().accept()

    def selected_prompt(self) -> str:
        return self._selected


class _SettingsDialog(QDialog):
    def __init__(self, parent: QWidget, settings: AiSettings) -> None:
        super().__init__(parent)
        self.setWindowTitle("模型設定")
        self.resize(520, 220)
        layout = QVBoxLayout(self)
        note = QLabel(
            "賽馬查詢、文件搜尋和報表不需要模型。"
            "只有開放式日常問題才會送到這裡。金鑰留空就只使用本機資料，文件不會外送。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self._base = QLineEdit(settings.base_url)
        self._base.setPlaceholderText("https://api.openai.com/v1")
        self._key = QLineEdit(settings.api_key)
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._model = QLineEdit(settings.model or "gpt-4o-mini")
        form.addRow("服務網址", self._base)
        form.addRow("API 金鑰", self._key)
        form.addRow("模型名稱", self._model)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def settings(self) -> AiSettings:
        return AiSettings(
            base_url=self._base.text().strip(),
            api_key=self._key.text().strip(),
            model=self._model.text().strip() or "gpt-4o-mini",
        )

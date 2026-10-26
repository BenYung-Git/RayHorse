from __future__ import annotations

import sys
import threading
import time
import webbrowser


def _print_help() -> None:
    print(
        "用法：\n"
        "  python -m racing           只開啟 Windows 介面\n"
        "  python -m racing both      同時開啟 Windows 介面 + WEB 伺服器（同 python -m racing.web_app）\n"
        "  python -m racing web       只開啟 WEB 伺服器（同 python -m racing.web_app）\n"
        "  python -m racing --help    顯示說明\n"
        "\n"
        "WEB 位址：http://127.0.0.1:800/app\n"
    )


def _start_web_app_server(*, open_browser: bool = False) -> None:
    """背景啟動與 `python -m racing.web_app` 相同的 Flask（0.0.0.0:800）。"""
    from werkzeug.serving import make_server

    from racing.web_app import app as flask_app
    from racing.web_app import _ensure_download_scheduler

    # both 模式不走 web_app.main()，必須在此啟動間隔下載排程
    _ensure_download_scheduler()

    server = make_server("0.0.0.0", 800, flask_app, threaded=True)

    def _serve() -> None:
        print(">>> WEB：http://127.0.0.1:800/app  （與 python -m racing.web_app 相同）")
        server.serve_forever()

    threading.Thread(target=_serve, name="racing-web-app", daemon=True).start()
    # 等 bind
    for _ in range(30):
        try:
            import urllib.request

            with urllib.request.urlopen("http://127.0.0.1:800/ping", timeout=0.3):
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    if open_browser:
        webbrowser.open("http://127.0.0.1:800/app")


def _run_windows(*, auto_open_web: bool = True) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from racing.config import default_config
    from racing.ui.main_window import MainWindow
    from racing.ui.theme import apply_fusion_style, build_app_stylesheet, theme_by_index

    config = default_config()
    app = QApplication(sys.argv)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    apply_fusion_style(app)
    app.setStyleSheet(build_app_stylesheet(theme_by_index(0)))
    w = MainWindow(config, auto_open_web=auto_open_web)
    w.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    """App entrypoint for `python -m racing` / `python -m racing both`."""
    args = list(sys.argv[1:] if argv is None else argv)
    mode = (args[0].strip().lower() if args else "windows")

    if mode in {"-h", "--help", "help"}:
        _print_help()
        return 0

    if mode in {"web", "web_app", "webapp"}:
        from racing.web_app import main as web_main

        web_main()
        return 0

    if mode == "both":
        # 先啟動與 racing.web_app 相同的伺服器，再開 Windows（不再重複嵌套啟動）
        try:
            _start_web_app_server(open_browser=True)
        except OSError as exc:
            print(f"WEB 伺服器啟動失敗（埠 800 可能已被占用）：{exc}")
            print("仍會開啟 Windows 介面；若 WEB 已在執行可直接使用。")
        return _run_windows(auto_open_web=False)

    if mode not in {"windows", "win", "ui", "desktop"}:
        # 未知參數時顯示說明，避免默默忽略
        if args:
            print(f"未知參數：{args[0]}\n")
            _print_help()
            return 2

    return _run_windows(auto_open_web=False)


if __name__ == "__main__":
    raise SystemExit(main())

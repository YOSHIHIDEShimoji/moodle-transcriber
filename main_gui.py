"""moodle-transcriber GUI フロントエンド (PySide6 / MVP)

- Basic セクション: URL リスト / Model / Format / Output / Start・Stop
- Advanced セクション: --no-transcribe, --timestamps,
  --browser, --save-interval, --keep-interval, --no-auto-routing, --restore-to
- 進捗バー + ログビュー
- 完了時のリネームダイアログ

CLI 互換: 既存の main.run(args) をそのまま QThread で呼び出す。
進捗とリネームは main の hook で受ける。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import signal
import sys
from contextlib import redirect_stdout
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QObject, Signal, Slot, QMetaObject, Q_ARG
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)

import main as _main


# ─── ログビュー向け stdout writer ─────────────────────────────────────────────


class _SignalEmitterWriter:
    """sys.stdout を redirect 先として使う Writer。1行ごとに Qt signal を emit する。"""

    def __init__(self, emitter: "Signal") -> None:
        self._emit = emitter
        self._buf = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buf += data
        # 行が確定するごとに emit。\r も改行扱いにして進捗バー連打を1行ずつ流す。
        while True:
            idx = max(self._buf.find("\n"), self._buf.find("\r"))
            if idx < 0:
                break
            line = self._buf[:idx]
            self._buf = self._buf[idx + 1 :]
            stripped = _strip_ansi(line).rstrip()
            if stripped:
                self._emit(stripped)
        return len(data)

    def flush(self) -> None:
        if self._buf.strip():
            self._emit(_strip_ansi(self._buf).rstrip())
            self._buf = ""


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


# ─── Worker QThread ───────────────────────────────────────────────────────────


class TranscribeWorker(QThread):
    progress = Signal(int, float, float)  # pct, cur_s, total_s
    log = Signal(str)
    finished_ok = Signal(int)
    rename_request = Signal(str)  # path として string を投げる
    error = Signal(str)

    def __init__(self, args: argparse.Namespace, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._args = args
        self._rename_answer: str | None = None
        self._rename_event = None  # threading.Event は run() で生成

    def run(self) -> None:  # noqa: D401 - QThread API
        import threading

        self._rename_event = threading.Event()

        def _on_progress(pct: int, cur_s: float, total_s: float) -> None:
            self.progress.emit(pct, cur_s, total_s)

        def _on_rename(path: Path) -> Path:
            self._rename_answer = None
            self._rename_event.clear()
            self.rename_request.emit(str(path))
            self._rename_event.wait()
            ans = self._rename_answer or str(path)
            return Path(ans)

        _main._progress_hook = _on_progress
        _main._rename_hook = _on_rename
        try:
            with redirect_stdout(_SignalEmitterWriter(self.log.emit)):  # type: ignore[arg-type]
                rc = _main.run(self._args)
            self.finished_ok.emit(rc)
        except SystemExit as e:
            self.finished_ok.emit(int(e.code) if e.code is not None else 0)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            _main._progress_hook = None
            _main._rename_hook = None

    @Slot(str)
    def submit_rename_answer(self, answer: str) -> None:
        """メインスレッドのダイアログから呼び出して結果を返す。"""
        self._rename_answer = answer
        if self._rename_event is not None:
            self._rename_event.set()


# ─── Main Window ──────────────────────────────────────────────────────────────


_MODELS = ["tiny", "base", "small", "medium", "large-v3"]
_FORMATS = ["txt", "srt", "vtt"]
_BROWSERS = ["chrome", "arc", "safari"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("moodle-transcriber")
        self.resize(720, 820)
        self._worker: TranscribeWorker | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(12)

        # ヘッダー
        header_title = QLabel("📚 Moodle Transcriber")
        header_title.setObjectName("headerTitle")
        header_subtitle = QLabel("講義動画を文字起こしして保存")
        header_subtitle.setObjectName("headerSubtitle")
        root.addWidget(header_title)
        root.addWidget(header_subtitle)

        # ─── Basic ──────────────────────────────────────────────────────
        basic = QGroupBox("Basic")
        bl = QVBoxLayout(basic)
        bl.setSpacing(10)

        # URL リスト
        url_label = QLabel("Moodle URL（複数指定可）")
        url_label.setObjectName("fieldLabel")
        self.url_list = QListWidget()
        self.url_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.url_list.setMinimumHeight(80)

        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://moodle.example.com/.../view.php?id=...")
        self.url_input.returnPressed.connect(self._add_url)
        btn_add = QPushButton("＋ 追加")
        btn_add.clicked.connect(self._add_url)
        btn_remove = QPushButton("− 削除")
        btn_remove.clicked.connect(self._remove_url)
        url_row.addWidget(self.url_input, stretch=1)
        url_row.addWidget(btn_add)
        url_row.addWidget(btn_remove)

        bl.addWidget(url_label)
        bl.addWidget(self.url_list)
        bl.addLayout(url_row)

        # モデル / フォーマット / 出力先（QFormLayout で整える）
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)

        self.model_combo = QComboBox()
        self.model_combo.addItems(_MODELS)
        self.model_combo.setCurrentText("large-v3")
        form.addRow("Model", self.model_combo)

        self.format_combo = QComboBox()
        self.format_combo.addItems(_FORMATS)
        form.addRow("Format", self.format_combo)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("(空欄なら out/YYYYMMDD/)")
        btn_browse = QPushButton("📁 参照…")
        btn_browse.clicked.connect(self._pick_output_dir)
        out_row.addWidget(self.output_edit, stretch=1)
        out_row.addWidget(btn_browse)
        out_widget = QWidget()
        out_widget.setLayout(out_row)
        form.addRow("Output Folder", out_widget)

        bl.addLayout(form)

        # Start / Stop
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setObjectName("primaryButton")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.setObjectName("dangerButton")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch(1)
        bl.addLayout(btn_row)

        root.addWidget(basic)

        # ─── Advanced (折りたたみ) ──────────────────────────────────────
        adv = QGroupBox("Advanced")
        adv.setCheckable(True)
        adv.setChecked(False)
        adv_layout = QFormLayout(adv)
        adv_layout.setSpacing(8)

        self.cb_no_transcribe = QCheckBox("文字起こしせず Chrome 自動再生のみ (--no-transcribe)")
        self.cb_timestamps = QCheckBox("タイムスタンプ付与 [HH:MM:SS]")
        self.cb_no_auto_routing = QCheckBox("音声ルーティングの自動切替を無効化")

        adv_layout.addRow(self.cb_no_transcribe)
        adv_layout.addRow(self.cb_timestamps)
        adv_layout.addRow(self.cb_no_auto_routing)

        self.browser_combo = QComboBox()
        self.browser_combo.addItems(_BROWSERS)
        adv_layout.addRow("Browser", self.browser_combo)

        self.save_spin = QSpinBox()
        self.save_spin.setRange(0, 600)
        self.save_spin.setSuffix(" sec")
        self.save_spin.setValue(60)
        adv_layout.addRow("Save interval", self.save_spin)

        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(5, 300)
        self.keep_spin.setSuffix(" sec")
        self.keep_spin.setValue(20)
        adv_layout.addRow("Keep interval", self.keep_spin)

        self.restore_to_edit = QLineEdit()
        self.restore_to_edit.setPlaceholderText("(空欄: 起動前のデバイスに戻す)")
        adv_layout.addRow("Restore to", self.restore_to_edit)

        # 折りたたみ時の中身を隠す（QGroupBox checkable のデフォルト挙動）
        adv.toggled.connect(lambda on: [w.setVisible(on) for w in adv.findChildren(QWidget)])
        root.addWidget(adv)

        # ─── Status ────────────────────────────────────────────────────
        status_box = QGroupBox("Status")
        sl = QVBoxLayout(status_box)

        head = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setStyleSheet("color: #888;")
        self.status_text = QLabel("待機中")
        self.progress_pct = QLabel("")
        head.addWidget(self.status_dot)
        head.addWidget(self.status_text)
        head.addStretch(1)
        head.addWidget(self.progress_pct)
        sl.addLayout(head)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        sl.addWidget(self.progress_bar)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        sl.addWidget(self.log_view, stretch=1)

        root.addWidget(status_box, stretch=1)

        # 折りたたみ初期状態反映
        for w in adv.findChildren(QWidget):
            w.setVisible(False)

        self.setStatusBar(QStatusBar())

    # ─── URL リスト ──────────────────────────────────────────────────────

    def _add_url(self) -> None:
        text = self.url_input.text().strip()
        if not text:
            return
        self.url_list.addItem(text)
        self.url_input.clear()

    def _remove_url(self) -> None:
        for item in self.url_list.selectedItems():
            self.url_list.takeItem(self.url_list.row(item))

    def _collect_urls(self) -> list[str]:
        return [self.url_list.item(i).text() for i in range(self.url_list.count())]

    def _pick_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Output Folder")
        if d:
            self.output_edit.setText(d)

    # ─── 実行 ────────────────────────────────────────────────────────────

    def _build_args(self) -> argparse.Namespace | None:
        urls = self._collect_urls()
        if not urls:
            self._append_log("URLを1件以上追加してください")
            return None

        # build_parser() のデフォルトを土台にする（CLI 互換）
        parser = _main.build_parser()
        args = parser.parse_args([])

        # Basic
        args.urls = urls if len(urls) > 1 else None
        args.moodle_url = urls[0] if len(urls) == 1 else None
        args.url_file = None
        args.model = self.model_combo.currentText()
        args.format = self.format_combo.currentText()
        out = self.output_edit.text().strip()
        args.output = out or None

        # Advanced
        args.no_transcribe = self.cb_no_transcribe.isChecked()
        args.timestamps = self.cb_timestamps.isChecked()
        args.no_auto_routing = self.cb_no_auto_routing.isChecked()
        args.keep_active = self.browser_combo.currentText()
        args.save_interval = float(self.save_spin.value())
        args.keep_interval = float(self.keep_spin.value())
        rt = self.restore_to_edit.text().strip()
        args.restore_to = rt or None

        return args

    def _on_start(self) -> None:
        args = self._build_args()
        if args is None:
            return
        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.progress_pct.setText("")
        self._set_status("running", "起動中…")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self._worker = TranscribeWorker(args)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._append_log)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.rename_request.connect(self._on_rename_request)
        self._worker.start()

    def _on_stop(self) -> None:
        if self._worker is None:
            return
        # main.run 内の SIGINT ハンドラに任せる
        self._append_log("[Stop] SIGINT を送信")
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception as e:  # noqa: BLE001
            self._append_log(f"[Stop] error: {e}")

    @Slot(int, float, float)
    def _on_progress(self, pct: int, cur_s: float, total_s: float) -> None:
        self.progress_bar.setValue(max(0, min(pct, 100)))
        self.progress_pct.setText(f"{pct}%  {_fmt_hms(cur_s)} / {_fmt_hms(total_s)}")
        self._set_status("running", "再生中" if pct < 100 else "視聴完了")

    @Slot(int)
    def _on_finished(self, rc: int) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if rc == 0:
            self._set_status("ok", "完了")
        else:
            self._set_status("error", f"終了コード {rc}")
        self._append_log(f"[finished] rc={rc}")

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_status("error", "エラー")
        self._append_log(f"[error] {msg}")

    @Slot(str)
    def _on_rename_request(self, path_str: str) -> None:
        path = Path(path_str)
        text, ok = QInputDialog.getText(
            self,
            "ファイル名を確認",
            f"出力ファイル名 (ベース): {path.parent}/...{path.suffix}",
            QLineEdit.Normal,
            path.stem,
        )
        if not ok or not text.strip():
            answer = path_str
        else:
            sanitized = _main._sanitize_filename(text.strip())
            answer = str(path.parent / f"{sanitized}{path.suffix}")
        if self._worker is not None:
            QMetaObject.invokeMethod(
                self._worker,
                "submit_rename_answer",
                Qt.QueuedConnection,
                Q_ARG(str, answer),
            )

    # ─── ヘルパ ──────────────────────────────────────────────────────────

    def _set_status(self, kind: str, text: str) -> None:
        color = {"ok": "#2a9d4a", "error": "#c44", "running": "#1f6feb"}.get(kind, "#888")
        self.status_dot.setStyleSheet(f"color: {color};")
        self.status_text.setText(text)

    def _append_log(self, line: str) -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{ts}  {line}")


def _fmt_hms(sec: float) -> str:
    if sec is None or sec < 0:
        return "--:--:--"
    s = int(sec)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ─── エントリポイント ────────────────────────────────────────────────────────


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    qss_path = Path(__file__).with_name("gui_style.qss")
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
    # システムフォント
    families = QFontDatabase.families()
    for cand in ("SF Pro Text", "Helvetica Neue", "Inter"):
        if cand in families:
            app.setFont(QFont(cand, 13))
            break

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

"""
WebConsoleCapture - PySide6 GUI.

A general-purpose desktop tool that captures live console / log output
from any web UI: either via the Chrome DevTools Protocol (CDP) or by
OCR-ing any rectangle on screen.

The UI is organised as a side-nav with three pages (Source, Capture,
Diagnostics) and a persistent live preview + status bar so the window
stays compact and never overflows.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Signal, QObject, Qt, QSize
from PySide6.QtGui import QFont, QTextCursor, QGuiApplication, QColor, QPalette, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QLineEdit, QPlainTextEdit,
    QFileDialog, QMessageBox, QSpinBox, QCheckBox, QStatusBar,
    QDoubleSpinBox, QGroupBox, QFormLayout, QGridLayout, QDialog,
    QDialogButtonBox, QTabWidget, QTextBrowser, QListWidget,
    QListWidgetItem, QStackedWidget, QScrollArea, QSplitter, QFrame,
    QSizePolicy,
)

from app.cdp.client import (
    CDPConsoleClient, list_targets, test_connection,
    BrowserTarget, Diagnostics, pick_element_in_tab, test_selector,
)
from app.logger.file_logger import FileLogger
from app.screen.region_picker import Region, pick_region_async
from app.screen.capture import ScreenOCRWorker
from app.gui.theme import WIN11_QSS, apply_mica


APP_NAME = "WebConsoleCapture"
APP_TAGLINE = "Capture console logs from any web UI"
APP_VERSION = "1.0.0"

DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
DEFAULT_FLAGS = (
    "--remote-debugging-port=9222 "
    "--remote-allow-origins=* "
    '--user-data-dir="%USERPROFILE%\\chrome-debug"'
)


def _find_chrome() -> str:
    for p in DEFAULT_CHROME_PATHS:
        if os.path.exists(p):
            return p
    return DEFAULT_CHROME_PATHS[0]


class _Bridge(QObject):
    line = Signal(str, str)
    status = Signal(str, bool)
    diagnostics = Signal(object)


def _scroll(inner: QWidget) -> QScrollArea:
    """Wrap a widget in a vertical scroll area so pages never overflow."""
    sa = QScrollArea()
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setWidget(inner)
    return sa


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - {APP_TAGLINE}")
        self.resize(1180, 760)
        self.setMinimumSize(960, 600)
        self.setFont(QFont("Segoe UI Variable", 9))

        self.bridge = _Bridge()
        self.bridge.line.connect(self._on_line)
        self.bridge.status.connect(self._on_status)
        self.bridge.diagnostics.connect(self._on_diagnostics)

        self.cdp: CDPConsoleClient | None = None
        self.ocr: ScreenOCRWorker | None = None
        self.logger: FileLogger | None = None
        self.targets: list[BrowserTarget] = []
        self.last_diag: Diagnostics | None = None
        self.region: Region | None = None
        self._region_picker = None

        self._line_buffer: list[tuple[float, str, str]] = []
        self._dedup_first: dict[str, int] = {}
        self._preview_max = 5000

        self._events_prev: int = 0
        self._events_prev_at: float = time.time()
        self._events_rate: float = 0.0

        self._build_ui()
        self._build_menu()
        self._on_mode_changed(0)
        self._refresh_targets()

    # ============================= UI =============================

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("wccRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- Top: live capture status banner ---
        root.addWidget(self._build_status_banner())

        # --- Middle: sidebar nav + stacked pages, splittable with preview ---
        body = QSplitter(Qt.Vertical)
        body.setHandleWidth(6)

        nav_and_pages = QSplitter(Qt.Horizontal)
        nav_and_pages.setHandleWidth(6)
        nav_and_pages.addWidget(self._build_sidebar())
        nav_and_pages.addWidget(self._build_pages())
        nav_and_pages.setStretchFactor(0, 0)
        nav_and_pages.setStretchFactor(1, 1)
        nav_and_pages.setSizes([200, 900])

        body.addWidget(nav_and_pages)
        body.addWidget(self._build_preview_block())
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([440, 280])
        root.addWidget(body, 1)

        # --- Bottom: sticky action bar ---
        root.addWidget(self._build_action_bar())

        # Status bar
        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("Idle")
        self.count_label = QLabel("0 lines")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.count_label)

        # Periodic refresh
        self._stat_timer = QTimer(self)
        self._stat_timer.timeout.connect(self._tick_stats)
        self._stat_timer.start(500)
        self._refresh_diag_view()

    def _build_status_banner(self) -> QWidget:
        cap_bar = QWidget()
        cap_bar.setObjectName("captureStatusBar")
        cap_bar.setStyleSheet(
            "#captureStatusBar { background:#101a2e; border:1px solid #1f2d4a;"
            " border-radius:8px; }")
        lay = QHBoxLayout(cap_bar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(14)

        self.cap_dot = QLabel("\u25CF")
        self.cap_dot.setStyleSheet("color:#6b7280; font-size:18px;")
        lay.addWidget(self.cap_dot)

        self.cap_state_label = QLabel("Idle")
        self.cap_state_label.setStyleSheet(
            "color:#e6ecf5; font-weight:600; font-size:13px;")
        lay.addWidget(self.cap_state_label)

        for txt_attr, init in (
            ("cap_observer_label", "Observer: -"),
            ("cap_events_label", "Events: 0  (0.0/s)"),
            ("cap_last_label", "Last event: -"),
        ):
            sep = QLabel("\u2502")
            sep.setStyleSheet("color:#2a3a5a;")
            lay.addWidget(sep)
            lab = QLabel(init)
            lab.setStyleSheet(
                "color:#a8b0bd; font-family: Consolas, monospace;")
            setattr(self, txt_attr, lab)
            lay.addWidget(lab)

        lay.addStretch(1)
        title = QLabel(f"<b style='color:#e6ecf5'>{APP_NAME}</b> "
                       f"<span style='color:#6b7d99'>v{APP_VERSION}</span>")
        lay.addWidget(title)
        return cap_bar

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("wccSidebar")
        side.setStyleSheet(
            "#wccSidebar { background:#0d1730; border:1px solid #1f2d4a;"
            " border-radius:8px; } "
            "QListWidget { background: transparent; border: none;"
            " padding:6px; outline:0; } "
            "QListWidget::item { padding:10px 12px; border-radius:6px;"
            " color:#cfd6e4; margin:2px 4px; } "
            "QListWidget::item:selected { background:#1d2c52;"
            " color:#ffffff; }")
        side.setMinimumWidth(180)
        side.setMaximumWidth(260)
        v = QVBoxLayout(side)
        v.setContentsMargins(6, 8, 6, 8)
        v.setSpacing(6)

        hdr = QLabel("NAVIGATION")
        hdr.setStyleSheet(
            "color:#6b7d99; font-size:10px; font-weight:700;"
            " letter-spacing:1px; padding:4px 12px;")
        v.addWidget(hdr)

        self.nav = QListWidget()
        for label in ("Source", "Capture", "Diagnostics"):
            QListWidgetItem(label, self.nav)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(
            lambda i: self.pages.setCurrentIndex(i))
        v.addWidget(self.nav, 1)

        footer = QLabel(
            "<span style='color:#6b7d99'>by</span> "
            "<b style='color:#a8b8d6'>Amin Adineh</b>")
        footer.setStyleSheet("padding:8px 12px; font-size:11px;")
        v.addWidget(footer)
        return side

    def _build_pages(self) -> QStackedWidget:
        self.pages = QStackedWidget()
        self.pages.addWidget(_scroll(self._build_source_page()))
        self.pages.addWidget(_scroll(self._build_capture_page()))
        self.pages.addWidget(_scroll(self._build_diagnostics_page()))
        return self.pages

    # ---- Page 1: Source ----

    def _build_source_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        # Capture mode
        mode_box = QGroupBox("Capture mode")
        mh = QHBoxLayout(mode_box)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(
            "Chrome console (CDP - fastest, exact text)", userData="cdp")
        self.mode_combo.addItem(
            "Screen region (OCR - any app / web UI)", userData="ocr")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mh.addWidget(self.mode_combo, 1)
        v.addWidget(mode_box)

        # Chrome launch
        launch_box = QGroupBox("Chrome launch")
        lf = QFormLayout(launch_box)
        self.chrome_path_edit = QLineEdit(_find_chrome())
        path_row = QHBoxLayout()
        path_row.addWidget(self.chrome_path_edit, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse_chrome)
        path_row.addWidget(b)
        pw = QWidget(); pw.setLayout(path_row)
        lf.addRow("Chrome path:", pw)

        self.flags_edit = QLineEdit(DEFAULT_FLAGS)
        self.flags_edit.setToolTip(
            "Required flags: --remote-debugging-port=<port> AND "
            "--remote-allow-origins=*  (Chrome 111+).")
        lf.addRow("Flags:", self.flags_edit)

        btns = QHBoxLayout()
        self.launch_btn = QPushButton("Launch Chrome")
        self.launch_btn.clicked.connect(self._launch_chrome)
        btns.addWidget(self.launch_btn)
        self.copy_cmd_btn = QPushButton("Copy command")
        self.copy_cmd_btn.clicked.connect(self._copy_chrome_command)
        btns.addWidget(self.copy_cmd_btn)
        btns.addStretch(1)
        bw = QWidget(); bw.setLayout(btns)
        lf.addRow("", bw)

        self.launch_hint = QLabel(
            "Close all Chrome windows before launching - Chrome ignores "
            "the debug flag if another instance is already using the "
            "same profile.")
        self.launch_hint.setWordWrap(True)
        self.launch_hint.setStyleSheet("color: #9fb0cc;")
        lf.addRow("", self.launch_hint)
        v.addWidget(launch_box)

        # Connection
        conn_box = QGroupBox("Connection")
        cf = QFormLayout(conn_box)
        host_row = QHBoxLayout()
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setMaximumWidth(160)
        host_row.addWidget(self.host_edit)
        host_row.addWidget(QLabel("Port:"))
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(9222)
        host_row.addWidget(self.port_edit)
        host_row.addStretch(1)
        hw = QWidget(); hw.setLayout(host_row)
        cf.addRow("Host:", hw)

        tab_row = QHBoxLayout()
        self.tab_combo = QComboBox()
        self.tab_combo.setMinimumWidth(360)
        tab_row.addWidget(self.tab_combo, 1)
        self.refresh_btn = QPushButton("Refresh tabs")
        self.refresh_btn.clicked.connect(self._refresh_targets)
        tab_row.addWidget(self.refresh_btn)
        self.test_btn = QPushButton("Test CDP connection")
        self.test_btn.clicked.connect(self._test_connection)
        tab_row.addWidget(self.test_btn)
        tw = QWidget(); tw.setLayout(tab_row)
        cf.addRow("Tab:", tw)
        v.addWidget(conn_box)

        # DOM selector
        dom_box = QGroupBox("Page area (optional CSS selector)")
        df = QFormLayout(dom_box)
        self.selector_edit = QLineEdit()
        self.selector_edit.setPlaceholderText(
            'e.g. [data-testid="console"]  (iframes searched automatically)')
        df.addRow("Selector:", self.selector_edit)

        srow = QHBoxLayout()
        self.pick_elem_btn = QPushButton("Pick on page...")
        self.pick_elem_btn.clicked.connect(self._pick_element_on_page)
        srow.addWidget(self.pick_elem_btn)
        self.test_sel_btn = QPushButton("Test selector")
        self.test_sel_btn.clicked.connect(self._test_selector)
        srow.addWidget(self.test_sel_btn)
        self.live_check = QCheckBox("Live (MutationObserver)")
        self.live_check.setChecked(True)
        srow.addWidget(self.live_check)
        srow.addWidget(QLabel("Poll (ms):"))
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(100, 5000)
        self.poll_spin.setSingleStep(100)
        self.poll_spin.setValue(500)
        srow.addWidget(self.poll_spin)
        srow.addStretch(1)
        sw = QWidget(); sw.setLayout(srow)
        df.addRow("", sw)
        v.addWidget(dom_box)
        self.live_check.toggled.connect(
            lambda on: self.poll_spin.setEnabled(not on))
        self.poll_spin.setEnabled(False)

        # OCR region
        ocr_box = QGroupBox("Screen region (OCR mode)")
        of = QFormLayout(ocr_box)
        rrow = QHBoxLayout()
        self.region_pick_btn = QPushButton("Pick screen region...")
        self.region_pick_btn.clicked.connect(self._pick_region)
        rrow.addWidget(self.region_pick_btn)
        self.region_label = QLabel("No region selected")
        self.region_label.setStyleSheet("color:#9fb0cc;")
        rrow.addWidget(self.region_label, 1)
        rrow.addWidget(QLabel("OCR poll (ms):"))
        self.ocr_poll_spin = QSpinBox()
        self.ocr_poll_spin.setRange(100, 5000)
        self.ocr_poll_spin.setSingleStep(100)
        self.ocr_poll_spin.setValue(400)
        rrow.addWidget(self.ocr_poll_spin)
        rw = QWidget(); rw.setLayout(rrow)
        of.addRow("Region:", rw)
        self.region_row_widget = ocr_box
        v.addWidget(ocr_box)

        v.addStretch(1)
        return page

    # ---- Page 2: Capture (output settings) ----

    def _build_capture_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        out_box = QGroupBox("Log output")
        of = QFormLayout(out_box)
        path_row = QHBoxLayout()
        default_path = str(Path.cwd() / "console_log.txt")
        self.path_edit = QLineEdit(default_path)
        path_row.addWidget(self.path_edit, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse_log)
        path_row.addWidget(b)
        pw = QWidget(); pw.setLayout(path_row)
        of.addRow("Log file:", pw)

        self.ts_check = QCheckBox("Prefix each line with a timestamp")
        self.ts_check.setChecked(True)
        of.addRow("Timestamps:", self.ts_check)

        self.flush_spin = QDoubleSpinBox()
        self.flush_spin.setRange(0.2, 30.0)
        self.flush_spin.setSingleStep(0.5)
        self.flush_spin.setValue(1.0)
        self.flush_spin.setSuffix(" s")
        of.addRow("Flush every:", self.flush_spin)
        v.addWidget(out_box)

        prev_box = QGroupBox("Preview options")
        pf = QFormLayout(prev_box)
        opts = QHBoxLayout()
        self.dedup_check = QCheckBox("Deduplicate")
        self.dedup_check.setChecked(True)
        self.dedup_check.toggled.connect(self._rerender_preview)
        opts.addWidget(self.dedup_check)
        self.show_ts_check = QCheckBox("Show timestamps")
        self.show_ts_check.setChecked(True)
        self.show_ts_check.toggled.connect(self._rerender_preview)
        opts.addWidget(self.show_ts_check)
        self.autoscroll_check = QCheckBox("Auto-scroll")
        self.autoscroll_check.setChecked(True)
        opts.addWidget(self.autoscroll_check)
        opts.addStretch(1)
        ow = QWidget(); ow.setLayout(opts)
        pf.addRow("Display:", ow)
        v.addWidget(prev_box)

        v.addStretch(1)
        return page

    # ---- Page 3: Diagnostics ----

    def _build_diagnostics_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        diag_box = QGroupBox("Connection diagnostics")
        grid = QGridLayout(diag_box)
        self.diag_labels: dict[str, QLabel] = {}
        for i, (key, caption) in enumerate([
            ("state", "State"),
            ("host", "Host"),
            ("port", "Port"),
            ("ws", "WebSocket URL"),
            ("attempt", "Reconnect attempts"),
            ("retry", "Next retry in"),
            ("events", "Events received"),
            ("last_error", "Last error"),
        ]):
            row, col = i // 2, (i % 2) * 2
            cap = QLabel(f"{caption}:")
            cap.setProperty("muted", True)
            val = QLabel("-")
            val.setStyleSheet("font-family: Consolas, monospace;")
            val.setWordWrap(True)
            grid.addWidget(cap, row, col)
            grid.addWidget(val, row, col + 1)
            self.diag_labels[key] = val
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        v.addWidget(diag_box)

        sel_box = QGroupBox("Selector / MutationObserver")
        sgrid = QGridLayout(sel_box)
        self.seldiag_labels: dict[str, QLabel] = {}
        for i, (key, caption) in enumerate([
            ("sel", "CSS selector"),
            ("matches", "Matches"),
            ("node", "Matched node(s)"),
            ("observer", "Observer status"),
            ("fires", "Observer fires"),
            ("last_fire", "Last fire"),
        ]):
            row, col = i // 2, (i % 2) * 2
            cap = QLabel(f"{caption}:")
            cap.setProperty("muted", True)
            val = QLabel("-")
            val.setStyleSheet("font-family: Consolas, monospace;")
            val.setWordWrap(True)
            sgrid.addWidget(cap, row, col)
            sgrid.addWidget(val, row, col + 1)
            self.seldiag_labels[key] = val
        sgrid.setColumnStretch(1, 1)
        sgrid.setColumnStretch(3, 1)
        v.addWidget(sel_box)

        v.addStretch(1)
        return page

    # ---- Preview block (persistent, bottom of body) ----

    def _build_preview_block(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("<b>Live preview</b>"))
        hdr.addStretch(1)
        self.preview_count_label = QLabel("0 raw / 0 unique")
        self.preview_count_label.setProperty("muted", True)
        hdr.addWidget(self.preview_count_label)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_preview)
        hdr.addWidget(clear_btn)
        v.addLayout(hdr)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumBlockCount(self._preview_max)
        self.preview.setFont(QFont("Consolas", 9))
        v.addWidget(self.preview, 1)
        return wrap

    # ---- Sticky action bar ----

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("wccActionBar")
        bar.setStyleSheet(
            "#wccActionBar { background:#101a2e; border:1px solid #1f2d4a;"
            " border-radius:8px; }")
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(8)
        h.addStretch(1)
        self.start_btn = QPushButton("Start capture")
        self.start_btn.setProperty("accent", True)
        self.start_btn.setMinimumWidth(140)
        self.start_btn.clicked.connect(self._start)
        h.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setProperty("danger", True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setMinimumWidth(100)
        self.stop_btn.clicked.connect(self._stop)
        h.addWidget(self.stop_btn)
        self.export_btn = QPushButton("Export CSV...")
        self.export_btn.clicked.connect(self._export_csv)
        h.addWidget(self.export_btn)
        return bar

    # =========================== actions ===========================

    def _browse_chrome(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate chrome.exe", self.chrome_path_edit.text(),
            "Chrome (chrome.exe);;All files (*.*)")
        if path:
            self.chrome_path_edit.setText(path)

    def _chrome_command(self) -> str:
        path = self.chrome_path_edit.text().strip()
        flags = self.flags_edit.text().strip()
        quoted = f'"{path}"' if " " in path and not path.startswith('"') else path
        return f"{quoted} {flags}".strip()

    def _copy_chrome_command(self) -> None:
        QGuiApplication.clipboard().setText(self._chrome_command())
        self._set_status("Chrome launch command copied to clipboard.", True)

    def _launch_chrome(self) -> None:
        path = self.chrome_path_edit.text().strip()
        if not os.path.exists(path):
            QMessageBox.warning(
                self, "Chrome not found",
                f"chrome.exe not found at:\n{path}\n\nClick Browse... to locate it.")
            return
        flags = [os.path.expandvars(a) for a in
                 _split_flags(self.flags_edit.text())]
        try:
            subprocess.Popen([path] + flags, close_fds=True)
        except Exception as e:
            QMessageBox.critical(self, "Launch failed", str(e))
            return
        self._set_status(
            "Launching Chrome - give it a couple of seconds, then "
            "press Refresh tabs.", True)
        QTimer.singleShot(2500, self._refresh_targets)

    def _browse_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose log file", self.path_edit.text(),
            "Text files (*.txt);;All files (*.*)")
        if path:
            self.path_edit.setText(path)

    def _refresh_targets(self) -> None:
        self.tab_combo.clear()
        host = self.host_edit.text().strip() or "127.0.0.1"
        port = int(self.port_edit.value())
        try:
            self.targets = list_targets(host, port)
        except Exception as e:
            self.targets = []
            self._set_status(f"Cannot reach Chrome at {host}:{port}: {e}", False)
            return
        if not self.targets:
            self._set_status("Chrome is reachable but no tabs are open.", False)
            return
        for t in self.targets:
            self.tab_combo.addItem(t.label(), userData=t)
        self._set_status(
            f"Found {len(self.targets)} tab(s). Pick one and press Start.", True)

    def _test_connection(self) -> None:
        idx = self.tab_combo.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "No tab", "Refresh tabs and pick one first.")
            return
        target: BrowserTarget = self.tab_combo.itemData(idx)
        host = self.host_edit.text().strip() or "127.0.0.1"
        port = int(self.port_edit.value())
        self._set_status("Testing CDP connection...", True)
        self.test_btn.setEnabled(False)
        QApplication.processEvents()
        try:
            res = test_connection(host, port, target.ws_url)
        finally:
            self.test_btn.setEnabled(True)
        self._set_status(res.summary, res.ok)
        body = res.summary
        if res.detail:
            body += f"\n\n{res.detail}"
        if res.events_seen:
            body += f"\n\nEvents received during test: {res.events_seen}"
        (QMessageBox.information if res.ok else QMessageBox.warning)(
            self, "CDP connection test", body)

    def _current_mode(self) -> str:
        return self.mode_combo.currentData() or "cdp"

    def _on_mode_changed(self, _idx: int) -> None:
        ocr = self._current_mode() == "ocr"
        self.region_row_widget.setVisible(ocr)
        for w in (self.tab_combo, self.refresh_btn, self.test_btn,
                  self.selector_edit, self.poll_spin,
                  self.pick_elem_btn, self.test_sel_btn,
                  self.live_check):
            w.setEnabled(not ocr)
        if not ocr:
            self.poll_spin.setEnabled(not self.live_check.isChecked())
        self._set_status(
            "Screen-OCR mode: pick a region, then press Start." if ocr else
            "CDP mode: pick a Chrome tab, then press Start.", True)

    def _pick_region(self) -> None:
        self.showMinimized()
        QTimer.singleShot(250, self._show_picker)

    def _show_picker(self) -> None:
        self._region_picker = pick_region_async(self._on_region_picked)

    def _on_region_picked(self, region) -> None:
        self.showNormal(); self.raise_(); self.activateWindow()
        if region is None:
            self._set_status("Region selection cancelled.", False)
            return
        self.region = region
        self.region_label.setText(f"Region: {region}")
        self._set_status(
            f"Region captured ({region}). Press Start to begin OCR.", True)

    def _start(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "No file", "Pick an output file.")
            return
        self.logger = FileLogger(
            path=path,
            flush_interval=float(self.flush_spin.value()),
            timestamps=self.ts_check.isChecked(),
        )
        self.logger.start()
        mode = self._current_mode()
        if mode == "ocr":
            if self.region is None:
                QMessageBox.warning(
                    self, "No region",
                    "Click 'Pick screen region...' and drag a rectangle.")
                self.logger.stop(); self.logger = None
                return
            self.ocr = ScreenOCRWorker(
                region=self.region, on_line=self._enqueue_line,
                on_status=self._enqueue_status,
                poll_ms=int(self.ocr_poll_spin.value()))
            self.ocr.start()
            self.preview.appendPlainText(
                f"--- Capturing screen region: {self.region} ---")
        else:
            idx = self.tab_combo.currentIndex()
            if idx < 0:
                QMessageBox.warning(self, "No tab", "Pick a Chrome tab first.")
                self.logger.stop(); self.logger = None
                return
            target: BrowserTarget = self.tab_combo.itemData(idx)
            self.cdp = CDPConsoleClient(
                host=self.host_edit.text().strip() or "127.0.0.1",
                port=int(self.port_edit.value()),
                target=target,
                on_line=self._enqueue_line,
                on_status=self._enqueue_status,
                on_diagnostics=self._enqueue_diag,
                dom_selector=self.selector_edit.text(),
                dom_poll_ms=int(self.poll_spin.value()),
                use_mutation_observer=self.live_check.isChecked())
            self.cdp.start()
            self.preview.appendPlainText(f"--- Capturing: {target.label()} ---")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        for w in (self.mode_combo, self.tab_combo, self.refresh_btn,
                  self.path_edit, self.selector_edit, self.pick_elem_btn,
                  self.test_sel_btn, self.live_check, self.region_pick_btn):
            w.setEnabled(False)

    def _pick_element_on_page(self) -> None:
        if self.cdp is not None:
            QMessageBox.information(self, "Stop monitoring first",
                "Stop the current capture before picking an element.")
            return
        idx = self.tab_combo.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "No tab", "Pick a Chrome tab first.")
            return
        target: BrowserTarget = self.tab_combo.itemData(idx)
        self._set_status(
            "Switch to Chrome and click the panel you want to capture (Esc to cancel).",
            True)
        self.pick_elem_btn.setEnabled(False)
        QApplication.processEvents()
        try:
            res = pick_element_in_tab(target.ws_url, timeout_s=60.0)
        finally:
            self.pick_elem_btn.setEnabled(
                self._current_mode() == "cdp" and self.cdp is None)
        if not res.ok:
            self._set_status(f"Pick cancelled: {res.error}", False)
            return
        self.selector_edit.setText(res.selector)
        self._set_status(f"Selector captured: {res.selector}", True)
        QMessageBox.information(
            self, "Element selected",
            f"CSS selector:\n\n{res.selector}\n\nPress Start to begin.")

    def _test_selector(self) -> None:
        if self.cdp is not None:
            QMessageBox.information(self, "Stop monitoring first",
                "Stop the current capture before testing the selector.")
            return
        sel = self.selector_edit.text().strip()
        if not sel:
            QMessageBox.warning(self, "No selector",
                "Enter a CSS selector first, or use 'Pick on page'.")
            return
        idx = self.tab_combo.currentIndex()
        if idx < 0:
            QMessageBox.warning(self, "No tab", "Pick a Chrome tab first.")
            return
        target: BrowserTarget = self.tab_combo.itemData(idx)
        self.test_sel_btn.setEnabled(False)
        self._set_status(f"Testing '{sel}' (watching ~1.5s)...", True)
        QApplication.processEvents()
        try:
            res = test_selector(target.ws_url, sel, max_lines=8, settle_s=1.5)
        finally:
            self.test_sel_btn.setEnabled(
                self._current_mode() == "cdp" and self.cdp is None)
        if not res.ok:
            self._set_status(f"Selector test failed: {res.error}", False)
            QMessageBox.warning(self, "Selector test failed",
                                res.error or "No element matched.")
            return
        self.preview.appendPlainText(
            f"--- Selector test: {sel}  (matches: {res.count}) ---")
        if res.samples:
            for ln in res.samples:
                self.preview.appendPlainText(f"[SAMPLE] {ln}")
        else:
            self.preview.appendPlainText(
                "[SAMPLE] (element matched, but no text yet)")
        self.preview.moveCursor(QTextCursor.End)
        self._set_status(
            f"Selector OK: {res.count} match(es), {len(res.samples)} sample line(s).",
            True)

    def _stop(self) -> None:
        if self.cdp:
            self.cdp.stop(); self.cdp = None
        if self.ocr:
            self.ocr.stop(); self.ocr = None
        if self.logger:
            self.logger.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.mode_combo.setEnabled(True)
        self._on_mode_changed(self.mode_combo.currentIndex())
        self.path_edit.setEnabled(True)
        self.region_pick_btn.setEnabled(True)
        self._set_status("Stopped.", False)

    def _export_csv(self) -> None:
        if not self.logger:
            QMessageBox.information(
                self, "Nothing to export",
                "Start a capture first - there are no logs to export.")
            return
        default = Path(self.path_edit.text()).with_suffix("")
        default = f"{default}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export logs to CSV", default,
            "CSV files (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            n = self.logger.export_csv(path)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        self._set_status(f"Exported {n} rows to {path}", True)

    # ----- thread-safe bridges -----

    def _enqueue_line(self, level: str, message: str) -> None:
        if self.logger:
            self.logger.add(level, message)
        self.bridge.line.emit(level, message)

    def _enqueue_status(self, text: str, ok: bool) -> None:
        self.bridge.status.emit(text, ok)

    def _enqueue_diag(self, diag: Diagnostics) -> None:
        self.bridge.diagnostics.emit(diag)

    # ----- preview -----

    def _format_line(self, ts: float, level: str, message: str,
                     unique: bool = True) -> str:
        parts = []
        if self.show_ts_check.isChecked():
            parts.append(datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3])
        parts.append(f"[{level}]")
        if not unique and self.dedup_check.isChecked():
            parts.append("(dup)")
        parts.append(message)
        return " ".join(parts)

    def _on_line(self, level: str, message: str) -> None:
        ts = time.time()
        idx = len(self._line_buffer)
        self._line_buffer.append((ts, level, message))
        first = self._dedup_first.setdefault(message, idx)
        unique = (first == idx)
        if len(self._line_buffer) > self._preview_max * 2:
            drop = len(self._line_buffer) - self._preview_max
            self._line_buffer = self._line_buffer[drop:]
            self._dedup_first = {}
            for i, (_, _, m) in enumerate(self._line_buffer):
                self._dedup_first.setdefault(m, i)
        if self.dedup_check.isChecked() and not unique:
            self._update_preview_count(); return
        self.preview.appendPlainText(self._format_line(ts, level, message, unique))
        if self.autoscroll_check.isChecked():
            self.preview.moveCursor(QTextCursor.End)
        self._update_preview_count()

    def _update_preview_count(self) -> None:
        self.preview_count_label.setText(
            f"{len(self._line_buffer)} raw / {len(self._dedup_first)} unique")

    def _rerender_preview(self) -> None:
        self.preview.clear()
        dedup = self.dedup_check.isChecked()
        for i, (ts, level, message) in enumerate(self._line_buffer):
            unique = self._dedup_first.get(message, i) == i
            if dedup and not unique:
                continue
            self.preview.appendPlainText(self._format_line(ts, level, message, unique))
        if self.autoscroll_check.isChecked():
            self.preview.moveCursor(QTextCursor.End)
        self._update_preview_count()

    def _clear_preview(self) -> None:
        self._line_buffer.clear()
        self._dedup_first.clear()
        self.preview.clear()
        self._update_preview_count()

    def _on_status(self, text: str, ok: bool) -> None:
        self._set_status(text, ok)

    def _on_diagnostics(self, diag: Diagnostics) -> None:
        self.last_diag = diag
        self._refresh_diag_view()

    def _set_status(self, text: str, ok: bool) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "color:#7ee7a0;" if ok else "color:#ff8a8a;")

    def _tick_stats(self) -> None:
        if self.logger:
            self.count_label.setText(f"{self.logger.lines_written()} lines")
        d = self.last_diag
        if d is not None:
            now = time.time()
            dt = max(0.001, now - self._events_prev_at)
            delta = max(0, d.events_received - self._events_prev)
            inst = delta / dt
            self._events_rate = 0.6 * self._events_rate + 0.4 * inst
            self._events_prev = d.events_received
            self._events_prev_at = now
        self._refresh_diag_view()
        self._refresh_capture_banner()

    def _refresh_capture_banner(self) -> None:
        d = self.last_diag
        cdp_running = self.cdp is not None
        ocr_running = self.ocr is not None
        if not cdp_running and not ocr_running:
            self.cap_dot.setStyleSheet("color:#6b7280; font-size:18px;")
            self.cap_state_label.setText("Idle  -  not capturing")
            self.cap_state_label.setStyleSheet(
                "color:#a8b0bd; font-weight:600; font-size:13px;")
            self.cap_observer_label.setText("Observer: -")
            self.cap_events_label.setText("Events: 0  (0.0/s)")
            self.cap_last_label.setText("Last event: -")
            return
        if ocr_running and not cdp_running:
            self.cap_dot.setStyleSheet("color:#7ee7a0; font-size:18px;")
            self.cap_state_label.setText("Capturing  -  screen OCR")
            self.cap_state_label.setStyleSheet(
                "color:#7ee7a0; font-weight:600; font-size:13px;")
            self.cap_observer_label.setText("Observer: n/a (OCR)")
            n = self.logger.lines_written() if self.logger else 0
            self.cap_events_label.setText(f"Lines: {n}")
            self.cap_last_label.setText("Last event: -")
            return
        state = (d.state if d else "connecting")
        color = {
            "connected": "#7ee7a0", "connecting": "#f0c674",
            "reconnecting": "#f0c674", "idle": "#a8b0bd",
            "stopped": "#a8b0bd",
        }.get(state, "#ff8a8a")
        if state == "connected":
            label = "Capturing live"
        elif state == "reconnecting":
            wait = d.next_retry_in if d else 0.0
            label = f"Reconnecting (attempt #{d.attempt if d else 0}, retry in {wait:.1f}s)"
        elif state == "connecting":
            label = "Connecting to Chrome..."
        else:
            label = state.capitalize()
        self.cap_dot.setStyleSheet(f"color:{color}; font-size:18px;")
        self.cap_state_label.setText(label)
        self.cap_state_label.setStyleSheet(
            f"color:{color}; font-weight:600; font-size:13px;")
        if d is None:
            self.cap_observer_label.setText("Observer: -")
        elif not self.selector_edit.text().strip():
            self.cap_observer_label.setText("Observer: off (console only)")
        elif d.observer_installed:
            self.cap_observer_label.setText(
                f"Observer: attached  -  {d.observer_fires} fires")
            self.cap_observer_label.setStyleSheet(
                "color:#7ee7a0; font-family: Consolas, monospace;")
        else:
            self.cap_observer_label.setText(
                "Observer: retrying  -  DOM polling fallback")
            self.cap_observer_label.setStyleSheet(
                "color:#f0c674; font-family: Consolas, monospace;")
        ev = d.events_received if d else 0
        self.cap_events_label.setText(f"Events: {ev}  ({self._events_rate:.1f}/s)")
        if d and d.last_fire_at > 0:
            age = time.time() - d.last_fire_at
            self.cap_last_label.setText(f"Last event: {age:.1f}s ago")
        else:
            self.cap_last_label.setText("Last event: -")

    def _refresh_diag_view(self) -> None:
        d = self.last_diag
        labels = self.diag_labels
        sl = self.seldiag_labels
        sl["sel"].setText(self.selector_edit.text().strip() or "-")
        if d is None:
            for k in ("state", "host", "port", "ws", "attempt", "retry",
                      "events", "last_error"):
                labels[k].setText("-")
            labels["state"].setText("idle")
            for k in ("matches", "node", "observer", "fires", "last_fire"):
                sl[k].setText("-")
            return
        color = {
            "connected": "#7ee7a0", "connecting": "#f0c674",
            "reconnecting": "#f0c674", "idle": "#a8b0bd",
            "stopped": "#a8b0bd",
        }.get(d.state, "#ff8a8a")
        labels["state"].setText(d.state)
        labels["state"].setStyleSheet(
            f"color:{color}; font-family: Consolas, monospace;")
        labels["host"].setText(d.host or "-")
        labels["port"].setText(str(d.port or "-"))
        labels["ws"].setText(d.ws_url or "-")
        labels["attempt"].setText(str(d.attempt))
        if d.state == "reconnecting" and d.next_retry_in > 0:
            labels["retry"].setText(f"{d.next_retry_in:.1f} s")
        else:
            labels["retry"].setText("-")
        labels["events"].setText(str(d.events_received))
        if d.last_error:
            age = max(0, int(time.time() - d.last_error_at))
            labels["last_error"].setText(f"{d.last_error}  ({age}s ago)")
            labels["last_error"].setStyleSheet(
                "color:#ff8a8a; font-family: Consolas, monospace;")
        else:
            labels["last_error"].setText("-")
            labels["last_error"].setStyleSheet(
                "font-family: Consolas, monospace;")
        sl["matches"].setText(str(d.observer_match_count))
        sl["node"].setText(d.observer_node_info or "-")
        if d.observer_installed:
            sl["observer"].setText("attached (live)")
            sl["observer"].setStyleSheet(
                "color:#7ee7a0; font-family: Consolas, monospace;")
        elif self.cdp is not None and self.selector_edit.text().strip():
            sl["observer"].setText("not attached")
            sl["observer"].setStyleSheet(
                "color:#f0c674; font-family: Consolas, monospace;")
        else:
            sl["observer"].setText("-")
            sl["observer"].setStyleSheet("font-family: Consolas, monospace;")
        sl["fires"].setText(str(d.observer_fires))
        if d.last_fire_at > 0:
            age = time.time() - d.last_fire_at
            sl["last_fire"].setText(f"{age:.1f} s ago")
        else:
            sl["last_fire"].setText("-")

    def closeEvent(self, event) -> None:
        try:
            self._stop()
        finally:
            super().closeEvent(event)

    # ----- menu -----

    def _build_menu(self) -> None:
        mb = self.menuBar()
        help_menu = mb.addMenu("&Help")
        help_menu.addAction("How to use").triggered.connect(
            lambda: self._open_help_dialog("how"))
        help_menu.addAction("About").triggered.connect(
            lambda: self._open_help_dialog("about"))
        help_menu.addAction("License").triggered.connect(
            lambda: self._open_help_dialog("license"))

    def _open_help_dialog(self, tab: str = "about") -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Help - {APP_NAME}")
        dlg.resize(720, 560)
        lay = QVBoxLayout(dlg)
        tabs = QTabWidget(dlg)

        how = QTextBrowser(); how.setOpenExternalLinks(True)
        how.setHtml(f"""
        <h2>How to use {APP_NAME}</h2>
        <p>{APP_NAME} captures live console output from any web UI, either via the
        Chrome DevTools Protocol or by OCR-ing a region of your screen.</p>
        <h3>1. Pick a source</h3>
        <ul>
          <li><b>Source</b> page &rarr; choose <i>Chrome console (CDP)</i> or
              <i>Screen region (OCR)</i>.</li>
          <li><b>CDP</b>: launch Chrome with the bundled flags, refresh tabs, pick one.</li>
          <li><b>OCR</b>: drag a rectangle over the area you want to watch.</li>
        </ul>
        <h3>2. Configure output</h3>
        <ul>
          <li><b>Capture</b> page &rarr; choose the log file path, timestamp + flush options.</li>
        </ul>
        <h3>3. Start &amp; monitor</h3>
        <ul>
          <li>Click <b>Start capture</b> at the bottom. The status banner shows live state.</li>
          <li>Open <b>Diagnostics</b> for connection and selector details.</li>
        </ul>
        """)
        tabs.addTab(how, "How to use")

        about = QTextBrowser(); about.setOpenExternalLinks(True)
        about.setHtml(f"""
        <h2>{APP_NAME} v{APP_VERSION}</h2>
        <p>{APP_TAGLINE}.</p>
        <p><b>{APP_NAME}</b> is a desktop tool that captures live console / log
        output from any web UI via the Chrome DevTools Protocol or on-screen OCR.</p>
        <h3>Author</h3>
        <p><b>Amin Adineh</b><br/>Brandenburg University of Technology<br/>
        Cottbus - Senftenberg, Germany</p>
        <h3>Built with</h3>
        <p>Python, PySide6 (Qt), Chrome DevTools Protocol, RapidOCR.</p>
        <p style="color:#888;">&copy; 2026 Amin Adineh.</p>
        """)
        tabs.addTab(about, "About")

        lic = QTextBrowser(); lic.setOpenExternalLinks(True)
        lic.setHtml(f"""
        <h2>License</h2>
        <p><b>{APP_NAME}</b><br/>Copyright &copy; 2026 Amin Adineh<br/>
        Released under the MIT License - see <code>LICENSE</code>.</p>
        <h3>Third-party</h3>
        <ul>
          <li>PySide6 - LGPL v3</li>
          <li>websocket-client, requests, mss, rapidocr-onnxruntime, Pillow - their respective licenses</li>
        </ul>
        """)
        tabs.addTab(lic, "License")

        tabs.setCurrentIndex({"how": 0, "about": 1, "license": 2}.get(tab, 1))
        lay.addWidget(tabs, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject); btns.accepted.connect(dlg.accept)
        btns.button(QDialogButtonBox.Close).clicked.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()


def _split_flags(s: str) -> list[str]:
    import shlex
    try:
        return shlex.split(s, posix=False)
    except Exception:
        return s.split()


def _app_icon() -> QIcon:
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "app" / "assets" / "icon.ico")
        candidates.append(Path(base) / "app" / "assets" / "icon.png")
    here = Path(__file__).resolve().parent.parent
    candidates.append(here / "assets" / "icon.ico")
    candidates.append(here / "assets" / "icon.png")
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "assets" / "icon.ico")
    candidates.append(exe_dir / "assets" / "icon.png")
    for c in candidates:
        if c.is_file():
            return QIcon(str(c))
    return QIcon()


def run() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    _icon = _app_icon()
    if not _icon.isNull():
        app.setWindowIcon(_icon)
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(13, 20, 38))
    pal.setColor(QPalette.WindowText, QColor(243, 243, 243))
    pal.setColor(QPalette.Base, QColor(15, 27, 53))
    pal.setColor(QPalette.AlternateBase, QColor(25, 38, 74))
    pal.setColor(QPalette.Text, QColor(243, 243, 243))
    pal.setColor(QPalette.Button, QColor(25, 38, 74))
    pal.setColor(QPalette.ButtonText, QColor(243, 243, 243))
    pal.setColor(QPalette.Highlight, QColor(76, 194, 255))
    pal.setColor(QPalette.HighlightedText, QColor(27, 27, 31))
    app.setPalette(pal)
    app.setStyleSheet(WIN11_QSS)
    w = MainWindow()
    if not _icon.isNull():
        w.setWindowIcon(_icon)
    if sys.platform == "win32":
        w.setAttribute(Qt.WA_TranslucentBackground, True)
    w.show()
    apply_mica(w)
    sys.exit(app.exec())


if __name__ == "__main__":
    run()

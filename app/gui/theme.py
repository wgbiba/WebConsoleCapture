"""Dark blue theme for AmiCDL (previous look restored)."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


# Dark navy/blue palette
WIN11_QSS = """
* { font-family: "Segoe UI Variable", "Segoe UI", Arial; font-size: 9pt; }

QMainWindow, QWidget {
    background: transparent;
    color: #e6eefc;
}

QWidget#rclRoot {
    background: rgba(13, 20, 38, 245);
    border-radius: 10px;
}

QLabel { color: #e6eefc; background: transparent; }
QLabel[muted="true"] { color: #9fb0cc; }

QMenuBar { background: #0b1326; color: #e6eefc; }
QMenuBar::item:selected { background: #1b2c52; }
QMenu { background: #0f1b35; color: #e6eefc; border: 1px solid #1b2c52; }
QMenu::item:selected { background: #1f3a73; }

QGroupBox {
    background: #111e3a;
    border: 1px solid #1f2f55;
    border-radius: 8px;
    margin-top: 16px;
    padding: 14px 14px 12px 14px;
    color: #e6eefc;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #cfe1ff;
    background: transparent;
}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextBrowser {
    background: #0c1730;
    color: #e6eefc;
    border: 1px solid #25406f;
    border-bottom: 1px solid #3b62a8;
    border-radius: 5px;
    padding: 5px 9px;
    selection-background-color: #2b6ce6;
    selection-color: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextBrowser:focus {
    border: 1px solid #2b6ce6;
    border-bottom: 2px solid #4c9aff;
    background: #0c1730;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QPlainTextEdit:disabled {
    color: #6c7a94;
    background: #0a1226;
}

QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #0f1b35;
    color: #e6eefc;
    border: 1px solid #25406f;
    selection-background-color: #1f3a73;
    selection-color: #ffffff;
    outline: 0;
}

QPushButton {
    background: #19264a;
    color: #e6eefc;
    border: 1px solid #25406f;
    border-radius: 5px;
    padding: 6px 16px;
    min-height: 22px;
}
QPushButton:hover { background: #213363; border-color: #3b62a8; }
QPushButton:pressed { background: #15203f; color: #9fb0cc; }
QPushButton:disabled { color: #6c7a94; background: #0f1a33; }

QPushButton[accent="true"] {
    background: #2b6ce6;
    color: #ffffff;
    border: 1px solid #2b6ce6;
    font-weight: 600;
}
QPushButton[accent="true"]:hover { background: #4080f0; border-color: #4080f0; }
QPushButton[accent="true"]:pressed { background: #1f5bcc; }

QPushButton[danger="true"] {
    background: #d6453b; color: #ffffff; border: 1px solid #d6453b;
    font-weight: 600;
}
QPushButton[danger="true"]:hover { background: #e25249; }
QPushButton[danger="true"]:pressed { background: #b4382f; }

QCheckBox { color: #e6eefc; spacing: 8px; }
QCheckBox::indicator {
    width: 18px; height: 18px;
    border-radius: 4px;
    border: 1px solid #3b62a8;
    background: #0c1730;
}
QCheckBox::indicator:hover { border: 1px solid #4c9aff; }
QCheckBox::indicator:checked {
    background: #2b6ce6;
    border: 1px solid #2b6ce6;
    image: none;
}
QCheckBox::indicator:checked:hover { background: #4080f0; }

QStatusBar {
    background: rgba(11, 19, 38, 220);
    color: #cfe1ff;
    border-top: 1px solid #1f2f55;
}

QTabWidget::pane { border: 1px solid #1f2f55; background: #0f1b35; border-radius: 6px; }
QTabBar::tab {
    background: #111e3a; color: #cfe1ff;
    padding: 6px 14px; border: 1px solid #1f2f55;
    border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
}
QTabBar::tab:selected { background: #1f3a73; color: #ffffff; }
QTabBar::tab:hover { background: #1b2c52; }

QDialog { background: #0d1426; color: #e6eefc; }

QScrollBar:vertical {
    background: transparent; width: 12px; margin: 2px;
}
QScrollBar::handle:vertical {
    background: #25406f;
    border-radius: 5px; min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #3b62a8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QToolTip {
    background: #0f1b35; color: #e6eefc;
    border: 1px solid #25406f;
    padding: 6px 8px; border-radius: 4px;
}
"""


def apply_mica(window) -> bool:
    """Enable Windows 11 dark title bar (Mica dark) on the given window."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi

        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (1 = dark title bar)
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), 20,
            ctypes.byref(dark), ctypes.sizeof(dark))

        # DWMWA_SYSTEMBACKDROP_TYPE = 38, value 2 = Mica
        backdrop = ctypes.c_int(2)
        hr = dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), 38,
            ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        if hr != 0:
            legacy = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), 1029,
                ctypes.byref(legacy), ctypes.sizeof(legacy))
        return True
    except Exception:
        return False

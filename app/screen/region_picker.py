"""Fullscreen overlay that lets the user drag a rectangle to pick a
screen region.  Returns (x, y, w, h) in physical pixels of the virtual
desktop (the coordinate space `mss` uses)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QRect, QPoint, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QGuiApplication
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout


@dataclass
class Region:
    x: int
    y: int
    w: int
    h: int

    def as_mss(self) -> dict:
        return {"left": self.x, "top": self.y,
                "width": self.w, "height": self.h}

    def __str__(self) -> str:
        return f"{self.w}x{self.h} @ ({self.x},{self.y})"


class RegionPicker(QWidget):
    """Translucent fullscreen overlay; user drags a rectangle, releases,
    and `picked` fires.  Press Esc to cancel."""

    picked = Signal(object)  # Region or None

    def __init__(self):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)

        # Cover the entire virtual desktop (all monitors).
        geo = QRect()
        for s in QGuiApplication.screens():
            geo = geo.united(s.geometry())
        self.setGeometry(geo)
        self._origin_offset = geo.topLeft()

        self._start: Optional[QPoint] = None
        self._end: Optional[QPoint] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        hint = QLabel(
            "Drag to select the console area  -  release to confirm  -  "
            "Esc to cancel")
        hint.setStyleSheet(
            "background: rgba(0,0,0,180); color: white; "
            "padding: 8px 12px; border-radius: 6px; font: 10pt 'Segoe UI';")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint, 0, Qt.AlignTop | Qt.AlignHCenter)
        layout.addStretch(1)

    # --- mouse ---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = e.position().toPoint()
            self._end = self._start
            self.update()

    def mouseMoveEvent(self, e):
        if self._start is not None:
            self._end = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._start is None:
            return
        self._end = e.position().toPoint()
        rect = QRect(self._start, self._end).normalized()
        self.hide()
        if rect.width() < 5 or rect.height() < 5:
            self.picked.emit(None)
        else:
            # Translate widget-local coords back to virtual-desktop coords.
            dpr = self.devicePixelRatioF() or 1.0
            x = int((rect.x() + self._origin_offset.x()) * dpr)
            y = int((rect.y() + self._origin_offset.y()) * dpr)
            w = int(rect.width() * dpr)
            h = int(rect.height() * dpr)
            self.picked.emit(Region(x, y, w, h))
        self.deleteLater()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
            self.picked.emit(None)
            self.deleteLater()

    # --- paint ---
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._start and self._end:
            rect = QRect(self._start, self._end).normalized()
            # Clear the selection area (show the screen underneath).
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(rect, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            pen = QPen(QColor(0, 200, 120), 2)
            p.setPen(pen)
            p.drawRect(rect)


def pick_region_async(callback) -> RegionPicker:
    """Show the picker; call ``callback(region_or_none)`` when done."""
    picker = RegionPicker()
    picker.picked.connect(callback)
    picker.showFullScreen()
    picker.raise_()
    picker.activateWindow()
    return picker

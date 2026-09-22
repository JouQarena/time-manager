"""Tray/app icons drawn with QPainter — no binary assets to ship or trust."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap

from app.ui.qt.theme import TRAY_ICON_COLORS


def app_icon(size: int = 128, role: str = "ok", badge: str = "") -> QIcon:
    """A rounded square with a clock face (and an optional status dot)."""
    return QIcon(pixmap(size, role=role, badge=badge))


def pixmap(size: int = 64, *, role: str = "ok", badge: str = "") -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)

    base = QColor(TRAY_ICON_COLORS.get(role, TRAY_ICON_COLORS["ok"]))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(base))
    margin = size * 0.06
    painter.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin),
        size * 0.22, size * 0.22,
    )

    # clock face
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(255, 255, 255, 235), max(1.4, size * 0.075)))
    radius = size * 0.28
    painter.drawEllipse(QRectF(size / 2 - radius, size / 2 - radius, radius * 2, radius * 2))
    # hands (10:10)
    painter.drawLine(
        int(size / 2), int(size / 2), int(size / 2), int(size / 2 - radius * 0.6)
    )
    painter.drawLine(
        int(size / 2), int(size / 2),
        int(size / 2 + radius * 0.5), int(size / 2 + radius * 0.28),
    )

    if badge:
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * 0.42))
        painter.setFont(font)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(
            QRectF(0, size * 0.52, size, size * 0.42),
            Qt.AlignHCenter | Qt.AlignVCenter, badge,
        )
    painter.end()
    return pix

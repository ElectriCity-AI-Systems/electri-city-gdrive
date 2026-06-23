"""Render the ElectriDrive app icon to assets/electridrive.png (run offscreen)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication


def render(size: int = 256) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    # rounded dark background
    bg = QRectF(8, 8, size - 16, size - 16)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#0E1116"))
    p.drawRoundedRect(bg, size * 0.22, size * 0.22)
    p.setBrush(Qt.NoBrush)
    pen = p.pen()
    pen.setColor(QColor("#232B36"))
    pen.setWidthF(size * 0.012)
    p.setPen(pen)
    p.drawRoundedRect(bg, size * 0.22, size * 0.22)

    # electric bolt with cyan->blue gradient
    s = size / 24.0
    pts = [(13, 2), (4, 14), (11, 14), (11, 22), (20, 9), (13, 9), (13, 2)]
    path = QPainterPath(QPointF(pts[0][0] * s, pts[0][1] * s))
    for x, y in pts[1:]:
        path.lineTo(QPointF(x * s, y * s))
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#22D3EE"))
    grad.setColorAt(1, QColor("#3B82F6"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawPath(path)
    p.end()
    return pm


def main():
    QApplication([])
    out = Path(__file__).resolve().parents[1] / "assets" / "electridrive.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    render(256).save(str(out))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

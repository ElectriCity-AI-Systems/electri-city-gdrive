"""Crisp, theme-tinted vector icons drawn at runtime with QPainter.

No external assets or QtSvg dependency. Each icon is stroked in a 24x24 unit space
and scaled to the requested pixel size, so they stay sharp at any size.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

_U = 24.0  # design unit space


def _pen(color: QColor, w: float = 2.0) -> QPen:
    pen = QPen(color)
    pen.setWidthF(w)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw(name: str, p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c))
    if name == "bolt":
        path = QPainterPath(QPointF(13, 2))
        for pt in [(4, 14), (11, 14), (11, 22), (20, 9), (13, 9), (13, 2)]:
            path.lineTo(QPointF(*pt))
        p.setBrush(c)
        p.setPen(_pen(c, 1.2))
        p.drawPath(path)
    elif name == "cloud":
        p.drawArc(QRectF(3, 9, 10, 10), 30 * 16, 220 * 16)
        p.drawArc(QRectF(9, 6, 11, 11), -40 * 16, 200 * 16)
        p.drawLine(QPointF(6.2, 18), QPointF(17, 18))
    elif name == "clock":
        p.drawEllipse(QRectF(3, 3, 18, 18))
        p.drawLine(QPointF(12, 7.5), QPointF(12, 12))
        p.drawLine(QPointF(12, 12), QPointF(15.5, 14))
    elif name == "star":
        pts = [(12, 2.5), (14.6, 9), (21.5, 9.4), (16.2, 13.9),
               (18, 20.6), (12, 16.8), (6, 20.6), (7.8, 13.9),
               (2.5, 9.4), (9.4, 9)]
        p.drawPolygon(QPolygonF([QPointF(*pt) for pt in pts]))
    elif name == "users":
        p.drawEllipse(QRectF(6, 4, 7, 7))
        p.drawArc(QRectF(3, 12, 13, 12), 20 * 16, 140 * 16)
        p.drawArc(QRectF(14, 6, 6, 6), -80 * 16, 200 * 16)
        p.drawArc(QRectF(15, 13, 7, 9), 40 * 16, 100 * 16)
    elif name == "trash":
        p.drawLine(QPointF(4, 6.5), QPointF(20, 6.5))
        p.drawLine(QPointF(9, 6.5), QPointF(9.7, 4), )
        p.drawLine(QPointF(9.7, 4), QPointF(14.3, 4))
        p.drawLine(QPointF(14.3, 4), QPointF(15, 6.5))
        path = QPainterPath(QPointF(6, 6.5))
        path.lineTo(QPointF(7, 21))
        path.lineTo(QPointF(17, 21))
        path.lineTo(QPointF(18, 6.5))
        p.drawPath(path)
        for x in (10, 12, 14):
            p.drawLine(QPointF(x, 9.5), QPointF(x, 18))
    elif name == "transfers":
        p.drawLine(QPointF(8, 4), QPointF(8, 20))
        p.drawPolyline(QPolygonF([QPointF(4.5, 8), QPointF(8, 4), QPointF(11.5, 8)]))
        p.drawLine(QPointF(16, 20), QPointF(16, 4))
        p.drawPolyline(QPolygonF([QPointF(12.5, 16), QPointF(16, 20), QPointF(19.5, 16)]))
    elif name == "sync":
        p.drawArc(QRectF(4, 4, 16, 16), 60 * 16, 200 * 16)
        p.drawPolyline(QPolygonF([QPointF(18.5, 3.5), QPointF(19.2, 8.5), QPointF(14.5, 8.0)]))
        p.drawArc(QRectF(4, 4, 16, 16), 240 * 16, 200 * 16)
        p.drawPolyline(QPolygonF([QPointF(5.5, 20.5), QPointF(4.8, 15.5), QPointF(9.5, 16.0)]))
    elif name == "drive":
        p.drawPolyline(QPolygonF([QPointF(8, 4), QPointF(16, 4), QPointF(21, 14),
                                  QPointF(17, 21), QPointF(7, 21), QPointF(3, 14),
                                  QPointF(8, 4)]))
        p.drawLine(QPointF(8, 4), QPointF(12, 14))
        p.drawLine(QPointF(21, 14), QPointF(12, 14))
        p.drawLine(QPointF(7, 21), QPointF(12, 14))
    elif name == "server":
        p.drawRoundedRect(QRectF(3.5, 4.5, 17, 6), 2, 2)
        p.drawRoundedRect(QRectF(3.5, 13, 17, 6), 2, 2)
        p.setBrush(c)
        p.drawEllipse(QRectF(6.3, 6.7, 1.6, 1.6))
        p.drawEllipse(QRectF(6.3, 15.2, 1.6, 1.6))
    elif name == "settings":  # sliders
        for i, y in enumerate((7, 12, 17)):
            p.drawLine(QPointF(4, y), QPointF(20, y))
            knob = (9, 15, 7)[i]
            p.setBrush(QColor("#0E1116"))
            p.drawEllipse(QRectF(knob - 2.4, y - 2.4, 4.8, 4.8))
    elif name == "search":
        p.drawEllipse(QRectF(4, 4, 12, 12))
        p.drawLine(QPointF(14.5, 14.5), QPointF(20, 20))
    elif name == "upload":
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawPolyline(QPolygonF([QPointF(7, 9), QPointF(12, 4), QPointF(17, 9)]))
        p.drawPolyline(QPolygonF([QPointF(4, 20), QPointF(20, 20)]))
    elif name == "download":
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawPolyline(QPolygonF([QPointF(7, 10), QPointF(12, 15), QPointF(17, 10)]))
        p.drawPolyline(QPolygonF([QPointF(4, 20), QPointF(20, 20)]))
    elif name == "folder":
        path = QPainterPath(QPointF(3, 7))
        path.lineTo(QPointF(9, 7))
        path.lineTo(QPointF(11, 9.5))
        path.lineTo(QPointF(21, 9.5))
        path.lineTo(QPointF(21, 19))
        path.lineTo(QPointF(3, 19))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "file":
        path = QPainterPath(QPointF(6, 3))
        path.lineTo(QPointF(14, 3))
        path.lineTo(QPointF(19, 8))
        path.lineTo(QPointF(19, 21))
        path.lineTo(QPointF(6, 21))
        path.closeSubpath()
        p.drawPath(path)
        p.drawPolyline(QPolygonF([QPointF(14, 3), QPointF(14, 8), QPointF(19, 8)]))
    elif name == "account":
        p.drawEllipse(QRectF(3, 3, 18, 18))
        p.drawEllipse(QRectF(8.5, 7.5, 7, 7))
        p.drawArc(QRectF(5, 14, 14, 12), 20 * 16, 140 * 16)
    elif name == "dots":
        p.setBrush(c)
        for x in (6, 12, 18):
            p.drawEllipse(QRectF(x - 1.4, 10.6, 2.8, 2.8))
    elif name == "chevron":
        p.drawPolyline(QPolygonF([QPointF(9, 5), QPointF(16, 12), QPointF(9, 19)]))
    elif name == "plus":
        p.drawLine(QPointF(12, 5), QPointF(12, 19))
        p.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "refresh":
        p.drawArc(QRectF(4, 4, 16, 16), 60 * 16, 250 * 16)
        p.drawPolyline(QPolygonF([QPointF(18.5, 3.5), QPointF(19.4, 8.7), QPointF(14.3, 8.0)]))
    elif name == "check":
        p.drawPolyline(QPolygonF([QPointF(5, 12.5), QPointF(10, 18), QPointF(19, 6)]))
    elif name == "close":
        p.drawLine(QPointF(6, 6), QPointF(18, 18))
        p.drawLine(QPointF(18, 6), QPointF(6, 18))
    elif name == "link":
        p.drawArc(QRectF(3, 9, 11, 6), 90 * 16, 180 * 16)
        p.drawArc(QRectF(10, 9, 11, 6), -90 * 16, 180 * 16)
        p.drawLine(QPointF(9, 12), QPointF(15, 12))
    elif name == "eject":
        p.drawPolygon(QPolygonF([QPointF(12, 5), QPointF(18, 13), QPointF(6, 13)]))
        p.drawLine(QPointF(6, 18), QPointF(18, 18))


def pixmap(name: str, color: str | QColor, size: int = 20) -> QPixmap:
    c = QColor(color)
    scale = 2  # render at 2x for crispness on HiDPI
    px = size * scale
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(px / _U, px / _U)
    p.setBrush(Qt.NoBrush)
    _draw(name, p, c)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, color: str | QColor, size: int = 20) -> QIcon:
    return QIcon(pixmap(name, color, size))

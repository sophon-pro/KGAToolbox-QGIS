# -*- coding: utf-8 -*-
"""Symbol Bag – colour wheel, swatches, colour-stop strip and eyedropper.

The wheel is painted with QPainter:
    outer ring   hue, full 360° spectrum
    left arc     saturation
    right arc    brightness (value)
    centre       the selected colour (over a checkerboard, so opacity shows)
Indicators glide on a spring when the colour is changed from elsewhere
(swatch, HEX, undo …); while the user drags they follow the mouse exactly.
"""

import math

from qgis.PyQt.QtCore import QPoint, QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import (QBrush, QColor, QConicalGradient, QCursor, QFont, QGuiApplication, QLinearGradient,
                             QPainter, QPainterPath, QPen, QPixmap)
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

from .motion import Tween, ease_out, lerp, spring

SAT_ARC = (120.0, 240.0)       # degrees, counter-clockwise from 3 o'clock (left side)
VAL_ARC = (-60.0, 60.0)        # right side


def checker_brush(cell=5):
    pm = QPixmap(cell * 2, cell * 2)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    p.fillRect(0, 0, cell, cell, QColor("#d0d4da"))
    p.fillRect(cell, cell, cell, cell, QColor("#d0d4da"))
    p.end()
    return QBrush(pm)


def contrast_text(c):
    lum = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
    return QColor("#1b1f24") if (lum > 0.6 or c.alphaF() < 0.45) else QColor("#ffffff")


def _hue_delta(a, b):
    """Shortest signed hue distance a → b in 0..1 units."""
    d = (b - a) % 1.0
    return d - 1.0 if d > 0.5 else d


# ----------------------------------------------------------------------
class ColorWheel(QWidget):
    colorChanged = pyqtSignal(QColor, bool)       # colour, final (mouse released / set)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.h, self.s, self.v, self.a = 0.0, 1.0, 1.0, 1.0     # target / real value
        self.dh, self.ds, self.dv, self.da = 0.0, 1.0, 1.0, 1.0  # displayed (animated)
        self.motion_enabled = True
        self.speed = 1.0
        self.theme = None
        self._drag = None
        self._hover = None
        self._tween = None
        self._ring_cache = None
        self._checker = checker_brush(6)
        self.setFixedSize(200, 200)

    # --- geometry ---------------------------------------------------------
    def set_diameter(self, d):
        d = int(max(110, min(460, d)))
        if d != self.width():
            self.setFixedSize(d, d)
            self._ring_cache = None
            self.update()

    def _geom(self):
        d = min(self.width(), self.height())
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        r_out = d / 2.0 - 3
        ring_w = max(10.0, d * 0.085)
        r_ring = r_out - ring_w / 2.0
        arc_w = max(7.0, d * 0.055)
        r_arc = r_out - ring_w - 6 - arc_w / 2.0
        r_center = r_arc - arc_w / 2.0 - 7
        return c, r_out, ring_w, r_ring, arc_w, r_arc, r_center

    # --- colour --------------------------------------------------------------
    def color(self):
        c = QColor.fromHsvF(self.h % 1.0, self.s, self.v)
        c.setAlphaF(self.a)
        return c

    def displayed_color(self):
        c = QColor.fromHsvF(self.dh % 1.0, max(0.0, min(1.0, self.ds)), max(0.0, min(1.0, self.dv)))
        c.setAlphaF(max(0.0, min(1.0, self.da)))
        return c

    def set_color(self, color, animate=True):
        color = QColor(color)
        if not color.isValid():
            return
        h = color.hsvHueF()
        if h < 0:            # grey: keep the hue the user had
            h = self.h % 1.0
        target = (h, color.hsvSaturationF(), color.valueF(), color.alphaF())
        self.h, self.s, self.v, self.a = target
        if self._tween is not None:
            self._tween.stop()
        if not (animate and self.motion_enabled and self.isVisible()):
            self.dh, self.ds, self.dv, self.da = target
            self.update()
            return
        start = (self.dh, self.ds, self.dv, self.da)
        dh = _hue_delta(start[0] % 1.0, h)

        def frame(t):
            k = spring(t, 0.68)
            self.dh = (start[0] + dh * k) % 1.0
            self.ds = lerp(start[1], target[1], k)
            self.dv = lerp(start[2], target[2], k)
            self.da = lerp(start[3], target[3], ease_out(t))
            self.update()
        self._tween = Tween(self, 420 * self.speed, frame).start()

    def set_alpha(self, a, emit=True):
        self.a = self.da = max(0.0, min(1.0, a))
        self.update()
        if emit:
            self.colorChanged.emit(self.color(), True)

    def _set_from_drag(self, h=None, s=None, v=None):
        if self._tween is not None:
            self._tween.stop()
        if h is not None:
            self.h = h % 1.0
        if s is not None:
            self.s = max(0.0, min(1.0, s))
        if v is not None:
            self.v = max(0.0, min(1.0, v))
        self.dh, self.ds, self.dv, self.da = self.h, self.s, self.v, self.a
        self.update()
        self.colorChanged.emit(self.color(), False)

    # --- hit testing -------------------------------------------------------------
    def _hit(self, pos):
        c, r_out, ring_w, r_ring, arc_w, r_arc, r_center = self._geom()
        dx, dy = pos.x() - c.x(), c.y() - pos.y()
        dist = math.hypot(dx, dy)
        ang = math.degrees(math.atan2(dy, dx))
        if r_ring - ring_w / 2.0 - 4 <= dist <= r_out + 4:
            return "hue"
        if abs(dist - r_arc) <= arc_w / 2.0 + 6:
            a = ang % 360.0
            if SAT_ARC[0] - 8 <= a <= SAT_ARC[1] + 8:
                return "sat"
            va = ((ang + 180.0) % 360.0) - 180.0
            if VAL_ARC[0] - 8 <= va <= VAL_ARC[1] + 8:
                return "val"
        return None

    def _apply_pos(self, part, pos):
        c = self._geom()[0]
        dx, dy = pos.x() - c.x(), c.y() - pos.y()
        ang = math.degrees(math.atan2(dy, dx))
        if part == "hue":
            self._set_from_drag(h=(ang % 360.0) / 360.0)
        elif part == "sat":
            a = max(SAT_ARC[0], min(SAT_ARC[1], ang % 360.0))
            self._set_from_drag(s=1.0 - (a - SAT_ARC[0]) / (SAT_ARC[1] - SAT_ARC[0]))
        elif part == "val":
            a = max(VAL_ARC[0], min(VAL_ARC[1], ((ang + 180.0) % 360.0) - 180.0))
            self._set_from_drag(v=(a - VAL_ARC[0]) / (VAL_ARC[1] - VAL_ARC[0]))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position() if hasattr(e, "position") else QPointF(e.pos())
        part = self._hit(pos)
        if part:
            self._drag = part
            self._apply_pos(part, pos)

    def mouseMoveEvent(self, e):
        pos = e.position() if hasattr(e, "position") else QPointF(e.pos())
        if self._drag:
            self._apply_pos(self._drag, pos)
        else:
            hov = self._hit(pos)
            if hov != self._hover:
                self._hover = hov
                self.setCursor(Qt.CursorShape.PointingHandCursor if hov else Qt.CursorShape.ArrowCursor)
                self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = None
            self.colorChanged.emit(self.color(), True)

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    def wheelEvent(self, e):
        step = 1.0 / 360.0 * (3 if e.angleDelta().y() > 0 else -3)
        self._set_from_drag(h=self.h + step)
        self.colorChanged.emit(self.color(), True)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._set_from_drag(h=self.h + (-1 if k == Qt.Key.Key_Left else 1) / 360.0 * 2)
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._set_from_drag(v=self.v + (0.02 if k == Qt.Key.Key_Up else -0.02))
        else:
            return super().keyPressEvent(e)
        self.colorChanged.emit(self.color(), True)

    # --- painting ------------------------------------------------------------------
    def _ring(self):
        """The hue ring never changes for a given size – cache it."""
        dpr = self.devicePixelRatioF()
        key = (self.width(), dpr)
        if self._ring_cache is not None and self._ring_cache[0] == key:
            return self._ring_cache[1]
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, r_out, ring_w, r_ring, *_ = self._geom()
        g = QConicalGradient(c, 0)
        for i in range(13):
            g.setColorAt(i / 12.0, QColor.fromHsvF((i / 12.0) % 1.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(g), ring_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawEllipse(c, r_ring, r_ring)
        p.setPen(QPen(QColor(0, 0, 0, 35), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r_out, r_out)
        p.drawEllipse(c, r_out - ring_w, r_out - ring_w)
        p.end()
        self._ring_cache = (key, pm)
        return pm

    def _arc_path(self, c, r, a0, a1):
        path = QPainterPath()
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        path.arcMoveTo(rect, a0)
        path.arcTo(rect, a0, a1 - a0)
        return path

    def _knob(self, p, pt, radius, fill, active):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawEllipse(pt + QPointF(0, 1.2), radius + 1.5, radius + 1.5)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(pt, radius + (2.6 if active else 2.0), radius + (2.6 if active else 2.0))
        p.setBrush(fill)
        p.drawEllipse(pt, radius - 0.5, radius - 0.5)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, r_out, ring_w, r_ring, arc_w, r_arc, r_center = self._geom()
        p.drawPixmap(0, 0, self._ring())
        h, s, v = self.dh % 1.0, max(0.0, min(1.0, self.ds)), max(0.0, min(1.0, self.dv))

        # saturation arc (left): grey → full colour
        g = QConicalGradient(c, SAT_ARC[0])
        span = (SAT_ARC[1] - SAT_ARC[0]) / 360.0
        g.setColorAt(0.0, QColor.fromHsvF(h, 1.0, max(0.25, v)))
        g.setColorAt(span, QColor.fromHsvF(h, 0.0, max(0.25, v)))
        g.setColorAt(1.0, QColor.fromHsvF(h, 1.0, max(0.25, v)))
        p.setPen(QPen(QBrush(g), arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(self._arc_path(c, r_arc, SAT_ARC[0], SAT_ARC[1]))
        # value arc (right): black → bright
        g = QConicalGradient(c, VAL_ARC[0])
        span = (VAL_ARC[1] - VAL_ARC[0]) / 360.0
        g.setColorAt(0.0, QColor.fromHsvF(h, s, 0.0))
        g.setColorAt(span, QColor.fromHsvF(h, s, 1.0))
        g.setColorAt(1.0, QColor.fromHsvF(h, s, 0.0))
        p.setPen(QPen(QBrush(g), arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(self._arc_path(c, r_arc, VAL_ARC[0], VAL_ARC[1]))

        # little labels at the arc ends
        f = QFont(self.font())
        f.setPointSizeF(max(6.5, self.width() / 34.0))
        f.setBold(True)
        p.setFont(f)
        sub = QColor(self.theme.p["sub"]) if self.theme else QColor("#777")
        p.setPen(sub)
        for text, ang in (("S", 180.0), ("B", 0.0)):
            rr = r_arc - arc_w - 4 if self.width() > 170 else r_arc
            if self.width() > 170:
                pt = QPointF(c.x() + rr * math.cos(math.radians(ang)), c.y() - rr * math.sin(math.radians(ang)))
                p.drawText(QRectF(pt.x() - 8, pt.y() - 8, 16, 16), Qt.AlignmentFlag.AlignCenter, text)

        # centre: selected colour over a checkerboard
        col = self.displayed_color()
        center_rect = QRectF(c.x() - r_center, c.y() - r_center, 2 * r_center, 2 * r_center)
        if self.width() > 170:
            center_rect = center_rect.adjusted(10, 10, -10, -10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(center_rect.translated(0, 1.5))
        p.setBrush(self._checker)
        p.drawEllipse(center_rect)
        p.setBrush(col)
        p.setPen(QPen(QColor(255, 255, 255, 220), 2.5))
        p.drawEllipse(center_rect)
        if center_rect.width() > 56:
            f2 = QFont(self.font())
            f2.setPointSizeF(max(7.0, center_rect.width() / 9.0))
            f2.setBold(True)
            p.setFont(f2)
            p.setPen(contrast_text(col))
            text = col.name().upper()
            if col.alpha() < 255:
                text += "\n{}%".format(int(round(col.alphaF() * 100)))
            p.drawText(center_rect, Qt.AlignmentFlag.AlignCenter, text)

        # knobs
        hr = math.radians(h * 360.0)
        hue_pt = QPointF(c.x() + r_ring * math.cos(hr), c.y() - r_ring * math.sin(hr))
        self._knob(p, hue_pt, ring_w * 0.42, QColor.fromHsvF(h, 1.0, 1.0), self._drag == "hue" or self._hover == "hue")
        sa = math.radians(SAT_ARC[0] + (1.0 - s) * (SAT_ARC[1] - SAT_ARC[0]))
        self._knob(p, QPointF(c.x() + r_arc * math.cos(sa), c.y() - r_arc * math.sin(sa)), arc_w * 0.55,
                   QColor.fromHsvF(h, s, max(0.25, v)), self._drag == "sat" or self._hover == "sat")
        va = math.radians(VAL_ARC[0] + v * (VAL_ARC[1] - VAL_ARC[0]))
        self._knob(p, QPointF(c.x() + r_arc * math.cos(va), c.y() - r_arc * math.sin(va)), arc_w * 0.55,
                   QColor.fromHsvF(h, s, v), self._drag == "val" or self._hover == "val")
        p.end()


# ----------------------------------------------------------------------
class SwatchGrid(QWidget):
    """Row(s) of colour swatches. Click to pick, right-click for actions."""

    picked = pyqtSignal(QColor)
    pickedIndex = pyqtSignal(int)
    menuRequested = pyqtSignal(QColor, QPoint)

    def __init__(self, parent=None, size=18, max_rows=None):
        super().__init__(parent)
        self.colors = []
        self.labels = []          # optional tooltip label per swatch
        self.size = size
        self.gap = 4
        self.max_rows = max_rows
        self.current = None
        self.current_index = None   # when set, marks that swatch (not by colour)
        self.favorites = set()
        self.empty_text = ""
        self._hover = -1
        self._checker = checker_brush(4)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_colors(self, colors, current=None):
        self.colors = [QColor(c) for c in colors]
        self.current = QColor(current) if current is not None else self.current
        self.updateGeometry()
        self._fix_height()
        self.update()

    def _per_row(self, width=None):
        w = width if width is not None else self.width()
        return max(1, (w + self.gap) // (self.size + self.gap))

    def _rows(self, width=None):
        n = len(self.colors)
        rows = max(1, (n + self._per_row(width) - 1) // self._per_row(width))
        return min(rows, self.max_rows) if self.max_rows else rows

    def height_for(self, width):
        return self._rows(width) * (self.size + self.gap) - self.gap + 4

    def fit_width(self, width):
        """Set the height for a known width (works while the window is hidden,
        when Qt has not delivered resize events yet)."""
        self.setFixedHeight(self.height_for(width))

    def _fix_height(self):
        self.setFixedHeight(self.height_for(self.width()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fix_height()

    def _rect(self, i):
        pr = self._per_row()
        r, c = divmod(i, pr)
        return QRectF(c * (self.size + self.gap) + 1, r * (self.size + self.gap) + 2, self.size, self.size)

    def _index_at(self, pos):
        for i in range(min(len(self.colors), self._per_row() * self._rows())):
            if self._rect(i).adjusted(-2, -2, 2, 2).contains(QPointF(pos)):
                return i
        return -1

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.colors and self.empty_text:
            p.setPen(QColor("#8a94a3"))
            f = QFont(self.font())
            f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.empty_text)
        visible = self._per_row() * self._rows()
        for i, col in enumerate(self.colors[:visible]):
            r = self._rect(i)
            if i == self._hover:
                r = r.adjusted(-1.5, -1.5, 1.5, 1.5)
            p.setPen(Qt.PenStyle.NoPen)
            if col.alpha() < 255:
                p.setBrush(self._checker)
                p.drawRoundedRect(r, 5, 5)
            p.setBrush(col)
            if self.current_index is not None:
                sel = i == self.current_index
            else:
                sel = self.current is not None and col.rgba() == self.current.rgba()
            p.setPen(QPen(QColor(0, 0, 0, 45), 1))
            p.drawRoundedRect(r, 5, 5)
            if sel:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 3.5, 3.5)
                p.setPen(QPen(QColor(0, 0, 0, 120), 1))
                p.drawRoundedRect(r, 5, 5)
        p.end()

    def mouseMoveEvent(self, e):
        i = self._index_at(e.pos())
        if i != self._hover:
            self._hover = i
            tip = ""
            if i >= 0:
                tip = self.colors[i].name(QColor.NameFormat.HexArgb if self.colors[i].alpha() < 255
                                          else QColor.NameFormat.HexRgb).upper()
                if i < len(self.labels) and self.labels[i]:
                    tip = "{}  ·  {}".format(self.labels[i], tip)
            self.setToolTip(tip)
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()

    def mousePressEvent(self, e):
        i = self._index_at(e.pos())
        if i < 0:
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(QColor(self.colors[i]))
            self.pickedIndex.emit(i)
        elif e.button() == Qt.MouseButton.RightButton:
            gp = e.globalPos() if hasattr(e, "globalPos") else e.globalPosition().toPoint()
            self.menuRequested.emit(QColor(self.colors[i]), gp)


# ----------------------------------------------------------------------
class StopStrip(QWidget):
    """Raster colour ramp: a gradient bar with one handle per colour stop.
    Click a handle to edit that stop."""

    stopClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.colors = []
        self.current = -1
        self.discrete = False
        self.setFixedHeight(30)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._checker = checker_brush(4)

    def set_stops(self, colors, current=-1):
        self.colors = [QColor(c) for c in colors]
        self.current = current
        self.update()

    def _x(self, i):
        n = len(self.colors)
        return 8 + (self.width() - 16) * (i / float(n - 1) if n > 1 else 0.5)

    def paintEvent(self, e):
        if not self.colors:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(8, 4, self.width() - 16, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._checker)
        p.drawRoundedRect(bar, 4, 4)
        n = len(self.colors)
        if self.discrete or n == 1:
            w = bar.width() / n
            for i, c in enumerate(self.colors):
                p.setBrush(c)
                p.drawRect(QRectF(bar.left() + i * w, bar.top(), w + 0.5, bar.height()))
        else:
            g = QLinearGradient(bar.topLeft(), bar.topRight())
            for i, c in enumerate(self.colors):
                g.setColorAt(i / float(n - 1), c)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(bar, 4, 4)
        for i, c in enumerate(self.colors):
            x = self._x(i)
            sel = i == self.current
            p.setPen(QPen(QColor("#1b1f24") if sel else QColor(0, 0, 0, 90), 1.6 if sel else 1))
            p.setBrush(QColor("#ffffff"))
            tri = QPainterPath()
            tri.moveTo(x, 16)
            tri.lineTo(x - 5, 24)
            tri.lineTo(x + 5, 24)
            tri.closeSubpath()
            p.drawPath(tri)
            p.setBrush(c)
            p.drawEllipse(QPointF(x, 25), 4.6 if sel else 3.8, 4.6 if sel else 3.8)
        p.end()

    def mousePressEvent(self, e):
        if not self.colors:
            return
        x = e.pos().x()
        i = min(range(len(self.colors)), key=lambda k: abs(self._x(k) - x))
        self.stopClicked.emit(i)


# ----------------------------------------------------------------------
class _EyedropperOverlay(QWidget):
    def __init__(self, session, screen, shot):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.session = session
        self.shot = shot
        self.img = shot.toImage()
        self.dpr = shot.devicePixelRatio() or 1.0
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.pos_ = None

    def color_at(self, pos):
        x = int(pos.x() * self.img.width() / max(1, self.width()))
        y = int(pos.y() * self.img.height() / max(1, self.height()))
        x = max(0, min(self.img.width() - 1, x))
        y = max(0, min(self.img.height() - 1, y))
        return QColor(self.img.pixel(x, y)), x, y

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.shot)
        if self.pos_ is not None:
            col, px, py = self.color_at(self.pos_)
            zoom, n = 9, 11
            size = zoom * n
            loupe = QRectF(self.pos_.x() + 22, self.pos_.y() + 22, size, size)
            if loupe.right() > self.width() - 4:
                loupe.moveRight(self.pos_.x() - 22)
            if loupe.bottom() > self.height() - 40:
                loupe.moveBottom(self.pos_.y() - 22)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addEllipse(loupe)
            p.save()
            p.setClipPath(path)
            src = self.img.copy(px - n // 2, py - n // 2, n, n)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawImage(loupe, src)
            p.setPen(QPen(QColor(255, 255, 255, 220), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(loupe.left() + (n // 2) * zoom, loupe.top() + (n // 2) * zoom, zoom, zoom))
            p.restore()
            p.setPen(QPen(QColor("#ffffff"), 3))
            p.drawEllipse(loupe)
            p.setPen(QPen(QColor(0, 0, 0, 120), 1))
            p.drawEllipse(loupe.adjusted(-1.5, -1.5, 1.5, 1.5))
            label = QRectF(loupe.left() + size / 2 - 42, loupe.bottom() + 6, 84, 22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 24, 30, 220))
            p.drawRoundedRect(label, 11, 11)
            p.setBrush(col)
            p.drawEllipse(QPointF(label.left() + 12, label.center().y()), 6, 6)
            p.setPen(QColor("#ffffff"))
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            p.drawText(label.adjusted(22, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, col.name().upper())
        p.end()

    def mouseMoveEvent(self, e):
        self.pos_ = e.pos()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            col, _x, _y = self.color_at(e.pos())
            self.session.finish(col)
        else:
            self.session.finish(None)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.session.finish(None)


class EyedropperSession(object):
    """Pick a colour anywhere on screen (map canvas, other apps …).
    A screenshot of every screen is shown full-screen with a magnifier."""

    def __init__(self, callback):
        self.callback = callback
        self.overlays = []

    def start(self):
        screens = QGuiApplication.screens()
        for screen in screens:
            shot = screen.grabWindow(0)
            if shot.isNull():
                continue
            ov = _EyedropperOverlay(self, screen, shot)
            self.overlays.append(ov)
        if not self.overlays:
            return False
        for ov in self.overlays:
            ov.show()
            ov.raise_()
            ov.activateWindow()
            ov.pos_ = ov.mapFromGlobal(QCursor.pos())
        self.overlays[0].grabKeyboard()
        return True

    def finish(self, color):
        for ov in self.overlays:
            try:
                ov.releaseKeyboard()
                ov.hide()
                ov.deleteLater()
            except RuntimeError:
                pass
        self.overlays = []
        cb, self.callback = self.callback, None
        if cb is not None:
            cb(color)


# ----------------------------------------------------------------------
class WidthDial(QWidget):
    """Thickness wheel: a tapered ring that grows thicker clockwise.

    Drag around it (or scroll) to set the line / outline width; the centre
    shows a sample stroke at that thickness and the value. The scale is
    finer for thin lines (square-root mapping), where cartography needs it.
    """

    valueChanged = pyqtSignal(float, bool)       # value, final
    START, SPAN = 225.0, 270.0                    # degrees (Qt: 0 = 3 o'clock, CCW +)

    def __init__(self, parent=None, diameter=104):
        super().__init__(parent)
        self.theme = None
        self.value = 0.5
        self.vmax = 5.0
        self.unit = "mm"
        self.sample = "stroke"           # "stroke" (line / outline width) or "dot" (point size)
        self.color = QColor("#2f80ed")
        self.motion_enabled = True
        self.speed = 1.0
        self._shown_t = self._t(self.value)
        self._tween = None
        self._drag = False
        self.setFixedSize(diameter, diameter)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Drag around the wheel (or scroll) to change the thickness")

    # --- value <-> position ------------------------------------------------------
    def _t(self, v):
        return math.sqrt(max(0.0, min(1.0, v / self.vmax))) if self.vmax > 0 else 0.0

    def _v(self, t):
        return self.vmax * max(0.0, min(1.0, t)) ** 2

    def set_range(self, vmax):
        self.vmax = max(0.1, float(vmax))
        self._shown_t = self._t(self.value)
        self.update()

    def set_value(self, v, animate=True):
        v = max(0.0, float(v))
        if v > self.vmax:
            self.set_range(v * 1.5)
        self.value = v
        t1 = self._t(v)
        if self._tween is not None:
            self._tween.stop()
        if not (animate and self.motion_enabled and self.isVisible()):
            self._shown_t = t1
            self.update()
            return
        t0 = self._shown_t

        def frame(k):
            self._shown_t = lerp(t0, t1, ease_out(k, 3))
            self.update()
        self._tween = Tween(self, 200 * self.speed, frame).start()

    def _geom(self):
        r = self.width() / 2.0
        c = QPointF(r, r)
        return c, r - 9.0          # centre, ring radius (room for the widest end)

    def _angle_of(self, t):
        return math.radians(self.START - self.SPAN * t)

    # --- painting -------------------------------------------------------------------
    def _ring_path(self, c, rr, t0, t1, w0=5.0, w1=13.0, steps=48):
        """Tapered band from t0 to t1 (thickness w0 at t=0 … w1 at t=1)."""
        outer, inner = [], []
        n = max(2, int(steps * (t1 - t0)) + 2)
        for i in range(n):
            t = t0 + (t1 - t0) * i / (n - 1.0)
            a = self._angle_of(t)
            half = (w0 + (w1 - w0) * t) / 2.0
            cx, cy = math.cos(a), -math.sin(a)
            outer.append(QPointF(c.x() + (rr + half) * cx, c.y() + (rr + half) * cy))
            inner.append(QPointF(c.x() + (rr - half) * cx, c.y() + (rr - half) * cy))
        path = QPainterPath(outer[0])
        for p in outer[1:]:
            path.lineTo(p)
        for p in reversed(inner):
            path.lineTo(p)
        path.closeSubpath()
        return path

    def paintEvent(self, e):
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, rr = self._geom()
        sub = QColor(t.p["sub"]) if t else QColor("#888888")
        track = QColor(sub)
        track.setAlpha(70)
        accent = QColor(t.accent) if t else QColor("#2f80ed")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawPath(self._ring_path(c, rr, 0.0, 1.0))
        k = max(0.0, min(1.0, self._shown_t))
        p.setBrush(accent)
        p.drawPath(self._ring_path(c, rr, 0.0, max(k, 0.015)))
        # knob
        a = self._angle_of(k)
        kp = QPointF(c.x() + rr * math.cos(a), c.y() - rr * math.sin(a))
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(accent, 2.2))
        p.drawEllipse(kp, 7.0, 7.0)
        # centre: sample stroke + value
        inner_r = rr - 14
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.p["card"]) if t else QColor("#ffffff"))
        p.drawEllipse(c, inner_r, inner_r)
        if self.sample == "dot":
            # a dot growing with the size (up to the inner circle)
            frac = self._v(k) / self.vmax if self.vmax > 0 else 0.0
            dr = max(2.0, inner_r * 0.32 * math.sqrt(max(0.0, min(1.0, frac))))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.color if self.color.alpha() > 0 else sub)
            p.drawEllipse(QPointF(c.x(), c.y() - inner_r * 0.22), dr, dr)
        else:
            px = self._sample_px(self._v(k))
            pen = QPen(self.color if self.color.alpha() > 0 else sub, px)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            half = inner_r * 0.55
            y = c.y() - inner_r * 0.18
            p.drawLine(QPointF(c.x() - half, y), QPointF(c.x() + half, y))
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.95))
        p.setFont(f)
        p.setPen(QColor(t.p["text"]) if t else QColor("#333333"))
        p.drawText(QRectF(c.x() - inner_r, c.y() + inner_r * 0.12, inner_r * 2, inner_r * 0.5),
                   Qt.AlignmentFlag.AlignCenter, self.text_for(self._v(k)))
        p.end()

    def text_for(self, v):
        return "{:g} {}".format(round(v, 2), self.unit)

    def _sample_px(self, v):
        scale = {"mm": 3.78, "pt": 1.33, "px": 1.0, "in": 96.0}.get(self.unit, 1.0)
        return max(1.0, min(14.0, v * scale))

    # --- interaction --------------------------------------------------------------------
    def _t_at(self, pos):
        c, _rr = self._geom()
        ang = math.degrees(math.atan2(-(pos.y() - c.y()), pos.x() - c.x()))
        d = (self.START - ang) % 360.0               # clockwise distance from the start
        if d > self.SPAN:                             # in the gap at the bottom: nearest end
            d = self.SPAN if d - self.SPAN < (360.0 - d) else 0.0
        return d / self.SPAN

    def _emit_at(self, pos, final):
        v = round(self._v(self._t_at(pos)), 2)
        self.value = v
        self._shown_t = self._t(v)
        self.update()
        self.valueChanged.emit(v, final)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._tween is not None:
                self._tween.stop()
            self._drag = True
            self._emit_at(e.position() if hasattr(e, "position") else QPointF(e.pos()), False)

    def mouseMoveEvent(self, e):
        if self._drag:
            self._emit_at(e.position() if hasattr(e, "position") else QPointF(e.pos()), False)

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self._emit_at(e.position() if hasattr(e, "position") else QPointF(e.pos()), True)

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        step = 0.05 if self.unit == "mm" else 0.25
        v = round(max(0.0, self.value + steps * step), 2)
        if v > self.vmax:
            self.set_range(v * 1.5)
        self.set_value(v, animate=False)
        self.valueChanged.emit(v, True)
        e.accept()

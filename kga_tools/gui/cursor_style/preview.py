"""Live cursor preview.

The preview paints the *same* pixmap that becomes the map-canvas cursor, at
true device-pixel size, and hovering over it shows the real QCursor - so what
the user sees here is the actual implementation, not an illustration.
"""

from qgis.PyQt.QtCore import Qt, QRectF, QPointF, QRect, QSize
from qgis.PyQt.QtGui import (QPainter, QColor, QPen, QLinearGradient,
                             QPainterPath, QFont, QPolygonF, QPixmap)
from qgis.PyQt.QtWidgets import QWidget, QSizePolicy

from .style import BASE_NATIVE, BASE_SYSTEM_ARROW, BASE_SYSTEM_CROSS

BACKGROUNDS = [("light", "Light"), ("dark", "Dark"), ("map", "Map"), ("split", "Split")]
ZOOM = 4


def paint_background(p, rect, mode):
    if mode == "dark":
        p.fillRect(rect, QColor("#1b1f24"))
        _grid(p, rect, QColor(255, 255, 255, 18))
    elif mode == "map":
        _fake_map(p, rect)
    elif mode == "split":
        left = QRectF(rect.left(), rect.top(), rect.width() / 2, rect.height())
        right = QRectF(rect.center().x(), rect.top(), rect.width() / 2, rect.height())
        p.fillRect(left, QColor("#f3f3f1"))
        p.fillRect(right, QColor("#1b1f24"))
    else:
        p.fillRect(rect, QColor("#f3f3f1"))
        _grid(p, rect, QColor(0, 0, 0, 16))


def _grid(p, rect, color):
    p.setPen(QPen(color, 1))
    step = 16
    x = rect.left() + step
    while x < rect.right():
        p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        x += step
    y = rect.top() + step
    while y < rect.bottom():
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        y += step


def _fake_map(p, rect):
    """Light basemap on the left, dark 'imagery' on the right, with roads and
    a parcel outline - the situations a digitizing cursor must survive."""
    p.fillRect(rect, QColor("#eae6dc"))
    img = QRectF(rect.left() + rect.width() * 0.55, rect.top(), rect.width() * 0.45, rect.height())
    g = QLinearGradient(img.topLeft(), img.bottomRight())
    g.setColorAt(0, QColor("#24361f"))
    g.setColorAt(0.5, QColor("#3d4a2a"))
    g.setColorAt(1, QColor("#1c2617"))
    p.fillRect(img, g)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QPen(QColor("#a8c8e8"), 9))
    path = QPainterPath(QPointF(rect.left(), rect.bottom() - 18))
    path.cubicTo(rect.left() + rect.width() * 0.3, rect.bottom() - 40,
                 rect.left() + rect.width() * 0.45, rect.top() + 30,
                 rect.right(), rect.top() + 22)
    p.drawPath(path)
    p.setPen(QPen(QColor("#ffffff"), 5))
    p.drawLine(QPointF(rect.left(), rect.top() + rect.height() * 0.38),
               QPointF(rect.right(), rect.top() + rect.height() * 0.62))
    p.setPen(QPen(QColor("#f6c56b"), 3))
    p.drawLine(QPointF(rect.left() + rect.width() * 0.3, rect.top()),
               QPointF(rect.left() + rect.width() * 0.4, rect.bottom()))
    p.setPen(QPen(QColor("#c0392b"), 1.5))
    p.setBrush(QColor(192, 57, 43, 40))
    p.drawPolygon(QPolygonF([QPointF(rect.left() + 18, rect.top() + 16),
                             QPointF(rect.left() + 70, rect.top() + 12),
                             QPointF(rect.left() + 80, rect.top() + 52),
                             QPointF(rect.left() + 26, rect.top() + 60)]))
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)


def system_arrow_pixmap(size, dpr=1.0):
    """Generic arrow used only to *illustrate* the OS pointer (the real one is
    shown on hover)."""
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size / 24.0
    poly = QPolygonF([QPointF(x * s, y * s) for x, y in
                      ((5, 2), (5, 19), (9, 15), (12, 22), (14.5, 21), (11.5, 14), (17, 14))])
    p.setPen(QPen(QColor("white"), 1.6 * s))
    p.setBrush(QColor("black"))
    p.drawPolygon(poly)
    p.end()
    return pm


def _align(flags):
    """Alignment flags as the int QPainter.drawText wants, on PyQt5 and PyQt6."""
    try:
        return int(flags)
    except TypeError:
        return int(flags.value)


class CursorPreview(QWidget):
    """Shows the cursor at true size on a selectable background, plus an
    optional pixel-zoom inset with the hotspot marked."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self._result = None
        self._style = None
        self._bg = "map"
        self._zoom = True
        self._hover = False
        self.setToolTip("Move the mouse over this area to try the real cursor.")

    def sizeHint(self):
        return QSize(300, 190)

    def set_background(self, mode):
        if mode != self._bg:
            self._bg = mode
            self.update()

    def set_zoom(self, on):
        if on != self._zoom:
            self._zoom = bool(on)
            self.update()

    def set_result(self, style, result):
        """result: RenderResult from the renderer for this widget's DPR."""
        if result is self._result and style == self._style:
            return
        self._style, self._result = style, result
        if result is not None and result.cursor is not None:
            self.setCursor(result.cursor)
        else:
            self.unsetCursor()
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        paint_background(p, r, self._bg)
        p.setClipping(False)

        res, st = self._result, self._style
        light_bg = self._bg == "light"
        text_col = QColor("#333") if light_bg else QColor("#eee")
        if self._bg in ("map", "split"):
            text_col = QColor("#222")
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)

        cx, cy = self.width() // 2, self.height() // 2
        if st is None:
            pass
        elif st.base == BASE_NATIVE:
            self._caption(p, text_col, "Each QGIS tool keeps its own native cursor")
        elif st.base in (BASE_SYSTEM_ARROW, BASE_SYSTEM_CROSS):
            if st.base == BASE_SYSTEM_ARROW:
                pm = system_arrow_pixmap(24, self.devicePixelRatioF())
                p.drawPixmap(cx - 5, cy - 2, pm)
            else:
                p.setPen(QPen(QColor("#000"), 1))
                p.drawLine(cx - 10, cy, cx + 10, cy)
                p.drawLine(cx, cy - 10, cx, cy + 10)
            self._caption(p, text_col, "Operating-system cursor · hover to see it exactly")
        elif res is not None and res.pixmap is not None:
            pm = res.pixmap
            lw = pm.width() / pm.devicePixelRatio()
            # place so the hotspot pixel centre sits on the widget centre
            x = cx - res.center / pm.devicePixelRatio()
            y = cy - res.center / pm.devicePixelRatio()
            if self._zoom:
                x -= self.width() * 0.18
            p.drawPixmap(QPointF(x, y), pm)
            if self._zoom:
                self._draw_zoom(p, res)
            size_txt = f"{int(round(lw))} px" if abs(lw - round(lw)) < 0.01 else f"{lw:.1f} px"
            note = "  · hover to try it" if not self._hover else "  · this is the real cursor"
            if res.clamped:
                note = "  · reduced to the maximum cursor size"
            self._caption(p, text_col, f"Actual size {size_txt}{note}")
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 8, 8)
        p.end()

    def _caption(self, p, col, text):
        p.setPen(col)
        p.drawText(QRect(8, self.height() - 22, self.width() - 16, 18),
                   _align(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)

    def _draw_zoom(self, p, res):
        # Zoom is an integer number of *device* pixels per cursor pixel, so
        # every cursor pixel is shown as a crisp block at any display scale.
        img = res.pixmap.toImage()
        n = img.width()
        dpr = self.devicePixelRatioF()
        box = min(self.height() - 44, int(self.width() * 0.38))
        zd = max(2, min(ZOOM * 3, int(box * dpr) // max(1, n)))
        size = n * zd / dpr                      # logical size of the inset
        bx = self.width() - size - 12
        by = max(6.0, (self.height() - 20 - size) / 2)
        frame = QRectF(bx - 1, by - 1, size + 2, size + 2)
        p.fillRect(frame, QColor(255, 255, 255, 230) if self._bg != "dark" else QColor(20, 22, 26, 230))
        paint_background(p, QRectF(bx, by, size, size), self._bg)
        scaled = QPixmap.fromImage(img.scaled(n * zd, n * zd, Qt.AspectRatioMode.IgnoreAspectRatio,
                                              Qt.TransformationMode.FastTransformation))
        scaled.setDevicePixelRatio(dpr)
        p.drawPixmap(QPointF(bx, by), scaled)
        cell = zd / dpr
        p.setPen(QPen(QColor("#00b0ff"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(bx + res.center * cell, by + res.center * cell, cell, cell))
        p.setPen(QPen(QColor(0, 0, 0, 70), 1))
        p.drawRect(frame)
        p.setPen(QColor("#555") if self._bg in ("light", "map", "split") else QColor("#ccc"))
        label_w = 130
        lx = min(self.width() - label_w - 4, bx + size / 2 - label_w / 2)
        p.drawText(QRectF(lx, by + size + 2, label_w, 14),
                   _align(Qt.AlignmentFlag.AlignHCenter), f"{zd}× · blue = hotspot")

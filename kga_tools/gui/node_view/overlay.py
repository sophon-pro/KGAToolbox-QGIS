# -*- coding: utf-8 -*-
"""The canvas item that paints the vertex / node / midpoint markers.

Markers are not drawn one by one with QPainter (thousands of Python calls and QRectF objects per frame).
Each marker shape is rendered once, with antialiasing, into a tiny sprite; the sprite's pixels are then
copied to every point with numpy fancy indexing straight into the item's QImage. 20 000 markers take a few
milliseconds, and the finished image is reused until the view or the data changes - while Smooth
Navigation zooms and pans it simply moves and scales this (top-level) item with the rest of the scene.
"""

import traceback
from contextlib import suppress

import numpy as np
from qgis.core import Qgis, QgsMessageLog, QgsPointXY
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPolygonF

from ...branding import LOG_TAG

#: Above the map image, the Pro selection outline (z -5), below every rubber band and marker (z 0).
OVERLAY_Z = -4
NOTE = 'Zoom in to see vertices and nodes'


class Style(object):
    """Colours and sizes (logical pixels) of the markers."""

    def __init__(self, vertex_color='#505050', node_color='#ff00ff', mid_color='#2f7fd8', vertex_px=9, node_px=12):
        self.vertex_color = QColor(vertex_color)
        self.node_color = QColor(node_color)
        self.mid_color = QColor(mid_color)
        self.vertex_px = int(vertex_px)
        self.node_px = int(node_px)

    def key(self):
        return (self.vertex_color.name(), self.node_color.name(), self.mid_color.name(),
                self.vertex_px, self.node_px)


def image_pixels(image):
    """A writable numpy (height, bytesPerLine / 4) uint32 view of a 32-bit QImage."""
    ptr = image.bits()
    size = image.sizeInBytes() if hasattr(image, 'sizeInBytes') else image.byteCount()
    with suppress(AttributeError):
        ptr.setsize(size)
    return np.frombuffer(ptr, dtype=np.uint32).reshape(image.height(), image.bytesPerLine() // 4)


def make_sprite(shape, size, color, dpr):
    """(dy, dx, value) arrays: the non-transparent pixels of a marker relative to its centre pixel, with
    their premultiplied ARGB values. *shape* is 'square', 'circle' or 'diamond'; *size* is in logical px."""
    side = int(round(size * dpr))
    border = max(1.0, 1.6 * dpr)
    halo = border + 2.0 * max(1.0, dpr * 0.8)
    pad = int(np.ceil(halo)) + 2
    full = side + 2 * pad
    full += full % 2                        # even: the centre falls on a pixel corner, edges stay crisp
    image = QImage(full, full, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, shape != 'square')
        c = full / 2.0
        half = side / 2.0 - border / 2.0
        for width, colour in ((halo, QColor(255, 255, 255, 170)), (border, color)):
            pen = QPen(colour, width)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            if shape == 'circle':
                painter.drawEllipse(QPointF(c, c), half, half)
            elif shape == 'diamond':
                r = half * 1.15
                painter.drawPolygon(QPolygonF([QPointF(c, c - r), QPointF(c + r, c), QPointF(c, c + r),
                                               QPointF(c - r, c)]))
            else:
                painter.drawRect(QRectF(c - half, c - half, 2 * half, 2 * half))
    finally:
        painter.end()
    pixels = image_pixels(image)[:, :full]
    ys, xs = np.nonzero(pixels >> 24)
    return ys - full // 2, xs - full // 2, pixels[ys, xs].copy()


def stamp(buffer, px, py, sprite):
    """Copy *sprite* to every (px, py) of the uint32 image *buffer* (later calls paint over earlier ones).

    One flat index per pixel of every marker (point index + sprite offset) and one assignment: markers well
    inside the image need no clipping at all, only the few near its edges are masked."""
    dy, dx, value = sprite
    height, width = buffer.shape
    if not len(dy) or not len(px):
        return
    reach = int(max(np.abs(dy).max(), np.abs(dx).max()))
    keep = (px >= -reach) & (px < width + reach) & (py >= -reach) & (py < height + reach)
    px, py = px[keep], py[keep]
    inside = (px >= reach) & (px < width - reach) & (py >= reach) & (py < height - reach)
    flat = buffer.reshape(-1)
    offsets = dy.astype(np.int64) * width + dx
    step = max(1, 1_500_000 // len(dy))
    ix, iy = px[inside], py[inside]
    for start in range(0, len(ix), step):
        base = iy[start:start + step] * width + ix[start:start + step]
        flat[base[:, None] + offsets[None, :]] = value
    ex, ey = px[~inside], py[~inside]
    for start in range(0, len(ex), step):
        x = ex[start:start + step, None] + dx[None, :]
        y = ey[start:start + step, None] + dy[None, :]
        ok = (x >= 0) & (x < width) & (y >= 0) & (y < height)
        buffer[y[ok], x[ok]] = np.broadcast_to(value, x.shape)[ok]


def pixel_transform(map_settings, centre):
    """(origin pixel, per-map-x step, per-map-y step) of the map -> pixel mapping around *centre* (x, y).

    The mapping is affine (a rotated map included), so three sample points describe it for every point,
    whatever the rotation."""
    mtp = map_settings.mapToPixel()
    cx, cy = centre
    p0 = mtp.transform(QgsPointXY(cx, cy))
    px = mtp.transform(QgsPointXY(cx + 1.0, cy))
    py = mtp.transform(QgsPointXY(cx, cy + 1.0))
    return ((p0.x(), p0.y()), (px.x() - p0.x(), px.y() - p0.y()), (py.x() - p0.x(), py.y() - p0.y()))


class NodeOverlay(QgsMapCanvasItem):
    """Paints the current Result over the map."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setZValue(OVERLAY_Z)
        self.result = None
        self.style = Style()
        self._sprites = None
        self._sprite_key = None
        self._image = None
        self._image_key = None

    # -- state
    def set_result(self, result):
        self.result = result
        self._image = None
        self.update()

    def set_style(self, style):
        self.style = style
        self._image = None
        self.update()

    def clear(self):
        self.result = None
        self._image = None
        self.update()

    def boundingRect(self):                 # noqa: N802
        return QRectF(0, 0, self.canvas.width(), self.canvas.height())

    def updatePosition(self):               # noqa: N802
        self.setPos(0, 0)

    # -- painting
    def paint(self, painter, option=None, widget=None):
        try:
            self._paint(painter)
        except Exception:                    # never let a paint error reach Qt
            QgsMessageLog.logMessage('Vertices and nodes view failed to paint:\n{}'.format(traceback.format_exc()),
                                     LOG_TAG, Qgis.MessageLevel.Warning)

    def _paint(self, painter):
        result = self.result
        if result is None:
            return
        if result.too_many:
            self._paint_note(painter)
            return
        if not (len(result.squares) or len(result.circles) or len(result.diamonds)):
            return
        settings = self.canvas.mapSettings()
        dpr = self.canvas.devicePixelRatioF()
        extent = settings.visibleExtent()
        key = (round(extent.xMinimum(), 6), round(extent.yMinimum(), 6), round(extent.xMaximum(), 6),
               round(extent.yMaximum(), 6), self.canvas.width(), self.canvas.height(), dpr,
               round(settings.rotation(), 6), id(result), self.style.key())
        if self._image is None or key != self._image_key:
            self._image = self._render(settings, dpr, result)
            self._image_key = key
        painter.drawImage(0, 0, self._image)

    def _render(self, settings, dpr, result):
        width, height = round(self.canvas.width() * dpr), round(self.canvas.height() * dpr)
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        buffer = image_pixels(image)         # 32-bit rows: bytesPerLine is exactly 4 * width
        centre = ((result.rect[0] + result.rect[2]) / 2.0, (result.rect[1] + result.rect[3]) / 2.0)
        (ox, oy), (ax, ay), (bx, by) = pixel_transform(settings, centre)
        sprites = self._get_sprites(dpr)
        for points, name in ((result.diamonds, 'diamond'), (result.squares, 'square'), (result.circles, 'circle')):
            if not len(points):
                continue
            dx, dy = points[:, 0] - centre[0], points[:, 1] - centre[1]
            px = np.rint((ox + dx * ax + dy * bx) * dpr).astype(np.int64)
            py = np.rint((oy + dx * ay + dy * by) * dpr).astype(np.int64)
            stamp(buffer, px, py, sprites[name])
        return image

    def _get_sprites(self, dpr):
        key = (self.style.key(), dpr)
        if self._sprite_key != key:
            style = self.style
            self._sprites = {
                'square': make_sprite('square', style.vertex_px, style.vertex_color, dpr),
                'circle': make_sprite('circle', style.node_px, style.node_color, dpr),
                'diamond': make_sprite('diamond', max(5, style.vertex_px - 1), style.mid_color, dpr),
            }
            self._sprite_key = key
        return self._sprites

    def _paint_note(self, painter):
        painter.save()
        try:
            font = QFont(painter.font())
            font.setPointSizeF(max(font.pointSizeF(), 9.0))
            painter.setFont(font)
            metrics = painter.fontMetrics()
            w, h = metrics.horizontalAdvance(NOTE) + 20, metrics.height() + 10
            rect = QRectF(12, 12, w, h)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(40, 40, 40, 170))
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), NOTE)
        finally:
            painter.restore()

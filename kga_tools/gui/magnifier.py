"""
Magnify Toolbar: a toolbar's buttons swell under the cursor like the macOS Dock (ArcGIS Pro's
"Magnify Toolbar" option).

While the cursor is over the toolbar this overlay sits on top of it and paints every button itself: the
one under the cursor grows to PEAK times its size, its neighbours a little less, and the rest of the row
makes room. Buttons grow toward the map (up for a bottom toolbar) so they pop out of the bar. Sizes follow
the cursor through a short exponential ease, driven by one timer that runs only while something moves.
The overlay also takes the clicks (a magnified button is bigger than the real one underneath) and passes
them on with QAbstractButton.click(). When the cursor leaves and the buttons are back to normal size the
overlay hides and the real buttons are in charge again.
"""

import math
from contextlib import suppress

from qgis.PyQt.QtCore import QElapsedTimer, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QRegion
from qgis.PyQt.QtWidgets import QToolButton, QWidget

PEAK = 1.8             # size of the button under the cursor, as a multiple of its normal size
SIGMA = 0.8            # how fast the effect fades with distance, in button widths
EASE_MS = 55.0         # time constant of the size ease
TICK_MS = 16
REST = 0.004           # scales this close to 1 count as back to normal
PAD = 4                # bar padding around the buttons (matches the real bar)


class _Item:
    __slots__ = ("widget", "kind", "base", "rect", "scale")

    def __init__(self, widget, kind, base):
        self.widget = widget
        self.kind = kind            # 'button', 'arrow' (never grows) or 'line' (separator)
        self.base = base            # QRectF in the toolbar's coordinates when nothing is magnified
        self.rect = QRectF(base)
        self.scale = 1.0


class Magnifier(QWidget):
    """The overlay for the sketch toolbar. The hook methods below (those starting with `_host_`) say where the
    buttons are and how the host is laid out; ToolbarMagnifier (gui/toolbar_magnify.py) overrides them for
    an ordinary docked QToolBar."""

    def __init__(self, bar, parent=None):
        super().__init__(parent if parent is not None else bar)
        self.bar = bar
        self.items = []
        self.active = False
        self.hover = None               # the item under the cursor, or None
        self._press = None
        self._cursor = None             # the cursor's position along the bar's long axis, or None
        self._region = QRegion()
        self._bar_rect = QRectF()
        self._colors = {}
        self._cache = {}
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()

    # ------------------------------------------------------------------ on / off
    # ------------------------------------------------------------------ hooks (sketch toolbar versions)
    def _host_ready(self):
        return self.bar._frame is not None

    def _host_place(self):
        """Cover the host and come to the front (above the gear)."""
        self.setGeometry(self.bar.rect())
        self.show()
        self.raise_()
        if self.bar._gear is not None:
            self.bar._gear.raise_()

    def _host_vertical(self):
        return self.bar._vertical()

    def _host_grow(self):
        """The side the buttons grow toward: 'up', 'down', 'left' or 'right'."""
        return {"bottom": "up", "top": "down", "left": "right", "right": "left"}[self.bar.position]

    def _host_widgets(self):
        """(widget, kind) for every button / separator, kind being 'button', 'arrow' or 'line'."""
        self.bar.layout().activate()               # positions are only valid once the layouts have run
        frame = self.bar._frame
        frame.layout().activate()
        for widget in frame.findChildren(QWidget):
            if widget.isHidden():
                continue
            if isinstance(widget, QToolButton):
                yield widget, ("arrow" if widget.objectName() == "KgaSketchArrow" else "button")
            elif widget.objectName() == "KgaSketchLine":
                yield widget, "line"

    def _host_frame_rect(self):
        return QRectF(self.bar.frame_rect())

    def _host_colors(self):
        return self.bar.colors()

    def _host_set_mask(self, region):
        self.bar.apply_mask(region)

    def _host_base_region(self):
        return self.bar.base_region()

    def _host_click(self, item, _pos):
        item.widget.click()

    def _paint_line(self, painter, item, colors):
        painter.fillRect(item.rect, QColor(colors["line"]))

    def _paint_arrow_mark(self, painter, item, colors):
        """A mark for a button that opens a menu; the sketch toolbar has separate arrow buttons instead."""

    # ------------------------------------------------------------------ on / off
    def activate(self):
        if self.active or not self._host_ready():
            return
        self._host_place()                  # in place first: item rectangles are measured in this widget
        self._collect()
        if not self.items:
            self.hide()
            return
        self._colors = self._host_colors()
        self.active = True
        self._clock.start()
        self._tick()
        self._timer.start()

    def deactivate(self):
        self._timer.stop()
        self.active = False
        self.hover = None
        self._press = None
        self._cursor = None
        self._region = QRegion()
        self.setToolTip("")
        self.hide()
        self._host_set_mask(None)

    def reset(self):
        """Forget everything (the bar was rebuilt: the buttons we held are gone)."""
        self.deactivate()
        self.items = []
        self._cache.clear()

    # ------------------------------------------------------------------ geometry
    def _vertical(self):
        return self._host_vertical()

    def _collect(self):
        vertical = self._vertical()
        found = []
        for widget, kind in self._host_widgets():
            top_left = self.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
            found.append(_Item(widget, kind, QRectF(QRect(top_left, widget.size()))))
        found.sort(key=lambda i: i.base.top() if vertical else i.base.left())
        self.items = found
        self._cache.clear()

    def _targets(self):
        """The size each button is easing toward, given where the cursor is."""
        vertical = self._vertical()
        out = []
        for item in self.items:
            if item.kind != "button" or self._cursor is None:
                out.append(1.0)
                continue
            centre = item.base.center().y() if vertical else item.base.center().x()
            size = item.base.height() if vertical else item.base.width()
            d = abs(self._cursor - centre) / size
            out.append(1.0 + (PEAK - 1.0) * math.exp(-(d / SIGMA) ** 2))
        return out

    def _layout(self):
        """Give every item its magnified rectangle: grown toward the map, the row spread to make room."""
        items = self.items
        if not items:
            return
        vertical = self._vertical()
        grow = self._host_grow()

        def span(r):
            return (r.top(), r.height()) if vertical else (r.left(), r.width())

        starts, centres_old, weights = [], [], []
        cursor = span(items[0].base)[0]
        previous_end = cursor
        for item in items:
            start, length = span(item.base)
            cursor += start - previous_end                      # the original gap before this item
            grown = length * item.scale
            starts.append((cursor, grown))
            centres_old.append(start + length / 2.0)
            weights.append(item.scale - 1.0)
            cursor += grown
            previous_end = start + length
        total = sum(weights)
        shift = 0.0
        if total > 1e-4:
            shift = sum(w * (co - (s + g / 2.0)) for w, co, (s, g) in zip(weights, centres_old, starts)) / total
        for item, (start, grown) in zip(items, starts):
            base = item.base
            s = item.scale
            if vertical:
                side = base.width() * s if item.kind == "button" else base.width()
                x = base.left() if grow == "right" else base.right() - side
                item.rect = QRectF(x, start + shift, side, grown)
            else:
                side = base.height() * s if item.kind == "button" else base.height()
                y = base.top() if grow == "down" else base.bottom() - side
                item.rect = QRectF(start + shift, y, grown, side)
        # the bar itself widens when the row outgrows it
        frame = self._host_frame_rect()
        first, last = items[0].rect, items[-1].rect
        if vertical:
            lo, hi = min(frame.top(), first.top() - PAD), max(frame.bottom(), last.bottom() + PAD)
            self._bar_rect = QRectF(frame.left(), lo, frame.width(), hi - lo)
        else:
            lo, hi = min(frame.left(), first.left() - PAD), max(frame.right(), last.right() + PAD)
            self._bar_rect = QRectF(lo, frame.top(), hi - lo, frame.height())
        region = QRegion(self._bar_rect.toAlignedRect())
        for item in items:
            if item.scale > 1.0 + REST:
                region += QRegion(item.rect.toAlignedRect().adjusted(-1, -1, 1, 1))
        self._region = region

    # ------------------------------------------------------------------ animation
    def _tick(self):
        if not self.active:
            return
        try:
            self._step()
        except RuntimeError:                  # a button went away under us (the bar was rebuilt)
            self.reset()

    def _step(self):
        dt = min(max(self._clock.restart(), 1), 50)
        k = 1.0 - math.exp(-dt / EASE_MS)
        pos = self.mapFromGlobal(QCursor.pos())
        inside = self._region.contains(pos) if not self._region.isEmpty() else self._host_base_region().contains(pos)
        if inside:
            self._cursor = pos.y() if self._vertical() else pos.x()
        else:
            self._cursor = None
        for item, target in zip(self.items, self._targets()):
            item.scale += (target - item.scale) * k
        if self._cursor is None and all(abs(i.scale - 1.0) < REST for i in self.items):
            self.deactivate()
            return
        self._layout()
        self._host_set_mask(self._region)
        hover = self._hit(QPointF(pos)) if inside else None
        if hover is not self.hover:
            self.hover = hover
            self.setToolTip(hover.widget.toolTip() if hover is not None else "")
        self.update()

    def _hit(self, pos):
        for item in sorted(self.items, key=lambda i: i.scale, reverse=True):     # the biggest is drawn last = on top
            if item.kind != "line" and item.rect.contains(pos):
                return item
        return None

    # ------------------------------------------------------------------ painting
    def _pixmap(self, widget, side, enabled):
        dpr = self.devicePixelRatioF()
        px = max(4, int(round(side * dpr / 4.0)) * 4)
        icon = widget.icon()
        key = (id(widget), icon.cacheKey(), px, enabled)
        pix = self._cache.get(key)
        if pix is None:
            if len(self._cache) > 300:
                self._cache.clear()
            pix = icon.pixmap(QSize(px, px), QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled)
            self._cache[key] = pix
        return pix

    def paintEvent(self, _event):
        if not self.active or not self.items:
            return
        t = self._colors
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.setBrush(QColor(t["bg"]))
        painter.drawRoundedRect(self._bar_rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        painter.setPen(Qt.PenStyle.NoPen)
        for item in self.items:
            if item.kind == "line":
                self._paint_line(painter, item, t)
        for item in sorted(self.items, key=lambda i: i.scale):
            if item.kind != "line":
                self._paint_button(painter, item, t)
        painter.end()

    def _paint_button(self, painter, item, t):
        widget = item.widget
        enabled = widget.isEnabled()
        rect = item.rect
        fill = border = None
        if widget.property("open"):
            fill = border = QColor("#0f5fc4")
        elif widget.isChecked():
            fill, border = QColor(t["on"]), QColor(t["on_border"])
        elif enabled and item is self.hover:
            fill, border = QColor(t["hover"]), QColor(t["on_border"]) if item.scale > 1.2 else None
        if fill is not None:
            painter.setBrush(fill)
            painter.setPen(QPen(border, 1) if border is not None else Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 2 + item.scale, 2 + item.scale)
            painter.setPen(Qt.PenStyle.NoPen)
        side = widget.iconSize().width() * (item.scale if item.kind == "button" else 1.0)
        pix = self._pixmap(widget, side, enabled)
        painter.drawPixmap(QRectF(rect.center().x() - side / 2.0, rect.center().y() - side / 2.0, side, side),
                           pix, QRectF(pix.rect()))
        self._paint_arrow_mark(painter, item, t)

    # ------------------------------------------------------------------ clicks
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = self._hit(QPointF(event.pos()))
        event.accept()

    def mouseReleaseEvent(self, event):
        pressed, self._press = self._press, None
        item = self._hit(QPointF(event.pos()))
        event.accept()
        if event.button() != Qt.MouseButton.LeftButton or item is None or item is not pressed:
            return
        widget, opens_list = item.widget, item.kind == "arrow" or self._opens_menu(item.widget)
        if not widget.isEnabled():
            return
        if opens_list:
            self.deactivate()          # the list pops up beside the real button: no magnified bar in the way
        with suppress(RuntimeError):
            self._host_click(item, QPointF(event.pos()))
        if self.active:
            self.update()

    @staticmethod
    def _opens_menu(_widget):
        return False

    def leaveEvent(self, event):
        self._press = None
        super().leaveEvent(event)

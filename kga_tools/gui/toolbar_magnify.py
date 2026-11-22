# -*- coding: utf-8 -*-
"""Magnify for the KGA toolbars (the macOS Dock effect of the sketch toolbar, on an ordinary docked QToolBar).

`ToolbarMagnify(toolbar)` watches a toolbar; when the cursor enters it, a `ToolbarMagnifier` overlay (a child
of the main window, so the swollen buttons can spill over the map) takes over drawing and clicking until the
cursor leaves. Turned on and off by Settings > Appearance > Toolbars.

The overlay only paints and answers clicks for the buttons. A toolbar holding anything else (combo boxes, other
widgets), a floating toolbar, or one with the overflow ">>" button showing, is left alone. A click on a magnified
button is replayed on the real one at the matching spot, so split buttons and menus keep working; a right-click
on the bar is passed on, so the toolbar menu still opens.
"""

from contextlib import suppress

from qgis.PyQt.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QContextMenuEvent, QMouseEvent, QPalette, QPolygonF, QRegion
from qgis.PyQt.QtWidgets import QApplication, QMainWindow, QToolButton

from ..core import kga_settings as ks
from .magnifier import Magnifier

ROOM = 44              # how far the overlay reaches beyond the toolbar, for the swollen buttons


def _is_separator(widget):
    return widget.metaObject().className() == "QToolBarSeparator"


class ToolbarMagnifier(Magnifier):

    def __init__(self, toolbar):
        super().__init__(toolbar, toolbar.parentWidget())

    # ------------------------------------------------------------------ the host
    def _bar_widgets(self):
        """[(widget, kind)] of the toolbar's visible buttons and separators, or None if it holds anything else."""
        out = []
        for action in self.bar.actions():
            widget = self.bar.widgetForAction(action)
            if widget is None or widget.isHidden():
                continue
            if isinstance(widget, QToolButton):
                out.append((widget, "button"))
            elif _is_separator(widget):
                out.append((widget, "line"))
            else:
                return None
        return out

    def _host_ready(self):
        bar = self.bar
        if not ks.magnify_toolbars() or not bar.isVisible() or bar.isFloating():
            return False
        if self.parentWidget() is None or not self._bar_widgets():
            return False
        ext = bar.findChild(QToolButton, "qt_toolbar_ext_button")
        return ext is None or ext.isHidden()

    def _area_grow(self):
        win = self.parentWidget()
        area = win.toolBarArea(self.bar) if isinstance(win, QMainWindow) else Qt.ToolBarArea.TopToolBarArea
        return {Qt.ToolBarArea.BottomToolBarArea: "up", Qt.ToolBarArea.LeftToolBarArea: "right",
                Qt.ToolBarArea.RightToolBarArea: "left"}.get(area, "down")

    def _host_place(self):
        win, bar = self.parentWidget(), self.bar
        top_left = win.mapFromGlobal(bar.mapToGlobal(QPoint(0, 0)))
        r = QRect(top_left, bar.size())
        grow = self._area_grow()
        if self._host_vertical():
            r.adjust(0, -ROOM, 0, ROOM)
            if grow == "right":
                r.adjust(0, 0, ROOM, 0)
            else:
                r.adjust(-ROOM, 0, 0, 0)
        else:
            r.adjust(-ROOM, 0, ROOM, 0)
            if grow == "down":
                r.adjust(0, 0, 0, ROOM)
            else:
                r.adjust(0, -ROOM, 0, 0)
        self.setGeometry(r.intersected(win.rect()))
        self.show()
        self.raise_()

    def _host_vertical(self):
        return self.bar.orientation() == Qt.Orientation.Vertical

    def _host_grow(self):
        return self._area_grow()

    def _host_widgets(self):
        return self._bar_widgets() or []

    def _toolbar_rect(self):
        return QRect(self.mapFromGlobal(self.bar.mapToGlobal(QPoint(0, 0))), self.bar.size())

    def _host_frame_rect(self):
        bar = QRectF(self._toolbar_rect())
        span = QRectF(self.items[0].base)
        for item in self.items:
            span = span.united(item.base)
        if self._host_vertical():
            return QRectF(bar.left(), span.top() - 2, bar.width(), span.height() + 4)
        return QRectF(span.left() - 2, bar.top(), span.width() + 4, bar.height())

    def _host_base_region(self):
        return QRegion(self._toolbar_rect())

    def _host_set_mask(self, region):
        if region is None:
            self.clearMask()
        else:
            self.setMask(region)

    def _host_colors(self):
        pal = self.bar.palette()
        bg = pal.color(QPalette.ColorRole.Window)
        with suppress(Exception):                      # the real background (themes may paint it by style sheet)
            sample = self.bar.grab(QRect(self.bar.width() - 2, 1, 1, 1)).toImage().pixelColor(0, 0)
            if sample.alpha() == 255:
                bg = sample
        hi = pal.color(QPalette.ColorRole.Highlight)
        soft, strong = QColor(hi), QColor(hi)
        soft.setAlpha(60)
        strong.setAlpha(110)
        return {"bg": bg, "border": bg, "hover": soft, "on": strong, "on_border": hi,
                "line": pal.color(QPalette.ColorRole.Mid), "arrow": pal.color(QPalette.ColorRole.WindowText)}

    # ------------------------------------------------------------------ painting
    def _paint_line(self, painter, item, colors):
        r = item.rect
        if self._host_vertical():
            painter.fillRect(QRectF(r.left() + 3, r.center().y() - 0.5, r.width() - 6, 1), QColor(colors["line"]))
        else:
            painter.fillRect(QRectF(r.center().x() - 0.5, r.top() + 3, 1, r.height() - 6), QColor(colors["line"]))

    def _paint_arrow_mark(self, painter, item, colors):
        widget = item.widget
        if not self._opens_menu(widget):
            return
        s, r = item.scale, item.rect
        x, y = r.right() - 6 * s, r.bottom() - 5 * s
        painter.setBrush(QColor(colors["arrow"]))
        painter.drawPolygon(QPolygonF([QPointF(x, y), QPointF(x + 4 * s, y), QPointF(x + 2 * s, y + 2.5 * s)]))

    # ------------------------------------------------------------------ clicks
    @staticmethod
    def _opens_menu(widget):
        action = widget.defaultAction()
        return (widget.menu() is not None or (action is not None and action.menu() is not None)
                or widget.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup)

    def _host_click(self, item, pos):
        """Replay the click on the real button, at the spot of it that was under the cursor."""
        widget, rect = item.widget, item.rect
        local = QPointF((pos.x() - rect.left()) / item.scale, (pos.y() - rect.top()) / item.scale)
        local = QPointF(min(max(local.x(), 0), widget.width() - 1), min(max(local.y(), 0), widget.height() - 1))
        for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                              (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
            event = QMouseEvent(kind, local, QPointF(widget.mapToGlobal(local.toPoint())), Qt.MouseButton.LeftButton,
                                buttons, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, event)

    def contextMenuEvent(self, event):
        """Right-click on the bar: let the toolbar (and QGIS's toolbar menu) answer it."""
        point = self.bar.mapFromGlobal(event.globalPos())
        self.deactivate()
        QApplication.sendEvent(self.bar, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, event.globalPos()))


class ToolbarMagnify(QObject):
    """Attach the magnify effect to one toolbar."""

    def __init__(self, toolbar):
        super().__init__(toolbar)
        self.toolbar = toolbar
        self._mag = None
        toolbar.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.toolbar and event.type() == QEvent.Type.Enter and ks.magnify_toolbars():
            if self._mag is None and self.toolbar.parentWidget() is not None:
                self._mag = ToolbarMagnifier(self.toolbar)
            if self._mag is not None:
                self._mag.activate()
        return False

    def refresh(self):
        if self._mag is not None and not ks.magnify_toolbars():
            self._mag.reset()

    def remove(self):
        with suppress(RuntimeError):
            self.toolbar.removeEventFilter(self)
        if self._mag is not None:
            with suppress(RuntimeError):
                self._mag.reset()
                self._mag.deleteLater()
        self._mag = None
        self.deleteLater()

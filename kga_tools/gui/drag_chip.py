# -*- coding: utf-8 -*-
"""A small badge for drags ('bridges  +7 more'), and the Layers panel drag that uses it.

QGIS's Layers panel drags a picture of every selected row, which can cover
half the window. `LayerDragChip` gives it the same compact badge as the
Data Explorer: the first layer's icon and name, then '+ N more'.

QgsLayerTreeView is a C++ widget, so its startDrag() cannot be overridden from
Python. Instead an event filter on its viewport catches the mouse move that
would start Qt's drag and starts an equivalent drag itself: the same mime data
(from the view's own model), the same actions, and - like Qt's own
QAbstractItemView::startDrag() - the dragged rows are removed afterwards when
the drop was a move. Drops on the Layers panel, the map tabs and anywhere else
see exactly what they saw before; only the picture changes.
"""

from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from qgis.PyQt.QtGui import QColor, QDrag, QFont, QFontMetrics, QMouseEvent, QPainter, QPixmap
from qgis.PyQt.QtWidgets import QApplication


def drag_pixmap(texts, icon):
    """A small badge for the drag: the first name (and '+ N more'), not a picture of every row."""
    label = texts[0] if len(texts) == 1 else "%s  +%d more" % (texts[0], len(texts) - 1)
    font = QFont()
    font.setPointSize(9)
    fm = QFontMetrics(font)
    width = min(fm.horizontalAdvance(label) + 46, 320)
    pix = QPixmap(width, 28)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(255, 255, 255, 235))
    painter.setPen(QColor(120, 160, 210))
    painter.drawRoundedRect(0, 0, width - 1, 27, 4, 4)
    if icon is not None and not icon.isNull():
        painter.drawPixmap(6, 5, icon.pixmap(18, 18))
    painter.setPen(QColor(30, 30, 30))
    painter.setFont(font)
    painter.drawText(QRect(30, 0, width - 36, 28), int(Qt.AlignmentFlag.AlignVCenter),
                     fm.elidedText(label, Qt.TextElideMode.ElideMiddle, width - 36))
    painter.end()
    return pix


def _pos(event):
    if hasattr(event, "position"):
        return event.position().toPoint()
    return event.pos()


class LayerDragChip(QObject):
    """Swaps the Layers panel's row-picture drag for a compact badge."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self._viewport = None
        self._press = None      # viewport position of a left press on a selected row
        self._dragging = False

    # ------------------------------------------------------------ install
    def install(self):
        view = self.iface.layerTreeView()
        if view is None or self._viewport is not None:
            return
        self._viewport = view.viewport()
        self._viewport.installEventFilter(self)

    def uninstall(self):
        if self._viewport is not None and not sip.isdeleted(self._viewport):
            self._viewport.removeEventFilter(self)
        self._viewport = None
        self._press = None

    # ------------------------------------------------------------- events
    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self._press = None
            if event.button() == Qt.MouseButton.LeftButton:
                self._press = _pos(event)
            return False
        if kind == QEvent.Type.MouseButtonRelease:
            self._press = None
            return False
        if kind != QEvent.Type.MouseMove or self._press is None or self._dragging:
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            self._press = None
            return False
        if (_pos(event) - self._press).manhattanLength() < QApplication.startDragDistance():
            return False
        press, self._press = self._press, None
        return self._start(press)

    # --------------------------------------------------------------- drag
    def _draggable(self, view, press):
        """The selected, draggable rows - only when the press was on one of them,
        which is exactly when Qt itself would start a drag."""
        if not view.dragEnabled():
            return []
        index = view.indexAt(press)
        selection = view.selectionModel()
        if not index.isValid() or selection is None or not selection.isSelected(index):
            return []
        model = view.model()
        return [i for i in selection.selectedIndexes()
                if i.column() == 0 and model.flags(i) & Qt.ItemFlag.ItemIsDragEnabled]

    def _start(self, press):
        view = self.iface.layerTreeView()
        if view is None:
            return False
        indexes = self._draggable(view, press)
        if not indexes:
            return False        # not a drag Qt would start: leave the event to the view
        model = view.model()
        mime = model.mimeData(indexes)
        if mime is None:
            return False
        nodes = [view.index2node(i) for i in indexes]
        nodes = [n for n in nodes if n is not None]
        top = self._topmost(nodes)
        names = [n.name() for n in top] or [indexes[0].data()]
        icon = indexes[0].data(Qt.ItemDataRole.DecorationRole)
        if top:
            first = view.node2index(top[0])
            if first.isValid():
                icon = first.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not hasattr(icon, "pixmap"):
            icon = None

        drag = QDrag(view)
        drag.setMimeData(mime)
        drag.setPixmap(drag_pixmap(names, icon))
        drag.setHotSpot(QPoint(12, 14))
        supported = model.supportedDragActions()
        default = view.defaultDropAction()
        if default == Qt.DropAction.IgnoreAction or not (supported & default):
            # Qt's own choice when the view names none (QGIS names Move)
            default = Qt.DropAction.CopyAction if supported & Qt.DropAction.CopyAction \
                else Qt.DropAction.IgnoreAction
        self._dragging = True
        try:
            result = drag.exec(supported, default)
        finally:
            self._dragging = False
            self._reset_view(view)
        if result == Qt.DropAction.MoveAction:
            # what QAbstractItemView::startDrag() does after a move: the drop
            # target inserted copies, so the originals go
            for node in top:
                if sip.isdeleted(node):
                    continue
                parent = node.parent()
                if parent is None:
                    continue
                me = sip.unwrapinstance(node)
                if any(sip.unwrapinstance(c) == me for c in parent.children()):
                    parent.removeChildNode(node)
        return True

    @staticmethod
    def _topmost(nodes):
        """Nodes without a selected ancestor, in the order given."""
        keys = {sip.unwrapinstance(n) for n in nodes}
        out = []
        for node in nodes:
            parent = node.parent()
            inside = False
            while parent is not None:
                if sip.unwrapinstance(parent) in keys:
                    inside = True
                    break
                parent = parent.parent()
            if not inside:
                out.append(node)
        return out

    @staticmethod
    def _reset_view(view):
        reset_drag_state(view)


def reset_drag_state(view):
    """After a drag started in place of an item view's own, the view still waits
    for the drag it never started: a release with Ctrl held off any row ends that
    state without touching the selection."""
    with suppress(RuntimeError, TypeError):
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(-1, -1),
                              QPointF(-1, -1), Qt.MouseButton.LeftButton,
                              Qt.MouseButton.NoButton,
                              Qt.KeyboardModifier.ControlModifier)
        QApplication.sendEvent(view.viewport(), release)

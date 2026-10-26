"""
Overlap switcher - the small "switch selection" button that ArcGIS Pro shows next to a selected
feature when other features lie underneath the click.

* OverlapSwitcher  - owned by SelectTool. After a click selects one of several overlapping features,
                     a button appears beside the click; it opens a list (grouped by layer, with the
                     symbol and display value of every feature under the click) and choosing a row
                     switches the selection to that feature.

The button goes away as soon as the selection changes by any other means, the map is panned or
zoomed, another click is made, or the tool is deactivated.
"""

from contextlib import suppress

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsRenderContext,
    QgsSymbolLayerUtils,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint, QPointF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import QApplication, QFrame, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

BUTTON_SIZE = QSize(40, 34)
BUTTON_OFFSET = QPoint(16, -17)          # top-left of the button relative to the click
SWATCH_PX = 16
MAX_POPUP_ROWS = 14

_FILL = QColor(197, 220, 240)
_FILL_HOVER = QColor(176, 208, 236)
_BORDER = QColor(122, 167, 199)
_INK = QColor(50, 62, 74)


# ---------------------------------------------------------------------------------- row content
def feature_label(layer, feature):
    """The value Pro would show: the layer's display expression, else a sensible field, else the id."""
    with suppress(Exception):
        text = layer.displayExpression()
        if text:
            ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            ctx.setFeature(feature)
            expr = QgsExpression(text)
            value = expr.evaluate(ctx)
            if not expr.hasEvalError() and value not in (None, "") and str(value) != "NULL":
                return str(value)
    with suppress(Exception):
        for name in (layer.displayField(),):
            if name and feature.fieldNameIndex(name) >= 0 and feature[name] not in (None, ""):
                return str(feature[name])
    return str(feature.id())


def feature_icon(layer, feature):
    """The feature's own symbol as a small swatch (falls back to an empty icon)."""
    with suppress(Exception):
        renderer = layer.renderer()
        if renderer is not None:
            ctx = QgsRenderContext()
            ctx.setExpressionContext(QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer)))
            renderer.startRender(ctx, layer.fields())
            try:
                symbol = renderer.symbolForFeature(feature, ctx)
                if symbol is None:
                    symbol = renderer.symbol()
                if symbol is not None:
                    return QgsSymbolLayerUtils.symbolPreviewIcon(symbol, QSize(SWATCH_PX, SWATCH_PX))
            finally:
                renderer.stopRender(ctx)
    return QIcon()


# ---------------------------------------------------------------------------------- popup list
class _ChoicePopup(QFrame):
    """The list that drops down from the button. Rows are indexes into the caller's candidate list."""

    chosen = pyqtSignal(int)

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("kgaOverlapPopup")
        self.setStyleSheet(
            "#kgaOverlapPopup{background:#ffffff;border:1px solid #9aa5b1;}"
            "QListWidget{border:none;background:#ffffff;outline:0;}"
            "QListWidget::item{padding:3px 8px 3px 4px;border:1px solid transparent;}"
            "QListWidget::item:hover{background:#e3f0fa;}"
            "QListWidget::item:selected{background:#c5dcf0;border:1px solid #7aa7c7;color:#1b2a38;}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self.list = QListWidget(self)
        self.list.setIconSize(QSize(SWATCH_PX, SWATCH_PX))
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(self._clicked)
        layout.addWidget(self.list)

    def fill(self, rows, current):
        """rows: [(index, layer_name, icon, label)] in display order; current: the selected index."""
        self.list.clear()
        header_font = QFont(self.list.font())
        header_font.setBold(True)
        last_layer, height, shown = None, 0, 0
        for index, layer_name, icon, label in rows:
            if layer_name != last_layer:
                header = QListWidgetItem(layer_name)
                header.setFont(header_font)
                header.setFlags(Qt.ItemFlag.NoItemFlags)           # a title, not a choice
                header.setForeground(_INK)
                self.list.addItem(header)
                last_layer = layer_name
                shown += 1
            item = QListWidgetItem(icon, label)
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)
            if index == current:
                self.list.setCurrentItem(item)
            shown += 1
        rows_px = self.list.sizeHintForRow(0) if self.list.count() else 20
        visible = min(shown, MAX_POPUP_ROWS)
        width = max(self.list.sizeHintForColumn(0) + 28, 130)
        self.list.setFixedSize(width, rows_px * visible + 4)
        self.adjustSize()

    def _clicked(self, item):
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is not None:
            self.hide()
            self.chosen.emit(int(index))


# ---------------------------------------------------------------------------------- the button
class _SwitchButton(QWidget):
    """A pale-blue button: stacked-features icon plus a drop-down chevron."""

    pressed = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(BUTTON_SIZE)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMouseTracking(True)
        self.setToolTip("Switch the selection to another feature under this point")
        self._hover = False
        self._down = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()
            self.pressed.emit()
        e.accept()

    def mouseReleaseEvent(self, e):
        self._down = False
        self.update()
        e.accept()

    def mouseMoveEvent(self, e):
        e.accept()

    def mouseDoubleClickEvent(self, e):
        e.accept()

    def wheelEvent(self, e):
        e.ignore()                     # let the canvas zoom even over the button

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(_FILL_HOVER if (self._hover or self._down) else _FILL)
        p.setPen(QPen(_BORDER, 1))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)

        # two overlapping features, the front one picked, with a pointer
        p.setPen(QPen(_INK, 1))
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawRect(6, 8, 13, 12)
        p.setBrush(QColor(235, 247, 215))
        p.drawRect(9, 12, 13, 12)
        arrow = QPainterPath()
        arrow.moveTo(QPointF(15, 15))
        arrow.lineTo(QPointF(15, 27))
        arrow.lineTo(QPointF(18, 24))
        arrow.lineTo(QPointF(21, 29))
        arrow.lineTo(QPointF(23, 28))
        arrow.lineTo(QPointF(20.5, 23))
        arrow.lineTo(QPointF(25, 23))
        arrow.closeSubpath()
        p.setBrush(QColor(255, 255, 255))
        p.drawPath(arrow)

        # chevron
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_INK, 1.6))
        cx, cy = self.width() - 9, self.height() // 2
        p.drawPolyline(QPointF(cx - 3.5, cy - 1.5), QPointF(cx, cy + 2), QPointF(cx + 3.5, cy - 1.5))
        p.end()


# ---------------------------------------------------------------------------------- controller
class OverlapSwitcher:
    """Shows the button for a SelectTool and relays the user's choice back to it."""

    def __init__(self, canvas, tool):
        self.canvas = canvas
        self.tool = tool
        self._candidates = []
        self._index = 0
        self._busy = False
        self.button = _SwitchButton(canvas)
        self.button.hide()
        self.button.pressed.connect(self._open_popup)
        self.popup = _ChoicePopup(canvas)
        self.popup.hide()
        self.popup.chosen.connect(self._chosen)
        canvas.extentsChanged.connect(self.hide)
        canvas.selectionChanged.connect(self._selection_changed)

    # ------------------------------------------------------------------ lifecycle
    def dispose(self):
        with suppress(Exception):
            self.canvas.extentsChanged.disconnect(self.hide)
            self.canvas.selectionChanged.disconnect(self._selection_changed)
        for widget in (self.popup, self.button):
            with suppress(Exception):
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def visible(self):
        return not sip.isdeleted(self.button) and self.button.isVisible()

    def hide(self, *_args):
        self._candidates = []
        if sip.isdeleted(self.button):
            return
        self.popup.hide()
        self.button.hide()

    def show_for(self, candidates, index, pixel):
        """Show the button beside `pixel` (canvas pixels) for a click that found `candidates`."""
        if len(candidates) < 2:
            self.hide()
            return
        self._candidates = list(candidates)
        self._index = index
        pos = QPoint(int(pixel.x()), int(pixel.y())) + BUTTON_OFFSET
        x = max(2, min(pos.x(), self.canvas.width() - BUTTON_SIZE.width() - 2))
        y = max(2, min(pos.y(), self.canvas.height() - BUTTON_SIZE.height() - 2))
        self.button.move(x, y)
        self.button.show()
        self.button.raise_()

    def set_current(self, index):
        self._index = index

    # ------------------------------------------------------------------ popup
    def _rows(self):
        rows = []
        for i, (layer, fid) in enumerate(self._candidates):
            if sip.isdeleted(layer):
                continue
            feature = next(iter(layer.getFeatures(QgsFeatureRequest(fid))), None)
            if feature is None:
                continue
            rows.append((i, layer.name(), feature_icon(layer, feature), feature_label(layer, feature)))
        return rows

    def _open_popup(self):
        if self.popup.isVisible():
            self.popup.hide()
            return
        rows = self._rows()
        if not rows:
            return
        self.popup.fill(rows, self._index)
        below = self.button.mapToGlobal(QPoint(0, self.button.height()))
        screen = QApplication.screenAt(below)
        if screen is not None:
            area = screen.availableGeometry()
            if below.y() + self.popup.height() > area.bottom():
                below = self.button.mapToGlobal(QPoint(0, -self.popup.height()))
            below.setX(max(area.left(), min(below.x(), area.right() - self.popup.width())))
        self.popup.move(below)
        self.popup.show()

    def _chosen(self, index):
        if index == self._index:
            return
        self._busy = True
        try:
            self.tool.goto_candidate(index)
        finally:
            self._busy = False

    # ------------------------------------------------------------------ keeping in step
    def _selection_changed(self, *_args):
        """Any selection change we did not cause means the user has moved on."""
        if self._busy or not self.visible():
            return
        layer, fid = self._candidates[self._index] if self._candidates else (None, None)
        if layer is None or sip.isdeleted(layer) or set(layer.selectedFeatureIds()) != {fid}:
            self.hide()
            return
        from .selection import selection_summary
        if selection_summary()[0] != 1:
            self.hide()


__all__ = ["OverlapSwitcher", "feature_label", "feature_icon"]

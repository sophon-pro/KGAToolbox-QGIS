# -*- coding: utf-8 -*-
"""Go To XY: a small bar over the map, like ArcGIS Pro's.

Pick a unit, type X and Y (or one MGRS reference) and press Zoom To. The map
moves there and a temporary point is drawn with the symbol chosen on the bar.
The point lives only on the map canvas - it is not a layer and is not saved
with the project - and goes away with Clear, the next Zoom To, or closing the
bar.

The chevron at the left folds the bar down to a single button.
"""

import os
import traceback
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsCoordinateTransform,
                       QgsProject, QgsTask, QgsMarkerSymbol, QgsMarkerSymbolLayer, QgsMessageLog, QgsPointXY,
                       QgsRectangle, QgsRenderContext, QgsSettings,
                       QgsSvgMarkerSymbolLayer, QgsSymbolLayerUtils)
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import (QEvent, QPointF, QRectF, QSize, Qt, QTimer,
                              pyqtSignal)
from qgis.PyQt.QtGui import (QColor, QIcon, QPainter, QPixmap,
                             QTextDocument)
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QListWidget,
                                 QListWidgetItem, QStyle,
                                 QStyledItemDelegate, QComboBox, QFrame, QMenu, QHBoxLayout, QLabel,
                                 QLineEdit, QPushButton, QToolButton, QWidget)

from ..branding import LOG_TAG
from ..core import go_to_xy as G
from . import window_reveal

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

SETTING_UNIT = 'kga_tools/go_to_xy/unit'
SETTING_COMBINED = 'kga_tools/go_to_xy/combined'
#: The map scale a typed position is shown at, unless the map is already
#: closer than that.
ZOOM_SCALE = 5000
MARGIN = 8
ROW_HEIGHT = 26
BLUE = '#174592'

STYLE = """
QFrame#KgaGoToXYFrame { background: palette(window); border: 1px solid palette(mid);
    border-radius: 3px; }
QFrame#KgaGoToXYFrame QLabel { background: transparent; }
QToolButton#KgaGoToXYFold { background: palette(window); border: 1px solid palette(mid);
    border-radius: 10px; }
QToolButton#KgaGoToXYFold:hover { background: palette(button); }
QPushButton { padding: 0 10px; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 0 14px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:pressed { background: #0f2f66; }
QLineEdit[bad="true"] { border: 1px solid #d32f2f; }
""" % {'blue': BLUE}


def tr(text):
    return text


#: Overridable so a test can search and run without the network or a thread.
search_places = G.search_places
suggest_places = G.suggest_places
resolve_map_link = G.resolve_map_link
#: How long to wait after a keystroke before asking for suggestions.
SUGGEST_DELAY_MS = 350


def run_in_background(work, done):
    """Run `work()` off the GUI thread; `done(result, error)` runs on it."""
    def job(_task):
        return [work()]                 # [] would come back as None

    def finished(exception, result=None):
        done(result[0] if result else None, exception)

    task = QgsTask.fromFunction(tr('Go To XY'), job, on_finished=finished)
    QgsApplication.taskManager().addTask(task)
    return task


PIN_SVG = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'icons', 'navigation', 'pin.svg')
PIN_SIZE = 7.0

#: (label, kind, colour). Pins have their tip on the point.
PRESETS = (
    ('Red pin', 'pin', '#e53935'),
    ('Blue pin', 'pin', '#1e66d0'),
    ('Green pin', 'pin', '#2e9e44'),
    ('Orange pin', 'pin', '#f57c00'),
    ('Yellow pin', 'pin', '#f9c80e'),
    ('Purple pin', 'pin', '#8e44ad'),
    ('Black pin', 'pin', '#222222'),
    ('Red circle', 'circle', '#e53935'),
    ('Blue circle', 'circle', '#1e66d0'),
    ('Red star', 'star', '#e53935'),
    ('Red cross', 'cross', '#e53935'),
    ('Red diamond', 'diamond', '#e53935'),
)


def pin_symbol(color='#e53935'):
    """A teardrop pin whose tip sits on the point."""
    layer = QgsSvgMarkerSymbolLayer(PIN_SVG, PIN_SIZE, 0.0)
    layer.setFillColor(QColor(color))
    layer.setStrokeColor(QColor('#ffffff'))
    layer.setStrokeWidth(0.5)
    layer.setVerticalAnchorPoint(
        QgsMarkerSymbolLayer.VerticalAnchorPoint.Bottom)
    return QgsMarkerSymbol([layer])


def pin_icon(color):
    """The pin drawn whole in a small icon (a symbol preview would clip it,
    because the pin stands above its point)."""
    with open(PIN_SVG, encoding='utf-8') as handle:
        text = handle.read()
    text = text.replace('param(fill) #e53935', color)
    text = text.replace('param(fill-opacity) 1', '1')
    text = text.replace('param(outline) #ffffff', '#ffffff')
    text = text.replace('param(outline-opacity) 1', '1')
    text = text.replace('param(outline-width) 1.4', '1.4')
    renderer = QSvgRenderer(text.encode('utf-8'))
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(4.5, 0, 15, 24))
    painter.end()
    return QIcon(pixmap)


def preset_symbol(kind, color):
    if kind == 'pin':
        return pin_symbol(color)
    # A cross is drawn only with its outline, so that carries the colour.
    outline = color if kind == 'cross' else '#ffffff'
    return QgsMarkerSymbol.createSimple({
        'name': kind, 'color': color, 'outline_color': outline,
        'outline_width': '0.9' if kind == 'cross' else '0.6',
        'size': '4.5' if kind == 'circle' else '5.5'})


def default_symbol():
    return pin_symbol()


ROLE_TITLE = Qt.ItemDataRole.UserRole
ROLE_DETAIL = Qt.ItemDataRole.UserRole + 1
ROLE_FOUND = Qt.ItemDataRole.UserRole + 2
ROW_H = 34


def _bold_match(title, query):
    """The title as HTML with the typed part in bold."""
    from html import escape
    typed = query.strip()
    at = title.lower().find(typed.lower()) if typed else -1
    if at < 0:
        return escape(title)
    end = at + len(typed)
    return '{}<b>{}</b>{}'.format(escape(title[:at]), escape(title[at:end]),
                                  escape(title[end:]))


class _RowDelegate(QStyledItemDelegate):
    """One suggestion: a pin, the name with the typed part bold, then where
    it is in grey."""

    def __init__(self, parent):
        super().__init__(parent)
        self.query = ''

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, painter, option, index):
        from html import escape
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        if selected:
            painter.fillRect(option.rect, option.palette.highlight())
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        left = option.rect.left() + 10
        if icon is not None:
            icon.paint(painter, left, option.rect.top() + (ROW_H - 18) // 2,
                       18, 18)
        if selected:
            text = grey = option.palette.highlightedText().color().name()
        else:
            text = option.palette.text().color().name()
            grey = option.palette.mid().color().name()
        html = ('<span style="color:{0}">{1}</span> '
                '<span style="color:{2}">{3}</span>').format(
                    text, _bold_match(index.data(ROLE_TITLE) or '',
                                      self.query),
                    grey, escape(index.data(ROLE_DETAIL) or ''))
        doc = QTextDocument()
        doc.setHtml(html)
        painter.translate(left + 28, option.rect.top()
                          + (ROW_H - doc.size().height()) / 2)
        doc.drawContents(painter)
        painter.restore()


class SuggestPopup(QListWidget):
    """The drop-down of places under the search box. It never takes the
    keyboard: the box keeps focus and forwards Up, Down, Enter and Esc."""

    chosen = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setStyleSheet('QListWidget { border: 1px solid palette(mid); '
                           'background: palette(base); }')
        self.delegate = _RowDelegate(self)
        self.setItemDelegate(self.delegate)
        self._icon = pin_icon('#757575')
        self.itemClicked.connect(self._clicked)
        self.itemEntered.connect(self.setCurrentItem)

    def show_results(self, results, query, anchor):
        """Fill the list and open it under ``anchor`` (a widget)."""
        self.clear()
        self.delegate.query = query
        for found in results:
            item = QListWidgetItem(self._icon, found.label)
            item.setData(ROLE_TITLE, found.title)
            item.setData(ROLE_DETAIL, found.detail)
            item.setData(ROLE_FOUND, found)
            item.setSizeHint(QSize(100, ROW_H))
            self.addItem(item)
        self.setCurrentRow(-1)
        if not results:
            self.hide()
            return
        width = max(anchor.width() + 120, 380)
        self.resize(width, ROW_H * len(results) + 4)
        self.move(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        self.show()
        self.raise_()

    def move_selection(self, step):
        """Down (+1) / Up (-1); Up from the first row goes back to none."""
        count = self.count()
        if not count:
            return
        row = self.currentRow() + step
        if row >= count:
            row = count - 1
        self.setCurrentRow(row)             # -1 clears the selection

    def current_found(self):
        item = self.currentItem()
        return item.data(ROLE_FOUND) if item is not None else None

    def _clicked(self, item):
        self.chosen.emit(item.data(ROLE_FOUND))


class TempPoint(QgsMapCanvasItem):
    """One point on the map canvas, painted with a marker symbol.

    Kept as a WGS 84 position so it stays put when the map's coordinate system
    changes."""

    def __init__(self, canvas, symbol=None):
        super().__init__(canvas)
        self.canvas = canvas
        self._symbol = symbol or default_symbol()
        self._latlon = None
        self.setZValue(1000)
        canvas.destinationCrsChanged.connect(self.updatePosition)

    def has_point(self):
        return self._latlon is not None

    def latlon(self):
        return self._latlon

    def set_symbol(self, symbol):
        self._symbol = symbol
        self.prepareGeometryChange()
        self.update()

    def set_latlon(self, lat, lon):
        self._latlon = (lat, lon)
        self.updatePosition()
        self.update()

    def clear(self):
        self._latlon = None
        self.update()

    def map_point(self):
        """The position in the canvas's coordinate system, or None."""
        if self._latlon is None:
            return None
        lat, lon = self._latlon
        try:
            return G._convert(QgsPointXY(lon, lat), G.wgs84(),
                              self.canvas.mapSettings().destinationCrs())
        except G.GoToError:
            return None

    def _radius(self):
        dpi = self.canvas.logicalDpiX() or 96
        size = getattr(self._symbol, 'size', lambda: 4.0)()
        return max(12.0, size * dpi / 25.4 * 1.2 + 6.0)

    def boundingRect(self):
        r = self._radius()
        return QRectF(-r, -r, 2 * r, 2 * r)

    def updatePosition(self, *_args):
        point = self.map_point()
        if point is None:
            self.hide()
            return
        self.show()
        self.setPos(self.toCanvasCoordinates(point))

    def paint(self, painter, option=None, widget=None):
        if self._latlon is None or self._symbol is None:
            return
        context = QgsRenderContext.fromQPainter(painter)
        symbol = self._symbol.clone()
        symbol.startRender(context)
        try:
            symbol.renderPoint(QPointF(0, 0), None, context)
        finally:
            symbol.stopRender(context)

    def dispose(self):
        with suppress(Exception):
            self.canvas.destinationCrsChanged.disconnect(self.updatePosition)
        with suppress(Exception):
            self.canvas.scene().removeItem(self)


class GoToXYBar(QWidget):
    """Floats over the top left of the map canvas."""

    closed = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.last_message = ''
        self.results = []
        self._suggest_seq = 0
        self._typed_len = 0
        self._task = None
        self._menu = None
        self._disposed = False
        self._wanted = False
        self.button = None                  # the toolbar button the bar opens from / closes into
        self.setObjectName('KgaGoToXYBar')
        self.setStyleSheet(STYLE)
        self.point = TempPoint(canvas)
        self.popup = SuggestPopup()

        row = QHBoxLayout(self)
        row.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        row.setSpacing(6)

        self.fold = QToolButton(self)
        self.fold.setObjectName('KgaGoToXYFold')
        self.fold.setFixedSize(20, 20)
        self.fold.setArrowType(Qt.ArrowType.DownArrow)
        self.fold.setToolTip(tr('Fold the bar away or open it again'))
        self.fold.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.fold.clicked.connect(self._toggle_fold)
        row.addWidget(self.fold, 0, Qt.AlignmentFlag.AlignTop)

        self.frame = QFrame(self)
        self.frame.setObjectName('KgaGoToXYFrame')
        inner = QHBoxLayout(self.frame)
        inner.setContentsMargins(8, 5, 6, 5)
        inner.setSpacing(6)
        row.addWidget(self.frame)

        self.x_label = QLabel('X:')
        self.x_edit = QLineEdit()
        self.x_edit.setMinimumWidth(110)
        self.y_label = QLabel('Y:')
        self.y_edit = QLineEdit()
        self.y_edit.setMinimumWidth(110)
        self.zone_label = QLabel(tr('Zone:'))
        self.zone_edit = QLineEdit()
        self.zone_edit.setFixedWidth(52)
        self.zone_edit.setToolTip(tr(
            'The UTM zone and hemisphere, for example 48N or 33S'))

        self.unit_combo = QComboBox()
        for unit_id, label in G.UNITS:
            self.unit_combo.addItem(label, unit_id)
        self.unit_combo.setToolTip(tr('The unit of the coordinate you type'))

        self.combined_check = QCheckBox(tr('Combined'))
        self.combined_check.setToolTip(tr(
            'Type or paste both values in one box, for example '
            '11.5601, 104.9151 (latitude first for degrees)'))
        self.combined_check.setChecked(
            str(QgsSettings().value(SETTING_COMBINED, 'false')).lower()
            == 'true')

        self.zoom_btn = QPushButton(tr('Zoom To'))
        self.zoom_btn.setObjectName('kgaPrimary')
        self.zoom_btn.setToolTip(tr(
            'Move the map to this position and mark it with a temporary '
            'point'))
        self.zoom_btn.setAutoDefault(False)

        self.symbol = default_symbol()

        self.pin_btn = QToolButton()
        self.pin_btn.setIcon(self._preset_icon(PRESETS[0]))
        self.pin_btn.setIconSize(QSize(18, 18))
        self.pin_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pin_btn.setToolTip(tr('Choose the pin style of the temporary point'))
        self.pin_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        menu = QMenu(self.pin_btn)
        for preset in PRESETS:
            action = menu.addAction(self._preset_icon(preset), tr(preset[0]))
            action.triggered.connect(
                lambda _c=False, p=preset: self.use_preset(p))
        self.pin_btn.setMenu(menu)

        self.clear_btn = QPushButton(tr('Clear'))
        self.clear_btn.setAutoDefault(False)
        self.clear_btn.setToolTip(tr('Remove the temporary point from the map'))
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn = QToolButton()
        self.close_btn.setText('×')
        self.close_btn.setStyleSheet('font-size: 16px;')
        self.close_btn.setAutoRaise(True)
        self.close_btn.setToolTip(tr('Close Go To XY'))
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        for widget in (self.x_edit, self.y_edit, self.zone_edit,
                       self.unit_combo, self.zoom_btn, self.clear_btn):
            widget.setFixedHeight(ROW_HEIGHT)
        self.close_btn.setFixedSize(ROW_HEIGHT, ROW_HEIGHT)
        self.pin_btn.setFixedSize(46, ROW_HEIGHT)

        for widget in (self.x_label, self.x_edit, self.y_label, self.y_edit,
                       self.zone_label, self.zone_edit, self.unit_combo,
                       self.combined_check,
                       self.zoom_btn, self.pin_btn, self.clear_btn,
                       self.close_btn):
            inner.addWidget(widget)

        saved = QgsSettings().value(SETTING_UNIT, G.DEFAULT_UNIT)
        index = self.unit_combo.findData(saved)
        self.unit_combo.setCurrentIndex(max(index, 0))
        self._unit_changed()

        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        self.combined_check.toggled.connect(self._combined_toggled)
        self.zoom_btn.clicked.connect(self.zoom)
        for edit in (self.x_edit, self.y_edit, self.zone_edit):
            edit.returnPressed.connect(self.zoom)
            edit.textEdited.connect(lambda _t, e=edit: self._mark(e, False))
        self.clear_btn.clicked.connect(self.clear)
        self.close_btn.clicked.connect(self.close_bar)

        self.popup.chosen.connect(self._suggestion_chosen)
        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.timeout.connect(self._ask_suggestions)
        self.x_edit.textEdited.connect(self._typed)
        self.x_edit.installEventFilter(self)

        canvas.installEventFilter(self)
        self.hide()

    # --------------------------------------------------------------- layout

    @staticmethod
    def _preset_icon(preset):
        _label, kind, color = preset
        if kind == 'pin':
            return pin_icon(color)
        return QgsSymbolLayerUtils.symbolPreviewIcon(
            preset_symbol(kind, color), QSize(24, 24))

    def use_preset(self, preset):
        """Make the point look like a ready-made pin or marker."""
        _label, kind, color = preset
        self.symbol = preset_symbol(kind, color)
        self.point.set_symbol(self.symbol)
        self.pin_btn.setIcon(self._preset_icon(preset))

    def unit(self):
        return self.unit_combo.currentData()

    def _unit_changed(self, *_args):
        self.popup.hide()
        unit = self.unit()
        can_combine = unit in G.PAIRABLE
        combined = can_combine and self.combined_check.isChecked()
        pair = unit not in G.GRIDS and unit != G.SEARCH and not combined
        x_hint, y_hint = G.EXAMPLES.get(unit, ('', ''))
        label = 'X:' if pair else unit_name(unit) + ':'
        if combined:
            label = 'Lat, Lon:' if unit in G.ANGLES else 'X, Y:'
            x_hint = G.COMBINED_EXAMPLES.get(unit, '')
        self.combined_check.setVisible(can_combine)
        self.x_label.setText(label)
        self.zoom_btn.setText(tr('Search') if unit == G.SEARCH
                              else tr('Zoom To'))
        self.x_edit.setPlaceholderText(x_hint)
        self.y_label.setVisible(pair)
        self.y_edit.setVisible(pair)
        self.y_edit.setPlaceholderText(y_hint)
        self.zone_label.setVisible(unit == 'utm')
        self.zone_edit.setVisible(unit == 'utm')
        self.zone_edit.setPlaceholderText(G.ZONE_EXAMPLE)
        self.x_edit.setMinimumWidth(
            110 if pair else (270 if unit == G.SEARCH else 220
                              if combined else 190))
        with suppress(Exception):
            QgsSettings().setValue(SETTING_UNIT, unit)
        for edit in (self.x_edit, self.y_edit, self.zone_edit):
            self._mark(edit, False)
        self.adjustSize()
        self.reposition()

    def _combined_toggled(self, on):
        with suppress(Exception):
            QgsSettings().setValue(SETTING_COMBINED, 'true' if on else 'false')
        self._unit_changed()

    def combined(self):
        return self.unit() in G.PAIRABLE and self.combined_check.isChecked()

    def _toggle_fold(self):
        folded = not self.frame.isHidden()
        self.frame.setVisible(not folded)
        self.fold.setArrowType(Qt.ArrowType.RightArrow if folded
                               else Qt.ArrowType.DownArrow)
        self.adjustSize()
        self.reposition()

    def reposition(self):
        if self._disposed:
            return
        self.adjustSize()
        self.move(0, 0)

    def eventFilter(self, obj, event):
        if obj is self.x_edit and self.popup.isVisible():
            kind = event.type()
            if kind == QEvent.Type.KeyPress:
                key = event.key()
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    self.popup.move_selection(
                        1 if key == Qt.Key.Key_Down else -1)
                    return True
                if key == Qt.Key.Key_Escape:
                    self.popup.hide()
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    found = self.popup.current_found()
                    if found is not None:
                        self._suggestion_chosen(found)
                        return True
                    self.popup.hide()
            elif kind == QEvent.Type.FocusOut:
                QTimer.singleShot(150, self._hide_if_unfocused)
        if obj is self.canvas and event.type() == QEvent.Type.Resize \
                and self.isVisible():
            QTimer.singleShot(0, self.reposition)
        return False

    def _place(self):
        self.show()
        self.raise_()
        self.reposition()
        self.x_edit.setFocus()

    def show_bar(self):
        """Show the bar; it opens out of the toolbar button the user just pressed."""
        self._wanted = True
        if self.isVisible():
            self._place()
            return
        window_reveal.reveal_widget(self, self._origin(), self._land)

    def _origin(self):
        """Where the bar flies from / to: the button just pressed, else its own toolbar button."""
        origin = window_reveal.take_origin()
        if origin is None:
            origin = window_reveal.button_rect(self.button)
        return origin

    def _land(self):
        if self._wanted and not self._disposed:     # closed again during the animation: stay hidden
            self._place()

    def close_bar(self):
        self._wanted = False
        self.popup.hide()
        self.clear()
        if self.isVisible() and not self._disposed:
            window_reveal.close_widget(self, self._origin())
        self.hide()
        self.closed.emit()

    @staticmethod
    def _mark(edit, bad):
        edit.setProperty('bad', bad)
        edit.style().unpolish(edit)
        edit.style().polish(edit)

    # ----------------------------------------------------------------- work

    def map_crs(self):
        return self.canvas.mapSettings().destinationCrs()

    def _say(self, text, error=True):
        self.last_message = text
        if iface is None:
            return
        bar = iface.messageBar()
        if error:
            bar.pushWarning(tr('Go To XY'), text)
        else:
            bar.pushInfo(tr('Go To XY'), text)

    def zoom(self):
        """Move the map to the typed position and mark it."""
        self.last_message = ''
        for edit in (self.x_edit, self.y_edit, self.zone_edit):
            self._mark(edit, False)
        if self.unit() == G.SEARCH:
            return self._search()
        try:
            place = G.locate(self.unit(), self.x_edit.text(),
                             self.y_edit.text(), self.zone_edit.text(),
                             self.map_crs(), combined=self.combined())
        except G.GoToError as exc:
            self._flag_bad()
            self._say(str(exc))
            return False
        except Exception as exc:                  # noqa: BLE001
            QgsMessageLog.logMessage(
                'Go To XY failed:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self._say(str(exc))
            return False

        self._go(place)
        return True

    def _go(self, place):
        """Mark the place and bring it into view."""
        self.point.set_symbol(self.symbol)
        self.point.set_latlon(place.lat, place.lon)
        canvas = self.canvas
        box = canvas.extent()
        if box.isNull() or box.isEmpty() or not box.isFinite():
            # A map with nothing in it has no usable extent to move.
            canvas.setExtent(QgsRectangle.fromCenterAndSize(
                place.point, 1000.0, 1000.0))
        frame = self._area_in_map(place.extent)
        if frame is not None:
            canvas.setExtent(frame)
        else:
            canvas.setCenter(place.point)
            if canvas.scale() > ZOOM_SCALE:
                canvas.zoomScale(ZOOM_SCALE)
        canvas.refresh()

    def _area_in_map(self, extent):
        """A found area's box in the map's coordinate system, a little
        wider, or None when it cannot be drawn there."""
        if extent is None:
            return None
        try:
            transform = QgsCoordinateTransform(
                G.wgs84(), self.map_crs(), QgsProject.instance())
            box = transform.transformBoundingBox(extent)
        except Exception:                       # noqa: BLE001
            return None
        if (box.isNull() or not box.isFinite() or box.width() <= 0
                or box.height() <= 0):
            return None
        box.scale(1.15)
        return box

    # --------------------------------------------------------------- search

    # ---------------------------------------------------------- suggestions

    def _typed(self, _text=''):
        """A key was pressed in the search box: ask for places soon."""
        if self.unit() != G.SEARCH:
            return
        text = self.x_edit.text().strip()
        pasted = len(text) - self._typed_len > 8
        self._typed_len = len(text)
        self._suggest_timer.stop()
        self._suggest_seq += 1
        if G.is_map_link(text):
            self.popup.hide()
            if pasted:                  # a pasted link needs no Enter
                self._suggest_timer.start(SUGGEST_DELAY_MS)
            return
        if len(text) < G.SUGGEST_MIN_CHARS:
            self.popup.hide()
            return
        self._suggest_timer.start(SUGGEST_DELAY_MS)

    def _bias(self):
        """The middle of the map as (lat, lon), so nearby places rank first."""
        try:
            centre = G._convert(self.canvas.center(), self.map_crs(),
                                G.wgs84())
            return centre.y(), centre.x()
        except Exception:                       # noqa: BLE001
            return None

    def _ask_suggestions(self):
        text = self.x_edit.text().strip()
        if G.is_map_link(text):
            self._resolve_link(text)
            return
        seq = self._suggest_seq
        bias = self._bias()
        self._suggest_task = run_in_background(
            lambda: suggest_places(text, bias),
            lambda results, error: self._got_suggestions(
                seq, text, results, error))

    def _got_suggestions(self, seq, text, results, error):
        if self._disposed or seq != self._suggest_seq or error is not None:
            return                  # a newer key was pressed, or no network
        if self.unit() != G.SEARCH:
            return
        self.popup.show_results(results or [], text, self.x_edit)

    def _suggestion_chosen(self, found):
        self.popup.hide()
        self._suggest_seq += 1
        self.x_edit.setText(found.label)
        self.use_found(found)

    def _hide_if_unfocused(self):
        if not self._disposed and not self.x_edit.hasFocus():
            self.popup.hide()

    def _resolve_link(self, text):
        """Open a pasted Google Maps link and go to the place it names."""
        self.popup.hide()
        self._suggest_seq += 1
        self.zoom_btn.setEnabled(False)
        self.zoom_btn.setText(tr('Opening...'))
        self._task = run_in_background(
            lambda: resolve_map_link(text), self._link_resolved)
        return True

    def _link_resolved(self, found, error):
        if self._disposed:
            return
        self.zoom_btn.setEnabled(True)
        self.zoom_btn.setText(tr('Search'))
        if error is not None or found is None:
            self._mark(self.x_edit, True)
            self._say(str(error or tr('The link could not be opened.')))
            return
        self.x_edit.setText(found.label)
        self._typed_len = len(found.label)
        self.use_found(found)

    def _search(self):
        self.popup.hide()
        self._suggest_seq += 1
        text = self.x_edit.text().strip()
        if G.is_map_link(text):
            return self._resolve_link(text)
        try:
            if not text:
                G.search_places(text)           # raises with the hint
        except G.GoToError as exc:
            self._mark(self.x_edit, True)
            self._say(str(exc))
            return False
        self.zoom_btn.setEnabled(False)
        self.zoom_btn.setText(tr('Searching...'))
        self._task = run_in_background(
            lambda: search_places(text), self._searched)
        return True

    def _searched(self, results, error):
        if self._disposed:
            return
        self.zoom_btn.setEnabled(True)
        self.zoom_btn.setText(tr('Search'))
        if error is not None or results is None:
            self._mark(self.x_edit, True)
            self._say(str(error or tr('The place search failed.')))
            return
        self.results = list(results)
        if not self.results:
            self._mark(self.x_edit, True)
            self._say(tr('No place found for "{}".').format(
                self.x_edit.text().strip()))
        elif len(self.results) == 1:
            self.use_found(self.results[0])
        else:
            self._choose(self.results)

    def _choose(self, results):
        """List the places found under the box, like a search drop-down."""
        menu = QMenu(self)
        for found in results:
            label = (found.label if len(found.label) <= 90
                     else found.label[:87] + '...')
            menu.addAction(label).triggered.connect(
                lambda _c=False, f=found: self.use_found(f))
        self._menu = menu
        menu.popup(self.x_edit.mapToGlobal(self.x_edit.rect().bottomLeft()))

    def use_found(self, found):
        try:
            place = found.to_place(self.map_crs())
        except G.GoToError as exc:
            self._say(str(exc))
            return False
        self._go(place)
        return True

    def _flag_bad(self):
        unit = self.unit()
        edits = [self.x_edit]
        if unit not in G.GRIDS and not self.combined():
            edits.append(self.y_edit)
        if unit == 'utm':
            edits.append(self.zone_edit)
        for edit in edits:
            self._mark(edit, True)

    def clear(self):
        self.point.clear()
        self.canvas.refresh()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self.popup.hide()
        self.popup.deleteLater()
        with suppress(Exception):
            self.canvas.removeEventFilter(self)
        self.point.dispose()
        self.hide()
        self.deleteLater()


def unit_name(unit):
    return {'mgrs': 'MGRS', 'usng': 'USNG'}.get(unit, G.label_of(unit))


# --------------------------------------------------------------- module API

_BAR = None


def _alive(widget):
    if widget is None:
        return False
    try:
        widget.objectName()
    except RuntimeError:                        # deleted by Qt
        return False
    return True


def get_bar(canvas=None):
    global _BAR
    canvas = canvas or (iface.mapCanvas() if iface is not None else None)
    if canvas is None:
        return None
    if not _alive(_BAR) or _BAR.canvas is not canvas:
        if _alive(_BAR):
            _BAR.dispose()
        _BAR = GoToXYBar(canvas)
    return _BAR


def set_go_to_xy_visible(on):
    """Show or hide the bar; returns whether it is showing."""
    bar = get_bar()
    if bar is None:
        return False
    if on:
        bar.show_bar()
    else:
        bar.close_bar()
    return bar.isVisible()


def current_bar():
    return _BAR if _alive(_BAR) else None


def close_go_to_xy():
    """Remove the bar and its point on plugin unload."""
    global _BAR
    bar, _BAR = _BAR, None
    if _alive(bar):
        bar.dispose()

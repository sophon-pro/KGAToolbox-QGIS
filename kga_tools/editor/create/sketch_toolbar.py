"""
The sketch toolbar: ArcGIS Pro's floating editing toolbar for the Create Features construction tool.

It floats over the map canvas while the construction tool is active and sits at the centre of the bottom,
top, left or right edge (the gear button picks the edge; the choice is remembered). From left to right (top
to bottom when it sits on a side):

    segment types    Straight Segment | construction methods v | arcs v | trace v
    sketch tools     Pointer (draw, drag a vertex) | Move Sketch                       (lines and polygons)
    actions          Finish | Cancel

A button with a small arrow keeps a list of methods; picking one makes it the button's face. Points get Point
and a list of constructions only; Rectangle gets Horizontal / Grid Aligned and Square toggles, Circle its two
variants, Ellipse Finish and Cancel only.
"""

import os
import re
from contextlib import suppress

from qgis.PyQt.QtCore import QByteArray, QEvent, QPoint, QRect, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap, QPolygon, QRegion
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QMenu,
    QToolButton,
    QWidget,
)

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QActionGroup
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QActionGroup

from ...core import kga_settings as ks
from ..sketch.construction_tool import SHAPE_POINTS
from ...gui.magnifier import PEAK, Magnifier
from .templates import GEOM_POINT

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
SETTING = ks.KEY_SKETCH_BAR_POSITION
POSITIONS = (("bottom", "Bottom"), ("top", "Top"), ("left", "Left"), ("right", "Right"))
EDGE_GAP = 10                   # pixels between the toolbar and the canvas edge
BUTTON = 36                     # medium; Settings > Editor > Toolbar > Size picks small / medium / large
ICON = 28
SIZES = {"small": (28, 22), "medium": (36, 28), "large": (44, 34)}


def metrics():
    """(button, icon) side in pixels for the size chosen in Settings."""
    return SIZES.get(ks.sketch_bar_size(), SIZES["medium"])

# id -> (tooltip, icon file)
ITEMS = {
    "straight": ("Straight Segment - click the vertices", "seg_straight"),
    "circle": ("Circle - click the centre, then a point on the edge", "circle_center"),
    "circle3": ("Three-Point Circle - click three points on the edge", "circle_3pt"),
    "rect_aligned": ("Horizontal / Grid Aligned - make the rectangle oriented horizontal with the map", "rect_aligned"),
    "rect_square": ("Square - make a square", "rect_square"),
    "point": ("Point - click to place a point", "seg_point"),
    "right_angle": ("Right Angle - every segment turns exactly 90 degrees", "seg_right_angle"),
    "intersection": ("Intersection - define two lines (click an edge, or a point and aim); the vertex goes where they cross",
                     "seg_intersection"),
    "line_circle": ("Line-Circle Intersection - define a line, then a circle by its centre and radius", "seg_line_circle"),
    "circle_circle": ("Circle-Circle Intersection - two circles, each by its centre and radius", "seg_dist_dist"),
    "midpoint": ("Midpoint - click the two ends of a segment; the vertex goes at its middle", "seg_midpoint"),
    "arc3": ("Three-Point Arc - a point on the arc, then its end", "seg_arc3"),
    "arc_end": ("End Point Arc Segment - click the end point, then set the bow (R types a radius)", "seg_arc_end"),
    "arc_tangent": ("Tangent Curve Segment - a circular arc tangent to the previous segment", "seg_arc_tangent"),
    "bezier": ("Bezier Curve Segment - press at the vertex and drag to pull its handle", "seg_bezier"),
    "trace": ("Trace - follow the edges of existing features", "seg_trace"),
    "pointer": ("Pointer - add vertices, drag a vertex to move it", "mode_pointer"),
    "move": ("Move Sketch - drag the whole sketch", "mode_move"),
    "finish": ("Finish Sketch (F2)", "act_finish"),
    "cancel": ("Delete Sketch", "act_cancel"),
}

LINE_SPLITS = (("right_angle", "intersection", "line_circle", "circle_circle", "midpoint"),
               ("bezier", "arc_tangent", "arc_end", "arc3"),
               ("trace",))
POINT_SPLITS = (("intersection", "line_circle", "circle_circle", "midpoint"),)

DARK = {"bg": "#303030", "border": "#5a5a5a", "hover": "#4a4a4a", "on": "#2c4f75", "on_border": "#4a8fd4",
        "line": "#5a5a5a", "arrow": "#c8c8c8", "dark": True}
LIGHT = {"bg": "#f4f4f4", "border": "#a8a8a8", "hover": "#dadada", "on": "#cfe3f7", "on_border": "#4a8fd4",
         "line": "#c2c2c2", "arrow": "#444444", "dark": False}
# the icons are drawn light on dark; on a light theme these colours are swapped for dark ones
LIGHT_ICON_COLORS = {"#d2d2d2": "#3c3c3c", "#9fb4c8": "#46607a", "#9a9a9a": "#6a6a6a", "#e4e4e4": "#4a4a4a",
                     "#3a3a3a": "#f4f4f4", "#e8e8e8": "#262626", "#1c1c1c": "#ffffff", "#c4c4c4": "#555555",
                     "#b8b8b8": "#8a8a8a", "#bdbdbd": "#777777", "#7c7c7c": "#8a8a8a", "#d0d0d0": "#444444",
                     "#e9f2ea": "#ffffff", "#2f8f3a": "#2f8f3a", "#8ec5ff": "#1f6fc0", "#4aa3ff": "#1f6fc0"}


def theme():
    """The toolbar's colours: dark on a dark QGIS theme, light on a light one."""
    return DARK if QApplication.palette().window().color().lightness() < 128 else LIGHT


def make_qss(t):
    return f"""
QFrame#KgaSketchBar {{ background: {t['bg']}; border: 1px solid {t['border']}; border-radius: 3px; }}
QFrame#KgaSketchBar QToolButton, QFrame#KgaSketchPalette QToolButton {{
    background: transparent; border: 1px solid transparent; border-radius: 2px; padding: 0px;
}}
QFrame#KgaSketchBar QToolButton:hover, QFrame#KgaSketchPalette QToolButton:hover {{ background: {t['hover']}; }}
QFrame#KgaSketchBar QToolButton:checked, QFrame#KgaSketchPalette QToolButton:checked {{
    background: {t['on']}; border-color: {t['on_border']}; }}
QFrame#KgaSketchBar QToolButton:disabled {{ background: transparent; }}
QFrame#KgaSketchBar QToolButton#KgaSketchArrow {{ border: none; background: transparent; }}
QFrame#KgaSketchBar QToolButton#KgaSketchArrow:hover {{ background: {t['hover']}; }}
QFrame#KgaSketchBar QToolButton[open="true"], QFrame#KgaSketchBar QToolButton#KgaSketchArrow[open="true"] {{
    background: #0f5fc4; border-color: #0f5fc4; }}
QFrame#KgaSketchBar QFrame#KgaSketchLine {{ background: {t['line']}; border: none; }}
QFrame#KgaSketchPalette {{ background: {t['bg']}; border: 1px solid {t['border']}; border-radius: 3px; }}
QToolButton#KgaSketchGear {{ background: {t['bg']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QToolButton#KgaSketchGear:hover {{ background: {t['hover']}; }}
QToolButton#KgaSketchGear::menu-indicator {{ image: none; }}
"""


def _icon(name):
    path = os.path.join(ICON_DIR, name + ".svg")
    if theme()["dark"]:
        return QIcon(path)
    with open(path, encoding="utf-8") as f:
        text = re.sub(r"#[0-9a-fA-F]{6}", lambda m: LIGHT_ICON_COLORS.get(m.group(0).lower(), m.group(0)), f.read())
    renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pix)
    return icon


def _arrow_icon(size=8):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(theme()["arrow"]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPolygon(QPolygon([QPoint(0, 2), QPoint(size, 2), QPoint(size // 2, size - 1)]))
    painter.end()
    return QIcon(pix)


def _button(item, checkable=False, parent=None):
    tip, icon = ITEMS[item]
    button, icon_side = metrics()
    b = QToolButton(parent)
    b.setIcon(_icon(icon))
    b.setIconSize(QSize(icon_side, icon_side))
    b.setFixedSize(button, button)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)           # the canvas keeps the keyboard (Esc, Backspace, F2)
    b.setAutoRaise(True)
    return b


class _Palette(QFrame):
    """The list of methods behind a button's arrow: one icon button per method, in a column."""

    def __init__(self, items, current, chosen, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("KgaSketchPalette")
        self.setStyleSheet(make_qss(theme()))
        box = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        box.setContentsMargins(3, 3, 3, 3)
        box.setSpacing(2)
        self.buttons = {}
        for item in items:
            b = _button(item, checkable=True)
            b.setChecked(item == current)
            b.clicked.connect(lambda _=False, i=item: (self.close(), chosen(i)))
            box.addWidget(b)
            self.buttons[item] = b


class _Split(QWidget):
    """A button with a list of methods behind a small arrow; the face shows the one picked last."""

    def __init__(self, items, vertical, owner):
        super().__init__()
        self.items = items
        self.owner = owner
        self.current = "arc3" if "arc3" in items else items[0]
        self.face = _button(self.current, checkable=True)
        self.arrow = QToolButton()
        self.arrow.setObjectName("KgaSketchArrow")
        self.arrow.setIcon(_arrow_icon())
        self.arrow.setIconSize(QSize(8, 8))
        self.arrow.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.arrow.setAutoRaise(True)
        self.arrow.setToolTip("More methods")
        box = QBoxLayout(QBoxLayout.Direction.TopToBottom if vertical else QBoxLayout.Direction.LeftToRight, self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        button = metrics()[0]
        if vertical:
            self.arrow.setFixedSize(button, 12)
        else:
            self.arrow.setFixedSize(12, button)
        box.addWidget(self.face)
        box.addWidget(self.arrow)
        self.arrow.clicked.connect(self.open_list)
        self.arrow.setVisible(len(items) > 1)        # a list of one (Trace) needs no drop-down

    def show_item(self, item):
        self.current = item
        tip, icon = ITEMS[item]
        self.face.setIcon(_icon(icon))
        self.face.setToolTip(tip)

    def set_open(self, on):
        for w in (self.face, self.arrow):
            w.setProperty("open", on)
            w.style().unpolish(w)
            w.style().polish(w)

    def open_list(self):
        palette = _Palette(self.items, self.current, self.owner.pick_from_list, self.owner)
        palette.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        palette.adjustSize()
        self.set_open(True)
        palette.destroyed.connect(lambda *_: self._closed())
        self.owner.place_popup(palette, self)
        palette.show()

    def _closed(self):
        with suppress(RuntimeError):                    # the toolbar may have been rebuilt meanwhile
            self.set_open(False)


class SketchToolbar(QWidget):
    """Floats over the map canvas while the construction tool is the active map tool."""

    def __init__(self, canvas, tool):
        super().__init__(canvas)
        self.canvas = canvas
        self.tool = tool
        self.position = ks.sketch_bar_position()
        self._config = None
        self._mag = None               # the Magnify Toolbar overlay while that option is on
        self._faces = {}               # segment id -> its button (a plain button or the face of a split)
        self._splits = []
        self._actions = {}
        self._frame = None
        self._gear = None
        self._disposed = False
        self.setObjectName("KgaSketchToolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.hide()
        canvas.installEventFilter(self)
        tool.stateChanged.connect(self.sync)
        tool.activated.connect(self._tool_on)
        tool.deactivated.connect(self._tool_off)

    # ------------------------------------------------------------------ show / hide
    def _tool_on(self):
        self.refresh_settings()

    def _tool_off(self):
        self._release_magnifier()
        self.hide()

    def refresh_settings(self):
        """Take the Settings > Editor > Toolbar choices (shown, edge, size, magnify) and redraw."""
        if self._disposed:
            return
        self.position = ks.sketch_bar_position()
        self._config = None
        wanted = ks.sketch_bar_enabled() and self.canvas.mapTool() is self.tool
        if not wanted:
            self._release_magnifier()
            self.hide()
            return
        self.sync()
        self.show()
        self.raise_()
        self.reposition()

    def _release_magnifier(self):
        if self._mag is not None:
            self._mag.reset()

    # ------------------------------------------------------------------ build
    def _vertical(self):
        return self.position in ("left", "right")

    def _wanted_config(self):
        t = self.tool
        if t.mode == "circle":
            kind = "circle"
        elif t.mode == "rectangle":
            kind = "rectangle"
        elif t.mode in SHAPE_POINTS:
            kind = "shape"
        elif t.geom_type == GEOM_POINT:
            kind = "point"
        else:
            kind = "line"
        return kind, self._vertical(), ks.sketch_bar_size(), ks.sketch_bar_magnify()

    def _clear(self):
        self._release_magnifier()
        if self._frame is not None:
            self._frame.hide()
            self._frame.setParent(None)
            self._frame.deleteLater()
        if self._gear is not None:
            self._gear.setParent(None)
            self._gear.deleteLater()
        self._frame = self._gear = None
        self._faces, self._splits, self._actions = {}, [], {}
        if self.layout() is not None:
            QWidget().setLayout(self.layout())             # drop the old layout, keep the widgets' owner clear

    def _separator(self, vertical):
        line = QFrame()
        line.setObjectName("KgaSketchLine")
        if vertical:
            line.setFixedSize(metrics()[0] - 6, 1)
        else:
            line.setFixedSize(1, metrics()[0] - 6)
        return line

    def _rebuild(self):
        kind, vertical, _size, magnify = self._config = self._wanted_config()
        self._clear()
        outer = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        left = top = right = bottom = EDGE_GAP
        if magnify:                          # room for the swollen buttons: toward the map and past both ends
            room = int((PEAK - 1.0) * metrics()[0]) + 6
            if vertical:
                top, bottom = top + room, bottom + room
                left, right = (left, right + room) if self.position == "left" else (left + room, right)
            else:
                left, right = left + room, right + room
                top, bottom = (top + room, bottom) if self.position == "bottom" else (top, bottom + room)
        outer.setContentsMargins(left, top, right, bottom)
        frame = QFrame(self)
        frame.setObjectName("KgaSketchBar")
        frame.setStyleSheet(make_qss(theme()))
        self._frame = frame
        if magnify:
            frame.installEventFilter(self)
            if self._mag is None:
                self._mag = Magnifier(self)
        direction = QBoxLayout.Direction.TopToBottom if vertical else QBoxLayout.Direction.LeftToRight
        row = QBoxLayout(direction, frame)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        outer.addWidget(frame)

        groups = []
        if kind == "circle":
            groups.append(("segments", ["circle", "circle3"]))
        elif kind == "rectangle":
            groups.append(("options", ["rect_aligned", "rect_square"]))
        elif kind == "point":
            groups.append(("segments", ["point"] + [("split", s) for s in POINT_SPLITS]))
        elif kind == "line":
            groups.append(("segments", ["straight"] + [("split", s) for s in LINE_SPLITS]))
            groups.append(("modes", ["pointer", "move"]))
        groups.append(("actions", ["finish", "cancel"]))

        for index, (_name, entries) in enumerate(groups):
            if index:
                row.addWidget(self._separator(vertical))
            for entry in entries:
                if isinstance(entry, tuple):
                    split = _Split(entry[1], vertical, self)
                    split.face.clicked.connect(lambda _=False, s=split: self._segment_clicked(s.current))
                    row.addWidget(split)
                    self._splits.append(split)
                    for item in entry[1]:
                        self._faces[item] = split
                    continue
                button = _button(entry, checkable=entry not in ("finish", "cancel"))
                row.addWidget(button)
                if entry in ("point", "straight", "circle"):
                    self._faces["straight"] = button
                    button.clicked.connect(lambda _=False: self._segment_clicked("straight"))
                elif entry == "circle3":
                    self._faces[entry] = button
                    button.clicked.connect(lambda _=False: self._segment_clicked("circle3"))
                elif entry in ("rect_aligned", "rect_square"):
                    self._faces[entry] = button
                    button.clicked.connect(lambda checked=False, n=entry[5:]: self.tool.set_rect_option(n, checked))
                elif entry in ("pointer", "move"):
                    self._faces[entry] = button
                    button.clicked.connect(lambda _=False, m=entry: self._mode_clicked(m))
                else:
                    self._actions[entry] = button
        self._actions["finish"].clicked.connect(self._finish)
        self._actions["cancel"].clicked.connect(self._cancel)

        gear = QToolButton(self)
        gear.setObjectName("KgaSketchGear")
        gear.setStyleSheet(make_qss(theme()))
        gear.setIcon(_icon("gear"))
        gear.setIconSize(QSize(14, 14))
        gear.setFixedSize(20, 20)
        gear.setToolTip("Toolbar position (more under Settings > Editor)")
        gear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        gear.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        gear.setMenu(self._position_menu())
        self._gear = gear
        self.adjustSize()
        self._place_gear()
        self.apply_mask()
        gear.show()
        frame.show()
        gear.raise_()

    def _position_menu(self):
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for ident, label in POSITIONS:
            action = menu.addAction(f"{label} of the map")
            action.setCheckable(True)
            action.setChecked(ident == self.position)
            action.setData(ident)
            group.addAction(action)
            action.triggered.connect(lambda _=False, i=ident: self.set_position(i))
        return menu

    def frame_rect(self):
        """The bar's own rectangle in this widget (the rest is room for magnified buttons)."""
        m = self.layout().contentsMargins() if self.layout() is not None else None
        if m is None:
            return self.rect()
        return QRect(m.left(), m.top(), self.width() - m.left() - m.right(), self.height() - m.top() - m.bottom())

    def _place_gear(self):
        if self._gear is not None:
            frame = self.frame_rect()
            self._gear.move(frame.right() + 1 - 11, frame.bottom() + 1 - 11)
            self._gear.raise_()

    def base_region(self):
        region = QRegion(self.frame_rect())
        if self._gear is not None:
            region += QRegion(self._gear.geometry())
        return region

    def apply_mask(self, extra=None):
        """Only the bar, the gear and any magnified buttons take the mouse: the rest of this widget is empty
        space over the map, which must keep clicking through."""
        region = self.base_region()
        if extra is not None:
            region += extra
        self.setMask(region)

    # ------------------------------------------------------------------ position
    def set_position(self, position):
        if position not in dict(POSITIONS):
            return
        self.position = position
        ks.set_value(SETTING, position)
        self._config = None
        self.sync()
        self.reposition()

    def reposition(self):
        if self._disposed:
            return
        self.adjustSize()
        self._place_gear()
        self.apply_mask()
        cw, ch, w, h = self.canvas.width(), self.canvas.height(), self.width(), self.height()
        x, y = (cw - w) // 2, (ch - h) // 2                  # the layout margin keeps EDGE_GAP clear of the edge
        if self.position == "bottom":
            y = ch - h
        elif self.position == "top":
            y = 0
        elif self.position == "left":
            x = 0
        else:
            x = cw - w
        self.move(max(0, x), max(0, y))

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ApplicationPaletteChange and not self._disposed:
            self._config = None                          # the QGIS theme changed: rebuild with the other colours
            QTimer.singleShot(0, self.sync)
        super().changeEvent(event)

    def colors(self):
        return dict(theme())

    def eventFilter(self, obj, event):
        if obj is self.canvas and event.type() == QEvent.Type.Resize and self.isVisible():
            QTimer.singleShot(0, self.reposition)
        elif obj is self._frame and event.type() == QEvent.Type.Enter and self._mag is not None                 and not self._disposed:
            self._mag.activate()
        return False

    def place_popup(self, popup, split):
        """Put a list next to the button it belongs to, on the side that faces the map."""
        origin = split.mapToGlobal(QPoint(0, 0))
        w, h = popup.width(), popup.height()
        if self.position == "bottom":
            x, y = origin.x() + (split.width() - w) // 2, origin.y() - h - 4
        elif self.position == "top":
            x, y = origin.x() + (split.width() - w) // 2, origin.y() + split.height() + 4
        elif self.position == "left":
            x, y = origin.x() + split.width() + 4, origin.y()
        else:
            x, y = origin.x() - w - 4, origin.y()
        screen = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - w))
            y = max(area.top(), min(y, area.bottom() - h))
        popup.move(x, y)

    # ------------------------------------------------------------------ tool <-> buttons
    def pick_from_list(self, item):
        split = self._faces.get(item)
        if isinstance(split, _Split):
            split.show_item(item)
        self._segment_clicked(item)

    def _segment_clicked(self, item):
        self.tool.set_segment(item)
        self.sync()
        self.canvas.setFocus()

    def _mode_clicked(self, mode):
        self.tool.set_sketch_mode(mode)
        self.sync()
        self.canvas.setFocus()

    def _finish(self):
        self.tool.finish_sketch()
        self.canvas.setFocus()

    def _cancel(self):
        self.tool.cancel_sketch()

    def sync(self):
        """Match the buttons to the tool: the template's kind, the segment type, the sketch."""
        if self._disposed:
            return
        if self._config != self._wanted_config():
            self._rebuild()
            self.reposition()
        tool = self.tool
        for split in self._splits:
            if tool.segment in split.items:
                split.show_item(tool.segment)
        for item, holder in self._faces.items():
            button = holder.face if isinstance(holder, _Split) else holder
            if item in ("rect_aligned", "rect_square"):
                on = getattr(tool, item)
            elif item in ("pointer", "move"):
                on = tool.sketch_mode == item
                button.setEnabled(item == "pointer" or tool.has_sketch())
            else:
                if isinstance(holder, _Split):
                    on = tool.segment in holder.items and holder.current == tool.segment
                else:
                    on = tool.segment == item
            if button.isChecked() != on:
                button.blockSignals(True)
                button.setChecked(on)
                button.blockSignals(False)
        if "finish" in self._actions:
            self._actions["finish"].setEnabled(tool.can_finish())
            self._actions["cancel"].setEnabled(tool.has_sketch())

    def dispose(self):
        self._disposed = True
        with suppress(Exception):
            self.canvas.removeEventFilter(self)
        with suppress(Exception):
            self.tool.stateChanged.disconnect(self.sync)
        self.hide()
        self.deleteLater()

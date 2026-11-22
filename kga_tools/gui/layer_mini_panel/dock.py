# -*- coding: utf-8 -*-
"""The dock: header (layer, geometry, status), navigation and the three sections.

The window looks like the KGA bags (Tool Bag, Symbol Bag): a rounded body, a gradient header that is the
title bar (drag it to move the panel) and a zipper band under it. The header holds the layer, the gear
menu, Minimize (into a bubble on the map, in the bags' column) and Close.

Only the visible section is refreshed. Hidden sections are marked stale and
catch up when the user switches to them, so changing layers costs one section's
update, not three. Colours follow the current Symbol Bag style.
"""

from contextlib import suppress

from qgis.core import QgsSettings, QgsVectorLayer
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy,
                                 QStackedWidget, QToolButton, QVBoxLayout, QWidget)

try:
    from qgis.PyQt.QtWidgets import QActionGroup
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QActionGroup

from . import compat as C
from .attributes import AttributesSection
from .labels import LabelsSection
from .symbols import SymbolsSection
from .theme import (CORNERS, DEFAULT, DEFAULT_LOOK, DENSITIES, FOLLOW_BAG, HEADERS, SCHEME_ORDER, SCHEMES,
                    bag_style_for, header_foreground, scheme_palette, stylesheet)
from .ui_common import ElidedLabel, icon
from ..panel_bubble import ToolbarReveal, minimize_icon
from ..symbol_bag.ui.theme import header_gradient, paint_zipper

_EXPANDING = C.enum_value(QSizePolicy, "Policy", "Expanding")
_FIXED = C.enum_value(QSizePolicy, "Policy", "Fixed")
_RESIZE = C.enum_value(QEvent, "Type", "Resize")
_TEXT_ONLY = C.enum_value(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
SECTIONS = (("attributes", "Attributes"), ("labels", "Labels"), ("symbols", "Colors"))
GEOM_ICON = {C.GEOM_POINT: "point", C.GEOM_LINE: "line", C.GEOM_POLYGON: "polygon"}
ZIPPER_H = 11
# The floating window never gets wider than this; wide tables scroll sideways instead. Small screens get less:
MAX_FLOAT_WIDTH = 1100        # screens 1600 px wide or more (logical px, after Windows display scaling)
MAX_FLOAT_WIDTH_SMALL = 950   # smaller screens, e.g. 1366 x 768 or 1920 at 150 % (1280)
SMALL_SCREEN = 1600
MAX_SCREEN_SHARE = 0.75       # and never more than three quarters of the screen (e.g. 768 px on 1024)


def max_float_width(screen_width):
    """The widest the floating Layer Bag may be on a screen *screen_width* logical px wide."""
    cap = MAX_FLOAT_WIDTH if screen_width >= SMALL_SCREEN else MAX_FLOAT_WIDTH_SMALL
    return int(min(cap, screen_width * MAX_SCREEN_SHARE))
QWIDGETSIZE_MAX = 16777215
BUBBLE_KEY = "layer_mini_panel"
TITLE_QSS = """
QFrame#header { background: transparent; border: none; }
QToolButton#bagButton { border: none; border-radius: 6px; padding: 3px; background: transparent; }
QToolButton#bagButton:hover { background: rgba(255,255,255,70); }
QToolButton#bagButton:pressed { background: rgba(0,0,0,40); }
QToolButton#bagButton::menu-indicator { image: none; width: 0px; }
"""


class _BagTitleBar(QWidget):
    """The dock's title bar: the layer header and the zipper band. Mouse presses it does not use go to
    the dock, which moves the floating panel; a double-click only docks it when docking is allowed."""

    def __init__(self, dock, header):
        super().__init__(dock)
        self.dock = dock
        self.setObjectName("LMPTitle")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 0)
        lay.setSpacing(0)
        lay.addWidget(header)
        self.zipper = QWidget(self)
        self.zipper.setFixedHeight(ZIPPER_H)
        self.zipper.setAttribute(C.enum_value(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"), True)
        lay.addWidget(self.zipper)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if self.dock.allow_docking:
            e.ignore()                   # QDockWidget docks / floats it
        else:
            e.accept()                   # never dropped into a dock tab by accident


def _two_state_icon(name, off, on):
    ic = QIcon()
    mode_on = C.enum_value(QIcon, "State", "On")
    mode_off = C.enum_value(QIcon, "State", "Off")
    normal = C.enum_value(QIcon, "Mode", "Normal")
    for px in (16, 32):
        ic.addPixmap(icon(name, color=off).pixmap(px, px), normal, mode_off)
        ic.addPixmap(icon(name, color=on).pixmap(px, px), normal, mode_on)
    return ic


class MiniPanelDock(ToolbarReveal, QgsDockWidget):
    minimizeRequested = pyqtSignal()
    # what a sibling bag (the Label Bag) overrides
    TITLE = C.PLUGIN_NAME
    OBJECT_NAME = "LayerMiniPanelDock"
    BUBBLE_KEY = BUBBLE_KEY
    HELP_PAGE = "layer_bag"
    GEOMETRY_KEY = "float_geometry"
    EMPTY_TEXT = "Select a layer in the Layers panel to see its attributes, labels and colors."
    EXTENDED_LABELS = False       # the Label Bag: shadow, offset, number options

    def __init__(self, iface, watcher, parent=None, bag_provider=None):
        super().__init__(self.TITLE, parent)
        self.setObjectName(self.OBJECT_NAME)
        # the panel paints its own rounded bag body (see paintEvent)
        self.setAttribute(C.enum_value(Qt, "WidgetAttribute", "WA_TranslucentBackground"), True)
        self.bag_group = None            # BubbleGroup, set by the controller (shared bag menus)
        self.topLevelChanged.connect(self._limit_width)     # the width cap is for the floating window only
        self.iface = iface
        self.watcher = watcher
        self.layer = None
        self.palette_ = None
        self._stale = {k: True for k, _ in SECTIONS}
        self.metrics = dict(header_updates=0, restyles=0, fits=0)
        st = QgsSettings()
        self.scheme = st.value(C.SETTINGS_KEY + "scheme", FOLLOW_BAG)
        self.look = {k: st.value(C.SETTINGS_KEY + k, v) for k, v in DEFAULT_LOOK.items()}
        self.autofit = st.value(C.SETTINGS_KEY + "autofit", True, type=bool)
        self.allow_docking = st.value(C.SETTINGS_KEY + "allow_docking", False, type=bool)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(400)
        self._geo_timer.timeout.connect(self.save_geometry)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(120)
        self._fit_timer.timeout.connect(self._auto_fit_now)
        self._anim = None

        root = QWidget()
        root.setObjectName("LMPRoot")
        root.setAttribute(C.enum_value(Qt, "WidgetAttribute", "WA_StyledBackground"), True)
        self.setWidget(root)
        root.setMinimumWidth(240)
        self.root = root
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(10)

        # ---------------- header
        head = QFrame()
        head.setObjectName("header")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 9, 10, 9)
        hl.setSpacing(10)
        self.geom_icon = QLabel()
        self.geom_icon.setObjectName("geomTile")
        self.geom_icon.setFixedSize(38, 38)
        self.geom_icon.setAlignment(C.AlignCenter)
        hl.addWidget(self.geom_icon)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        self.name_lbl = ElidedLabel(mode=C.ElideMiddle, hug=True)
        self.name_lbl.setObjectName("layerName")
        self.meta_lbl = ElidedLabel()
        self.meta_lbl.setObjectName("layerMeta")
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(self.name_lbl, 0)     # as wide as the name; elides when room runs out
        self.switch_btn = QToolButton()
        self.switch_btn.setObjectName("bagButton")
        self.switch_btn.setToolTip("Switch layer - pick another layer of this map")
        self.switch_btn.setIconSize(QSize(14, 14))
        self.switch_btn.clicked.connect(lambda _c=False: self.show_layer_menu())
        name_row.addWidget(self.switch_btn)
        name_row.addStretch(1)
        txt.addLayout(name_row)
        txt.addWidget(self.meta_lbl)
        hl.addLayout(txt, 1)
        self.status_lbl = QLabel()
        self.status_lbl.setObjectName("badge")
        hl.addWidget(self.status_lbl)
        self.menu_btn = QToolButton()
        self.menu_btn.setObjectName("bagButton")
        self.menu_btn.setToolTip("Bag style, window style, size, bubble and animation")
        self.menu_btn.setIconSize(QSize(16, 16))
        self.menu_btn.setPopupMode(C.enum_value(QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._fill_menu)
        self.menu_btn.setMenu(self.menu)
        hl.addWidget(self.menu_btn)
        self.min_btn = QToolButton()
        self.min_btn.setObjectName("bagButton")
        self.min_btn.setToolTip("Minimize to a bubble on the map")
        self.min_btn.setIconSize(QSize(16, 16))
        self.min_btn.clicked.connect(lambda _c=False: self.minimizeRequested.emit())
        self.min_btn.hide()              # shown by the controller when the bubble is set up
        hl.addWidget(self.min_btn)
        self.close_btn = QToolButton()
        self.close_btn.setObjectName("bagButton")
        self.close_btn.setToolTip("Close")
        self.close_btn.setIconSize(QSize(16, 16))
        self.close_btn.clicked.connect(lambda _c=False: self.close())
        hl.addWidget(self.close_btn)
        hl.setSpacing(6)
        self.header = head
        self.root_layout = lay
        self.title_bar = _BagTitleBar(self, head)
        self.setTitleBarWidget(self.title_bar)

        # ---------------- navigation
        seg = QFrame()
        seg.setObjectName("segment")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tab_buttons = {}
        for i, (key, label) in enumerate(SECTIONS):
            b = QToolButton()
            b.setText(label)
            b.setIconSize(QSize(16, 16))
            b.setCheckable(True)
            b.setToolButtonStyle(C.ToolButtonTextBesideIcon)
            b.setSizePolicy(_EXPANDING, _FIXED)
            b.setToolTip(label)
            self.group.addButton(b, i)
            sl.addWidget(b, 1)
            self.tab_buttons[key] = b
        lay.addWidget(seg)

        # ---------------- sections
        self.stack = QStackedWidget()
        self.attributes = AttributesSection(iface, watcher)
        self.labels = LabelsSection(iface, watcher, extended=self.EXTENDED_LABELS)
        self.symbols = SymbolsSection(iface, watcher, bag_provider=bag_provider)
        for w in (self.attributes, self.labels, self.symbols):
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)

        self.empty = QLabel()
        self.empty.setObjectName("hint")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(C.AlignCenter)
        self.empty.hide()
        lay.addWidget(self.empty)
        self._empty_spacer = QWidget()
        lay.addWidget(self._empty_spacer, 1)
        self._empty_spacer.hide()

        if hasattr(self.group, "idClicked"):
            self.group.idClicked.connect(self.show_section)
        else:
            self.group.buttonClicked[int].connect(self.show_section)
        self.tab_buttons["attributes"].setChecked(True)

        self.attributes.use_label_field = self.use_label_field
        self.attributes.on_view_changed = self.request_fit
        self.attributes.stats_view.on_result = self.request_fit
        self.symbols.on_colors_created = self._colors_created

        watcher.layerChanged.connect(self.set_layer)
        watcher.nameChanged.connect(self._update_header)
        watcher.nameChanged.connect(self.attributes.editing_changed)
        watcher.selectionChanged.connect(self._on_selection)
        watcher.styleChanged.connect(self._on_style)
        watcher.fieldsChanged.connect(self._on_fields)
        watcher.dataChanged.connect(self._on_data)

        self.apply_theme(force=True)

    # ------------------------------------------------------------------ look
    def palette(self):
        if self.scheme == FOLLOW_BAG:
            try:
                p = self.symbols.bridge.palette()
            except Exception:
                p = None
            return p or dict(DEFAULT)
        return scheme_palette(self.scheme) or dict(DEFAULT)

    def apply_theme(self, force=False):
        """Restyle when the scheme / window style changed (cheap check otherwise)."""
        p = self.palette()
        sig = (tuple(sorted((k, str(v)) for k, v in p.items())), tuple(sorted(self.look.items())))
        if not force and sig == getattr(self, "_style_sig", None):
            return
        self._style_sig = sig
        self.palette_ = p
        qss = stylesheet(p, self.look)
        self.root.setStyleSheet(qss + "\nQWidget#LMPRoot { background: transparent; }\n")
        self.title_bar.setStyleSheet(qss + TITLE_QSS)
        compact = self.look.get("density") == "compact"
        self.root_layout.setContentsMargins(*((7, 5, 7, 7) if compact else (10, 8, 10, 10)))
        self.root_layout.setSpacing(6 if compact else 10)
        for view in (self.attributes.tview, self.attributes.view):
            view.verticalHeader().setMinimumSectionSize(16)
            view.verticalHeader().setDefaultSectionSize(20 if compact else 24)
        for key, b in self.tab_buttons.items():
            b.setIcon(_two_state_icon(key, p["sub"], "#ffffff"))
        fg = header_foreground(p, self.look)
        self.menu_btn.setIcon(icon("settings", color=fg))
        self.min_btn.setIcon(minimize_icon(QColor(fg)))
        self.close_btn.setIcon(icon("close", color=fg))
        self.switch_btn.setIcon(icon("switch", color=fg))
        self.update()
        self.attributes.set_palette(p)
        self._theme_colors_widget()
        self.metrics["restyles"] += 1
        self._update_header()
        self.request_fit()

    def _theme_colors_widget(self):
        """The embedded colour wheel follows the chosen scheme (bag styles)."""
        c = self.symbols.colors
        if c is None:
            return
        with suppress(RuntimeError, Exception):
            if self.scheme == FOLLOW_BAG:
                t = self.symbols.bridge.theme()
            else:
                t = self.symbols.bridge.make_theme(bag_style_for(self.palette_) or "white")
            if t is not None:
                c.apply_theme(t)

    def _colors_created(self, widget):
        self._theme_colors_widget()
        widget.installEventFilter(self)
        self.request_fit()

    def eventFilter(self, obj, e):  # noqa: N802
        if e.type() == _RESIZE and obj is self.symbols.colors and self.current_key() == "symbols":
            self.request_fit()
        return False

    # ------------------------------------------------------------------ settings menu
    def _fill_menu(self):
        m = self.menu
        m.clear()
        sm = m.addMenu("Bag Style (colour)")
        grp = QActionGroup(sm)
        bag_name = ""
        with suppress(Exception):
            bp = self.symbols.bridge.palette()
            st = bag_style_for(bp) if bp else None
            bag_name = " ({})".format(SCHEMES[st]["label"]) if st in SCHEMES else ""
        for key, label in [(FOLLOW_BAG, "Follow Symbol Bag" + bag_name)] + \
                [(k, SCHEMES[k]["label"]) for k in SCHEME_ORDER]:
            a = sm.addAction(self._swatch(key), label)
            a.setCheckable(True)
            a.setChecked(self.scheme == key)
            grp.addAction(a)
            a.triggered.connect(lambda _c=False, k=key: self.set_scheme(k))
            if key == FOLLOW_BAG:
                sm.addSeparator()
        wm = m.addMenu("Window style")
        for title, opt, choices in (("Header", "header", HEADERS), ("Corners", "corners", CORNERS),
                                    ("Density", "density", DENSITIES)):
            wm.addSection(title)
            g = QActionGroup(wm)
            for key, label in choices:
                a = wm.addAction(label)
                a.setCheckable(True)
                a.setChecked(self.look.get(opt) == key)
                g.addAction(a)
                a.triggered.connect(lambda _c=False, o=opt, k=key: self.set_look(o, k))
        if self.bag_group is not None:
            # the same Bubble / Animation / Sound Effects menus as the Tool Bag and the Symbol Bag
            from ..bag_common import fill_common_menu
            fill_common_menu(m, self.bag_group, None, None, key=self.BUBBLE_KEY, with_style=False)
        m.addSeparator()
        a = m.addAction("Allow docking")
        a.setCheckable(True)
        a.setChecked(self.allow_docking)
        a.setToolTip("Off: the panel always floats over the map and cannot be dropped into a dock tab")
        a.toggled.connect(self.set_allow_docking)
        a = m.addAction("Auto-fit size to content")
        a.setCheckable(True)
        a.setChecked(self.autofit)
        a.setToolTip("The panel sizes itself to the table, the colour wheel… when you switch views")
        a.toggled.connect(self.set_autofit)
        m.addAction("Fit to content now", lambda: self.fit_to_content(force=True))
        m.addSeparator()
        m.addAction("Help", self._help)
        m.addSeparator()
        br = self.symbols.bridge
        if br.available or br.resolve():
            built_in = br.package is not None and br.package.endswith("gui.symbol_bag")
            info = m.addAction("Symbol Bag {} {}".format(br.version(), "(built in)" if built_in else "connected"))
        else:
            info = m.addAction("Symbol Bag not found")
        info.setEnabled(False)

    def _help(self):
        from ...branding import open_docs
        open_docs(self.HELP_PAGE)

    def _swatch(self, key):
        from qgis.PyQt.QtGui import QColor, QPainter, QPixmap
        p = (self.symbols.bridge.palette() if key == FOLLOW_BAG else scheme_palette(key)) or DEFAULT
        pm = QPixmap(16, 16)
        pm.fill(QColor(0, 0, 0, 0))
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setPen(QColor(0, 0, 0, 40))
        pt.setBrush(QColor(p["h2"]))
        pt.drawEllipse(1, 1, 14, 14)
        pt.setBrush(QColor(p["accent"]))
        pt.drawEllipse(5, 5, 6, 6)
        pt.end()
        return QIcon(pm)

    def set_scheme(self, key):
        self.scheme = key
        QgsSettings().setValue(C.SETTINGS_KEY + "scheme", key)
        self.apply_theme(force=True)

    def set_look(self, option, value):
        self.look[option] = value
        QgsSettings().setValue(C.SETTINGS_KEY + option, value)
        self.apply_theme(force=True)

    # ------------------------------------------------------------------ switch layer
    @staticmethod
    def switch_layers():
        """The vector layers of the current map (the active map tab; the whole project without Map Tabs),
        in Layers panel order."""
        from qgis.core import QgsProject
        try:
            from ..map_tabs import active_map_layer_ids
            ids = active_map_layer_ids()
        except Exception:
            ids = None
        out = []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            lyr = node.layer()
            if isinstance(lyr, QgsVectorLayer) and (ids is None or lyr.id() in ids):
                out.append(lyr)
        return out

    def build_layer_menu(self):
        from qgis.core import QgsIconUtils
        menu = QMenu(self.switch_btn)
        menu.setObjectName("LMPLayerMenu")
        menu.addSection("Switch layer")
        layers = self.switch_layers()
        for lyr in layers:
            a = menu.addAction(QgsIconUtils.iconForLayer(lyr), lyr.name())
            a.setCheckable(True)
            a.setChecked(lyr is self.layer)
            a.triggered.connect(lambda _c=False, target=lyr: self.switch_to(target))
        if not layers:
            menu.addAction("No vector layers in this map").setEnabled(False)
        return menu

    def show_layer_menu(self):
        menu = self.build_layer_menu()
        menu.aboutToHide.connect(menu.deleteLater)
        b = self.switch_btn
        menu.popup(b.mapToGlobal(b.rect().bottomLeft()))
        return menu

    def switch_to(self, layer):
        """Make *layer* the current layer of the Layers panel; the panel follows it."""
        with suppress(RuntimeError, AttributeError):
            view = self.iface.layerTreeView()
            if view is not None:
                view.setCurrentLayer(layer)
                return
        with suppress(Exception):
            self.iface.setActiveLayer(layer)

    # ------------------------------------------------------------------ bag window
    def paintEvent(self, _e):  # noqa: N802
        """Rounded bag body, gradient (or solid / plain) header behind the title bar, zipper band."""
        p = self.palette_ or DEFAULT
        floating = self.isFloating()
        square = self.look.get("corners") == "square" or not floating
        radius = 4.0 if square else 14.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border = QColor(p["h2"])
        border.setAlpha(170)
        painter.setPen(QPen(border, 1))
        painter.setBrush(QColor(p["body"]))
        painter.drawRoundedRect(body, radius, radius)
        bar = self.title_bar.geometry()
        head_bottom = bar.top() + self.header.geometry().bottom() + 1
        head = QRectF(body.left(), body.top(), body.width(), head_bottom - body.top())
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRoundedRect(head, radius, radius)
        path.addRect(QRectF(head.left(), head.top() + radius, head.width(), max(0.0, head.height() - radius)))
        style = self.look.get("header", "gradient")
        painter.setPen(Qt.PenStyle.NoPen)
        if style == "minimal":
            painter.setBrush(QColor(p["card"]))
        elif style == "solid":
            painter.setBrush(QColor(p["h2"]))
        else:
            painter.setBrush(header_gradient(head, p))
        painter.drawPath(path.simplified())
        zip_geo = self.title_bar.zipper.geometry().translated(bar.topLeft())
        if style == "minimal":
            line = QColor(p["h2"])
            line.setAlpha(120)
            painter.setPen(QPen(line, 1))
            painter.drawLine(int(body.left()) + 8, zip_geo.top(), int(body.right()) - 8, zip_geo.top())
        else:
            paint_zipper(painter, QRectF(zip_geo).adjusted(1, 0, -1, 0), p, bool(p.get("dark")))
        painter.end()

    def _screen_width(self):
        screen = None
        with suppress(Exception):
            screen = QGuiApplication.screenAt(self.geometry().center()) or self.screen()
        screen = screen or QGuiApplication.primaryScreen()
        return screen.availableGeometry().width()

    def width_cap(self):
        return max_float_width(self._screen_width())

    def _limit_width(self, floating=None):
        """Floating: at most width_cap() wide (also when dragged), for the screen it is on. Docked: QGIS decides."""
        floating = self.isFloating() if floating is None else floating
        cap = self.width_cap() if floating else QWIDGETSIZE_MAX
        if self.maximumWidth() != cap:
            self.setMaximumWidth(cap)

    def show_minimize(self, on):
        self.min_btn.setVisible(bool(on))

    # ------------------------------------------------------------------ floating window
    def make_floating(self):
        """Float over the map: last position, or top-right of the map canvas the first time.
        With Allow docking on, the user may then drag it into a dock area; otherwise it never docks."""
        self.setFloating(True)
        self._limit_width(True)
        self.setAllowedAreas(C.enum_value(Qt, "DockWidgetArea",
                                          "AllDockWidgetAreas" if self.allow_docking else "NoDockWidgetArea"))
        g = QgsSettings().value(C.SETTINGS_KEY + self.GEOMETRY_KEY, None)
        rect = None
        if g:
            try:
                x, y, w, h = [int(v) for v in str(g).split(",")]
                rect = QRect(x, y, max(300, w), max(360, h))
            except ValueError:
                rect = None
        screens = QGuiApplication.screens()
        if rect is not None and not any(s.availableGeometry().intersects(rect) for s in screens):
            rect = None                     # saved on a monitor that is no longer there
        if rect is None:
            canvas = self.iface.mapCanvas()
            tr = canvas.mapToGlobal(canvas.rect().topRight())
            rect = QRect(tr.x() - 440, tr.y() + 16, 420, 640)
        self.setGeometry(rect)

    def save_geometry(self):
        if self.isFloating() and self.isVisible():
            g = self.geometry()
            QgsSettings().setValue(C.SETTINGS_KEY + self.GEOMETRY_KEY,
                                   "{},{},{},{}".format(g.x(), g.y(), g.width(), g.height()))

    def moveEvent(self, e):  # noqa: N802
        super().moveEvent(e)
        if self.isFloating():
            self._geo_timer.start()
            self._limit_width(True)          # moved to another screen: its own cap

    def hideEvent(self, e):  # noqa: N802
        self.save_geometry()
        super().hideEvent(e)

    def set_allow_docking(self, on):
        self.allow_docking = bool(on)
        QgsSettings().setValue(C.SETTINGS_KEY + "allow_docking", self.allow_docking)
        if on:
            self.setAllowedAreas(C.enum_value(Qt, "DockWidgetArea", "AllDockWidgetAreas"))
        else:
            self.make_floating()
            self.show()

    def set_autofit(self, on):
        self.autofit = bool(on)
        QgsSettings().setValue(C.SETTINGS_KEY + "autofit", self.autofit)
        if on:
            self.fit_to_content(force=True)

    # ------------------------------------------------------------------ smart size
    def request_fit(self):
        if self.autofit and self.isVisible():
            self._fit_timer.start()

    def _auto_fit_now(self):
        if QApplication.mouseButtons():          # the user is dragging: never fight a manual resize
            self._fit_timer.start()
            return
        self.fit_to_content()

    def _content_size(self):
        """(width, height) the current view needs inside the stack."""
        key = self.current_key()
        a = self.attributes
        if key == "attributes":
            view = a.current_view()
            if view in ("table", "fields"):
                tv = a.tview if view == "table" else a.view
                model = tv.model()
                hh = tv.horizontalHeader()
                cols = model.columnCount()
                if view == "fields":
                    w = sum(max(90, tv.sizeHintForColumn(c) + 16) for c in range(cols))
                elif len(a.natural_widths) == cols:
                    w = sum(a.natural_widths)     # not the stretched last column
                else:
                    w = sum(hh.sectionSize(c) for c in range(cols))
                rows = model.rowCount()
                shown = max(4, min(rows, 16))
                rh = tv.verticalHeader().defaultSectionSize()
                h_table = hh.height() + shown * rh + 2 * tv.frameWidth() + 2
                w_table = w + 2 * tv.frameWidth() + (tv.verticalScrollBar().sizeHint().width() if rows > shown else 0) + 4
                chrome_h = a.height() - tv.height()
                chrome_w = a.width() - tv.width()
                return max(w_table + chrome_w, 340), h_table + chrome_h
            hint = a.stats_view.sizeHint()
            chrome_h = a.height() - a.stack.height()
            return max(hint.width(), 380), hint.height() + chrome_h
        if key == "labels":
            body = self.labels.findChild(QWidget, "labelsBody")
            hint = body.sizeHint() if body is not None else self.labels.sizeHint()
            return max(hint.width() + 12, 340), hint.height() + 8
        sym = self.symbols
        chrome_h = sym.height() - sym.scroll.height()
        c = sym.colors
        if c is not None and c.isVisible():
            return max(c.minimumSizeHint().width(), 330) + 8, c.height() + chrome_h + 4
        return 360, sym.sizeHint().height()

    def fit_to_content(self, force=False):
        if not (self.autofit or force) or not self.isVisible() or not self.stack.isVisible():
            return
        cw, ch = self._content_size()
        extra_w = self.width() - self.stack.width()
        extra_h = self.height() - self.stack.height()
        w, h = int(cw + extra_w), int(ch + extra_h)
        screen = (self.screen() if hasattr(self, "screen") and self.screen() else QGuiApplication.primaryScreen())
        avail = screen.availableGeometry()
        w = max(self.minimumSizeHint().width(), 300, min(w, int(avail.width() * 0.85)))
        if self.isFloating():
            w = min(w, self.width_cap())
        h = max(360, min(h, int(avail.height() * 0.9)))
        self.metrics["fits"] += 1
        if self.isFloating():
            g = self.geometry()
            target = QRect(g.left(), g.top(), w, h)
            if target.bottom() > avail.bottom():
                target.moveBottom(avail.bottom())
            if target.right() > avail.right():
                target.moveRight(avail.right())
            if abs(target.width() - g.width()) < 6 and abs(target.height() - g.height()) < 6:
                return
            if self._anim is not None:
                self._anim.stop()
            self._anim = QPropertyAnimation(self, b"geometry", self)
            self._anim.setDuration(160)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim.setStartValue(g)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            # docked: only the width is ours to set (QGIS arranges the height)
            mw = self.iface.mainWindow()
            w = min(w, int(mw.width() * 0.45))
            if abs(w - self.width()) >= 8:
                with suppress(Exception):
                    mw.resizeDocks([self], [w], C.Horizontal)

    # ------------------------------------------------------------------ helpers
    def current_key(self):
        return SECTIONS[self.stack.currentIndex()][0]

    def section(self, key):
        return {"attributes": self.attributes, "labels": self.labels, "symbols": self.symbols}[key]

    def show_section(self, index):
        self.stack.setCurrentIndex(index)
        self.tab_buttons[SECTIONS[index][0]].setChecked(True)
        self._bring_current()
        self.request_fit()

    def _bring_current(self):
        key = self.current_key()
        sec = self.section(key)
        if self._stale.get(key):
            self._stale[key] = False
            sec.set_layer(self._target())
        if key in ("symbols", "attributes"):
            sec.ensure_current()
        if key == "symbols" and self.scheme == FOLLOW_BAG:
            self.apply_theme()        # the bag style may have changed meanwhile

    def _target(self):
        lyr = self.layer
        return lyr if isinstance(lyr, QgsVectorLayer) and lyr.isValid() else None

    def use_label_field(self, name):
        self.show_section(1)
        self.labels.use_field(name)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.isFloating():
            self._geo_timer.start()
        style = C.ToolButtonTextBesideIcon if self.width() >= 380 else (
            _TEXT_ONLY if self.width() >= 280 else C.ToolButtonIconOnly)
        for b in self.tab_buttons.values():
            b.setToolButtonStyle(style)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.apply_theme()
        self._bring_current()
        self.request_fit()

    # ------------------------------------------------------------------ watcher slots
    def set_layer(self, layer):
        vector_now = isinstance(layer, QgsVectorLayer) and layer.isValid()
        target = layer if vector_now else None
        # drop every section's reference to the previous layer first: the widget
        # changes below can trigger layout events, and the old layer may be mid-deletion
        for key, _l in SECTIONS:
            sec = self.section(key)
            if getattr(sec, "layer", None) is not None and sec.layer is not target:
                if key == "labels":
                    sec.flush()
                sec.layer = None
                if key == "attributes":
                    sec._counts = None
        self.layer = layer
        self._update_header()
        if layer is None:
            self._set_empty(self.EMPTY_TEXT)
        elif not isinstance(layer, QgsVectorLayer):
            self._set_empty("“{}” is not a vector layer. This panel works with point, line, polygon and "
                            "table layers.".format(layer.name()))
        elif not layer.isValid():
            self._set_empty("“{}” is not valid – its data source is unavailable. Fix the source "
                            "(right-click ▸ Change Data Source) and select it again.".format(layer.name()))
        else:
            self._set_empty(None)
        for key, _l in SECTIONS:
            self._stale[key] = True
        if self.isVisible():
            self._stale[self.current_key()] = False
            sec = self.section(self.current_key())
            sec.set_layer(target)
            if self.current_key() in ("symbols", "attributes"):
                sec.ensure_current()
        elif target is None:
            for key, _l in SECTIONS:
                self.section(key).set_layer(None)
        self.request_fit()

    def _set_empty(self, text):
        show = bool(text)
        if show:
            self.empty.setText(text)
        self.empty.setVisible(show)
        self._empty_spacer.setVisible(show)
        self.stack.setVisible(not show)
        for b in self.tab_buttons.values():
            b.setEnabled(not show)

    def _update_header(self):
        self.metrics["header_updates"] += 1
        lyr = self.layer
        p = self.palette_ or DEFAULT
        tile = header_foreground(p, self.look)
        if lyr is None:
            self.name_lbl.setText("No layer selected")
            self.meta_lbl.setText("Pick a layer in the Layers panel")
            self.geom_icon.setPixmap(icon("table_none", color=tile).pixmap(22, 22))
            self.status_lbl.hide()
            return
        self.name_lbl.setText(lyr.name())
        self.name_lbl.setToolTip(lyr.name())
        if isinstance(lyr, QgsVectorLayer):
            fam = C.geometry_family(lyr)
            self.geom_icon.setPixmap(icon(GEOM_ICON.get(fam, "table"), color=tile).pixmap(22, 22))
            parts = [C.wkb_label(lyr) if fam != C.GEOM_NONE else "Table", lyr.providerType()]
            with suppress(Exception):
                if lyr.isSpatial() and lyr.crs().authid():
                    parts.append(lyr.crs().authid())
            meta = " · ".join(parts)
            self.meta_lbl.setText(meta)
            self.meta_lbl.setToolTip("{}\n{}".format(meta, lyr.publicSource()))
            status, warn = self._status(lyr)
        else:
            self.geom_icon.setPixmap(icon("warning", color=tile).pixmap(22, 22))
            self.meta_lbl.setText("Not a vector layer")
            status, warn = "", False
        self.status_lbl.setText(status)
        name = "badgeWarn" if warn else "badge"
        if self.status_lbl.objectName() != name:
            self.status_lbl.setObjectName(name)
            self.status_lbl.style().unpolish(self.status_lbl)
            self.status_lbl.style().polish(self.status_lbl)
        self.status_lbl.setVisible(bool(status))

    @staticmethod
    def _status(lyr):
        if not lyr.isValid():
            return "Invalid", True
        if lyr.isEditable():
            return "Editing", True
        if lyr.readOnly():
            return "Read-only", False
        return "", False

    def _on_selection(self):
        if self.layer is None:
            return
        if self.current_key() == "attributes" and self.isVisible():
            self.attributes.refresh_selection()
        else:
            self._stale["attributes"] = True

    def _on_style(self):
        """Renderer / labeling changed outside the panel (e.g. Layer Properties, Layer Colors)."""
        if not self.isVisible():
            self._stale["labels"] = self._stale["symbols"] = True
            return
        key = self.current_key()
        if key == "labels":
            self.labels.refresh()
            self._stale["symbols"] = True
        elif key == "symbols":
            self.symbols.refresh_renderer()
            self._stale["labels"] = True
        else:
            self._stale["labels"] = self._stale["symbols"] = True

    def _on_fields(self):
        if self.current_key() == "attributes" and self.isVisible():
            self.attributes.refresh_fields()
        else:
            self._stale["attributes"] = True
        if self.current_key() == "labels" and self.isVisible():
            self.labels.refresh()
        else:
            self._stale["labels"] = True

    def _on_data(self):
        self._update_header()
        if self.current_key() == "attributes" and self.isVisible():
            self.attributes.refresh_data()
        else:
            self._stale["attributes"] = True

    def cleanup(self):
        self.labels.flush()
        self.attributes.cleanup()
        self.symbols.cleanup()
        for sig, slot in ((self.watcher.layerChanged, self.set_layer),
                          (self.watcher.nameChanged, self._update_header),
                          (self.watcher.nameChanged, self.attributes.editing_changed),
                          (self.watcher.selectionChanged, self._on_selection),
                          (self.watcher.styleChanged, self._on_style),
                          (self.watcher.fieldsChanged, self._on_fields),
                          (self.watcher.dataChanged, self._on_data)):
            with suppress(TypeError, RuntimeError):
                sig.disconnect(slot)

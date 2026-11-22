# -*- coding: utf-8 -*-
"""Symbol Bag – the floating bag window and its bubble.

A frameless, translucent window that floats over the map like a little bag:
coloured header with round buttons, a stitched zipper band, an optional key
ring or handle on top, and symbol cards inside. It can fold away into a
round bubble on the map.
"""

from contextlib import suppress
import math
import os

from qgis.PyQt.QtCore import (QEasingCurve, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
                              QVariantAnimation, pyqtSignal)
from qgis.PyQt.QtGui import QColor, QConicalGradient, QCursor, QFont, QKeySequence, QPainter, QPainterPath, QPen
from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QComboBox, QHBoxLayout, QLabel,
                                 QLineEdit, QMenu, QSizeGrip, QSizePolicy, QStackedWidget, QToolButton,
                                 QVBoxLayout, QWidget)

try:
    from qgis.PyQt.QtWidgets import QActionGroup, QShortcut
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QActionGroup, QShortcut

from ....core.compat import scoped
from ..core.applier import LayerStyleUndo
from ..core.log import log_error
from ..core.storage import KIND_FILL, KIND_LABELS, KIND_LINE, KIND_MARKER, KIND_RASTER
from ..core.symbols import layer_kind
from .actions import UNSORTED, BagActions, LayerDropFilter
from .dialogs import DEFAULT_SETTINGS, ICON_SIZES, SHOW_AS, BagStyleDialog, SettingsDialog
from .symbol_view import ROLE_UID, PreviewCache, SymbolModel, SymbolView
from .motion import (SPEED_FACTOR, SPEEDS, Tween, arc_control, bezier, ease_in, ease_in_out, ease_out,
                     lerp, segment, spring)
from .theme import Theme, header_gradient, paint_zipper
from .widgets import ChipBar, GhostWidget

MAX_SIZE = 16777215
KINDS = (KIND_MARKER, KIND_LINE, KIND_FILL, KIND_RASTER)
CATEGORIES = [
    ("all", "All symbols", "all"),
    (KIND_MARKER, "Point symbols", "point"),
    (KIND_LINE, "Line symbols", "line"),
    (KIND_FILL, "Polygon symbols", "polygon"),
    (KIND_RASTER, "Terrain & raster styles", "raster"),
    ("fav", "Favorites", "star"),
    ("recent", "Recently used", "recent"),
]
KIND_ICON = {KIND_MARKER: "point", KIND_LINE: "line", KIND_FILL: "polygon", KIND_RASTER: "raster"}
SORTS = [("manual", "Manual order (drag to reorder)"), ("name", "Name"),
         ("used", "Most used"), ("type", "Type"), ("newest", "Newest first")]
BUBBLE_POS_NAMES = ["Top left", "Top center", "Top right", "Middle left", "Center", "Middle right",
                    "Bottom left", "Bottom center", "Bottom right"]
DECO_H = {"ring": 18, "handle": 20, "none": 2}


# ----------------------------------------------------------------------
class DragArea(QWidget):
    """Header: drag to move the bag, double-click to fold it."""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self._press = None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.win.windowHandle()
            if handle is not None and hasattr(handle, "startSystemMove") and not os.environ.get("SB_MANUAL_MOVE"):
                with suppress(Exception):
                    if handle.startSystemMove():
                        return
            self._press = e.globalPos() - self.win.frameGeometry().topLeft() if hasattr(e, "globalPos") \
                else e.globalPosition().toPoint() - self.win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.MouseButton.LeftButton:
            gp = e.globalPos() if hasattr(e, "globalPos") else e.globalPosition().toPoint()
            self.win.move(gp - self._press)

    def mouseReleaseEvent(self, e):
        self._press = None
        self.win.save_window_state()

    def mouseDoubleClickEvent(self, e):
        self.win.minimize()


# ----------------------------------------------------------------------
class BubbleWidget(QWidget):
    """The round bubble the bag hides in. Click to open, drag to move.

    It pops in with a spring, swells a little on hover and squishes when
    pressed. All of it is painted (no window resizing), so it stays smooth.
    """

    clicked = pyqtSignal()
    moved = pyqtSignal(QPoint)
    MARGIN = 9           # room around the circle for the spring overshoot

    def __init__(self, parent):
        # clicking the bubble must not take focus from QGIS (that makes the
        # main window's title bar flash inactive / active on Windows)
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme = None
        self.count = 0
        self.size_px = 44
        self.scale = 1.0
        self.alpha = 1.0
        self.badge_scale = 1.0
        self.motion_enabled = True
        self.speed = 1.0
        self._tween = None
        self._badge_tween = None
        self._press = None
        self._dragged = False
        self._closing = False      # shrinking away: hover / press must not revive it
        self.pop_damping = 0.42    # bag: playful pop; Layer Colors: calm
        self.mark = "bag"          # "bag": Symbol Bag mark; "wheel": the Layer Colors tool
        self.group = None          # BubbleGroup: sits in one column with the other bags' bubbles
        self.group_key = None
        self.draggable = True
        self.setToolTip("Symbol Bag – click to open, drag to move")

    def attach_group(self, group, key):
        self.group, self.group_key, self.draggable = group, key, False
        group.register(key, self)
        self.set_size(group.size_of(key))
        self.setToolTip(self.toolTip().replace(", drag to move", ""))

    def group_set_size(self, px):
        if px != self.size_px:
            self.set_size(px)

    def group_center(self):
        return self.circle_center_global()

    def group_move(self, center):
        c = self.circle_rect().center()
        self.move(int(round(center.x() - c.x())), int(round(center.y() - c.y())))

    def showEvent(self, e):
        super().showEvent(e)
        if self.group is not None and not self._closing:
            self.group.set_present(self.group_key, True)

    def hideEvent(self, e):
        super().hideEvent(e)
        if self.group is not None:
            self.group.set_present(self.group_key, False)

    def set_size(self, px):
        if self.group is not None:
            px = self.group.size_of(self.group_key)
        self.size_px = px
        self.setFixedSize(px + 2 * self.MARGIN + 6, px + 2 * self.MARGIN + 6)
        self.update()

    def circle_rect(self):
        s = self.size_px
        return QRectF(self.MARGIN, self.MARGIN + 6, s, s)

    def circle_center_global(self):
        return QPointF(self.mapToGlobal(self.circle_rect().center().toPoint()))

    # --- motion ---------------------------------------------------------
    def spring_to(self, scale, alpha=None, duration=320, damping=0.55, finished=None, start_scale=None):
        if self._tween is not None:
            self._tween.stop()
        s0 = self.scale if start_scale is None else start_scale
        a0 = self.alpha
        a1 = a0 if alpha is None else alpha
        if not self.motion_enabled:
            self.scale, self.alpha = scale, a1
            self.update()
            if finished:
                finished()
            return

        def frame(t):
            k = spring(t, damping)
            self.scale = lerp(s0, scale, k)
            self.alpha = lerp(a0, a1, ease_out(t * 1.6, 2))
            self.update()
        self._tween = Tween(self, duration * self.speed, frame, finished).start()

    def pop_in(self):
        """Appear: 0 → 1.2 → 1."""
        self._closing = False
        self.alpha = 1.0
        self.spring_to(1.0, 1.0, 460, self.pop_damping, start_scale=0.0)

    def shrink_out(self, finished=None):
        """Disappear. Once started nothing (hover, press, a stray click)
        can stop it, so the bubble never stays behind on the map."""
        if self._tween is not None:
            self._tween.stop()
        self._closing = True
        self._press = None
        s0 = self.scale
        if not self.motion_enabled:
            self.scale = 0.0
            if finished:
                finished()
            return

        def frame(t):
            self.scale = s0 * (1.0 - ease_in(t, 2))
            self.alpha = 1.0 - ease_in(t, 2)
            self.update()
        self._tween = Tween(self, 150 * self.speed, frame, finished).start()

    def set_count(self, n):
        if n != self.count and self.isVisible() and self.motion_enabled and self.count:
            if self._badge_tween is not None:
                self._badge_tween.stop()

            def frame(t):
                self.badge_scale = 0.5 + 0.5 * spring(t, 0.4)
                self.update()
            self._badge_tween = Tween(self, 380 * self.speed, frame).start()
        self.count = n
        self.update()

    def enterEvent(self, e):
        if self._press is None and not self._closing:
            self.spring_to(1.08, None, 260, 0.5)

    def leaveEvent(self, e):
        if self._press is None and not self._closing:
            self.spring_to(1.0, None, 260, 0.6)

    # --- painting ---------------------------------------------------------
    def paintEvent(self, event):
        if self.theme is None or self.scale <= 0.01 or self.alpha <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(max(0.0, min(1.0, self.alpha)))
        circle = self.circle_rect()
        c = circle.center()
        p.translate(c)
        p.scale(self.scale, self.scale)
        p.translate(-c)
        s = self.size_px
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 50))
        p.drawEllipse(circle.translated(0, 2))
        p.setBrush(header_gradient(circle, self.theme.p))
        p.setPen(QPen(QColor(255, 255, 255, 200), 2))
        p.drawEllipse(circle)
        if self.mark == "wheel":
            ring = circle.adjusted(s * 0.2, s * 0.2, -s * 0.2, -s * 0.2)
            grad = QConicalGradient(ring.center(), 90)
            for i in range(7):
                grad.setColorAt(i / 6.0, QColor.fromHsv((i * 60) % 360, 230, 255))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawEllipse(ring)
            p.setBrush(QColor("#ffffff"))
            inner = ring.adjusted(ring.width() * 0.27, ring.height() * 0.27, -ring.width() * 0.27, -ring.height() * 0.27)
            p.drawEllipse(inner)
        else:
            icon_r = circle.adjusted(s * 0.2, s * 0.2, -s * 0.2, -s * 0.2).toRect()
            self.theme.icon("sbmark", self.theme.p["htext"], max(16, int(s * 0.6))).paint(p, icon_r)
        if self.count:
            text = str(self.count) if self.count < 1000 else "999+"
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSizeF(7.5)
            p.setFont(f)
            w = max(18, p.fontMetrics().horizontalAdvance(text) + 9)
            badge = QRectF(circle.right() - w * 0.6, circle.top() - 8, w, 18)
            if self.badge_scale != 1.0:
                bc = badge.center()
                p.translate(bc)
                p.scale(self.badge_scale, self.badge_scale)
                p.translate(-bc)
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.setBrush(QColor(self.theme.accent).darker(105))
            p.drawRoundedRect(badge, 9, 9)
            p.setPen(QColor("#ffffff"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    # --- mouse ------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._closing:
            gp = e.globalPos() if hasattr(e, "globalPos") else e.globalPosition().toPoint()
            self._press = gp - self.frameGeometry().topLeft()
            self._dragged = False
            self.spring_to(0.9, None, 180, 0.7)

    def mouseMoveEvent(self, e):
        if self._press is not None and self.draggable:
            gp = e.globalPos() if hasattr(e, "globalPos") else e.globalPosition().toPoint()
            target = gp - self._press
            if (target - self.pos()).manhattanLength() > 3 or self._dragged:
                self._dragged = True
                self.move(target)

    def mouseReleaseEvent(self, e):
        if self._press is None:
            return
        self._press = None
        if self._dragged:
            self.spring_to(1.0, None, 360, 0.45)
            self.moved.emit(self.pos())
        else:
            self.clicked.emit()


# ----------------------------------------------------------------------
class SymbolBagPanel(QWidget, BagActions):
    visibilityChanged = pyqtSignal(bool)
    symbolApplied = pyqtSignal(object)       # a symbol was applied: the list of layers that changed

    def __init__(self, iface, store, plugin_dir, version, parent=None, group=None):
        self.group = group
        self.target_override = None            # callable -> (layer, class key) while Quick Symbology has claimed applies
        parent = parent or (iface.mainWindow() if iface else None)
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("SymbolBagPanel")
        self.setWindowTitle("Symbol Bag")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.iface = iface
        self.store = store
        self.plugin_dir = plugin_dir
        self.version = version
        self.undo = LayerStyleUndo()
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(store.get_setting("ui", {}) or {})
        self._pushing = False
        if group is not None:                  # the design, bubble, motion and sound are shared
            self.settings.update(self._common_values())
            group.settingsChanged.connect(self._on_common)
        self.category = "all"
        self.collection_id = None
        self.smart_kind = None
        self.smart_layer_name = ""
        self.smart_override = False
        self.collapsed = False
        self.in_bubble = False
        self._expanded_geo = None
        self._anims = []
        self._fade_anim = None
        self._refresh_pending = False
        self._closing = False
        self.layer_filter = None

        self.cache = PreviewCache(store)
        self.model = SymbolModel(self.cache, self)
        self.bubble = BubbleWidget(parent)
        self.ghost = GhostWidget(parent)
        self.picker = None          # Symbol Colors: recolours bag symbols (opened inside the bag)
        self.layer_picker = None    # Layer Colors: standalone, recolours map layers
        self.embedded_pickers = []  # Layer Colors placed inside other panels
        self._last_pos = None
        self._ghost_anim = None
        if group is not None:
            self.bubble.attach_group(group, "symbol")
            group.register_style("symbol", lambda: self.settings["style"],
                                 lambda k: self.apply_settings({"style": k}))
        self.bubble.clicked.connect(self.restore_from_bubble)
        self.bubble.moved.connect(self._bubble_moved)
        self._build_ui()
        self._build_shortcuts()
        self.apply_settings(self.settings, save=False)
        self.store.add_listener(self._store_changed)
        if iface is not None:
            self.layer_filter = LayerDropFilter(self, iface.layerTreeView(), iface.mapCanvas())
            iface.layerTreeView().currentLayerChanged.connect(self._layer_changed)
            self._tree_sel_model = iface.layerTreeView().selectionModel()
            self._tree_sel_model.selectionChanged.connect(self._tree_selection_changed)
            iface.mapCanvas().layersChanged.connect(self._scope_changed)
            self._layer_changed(iface.activeLayer(), refresh=False)
        self.resize(320, 560)
        self.setMinimumSize(220, 120)
        self.refresh()

    # ==================================================================
    # UI construction
    # ==================================================================
    def _hbtn(self, icon, tip, slot=None, size=24):
        b = QToolButton()
        b.setObjectName("SBHeaderBtn")
        b.setToolTip(tip)
        b.setFixedSize(size, size)
        b.setIconSize(QSize(size - 9, size - 9))
        b.setProperty("sb_icon", icon)
        if slot is not None:
            b.clicked.connect(slot)
        return b

    def _tbtn(self, icon, tip, slot=None):
        b = QToolButton()
        b.setObjectName("SBTool")
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setIconSize(QSize(16, 16))
        b.setProperty("sb_icon", icon)
        if slot is not None:
            b.clicked.connect(slot)
        return b

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        self.root_layout = root

        # header -----------------------------------------------------------
        self.header = DragArea(self)
        self.header.setObjectName("SBHeader")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(10, 6, 8, 6)
        hl.setSpacing(5)
        self.logo = QLabel()
        self.title_label = QLabel("Symbol Bag")
        self.title_label.setObjectName("SBTitleLabel")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        hl.addWidget(self.logo)
        hl.addWidget(self.title_label)
        hl.addStretch(1)
        self.add_btn = self._hbtn("plus", "Add symbols")
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.color_btn = self._hbtn("palette", "Symbol Colors – recolor the selected symbol in the bag",
                                    lambda: self.open_picker(self.current_uid(), self._btn_center(self.color_btn)))
        self.menu_btn = self._hbtn("settings", "Bag options")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.min_btn = self._hbtn("minimize", "Minimize", self.minimize)
        self.close_btn = self._hbtn("close", "Hide Symbol Bag", self.close_animated)
        for b in (self.add_btn, self.color_btn, self.menu_btn, self.min_btn, self.close_btn):
            hl.addWidget(b)
        self.add_btn.setMenu(self._build_add_menu())
        self.main_menu = QMenu(self)
        self.main_menu.aboutToShow.connect(self._fill_main_menu)
        self.menu_btn.setMenu(self.main_menu)
        root.addWidget(self.header)

        self.zipper = QWidget()
        self.zipper.setFixedHeight(11)
        self.zipper.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        root.addWidget(self.zipper)

        # content ----------------------------------------------------------
        self.content = QWidget()
        self.content.setObjectName("SBContent")
        cl = QVBoxLayout(self.content)
        cl.setContentsMargins(8, 7, 8, 0)
        cl.setSpacing(6)
        self.content_layout = cl

        # the layer the bag works on: the layers of the map that is open (blank = nothing selected)
        from qgis.gui import QgsMapLayerComboBox
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("SBLayerCombo")
        with suppress(Exception):
            from qgis.core import QgsMapLayerProxyModel
            try:
                f = Qgis.LayerFilter.HasGeometry | Qgis.LayerFilter.RasterLayer
            except AttributeError:
                f = scoped(QgsMapLayerProxyModel, 'Filter', 'HasGeometry') | scoped(QgsMapLayerProxyModel, 'Filter', 'RasterLayer')
            self.layer_combo.setFilters(f)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layer_combo.setMinimumContentsLength(8)
        self.layer_combo.setToolTip("The layer symbols are applied to. Lists the layers of the open map; "
                                    "it follows the layer selected in the Layers panel.")
        self.layer_combo.layerChanged.connect(self._layer_combo_changed)
        cl.addWidget(self.layer_combo)

        srow = QHBoxLayout()
        srow.setSpacing(3)
        self.search = QLineEdit()
        self.search.setObjectName("SBSearch")
        self.search.setPlaceholderText("Search symbols…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.coll_btn = self._tbtn("folder", "Collections")
        self.coll_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.coll_menu = QMenu(self)
        self.coll_menu.aboutToShow.connect(self._fill_collection_menu)
        self.coll_btn.setMenu(self.coll_menu)
        self.coll_btn.setFixedWidth(28)
        srow.addWidget(self.search, 1)
        srow.addWidget(self.coll_btn)
        cl.addLayout(srow)

        self.chip_bar = ChipBar()
        chip_row = QHBoxLayout(self.chip_bar)
        chip_row.setContentsMargins(3, 3, 3, 3)
        chip_row.setSpacing(2)
        self.chips = QButtonGroup(self)
        self.chips.setExclusive(True)
        self.chip_buttons = {}
        for key, tip, icon in CATEGORIES:
            b = QToolButton()
            b.setObjectName("SBChip")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setProperty("sb_icon", icon)
            b.setIconSize(QSize(14, 14))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            b.clicked.connect(lambda _c=False, k=key: self.set_category(k, user=True))
            self.chips.addButton(b)
            self.chip_buttons[key] = b
            self.chip_bar.add(b)
            chip_row.addWidget(b)
        self.chip_buttons["all"].setChecked(True)
        self.chip_bar.set_active(self.chip_buttons["all"], animate=False)
        bar_row = QHBoxLayout()
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(3)
        bar_row.addWidget(self.chip_bar, 1)       # as wide as the search box above it
        bar_row.addSpacing(28)
        cl.addLayout(bar_row)

        self.smart_label = QLabel()
        self.smart_label.setObjectName("SBSmart")
        self.smart_label.setTextFormat(Qt.TextFormat.RichText)
        self.smart_label.setWordWrap(True)
        self.smart_label.linkActivated.connect(self._smart_link)
        self.smart_label.hide()                   # replaced by the layer box above the search field
        cl.addWidget(self.smart_label)

        self.view = SymbolView()
        self.view.setModel(self.model)
        self.view.viewport().setAutoFillBackground(False)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._view_menu)
        self.view.doubleClicked.connect(self._double_clicked)
        self.view.reorderRequested.connect(self._reorder)
        self.view.filesDropped.connect(self.import_files)
        self.view.favoriteToggled.connect(self._toggle_favorite)
        self.view.recolorRequested.connect(self.open_picker)
        self.view.selectionModel().currentChanged.connect(self._current_card_changed)
        self.view.menuRequested.connect(lambda uid, gp: self._show_symbol_menu(gp))
        self.empty = QLabel()
        self.empty.setObjectName("SBEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.empty)
        cl.addWidget(self.stack, 1)

        self.footer = QWidget()
        self.footer.setObjectName("SBFooter")
        fl = QHBoxLayout(self.footer)
        fl.setContentsMargins(2, 0, 0, 0)
        fl.setSpacing(1)
        self.count_label = QLabel()
        self.count_label.setObjectName("SBFooterLabel")
        self.count_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.apply_btn = self._tbtn("apply", "Apply the selected symbol to the layer or classes selected in the "
                                    "Layers panel (or double-click it)", self.apply_selected)
        self.apply_btn.setText("Apply")
        self.apply_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.apply_btn.setObjectName("SBApplyBtn")
        self.apply_btn.setEnabled(False)
        self.undo_btn = self._tbtn("undo", "Undo last apply (Ctrl+Z)", self.undo_last)
        self.import_btn = self._tbtn("import", "Import symbols…", lambda: self.import_files())
        self.export_btn = self._tbtn("export", "Export the symbols shown…", self.export_current_view)
        self.grip = QSizeGrip(self.footer)
        self.grip.setFixedSize(14, 14)
        fl.addWidget(self.count_label, 1)
        fl.addWidget(self.apply_btn)
        fl.addWidget(self.undo_btn)
        fl.addWidget(self.import_btn)
        fl.addWidget(self.export_btn)
        fl.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        cl.addWidget(self.footer)
        root.addWidget(self.content, 1)

        # legacy names used by shared actions
        self.target_label = self.count_label

    def _build_add_menu(self):
        m = QMenu(self)
        m.addAction("Create Symbol…", self.create_symbol)
        m.addAction("Add Current Layer Symbol to Bag", self.add_current_layer)
        m.addSeparator()
        m.addAction("Import from QGIS Style Manager…", self.import_style_manager)
        m.addAction("Import from Current Project…", self.import_project)
        m.addAction("Import from KGA Layer Symbol…", self.import_kga_layer_symbol)
        m.addAction("Import Files (QML, XML, SLD, SVG, DB)…", lambda: self.import_files())
        m.addAction("Import Collection (.sbag)…", self.import_collection)
        return m

    def _check_action(self, menu, text, checked, slot, group=None):
        a = menu.addAction(text)
        a.setCheckable(True)
        a.setChecked(checked)
        if group is not None:
            group.addAction(a)
        a.triggered.connect(slot)
        return a

    def _fill_main_menu(self):
        """The ⚙ menu – rebuilt each time so check marks are current."""
        m = self.main_menu
        m.clear()
        t = self.theme
        s = self.settings
        m.addAction(t.icon("plus"), "Create Symbol…", self.create_symbol)
        m.addAction(t.icon("layers"), "Add Current Layer Symbol to Bag", self.add_current_layer)
        imp = m.addMenu(t.icon("import"), "Import")
        for a in self._build_add_menu().actions()[3:]:
            imp.addAction(a)
        m.addSeparator()
        m.addAction(t.icon("style"), "Bag Style…", self.show_bag_style)
        self._check_action(m, "Smart Filter by Selected Layer", s["smart"],
                           lambda c: self.set_option("smart", c, refresh_smart=True))
        if self.group is not None:             # the same entries in all three bags
            from ...bag_common import fill_common_menu
            fill_common_menu(m, self.group, lambda: self.settings["style"],
                             lambda k: self.apply_settings({"style": k}), key="symbol")
        else:
            bub = m.addMenu("Bubble")
            self._check_action(bub, "Minimize to a Bubble", s["bubble"], lambda c: self.set_option("bubble", c))
            if self.group is None:
                size = bub.addMenu("Bubble Size")
                g = QActionGroup(size)
                for px, lbl in ((36, "Small"), (44, "Medium"), (56, "Large"), (68, "Extra large")):
                    self._check_action(size, lbl, s["bubble_size"] == px, lambda _c, v=px: self.set_option("bubble_size", v), g)
                pos = bub.addMenu("Bubble Position")
                g = QActionGroup(pos)
                for i, lbl in enumerate(BUBBLE_POS_NAMES):
                    self._check_action(pos, lbl, s["bubble_pos"] == i and not s.get("bubble_xy"),
                                       lambda _c, v=i: self._set_bubble_pos(v), g)
            else:
                note = bub.addAction("Size and position: right-click the Tool Bag bubble")
                note.setEnabled(False)
            mot = m.addMenu("Motion")
            g = QActionGroup(mot)
            current = s.get("motion", "normal") if s.get("animations", True) else "off"
            for key, lbl, _f in SPEEDS:
                self._check_action(mot, lbl, current == key,
                                   lambda _c, v=key: self.apply_settings({"motion": v, "animations": v != "off"}), g)
            self.fill_sound_menu(m.addMenu(t.icon("speaker" if s.get("sound", True) else "speaker_off"), "Sound Effects"))
        show = m.addMenu("Show Symbols As")
        g = QActionGroup(show)
        for key, lbl in SHOW_AS:
            self._check_action(show, lbl, s["show_as"] == key, lambda _c, v=key: self.set_option("show_as", v), g)
        cols = m.addMenu("Columns")
        g = QActionGroup(cols)
        self._check_action(cols, "Auto", s["columns"] == 0, lambda _c: self.set_option("columns", 0), g)
        for n in range(1, 7):
            self._check_action(cols, str(n), s["columns"] == n, lambda _c, v=n: self.set_option("columns", v), g)
        size = m.addMenu("Icon Size")
        g = QActionGroup(size)
        for px, lbl in ICON_SIZES:
            self._check_action(size, lbl, s["preview_size"] == px,
                               lambda _c, v=px: self.set_option("preview_size", v), g)
        sort = m.addMenu("Sort By")
        g = QActionGroup(sort)
        for key, lbl in SORTS:
            self._check_action(sort, lbl, s.get("sort") == key, lambda _c, v=key: self.set_option("sort", v), g)
        m.addSeparator()
        lib = m.addMenu(t.icon("backup"), "Library")
        lib.addAction("Export Entire Library…", lambda: self.export_collection(None))
        lib.addAction("Backup Library…", self.backup_library)
        lib.addAction("Restore Library…", self.restore_library)
        lib.addSeparator()
        lib.addAction("Restore Default Symbols", self.restore_defaults)
        lib.addAction("Refresh Previews", self.refresh_library)
        lib.addAction("Clear Recently Used", self.store.clear_recent)
        m.addAction(t.icon("settings"), "Preferences…", self.show_settings)
        m.addAction(t.icon("help"), "Help", self.show_about)
        m.addSeparator()
        m.addAction(t.icon("minimize"), "Minimize to Bubble" if s["bubble"] else "Fold Up", self.minimize)
        m.addAction(t.icon("close"), "Hide Symbol Bag", self.close_animated)

    def _fill_collection_menu(self):
        m = self.coll_menu
        m.clear()
        t = self.theme
        counts = self.store.collection_counts()
        cols = self.store.collections()
        children = {}
        for c in cols:
            children.setdefault(c["parent_id"], []).append(c)

        def total(cid):
            return counts.get(cid, 0) + sum(total(ch["id"]) for ch in children.get(cid, []))

        g = QActionGroup(m)
        self._check_action(m, "All collections  ({})".format(self.store.count()), self.collection_id is None,
                           lambda _c: self.set_collection(None), g)
        m.addSeparator()

        def add(pid, depth):
            for c in children.get(pid, []):
                a = self._check_action(m, ("      " * depth) + "{}  ({})".format(c["name"], total(c["id"])),
                                       self.collection_id == c["id"],
                                       lambda _c, cid=c["id"]: self.set_collection(cid), g)
                a.setIcon(t.icon("folder", t.accent, 16))
                add(c["id"], depth + 1)
        add(None, 0)
        self._check_action(m, "Unsorted  ({})".format(counts.get(None, 0)), self.collection_id == UNSORTED,
                           lambda _c: self.set_collection(UNSORTED), g)
        m.addSeparator()
        m.addAction(t.icon("folder_plus"), "New Collection…", lambda: self._new_and_open(None))
        cid = self.collection_id
        if cid not in (None, UNSORTED):
            name = self.store.collection(cid)["name"] if self.store.collection(cid) else ""
            m.addAction(t.icon("folder_plus"), "New Sub-collection in '{}'…".format(name),
                        lambda: self._new_and_open(cid))
            m.addAction(t.icon("rename"), "Rename '{}'…".format(name), lambda: self.rename_collection(cid))
            m.addAction(t.icon("export"), "Export '{}'…".format(name), lambda: self.export_collection(cid))
            m.addAction(t.icon("trash", "#c0392b"), "Delete '{}'…".format(name), lambda: self.delete_collection(cid))

    def _new_and_open(self, parent):
        cid = self.new_collection(parent)
        if cid is not None:
            self.set_collection(cid)

    def _build_shortcuts(self):
        def sc(seq, slot):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            s.activated.connect(slot)
            return s
        self._shortcuts = [
            sc("Ctrl+F", lambda: (self.search.setFocus(), self.search.selectAll())),
            sc(QKeySequence.StandardKey.Delete, self.delete_selected),
            sc("F2", self.rename_selected),
            sc("Ctrl+Z", self.undo_last),
            sc("Return", lambda: self.apply_selected(multiple=True)),
            sc("Ctrl+D", self.duplicate_selected),
            sc("Escape", lambda: self.search.clear()),
        ]

    # ==================================================================
    # painting the bag
    # ==================================================================
    def paintEvent(self, event):
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        deco = DECO_H.get(self.settings["top"], 2)
        radius = 16 if not t.compact else 11
        body = QRectF(self.rect()).adjusted(1, deco, -1, -1)
        border = QColor(t.p["h2"])
        border.setAlpha(160)

        p.setPen(QPen(border, 1))
        p.setBrush(t.body_color())
        p.drawRoundedRect(body, radius, radius)

        head = QRectF(body.left(), body.top(), body.width(), self.header.geometry().bottom() - body.top() + 1)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRoundedRect(head, radius, radius)
        path.addRect(QRectF(head.left(), head.top() + radius, head.width(), head.height() - radius))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(header_gradient(head, t.p))
        p.drawPath(path.simplified())
        if not self.collapsed or self.zipper.isVisible():
            zr = QRectF(self.zipper.geometry()).adjusted(1, 0, -1, 0)
            paint_zipper(p, zr, t.p, t.dark)

        # decoration on top
        top = self.settings["top"]
        dark_h = QColor(t.p["h2"]).darker(118)
        if top == "ring":
            c = QPointF(body.left() + min(130.0, body.width() * 0.42), body.top() - 3)
            p.setPen(QPen(dark_h, 3.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, 9.5, 9.5)
            p.setPen(QPen(QColor(255, 255, 255, 120), 1.2))
            p.drawArc(QRectF(c.x() - 8, c.y() - 8, 16, 16), 40 * 16, 90 * 16)
        elif top == "handle":
            w = min(110.0, body.width() * 0.38)
            r = QRectF(body.center().x() - w / 2, body.top() - deco + 2, w, deco * 2)
            p.setPen(QPen(dark_h, 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(r, 0, 180 * 16)
        p.end()

    # ==================================================================
    # theme / settings
    # ==================================================================
    def apply_settings(self, values, save=True):
        self.settings.update(values)
        s = self.settings
        self.theme = Theme(s["style"], s["background"] / 100.0, s["compact"])
        t = self.theme
        self.setStyleSheet(t.stylesheet())
        deco = DECO_H.get(s["top"], 2)
        self.root_layout.setContentsMargins(1, deco, 1, 1)
        compact = s["compact"]
        self.header.layout().setContentsMargins(10, 4 if compact else 7, 8, 4 if compact else 7)
        self.content_layout.setContentsMargins(6 if compact else 8, 5 if compact else 7, 6 if compact else 8, 1)
        self.content_layout.setSpacing(4 if compact else 6)
        self.view.card_delegate.theme = t
        self.chip_bar.theme = t
        self.chip_bar.update()
        self._apply_motion_settings()
        for pk in (getattr(self, "picker", None), getattr(self, "layer_picker", None)):
            if pk is not None:
                pk.apply_theme(pk.own_theme(t))
        for pk in list(getattr(self, "embedded_pickers", [])):
            with suppress(RuntimeError):
                pk.apply_theme(t)
        self.model.preview_size = s["preview_size"]
        self.view.apply_layout(s["show_as"] == "list", s["preview_size"], s["show_as"] == "grid_names",
                               s["columns"])
        self.logo.setPixmap(t.icon("sbmark", t.p["htext"], 18).pixmap(18, 18))
        self._recolor_icons()
        self.bubble.theme = t
        self.bubble.set_size(s["bubble_size"])
        self.bubble.count = self.store.count() if self.store.conn else 0
        if self.in_bubble:
            self._place_bubble()
            self.bubble.update()
        if not self._anims:
            self.setWindowOpacity(s["fade"] / 100.0)
        self.min_btn.setToolTip("Minimize to bubble" if s["bubble"] else "Fold up")
        if save:
            self.store.set_setting("ui", self.settings)
        self._push_common()
        self._update_smart_label()
        self.update()
        self.view.viewport().update()

    # --- settings shared with the Tool Bag and Layer Colors --------------------------
    def _common_values(self):
        g = self.group
        motion = g.get("motion")
        values = {"bubble": g.get("bubble"), "animations": motion != "off",
                  "sound": g.get("sound"), "sound_style": g.get("sound_style"),
                  "sound_volume": g.get("sound_volume")}
        if motion != "off":
            values["motion"] = motion
        return values

    def _push_common(self):
        g = self.group
        if g is None or self._pushing:
            return
        s = self.settings
        self._pushing = True
        try:
            g.set("bubble", bool(s["bubble"]))
            g.set("motion", s.get("motion", "normal") if s.get("animations", True) else "off")
            g.set("sound", bool(s.get("sound", True)))
            g.set("sound_style", s.get("sound_style", "whoosh"))
            g.set("sound_volume", int(s.get("sound_volume", 70)))
        finally:
            self._pushing = False

    def _on_common(self, key):
        if self._pushing or key not in ("bubble", "motion", "sound", "sound_style", "sound_volume"):
            return
        values = {k: v for k, v in self._common_values().items() if self.settings.get(k) != v}
        if values:
            self.apply_settings(values, save=True)
            lp = getattr(self, "layer_picker", None)
            if lp is not None:
                lp._update_sound_btn()

    def set_option(self, key, value, refresh_smart=False):
        self.apply_settings({key: value})
        if refresh_smart:
            if value and self.iface is not None:
                self._layer_changed(self.iface.activeLayer())
            else:
                self._update_smart_label()
                self.refresh()
        elif key == "sort":
            self.refresh()

    # --- sound effects ----------------------------------------------------------------
    def fill_sound_menu(self, menu):
        """On / off and the sound styles (choosing a style plays it once)."""
        from .sound import STYLES
        menu.clear()
        s = self.settings
        on = menu.addAction("Sound On")
        on.setCheckable(True)
        on.setChecked(bool(s.get("sound", True)))
        on.triggered.connect(lambda c: self.set_sound(bool(c)))
        menu.addAction(self._volume_action(menu))
        menu.addSection("Style")
        g = QActionGroup(menu)
        for key, label, desc in STYLES:
            a = menu.addAction("{}  –  {}".format(label, desc))
            a.setCheckable(True)
            a.setChecked(s.get("sound_style", "whoosh") == key)
            a.setActionGroup(g)
            a.triggered.connect(lambda _c=False, k=key: self.set_sound_style(k))
        return menu

    def _volume_action(self, menu):
        """'Volume  [slider]  70 %' inside the menu; release = hear it."""
        from qgis.PyQt.QtWidgets import QSlider, QWidgetAction
        act = QWidgetAction(menu)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 4, 12, 4)
        lay.setSpacing(8)
        lab = QLabel("Volume")
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setObjectName("SBVolume")
        sl.setRange(0, 100)
        sl.setSingleStep(10)
        sl.setPageStep(10)
        sl.setValue(int(self.settings.get("sound_volume", 70)))
        sl.setMinimumWidth(120)
        val = QLabel("{} %".format(sl.value()))
        val.setMinimumWidth(36)
        sl.valueChanged.connect(lambda v: (val.setText("{} %".format(v)), self.set_sound_volume(v, preview=False)))
        sl.sliderReleased.connect(lambda: self.set_sound_volume(sl.value(), preview=True))
        lay.addWidget(lab)
        lay.addWidget(sl, 1)
        lay.addWidget(val)
        act.setDefaultWidget(w)
        act.slider = sl
        return act

    def set_sound_volume(self, value, preview=True):
        value = int(max(0, min(100, value)))
        self.settings["sound_volume"] = value
        self.store.set_setting("ui", self.settings)
        if self.layer_picker is not None:
            self.layer_picker._update_sound_btn()
        if preview and value > 0:
            from . import sound
            sound.play_event("in", self.settings.get("sound_style", "whoosh"), value / 100.0)

    def set_sound(self, on):
        self.apply_settings({"sound": bool(on)})

    def set_sound_style(self, style, preview=True):
        """Choose a style (also switches sound on) and let the user hear it."""
        self.apply_settings({"sound_style": style, "sound": True})
        if preview:
            from . import sound
            sound.play_event("in", style, self.settings.get("sound_volume", 70) / 100.0)

    def _set_bubble_pos(self, index):
        self.settings.pop("bubble_xy", None)
        self.set_option("bubble_pos", index)

    def _recolor_icons(self):
        t = self.theme
        for b in (self.add_btn, self.color_btn, self.menu_btn, self.min_btn, self.close_btn):
            b.setIcon(t.icon(b.property("sb_icon"), t.accent if not t.dark else t.p["htext"], 16))
        for b in (self.coll_btn, self.undo_btn, self.import_btn, self.export_btn):
            b.setIcon(t.icon(b.property("sb_icon"), t.p["sub"], 16))
        self.apply_btn.setIcon(t.icon("apply", t.accent, 16))
        if self.collection_id is not None:
            self.coll_btn.setIcon(t.icon("folder", t.accent, 16))
        for key, b in self.chip_buttons.items():
            if b.isChecked():
                color = "#ffffff"
            elif key == "fav":
                color = "#e0a800"
            else:
                color = t.p["sub"]
            b.setIcon(t.icon(b.property("sb_icon"), color, 14))
        self.title_label.setStyleSheet("color: {};".format(t.p["htext"]))

    def show_bag_style(self):
        dlg = BagStyleDialog(self.settings, lambda v: self.apply_settings(v, save=False), self)
        if self.group is not None:            # the bubbles' size and place belong to the Tool Bag group
            for w in (dlg.size_caption, dlg.bubble_size, dlg.bubble_lbl, dlg.bubble_hint, dlg.pos_frame):
                w.hide()
        if dlg.exec():
            self.apply_settings(dlg.values, save=True)
        else:
            self.apply_settings(dlg.original, save=False)
        self.refresh()

    def show_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            smart_changed = dlg.values["smart"] != self.settings["smart"]
            self.apply_settings(dlg.values, save=True)
            if smart_changed:
                self.set_option("smart", dlg.values["smart"], refresh_smart=True)
            self.refresh()

    # ==================================================================
    # smart filter
    # ==================================================================
    def _layer_changed(self, layer, refresh=True):
        try:
            kind = layer_kind(layer) if layer is not None else None
        except RuntimeError:
            kind = None
        self.smart_kind = kind
        self.smart_layer_name = layer.name() if (layer is not None and kind) else ""
        self.smart_override = False
        QTimer.singleShot(0, self._picker_follow_layers)
        if self.settings.get("smart", True) and self.category in KINDS + ("all",):
            self.category = kind or "all"
            self.chip_buttons[self.category].setChecked(True)
            self.chip_bar.set_active(self.chip_buttons[self.category])
            self._recolor_icons()
        self._update_smart_label()
        if refresh:
            self.refresh(fade=True)

    def _tree_selection_changed(self, *_):
        QTimer.singleShot(0, self._update_smart_label)
        QTimer.singleShot(0, self._picker_follow_layers)

    def _smart_active(self):
        return bool(self.settings.get("smart", True) and self.smart_kind and not self.smart_override)

    def _update_smart_label(self):
        """Keep the layer box on the layer selected in the Layers panel (blank when none is)."""
        if not hasattr(self, "layer_combo") or self.iface is None:
            return
        layer = self.iface.activeLayer()
        self._refresh_layer_scope(layer)
        self.layer_combo.blockSignals(True)
        try:
            self.layer_combo.setLayer(layer if (layer is not None and self.smart_kind) else None)
        except RuntimeError:
            pass
        finally:
            self.layer_combo.blockSignals(False)

    def _scope_changed(self):
        QTimer.singleShot(0, self._update_smart_label)

    def _layer_combo_changed(self, layer):
        if layer is not None and self.iface is not None:
            self.iface.setActiveLayer(layer)

    def _refresh_layer_scope(self, keep=None):
        """List only the layers drawn on the open map (plus the selected one)."""
        shown = {lyr.id() for lyr in self.iface.mapCanvas().layers()}
        if keep is not None:
            with suppress(RuntimeError):
                shown.add(keep.id())
        self.layer_combo.setExceptedLayerList(
            [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.id() not in shown])

    def _smart_link(self, link):
        if link == "all":
            self.set_category("all", user=True)
        else:
            self.smart_override = False
            self.set_category(self.smart_kind, user=False)

    # ==================================================================
    # window behaviour: animations, minimize, bubble
    # ==================================================================
    # --- motion settings ---------------------------------------------------
    def motion_on(self):
        return bool(self.settings.get("animations", True)) and self.settings.get("motion", "normal") != "off"

    def _speed(self):
        return SPEED_FACTOR.get(self.settings.get("motion", "normal"), 1.0) or 1.0

    def _apply_motion_settings(self):
        on, speed = self.motion_on(), self._speed()
        self.view.set_motion(on, speed)
        self.bubble.motion_enabled = on
        self.bubble.speed = speed
        self.chip_bar.motion_enabled = on
        self.chip_bar.speed = speed

    def _animate(self, start, end, setter, duration=180, finished=None, curve=QEasingCurve.Type.OutCubic):
        if not self.motion_on():
            setter(end)
            if finished:
                finished()
            return None
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(int(duration * self._speed()))
        anim.setEasingCurve(curve)
        anim.valueChanged.connect(setter)

        def done():
            if anim in self._anims:
                self._anims.remove(anim)
            if finished:
                finished()
        anim.finished.connect(done)
        self._anims.append(anim)
        anim.start()
        return anim

    def minimize(self):
        if self._ghost_anim is not None and self._ghost_anim.running:
            return                      # let the current motion finish
        if self.in_bubble:
            self.restore_from_bubble()
        elif self.settings.get("bubble", True):
            self.minimize_to_bubble()
        else:
            self.toggle_collapsed()

    # --- snapshot ("ghost") motion engine ---------------------------------
    def _snapshot(self, hold_cards=False):
        """Image of the bag as it currently looks (works while hidden).
        hold_cards=True captures an empty bag so the cards can cascade in later."""
        lay = self.layout()
        if lay is not None:
            lay.activate()
        self.view.doItemsLayout()
        if hold_cards:
            self.view.hold_cards()
        return self.grab()

    def _fly(self, pixmap, duration, frame, finished=None, max_scale=1.12, drawer=False):
        """Animate the ghost. frame(t) returns a dict with any of:
        center (QPointF, global), sx, sy, rotation, opacity, image_opacity, ring, drawer."""
        self._stop_ghost()
        if not self.motion_on() or pixmap is None or pixmap.isNull():
            if finished:
                finished()
            return
        g = self.ghost
        g.reset()
        g.pixmap = pixmap
        size = QSizeF(pixmap.size()) / pixmap.devicePixelRatio()
        if drawer:
            g.set_box(size.width() + 2, size.height() + 2)
        else:
            tilt = math.radians(10)
            w = (size.width() * math.cos(tilt) + size.height() * math.sin(tilt)) * max_scale + 48
            h = (size.width() * math.sin(tilt) + size.height() * math.cos(tilt)) * max_scale + 48
            g.set_box(w, h)

        def apply(t):
            st = frame(t)
            g.sx = st.get("sx", 1.0)
            g.sy = st.get("sy", 1.0)
            g.rotation = st.get("rotation", 0.0)
            g.opacity = st.get("opacity", 1.0)
            g.image_opacity = st.get("image_opacity", 1.0)
            g.ring = st.get("ring")
            g.drawer = st.get("drawer")
            g.place(st["center"])
            g.update()

        apply(0.0)
        g.show_now()

        def done():
            self._ghost_anim = None
            if finished:
                finished()
            g.hide_soon()
        self._ghost_anim = Tween(self, duration * self._speed(), apply, done).start()

    def _stop_ghost(self):
        if self._ghost_anim is not None:
            anim = self._ghost_anim
            self._ghost_anim = None
            anim.stop(finish=True)
            self.ghost.hide()

    def _fade(self):
        return self.settings.get("fade", 100) / 100.0

    @staticmethod
    def _center(rect):
        return QPointF(rect.x() + rect.width() / 2.0, rect.y() + rect.height() / 2.0)

    # --- fold up (when the bubble is off): a drawer sliding under the header --
    def toggle_collapsed(self):
        folded_h = self.header.geometry().bottom() + 2
        geo = self.geometry()
        radius = 16 if not self.settings.get("compact") else 11
        center = self._center(geo)
        if not self.collapsed:
            self.collapsed = True
            self._expanded_geo = QRect(geo)
            pm = self._snapshot()
            full_h = pm.height() / pm.devicePixelRatio()
            self.setMinimumHeight(0)
            self.zipper.hide()
            self.content.hide()
            self.resize(geo.width(), folded_h)
            self.hide_for_ghost()

            def frame(t):
                vis = lerp(full_h, folded_h, ease_in(t, 2.4))
                return {"center": center, "drawer": (folded_h, vis, radius), "opacity": self._fade()}
            self._fly(pm, 240, frame, self.show_after_ghost, drawer=True)
        else:
            self.collapsed = False
            target = QRect(geo.topLeft(), self._expanded_geo.size() if self._expanded_geo else QSize(320, 560))
            self.hide_for_ghost()
            self.zipper.show()
            self.content.show()
            self.setGeometry(target)
            self.setMinimumHeight(120)
            pm = self._snapshot()
            full_h = pm.height() / pm.devicePixelRatio()
            tcenter = self._center(target)

            def frame(t):
                vis = min(full_h, lerp(folded_h, full_h, spring(t, 0.78)))
                return {"center": tcenter, "drawer": (folded_h, vis, radius), "opacity": self._fade()}
            self._fly(pm, 380, frame, self.show_after_ghost, drawer=True)
        self.min_btn.setProperty("sb_icon", "expand" if self.collapsed else "minimize")
        self.min_btn.setToolTip("Unfold" if self.collapsed else "Fold up")
        self._recolor_icons()

    def hide_for_ghost(self):
        """Hide the real window without telling the plugin the bag closed."""
        self._ghosting = True
        self.hide()
        self._ghosting = False

    def show_after_ghost(self):
        self._ghosting = True
        self.setWindowOpacity(self._fade())
        self.show()
        self.raise_()
        self._ghosting = False

    def _bubble_target(self):
        """Top-left position of the bubble in global coordinates."""
        if self.group is not None:             # its place in the column of bubbles
            self.bubble.group_set_size(self.group.size_of("symbol"))
            c = self.group.target_for("symbol")
            off = self.bubble.circle_rect().center()
            return QPoint(int(round(c.x() - off.x())), int(round(c.y() - off.y())))
        xy = self.settings.get("bubble_xy")
        size = self.bubble.size()
        if xy:
            return QPoint(int(xy[0]), int(xy[1]))
        if self.iface is not None:
            canvas = self.iface.mapCanvas()
            area = QRect(canvas.mapToGlobal(QPoint(0, 0)), canvas.size())
        else:
            area = QApplication.primaryScreen().availableGeometry()
        m = 18 - BubbleWidget.MARGIN
        i = self.settings.get("bubble_pos", 2)
        col, row = i % 3, i // 3
        xs = [area.left() + m, area.center().x() - size.width() // 2, area.right() - size.width() - m]
        ys = [area.top() + m, area.center().y() - size.height() // 2, area.bottom() - size.height() - m]
        return QPoint(xs[col], ys[row])

    def _place_bubble(self):
        pos = self._bubble_target()
        parent = self.bubble.parentWidget()
        self.bubble.move(parent.mapFromGlobal(pos) if parent is not None and not self.bubble.isWindow() else pos)

    def _bubble_moved(self, pos):
        if self.group is not None:
            return
        self.settings["bubble_xy"] = [pos.x(), pos.y()]
        self.store.set_setting("ui", self.settings)

    # --- bubble: the bag is tossed along an arc into the bubble -------------
    @staticmethod
    def _genie_style():
        from ...bag_group import animation_style
        return animation_style() == "genie"

    def _genie(self, minimize, pm, geo, finished=None, target=None):
        """macOS-style genie: the bag pours into the bubble and flows back out."""
        from .widgets import GenieWidget
        self._stop_ghost()
        if getattr(self, "_genie_w", None) is None:
            self._genie_w = GenieWidget(self.parentWidget())
        g = self._genie_w
        if target is None:
            r = self.bubble.circle_rect()
            target = QRectF(QPointF(self.bubble.mapToGlobal(r.topLeft().toPoint())), r.size())
        g.setup(pm, geo, target)
        s = self.settings
        if s.get("sound", True):
            from . import sound
            sound.play_event("in" if minimize else "out", s.get("sound_style", "whoosh"),
                             s.get("sound_volume", 70) / 100.0)
        g.set_t(0.0 if minimize else 1.0)
        g.show_now()                        # exactly over the bag ...
        if minimize:
            self.hide()                     # ... so hiding it changes nothing on screen

        def frame(t):
            g.set_t(t if minimize else 1.0 - t)

        def end():
            self._ghost_anim = None
            if minimize:
                g.hide()
                g.pixmap = None
            else:
                self.show_after_ghost()     # the bag appears exactly under the last frame ...
                g.hide_soon()               # ... then the overlay leaves
            if finished:
                finished()
        self._ghost_anim = Tween(self, (520 if minimize else 460) * self._speed(), frame, end).start()

    def minimize_to_bubble(self):
        if self.in_bubble:
            return
        self.close_picker()
        self.in_bubble = True
        self._expanded_geo = self.geometry()
        self.bubble.set_count(self.store.count())
        self._place_bubble()
        geo = self.geometry()
        pm = self._snapshot()
        if self.motion_on() and self._genie_style():
            self.bubble.scale = 0.0
            self.bubble.alpha = 1.0
            self.bubble._closing = False
            self.bubble.show()
            self.bubble.raise_()
            self.bubble.pop_in()
            self._genie(True, pm, geo)
            return
        pw, ph = float(geo.width()), float(geo.height())
        p0 = self._center(geo)
        p1 = self.bubble.circle_center_global()
        ctrl = arc_control(p0, p1, 0.22)
        d = float(self.bubble.size_px)
        sx_end, sy_end = d / pw, d / ph
        side = 1.0 if p1.x() >= p0.x() else -1.0
        fade = self._fade()
        popped = [False]

        def frame(t):
            f = segment(t, 0.0, 0.62)                  # travel phase
            path = ease_in_out(f)
            if f > 0.82 and not popped[0]:             # bubble pops as the bag arrives
                popped[0] = True
                self.bubble.scale = 0.0
                self.bubble.show()
                self.bubble.raise_()
                self.ghost.raise_()
                self.bubble.pop_in()
            st = {"center": bezier(p0, ctrl, p1, path),
                  "sx": lerp(1.0, sx_end, ease_in(f, 2.2)),
                  "sy": lerp(1.0, sy_end, ease_in(f, 1.5)),
                  "rotation": 9.0 * side * math.sin(math.pi * f),
                  "opacity": fade,
                  "image_opacity": 1.0 - ease_in(segment(f, 0.55, 1.0), 1.4)}
            r = segment(t, 0.55, 1.0)                  # ripple on landing
            if r > 0:
                st["ring"] = (d / 2.0 * (1.0 + 0.95 * ease_out(r)), 0.55 * (1.0 - r),
                              2.6 * (1.0 - r) + 0.4, self.theme.accent)
            return st

        def done():
            if not self.bubble.isVisible():
                self.bubble.show()
                self.bubble.pop_in()
        if not self.motion_on():
            self.hide()
            self.bubble.scale = 1.0
            self.bubble.alpha = 1.0
            self.bubble.show()
            return
        self._fly(pm, 560, frame, done)
        self.hide()                 # after the ghost is on screen: no blank frame

    def restore_from_bubble(self):
        if not self.in_bubble:
            return
        self.in_bubble = False
        p0 = self.bubble.circle_center_global()
        geo = self._expanded_geo or QRect(self._bubble_target(), QSize(320, 560))
        if self.group is not None:             # reveal the bag at its bubble's spot
            geo = QRect(self.group.anchor_topleft(p0, self.bubble.size_px / 2.0, geo.width(), geo.height(), "symbol"),
                        geo.size())
            self._expanded_geo = geo
        self.setGeometry(geo)
        pm = self._snapshot(hold_cards=True)
        if self.motion_on() and self._genie_style():
            self.visibilityChanged.emit(True)

            def poured_out():
                self.bubble.shrink_out(self.bubble.hide)
                self.activateWindow()
                self.view.start_cascade()
            self._genie(False, pm, geo, poured_out)
            return
        pw, ph = float(geo.width()), float(geo.height())
        p1 = self._center(geo)
        ctrl = arc_control(p0, p1, 0.18)
        d = float(self.bubble.size_px)
        sx0, sy0 = d / pw, d / ph
        side = 1.0 if p1.x() >= p0.x() else -1.0
        fade = self._fade()
        self.bubble.shrink_out(self.bubble.hide)

        def frame(t):
            k = spring(t, 0.6)
            return {"center": bezier(p0, ctrl, p1, spring(t, 0.7)),
                    "sx": lerp(sx0, 1.0, k), "sy": lerp(sy0, 1.0, spring(t, 0.66)),
                    "rotation": -8.0 * side * (1.0 - ease_out(t, 2)) * (1.0 - t),
                    "opacity": fade * ease_out(segment(t, 0.0, 0.3), 2)}

        def done():
            self.show_after_ghost()
            self.activateWindow()
            self.view.start_cascade()
        self.visibilityChanged.emit(True)
        if not self.motion_on():
            self.bubble.hide()
            done()
            return
        self._fly(pm, 520, frame, done)

    # --- open: drops in and settles; close: lifts away -----------------------
    def show_animated(self):
        self._closing = False
        if self.in_bubble:
            self.bubble.show()
            self.bubble.pop_in()
            self.visibilityChanged.emit(True)
            return
        geo = self.geometry()
        slot = self.group.slot_rect("symbol") if self.group is not None else None
        if slot is not None and self.motion_on() and self._genie_style():
            # from the toolbar: out of where its bubble sits, at that spot
            geo = QRect(self.group.anchor_topleft(slot.center(), slot.width() / 2.0, geo.width(), geo.height(), "symbol"),
                        geo.size())
            self.setGeometry(geo)
            pm = self._snapshot(hold_cards=True)

            def opened():
                self.activateWindow()
                self.search.setFocus()
                self.view.start_cascade()
            self.visibilityChanged.emit(True)
            self._genie(False, pm, geo, opened, target=slot)
            return
        pm = self._snapshot(hold_cards=True)
        p1 = self._center(geo)
        p0 = QPointF(p1.x(), p1.y() - 28)
        fade = self._fade()

        def frame(t):
            k = spring(t, 0.58)
            return {"center": QPointF(p1.x(), lerp(p0.y(), p1.y(), k)),
                    "sx": 0.92 + 0.08 * spring(t, 0.62), "sy": 0.92 + 0.08 * k,
                    "opacity": fade * ease_out(segment(t, 0.0, 0.35), 2)}

        def done():
            self.show_after_ghost()
            self.activateWindow()
            self.search.setFocus()
            self.view.start_cascade()
        self.visibilityChanged.emit(True)
        self._fly(pm, 460, frame, done)

    def close_animated(self):
        self.close_picker()
        if self.in_bubble:
            self.bubble.shrink_out(self.bubble.hide)
            self.visibilityChanged.emit(False)
            return
        if not self.isVisible():
            return
        geo = self.geometry()
        pm = self._snapshot()
        slot = self.group.slot_rect("symbol") if self.group is not None else None
        if slot is not None and self.motion_on() and self._genie_style():
            self._expanded_geo = QRect(geo)
            self._genie(True, pm, geo, target=slot)      # pours into where its bubble sits
            return
        p0 = self._center(geo)
        fade = self._fade()

        def frame(t):
            e = ease_in(t, 2)
            return {"center": QPointF(p0.x(), p0.y() - 18 * e), "sx": 1.0 - 0.08 * e, "sy": 1.0 - 0.08 * e,
                    "opacity": fade * (1.0 - ease_in(t, 1.6))}
        self._fly(pm, 220, frame)
        self.hide()                 # after the ghost is on screen: no blank frame

    def is_active(self):
        """Visible as a bag or as a bubble (or animating into view)."""
        return (self.isVisible() or (self.in_bubble and self.bubble.isVisible())
                or (self._ghost_anim is not None and self._ghost_anim.running and not self.in_bubble))

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_ghosting", False):
            self.visibilityChanged.emit(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        if not self.in_bubble and not getattr(self, "_ghosting", False):
            self.visibilityChanged.emit(False)

    def moveEvent(self, event):
        super().moveEvent(event)
        # the Symbol Colors picker belongs to the bag: it travels with it
        pos = self.pos()
        last, self._last_pos = self._last_pos, QPoint(pos)
        p = self.picker
        if last is not None and p is not None and p.isVisible() and self.isVisible() \
                and not getattr(self, "_ghosting", False):
            p.move(p.pos() + (pos - last))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.title_label.setVisible(self.width() > 250)
        self.update()

    def save_window_state(self):
        try:
            geo = self._expanded_geo if (self.collapsed or self.in_bubble) and self._expanded_geo else self.geometry()
            self.store.set_setting("window", {
                "geometry": [geo.x(), geo.y(), geo.width(), geo.height()],
                "visible": self.is_active(),
            })
        except Exception as e:
            log_error("Could not save window state: {}".format(e))

    def restore_window_state(self):
        state = self.store.get_setting("window", None)
        if not state or not isinstance(state.get("geometry"), list):
            return False
        x, y, w, h = state["geometry"]
        rect = QRect(x, y, max(220, w), max(160, h))
        screen = QApplication.screenAt(rect.center()) if hasattr(QApplication, "screenAt") else None
        if screen is None:
            return False
        self.setGeometry(rect)
        return True

    # ==================================================================
    # data → view
    # ==================================================================
    def _store_changed(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(0, self.refresh)

    def set_category(self, key, user=False):
        if user:
            if key == "all":
                self.smart_override = bool(self.smart_kind)
            elif key in KINDS:
                self.smart_override = key != self.smart_kind
        self.category = key
        self.chip_buttons[key].setChecked(True)
        self.chip_bar.set_active(self.chip_buttons[key])
        self._recolor_icons()
        self._update_smart_label()
        self.refresh(fade=True)

    def set_collection(self, cid):
        self.collection_id = cid
        self._recolor_icons()
        self.refresh(fade=True)

    def _matches(self, rec, tokens, coll_names):
        hay = " ".join([rec.name, rec.tags or "", rec.description or "", rec.kind_label, rec.kind,
                        rec.subtype or "", coll_names.get(rec.collection_id, "")]).lower()
        return all(t in hay for t in tokens)

    def effective_kind(self):
        c = self.category
        if c in KINDS:
            return c
        if c in ("fav", "recent") and self._smart_active():
            return self.smart_kind
        return None

    def visible_records(self):
        records = self.store.symbol_records()
        coll_names = {c["id"]: c["name"] for c in self.store.collections()}
        if self.collection_id == UNSORTED:
            records = [r for r in records if r.collection_id is None]
        elif self.collection_id is not None:
            ids = set(self.store.descendant_collections(self.collection_id))
            records = [r for r in records if r.collection_id in ids]
        kind = self.effective_kind()
        if kind:
            records = [r for r in records if r.kind == kind]
        c = self.category
        if c == "fav":
            records = [r for r in records if r.favorite]
        elif c == "recent":
            records = sorted([r for r in records if r.last_used], key=lambda r: -r.last_used)
            records = records[:self.settings.get("recent_limit", 24)]
        tokens = [t for t in self.search.text().lower().split() if t]
        if tokens:
            records = [r for r in records if self._matches(r, tokens, coll_names)]
        sort = self.settings.get("sort", "manual")
        if c != "recent":
            if sort == "name":
                records.sort(key=lambda r: r.name.lower())
            elif sort == "used":
                records.sort(key=lambda r: (-r.use_count, r.name.lower()))
            elif sort == "type":
                order = {KIND_MARKER: 0, KIND_LINE: 1, KIND_FILL: 2, KIND_RASTER: 3}
                records.sort(key=lambda r: (order.get(r.kind, 9), r.sort_order))
            elif sort == "newest":
                records.sort(key=lambda r: -r.created)
        return records, coll_names

    def refresh(self, fade=False):
        self._refresh_pending = False
        if self.store.conn is None:
            return
        selected = set(self.selected_uids())
        records, coll_names = self.visible_records()
        self.model.collection_names = coll_names
        self.view.motion.hover.clear()
        self.model.set_records(records)
        self.view.reorder_enabled = self.settings.get("sort", "manual") == "manual" and self.category != "recent"
        sel = self.view.selectionModel()
        for uid in selected:
            idx = self.model.index_of(uid)
            if idx.isValid():
                sel.select(idx, sel.SelectionFlag.Select)
        total = self.store.count()
        if not records:
            if total == 0:
                self.empty.setText("Your bag is empty.\n\nClick  +  to create a symbol\nor import some.")
            elif self.effective_kind() and self.category not in ("fav", "recent") and not self.search.text():
                self.empty.setText("No {} symbols here yet.\n\nSelect a symbol on a layer and use\n"
                                   "+ → Add Current Layer Symbol.".format(KIND_LABELS[self.effective_kind()].lower()))
            else:
                self.empty.setText("No symbols match.")
            self.stack.setCurrentWidget(self.empty)
        else:
            self.stack.setCurrentWidget(self.view)
        self.bubble.set_count(total)
        self._update_footer()
        if fade and self.motion_on() and self.isVisible():
            self._fade_view()

    def _fade_view(self):
        """Cards cascade in after a filter change (rise, scale, fade, staggered)."""
        self.view.start_cascade()

    def _collection_label(self):
        if self.collection_id is None:
            return ""
        if self.collection_id == UNSORTED:
            return "Unsorted"
        return " / ".join(self.store.collection_path(self.collection_id)) or "?"

    def _update_footer(self, *_):
        n = self.model.rowCount()
        total = self.store.count() if self.store.conn else 0
        text = "{} symbol{}".format(n, "" if n == 1 else "s") + ("" if n == total else " of {}".format(total))
        coll = self._collection_label()
        if coll:
            text += "  ·  " + coll
        self.count_label.setText(text)
        self.coll_btn.setToolTip("Collection: " + (coll or "All collections"))
        self.undo_btn.setEnabled(self.undo.can_undo())
        self._update_apply_btn()

    # ==================================================================
    # ==================================================================
    # colour picker
    # ==================================================================
    def _btn_center(self, w):
        return w.mapToGlobal(w.rect().center())

    def ensure_picker(self):
        """Symbol Colors – the picker inside the bag (bag symbols only)."""
        if self.picker is None:
            from .color_picker import ColorPicker
            self.picker = ColorPicker(self, "symbol")
            self.picker.previewChanged.connect(self._picker_preview)
        return self.picker

    def ensure_layer_picker(self):
        """Layer Colors – the standalone tool for map layers."""
        if self.layer_picker is None:
            from .color_picker import ColorPicker
            from qgis.core import QgsProject
            self.layer_picker = ColorPicker(self, "layer")
            if not self.embedded_pickers:
                QgsProject.instance().layersWillBeRemoved.connect(self._picker_layers_removed)
        return self.layer_picker

    def open_picker(self, uid=None, origin=None, mode=None):
        """uid → recolour that bag symbol (Symbol Colors);
        mode="layer" → the standalone Layer Colors tool."""
        if mode == "layer":
            self.open_layer_colors(origin)
            return
        picker = self.ensure_picker()
        if origin is None:
            origin = self._btn_center(self.color_btn) if self.isVisible() else QCursor.pos()
        picker.open_from(origin, uid or None)

    def open_layer_colors(self, origin=None):
        self.ensure_layer_picker().open_from(origin if origin is not None else QCursor.pos())

    def toggle_layer_colors(self, origin=None):
        p = self.ensure_layer_picker()
        if p.in_bubble:
            p.restore_from_bubble()
        elif p.isVisible():
            p.close_animated()
        else:
            self.open_layer_colors(origin)

    def toggle_picker(self, mode=None, origin=None):
        if mode == "layer":
            self.toggle_layer_colors(origin)
            return
        picker = self.ensure_picker()
        if picker.isVisible():
            picker.close_animated()
        else:
            self.open_picker(None, origin)

    def _picker_layers_removed(self, layers):
        # QgsProject.layersWillBeRemoved comes as layer ids or as layer objects
        ids = [x if isinstance(x, str) else x.id() for x in layers]
        for p in [self.layer_picker] + list(self.embedded_pickers):
            if p is not None and p.layer_id in ids:
                with suppress(RuntimeError):
                    p.layer_id = None
                    p._end_session()
                    p._set_target_ui()

    def _picker_follow_layers(self):
        for p in [self.layer_picker] + list(self.embedded_pickers):
            with suppress(RuntimeError):
                if p is not None and p.isVisible():
                    p.follow_layer_selection()

    def create_embedded_layer_colors(self, parent):
        """Layer Colors as a child widget for another panel's layout."""
        from .color_picker import ColorPicker
        from qgis.core import QgsProject
        if self.layer_picker is None and not self.embedded_pickers:
            QgsProject.instance().layersWillBeRemoved.connect(self._picker_layers_removed)
        w = ColorPicker(self, "layer", embedded=parent)
        self.embedded_pickers.append(w)
        w.destroyed.connect(lambda *_a, w=w: self._forget_embedded(w))
        return w

    def _forget_embedded(self, w):
        self.embedded_pickers = [p for p in self.embedded_pickers if p is not w]

    def _picker_preview(self, uid, pixmap):
        ov = self.view.card_delegate.preview_override
        if pixmap is None:
            ov.pop(uid, None)
        else:
            ov[uid] = pixmap
        idx = self.model.index_of(uid)
        if idx.isValid():
            self.view.viewport().update(self.view.visualRect(idx).adjusted(-8, -8, 8, 8))

    def _update_apply_btn(self, *_):
        self.apply_btn.setEnabled(bool(self.current_uid() or self.selected_uids()))

    def _current_card_changed(self, current, _previous):
        self._update_apply_btn()
        if self.picker is not None and self.picker.isVisible() and current.isValid():
            uid = current.data(ROLE_UID)
            if uid and uid != self.picker.uid:
                if self.picker.dirty:
                    self._msg("Unsaved color changes were discarded (the original symbol is unchanged).")
                self.picker.retarget(uid)

    def close_picker(self):
        """The bag is going away: its Symbol Colors picker goes with it
        (the standalone Layer Colors tool is independent and stays)."""
        if self.picker is not None and self.picker.isVisible():
            self.picker.close_animated()

    def cleanup(self):
        """Disconnect everything – called when the plugin unloads."""
        for a in list(self._anims):
            a.stop()
        self._anims = []
        self.store.remove_listener(self._store_changed)
        if self.group is not None:
            with suppress(Exception):
                self.group.settingsChanged.disconnect(self._on_common)
        if self.iface is not None:
            with suppress(Exception):
                self.iface.mapCanvas().layersChanged.disconnect(self._scope_changed)
        if self.layer_filter is not None:
            self.layer_filter.remove()
            self.layer_filter = None
        if self.iface is not None:
            with suppress(TypeError, RuntimeError, AttributeError):
                self.iface.layerTreeView().currentLayerChanged.disconnect(self._layer_changed)
                self._tree_sel_model.selectionChanged.disconnect(self._tree_selection_changed)
        self.cache.clear()
        self._stop_ghost()
        if self.layer_picker is not None or self.embedded_pickers:
            with suppress(TypeError, RuntimeError):
                from qgis.core import QgsProject
                QgsProject.instance().layersWillBeRemoved.disconnect(self._picker_layers_removed)
        for p in list(self.embedded_pickers):
            with suppress(RuntimeError):
                p.cleanup()             # the host notices through the widget's destroyed signal
        self.embedded_pickers = []
        if self.layer_picker is not None:
            self.layer_picker.cleanup()
            self.layer_picker = None
        for p in list(self.embedded_pickers):
            try:
                p.cleanup()            # the host panel sees the widget's destroyed() signal
            except RuntimeError:
                pass
        self.embedded_pickers = []
        if self.picker is not None:
            self.picker.cleanup()
            self.picker = None
        for w in [x for x in (self.bubble, self.ghost, getattr(self, "_genie_w", None)) if x is not None]:
            with suppress(RuntimeError):
                w.hide()
                w.setParent(None)
                w.deleteLater()

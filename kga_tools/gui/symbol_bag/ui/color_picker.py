# -*- coding: utf-8 -*-
"""
Symbol Bag – Smart Color Picker.

Pick a colour – customise a symbol – apply instantly.

Workflow: select a symbol card → open the picker (header button, the card's
palette icon or right-click → Recolor) → choose what to recolour (whole
symbol, all fills, all outlines, one colour of one layer, or a raster colour
stop) → pick a colour. The card previews the change live; nothing is saved
until you press Apply, Save as New or Replace. Undo / Redo / Reset work on
the colour edits of this session.
"""

from contextlib import suppress
import time

from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFileDialog, QHBoxLayout,
                                 QInputDialog, QLabel, QLayout, QLineEdit, QMenu, QMessageBox, QPushButton,
                                 QSizeGrip, QSizePolicy, QSlider, QSpinBox, QDoubleSpinBox, QStackedWidget, QToolButton,
                                 QVBoxLayout, QWidget)
from qgis.core import Qgis, QgsProject, QgsStyle

from ....core.compat import scoped
from ..core import recolor as rc
from ..core.applier import legend_rule_key as legend_key_of
from ..core.palettes import PaletteStore, to_hex
from ..core.storage import KIND_RASTER, LibraryError
from ..core.symbols import broken_preview, raster_preview_image, symbol_from_xml, vector_preview
from .color_wheel import ColorWheel, EyedropperSession, StopStrip, SwatchGrid, WidthDial
from .motion import ease_in, ease_out
from .theme import BAG_STYLES, header_gradient, paint_zipper
from .widgets import ChipBar, GhostWidget

SIZES = [("small", "Small", 150), ("medium", "Medium", 200), ("large", "Large", 260)]
SIZE_PX = {k: px for k, _l, px in SIZES}
PAD = 14


class _Header(QWidget):
    """Drag to move the picker."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._press = None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            gp = e.globalPos() if hasattr(e, "globalPos") else e.globalPosition().toPoint()
            self._press = gp - self.win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._press is not None:
            gp = e.globalPos() if hasattr(e, "globalPos") else e.globalPosition().toPoint()
            self.win.move(gp - self._press)

    def mouseReleaseEvent(self, e):
        if self._press is not None:
            self._press = None
            self.win.save_state()



class FlowLayout(QLayout):
    """Lays chips out left to right and wraps onto the next line when full,
    so many choices never get squeezed or cut."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self._space = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0) if hasattr(Qt, "Orientations") else Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _do_layout(self, rect, test):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            w = it.widget()
            if w is not None and w.isHidden():
                continue
            hint = it.sizeHint()
            nx = x + hint.width() + self._space
            if nx - self._space > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y += line_h + self._space
                nx = x + hint.width() + self._space
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()

class ColorPicker(QWidget):
    """Floating, movable, resizable colour picker in the Symbol Bag style.

    Two separate tools share this class (role):
      "symbol" – opened inside the bag; recolours symbols in the bag
                 (Save as New / Replace) and sits beside the bag;
      "layer"  – the standalone "Layer Colors" tool from the toolbar; recolours
                 the layer / categories selected in the Layers panel live on the
                 map and minimizes to its own bubble, like the bag."""

    previewChanged = pyqtSignal(str, object)     # uid, QPixmap or None

    def __init__(self, panel, role="symbol", embedded=None):
        # The tool does not take focus when clicked (buttons, wheel, swatches):
        # on Windows focus moving between QGIS and a floating window makes the
        # QGIS title bar flash. Only typing in a field turns focus on (_allow_focus).
        #
        # embedded=<parent widget>: the same tool as an ordinary child widget that
        # another panel places in its own layout (no floating window, no bubble,
        # no close / minimize; it follows the Layers panel selection).
        self.embedded = embedded is not None
        if self.embedded:
            super().__init__(embedded)
        else:
            super().__init__(panel.parentWidget(), Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowDoesNotAcceptFocus)
        self._no_focus = True
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # opening the tool must not take focus from QGIS (on Windows that makes
        # the main window's title bar flick inactive/active); clicking a field
        # still focuses it normally
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.role = "layer" if role == "layer" else "symbol"
        self.setObjectName("SymbolBagLayerColors" if self.role == "layer" else "SymbolBagColorPicker")
        self.setWindowTitle("Symbol Bag – Layer Colors" if self.role == "layer" else "Symbol Bag – Symbol Colors")
        self.panel = panel
        self.store = panel.store
        self.palettes = PaletteStore(self.store)
        self._skey = "layer_picker" if self.role == "layer" else "picker"
        if self.embedded:
            self._skey = "layer_picker_embedded"
        self.state = dict({"size": "medium", "custom": 220, "palette": "Professional GIS", "input": "hex"},
                          **(self.store.get_setting(self._skey, {}) or {}))
        self.state.pop("mode", None)
        if self.role == "layer" and self.state.get("style") not in BAG_STYLES:
            self.state["style"] = panel.theme.name       # its own colour from now on (never follows the bag)
        self.bubble = None
        self.in_bubble = False
        self.theme = self.own_theme(panel.theme)
        self.ghost = None if self.embedded else GhostWidget(panel.parentWidget())
        self._tween = None
        self._origin = None
        self._programmatic = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(30)
        self._preview_timer.timeout.connect(self._render_preview)
        # recolour session (see "recolour session" below)
        self.mode = self.role
        self.uid = None          # symbol mode: bag symbol
        self.layer_id = None     # layer mode: map layer …
        self.layer_keys = None   # … and its selected legend classes (None = whole layer)
        self.kind = None
        self.items = []          # [(item key, label)]
        self.title_text = ""
        self.original = None     # {item: xml} when the session started
        self.saved = None        # {item: xml} as stored (bag) / as on the map
        self.committed = None    # last confirmed edit (undo point)
        self.working = None      # what the preview / map shows right now
        self.target = ("_", ("tint",))
        self.undo_stack, self.redo_stack = [], []
        self._snapshot_taken = False
        self._pending_color = None
        self._pending_width = None
        self._pending_size = None
        self.dim_mode = "size"           # point layers: the dial sets "size" or the outline "width"
        self._render_t0 = None
        self._last_render_s = 0.0      # duration of the canvas's last complete render
        self._dense_px = None
        self._dense_timer = QTimer(self)
        self._dense_timer.setSingleShot(True)
        self._dense_timer.setInterval(300)
        self._dense_timer.timeout.connect(self._check_density)
        self.color = QColor("#2f80ed")
        self._build()
        self._watch_canvas()
        self.apply_theme(self.theme)
        self.set_size_mode(self.state["size"], animate=False)
        self._load_palettes()
        self._refresh_recent()
        self._refresh_favs()
        self._apply_swatch_source()
        self._set_target_ui()
        self._show_color(self.color, source=None, animate=False)
        for w in self.findChildren(QLineEdit):          # incl. the spin boxes' editors
            w.installEventFilter(self)
        self._scope_timer = QTimer(self)
        self._scope_timer.setSingleShot(True)
        self._scope_timer.setInterval(0)
        self._scope_timer.timeout.connect(self._refresh_layer_scope)
        if self.role == "layer" and self.panel.iface is not None:
            canvas = self.panel.iface.mapCanvas()
            canvas.layersChanged.connect(self._scope_timer.start)
            QgsProject.instance().layersAdded.connect(self._scope_timer.start)
            QgsProject.instance().layersRemoved.connect(self._scope_timer.start)
            self._refresh_layer_scope()
        if self.embedded:
            self._setup_embedded()

    # ==================================================================
    # construction
    # ==================================================================
    def _btn(self, icon, tip, slot=None, obj="SBHeaderBtn", size=24):
        b = QToolButton()
        b.setObjectName(obj)
        b.setToolTip(tip)
        b.setProperty("sb_icon", icon)
        if obj == "SBHeaderBtn":
            b.setFixedSize(size, size)
            b.setIconSize(QSize(size - 9, size - 9))
        else:
            b.setAutoRaise(True)
            b.setIconSize(QSize(16, 16))
        if slot is not None:
            b.clicked.connect(slot)
        return b

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self.header = _Header(self)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(10, 6, 8, 6)
        hl.setSpacing(5)
        self.logo = QLabel()
        self.logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title = QLabel("Layer Colors" if self.role == "layer" else "Symbol Colors")
        self.title.setObjectName("SBTitleLabel")
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        hl.addWidget(self.logo)
        hl.addWidget(self.title)
        hl.addStretch(1)
        has_gear = self.role == "layer" and getattr(self.panel, "group", None) is not None
        self.size_btn = None
        if not has_gear:               # with the gear, wheel size and sound live in its menu
            self.size_btn = self._btn("resize", "Wheel size (Small / Medium / Large) and sound effects")
            self.size_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self.size_menu = QMenu(self)
            self.size_menu.aboutToShow.connect(self._fill_size_menu)
            self.size_btn.setMenu(self.size_menu)
        self.close_btn = self._btn("close", "Close the color picker", self.close_animated)
        self.sound_btn = None
        if self.role == "layer" and not has_gear:
            # speaker: click = sound on / off; right-click = sound style
            self.sound_btn = self._btn("speaker", "", self._toggle_sound)
            self.sound_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.sound_btn.customContextMenuRequested.connect(self._sound_style_menu)
            hl.addWidget(self.sound_btn)
        self.gear_btn = None
        if has_gear:
            # the same settings as the Tool Bag and the Symbol Bag: design, bubble, animation, sound
            self.gear_btn = self._btn("settings", "Settings (shared with the Tool Bag and the Symbol Bag)")
            self.gear_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self.gear_menu = QMenu(self)
            self.gear_menu.aboutToShow.connect(self._fill_gear_menu)
            self.gear_btn.setMenu(self.gear_menu)
            hl.addWidget(self.gear_btn)
        if self.size_btn is not None:
            hl.addWidget(self.size_btn)
        self.min_btn = None
        if self.role == "layer":
            self.min_btn = self._btn("minimize", "Minimize to a bubble on the map", self.minimize_to_bubble)
            hl.addWidget(self.min_btn)
        hl.addWidget(self.close_btn)
        root.addWidget(self.header)
        self.zipper = QWidget()
        self.zipper.setFixedHeight(11)
        self.zipper.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        root.addWidget(self.zipper)

        body = QWidget()
        body.setObjectName("SBContent")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 6, 10, 4)
        bl.setSpacing(6)
        root.addWidget(body, 1)

        # --- layer role: which layer (always visible, so it is never a dead end)
        self.layer_row = None
        self.layer_combo = None
        if self.role == "layer":
            from qgis.gui import QgsMapLayerComboBox
            self.layer_row = QWidget()
            lr = QHBoxLayout(self.layer_row)
            lr.setContentsMargins(0, 0, 0, 0)
            lr.setSpacing(6)
            lab = QLabel("Layer")
            lab.setObjectName("SBPickerSection")
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
            self.layer_combo.setToolTip("The layer to recolor – or simply select a layer / category in the Layers panel")
            self.layer_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.layer_combo.setMinimumContentsLength(10)
            self.layer_combo.layerChanged.connect(self._layer_combo_changed)
            lr.addWidget(lab)
            lr.addWidget(self.layer_combo, 1)
            bl.addWidget(self.layer_row)

        # --- what is being recoloured ---------------------------------------
        self.target_box = QWidget()
        tl = QHBoxLayout(self.target_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        self.preview = QLabel()
        self.preview.setFixedSize(46, 46)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tl.addWidget(self.preview)
        tcol = QVBoxLayout()
        tcol.setSpacing(3)
        self.name_label = QLabel()
        self.name_label.setObjectName("SBPickerName")
        self.name_label.setMinimumWidth(40)
        try:
            self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        except AttributeError:
            self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.target_combo = QComboBox()
        self.target_combo.setToolTip("What to recolor: the whole symbol (keeping its shades), every fill,\n"
                                     "every outline, or one color of one symbol layer.")
        self.target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target_combo.setMinimumContentsLength(12)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        tcol.addWidget(self.name_label)
        # what to recolour: box-shaped choices (Whole / Fill color / Outline / Layers ▾).
        # The combo stays as the list of targets behind them (not shown).
        self.target_combo.hide()
        self.part_bar = QWidget()
        self.part_layout = FlowLayout(self.part_bar, spacing=4)
        self.part_buttons = []
        tcol.addWidget(self.part_bar)
        tl.addLayout(tcol, 1)
        bl.addWidget(self.target_box)
        # dense-layer hint (layer mode): at small scales the outline is most of what is seen
        self.dense_hint = QLabel()
        self.dense_hint.setObjectName("SBPickerHint")
        self.dense_hint.setWordWrap(True)
        self.dense_hint.linkActivated.connect(self._dense_link)
        self.dense_hint.hide()
        bl.addWidget(self.dense_hint)
        # empty fill (outline-only polygons): explain that a fill colour fills them
        self.fill_hint = QLabel()
        self.fill_hint.setObjectName("SBPickerHint")
        self.fill_hint.setWordWrap(True)
        self.fill_hint.setText("The fill is empty here – only the outlines show. Picking a color fills the "
                               "polygons; lower <b>Opacity</b> to keep the imagery underneath visible.")
        self.fill_hint.hide()
        bl.addWidget(self.fill_hint)
        # class chooser: one compact row however many classes the layer has
        # (sits under the Layer row: Layer -> Class -> what to recolour)
        self.items_box = QWidget()
        il = QHBoxLayout(self.items_box)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(6)
        lab = QLabel("Class")
        lab.setObjectName("SBPickerSection")
        self.class_combo = QComboBox()
        self.class_combo.setToolTip("Recolor one class – or All classes at once.\n"
                                    "Selecting a category in the Layers panel picks it here too.")
        self.class_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.class_combo.setMinimumContentsLength(10)
        self.class_combo.setMaxVisibleItems(16)
        self.class_combo.setIconSize(QSize(18, 18))
        self.class_combo.activated.connect(self._class_chosen)
        il.addWidget(lab)
        il.addWidget(self.class_combo, 1)
        bl.insertWidget(bl.indexOf(self.target_box), self.items_box)
        if self.layer_row is not None:                 # line the labels up
            w = max(lab.sizeHint().width(), self.layer_row.layout().itemAt(0).widget().sizeHint().width())
            lab.setFixedWidth(w)
            self.layer_row.layout().itemAt(0).widget().setFixedWidth(w)
        self.ramp_box = QWidget()
        rl = QVBoxLayout(self.ramp_box)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(3)
        self.stops = StopStrip()
        self.stops.setToolTip("Click a stop to edit its color")
        self.stops.stopClicked.connect(self._stop_clicked)
        rl.addWidget(self.stops)
        rrow = QHBoxLayout()
        rrow.setSpacing(4)
        self.ramp_combo = QComboBox()
        self.ramp_combo.addItem("Color ramp preset…", None)
        for name in QgsStyle.defaultStyle().colorRampNames():
            self.ramp_combo.addItem(name, name)
        self.ramp_combo.activated.connect(self._ramp_preset)
        self.reverse_btn = QPushButton("Reverse")
        self.reverse_btn.clicked.connect(self._reverse_ramp)
        rrow.addWidget(self.ramp_combo, 1)
        rrow.addWidget(self.reverse_btn)
        rl.addLayout(rrow)
        bl.addWidget(self.ramp_box)
        self.hint = QLabel()
        self.hint.setObjectName("SBPickerHint")
        self.hint.setWordWrap(True)
        bl.addWidget(self.hint)

        # --- wheel --------------------------------------------------------------
        wrow = QHBoxLayout()
        wrow.addStretch(1)
        self.wheel = ColorWheel()
        self.wheel.colorChanged.connect(lambda c, final: self._on_color(c, "wheel", final))
        wrow.addWidget(self.wheel)
        wrow.addStretch(1)
        bl.addLayout(wrow)

        # --- exact values ---------------------------------------------------------
        seg = QHBoxLayout()
        seg.setSpacing(2)
        self.input_group = QButtonGroup(self)
        self.input_stack = QStackedWidget()
        self.input_stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.input_bar = ChipBar()           # same gliding pill as the bag's categories
        ib = QHBoxLayout(self.input_bar)
        ib.setContentsMargins(3, 3, 3, 3)
        ib.setSpacing(2)
        for i, key in enumerate(("hex", "rgb", "hsv")):
            b = QToolButton()
            b.setObjectName("SBChip")
            b.setCheckable(True)
            b.setText(key.upper())
            b.clicked.connect(lambda _c=False, k=i: self._set_input_tab(k))
            self.input_group.addButton(b, i)
            self.input_bar.add(b)
            ib.addWidget(b)
        seg.addWidget(self.input_bar)
        seg.addStretch(1)
        self.eyedrop_btn = self._btn("eyedropper", "Eyedropper: pick a color anywhere on the screen",
                                     self.start_eyedropper, obj="SBTool")
        self.fav_btn = self._btn("star", "Add this color to Favorites", self._toggle_fav, obj="SBTool")
        self.copy_btn = self._btn("copy", "Copy HEX", self._copy_hex, obj="SBTool")
        for b in (self.eyedrop_btn, self.fav_btn, self.copy_btn):
            seg.addWidget(b)
        bl.addLayout(seg)
        # hex
        hexw = QWidget()
        hx = QHBoxLayout(hexw)
        hx.setContentsMargins(0, 0, 0, 0)
        self.hex_edit = QLineEdit()
        self.hex_edit.setObjectName("SBSearch")
        self.hex_edit.setPlaceholderText("#RRGGBB or #AARRGGBB")
        self.hex_edit.editingFinished.connect(self._hex_entered)
        hx.addWidget(QLabel("HEX"))
        hx.addWidget(self.hex_edit, 1)
        self.input_stack.addWidget(hexw)
        # rgb / hsv
        self.rgb = [self._spin(0, 255) for _ in range(3)]
        self.hsv = [self._spin(0, 359), self._spin(0, 100), self._spin(0, 100)]
        for spins, labels, slot in ((self.rgb, "RGB", self._rgb_entered), (self.hsv, "HSV", self._hsv_entered)):
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(4)
            for lbl, sp in zip(labels, spins):
                lay.addWidget(QLabel(lbl))
                lay.addWidget(sp, 1)
                sp.valueChanged.connect(lambda _v, f=slot: f(False))
                sp.editingFinished.connect(lambda f=slot: f(True))
            self.input_stack.addWidget(w)
        bl.addWidget(self.input_stack)
        orow = QHBoxLayout()
        orow.addWidget(QLabel("Opacity"))
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(0, 100)
        self.alpha_slider.valueChanged.connect(lambda v: self._alpha_changed(v, False))
        self.alpha_slider.sliderReleased.connect(lambda: self._alpha_changed(self.alpha_slider.value(), True))
        self.alpha_spin = self._spin(0, 100)
        self.alpha_spin.setSuffix(" %")
        self.alpha_spin.editingFinished.connect(lambda: self._alpha_changed(self.alpha_spin.value(), True))
        orow.addWidget(self.alpha_slider, 1)
        orow.addWidget(self.alpha_spin)
        bl.addLayout(orow)

        # --- swatches: one compact strip (palette, Recent or Favorites) ----------
        srow = QHBoxLayout()
        srow.setSpacing(4)
        self.swatch_src_btn = QToolButton()
        self.swatch_src_btn.setObjectName("SBSwatchSource")
        self.swatch_src_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.swatch_src_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.swatch_src_btn.setToolTip("Which colors to show: a palette, Recent or Favorites")
        self.swatch_src_menu = QMenu(self)
        self.swatch_src_menu.aboutToShow.connect(self._fill_source_menu)
        self.swatch_src_btn.setMenu(self.swatch_src_menu)
        self.palette_btn = self._btn("menu", "Palette actions", obj="SBTool")
        self.palette_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.palette_menu = QMenu(self)
        self.palette_menu.aboutToShow.connect(self._fill_palette_menu)
        self.palette_btn.setMenu(self.palette_menu)
        srow.addWidget(self.swatch_src_btn)
        srow.addStretch(1)
        srow.addWidget(self.palette_btn)
        bl.addLayout(srow)
        self.palette_combo = QComboBox()          # holds the current palette (not shown)
        self.palette_combo.currentIndexChanged.connect(self._palette_changed)
        self.palette_combo.hide()
        self.palette_grid = SwatchGrid(size=18, max_rows=2)
        self.palette_grid.picked.connect(lambda c: self._on_color(c, "swatch", True))
        self.palette_grid.menuRequested.connect(lambda c, gp: self._swatch_menu(c, gp, in_palette=True))
        self.recent_grid = SwatchGrid(size=18, max_rows=2)
        self.recent_grid.empty_text = "Colors you use appear here"
        self.recent_grid.picked.connect(lambda c: self._on_color(c, "swatch", True))
        self.recent_grid.menuRequested.connect(lambda c, gp: self._swatch_menu(c, gp))
        self.fav_grid = SwatchGrid(size=18, max_rows=2)
        self.fav_grid.empty_text = "Click ☆ to keep a color here"
        self.fav_grid.picked.connect(lambda c: self._on_color(c, "swatch", True))
        self.fav_grid.menuRequested.connect(lambda c, gp: self._swatch_menu(c, gp))
        for g in (self.palette_grid, self.recent_grid, self.fav_grid):
            bl.addWidget(g)

        # --- thickness wheel -------------------------------------------------------
        self.width_box = QWidget()
        wl = QHBoxLayout(self.width_box)
        wl.setContentsMargins(0, 4, 0, 0)
        wl.setSpacing(10)
        self.width_dial = WidthDial(diameter=104)
        self.width_dial.valueChanged.connect(lambda v, final: self._on_width(v, final, source="dial"))
        wl.addWidget(self.width_dial)
        wcol = QVBoxLayout()
        wcol.setSpacing(5)
        # point layers: one dial, two jobs
        self.dim_switch = QWidget()
        dsl = QHBoxLayout(self.dim_switch)
        dsl.setContentsMargins(0, 0, 0, 0)
        dsl.setSpacing(3)
        self.size_chip = QToolButton()
        self.size_chip.setText("Size")
        self.size_chip.setToolTip("Point size")
        self.outline_chip = QToolButton()
        self.outline_chip.setText("Outline")
        self.outline_chip.setToolTip("Outline (stroke) width of the point")
        for b, mode in ((self.size_chip, "size"), (self.outline_chip, "width")):
            b.setObjectName("SBPartChip")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, m=mode: self.set_dim_mode(m))
            dsl.addWidget(b)
        dsl.addStretch(1)
        self.dim_switch.hide()
        wcol.addWidget(self.dim_switch)
        self.width_label = QLabel("Line width")
        self.width_label.setObjectName("SBPickerSection")
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setDecimals(2)
        self.width_spin.setRange(0.0, 999.0)
        self.width_spin.setSingleStep(0.05)
        self.width_spin.setKeyboardTracking(False)
        self.width_spin.setToolTip("Exact thickness (0 = hairline)")
        self.width_spin.valueChanged.connect(lambda v: self._on_width(v, True, source="spin"))
        wcol.addWidget(self.width_label)
        wcol.addWidget(self.width_spin)
        self.presets_box = QWidget()
        prs = FlowLayout(self.presets_box, spacing=3)
        self.width_presets = []
        for _i in range(4):
            b = QToolButton()
            b.setObjectName("SBWidthPreset")
            b.clicked.connect(lambda _c=False, btn=b: self._on_width(btn.property("w"), True))
            self.width_presets.append(b)
            prs.addWidget(b)
        wcol.addWidget(self.presets_box)
        self.width_hint = QLabel()
        self.width_hint.setObjectName("SBPickerHint")
        self.width_hint.setWordWrap(True)
        wcol.addWidget(self.width_hint)
        wcol.addStretch(1)
        wl.addLayout(wcol, 1)
        bl.addWidget(self.width_box)

        # --- actions -------------------------------------------------------------------
        self.actions_box = QWidget()
        al = QVBoxLayout(self.actions_box)
        al.setContentsMargins(0, 4, 0, 0)
        al.setSpacing(4)
        r1 = QHBoxLayout()
        r1.setSpacing(2)
        self.undo_btn = self._btn("undo", "Undo color change", self.undo, obj="SBTool")
        self.redo_btn = self._btn("redo", "Redo color change", self.redo, obj="SBTool")
        self.reset_btn = self._btn("reset", "Reset to the original colors", self.reset, obj="SBTool")
        self.state_label = QLabel()
        self.state_label.setObjectName("SBPickerHint")
        for b in (self.undo_btn, self.redo_btn, self.reset_btn):
            r1.addWidget(b)
        r1.addWidget(self.state_label, 1)
        al.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(4)
        self.save_new_btn = QPushButton("Save as New")
        self.save_new_btn.setToolTip("Keep the original and add the recolored version to the bag")
        self.save_new_btn.clicked.connect(self.save_as_new)
        self.replace_btn = QPushButton("Replace")
        self.replace_btn.setToolTip("Overwrite the symbol in the bag with the recolored version")
        self.replace_btn.clicked.connect(self.replace_existing)
        self.done_btn = QPushButton("Done")
        self.done_btn.setObjectName("SBPrimary")
        self.done_btn.setToolTip("Keep the new colors on the layer and minimize the tool to its bubble\n"
                                 "(✕ closes it; Ctrl+Z in Symbol Bag still restores the previous style)")
        self.done_btn.clicked.connect(self.done)
        if self.role == "symbol":
            self.save_new_btn.setObjectName("SBPrimary")
        for b in (self.done_btn, self.save_new_btn, self.replace_btn):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            r2.addWidget(b)
        al.addLayout(r2)
        bl.addWidget(self.actions_box)
        bl.addStretch(1)
        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(14, 14)
        self.grip.setToolTip("Drag to resize the color wheel")
        grip_row.addWidget(self.grip)
        bl.addLayout(grip_row)
        self.input_group.button({"hex": 0, "rgb": 1, "hsv": 2}.get(self.state.get("input"), 0)).setChecked(True)
        self._set_input_tab(self.input_group.checkedId(), save=False)

    def _spin(self, lo, hi):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setKeyboardTracking(False)
        s.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return s

    # ==================================================================
    # theme & painting
    # ==================================================================
    def own_theme(self, panel_theme):
        """Layer Colors has its own colour (set in its gear menu); until chosen it matches the bag."""
        key = self.state.get("style") if self.role == "layer" else None
        if key not in BAG_STYLES:
            return panel_theme
        from .theme import Theme
        return Theme(key, panel_theme.background, panel_theme.compact)

    def choose_style(self):
        """The colour of Layer Colors only; the Symbol Bag's own Bag Style dialog is in the bag."""
        from .dialogs import StyleOnlyDialog
        dlg = StyleOnlyDialog(self.style_key(), self.set_style_key, self)
        dlg.exec()

    def style_key(self):
        return self.theme.name

    def set_style_key(self, key):
        self.state["style"] = key
        self.store.set_setting(self._skey, self.state)
        self.apply_theme(self.own_theme(self.panel.theme))

    def apply_theme(self, theme):
        self.theme = theme
        t = theme
        a = t.accent.name()
        extra = """
QLabel#SBPickerName {{ font-weight: 700; color: {text}; }}
QLabel#SBPickerHint {{ color: {sub}; font-size: 8pt; }}
QLabel#SBPickerSection {{ color: {sub}; font-size: 8pt; font-weight: 600; }}
QPushButton {{ border: 1px solid rgba(0,0,0,40); border-radius: 12px; padding: 4px 8px;
              background: {card}; color: {text}; }}
QPushButton:hover {{ border-color: {a}; }}
QPushButton:disabled {{ color: {sub}; }}
QPushButton#SBPrimary {{ background: {a}; color: white; border: none; font-weight: 600; }}
QPushButton#SBPrimary:disabled {{ background: rgba(120,120,120,90); }}
QToolButton#SBSwatchSource {{ border: none; background: transparent; color: {sub}; font-size: 8pt;
                              font-weight: 600; padding: 1px 2px; }}
QToolButton#SBSwatchSource:hover {{ color: {a}; }}
QToolButton#SBSwatchSource::menu-indicator {{ image: none; width: 0px; }}
QToolButton#SBPartChip {{ border: 1px solid rgba(0,0,0,45); border-radius: 6px; padding: 3px 6px;
                          background: {card}; color: {text}; font-size: 8.5pt; }}
QToolButton#SBPartChip:hover {{ border-color: {a}; }}
QToolButton#SBPartChip:checked {{ background: {a}; color: white; border-color: {a}; font-weight: 600; }}
QToolButton#SBPartChip::menu-indicator {{ image: none; width: 0px; }}
QToolButton#SBWidthPreset {{ border: 1px solid rgba(0,0,0,40); border-radius: 9px; padding: 1px 6px;
                             background: {card}; color: {text}; font-size: 8pt; }}
QToolButton#SBWidthPreset:hover {{ border-color: {a}; }}
QToolButton#SBWidthPreset:disabled {{ color: {sub}; }}
""".format(a=a, **t.p)
        self.setStyleSheet(t.stylesheet() + extra)
        self.logo.setPixmap(t.icon("palette", t.p["htext"], 18).pixmap(18, 18))
        self.title.setStyleSheet("color: {};".format(t.p["htext"]))
        self._update_sound_btn(icon=False)
        for b in [x for x in (self.sound_btn, self.gear_btn, self.size_btn, self.min_btn, self.close_btn) if x is not None]:
            b.setIcon(t.icon(b.property("sb_icon"), t.accent if not t.dark else t.p["htext"], 16))
        for b in (self.eyedrop_btn, self.copy_btn, self.undo_btn, self.redo_btn, self.reset_btn, self.palette_btn):
            b.setIcon(t.icon(b.property("sb_icon"), t.p["sub"], 16))
        self.wheel.theme = t
        self.width_dial.theme = t
        self.width_dial.update()
        self.input_bar.theme = t
        self.input_bar.update()
        if self.bubble is not None:
            self.bubble.theme = t
            self.bubble.set_size(self.panel.settings.get("bubble_size", 44))
            self.bubble.update()
        self._update_fav_btn()
        self.set_motion(self.panel.motion_on(), self.panel._speed())
        self.update()

    def set_motion(self, enabled, speed):
        self.wheel.motion_enabled = enabled
        self.wheel.speed = speed
        self.width_dial.motion_enabled = enabled
        self.width_dial.speed = speed
        self.input_bar.motion_enabled = enabled
        self.input_bar.speed = speed
        if self.bubble is not None:
            self.bubble.motion_enabled = enabled
            self.bubble.speed = speed

    def paintEvent(self, e):
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = 16
        border = QColor(t.p["h2"])
        border.setAlpha(160)
        p.setPen(QPen(border, 1))
        bg = t.body_color()
        bg.setAlphaF(max(bg.alphaF(), 0.97))
        p.setBrush(bg)
        p.drawRoundedRect(body, radius, radius)
        head = QRectF(body.left(), body.top(), body.width(), self.header.geometry().bottom() - body.top() + 1)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRoundedRect(head, radius, radius)
        path.addRect(QRectF(head.left(), head.top() + radius, head.width(), head.height() - radius))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(header_gradient(head, t.p))
        p.drawPath(path.simplified())
        paint_zipper(p, QRectF(self.zipper.geometry()).adjusted(1, 0, -1, 0), t.p, t.dark)
        p.end()

    # ==================================================================
    # size: Small / Medium / Large / custom (drag the corner)
    # ==================================================================
    def _refresh_layer_scope(self):
        """The Layer list shows the layers of the map that is open (the ones drawn on the map canvas),
        not every layer of the project; the layer being recolored always stays listed."""
        iface = self.panel.iface
        if self.layer_combo is None or iface is None:
            return
        shown = {lyr.id() for lyr in iface.mapCanvas().layers()}
        if self.layer_id:
            shown.add(self.layer_id)
        self.layer_combo.setExceptedLayerList(
            [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.id() not in shown])

    def _fill_gear_menu(self):
        from ...bag_common import fill_common_menu
        m = self.gear_menu
        m.clear()
        m.addAction("Bag Style...", lambda: QTimer.singleShot(0, self.choose_style))
        self._add_size_items(m.addMenu("Wheel Size"))
        fill_common_menu(m, self.panel.group, self.style_key, self.set_style_key, key="colors")

    def _fill_size_menu(self):
        m = self.size_menu
        m.clear()
        self._add_size_items(m)
        m.addSeparator()
        self.panel.fill_sound_menu(m.addMenu("Sound Effects"))

    def _add_size_items(self, m):
        for key, label, px in SIZES:
            a = m.addAction("{}  ({} px)".format(label, px))
            a.setCheckable(True)
            a.setChecked(self.state["size"] == key)
            a.triggered.connect(lambda _c=False, k=key: self.set_size_mode(k))
        a = m.addAction("Custom – drag the bottom-right corner")
        a.setCheckable(True)
        a.setChecked(self.state["size"] == "custom")
        a.setEnabled(False)

    def _diameter(self):
        if self.state["size"] == "custom":
            return int(self.state.get("custom", 220))
        return SIZE_PX.get(self.state["size"], 200)

    def _layout_for(self, d):
        self.wheel.set_diameter(d)
        self._fit(max(d + 2 * PAD + 4, 286))

    def _fit(self, width=None):
        """Size the window exactly to its content for `width`.

        Swatch rows and the wrapped hint depend on the width, so their heights
        are computed directly – Qt does not deliver resize events to a hidden
        window, which would otherwise leave stale heights (gaps or clipping)."""
        self._programmatic += 1
        try:
            w = int(width or self.width())
            content_w = w - 2 - 20
            d = int(max(120, min(self._diameter(), content_w - 4)))
            if d != self.wheel.width():
                self.wheel.set_diameter(d)
            if hasattr(self, "presets_box"):
                pw = max(60, content_w - self.width_dial.width() - 12)
                ph = self.presets_box.layout().heightForWidth(pw)
                if ph > 0:
                    self.presets_box.setFixedHeight(ph)
            for grid in (self.palette_grid, self.recent_grid, self.fav_grid):
                grid.fit_width(content_w)
            for lbl in (self.dense_hint, self.fill_hint):
                lh = lbl.heightForWidth(content_w) if lbl.isVisibleTo(self) else -1
                if lh > 0:
                    lbl.setFixedHeight(lh)
                else:
                    lbl.setMinimumHeight(0)
                    lbl.setMaximumHeight(16777215)
            self._fit_part_bar(content_w)
            hint_h = self.hint.heightForWidth(content_w) if self.hint.isVisibleTo(self) else -1
            if hint_h > 0:
                self.hint.setFixedHeight(hint_h)
            else:
                self.hint.setMinimumHeight(0)
                self.hint.setMaximumHeight(16777215)
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            # Nested layouts cache size hints and only refresh them when Qt
            # processes its posted LayoutRequest events – which it postpones while
            # the window is hidden (during a resize animation). Flush and measure
            # until stable, so a shrinking wheel never leaves the old height.
            self.wheel.updateGeometry()
            h = -1
            for _ in range(3):
                for sub in self.findChildren(QLayout):
                    sub.invalidate()
                # a parent layout caches each container's size; while hidden Qt
                # never posts the event that refreshes it, so do it explicitly
                for child in self.findChildren(QWidget):
                    if child.layout() is not None:
                        child.updateGeometry()
                self.layout().invalidate()
                QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
                self.layout().activate()
                nh = self.sizeHint().height()
                if nh == h:
                    break
                h = nh
            # Height always equals the content (it never stretches or clips);
            # only the width is user-resizable, and the wheel follows it.
            self.setFixedHeight(h)
            self.setMinimumWidth(min(w, self.minimumSizeHint().width()))
            self.setMaximumWidth(16777215)
            self.resize(w, h)
        finally:
            self._programmatic -= 1

    def set_size_mode(self, mode, animate=True):
        self.state["size"] = mode
        self.save_state()
        if self.embedded:
            d = int(max(120, min(self._diameter(), self.width() - 2 * PAD - 4)))
            self.wheel.set_diameter(d)
            self._fit(self.width())
            return
        top = self.geometry().top()
        cx = self.geometry().center().x()
        self._layout_for(self._diameter())
        if self.isVisible():                 # grow / shrink around the same top-centre
            self.move(int(cx - self.width() / 2.0), top)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._elide_name)
        if self._programmatic or not self.isVisible():
            return
        if self.embedded:
            # the host layout sets the width: the wheel fits it, up to the chosen size
            d = int(max(120, min(self._diameter(), self.width() - 2 * PAD - 4)))
            if d != self.wheel.width() or self.height() != self.sizeHint().height():
                self.wheel.set_diameter(d)
                QTimer.singleShot(0, lambda: self._alive() and self._fit(self.width()))
            return
        # user dragged the corner: the wheel follows the width, keeping its circle
        d = int(max(120, min(420, self.width() - 2 * PAD - 4)))
        if self.state["size"] != "custom" or d != self.state.get("custom"):
            self.state["size"] = "custom"
            self.state["custom"] = d
            self.wheel.set_diameter(d)
            QTimer.singleShot(0, self._fit_height)

    def _fit_height(self):
        self._fit(self.width())
        self.save_state()

    # ==================================================================
    # open / close (expands from the button that opened it)
    # ==================================================================
    # The tool appears and disappears by fading the real window in place.
    # No moving snapshot is swapped with the window, so there is nothing to
    # line up (no double image / 1-px shake with Windows display scaling) and
    # never a frame without the window (no blink).
    def _stop_motion(self):
        if self._tween is not None and self._tween.running:
            self._tween.stop(finish=True)

    def _fade(self, show, finished=None, duration=170):
        self._stop_motion()
        from .motion import Tween

        def end():
            if not show:
                self.hide()
            self.setWindowOpacity(1.0)
            if finished:
                finished()
        if not self.panel.motion_on():
            if show:
                self._show_real()
            end()
            self._tween = None
            return
        if show:
            self.setWindowOpacity(0.0)
            self._show_real()

            def frame(t):
                self.setWindowOpacity(ease_out(t, 2))
        else:
            def frame(t):
                self.setWindowOpacity(1.0 - ease_in(t, 2))
        self._tween = Tween(self, duration * self.panel._speed(), frame, end).start()

    def _show_real(self):
        if self.bubble is not None and not self.in_bubble and self.bubble.isVisible():
            self.bubble.shrink_out(self.bubble.hide)
        self.show()
        self.raise_()

    def _initial_geometry(self):
        geo = self.state.get("geo")
        if isinstance(geo, list) and len(geo) == 2:
            pt = QPoint(geo[0], geo[1])
            if QGuiApplication.screenAt(pt) is not None:
                return pt
        if self.role == "layer":
            # standalone: top-left of the map canvas (the bag may be closed)
            iface = self.panel.iface
            if iface is not None:
                canvas = iface.mapCanvas()
                tl = canvas.mapToGlobal(QPoint(0, 0))
                return QPoint(tl.x() + 24, tl.y() + 24)
        bag = self.panel.geometry()
        x = bag.left() - self.width() - 10
        screen = QGuiApplication.screenAt(bag.center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        if x < avail.left():
            x = bag.right() + 10
        y = max(avail.top(), min(bag.top(), avail.bottom() - self.height()))
        return QPoint(x, y)

    def open_from(self, origin, uid=None):
        """Open (or bring forward) the picker, growing out of `origin` (global point).
        Symbol role: recolour bag symbol `uid`; layer role: the Layers panel selection."""
        self._origin = QPointF(origin) if origin is not None else None
        if self.role == "layer":
            self._refresh_layer_scope()
            self.follow_layer_selection()
            if self.in_bubble:
                self.restore_from_bubble()
                return
        else:
            self.retarget(uid)
        self._stop_motion()
        if self.isVisible():
            self.raise_()
            return
        self._layout_for(self._diameter())
        self.move(self._initial_geometry())
        self.layout().activate()
        slot = self._slot_rect()
        if slot is not None:                 # open from the toolbar: out of where its bubble sits
            self.move(self.panel.group.anchor_topleft(slot.center(), slot.width() / 2.0,
                                                      self.width(), self.height(), "colors"))
            if self.panel.motion_on() and self.panel._genie_style():
                self._genie(False, target=slot)
                return
        self._fade(True, duration=240)

    # --- focus only when typing ------------------------------------------------------
    def eventFilter(self, obj, e):
        if self._no_focus and e.type() == QEvent.Type.MouseButtonPress:
            self._allow_focus()
        return False

    def _allow_focus(self):
        wh = self.windowHandle()
        if wh is not None:
            wh.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self._no_focus = False
        self.activateWindow()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._hold_canvas(False)
        if not self._no_focus:
            wh = self.windowHandle()
            if wh is not None:
                wh.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
            self._no_focus = True

    def enterEvent(self, e):
        super().enterEvent(e)
        # catch a Layers-panel selection made while the tool waited
        if self.role == "layer" and not self.active and self.isVisible():
            self.follow_layer_selection()

    def is_open(self):
        """Visible on screen or waiting in its bubble."""
        return self.isVisible() or self.in_bubble

    def close_animated(self):
        if self.in_bubble:
            self.in_bubble = False
            self.bubble.shrink_out(self.bubble.hide)
            self._end_session()
            self._set_target_ui()
            return
        if not self.isVisible():
            self._end_session()
            return
        self.save_state()
        slot = self._slot_rect()
        if slot is not None and self.panel.motion_on() and self.panel._genie_style():
            self._genie(True, self._end_session, target=slot)      # pours into where its bubble sits
            return
        self._fade(False, self._end_session, duration=200)

    def save_state(self):
        if self.isVisible():
            self.state["geo"] = [self.x(), self.y()]
        self.store.set_setting(self._skey, self.state)

    # ==================================================================
    # layer role: minimize to a bubble on the map (tossed along an arc,
    # like the bag) and spring back out
    # ==================================================================
    def _ensure_bubble(self):
        if self.bubble is None:
            from .panel import BubbleWidget
            b = BubbleWidget(self.panel.parentWidget())
            b.mark = "wheel"
            b.pop_damping = 0.8
            b.setToolTip("Layer Colors – click to open, drag to move")
            b.theme = self.theme
            b.set_size(self.panel.settings.get("bubble_size", 44))
            b.motion_enabled = self.panel.motion_on()
            b.speed = self.panel._speed()
            if getattr(self.panel, "group", None) is not None:
                b.attach_group(self.panel.group, "colors")
                self.panel.group.register_style("colors", self.style_key, self.set_style_key)
            b.clicked.connect(self._bubble_clicked)
            b.moved.connect(self._bubble_moved)
            self.bubble = b
        return self.bubble

    def _bubble_clicked(self):
        if self.in_bubble:
            self.restore_from_bubble()
        else:                      # stale bubble: never leave a dead button on the map
            self.bubble.shrink_out(self.bubble.hide)
            self.open_from(self.bubble.circle_center_global())

    def _bubble_target(self):
        """Where the bubble sits: where the user dragged it, else like a Dock –
        on the bottom edge of the map, under the window (so the window pours
        straight down into it, the classic Genie)."""
        group = getattr(self.panel, "group", None)
        if group is not None:               # its place in the column of bubbles
            self.bubble.group_set_size(group.size_of("colors"))
            c = group.target_for("colors")
            off = self.bubble.circle_rect().center()
            return QPoint(int(round(c.x() - off.x())), int(round(c.y() - off.y())))
        xy = self.state.get("dock_xy")
        if isinstance(xy, list) and len(xy) == 2 and QGuiApplication.screenAt(QPoint(xy[0], xy[1])) is not None:
            return QPoint(int(xy[0]), int(xy[1]))
        size = self.bubble.size()
        iface = self.panel.iface
        if iface is not None:
            canvas = iface.mapCanvas()
            area = QRect(canvas.mapToGlobal(QPoint(0, 0)), canvas.size())
        else:
            area = (QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()).availableGeometry()
        geo = self.geometry() if self.isVisible() else QRect(self._initial_geometry(), self.size())
        x = int(geo.center().x() - size.width() / 2.0)
        x = max(area.left() + 8, min(area.right() - size.width() - 8, x))
        y = area.bottom() - size.height() - 6
        if y < geo.bottom() + 20:          # map too short: sit to the side instead
            y = max(area.top() + 8, geo.top() + 40)
            x = min(area.right() - size.width() - 8, geo.right() + 24)
        return QPoint(x, y)

    def _bubble_moved(self, pos):
        if getattr(self.panel, "group", None) is not None:
            return
        self.state["dock_xy"] = [pos.x(), pos.y()]
        self.store.set_setting(self._skey, self.state)

    # --- Genie: pour into the bubble / flow back out (macOS style) ---------------
    def _bubble_rect(self):
        b = self.bubble
        r = b.circle_rect()
        return QRectF(QPointF(b.mapToGlobal(r.topLeft().toPoint())), r.size())

    def _update_sound_btn(self, icon=True):
        if self.sound_btn is None:
            return
        on = bool(self.panel.settings.get("sound", True))
        self.sound_btn.setProperty("sb_icon", "speaker" if on else "speaker_off")
        from .sound import STYLES
        style = dict((k, l) for k, l, _d in STYLES).get(self.panel.settings.get("sound_style", "whoosh"), "Whoosh")
        self.sound_btn.setToolTip("Sound: {} ({}, volume {} %)\nClick: turn sound {}\n"
                                  "Right-click: volume and sound style".format(
                                      "on" if on else "off", style, self.panel.settings.get("sound_volume", 70),
                                      "off" if on else "on"))
        if icon and self.theme is not None:
            t = self.theme
            self.sound_btn.setIcon(t.icon(self.sound_btn.property("sb_icon"), t.accent if not t.dark else t.p["htext"], 16))

    def _sound(self, event):
        s = self.panel.settings
        if s.get("sound", True):
            from . import sound
            sound.play_event(event, s.get("sound_style", "whoosh"), s.get("sound_volume", 70) / 100.0)

    def set_sound(self, on):
        self.panel.set_sound(on)

    def _toggle_sound(self):
        on = not self.panel.settings.get("sound", True)
        self.panel.set_sound(on)
        if on:                                  # a short confirmation that it is back on
            from . import sound
            s = self.panel.settings
            sound.play_event("out", s.get("sound_style", "whoosh"), s.get("sound_volume", 70) / 100.0)

    def _sound_style_menu(self, pos):
        m = self.panel.fill_sound_menu(QMenu(self))
        m.exec(self.sound_btn.mapToGlobal(pos))

    def _slot_rect(self):
        """The circle where this tool's bubble sits (or would sit) in the bubble column, or None."""
        group = getattr(self.panel, "group", None)
        if group is None or self.role != "layer":
            return None
        c = group.targets(extra="colors").get("colors")
        if c is None:
            return None
        d = float(group.size_of("colors"))
        return QRectF(c.x() - d / 2.0, c.y() - d / 2.0, d, d)

    def _genie(self, minimize, finished=None, target=None):
        from .motion import Tween
        from .widgets import GenieWidget
        self._stop_motion()
        if getattr(self, "_genie_w", None) is None:
            self._genie_w = GenieWidget(self.panel.parentWidget())
        g = self._genie_w
        self.setWindowOpacity(1.0)
        g.setup(self.grab(), self.geometry(), target if target is not None else self._bubble_rect())
        self._sound("in" if minimize else "out")
        if minimize:
            g.set_t(0.0)
            g.show_now()            # exactly over the window …
            self.hide()             # … so hiding it now changes nothing on screen

            def frame(t):
                g.set_t(t)

            def end():
                g.hide()
                g.pixmap = None
                if finished:
                    finished()
            duration = 520
        else:
            g.set_t(1.0)
            g.show_now()

            def frame(t):
                g.set_t(1.0 - t)

            def end():
                self._show_real()   # the window appears exactly under the last frame …
                g.hide_soon()       # … then the overlay leaves
                if finished:
                    finished()
            duration = 460
        self._tween = Tween(self, duration * self.panel._speed(), frame, end).start()

    def minimize_to_bubble(self):
        if self.role != "layer" or self.in_bubble or not self.isVisible():
            return
        if not self.panel.settings.get("bubble", True):      # bubble turned off: just close it
            self.close_animated()
            return
        self._commit()
        self.save_state()
        bubble = self._ensure_bubble()
        self.in_bubble = True
        bubble.move(self._bubble_target())
        bubble.scale = 1.0
        bubble.alpha = 1.0
        bubble._closing = False
        bubble.show()               # the target is there before the window pours in
        if not self.panel.motion_on():
            self.hide()
            return
        bubble.scale = 0.0
        bubble.pop_in()
        if self.panel._genie_style():
            self._genie(True)
        else:
            self._fade(False)

    def _anchor_to_bubble(self):
        """Reveal the window at its bubble's spot: a bubble in the right column opens a window whose
        right edge is there, a bottom bubble one whose bottom edge is there, and so on (kept on the map)."""
        group = getattr(self.panel, "group", None)
        if group is None or self.bubble is None:
            return
        c = self.bubble.circle_center_global()
        self.move(group.anchor_topleft(c, self.bubble.size_px / 2.0, self.width(), self.height(), "colors"))

    def restore_from_bubble(self):
        if not self.in_bubble:
            return
        self._stop_motion()
        self.in_bubble = False
        self._refresh_layer_scope()
        self.follow_layer_selection()
        self._layout_for(self._diameter())
        self.move(self._initial_geometry())
        self.layout().activate()
        self._anchor_to_bubble()
        if not self.panel.motion_on():
            self.bubble.hide()
            self._show_real()
            return
        if self.panel._genie_style():
            self._genie(False, lambda: self.bubble.shrink_out(self.bubble.hide))
        else:
            self._fade(True)

    # ==================================================================
    # recolour session – two sources:
    #   "symbol": a symbol in the bag   (preview on its card, saved on request)
    #   "layer":  a layer / its selected classes in the Layers panel
    #             (changes show live on the map)
    # A definition is a dict {item key: xml}; a target is (item key, what).
    # ==================================================================
    @property
    def active(self):
        return self.working is not None

    @property
    def dirty(self):
        return self.working is not None and self.working != self.saved

    def _layer(self):
        from qgis.core import QgsProject
        return QgsProject.instance().mapLayer(self.layer_id) if self.layer_id else None

    # --- starting a session -------------------------------------------------------
    def retarget(self, uid):
        """Symbol mode: recolour bag symbol `uid`."""
        if self.role != "symbol":
            return
        if uid is not None and uid == self.uid and self.active:
            return
        self._end_session()
        rec = self.store.record(uid) if uid else None
        if rec is not None:
            self.uid = uid
            self.kind = rec.kind
            xml = self.store.symbol_xml(uid)
            self.items = [("_", rec.name)]
            self.title_text = rec.name
            self._begin({"_": xml})
        self._set_target_ui()

    def _fallback_layer(self):
        """Nothing selected in the Layers panel: the active layer, else the
        top visible layer that has colours – the tool always has a target."""
        iface = self.panel.iface
        if iface is None:
            return None
        from ..core.symbols import layer_kind
        layer = iface.activeLayer()
        if layer is not None and layer_kind(layer):
            return layer
        from qgis.core import QgsProject
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            lyr = node.layer()
            if lyr is not None and node.isVisible() and layer_kind(lyr):
                return lyr
        for node in root.findLayers():
            lyr = node.layer()
            if lyr is not None and layer_kind(lyr):
                return lyr
        return None

    def follow_layer_selection(self, layer=None, keys=None):
        """Layer role: recolour exactly what is selected in the Layers panel –
        selected classes only, or the whole layer. `layer` forces a layer
        (the Layer box in the tool)."""
        if self.role != "layer" or getattr(self, "_dead", False):
            return
        if layer is None:
            if self.active and self._nothing_selected():
                return                 # selection cleared (e.g. legend rebuilt): keep working
            classes, layers = self.panel.selection_targets() if self.panel.iface is not None else ([], [])
            if classes:
                layer, keys, _labels = classes[0]
            elif layers:
                layer, keys = layers[0], None
            else:
                layer, keys = self._fallback_layer(), None
        self._sync_layer_combo(layer)
        if layer is not None and layer.id() == self.layer_id and keys == self.layer_keys and self.active:
            return
        self._end_session()
        from ..core.symbols import layer_kind
        kind = layer_kind(layer) if layer is not None else None
        if layer is not None and kind is not None:
            items = rc.layer_items(layer, keys)
            if items:
                self.layer_id = layer.id()
                self.layer_keys = keys
                self.kind = kind
                self.items = [(k, lbl) for k, lbl, _x in items]
                if keys:
                    self.title_text = "{}  ·  {}".format(", ".join(lbl for _k, lbl in self.items), layer.name())
                else:
                    self.title_text = layer.name()
                self._begin({k: x for k, _l, x in items})
        self._set_target_ui()

    def _nothing_selected(self):
        iface = self.panel.iface
        if iface is None:
            return True
        try:
            view = iface.layerTreeView()
            return not view.selectedLayers() and not self.panel._selected_legend_nodes()
        except RuntimeError:
            return True

    def _sync_layer_combo(self, layer):
        if self.layer_combo is None:
            return
        self.layer_combo.blockSignals(True)
        try:
            self.layer_combo.setLayer(layer)
        finally:
            self.layer_combo.blockSignals(False)

    def _layer_combo_changed(self, layer):
        if layer is None:
            return
        # select it in the Layers panel too, so bag and tool agree
        iface = self.panel.iface
        if iface is not None:
            with suppress(RuntimeError):
                iface.layerTreeView().setCurrentLayer(layer)
        self.follow_layer_selection(layer, None)

    def _begin(self, doc):
        self.original = dict(doc)
        self.saved = dict(doc)
        self.committed = dict(doc)
        self.working = dict(doc)
        self.undo_stack, self.redo_stack = [], []
        self._snapshot_taken = False
        self._pending_width = None
        self._pending_size = None
        self._applied = dict(doc)        # what the map currently shows (layer role)
        self._final_applied = dict(doc)  # what the layer's saved renderer holds
        self._pending_color = None

    def _end_session(self):
        """Forget the current target. In symbol mode unsaved changes are
        dropped (the bag was never touched); in layer mode the changes are
        already on the map (Reset / the bag's Ctrl+Z restore them)."""
        if self.uid is not None:
            self.previewChanged.emit(self.uid, None)
        if self.mode == "layer" and self.working is not None and self.working != self.committed:
            self._apply_to_layer(self.committed, final=True)
        self.uid = None
        self.layer_id = None
        self.layer_keys = None
        self.kind = None
        self.items = []
        self.title_text = ""
        self.original = self.saved = self.committed = self.working = None
        self.undo_stack, self.redo_stack = [], []

    # --- the target panel -----------------------------------------------------------
    def _elide_name(self):
        lbl = self.name_label
        w = max(40, lbl.width() if lbl.width() > 60 else self.width() - 110)
        mode = Qt.TextElideMode.ElideMiddle
        lbl.setText(lbl.fontMetrics().elidedText(self._shown_name(), mode, w))

    def _shown_name(self):
        """Name above the boxes: what is being edited right now – the chosen
        class (or 'All classes') when the layer has several, else the title."""
        if self.mode == "layer" and self.active and len(self.items) > 1:
            key = self.target[0]
            return "All classes" if key == "*" else dict(self.items).get(key, self.title_text)
        return self.title_text or ""

    def _set_target_ui(self):
        has = self.active
        layer_mode = self.mode == "layer"
        self.target_box.setVisible(has)
        self.actions_box.setVisible(has)
        self.width_box.setVisible(has and self.kind != KIND_RASTER)
        self.ramp_box.setVisible(False)
        # the class row only when there is something to choose (several classes)
        self.items_box.setVisible(has and layer_mode and len(self.items) > 1)
        self.replace_btn.setVisible(not layer_mode)
        self.done_btn.setVisible(layer_mode)
        self.save_new_btn.setText("Save to Bag" if layer_mode else "Save as New")
        self.save_new_btn.setToolTip("Add this layer's recolored symbol to the bag" if layer_mode else
                                     "Keep the original and add the recolored version to the bag")
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        if not has:
            if layer_mode:
                self.hint.setText("Choose a layer above – or select a layer or one of its categories in the "
                                  "Layers panel. Its colors appear here and change live on the map.")
            else:
                self.hint.setText("Select a symbol card, then click its palette icon to recolor it. "
                                  "You can still pick, copy and save colors here.")
            self.hint.show()
            self.target_combo.blockSignals(False)
            self._update_actions()
            self._fit_if_visible()
            return
        self._elide_name()
        self.name_label.setToolTip(self.title_text)
        targets = self._targets()
        for label, target in targets:
            self.target_combo.addItem(label, target)
        self.target_combo.blockSignals(False)
        if not targets:
            self.hint.setText("This style has no colors to recolor (e.g. hillshade, grayscale or multiband). "
                              "Edit it with the raster properties instead.")
            self.hint.show()
            self.target_combo.setEnabled(False)
        else:
            self.hint.hide()
            self.target_combo.setEnabled(True)
        self.ramp_box.setVisible(self.kind == KIND_RASTER and bool(targets))
        self.target = targets[0][1] if targets else ("_", ("tint",))
        self.target_combo.setCurrentIndex(0)
        self._rebuild_parts()
        self._refresh_items_grid()
        self._refresh_stops()
        self._show_color(self._current_color(), source=None, animate=True)
        self._refresh_width()
        self._update_preview_now()
        self._update_actions()
        self._check_density()
        self._fit_if_visible()

    def _targets(self):
        """[(label, (item key, target))] for the combo."""
        out = []
        many = len(self.items) > 1
        if many and self.kind != KIND_RASTER:
            kinds = [[t for _l, t in rc.targets_for(self.kind, self.working[k])] for k, _l in self.items]
            fill_o = [("All classes · fill / line colors", ("*", ("fills",)))] \
                if any(("fills",) in ts for ts in kinds) else []
            out_o = [("All classes · outline colors", ("*", ("outlines",)))] \
                if any(("outlines",) in ts for ts in kinds) else []
            both_o = [("All classes · fill + outline", ("*", ("both",)))] \
                if any(("both",) in ts for ts in kinds) else []
            empty = all(rc.is_empty_target(symbol_from_xml(self.working[k]), ("fills",)) for k, _l in self.items)
            out.extend((out_o + fill_o if empty else fill_o + out_o) + both_o)
        for key, item_label in self.items:
            for label, target in rc.targets_for(self.kind, self.working[key]):
                out.append(("{} · {}".format(item_label, label) if many else label, (key, target)))
        return out

    def _fit_if_visible(self):
        self._fit(self.width())

    def _xml_for(self, key, doc=None):
        doc = doc if doc is not None else self.working
        if key == "*":
            key = self.items[0][0]
        return doc.get(key)

    def _current_color(self):
        key, target = self.target
        return rc.current_color(self.kind, self._xml_for(key), target)

    def _target_changed(self, index):
        if index < 0 or not self.active:
            return
        self._commit()
        self.target = self.target_combo.itemData(index)
        self._elide_name()
        self._rebuild_parts()
        self._refresh_stops()
        self._refresh_items_grid()
        self._show_color(self._current_color(), source=None, animate=True)
        self._refresh_width()
        self._render_preview_tile()
        self._update_dense_hint()

    def _refresh_items_grid(self):
        """Layer role: the Class list – All classes + each class with its own
        symbol (like the legend). Updated in place so it never flickers."""
        if not (self.mode == "layer" and self.active and len(self.items) > 1):
            return
        from qgis.PyQt.QtGui import QIcon
        cb = self.class_combo
        cb.blockSignals(True)
        try:
            want = 1 + len(self.items)
            if cb.count() != want or cb.itemData(1) != self.items[0][0]:
                cb.clear()
                cb.addItem("All classes", "*")
                for key, lbl in self.items:
                    cb.addItem(lbl, key)
            for i, (key, lbl) in enumerate(self.items):
                with suppress(Exception):
                    if self.kind == KIND_RASTER:
                        pm = QPixmap(18, 18)
                        pm.fill(rc.current_color(self.kind, self.working[key], ("tint",)))
                    else:
                        pm = vector_preview(symbol_from_xml(self.working[key]), 18)
                    cb.setItemIcon(i + 1, QIcon(pm))
                cb.setItemText(i + 1, lbl)
            cur = self.target[0]
            idx = cb.findData(cur)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            cb.blockSignals(False)

    # --- box-shaped choices ------------------------------------------------------------
    PART_NAMES = {("tint",): "Whole", ("outlines",): "Outline", ("both",): "Fill + outline"}

    def _part_name(self, target, label):
        if target in self.PART_NAMES:
            return self.PART_NAMES[target]
        if target == ("fills",):
            if self.kind == "line":
                return "Line color" if "All" not in label else "All lines"
            return "Fill color" if "All" not in label else "All fills"
        return None                                  # a single layer / stop: in "Layers ▾"

    def _rebuild_parts(self):
        """Boxes for the current class (or All classes): Whole / Fill color /
        Outline, plus 'Layers ▾' for the layers of a multi-layer symbol."""
        while self.part_layout.count():
            item = self.part_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.part_buttons = []
        if not self.active:
            self.part_bar.hide()
            return
        key = self.target[0]
        opts = [(self.target_combo.itemText(k), self.target_combo.itemData(k), k)
                for k in range(self.target_combo.count()) if self.target_combo.itemData(k)[0] == key]
        prefix = "All classes · " if key == "*" else (
            dict(self.items).get(key, "") + " · " if len(self.items) > 1 else "")
        main, more = [], []
        if self.kind in rc.SIZE_KINDS:
            # points: what you change is the point (colour + size) or its outline (colour + width)
            by_target = {target: (label, idx) for label, (_k, target), idx in opts}
            point_t = ("tint",) if ("tint",) in by_target else (("fills",) if ("fills",) in by_target else None)
            if point_t is not None:
                main.append(("Size", point_t, by_target[point_t][1]))
            if ("outlines",) in by_target:
                main.append(("Outline", ("outlines",), by_target[("outlines",)][1]))
            for label, (_k, target), idx in opts:
                if target in (point_t, ("outlines",)):
                    continue
                own = label[len(prefix):] if prefix and label.startswith(prefix) else label
                more.append((own, target, idx))
        else:
            for label, (_k, target), idx in opts:
                if self.kind == KIND_RASTER:
                    if target == ("tint",):
                        main.append(("Whole ramp", target, idx))
                    continue                             # stops: click them on the ramp
                own = label[len(prefix):] if prefix and label.startswith(prefix) else label
                name = self._part_name(target, own)
                (main if name else more).append((name or own, target, idx))
        show = len(main) + (1 if more else 0) > 1 or bool(more)
        self.part_bar.setVisible(show)
        if not show:
            return
        cur = self.target[1]
        for name, target, idx in main:
            b = QToolButton()
            b.setObjectName("SBPartChip")
            b.setText(name)
            b.setCheckable(True)
            b.setChecked(target == cur)
            b.setToolTip({"Whole": "Recolor the whole symbol, keeping its light / dark shades",
                          "Size": "The point: its colour (wheel) and its size (dial)",
                          "Outline": "The outline: its colour (wheel) and its width (dial)"
                          if self.kind in rc.SIZE_KINDS else "Only the outline color",
                          "Fill + outline": "Fill and outline get the same color – best for layers with many "
                                            "small features, where the outline is most of what you see"}
                         .get(name, "Only the {}".format(name.lower())))
            b.clicked.connect(lambda _c=False, i=idx: self._choose_target(i))
            b.setMinimumWidth(b.sizeHint().width())
            self.part_layout.addWidget(b)
            self.part_buttons.append(b)
        if more:
            b = QToolButton()
            b.setObjectName("SBPartChip")
            b.setCheckable(True)
            sel = [n for n, t, _i in more if t == cur]
            b.setChecked(bool(sel))
            only_layers = all(t not in (("both",), ("fills",), ("tint",), ("outlines",)) for _n, t, _i in more)
            sel_name = sel[0].split(" · ")[0] if sel else ""
            if sel_name.startswith("Fill + outline"):
                sel_name = "Fill + outline"
            b.setText((sel_name if sel else ("Layers" if only_layers else "More")) + " ▾")
            b.setMinimumWidth(b.sizeHint().width())       # never cut to "Lay…"
            b.setToolTip("Recolor one color of one symbol layer" if only_layers
                         else "More choices: same colour for fill and outline, single layers")
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            m = QMenu(b)
            for n, t, i in more:
                a = m.addAction(n, lambda i=i: self._choose_target(i))
                a.setCheckable(True)
                a.setChecked(t == cur)
            b.setMenu(m)
            self.part_layout.addWidget(b)
            self.part_buttons.append(b)
        self._fit_part_bar()

    def _fit_part_bar(self, content_w=None):
        """Height of the (possibly wrapped) chip row for the available width."""
        if not hasattr(self, "part_bar"):
            return
        avail = self.part_bar.width() if self.part_bar.width() > 40 and content_w is None else \
            max(80, (content_w or self.width() - 22) - 62)
        h = self.part_layout.heightForWidth(int(avail))
        if h > 0:
            self.part_bar.setFixedHeight(h)

    def _choose_target(self, index):
        if index != self.target_combo.currentIndex():
            self.target_combo.setCurrentIndex(index)
        else:
            self._rebuild_parts()

    def _all_clicked(self):
        for k in range(self.target_combo.count()):
            if self.target_combo.itemData(k)[0] == "*":
                self._choose_target(k)
                return

    def _class_chosen(self, index):
        key = self.class_combo.itemData(index)
        if key == "*":
            self._all_clicked()
        elif key is not None:
            self._item_clicked([k for k, _l in self.items].index(key))

    def _item_clicked(self, i):
        key = self.items[i][0]
        for k in range(self.target_combo.count()):
            if self.target_combo.itemData(k)[0] == key:
                self.target_combo.setCurrentIndex(k)
                return

    # --- raster ramps -------------------------------------------------------------------
    def _ramp_key(self):
        return self.items[0][0] if self.items else None

    def _refresh_stops(self):
        if self.kind != KIND_RASTER or not self.active:
            return
        xml = self.working[self._ramp_key()]
        stops = rc.raster_stops(xml)
        t = self.target[1]
        cur = t[1] if t[0] == "stop" else -1
        self.stops.discrete = "DISCRETE" in xml or "paletteEntry" in xml
        self.stops.set_stops([s[3] for s in stops], cur)

    def _stop_clicked(self, i):
        for k in range(self.target_combo.count()):
            if self.target_combo.itemData(k)[1] == ("stop", i):
                self.target_combo.setCurrentIndex(k)
                return

    def _ramp_preset(self, index):
        name = self.ramp_combo.itemData(index)
        if not name or not self.active or self.kind != KIND_RASTER:
            return
        key = self._ramp_key()
        ramp = QgsStyle.defaultStyle().colorRamp(name)
        self.working = dict(self.committed)
        self.working[key] = rc.apply_ramp(self.committed[key], ramp)
        self._commit()
        self._after_definition_change()
        self.ramp_combo.setCurrentIndex(0)

    def _reverse_ramp(self):
        if not self.active or self.kind != KIND_RASTER:
            return
        key = self._ramp_key()
        self.working = dict(self.committed)
        self.working[key] = rc.reverse_ramp(self.committed[key])
        self._commit()
        self._after_definition_change()

    def _after_definition_change(self):
        self._refresh_stops()
        self._refresh_items_grid()
        self._show_color(self._current_color(), source=None, animate=True)
        self._refresh_width()
        self._schedule_preview()
        self._update_actions()

    # --- colour changes ----------------------------------------------------------
    def _on_color(self, color, source, final):
        color = QColor(color)
        if not color.isValid():
            return
        self._show_color(color, source=source, animate=source != "wheel")
        if self.active and self.target_combo.isEnabled():
            # while dragging only remember the colour; the (heavier) recolour
            # runs once per preview tick, so the wheel never waits for it
            self._pending_color = color
            if final:
                self._recompute()
            self._schedule_preview()
        if final:
            self._commit()
            self._refresh_items_grid()
            self.palettes.push_recent(color)
            self._refresh_recent()

    def _recompute(self):
        color, self._pending_color = self._pending_color, None
        width, self._pending_width = self._pending_width, None
        size, self._pending_size = self._pending_size, None
        if (color is None and width is None and size is None) or not self.active:
            return
        key, target = self.target
        keys = [k for k, _l in self.items] if key == "*" else [key]
        new = dict(self.committed)
        for k in keys:
            x = new[k]
            if color is not None:
                x = rc.recolor(self.kind, x, target, color)
            if width is not None:
                x = rc.set_width(self.kind, x, width)
            if size is not None:
                x = rc.set_size(self.kind, x, size)
            new[k] = x
        self.working = new
        if self.kind == KIND_RASTER:
            stops = rc.raster_stops(self.working[self._ramp_key()])
            self.stops.set_stops([s[3] for s in stops], self.stops.current)

    # --- thickness -------------------------------------------------------------------
    def _on_width(self, value, final, source=None):
        if value is None or not self.active or self.kind == KIND_RASTER or not self.width_dial.isEnabled():
            return
        value = round(max(0.0, float(value)), 2)
        if source != "dial":
            self.width_dial.set_value(value, animate=True)
        if source != "spin":
            self.width_spin.blockSignals(True)
            self.width_spin.setValue(value)
            self.width_spin.blockSignals(False)
        if self._sizing():
            self._pending_size = value
        else:
            self._pending_width = value
        if final:
            self._recompute()
        self._schedule_preview()
        if final:
            self._commit()
            self._refresh_items_grid()

    def _stroke_color(self, shown):
        """Colour of the sample stroke in the thickness wheel: the line itself,
        or the outline of a fill / marker (not its fill colour)."""
        if self.kind in ("line", None) or not self.active or self.target[1] == ("outlines",):
            return QColor(shown)
        try:
            c = rc.current_color(self.kind, self._xml_for(self.target[0]), ("outlines",))
            return c if c.isValid() else QColor(shown)
        except Exception:
            return QColor(shown)

    def _sizing(self):
        return self.kind in rc.SIZE_KINDS and self.dim_mode == "size"

    def set_dim_mode(self, mode):
        """Points: 'size' picks the Size chip, 'width' the Outline chip."""
        key = self.target[0]
        want = [("outlines",)] if mode == "width" else [("tint",), ("fills",)]
        for t in want:
            for k in range(self.target_combo.count()):
                if self.target_combo.itemData(k) == (key, t):
                    self._choose_target(k)
                    return
        self._commit()
        self.dim_mode = mode
        self._refresh_width()

    def _refresh_width(self):
        show = self.active and self.kind != KIND_RASTER
        self.width_box.setVisible(show)
        if not show:
            return
        point = self.kind in rc.SIZE_KINDS
        if point:
            # the chip above decides: Outline -> outline width, anything else -> point size
            self.dim_mode = "width" if self.target[1] == ("outlines",) else "size"
        self.dim_switch.hide()
        if self._sizing():
            self._refresh_size()
            return
        self.width_dial.sample = "stroke"
        self.width_dial.setToolTip("Drag around the wheel (or scroll) to change the thickness")
        self.width_spin.setToolTip("Exact thickness (0 = hairline)")
        key = self.target[0]
        info = rc.width_info(self.kind, self._xml_for(key))
        enabled = info is not None
        for w in [self.width_dial, self.width_spin] + self.width_presets:
            w.setEnabled(enabled)
        label = "Outline width" if point else rc.WIDTH_LABELS.get(self.kind, "Width")
        if key == "*":
            label += " · all classes"
        self.width_label.setText(label)
        if not enabled:
            self.width_hint.setText("No outline to thicken" if self.kind in ("fill", "polygon") else "No stroke to thicken")
            self.width_hint.show()
            return
        self.width_hint.hide()
        value, unit = info
        self.width_dial.unit = unit
        base = 5.0 if unit == "mm" else 20.0
        self.width_dial.set_range(max(base, value * 2.0))
        self.width_dial.color = self._stroke_color(self.color)
        self.width_dial.set_value(value, animate=True)
        self.width_spin.blockSignals(True)
        self.width_spin.setSuffix(" " + unit)
        self.width_spin.setSingleStep(0.05 if unit == "mm" else 0.25)
        self.width_spin.setValue(value)
        self.width_spin.blockSignals(False)
        for b, w in zip(self.width_presets, [0.26, 0.5, 1.0, 2.0] if unit == "mm" else [1.0, 2.0, 4.0, 8.0]):
            b.setProperty("w", w)
            b.setText("{:g}".format(w))
            b.setToolTip("{:g} {}".format(w, unit))

    def _refresh_size(self):
        key = self.target[0]
        info = rc.size_info(self.kind, self._xml_for(key))
        enabled = info is not None
        for w in [self.width_dial, self.width_spin] + self.width_presets:
            w.setEnabled(enabled)
        self.width_label.setText("Point size" + (" · all classes" if key == "*" else ""))
        if not enabled:
            self.width_hint.setText("This symbol has no size to change")
            self.width_hint.show()
            return
        self.width_hint.hide()
        value, unit = info
        self.width_dial.sample = "dot"
        self.width_dial.setToolTip("Drag around the wheel (or scroll) to change the point size")
        self.width_spin.setToolTip("Exact point size")
        self.width_dial.unit = unit
        base = 12.0 if unit == "mm" else 40.0
        self.width_dial.set_range(max(base, value * 2.0))
        self.width_dial.color = QColor(self.color)
        self.width_dial.set_value(value, animate=True)
        self.width_spin.blockSignals(True)
        self.width_spin.setSuffix(" " + unit)
        self.width_spin.setSingleStep(0.2 if unit == "mm" else 1.0)
        self.width_spin.setValue(value)
        self.width_spin.blockSignals(False)
        for b, w in zip(self.width_presets, [2.0, 3.0, 4.0, 6.0] if unit == "mm" else [6.0, 10.0, 14.0, 20.0]):
            b.setProperty("w", w)
            b.setText("{:g}".format(w))
            b.setToolTip("{:g} {}".format(w, unit))

    def _show_color(self, color, source=None, animate=True):
        """Update every control to show `color` (without feedback loops)."""
        self.color = QColor(color)
        if source != "wheel":
            self.wheel.set_color(color, animate=animate)
        for w in [self.hex_edit, self.alpha_slider, self.alpha_spin] + self.rgb + self.hsv:
            w.blockSignals(True)
        if source != "hex":
            self.hex_edit.setText(to_hex(color).upper())
        if source != "rgb":
            for sp, v in zip(self.rgb, (color.red(), color.green(), color.blue())):
                sp.setValue(v)
        if source != "hsv":
            h = color.hsvHue()
            self.hsv[0].setValue(h if h >= 0 else self.hsv[0].value())
            self.hsv[1].setValue(int(round(color.hsvSaturationF() * 100)))
            self.hsv[2].setValue(int(round(color.valueF() * 100)))
        a = int(round(color.alphaF() * 100))
        self.alpha_slider.setValue(a)
        self.alpha_spin.setValue(a)
        for w in [self.hex_edit, self.alpha_slider, self.alpha_spin] + self.rgb + self.hsv:
            w.blockSignals(False)
        for grid in (self.palette_grid, self.recent_grid, self.fav_grid):
            grid.current = QColor(color)
            grid.update()
        self.width_dial.color = QColor(color) if self._sizing() else self._stroke_color(color)
        self.width_dial.update()
        self._update_fav_btn()

    def _hex_entered(self):
        text = self.hex_edit.text().strip()
        if text and not text.startswith("#"):
            text = "#" + text
        c = QColor(text)
        if not c.isValid():
            self.hex_edit.setText(to_hex(self.color).upper())
            self.hex_edit.setToolTip("Not a valid color: use #RRGGBB or #AARRGGBB")
            return
        self._on_color(c, "hex", True)

    def _rgb_entered(self, final):
        c = QColor(self.rgb[0].value(), self.rgb[1].value(), self.rgb[2].value())
        c.setAlpha(self.color.alpha())
        self._on_color(c, "rgb", final)

    def _hsv_entered(self, final):
        c = QColor.fromHsv(self.hsv[0].value() % 360, int(self.hsv[1].value() * 2.55), int(self.hsv[2].value() * 2.55))
        c.setAlpha(self.color.alpha())
        self._on_color(c, "hsv", final)

    def _alpha_changed(self, value, final):
        c = QColor(self.color)
        c.setAlphaF(value / 100.0)
        self._on_color(c, "alpha", final)

    def _set_input_tab(self, index, save=True):
        self.input_stack.setCurrentIndex(index)
        b = self.input_group.button(index)
        b.setChecked(True)
        self.input_bar.set_active(b, animate=save)
        if save:
            self.state["input"] = ("hex", "rgb", "hsv")[index]
            self.save_state()

    # --- undo / redo / reset ------------------------------------------------------
    def _commit(self):
        if self.working is not None and self.working != self.committed:
            self.undo_stack.append(self.committed)
            del self.undo_stack[:-50]
            self.redo_stack = []
            self.committed = self.working
            if self.mode == "layer":
                self._apply_to_layer(self.working, final=True)
        self._update_actions()

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self.committed)
        self.committed = self.working = self.undo_stack.pop()
        self._after_definition_change()
        if self.mode == "layer":
            self._apply_to_layer(self.working, final=True)

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self.committed)
        self.committed = self.working = self.redo_stack.pop()
        self._after_definition_change()
        if self.mode == "layer":
            self._apply_to_layer(self.working, final=True)

    def reset(self):
        if not self.active or self.working == self.original:
            return
        self.working = dict(self.original)
        self._commit()
        self._after_definition_change()

    def _update_actions(self):
        has = self.active
        self.undo_btn.setEnabled(has and bool(self.undo_stack))
        self.redo_btn.setEnabled(has and bool(self.redo_stack))
        self.reset_btn.setEnabled(has and self.working != self.original)
        changed = has and self.dirty
        if self.mode == "layer":
            self.save_new_btn.setEnabled(has and self.target_combo.count() > 0)
            if not has:
                self.state_label.setText("")
            elif self.working != self.original:
                self.state_label.setText("Live on the map")
            else:
                self.state_label.setText("Layer's original colors")
            return
        self.save_new_btn.setEnabled(changed)
        self.replace_btn.setEnabled(changed)
        if not has:
            self.state_label.setText("")
        elif changed:
            self.state_label.setText("Preview only – not saved")
        elif self.working != self.original:
            self.state_label.setText("Saved")
        else:
            self.state_label.setText("Original colors")

    # --- preview ------------------------------------------------------------------------
    def _schedule_preview(self):
        if not self._preview_timer.isActive():
            self._preview_timer.setInterval(40 if self.mode == "layer" else 30)
            self._preview_timer.start()

    def _canvas_busy(self):
        iface = self.panel.iface
        try:
            return iface is not None and iface.mapCanvas().isDrawing()
        except (RuntimeError, AttributeError):
            return False

    def _update_preview_now(self):
        self._preview_timer.stop()
        self._render_preview()

    def _render_preview(self):
        if not self.active:
            return
        if self.mode == "layer" and (self._pending_color is not None or self._pending_width is not None
                                     or self._pending_size is not None) \
                and self._canvas_busy():
            # the map is still drawing the previous colour: let it finish
            # instead of restarting it every frame (that is what feels slow)
            self._preview_timer.start(45)
            return
        self._recompute()
        self._render_preview_tile()
        if self.mode == "layer":
            self._apply_to_layer(self.working, final=False)
            return
        xml = self.working["_"]
        size = self.panel.settings.get("preview_size", 44)
        try:
            if self.kind == KIND_RASTER:
                card = QPixmap.fromImage(raster_preview_image(xml, 120)).scaled(
                    size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            else:
                card = vector_preview(symbol_from_xml(xml), size)
        except Exception:
            card = broken_preview(size)
        self.previewChanged.emit(self.uid, card if self.working != self.saved else None)

    def _render_preview_tile(self):
        if not self.active:
            return
        key = self.target[0]
        xml = self._xml_for(key)
        try:
            if self.kind == KIND_RASTER:
                own = QPixmap.fromImage(raster_preview_image(xml, 120)).scaled(
                    40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            else:
                own = vector_preview(symbol_from_xml(xml), 40)
        except Exception:
            own = broken_preview(40)
        tile = QPixmap(46, 46)
        tile.fill(Qt.GlobalColor.transparent)
        p = QPainter(tile)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.theme.tile(self.kind) if not self.theme.dark else QColor("#f4f6f9"))
        p.drawRoundedRect(QRectF(0, 0, 46, 46), 10, 10)
        p.drawPixmap(int((46 - own.width() / own.devicePixelRatio()) / 2),
                     int((46 - own.height() / own.devicePixelRatio()) / 2), own)
        p.end()
        self.preview.setPixmap(tile)

    def _apply_to_layer(self, doc, final):
        """Layer mode: show the colours on the map. Final = also refresh the
        legend in the Layers panel and mark the project as changed."""
        layer = self._layer()
        if layer is None or doc is None:
            return
        applied = getattr(self, "_applied", None) or {}
        if not self._snapshot_taken and any(applied.get(k) != x for k, x in doc.items()):
            # one entry in the bag's undo (Ctrl+Z) for this whole session,
            # taken before the first change reaches the layer
            self.panel.undo.snapshot([layer])
            self._snapshot_taken = True
        if final:
            QTimer.singleShot(400, lambda: self._hold_canvas(False))
            changed = {k: x for k, x in doc.items() if getattr(self, "_final_applied", {}).get(k) != x}
            if changed:
                self._commit_to_layer(layer, changed)
            self._applied = dict(applied, **doc)
            self._final_applied = dict(doc)
        else:
            changed = {k: x for k, x in doc.items() if applied.get(k) != x}
            if changed:
                self._hold_canvas(True)
                rc.apply_layer_items(layer, changed, live=True)   # in place, no legend rebuild
                self._applied = dict(applied, **changed)
            return
        if changed:
            from qgis.core import QgsProject
            QgsProject.instance().setDirty(True)
            self.panel._update_footer()
            QTimer.singleShot(0, self._restore_legend_selection)

    # --- dense layers -------------------------------------------------------------------
    SLOW_RENDER_S = 0.6       # renders slower than this are shown progressively
    DENSE_PX = 4.0            # median feature size (px) below which outlines dominate
    DENSE_SAMPLE = 200        # features sampled in the visible extent

    def _watch_canvas(self):
        iface = self.panel.iface
        if iface is None:
            return
        try:
            canvas = iface.mapCanvas()
            canvas.renderStarting.connect(self._on_render_start)
            canvas.mapCanvasRefreshed.connect(self._on_render_done)
            canvas.extentsChanged.connect(self._on_extent_changed)
        except (AttributeError, RuntimeError, TypeError):
            pass

    def _on_render_start(self):
        self._render_t0 = time.time()

    def _on_render_done(self):
        if self._render_t0 is not None:
            self._last_render_s = time.time() - self._render_t0
            self._render_t0 = None

    def _on_extent_changed(self):
        if self.mode == "layer" and self.isVisible() and self.active:
            self._dense_timer.start()

    def _check_density(self):
        """Median on-screen size (px) of the polygons in view. Reads at most
        DENSE_SAMPLE features through the spatial index, without attributes."""
        self._dense_px = None
        layer = self._layer() if self.mode == "layer" and self.active else None
        iface = self.panel.iface
        if layer is None or iface is None or self.kind != "fill":
            self._update_dense_hint()
            return
        try:
            from qgis.core import QgsCoordinateTransform, QgsFeature, QgsFeatureRequest, QgsProject
            canvas = iface.mapCanvas()
            dest = canvas.mapSettings().destinationCrs()
            upp = canvas.mapUnitsPerPixel()
            if upp > 0:
                to_layer = QgsCoordinateTransform(dest, layer.crs(), QgsProject.instance())
                to_map = QgsCoordinateTransform(layer.crs(), dest, QgsProject.instance())
                rect = to_layer.transformBoundingBox(canvas.extent())
                req = QgsFeatureRequest().setFilterRect(rect).setNoAttributes().setLimit(self.DENSE_SAMPLE)
                sizes = []
                f = QgsFeature()
                it = layer.getFeatures(req)
                while it.nextFeature(f):
                    g = f.geometry()
                    if g is None or g.isEmpty():
                        continue
                    bb = to_map.transformBoundingBox(g.boundingBox())
                    sizes.append(max(bb.width(), bb.height()) / upp)
                if len(sizes) >= 20:
                    sizes.sort()
                    self._dense_px = sizes[len(sizes) // 2]
        except Exception:
            self._dense_px = None
        self._update_dense_hint()

    def _has_both_target(self):
        key = self.target[0]
        return any(self.target_combo.itemData(k) == (key, ("both",)) for k in range(self.target_combo.count()))

    def _update_fill_hint(self):
        show = False
        if self.active and self.kind == "fill" and self.target[1] == ("fills",) and self.original:
            key = self.target[0]
            keys = [k for k, _l in self.items] if key == "*" else [key]
            try:
                show = any(rc.is_empty_target(symbol_from_xml(self.original.get(k)), ("fills",)) for k in keys)
            except Exception:
                show = False
        was = self.fill_hint.isVisible()
        self.fill_hint.setVisible(show)
        if was != show:
            self._fit_if_visible()

    def _update_dense_hint(self):
        self._update_fill_hint()
        dense = (self._dense_px is not None and self._dense_px < self.DENSE_PX and self.mode == "layer"
                 and self.active and self.target[1] in (("tint",), ("fills",)) and self._has_both_target())
        was = self.dense_hint.isVisible()
        if dense:
            self.dense_hint.setText(
                "At this scale most features are only about {:.0f} px wide, so the map shows mainly their "
                "outlines and a new fill color is hard to see. <a href='both'>Use Fill + outline</a> "
                "or zoom in.".format(max(1, self._dense_px)))
        self.dense_hint.setVisible(dense)
        if was != dense:
            self._fit_if_visible()

    def _dense_link(self, _link):
        key = self.target[0]
        for k in range(self.target_combo.count()):
            if self.target_combo.itemData(k) == (key, ("both",)):
                self._choose_target(k)
                break
        self._update_dense_hint()

    def _hold_canvas(self, on):
        """While dragging, QGIS would show half-drawn map frames every 250 ms
        (the layer being recoloured blinks). Hold the last finished map until
        the new one is complete instead; restored right after the drag."""
        iface = self.panel.iface
        if iface is None:
            return
        with suppress(RuntimeError, AttributeError):
            canvas = iface.mapCanvas()
            if on and self._last_render_s > self.SLOW_RENDER_S:
                # a slow layer (many features): holding the old frame would show
                # nothing for the whole render - let the new colour draw in instead
                return
            if on and getattr(self, "_canvas_interval", None) is None:
                self._canvas_interval = canvas.mapUpdateInterval()
                canvas.setMapUpdateInterval(60000)
            elif not on and getattr(self, "_canvas_interval", None) is not None:
                if getattr(self.wheel, "_drag", None) is None:     # not while still dragging
                    canvas.setMapUpdateInterval(self._canvas_interval)
                    self._canvas_interval = None

    def _commit_to_layer(self, layer, changed):
        """Store an edit on the layer without making QGIS rebuild its legend.

        Replacing the renderer makes the Layers panel throw away and rebuild
        every legend row of the layer (the panel flashes and a selected
        category is deselected). For vector classes the legend row itself is
        given the new symbol instead: QGIS updates the renderer, the row's
        icon and the map – nothing is rebuilt, the selection stays."""
        from qgis.core import QgsRasterLayer
        nodes = {}
        iface = self.panel.iface
        if iface is not None and not isinstance(layer, QgsRasterLayer):
            try:
                from qgis.core import QgsProject
                tree_node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
                model = iface.layerTreeView().layerTreeModel()
                if tree_node is not None:
                    for ln in model.layerLegendNodes(tree_node):
                        k = legend_key_of(ln)
                        if k is not None and hasattr(ln, "setSymbol"):
                            nodes[k] = ln
            except (RuntimeError, AttributeError):
                nodes = {}
        rest = {}
        for key, xml in changed.items():
            ln = nodes.get(key)
            sym = symbol_from_xml(xml) if ln is not None else None
            if ln is not None and sym is not None:
                with suppress(RuntimeError, TypeError):
                    ln.setSymbol(sym)          # renderer + legend icon + repaint
                    continue
            rest[key] = xml
        if rest:
            rc.apply_layer_items(layer, rest)   # raster / no legend row: full update
        layer.emitStyleChanged()
        layer.triggerRepaint()

    def _restore_legend_selection(self):
        """QGIS rebuilds a layer's legend when its style changes, which drops a
        selected category row. Select the same classes again so the tool and
        the bag keep pointing at what the user picked."""
        iface = self.panel.iface
        layer = self._layer()
        if iface is None or layer is None or not self.layer_keys:
            return
        view = iface.layerTreeView()
        with suppress(RuntimeError, AttributeError, TypeError):
            if any(legend_key_of(n) in self.layer_keys for n in self.panel._selected_legend_nodes()):
                return
            from qgis.core import QgsProject
            from qgis.PyQt.QtCore import QItemSelectionModel
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            model = view.layerTreeModel()
            sm = view.selectionModel()
            first = True
            for ln in model.layerLegendNodes(node):
                if legend_key_of(ln) in self.layer_keys:
                    idx = view.legendNode2index(ln) if hasattr(view, "legendNode2index") \
                        else model.legendNode2index(ln)
                    if idx is None or not idx.isValid():
                        continue
                    flag = QItemSelectionModel.SelectionFlag.ClearAndSelect if first \
                        else QItemSelectionModel.SelectionFlag.Select
                    sm.select(idx, flag)
                    if first:
                        sm.setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.NoUpdate)
                    first = False

    # --- confirm --------------------------------------------------------------------------
    def done(self):
        """Keep the colours and tuck the tool into its bubble – ready for the
        next layer (only ✕ closes it)."""
        self._commit()
        if self.embedded:
            # keep the colours; the next change starts a fresh undo step
            self._end_session()
            self.follow_layer_selection()
            return
        if self.role == "layer" and self.isVisible():
            self.minimize_to_bubble()
            # the edit is finished: the next opening starts a fresh session
            # (its own Ctrl+Z step) on whatever is selected then
            self._end_session()
        else:
            self.close_animated()

    def save_as_new(self):
        if not self.active:
            return
        self._commit()
        key = self.target[0]
        key = self.items[0][0] if key == "*" else key
        xml = self.working[key]
        if self.mode == "layer":
            label = dict(self.items).get(key, "")
            default = label or self.title_text
            rec = None
        else:
            if not self.dirty:
                return
            rec = self.store.record(self.uid)
            default = "{} – {}".format(rec.name, self.color.name().upper())
        name, ok = QInputDialog.getText(self, "Save to Symbol Bag", "Name:", QLineEdit.EchoMode.Normal, default)
        if not ok or not name.strip():
            return
        subtype = rc.raster_style_info(xml)["renderer"] if self.kind == KIND_RASTER else (rec.subtype if rec else "")
        if rec is not None:
            tags = ", ".join([t for t in rec.tag_list if t != "recolored"] + ["recolored"])
            uid, created = self.store.add_symbol(name.strip(), self.kind, xml, subtype, rec.description, tags,
                                                 rec.collection_id, False)
        else:
            layer = self._layer()
            uid, created = self.store.add_symbol(
                name.strip(), self.kind, xml, subtype,
                "From layer '{}'".format(layer.name() if layer else ""), "from layer")
        if not created:
            self.panel._msg("An identical symbol is already in the bag ('{}').".format(self.store.record(uid).name))
        else:
            self.panel._msg("Saved '{}' to the bag.".format(name.strip()), Qgis.MessageLevel.Success)
        if self.mode == "symbol":
            self.previewChanged.emit(self.uid, None)
            self.uid = None
            self.retarget(uid)
        self.panel._select_after_refresh(uid)

    def replace_existing(self):
        if self.mode != "symbol" or self.uid is None or not self.dirty:
            return
        self._commit()
        rec = self.store.record(self.uid)
        xml = self.working["_"]
        fields = {"xml": xml, "kind": self.kind}
        if self.kind == KIND_RASTER:
            fields["subtype"] = rc.raster_style_info(xml)["renderer"]
        self.store.update_symbol(self.uid, **fields)
        self.panel.cache.invalidate(self.uid)
        self.saved = dict(self.working)
        self.previewChanged.emit(self.uid, None)
        self._update_actions()
        self.panel._msg("Replaced the colors of '{}'. Reset + Replace brings the original back.".format(rec.name),
                        Qgis.MessageLevel.Success)

    # ==================================================================
    # palettes, favourites, recent
    # ==================================================================
    def _load_palettes(self, select=None):
        names = self.palettes.names()
        self.palette_combo.blockSignals(True)
        self.palette_combo.clear()
        for n in names:
            self.palette_combo.addItem(n, n)
        want = select or self.state.get("palette")
        i = self.palette_combo.findData(want)
        self.palette_combo.setCurrentIndex(i if i >= 0 else 0)
        self.palette_combo.blockSignals(False)
        self._palette_changed()

    def current_palette(self):
        return self.palette_combo.currentData()

    # --- the swatch strip: one source at a time ------------------------------------
    def _swatch_source(self):
        src = self.state.get("swatches", "palette")
        return src if src in ("palette", "recent", "favorites") else "palette"

    def set_swatch_source(self, src, palette=None):
        if palette:
            i = self.palette_combo.findData(palette)
            if i >= 0:
                self.palette_combo.setCurrentIndex(i)
        self.state["swatches"] = src
        self.store.set_setting(self._skey, self.state)
        self._apply_swatch_source()

    def _apply_swatch_source(self):
        src = self._swatch_source()
        self.palette_grid.setVisible(src == "palette")
        self.recent_grid.setVisible(src == "recent")
        self.fav_grid.setVisible(src == "favorites")
        self._update_source_text()
        self._fit_if_visible()

    def _update_source_text(self):
        if not hasattr(self, "swatch_src_btn"):
            return
        src = self._swatch_source()
        text = {"recent": "Recent colors", "favorites": "Favorites"}.get(src) or (self.current_palette() or "Palette")
        self.swatch_src_btn.setText(text + "  ▾")

    def _fill_source_menu(self):
        m = self.swatch_src_menu
        m.clear()
        src = self._swatch_source()
        for key, label in (("recent", "Recent colors"), ("favorites", "Favorites")):
            a = m.addAction(label, lambda k=key: self.set_swatch_source(k))
            a.setCheckable(True)
            a.setChecked(src == key)
        m.addSection("Palettes")
        cur = self.current_palette()
        for n in self.palettes.names():
            a = m.addAction(n, lambda pn=n: self.set_swatch_source("palette", pn))
            a.setCheckable(True)
            a.setChecked(src == "palette" and n == cur)

    def _palette_changed(self, *_):
        name = self.current_palette()
        self.palette_grid.set_colors(self.palettes.colors(name) if name else [], self.color)
        if name:
            self.state["palette"] = name
            self.store.set_setting(self._skey, self.state)
        self._update_source_text()
        self._fit_if_visible()

    def _refresh_recent(self):
        self.recent_grid.set_colors(self.palettes.recent(), self.color)

    def _refresh_favs(self):
        self.fav_grid.set_colors(self.palettes.favorites(), self.color)
        self._update_fav_btn()

    def _update_fav_btn(self):
        if not hasattr(self, "fav_btn") or self.theme is None:
            return
        fav = self.palettes.is_favorite(self.color)
        self.fav_btn.setIcon(self.theme.icon("star_on" if fav else "star", "#f2b01e" if fav else self.theme.p["sub"], 16))
        self.fav_btn.setToolTip("Remove from Favorites" if fav else "Add this color to Favorites")

    def _toggle_fav(self):
        self.palettes.set_favorite(self.color, not self.palettes.is_favorite(self.color))
        self._refresh_favs()
        self._fit_if_visible()

    def _copy_hex(self):
        QApplication.clipboard().setText(to_hex(self.color).upper())
        self.panel._msg("Copied {}".format(to_hex(self.color).upper()))

    def _swatch_menu(self, color, gp, in_palette=False):
        m = QMenu(self)
        fav = self.palettes.is_favorite(color)
        m.addAction("Remove from Favorites" if fav else "Add to Favorites",
                    lambda: (self.palettes.set_favorite(color, not fav), self._refresh_favs(), self._fit_if_visible()))
        add = m.addMenu("Add to Palette")
        for n in self.palettes.names():
            add.addAction(n, lambda _c=False, pn=n: self._add_to_palette(pn, color))
        if in_palette:
            m.addAction("Remove from '{}'".format(self.current_palette()),
                        lambda: (self.palettes.remove_color(self.current_palette(), color), self._palette_changed()))
        m.addSeparator()
        m.addAction("Copy {}".format(to_hex(color).upper()), lambda: QApplication.clipboard().setText(to_hex(color).upper()))
        m.exec(gp)

    def _add_to_palette(self, name, color):
        self.palettes.add_color(name, color)
        if name == self.current_palette():
            self._palette_changed()

    def _fill_palette_menu(self):
        m = self.palette_menu
        m.clear()
        name = self.current_palette()
        m.addAction("Add Current Color to '{}'".format(name), lambda: self._add_to_palette(name, self.color))
        m.addSeparator()
        m.addAction("New Palette…", self.new_palette)
        m.addAction("New Palette from These Colors", self.palette_from_symbol).setEnabled(self.active)
        m.addAction("Rename Palette…", self.rename_palette)
        m.addAction("Delete Palette", self.delete_palette)
        m.addSeparator()
        m.addAction("Import Palette (.gpl)…", self.import_palette)
        m.addAction("Export Palette (.gpl)…", self.export_palette)
        m.addAction("Use in QGIS Color Buttons", self.publish_palette)
        m.addSeparator()
        m.addAction("Restore Default Palettes", lambda: (self.palettes.restore_defaults(), self._load_palettes(name)))

    def new_palette(self, colors=(), default=""):
        name, ok = QInputDialog.getText(self, "New Palette", "Palette name:", QLineEdit.EchoMode.Normal, default)
        if not ok or not name.strip():
            return None
        try:
            self.palettes.add_palette(name.strip(), colors)
        except LibraryError as e:
            QMessageBox.warning(self, "Palettes", str(e))
            return None
        self._load_palettes(name.strip())
        return name.strip()

    def palette_from_symbol(self):
        if not self.active:
            return
        cols = []
        for key, _lbl in self.items:
            xml = self.working[key]
            if self.kind == KIND_RASTER:
                cols += [st[3] for st in rc.raster_stops(xml)]
            else:
                sym = symbol_from_xml(xml)
                cols += [sl.color for sl in rc.color_slots(sym) if sl.color.alpha() > 0]
        uniq = []
        for c in cols:
            if all(c.rgba() != u.rgba() for u in uniq):
                uniq.append(c)
        self.new_palette(uniq, self.palettes.unique_name(self.title_text + " colors"))

    def rename_palette(self):
        old = self.current_palette()
        if not old:
            return
        name, ok = QInputDialog.getText(self, "Rename Palette", "New name:", QLineEdit.EchoMode.Normal, old)
        if ok and name.strip() and name.strip() != old:
            try:
                self.palettes.rename_palette(old, name.strip())
            except LibraryError as e:
                QMessageBox.warning(self, "Palettes", str(e))
                return
            self._load_palettes(name.strip())

    def delete_palette(self):
        name = self.current_palette()
        if not name:
            return
        if QMessageBox.question(self, "Delete Palette", "Delete the palette '{}'?".format(name)) \
                == QMessageBox.StandardButton.Yes:
            self.palettes.delete_palette(name)
            self._load_palettes()

    def import_palette(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Palette", self.panel._last_dir(), "GIMP / QGIS palette (*.gpl)")
        if not path:
            return
        try:
            name = self.palettes.import_gpl(path)
        except LibraryError as e:
            QMessageBox.warning(self, "Import Palette", str(e))
            return
        self._load_palettes(name)
        self.panel._msg("Imported palette '{}' ({} colors).".format(name, len(self.palettes.colors(name))),
                        Qgis.MessageLevel.Success)

    def export_palette(self):
        name = self.current_palette()
        if not name:
            return
        import os
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip() or "palette"
        path, _ = QFileDialog.getSaveFileName(self, "Export Palette", os.path.join(self.panel._last_dir(), safe + ".gpl"),
                                              "GIMP / QGIS palette (*.gpl)")
        if not path:
            return
        if not path.lower().endswith(".gpl"):
            path += ".gpl"
        try:
            n = self.palettes.export_gpl(name, path)
        except LibraryError as e:
            QMessageBox.warning(self, "Export Palette", str(e))
            return
        self.panel._msg("Exported '{}' ({} colors).".format(name, n), Qgis.MessageLevel.Success)

    def publish_palette(self):
        name = self.current_palette()
        if not name:
            return
        try:
            self.palettes.publish_to_qgis(name)
        except LibraryError as e:
            QMessageBox.warning(self, "Palettes", str(e))
            return
        self.panel._msg("'{}' is now available in every QGIS color button (palette menu).".format(name),
                        Qgis.MessageLevel.Success)

    # ==================================================================
    # eyedropper
    # ==================================================================
    def start_eyedropper(self):
        """Hide the picker and the bag, grab the screen, let the user click a pixel."""
        self._eyedrop_restore = (self.isVisible(), self.panel.isVisible())
        self.hide()
        if self.panel.isVisible():
            self.panel.hide_for_ghost()

        def go():
            session = EyedropperSession(self._eyedropped)
            self._eyedrop = session
            if not session.start():
                self._eyedropped(None)
                self.panel._msg("The eyedropper cannot capture the screen on this system.",
                                Qgis.MessageLevel.Warning)
        QTimer.singleShot(160, go)

    def _eyedropped(self, color):
        picker_vis, bag_vis = getattr(self, "_eyedrop_restore", (True, True))
        if bag_vis:
            self.panel.show_after_ghost()
        if picker_vis:
            self._show_real()
        self._eyedrop = None
        if color is not None and color.isValid():
            color.setAlpha(255)
            self._on_color(color, "eyedropper", True)

    # ==================================================================
    # --- embedded in another panel ------------------------------------------------------
    def _setup_embedded(self):
        for b in (self.min_btn, self.close_btn):
            if b is not None:
                b.hide()
        if self.layer_row is not None:
            self.layer_row.hide()      # the host shows the current layer
        self.done_btn.setText("Keep colors")
        self.done_btn.setToolTip("Keep these colors; the next change starts a new undo step")
        self.setMinimumWidth(0)

    def showEvent(self, e):
        super().showEvent(e)
        if self.embedded:
            QTimer.singleShot(0, self._embedded_shown)

    def _alive(self):
        st = getattr(self, "store", None)
        return not getattr(self, "_dead", False) and st is not None and getattr(st, "conn", None) is not None

    def _embedded_shown(self):
        if not self._alive():
            return
        try:
            self.follow_layer_selection()
            d = int(max(120, min(self._diameter(), self.width() - 2 * PAD - 4)))
            self.wheel.set_diameter(d)
            self._fit(self.width())
        except RuntimeError:
            pass

    def cleanup(self):
        if getattr(self, "_dead", False):
            return
        self._dead = True
        with suppress(Exception):
            self.panel.embedded_pickers.remove(self)
        if self._tween is not None:
            self._tween.stop()
        self._preview_timer.stop()
        self._scope_timer.stop()
        if self.role == "layer" and self.panel.iface is not None:
            with suppress(Exception):
                self.panel.iface.mapCanvas().layersChanged.disconnect(self._scope_timer.start)
                QgsProject.instance().layersAdded.disconnect(self._scope_timer.start)
                QgsProject.instance().layersRemoved.disconnect(self._scope_timer.start)
        with suppress(Exception):
            from . import sound
            sound.stop_all()
        self.wheel._drag = None
        self._hold_canvas(False)
        self._end_session()
        for w in [x for x in (self.bubble, self.ghost, getattr(self, "_genie_w", None), self) if x is not None]:
            with suppress(RuntimeError):
                w.hide()
                w.setParent(None)
                w.deleteLater()

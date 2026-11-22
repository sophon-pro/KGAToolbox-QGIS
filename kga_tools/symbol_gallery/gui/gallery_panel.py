# -*- coding: utf-8 -*-
"""The Symbol Gallery panel (model/view, lazy thumbnails, editing, classes).

A plain widget, hosted by the stand-alone KGA Layer Symbol window (it follows the
Layers panel) and by the KGA Layer Symbol tab of Layer Properties (pinned to the
layer being edited).
"""
from contextlib import suppress
import os
from collections import Counter

from qgis.PyQt.QtCore import Qt, QSize, QTimer, QUrl, QItemSelectionModel, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QColor
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox,
    QToolButton, QButtonGroup, QCheckBox, QListView,
    QAbstractItemView, QSlider, QPushButton, QMenu, QFileDialog,
    QApplication, QSizePolicy, QMessageBox, QSplitter, QDoubleSpinBox, QStackedWidget,
)
try:  # Qt6 / QGIS 4: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

from qgis.core import (
    QgsApplication, QgsSettings, QgsSymbolLayerUtils, QgsProject, QgsStyle,
    QgsRasterLayer,
)
from qgis.gui import QgsFilterLineEdit, QgsColorButton

from ..core import catalog, previews, packs
from ..core import apply as applier
from ..core.catalog import KIND_LABELS, MARKER, LINE, FILL, RAMP, Entry
from ..core.compat import SYMBOL_MARKER, SYMBOL_LINE, LEGEND_RULE_KEY_ROLE, STYLE_RAMP, STYLE_SYMBOL
from ..core.thumbs import ThumbnailService
from .model import GalleryModel
from .props import SymbolPropertiesWidget
from .symbol_popup import CURRENT_ID

SETTINGS = "kga_tools/layer_symbol/"
FAV = "__favorites__"
RECENT = "__recent__"
RAMPS = "__ramps__"          # virtual field: every colour ramp, from all fields
VIRTUAL = ("", FAV, RECENT, RAMPS)

# Smart raster view: guess what a raster holds from its name / file name, so
# selecting e.g. "CamDEM_30m" opens the terrain ramps and "S2_NDVI" the RS ones.
RASTER_HINTS = [
    ("Hydraulics & Flood", ("depth", "wse", "velocity", "vel_", "hazard", "flood",
                            "inund", "ras_", "hecras", "hec-ras", "d_x_v", "dxv")),
    ("Remote Sensing", ("ndvi", "evi", "savi", "ndwi", "mndwi", "nbr", "ndbi", "ndsi",
                        "ndmi", "lst", "sentinel", "landsat", "modis", "s2_", "s1_",
                        "sar", "sigma0", "backscatter", "chl", "sst", "lai", "cloud",
                        "viirs", "night")),
    ("Weather & Climate", ("rain", "precip", "chirps", "era5", "temp", "tmax", "tmin",
                           "wind", "humid", "spi", "drought", "dbz", "radar")),
    ("Irrigation & Drainage", ("soil_moist", "soilmoist", "moisture", "_et", "evapo",
                               "smap")),
    ("Agriculture", ("yield", "crop", "ndre", "gndvi")),
    ("Terrain & Geology", ("dem", "dtm", "dsm", "srtm", "alos", "aster", "elev", "height",
                           "terrain", "topo", "bathy", "slope", "aspect", "hand",
                           "relief", "lidar", "copernicus_30", "glo30", "glo-30", "fabdem",
                           "merit", "nasadem", "mnt")),
]


def guess_raster_field(layer):
    """Best gallery field for a raster layer, or None if the name gives no clue."""
    try:
        text = ("%s %s" % (layer.name(), os.path.basename(layer.source()))).lower()
    except Exception:
        return None
    for field, keys in RASTER_HINTS:
        if any(k in text for k in keys):
            return field
    return None
MAX_RECENT = 30
DEFAULT_CATEGORY = "Classic Patterns"

KIND_FILTERS = [("all", "All"), (MARKER, "Point"), (LINE, "Line"),
                (FILL, "Polygon"), (RAMP, "Ramp")]
BACKGROUND_CHOICES = [("light", "Light"), ("paper", "Paper"), ("dark", "Dark")]

STYLE_SHEET = """
QToolButton#segment {
    padding: 3px 8px; border: 1px solid palette(mid); border-radius: 0px;
    background: palette(base);
}
QToolButton#segment:checked {
    background: palette(highlight); color: palette(highlighted-text);
    border-color: palette(highlight);
}
QFrame#card {
    border: 1px solid palette(mid); border-radius: 6px; background: palette(base);
}
QLabel#detailTitle { font-weight: 600; }
QLabel#muted { color: palette(dark); }
QLabel#section { font-weight: 600; }
QPushButton#primary { font-weight: 600; padding: 4px 12px; }
QFrame#propBox { border: 1px solid palette(midlight); border-radius: 4px; }
"""


def _icon(name):
    return QgsApplication.getThemeIcon(name)


def _segmented(items, parent):
    """Row of exclusive toggle buttons. Returns (layout, QButtonGroup)."""
    lay = QHBoxLayout()
    lay.setSpacing(0)
    grp = QButtonGroup(parent)
    grp.setExclusive(True)
    for i, (key, label) in enumerate(items):
        b = QToolButton()
        b.setText(label)
        b.setCheckable(True)
        b.setObjectName("segment")
        b.setProperty("key", key)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        grp.addButton(b, i)
        lay.addWidget(b)
    grp.button(0).setChecked(True)
    return lay, grp


class GalleryPanel(QWidget):
    """Browse, preview, edit and apply curated symbols.

    *layer* pins the panel to one layer (Layer Properties / Layer Styling tab): it then
    ignores the Layers panel selection and shows its messages in a status line.
    A pinned panel defers: its host's Apply / OK buttons apply (`changed` tells the
    host something is waiting).
    """

    changed = pyqtSignal()      # a pick or an edit is waiting for the host's Apply

    def __init__(self, iface, parent=None, layer=None):
        super().__init__(parent)
        self.iface = iface
        self._pinned = layer is not None
        # Layer Properties / Layer Styling: edits wait for the host's Apply / OK
        self._deferred = self._pinned
        self._pin_layer = layer
        self._picked = False
        self.settings = QgsSettings()
        self._history = []          # (layer_id, renderer_clone, label, added_layer_ids)
        self._saved_view = None     # (category, kind) shown before a raster was selected
        self._whole_class_colours = True   # whole classified layer: keep class colours
        self._ramp_focus = None     # field guessed for the selected raster
        self._layer = None
        self._edits = {}            # entry id -> edited QgsSymbol / colour ramp
        self._draft = (None, None)  # (entry id, symbol bound to the properties panel)
        self._target_keys = []      # legend classes selected in the Layers panel
        self._class_labels = {}     # legend key -> label (for the target summary)
        self._syncing = False
        self._extras_loaded = False
        self._favorites = list(self.settings.value(SETTINGS + "favorites", [], type=list) or [])
        self._recent = list(self.settings.value(SETTINGS + "recent", [], type=list) or [])

        self.thumbs = ThumbnailService(self._lookup, self)
        self.model = GalleryModel(self.thumbs, self)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(self._apply_filter)

        self._build_ui()
        self._restore_settings()
        self._connect_layer_tracking()
        self._apply_filter()
        self._on_current_changed()
        if self._pinned:
            self.open_current_style()
        # icon libraries and style packs load right after the panel is shown
        QTimer.singleShot(0, self._load_extras)
        # disconnect from QGIS before it tears down the project on exit
        self._cleaned = False
        QgsApplication.instance().aboutToQuit.connect(self.cleanup)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _lookup(entry_id):
        return catalog.get(entry_id) if catalog.has(entry_id) else None

    def _dpr(self):
        try:
            return float(self.devicePixelRatioF())
        except AttributeError:  # pragma: no cover
            return 1.0

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget()
        root.setStyleSheet(STYLE_SHEET)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(5)

        self.search = QgsFilterLineEdit()
        self.search.setShowSearchIcon(True)
        self.search.setPlaceholderText("Search symbols, e.g. canal, hatch, hospital, zoning…")
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        lay.addWidget(self.search)

        self.category = QComboBox()
        self.category.setMaxVisibleItems(32)
        self.category.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.category.currentIndexChanged.connect(self._on_category_changed)
        lay.addWidget(self.category)

        seg, self.kind_group = _segmented(KIND_FILTERS, self)
        self.kind_group.buttonClicked.connect(lambda _b: self._apply_filter())
        lay.addLayout(seg)

        opts = QHBoxLayout()
        opts.setSpacing(10)
        self.match_layer = QCheckBox("Fits active layer")
        self.match_layer.setToolTip("Only show symbols that can be applied to the layer "
                                    "selected in the Layers panel.")
        self.match_layer.toggled.connect(self._on_match_toggled)
        opts.addWidget(self.match_layer)
        self.auto_apply = QCheckBox("Auto-apply on click")
        self.auto_apply.setToolTip("Apply a symbol as soon as you click it "
                                   "(to the whole layer or to the selected classes).")
        self.auto_apply.toggled.connect(
            lambda v: self.settings.setValue(SETTINGS + "auto_apply", v))
        opts.addWidget(self.auto_apply)
        opts.addStretch(1)
        lay.addLayout(opts)

        # ---- grid
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setLayoutMode(QListView.LayoutMode.Batched)
        self.view.setBatchSize(300)
        self.view.setWordWrap(True)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setSpacing(3)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(24)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.selectionModel().currentChanged.connect(
            lambda *_: (self._on_current_changed(),
                        self._mark_picked() if self.view.hasFocus() else None))
        self.view.clicked.connect(self._on_clicked)
        self.view.clicked.connect(lambda _i: self._mark_picked())
        self.view.doubleClicked.connect(lambda _i: self.apply_current())
        self.view.setMinimumHeight(120)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        grid_box = QWidget()
        gl = QVBoxLayout(grid_box)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(3)
        gl.addWidget(self.view, 1)
        self.empty = QLabel("No symbols match your filters.\nTry clearing the search "
                            "or choosing ‘All categories’.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        self.empty.hide()
        gl.addWidget(self.empty, 1)
        self.splitter.addWidget(grid_box)

        # ---- view controls: slim bar under the thumbnails (visible in compact mode too)
        corner = QWidget()
        view_row = QHBoxLayout(corner)
        view_row.setContentsMargins(2, 0, 2, 0)
        view_row.setSpacing(6)
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        view_row.addWidget(self.count_label, 1)
        self.bg_combo = QComboBox()
        self.bg_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for key, label in BACKGROUND_CHOICES:
            self.bg_combo.addItem(label, key)
        self.bg_combo.setToolTip("Thumbnail background – use Dark for neon/glow styles")
        self.bg_combo.currentIndexChanged.connect(self._on_appearance_changed)
        view_row.addWidget(self.bg_combo, 0)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(56, 160)
        self.size_slider.setSingleStep(8)
        self.size_slider.setPageStep(16)
        self.size_slider.setFixedWidth(80)
        self.size_slider.setToolTip("Thumbnail size")
        self.size_slider.valueChanged.connect(lambda _v: self._size_timer.start())
        self._size_timer = QTimer(self)
        self._size_timer.setSingleShot(True)
        self._size_timer.setInterval(120)
        self._size_timer.timeout.connect(self._on_appearance_changed)
        view_row.addWidget(self.size_slider)
        gl.addWidget(corner)

        # ---- tab 1: Symbol (preview, description, quick edits)
        card = QWidget()
        cl = QGridLayout(card)
        cl.setContentsMargins(8, 8, 8, 6)
        cl.setHorizontalSpacing(8)
        cl.setVerticalSpacing(3)
        self.preview = QLabel()
        self.preview.setFixedSize(120, 80)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.preview, 0, 0, 4, 1, Qt.AlignmentFlag.AlignTop)
        self.title = QLabel()
        self.title.setObjectName("detailTitle")
        self.title.setWordWrap(True)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() + 1.5)
        self.title.setFont(f)
        cl.addWidget(self.title, 0, 1)
        self.fav_btn = QToolButton()
        self.fav_btn.setAutoRaise(True)
        self.fav_btn.setText("☆")
        f2 = self.fav_btn.font()
        f2.setPointSizeF(f2.pointSizeF() + 4)
        self.fav_btn.setFont(f2)
        self.fav_btn.setToolTip("Add to / remove from Favorites")
        self.fav_btn.clicked.connect(self.toggle_favorite)
        cl.addWidget(self.fav_btn, 0, 2, Qt.AlignmentFlag.AlignTop)
        self.meta = QLabel()
        self.meta.setObjectName("muted")
        cl.addWidget(self.meta, 1, 1, 1, 2)
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.desc, 2, 1, 1, 2)
        self.tags = QLabel()
        self.tags.setObjectName("muted")
        self.tags.setWordWrap(True)
        cl.addWidget(self.tags, 3, 1, 1, 2)
        edit = QHBoxLayout()
        edit.setSpacing(4)
        edit.addWidget(QLabel("Colour"))
        self.col_btn = QgsColorButton()
        self.col_btn.setFixedWidth(48)
        self.col_btn.setToolTip("Main colour of the symbol")
        self.col_btn.setAllowOpacity(True)
        self.col_btn.colorChanged.connect(self._on_main_colour)
        edit.addWidget(self.col_btn)
        self.out_lbl = QLabel("Outline")
        edit.addWidget(self.out_lbl)
        self.out_btn = QgsColorButton()
        self.out_btn.setFixedWidth(48)
        self.out_btn.setToolTip("Outline / stroke colour")
        self.out_btn.setAllowOpacity(True)
        self.out_btn.colorChanged.connect(self._on_outline_colour)
        edit.addWidget(self.out_btn)
        self.size_lbl = QLabel("Size")
        edit.addWidget(self.size_lbl)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setDecimals(2)
        self.size_spin.setRange(0.05, 100)
        self.size_spin.setSingleStep(0.1)
        self.size_spin.setSuffix(" mm")
        self.size_spin.setToolTip("Marker size or line width")
        self.size_spin.valueChanged.connect(self._on_size_spin)
        edit.addWidget(self.size_spin)
        edit.addStretch(1)
        self.edit_btn = QToolButton()
        self.edit_btn.setIcon(_icon("/mActionStyleManager.svg"))
        self.edit_btn.setText("Edit…")
        self.edit_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.edit_btn.setToolTip("Open the full QGIS symbol editor for this symbol")
        self.edit_btn.clicked.connect(self.edit_current)
        edit.addWidget(self.edit_btn)
        self.reset_btn = QToolButton()
        self.reset_btn.setIcon(_icon("/mActionUndo.svg"))
        self.reset_btn.setToolTip("Reset to the original gallery design")
        self.reset_btn.clicked.connect(self.reset_edit)
        edit.addWidget(self.reset_btn)
        # editing lives on the Current style page (and right-click ▸ Edit Symbol…);
        # these controls are kept off-screen only so older code paths stay valid
        self._edit_row = QWidget()
        self._edit_row.setLayout(edit)
        self._edit_row.hide()
        self.edit_hint = QLabel("Double-click to apply · adjust colours and patterns in the "
                                "pop-up · right-click for more")
        self.edit_hint.setObjectName("muted")
        self.edit_hint.setWordWrap(True)
        self.edit_hint.hide()   # the hint line was removed from the layout
        cl.setRowStretch(5, 1)
        cl.setColumnStretch(1, 1)

        # ---- tab 2: Properties (every part of the symbol)
        self.props = SymbolPropertiesWidget()
        self.props.changed.connect(self._on_prop_changed)
        self.props.hide()             # not shown: the Current style page has the parts editor

        self.card = card
        card.setObjectName("infoCard")
        card.setMinimumHeight(110)
        self.splitter.addWidget(card)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        lay.addWidget(self.splitter, 1)

        # ---- always-visible target summary (where Apply will go)
        self.target = QLabel()
        self.target.setWordWrap(True)
        self.target.setTextFormat(Qt.TextFormat.RichText)
        target_row = QHBoxLayout()
        target_row.setSpacing(6)
        target_row.addWidget(self.target, 1)
        self.current_btn = QPushButton("Current style  ›")
        self.current_btn.setToolTip("Back to the style settings of this layer")
        self.current_btn.clicked.connect(self.show_current_style)
        target_row.addWidget(self.current_btn, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(target_row)

        # ---- actions
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.apply_btn = QPushButton(_icon("/mActionStyleManager.svg"), "Apply")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self.apply_current)
        actions.addWidget(self.apply_btn, 1)
        self.save_btn = QToolButton()
        self.save_btn.setIcon(_icon("/mActionFileSave.svg"))
        self.save_btn.setToolTip("Save this symbol (with your edits) to the QGIS Style "
                                 "Manager, tagged ‘Symbol Gallery’")
        self.save_btn.clicked.connect(self.save_current)
        actions.addWidget(self.save_btn)
        self.undo_btn = QToolButton()
        self.undo_btn.setIcon(_icon("/mActionUndo.svg"))
        self.undo_btn.clicked.connect(self.undo)
        actions.addWidget(self.undo_btn)
        self.more_btn = QToolButton()
        self.more_btn.setIcon(_icon("/mActionOptions.svg"))
        self.more_btn.setToolTip("More actions")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_btn)
        self.act_copy = self.more_menu.addAction(
            _icon("/mActionEditCopy.svg"), "Copy Symbol (paste in any symbol editor)",
            self.copy_current)
        self.more_menu.addSeparator()
        self.act_save_vis = self.more_menu.addAction(
            _icon("/mActionFileSave.svg"), "Save All Shown to Style Manager…",
            self.save_visible)
        self.act_export = self.more_menu.addAction(
            _icon("/mActionSharingExport.svg"), "Export Shown as Style XML…",
            self.export_visible)
        self.more_menu.addSeparator()
        self.more_menu.addAction(_icon("/mActionAdd.svg"),
                                 "Add Style Pack… (QGIS .xml or ArcGIS Pro .stylx)",
                                 self.add_pack)
        self.act_remove_pack = self.more_menu.addAction(
            _icon("/mActionRemove.svg"), "Remove This Style Pack", self.remove_pack)
        self.more_menu.addAction(_icon("/mActionFileOpen.svg"), "Open My Packs Folder",
                                 self.open_pack_folder)
        self.more_menu.addSeparator()
        self.act_keep = QAction("Whole layer: keep Categorized / Graduated classes",
                                self.more_menu)
        self.act_keep.setCheckable(True)
        self.act_keep.setToolTip("Applying to the whole layer keeps every class and its "
                                 "colour. When off, the layer becomes a single symbol.")
        self.act_keep.toggled.connect(
            lambda v: self.settings.setValue(SETTINGS + "keep_classes", v))
        self.more_menu.addAction(self.act_keep)
        self.act_class_colour = QAction("Classes: recolour with each class's own colour",
                                        self.more_menu)
        self.act_class_colour.setCheckable(True)
        self.act_class_colour.setToolTip("When styling classes selected in the Layers panel, "
                                         "keep each class's colour and take only the "
                                         "pattern/shape from the gallery symbol.")
        self.act_class_colour.toggled.connect(
            lambda v: self.settings.setValue(SETTINGS + "class_colour", v))
        self.more_menu.addAction(self.act_class_colour)
        self.more_menu.addSeparator()
        self.act_smart = QAction("Smart view: show colour ramps when a raster is selected",
                                 self.more_menu)
        self.act_smart.setCheckable(True)
        self.act_smart.setToolTip(
            "Selecting a raster switches the gallery to colour ramps (DEM → terrain, "
            "NDVI → remote sensing, depth → hydraulics…). Selecting a vector layer "
            "brings back the field you were browsing.")
        self.act_smart.toggled.connect(
            lambda v: self.settings.setValue(SETTINGS + "smart_raster", v))
        self.more_menu.addAction(self.act_smart)
        self.act_hillshade = QAction("DEM ramps: add hillshade automatically (RAS Mapper look)",
                                     self.more_menu)
        self.act_hillshade.setCheckable(True)
        self.act_hillshade.setToolTip(
            "When an elevation ramp is applied to a DEM, a soft multidirectional hillshade "
            "of the same file is added right above it (multiply blend), like HEC-RAS "
            "RAS Mapper. The DEM itself is not changed; Undo removes the hillshade.")
        self.act_hillshade.toggled.connect(
            lambda v: self.settings.setValue(SETTINGS + "auto_hillshade", v))
        self.more_menu.addAction(self.act_hillshade)
        self.act_compact = QAction("Compact layout: hide the symbol info card", self.more_menu)
        self.act_compact.setCheckable(True)
        self.act_compact.setToolTip("More room for thumbnails – symbol names are still "
                                    "shown under each thumbnail and as tooltips.")
        self.act_compact.toggled.connect(self._set_compact)
        self.more_menu.addAction(self.act_compact)
        self.act_popup = QAction("Open style settings after applying", self.more_menu)
        self.act_popup.setCheckable(True)
        self.act_popup.setVisible(not self._deferred)
        self.act_popup.setToolTip("After a symbol or DEM colour ramp is applied, open the "
                                  "Current style page to fine-tune it live on the map.")
        self.act_popup.toggled.connect(
            lambda on: self.set_dem_mode("after" if on else "direct"))
        self.more_menu.addAction(self.act_popup)
        self.act_hs_toggle = self.more_menu.addAction("Add Hillshade to This DEM",
                                                      self.toggle_hillshade)
        self.act_hs_opts = self.more_menu.addAction(
            _icon("/mActionOptions.svg"), "Hillshade Options… (method, strength, smooth)",
            self.hillshade_options)
        self.more_menu.addSeparator()
        self.more_menu.addAction("Clear Thumbnail Cache", self.clear_cache)
        self.more_menu.addAction("Credits & Licences…", self.show_credits)
        self.more_menu.aboutToShow.connect(self._update_menu)
        self.more_btn.setMenu(self.more_menu)
        actions.addWidget(self.more_btn)
        if self._deferred:
            # Layer Properties: Save / Undo / More sit at the right of the target line
            # (its own Apply / OK buttons apply the pick)
            self.apply_btn.setParent(root)
            for b in (self.save_btn, self.undo_btn, self.more_btn):
                target_row.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        else:
            lay.addLayout(actions)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        lay.addWidget(self.status)

        if self._deferred:
            # Layer Properties has its own Apply / OK buttons, which apply the pick
            self.apply_btn.setVisible(False)

        # page 2: "Current style" (the old quick-settings pop-ups live here)
        style_page = QWidget()
        style_page.setStyleSheet(STYLE_SHEET)
        sl = QVBoxLayout(style_page)
        sl.setContentsMargins(6, 6, 6, 6)
        sl.setSpacing(5)
        self.style_stack = QStackedWidget()
        sl.addWidget(self.style_stack, 1)
        self.status2 = QLabel()
        self.status2.setWordWrap(True)
        self.status2.setVisible(False)
        sl.addWidget(self.status2)

        self.pages = QStackedWidget()
        self.pages.addWidget(root)
        self.pages.addWidget(style_page)
        self._last_page = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.pages)
        self.setMinimumWidth(300)

    # ------------------------------------------------------------ settings
    def _restore_settings(self):
        s = self.settings
        for w in (self.size_slider, self.bg_combo, self.category, self.auto_apply,
                  self.act_keep):
            w.blockSignals(True)
        self.size_slider.setValue(int(s.value(SETTINGS + "icon_size", 96)))
        self.bg_combo.setCurrentIndex(max(0, self.bg_combo.findData(
            s.value(SETTINGS + "background", "light"))))
        self.auto_apply.setChecked(s.value(SETTINGS + "auto_apply", False, type=bool))
        self.act_keep.setChecked(s.value(SETTINGS + "keep_classes", True, type=bool))
        self.act_class_colour.setChecked(s.value(SETTINGS + "class_colour", False, type=bool))
        self.act_hillshade.setChecked(s.value(SETTINGS + "auto_hillshade", True, type=bool))
        self.act_smart.setChecked(s.value(SETTINGS + "smart_raster", True, type=bool))
        self.act_popup.blockSignals(True)
        self.act_popup.setChecked(self.dem_mode() == "after")
        self.act_popup.blockSignals(False)
        self.act_compact.setChecked(s.value(SETTINGS + "compact", False, type=bool))
        # open on a single field (never on the full "All categories" list)
        start = s.value(SETTINGS + "category", DEFAULT_CATEGORY) or DEFAULT_CATEGORY
        self._wanted_category = start      # may live in a library that loads a moment later
        self._fill_categories(start)
        for w in (self.size_slider, self.bg_combo, self.category, self.auto_apply,
                  self.act_keep):
            w.blockSignals(False)
        state = s.value(SETTINGS + "splitter")
        if state:
            self.splitter.restoreState(state)
        self._apply_grid_size()

    # ------------------------------------------------------------- loading
    def _load_extras(self):
        if self._extras_loaded:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            packs.reload_packs()
        except Exception as ex:  # pragma: no cover
            self._info("Could not load icon libraries: %s" % ex, "warning")
        finally:
            QApplication.restoreOverrideCursor()
        self._extras_loaded = True
        wanted = getattr(self, "_wanted_category", None)
        self._wanted_category = None
        current = self.category.currentData()
        # restore the remembered field if the user hasn't picked another one meanwhile
        target = wanted if (wanted and current in (DEFAULT_CATEGORY, "", None)) else current
        self._fill_categories(target)
        self._apply_filter()

    def _fill_categories(self, wanted=None):
        if wanted is None:
            wanted = self.category.currentData()
        counts = Counter(e.category for e in catalog.ENTRIES)
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All categories  (%d)" % len(catalog.ENTRIES), "")
        self.category.addItem("★  Favorites  (%d)" % sum(
            1 for i in self._favorites if catalog.has(i)), FAV)
        self.category.addItem("🕘  Recently used", RECENT)
        self.category.addItem("🎨  Raster colour ramps  (%d)" % sum(
            1 for e in catalog.ENTRIES if e.kind == RAMP), RAMPS)
        self.category.insertSeparator(self.category.count())
        fields = [c for c in catalog.CATEGORIES if not c.endswith("(pack)")]
        others = [c for c in catalog.CATEGORIES if c.endswith("(pack)")]
        for cat in fields:
            self.category.addItem("%s  (%d)" % (cat, counts[cat]), cat)
        if others:
            self.category.insertSeparator(self.category.count())
            for cat in others:
                self.category.addItem("%s  (%d)" % (cat, counts[cat]), cat)
        idx = self.category.findData(wanted) if wanted is not None else -1
        if idx < 0 and wanted != "":
            idx = self.category.findData(DEFAULT_CATEGORY)
        self.category.setCurrentIndex(max(0, idx))
        self.category.blockSignals(False)

    # --------------------------------------------------------- appearance
    def _apply_grid_size(self):
        px = self.size_slider.value()
        size = QSize(px, int(px * 0.68))
        self.view.setIconSize(size)
        fm = self.view.fontMetrics()
        self.view.setGridSize(QSize(px + 14, size.height() + fm.height() * 2 + 10))
        self.model.set_appearance(size, self.bg_combo.currentData(), self._dpr())

    def _on_appearance_changed(self, *_):
        self.settings.setValue(SETTINGS + "icon_size", self.size_slider.value())
        self.settings.setValue(SETTINGS + "background", self.bg_combo.currentData())
        self._apply_grid_size()
        self._update_detail()

    def _on_category_changed(self, *_):
        cat = self.category.currentData()
        if cat not in ("", None, RAMPS):   # "All" / raster view is never remembered
            self.settings.setValue(SETTINGS + "category", cat)
        self._apply_filter()

    def _on_match_toggled(self, *_):
        self._apply_filter()

    # -------------------------------------------------------------- filter
    def _current_kind(self):
        b = self.kind_group.checkedButton()
        return b.property("key") if b else "all"

    def _apply_filter(self):
        self._search_timer.stop()
        text = self.search.text().strip().lower()
        toks = text.split()
        cat = self.category.currentData()
        kind = self._current_kind()
        layer = self._layer if self.match_layer.isChecked() else None
        only_layer = self.match_layer.isChecked()
        fav = set(self._favorites) if cat == FAV else None
        if cat == RECENT:
            pool = [catalog.get(i) for i in self._recent if catalog.has(i)]
        else:
            pool = catalog.ENTRIES

        def collect(field):
            out = []
            for e in pool:
                if fav is not None and e.id not in fav:
                    continue
                if field == RAMPS:
                    if e.kind != RAMP:
                        continue
                elif field and field not in (FAV, RECENT) and e.category != field:
                    continue
                if kind != "all" and e.kind != kind:
                    continue
                if toks:
                    hay = e.haystack()
                    if not all(t in hay for t in toks):
                        continue
                if only_layer and not applier.check(e, layer)[0]:
                    continue
                out.append(e)
            return out

        rows = collect(cat)
        if cat == RAMPS and self._ramp_focus:   # ramps of the guessed field first
            rows.sort(key=lambda e: e.category != self._ramp_focus)
        if self._saved_view is not None and kind == RAMP and cat == "Terrain & Geology":
            # a DEM is selected: elevation ramps first, RAS Mapper style at the top
            rows.sort(key=lambda e: (not e.id.startswith("dem_rasmapper"),
                                     "relief" not in (e.tags or [])))
        widened = False
        if toks and not rows and cat not in VIRTUAL + (None,):
            rows = collect("")          # nothing in this field: search everywhere
            widened = bool(rows)
        current = self.current_entry()
        self.model.set_entries(rows)
        total = len(catalog.ENTRIES)
        if widened:
            self.count_label.setText("No match in this field – %d found in all fields"
                                     % len(rows))
        else:
            self.count_label.setText("%d of %d shown" % (len(rows), total)
                                     if len(rows) != total else "%d symbols" % total)
        self.view.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        if current is not None and self.model.row_of(current.id) >= 0:
            self._select_row(self.model.row_of(current.id), scroll=False)
        self._on_current_changed()

    def visible_entries(self):
        return self.model.entries()

    def _select_row(self, row, scroll=True):
        idx = self.model.index(row)
        self.view.selectionModel().setCurrentIndex(
            idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if scroll:
            self.view.scrollTo(idx)

    # -------------------------------------------------------------- detail
    def current_entry(self):
        idx = self.view.currentIndex() if hasattr(self, "view") else None
        if idx is None or not idx.isValid():
            return None
        return self.model.entry(idx.row())

    def current_object(self):
        """Edited symbol/ramp for the current entry, or a fresh one."""
        e = self.current_entry()
        if e is None:
            return None
        obj = self._edits.get(e.id)
        return obj.clone() if obj is not None else e.build()

    def _update_detail(self):
        e = self.current_entry()
        if e is None:
            self.preview.clear()
            return
        size = QSize(120, 80)
        bg = self.bg_combo.currentData()
        if e.id in self._edits:
            dpr = self._dpr()
            real = QSize(int(120 * dpr), int(80 * dpr))
            obj = self._edits[e.id]
            pm = (previews.ramp_pixmap(obj, real, bg) if e.is_ramp else
                  previews.symbol_pixmap(obj, real, bg, zoom=1.8 * dpr))
            pm.setDevicePixelRatio(dpr)
        else:
            pm = self.thumbs.get_now(e, size, bg, self._dpr())
        self.preview.setPixmap(pm)

    def _on_current_changed(self):
        e = self.current_entry()
        has = e is not None
        # the info card appears once a symbol is selected (and compact layout is off)
        compact = hasattr(self, "act_compact") and self.act_compact.isChecked()
        self.card.setVisible(has and not compact)
        self.save_btn.setEnabled(has)
        self.act_copy.setEnabled(has and not e.is_ramp)
        self.act_save_vis.setEnabled(bool(self.model.rowCount()))
        self.act_export.setEnabled(bool(self.model.rowCount()))
        if not has:
            self.title.setText("Pick a symbol")
            self.meta.setText("")
            self.desc.setText("Click a thumbnail to see and edit it. Double-click (or turn "
                              "on Auto-apply) to style the active layer or selected classes.")
            self.tags.setText("")
            self.preview.clear()
        else:
            edited = e.id in self._edits
            self.title.setText(e.name + ("  <span style='color:#b45309'>(edited)</span>"
                                         if edited else ""))
            self.title.setTextFormat(Qt.TextFormat.RichText)
            self.meta.setText("%s · %s" % (e.category, KIND_LABELS[e.kind]))
            self.desc.setText(e.description)
            self.tags.setText("Tags: " + ", ".join(e.tags))
            self._update_detail()
        self._sync_star(e)
        self._sync_editors()
        self._sync_props()
        self._update_apply_state()

    # ------------------------------------------------------------- editing
    def _sync_props(self):
        """Bind the properties panel to the symbol that will be applied."""
        e = self.current_entry()
        if e is None:
            self._draft = (None, None)
            return
        if e.is_ramp:
            self._draft = (None, None)
            return
        obj = self._edits.get(e.id)
        if obj is None:
            obj = e.build()
        self._draft = (e.id, obj)

    def _on_prop_changed(self):
        e = self.current_entry()
        eid, obj = self._draft
        if e is None or obj is None or eid != e.id:
            return
        self._edits[e.id] = obj          # the panel edits this very object
        self.title.setText(e.name + "  <span style='color:#b45309'>(edited)</span>")
        self.reset_btn.setEnabled(True)
        self._update_detail()
        self._sync_editors()
        if self.auto_apply.isChecked() and self._can_apply(e)[0]:
            self.apply_current(quiet=True)
    def _editable(self):
        """Ensure an editable copy exists for the current entry and return it."""
        e = self.current_entry()
        if e is None:
            return None, None
        if e.id not in self._edits:
            eid, draft = self._draft
            # share the object bound to the properties panel
            self._edits[e.id] = draft if (eid == e.id and draft is not None) else e.build()
        return e, self._edits[e.id]

    def _sync_editors(self):
        e = self.current_entry()
        widgets = (self.col_btn, self.out_btn, self.size_spin, self.edit_btn, self.reset_btn)
        for w in widgets:
            w.blockSignals(True)
        try:
            if e is None:
                for w in widgets:
                    w.setEnabled(False)
                return
            obj = self._edits.get(e.id)
            if obj is None:
                eid, draft = self._draft
                obj = draft if (eid == e.id and draft is not None) else e.build()
            self.edit_btn.setEnabled(True)
            self.reset_btn.setEnabled(e.id in self._edits)
            if e.is_ramp:
                for w in (self.col_btn, self.out_btn, self.size_spin):
                    w.setEnabled(False)
                return
            idx = applier._color_layer_index(e, obj)
            self.col_btn.setEnabled(True)
            self.col_btn.setColor(obj.symbolLayer(idx).color())
            stroke = None
            for i in range(obj.symbolLayerCount()):
                sl = obj.symbolLayer(i)
                if hasattr(sl, "strokeColor"):
                    stroke = sl.strokeColor()
                    break
            self.out_btn.setEnabled(stroke is not None)
            if stroke is not None:
                self.out_btn.setColor(stroke)
            if obj.type() == SYMBOL_MARKER:
                self.size_lbl.setText("Size")
                self.size_spin.setEnabled(True)
                self.size_spin.setValue(obj.size())
            elif obj.type() == SYMBOL_LINE:
                self.size_lbl.setText("Width")
                self.size_spin.setEnabled(True)
                self.size_spin.setValue(obj.width())
            else:
                self.size_lbl.setText("Size")
                self.size_spin.setEnabled(False)
            fill = obj.type() not in (SYMBOL_MARKER, SYMBOL_LINE)
            self.size_lbl.setVisible(not fill)
            self.size_spin.setVisible(not fill)
        finally:
            for w in widgets:
                w.blockSignals(False)

    def _after_edit(self):
        self._picked = True
        e = self.current_entry()
        self._on_current_changed()
        if e is not None and self.auto_apply.isChecked() and self._can_apply(e)[0]:
            self.apply_current(quiet=True)

    def _on_main_colour(self, colour):
        e, obj = self._editable()
        if obj is None or e.is_ramp:
            return
        obj.symbolLayer(applier._color_layer_index(e, obj)).setColor(QColor(colour))
        self._after_edit()

    def _on_outline_colour(self, colour):
        e, obj = self._editable()
        if obj is None or e.is_ramp:
            return
        for i in range(obj.symbolLayerCount()):
            sl = obj.symbolLayer(i)
            if hasattr(sl, "setStrokeColor"):
                sl.setStrokeColor(QColor(colour))
        self._after_edit()

    def _on_size_spin(self, value):
        e, obj = self._editable()
        if obj is None or e.is_ramp:
            return
        if obj.type() == SYMBOL_MARKER:
            obj.setSize(value)
        elif obj.type() == SYMBOL_LINE:
            obj.setWidth(value)
        self._after_edit()

    def edit_current(self):
        e = self.current_entry()
        if e is None:
            return
        obj = (self._edits.get(e.id) or e.build()).clone()
        if e.is_ramp:
            from qgis.gui import QgsGradientColorRampDialog
            if not hasattr(obj, "stops"):
                self._info("This ramp type can be edited in the Style Manager.", "info")
                return
            dlg = QgsGradientColorRampDialog(obj, self)
            try:
                if dlg.exec():
                    self._edits[e.id] = dlg.ramp().clone()
            finally:
                dlg.deleteLater()
            self._after_edit()
            return
        from qgis.gui import QgsSymbolSelectorDialog
        layer = self._layer if applier.check(e, self._layer)[0] else None
        dlg = QgsSymbolSelectorDialog(obj, QgsStyle.defaultStyle(), layer, self)
        dlg.setWindowTitle("Edit Symbol – %s" % e.name)
        try:
            accepted = dlg.exec()
        finally:
            # the dialog keeps a pointer to *obj*: destroy it before obj can go away
            dlg.setParent(None)
            dlg.deleteLater()
        if accepted:
            self._edits[e.id] = obj.clone()
            self._after_edit()
        self._keep_alive = obj      # released on next edit, after the dialog is gone

    def reset_edit(self):
        e = self.current_entry()
        if e is not None and self._edits.pop(e.id, None) is not None:
            self._on_current_changed()

    # ---------------------------------------------------------- favorites
    def _remember_recent(self, entry_id):
        if entry_id in self._recent:
            self._recent.remove(entry_id)
        self._recent.insert(0, entry_id)
        del self._recent[MAX_RECENT:]
        self.settings.setValue(SETTINGS + "recent", self._recent)

    def toggle_favorite(self):
        e = self.current_entry()
        if e is None:
            return
        if e.id in self._favorites:
            self._favorites.remove(e.id)
        else:
            self._favorites.append(e.id)
        self.settings.setValue(SETTINGS + "favorites", self._favorites)
        idx = self.category.findData(FAV)
        if idx >= 0:
            self.category.setItemText(idx, "★  Favorites  (%d)" % sum(
                1 for i in self._favorites if catalog.has(i)))
        self._sync_star(e)
        if self.category.currentData() == FAV:
            self._apply_filter()

    def _sync_star(self, e):
        on = e is not None and e.id in self._favorites
        self.fav_btn.setText("★" if on else "☆")
        self.fav_btn.setEnabled(e is not None)

    # ---------------------------------------------------- layer tracking
    def _tree_view(self):
        try:
            return self.iface.layerTreeView()
        except AttributeError:
            return None

    def _connect_layer_tracking(self):
        if self._pinned:
            QgsProject.instance().layerWillBeRemoved.connect(self._on_layer_removed)
            self._on_layer_changed(self._pin_layer)
            return
        try:
            self.iface.currentLayerChanged.connect(self._on_layer_changed)
        except (AttributeError, TypeError):
            pass
        QgsProject.instance().layerWillBeRemoved.connect(self._on_layer_removed)
        tv = self._tree_view()
        if tv is not None and tv.selectionModel() is not None:
            tv.selectionModel().selectionChanged.connect(self._on_tree_selection)
        self._on_layer_changed(self.iface.activeLayer())

    def _disconnect_layer_tracking(self):
        pairs = [(getattr(self.iface, "currentLayerChanged", None), self._on_layer_changed),
                 (QgsProject.instance().layerWillBeRemoved, self._on_layer_removed)]
        tv = self._tree_view()
        if tv is not None and tv.selectionModel() is not None:
            pairs.append((tv.selectionModel().selectionChanged, self._on_tree_selection))
        for sig, slot in pairs:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self._watch_renderer(None)

    def _watch_renderer(self, layer):
        old = self._layer
        if old is not None:
            try:
                old.rendererChanged.disconnect(self._on_renderer_changed)
            except (TypeError, RuntimeError, AttributeError):
                pass
        if layer is not None and hasattr(layer, "rendererChanged"):
            layer.rendererChanged.connect(self._on_renderer_changed)

    def _on_layer_changed(self, layer):
        if getattr(self, "_cleaned", False):
            return
        self._watch_renderer(layer)
        changed = (layer is not self._layer)
        self._layer = layer
        if changed:
            if not self._pinned and hasattr(self, "pages"):
                self.show_gallery()
            self._smart_view(layer)
        self._read_tree_target()
        if self.match_layer.isChecked():
            self._apply_filter()
        else:
            self._update_apply_state()

    # ------------------------------------------------- smart raster view
    def _set_view(self, category, kind):
        """Switch field + kind filter without remembering it as the user's choice."""
        self.category.blockSignals(True)
        idx = self.category.findData(category)
        if idx >= 0:
            self.category.setCurrentIndex(idx)
        self.category.blockSignals(False)
        for b in self.kind_group.buttons():
            if b.property("key") == kind:
                b.setChecked(True)
        self._apply_filter()

    def _smart_view(self, layer):
        """Selecting a raster opens the colour ramps (once per selection); going back
        to a vector layer restores the field and kind that were shown before."""
        if not hasattr(self, "act_smart"):
            return
        is_raster = isinstance(layer, QgsRasterLayer)
        if is_raster and self.act_smart.isChecked():
            if self._saved_view is None:       # remember the vector view once
                self._saved_view = (self.category.currentData(), self._current_kind())
            field = guess_raster_field(layer)
            self._ramp_focus = field
            # a field with its own ramps (DEM -> Terrain, NDVI -> Remote Sensing…);
            # otherwise every ramp, with nothing hidden
            has = field and any(e.kind == RAMP and e.category == field
                                for e in catalog.ENTRIES)
            self._set_view(field if has else RAMPS, RAMP)
            what = ("%s ramps" % field) if has else "all colour ramps"
            self.target.setToolTip("Raster selected – showing %s." % what)
        elif not is_raster and layer is not None and self._saved_view is not None:
            cat, kind = self._saved_view
            self._saved_view = None
            self._ramp_focus = None
            self._set_view(cat, kind)

    def _on_layer_removed(self, layer_id):
        if getattr(self, "_cleaned", False):
            return
        self._history = [h for h in self._history if h[0] != layer_id]
        for name in ("_dem_pop", "_sym_pop"):
            pop = getattr(self, name, None)
            if pop is not None and pop.layer is not None:
                try:
                    if pop.layer.id() == layer_id:
                        pop.layer = None
                        pop.dirty = False
                        self.show_gallery()
                except RuntimeError:
                    pass
        if self._layer is not None and self._layer.id() == layer_id:
            self._watch_renderer(None)
            self._layer = None
            self._target_keys = []
            self._update_apply_state()
        self._update_undo()

    def _on_renderer_changed(self):
        if getattr(self, "_cleaned", False):
            return
        self._read_tree_target()
        if self.match_layer.isChecked():
            self._apply_filter()
        else:
            self._update_apply_state()

    # ------------------------------------------------------ smart target
    def _mode(self):
        """'classes' when legend classes are selected in the Layers panel, else 'layer'."""
        return "classes" if self._target_keys else "layer"

    def selected_class_keys(self):
        return list(self._target_keys)

    def _legend_nodes(self, tv):
        try:
            return list(tv.selectedLegendNodes())
        except AttributeError:  # pragma: no cover - QGIS < 3.32
            node = tv.currentLegendNode()
            return [node] if node is not None else []

    def _read_tree_target(self):
        """Work out the target from the Layers panel selection:
        a layer -> whole layer, legend classes -> just those classes."""
        keys = []
        tv = None if self._pinned else self._tree_view()
        valid = {}
        if tv is not None and self._layer is not None:
            valid = {k: lbl for k, lbl, _c in applier.class_keys(self._layer)}
            self._class_labels = valid
            for n in self._legend_nodes(tv):
                try:
                    if n.layerNode().layerId() != self._layer.id():
                        continue
                except AttributeError:
                    continue
                k = n.data(LEGEND_RULE_KEY_ROLE)
                if k and k in valid and k not in keys:
                    keys.append(k)
            if not keys and self._target_keys and not self._tree_has_selection(tv):
                # the selection vanished because QGIS rebuilt the legend (e.g. after we
                # styled the classes) – that is not a user choice: keep the classes
                kept = [k for k in self._target_keys if k in valid]
                if kept:
                    keys = kept
                    self._queue_reselect(self._layer, list(kept))
        self._target_keys = keys
        self._update_apply_state()

    @staticmethod
    def _tree_has_selection(tv):
        try:
            return tv.selectionModel() is not None and tv.selectionModel().hasSelection()
        except (AttributeError, RuntimeError):
            return False

    def _on_tree_selection(self, *_):
        if getattr(self, "_cleaned", False):
            return
        if not self._syncing:
            self._read_tree_target()

    def _queue_reselect(self, layer, keys):
        """Coalesce re-highlighting into one deferred call (after QGIS rebuilt the legend)."""
        self._reselect_args = (layer, list(keys))
        if not getattr(self, "_reselect_pending", False):
            self._reselect_pending = True
            QTimer.singleShot(0, self._run_reselect)

    def _run_reselect(self):
        self._reselect_pending = False
        if getattr(self, "_cleaned", False):
            return
        args, self._reselect_args = getattr(self, "_reselect_args", None), None
        if args:
            self._reselect_tree(*args)

    def _reselect_tree(self, layer, keys):
        """Re-select legend classes after QGIS rebuilt the legend (keeps the target)."""
        tv = self._tree_view()
        if tv is None or not keys or layer is None:
            return
        with suppress(Exception):  # pragma: no cover - never break applying
            if QgsProject.instance().mapLayer(layer.id()) is None:
                return
            from qgis.PyQt.QtCore import QItemSelection
            model = tv.layerTreeModel()
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is None:
                return
            sel = QItemSelection()
            for ln in model.layerLegendNodes(node):
                if ln.data(LEGEND_RULE_KEY_ROLE) in keys:
                    idx = model.legendNode2index(ln)
                    try:
                        idx = tv.proxyModel().mapFromSource(idx)
                    except AttributeError:
                        pass
                    if idx.isValid():
                        sel.select(idx, idx)
            if not sel.isEmpty():
                self._syncing = True
                try:
                    tv.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                finally:
                    self._syncing = False

    # ------------------------------------------------------- apply state
    def _can_apply(self, e):
        if e is None:
            return False, "Select a symbol first."
        ok, reason = applier.check(e, self._layer)
        if not ok:
            return ok, reason
        if self._mode() == "classes" and e.is_ramp:
            return False, ("Colour ramps style the whole layer – select the layer (not a "
                           "class) in the Layers panel.")
        return True, reason

    def _update_apply_state(self):
        e = self.current_entry()
        layer = self._layer
        mode = self._mode()
        if layer is None:
            head = ("<span style='color:gray'>Select a layer – or classes of a layer – in "
                    "the Layers panel.</span>")
        elif mode == "classes":
            labels = [self._class_labels.get(k, "?") for k in self._target_keys]
            shown = ", ".join(_escape(x) for x in labels[:3])
            if len(labels) > 3:
                shown += " … +%d" % (len(labels) - 3)
            head = ("Target: <b>%d class%s</b> of <b>%s</b> – %s"
                    % (len(labels), "" if len(labels) == 1 else "es",
                       _escape(layer.name()), shown))
        else:
            head = "Target: <b>%s</b> <span style='color:gray'>(whole %s layer)</span>" % (
                _escape(layer.name()), applier.geometry_word(layer))
        ok, reason = self._can_apply(e)
        self.current_btn.setVisible(self._current_available())
        self.apply_btn.setEnabled(ok)
        if mode == "classes":
            n = len(self._target_keys)
            self.apply_btn.setText("Apply to %d Class%s" % (n, "" if n == 1 else "es"))
        else:
            self.apply_btn.setText("Apply to Layer")
        self.apply_btn.setToolTip(reason or "")
        if e is not None and reason:
            colour = "gray" if ok else "#b45309"
            head += "<br><span style='color:%s'>%s</span>" % (colour, _escape(reason))
        self.target.setText(head)
        self._update_undo()

    def _update_undo(self):
        self.undo_btn.setEnabled(bool(self._history))
        if self._history:
            lid, name = self._history[-1][0], self._history[-1][2]
            lyr = QgsProject.instance().mapLayer(lid)
            self.undo_btn.setToolTip("Undo '%s' on '%s'" % (name, lyr.name() if lyr else "?"))
        else:
            self.undo_btn.setToolTip("Nothing to undo")

    # ------------------------------------------------------------- actions
    def _bar(self):
        try:
            return self.iface.messageBar()
        except AttributeError:
            return None

    def _info(self, msg, level="success", duration=None):
        if self._pinned:
            colour = {"success": "#15803d", "warning": "#b45309", "critical": "#b91c1c"}.get(level)
            for lbl in (self.status, self.status2):
                lbl.setStyleSheet("color: %s;" % colour if colour else "")
                lbl.setText(msg)
                lbl.setVisible(True)
            return
        bar = self._bar()
        if bar is None:
            return
        fn = {"success": bar.pushSuccess, "warning": bar.pushWarning,
              "info": bar.pushInfo, "critical": bar.pushCritical}[level]
        fn("KGA Layer Symbol", msg)

    def _mark_picked(self):
        self._picked = True
        self.notify_pending()

    def notify_pending(self):
        if self._deferred:
            self.changed.emit()

    def _on_clicked(self, index):
        if self.auto_apply.isChecked() and index.isValid():
            e = self.model.entry(index.row())
            if e is not None and self._can_apply(e)[0]:
                self.apply_current(quiet=True)

    def apply_current(self, quiet=False):
        self._picked = False
        e = self.current_entry()
        layer = self._layer
        if e is None:
            return
        ok, reason = self._can_apply(e)
        if not ok:
            if not quiet:
                self._info(reason, "warning")
            return
        dem = e.is_ramp and self._mode() != "classes" and applier.is_dem(layer)
        if self._deferred and (not e.is_ramp or dem):
            # Layer Properties: adjust first; its Apply / OK button applies the result
            self._open_page(e, layer, dem, dirty=True)
            self.notify_pending()
            return
        if not e.is_ramp:
            self._apply_symbol_flow(e, layer, quiet)
            return
        mode = self.dem_mode() if dem else "direct"
        if self._apply_now(e, layer, quiet) and mode == "after":
            self._dem_popup().open_for(e, layer, "after")

    def _apply_symbol_flow(self, e, layer, quiet):
        """Point / line / polygon symbols: same Current style page as DEM ramps."""
        mode = self.dem_mode()
        if self._apply_now(e, layer, quiet) and mode == "after":
            self._sym_popup().open_for(e, layer, "after")

    def _open_page(self, e, layer, dem, dirty):
        """Show the Current style page in "before" mode (nothing changes until commit)."""
        if dem:
            self._dem_popup().open_for(e, layer, "before", dirty=dirty)
        else:
            self._sym_popup().open_for(e, layer, "before", dirty=dirty)

    # ------------------------------------------------------- pages
    def _sym_popup(self):
        pop = getattr(self, "_sym_pop", None)
        if pop is None:
            from .symbol_popup import SymbolQuickPopup
            pop = self._sym_pop = SymbolQuickPopup(self)
            pop.show_next.setVisible(not self._deferred)
            self.style_stack.addWidget(pop)
        return pop

    def show_style_page(self, page):
        self._last_page = page
        self.style_stack.setCurrentWidget(page)
        self.pages.setCurrentIndex(1)

    def show_gallery(self):
        page = self._style_page()
        if page is not None:
            page.flush()
        self.pages.setCurrentIndex(0)
        self._update_apply_state()

    def _style_page(self):
        """The Current style page being shown, or None when the gallery is."""
        if self.pages.currentIndex() != 1:
            return None
        return self.style_stack.currentWidget()

    @staticmethod
    def _single_symbol(layer):
        from qgis.core import QgsSingleSymbolRenderer, QgsVectorLayer
        if not isinstance(layer, QgsVectorLayer):
            return False
        r = layer.renderer()
        return isinstance(r, QgsSingleSymbolRenderer) and r.symbol() is not None

    def _current_available(self):
        layer, lp = self._layer, self._last_page
        if layer is None:
            return False
        return self._single_symbol(layer) or (lp is not None and lp.layer is layer)

    def show_current_style(self):
        """Back to the style settings of the layer, without picking a style again."""
        layer, lp = self._layer, self._last_page
        if lp is not None and lp.layer is layer and (lp.dirty or not self._single_symbol(layer)):
            self.show_style_page(lp)        # keep what was being edited
        elif not self.open_current_style() and lp is not None and lp.layer is layer:
            self.show_style_page(lp)

    def open_current_style(self):
        """Open on the layer's own symbol (single-symbol vector layers); False otherwise."""
        layer = self._layer
        if not self._single_symbol(layer):
            return False
        sym = layer.renderer().symbol().clone()
        kind = {SYMBOL_MARKER: MARKER, SYMBOL_LINE: LINE}.get(sym.type(), FILL)
        entry = Entry(CURRENT_ID, "Current style", "", kind, [], "", lambda: sym.clone())
        self._current_entry = entry
        if self._deferred:
            self._sym_popup().open_for(entry, layer, "before")
        else:
            # live editing: keep the original so Undo can bring it back
            if not (self._history and self._history[-1][0] == layer.id()
                    and self._history[-1][2] == "Current style"):
                self._history.append((layer.id(), layer.renderer().clone(),
                                      "Current style", []))
                del self._history[:-30]
                self._update_undo()
            self._sym_popup().open_for(entry, layer, "after")
        return True

    def apply_picked(self):
        """The Apply / OK of Layer Properties: commit what is on the Current style page,
        or apply the symbol picked in the gallery (and show it on that page)."""
        page = self._style_page()
        if page is not None:
            return page.commit()
        layer = self._layer
        e = self.current_entry()
        if self._picked and e is not None and layer is not None:
            self._picked = False
            ok, reason = self._can_apply(e)
            if not ok:
                self._info(reason, "warning")
                return False
            dem = e.is_ramp and applier.is_dem(layer)
            if not self._apply_now(e, layer):
                return False
            if not e.is_ramp or dem:
                self._open_page(e, layer, dem, dirty=False)
            return True
        if self._last_page is not None:
            return self._last_page.commit()
        return False

    # helpers used by the symbol pop-up (it edits its own copy, never the gallery's)
    def gallery_symbol(self, e):
        """The symbol as the gallery would apply it (with the user's gallery edits)."""
        if e.id in self._edits:
            return self._edits[e.id]
        eid, draft = self._draft
        if eid == e.id and draft is not None:
            return draft
        return e.build()

    @staticmethod
    def is_classified(layer):
        from qgis.core import (QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer,
                               QgsRuleBasedRenderer)
        try:
            return isinstance(layer.renderer(), (QgsCategorizedSymbolRenderer,
                                                 QgsGraduatedSymbolRenderer,
                                                 QgsRuleBasedRenderer))
        except AttributeError:
            return False

    def class_colours_default(self):
        if self._mode() == "classes":
            return self.act_class_colour.isChecked()
        return self._whole_class_colours

    def set_class_colours_default(self, on):
        if self._mode() == "classes":
            self.act_class_colour.setChecked(bool(on))
        else:
            self._whole_class_colours = bool(on)

    def reapply(self, e, layer, symbol=None, class_colours=None):
        """Live update from a pop-up: replace the last application, no new Undo step."""
        if layer is None or QgsProject.instance().mapLayer(layer.id()) is None:
            return False
        base = None          # start again from the style the layer had before
        for item in reversed(self._history):
            if item[0] == layer.id():
                base = item[1]
                break
        return self._apply_now(e, layer, True, record=False, symbol=symbol,
                               class_colours=class_colours, base=base)

    def apply_entry_now(self, e, layer, symbol=None, class_colours=None):
        if layer is None or QgsProject.instance().mapLayer(layer.id()) is None:
            return False
        return self._apply_now(e, layer, False, symbol=symbol, class_colours=class_colours)

    def target_text(self):
        layer = self._layer
        if layer is None:
            return "–"
        if self._mode() == "classes":
            n = len(self._target_keys)
            return "%d class%s of <b>%s</b>" % (n, "" if n == 1 else "es", layer.name())
        return "<b>%s</b>" % layer.name()

    # ------------------------------------------------- DEM quick settings
    def _set_compact(self, on):
        """Compact layout: the info card is hidden, thumbnails get all the room."""
        self.settings.setValue(SETTINGS + "compact", bool(on))
        self.card.setVisible(not on and self.current_entry() is not None)

    def dem_mode(self):
        m = self.settings.value(SETTINGS + "dem_popup", "after")
        # "before" (adjust-then-apply) was merged into "after" in 2.10.1
        return "direct" if m == "direct" else "after"

    def set_dem_mode(self, mode):
        mode = "direct" if mode == "direct" else "after"
        self.settings.setValue(SETTINGS + "dem_popup", mode)
        if hasattr(self, "act_popup") and self.act_popup.isChecked() != (mode == "after"):
            self.act_popup.blockSignals(True)
            self.act_popup.setChecked(mode == "after")
            self.act_popup.blockSignals(False)

    def _dem_popup(self):
        pop = getattr(self, "_dem_pop", None)
        if pop is None:
            from .dem_popup import DemQuickPopup
            pop = self._dem_pop = DemQuickPopup(self)
            pop.show_next.setVisible(not self._deferred)
            self.style_stack.addWidget(pop)
        return pop

    def apply_dem_ramp_now(self, entry, layer):
        """Called by the pop-up's Apply button ("before" mode)."""
        if QgsProject.instance().mapLayer(layer.id()) is None:
            return False
        return self._apply_now(entry, layer, False)

    def note_added_layer(self, layer, added):
        """A hillshade added from the pop-up is removed by the same Undo step."""
        for item in reversed(self._history):
            if item[0] == layer.id():
                if len(item) > 3:
                    item[3].append(added.id())
                break

    def _apply_now(self, e, layer, quiet=False, record=True, symbol=None,
                   class_colours=None, base=None):
        override = symbol if symbol is not None else self._edits.get(e.id)
        if class_colours is None:
            class_colours = self.class_colours_default()
        try:
            if self._mode() == "classes":
                msg, previous = applier.apply_to_classes(
                    e, layer, self.selected_class_keys(),
                    override.clone() if override is not None else None,
                    class_colours, base=base)
            elif e.is_ramp and override is not None:
                msg, previous = self._apply_edited_ramp(e, layer, override)
            else:
                msg, previous = applier.apply(e, layer, self.act_keep.isChecked(),
                                              override.clone() if override is not None
                                              else None, class_colours=class_colours,
                                              base=base)
        except applier.ApplyError as ex:
            self._info(str(ex), "warning")
            return False
        except Exception as ex:  # unexpected – report, never crash QGIS
            self._info("Could not apply '%s': %s" % (e.name, ex), "critical")
            return False
        added = []
        if (previous is not None and record and self.act_hillshade.isChecked()
                and applier.wants_hillshade(e) and applier.is_dem(layer)
                and applier.hillshade_for(layer) is None):
            with suppress(Exception):  # the ramp itself was applied – a hillshade is a bonus
                added.append(applier.add_hillshade(layer).id())
                msg += (" Hillshade added for the RAS Mapper look (Undo removes it; "
                        "⋯ ▸ Hillshade Options to change it).")
        if previous is not None and record:
            self._history.append((layer.id(), previous, e.name, added))
            if self._last_page is not None and self._last_page.layer is layer                     and not self._last_page.dirty:
                self._last_page = None      # the layer changed under it
            del self._history[:-30]
        keys = list(self._target_keys)
        if keys:                       # legend was rebuilt: keep the same classes targeted
            self._target_keys = keys
            self._queue_reselect(layer, keys)
        if record and e.id != CURRENT_ID:
            self._remember_recent(e.id)
        if not quiet:
            self._info(msg)
        self.target.setToolTip(msg)
        self._update_apply_state()
        return True

    def _apply_edited_ramp(self, e, layer, ramp):
        class _E:                       # tiny adapter: same entry, edited ramp
            pass
        proxy = _E()
        for a in ("id", "name", "category", "kind", "tags", "description", "color_layer"):
            setattr(proxy, a, getattr(e, a))
        proxy.is_ramp = True
        proxy.build = lambda: ramp.clone()
        return applier.apply(proxy, layer)

    def undo(self):
        while self._history:
            item = self._history.pop()
            lid, renderer, name = item[:3]
            layer = QgsProject.instance().mapLayer(lid)
            for extra in (item[3] if len(item) > 3 else []):
                if QgsProject.instance().mapLayer(extra) is not None:
                    QgsProject.instance().removeMapLayer(extra)
            if layer is None:
                continue
            keys = list(self._target_keys)
            applier.restore(layer, renderer)
            if keys:
                self._target_keys = keys
                self._queue_reselect(layer, keys)
            self._info("Restored the previous style of '%s'." % layer.name(), "info")
            self._last_page = None
            break
        self._update_apply_state()

    def _refresh_legend(self, layer):
        tv = self._tree_view()
        if tv is not None:
            try:
                tv.refreshLayerSymbology(layer.id())
            except AttributeError:
                pass

    def save_current(self):
        e = self.current_entry()
        if e is None:
            return
        if e.id in self._edits:
            style = QgsStyle.defaultStyle()
            name = applier.style_name(e) + " (edited)"
            if e.is_ramp:
                style.addColorRamp(name, self._edits[e.id].clone(), True)
                etype = STYLE_RAMP
            else:
                style.addSymbol(name, self._edits[e.id].clone(), True)
                etype = STYLE_SYMBOL
            style.tagSymbol(etype, name, [applier.STYLE_TAG, e.category])
            self._info("'%s' saved in Style Manager." % name)
            return
        _added, updated = applier.save_to_style([e])
        self._info("'%s' %s in Style Manager (tag: %s)." % (
            applier.style_name(e), "updated" if updated else "saved", applier.STYLE_TAG))

    def save_visible(self):
        entries = self.visible_entries()
        if not entries:
            return
        if QMessageBox.question(
                self, "Save to Style Manager",
                "Save %d symbols/ramps to your QGIS Style Manager?\n\nThey will be named "
                "‘%s…’ and tagged ‘%s’." % (len(entries), applier.STYLE_PREFIX,
                                            applier.STYLE_TAG)) != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            added, updated = applier.save_to_style(entries)
        finally:
            QApplication.restoreOverrideCursor()
        self._info("Style Manager: %d added, %d updated (tag: %s)." % (
            added, updated, applier.STYLE_TAG))

    def export_visible(self):
        entries = self.visible_entries()
        if not entries:
            return
        last = self.settings.value(SETTINGS + "last_export", "symbol_gallery.xml")
        path, _ = QFileDialog.getSaveFileName(self, "Export Symbols", last,
                                              "QGIS style XML (*.xml)")
        if not path:
            return
        if not path.lower().endswith(".xml"):
            path += ".xml"
        try:
            applier.export_xml(entries, path)
        except Exception as ex:
            self._info("Export failed: %s" % ex, "critical")
            return
        self.settings.setValue(SETTINGS + "last_export", path)
        self._info("Exported %d items to %s." % (len(entries), path))

    def copy_current(self):
        obj = self.current_object()
        e = self.current_entry()
        if obj is None or e.is_ramp:
            return
        QApplication.clipboard().setMimeData(QgsSymbolLayerUtils.symbolToMimeData(obj))
        self._info("Symbol copied – paste it in any symbol editor or via the layer's "
                   "Styles ▸ Paste Symbol.", "info")

    def clear_cache(self):
        import shutil
        from ..core.thumbs import cache_dir
        d = cache_dir()
        self.thumbs.clear_queue()
        self.thumbs.clear_memory()
        if d and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        self.thumbs._dir = cache_dir()
        self.model.set_appearance(QSize(1, 1), "", 0)   # force reset
        self._apply_grid_size()
        self._info("Thumbnail cache cleared.", "info")

    def show_credits(self):
        from ..core import library
        lines = ["<b>Symbol Gallery</b> – built-in symbols are original work (GPL-2.0+).",
                 "<br><b>Bundled icon libraries</b>:"]
        for m in library.libraries():
            lines.append("• %s – %d icons – %s, %s %s" % (
                _escape(m["title"]), m["count"], _escape(m["source"]),
                _escape(m["licence"]), ("<a href='%s'>%s</a>" % (m["url"], m["url"])
                                        if m["url"] else "")))
        box = QMessageBox(self)
        box.setWindowTitle("Credits & Licences")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText("<br>".join(lines))
        box.exec()

    # -------------------------------------------------------------- packs
    def _update_menu(self):
        dem = applier.is_dem(self._layer)
        self.act_hs_toggle.setVisible(dem)
        self.act_hs_opts.setVisible(dem)
        if dem:
            has = applier.hillshade_for(self._layer) is not None
            self.act_hs_toggle.setText("Remove Hillshade from '%s'" % self._layer.name() if has
                                       else "Add Hillshade to '%s' (RAS Mapper look)"
                                       % self._layer.name())
        cat = self.category.currentData()
        self.act_remove_pack.setVisible(bool(cat) and cat not in (FAV, RECENT)
                                        and packs.is_user_pack(cat))

    def hillshade_options(self):
        layer = self._layer
        if not applier.is_dem(layer):
            self._info("Select a single-band elevation raster (DEM) first.", "warning")
            return
        from .hillshade_dialog import HillshadeDialog
        old = getattr(self, "_hs_dialog", None)
        if old is not None:
            try:
                old.close()
                old.deleteLater()
            except RuntimeError:
                pass
        dlg = HillshadeDialog(layer, self.iface, self, info=self._info)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda *_: setattr(self, "_hs_dialog", None))
        self._hs_dialog = dlg
        dlg.show()

    def toggle_hillshade(self):
        layer = self._layer
        if not applier.is_dem(layer):
            self._info("Select a single-band elevation raster (DEM) first.", "warning")
            return
        if applier.remove_hillshade(layer):
            self._info("Hillshade removed from '%s'." % layer.name(), "info")
            return
        try:
            hs = applier.add_hillshade(layer)
        except Exception as ex:
            self._info(str(ex), "warning")
            return
        self._info("Added '%s' above the DEM (multiply blend, 50 %% opacity – change it in "
                   "its Layer Properties)." % hs.name())

    def add_pack(self):
        last = self.settings.value(SETTINGS + "last_pack_dir", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Add Style Pack", last,
            "Style libraries (*.xml *.stylx);;QGIS style XML (*.xml);;"
            "ArcGIS Pro style (*.stylx);;ArcMap style (*.style)")
        if not path:
            return
        self.settings.setValue(SETTINGS + "last_pack_dir", os.path.dirname(path))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            title, added, skipped = packs.add_pack(path)
            packs.reload_packs()
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Add Style Pack", str(ex))
            return
        QApplication.restoreOverrideCursor()
        self._fill_categories(title)
        self._apply_filter()
        msg = "Added pack '%s' with %d items." % (title, added)
        if skipped:
            msg += " %d items could not be converted and were skipped." % skipped
        self._info(msg)

    def remove_pack(self):
        cat = self.category.currentData()
        if not cat or not packs.is_user_pack(cat):
            return
        if QMessageBox.question(self, "Remove Style Pack",
                                "Remove the pack '%s' from the gallery?\n\nSymbols you "
                                "already applied or saved are not affected." % cat
                                ) != QMessageBox.StandardButton.Yes:
            return
        packs.remove_user_pack(cat)
        packs.reload_packs()
        self._fill_categories("")
        self._apply_filter()
        self._info("Pack '%s' removed." % cat, "info")

    def open_pack_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(packs.user_pack_dir(create=True)))

    # ------------------------------------------------------------ menus
    def _context_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        self._select_row(idx.row(), scroll=False)
        e = self.current_entry()
        m = QMenu(self.view)
        a = m.addAction(_icon("/mActionStyleManager.svg"), self.apply_btn.text(),
                        self.apply_current)
        a.setEnabled(self.apply_btn.isEnabled())
        m.addAction(_icon("/mActionStyleManager.svg"),
                    "Edit Colour Ramp Before Applying…" if (e is not None and e.is_ramp)
                    else "Edit Symbol Before Applying…", self.edit_current)
        if e is not None and e.id in self._edits:
            m.addAction(_icon("/mActionUndo.svg"), "Reset to Original Design",
                        self.reset_edit)
        m.addAction(_icon("/mActionFileSave.svg"), "Save to Style Manager", self.save_current)
        c = m.addAction(_icon("/mActionEditCopy.svg"), "Copy Symbol", self.copy_current)
        c.setEnabled(e is not None and not e.is_ramp)
        m.addSeparator()
        fav_on = e is not None and e.id in self._favorites
        m.addAction("★ Remove from Favorites" if fav_on else "☆ Add to Favorites",
                    self.toggle_favorite)
        m.exec(self.view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ cleanup
    def closeEvent(self, event):
        self.settings.setValue(SETTINGS + "splitter", self.splitter.saveState())
        super().closeEvent(event)

    def cleanup(self):
        """Disconnect every signal from QGIS objects (safe to call more than once)."""
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        for name in ("_hs_dialog", "_dem_pop", "_sym_pop"):
            dlg = getattr(self, name, None)
            if dlg is not None:
                try:
                    dlg.close()
                except RuntimeError:
                    pass
        try:
            QgsApplication.instance().aboutToQuit.disconnect(self.cleanup)
        except (TypeError, RuntimeError):
            pass
        try:
            self.settings.setValue(SETTINGS + "splitter", self.splitter.saveState())
        except RuntimeError:
            pass
        self.thumbs.clear_queue()
        self._disconnect_layer_tracking()
        self._layer = None
        self._history = []
        self._edits = {}
        self.thumbs.clear_memory()


def _escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

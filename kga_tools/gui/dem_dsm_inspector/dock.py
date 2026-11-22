"""
dock.py - The DEM / DSM Inspector dock panel (QGIS 3.44+ / Qt5 and QGIS 4.x / Qt6).

Layout (top to bottom)
    header . input layer(s) . options (collapsible) . run / cancel
    verdict card (classification + confidence)
    tabs: Report | Indicators | Log
    export buttons
"""

import math
import os
import re
import tempfile

from qgis.core import (
    Qgis, QgsApplication, QgsTask, QgsVectorLayer, QgsColorRampShader, QgsCoordinateTransform, QgsProject,
    QgsPalettedRasterRenderer, QgsProviderRegistry, QgsRasterLayer, QgsRasterShader, QgsSettings,
    QgsSingleBandPseudoColorRenderer, QgsVectorFileWriter,
)
from qgis.gui import QgsDockWidget, QgsMapLayerComboBox, QgsRasterBandComboBox
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont, QTextDocument
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QLineEdit, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ...branding import help_button
from ..panel_bubble import ToolbarReveal
from ...core.dem_dsm_inspector import report
from ...core.dem_dsm_inspector.task import InspectTask
from .filter_tab import FilterTab
from .nav import SideNav
from .block_inspector import (LABEL_STYLE, BlockInspector, BlockPickTool, build_grid_layer,
                              build_peaks_layer)

SETTINGS = "KGA/dem_dsm_inspector/"

VERDICT_COLOURS = {  # text, background
    "DSM": ("#92400E", "#FEF3C7"),
    "DEM": ("#065F46", "#D1FAE5"),
    "INCONCLUSIVE": ("#374151", "#E5E7EB"),
    "PARTIAL": ("#5B21B6", "#EDE9FE"),
    "Partial": ("#5B21B6", "#EDE9FE"),
    "Neutral": ("#374151", "#F3F4F6"),
    "N/A": ("#6B7280", "#F9FAFB"),
}

STYLE = """
#runBtn     { background: #2563EB; color: white; font-weight: 600; padding: 7px 14px;
              border-radius: 5px; border: 1px solid transparent; }
#runBtn:hover    { background: #1D4ED8; }
#runBtn:disabled { background: #93C5FD; }
#verdictCard { border-radius: 8px; }
#verdictCls  { font-size: 20pt; font-weight: 700; }
#verdictConf { font-size: 20pt; font-weight: 700; }
#verdictCap  { font-size: 7pt; font-weight: 700; letter-spacing: 1px; }
#tabIntro    { color: palette(mid); font-size: 8pt; }
#footer      { background: transparent; border: none; border-top: 1px solid palette(midlight); }
#chip        { font-size: 9pt; }
#runProgress { background: palette(midlight); border: none; border-radius: 2px; }
#runProgress::chunk { background: #2563EB; border-radius: 2px; }
#cancelBtn, #helpBtn { padding: 7px 14px; border: 1px solid palette(mid); border-radius: 5px;
               background: transparent; }
#cancelBtn:hover, #helpBtn:hover { border-color: #B91C1C; color: #B91C1C; }
#cancelBtn:disabled { color: palette(mid); border-color: palette(midlight); }
#exportBtn   { padding: 3px 4px; }
#presetBtn   { padding: 3px 6px; font-size: 8pt; }
"""


def _vector_source(layer):
    """(path, layer_name) of a polygon layer that GDAL can read.  Non-file
    layers (memory, database) are exported to a temporary GeoPackage."""
    if layer is None:
        return None, None
    if layer.providerType() == "ogr":
        parts = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
        if parts.get("path") and os.path.isfile(parts["path"]):
            return parts["path"], parts.get("layerName") or None
    out = os.path.join(tempfile.mkdtemp(prefix="dem_dsm_bldg_"), "buildings.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, out, QgsProject.instance().transformContext(), opts)
    return out, None


def _file_path(layer):
    """Local file path of a GDAL raster layer, or None."""
    if layer is None or layer.providerType() != "gdal":
        return None
    parts = QgsProviderRegistry.instance().decodeUri("gdal", layer.source())
    path = parts.get("path") or layer.source()
    return path if (os.path.isfile(path) or path.startswith("/vsi")) else None


class InspectorDock(ToolbarReveal, QgsDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("DEM / DSM Inspector", parent)
        self.iface = iface
        self.task = None
        self.result = None
        self.setObjectName("DemDsmInspectorDock")
        self._build()
        self._load_settings()

    # ==================================================================
    # UI construction
    # ==================================================================
    def _build(self):
        root = QWidget()
        root.setStyleSheet(STYLE)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Side navigation: SET UP pages and RESULTS pages, one page at a time
        self.nav = SideNav()
        outer.addWidget(self.nav, 1)
        self.setup_tabs = self.nav.section("SET UP")

        def stack_page():
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(10, 6, 4, 4)
            v.setSpacing(6)
            return w, v

        # ---- step 1: the layer (always visible) -------------------------------
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.layer_cb = QgsMapLayerComboBox()
        self.layer_cb.setFilters(Qgis.LayerFilter.RasterLayer)
        self.band_cb = QgsRasterBandComboBox()
        self.layer_cb.layerChanged.connect(self.band_cb.setLayer)
        self.band_cb.setLayer(self.layer_cb.currentLayer())
        form.addRow("Raster layer", self.layer_cb)
        form.addRow("Band", self.band_cb)
        p_lay, lv = stack_page()
        intro = QLabel("Choose the elevation raster. The panel checks its real resolution and "
                       "selects a matching orthophoto / reference layer automatically.")
        intro.setWordWrap(True)
        intro.setObjectName("tabIntro")
        lv.addWidget(intro)
        lv.addLayout(form)
        self.layer_info = QLabel()
        self.layer_info.setWordWrap(True)
        self.layer_info.setTextFormat(Qt.TextFormat.RichText)
        self.layer_info.setStyleSheet("QLabel { background: #F1F5F9; color: #334155; "
                                      "border-radius: 4px; padding: 6px; font-size: 8pt; }")
        lv.addWidget(self.layer_info)
        lv.addStretch(1)
        self.setup_tabs.addTab(p_lay, "Layer")

        # ---- step 2: evidence & settings, one page each --------------------------

        def page(intro):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(10, 6, 4, 4)
            lab = QLabel(intro)
            lab.setWordWrap(True)
            lab.setObjectName("tabIntro")
            v.addWidget(lab)
            f = QFormLayout()
            f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            v.addLayout(f)
            v.addStretch(1)
            return w, f

        # -- Orthophoto
        p_or, of2 = page("Best evidence for LiDAR data: the image shows where trees and roofs "
                         "are; the tool measures how high they stand above the yard next to them.")
        self.ortho_chk = QCheckBox("Use an orthophoto of the same area")
        self.ortho_chk.setToolTip(
            "The orthophoto shows where trees and roofs really are. The tool then measures "
            "whether those pixels stand above the open ground next to them: by the object "
            "height in a DSM, ~0 m in a DEM, and partly in a partially filtered DTM.")
        of2.addRow(self.ortho_chk)
        self.ortho_cb = QgsMapLayerComboBox()
        self.ortho_cb.setFilters(Qgis.LayerFilter.RasterLayer)
        of2.addRow("Orthophoto", self.ortho_cb)
        self.r_cb, self.g_cb, self.b_cb, self.nir_cb = (QgsRasterBandComboBox() for _ in range(4))
        self.nir_cb.setShowNotSetOption(True, "None (use RGB)")
        rgb = QHBoxLayout()
        for lbl, cb in (("R", self.r_cb), ("G", self.g_cb), ("B", self.b_cb)):
            rgb.addWidget(QLabel(lbl))
            rgb.addWidget(cb, 1)
        of2.addRow("Bands", rgb)
        of2.addRow("NIR band", self.nir_cb)
        self.bldg_cb = QgsMapLayerComboBox()
        self.bldg_cb.setFilters(Qgis.LayerFilter.PolygonLayer)
        self.bldg_cb.setAllowEmptyLayer(True, "None")
        self.bldg_cb.setLayer(None)
        self.bldg_cb.setToolTip("Optional footprints (e.g. Google Open Buildings, OSM, "
                                "your own digitising) - makes the roof check exact.")
        of2.addRow("Buildings", self.bldg_cb)
        bhint = QLabel(
            "Needs a <b>polygon</b> layer of building outlines. A basemap such as the "
            "OpenStreetMap tiles is only a picture and cannot be used. Get OSM outlines with "
            "the <i>QuickOSM</i> plugin (key <code>building</code>, extent = your DEM), or "
            "download Google Open Buildings for your area.")
        bhint.setWordWrap(True)
        bhint.setStyleSheet("color: palette(dark); font-size: 8pt;")
        of2.addRow("", bhint)
        self.setup_tabs.addTab(p_or, "Orthophoto")
        self.ortho_group = p_or

        # -- Compare
        p_cmp, cf = page("Decisive when you have two layers of the same area: the DSM stands "
                         "above the DEM wherever there are trees or houses.")
        self.pair_chk = QCheckBox("Compare with a second layer")
        self.pair_chk.setToolTip("Strongest evidence: a DSM stands above the DEM wherever "
                                 "buildings or trees exist. Use two layers of the same area.")
        self.ref_cb = QgsMapLayerComboBox()
        self.ref_cb.setFilters(Qgis.LayerFilter.RasterLayer)
        self.ref_cb.setAllowEmptyLayer(True, "None")
        self.ref_cb.setEnabled(False)
        self.pair_chk.toggled.connect(self.ref_cb.setEnabled)
        cf.addRow(self.pair_chk)
        cf.addRow("Reference layer", self.ref_cb)
        self.setup_tabs.addTab(p_cmp, "Compare")

        # -- Filter
        p_fi, ff = page("Filters the surface with growing windows and follows the highest "
                        "points: trees and houses collapse by metres once the window is wider "
                        "than they are; terrain stays.")
        self.ms_chk = QCheckBox("Multi-scale filter test")
        self.ms_chk.setChecked(True)
        ff.addRow(self.ms_chk)
        self.ms_scales = QLineEdit()
        self.ms_scales.setPlaceholderText("auto")
        self.ms_scales.setToolTip(
            "Filter windows in metres, separated by commas. Leave empty for the automatic "
            "ladder (1.5-16.5 m on 0.5 m data, up to village scale on 30 m data). Use several "
            "windows: houses need 8-16 m windows to vanish.")
        ff.addRow("Windows (m)", self.ms_scales)
        presets = QHBoxLayout()
        self.ms_preset_btns = []
        for label, txt in (("Auto", ""), ("LiDAR 1-16 m", "1, 2, 3, 5, 8, 12, 16"),
                           ("Objects 5-16 m", "5, 8, 12, 16"),
                           ("30 m data", "60, 90, 150, 240")):
            btn = QPushButton(label)
            btn.setObjectName("presetBtn")
            btn.clicked.connect(lambda _c=False, t=txt: self.ms_scales.setText(t))
            presets.addWidget(btn)
            self.ms_preset_btns.append(btn)
        ff.addRow("", presets)
        self.ms_hint = QLabel()
        self.ms_hint.setWordWrap(True)
        self.ms_hint.setObjectName("tabIntro")
        ff.addRow("", self.ms_hint)
        exp_row = QHBoxLayout()
        self.ms_export_chk = QCheckBox("Add filtered surface + dZ at")
        self.ms_export_sb = QDoubleSpinBox()
        self.ms_export_sb.setRange(0.5, 500)
        self.ms_export_sb.setValue(8.0)
        self.ms_export_sb.setSuffix(" m")
        exp_row.addWidget(self.ms_export_chk)
        exp_row.addWidget(self.ms_export_sb)
        ff.addRow(exp_row)
        exp_hint = QLabel("Adds one 'dZ' and one 'filtered surface' layer. With the Whole-layer "
                          "scope they cover the sampled tiles only - use Settings > Current map "
                          "extent for a complete area.")
        exp_hint.setWordWrap(True)
        exp_hint.setObjectName("tabIntro")
        ff.addRow(exp_hint)
        for w in [self.ms_scales, self.ms_export_chk, self.ms_export_sb] + self.ms_preset_btns:
            self.ms_chk.toggled.connect(w.setEnabled)
        self.ms_scales.textChanged.connect(lambda *_: self._update_ms_hint())
        self.setup_tabs.addTab(p_fi, "Filter")

        # -- Settings
        p_set, of = page("Defaults suit most data. Use the map-extent scope to look closely "
                         "at one area (e.g. a village and its fields).")
        self.scope_layer = QRadioButton("Whole layer (sampled tiles)")
        self.scope_extent = QRadioButton("Current map extent (full resolution)")
        self.scope_layer.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.scope_layer)
        grp.addButton(self.scope_extent)
        of.addRow("Scope", self.scope_layer)
        of.addRow("", self.scope_extent)
        self.tiles_sb = QSpinBox()
        self.tiles_sb.setRange(4, 64)
        self.tiles_sb.setValue(16)
        self.tiles_sb.setToolTip("Number of full-resolution tiles sampled across the layer.")
        self.tile_sz = QSpinBox()
        self.tile_sz.setRange(128, 2048)
        self.tile_sz.setSingleStep(128)
        self.tile_sz.setValue(512)
        self.tile_sz.setSuffix(" px")
        row = QHBoxLayout()
        row.addWidget(self.tiles_sb)
        row.addWidget(QLabel("tiles of"))
        row.addWidget(self.tile_sz)
        of.addRow("Sampling", row)
        self.units_cb = QComboBox()
        self.units_cb.addItems(["Metres", "Feet"])
        of.addRow("Elevation units", self.units_cb)
        self.width_sb = QDoubleSpinBox()
        self.width_sb.setRange(10, 300)
        self.width_sb.setValue(60)
        self.width_sb.setSuffix(" m")
        self.width_sb.setToolTip("Widest building/crown the detectors should isolate.")
        of.addRow("Max object width", self.width_sb)
        self.height_sb = QDoubleSpinBox()
        self.height_sb.setRange(0.5, 20)
        self.height_sb.setSingleStep(0.5)
        self.height_sb.setValue(2.5)
        self.height_sb.setSuffix(" m")
        self.height_sb.setToolTip("Smallest height counted as an above-ground object. "
                                  "Raised automatically when the data are noisy.")
        of.addRow("Min object height", self.height_sb)
        self.block_chk = QCheckBox("Block grid inspection (adaptive N x N)")
        self.block_chk.setChecked(True)
        self.block_chk.setToolTip("Divide the layer into an adaptive grid and score every block "
                                  "(dZ max, slope breaks, roughness, profiles, continuity). "
                                  "Adds a colour-coded grid layer you can click to inspect.")
        of.addRow(self.block_chk)
        self.diag_chk = QCheckBox("Add object-height layers to the map")
        self.diag_chk.setToolTip("Writes GeoTIFFs of the detected object heights and mask "
                                 "so you can verify the detections visually.")
        of.addRow(self.diag_chk)
        self.defaults_btn = QPushButton("Restore defaults")
        self.defaults_btn.clicked.connect(self._restore_defaults)
        of.addRow("", self.defaults_btn)
        self.setup_tabs.addTab(p_set, "Settings")

        # wiring shared by the tabs
        self._filter_ortho_layers()
        self._on_project(QgsProject.instance().layersAdded, lambda *_: self._filter_ortho_layers())
        self.ortho_cb.layerChanged.connect(self._guard(self._ortho_changed))
        self.ortho_chk.toggled.connect(self._ortho_toggled)
        self._ortho_changed(self.ortho_cb.currentLayer())
        self._ortho_toggled(False)
        for sig in (self.ortho_cb.layerChanged, self.ref_cb.layerChanged,
                    self.pair_chk.toggled, self.ortho_chk.toggled, self.ms_chk.toggled):
            sig.connect(self._guard(lambda *_: (self._refresh_info(), self._update_tab_labels())))

        # ---- step 3: run -------------------------------------------------------
        run_row = QHBoxLayout()
        self.run_btn = QPushButton("Run diagnostic")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        run_row.addWidget(self.run_btn, 1)
        run_row.addWidget(self.cancel_btn)
        help_btn = help_button('dem_dsm_inspector')
        help_btn.setObjectName("helpBtn")
        run_row.addWidget(help_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setObjectName("runProgress")
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        self.progress.setFixedHeight(4)

        # ---- verdict card -------------------------------------------------
        self.card = QFrame()
        self.card.setObjectName("verdictCard")
        cl = QHBoxLayout(self.card)
        cl.setContentsMargins(16, 12, 16, 14)
        cl.setSpacing(24)
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(2)
        right.setSpacing(2)
        cap1, cap2 = QLabel("CLASSIFICATION"), QLabel("CONFIDENCE")
        for c in (cap1, cap2):
            c.setObjectName("verdictCap")
        self.cls_lbl = QLabel("-")
        self.cls_lbl.setObjectName("verdictCls")
        self.cls_sub = QLabel("Run the diagnostic to classify the layer.")
        self.cls_sub.setWordWrap(True)
        self.conf_lbl = QLabel("-")
        self.conf_lbl.setObjectName("verdictConf")
        self.conf_bar = QProgressBar()
        self.conf_bar.setRange(0, 100)
        self.conf_bar.setTextVisible(False)
        self.conf_bar.setFixedHeight(6)
        left.addWidget(cap1)
        left.addWidget(self.cls_lbl)
        left.addWidget(self.cls_sub)
        right.addWidget(cap2)
        right.addWidget(self.conf_lbl)
        right.addWidget(self.conf_bar)
        right.addStretch(1)
        cl.addLayout(left, 3)
        cl.addLayout(right, 2)
        self._paint_card("INCONCLUSIVE", blank=True)

        # ---- tabs ------------------------------------------------------------
        self.tabs = self.nav.section("RESULTS")
        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(True)
        self.report_view.setStyleSheet("QTextBrowser { background: white; color: #111827; }")
        self.report_view.setHtml(self._welcome_html())

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Indicator", "Measured", "Score", "Weight", "Verdict"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self.log.setFont(mono)

        p_sum, sv = stack_page()
        sv.addWidget(self.card)
        sv.addWidget(self.report_view, 1)
        self._summary_layout = sv
        self.tabs.addTab(p_sum, "Summary")
        self.tabs.addTab(self.table, "Indicators")
        # Blocks tab: sortable list + click-to-inspect
        blk = QWidget()
        bl = QVBoxLayout(blk)
        bl.setContentsMargins(0, 4, 0, 0)
        brow = QHBoxLayout()
        self.pick_btn = QPushButton("Pick a block on the map")
        self.pick_btn.setCheckable(True)
        self.pick_btn.setEnabled(False)
        self.pick_btn.toggled.connect(self._toggle_pick)
        self.block_info = QLabel("Run the diagnostic to build the block grid.")
        self.block_info.setWordWrap(True)
        brow.addWidget(self.pick_btn)
        brow.addWidget(self.block_info, 1)
        bl.addLayout(brow)
        self.block_table = QTableWidget(0, 6)
        self.block_table.setHorizontalHeaderLabels(["Block", "Label", "Score", "dZ max (m)",
                                                    "Spikes/km", "Islands (pm)"])
        self.block_table.verticalHeader().setVisible(False)
        self.block_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.block_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.block_table.setSortingEnabled(True)
        self.block_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.block_table.cellDoubleClicked.connect(
            lambda row, _c: self.open_block(int(self.block_table.item(row, 0).data(Qt.ItemDataRole.UserRole))))
        bl.addWidget(self.block_table)
        hint = QLabel("Double-click a row, or use 'Pick a block' and click the map.")
        hint.setStyleSheet("color: palette(mid);")
        bl.addWidget(hint)

        self.tabs.addTab(self.log, "Log")
        self.tabs.insertTab(2, blk, "Blocks")
        self.filter_tab = FilterTab()
        self.tabs.insertTab(3, self.filter_tab, "Filter chart")

        # ---- export row --------------------------------------------------------
        ex = QHBoxLayout()
        self.export_btns = []
        for label, slot in (("Pop out", self.pop_out), ("HTML", self.export_html),
                            ("PDF", self.export_pdf), ("JSON", self.export_json),
                            ("CSV", self.export_csv)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            b.setEnabled(False)
            b.setObjectName("exportBtn")
            ex.addWidget(b)
            self.export_btns.append(b)
        self._summary_layout.addLayout(ex)

        # ---- footer: always visible (status chip, progress, run) ---------------
        foot = QFrame()
        foot.setObjectName("footer")
        fv = QVBoxLayout(foot)
        fv.setContentsMargins(2, 8, 2, 2)
        fv.setSpacing(6)
        self.chip = QLabel()
        self.chip.setObjectName("chip")
        self.chip.setTextFormat(Qt.TextFormat.RichText)
        self.chip.setWordWrap(True)
        fv.addWidget(self.chip)
        fv.addWidget(self.progress)
        fv.addLayout(run_row)
        outer.addWidget(foot)
        self._set_chip(None)
        self.setup_tabs.setCurrentIndex(0)          # "Layer" page
        self._update_tab_labels()
        self._update_ms_hint()

        self.setWidget(root)
        self._probe_task = None
        self._probe = {}                       # layer id -> real-resolution factor
        self.layer_cb.layerChanged.connect(self._guard(self._on_layer_changed))
        self._on_project(QgsProject.instance().layersAdded,
                         lambda *_: self._on_layer_changed(self.layer_cb.currentLayer(), auto=False))
        self._on_project(QgsProject.instance().layersWillBeRemoved, lambda *_: self.cancel_probe())
        self._on_layer_changed(self.layer_cb.currentLayer())

    @staticmethod
    def _guard(fn):
        """Wrap a slot so a signal fired while the panel is being torn down (its
        widgets already deleted) is ignored instead of raising."""
        def guarded(*args):
            try:
                return fn(*args)
            except RuntimeError as exc:
                if "deleted" not in str(exc):
                    raise
        return guarded

    @staticmethod
    def _on_project(signal, fn):
        """Connect a project signal to `fn`, and drop the connection once the
        panel's widgets are gone (the project outlives the dock)."""
        def guarded(*args):
            try:
                fn(*args)
            except RuntimeError:            # wrapped C/C++ object has been deleted
                try:
                    signal.disconnect(guarded)
                except (TypeError, RuntimeError):
                    pass
        signal.connect(guarded)

    # ==================================================================
    # Helpers
    # ==================================================================
    def _paint_card(self, cls, blank=False):
        fg, bg = VERDICT_COLOURS.get(cls, VERDICT_COLOURS["INCONCLUSIVE"])
        if blank:
            fg, bg = "#6B7280", "#F3F4F6"
        self.card.setStyleSheet(
            "#verdictCard {{ background: {bg}; border: 1px solid {fg}; border-radius: 10px; }}"
            "#verdictCard QLabel {{ color: {fg}; background: transparent; }}"
            "#verdictCard QProgressBar {{ background: rgba(0,0,0,0.08); border: none; "
            "border-radius: 4px; }}"
            "#verdictCard QProgressBar::chunk {{ background: {fg}; border-radius: 4px; }}"
            .format(bg=bg, fg=fg))

    @staticmethod
    def _welcome_html():
        return ("<div style='font-family:Segoe UI,Arial;color:#374151;padding:6px;'>"
                "<h3 style='color:#111827;'>How to use</h3><ol>"
                "<li><b>Layer</b> - pick the raster; the panel checks its real resolution and "
                "picks a matching orthophoto / reference for you.</li>"
                "<li><b>Set-up pages</b> (left) - <i>Orthophoto</i> and <i>Compare</i> add the "
                "strongest evidence; <i>Filter</i> sets the multi-scale filter windows; "
                "<i>Settings</i> holds scope, sampling and thresholds. A &#10003; marks what is "
                "switched on.</li><li><b>Run diagnostic</b> (bottom) - the panel opens "
                "<i>Summary</i>; <i>Indicators</i>, <i>Blocks</i> (click a block on the map), "
                "<i>Filter chart</i> and <i>Log</i> hold the details.</li></ol>"
                "<h3 style='color:#111827;'>What is checked</h3><ul>"
                "<li>Height above local ground, steep edges, roughness, profile spikes, "
                "sharp-edged footprints, product name</li>"
                "<li>Block grid and the highest cell of every block</li>"
                "<li>Multi-scale filter: do the highest points collapse when filtered?</li>"
                "<li>Optional: orthophoto (trees / roofs vs the yard beside them) and a "
                "second layer of the same area</li></ul>"
                "<p style='color:#6B7280;'>No raster-only method is 100% certain: a DSM of "
                "bare land is identical to a DEM. The confidence is capped whenever the data "
                "cannot physically prove the answer.</p></div>")

    def _log(self, text):
        self.log.appendPlainText(text)

    def _load_settings(self):
        s = QgsSettings()
        self.tiles_sb.setValue(int(s.value(SETTINGS + "tiles", 16)))
        self.tile_sz.setValue(int(s.value(SETTINGS + "tile_size", 512)))
        self.units_cb.setCurrentIndex(int(s.value(SETTINGS + "units", 0)))
        # Object width / height are NOT remembered: they start at the safe
        # defaults every session (use "Restore defaults" within a session).

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS + "tiles", self.tiles_sb.value())
        s.setValue(SETTINGS + "tile_size", self.tile_sz.value())
        s.setValue(SETTINGS + "units", self.units_cb.currentIndex())


    # ==================================================================
    # Live setup: react to the selected layer
    # ==================================================================
    def _set_chip(self, r, text=None):
        """Footer status: always tells what the panel shows right now."""
        if r is None:
            layer = self.layer_cb.currentLayer() if hasattr(self, "layer_cb") else None
            html = "<span style='color:#6B7280'>Ready{}</span>".format(
                " &middot; " + layer.name() if layer else "")
        elif r == "running":
            html = "<span style='color:#1D4ED8'>&#9679; Analysing {}...</span>".format(text)
        elif r == "error":
            html = "<span style='color:#B91C1C'><b>Stopped</b> - see Log</span>"
        else:
            fg, bg = VERDICT_COLOURS.get(r.classification, VERDICT_COLOURS["INCONCLUSIVE"])
            label = {"PARTIAL": "PARTIAL DEM", "INCONCLUSIVE": "INCONCLUSIVE"}.get(
                r.classification, r.classification)
            html = ("<span style='background:{bg};color:{fg};font-weight:700;'>&nbsp;{lab}&nbsp;"
                    "</span>&nbsp; <b>{c:.0f}%</b> <span style='color:#6B7280'>&middot; {lay}"
                    "</span>").format(bg=bg, fg=fg, lab=label, c=r.confidence,
                                      lay=r.info.get("layer", ""))
        self.chip.setText(html)

    def _results_enabled(self, on):
        """Result pages are greyed out until there is a result (Log stays open)."""
        for i in range(self.tabs.count() - 1):
            self.tabs.setPageEnabled(i, on)

    def _update_tab_labels(self):
        """Tab names are fixed; kept so the option toggles have one place to call."""

    def _update_ms_hint(self):
        """Live preview of the filter windows actually used (rounded to pixels)."""
        layer = self.layer_cb.currentLayer()
        txt = self.ms_scales.text().strip()
        if not txt or txt.lower() == "auto":
            self.ms_hint.setText("Automatic windows: 1.5 - 16.5 m on 0.5 m data, up to village "
                                 "scale on 30 m data.")
            return
        try:
            vals = sorted({float(v) for v in re.split(r"[,;\s]+", txt) if v})
        except ValueError:
            self.ms_hint.setText("<span style='color:#B91C1C'>Use numbers in metres separated "
                                 "by commas, e.g. 1, 2, 3, 5, 8, 12, 16</span>")
            return
        res = self._pixel_m(layer) if layer is not None else None
        if res:
            k = self._probe.get(layer.id()) or 1
            res_eff = res * (k if k >= 3 else 1)
            used = []
            for s in vals:
                px = max(3, int(round(s / res_eff)) | 1)
                if s / res_eff >= 1.5 and px * res_eff not in used:
                    used.append(round(px * res_eff, 2))
            msg = "Used: {} m".format(", ".join("{:g}".format(u) for u in used) or "none")
        else:
            used, msg = vals, ""
        warn = ""
        if len(used) < 3:
            warn = " - use at least 3 windows (e.g. 1, 2, 3, 5, 8, 12, 16) so the tool can see " \
                   "at which scale objects disappear."
        elif max(used) < 5:
            warn = " - add windows of 8-16 m: houses and crowns are wider than 5 m."
        self.ms_hint.setText(msg + ("<span style='color:#B45309'>{}</span>".format(warn)
                                    if warn else ""))

    def _restore_defaults(self):
        self.scope_layer.setChecked(True)
        self.tiles_sb.setValue(16)
        self.tile_sz.setValue(512)
        self.units_cb.setCurrentIndex(0)
        self.width_sb.setValue(60.0)
        self.height_sb.setValue(2.5)
        self.diag_chk.setChecked(False)
        self.block_chk.setChecked(True)
        self.ms_chk.setChecked(True)
        self.ms_scales.clear()
        self.ms_export_chk.setChecked(False)
        self.ms_export_sb.setValue(8.0)

    @staticmethod
    def _overlap(main, other):
        """Share (0-1) of `main`'s extent covered by `other`'s extent."""
        try:
            if main is None or other is None or not other.isValid():
                return 0.0
            xf = QgsCoordinateTransform(other.crs(), main.crs(), QgsProject.instance())
            ext = xf.transformBoundingBox(other.extent())
            inter = ext.intersect(main.extent())
            a = main.extent().area()
            return float(inter.area() / a) if a > 0 and not inter.isEmpty() else 0.0
        except Exception:
            return 0.0

    def _clear_result(self, layer):
        """Forget the previous run: a result always belongs to ONE layer."""
        if self.task is not None:
            return
        self.result = None
        self._paint_card("INCONCLUSIVE", blank=True)
        self.cls_lbl.setText("-")
        self.cls_sub.setText("Ready to analyse {}.".format(layer.name()) if layer
                             else "Choose a raster layer.")
        self.conf_lbl.setText("-")
        self.conf_bar.setValue(0)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        self.report_view.setHtml(self._welcome_html())
        self.table.setRowCount(0)
        self.block_table.setRowCount(0)
        self.filter_tab.clear()
        self._set_chip(None)
        self._results_enabled(False)
        if self.nav.main.currentIndex() == self.tabs.index:
            self.setup_tabs.setCurrentIndex(0)
        self.block_info.setText("Run the diagnostic to build the block grid.")
        self.pick_btn.setChecked(False)
        self.pick_btn.setEnabled(False)
        for b in self.export_btns:
            b.setEnabled(False)

    def _on_layer_changed(self, layer, auto=True):
        """New input layer: clear the old result, exclude it from the other
        pickers, auto-select the matching orthophoto / reference / buildings
        and probe its real resolution in the background."""
        self._filter_ortho_layers()
        self.ref_cb.setExceptedLayerList([layer] if layer else [])
        if auto:
            self._clear_result(layer)
        if layer is None:
            self._refresh_info()
            return
        if auto:
            self._auto_select(layer)
        self._refresh_info()
        self._update_tab_labels()
        self._update_ms_hint()
        if auto and layer.id() not in self._probe:
            # Delay: when layers are being removed the combo box jumps through
            # several layers; only probe the one that is still selected.
            lid = layer.id()
            QTimer.singleShot(500, lambda: self._maybe_probe(lid))

    def _maybe_probe(self, lid):
        layer = self.layer_cb.currentLayer()
        if layer is not None and layer.id() == lid and QgsProject.instance().mapLayer(lid) \
                and lid not in self._probe:
            self._start_probe(layer)

    def cancel_probe(self):
        t = self._probe_task
        self._probe_task = None
        try:
            if t is not None and t.status() in (QgsTask.TaskStatus.Queued,
                                                QgsTask.TaskStatus.OnHold,
                                                QgsTask.TaskStatus.Running):
                t.cancel()
        except RuntimeError:          # already deleted by the task manager
            pass

    def _auto_select(self, layer):
        proj = QgsProject.instance()
        rasters = [lk for lk in proj.mapLayers().values()
                   if isinstance(lk, QgsRasterLayer) and lk is not layer and lk.isValid()]
        # Orthophoto: multi-band image covering most of the layer
        imgs = sorted(((self._overlap(layer, lk), lk) for lk in rasters if lk.bandCount() >= 3),
                      key=lambda t: -t[0])
        cur = self.ortho_cb.currentLayer()
        cur_ov = self._overlap(layer, cur) if cur is not None else 0.0
        if imgs and imgs[0][0] >= 0.2 and (cur is None or cur_ov < 0.2 or imgs[0][0] > cur_ov + 0.3):
            self.ortho_cb.setLayer(imgs[0][1])
            cur, cur_ov = imgs[0][1], imgs[0][0]
        self.ortho_chk.setChecked(cur is not None and cur_ov >= 0.2)

        # Reference: another single-band elevation raster of the same area
        elev = sorted(((self._overlap(layer, lk), lk) for lk in rasters if lk.bandCount() < 3),
                      key=lambda t: -t[0])
        rcur = self.ref_cb.currentLayer()
        if rcur is layer or self._overlap(layer, rcur) < 0.2:
            self.ref_cb.setLayer(elev[0][1] if elev and elev[0][0] >= 0.2 else None)
            self.pair_chk.setChecked(False)
        # Buildings: an overlapping polygon layer that is named like footprints
        bl = [lk for lk in proj.mapLayers().values()
              if isinstance(lk, QgsVectorLayer) and lk.geometryType() == Qgis.GeometryType.Polygon
              and re.search(r"build|bldg|house|footprint|structure", lk.name(), re.I)
              and self._overlap(layer, lk) > 0]
        if self._overlap(layer, self.bldg_cb.currentLayer()) <= 0:
            self.bldg_cb.setLayer(bl[0] if bl else None)

    def _start_probe(self, layer):
        path = _file_path(layer)
        if path is None:
            return
        band = max(1, self.band_cb.currentBand())
        lid = layer.id()

        def probe(_task):
            from ..core import focal, raster_io
            tiles, _ = raster_io.sample_tiles(path, band, 3, 768)
            return focal.effective_factor(tiles) if tiles else 1

        def done(exc, k=None):
            if exc is None and k is not None:
                self._probe[lid] = k
                cur = self.layer_cb.currentLayer()
                if cur is not None and cur.id() == lid:
                    res = self._pixel_m(cur)
                    if k >= 3 and res * k >= 10 and self.ortho_chk.isChecked():
                        self.ortho_chk.setChecked(False)   # orthophoto useless at ~30 m
                    self._refresh_info()
                    self._update_ms_hint()

        self.cancel_probe()
        self._probe_task = QgsTask.fromFunction("DEM/DSM: checking real resolution", probe,
                                                on_finished=done)
        QgsApplication.taskManager().addTask(self._probe_task)

    @staticmethod
    def _pixel_m(layer):
        px = (abs(layer.rasterUnitsPerPixelX()) * abs(layer.rasterUnitsPerPixelY())) ** 0.5
        if layer.crs().isGeographic():
            px *= 111320.0 * math.cos(math.radians(layer.extent().center().y()))
        return px

    def _refresh_info(self):
        layer = self.layer_cb.currentLayer()
        if layer is None:
            self.layer_info.setText("No raster layer selected.")
            return
        res = self._pixel_m(layer)
        lines = ["<b>{}</b> &middot; {:.2f} m pixels &middot; {} x {} px &middot; {}".format(
            layer.name(), res, layer.width(), layer.height(), layer.crs().authid() or "no CRS")]
        k = self._probe.get(layer.id())
        if k is None:
            lines.append("Checking real resolution...")
        elif k >= 3 and res * k >= 10:
            lines.append("&#9888; Resampled: real detail ~{:.0f} m (e.g. Copernicus / SRTM). "
                         "Orthophoto check switched off; use the pairwise check for proof."
                         .format(res * k))
        elif k >= 3:
            lines.append("Interpolated surface (points ~{:.0f} m apart) - typical of a DTM "
                         "built from ground points.".format(res * k))
        else:
            lines.append("Native resolution.")
        if _file_path(layer) is None:
            lines.append("&#9888; Not a file-based GDAL raster - cannot be analysed.")
        if self.ortho_chk.isChecked():
            o = self.ortho_cb.currentLayer()
            ov = self._overlap(layer, o)
            lines.append(("&#10003; Orthophoto: {} (covers {:.0f}% of the layer)".format(o.name(), 100 * ov)
                          if ov >= 0.2 else "&#9888; Orthophoto '{}' covers only {:.0f}% of this layer."
                          .format(o.name() if o else "-", 100 * ov)))
        else:
            imgs = [lk for lk in QgsProject.instance().mapLayers().values()
                    if isinstance(lk, QgsRasterLayer) and lk.bandCount() >= 3
                    and self._overlap(layer, lk) >= 0.2]
            if not imgs:
                lines.append("No orthophoto covering this layer is loaded.")
        if self.pair_chk.isChecked():
            rl = self.ref_cb.currentLayer()
            ov = self._overlap(layer, rl)
            lines.append(("&#10003; Reference: {} (overlap {:.0f}%)".format(rl.name(), 100 * ov)
                          if ov > 0 else "&#9888; Reference layer does not overlap this layer."))
        self.layer_info.setText("<br>".join(lines))

    def _filter_ortho_layers(self):
        """Only multi-band rasters (images) can be orthophotos."""
        single = [lk for lk in QgsProject.instance().mapLayers().values()
                  if isinstance(lk, QgsRasterLayer) and lk.bandCount() < 3]
        main = self.layer_cb.currentLayer() if hasattr(self, "layer_cb") else None
        self.ortho_cb.setExceptedLayerList(single + ([main] if main else []))

    def _ortho_changed(self, layer):
        for cb in (self.r_cb, self.g_cb, self.b_cb, self.nir_cb):
            cb.setLayer(layer)
        if layer is not None:
            n = layer.bandCount()
            self.r_cb.setBand(1)
            self.g_cb.setBand(min(2, n))
            self.b_cb.setBand(min(3, n))
            self.nir_cb.setBand(self._guess_nir(layer))

    @staticmethod
    def _guess_nir(layer):
        """Return the NIR band only when the file says so (name or colour
        interpretation); RGBA drone orthophotos have ALPHA as band 4."""
        prov = layer.dataProvider()
        for b in range(1, layer.bandCount() + 1):
            name = (prov.generateBandName(b) + " " + (prov.bandDescription(b) or "")).lower()
            if prov.colorInterpretation(b) == Qgis.RasterColorInterpretation.AlphaBand:
                continue
            if "nir" in name or "infrared" in name or "near" in name:
                return b
        return -1

    def _ortho_toggled(self, on):
        for w in (self.ortho_cb, self.r_cb, self.g_cb, self.b_cb, self.nir_cb, self.bldg_cb):
            w.setEnabled(on)

    def _set_running(self, running):
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for b in self.export_btns:
            b.setEnabled(not running and self.result is not None)

    # ==================================================================
    # Run
    # ==================================================================
    def start(self):
        layer = self.layer_cb.currentLayer()
        path = _file_path(layer)
        if path is None:
            self.iface.messageBar().pushWarning(
                "DEM / DSM Inspector", "Select a file-based (GDAL) raster layer.")
            return
        ref_path = None
        if self.pair_chk.isChecked():
            ref = self.ref_cb.currentLayer()
            ref_path = _file_path(ref)
            if ref_path is None or ref is layer:
                self.iface.messageBar().pushWarning(
                    "DEM / DSM Inspector", "Choose a different, file-based reference layer.")
                return

        ortho_kw = {}
        if self.ortho_chk.isChecked():
            olayer = self.ortho_cb.currentLayer()
            opath = _file_path(olayer)
            if opath is None or olayer is layer:
                self.iface.messageBar().pushWarning(
                    "DEM / DSM Inspector", "Choose a file-based orthophoto layer.")
                return
            if olayer.bandCount() < 3:
                self.iface.messageBar().pushWarning(
                    "DEM / DSM Inspector",
                    "'{}' has {} band - it looks like an elevation raster, not an orthophoto. "
                    "Choose the RGB image (e.g. the JICA / KTTL orthophoto .ecw)."
                    .format(olayer.name(), olayer.bandCount()))
                return
            rgb = (self.r_cb.currentBand(), self.g_cb.currentBand(), self.b_cb.currentBand())
            if min(rgb) < 1 or len(set(rgb)) < 3:
                self.iface.messageBar().pushWarning(
                    "DEM / DSM Inspector", "Select three different bands for red, green and blue.")
                return
            nir = self.nir_cb.currentBand()
            bpath, blayer = _vector_source(self.bldg_cb.currentLayer())
            ortho_kw = dict(ortho_path=opath, ortho_nir=nir if nir > 0 else None,
                            ortho_bands=(self.r_cb.currentBand(), self.g_cb.currentBand(),
                                         self.b_cb.currentBand()),
                            buildings_path=bpath, buildings_layer=blayer)

        scales = None
        txt = self.ms_scales.text().strip()
        if txt and txt.lower() != "auto":
            try:
                scales = sorted({float(v) for v in re.split(r"[,;\s]+", txt) if v})
                if not scales or min(scales) <= 0:
                    raise ValueError
            except ValueError:
                self.iface.messageBar().pushWarning(
                    "DEM / DSM Inspector", "Filter windows must be positive numbers in metres, "
                    "e.g. 1, 2, 3, 5, 8, 12, 16 (or leave empty for auto).")
                return

        extent = None
        if self.scope_extent.isChecked():
            canvas = self.iface.mapCanvas()
            xform = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(),
                                           layer.crs(), QgsProject.instance())
            r = xform.transformBoundingBox(canvas.extent())
            extent = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())

        self._save_settings()
        kwargs = dict(
            path=path, band=max(1, self.band_cb.currentBand()), layer_name=layer.name(),
            ref_path=ref_path, ref_band=1, n_tiles=self.tiles_sb.value(),
            tile=self.tile_sz.value(), z_units="ft" if self.units_cb.currentIndex() == 1 else "m",
            extent=extent,
            params={"object_width_m": self.width_sb.value(),
                    "min_object_height_m": self.height_sb.value(),
                    "multiscale": self.ms_chk.isChecked(),
                    "ms_scales_m": scales,
                    "ms_export_m": (self.ms_export_sb.value()
                                    if self.ms_chk.isChecked() and self.ms_export_chk.isChecked()
                                    else None),
                    "export_objects": self.diag_chk.isChecked()},
            write_diagnostics=self.diag_chk.isChecked() or (
                self.ms_chk.isChecked() and self.ms_export_chk.isChecked()),
            block_grid=self.block_chk.isChecked(),
            **ortho_kw,
        )
        self.result = None
        self.log.clear()
        self._log("Layer      : {}\nSource     : {}".format(layer.name(), path))
        if ref_path:
            self._log("Reference  : {}".format(ref_path))
        if ortho_kw:
            self._log("Orthophoto : {}  bands {}{}".format(
                ortho_kw["ortho_path"], ortho_kw["ortho_bands"],
                "  NIR {}".format(ortho_kw["ortho_nir"]) if ortho_kw["ortho_nir"] else ""))
            if ortho_kw["buildings_path"]:
                self._log("Buildings  : {}".format(ortho_kw["buildings_path"]))
        self._log("Scope      : {}".format("map extent" if extent else
                  "{} tiles x {} px".format(self.tiles_sb.value(), self.tile_sz.value())))
        self._log("Filter     : {}".format(
            "off" if not self.ms_chk.isChecked() else
            "windows {} m{}".format(", ".join("{:g}".format(s) for s in scales) if scales else "auto",
                                    ", export at {:g} m".format(self.ms_export_sb.value())
                                    if self.ms_export_chk.isChecked() else "")))

        self._paint_card("INCONCLUSIVE", blank=True)
        self.cls_lbl.setText("...")
        self.cls_sub.setText("Analysing {}".format(layer.name()))
        self.conf_lbl.setText("-")
        self.conf_bar.setValue(0)
        self.table.setRowCount(0)

        self.task = InspectTask("DEM/DSM diagnostic: " + layer.name(), kwargs, self._done)
        self.task.progressChanged.connect(self._on_progress)
        self._set_running(True)
        self.progress.setFormat("Starting...")
        self._set_chip("running", layer.name())
        QgsApplication.taskManager().addTask(self.task)

    # ==================================================================
    # Block grid
    # ==================================================================
    def _show_blocks(self, r):
        blocks = r.info.get("blocks") or []
        self.block_table.setSortingEnabled(False)
        self.block_table.setRowCount(len(blocks))
        for i, b in enumerate(blocks):
            v = b.get("values", {})
            cells = ["r{} c{}".format(b["row"], b["col"]), b["label"],
                     b.get("score", 0.0), v.get("dz", 0.0), v.get("profile", 0.0), v.get("moran", 0.0)]
            for c, val in enumerate(cells):
                it = QTableWidgetItem()
                if isinstance(val, float):
                    it.setData(Qt.ItemDataRole.DisplayRole, round(val, 2))
                else:
                    it.setText(val)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, i)
                if c == 1:
                    fill = LABEL_STYLE.get(val, LABEL_STYLE["N/A"])[0]
                    it.setBackground(QColor(fill))
                self.block_table.setItem(i, c, it)
        self.block_table.setSortingEnabled(True)
        self.block_table.sortItems(2, Qt.SortOrder.DescendingOrder)
        self.pick_btn.setEnabled(bool(blocks))
        if not blocks:
            self.block_info.setText("Block grid not run (disabled in Options).")
            return
        s = r.info["block_summary"]
        g = r.info["block_grid"]
        c = s["counts"]
        self.block_info.setText(
            "{} x {} blocks of ~{:.0f} m: {} with objects, {} mixed, {} bare, {} not evaluable."
            .format(g["nx"], g["ny"], g["block_m"][0], c["OBJECTS"], c["MIXED"], c["BARE"], c["N/A"]))
        # grid layer on the map (replace the previous one)
        if getattr(self, "grid_layer_id", None):
            QgsProject.instance().removeMapLayer(self.grid_layer_id)
        if getattr(self, "peaks_layer_id", None):
            QgsProject.instance().removeMapLayer(self.peaks_layer_id)
        lyr = build_grid_layer(r, "Block grid - {}".format(r.info.get("layer", "")))
        QgsProject.instance().addMapLayer(lyr)
        self.grid_layer_id = lyr.id()
        pl = build_peaks_layer(r, "Highest cells - {}".format(r.info.get("layer", "")))
        if pl.featureCount():
            QgsProject.instance().addMapLayer(pl)
            self.peaks_layer_id = pl.id()

    def _toggle_pick(self, on):
        canvas = self.iface.mapCanvas()
        if on:
            self._prev_tool = canvas.mapTool()
            self.pick_tool = BlockPickTool(canvas, self)
            self.pick_tool.deactivated.connect(lambda: self.pick_btn.setChecked(False))
            canvas.setMapTool(self.pick_tool)
        elif getattr(self, "pick_tool", None) is not None and canvas.mapTool() is self.pick_tool:
            canvas.unsetMapTool(self.pick_tool)
            if getattr(self, "_prev_tool", None) is not None:
                canvas.setMapTool(self._prev_tool)

    def open_block(self, idx):
        if self.result is None:
            return
        if getattr(self, "inspector", None) is None:
            self.inspector = BlockInspector(self)
        try:
            self.inspector.show_block(self.result, idx)
        except Exception as exc:  # file moved, etc.
            self.iface.messageBar().pushWarning("DEM / DSM Inspector",
                                                "Cannot open block: {}".format(exc))

    def cancel(self):
        self.cancel_probe()
        if self.task is not None:
            self.task.cancel()

    def _on_progress(self, value):
        msg = self.task.message if self.task else ""
        self.progress.setValue(int(value))
        self.chip.setText("<span style='color:#1D4ED8'>&#9679; {} &middot; {}%</span>".format(
            msg.replace("<", "&lt;"), int(value)))

    def _done(self, ok, result, error):
        self.task = None
        self.progress.setValue(100 if ok else 0)
        self.progress.setFormat("Finished" if ok else "Stopped")
        if not ok:
            self._set_running(False)
            self._log("\n" + (error or "Unknown error"))
            short = (error or "Unknown error").split("\n")[0]
            self.cls_lbl.setText("-")
            self.cls_sub.setText(short)
            self.report_view.setHtml(
                "<div style='font-family:Segoe UI,Arial;padding:6px;'><h3 style='color:#B91C1C;'>"
                "The diagnostic did not finish</h3><p>{}</p><p style='color:#6B7280;'>"
                "Details are in the Log tab.</p></div>".format(short.replace("<", "&lt;")))
            self.iface.messageBar().pushWarning("DEM / DSM Inspector", short)
            self._set_chip("error", short)
            self.tabs.setCurrentIndex(self.tabs.count() - 1)          # Log
            return
        self.result = result
        self._set_running(False)
        self.show_result(result)

    # ==================================================================
    # Results
    # ==================================================================
    def show_result(self, r):
        self._paint_card(r.classification)
        self._set_chip(r)
        self._results_enabled(True)
        self.cls_lbl.setText({"INCONCLUSIVE": "INCONCLUSIVE", "PARTIAL": "PARTIAL DEM"}
                             .get(r.classification, r.headline))
        self.cls_sub.setText({
            "DSM": "Surface model - includes buildings / vegetation",
            "DEM": "Bare-earth elevation model",
            "INCONCLUSIVE": "Evidence leans {} but is too weak to decide".format(r.lean),
            "PARTIAL": "Bare earth with residual trees / houses (partially filtered)",
        }[r.classification] + (
            ("\nInterpolated surface: points ~{:.0f} m apart" if r.info.get("interpolated_surface")
             else "\nResampled data: real detail ~{:.0f} m").format(r.info["pixel_size_m"])
            if r.info.get("resampling_factor") else ""))
        self.conf_lbl.setText("{:.0f}%".format(r.confidence))
        self.conf_bar.setValue(int(round(r.confidence)))

        self.report_view.setHtml(report.build_html(r, include_card=False, compact=True))

        self.table.setRowCount(len(r.indicators))
        for row, ind in enumerate(r.indicators):
            name = QTableWidgetItem(ind.name)
            name.setToolTip("{}\n\n{}\n\nMethod: {}".format(ind.question, ind.finding, ind.method))
            vals = [name, QTableWidgetItem(ind.value_text),
                    QTableWidgetItem("{:+.2f}".format(ind.score)),
                    QTableWidgetItem("{:.2f}".format(ind.weight)),
                    QTableWidgetItem(ind.verdict)]
            fg, bg = VERDICT_COLOURS.get(ind.verdict, VERDICT_COLOURS["Neutral"])
            vals[4].setForeground(QColor(fg))
            vals[4].setBackground(QColor(bg))
            vals[4].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            for c, it in enumerate(vals):
                self.table.setItem(row, c, it)
        self.table.resizeRowsToContents()

        self._log("\n" + report.text_summary(r))
        self._log("\nRuntime    : {} s".format(r.info.get("runtime_s")))
        self.tabs.setCurrentIndex(0)

        self._show_blocks(r)
        self.filter_tab.show_result(r)

        layers = r.info.get("diagnostic_layers", [])
        if layers:
            self._add_diagnostic_layers(layers, r)

        level = Qgis.MessageLevel.Success if r.classification != "INCONCLUSIVE" else Qgis.MessageLevel.Info
        self.iface.messageBar().pushMessage(
            "DEM / DSM Inspector", "{} - {}  ({:.0f}% confidence)".format(
                r.info.get("layer", ""), r.headline, r.confidence), level, 8)

    def _add_diagnostic_layers(self, layers, result):
        """One layer per product (tiles joined in a VRT), styled by kind, in a
        group that REPLACES the group of the previous run for the same layer."""
        root = QgsProject.instance().layerTreeRoot()
        name = "DEM-DSM diagnostics - {}".format(result.info.get("layer", ""))
        old = root.findGroup(name)
        if old is not None:
            QgsProject.instance().removeMapLayers([n.layerId() for n in old.findLayers()])
            root.removeChildNode(old)
        group = root.insertGroup(0, name)
        partial = self.scope_layer.isChecked()
        for item in layers:
            label = item["label"] + (" - {} sampled tiles".format(item["tiles"])
                                     if partial and item["tiles"] > 1 else "")
            lyr = QgsRasterLayer(item["path"], label)
            if not lyr.isValid():
                continue
            kind = item["kind"]
            if kind in ("height", "dz"):
                shader_fn = QgsColorRampShader(0.0, 30.0)
                shader_fn.setColorRampType(Qgis.ShaderInterpolationMethod.Linear)
                shader_fn.setColorRampItemList([
                    QgsColorRampShader.ColorRampItem(0.0, QColor(255, 255, 255, 0), "0 m"),
                    QgsColorRampShader.ColorRampItem(1.0, QColor("#FEF3C7"), "1 m"),
                    QgsColorRampShader.ColorRampItem(2.5, QColor("#FDE68A"), "2.5 m"),
                    QgsColorRampShader.ColorRampItem(10.0, QColor("#F59E0B"), "10 m"),
                    QgsColorRampShader.ColorRampItem(30.0, QColor("#B91C1C"), "30 m"),
                ])
                shader = QgsRasterShader()
                shader.setRasterShaderFunction(shader_fn)
                lyr.setRenderer(QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader))
            elif kind == "mask":        # objects in red, ground transparent
                lyr.setRenderer(QgsPalettedRasterRenderer(
                    lyr.dataProvider(), 1,
                    [QgsPalettedRasterRenderer.Class(1, QColor(220, 38, 38, 160), "object")]))
            # "surface": QGIS default grey stretch, like any elevation raster
            QgsProject.instance().addMapLayer(lyr, False)
            group.addLayer(lyr)
            if kind == "surface":
                group.findLayer(lyr.id()).setItemVisibilityChecked(False)
        self._log("Diagnostic layers added: {} (group '{}')".format(len(layers), name))
        if partial:
            self._log("Whole-layer scope: only the sampled tiles are covered. Use "
                      "'Current map extent' for a complete area.")

    # ==================================================================
    # Export
    # ==================================================================
    def _ask(self, title, filt, ext):
        if self.result is None:
            return None
        stem = os.path.splitext(self.result.info.get("layer", "report"))[0]
        start = os.path.join(QgsSettings().value(SETTINGS + "last_dir", os.path.expanduser("~")),
                             "{}_dem_dsm_report.{}".format(stem, ext))
        path, _ = QFileDialog.getSaveFileName(self, title, start, filt)
        if path:
            QgsSettings().setValue(SETTINGS + "last_dir", os.path.dirname(path))
            if not path.lower().endswith("." + ext):
                path += "." + ext
        return path or None

    def _saved(self, path):
        self.iface.messageBar().pushSuccess("DEM / DSM Inspector", "Saved {}".format(path))

    def export_html(self):
        p = self._ask("Save HTML report", "HTML (*.html)", "html")
        if p:
            self._saved(report.write_html(self.result, p))

    def export_json(self):
        p = self._ask("Save JSON", "JSON (*.json)", "json")
        if p:
            self._saved(report.write_json(self.result, p))

    def export_csv(self):
        p = self._ask("Save CSV", "CSV (*.csv)", "csv")
        if p:
            self._saved(report.write_csv(self.result, p))

    def export_pdf(self):
        p = self._ask("Save PDF report", "PDF (*.pdf)", "pdf")
        if not p:
            return
        self._saved(write_pdf(report.build_html(self.result), p))

    def pop_out(self):
        if self.result is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("DEM / DSM report - " + self.result.info.get("layer", ""))
        dlg.resize(980, 820)
        v = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setStyleSheet("QTextBrowser { background: white; color: #111827; }")
        tb.setHtml(report.build_html(self.result))
        v.addWidget(tb)
        dlg.show()


def write_pdf(html_text, path):
    """Render the HTML report to an A4 PDF (Qt5 and Qt6)."""
    from qgis.PyQt.QtPrintSupport import QPrinter
    from qgis.PyQt.QtGui import QPageLayout, QPageSize
    from qgis.PyQt.QtCore import QMarginsF

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    printer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                                      QPageLayout.Orientation.Landscape,
                                      QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter))
    doc = QTextDocument()
    doc.setHtml(html_text)
    (getattr(doc, "print_", None) or getattr(doc, "print"))(printer)
    return path

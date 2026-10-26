# -*- coding: utf-8 -*-
"""Paddy Irrigation Tools - the window.

Pick a DEM and any number of polygon / line layers, choose what to find and how,
and press Run. The work is `core/block_elevation.run_analysis`, called on the GUI
thread with a wait cursor and a log box that keeps the window repainting.
"""

import os
import time
from contextlib import suppress
import traceback

from qgis.core import (
    QgsFeature, QgsField, QgsProject, QgsRasterLayer, QgsVectorLayer,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox, QgsRasterBandComboBox
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from ..branding import open_docs
from ..core import output_defaults as od
from ..core.block_elevation import (
    KEY, LINE_GEOM, METHODS, POLYGON_GEOM, RES_CHOICES, RESERVED, add_issue_layer, add_point_layer,
    export_results, run_analysis, safe_name, summary_table, table_name,
)
from ..core.compat import T_INT, T_LONGLONG, T_STRING, make_field
from ..core.modify_features import layer_filter
from .gravity_command_page import GravityCommandPage


class BlockElevationDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._torn_down = False
        self._running = False
        self.setWindowTitle("Paddy Irrigation Tools")
        self.resize(800, 700)
        self.setMinimumWidth(780)
        self._last_points, self._last_blocks, self._swapped = None, [], {}
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        self.steps = QTabWidget()
        self.steps.setDocumentMode(True)
        root.addWidget(self.steps)
        page1 = QWidget()
        outer = QVBoxLayout(page1)
        outer.setContentsMargins(0, 4, 0, 0)
        self.steps.addTab(page1, "Step 1 - High points")
        self.page2 = GravityCommandPage(on_help=lambda: open_docs('highest_point_per_block'),
                                        on_close=self.close)
        self.steps.addTab(self.page2, "Step 2 - Gravity command")
        self.steps.setTabToolTip(0, "Find the high (and low) ground of every paddy block from a DEM")
        self.steps.setTabToolTip(1, "Can every block be irrigated by gravity? Field - TC - SC - MC - Reservoir")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # ---- inputs ----
        g_in = QGroupBox("Inputs")
        fl = QFormLayout(g_in)
        self.dem_cb = QgsMapLayerComboBox()
        self.dem_cb.setFilters(layer_filter('RasterLayer'))
        self.band_cb = QgsRasterBandComboBox()
        # checkable list of polygon layers (process many layers in one run)
        self.blk_filter = QLineEdit()
        self.blk_filter.setPlaceholderText("Filter layers, e.g. BM")
        self.blk_list = QListWidget()
        self.blk_list.setMinimumHeight(80)
        self.blk_list.setMaximumHeight(110)
        btn_all, btn_none, btn_vis = QPushButton("Select All"), QPushButton("Clear"), QPushButton("Filtered")
        btn_all.setToolTip("Tick every polygon layer")
        btn_none.setToolTip("Untick every layer")
        btn_vis.setToolTip("Tick only the layers matching the filter text")
        hb_l = QHBoxLayout()
        hb_l.addWidget(self.blk_filter, 1)
        for bt in (btn_all, btn_none, btn_vis):
            bt.setMaximumWidth(90)
            hb_l.addWidget(bt)
        self.blk_count = QLabel("")
        vb_l = QVBoxLayout()
        vb_l.addLayout(hb_l)
        vb_l.addWidget(self.blk_list)
        vb_l.addWidget(self.blk_count)
        self.sel_chk = QCheckBox("Selected features only (layers that have a selection)")
        fl.addRow("DEM raster:", self.dem_cb)
        fl.addRow("Band:", self.band_cb)
        fl.addRow("Block layers:", vb_l)
        self.blk_filter.setToolTip("Polygon and line layers are listed.")
        fl.addRow("", self.sel_chk)
        btn_all.clicked.connect(lambda: self.set_all_checked(True))
        btn_none.clicked.connect(lambda: self.set_all_checked(False))
        btn_vis.clicked.connect(self.check_filtered)
        lay.addWidget(g_in)

        # ---- zones ----
        g_z = QGroupBox("Zones")
        vz = QVBoxLayout(g_z)
        self.rb_feat = QRadioButton("Each feature separately (one point per polygon / line)")
        self.rb_field = QRadioButton("Combine features with the same value of:")
        self.rb_feat.setChecked(True)
        self.field_cb = QComboBox()
        self.field_cb.setEditable(True)
        self.field_cb.setToolTip("Fields found in the ticked layers. Grouping is done inside each layer.")
        h = QHBoxLayout()
        h.addWidget(self.rb_field)
        h.addWidget(self.field_cb, 1)
        vz.addWidget(self.rb_feat)
        vz.addLayout(h)
        lay.addWidget(g_z)

        # ---- clean blocks ----
        g_c = QGroupBox("Clean blocks (working copy only - your layers are not changed)")
        fc = QFormLayout(g_c)
        fc.addRow("", QLabel("<i>Invalid geometries are always repaired.</i>"))
        self.snap_chk = QCheckBox("Snap boundaries that almost touch, within")
        self.snap_tol = QDoubleSpinBox()
        self.snap_tol.setRange(0.01, 50)
        self.snap_tol.setDecimals(2)
        self.snap_tol.setValue(0.5)
        self.snap_tol.setSuffix(" m")
        self.snap_chk.setChecked(True)
        hs = QHBoxLayout()
        hs.addWidget(self.snap_chk)
        hs.addWidget(self.snap_tol)
        hs.addStretch()
        fc.addRow("", hs)
        self.ovl_chk = QCheckBox("Remove small overlaps, up to")
        self.ovl_pct = QDoubleSpinBox()
        self.ovl_pct.setRange(0.1, 100)
        self.ovl_pct.setDecimals(1)
        self.ovl_pct.setValue(10)
        self.ovl_pct.setSuffix(" % of the smaller block")
        self.ovl_chk.setChecked(True)
        self.ovl_chk.setToolTip("The shared area stays with the larger block. Bigger overlaps "
                                "(e.g. a block drawn inside another) are only reported.")
        ho = QHBoxLayout()
        ho.addWidget(self.ovl_chk)
        ho.addWidget(self.ovl_pct)
        ho.addStretch()
        fc.addRow("", ho)
        self.ovl_chk.toggled.connect(self.ovl_pct.setEnabled)
        self.gap_chk = QCheckBox("Fill gaps between blocks up to")
        self.gap_max = QDoubleSpinBox()
        self.gap_max.setRange(0.1, 1000000)
        self.gap_max.setDecimals(1)
        self.gap_max.setValue(100)
        self.gap_max.setSuffix(" m²")
        self.gap_chk.setChecked(True)
        self.gap_chk.setToolTip("Only holes completely surrounded by blocks. Each gap is added to "
                                "the block sharing the longest border with it. Larger gaps "
                                "(roads, ponds) are only reported.")
        hg = QHBoxLayout()
        hg.addWidget(self.gap_chk)
        hg.addWidget(self.gap_max)
        hg.addStretch()
        fc.addRow("", hg)
        self.ring_chk = QCheckBox("Keep rings drawn inside a block as excluded areas (trees, ponds, houses)")
        self.ring_chk.setChecked(True)
        self.ring_chk.setToolTip("Rings (holes) you added with 'Add Ring' are never filled, so the "
                                 "highest / lowest point is searched only in the paddy area.")
        fc.addRow("", self.ring_chk)
        self.issue_chk = QCheckBox("Add a 'Topology_issues' layer showing what was found / fixed")
        self.issue_chk.setChecked(True)
        fc.addRow("", self.issue_chk)
        self.cleaned_chk = QCheckBox("Add the cleaned blocks as a layer (to check or save)")
        fc.addRow("", self.cleaned_chk)
        self.snap_chk.toggled.connect(self.snap_tol.setEnabled)
        self.gap_chk.toggled.connect(self.gap_max.setEnabled)

        # ---- attributes copied to the points ----
        g_a = QGroupBox("Point attributes (copied from the block holding the peak)")
        va = QVBoxLayout(g_a)
        self.attr_list = QListWidget()
        self.attr_list.setMaximumHeight(75)
        ha = QHBoxLayout()
        a_all, a_none = QPushButton("All"), QPushButton("None")
        for bt in (a_all, a_none):
            bt.setMaximumWidth(70)
        self.attr_info = QLabel("")
        ha.addWidget(self.attr_info, 1)
        ha.addWidget(a_all)
        ha.addWidget(a_none)
        va.addWidget(self.attr_list)
        va.addLayout(ha)
        a_all.clicked.connect(lambda: self.set_attrs_checked(True))
        a_none.clicked.connect(lambda: self.set_attrs_checked(False))
        self._attr_ticks = None  # remembered ticks by field name

        # ---- processing ----
        g_p = QGroupBox("Processing")
        fp = QFormLayout(g_p)
        self.dem_info = QLabel("DEM resolution: -")
        fp.addRow("", self.dem_info)

        self.method_cb = QComboBox()
        for label, val in METHODS:
            self.method_cb.addItem(label, val)
        self.method_cb.setToolTip("For irrigation design use 'Top of general slope': trees, dikes and "
                                  "huts are filtered out and the high side of the field is found. "
                                  "Applies to polygon blocks; lines always use the highest DEM cell.")
        fp.addRow("High / low point method:", self.method_cb)
        self.ground_win = QDoubleSpinBox()
        self.ground_win.setRange(0.5, 200)
        self.ground_win.setDecimals(1)
        self.ground_win.setValue(8)
        self.ground_win.setSuffix(" m")
        self.ground_win.setToolTip("Raised objects NARROWER than this are removed (tree crowns, "
                                   "dikes, huts). Make it wider than the widest tree crown / dike; "
                                   "real high ground wider than this is kept.")
        self.smooth_win = QDoubleSpinBox()
        self.smooth_win.setRange(0.5, 500)
        self.smooth_win.setDecimals(1)
        self.smooth_win.setValue(15)
        self.smooth_win.setSuffix(" m")
        self.smooth_win.setToolTip("Moving-average window on the ground surface. Larger = more "
                                   "'general slope', less local bumps (suggested 10-30 m).")
        hw = QHBoxLayout()
        hw.addWidget(QLabel("Ground filter"))
        hw.addWidget(self.ground_win)
        hw.addWidget(QLabel("  Smoothing"))
        hw.addWidget(self.smooth_win)
        hw.addStretch()
        fp.addRow("", hw)
        mnote = QLabel("<i>Trees, dikes and huts narrower than the ground filter are removed; "
                       "smoothing gives the general slope.</i>")
        self.method_cb.setToolTip(self.method_cb.toolTip() + "\n\nEach block also gets Slope_pct, "
                                  "Fall_m and Flow_dir (downslope direction) from a plane fit of its "
                                  "ground surface. 1 m with Average is fast and accurate enough.")
        fp.addRow("", mnote)

        def _method_changed(_=None):
            m = self.method_cb.currentData()
            self.ground_win.setEnabled(m in ("slope", "ground"))
            self.smooth_win.setEnabled(m == "slope")
        self.method_cb.currentIndexChanged.connect(_method_changed)
        _method_changed()

        self.res_cb = QComboBox()
        for label, val in RES_CHOICES:
            self.res_cb.addItem(label, val)
        self.res_custom = QDoubleSpinBox()
        self.res_custom.setRange(0.01, 10000)
        self.res_custom.setDecimals(2)
        self.res_custom.setValue(5)
        self.res_custom.setSuffix(" m")
        self.res_custom.setEnabled(False)
        hr = QHBoxLayout()
        hr.addWidget(self.res_cb, 1)
        hr.addWidget(self.res_custom)
        fp.addRow("Analysis resolution:", hr)

        self.resample_cb = QComboBox()
        self.resample_cb.setToolTip("The terrain methods always use Average.")
        self.resample_cb.addItem("Maximum (keeps the highest point, recommended)", 7)
        self.resample_cb.addItem("Average (smooths peaks)", 5)
        self.resample_cb.addItem("Nearest neighbour", 0)
        fp.addRow("When coarsening, use:", self.resample_cb)

        self.subdiv_chk = QCheckBox("Small-block safety (split cells for tiny blocks)")
        self.subdiv_chk.setToolTip("QGIS counts a DEM cell only if its centre is inside the block. "
                                   "When a block is only a few cells wide, cells are split "
                                   "(values unchanged) so the block still gets its cells.")
        self.subdiv_chk.setChecked(True)
        fp.addRow("", self.subdiv_chk)

        self.buf = QDoubleSpinBox()
        self.buf.setRange(0, 1000)
        self.buf.setDecimals(2)
        self.buf.setSuffix(" m")
        fp.addRow("Inner buffer:", self.buf)
        bnote = QLabel("<i>0 = whole block. 2–5 m skips dikes, roads and canal banks.</i>")
        fp.addRow("", bnote)

        # ---- output ----
        g_o = QGroupBox("Output")
        fo = QFormLayout(g_o)
        self.max_chk = QCheckBox("Highest point (▲)")
        self.min_chk = QCheckBox("Lowest point (▼)")
        self.max_chk.setToolTip("Red triangles in the layer HighestPoints")
        self.min_chk.setToolTip("Blue triangles in the layer LowestPoints; in write mode also "
                                "Lowest_elevation, MinElev_X, MinElev_Y on the blocks")
        self.max_chk.setChecked(True)
        hf_ = QHBoxLayout()
        hf_.addWidget(self.max_chk)
        hf_.addWidget(self.min_chk)
        hf_.addStretch()
        fo.addRow("Find:", hf_)
        self.rb_points = QRadioButton("Point layers only (block layers are not changed)")
        self.rb_write = QRadioButton("Points + write results into the block layers")
        self.rb_points.setChecked(True)
        fo.addRow("Create:", self.rb_points)
        fo.addRow("", self.rb_write)
        self.out_field = QLineEdit("Higher_elevation")
        self.dec = QSpinBox()
        self.dec.setRange(0, 6)
        self.dec.setValue(3)
        self.extra_chk = QCheckBox("Also write Elev_Min, Elev_Mean, Elev_Cells (QA)")
        self.extra_chk.setChecked(True)
        self.new_chk = QCheckBox("Create new output layers (leave the originals untouched)")
        self.xy_chk = QCheckBox("Find XY of the highest point (+ red point layer)")
        self.xy_chk.setToolTip("Adds MaxElev_X / MaxElev_Y to the blocks and a HighestPoints "
                               "layer with elev, X, Y, Lon, Lat and Top_cells.")
        self.extra_chk.setText("Also write Elev_Min, Elev_Mean, Elev_Cells to the blocks (QA)")
        self.xy_chk.setChecked(True)
        self.suffix_chk = QCheckBox("Add the resolution to names (e.g. HighestPoints_1m)")
        self.suffix_chk.setToolTip("Lets you run 0.5 m, 1 m and 30 m and compare the fields side by side.")
        fo.addRow("Decimals:", self.dec)
        fo.addRow("Field name:", self.out_field)
        fo.addRow("", self.extra_chk)
        fo.addRow("", self.new_chk)
        fo.addRow("", self.suffix_chk)
        self.xy_chk.setVisible(False)  # the highest point is always created now
        self._write_widgets = (self.out_field, self.extra_chk, self.new_chk)
        self.rb_write.toggled.connect(self.update_write_widgets)
        self.update_write_widgets()

        # ---- save to file ----
        g_f = QGroupBox("Save results to file (optional)")
        ff = QFormLayout(g_f)
        hf = QHBoxLayout()
        self.fmt_gpkg = QCheckBox("GeoPackage (.gpkg)")
        self.fmt_shp = QCheckBox("Shapefile (.shp)")
        self.fmt_xlsx = QCheckBox("Excel (.xlsx)")
        for c in (self.fmt_gpkg, self.fmt_shp, self.fmt_xlsx):
            hf.addWidget(c)
        hf.addStretch()
        ff.addRow("Formats:", hf)
        self.out_dir = QgsFileWidget()
        self.out_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self.out_dir.setDialogTitle("Choose output folder")
        ff.addRow("Folder:", self.out_dir)
        self.out_base = QLineEdit("Block_MaxElev")
        ff.addRow("File name:", self.out_base)
        self.load_chk = QCheckBox("Load saved GeoPackage / Shapefiles into QGIS")
        self.load_chk.setToolTip("The saved layers replace the temporary result layers in the project.")
        self.load_chk.setChecked(True)
        ff.addRow("", self.load_chk)
        note = QLabel("<i>Saves the point layers, Topology_issues, Summary and (if written) the block "
                      "layers.<br>Existing files are never overwritten: _1, _2 … is added.</i>")
        ff.addRow("", note)
        with suppress(Exception):
            self.out_dir.setFilePath(od.project_folder() or QgsProject.instance().homePath()
                                     or os.path.expanduser("~"))

        # ---- one tab per section (Inputs and Zones stay above the tabs) ----
        self.tabs = QTabWidget()
        for gb, name, tip in (
                (g_p, "Processing", "DEM resolution, high / low point method, inner buffer"),
                (g_c, "Clean blocks", "Works on a working copy - your layers are not changed"),
                (g_a, "Attributes", "Block fields copied onto the points"),
                (g_o, "Output", "What to find and what to create"),
                (g_f, "Save", "Save results to GeoPackage / Shapefile / Excel (optional)")):
            gb.setFlat(True)
            if gb is not g_p:
                gb.setTitle("")
            page = QWidget()
            QVBoxLayout(page).addWidget(gb)
            page.layout().setContentsMargins(2, 2, 2, 2)
            page.layout().addStretch()
            idx = self.tabs.addTab(page, name)
            self.tabs.setTabToolTip(idx, tip)
        g_p.setTitle("")
        g_c.setTitle("")
        # the tab area follows the page shown, not the tallest page (QTabWidget sizes to the
        # largest page, so the height is set from the current page)
        def _fit_tab(_=None):
            pg = self.tabs.currentWidget()
            if pg is None:
                return
            pg.layout().invalidate()
            h = pg.sizeHint().height() + self.tabs.tabBar().sizeHint().height() + 8
            if h == self.tabs.height():
                return
            self.tabs.setFixedHeight(h)
            self._fit_window()
        self.tabs.currentChanged.connect(_fit_tab)
        self._fit_tab = _fit_tab
        self._s1_scroll, self._s1_inner = scroll, inner
        _fit_tab()
        self.page2.tabs.currentChanged.connect(lambda _: QTimer.singleShot(0, self._fit_window))
        self.steps.currentChanged.connect(lambda _: QTimer.singleShot(0, self._fit_window))
        lay.addWidget(self.tabs)
        lay.addStretch(1)  # spare height goes below the tabs, never into Inputs / Zones

        # compact sections: tighter margins and spacing
        lay.setSpacing(4)
        lay.setContentsMargins(6, 4, 6, 4)
        for gb in inner.findChildren(QGroupBox):
            gl = gb.layout()
            if gl is not None:
                gl.setContentsMargins(6, 2, 6, 4)
                gl.setSpacing(3)

        # ---- log (collapsed by default) + buttons ----
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(120)
        self.log_group = QGroupBox("Log")
        self.log_group.setCheckable(True)
        lg = QVBoxLayout(self.log_group)
        lg.setContentsMargins(6, 2, 6, 4)
        lg.addWidget(self.log_box)
        self.log_group.toggled.connect(self.log_box.setVisible)
        self.log_group.setChecked(False)
        self.log_box.setVisible(False)
        outer.addWidget(self.log_group, 0)
        hb = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.next_btn = QPushButton("Next: Step 2 ▶")
        self.next_btn.setToolTip("Open step 2 with these blocks and the new HighestPoints layer")
        self.next_btn.setEnabled(False)
        close_btn = QPushButton("Close")
        help_btn = QPushButton("Help")
        hb.addWidget(help_btn)
        hb.addStretch()
        hb.addWidget(self.run_btn)
        hb.addWidget(self.next_btn)
        hb.addWidget(close_btn)
        outer.addLayout(hb)

        # dropdowns may shrink to the window width instead of their longest item
        for cb in (self.dem_cb, self.band_cb, self.res_cb, self.resample_cb, self.field_cb):
            with suppress(Exception):
                cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                cb.setMinimumContentsLength(12)
        for w in (self.blk_list, self.log_box):
            w.setMinimumWidth(0)

        # ---- signals ----
        self.dem_cb.layerChanged.connect(self.band_cb.setLayer)
        self.dem_cb.layerChanged.connect(self.update_dem_info)
        self.res_cb.currentIndexChanged.connect(
            lambda i: self.res_custom.setEnabled(self.res_cb.currentData() == "custom"))
        self.blk_list.itemChanged.connect(lambda _: self.on_blocks_changed())
        self.blk_filter.textChanged.connect(self.apply_filter)
        prj = QgsProject.instance()
        prj.layersAdded.connect(self._layers_changed)
        prj.legendLayersAdded.connect(self._layers_changed)
        prj.layersRemoved.connect(self._layers_changed)
        self.run_btn.clicked.connect(self.on_run)
        close_btn.clicked.connect(self.close)
        self.next_btn.clicked.connect(self.go_next)
        help_btn.clicked.connect(lambda: open_docs('highest_point_per_block'))

        self.preselect()

    def _fit_window(self):
        """The window follows the tab shown: its height is everything outside the scroll area
        plus the full content, so the sections above the tabs keep their size."""
        if not self.isVisible() or self.isMaximized() or self.isFullScreen():
            return
        if self.steps.currentIndex() == 0:
            sc, content = self._s1_scroll, self._s1_inner
        else:
            sc, content = self.page2.scroll, self.page2.content
        self.resize(self.width(), self.height() - sc.height() + content.sizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_fit_tab"):  # a new width re-wraps the notes, so the height changes
            QTimer.singleShot(0, self._fit_tab)  # after the layout has given the labels their width

    def showEvent(self, e):
        super().showEvent(e)
        if hasattr(self, "_fit_tab"):
            QTimer.singleShot(0, self._fit_tab)

    # ---------- lifecycle ----------
    def _layers_changed(self, *_):
        if not self._running:
            self.fill_layer_list()

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    def shutdown(self):
        """Close or plugin unload: stop listening to the project, hide the window."""
        if self._torn_down:
            return
        self._torn_down = True
        self.page2.shutdown()
        prj = QgsProject.instance()
        for sig in (prj.layersAdded, prj.legendLayersAdded, prj.layersRemoved):
            with suppress(TypeError, RuntimeError):
                sig.disconnect(self._layers_changed)
        self.hide()

    # ---------- helpers ----------
    def preselect(self):
        layers = list(QgsProject.instance().mapLayers().values())
        for lyr in layers:  # a single-band raster that is not the imagery
            if isinstance(lyr, QgsRasterLayer) and lyr.bandCount() == 1 \
                    and not lyr.source().lower().endswith(".ecw"):
                self.dem_cb.setLayer(lyr)
                break
        self.band_cb.setLayer(self.dem_cb.currentLayer())
        self.update_dem_info(self.dem_cb.currentLayer())
        self.fill_layer_list()

    # ---------- block layer list ----------
    @staticmethod
    def is_output_layer(lyr):
        return (lyr.name().endswith("_MaxElev") or lyr.providerType() == "memory"
                or lyr.name().startswith("HighestPoint"))

    def fill_layer_list(self):
        """(Re)build the list, keeping the ticks of layers that are still there."""
        try:
            self.blk_list.blockSignals(True)
        except RuntimeError:  # dialog already closed
            return
        checked = set(self.checked_layer_ids())
        first = self.blk_list.count() == 0
        self.blk_list.clear()
        root = QgsProject.instance().layerTreeRoot()
        ordered = [n.layer() for n in root.findLayers() if n.layer() is not None]
        in_tree = {lyr.id() for lyr in ordered}
        # layers not (yet) in the Layers panel are listed too
        ordered += [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.id() not in in_tree]
        for lyr in ordered:
            if not (isinstance(lyr, QgsVectorLayer) and lyr.isValid()
                    and lyr.geometryType() in (POLYGON_GEOM, LINE_GEOM)):
                continue
            nsel = lyr.selectedFeatureCount()
            kind = "line" if lyr.geometryType() == LINE_GEOM else "polygon"
            text = f"{lyr.name()}   ({kind}, {lyr.featureCount()} features" + \
                   (f", {nsel} selected)" if nsel else ")")
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, lyr.id())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            tick = (lyr.id() in checked) if not first else \
                   ("block" in lyr.name().lower() and not self.is_output_layer(lyr))
            it.setCheckState(Qt.CheckState.Checked if tick else Qt.CheckState.Unchecked)
            it.setToolTip(lyr.source())
            self.blk_list.addItem(it)
        self.blk_list.blockSignals(False)
        self.apply_filter(self.blk_filter.text())
        self.on_blocks_changed()

    def checked_layer_ids(self):
        ids = []
        for i in range(self.blk_list.count()):
            it = self.blk_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        return ids

    def checked_layers(self):
        prj = QgsProject.instance()
        return [prj.mapLayer(i) for i in self.checked_layer_ids() if prj.mapLayer(i) is not None]

    def set_checked_layers(self, layers):
        ids = {lyr.id() for lyr in layers}
        self.blk_list.blockSignals(True)
        for i in range(self.blk_list.count()):
            it = self.blk_list.item(i)
            on = it.data(Qt.ItemDataRole.UserRole) in ids
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.blk_list.blockSignals(False)
        self.on_blocks_changed()

    def set_all_checked(self, on):
        self.blk_list.blockSignals(True)
        for i in range(self.blk_list.count()):
            it = self.blk_list.item(i)
            layer = QgsProject.instance().mapLayer(it.data(Qt.ItemDataRole.UserRole))
            tick = on and not (layer is not None and self.is_output_layer(layer))
            it.setCheckState(Qt.CheckState.Checked if tick else Qt.CheckState.Unchecked)
        self.blk_list.blockSignals(False)
        self.on_blocks_changed()

    def check_filtered(self):
        self.blk_list.blockSignals(True)
        for i in range(self.blk_list.count()):
            it = self.blk_list.item(i)
            it.setCheckState(Qt.CheckState.Unchecked if it.isHidden() else Qt.CheckState.Checked)
        self.blk_list.blockSignals(False)
        self.on_blocks_changed()

    def apply_filter(self, text):
        t = (text or "").strip().lower()
        for i in range(self.blk_list.count()):
            it = self.blk_list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def update_dem_info(self, layer):
        if layer is None or not isinstance(layer, QgsRasterLayer):
            self.dem_info.setText("DEM resolution: -")
            return
        cx, cy = layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY()
        unit = "deg" if layer.crs().isGeographic() else "m"
        self.dem_info.setText(f"<i>Detected DEM resolution: {cx:g} x {cy:g} {unit} "
                              f"({layer.width()} x {layer.height()} cells, {layer.crs().authid()})</i>")
        self.res_cb.setItemText(0, f"Native ({cx:g} {unit})")

    def on_blocks_changed(self):
        layers = self.checked_layers()
        n_feat = sum(lyr.featureCount() for lyr in layers)
        self.blk_count.setText(f"<i>{len(layers)} layer(s) ticked, {n_feat} features in total</i>")
        # fields available in the ticked layers (text and whole numbers)
        current = self.field_cb.currentText()
        names = []
        for lyr in layers:
            for f in lyr.fields():
                if int(f.type()) in (T_STRING, T_INT, T_LONGLONG) \
                        or f.typeName().lower() in ("string", "text", "integer", "integer64", "int"):
                    if f.name() not in names:
                        names.append(f.name())
        self.field_cb.blockSignals(True)
        self.field_cb.clear()
        self.field_cb.addItems(names)
        pick = current if current in names else ("layer" if "layer" in names else
                                                  (names[0] if names else ""))
        self.field_cb.setCurrentText(pick)
        self.field_cb.blockSignals(False)
        self.fill_attr_list(layers)

    def load_saved(self, saved, pairs):
        """Add the saved GeoPackage (or Shapefiles) to QGIS and drop the temporary copies.
        pairs = [(table_name, temporary_layer)]; only temporary (memory) layers are replaced."""
        prj = QgsProject.instance()
        swap = []
        for tname, temp in pairs:
            if temp is None or temp.providerType() != "memory":
                continue
            if "gpkg" in saved:
                src = f"{saved['gpkg']}|layername={tname}"
            elif "shp" in saved:
                src = os.path.join(saved["shp"], tname + ".shp")
            else:
                continue
            lyr = QgsVectorLayer(src, temp.name(), "ogr")
            if lyr.isValid():
                if lyr.geometryType() == temp.geometryType():
                    lyr.setRenderer(temp.renderer().clone())  # keep the triangles / issue colours
                # keep the Error Inspector stamp on the saved copy of Topology_issues
                prop = "kga_tools/topology_error_layer"
                if temp.customProperty(prop):
                    lyr.setCustomProperty(prop, temp.customProperty(prop))
                swap.append((temp, lyr))
        if not swap:
            return
        old_ids = [t.id() for t, _ in swap]  # read the ids BEFORE the temporary layers are deleted
        prj.removeMapLayers(old_ids)
        prj.addMapLayers([lyr for _, lyr in swap])
        for oid, (_, lyr) in zip(old_ids, swap):
            self._swapped[oid] = lyr.id()  # lets 'Next' follow the result to its saved copy
        self.log(f"Loaded {len(swap)} saved layer(s) into QGIS (temporary copies removed).")

    def go_next(self):
        """Step 2 with the blocks and the HighestPoints layer this run made."""
        prj = QgsProject.instance()
        pts = prj.mapLayer(self._last_points) if self._last_points else None  # None if removed
        blocks = [prj.mapLayer(i) for i in self._last_blocks if prj.mapLayer(i) is not None]
        self.page2.set_inputs(blocks[0] if blocks else None, pts)
        self.steps.setCurrentIndex(1)

    def update_write_widgets(self, *_):
        on = self.rb_write.isChecked()
        for w in self._write_widgets:
            w.setEnabled(on)

    def set_attrs_checked(self, on):
        for i in range(self.attr_list.count()):
            self.attr_list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def fill_attr_list(self, layers):
        """All fields of the ticked layers; keeps the user's ticks by name."""
        if self._attr_ticks is not None:  # remember what the user ticked in the current list
            shown = {self.attr_list.item(i).data(Qt.ItemDataRole.UserRole)
                     for i in range(self.attr_list.count())}
            self._attr_ticks = set(self.checked_attrs()) | (self._attr_ticks - shown)
        names, where = [], {}
        for lyr in layers:
            for f in lyr.fields():
                if f.name() == KEY:
                    continue
                if f.name() not in where:
                    names.append(f.name())
                    where[f.name()] = []
                where[f.name()].append(lyr.name())
        self.attr_list.clear()
        for n in names:
            label = n if len(where[n]) == len(layers) else f"{n}   (only in: {', '.join(where[n][:3])}" + \
                ("…)" if len(where[n]) > 3 else ")")
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, n)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if self._attr_ticks is None:  # first time: tick 'layer' / name-like fields
                on = n.lower() in ("layer", "name", "block", "block_id", "blockname", "id")
            else:
                on = n in self._attr_ticks
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.attr_list.addItem(it)
        if names and self._attr_ticks is None:
            self._attr_ticks = set(self.checked_attrs())
        self.attr_info.setText("<i>Tick the fields that identify each block.</i>" if names else "<i>No fields in the ticked layers.</i>")

    def checked_attrs(self):
        out = []
        for i in range(self.attr_list.count()):
            it = self.attr_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def carry_definitions(self, layers):
        """[(source_name, output_name)], [QgsField] for the ticked attributes."""
        carry, defs, used = [], [], set(RESERVED)
        for n in self.checked_attrs():
            fields = [lyr.fields().field(lyr.fields().indexOf(n)) for lyr in layers
                      if lyr.fields().indexOf(n) != -1]
            if not fields:
                continue
            out = n if n.lower() not in used else f"blk_{n}"
            used.add(out.lower())
            fd = QgsField(fields[0])
            fd.setName(out)
            if len({f.type() for f in fields}) > 1:  # different types in different layers
                fd = make_field(out, T_STRING)
            carry.append((n, out))
            defs.append(fd)
        return carry, defs

    def selected_res(self):
        v = self.res_cb.currentData()
        return self.res_custom.value() if v == "custom" else v

    def log(self, msg):
        self.log_box.appendPlainText(str(msg))
        QApplication.processEvents()

    # ---------- run ----------
    def on_run(self):
        layers = self.checked_layers()
        if not layers:
            QMessageBox.warning(self, "No block layer", "Tick at least one block layer.")
            return
        if self.rb_field.isChecked():
            lacking = [lyr_.name() for lyr_ in layers if lyr_.fields().indexOf(self.field_cb.currentText()) == -1]
            if lacking:
                self.log(f"Note: '{self.field_cb.currentText()}' is missing in: {', '.join(lacking)}"
                         " - these layers will report an error.")
        base = {
            "dem": self.dem_cb.currentLayer(),
            "band": max(1, self.band_cb.currentBand()),
            "selected_only": self.sel_chk.isChecked(),
            "mode": "field" if self.rb_field.isChecked() else "feature",
            "zone_field": self.field_cb.currentText().strip(),
            "target_res": self.selected_res(),
            "resample": self.resample_cb.currentData(),
            "method": self.method_cb.currentData(),
            "ground_win": self.ground_win.value(),
            "smooth_win": self.smooth_win.value(),
            "subdivide": self.subdiv_chk.isChecked(),
            "suffix_res": self.suffix_chk.isChecked(),
            "find_xy": self.xy_chk.isChecked(),
            "inner_buffer": self.buf.value(),
            "out_field": self.out_field.text().strip() or "Higher_elevation",
            "decimals": self.dec.value(),
            "write_extra": self.extra_chk.isChecked(),
            "new_layer": self.new_chk.isChecked(),
            "write_blocks": self.rb_write.isChecked(),
        }
        base["find_max"] = self.max_chk.isChecked()
        base["find_min"] = self.min_chk.isChecked()
        base["find_xy"] = base["find_max"]
        if not (base["find_max"] or base["find_min"]):
            QMessageBox.warning(self, "Nothing to find", "Tick 'Highest point' and/or 'Lowest point'.")
            return
        carry, carry_defs = self.carry_definitions(layers)
        base["carry"] = carry
        base["label_field"] = carry[0][0] if carry else None
        base["snap_tol"] = self.snap_tol.value() if self.snap_chk.isChecked() else 0
        base["fix_overlaps"] = self.ovl_chk.isChecked()
        base["overlap_pct"] = self.ovl_pct.value()
        base["gap_max"] = self.gap_max.value() if self.gap_chk.isChecked() else 0
        base["keep_rings"] = self.ring_chk.isChecked()
        if base["write_blocks"] and not base["new_layer"]:
            editing = [lyr_.name() for lyr_ in layers if lyr_.isEditable()]
            if editing:
                QMessageBox.warning(self, "Layer in edit mode",
                                    "Save and stop editing these layers first, or tick "
                                    "'Create a new output layer':\n\n" + "\n".join(editing))
                return

        self.run_btn.setEnabled(False)
        self._running = True
        self.log_group.setChecked(True)             # show progress while it runs
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        t0 = time.time()
        summary, all_points, failed, sfx, results = [], [], [], "", []
        all_issues, cleaned, all_points_min = [], [], []
        formats = [k for k, c in (("gpkg", self.fmt_gpkg), ("shp", self.fmt_shp),
                                  ("xlsx", self.fmt_xlsx)) if c.isChecked()]
        folder = self.out_dir.filePath().strip()
        if formats and not folder:
            QMessageBox.warning(self, "Output folder", "Choose a folder to save the files in.")
            QApplication.restoreOverrideCursor()
            self.run_btn.setEnabled(True)
            self._running = False
            return
        try:
            for n, layer in enumerate(layers, 1):
                self.log("=" * 60)
                self.log(f"[{n}/{len(layers)}] {layer.name()}")
                p = dict(base, blocks=layer)
                shp = layer.dataProvider().storageType() == "ESRI Shapefile"
                if shp and len(p["out_field"]) > 10:
                    p["out_field"] = "Hi_Elev"
                    self.log("  (shapefile: field name shortened to Hi_Elev)")
                try:
                    ok, missing, records, sfx, result_layer = run_analysis(p, self.log)
                    all_issues += p.get("_issues", [])
                    if self.cleaned_chk.isChecked() and p.get("_zones") is not None \
                            and layer.geometryType() == POLYGON_GEOM:
                        cleaned.append((layer.name(), p["_zones"]))
                    summary.append((layer.name(), ok, len(missing), ""))
                    if result_layer is not None:
                        results.append((layer.name(), result_layer))
                    all_points += records
                    all_points_min += p.get("_records_min", [])
                except Exception as e:
                    self.log("ERROR: " + str(e))
                    self.log(traceback.format_exc())
                    summary.append((layer.name(), 0, 0, str(e)))
                    failed.append(layer.name())

            points_layer = low_layer = None
            pts_id = None
            self._swapped = {}
            self._last_blocks = [lyr.id() for lyr in layers if lyr.geometryType() == POLYGON_GEOM]
            npts, npts_min = len(all_points), len(all_points_min)
            if all_points or all_points_min:
                self.log("=" * 60)
            if all_points:
                points_layer = add_point_layer(all_points, base["dem"].crs(), "HighestPoints" + sfx,
                                               self.log, carry_defs, base["decimals"])
                pts_id = points_layer.id()  # the layer may be replaced by its saved copy below
            if all_points_min:
                low_layer = add_point_layer(all_points_min, base["dem"].crs(), "LowestPoints" + sfx,
                                            self.log, carry_defs, base["decimals"], lowest=True)
            issues_layer = None
            if all_issues and self.issue_chk.isChecked():
                issues_layer = add_issue_layer(all_issues, base["dem"].crs(), self.log)
            elif self.issue_chk.isChecked():
                self.log("No topology issues found.")
            for lname, z in cleaned:
                cl = QgsVectorLayer("Polygon", f"{lname}_cleaned", "memory")
                cl.setCrs(z.crs())
                keep = [f for f in z.fields() if f.name() != KEY]
                cl.dataProvider().addAttributes(keep)
                cl.updateFields()
                nf = []
                for f in z.getFeatures():
                    q = QgsFeature(cl.fields())
                    q.setGeometry(f.geometry())
                    q.setAttributes([f[k.name()] for k in keep])
                    nf.append(q)
                cl.dataProvider().addFeatures(nf)
                QgsProject.instance().addMapLayer(cl)
                self.log(f"Cleaned blocks added: {cl.name()} ({len(nf)} features)")

            saved = {}
            if formats and (results or points_layer is not None or low_layer is not None):
                self.log("=" * 60)
                res_txt = (f"{base['target_res']:g} m" if base["target_res"]
                           else f"native {base['dem'].rasterUnitsPerPixelX():g}")
                try:
                    extra = [("__raw__", lyr_) for lyr_ in (issues_layer,) if lyr_ is not None]
                    saved = export_results(results + extra, [points_layer, low_layer],
                                           summary_table(summary, res_txt), folder,
                                           self.out_base.text(), formats, res_txt, self.log,
                                           long_name=base["out_field"])
                    if self.load_chk.isChecked():
                        pairs = [(table_name(n, lyr_), lyr_) for n, lyr_ in results + extra]
                        pairs += [(safe_name(lyr_.name()), lyr_) for lyr_ in (points_layer, low_layer) if lyr_ is not None]
                        self.load_saved(saved, pairs)
                except Exception as e:
                    self.log("ERROR while saving: " + str(e))
                    self.log(traceback.format_exc())
                    failed.append("saving files")

            # summary table
            self.log("=" * 60)
            self.log(f"{'Layer':<30}{'with value':>12}{'no data':>10}  status")
            for name, ok, nmiss, err in summary:
                self.log(f"{name[:29]:<30}{ok:>12}{nmiss:>10}  {'ERROR: ' + err[:60] if err else 'ok'}")
            tot = sum(r[1] for r in summary)
            self.log(f"Total: {tot} block(s) in {len(layers) - len(failed)} layer(s), "
                     f"{time.time() - t0:.1f} s")
            QApplication.restoreOverrideCursor()
            found = []
            if base["find_max"]:
                found.append(f"{npts} highest point(s)")
            if base["find_min"]:
                found.append(f"{npts_min} lowest point(s)")
            msg = (f"{' and '.join(found)} found for {tot} block(s) in "
                   f"{len(layers) - len(failed)} layer(s).")
            if base["mode"] == "field":
                msg += (f"\n\nNote: 'Combine features with the same value of: {base['zone_field']}' "
                        "is selected, so blocks sharing that value got ONE point together. "
                        "Choose 'Each feature separately' for one point per block.")
            if all_issues:
                msg += f"\n\n{len(all_issues)} topology item(s) found/fixed - see Topology_issues."
            if saved:
                msg += "\n\nSaved:\n" + "\n".join(saved.values())
            if failed:
                msg += f"\n\n{len(failed)} layer(s) failed (see log): " + ", ".join(failed[:10])
                QMessageBox.warning(self, "Finished with errors", msg)
            else:
                QMessageBox.information(self, "Finished", msg)
            # by layer ID, not by name: every run creates a layer called 'HighestPoints'
            self._last_points = self._swapped.get(pts_id, pts_id) if pts_id else None
            self.next_btn.setEnabled(bool(self._last_points and self._last_blocks))
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.log("ERROR: " + str(e))
            self.log(traceback.format_exc())
            QMessageBox.critical(self, "Error", str(e))
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            self._running = False
            self.run_btn.setEnabled(True)

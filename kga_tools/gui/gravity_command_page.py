# -*- coding: utf-8 -*-
"""Paddy Irrigation Tools - step 2: gravity command check.

Can every block be irrigated by gravity? Takes the blocks and the high point of each
block (the points step 1 makes), optional canals and a reservoir level, and reports the
head blocks, plot-to-plot supply, canal turnouts and the water level each canal needs.
The work is `core/gravity_command.run_command`, called on the GUI thread.
"""

import os
import traceback
from contextlib import suppress

from qgis.core import Qgis, QgsFieldProxyModel, QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox, QgsRasterBandComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QScrollArea, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..core import output_defaults as od
from ..core.block_elevation import POLYGON_GEOM
from ..core.gravity_command import run_command, save_gpkg
from ..core.modify_features import layer_filter
from ..core.compat import enum_value

FF_NUM = enum_value((Qgis, 'FieldFilter.Numeric'), (QgsFieldProxyModel, 'Filter.Numeric'))   # Qgis.FieldFilter: QGIS 3.34+


def dspin(lo, hi, val, dec=2, suffix=" m", tip=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    s.setSuffix(suffix)
    if tip:
        s.setToolTip(tip)
    return s


class FitTabWidget(QTabWidget):
    """A tab widget as tall as the page being shown, not as the tallest page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _: self.updateGeometry())

    def _fit(self, hint):
        page = self.currentWidget()
        if page is not None:
            hint.setHeight(page.sizeHint().height() + self.tabBar().sizeHint().height() + 8)
        return hint

    def sizeHint(self):
        return self._fit(super().sizeHint())

    def minimumSizeHint(self):
        return self._fit(super().minimumSizeHint())


class ReportDialog(QDialog):
    """Results of the last run in plain language, with 'Save as HTML'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gravity command - results")
        self.resize(820, 720)
        self._html = ""
        lay = QVBoxLayout(self)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view)
        hb = QHBoxLayout()
        save_btn = QPushButton("Save as HTML...")
        close_btn = QPushButton("Close")
        hb.addWidget(save_btn)
        hb.addStretch()
        hb.addWidget(close_btn)
        lay.addLayout(hb)
        save_btn.clicked.connect(self.save)
        close_btn.clicked.connect(self.close)

    def set_html(self, html_text):
        self._html = html_text
        self.view.setHtml(html_text)

    def save(self):
        start = os.path.join(od.project_folder() or QgsProject.instance().homePath()
                             or os.path.expanduser("~"), "Gravity_command_report.html")
        path, _ = QFileDialog.getSaveFileName(self, "Save report", start, "HTML (*.html)")
        if not path:
            return
        if not path.lower().endswith((".html", ".htm")):
            path += ".html"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._html)
        except OSError as err:
            QMessageBox.warning(self, "Save report", str(err))


class GravityCommandPage(QWidget):
    def __init__(self, parent=None, on_help=None, on_close=None):
        super().__init__(parent)
        self._report_dlg = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        lay = QVBoxLayout(self.content)
        lay.setSpacing(4)
        lay.setContentsMargins(6, 4, 6, 4)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

        # ---- blocks and high points
        g1 = QGroupBox()
        f1 = QFormLayout(g1)
        self.blk_cb = QgsMapLayerComboBox()
        self.blk_cb.setFilters(layer_filter('PolygonLayer'))
        self.lbl_cb = QgsFieldComboBox()
        self.lbl_cb.setAllowEmptyFieldName(True)
        self.sel_chk = QCheckBox("Selected blocks only")
        self.pt_cb = QgsMapLayerComboBox()
        self.pt_cb.setFilters(layer_filter('PointLayer'))
        self.elev_cb = QgsFieldComboBox()
        self.elev_cb.setFilters(FF_NUM)
        self.resample_chk = QCheckBox("Re-sample elevation from the DEM (use this if you moved points)")
        self.sample_r = dspin(0.1, 50, 1.5, 1, " m", "Median of the DEM cells within this radius")
        self.dem_cb = QgsMapLayerComboBox()
        self.dem_cb.setFilters(layer_filter('RasterLayer'))
        self.dem_cb.setAllowEmptyLayer(True)
        self.band_cb = QgsRasterBandComboBox()
        self.match_tol = dspin(0, 50, 5, 1, " m", "A point just outside a block (on its dike) is "
                               "still matched if it is within this distance")
        f1.addRow("Block layer:", self.blk_cb)
        f1.addRow("Block name field:", self.lbl_cb)
        f1.addRow("", self.sel_chk)
        f1.addRow("High-point layer:", self.pt_cb)
        f1.addRow("Elevation field:", self.elev_cb)
        hr = QHBoxLayout()
        hr.addWidget(self.resample_chk)
        hr.addWidget(QLabel("radius"))
        hr.addWidget(self.sample_r)
        f1.addRow("", hr)
        f1.addRow("DEM (optional):", self.dem_cb)
        f1.addRow("Band:", self.band_cb)
        f1.addRow("Point-to-block tolerance:", self.match_tol)
        f1.addRow("", QLabel("<i>The DEM also gives the ground under each canal and orients main canals.</i>"))

        # ---- field requirements
        g2 = QGroupBox()
        f2 = QFormLayout(g2)
        self.depth = dspin(0, 2, 0.10, 2, " m", "Water depth needed over the HIGH point of the field")
        self.spill = dspin(0, 2, 0.05, 2, " m", "Head lost flowing from one plot to the next "
                           "(bund cut / pipe). A can feed B when top_A - top_B >= this")
        self.adj_tol = dspin(0, 50, 3, 1, " m", "Blocks separated by a dike / gap up to this width "
                             "are neighbours")
        self.min_shared = dspin(0, 500, 5, 1, " m", "Minimum common border (corner contacts ignored)")
        self.casc_chk = QCheckBox("Prefer plot-to-plot supply (turnouts only where needed)")
        self.casc_chk.setToolTip("Off: every block within reach of a canal gets its own turnout.\n"
                                 "On: a block is fed from a served higher neighbour when possible.")
        f2.addRow("Field water depth:", self.depth)
        f2.addRow("Plot-to-plot spill loss:", self.spill)
        f2.addRow("Neighbour gap tolerance:", self.adj_tol)
        f2.addRow("Minimum shared border:", self.min_shared)
        f2.addRow("", self.casc_chk)

        # ---- canals
        g3 = QGroupBox()
        f3 = QFormLayout(g3)
        self.can_cb = QgsMapLayerComboBox()
        self.can_cb.setFilters(layer_filter('LineLayer'))
        self.can_cb.setAllowEmptyLayer(True)
        self.cname_cb = QgsFieldComboBox()
        self.cname_cb.setAllowEmptyFieldName(True)
        self.clevel_cb = QgsFieldComboBox()
        self.clevel_cb.setAllowEmptyFieldName(True)
        self.clevel_cb.setToolTip("Values like MC / SC / TC, Main / Secondary / Tertiary or 1 / 2 / 3")
        self.dissolve_chk = QCheckBox("Join pieces with the same name into one canal")
        self.dissolve_chk.setChecked(True)
        self.dir_cb = QComboBox()
        self.dir_cb.addItem("Auto (upstream = junction with parent canal)", "auto")
        self.dir_cb.addItem("As digitised (first vertex = upstream)", "digitised")
        self.search = dspin(0, 2000, 100, 0, " m", "Blocks farther than this from a canal cannot "
                            "get a direct turnout")
        self.prefer_cb = QComboBox()
        self.prefer_cb.addItem("Lowest-level canal first (TC, then SC, then MC)", "lowest")
        self.prefer_cb.addItem("Nearest canal", "nearest")
        self.junc_tol = dspin(0, 100, 5, 1, " m", "A canal end within this distance of another canal "
                              "is a junction")
        self.tloss = dspin(0, 2, 0.05, 2, " m", "Head loss through the turnout / offtake gate")
        self.fgrad = dspin(0, 50, 0.5, 2, " m/km", "Water surface slope of the field channel "
                           "from the turnout to the high point")
        self.cgrad = dspin(0, 50, 0.2, 2, " m/km", "Water surface slope of the canals (design mode "
                           "and ground profile)")
        self.sloss = dspin(0, 2, 0.10, 2, " m", "Head loss at each canal offtake structure "
                           "(SC from MC, TC from SC, MC from reservoir)")
        f3.addRow("Canal layer:", self.can_cb)
        f3.addRow("Name field:", self.cname_cb)
        f3.addRow("Level field (MC/SC/TC):", self.clevel_cb)
        f3.addRow("", self.dissolve_chk)
        f3.addRow("Flow direction:", self.dir_cb)
        f3.addRow("Junction tolerance:", self.junc_tol)
        f3.addRow("Turnout search distance:", self.search)
        f3.addRow("Serve from:", self.prefer_cb)
        f3.addRow("Turnout loss:", self.tloss)
        f3.addRow("Field-channel gradient:", self.fgrad)
        f3.addRow("Canal gradient:", self.cgrad)
        f3.addRow("Structure loss:", self.sloss)
        self.rb_design = QRadioButton("DESIGN: compute the water level each canal needs")
        self.rb_check = QRadioButton("CHECK: compare with canal water levels in the attributes")
        self.rb_design.setChecked(True)
        f3.addRow("Mode:", self.rb_design)
        f3.addRow("", self.rb_check)
        self.wlmode_cb = QComboBox()
        self.wlmode_cb.addItem("One water level per feature", "const")
        self.wlmode_cb.addItem("Start and end water level per feature", "startend")
        self.wl1_cb = QgsFieldComboBox()
        self.wl1_cb.setFilters(FF_NUM)
        self.wl1_cb.setAllowEmptyFieldName(True)
        self.wl2_cb = QgsFieldComboBox()
        self.wl2_cb.setFilters(FF_NUM)
        self.wl2_cb.setAllowEmptyFieldName(True)
        f3.addRow("Water level from:", self.wlmode_cb)
        f3.addRow("WL field (or start WL):", self.wl1_cb)
        f3.addRow("End WL field:", self.wl2_cb)

        # ---- reservoir
        g4 = QGroupBox()
        f4 = QFormLayout(g4)
        self.res_chk = QCheckBox("Check against reservoir full supply level (FSL)")
        self.res_fsl = dspin(-100, 5000, 20, 2, " m")
        hres = QHBoxLayout()
        hres.addWidget(self.res_chk)
        hres.addWidget(self.res_fsl)
        hres.addStretch()
        f4.addRow("", hres)
        self.res_cb = QgsMapLayerComboBox()
        self.res_cb.setFilters(layer_filter('PointLayer'))
        self.res_cb.setAllowEmptyLayer(True)
        self.res_cb.setToolTip("A point at the reservoir / intake: the main canal starts at its end "
                               "nearest to this point")
        f4.addRow("Reservoir / intake point:", self.res_cb)

        # ---- save
        g5 = QGroupBox()
        f5 = QFormLayout(g5)
        self.save_chk = QCheckBox("Save all result layers to a GeoPackage")
        self.save_path = QgsFileWidget()
        self.save_path.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.save_path.setFilter("GeoPackage (*.gpkg)")
        with suppress(Exception):
            self.save_path.setFilePath(os.path.join(
                od.project_folder() or QgsProject.instance().homePath() or os.path.expanduser("~"),
                "Gravity_command.gpkg"))
        f5.addRow("", self.save_chk)
        f5.addRow("File:", self.save_path)
        f5.addRow("", QLabel("<i>A report (.html) is saved next to the GeoPackage.</i>"))

        # ---- one tab per section, as tall as the page shown
        self.tabs = FitTabWidget()
        for gb, name, tip in (
                (g1, "Blocks", "Block layer, high points and the DEM"),
                (g2, "Field", "Water depth and plot-to-plot supply"),
                (g3, "Canals", "Canal layer, losses and the DESIGN / CHECK mode (optional)"),
                (g4, "Reservoir", "Full supply level and intake point (optional)"),
                (g5, "Save", "Save the result layers and report (optional)")):
            gb.setFlat(True)
            page = QWidget()
            QVBoxLayout(page).addWidget(gb)
            page.layout().setContentsMargins(2, 2, 2, 2)
            page.layout().addStretch()
            self.tabs.setTabToolTip(self.tabs.addTab(page, name), tip)
        intro = QLabel("<b>Can every block be irrigated by gravity?</b> Field - TC - SC - MC - Reservoir")
        lay.addWidget(intro)
        lay.addWidget(self.tabs)
        lay.addStretch(1)

        # ---- log (collapsed by default) + buttons
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
        help_btn = QPushButton("Help")
        help_btn.setVisible(on_help is not None)
        if on_help is not None:
            help_btn.clicked.connect(lambda: on_help())
        hb.addWidget(help_btn)
        hb.addStretch()
        hb.addWidget(self.run_btn)
        close_btn = QPushButton("Close")
        close_btn.setVisible(on_close is not None)
        if on_close is not None:
            close_btn.clicked.connect(on_close)
        hb.addWidget(close_btn)
        outer.addLayout(hb)

        # ---- signals
        self.blk_cb.layerChanged.connect(self.lbl_cb.setLayer)
        self.pt_cb.layerChanged.connect(self.on_points)
        self.dem_cb.layerChanged.connect(self.band_cb.setLayer)
        self.can_cb.layerChanged.connect(self.on_canals)
        self.resample_chk.toggled.connect(self.update_enabled)
        self.rb_check.toggled.connect(self.update_enabled)
        self.wlmode_cb.currentIndexChanged.connect(self.update_enabled)
        self.can_cb.layerChanged.connect(self.update_enabled)
        self.res_chk.toggled.connect(self.res_fsl.setEnabled)
        self.run_btn.clicked.connect(self.on_run)
        for cb in (self.blk_cb, self.pt_cb, self.dem_cb, self.can_cb, self.res_cb, self.dir_cb,
                   self.prefer_cb, self.wlmode_cb, self.lbl_cb, self.elev_cb, self.cname_cb,
                   self.clevel_cb, self.wl1_cb, self.wl2_cb, self.band_cb):
            with suppress(Exception):
                cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                cb.setMinimumContentsLength(12)
        self.preselect()

    # ---------- inputs ----------
    def set_inputs(self, blocks=None, points=None):
        """Called by step 1 ('Next'): use its block layer and new HighestPoints layer."""
        if blocks is not None:
            self.blk_cb.setLayer(blocks)
            self.lbl_cb.setLayer(blocks)
        if points is not None:
            self.pt_cb.setLayer(points)
            self.on_points(points)

    def preselect(self):
        layers = list(QgsProject.instance().mapLayers().values())

        def first(pred):
            for lyr in layers:
                with suppress(Exception):
                    if pred(lyr):
                        return lyr
            return None
        lyr = first(lambda x: isinstance(x, QgsVectorLayer) and x.geometryType() == POLYGON_GEOM
                    and ("paddy" in x.name().lower() or "block" in x.name().lower())
                    and not x.name().startswith(("Command", "Topology")))
        if lyr:
            self.blk_cb.setLayer(lyr)
        self.lbl_cb.setLayer(self.blk_cb.currentLayer())
        lyr = first(lambda x: isinstance(x, QgsVectorLayer) and x.name().startswith("HighestPoints"))
        if lyr:
            self.pt_cb.setLayer(lyr)
        self.on_points(self.pt_cb.currentLayer())
        self.dem_cb.setLayer(None)
        lyr = first(lambda x: isinstance(x, QgsRasterLayer) and x.bandCount() == 1)
        if lyr:
            self.dem_cb.setLayer(lyr)
        self.band_cb.setLayer(self.dem_cb.currentLayer())
        self.can_cb.setLayer(None)
        lyr = first(lambda x: isinstance(x, QgsVectorLayer) and "canal" in x.name().lower()
                    and not x.name().startswith("Canal_Command"))
        if lyr:
            self.can_cb.setLayer(lyr)
        self.on_canals(self.can_cb.currentLayer())
        self.res_cb.setLayer(None)
        self.res_fsl.setEnabled(False)
        self.update_enabled()

    def on_points(self, layer):
        self.elev_cb.setLayer(layer)
        if layer is not None:
            for n in ("elev", "Ground_z", "Higher_elevation", "z", "Z"):
                if layer.fields().indexOf(n) != -1:
                    self.elev_cb.setField(n)
                    break

    def on_canals(self, layer):
        for cb in (self.cname_cb, self.clevel_cb, self.wl1_cb, self.wl2_cb):
            cb.setLayer(layer)
        if layer is None:
            return
        names = [f.name() for f in layer.fields()]

        def guess(cb, keys):
            low = {n.lower(): n for n in names}
            for k in keys:  # exact name first, in order of preference
                if k in low:
                    cb.setField(low[k])
                    return
            for k in keys:  # then names containing a longer key (e.g. 'canal_name')
                if len(k) >= 4:
                    for n in names:
                        if k in n.lower():
                            cb.setField(n)
                            return
        guess(self.cname_cb, ("name", "canal_id", "id"))
        guess(self.clevel_cb, ("level", "type", "class", "order", "category"))

    def update_enabled(self, *_):
        self.sample_r.setEnabled(self.resample_chk.isChecked())
        self.elev_cb.setEnabled(not self.resample_chk.isChecked())
        has_c = self.can_cb.currentLayer() is not None
        chk = self.rb_check.isChecked()
        for w in (self.wlmode_cb, self.wl1_cb):
            w.setEnabled(has_c and chk)
        self.wl2_cb.setEnabled(has_c and chk and self.wlmode_cb.currentData() == "startend")

    # ---------- run ----------
    def log(self, msg):
        self.log_box.appendPlainText(str(msg))
        QApplication.processEvents()

    def show_report(self, html_text):
        if self._report_dlg is None:
            self._report_dlg = ReportDialog(self.window())
        self._report_dlg.set_html(html_text)
        self._report_dlg.show()
        self._report_dlg.raise_()
        self._report_dlg.activateWindow()

    def shutdown(self):
        dlg, self._report_dlg = self._report_dlg, None
        if dlg is not None:
            with suppress(RuntimeError):
                dlg.close()

    def on_run(self):
        p = dict(
            blocks=self.blk_cb.currentLayer(), label_field=self.lbl_cb.currentField() or None,
            selected_only=self.sel_chk.isChecked(), points=self.pt_cb.currentLayer(),
            elev_field=self.elev_cb.currentField() or None, resample=self.resample_chk.isChecked(),
            sample_r=self.sample_r.value(), dem=self.dem_cb.currentLayer(),
            band=max(1, self.band_cb.currentBand()), match_tol=self.match_tol.value(),
            depth=self.depth.value(), spill_loss=self.spill.value(), adj_tol=self.adj_tol.value(),
            min_shared=self.min_shared.value(), cascade_first=self.casc_chk.isChecked(),
            canals=self.can_cb.currentLayer(), canal_name=self.cname_cb.currentField() or None,
            canal_level=self.clevel_cb.currentField() or None, dissolve=self.dissolve_chk.isChecked(),
            direction=self.dir_cb.currentData(), junc_tol=self.junc_tol.value(),
            search=self.search.value(), prefer=self.prefer_cb.currentData(),
            turnout_loss=self.tloss.value(), fgrad=self.fgrad.value() / 1000.0,
            cgrad=self.cgrad.value() / 1000.0, struct_loss=self.sloss.value(),
            mode="check" if self.rb_check.isChecked() else "design",
            wl_mode=self.wlmode_cb.currentData(), wl_field=self.wl1_cb.currentField() or None,
            wl_field2=self.wl2_cb.currentField() or None,
            res_fsl=self.res_fsl.value() if self.res_chk.isChecked() else None,
            res_point=self.res_cb.currentLayer(), gnd_step=5.0)
        if p["mode"] == "check" and p["wl_mode"] == "startend" and not p["wl_field2"]:
            QMessageBox.warning(self, "Water level", "Choose the end water level field.")
            return
        self.run_btn.setEnabled(False)
        self.log_group.setChecked(True)             # show progress while it runs
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.log("=" * 60)
        try:
            s = run_command(p, self.log)
            if self.save_chk.isChecked() and self.save_path.filePath().strip():
                saved = save_gpkg(s["layers"], self.save_path.filePath().strip(), self.log)
                rpt = os.path.splitext(saved)[0] + "_report.html"
                try:
                    with open(rpt, "w", encoding="utf-8") as fh:
                        fh.write(s["report"])
                    self.log(f"Saved report: {rpt}")
                except OSError as err:
                    self.log(f"NOTE: report not saved ({err})")
            QApplication.restoreOverrideCursor()
            self.show_report(s["report"])
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.log("ERROR: " + str(e))
            self.log(traceback.format_exc())
            QMessageBox.critical(self, "Error", str(e))
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            self.run_btn.setEnabled(True)

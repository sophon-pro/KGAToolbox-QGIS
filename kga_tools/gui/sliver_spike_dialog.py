# -*- coding: utf-8 -*-
"""Sliver and Spike Cleaner - the window.

Same layout as Paddy Irrigation Tools: Input on top, settings in tabs below,
a collapsible log, and Help / Run / Close along the bottom. The geometry work is
in `core/sliver_spike_cleaner`; it runs on the GUI thread with a progress bar.
The input layer is never modified.
"""

import os

from qgis.core import (
    Qgis, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,
    QgsUnitTypes, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QTabWidget, QVBoxLayout, QWidget,
)

from .window_reveal import revealed_preview
from ..branding import open_docs
from ..core import output_defaults as od
from ..core.compat import T_DOUBLE, T_INT, T_LONGLONG, T_STRING, make_field
from ..core.sliver_spike_cleaner import clean_feature, make_valid, polygonal_only

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

WARN = "color: #c0392b;"

PRESETS = {
    "Light": {"angle": 5.0, "width": 1.0, "area": 5.0,
              "tip": "Gentle - only very thin spikes and strips (narrower than 1 m)."},
    "Standard": {"angle": 10.0, "width": 2.0, "area": 10.0,
                 "tip": "Recommended for most split results (narrower than 2 m)."},
    "Strong": {"angle": 15.0, "width": 3.0, "area": 25.0,
               "tip": "Aggressive - removes everything narrower than 3 m."},
}


def save_layer(layer, path, layer_name):
    """Write `layer` to a GeoPackage layer or Shapefile and load it back."""
    ext = os.path.splitext(path)[1].lower()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG" if ext == ".gpkg" else "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    opts.layerName = layer_name
    if ext == ".gpkg" and os.path.exists(path):
        opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    res = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), opts)
    if res[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(res[1])
    uri = "{}|layername={}".format(path, layer_name) if ext == ".gpkg" else path
    return QgsVectorLayer(uri, layer_name, "ogr")


@revealed_preview
class CleanSpikesDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent or (iface.mainWindow() if iface else None))
        self._torn_down = False
        self._applying_preset = False
        self.setWindowTitle("Sliver and Spike Cleaner")
        self.resize(640, 560)
        self.setMinimumWidth(600)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # ---- input ----
        g_in = QGroupBox("Input")
        fi = QFormLayout(g_in)
        self.cmb_layer = QgsMapLayerComboBox()
        self.cmb_layer.setFilters(Qgis.LayerFilter.PolygonLayer)
        active = iface.activeLayer() if iface else None
        if (active is not None and active.type() == Qgis.LayerType.Vector
                and active.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry):
            self.cmb_layer.setLayer(active)
        self.refresh_layers()
        fi.addRow("Polygon layer:", self.cmb_layer)
        self.lbl_info = QLabel("")
        fi.addRow("", self.lbl_info)
        self.chk_selected = QCheckBox("Selected features only")
        self.chk_copy_unsel = QCheckBox("Keep the unselected features unchanged in the output")
        self.chk_copy_unsel.setChecked(True)
        self.chk_copy_unsel.setEnabled(False)
        fi.addRow("", self.chk_selected)
        fi.addRow("", self.chk_copy_unsel)
        self.lbl_units = QLabel("")
        self.lbl_units.setWordWrap(True)
        fi.addRow("", self.lbl_units)
        outer.addWidget(g_in)

        # ---- clean ----
        g_c = QWidget()
        fc = QFormLayout(g_c)
        self.preset_cb = QComboBox()
        for name in PRESETS:
            self.preset_cb.addItem("{}  ({:g} m)".format(name, PRESETS[name]["width"]), name)
        self.preset_cb.addItem("Custom", "Custom")
        fc.addRow("Strength:", self.preset_cb)
        self.lbl_preset = QLabel("")
        fc.addRow("", self.lbl_preset)

        self.chk_spike = QCheckBox("Trim spikes and wedges sharper than")
        self.spn_angle = self._spin(0.1, 45, 1, 10.0, " °")
        self.spn_angle.setToolTip("Corners sharper than this are spikes. Normal corners are about 90°.")
        fc.addRow("", self._row(self.chk_spike, self.spn_angle))
        self.chk_width = QCheckBox("Remove slivers and narrow parts narrower than")
        self.spn_width = self._spin(0.01, 1000, 2, 2.0, " m")
        self.spn_width.setToolTip("Any part narrower than this is removed, however long it is.")
        fc.addRow("", self._row(self.chk_width, self.spn_width))
        self.chk_small = QCheckBox("Remove small pieces and holes smaller than")
        self.spn_area = self._spin(0, 1e9, 2, 10.0, " m²")
        self.chk_small.setChecked(True)
        self.chk_small.setEnabled(False)
        self.chk_small.setToolTip("Always on; set the area to 0 to keep every piece.")
        fc.addRow("", self._row(self.chk_small, self.spn_area))
        self.chk_spike.setChecked(True)
        self.chk_width.setChecked(True)
        self.chk_spike.toggled.connect(self.spn_angle.setEnabled)
        self.chk_width.toggled.connect(self.spn_width.setEnabled)

        self.cmb_narrow = QComboBox()
        self.cmb_narrow.addItem("Keep it and flag for review (cleaned = 2)", "keep")
        self.cmb_narrow.addItem("Delete it", "delete")
        self.cmb_narrow.setToolTip("What to do with a feature that is narrow all over")
        fc.addRow("A whole feature that is narrow:", self.cmb_narrow)
        self.chk_width.toggled.connect(self.cmb_narrow.setEnabled)
        # two fixed lines: a wrapped label reports a tall size hint and leaves blank space in the tab
        warn = QLabel("<i>Real narrow features (bunds, canals, roads) are narrow too.<br>"
                      "Run once with 'Keep and flag', check them, then use 'Delete'.</i>")
        warn.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        fc.addRow("", warn)

        # ---- output ----
        g_o = QWidget()
        fo = QFormLayout(g_o)
        orow = QHBoxLayout()
        self.rad_temp = QRadioButton("Temporary layer")
        self.rad_file = QRadioButton("Save to file")
        self.rad_temp.setChecked(True)
        orow.addWidget(self.rad_temp)
        orow.addWidget(self.rad_file)
        orow.addStretch()
        fo.addRow("Save as:", orow)
        self.file_widget = QgsFileWidget()
        self.file_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.file_widget.setFilter("GeoPackage (*.gpkg);;ESRI Shapefile (*.shp)")
        self.file_widget.setEnabled(False)
        fo.addRow("File:", self.file_widget)
        self.txt_name = QLineEdit()
        fo.addRow("Layer name:", self.txt_name)
        self.chk_qa = QCheckBox("Add check fields (cleaned, area_old, area_new, area_rem)")
        self.chk_qa.setChecked(True)
        fo.addRow("", self.chk_qa)
        self.chk_removed = QCheckBox("Add a '_removed' layer showing what was cut")
        self.chk_removed.setChecked(True)
        fo.addRow("", self.chk_removed)
        self.chk_add = QCheckBox("Add results to the map")
        self.chk_add.setChecked(True)
        fo.addRow("", self.chk_add)
        fo.addRow("", QLabel("<i>The input layer is never changed.</i>"))

        # ---- advanced ----
        g_a = QWidget()
        fa = QFormLayout(g_a)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("Square trim along the corner edge (recommended)", "trim")
        self.cmb_mode.addItem("Remove the tip vertex only", "tip")
        fa.addRow("Spike method:", self.cmb_mode)
        self.chk_spike.toggled.connect(self.cmb_mode.setEnabled)
        self.spn_trim = self._spin(0, 1e9, 2, 50.0, " m²")
        self.spn_trim.setToolTip("A spike is only cut if the removed piece is smaller than this.")
        fa.addRow("Max area cut per spike:", self.spn_trim)
        self.spn_snap = self._spin(0, 10, 3, 0.01, " m")
        self.spn_snap.setToolTip("Result vertices this close to an original vertex are snapped "
                                 "back to it. 0 = off.")
        fa.addRow("Snap to original within:", self.spn_snap)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Run the tool to see the details here.")
        self.tabs = QTabWidget()
        for page, name, tip in (
                (g_c, "Clean", "Strength, spikes, narrow parts and small pieces"),
                (g_o, "Output", "Where the result goes"),
                (g_a, "Advanced", "Spike method, maximum cut, snapping"),
                (self.log, "Log", "What the last run did")):
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(2, 2, 2, 2)
            lay.addWidget(page)
            if page is not self.log:
                lay.addStretch()
            self.tabs.setTabToolTip(self.tabs.addTab(box, name), tip)
        self.log.setMinimumHeight(170)
        outer.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self._fit_tab)
        self._fit_tab()

        # ---- summary + progress ----
        self.lbl_status = QLabel("Ready")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.hide()                 # shown only while a run is going
        outer.addWidget(self.lbl_status)
        outer.addWidget(self.progress)

        hb = QHBoxLayout()
        btn_help = QPushButton("Help")
        btn_close = QPushButton("Close")
        self.btn_run = QPushButton("Run")
        self.btn_run.setDefault(True)
        hb.addWidget(btn_help)
        hb.addStretch()
        hb.addWidget(self.btn_run)
        hb.addWidget(btn_close)
        outer.addLayout(hb)

        g_in.layout().setContentsMargins(6, 4, 6, 4)

        btn_help.clicked.connect(lambda: open_docs('sliver_spike_cleaner'))
        btn_close.clicked.connect(self.close)
        self.btn_run.clicked.connect(self.run)
        self.rad_file.toggled.connect(self.file_widget.setEnabled)
        self.rad_file.toggled.connect(self._suggest_file)
        self.chk_selected.toggled.connect(self.chk_copy_unsel.setEnabled)
        self.chk_selected.toggled.connect(self.update_info)
        self.cmb_layer.layerChanged.connect(self.update_info)
        self.preset_cb.currentIndexChanged.connect(self._preset_changed)
        for w in (self.spn_angle, self.spn_width, self.spn_area):
            w.valueChanged.connect(self._custom_values)

        self.apply_preset("Standard")
        self.update_info()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row(check, spin):
        h = QHBoxLayout()
        h.addWidget(check)
        h.addWidget(spin)
        h.addStretch()
        return h

    @staticmethod
    def _spin(lo, hi, dec, val, suffix=""):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(val)
        s.setSuffix(suffix)
        return s

    def _fit_tab(self, _=None):
        """Size the tab area to the page shown, not to the tallest page, and let the window follow."""
        page = self.tabs.currentWidget()
        if page is None:
            return
        page.layout().invalidate()
        # wrapped labels report a tall size hint; ask for the height at the real width instead
        width = max(self.tabs.width(), self.minimumWidth()) - 4
        height = max(page.layout().totalHeightForWidth(width), page.layout().sizeHint().height()
                     if not page.layout().hasHeightForWidth() else 0)
        self.tabs.setFixedHeight(height + self.tabs.tabBar().sizeHint().height() + 8)
        QTimer.singleShot(0, self._fit_window)

    def _fit_window(self):
        self.resize(self.width(), self.sizeHint().height())

    def _preset_changed(self, _=None):
        name = self.preset_cb.currentData()
        if name in PRESETS and not self._applying_preset:
            self.apply_preset(name)

    def apply_preset(self, name):
        p = PRESETS[name]
        self._applying_preset = True
        self.spn_angle.setValue(p["angle"])
        self.spn_width.setValue(p["width"])
        self.spn_area.setValue(p["area"])
        self.preset_cb.setCurrentIndex(self.preset_cb.findData(name))
        self._applying_preset = False
        self.lbl_preset.setText("<i>{}</i>".format(p["tip"]))

    def _custom_values(self, *args):
        if self._applying_preset:
            return
        self._applying_preset = True
        self.preset_cb.setCurrentIndex(self.preset_cb.findData("Custom"))
        self._applying_preset = False
        self.lbl_preset.setText("<i>Custom settings.</i>")

    def _suggest_file(self, on):
        """Start the file in the project's default folder (output defaults)."""
        if on and not self.file_widget.filePath().strip():
            name = self.txt_name.text().strip() or "cleaned"
            self.file_widget.setFilePath(od.default_file(name, "gpkg"))

    def closeEvent(self, e):
        self.log.clear()
        super().closeEvent(e)

    def shutdown(self):
        """Plugin unload: hide the window."""
        self._torn_down = True
        self.hide()

    def write(self, msg):
        self.log.appendPlainText(msg)
        QApplication.processEvents()

    def status(self, msg, error=False):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(WARN if error else "")
        if error:
            self.write("ERROR: " + msg)
            self.tabs.setCurrentWidget(self.log.parentWidget())
        QApplication.processEvents()

    def refresh_layers(self):
        """List only the polygon layers of the active map (every layer while Map Tabs is off)."""
        try:
            from . import map_tabs                      # not at import time: it pulls in a lot of the GUI
            ids = map_tabs.active_map_layer_ids()
        except Exception:                               # pragma: no cover - head-less
            ids = None
        keep = self.cmb_layer.currentLayer()
        if ids is None:
            self.cmb_layer.setExceptedLayerList([])
        else:
            self.cmb_layer.setExceptedLayerList(
                [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.id() not in ids])
        if keep is not None and (ids is None or keep.id() in ids):
            self.cmb_layer.setLayer(keep)

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_layers()
        self.update_info()

    def update_info(self, *args):
        lyr = self.cmb_layer.currentLayer()
        if not lyr:
            self.lbl_info.setText("No polygon layer in the project.")
            return
        n_sel = lyr.selectedFeatureCount()
        txt = (f"{lyr.featureCount()} features  ·  {n_sel} selected  ·  "
               f"{QgsWkbTypes.displayString(lyr.wkbType())}  ·  {lyr.crs().authid()}")
        if self.chk_selected.isChecked() and n_sel == 0:
            txt += "   ⚠ nothing selected"
        self.lbl_info.setText(txt)
        unit = QgsUnitTypes.toString(lyr.crs().mapUnits())
        if lyr.crs().isGeographic():
            self.lbl_units.setText(
                "<span style='color:#c0392b'>This layer is in {}. Widths and areas would "
                "be in degrees - reproject it to a projected CRS (metres) first.</span>".format(unit))
        else:
            self.lbl_units.setText(f"Layer units: {unit}")
        self.txt_name.setText(f"{lyr.name()}_clean")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self):
        src = self.cmb_layer.currentLayer()
        if not src:
            self.status("Choose a polygon layer first.", True)
            return
        only_sel = self.chk_selected.isChecked()
        if only_sel and src.selectedFeatureCount() == 0:
            self.status("Nothing is selected – select parcels or untick "
                        "'selected features only'.", True)
            return
        out_path = self.file_widget.filePath().strip()
        if self.rad_file.isChecked():
            if not out_path:
                self.status("Choose an output file.", True)
                return
            if os.path.splitext(out_path)[1].lower() not in (".gpkg", ".shp"):
                out_path += ".gpkg"
            if os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(
                    os.path.abspath(src.source().split("|")[0])):
                self.status("The output file is the input layer's file - "
                            "choose a different file.", True)
                return

        cfg = {
            'spike_on': self.chk_spike.isChecked(),
            'mode': self.cmb_mode.currentData(),
            'angle': self.spn_angle.value(),
            'max_trim': self.spn_trim.value(),
            'width_on': self.chk_width.isChecked(),
            'width': self.spn_width.value(),
            'narrow_action': self.cmb_narrow.currentData(),
            'min_area': self.spn_area.value(),
            'snap_tol': self.spn_snap.value(),
        }
        out_name = self.txt_name.text().strip() or f"{src.name()}_clean"
        qa = self.chk_qa.isChecked()
        want_removed = self.chk_removed.isChecked()
        copy_unsel = only_sel and self.chk_copy_unsel.isChecked()
        sel_ids = set(src.selectedFeatureIds()) if only_sel else None

        self.btn_run.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self.btn_run.setText("Cleaning...")
        self.log.clear()
        self.tabs.setCurrentWidget(self.log.parentWidget())
        self.status("Cleaning…")
        self.write(f"Input: {src.name()}")
        self.write("Steps: " + ", ".join(s for s, on in [
            (f"spikes<{cfg['angle']}° ({cfg['mode']})", cfg['spike_on']),
            (f"width<{cfg['width']}", cfg['width_on']),
            (f"area<{cfg['min_area']}", True),
            (f"snap {cfg['snap_tol']}", cfg['snap_tol'] > 0)] if on))

        try:
            self._process(src, cfg, out_name, out_path, qa, want_removed,
                          copy_unsel, sel_ids)
        except Exception as e:
            self.status(f"Failed: {e}", True)
        finally:
            self.progress.hide()
            self.btn_run.setEnabled(True)
            self.btn_run.setText("Run")

    def _process(self, src, cfg, out_name, out_path, qa, want_removed,
                 copy_unsel, sel_ids):
        src_multi = QgsWkbTypes.isMultiType(src.wkbType())
        flat_type = QgsWkbTypes.flatType(src.wkbType())
        if QgsWkbTypes.hasZ(src.wkbType()) or QgsWkbTypes.hasM(src.wkbType()):
            self.write("Note: Z/M values are dropped (output is 2D).")
        out = QgsVectorLayer(QgsWkbTypes.displayString(flat_type), out_name, "memory")
        out.setCrs(src.crs())
        pr = out.dataProvider()
        # a "fid" column would clash with the GeoPackage key of the output
        keep = [i for i, fl in enumerate(src.fields())
                if fl.name().lower() != "fid"]
        pr.addAttributes([src.fields()[i] for i in keep])
        qa_fields = [("cleaned", T_INT), ("area_old", T_DOUBLE),
                     ("area_new", T_DOUBLE), ("area_rem", T_DOUBLE)]
        if qa:
            pr.addAttributes([make_field(n, t) for n, t in qa_fields
                              if src.fields().indexOf(n) == -1])
        out.updateFields()
        idx = {n: out.fields().indexOf(n) for n, _ in qa_fields}

        rem = None
        if want_removed:
            rem = QgsVectorLayer("MultiPolygon", f"{out_name}_removed", "memory")
            rem.setCrs(src.crs())
            rem.dataProvider().addAttributes([
                make_field("src_fid", T_LONGLONG),
                make_field("reason", T_STRING),
                make_field("area", T_DOUBLE)])
            rem.updateFields()
        rem_batch = []

        def add_removed(fid, geom, reason):
            if rem is None or geom is None or geom.isEmpty():
                return
            for p in polygonal_only(geom).asGeometryCollection():
                if p.area() > 0.01:
                    rf = QgsFeature(rem.fields())
                    p.convertToMultiType()
                    rf.setGeometry(p)
                    rf.setAttributes([fid, reason, round(p.area(), 3)])
                    rem_batch.append(rf)

        def make_feature(f, geom, flag, a_old, a_new):
            nf = QgsFeature(out.fields())
            vals = f.attributes()
            attrs = [vals[i] for i in keep]
            attrs += [None] * (len(out.fields()) - len(attrs))
            nf.setAttributes(attrs)
            if geom is not None:
                nf.setGeometry(geom)
            if qa:
                for n, v in (("cleaned", flag), ("area_old", a_old),
                             ("area_new", a_new), ("area_rem", a_old - a_new)):
                    if idx[n] >= 0:
                        nf.setAttribute(idx[n], round(v, 3) if n != "cleaned" else v)
            return nf

        stats = {'trimmed': 0, 'tips': 0, 'skipped': 0, 'narrow_feat': 0,
                 'small': 0, 'multi_to_single': 0}
        counts = {'written': 0, 'changed': 0, 'flagged': 0, 'deleted': 0, 'errors': 0}
        deleted_reasons = {}
        batch = []
        total = src.featureCount() or 1

        for i, f in enumerate(src.getFeatures()):
            if i % 25 == 0:
                self.progress.setValue(int(100 * i / total))
                self.lbl_status.setText(f"Cleaning… {i}/{total}")
                QApplication.processEvents()

            g = f.geometry()
            process = (sel_ids is None) or (f.id() in sel_ids)
            if not process:
                if copy_unsel:
                    a = g.area() if g else 0.0
                    gg = QgsGeometry(g) if g else None
                    if gg is not None and src_multi:
                        gg.convertToMultiType()
                    batch.append(make_feature(f, gg, 0, a, a))
                    counts['written'] += 1
                continue

            if not g or g.isEmpty():
                batch.append(make_feature(f, None, 0, 0.0, 0.0))
                counts['written'] += 1
                continue

            a_old = g.area()
            try:
                new_g, status = clean_feature(g, cfg, stats)
            except Exception as e:
                self.write(f"  fid {f.id()}: error, original kept ({e})")
                counts['errors'] += 1
                new_g, status = QgsGeometry(g), 'error'

            if new_g is None:
                counts['deleted'] += 1
                deleted_reasons[status] = deleted_reasons.get(status, 0) + 1
                add_removed(f.id(), make_valid(g), status)
                continue

            if not src_multi and new_g.isMultipart():
                parts = new_g.asGeometryCollection()
                biggest = max(parts, key=lambda p: p.area())
                add_removed(f.id(), new_g.difference(biggest),
                            "extra part (single-part layer)")
                new_g = biggest
                stats['multi_to_single'] += 1
            if src_multi:
                new_g.convertToMultiType()

            changed = not new_g.isGeosEqual(g)
            if status == 'narrow_kept':
                flag = 2
                counts['flagged'] += 1
            else:
                flag = 1 if changed else 0
            if changed:
                counts['changed'] += 1
                if rem is not None:
                    try:
                        add_removed(f.id(), make_valid(g).difference(new_g), "trimmed")
                    except Exception as exc:
                        self.write("  fid {}: removed piece not drawn ({})".format(f.id(), exc))

            batch.append(make_feature(f, new_g, flag, a_old, new_g.area()))
            counts['written'] += 1
            if len(batch) >= 500:
                pr.addFeatures(batch)
                batch = []

        if batch:
            pr.addFeatures(batch)
        out.updateExtents()
        if rem is not None:
            rem.dataProvider().addFeatures(rem_batch)
            rem.updateExtents()
        self.progress.setValue(100)

        result, result_rem = out, rem
        if self.rad_file.isChecked():
            result = save_layer(out, out_path, out_name)
            self.write(f"Saved: {out_path}")
            if rem is not None:
                if out_path.lower().endswith(".gpkg"):
                    result_rem = save_layer(rem, out_path, rem.name())
                else:
                    rp = os.path.splitext(out_path)[0] + "_removed.shp"
                    result_rem = save_layer(rem, rp, rem.name())
                    self.write(f"Saved: {rp}")

        if self.chk_add.isChecked():
            QgsProject.instance().addMapLayer(result)
            if result_rem is not None and result_rem.featureCount() > 0:
                QgsProject.instance().addMapLayer(result_rem)

        msg = f"Done – {counts['written']} features written to “{out_name}”."
        if counts['flagged']:
            msg += f"  {counts['flagged']} narrow features flagged (cleaned = 2)."
        self.status(msg)

        # ---- detailed log ----
        self.write("-" * 58)
        self.write(f"Features written           : {counts['written']}")
        self.write(f"Features changed (cleaned=1): {counts['changed']}")
        if counts['flagged']:
            self.write(f"Narrow features flagged (2) : {counts['flagged']}")
        self.write(f"Features deleted            : {counts['deleted']}"
                   + (f"  {deleted_reasons}" if deleted_reasons else ""))
        self.write(f"Spikes square-trimmed       : {stats['trimmed']}")
        self.write(f"Spike tips removed          : {stats['tips']}")
        if stats['skipped']:
            self.write(f"Spikes skipped (> max cut)  : {stats['skipped']}")
        self.write(f"Small parts / holes removed : {stats['small']}")
        if stats['multi_to_single']:
            self.write(f"Extra parts dropped (single-part layer): {stats['multi_to_single']}")
        if counts['errors']:
            self.write(f"Errors (original kept)      : {counts['errors']}")
        if rem is not None:
            self.write(f"Removed pieces layer        : {len(rem_batch)} pieces")
        self.write("Tip: check neighbours with Vector > Topology Checker.")

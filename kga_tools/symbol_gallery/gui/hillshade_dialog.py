# -*- coding: utf-8 -*-
"""Hillshade options: method, strength, relief exaggeration, light and blending –
with live preview on the map."""
import os

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QRadioButton, QLabel,
    QComboBox, QSlider, QCheckBox, QDoubleSpinBox, QPushButton, QFileDialog, QFrame,
)
from qgis.core import (
    QgsApplication, QgsProject, QgsRasterLayer, QgsTask, QgsSingleBandPseudoColorRenderer,
)

from ..core import hillshade as hs
from ..core import apply as applier

BAKED_KEY = "symbol_gallery/shaded_relief_of"


class _BakeTask(QgsTask):
    def __init__(self, dem_name, args):
        super().__init__("Shaded relief for %s" % dem_name, QgsTask.Flag.CanCancel)
        self.args = args
        self.error = None

    def run(self):
        def fb(pct, _msg):
            self.setProgress(pct)
            if self.isCanceled():
                raise RuntimeError("Cancelled")
        try:
            hs.bake(*self.args, feedback=fb)
            return True
        except Exception as ex:
            self.error = str(ex)
            return False


class HillshadeDialog(QDialog):
    """Non-modal; edits apply live to the DEM's hillshade layer when it exists."""

    def __init__(self, layer, iface=None, parent=None, info=None):
        super().__init__(parent)
        self.layer = layer
        self.iface = iface
        self.info = info or (lambda msg, level="success": None)
        self.opts = hs.load()
        self.stats = hs.terrain_stats(layer)
        self._task = None
        self.setWindowTitle("Hillshade Options – %s" % layer.name())
        self.setMinimumWidth(430)
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(120)
        self._live_timer.timeout.connect(self._update_live)
        self._loading = True          # widgets are being filled: don't save/preview
        self._build()
        self._load_ui()
        self._loading = False

    # ------------------------------------------------------------------ UI
    def _build(self):
        lay = QVBoxLayout(self)

        self.terrain = QLabel()
        self.terrain.setWordWrap(True)
        self.terrain.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.terrain)

        box = QGroupBox("Method")
        bl = QVBoxLayout(box)
        self.m_live = QRadioButton("Live hillshade layer  (recommended)")
        self.m_live.setToolTip("A second, blended hillshade layer of the same DEM. Instant, "
                               "adjustable any time, nothing written to disk.")
        lv = QLabel("Instant · adjust any time · no files written. Two layers in the panel.")
        lv.setObjectName("muted")
        lv.setIndent(22)
        self.m_baked = QRadioButton("Single shaded-relief image  (GeoTIFF)")
        self.m_baked.setToolTip("Colours and hillshade combined into one RGBA GeoTIFF – one "
                                "layer, fast to draw, looks the same in every program.")
        bk = QLabel("One layer · fastest drawing · same look in ArcGIS, Google Earth, "
                    "reports. Re-create it after changing colours.")
        bk.setObjectName("muted")
        bk.setWordWrap(True)
        bk.setIndent(22)
        for w in (self.m_live, lv, self.m_baked, bk):
            bl.addWidget(w)
        lay.addWidget(box)

        box = QGroupBox("Look")
        form = QFormLayout(box)
        self.strength = QComboBox()
        for k in ("subtle", "normal", "strong", "very strong"):
            self.strength.addItem(k.capitalize(), k)
        self.strength.addItem("Custom", "custom")
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(5, 100)
        self.op_label = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.strength)
        row.addWidget(self.opacity, 1)
        row.addWidget(self.op_label)
        form.addRow("Strength", row)

        self.auto_z = QCheckBox("Auto (adapts to flat floodplains)")
        self.z = QDoubleSpinBox()
        self.z.setRange(0.1, 100)
        self.z.setDecimals(1)
        self.z.setSingleStep(0.5)
        self.z.setSuffix(" ×")
        row = QHBoxLayout()
        row.addWidget(self.auto_z, 1)
        row.addWidget(self.z)
        form.addRow("Relief exaggeration", row)

        self.multi = QCheckBox("Multidirectional (soft, RAS Mapper-like)")
        form.addRow("Light", self.multi)
        self.azimuth = QDoubleSpinBox()
        self.azimuth.setRange(0, 360)
        self.azimuth.setDecimals(0)
        self.azimuth.setSuffix(" °")
        self.altitude = QDoubleSpinBox()
        self.altitude.setRange(1, 90)
        self.altitude.setDecimals(0)
        self.altitude.setSuffix(" °")
        row = QHBoxLayout()
        row.addWidget(QLabel("from"))
        row.addWidget(self.azimuth)
        row.addWidget(QLabel("height"))
        row.addWidget(self.altitude)
        row.addStretch(1)
        form.addRow("", row)

        self.smooth = QSlider(Qt.Orientation.Horizontal)
        self.smooth.setRange(0, hs.SMOOTH_MAX)
        self.smooth.setPageStep(1)
        self.smooth.setToolTip("0 = sharp cells · 1–2 = smooth display (no blocky cells) · "
                               "3–10 = also softens the relief of the hillshade")
        self.smooth_lbl = QLabel()
        self.smooth_lbl.setMinimumWidth(150)
        row = QHBoxLayout()
        row.addWidget(self.smooth, 1)
        row.addWidget(self.smooth_lbl)
        form.addRow("Smooth", row)

        self.blend = QComboBox()
        for k, label, _m in hs.BLENDS:
            self.blend.addItem(label, k)
        form.addRow("Blending", self.blend)
        lay.addWidget(box)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        buttons = QHBoxLayout()
        self.remove_btn = QPushButton("Remove Hillshade")
        self.apply_btn = QPushButton()
        self.apply_btn.setDefault(True)
        self.apply_btn.setObjectName("primary")
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch(1)
        buttons.addWidget(close)
        buttons.addWidget(self.apply_btn)
        lay.addLayout(buttons)

        self.m_live.toggled.connect(self._sync)
        self.strength.currentIndexChanged.connect(self._on_strength)
        self.opacity.valueChanged.connect(self._on_opacity)
        self.auto_z.toggled.connect(self._changed)
        self.z.valueChanged.connect(self._changed)
        self.multi.toggled.connect(self._changed)
        self.azimuth.valueChanged.connect(self._changed)
        self.altitude.valueChanged.connect(self._changed)
        self.blend.currentIndexChanged.connect(self._changed)
        self.smooth.valueChanged.connect(self._on_smooth)
        self.apply_btn.clicked.connect(self.run)
        self.remove_btn.clicked.connect(self.remove)

    def _load_ui(self):
        o = self.opts
        self.m_live.setChecked(True)
        i = self.strength.findData(o["strength"])
        self.strength.setCurrentIndex(i if i >= 0 else self.strength.findData("custom"))
        self.opacity.setValue(int(round(o["opacity"] * 100)))
        self.auto_z.setChecked(o["auto_z"])
        self.auto_value = hs.auto_exaggeration(self.layer, self.stats)
        self.z.setValue(self.auto_value if o["auto_z"] else o["z"])
        self.multi.setChecked(o["multi"])
        self.azimuth.setValue(o["azimuth"])
        self.altitude.setValue(o["altitude"])
        self.blend.setCurrentIndex(max(0, self.blend.findData(o["blend"])))
        self.smooth.setValue(int(o.get("smooth_level", 2)))
        st = self.stats
        if st:
            kind = ("flat floodplain" if st["mean_slope_deg"] < 1 else
                    "gentle terrain" if st["mean_slope_deg"] < 4 else
                    "hilly terrain" if st["mean_slope_deg"] < 12 else "mountains")
            self.terrain.setText(
                "<b>%s</b>: %.4g – %.4g (relief %.4g), mean slope %.2f° → <b>%s</b>. "
                "Auto exaggeration: <b>%.1f×</b>." % (
                    self.layer.name(), st["min"], st["max"], st["relief"],
                    st["mean_slope_deg"], kind, self.auto_value))
        else:
            self.terrain.setText("<b>%s</b>" % self.layer.name())
        self._sync()

    def _sync(self, *_):
        self.z.setEnabled(not self.auto_z.isChecked())
        if self.auto_z.isChecked():
            self.z.blockSignals(True)
            self.z.setValue(self.auto_value)
            self.z.blockSignals(False)
        self.azimuth.setEnabled(not self.multi.isChecked())
        self.altitude.setEnabled(not self.multi.isChecked())
        self.op_label.setText("%d %%" % self.opacity.value())
        self.smooth_lbl.setText("%d · %s" % (self.smooth.value(),
                                             hs.smooth_label(self.smooth.value())))
        live = self.m_live.isChecked()
        has = applier.hillshade_for(self.layer) is not None
        if live:
            self.apply_btn.setText("Update Hillshade" if has else "Add Hillshade")
        else:
            self.apply_btn.setText("Create Image…")
        self.remove_btn.setEnabled(has or self._baked_layer() is not None)

    # --------------------------------------------------------------- values
    def _read(self):
        o = self.opts
        o["strength"] = self.strength.currentData()
        o["opacity"] = self.opacity.value() / 100.0
        o["auto_z"] = self.auto_z.isChecked()
        if not o["auto_z"]:
            o["z"] = self.z.value()
        o["multi"] = self.multi.isChecked()
        o["azimuth"] = self.azimuth.value()
        o["altitude"] = self.altitude.value()
        o["blend"] = self.blend.currentData()
        o["smooth_level"] = self.smooth.value()
        o["smooth"] = o["smooth_level"] > 0
        return o

    def _on_smooth(self, on):
        if self._loading:
            return
        hs.smooth(self.layer, on)          # the DEM itself, even without hillshade
        self._changed()

    def _on_strength(self, *_):
        if self._loading:
            return
        k = self.strength.currentData()
        if k in hs.PRESETS:
            self.opacity.blockSignals(True)
            self.opacity.setValue(int(hs.PRESETS[k] * 100))
            self.opacity.blockSignals(False)
        self._changed()

    def _on_opacity(self, v):
        if self._loading:
            return
        match = [k for k, p in hs.PRESETS.items() if int(p * 100) == v]
        self.strength.blockSignals(True)
        self.strength.setCurrentIndex(self.strength.findData(match[0] if match else "custom"))
        self.strength.blockSignals(False)
        self._changed()

    def _changed(self, *_):
        if self._loading:
            return
        self._sync()
        hs.save(self._read())
        self._live_timer.start()          # preview on the map

    def _update_live(self):
        lyr = applier.hillshade_for(self.layer)
        if lyr is not None:
            hs.configure_live(lyr, self.layer, self.opts,
                              hs.exaggeration(self.layer, self.opts, self.stats))

    # -------------------------------------------------------------- actions
    def run(self):
        hs.save(self._read())
        if self.m_live.isChecked():
            try:
                had = applier.hillshade_for(self.layer) is not None
                applier.add_hillshade(self.layer, self.opts)
                self._update_live()
            except Exception as ex:
                self.info(str(ex), "warning")
                return
            self.info("Hillshade %s for '%s'." % ("updated" if had else "added",
                                                   self.layer.name()))
            self._sync()
        else:
            self._bake()

    def remove(self):
        done = applier.remove_hillshade(self.layer)
        b = self._baked_layer()
        if b is not None:
            QgsProject.instance().removeMapLayer(b.id())
            self._set_visible(self.layer, True)
            done = True
        if done:
            self.info("Hillshade removed from '%s'." % self.layer.name(), "info")
        self._sync()

    def _baked_layer(self):
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.customProperty(BAKED_KEY) == self.layer.id():
                return lyr
        return None

    @staticmethod
    def _set_visible(layer, on):
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node is not None:
            node.setItemVisibilityChecked(on)

    def _bake(self):
        layer = self.layer
        src = layer.source().split("|")[0]
        if layer.providerType() != "gdal" or not os.path.isfile(src):
            self.info("A shaded-relief image needs a DEM stored as a local file "
                      "(GeoTIFF, IMG, VRT…).", "warning")
            return
        r = layer.renderer()
        if not isinstance(r, QgsSingleBandPseudoColorRenderer):
            self.info("Apply a colour ramp to '%s' first – the image uses its colours."
                      % layer.name(), "warning")
            return
        stem = os.path.splitext(src)[0]
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Shaded Relief", stem + "_shaded_relief.tif", "GeoTIFF (*.tif)")
        if not path:
            return
        if not path.lower().endswith((".tif", ".tiff")):
            path += ".tif"
        if os.path.abspath(path) == os.path.abspath(src):
            self.info("Choose a different file name – the DEM must not be overwritten.",
                      "warning")
            return
        lo, hi, lut = hs.colour_lut(r)
        self.opts["smooth_source"] = hs.smoothed_source(layer, self.opts.get("smooth_level", 2))
        exag = hs.exaggeration(layer, self.opts, self.stats)
        mx, _my = hs._metres_per_unit(layer)
        task = _BakeTask(layer.name(), (src, path, lo, hi, lut, dict(self.opts), exag, mx))
        dem_id, name = layer.id(), layer.name()

        def finished():
            if task.error or not os.path.exists(path):
                self.info("Shaded relief failed: %s" % (task.error or "no output"), "critical")
                return
            dem = QgsProject.instance().mapLayer(dem_id)
            old = None
            for lyr in QgsProject.instance().mapLayers().values():
                if lyr.customProperty(BAKED_KEY) == dem_id:
                    old = lyr
            if old is not None:
                QgsProject.instance().removeMapLayer(old.id())
            out = QgsRasterLayer(path, "%s – shaded relief" % name, "gdal")
            out.setCustomProperty(BAKED_KEY, dem_id)
            hs.smooth(out, min(2, int(self.opts.get("smooth_level", 2))))
            proj = QgsProject.instance()
            proj.addMapLayer(out, False)
            root = proj.layerTreeRoot()
            node = root.findLayer(dem_id)
            parent = node.parent() if node is not None else root
            idx = parent.children().index(node) if node is not None else 0
            parent.insertLayer(idx, out)
            if dem is not None:          # the image already contains colours + relief
                self._set_visible(dem, False)
                live = applier.hillshade_for(dem)
                if live is not None:
                    self._set_visible(live, False)
            self.info("Shaded relief saved to %s and added above '%s' (the DEM and its "
                      "live hillshade were hidden)." % (path, name))
            try:
                self._sync()
            except RuntimeError:
                pass

        task.taskCompleted.connect(finished)
        task.taskTerminated.connect(finished)
        self._task = task
        QgsApplication.taskManager().addTask(task)
        self.info("Creating shaded relief for '%s' in the background…" % name, "info")

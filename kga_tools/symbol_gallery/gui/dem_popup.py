# -*- coding: utf-8 -*-
"""
The "Current style" page for a DEM colour ramp: hillshade on/off, strength, relief
exaggeration, light, blending and smoothing. A page of the gallery panel.

Two modes:
* "after"  – the ramp is already applied; every change shows on the map at once.
* "before" – nothing is applied yet; commit() applies it (the Apply / OK of
  Layer Properties).
"""
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QCheckBox, QSlider,
    QDoubleSpinBox, QComboBox, QPushButton, QFrame,
)

from ..core import hillshade as hs
from ..core import apply as applier

MODES = [
    ("after", "Apply, then show these quick settings"),
    ("direct", "Apply right away (no pop-up)"),
]


class DemQuickPopup(QWidget):
    def __init__(self, dock):
        super().__init__(dock)
        self.dock = dock
        self.dirty = False
        self.entry = None
        self.layer = None
        self.mode = "after"
        self.stats = None
        self.auto_value = 2.0
        self.opts = hs.load()
        self._loading = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._live)
        self._build()
        self._loading = False

    # ------------------------------------------------------------------ UI
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(300)
        lay.addWidget(self.title)
        self.terrain = QLabel()
        self.terrain.setObjectName("muted")
        self.terrain.setWordWrap(True)
        self.terrain.setMinimumWidth(300)
        lay.addWidget(self.terrain)

        form = QFormLayout()
        form.setVerticalSpacing(4)
        self.hillshade = QCheckBox("Add hillshade (RAS Mapper look)")
        form.addRow(self.hillshade)

        self.strength = QSlider(Qt.Orientation.Horizontal)
        self.strength.setRange(5, 100)
        self.strength_lbl = QLabel()
        self.strength_lbl.setMinimumWidth(36)
        row = QHBoxLayout()
        row.addWidget(self.strength, 1)
        row.addWidget(self.strength_lbl)
        form.addRow("Strength", row)

        self.auto_z = QCheckBox("Auto")
        self.z = QDoubleSpinBox()
        self.z.setRange(0.1, 100)
        self.z.setDecimals(1)
        self.z.setSingleStep(0.5)
        self.z.setSuffix(" ×")
        row = QHBoxLayout()
        row.addWidget(self.auto_z)
        row.addWidget(self.z, 1)
        form.addRow("Relief", row)

        self.multi = QCheckBox("Soft light from all sides")
        form.addRow("Light", self.multi)
        self.blend = QComboBox()
        for k, label, _m in hs.BLENDS:
            self.blend.addItem(label.split(" (")[0], k)
        form.addRow("Blending", self.blend)
        self.smooth = QSlider(Qt.Orientation.Horizontal)
        self.smooth.setRange(0, hs.SMOOTH_MAX)
        self.smooth.setPageStep(1)
        self.smooth.setToolTip("0 = sharp cells · 1–2 = smooth display (no blocky cells) · "
                               "3–10 = also softens the relief of the hillshade "
                               "(removes stair-steps and noise)")
        self.smooth_lbl = QLabel()
        self.smooth_lbl.setObjectName("muted")
        form.addRow("Smooth", self.smooth)
        form.addRow("", self.smooth_lbl)
        lay.addLayout(form)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self.show_next = QCheckBox("Open this page after applying")
        self.show_next.setToolTip("Untick to apply symbols and colour ramps and stay on the "
                                  "gallery (turn it back on in ⋯ ▸ Open style settings "
                                  "after applying).")
        lay.addWidget(self.show_next)

        buttons = QHBoxLayout()
        self.more_btn = QPushButton("More…")
        self.more_btn.setToolTip("Full hillshade options: light direction, shaded-relief image…")
        self.cancel_btn = QPushButton("Undo")
        self.cancel_btn.setToolTip("Remove this colour ramp again")
        self.back_btn = QPushButton("‹  Change style" if self.dock._deferred
                                    else "‹  Back to styles")
        self.back_btn.setToolTip("Back to the style gallery")
        self.back_btn.clicked.connect(self.dock.show_gallery)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.more_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        lay.addLayout(buttons)
        lay.addStretch(1)

        self.hillshade.toggled.connect(self._changed)
        self.strength.valueChanged.connect(self._changed)
        self.auto_z.toggled.connect(self._changed)
        self.z.valueChanged.connect(self._changed)
        self.multi.toggled.connect(self._changed)
        self.blend.currentIndexChanged.connect(self._changed)
        self.smooth.valueChanged.connect(self._changed)
        self.show_next.toggled.connect(
            lambda on: self.dock.set_dem_mode("after" if on else "direct"))
        self.cancel_btn.clicked.connect(self._cancel)
        self.more_btn.clicked.connect(self._more)

    # --------------------------------------------------------------- public
    def open_for(self, entry, layer, mode, dirty=False):
        """Show for *entry* on DEM *layer*; mode "after" (applied) or "before"."""
        same_layer = self.layer is layer
        self.entry, self.layer, self.mode = entry, layer, mode
        self.dirty = dirty
        if not same_layer or self.stats is None:
            self.stats = hs.terrain_stats(layer)
            self.auto_value = hs.auto_exaggeration(layer, self.stats)
        self.opts = hs.load()
        self._loading = True
        o = self.opts
        self.hillshade.setChecked(self.dock.act_hillshade.isChecked()
                                  if mode == "before"
                                  else applier.hillshade_for(layer) is not None)
        self.strength.setValue(int(round(o["opacity"] * 100)))
        self.auto_z.setChecked(o["auto_z"])
        self.z.setValue(self.auto_value if o["auto_z"] else o["z"])
        self.multi.setChecked(o["multi"])
        self.blend.setCurrentIndex(max(0, self.blend.findData(o["blend"])))
        self.smooth.setValue(int(o.get("smooth_level", 2)))
        self.show_next.blockSignals(True)
        self.show_next.setChecked(self.dock.dem_mode() != "direct")
        self.show_next.blockSignals(False)
        self._loading = False
        verb = "applied to" if mode == "after" else "for"
        self.title.setText("<b>%s</b> %s <b>%s</b>" % (entry.name, verb, layer.name()))
        st = self.stats
        if st:
            kind = ("flat floodplain" if st["mean_slope_deg"] < 1 else
                    "gentle terrain" if st["mean_slope_deg"] < 4 else
                    "hilly terrain" if st["mean_slope_deg"] < 12 else "mountains")
            self.terrain.setText("%.4g – %.4g, %s · auto relief %.1f×"
                                 % (st["min"], st["max"], kind, self.auto_value))
        else:
            self.terrain.setText("")
        self.cancel_btn.setVisible(mode == "after")
        self._sync()
        self.dock.show_style_page(self)

    # -------------------------------------------------------------- helpers
    def _sync(self):
        on = self.hillshade.isChecked()
        for w in (self.strength, self.auto_z, self.multi, self.blend):
            w.setEnabled(on)
        self.z.setEnabled(on and not self.auto_z.isChecked())
        if self.auto_z.isChecked():
            self.z.blockSignals(True)
            self.z.setValue(self.auto_value)
            self.z.blockSignals(False)
        self.strength_lbl.setText("%d %%" % self.strength.value())
        self.smooth_lbl.setText("%d · %s" % (self.smooth.value(),
                                             hs.smooth_label(self.smooth.value())))

    def _read(self):
        o = self.opts
        o["opacity"] = self.strength.value() / 100.0
        match = [k for k, p in hs.PRESETS.items() if int(p * 100) == self.strength.value()]
        o["strength"] = match[0] if match else "custom"
        o["auto_z"] = self.auto_z.isChecked()
        if not o["auto_z"]:
            o["z"] = self.z.value()
        o["multi"] = self.multi.isChecked()
        o["blend"] = self.blend.currentData()
        o["smooth_level"] = self.smooth.value()
        o["smooth"] = o["smooth_level"] > 0
        return o

    def _changed(self, *_):
        if self._loading:
            return
        self._sync()
        hs.save(self._read())
        self.dock.act_hillshade.setChecked(self.hillshade.isChecked())
        self.dirty = True
        if self.mode == "after":
            self._timer.start()

    def _live(self):
        """Mode "after": show every change on the map immediately."""
        layer = self.layer
        if layer is None or not applier.is_dem(layer):
            return
        hs.smooth(layer, self.opts["smooth_level"])
        if self.hillshade.isChecked():
            had = applier.hillshade_for(layer) is not None
            try:
                lyr = applier.add_hillshade(layer, self.opts)
            except Exception as ex:
                self.dock._info(str(ex), "warning")
                return
            hs.configure_live(lyr, layer, self.opts,
                              hs.exaggeration(layer, self.opts, self.stats))
            if not had:
                self.dock.note_added_layer(layer, lyr)
        else:
            applier.remove_hillshade(layer)

    def commit(self):
        """Apply the ramp ("before" mode; the Apply / OK of Layer Properties)."""
        if self.mode != "before" or not self.dirty or self.layer is None:
            return False
        hs.save(self._read())
        self.dock.act_hillshade.setChecked(self.hillshade.isChecked())
        self.dirty = False
        self.dock.apply_dem_ramp_now(self.entry, self.layer)
        return True

    def flush(self):
        """Leaving the page: show a change still waiting for the live timer."""
        if self._timer.isActive():
            self._timer.stop()
            self._live()

    def _cancel(self):
        if self.mode == "after":
            self.dock.undo()
        self.dock.show_gallery()

    def _more(self):
        self.dock.hillshade_options()

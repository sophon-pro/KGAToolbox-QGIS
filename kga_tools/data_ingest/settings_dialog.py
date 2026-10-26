"""Ingest Settings dialog - the counterpart of Premiere Pro's
Project Settings > Ingest Settings (the wrench next to the Ingest box)."""

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from . import presets as P


class IngestSettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ingest Settings")
        self.setMinimumWidth(560)
        self._settings = P.normalize(settings)
        self._build()
        self._load(self._settings)

    # ------------------------------------------------------------------ UI
    def _build(self):
        lay = QVBoxLayout(self)

        # Presets row
        row = QHBoxLayout()
        row.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self._fill_presets()
        self.preset_combo.activated.connect(self._preset_chosen)
        row.addWidget(self.preset_combo, 1)
        for text, slot in (("Save as…", self._save_preset),
                           ("Delete", self._delete_preset),
                           ("Import…", self._import_preset),
                           ("Export…", self._export_preset)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        # Ingest operation
        box = QGroupBox("Ingest operation")
        form = QFormLayout(box)
        self.mode_combo = QComboBox()
        for key, label in P.MODE_LABELS.items():
            self.mode_combo.addItem(label, key)
        self.mode_combo.currentIndexChanged.connect(self._update_enabled)
        form.addRow("Operation:", self.mode_combo)
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("<project folder>/ingest  (default)")
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse_dest)
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(browse)
        form.addRow("Primary destination:", dest_row)
        lay.addWidget(box)

        # Conversion ("transcode")
        self.conv_box = QGroupBox("Convert (transcode) settings")
        form = QFormLayout(self.conv_box)
        self.vector_fmt = QComboBox()
        self.vector_fmt.addItems(["GPKG", "FlatGeobuf", "ESRI Shapefile", "GeoJSON", "Parquet"])
        form.addRow("Vector format:", self.vector_fmt)
        self.raster_fmt = QComboBox()
        self.raster_fmt.addItem("Cloud Optimized GeoTIFF (COG)", "COG")
        self.raster_fmt.addItem("Tiled GeoTIFF + overviews", "GTiff")
        form.addRow("Raster format:", self.raster_fmt)
        self.compress = QComboBox()
        self.compress.addItems(["DEFLATE", "LZW", "ZSTD", "NONE"])
        form.addRow("Raster compression:", self.compress)
        self.reproject = QCheckBox("Reproject to the project CRS while converting")
        form.addRow("", self.reproject)
        lay.addWidget(self.conv_box)

        # Proxies
        self.proxy_box = QGroupBox("Proxy settings")
        form = QFormLayout(self.proxy_box)
        self.overviews = QCheckBox("Build raster overviews (pyramids)")
        form.addRow("", self.overviews)
        self.proxy_pct = QSpinBox()
        self.proxy_pct.setRange(0, 99)
        self.proxy_pct.setSuffix(" %  (0 = no low-res proxy)")
        form.addRow("Low-res raster proxy:", self.proxy_pct)
        self.spatial_index = QCheckBox("Build spatial index on copied/converted Shapefiles")
        form.addRow("", self.spatial_index)
        lay.addWidget(self.proxy_box)

        # Organisation
        box = QGroupBox("Organisation in the Layers panel")
        form = QFormLayout(box)
        self.bin_combo = QComboBox()
        for key, label in P.BIN_LABELS.items():
            self.bin_combo.addItem(label, key)
        self.bin_combo.currentIndexChanged.connect(self._update_enabled)
        form.addRow("Bins:", self.bin_combo)
        self.custom_bin = QLineEdit()
        form.addRow("Custom bin name:", self.custom_bin)
        self.sublayers = QComboBox()
        self.sublayers.addItem("Ask me (show sublayer picker)", "ask")
        self.sublayers.addItem("Load all sublayers", "all")
        self.sublayers.addItem("Load first sublayer only", "first")
        form.addRow("Multi-layer files:", self.sublayers)
        self.zoom = QCheckBox("Zoom to imported data")
        form.addRow("", self.zoom)
        self.history = QCheckBox("Record provenance in layer metadata (History)")
        form.addRow("", self.history)
        lay.addWidget(box)

        # CSV
        box = QGroupBox("Tables / CSV")
        form = QFormLayout(box)
        self.auto_xy = QCheckBox("Auto-detect X/Y (lon/lat, easting/northing) or WKT columns")
        form.addRow("", self.auto_xy)
        self.csv_crs = QLineEdit()
        self.csv_crs.setPlaceholderText("EPSG:4326")
        form.addRow("Default CRS for coordinates:", self.csv_crs)
        lay.addWidget(box)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _fill_presets(self, select=None):
        self.preset_combo.clear()
        for p in P.all_presets():
            label = p["name"] + ("" if P.is_builtin(p["name"]) else "  (user)")
            self.preset_combo.addItem(label, p)
        if select:
            for i in range(self.preset_combo.count()):
                if self.preset_combo.itemData(i)["name"] == select:
                    self.preset_combo.setCurrentIndex(i)

    # --------------------------------------------------------------- values
    def _load(self, s):
        self._set_combo(self.mode_combo, s["mode"])
        self.dest_edit.setText(s["destination"])
        self.vector_fmt.setCurrentText(s["vector_format"])
        self._set_combo(self.raster_fmt, s["raster_format"])
        self.compress.setCurrentText(s["raster_compress"])
        self.reproject.setChecked(bool(s["reproject"]))
        self.overviews.setChecked(bool(s["build_overviews"]))
        self.proxy_pct.setValue(int(s["proxy_raster_percent"]))
        self.spatial_index.setChecked(bool(s["spatial_index"]))
        self._set_combo(self.bin_combo, s["bin_mode"])
        self.custom_bin.setText(s["custom_bin"])
        self._set_combo(self.sublayers, s["load_sublayers"])
        self.zoom.setChecked(bool(s["zoom_to"]))
        self.history.setChecked(bool(s["write_history"]))
        self.auto_xy.setChecked(bool(s["auto_xy"]))
        self.csv_crs.setText(s["default_csv_crs"])
        self._fill_presets(select=s.get("name"))
        self._update_enabled()

    @staticmethod
    def _set_combo(combo, data):
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def settings(self):
        return P.normalize({
            "name": self._settings.get("name", "Custom"),
            "mode": self.mode_combo.currentData(),
            "destination": self.dest_edit.text().strip(),
            "vector_format": self.vector_fmt.currentText(),
            "raster_format": self.raster_fmt.currentData(),
            "raster_compress": self.compress.currentText(),
            "reproject": self.reproject.isChecked(),
            "build_overviews": self.overviews.isChecked(),
            "proxy_raster_percent": self.proxy_pct.value(),
            "spatial_index": self.spatial_index.isChecked(),
            "bin_mode": self.bin_combo.currentData(),
            "custom_bin": self.custom_bin.text().strip() or "Imported",
            "load_sublayers": self.sublayers.currentData(),
            "zoom_to": self.zoom.isChecked(),
            "write_history": self.history.isChecked(),
            "auto_xy": self.auto_xy.isChecked(),
            "default_csv_crs": self.csv_crs.text().strip() or "EPSG:4326",
        })

    def _update_enabled(self, *_):
        mode = self.mode_combo.currentData()
        self.conv_box.setEnabled(mode in (P.MODE_CONVERT, P.MODE_CONVERT_PROXY))
        self.proxy_box.setEnabled(mode in (P.MODE_PROXY, P.MODE_COPY_PROXY, P.MODE_CONVERT_PROXY))
        self.dest_edit.setEnabled(mode != P.MODE_LINK)
        self.custom_bin.setEnabled(self.bin_combo.currentData() == P.BIN_CUSTOM)

    # ------------------------------------------------------------- presets
    def _preset_chosen(self, idx):
        preset = self.preset_combo.itemData(idx)
        if preset:
            self._settings = P.normalize(preset)
            self._load(self._settings)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:",
                                        text=self._settings.get("name", ""))
        if not ok or not name.strip():
            return
        if P.is_builtin(name.strip()):
            QMessageBox.warning(self, "Save preset", "Built-in presets cannot be overwritten.")
            return
        s = self.settings()
        s["name"] = name.strip()
        P.upsert_user_preset(s)
        self._settings = s
        self._fill_presets(select=s["name"])

    def _delete_preset(self):
        preset = self.preset_combo.currentData()
        if not preset or P.is_builtin(preset["name"]):
            QMessageBox.information(self, "Delete preset", "Only user presets can be deleted.")
            return
        P.delete_user_preset(preset["name"])
        self._fill_presets()

    def _import_preset(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import preset", "", "Ingest preset (*.json)")
        if not path:
            return
        try:
            preset = P.import_preset_file(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import preset", str(exc))
            return
        self._settings = preset
        self._load(preset)

    def _export_preset(self):
        s = self.settings()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export preset", s["name"].replace(" ", "_") + ".json",
            "Ingest preset (*.json)")
        if path:
            P.export_preset_file(s, path)

    def _browse_dest(self):
        path = QFileDialog.getExistingDirectory(self, "Primary destination", self.dest_edit.text())
        if path:
            self.dest_edit.setText(path)

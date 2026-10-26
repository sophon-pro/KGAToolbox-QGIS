"""Interpret Data dialog - the counterpart of Premiere Pro's
Modify > Interpret Footage: override how a source is read before
(or without) changing the file itself."""

from qgis.core import QgsCoordinateReferenceSystem, QgsVectorDataProvider
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import inspector

DELIMS = [(",", "Comma ,"), (";", "Semicolon ;"), ("\t", "Tab"), ("|", "Pipe |"), (" ", "Space")]


class InterpretDialog(QDialog):
    """items: list of dicts with keys path, kind, name, overrides."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items
        self.single = len(items) == 1
        first = items[0]
        self.setWindowTitle("Interpret Data – %s" % (
            first["name"] if self.single else "%d items" % len(items)))
        self.setMinimumWidth(560)
        ov = dict(first.get("overrides") or {})
        kinds = {i["kind"] for i in items}

        lay = QVBoxLayout(self)
        general = QGroupBox("General")
        form = QFormLayout(general)

        self.name_edit = QLineEdit(ov.get("name", first["name"] if self.single else ""))
        self.name_edit.setEnabled(self.single)
        form.addRow("Layer name:", self.name_edit)

        self.crs_check = QCheckBox("Assume this CRS (override what the file says)")
        self.crs_check.setChecked(bool(ov.get("crs")))
        self.crs_widget = QgsProjectionSelectionWidget()
        if ov.get("crs"):
            self.crs_widget.setCrs(QgsCoordinateReferenceSystem(ov["crs"]))
        self.crs_widget.setEnabled(self.crs_check.isChecked())
        self.crs_check.toggled.connect(self.crs_widget.setEnabled)
        form.addRow(self.crs_check)
        form.addRow("CRS:", self.crs_widget)

        self.bin_edit = QLineEdit("/".join(ov.get("bin", [])) if ov.get("bin") else "")
        self.bin_edit.setPlaceholderText("Use preset bins (e.g. Fieldwork/2026)")
        form.addRow("Bin:", self.bin_edit)
        lay.addWidget(general)

        # Vector
        self.encoding = None
        self.subset = None
        if kinds & {inspector.KIND_VECTOR, inspector.KIND_TABLE}:
            box = QGroupBox("Vector / table")
            f = QFormLayout(box)
            self.encoding = QComboBox()
            self.encoding.addItem("(automatic)", "")
            for enc in QgsVectorDataProvider.availableEncodings():
                self.encoding.addItem(enc, enc)
            idx = self.encoding.findData(ov.get("encoding", ""))
            self.encoding.setCurrentIndex(max(idx, 0))
            f.addRow("Attribute encoding:", self.encoding)
            self.subset = QLineEdit(ov.get("subset", ""))
            self.subset.setPlaceholderText("Optional filter, e.g.  \"province\" = 'Kandal'")
            f.addRow("Import only features where:", self.subset)
            lay.addWidget(box)

        # Raster
        self.nodata = None
        if inspector.KIND_RASTER in kinds:
            box = QGroupBox("Raster")
            f = QFormLayout(box)
            self.nodata = QLineEdit("" if ov.get("nodata") in (None, "") else str(ov["nodata"]))
            self.nodata.setPlaceholderText("leave empty to keep the file's NoData")
            f.addRow("Treat as NoData:", self.nodata)
            lay.addWidget(box)

        # CSV (single delimited-text item only)
        self.csv = None
        if self.single and inspector.is_delimited_text(first["path"]):
            self._build_csv(lay, first, ov)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # --------------------------------------------------------------- CSV
    def _build_csv(self, lay, item, ov):
        sniff = inspector.sniff_csv(item["path"])
        csv_ov = ov.get("csv") or {}
        box = QGroupBox("Delimited text")
        f = QFormLayout(box)
        self.delim = QComboBox()
        for key, label in DELIMS:
            self.delim.addItem(label, key)
        idx = self.delim.findData(csv_ov.get("delimiter", sniff["delimiter"]))
        self.delim.setCurrentIndex(max(idx, 0))
        f.addRow("Delimiter:", self.delim)

        self.geom_mode = QComboBox()
        self.geom_mode.addItem("Point coordinates (X / Y)", "xy")
        self.geom_mode.addItem("Well-known text (WKT)", "wkt")
        self.geom_mode.addItem("No geometry (attribute table)", "none")
        f.addRow("Geometry:", self.geom_mode)

        fields = [""] + sniff["fields"]
        self.x_combo, self.y_combo, self.wkt_combo = QComboBox(), QComboBox(), QComboBox()
        for c in (self.x_combo, self.y_combo, self.wkt_combo):
            c.addItems(fields)
        self.x_combo.setCurrentText(csv_ov.get("x_field", sniff["x_field"]))
        self.y_combo.setCurrentText(csv_ov.get("y_field", sniff["y_field"]))
        self.wkt_combo.setCurrentText(csv_ov.get("wkt_field", sniff["wkt_field"]))
        f.addRow("X field (longitude/easting):", self.x_combo)
        f.addRow("Y field (latitude/northing):", self.y_combo)
        f.addRow("WKT field:", self.wkt_combo)
        mode = csv_ov.get("geom_mode") or (
            "wkt" if sniff["wkt_field"] else "xy" if sniff["x_field"] else "none")
        self.geom_mode.setCurrentIndex(max(self.geom_mode.findData(mode), 0))

        self.detect_types = QCheckBox("Detect field types (numbers, dates)")
        self.detect_types.setChecked(csv_ov.get("detect_types", True))
        f.addRow("", self.detect_types)
        if not ov.get("crs"):
            self.crs_check.setChecked(True)
            self.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))

        table = QTableWidget(len(sniff["rows"]), len(sniff["fields"]))
        table.setHorizontalHeaderLabels(sniff["fields"])
        for r, row in enumerate(sniff["rows"]):
            for c, val in enumerate(row[:len(sniff["fields"])]):
                table.setItem(r, c, QTableWidgetItem(val))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.setMaximumHeight(170)
        f.addRow(QLabel("Preview (first rows):"))
        f.addRow(table)
        self.geom_mode.currentIndexChanged.connect(self._csv_enable)
        self._csv_enable()
        self.csv = True
        lay.addWidget(box)

    def _csv_enable(self, *_):
        mode = self.geom_mode.currentData()
        self.x_combo.setEnabled(mode == "xy")
        self.y_combo.setEnabled(mode == "xy")
        self.wkt_combo.setEnabled(mode == "wkt")

    # ------------------------------------------------------------ result
    def _accept(self):
        if self.nodata is not None and self.nodata.text().strip():
            try:
                float(self.nodata.text().strip())
            except ValueError:
                self.nodata.setFocus()
                self.nodata.selectAll()
                return
        self.accept()

    def overrides(self):
        """Overrides common to all items (name only applies to single)."""
        ov = {}
        if self.single and self.name_edit.text().strip():
            ov["name"] = self.name_edit.text().strip()
        if self.crs_check.isChecked() and self.crs_widget.crs().isValid():
            crs = self.crs_widget.crs()
            ov["crs"] = crs.authid() or crs.toWkt()
        if self.bin_edit.text().strip():
            ov["bin"] = [p.strip() for p in self.bin_edit.text().split("/") if p.strip()]
        if self.encoding is not None and self.encoding.currentData():
            ov["encoding"] = self.encoding.currentData()
        if self.subset is not None and self.subset.text().strip():
            ov["subset"] = self.subset.text().strip()
        if self.nodata is not None and self.nodata.text().strip():
            ov["nodata"] = float(self.nodata.text().strip())
        if self.csv:
            ov["csv"] = {
                "delimiter": self.delim.currentData(),
                "geom_mode": self.geom_mode.currentData(),
                "x_field": self.x_combo.currentText(),
                "y_field": self.y_combo.currentText(),
                "wkt_field": self.wkt_combo.currentText(),
                "detect_types": self.detect_types.isChecked(),
            }
        return ov

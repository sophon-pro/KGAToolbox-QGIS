"""Mini Preview - a small, read-only map window for one dataset.

* Zoom in/out, pan, full extent, previous/next view, zoom to selection.
* Select features by clicking or dragging a box (Shift = add, Ctrl = remove),
  by attribute (field + operator + value picked from the data) or by
  expression; invert / clear / select all.
* Identify features, showing the fields chosen in "Fields" (field options)
  plus derived geometry attributes; raster layers report pixel values.
* Export the selection (or all features / features in view) to a NEW file
  in a choice of formats, fields and CRS; rasters export the visible area.

Non-destructive by design: the layer is opened read-only, is never added to
the project, and every export writes a new file - writing into the source
file, its folder dataset (GDB) or its GeoPackage is refused.

Heavy work (spatial and attribute selection, unique values, exports) runs in
background tasks so the window stays responsive on big datasets.
"""

import os

from qgis.core import (
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMemoryProviderUtils,
    QgsPointXY,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorFileWriterTask,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
    QgsApplication,
)
from qgis.gui import (
    QgsExpressionBuilderDialog,
    QgsFieldComboBox,
    QgsFileWidget,
    QgsHighlight,
    QgsMapCanvas,
    QgsMapTool,
    QgsMapToolIdentify,
    QgsMapToolPan,
    QgsMapToolZoom,
    QgsProjectionSelectionWidget,
    QgsRubberBand,
)
from qgis.PyQt.QtCore import QRect, Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QMenu,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import output_defaults as od
from . import compat
from . import loader
from . import tasks
from .compat import QAction, QActionGroup

LOG_TAG = "Data Explorer"
SELECTION_TABLE_LIMIT = 1000
SELECTION_COLOR = QColor(255, 214, 0, 200)

EXPORT_FORMATS = [
    # label, OGR driver, extension, supports layer name
    ("GeoPackage", "GPKG", ".gpkg", True),
    ("ESRI Shapefile", "ESRI Shapefile", ".shp", False),
    ("GeoJSON", "GeoJSON", ".geojson", False),
    ("FlatGeobuf", "FlatGeobuf", ".fgb", False),
    ("CSV (spreadsheet)", "CSV", ".csv", False),
    ("Excel workbook", "XLSX", ".xlsx", True),
    ("KML (Google Earth)", "KML", ".kml", False),
    ("GeoParquet", "Parquet", ".parquet", False),
]

OPERATORS = [
    ("=", "equals"), ("<>", "is not"), (">", "greater than"), (">=", "at least"),
    ("<", "less than"), ("<=", "at most"), ("contains", "contains"),
    ("starts", "starts with"), ("ends", "ends with"), ("in", "is one of (a, b, c)"),
    ("empty", "is empty"), ("notempty", "is not empty"),
]


def _icon(name):
    """QGIS theme icon, or one shipped with the plugin ("plugin:export.svg")."""
    if name.startswith("plugin:"):
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(os.path.dirname(__file__), "icons", name[len("plugin:"):]))
    return QgsApplication.getThemeIcon(name)


def _driver_available(driver):
    try:
        from osgeo import ogr
        return ogr.GetDriverByName(driver) is not None
    except ImportError:
        return driver in ("GPKG", "ESRI Shapefile", "GeoJSON", "CSV")


def exports_folder():
    """Where exports go by default: the project's default folder (set by the Catalog
    workspace), else <project folder>/exports, else a folder in the user's home."""
    base = od.project_folder()
    if not base:
        home = QgsProject.instance().homePath()
        base = os.path.join(home, "exports") if home else os.path.join(
            os.path.expanduser("~"), "KgaDataIngest", "exports")
    os.makedirs(base, exist_ok=True)
    return base


def protected_reason(out_path, source_path):
    """Why writing to out_path would modify the source (None if safe)."""
    if not out_path:
        return "Choose an output file."
    out = os.path.normcase(os.path.abspath(out_path))
    src = os.path.normcase(os.path.abspath(source_path.split("|")[0].replace("/vsizip/", "")))
    if out == src:
        return "That is the source file itself."
    if os.path.isdir(src) and (out + os.sep).startswith(src.rstrip(os.sep) + os.sep):
        return "That is inside the source dataset (%s)." % os.path.basename(src)
    src_stem, src_ext = os.path.splitext(src)
    out_stem, _ = os.path.splitext(out)
    if src_ext == ".shp" and out_stem == src_stem:
        return "That would overwrite the source Shapefile's companion files."
    try:
        if os.path.exists(out) and os.path.exists(src) and os.path.samefile(out, src):
            return "That is the source file itself."
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Map tools
# ---------------------------------------------------------------------------

class SelectTool(QgsMapTool):
    """Click or drag a box. Shift adds, Ctrl removes, Ctrl+Shift intersects."""

    def __init__(self, canvas, on_rect):
        super().__init__(canvas)
        self.on_rect = on_rect
        self.start = None
        self.band = QgsRubberBand(canvas, compat.geometry_type("Polygon"))
        self.band.setColor(QColor(255, 140, 0, 60))
        self.band.setStrokeColor(QColor(255, 140, 0))
        self.band.setWidth(1)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, e):  # noqa: N802
        self.start = e.pixelPoint()

    def canvasMoveEvent(self, e):  # noqa: N802
        if self.start is None:
            return
        p = e.pixelPoint()
        self.band.setToCanvasRectangle(QRect(self.start, p).normalized())

    def canvasReleaseEvent(self, e):  # noqa: N802
        if self.start is None:
            return
        p = e.pixelPoint()
        self.band.reset(compat.geometry_type("Polygon"))
        canvas = self.canvas()
        if (p - self.start).manhattanLength() < 4:
            r = self.searchRadiusMU(canvas)
            c = e.mapPoint()
            rect = QgsRectangle(c.x() - r, c.y() - r, c.x() + r, c.y() + r)
            single = True
        else:
            a = self.toMapCoordinates(self.start)
            b = self.toMapCoordinates(p)
            rect = QgsRectangle(a, b)
            single = False
        self.start = None
        mods = e.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if shift and ctrl:
            behavior = compat.select_behavior("IntersectSelection")
        elif shift:
            behavior = compat.select_behavior("AddToSelection")
        elif ctrl:
            behavior = compat.select_behavior("RemoveFromSelection")
        else:
            behavior = compat.select_behavior("SetSelection")
        self.on_rect(rect, behavior, single)

    def deactivate(self):
        self.band.reset(compat.geometry_type("Polygon"))
        super().deactivate()


class DrawAreaTool(QgsMapTool):
    """Draw a rectangle (drag) or a polygon (click points; right-click or double-click
    finishes, Esc cancels). Hands the finished shape, in canvas CRS, to `on_done`."""

    def __init__(self, canvas, polygon, on_done, on_cancel=None):
        super().__init__(canvas)
        self.polygon = polygon
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.start = None
        self.points = []
        self.band = QgsRubberBand(canvas, compat.geometry_type("Polygon"))
        self.band.setColor(QColor(0, 120, 215, 50))
        self.band.setStrokeColor(QColor(0, 120, 215))
        self.band.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _reset(self):
        self.start = None
        self.points = []
        self.band.reset(compat.geometry_type("Polygon"))

    def _finish(self, geom):
        self._reset()
        self.on_done(geom)

    def canvasPressEvent(self, e):  # noqa: N802
        if self.polygon:
            return
        self.start = QgsPointXY(e.mapPoint())

    def canvasMoveEvent(self, e):  # noqa: N802
        p = QgsPointXY(e.mapPoint())
        if self.polygon:
            if self.points:
                self.band.setToGeometry(QgsGeometry.fromPolygonXY([self.points + [p]]), None)
        elif self.start is not None:
            self.band.setToGeometry(QgsGeometry.fromRect(QgsRectangle(self.start, p)), None)

    def canvasReleaseEvent(self, e):  # noqa: N802
        p = QgsPointXY(e.mapPoint())
        if not self.polygon:
            if self.start is None:
                return
            rect = QgsRectangle(self.start, p)
            self.start = None
            if rect.width() == 0 or rect.height() == 0:
                self._reset()
                return
            self._finish(QgsGeometry.fromRect(rect))
            return
        if e.button() == Qt.MouseButton.RightButton:
            if len(self.points) >= 3:
                self._finish(QgsGeometry.fromPolygonXY([self.points]))
            else:
                self._reset()
            return
        self.points.append(p)
        self.band.setToGeometry(QgsGeometry.fromPolygonXY([self.points + [p]]), None)

    def canvasDoubleClickEvent(self, e):  # noqa: N802
        if self.polygon and len(self.points) >= 3:
            self._finish(QgsGeometry.fromPolygonXY([self.points]))

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self._reset()
            if self.on_cancel:
                self.on_cancel()
            e.accept()

    def deactivate(self):
        self.band.reset(compat.geometry_type("Polygon"))
        super().deactivate()


class IdentifyTool(QgsMapToolIdentify):
    def __init__(self, canvas, layer, on_results):
        super().__init__(canvas)
        self.layer = layer
        self.on_results = on_results
        self.setCursor(Qt.CursorShape.WhatsThisCursor)

    def canvasReleaseEvent(self, e):  # noqa: N802
        p = e.pixelPoint()
        try:
            results = self.identify(p.x(), p.y(), [self.layer], compat.identify_mode("TopDownAll"))
        except Exception as exc:
            QgsMessageLog.logMessage("Identify failed: %s" % exc, LOG_TAG)
            results = []
        self.on_results(results, e.mapPoint())


# ---------------------------------------------------------------------------
# Export dialog
# ---------------------------------------------------------------------------

class ExportDialog(QDialog):
    def __init__(self, layer, source_path, visible_fields, canvas_extent, parent=None, area=None, area_label=None):
        super().__init__(parent)
        self.layer = layer
        self.area = area  # QgsGeometry in layer CRS, or None for the map view
        if area is not None and canvas_extent is None:
            canvas_extent = area.boundingBox()
        self.source_path = source_path
        self.canvas_extent = canvas_extent
        self.setWindowTitle("Export features to a new file")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)

        box = QGroupBox("Features")
        bl = QVBoxLayout(box)
        n_sel, n_all = layer.selectedFeatureCount(), layer.featureCount()
        self.r_sel = QRadioButton("Selected features (%s)" % "{:,}".format(n_sel))
        self.r_all = QRadioButton("All features (%s)" % ("{:,}".format(n_all) if n_all >= 0 else "?"))
        self.r_view = QRadioButton("Features in the current map view" if area is None else
                                   "Features inside the %s" % (area_label or "chosen area"))
        self.r_sel.setEnabled(n_sel > 0)
        (self.r_sel if n_sel > 0 else self.r_all).setChecked(True)
        if area is not None:
            self.r_view.setChecked(True)
        group = QButtonGroup(self)
        for r in (self.r_sel, self.r_all, self.r_view):
            group.addButton(r)
            bl.addWidget(r)
            r.toggled.connect(lambda on: on and self._rename_default())
        lay.addWidget(box)

        form = QFormLayout()
        self.fmt = QComboBox()
        for label, driver, ext, named in EXPORT_FORMATS:
            if _driver_available(driver):
                self.fmt.addItem(label, (driver, ext, named))
        form.addRow("Format:", self.fmt)
        self.file = QgsFileWidget()
        self.file.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.file.setConfirmOverwrite(False)  # we ask ourselves, with a source check
        form.addRow("Save as (new file):", self.file)
        self.layer_name = QLineEdit(tasks_safe_name(layer.name()))
        form.addRow("Layer name:", self.layer_name)
        self.crs = QgsProjectionSelectionWidget()
        self.crs.setCrs(layer.crs())
        form.addRow("CRS:", self.crs)
        self.csv_geom = QComboBox()
        self.csv_geom.addItem("Geometry as WKT column", "AS_WKT")
        self.csv_geom.addItem("X / Y columns (points)", "AS_XY")
        self.csv_geom.addItem("No geometry", "")
        form.addRow("CSV geometry:", self.csv_geom)
        lay.addLayout(form)

        fbox = QGroupBox("Fields to export")
        fl = QVBoxLayout(fbox)
        self.fields = QListWidget()
        for i, f in enumerate(layer.fields()):
            it = QListWidgetItem(f.name())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if (not visible_fields or f.name() in visible_fields)
                             else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, i)
            self.fields.addItem(it)
        fl.addWidget(self.fields)
        row = QHBoxLayout()
        for text, state in (("All", Qt.CheckState.Checked), ("None", Qt.CheckState.Unchecked)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, s=state: [self.fields.item(i).setCheckState(s)
                                                         for i in range(self.fields.count())])
            row.addWidget(b)
        row.addStretch()
        fl.addLayout(row)
        lay.addWidget(fbox)

        self.add_to_project = QCheckBox("Add the new file to the project")
        self.add_to_project.setChecked(True)
        lay.addWidget(self.add_to_project)
        note = QLabel("🔒 The source data is never modified - this always writes a new file.")
        note.setWordWrap(True)
        lay.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.fmt.currentIndexChanged.connect(self._format_changed)
        self._format_changed()

    def _suffix(self):
        return "_selection" if self.r_sel.isChecked() else ("_in_view" if self.r_view.isChecked() else "_export")

    def _project_db(self):
        """The project's default GeoPackage, unless it is the data being exported."""
        db = od.project_database()
        if db and os.path.normcase(os.path.abspath(db)) != os.path.normcase(
                os.path.abspath(self.source_path.split("|")[0])):
            return db
        return ""

    def _is_project_db(self, path):
        db = self._project_db()
        return bool(db) and os.path.normcase(os.path.abspath(path or "")) == os.path.normcase(os.path.abspath(db))

    def _default_path(self, ext):
        db = self._project_db()
        if ext == ".gpkg" and db:  # like ArcGIS Pro: a new layer in the project's default database
            return db
        return unique_path(os.path.join(exports_folder(), tasks_safe_name(self.layer.name()) + self._suffix() + ext))

    def _sync_layer_name(self):
        """Into the project database the layer needs an unused table name."""
        if self._is_project_db(self.file.filePath()):
            self.layer_name.setText(od.unique_table(
                self._project_db(), od.safe_stem(tasks_safe_name(self.layer.name()) + self._suffix())))

    def _is_default_path(self, path):
        import re
        if self._is_project_db(path):
            return True
        base = os.path.splitext(os.path.basename(path))[0]
        return re.fullmatch(re.escape(tasks_safe_name(self.layer.name())) +
                            r"_(selection|in_view|export)(_\d+)?", base) is not None

    def _rename_default(self):
        if not hasattr(self, "fmt") or self.fmt.currentData() is None:
            return
        current = self.file.filePath()
        if not current or self._is_default_path(current):
            self.file.setFilePath(self._default_path(self.fmt.currentData()[1]))
            self._sync_layer_name()

    def _format_changed(self, *_):
        driver, ext, named = self.fmt.currentData()
        self.layer_name.setEnabled(named)
        self.csv_geom.setEnabled(driver == "CSV")
        self.file.setFilter("%s (*%s)" % (self.fmt.currentText(), ext))
        current = self.file.filePath()
        if not current or self._is_default_path(current):
            self.file.setFilePath(self._default_path(ext))
            self._sync_layer_name()
        else:
            self.file.setFilePath(unique_path(os.path.splitext(current)[0] + ext))

    def _accept(self):
        path = self.file.filePath()
        driver, ext, _named = self.fmt.currentData()
        if path and not path.lower().endswith(ext):
            path += ext
            self.file.setFilePath(path)
        reason = protected_reason(path, self.source_path)
        if reason:
            QMessageBox.warning(self, "Export", "Not allowed - this tool never modifies source data.\n" + reason)
            return
        if not self.selected_attributes():
            QMessageBox.warning(self, "Export", "Choose at least one field.")
            return
        if self._is_project_db(path):
            # Adding a layer to the existing database: only ask when that layer name is taken.
            name = self.layer_name.text().strip()
            if name.lower() in (od._table_names(path) or set()) and QMessageBox.question(
                    self, "Export", "The layer '%s' already exists in %s. Replace it?"
                    % (name, os.path.basename(path))) != QMessageBox.StandardButton.Yes:
                return
        elif os.path.exists(path):
            if QMessageBox.question(self, "Export", "%s already exists. Replace it?" % os.path.basename(path)) \
                    != QMessageBox.StandardButton.Yes:
                return
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.accept()

    def selected_attributes(self):
        return [self.fields.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.fields.count())
                if self.fields.item(i).checkState() == Qt.CheckState.Checked]

    def options(self):
        driver, _ext, named = self.fmt.currentData()
        o = QgsVectorFileWriter.SaveVectorOptions()
        o.driverName = driver
        o.fileEncoding = "UTF-8"
        if named and self.layer_name.text().strip():
            o.layerName = self.layer_name.text().strip()
        o.attributes = self.selected_attributes()
        # Never rewrite the whole project database: only its one layer.
        o.actionOnExistingFile = compat.writer_action(
            "CreateOrOverwriteLayer" if self._is_project_db(self.file.filePath()) else "CreateOrOverwriteFile")
        if self.r_sel.isChecked():
            o.onlySelectedFeatures = True
        elif self.r_view.isChecked() and self.canvas_extent is not None:
            o.filterExtent = self.canvas_extent
        dest = self.crs.crs()
        if dest.isValid() and self.layer.crs().isValid() and dest != self.layer.crs():
            o.ct = QgsCoordinateTransform(self.layer.crs(), dest, QgsProject.instance())
        if driver == "CSV" and self.csv_geom.currentData():
            o.layerOptions = ["GEOMETRY=%s" % self.csv_geom.currentData()]
        return o

    def output_path(self):
        return self.file.filePath()

    def exact_area(self):
        """The polygon to cut features with, when the area is not a plain rectangle."""
        if self.area is None or not self.r_view.isChecked():
            return None
        if self.area.isGeosEqual(QgsGeometry.fromRect(self.area.boundingBox())):
            return None
        return self.area


def tasks_safe_name(text):
    import re
    return re.sub(r"[^\w\-]+", "_", text or "layer", flags=re.UNICODE).strip("_") or "layer"


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists("%s_%d%s" % (base, i, ext)):
        i += 1
    return "%s_%d%s" % (base, i, ext)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class LayerPickDialog(QDialog):
    """Choose a layer from the current map, or browse for a file that is not in it."""

    def __init__(self, title, text, candidates, preview_layer, polygon_only, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.polygon_only = polygon_only
        self.preview_layer = preview_layer
        self._layers = []  # keeps browsed layers alive
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(text))
        row = QHBoxLayout()
        self.combo = QComboBox()
        row.addWidget(self.combo, 1)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self.browse)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for lyr in candidates:
            self._add(lyr)
        if self.combo.count():
            self.combo.setCurrentIndex(0)
        else:
            self.note.setText("No layer in the current map fits - use Browse to pick a file.")

    def _add(self, lyr):
        sel = lyr.selectedFeatureCount() if isinstance(lyr, QgsVectorLayer) else 0
        label = "%s%s%s" % (lyr.name(), " (this layer)" if lyr is self.preview_layer else "",
                            " - %d selected" % sel if sel else "")
        self.combo.addItem(label, lyr)

    def browse(self):
        from qgis.PyQt.QtWidgets import QFileDialog
        flt = "Vector data (*.shp *.gpkg *.geojson *.json *.kml *.gml *.fgb *.gdb);;All files (*)"
        if not self.polygon_only:
            flt = "Vector or raster data (*);;" + flt
        path, _ = QFileDialog.getOpenFileName(self, "Choose a layer", "", flt)
        if path:
            self.add_file(path)

    def add_file(self, path):
        name = os.path.splitext(os.path.basename(path))[0]
        lyr = QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid() and not self.polygon_only:
            lyr = QgsRasterLayer(path, name, "gdal")
        if not lyr.isValid():
            self.note.setText("Cannot read %s." % os.path.basename(path))
            return False
        if self.polygon_only and lyr.geometryType() != compat.geometry_type("Polygon"):
            self.note.setText("%s has no polygons." % os.path.basename(path))
            return False
        self._layers.append(lyr)
        self.note.setText("")
        self._add(lyr)
        self.combo.setCurrentIndex(self.combo.count() - 1)
        return True

    def chosen(self):
        return self.combo.currentData()


class MiniPreviewDialog(QDialog):
    def __init__(self, layer, data, iface, parent=None):
        super().__init__(parent)
        self.layer = layer
        self.data = data
        self.iface = iface
        self.source_path = data.get("path", layer.source())
        self.is_vector = isinstance(layer, QgsVectorLayer)
        self.is_raster = isinstance(layer, QgsRasterLayer)
        self.highlights = []
        self.identified = []
        self.visible_fields = []
        self._tasks = []
        self._values_task = None
        self._closing = False
        self.last_export = None
        if self.is_vector:
            layer.setReadOnly(True)
            self.visible_fields = [f.name() for f in layer.fields()]

        self.setWindowTitle("Mini Preview – %s  (read-only)" % (data.get("name") or layer.name()))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint |
                            Qt.WindowType.WindowMinimizeButtonHint)
        self.resize(1100, 700)
        self.setModal(False)
        self._build()
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.timeout.connect(self._refresh_selection_views)
        if self.is_vector:
            layer.selectionChanged.connect(lambda *_: self._sel_timer.start(120))
        self.zoom_full()
        self.act_pan.trigger()
        self._update_selection_label()

    # ------------------------------------------------------------------ UI
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.toolbar = QToolBar()
        lay.addWidget(self.toolbar)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(QColor(255, 255, 255))
        self.canvas.setSelectionColor(SELECTION_COLOR)
        self.canvas.enableAntiAliasing(True)
        if self.layer.crs().isValid():
            self.canvas.setDestinationCrs(self.layer.crs())
        self.canvas.setLayers([self.layer])
        self.canvas.xyCoordinates.connect(self._show_xy)
        self.canvas.scaleChanged.connect(self._show_scale)
        split.addWidget(self.canvas)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._identify_tab(), "Identify")
        if self.is_vector:
            self.tabs.addTab(self._find_tab(), "Find / Select")
            self.tabs.addTab(self._selection_tab(), "Selection")
            self.tabs.addTab(self._fields_tab(), "Fields")
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)

        status = QHBoxLayout()
        self.lock_label = QLabel("🔒 Read-only preview – exports create new files")
        self.sel_label = QLabel("")
        self.xy_label = QLabel("")
        self.scale_label = QLabel("")
        status.addWidget(self.lock_label)
        status.addStretch(1)
        for w in (self.sel_label, self.xy_label, self.scale_label):
            status.addWidget(w)
        lay.addLayout(status)

        self._build_toolbar()

    def _action(self, icon, text, slot, checkable=False, shortcut=None):
        act = QAction(_icon(icon), text, self)
        act.setCheckable(checkable)
        if shortcut:
            act.setShortcut(shortcut)
            act.setToolTip("%s (%s)" % (text, shortcut))
        act.triggered.connect(lambda _c=False: slot())
        self.toolbar.addAction(act)
        return act

    def _build_toolbar(self):
        self.tool_pan = QgsMapToolPan(self.canvas)
        self.tool_zin = QgsMapToolZoom(self.canvas, False)
        self.tool_zout = QgsMapToolZoom(self.canvas, True)
        self.tool_identify = IdentifyTool(self.canvas, self.layer, self._identify_results)
        group = QActionGroup(self)
        self.act_pan = self._action("/mActionPan.svg", "Pan", lambda: self._set_tool(self.tool_pan, self.act_pan), True, "P")
        self.act_zin = self._action("/mActionZoomIn.svg", "Zoom in", lambda: self._set_tool(self.tool_zin, self.act_zin), True, "+")
        self.act_zout = self._action("/mActionZoomOut.svg", "Zoom out", lambda: self._set_tool(self.tool_zout, self.act_zout), True, "-")
        tools = [self.act_pan, self.act_zin, self.act_zout]
        self._action("/mActionZoomFullExtent.svg", "Zoom to full extent", self.zoom_full, shortcut="Ctrl+Shift+F")
        self._action("/mActionZoomLast.svg", "Previous view", self.canvas.zoomToPreviousExtent)
        self._action("/mActionZoomNext.svg", "Next view", self.canvas.zoomToNextExtent)
        if self.is_vector:
            self._action("/mActionZoomToSelected.svg", "Zoom to selection", self.zoom_selection, shortcut="Ctrl+J")
        self.toolbar.addSeparator()
        if self.is_vector:
            self.tool_select = SelectTool(self.canvas, self._select_rect)
            self.act_select = self._action("/mActionSelectRectangle.svg",
                                           "Select features (click or drag; Shift adds, Ctrl removes)",
                                           lambda: self._set_tool(self.tool_select, self.act_select), True, "S")
            tools.append(self.act_select)
            self._action("/mActionSelectAll.svg", "Select all", self.select_all, shortcut="Ctrl+A")
            self._action("/mActionInvertSelection.svg", "Invert selection", self.invert_selection)
            self._action("/mActionDeselectAll.svg", "Clear selection", self.clear_selection, shortcut="Ctrl+Shift+A")
            self._action("/mIconExpressionSelect.svg", "Select by expression…", self.select_by_expression)
            self.toolbar.addSeparator()
        self.act_identify = self._action("/mActionIdentify.svg", "Identify features",
                                         lambda: self._set_tool(self.tool_identify, self.act_identify), True, "I")
        tools.append(self.act_identify)
        self.toolbar.addSeparator()
        self._build_export_button()
        for a in tools:
            group.addAction(a)
        group.setExclusive(True)

    def _build_export_button(self):
        """Export drop-down: the preview canvas, a drawn rectangle or polygon, the layer
        extent, or the boundary of a polygon layer."""
        what = "features" if self.is_vector else "the area"
        menu = QMenu(self)
        self.export_actions = {}
        for key, icon, text, tip in (
                ("canvas", "/mActionZoomToArea.svg", "Current preview canvas",
                 "Export what the preview shows now"),
                ("rect", "/mActionSelectRectangle.svg", "Draw rectangle",
                 "Drag a rectangle on the preview, then export %s inside it" % what),
                ("polygon", "/mActionSelectPolygon.svg", "Draw polygon",
                 "Click the corners (right-click or double-click to finish), then export %s inside it" % what),
                ("layer", "/mActionZoomFullExtent.svg", "Layer extent...", "Export the part inside the extent of a layer"),
                ("boundary", "/mActionSelectPolygon.svg", "Polygon boundary...",
                 "Choose a polygon layer; its (selected) polygons cut the export")):
            act = QAction(_icon(icon), text, self)
            act.setToolTip(tip)
            act.setStatusTip(tip)
            act.triggered.connect(lambda _c=False, k=key: self.start_export(k))
            if key == "canvas":
                act.setShortcut("Ctrl+E")
            menu.addAction(act)
            self.export_actions[key] = act
            if key == "polygon":
                menu.addSeparator()
        self.act_export = QAction(_icon("plugin:export.svg"), "Export", self)
        self.act_export.setMenu(menu)
        self.act_export.setToolTip("Export to a new file")
        self.toolbar.addAction(self.act_export)
        btn = self.toolbar.widgetForAction(self.act_export)
        if isinstance(btn, QToolButton):
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    def start_export(self, mode):
        """Entry point of every Export menu item."""
        if mode == "canvas":
            self._export_area(self._to_layer_crs(QgsGeometry.fromRect(self.canvas.extent())),
                              "current preview canvas")
        elif mode == "layer":
            self._export_layer_extent()
        elif mode in ("rect", "polygon"):
            self._draw_for_export(mode == "polygon")
        elif mode == "boundary":
            self._export_boundary()

    def _to_layer_crs(self, geom):
        geom = QgsGeometry(geom)
        src = self.canvas.mapSettings().destinationCrs()
        if src.isValid() and self.layer.crs().isValid() and src != self.layer.crs():
            geom.transform(QgsCoordinateTransform(src, self.layer.crs(), QgsProject.instance()))
        return geom

    def _draw_for_export(self, polygon):
        previous = self.canvas.mapTool()
        self._draw_tool = DrawAreaTool(
            self.canvas, polygon,
            lambda g: self._drawn(g, previous, polygon),
            on_cancel=lambda: self._restore_tool(previous))
        self.canvas.setMapTool(self._draw_tool)
        self.iface.messageBar().pushInfo(
            "Mini Preview", "Draw a polygon on the preview: click the corners, right-click to finish (Esc cancels)."
            if polygon else "Drag a rectangle on the preview (Esc cancels).")

    def _restore_tool(self, tool):
        if tool is not None:
            self.canvas.setMapTool(tool)

    def _drawn(self, geom, previous, polygon):
        self._restore_tool(previous)
        self._export_area(self._to_layer_crs(geom), "drawn polygon" if polygon else "drawn rectangle")

    def _map_layers(self, vector_only, polygon_only):
        """Layers of the current map (the project's layer panel), optionally only polygon layers."""
        found = []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            lyr = node.layer()
            if lyr is None or not lyr.isValid():
                continue
            if isinstance(lyr, QgsVectorLayer):
                if polygon_only and lyr.geometryType() != compat.geometry_type("Polygon"):
                    continue
            elif vector_only or polygon_only or not isinstance(lyr, QgsRasterLayer):
                continue
            found.append(lyr)
        return found

    def _pick_layer(self, title, text, polygon_only):
        """Ask which layer to use: the previewed one, a layer of the current map, or a browsed file."""
        candidates = self._map_layers(polygon_only, polygon_only)
        if not polygon_only or (self.is_vector and self.layer.geometryType() == compat.geometry_type("Polygon")):
            candidates.insert(0, self.layer)
        dlg = LayerPickDialog(title, text, candidates, self.layer, polygon_only, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.chosen()

    def _boundary_geometry(self, lyr):
        """Union of the selected polygons of `lyr` (all of them if none are selected), in layer CRS."""
        feats = list(lyr.getSelectedFeatures()) if lyr.selectedFeatureCount() else list(lyr.getFeatures())
        geoms = [f.geometry() for f in feats if f.hasGeometry() and not f.geometry().isEmpty()]
        if not geoms:
            return None
        union = QgsGeometry.unaryUnion(geoms)
        if lyr.crs().isValid() and self.layer.crs().isValid() and lyr.crs() != self.layer.crs():
            union.transform(QgsCoordinateTransform(lyr.crs(), self.layer.crs(), QgsProject.instance()))
        return union

    def _export_boundary(self):
        """Pick a polygon layer (or browse for a file); its selected polygons (else all) cut the export."""
        lyr = self._pick_layer("Polygon boundary", "Use the polygons of (selected ones if any, else all):", True)
        if lyr is None:
            return
        geom = self._boundary_geometry(lyr)
        if geom is None:
            QMessageBox.information(self, "Export", "That layer has no polygons to use.")
            return
        self._export_area(geom, "boundary of %s" % lyr.name())

    def _export_layer_extent(self):
        lyr = self._pick_layer("Layer extent", "Export the part inside the extent of:", False)
        if lyr is None:
            return
        if lyr is self.layer:
            self._export_area(QgsGeometry.fromRect(self.layer.extent()), "layer extent", whole=True)
            return
        rect = lyr.extent()
        if lyr.crs().isValid() and self.layer.crs().isValid() and lyr.crs() != self.layer.crs():
            rect = QgsCoordinateTransform(lyr.crs(), self.layer.crs(),
                                          QgsProject.instance()).transformBoundingBox(rect)
        self._export_area(QgsGeometry.fromRect(rect), "extent of %s" % lyr.name())

    def _export_area(self, geom, label, whole=False):
        if geom is None or geom.isEmpty():
            return
        if not geom.boundingBox().intersects(self.layer.extent()):
            QMessageBox.information(self, "Export", "The %s does not overlap the layer." % label)
            return
        if self.is_vector:
            self.export_features(area=geom, label=label, whole=whole)
        else:
            self.export_raster_view(area=geom, label=label)

    def _set_tool(self, tool, action):
        self.canvas.setMapTool(tool)
        action.setChecked(True)
        if tool is self.tool_identify:
            self.tabs.setCurrentIndex(0)

    # -------------------------------------------------------- identify tab
    def _identify_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Click the map with the Identify tool (I). The fields shown follow "
                             "the Fields tab." if self.is_vector else
                             "Click the map with the Identify tool (I) to read pixel values."))
        self.ident_tree = QTreeWidget()
        self.ident_tree.setHeaderLabels(["Feature / field", "Value"])
        self.ident_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.ident_tree.itemClicked.connect(self._ident_item_clicked)
        lay.addWidget(self.ident_tree, 1)
        row = QHBoxLayout()
        if self.is_vector:
            b = QPushButton("Select identified")
            b.clicked.connect(self._select_identified)
            row.addWidget(b)
            b = QPushButton("Zoom to identified")
            b.clicked.connect(self._zoom_identified)
            row.addWidget(b)
        b = QPushButton("Copy")
        b.setToolTip("Copy identified attributes as tab-separated text")
        b.clicked.connect(self._copy_identified)
        row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        return w

    def _display_field(self):
        if not self.is_vector:
            return None
        name = self.display_combo.currentText() if hasattr(self, "display_combo") else ""
        return name or None

    def _identify_results(self, results, point):
        self._clear_highlights()
        self.ident_tree.clear()
        self.identified = []
        if not results:
            self.ident_tree.addTopLevelItem(QTreeWidgetItem(["Nothing here", ""]))
            return
        display = self._display_field()
        shown = set(self.visible_fields) if self.is_vector else None
        for r in results:
            feat = r.mFeature
            if self.is_vector:
                label = str(feat.attribute(display)) if display and feat.fields().indexOf(display) >= 0 \
                    else "Feature %s" % feat.id()
                top = QTreeWidgetItem([label, "id %s" % feat.id()])
                top.setData(0, Qt.ItemDataRole.UserRole, feat.id())
                for f in feat.fields():
                    if shown is not None and f.name() not in shown:
                        continue
                    v = feat.attribute(f.name())
                    top.addChild(QTreeWidgetItem([f.name(), "" if v is None else str(v)]))
                derived = QTreeWidgetItem(["(derived)", ""])
                for k, v in (r.mDerivedAttributes or {}).items():
                    derived.addChild(QTreeWidgetItem([str(k), str(v)]))
                if derived.childCount():
                    top.addChild(derived)
                self.identified.append(feat)
                if feat.hasGeometry():
                    h = QgsHighlight(self.canvas, feat.geometry(), self.layer)
                    h.setColor(QColor(255, 0, 0))
                    h.setFillColor(QColor(255, 0, 0, 40))
                    h.setWidth(3)
                    h.show()
                    self.highlights.append(h)
            else:
                top = QTreeWidgetItem([r.mLabel or "Pixel", "%.3f, %.3f" % (point.x(), point.y())])
                for k, v in (r.mAttributes or {}).items():
                    top.addChild(QTreeWidgetItem([str(k), str(v)]))
            self.ident_tree.addTopLevelItem(top)
            top.setExpanded(True)

    def _ident_item_clicked(self, item, _col):
        fid = item.data(0, Qt.ItemDataRole.UserRole)
        if fid is not None and self.is_vector:
            self.canvas.flashFeatureIds(self.layer, [fid])

    def _select_identified(self):
        ids = [f.id() for f in self.identified]
        if ids:
            self.layer.selectByIds(ids, compat.select_behavior("AddToSelection"))

    def _zoom_identified(self):
        rect = None
        for f in self.identified:
            if f.hasGeometry():
                b = f.geometry().boundingBox()
                if rect is None:
                    rect = QgsRectangle(b)
                else:
                    rect.combineExtentWith(b)
        if rect is not None:
            self._zoom_to(rect)

    def _copy_identified(self):
        lines = []
        for i in range(self.ident_tree.topLevelItemCount()):
            top = self.ident_tree.topLevelItem(i)
            lines.append("%s\t%s" % (top.text(0), top.text(1)))
            for c in range(top.childCount()):
                ch = top.child(c)
                lines.append("  %s\t%s" % (ch.text(0), ch.text(1)))
        QApplication.clipboard().setText("\n".join(lines))

    def _clear_highlights(self):
        for h in self.highlights:
            try:
                h.hide()
                self.canvas.scene().removeItem(h)
            except RuntimeError:
                pass
        self.highlights = []

    # ------------------------------------------------------------ find tab
    def _find_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        # 1. the question, in words
        ask = QGroupBox("1.  Find features where…")
        form = QFormLayout(ask)
        self.find_field = QgsFieldComboBox()
        self.find_field.setLayer(self.layer)
        self.find_field.fieldChanged.connect(self._load_values)
        self.find_field.fieldChanged.connect(lambda *_: self._update_find_summary())
        form.addRow("Field:", self.find_field)
        self.find_op = QComboBox()
        for key, label in OPERATORS:
            self.find_op.addItem(label, key)
        self.find_op.currentIndexChanged.connect(self._op_changed)
        form.addRow("Condition:", self.find_op)
        self.find_value = QComboBox()
        self.find_value.setEditable(True)
        self.find_value.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.find_value.lineEdit().setPlaceholderText("type a value, or pick one from the list")
        self.find_value.editTextChanged.connect(lambda *_: self._update_find_summary())
        form.addRow("Value:", self.find_value)
        self.values_label = QLabel("")
        self.values_label.setStyleSheet("color: palette(mid);")
        form.addRow("", self.values_label)
        self.find_case = QCheckBox("Match upper/lower case exactly")
        self.find_case.toggled.connect(lambda *_: self._update_find_summary())
        form.addRow("", self.find_case)
        self.find_zoom = QCheckBox("Zoom to what is found")
        self.find_zoom.setChecked(True)
        form.addRow("", self.find_zoom)
        lay.addWidget(ask)

        # 2. what to do with the result
        act = QGroupBox("2.  Then…")
        al = QVBoxLayout(act)
        grid = QHBoxLayout()
        for text, beh, tip in (
                ("New selection", "SetSelection", "Select only the features found (replaces the current selection). Enter key does this too."),
                ("Add to selection", "AddToSelection", "Keep what is selected and add the features found"),
                ("Remove from selection", "RemoveFromSelection", "Unselect the features found"),
                ("Keep only these", "IntersectSelection", "Narrow the current selection to the features that also match")):
            b = QPushButton(text)
            b.setToolTip(tip)
            if beh == "SetSelection":
                b.setDefault(True)
            b.clicked.connect(lambda _c=False, bh=beh: self.select_by_attribute(compat.select_behavior(bh)))
            grid.addWidget(b)
        al.addLayout(grid)
        lay.addWidget(act)

        # 3. result, in words
        res = QGroupBox("Result")
        rl = QVBoxLayout(res)
        self.find_summary = QLabel("")
        self.find_summary.setWordWrap(True)
        self.find_summary.setTextFormat(Qt.TextFormat.RichText)
        rl.addWidget(self.find_summary)
        self.find_result = QLabel("Nothing searched yet.")
        self.find_result.setWordWrap(True)
        self.find_result.setTextFormat(Qt.TextFormat.RichText)
        rl.addWidget(self.find_result)
        exp_row = QHBoxLayout()
        self.show_expr = QCheckBox("Show as QGIS expression")
        self.show_expr.setToolTip("The same query written in QGIS expression language - copy it into "
                                  "Select by Expression, a filter or the Field Calculator")
        exp_row.addWidget(self.show_expr)
        exp_row.addStretch(1)
        rl.addLayout(exp_row)
        self.expr_box = QWidget()
        eb = QHBoxLayout(self.expr_box)
        eb.setContentsMargins(0, 0, 0, 0)
        self.expr_edit = QLineEdit()
        self.expr_edit.setReadOnly(True)
        eb.addWidget(self.expr_edit, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.expr_edit.text()))
        eb.addWidget(copy)
        self.expr_box.setVisible(False)
        self.show_expr.toggled.connect(self.expr_box.setVisible)
        rl.addWidget(self.expr_box)
        lay.addWidget(res)

        b = QPushButton(_icon("/mIconExpressionSelect.svg"), "Advanced: write your own QGIS expression…")
        b.clicked.connect(self.select_by_expression)
        lay.addWidget(b)
        lay.addStretch(1)
        if self.layer.fields().count():
            self.find_field.setField(self.layer.fields()[0].name())
            self._load_values(self.find_field.currentField())
        self._update_find_summary()
        return w

    # plain-language wording of the query --------------------------------
    OP_WORDS = {"=": "equals", "<>": "is not", ">": "is greater than", ">=": "is at least",
                "<": "is less than", "<=": "is at most", "contains": "contains", "starts": "starts with",
                "ends": "ends with", "in": "is one of", "empty": "is empty", "notempty": "is not empty"}

    def describe_query(self):
        """e.g.  parcel_no equals "04010304-0021" (upper/lower case ignored)"""
        from html import escape
        name = self.find_field.currentField()
        if not name:
            return ""
        field = self.layer.fields().field(name)
        op = self.find_op.currentData()
        raw = self.find_value.currentText().strip()
        words = self.OP_WORDS.get(op, op)
        text = "<b>%s</b> %s" % (escape(name), words)
        if op not in ("empty", "notempty"):
            if op == "in":
                vals = [v.strip() for v in raw.split(",") if v.strip()]
                text += " " + (", ".join("“%s”" % escape(v) for v in vals) if vals else "<i>(no value yet)</i>")
            else:
                text += " " + ("“%s”" % escape(raw) if raw else "<i>(no value yet)</i>")
            if not field.isNumeric() and op not in (">", ">=", "<", "<=") and not self.find_case.isChecked():
                text += " <span style='color:gray'>(upper/lower case ignored)</span>"
        return text

    def _update_find_summary(self):
        if hasattr(self, "find_summary"):
            q = self.describe_query()
            self.find_summary.setText("Looking for features where " + q if q else "Choose a field.")

    def _show_result(self, html, ok=True):
        color = "#2e7d32" if ok else "#c62828"
        self.find_result.setText("<span style='color:%s'>%s</span>" % (color, html))

    def _op_changed(self, *_):
        op = self.find_op.currentData()
        self.find_value.setEnabled(op not in ("empty", "notempty"))
        self.find_value.lineEdit().setPlaceholderText(
            "a, b, c  (comma-separated)" if op == "in" else "type a value, or pick one from the list")
        self._update_find_summary()

    def _load_values(self, field_name):
        idx = self.layer.fields().indexOf(field_name)
        self.find_value.clear()
        if idx < 0:
            return
        self.values_label.setText("Reading the values used in %s…" % field_name)
        if self._values_task is not None:
            try:
                self._values_task.cancel()
            except RuntimeError:
                pass
        task = tasks.UniqueValuesTask(QgsVectorLayerFeatureSource(self.layer), idx,
                                      lambda t, f=field_name: self._values_ready(t, f))
        self._values_task = task
        self._run_task(task)

    def _values_ready(self, task, field_name):
        self._forget_task(task)
        if task is not self._values_task or self._closing:
            return
        self._values_task = None
        if self.find_field.currentField() != field_name:
            return
        text = self.find_value.currentText()
        self.find_value.clear()
        self.find_value.addItems([str(v) for v in task.values])
        self.find_value.setEditText(text)
        n = len(task.values)
        if n == 0:
            msg = "This field has no values."
        elif task.truncated:
            msg = "More than %s different values – the list shows the first %s; you can type any value." % (
                "{:,}".format(n), "{:,}".format(n))
        else:
            msg = "%s different value%s in this field – open the list ▾ or start typing." % (
                "{:,}".format(n), "" if n == 1 else "s")
        self.values_label.setText(msg)

    def build_expression(self):
        name = self.find_field.currentField()
        if not name:
            return None
        field = self.layer.fields().field(name)
        col = QgsExpression.quotedColumnRef(name)
        op = self.find_op.currentData()
        raw = self.find_value.currentText()
        case = self.find_case.isChecked()
        like = "LIKE" if case else "ILIKE"

        def lit(v):
            v = v.strip()
            if field.isNumeric():
                try:
                    float(v)
                    return v
                except ValueError:
                    pass
            return QgsExpression.quotedValue(v)
        if op == "empty":
            return "%s IS NULL OR trim(to_string(%s)) = ''" % (col, col)
        if op == "notempty":
            return "%s IS NOT NULL AND trim(to_string(%s)) <> ''" % (col, col)
        if op == "in":
            items = [x for x in raw.split(",") if x.strip()]
            if not items:
                return None
            if field.isNumeric() or case:
                return "%s IN (%s)" % (col, ", ".join(lit(x) for x in items))
            return "lower(to_string(%s)) IN (%s)" % (col, ", ".join(
                QgsExpression.quotedValue(x.strip().lower()) for x in items))
        if op in ("contains", "starts", "ends"):
            pat = {"contains": "%%%s%%", "starts": "%s%%", "ends": "%%%s"}[op] % raw
            return "to_string(%s) %s %s" % (col, like, QgsExpression.quotedValue(pat))
        if op in ("=", "<>") and not field.isNumeric() and not case:
            return "lower(to_string(%s)) %s %s" % (col, op, QgsExpression.quotedValue(raw.lower()))
        return "%s %s %s" % (col, op, lit(raw))

    def select_by_attribute(self, behavior):
        op = self.find_op.currentData()
        if not self.find_field.currentField():
            self._show_result("Choose a field first.", ok=False)
            return
        if op not in ("empty", "notempty") and not self.find_value.currentText().strip():
            self._show_result("Type or pick a value first.", ok=False)
            return
        expr = self.build_expression()
        if not expr:
            self._show_result("Type or pick a value first.", ok=False)
            return
        self._update_find_summary()
        self.expr_edit.setText(expr)
        self.find_result.setText("Searching…")
        self._select_expression(expr, behavior, zoom=self.find_zoom.isChecked())

    def select_by_expression(self):
        ctx = self._expression_context()
        dlg = QgsExpressionBuilderDialog(self.layer, "", self, "generic", ctx)
        dlg.setWindowTitle("Select by expression (read-only)")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.expressionText().strip():
            if hasattr(self, "find_summary"):
                self.find_summary.setText("Looking for features matching your own expression:")
                self.expr_edit.setText(dlg.expressionText())
                self.show_expr.setChecked(True)
                self.find_result.setText("Searching…")
                self.tabs.setCurrentIndex(1)
            self._select_expression(dlg.expressionText(), compat.select_behavior("SetSelection"), zoom=True)

    def _expression_context(self):
        return QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(self.layer))

    def _select_expression(self, expr, behavior, zoom=False, rect=None, single=False):
        self.sel_label.setText("Selecting…")
        task = tasks.ExpressionSelectTask(QgsVectorLayerFeatureSource(self.layer), expr,
                                          self._expression_context(),
                                          lambda t: self._selection_ready(t, behavior, zoom, single), rect=rect)
        self._run_task(task)

    def _selection_ready(self, task, behavior, zoom, single):
        self._forget_task(task)
        if self._closing:
            return
        if task.error:
            if hasattr(self, "find_result"):
                self._show_result("The query could not run: %s" % task.error, ok=False)
            self._update_selection_label()
            return
        ids = task.ids[:1] if single else task.ids
        self.layer.selectByIds(ids, behavior)
        if zoom and self.layer.selectedFeatureCount():
            self.zoom_selection()
        self._update_selection_label()
        if task.expression and hasattr(self, "find_result"):
            n, total = len(ids), self.layer.selectedFeatureCount()

            def fmt(k):
                return "{:,}".format(k)
            beh = compat.enum_int(behavior)

            def is_(name):
                return beh == compat.enum_int(compat.select_behavior(name))
            if n == 0:
                hint = "untick “Match upper/lower case exactly”" if self.find_case.isChecked() \
                    else "pick the value from the list to avoid typing mistakes"
                self._show_result("✖ No features match. Check the value, or %s." % hint, ok=False)
            elif is_("IntersectSelection"):
                self._show_result("✔ Kept only the selected features that also match. <b>%s</b> selected now." % fmt(total))
            elif is_("RemoveFromSelection"):
                self._show_result("✔ %s matching feature%s unselected. <b>%s</b> selected now." % (
                    fmt(n), "" if n == 1 else "s", fmt(total)))
            elif is_("AddToSelection"):
                self._show_result("✔ %s feature%s found and added. <b>%s</b> selected now." % (
                    fmt(n), "" if n == 1 else "s", fmt(total)))
            else:
                self._show_result("✔ %s feature%s found and selected. See the <b>Selection</b> tab, or "
                                  "<b>Export</b> them." % (fmt(n), "" if n == 1 else "s"))

    # ------------------------------------------------------- selection tab
    def _selection_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.sel_info = QLabel("")
        lay.addWidget(self.sel_info)
        self.sel_table = QTableWidget(0, 0)
        self.sel_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sel_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sel_table.cellDoubleClicked.connect(self._sel_row_activated)
        lay.addWidget(self.sel_table, 1)
        row = QHBoxLayout()
        b = QPushButton(_icon("plugin:export.svg"), "Export selection…")
        b.clicked.connect(self.export_features)
        row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        return w

    def _refresh_selection_views(self):
        self._update_selection_label()
        if not self.is_vector or self._closing:
            return
        ids = self.layer.selectedFeatureIds()
        names = [n for n in self.visible_fields if self.layer.fields().indexOf(n) >= 0]
        self.sel_table.clear()
        self.sel_table.setColumnCount(len(names))
        self.sel_table.setHorizontalHeaderLabels(names)
        if not ids:
            self.sel_table.setRowCount(0)
            self.sel_info.setText("No features selected.")
            return
        shown = sorted(ids)[:SELECTION_TABLE_LIMIT]
        req = QgsFeatureRequest().setFilterFids(shown)
        req.setFlags(compat.request_flag("NoGeometry"))
        req.setSubsetOfAttributes(names, self.layer.fields())
        feats = list(self.layer.getFeatures(req))
        self.sel_table.setRowCount(len(feats))
        for r, f in enumerate(feats):
            for c, n in enumerate(names):
                v = f.attribute(n)
                it = QTableWidgetItem("" if v is None else str(v))
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, f.id())
                self.sel_table.setItem(r, c, it)
        self.sel_info.setText("%s selected%s. Double-click a row to zoom to it." % (
            "{:,}".format(len(ids)),
            " (showing first %s)" % "{:,}".format(SELECTION_TABLE_LIMIT) if len(ids) > SELECTION_TABLE_LIMIT else ""))

    def _sel_row_activated(self, row, _col):
        it = self.sel_table.item(row, 0)
        fid = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if fid is None:
            return
        feat = next(self.layer.getFeatures(QgsFeatureRequest().setFilterFid(fid)), None)
        if feat is not None and feat.hasGeometry():
            self._zoom_to(feat.geometry().boundingBox(), pad=0.5)
            self.canvas.flashFeatureIds(self.layer, [fid])

    # ---------------------------------------------------------- fields tab
    def _fields_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.display_combo = QgsFieldComboBox()
        self.display_combo.setLayer(self.layer)
        self.display_combo.setAllowEmptyFieldName(True)
        guess = self._guess_display_field()
        if guess:
            self.display_combo.setField(guess)
        form.addRow("Label identified features by:", self.display_combo)
        lay.addLayout(form)
        lay.addWidget(QLabel("Fields to show in Identify and Selection (also the default for Export):"))
        self.field_list = QListWidget()
        for f in self.layer.fields():
            it = QListWidgetItem("%s   (%s)" % (f.name(), f.typeName()))
            it.setData(Qt.ItemDataRole.UserRole, f.name())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.field_list.addItem(it)
        self.field_list.itemChanged.connect(self._fields_changed)
        lay.addWidget(self.field_list, 1)
        row = QHBoxLayout()
        for text, state in (("All", Qt.CheckState.Checked), ("None", Qt.CheckState.Unchecked)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, s=state: self._set_all_fields(s))
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        return w

    def _guess_display_field(self):
        names = [f.name() for f in self.layer.fields()]
        for cand in ("name", "name_en", "name_kh", "label", "title", "id"):
            for n in names:
                if n.lower() == cand:
                    return n
        for n in names:
            if "name" in n.lower():
                return n
        return names[0] if names else ""

    def _set_all_fields(self, state):
        self.field_list.blockSignals(True)
        for i in range(self.field_list.count()):
            self.field_list.item(i).setCheckState(state)
        self.field_list.blockSignals(False)
        self._fields_changed()

    def _fields_changed(self, *_):
        self.visible_fields = [self.field_list.item(i).data(Qt.ItemDataRole.UserRole)
                               for i in range(self.field_list.count())
                               if self.field_list.item(i).checkState() == Qt.CheckState.Checked]
        self._sel_timer.start(150)

    # ------------------------------------------------------------- zoom
    def zoom_full(self):
        ext = self.layer.extent()
        if ext.isEmpty() or not ext.isFinite():
            return
        self._zoom_to(ext, pad=0.03)

    def zoom_selection(self):
        if self.is_vector and self.layer.selectedFeatureCount():
            box = self.layer.boundingBoxOfSelected()
            self._zoom_to(box, pad=0.1)

    def _zoom_to(self, rect, pad=0.05):
        rect = QgsRectangle(rect)
        if rect.width() == 0 and rect.height() == 0:
            scale_size = max(self.canvas.extent().width(), self.canvas.extent().height()) * 0.02 or 10
            rect = QgsRectangle(rect.xMinimum() - scale_size, rect.yMinimum() - scale_size,
                                rect.xMaximum() + scale_size, rect.yMaximum() + scale_size)
        else:
            rect.scale(1.0 + pad)
        self.canvas.setExtent(rect)
        self.canvas.refresh()

    # -------------------------------------------------------- selection
    def _select_rect(self, rect, behavior, single):
        # map tool coordinates are in canvas CRS (= layer CRS here, but be safe)
        layer_rect = self.tool_select.toLayerCoordinates(self.layer, rect)
        self._select_expression("", behavior, zoom=False, rect=layer_rect, single=single)

    def select_all(self):
        self.layer.selectAll()

    def invert_selection(self):
        self.layer.invertSelection()

    def clear_selection(self):
        self.layer.removeSelection()

    def _update_selection_label(self):
        if self.is_vector:
            total = self.layer.featureCount()
            self.sel_label.setText("%s of %s selected" % ("{:,}".format(self.layer.selectedFeatureCount()),
                                                           "{:,}".format(total) if total >= 0 else "?"))
            if hasattr(self, "act_export"):
                self.act_export.setToolTip("Export %s to a new file" % (
                    "%s selected features" % "{:,}".format(self.layer.selectedFeatureCount())
                    if self.layer.selectedFeatureCount() else "features"))

    def _show_xy(self, p):
        crs = self.canvas.mapSettings().destinationCrs()
        self.xy_label.setText("%.4f, %.4f %s" % (p.x(), p.y(), crs.authid()))

    def _show_scale(self, scale):
        self.scale_label.setText("1:%s" % "{:,}".format(int(round(scale))))

    # ------------------------------------------------------------ export
    def export_features(self, area=None, label=None, whole=False):
        extent = None
        if area is None:
            try:
                extent = self.tool_pan.toLayerCoordinates(self.layer, self.canvas.extent())
            except Exception:
                extent = self.canvas.extent()
        dlg = ExportDialog(self.layer, self.source_path, self.visible_fields, extent, self,
                           area=area, area_label=label)
        if whole:
            dlg.r_all.setChecked(True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        out = dlg.output_path()
        options = dlg.options()
        add = dlg.add_to_project.isChecked()
        layer_name = options.layerName or os.path.splitext(os.path.basename(out))[0]
        source = self.layer
        exact = dlg.exact_area()
        if exact is not None:
            source = self._features_inside(exact)
            options.filterExtent = QgsRectangle()
            options.onlySelectedFeatures = False
            if source.featureCount() == 0:
                QMessageBox.information(self, "Export", "No features fall inside the %s." % (label or "area"))
                return
        task = QgsVectorFileWriterTask(source, out, options)
        task._kga_source = source  # keep the temporary layer alive while writing
        driver = options.driverName
        task.writeComplete.connect(
            lambda f, t=task: (self._forget_task(t), self._export_done(f or out, layer_name, add, driver)))
        task.errorOccurred.connect(
            lambda _code, msg, t=task: (self._forget_task(t), self.iface.messageBar().pushWarning(
                "Mini Preview", "Export failed: %s" % msg)))
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)
        self.iface.messageBar().pushInfo("Mini Preview", "Exporting to %s…" % os.path.basename(out))

    def _features_inside(self, geom):
        """A memory copy of the features that really intersect `geom` (not just its bounding box)."""
        mem = QgsMemoryProviderUtils.createMemoryLayer(
            "area_export", self.layer.fields(), self.layer.wkbType(), self.layer.crs())
        engine = QgsGeometry.createGeometryEngine(geom.constGet())
        engine.prepareGeometry()
        req = QgsFeatureRequest().setFilterRect(geom.boundingBox())
        keep = [f for f in self.layer.getFeatures(req)
                if f.hasGeometry() and engine.intersects(f.geometry().constGet())]
        mem.dataProvider().addFeatures(keep)
        mem.updateExtents()
        return mem

    def _export_done(self, path, layer_name, add, driver):
        self.last_export = path
        msg = "Exported to %s" % path
        if add and path and os.path.exists(path.split("|")[0]):
            uri = path if driver != "GPKG" or "|" in path else "%s|layername=%s" % (path, layer_name)
            new = QgsVectorLayer(uri, layer_name, "ogr")
            if new.isValid():
                loader.place_layer(new, {"source_path": path, "kind": "vector", "bin_path": ["Exports"]},
                                   {"bin_mode": "custom", "custom_bin": "Exports"})
                msg += " and added to the project (group 'Exports')"
        self.iface.messageBar().pushSuccess("Mini Preview", msg)

    def export_raster_view(self, area=None, label=None):
        src = self.layer.source()
        out = unique_path(os.path.join(exports_folder(), tasks_safe_name(self.layer.name()) + "_clip.tif"))
        from qgis.PyQt.QtWidgets import QFileDialog
        out, _ = QFileDialog.getSaveFileName(
            self, "Export the %s to a new GeoTIFF" % (label or "visible area"), out, "GeoTIFF (*.tif)")
        if not out:
            return
        if not out.lower().endswith((".tif", ".tiff")):
            out += ".tif"
        reason = protected_reason(out, self.source_path)
        if reason:
            QMessageBox.warning(self, "Export", "Not allowed - this tool never modifies source data.\n" + reason)
            return
        if area is None:
            ext = self.tool_pan.toLayerCoordinates(self.layer, self.canvas.extent())
        else:
            ext = area.boundingBox()
        ext = ext.intersect(self.layer.extent())
        if ext.isEmpty():
            QMessageBox.information(self, "Export", "The map view does not overlap the raster.")
            return
        cutline = None
        if area is not None and not area.isGeosEqual(QgsGeometry.fromRect(area.boundingBox())):
            cutline = (area.asWkt(), tasks.crs_for_gdal(self.layer.crs()))
        task = tasks.RasterExportTask(src, out, (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()),
                                      lambda t: self._raster_done(t), cutline=cutline)
        self._run_task(task)

    def _raster_done(self, task):
        self._forget_task(task)
        if task.error:
            self.iface.messageBar().pushWarning("Mini Preview", "Export failed: %s" % task.error)
            return
        new = QgsRasterLayer(task.dst, os.path.splitext(os.path.basename(task.dst))[0], "gdal")
        if new.isValid():
            loader.place_layer(new, {"source_path": task.dst, "kind": "raster", "bin_path": ["Exports"]},
                               {"bin_mode": "custom", "custom_bin": "Exports"})
        self.iface.messageBar().pushSuccess("Mini Preview", "Exported to %s" % task.dst)

    # ------------------------------------------------------------- tasks
    def _run_task(self, task):
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    def _forget_task(self, task):
        if task in self._tasks:
            self._tasks.remove(task)

    # ------------------------------------------------------------- close
    def done(self, result):  # noqa: D401 - Qt override
        self._shutdown()
        super().done(result)

    def closeEvent(self, event):  # noqa: N802
        self._shutdown()
        super().closeEvent(event)

    def _shutdown(self):
        if self._closing:
            return
        self._closing = True
        for t in list(self._tasks):
            try:
                if not isinstance(t, QgsVectorFileWriterTask):  # let exports finish
                    t.cancel()
            except RuntimeError:
                pass
        self._clear_highlights()
        if self.canvas.mapTool() is not None:
            self.canvas.unsetMapTool(self.canvas.mapTool())
        self.canvas.stopRendering()
        self.canvas.setLayers([])
        layer, self.layer = self.layer, None
        try:
            self.tool_identify.layer = None
        except AttributeError:
            pass
        if self._tasks:
            # an export is still writing from this layer: dispose once it is done
            pending = list(self._tasks)
            QTimer.singleShot(0, lambda: self._dispose_when_idle(layer, pending))
        else:
            tasks.dispose(layer)

    def _dispose_when_idle(self, layer, pending):
        alive = []
        for t in pending:
            try:
                if t.status() not in (t.TaskStatus.Complete, t.TaskStatus.Terminated):
                    alive.append(t)
            except RuntimeError:
                continue
        if alive:
            QTimer.singleShot(500, lambda: self._dispose_when_idle(layer, alive))
        else:
            tasks.dispose(layer)

# -*- coding: utf-8 -*-

import os
from contextlib import suppress

from osgeo import ogr, gdal

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtXml import QDomDocument
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                                 QTabWidget, QWidget, QComboBox,
                                 QTableWidget, QTableWidgetItem, QFileDialog,
                                 QLineEdit, QLabel, QMessageBox, QHeaderView,
                                 QAbstractItemView, QCheckBox,
                                 QProgressDialog, QApplication)
from qgis.core import (NULL, QgsFeatureRequest, QgsProcessingAlgorithm,
                       QgsMimeDataUtils, QgsProject,
                       QgsVectorLayer, QgsVectorFileWriter, QgsWkbTypes)
from ..branding import docs_url, help_button
from ..core import output_defaults as od
from ..gui.output_fields import FollowingField

try:
    from qgis.utils import iface
except ImportError:      # running outside QGIS
    iface = None


# ------------------------------------------------------ constants and helpers
QGIS_MIME = "application/x-vnd.qgis.qgis.uri"

DB_EXTENSIONS = ('.gpkg', '.gdb', '.sqlite', '.db', '.spatialite')

# House-keeping tables QGIS writes into a container; they are not user layers,
# so they must never show up in the import list.
SYSTEM_TABLES = ('layer_styles', 'qgis_projects')

# (label, ogr driver, extension, everything goes into one file)
EXPORT_FORMATS = [
    ("GeoPackage (.gpkg)  -  all layers in one file", 'GPKG', '.gpkg', True),
    ("ESRI Shapefile (.shp)  -  one file per layer", 'ESRI Shapefile', '.shp', False),
    ("GeoJSON (.geojson)  -  one file per layer", 'GeoJSON', '.geojson', False),
    ("KML (.kml)  -  one file per layer", 'KML', '.kml', False),
    ("CSV (.csv)  -  attributes, geometry as WKT", 'CSV', '.csv', False),
]

IMPORT_FILTER = ";;".join([
    "All vector files (*.gpkg *.shp *.geojson *.json *.kml *.kmz *.gpx *.tab "
    "*.dxf *.gml *.csv *.sqlite *.db)",
    "GeoPackage (*.gpkg)",
    "ESRI Shapefile (*.shp)",
    "GeoJSON (*.geojson *.json)",
    "KML / KMZ (*.kml *.kmz)",
    "GPX (*.gpx)",
    "MapInfo TAB (*.tab)",
    "AutoCAD DXF (*.dxf)",
    "GML (*.gml)",
    "CSV (*.csv)",
    "SpatiaLite / SQLite (*.sqlite *.db *.spatialite)",
    "All files (*)",
])


def is_database(path):
    """True if the path is a container that holds several layers."""
    return path.lower().endswith(DB_EXTENSIONS)


def split_uri(uri_text):
    """Split an OGR/QGIS uri into (container_path, layer_name_or_None)."""
    layer = None
    path = uri_text
    if '|' in uri_text:
        parts = uri_text.split('|')
        path = parts[0]
        for p in parts[1:]:
            if p.lower().startswith('layername='):
                layer = p.split('=', 1)[1]
    return path, layer


def sources_from_mimedata(md):
    """Return [(path, layer_name_or_None), ...] from a drop event."""
    found = []
    if md.hasFormat(QGIS_MIME):
        with suppress(Exception):
            for u in QgsMimeDataUtils.decodeUriList(md):
                path, layer = split_uri(u.uri)
                if not layer and u.name and is_database(path):
                    layer = u.name
                found.append((path, layer))
    if not found and md.hasUrls():
        for url in md.urls():
            local = url.toLocalFile()
            if local:
                found.append((local, None))
    return found


def open_ds(path):
    """Open a vector data source read-only, returning None instead of raising.

    GDAL's Python bindings raise RuntimeError rather than returning None once
    exceptions are enabled (QGIS turns them on), so every open has to be
    guarded.
    """
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    try:
        try:
            return gdal.OpenEx(path, gdal.OF_VECTOR)
        except Exception:
            return None
    finally:
        gdal.PopErrorHandler()


def list_layers(path):
    """Return [(name, geometry_type, feature_count), ...] for a data source."""
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    try:
        ds = open_ds(path)
        if ds is None:
            return []
        result = []
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            if lyr.GetName().lower() in SYSTEM_TABLES:
                continue
            try:
                geom = ogr.GeometryTypeToName(lyr.GetGeomType())
            except Exception:
                geom = ""
            try:
                count = lyr.GetFeatureCount(1)
            except Exception:
                count = -1
            result.append((lyr.GetName(), geom, count))
        ds = None
        return result
    finally:
        gdal.PopErrorHandler()


def safe_layer_name(name):
    """Make a layer name usable as a file name (shapefile and friends)."""
    cleaned = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)
    cleaned = cleaned.strip("_") or "layer"
    if cleaned[0].isdigit():
        cleaned = "L" + cleaned
    return cleaned


def unique_name(base, taken):
    """Return a name not present in `taken` (a set of lower-case names)."""
    if base.lower() not in taken:
        return base
    i = 1
    while "{0}_{1}".format(base, i).lower() in taken:
        i += 1
    return "{0}_{1}".format(base, i)


def geometry_name(layer):
    """Readable geometry type of a QgsVectorLayer, '-' when unknown."""
    try:
        return QgsWkbTypes.displayString(layer.wkbType()) or "-"
    except Exception:
        return "-"


def format_count(count):
    """Thousands-separated feature count, '-' when it could not be read."""
    return "{:,}".format(count) if count is not None and count >= 0 else "-"


# ---------------------------------------------------------- drop-aware widget
class DropLineEdit(QLineEdit):
    """Line edit that accepts files dragged from the QGIS Browser."""

    pathDropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(QGIS_MIME) or md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        sources = sources_from_mimedata(event.mimeData())
        if not sources:
            event.ignore()
            return
        path, _layer = sources[0]
        # A folder holds no single layer list, so there is nothing to scan.
        if os.path.isdir(path):
            event.ignore()
            return
        event.acceptProposedAction()
        self.pathDropped.emit(path)


# ----------------------------------------------------------------- the dialog
class LayerExportImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Layer Export / Import")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setAcceptDrops(True)
        self.resize(760, 560)
        self.import_path = ""
        self.export_skipped = 0

        self.layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # ---------------- Tab 1: Export ----------------
        self.tab_export = QWidget()
        self.layout_export = QVBoxLayout(self.tab_export)

        self.layout_export.addWidget(QLabel(
            "Layers currently loaded in the QGIS Layers panel:"))

        self.export_table = QTableWidget()
        self.export_table.setColumnCount(4)
        self.export_table.setHorizontalHeaderLabels(
            ["Layer", "Geometry", "Features", "Export As"])
        self.export_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.export_table.setAlternatingRowColors(True)
        self.export_table.verticalHeader().setDefaultSectionSize(22)
        self.export_table.itemChanged.connect(self.update_export_count)
        self.export_table.cellDoubleClicked.connect(self.toggle_export_row)
        exp_header = self.export_table.horizontalHeader()
        exp_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        exp_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        exp_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        exp_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        exp_header.setHighlightSections(False)
        self.layout_export.addWidget(self.export_table)

        btn_layout_exp = QHBoxLayout()
        self.btn_select_all_exp = QPushButton("Select All")
        self.btn_select_all_exp.clicked.connect(self.select_all_export)
        self.btn_clear_all_exp = QPushButton("Clear All")
        self.btn_clear_all_exp.clicked.connect(self.clear_all_export)
        self.btn_refresh_exp = QPushButton("Refresh from Layers Panel")
        self.btn_refresh_exp.clicked.connect(self.load_project_layers)
        btn_layout_exp.addWidget(self.btn_select_all_exp)
        btn_layout_exp.addWidget(self.btn_clear_all_exp)
        btn_layout_exp.addWidget(self.btn_refresh_exp)
        btn_layout_exp.addStretch()
        self.export_count_lbl = QLabel()
        btn_layout_exp.addWidget(self.export_count_lbl)
        self.layout_export.addLayout(btn_layout_exp)

        fmt_layout = QHBoxLayout()
        self.export_format_cb = QComboBox()
        self.export_format_cb.addItems([f[0] for f in EXPORT_FORMATS])
        self.export_format_cb.currentIndexChanged.connect(self.on_format_changed)
        fmt_layout.addWidget(QLabel("Output Format:"))
        fmt_layout.addWidget(self.export_format_cb, 1)
        self.layout_export.addLayout(fmt_layout)

        gpkg_layout = QHBoxLayout()
        self.gpkg_name_lbl = QLabel("GeoPackage File:")
        self.gpkg_name_le = QLineEdit("export_layers.gpkg")
        gpkg_layout.addWidget(self.gpkg_name_lbl)
        gpkg_layout.addWidget(self.gpkg_name_le, 1)
        self.layout_export.addLayout(gpkg_layout)

        out_layout = QHBoxLayout()
        self.export_dir_le = QLineEdit()
        self.export_dir_le.setReadOnly(True)
        self.export_dir_le.setPlaceholderText(
            "Select the folder the exported data is written to...")
        self.btn_browse_out = QPushButton("Browse Folder")
        self.btn_browse_out.clicked.connect(self.browse_export_folder)
        out_layout.addWidget(QLabel("Output Folder:"))
        out_layout.addWidget(self.export_dir_le, 1)
        self._dir_follow = FollowingField(self.export_dir_le)
        self._dir_follow.suggest_folder()       # the project folder
        out_layout.addWidget(self.btn_browse_out)
        self.layout_export.addLayout(out_layout)

        self.chk_selected_only = QCheckBox("Export selected features only")
        self.chk_export_overwrite = QCheckBox(
            "Overwrite layers that already exist in the target "
            "(otherwise a numbered name is used)")
        self.layout_export.addWidget(self.chk_selected_only)
        self.layout_export.addWidget(self.chk_export_overwrite)

        self.btn_run_export = QPushButton("Export Checked Layers")
        self.btn_run_export.clicked.connect(self.run_export)

        self.tabs.addTab(self.tab_export, "Export Layers")

        # ---------------- Tab 2: Import ----------------
        self.tab_import = QWidget()
        self.layout_import = QVBoxLayout(self.tab_import)

        src_layout = QHBoxLayout()
        self.import_file_le = DropLineEdit()
        self.import_file_le.setReadOnly(True)
        self.import_file_le.setPlaceholderText(
            "Select or drag a .gpkg / .shp / .geojson / .kml file "
            "from the Browser panel...")
        self.import_file_le.pathDropped.connect(self.set_import_source)
        self.btn_browse_src = QPushButton("Browse File")
        self.btn_browse_src.clicked.connect(self.browse_import_file)
        src_layout.addWidget(QLabel("Source File:"))
        src_layout.addWidget(self.import_file_le, 1)
        src_layout.addWidget(self.btn_browse_src)
        self.layout_import.addLayout(src_layout)

        self.layout_import.addWidget(QLabel("Layers found in the source file:"))

        self.import_table = QTableWidget()
        self.import_table.setColumnCount(4)
        self.import_table.setHorizontalHeaderLabels(
            ["Layer", "Geometry", "Features", "Add As"])
        self.import_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.import_table.setAlternatingRowColors(True)
        self.import_table.verticalHeader().setDefaultSectionSize(22)
        self.import_table.itemChanged.connect(self.update_import_count)
        self.import_table.cellDoubleClicked.connect(self.toggle_import_row)
        imp_header = self.import_table.horizontalHeader()
        imp_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        imp_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        imp_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        imp_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        imp_header.setHighlightSections(False)
        self.layout_import.addWidget(self.import_table)

        btn_layout_imp = QHBoxLayout()
        self.btn_select_all_imp = QPushButton("Select All")
        self.btn_select_all_imp.clicked.connect(self.select_all_import)
        self.btn_clear_all_imp = QPushButton("Clear All")
        self.btn_clear_all_imp.clicked.connect(self.clear_all_import)
        self.btn_rescan = QPushButton("Rescan File")
        self.btn_rescan.clicked.connect(self.scan_import_source)
        btn_layout_imp.addWidget(self.btn_select_all_imp)
        btn_layout_imp.addWidget(self.btn_clear_all_imp)
        btn_layout_imp.addWidget(self.btn_rescan)
        btn_layout_imp.addStretch()
        self.import_count_lbl = QLabel()
        btn_layout_imp.addWidget(self.import_count_lbl)
        self.layout_import.addLayout(btn_layout_imp)

        self.chk_add_to_map = QCheckBox("Add the imported layers to the project")
        self.chk_add_to_map.setChecked(True)
        self.chk_reproject = QCheckBox(
            "Reproject to the project CRS on import (creates a memory layer)")
        self.layout_import.addWidget(self.chk_add_to_map)
        self.layout_import.addWidget(self.chk_reproject)

        self.btn_run_import = QPushButton("Import Checked Layers")
        self.btn_run_import.clicked.connect(self.run_import)

        self.tabs.addTab(self.tab_import, "Import Layers")

        self._build_temp_tab()
        self.tabs.addTab(self.tab_temp, "Save Temporary Layers")

        self.layout.addWidget(self.tabs)

        close_layout = QHBoxLayout()
        close_layout.addWidget(help_button('layerexportimport', self))
        close_layout.addStretch()
        for button in (self.btn_run_export, self.btn_run_import,
                       self.btn_run_temp):
            close_layout.addWidget(button)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._on_tab_changed(self.tabs.currentIndex())
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        close_layout.addWidget(self.btn_close)
        self.layout.addLayout(close_layout)

        self.on_format_changed(self.export_format_cb.currentIndex())
        self.load_project_layers()
        self.update_import_count()
        self._start_temp_watch()

    # ---------------- Drops anywhere on the window ----------------
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(QGIS_MIME) or md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        sources = sources_from_mimedata(event.mimeData())
        if not sources:
            event.ignore()
            return
        path, _layer = sources[0]
        if os.path.isdir(path):
            event.ignore()
            return
        event.acceptProposedAction()
        # A file dropped on the window can only be meant for the import side.
        self.tabs.setCurrentWidget(self.tab_import)
        self.set_import_source(path)

    # ---------------- Tab 1: export from the Layers panel ----------------
    def project_vector_layers(self):
        """Vector layers in Layers-panel order, plus the count of skipped ones."""
        layers, skipped = [], 0
        try:
            nodes = QgsProject.instance().layerTreeRoot().findLayers()
        except Exception:
            return layers, skipped
        for node in nodes:
            lyr = node.layer()
            if lyr is None:
                continue
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                layers.append(lyr)
            else:
                skipped += 1
        return layers, skipped

    def load_project_layers(self):
        """Refresh from the Layers panel, keeping ticks and edited names."""
        previous = {}
        for row in range(self.export_table.rowCount()):
            item = self.export_table.item(row, 0)
            if item:
                previous[item.data(Qt.ItemDataRole.UserRole)] = (
                    item.checkState() == Qt.CheckState.Checked,
                    self.export_table.item(row, 3).text())

        layers, skipped = self.project_vector_layers()

        self.export_table.blockSignals(True)
        self.export_table.setRowCount(len(layers))
        for row, lyr in enumerate(layers):
            was_checked, old_name = previous.get(lyr.id(), (False, None))

            name_item = QTableWidgetItem(lyr.name())
            name_item.setData(Qt.ItemDataRole.UserRole, lyr.id())
            name_item.setFlags((name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                               & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setCheckState(Qt.CheckState.Checked if was_checked else Qt.CheckState.Unchecked)

            geom_item = QTableWidgetItem(geometry_name(lyr))
            geom_item.setFlags(geom_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            try:
                count = lyr.featureCount()
            except Exception:
                count = -1
            count_item = QTableWidgetItem(format_count(count))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            out_item = QTableWidgetItem(old_name or safe_layer_name(lyr.name()))
            out_item.setToolTip("Double-click to change the name used in the target.")

            self.export_table.setItem(row, 0, name_item)
            self.export_table.setItem(row, 1, geom_item)
            self.export_table.setItem(row, 2, count_item)
            self.export_table.setItem(row, 3, out_item)
        self.export_table.blockSignals(False)

        self.export_skipped = skipped
        self.update_export_count()

    def toggle_export_row(self, row, column):
        if column == 3:          # let the name column be edited normally
            return
        item = self.export_table.item(row, 0)
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)

    def checked_export_rows(self):
        rows = []
        for row in range(self.export_table.rowCount()):
            item = self.export_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def update_export_count(self, *_args):
        total = self.export_table.rowCount()
        picked = len(self.checked_export_rows())
        text = "{0} of {1} layers ticked".format(picked, total)
        if getattr(self, 'export_skipped', 0):
            text += "  ({0} non-vector skipped)".format(self.export_skipped)
        self.export_count_lbl.setText(text)
        self.btn_run_export.setText(
            "Export Checked Layers ({0})".format(picked) if picked
            else "Export Checked Layers")
        self.btn_run_export.setEnabled(picked > 0)

    def select_all_export(self):
        self.export_table.blockSignals(True)
        for row in range(self.export_table.rowCount()):
            self.export_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.export_table.blockSignals(False)
        self.update_export_count()

    def clear_all_export(self):
        self.export_table.blockSignals(True)
        for row in range(self.export_table.rowCount()):
            self.export_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.export_table.blockSignals(False)
        self.update_export_count()

    def showEvent(self, event):
        super().showEvent(event)
        self._dir_follow.retarget_folder()      # the project may have changed
        self._temp_db_follow.suggest_path(od.project_database())
        self._temp_dir_follow.retarget_folder()
        self.load_temp_layers()

    def on_format_changed(self, index):
        """The GeoPackage file name only means anything for the GPKG format."""
        single_file = EXPORT_FORMATS[index][3]
        self.gpkg_name_lbl.setVisible(single_file)
        self.gpkg_name_le.setVisible(single_file)

    def browse_export_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder",
            self.export_dir_le.text().strip() or od.project_folder())
        if folder:
            self.export_dir_le.setText(folder)

    def run_export(self):
        out_dir = self.export_dir_le.text().strip()
        if not out_dir or not os.path.isdir(out_dir):
            QMessageBox.warning(self, "Error",
                                "Please select a valid Output Folder first.")
            return

        rows = self.checked_export_rows()
        if not rows:
            QMessageBox.warning(self, "Error", "No layers are ticked.")
            return

        _label, driver, extension, single_file = EXPORT_FORMATS[
            self.export_format_cb.currentIndex()]
        overwrite = self.chk_export_overwrite.isChecked()
        only_selected = self.chk_selected_only.isChecked()

        # Where the data lands, and what sits there already
        container = None
        if single_file:
            gpkg_name = self.gpkg_name_le.text().strip() or "export_layers.gpkg"
            if not gpkg_name.lower().endswith(extension):
                gpkg_name += extension
            container = os.path.join(out_dir, gpkg_name)
            # An existing GeoPackage is added to, never replaced: only the
            # layers whose names clash are touched, and only when overwriting.
            existing = ({n.lower() for n, _g, _c in list_layers(container)}
                        if os.path.exists(container) else set())
        else:
            existing = {os.path.splitext(f)[0].lower()
                        for f in os.listdir(out_dir)
                        if f.lower().endswith(extension)}

        project = QgsProject.instance()
        layer_map = {lyr.id(): lyr for lyr in self.project_vector_layers()[0]}

        progress = QProgressDialog("Exporting layers...", "Cancel", 0, len(rows), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        success, failed, done = 0, 0, 0
        errors = []

        for row in rows:
            if progress.wasCanceled():
                break

            layer_id = self.export_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            layer = layer_map.get(layer_id) or project.mapLayer(layer_id)
            source_name = self.export_table.item(row, 0).text()

            out_name = self.export_table.item(row, 3).text().strip() or source_name
            if not single_file:
                out_name = safe_layer_name(out_name)

            progress.setLabelText("Exporting {0}...".format(out_name))
            QApplication.processEvents()

            if layer is None:
                failed += 1
                errors.append("{0}: no longer in the project (press Refresh)".format(
                    source_name))
                done += 1
                progress.setValue(done)
                continue

            if only_selected and layer.selectedFeatureCount() == 0:
                failed += 1
                errors.append("{0}: no features selected".format(source_name))
                done += 1
                progress.setValue(done)
                continue

            final_name = out_name
            if final_name.lower() in existing and not overwrite:
                final_name = unique_name(final_name, existing)

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = driver
            options.fileEncoding = 'UTF-8'
            options.onlySelectedFeatures = only_selected
            # Plain CSV drops the geometry; WKT keeps it in a readable column.
            if driver == 'CSV' and layer.geometryType() != QgsWkbTypes.GeometryType.NullGeometry:
                options.layerOptions = ['GEOMETRY=AS_WKT']

            if single_file:
                dest = container
                options.layerName = final_name
                try:
                    options.actionOnExistingFile = (
                        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
                        if not os.path.exists(dest)
                        else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
                except AttributeError:
                    pass
            else:
                dest = os.path.join(out_dir, final_name + extension)
                try:
                    options.actionOnExistingFile = \
                        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
                except AttributeError:
                    pass

            ok, message = self._write_layer(layer, dest, options)
            if ok:
                success += 1
                existing.add(final_name.lower())
            else:
                failed += 1
                errors.append("{0}: {1}".format(source_name, message or "write failed"))

            done += 1
            progress.setValue(done)

        progress.setValue(len(rows))

        summary = "Exported {0} layer(s).\nFailed: {1}".format(success, failed)
        if success:
            summary += "\n\n{0}".format(container if single_file else out_dir)
        if success and driver == 'ESRI Shapefile':
            summary += ("\n\nNote: the Shapefile format truncates field names to 10 "
                        "characters and cannot store several geometry types in one "
                        "file.")
        box = QMessageBox(self)
        box.setWindowTitle("Export Complete")
        box.setIcon(QMessageBox.Icon.Information if failed == 0 else QMessageBox.Icon.Warning)
        box.setText(summary)
        if errors:
            box.setDetailedText("\n".join(errors[:50]))
        box.exec()

    @staticmethod
    def _skip_bad_fid(layer, options):
        """Don't write a copied 'fid' field whose values would collide.

        A GeoPackage uses a field called fid as the row id. A layer copied
        out of another GeoPackage (e.g. selected features pasted as a
        temporary layer) can carry the same fid on every row, and then only
        the first row is written. Dropping the field lets the target number
        the rows itself; a fid column that is unique is kept as it is.
        """
        fields = layer.fields()
        index = fields.lookupField('fid')
        if index < 0:
            return
        seen = set()
        request = QgsFeatureRequest().setSubsetOfAttributes([index])
        if options.onlySelectedFeatures:
            request.setFilterFids(layer.selectedFeatureIds())
        for feature in layer.getFeatures(request):
            value = feature.attribute(index)
            if value is None or value == NULL or value in seen:
                break
            seen.add(value)
        else:
            return          # all unique: keep it
        options.attributes = [i for i in range(fields.count()) if i != index]

    def _write_layer(self, layer, dest_path, options):
        """Run QgsVectorFileWriter across API versions. Returns (ok, message)."""
        ctx = QgsProject.instance().transformContext()
        if options.driverName == 'GPKG':
            self._skip_bad_fid(layer, options)
        try:
            res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, dest_path, ctx, options)
        except AttributeError:
            try:
                res = QgsVectorFileWriter.writeAsVectorFormatV2(layer, dest_path, ctx, options)
            except Exception as e:
                return False, str(e)
        except Exception as e:
            return False, str(e)

        if isinstance(res, (tuple, list)):
            err = res[0]
            msg = res[1] if len(res) > 1 else ""
        else:
            err, msg = res, ""
        return err == QgsVectorFileWriter.WriterError.NoError, msg

    # ---------------- Tab 3: save temporary (memory) layers ----------------
    def _build_temp_tab(self):
        self.tab_temp = QWidget()
        lay = QVBoxLayout(self.tab_temp)
        lay.addWidget(QLabel(
            "Temporary (memory) layers in the Layers panel. The list updates "
            "by itself as layers come and go:"))

        self.temp_table = QTableWidget()
        self.temp_table.setColumnCount(4)
        self.temp_table.setHorizontalHeaderLabels(
            ["Layer", "Geometry", "Features", "Save As"])
        self.temp_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.temp_table.setAlternatingRowColors(True)
        self.temp_table.verticalHeader().setDefaultSectionSize(22)
        self.temp_table.itemChanged.connect(self.update_temp_count)
        self.temp_table.cellDoubleClicked.connect(self.toggle_temp_row)
        header = self.temp_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)
        lay.addWidget(self.temp_table)

        buttons = QHBoxLayout()
        btn_all = QPushButton("Select All")
        btn_all.clicked.connect(lambda: self._set_all_temp(True))
        btn_none = QPushButton("Clear All")
        btn_none.clicked.connect(lambda: self._set_all_temp(False))
        buttons.addWidget(btn_all)
        buttons.addWidget(btn_none)
        buttons.addStretch()
        self.temp_count_lbl = QLabel()
        buttons.addWidget(self.temp_count_lbl)
        lay.addLayout(buttons)

        target = QHBoxLayout()
        target.addWidget(QLabel("Save To:"))
        self.temp_target_cb = QComboBox()
        self.temp_target_cb.addItem("Project database (GeoPackage)", 'db')
        self.temp_target_cb.addItem("Folder (one file per layer)", 'folder')
        self.temp_target_cb.currentIndexChanged.connect(self.on_temp_target_changed)
        target.addWidget(self.temp_target_cb, 1)
        lay.addLayout(target)

        db_row = QHBoxLayout()
        self.temp_db_lbl = QLabel("GeoPackage:")
        self.temp_db_le = QLineEdit()
        self.temp_db_le.setPlaceholderText(
            "A GeoPackage; it is created if it does not exist...")
        self._temp_db_follow = FollowingField(self.temp_db_le)
        self._temp_db_follow.suggest_path(od.project_database())
        self.temp_db_btn = QPushButton("Browse")
        self.temp_db_btn.clicked.connect(self.browse_temp_db)
        db_row.addWidget(self.temp_db_lbl)
        db_row.addWidget(self.temp_db_le, 1)
        db_row.addWidget(self.temp_db_btn)
        lay.addLayout(db_row)

        dir_row = QHBoxLayout()
        self.temp_fmt_lbl = QLabel("Format:")
        self.temp_fmt_cb = QComboBox()
        for label, driver, extension, single in EXPORT_FORMATS:
            if not single and driver != 'CSV' and driver != 'KML':
                self.temp_fmt_cb.addItem(label.split("  -")[0],
                                         (driver, extension))
        self.temp_fmt_cb.insertItem(0, "GeoPackage (.gpkg)", ('GPKG', '.gpkg'))
        self.temp_fmt_cb.setCurrentIndex(0)
        self.temp_dir_lbl = QLabel("Folder:")
        self.temp_dir_le = QLineEdit()
        self.temp_dir_le.setPlaceholderText("Select the output folder...")
        self._temp_dir_follow = FollowingField(self.temp_dir_le)
        self._temp_dir_follow.suggest_folder()
        self.temp_dir_btn = QPushButton("Browse")
        self.temp_dir_btn.clicked.connect(self.browse_temp_dir)
        dir_row.addWidget(self.temp_fmt_lbl)
        dir_row.addWidget(self.temp_fmt_cb)
        dir_row.addWidget(self.temp_dir_lbl)
        dir_row.addWidget(self.temp_dir_le, 1)
        dir_row.addWidget(self.temp_dir_btn)
        lay.addLayout(dir_row)

        self.chk_temp_replace = QCheckBox(
            "Replace the temporary layer in the project with the saved one "
            "(keeps its style and position)")
        self.chk_temp_replace.setChecked(True)
        lay.addWidget(self.chk_temp_replace)

        # Lives in the window's bottom row, next to Close (see __init__).
        self.btn_run_temp = QPushButton("Save Checked Layers")
        self.btn_run_temp.clicked.connect(self.run_save_temp)

        self.on_temp_target_changed()

    def _on_tab_changed(self, _index):
        """Each tab's run button sits in the bottom row, shown for its tab only."""
        current = self.tabs.currentWidget()
        self.btn_run_export.setVisible(current is self.tab_export)
        self.btn_run_import.setVisible(current is self.tab_import)
        self.btn_run_temp.setVisible(current is self.tab_temp)

    def on_temp_target_changed(self, *_args):
        to_db = self.temp_target_cb.currentData() == 'db'
        for w in (self.temp_db_lbl, self.temp_db_le, self.temp_db_btn):
            w.setVisible(to_db)
        for w in (self.temp_fmt_lbl, self.temp_fmt_cb, self.temp_dir_lbl,
                  self.temp_dir_le, self.temp_dir_btn):
            w.setVisible(not to_db)

    def browse_temp_db(self):
        path, _f = QFileDialog.getSaveFileName(
            self, "GeoPackage to Save Into",
            self.temp_db_le.text().strip() or od.project_folder(),
            "GeoPackage (*.gpkg)", options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            if not path.lower().endswith('.gpkg'):
                path += '.gpkg'
            self.temp_db_le.setText(path)

    def browse_temp_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder",
            self.temp_dir_le.text().strip() or od.project_folder())
        if folder:
            self.temp_dir_le.setText(folder)

    # -- detection and auto-refresh
    @staticmethod
    def is_temporary(layer):
        """True for a memory-provider vector layer (a scratch layer)."""
        try:
            return (isinstance(layer, QgsVectorLayer) and layer.isValid()
                    and layer.providerType() == 'memory')
        except Exception:
            return False

    def _start_temp_watch(self):
        self._temp_watched = set()
        self._temp_timer = QTimer(self)
        self._temp_timer.setSingleShot(True)
        self._temp_timer.setInterval(250)
        self._temp_timer.timeout.connect(self.load_temp_layers)
        project = QgsProject.instance()
        project.layersAdded.connect(self._temp_layers_added)
        project.layersRemoved.connect(self._schedule_temp_refresh)
        project.cleared.connect(self._schedule_temp_refresh)
        self._temp_layers_added(list(project.mapLayers().values()))

    def _schedule_temp_refresh(self, *_args):
        self._temp_timer.start()

    def _temp_layers_added(self, layers):
        for lyr in layers:
            if lyr.id() in self._temp_watched or not isinstance(lyr, QgsVectorLayer):
                continue
            self._temp_watched.add(lyr.id())
            # The provider of a layer can still be loading; names and counts
            # change as a tool fills the layer, so listen to those too.
            for name in ('nameChanged', 'featureAdded', 'featureDeleted',
                         'afterCommitChanges', 'dataSourceChanged'):
                with suppress(Exception):
                    getattr(lyr, name).connect(self._schedule_temp_refresh)
        self._temp_timer.start()

    def temp_layers(self):
        layers = []
        with suppress(Exception):
            for node in QgsProject.instance().layerTreeRoot().findLayers():
                lyr = node.layer()
                if self.is_temporary(lyr):
                    layers.append(lyr)
        return layers

    def load_temp_layers(self):
        """Re-read the Layers panel; new scratch layers arrive ticked."""
        previous = {}
        for row in range(self.temp_table.rowCount()):
            item = self.temp_table.item(row, 0)
            if item:
                previous[item.data(Qt.ItemDataRole.UserRole)] = (
                    item.checkState() == Qt.CheckState.Checked,
                    self.temp_table.item(row, 3).text(), item.text())

        layers = self.temp_layers()
        self.temp_table.blockSignals(True)
        self.temp_table.setRowCount(len(layers))
        for row, lyr in enumerate(layers):
            was_checked, old_name, old_label = previous.get(
                lyr.id(), (True, None, None))
            # A rename in the Layers panel wins over an untouched "Save As".
            if old_name is not None and old_label != lyr.name() \
                    and old_name == safe_layer_name(old_label):
                old_name = None

            name_item = QTableWidgetItem(lyr.name())
            name_item.setData(Qt.ItemDataRole.UserRole, lyr.id())
            name_item.setFlags((name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                               & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setCheckState(Qt.CheckState.Checked if was_checked
                                    else Qt.CheckState.Unchecked)
            geom_item = QTableWidgetItem(geometry_name(lyr))
            geom_item.setFlags(geom_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            try:
                count = lyr.featureCount()
            except Exception:
                count = -1
            count_item = QTableWidgetItem(format_count(count))
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            out_item = QTableWidgetItem(old_name or od.safe_stem(lyr.name(), 'layer'))
            out_item.setToolTip("Double-click to change the saved name.")

            self.temp_table.setItem(row, 0, name_item)
            self.temp_table.setItem(row, 1, geom_item)
            self.temp_table.setItem(row, 2, count_item)
            self.temp_table.setItem(row, 3, out_item)
        self.temp_table.blockSignals(False)
        self.update_temp_count()

    def toggle_temp_row(self, row, column):
        if column == 3:
            return
        item = self.temp_table.item(row, 0)
        item.setCheckState(Qt.CheckState.Unchecked
                           if item.checkState() == Qt.CheckState.Checked
                           else Qt.CheckState.Checked)

    def _set_all_temp(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.temp_table.blockSignals(True)
        for row in range(self.temp_table.rowCount()):
            self.temp_table.item(row, 0).setCheckState(state)
        self.temp_table.blockSignals(False)
        self.update_temp_count()

    def checked_temp_rows(self):
        return [row for row in range(self.temp_table.rowCount())
                if self.temp_table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def update_temp_count(self, *_args):
        total = self.temp_table.rowCount()
        picked = len(self.checked_temp_rows())
        self.temp_count_lbl.setText(
            "{0} of {1} temporary layers ticked".format(picked, total)
            if total else "No temporary layers in the project")
        self.btn_run_temp.setText(
            "Save Checked Layers ({0})".format(picked) if picked
            else "Save Checked Layers")
        self.btn_run_temp.setEnabled(picked > 0)

    # -- saving
    def run_save_temp(self):
        to_db = self.temp_target_cb.currentData() == 'db'
        if to_db:
            database = self.temp_db_le.text().strip()
            if not database:
                QMessageBox.warning(
                    self, "Error", "Please choose the GeoPackage to save into "
                    "(open a project to get a default).")
                return
            if not database.lower().endswith('.gpkg'):
                database += '.gpkg'
            folder = os.path.dirname(database)
            driver, extension = 'GPKG', '.gpkg'
            if not os.path.isdir(folder):
                QMessageBox.warning(self, "Error",
                                    "The folder does not exist:\n" + folder)
                return
        else:
            folder = self.temp_dir_le.text().strip()
            if not folder or not os.path.isdir(folder):
                QMessageBox.warning(self, "Error",
                                    "Please select a valid output folder first.")
                return
            driver, extension = self.temp_fmt_cb.currentData()
            database = ''

        rows = self.checked_temp_rows()
        project = QgsProject.instance()
        success, failed, errors, saved = 0, 0, [], []
        taken = set()
        for row in rows:
            layer_id = self.temp_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            source_name = self.temp_table.item(row, 0).text()
            layer = project.mapLayer(layer_id)
            if layer is None or not self.is_temporary(layer):
                failed += 1
                errors.append("{0}: no longer a temporary layer in the project"
                              .format(source_name))
                continue
            stem = od.safe_stem(self.temp_table.item(row, 3).text(), 'layer')

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = driver
            options.fileEncoding = 'UTF-8'
            if to_db:
                name = od.unique_table(database, stem, taken)
                taken.add(name.lower())
                dest = database
                options.layerName = name
                options.actionOnExistingFile = (
                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                    if os.path.exists(database) else
                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
                uri = "{0}|layername={1}".format(database, name)
            else:
                dest = od.unique_file(folder, stem, extension, taken)
                taken.add(os.path.normcase(os.path.abspath(dest)))
                name = os.path.splitext(os.path.basename(dest))[0]
                options.actionOnExistingFile = \
                    QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
                uri = dest

            ok, message = self._write_layer(layer, dest, options)
            if not ok:
                failed += 1
                errors.append("{0}: {1}".format(source_name, message or "write failed"))
                continue
            success += 1
            saved.append((layer, uri, name))

        if self.chk_temp_replace.isChecked():
            for layer, uri, name in saved:
                err = self._replace_with_saved(layer, uri, name)
                if err:
                    errors.append(err)

        summary = "Saved {0} layer(s).\nFailed: {1}".format(success, failed)
        if success:
            summary += "\n\n{0}".format(database if to_db else folder)
        box = QMessageBox(self)
        box.setWindowTitle("Save Complete")
        box.setIcon(QMessageBox.Icon.Information if not errors
                    else QMessageBox.Icon.Warning)
        box.setText(summary)
        if errors:
            box.setDetailedText("\n".join(errors[:50]))
        box.exec()
        self.load_temp_layers()

    def _replace_with_saved(self, temp, uri, name):
        """Swap `temp` in the layer tree for the saved copy. Returns an error text."""
        project = QgsProject.instance()
        label = temp.name()
        new = QgsVectorLayer(uri, label, 'ogr')
        if not new.isValid():
            return "{0}: saved, but could not be reloaded from {1}".format(label, uri)
        with suppress(Exception):
            doc = QDomDocument()
            temp.exportNamedStyle(doc)
            new.importNamedStyle(doc)
        root = project.layerTreeRoot()
        node = root.findLayer(temp.id())
        project.addMapLayer(new, False)
        if node is not None and node.parent() is not None:
            parent = node.parent()
            index = parent.children().index(node)
            visible = node.itemVisibilityChecked()
            added = parent.insertLayer(index, new)
            if added is not None:
                added.setItemVisibilityChecked(visible)
        else:
            root.addLayer(new)
        project.removeMapLayer(temp.id())
        return ""

    # ---------------- Tab 2: import into the project ----------------
    def browse_import_file(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Select File to Import", "", IMPORT_FILTER)
        if path:
            self.set_import_source(path)

    def set_import_source(self, path):
        self.import_path = path
        self.import_file_le.setText(path)
        self.scan_import_source()

    def scan_import_source(self):
        """List what the source file holds, ticking everything it found."""
        path = self.import_path

        self.import_table.blockSignals(True)
        self.import_table.setRowCount(0)
        self.import_table.blockSignals(False)

        if not path:
            self.update_import_count()
            return

        if not os.path.exists(path):
            QMessageBox.warning(self, "Error",
                                "The source file no longer exists:\n{0}".format(path))
            self.update_import_count()
            return

        layers = list_layers(path)
        if not layers:
            # Some drivers report no layer list at all; offer the file itself as
            # a single layer rather than claiming it is empty.
            layers = [(os.path.splitext(os.path.basename(path))[0], "", -1)]

        self.import_table.blockSignals(True)
        self.import_table.setRowCount(len(layers))
        for row, (name, geom, count) in enumerate(layers):
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, name)
            name_item.setFlags((name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                               & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setCheckState(Qt.CheckState.Checked)

            geom_item = QTableWidgetItem(geom or "-")
            geom_item.setFlags(geom_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            count_item = QTableWidgetItem(format_count(count))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            as_item = QTableWidgetItem(name)
            as_item.setToolTip("Double-click to change the name used in the project.")

            self.import_table.setItem(row, 0, name_item)
            self.import_table.setItem(row, 1, geom_item)
            self.import_table.setItem(row, 2, count_item)
            self.import_table.setItem(row, 3, as_item)
        self.import_table.blockSignals(False)

        self.update_import_count()

    def toggle_import_row(self, row, column):
        if column == 3:          # let the name column be edited normally
            return
        item = self.import_table.item(row, 0)
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)

    def checked_import_rows(self):
        rows = []
        for row in range(self.import_table.rowCount()):
            item = self.import_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def update_import_count(self, *_args):
        total = self.import_table.rowCount()
        picked = len(self.checked_import_rows())
        self.import_count_lbl.setText("{0} of {1} layers ticked".format(picked, total))
        self.btn_run_import.setText(
            "Import Checked Layers ({0})".format(picked) if picked
            else "Import Checked Layers")
        self.btn_run_import.setEnabled(picked > 0)

    def select_all_import(self):
        self.import_table.blockSignals(True)
        for row in range(self.import_table.rowCount()):
            self.import_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.import_table.blockSignals(False)
        self.update_import_count()

    def clear_all_import(self):
        self.import_table.blockSignals(True)
        for row in range(self.import_table.rowCount()):
            self.import_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.import_table.blockSignals(False)
        self.update_import_count()

    def source_uri(self, layer_name, multi_layer):
        """URI of one layer of the source file.

        A single-layer file is opened by path alone - a plain .shp or .csv does
        not always answer to |layername=, while a container always does.
        """
        if multi_layer or is_database(self.import_path):
            return "{0}|layername={1}".format(self.import_path, layer_name)
        return self.import_path

    def run_import(self):
        path = self.import_path
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Error", "Please select a Source File first.")
            return

        rows = self.checked_import_rows()
        if not rows:
            QMessageBox.warning(self, "Error", "No layers are ticked.")
            return

        multi_layer = self.import_table.rowCount() > 1
        add_to_map = self.chk_add_to_map.isChecked()
        reproject = self.chk_reproject.isChecked()
        project = QgsProject.instance()
        target_crs = project.crs()

        progress = QProgressDialog("Importing layers...", "Cancel", 0, len(rows), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        success, failed, done = 0, 0, 0
        reprojected = 0
        errors = []

        for row in rows:
            if progress.wasCanceled():
                break

            src_name = self.import_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            out_name = self.import_table.item(row, 3).text().strip() or src_name

            progress.setLabelText("Importing {0}...".format(out_name))
            QApplication.processEvents()

            layer = QgsVectorLayer(self.source_uri(src_name, multi_layer),
                                   out_name, 'ogr')
            if not layer.isValid():
                failed += 1
                errors.append("{0}: cannot be opened as a vector layer".format(src_name))
                done += 1
                progress.setValue(done)
                continue

            if reproject and target_crs.isValid() and layer.crs().isValid() \
                    and layer.crs() != target_crs:
                try:
                    import processing
                    result = processing.run("native:reprojectlayer", {
                        'INPUT': layer,
                        'TARGET_CRS': target_crs,
                        'OUTPUT': 'memory:',
                    })
                    layer = result['OUTPUT']
                    layer.setName(out_name)
                    reprojected += 1
                except Exception as e:
                    # The layer is still perfectly usable in its own CRS.
                    errors.append("{0}: not reprojected - {1}".format(src_name, e))

            if add_to_map:
                project.addMapLayer(layer)

            success += 1
            done += 1
            progress.setValue(done)

        progress.setValue(len(rows))

        # Layers just added belong in the export list as well.
        if add_to_map and success:
            self.load_project_layers()

        summary = "Imported {0} layer(s).\nFailed: {1}".format(success, failed)
        if reprojected:
            summary += "\nReprojected to the project CRS: {0}".format(reprojected)
        if success and not add_to_map:
            summary += ("\n\nThe layers were read but not kept, because 'Add the "
                        "imported layers to the project' is off.")
        box = QMessageBox(self)
        box.setWindowTitle("Import Complete")
        box.setIcon(QMessageBox.Icon.Information if failed == 0 else QMessageBox.Icon.Warning)
        box.setText(summary)
        if errors:
            box.setDetailedText("\n".join(errors[:50]))
        box.exec()


# ---------------------------- launcher: the entry the toolbox and toolbar see
# Module-level reference keeps the modeless dialog alive after
# processAlgorithm() returns (otherwise Python garbage-collects it).
DIALOG_INSTANCE = None


class LayerExportImportAlgorithm(QgsProcessingAlgorithm):
    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return LayerExportImportAlgorithm()

    def name(self):
        return 'layerexportimport'

    def displayName(self):
        return self.tr('Layer Export / Import')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('layerexportimport')

    def shortHelpString(self):
        return self.tr(
            "Bulk export the vector layers of the current project, and import "
            "layers from any vector file back into it.\n\n"
            "Tab 1 writes the ticked layers to a GeoPackage - all of them into one "
            "file - or to Shapefile, GeoJSON, KML or CSV, one file per layer. Each "
            "layer can be renamed on the way out, and the export can be limited to "
            "the features currently selected.\n\n"
            "Tab 2 lists the layers held by a source file and adds the ticked ones "
            "to the project, optionally reprojected to the project CRS.\n\n"
            "The window opens modelessly, so you can drag a file from the Browser "
            "panel onto it and keep working in QGIS."
        )

    def flags(self):
        base = super().flags()
        try:
            from qgis.core import Qgis
            return base | Qgis.ProcessingAlgorithmFlag.NoThreading
        except (ImportError, AttributeError):
            return base | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        parent = iface.mainWindow() if iface else None

        # Closing a QDialog only hides it, so a cached instance would come back
        # with the previous ticks, output folder and scanned file still in it.
        # Re-running while the window is still open just brings it forward;
        # re-running after it was closed throws the old one away and starts clean.
        if DIALOG_INSTANCE is not None:
            try:
                still_open = DIALOG_INSTANCE.isVisible()
            except RuntimeError:        # Qt already destroyed the widget
                still_open = False
            if not still_open:
                try:
                    DIALOG_INSTANCE.deleteLater()
                except RuntimeError:
                    pass
                DIALOG_INSTANCE = None

        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = LayerExportImportDialog(parent)
        else:
            # Still open: the Layers panel may have moved on since it was built.
            DIALOG_INSTANCE.load_project_layers()

        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo("Layer Export / Import opened. "
                          "You can close this Processing dialog and keep working.")
        return {}

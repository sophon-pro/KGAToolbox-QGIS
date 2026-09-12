# -*- coding: utf-8 -*-

import os
import sqlite3
from contextlib import suppress

from osgeo import ogr, gdal

from qgis.PyQt.QtCore import (QCoreApplication, Qt, pyqtSignal,
                              QItemSelection, QItemSelectionModel)
from qgis.PyQt.QtXml import QDomDocument
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                                 QTabWidget, QWidget,
                                 QTableWidget, QTableWidgetItem, QFileDialog,
                                 QLineEdit, QLabel, QMessageBox, QHeaderView,
                                 QAbstractItemView, QCheckBox, QToolButton, QMenu,
                                 QSizePolicy,
                                 QDialogButtonBox, QProgressDialog, QApplication)
from qgis.core import (QgsProcessingAlgorithm, QgsMimeDataUtils, QgsProject,
                       QgsVectorLayer, QgsVectorFileWriter, QgsWkbTypes,
                       QgsMapLayer)
from ..branding import docs_url, help_button

try:
    from qgis.utils import iface
except ImportError:      # running outside QGIS
    iface = None


# ------------------------------------------------------ constants and helpers
QGIS_MIME = "application/x-vnd.qgis.qgis.uri"

DB_EXTENSIONS = ('.gpkg', '.gdb', '.sqlite', '.db', '.spatialite')

# House-keeping tables QGIS writes into a container; they are not user layers,
# so they must never show up in the delete / rename lists.
SYSTEM_TABLES = ('layer_styles', 'qgis_projects')

# The side tables that carry a copy of the layer name, and have to be moved
# with it on a rename. Written out in full rather than built from the table
# names so that no statement here is assembled at run time; both are optional,
# so a missing one is caught per statement.
RENAME_SIDE_TABLE_SQL = (
    "UPDATE gpkg_geometry_columns SET table_name = ? WHERE table_name = ?",
    "UPDATE gpkg_extensions SET table_name = ? WHERE table_name = ?",
)


def quote_ident(name):
    """A SQLite identifier that survives whatever the user typed.

    SQLite quotes identifiers with double quotes and escapes an embedded one by
    doubling it, which is the only safe way to paste a layer name into `ALTER
    TABLE` - a statement that takes no bound parameters.
    """
    return '"' + str(name).replace('"', '""') + '"'


# Shapefile first so it is the default filter of the dialog
FILE_FILTER = ";;".join([
    "ESRI Shapefile (*.shp)",
    "GeoJSON (*.geojson *.json)",
    "KML / KMZ (*.kml *.kmz)",
    "GPX (*.gpx)",
    "MapInfo TAB (*.tab)",
    "AutoCAD DXF (*.dxf)",
    "GML (*.gml)",
    "CSV (*.csv)",
    "All vector files (*.shp *.geojson *.json *.kml *.kmz *.gpx *.tab *.dxf *.gml *.csv)",
    "All files (*)",
])

DB_FILTER = ";;".join([
    "GeoPackage (*.gpkg)",
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
            low = p.lower()
            if low.startswith('layername='):
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


def open_ds(path, update=False):
    """Open a vector data source, returning None instead of raising.

    GDAL's Python bindings raise RuntimeError rather than returning None once
    exceptions are enabled (QGIS turns them on), so every open has to be
    guarded.  An *empty* GeoPackage/SpatiaLite is also rejected read-only with
    "not recognized as being in a supported file format", so fall back to an
    update-mode open, which succeeds and reports zero layers.
    """
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else 0)
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    try:
        with suppress(Exception):
            return gdal.OpenEx(path, flags)
        if not update:
            with suppress(Exception):
                return gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
        return None
    finally:
        gdal.PopErrorHandler()


def list_layers(path):
    """Return [(name, geometry_type, feature_count), ...] for a data source."""
    gdal.PushErrorHandler('CPLQuietErrorHandler')
    try:
        ds = open_ds(path, False)
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
    """Make a layer name usable as a file name (shapefile targets)."""
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


# -------------------------------------------------------------- style helpers
# Two ways of shipping a style with the data:
#
#   * containers (GeoPackage / SpatiaLite) get the style stored in the
#     ``layer_styles`` table.  QGIS looks that table up whenever a layer of
#     that container is added to *any* project and silently applies the row
#     flagged ``useAsDefault`` - nothing to click, nothing to remember.
#   * everything else gets a ``.qml`` sidecar named after the file, which
#     QGIS loads automatically for file based layers.
#
# Either way the whole style is copied, not just the symbology: the export
# uses AllStyleCategories, so labels, renderer, blending / opacity, field
# aliases, form config, actions, diagrams, joins and layer notes travel with
# the data.  Layers carrying several named styles (Style Manager) keep all of
# them; the one that was active at export time becomes the default.

def style_categories():
    """AllStyleCategories, whichever enum location this QGIS build uses."""
    try:
        return QgsMapLayer.StyleCategory.AllStyleCategories
    except AttributeError:
        try:
            return QgsMapLayer.StyleCategory.AllStyleCategories
        except AttributeError:
            return None


def styles_of(layer):
    """[(style_name, QgsMapLayerStyle), ...] with the active style last.

    Writing the active style last leaves the destination layer holding it, so
    the row saved last - the default one - is also the one a plain
    ``.qml`` sidecar ends up describing.
    """
    try:
        manager = layer.styleManager()
        names = list(manager.styles() or [])
        current = manager.currentStyle()
    except Exception:
        return []
    if not names:
        return []
    if current in names:
        names.remove(current)
        names.append(current)
    return [(n, manager.style(n)) for n in names]


def write_qml(layer, qml_path):
    """Serialise the layer's current style to a .qml file. (ok, message)."""
    doc = QDomDocument("qgis")
    cats = style_categories()
    try:
        if cats is None:
            layer.exportNamedStyle(doc)
        else:
            try:
                layer.exportNamedStyle(doc, categories=cats)
            except TypeError:               # older signature without kwargs
                layer.exportNamedStyle(doc)
    except Exception as e:
        return False, str(e)

    if doc.isNull() or not doc.toString():
        return False, "empty style document"
    try:
        with open(qml_path, 'w', encoding='utf-8') as fh:
            fh.write(doc.toString(2))
    except Exception as e:
        return False, str(e)
    return True, ""


def purge_container_styles(layer):
    """Drop the styles already stored for *layer* so re-exports don't stack.

    ``listStylesInDatabase`` returns the styles of this very layer first and
    says how many those are, so only that leading slice is removed - styles
    belonging to the other layers of the container are left alone.  Best
    effort: if the provider has no style table yet there is nothing to do.
    """
    try:
        res = layer.listStylesInDatabase()
    except Exception:
        return
    if not isinstance(res, (tuple, list)) or len(res) < 2:
        return
    try:
        related = int(res[0])
        ids = list(res[1] or [])
    except (TypeError, ValueError):
        return
    for style_id in ids[:max(related, 0)]:
        with suppress(Exception):
            layer.deleteStyleFromDatabase(style_id)


def save_style_to_container(layer, style_name, as_default):
    """Store the layer's current style in the container's style table.

    Returns (ok, message).  ``saveStyleToDatabaseV2`` is used where it exists
    (the plain call is deprecated since QGIS 3.44 and reports failures much
    more precisely); older builds fall back to the string-returning one.
    A style that cannot be expressed as SLD is not treated as a failure -
    QGIS reads the QML copy back, and that one is lossless.
    """
    cats = style_categories()
    saver = getattr(layer, 'saveStyleToDatabaseV2', None)
    if saver is not None:
        try:
            if cats is None:
                res = saver(style_name, "", as_default, "")
            else:
                res = saver(style_name, "", as_default, "", cats)
        except Exception as e:
            return False, str(e)

        flags, message = (list(res) + ["", ""])[:2] if isinstance(res, (tuple, list)) \
            else (res, "")
        try:
            fatal = (QgsMapLayer.SaveStyleResult.DatabaseWriteFailed
                     | QgsMapLayer.SaveStyleResult.QmlGenerationFailed)
            broke = bool(int(flags) & int(fatal))
        except (AttributeError, TypeError, ValueError):
            broke = bool(flags)
        return (not broke), ("" if not broke else (str(message) or "provider refused the style"))

    try:
        res = layer.saveStyleToDatabase(style_name, "", as_default, "")
    except Exception as e:
        return False, str(e)
    if isinstance(res, (tuple, list)):
        res = res[-1] if res else None
    if res is None or res is True:
        return True, ""
    if res is False:
        return False, "the provider refused to store the style"
    text = str(res).strip()
    return (not text), text


# --------------------------------------------------------- drop-aware widgets
class DropLineEdit(QLineEdit):
    """Line edit that accepts datasets dragged from the QGIS Browser."""

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
        path, _ = sources[0]
        # A layer inside a container -> use the container.
        # A single file (e.g. .shp) -> use its folder, which is the target.
        if is_database(path) or os.path.isdir(path):
            target = path
        else:
            target = os.path.dirname(path)
        event.acceptProposedAction()
        self.pathDropped.emit(target)


class ImportTable(QTableWidget):
    """Table of queued layers; accepts drops from the QGIS Browser."""

    sourcesDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

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
        event.acceptProposedAction()
        self.sourcesDropped.emit(sources)


# -------------------------- layer picker, for databases and multi-layer files
class LayerPickerDialog(QDialog):
    """Multi-select list of the layers stored inside one data source."""

    def __init__(self, source_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select layers - {0}".format(os.path.basename(source_path)))
        self.resize(600, 460)
        self.source_path = source_path

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- Source path, single line, elided by the tooltip ---
        path_lbl = QLabel(source_path)
        path_lbl.setToolTip(source_path)
        path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_lbl.setStyleSheet("color: palette(mid);")
        layout.addWidget(path_lbl)

        # --- Filter box ---
        self.filter_le = QLineEdit()
        self.filter_le.setPlaceholderText("Filter layers...")
        self.filter_le.setClearButtonEnabled(True)
        self.filter_le.textChanged.connect(self.apply_filter)
        layout.addWidget(self.filter_le)

        # --- Layer table ---
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Layer", "Geometry", "Features"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.doubleClicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self.update_count)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)

        layers = list_layers(source_path)
        self.table.setRowCount(len(layers))
        for row, (name, geom, count) in enumerate(layers):
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, name)

            geom_item = QTableWidgetItem(geom or "-")

            count_text = "{:,}".format(count) if (count is not None and count >= 0) else "-"
            count_item = QTableWidgetItem(count_text)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, geom_item)
            self.table.setItem(row, 2, count_item)

        layout.addWidget(self.table)

        # --- Footer: select helpers + live count ---
        btn_row = QHBoxLayout()
        btn_all = QPushButton("Select All")
        btn_all.clicked.connect(self.select_all_visible)
        btn_none = QPushButton("Clear Selection")
        btn_none.clicked.connect(self.table.clearSelection)
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        btn_row.addStretch()
        self.count_lbl = QLabel()
        btn_row.addWidget(self.count_lbl)
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add Selected")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.table.selectAll()          # everything selected by default
        self.update_count()
        self.filter_le.setFocus()

    # ---------------- behaviour ----------------
    def apply_filter(self, text):
        text = text.strip().lower()
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text().lower()
            hidden = bool(text) and text not in name
            self.table.setRowHidden(row, hidden)
        self.update_count()

    def select_all_visible(self):
        """
        Select every visible row in one go.

        QTableView.selectRow() issues a ClearAndSelect command in
        ExtendedSelection mode, so calling it in a loop leaves only the last
        row selected. Build one QItemSelection and apply it in a single call.
        """
        model = self.table.model()
        last_col = self.table.columnCount() - 1
        selection = QItemSelection()
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                selection.select(model.index(row, 0), model.index(row, last_col))
        self.table.selectionModel().select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        self.update_count()

    def update_count(self):
        total = self.table.rowCount()
        shown = sum(1 for r in range(total) if not self.table.isRowHidden(r))
        picked = len(self.selected_layers())
        if shown == total:
            self.count_lbl.setText("{0} of {1} layers selected".format(picked, total))
        else:
            self.count_lbl.setText(
                "{0} selected  -  showing {1} of {2}".format(picked, shown, total))

    def selected_layers(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()
                       if not self.table.isRowHidden(i.row())})
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]

    def has_layers(self):
        return self.table.rowCount() > 0


# ----------------------------------------------------------------- the dialog
class SpatialDataManagerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Spatial Data Manager")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setAcceptDrops(True)
        self.resize(760, 560)
        self.data_path = ""
        self.layer_names = []
        self.export_skipped = 0

        self.layout = QVBoxLayout(self)

        # ---------------- Target selector ----------------
        file_layout = QHBoxLayout()
        self.file_path_le = DropLineEdit()
        self.file_path_le.setReadOnly(True)
        self.file_path_le.setPlaceholderText(
            "Select or drag a target .gpkg / .gdb / folder from the Browser panel...")
        self.file_path_le.pathDropped.connect(self.set_target)

        self.browse_btn = QToolButton()
        self.browse_btn.setText("Browse Target")
        self.browse_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.browse_btn)
        menu.addAction("GeoPackage / SQLite file...", self.browse_database_file)
        menu.addAction("File Geodatabase (.gdb)...", self.browse_gdb)
        menu.addAction("Shapefile folder...", self.browse_folder)
        self.browse_btn.setMenu(menu)

        file_layout.addWidget(QLabel("Target Database:"))
        file_layout.addWidget(self.file_path_le)
        file_layout.addWidget(self.browse_btn)
        self.layout.addLayout(file_layout)

        self.tabs = QTabWidget()

        # ---------------- Tab 1: Delete ----------------
        self.tab_delete = QWidget()
        self.layout_delete = QVBoxLayout(self.tab_delete)

        self.delete_filter_le = QLineEdit()
        self.delete_filter_le.setPlaceholderText("Filter layers...")
        self.delete_filter_le.setClearButtonEnabled(True)
        self.delete_filter_le.textChanged.connect(self.filter_delete_table)
        self.layout_delete.addWidget(self.delete_filter_le)

        self.delete_table = QTableWidget()
        self.delete_table.setColumnCount(3)
        self.delete_table.setHorizontalHeaderLabels(["Layer", "Geometry", "Features"])
        self.delete_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.delete_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.delete_table.setAlternatingRowColors(True)
        self.delete_table.verticalHeader().setDefaultSectionSize(22)
        self.delete_table.itemChanged.connect(self.update_delete_count)
        self.delete_table.cellDoubleClicked.connect(self.toggle_delete_row)
        del_header = self.delete_table.horizontalHeader()
        del_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        del_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        del_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        del_header.setHighlightSections(False)
        self.layout_delete.addWidget(self.delete_table)

        btn_layout_del = QHBoxLayout()
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.clicked.connect(self.select_all)
        self.btn_clear_all = QPushButton("Clear All")
        self.btn_clear_all.clicked.connect(self.clear_all)
        btn_layout_del.addWidget(self.btn_select_all)
        btn_layout_del.addWidget(self.btn_clear_all)
        btn_layout_del.addStretch()
        self.delete_count_lbl = QLabel()
        btn_layout_del.addWidget(self.delete_count_lbl)
        self.layout_delete.addLayout(btn_layout_del)

        self.btn_delete = QPushButton("Delete Selected")
        self.btn_delete.setMinimumHeight(28)
        self.btn_delete.clicked.connect(self.delete_layers)
        self.layout_delete.addWidget(self.btn_delete)
        self.tabs.addTab(self.tab_delete, "1. Delete Data")

        # ---------------- Tab 2: Rename ----------------
        self.tab_rename = QWidget()
        self.layout_rename = QVBoxLayout(self.tab_rename)
        self.table_widget = QTableWidget()
        self.table_widget.setColumnCount(2)
        self.table_widget.setHorizontalHeaderLabels(["Original Name", "New Name"])
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.layout_rename.addWidget(self.table_widget)

        self.btn_rename = QPushButton("Apply Renames")
        self.btn_rename.clicked.connect(self.rename_layers)
        self.layout_rename.addWidget(self.btn_rename)
        self.tabs.addTab(self.tab_rename, "2. Rename Data")

        # ---------------- Tab 3: Import ----------------
        self.tab_import = QWidget()
        self.layout_import = QVBoxLayout(self.tab_import)

        self.layout_import.addWidget(QLabel(
            "Layers queued for import (drag layers or datasets here from the Browser panel):"))

        self.import_table = ImportTable()
        self.import_table.setColumnCount(3)
        self.import_table.setHorizontalHeaderLabels(["Source Layer", "Import As", "Source File"])
        self.import_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.import_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.import_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.import_table.sourcesDropped.connect(self.add_sources)
        self.layout_import.addWidget(self.import_table)

        btn_layout_imp_add = QHBoxLayout()
        self.btn_add = QToolButton()
        self.btn_add.setText("Add Data to Import List   \u25be")
        self.btn_add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_add.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_add.setMinimumHeight(28)
        add_menu = QMenu(self.btn_add)
        add_menu.addAction("Vector files (Shapefile, GeoJSON, KML...)...",
                           self.add_import_files)
        add_menu.addSeparator()
        add_menu.addAction("GeoPackage / SQLite database...",
                           self.add_import_database_file)
        add_menu.addAction("File Geodatabase (.gdb)...", self.add_import_gdb)
        add_menu.addAction("Folder of shapefiles...", self.add_import_folder)
        self.btn_add.setMenu(add_menu)
        btn_layout_imp_add.addWidget(self.btn_add)
        self.layout_import.addLayout(btn_layout_imp_add)

        btn_layout_imp_rem = QHBoxLayout()
        self.btn_remove_files = QPushButton("Remove Selected")
        self.btn_remove_files.clicked.connect(self.remove_import_files)
        self.btn_clear_list = QPushButton("Clear List")
        self.btn_clear_list.clicked.connect(lambda: self.import_table.setRowCount(0))
        btn_layout_imp_rem.addWidget(self.btn_remove_files)
        btn_layout_imp_rem.addWidget(self.btn_clear_list)
        btn_layout_imp_rem.addStretch()
        self.layout_import.addLayout(btn_layout_imp_rem)

        self.chk_overwrite = QCheckBox("Overwrite layers that already exist in the target")
        self.layout_import.addWidget(self.chk_overwrite)

        self.btn_run_import = QPushButton("Run Import")
        self.btn_run_import.clicked.connect(self.run_import)
        self.layout_import.addWidget(self.btn_run_import)

        self.tabs.addTab(self.tab_import, "3. Import Data")

        # ---------------- Tab 4: Export layers from the Layers panel ----------------
        self.tab_export = QWidget()
        self.layout_export = QVBoxLayout(self.tab_export)

        self.layout_export.addWidget(QLabel(
            "Layers currently loaded in the QGIS Layers panel:"))

        self.export_table = QTableWidget()
        self.export_table.setColumnCount(5)
        self.export_table.setHorizontalHeaderLabels(
            ["Layer", "Geometry", "Features", "Styles", "Export As"])
        self.export_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.export_table.setAlternatingRowColors(True)
        self.export_table.verticalHeader().setDefaultSectionSize(22)
        self.export_table.itemChanged.connect(self.update_export_count)
        self.export_table.cellDoubleClicked.connect(self.toggle_export_row)
        exp_header = self.export_table.horizontalHeader()
        exp_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        exp_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        exp_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        exp_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        exp_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        exp_header.setHighlightSections(False)
        self.layout_export.addWidget(self.export_table)

        btn_layout_exp = QHBoxLayout()
        self.btn_refresh_layers = QPushButton("Refresh Layer List")
        self.btn_refresh_layers.clicked.connect(self.load_project_layers)
        self.btn_export_all = QPushButton("Select All")
        self.btn_export_all.clicked.connect(self.select_all_export)
        self.btn_export_none = QPushButton("Clear All")
        self.btn_export_none.clicked.connect(self.clear_all_export)
        btn_layout_exp.addWidget(self.btn_refresh_layers)
        btn_layout_exp.addWidget(self.btn_export_all)
        btn_layout_exp.addWidget(self.btn_export_none)
        btn_layout_exp.addStretch()
        self.export_count_lbl = QLabel()
        btn_layout_exp.addWidget(self.export_count_lbl)
        self.layout_export.addLayout(btn_layout_exp)

        self.chk_selected_only = QCheckBox("Export only the selected features of each layer")
        self.chk_export_overwrite = QCheckBox("Overwrite layers that already exist in the target")
        self.layout_export.addWidget(self.chk_selected_only)
        self.layout_export.addWidget(self.chk_export_overwrite)

        self.chk_export_styles = QCheckBox(
            "Export the styles too (symbology, labels, aliases, forms, ...)")
        self.chk_export_styles.setChecked(True)
        self.chk_export_styles.setToolTip(
            "Stores every named style of each layer in the target.\n"
            "GeoPackage / SpatiaLite: saved in the layer_styles table, so QGIS\n"
            "re-applies it by itself whenever the layer is added to a project.\n"
            "Shapefile folders: saved as a .qml file next to each shapefile.")
        self.chk_export_styles.toggled.connect(self._sync_style_options)
        self.layout_export.addWidget(self.chk_export_styles)

        qml_row = QHBoxLayout()
        qml_row.addSpacing(20)
        self.chk_style_qml = QCheckBox("Also drop a .qml copy of each style beside the target")
        self.chk_style_qml.setToolTip(
            "A portable backup of the style. Always written for shapefile\n"
            "targets, since those have nowhere else to keep it.")
        qml_row.addWidget(self.chk_style_qml)
        qml_row.addStretch()
        self.layout_export.addLayout(qml_row)
        self._sync_style_options(True)

        self.btn_run_export = QPushButton("Export Checked Layers")
        self.btn_run_export.setMinimumHeight(28)
        self.btn_run_export.clicked.connect(self.run_export)
        self.layout_export.addWidget(self.btn_run_export)

        self.tabs.addTab(self.tab_export, "4. Export Layers")
        self.tabs.currentChanged.connect(self.on_tab_changed)

        self.layout.addWidget(self.tabs)

        # ---------------- Footer ----------------
        footer = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.load_layers)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        footer.addWidget(self.btn_refresh)
        footer.addWidget(help_button('spatialdatamanager', self))
        footer.addStretch()
        footer.addWidget(self.btn_close)
        self.layout.addLayout(footer)

    # ---------------- Dialog-level drop ----------------
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasFormat(QGIS_MIME) or md.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        sources = sources_from_mimedata(event.mimeData())
        if not sources:
            return
        event.acceptProposedAction()
        if self.tabs.currentWidget() is self.tab_import:
            self.add_sources(sources)
        elif not self.data_path:
            path, _ = sources[0]
            self.set_target(path if (is_database(path) or os.path.isdir(path))
                            else os.path.dirname(path))

    # ---------------- Target handling ----------------
    def browse_database_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Target Database", "", DB_FILTER)
        if path:
            self.set_target(path)

    def browse_gdb(self):
        path = QFileDialog.getExistingDirectory(self, "Select Target File Geodatabase (.gdb)")
        if path:
            self.set_target(path)

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Target Shapefile Folder")
        if path:
            self.set_target(path)

    def set_target(self, path):
        if not path or not os.path.exists(path):
            return
        self.data_path = path
        self.file_path_le.setText(path)
        self.load_layers()

    def load_layers(self):
        self.delete_table.blockSignals(True)
        self.delete_table.setRowCount(0)
        self.table_widget.setRowCount(0)
        self.layer_names = []

        if not self.data_path or not os.path.exists(self.data_path):
            self.delete_table.blockSignals(False)
            self.update_delete_count()
            return

        layers = list_layers(self.data_path)
        self.layer_names = [n for n, _g, _c in layers]

        # --- Tab 1: checkable table ---
        self.delete_table.setRowCount(len(layers))
        for row, (name, geom, count) in enumerate(layers):
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, name)
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            name_item.setCheckState(Qt.CheckState.Unchecked)

            geom_item = QTableWidgetItem(geom or "-")

            count_text = "{:,}".format(count) if (count is not None and count >= 0) else "-"
            count_item = QTableWidgetItem(count_text)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self.delete_table.setItem(row, 0, name_item)
            self.delete_table.setItem(row, 1, geom_item)
            self.delete_table.setItem(row, 2, count_item)
        self.delete_table.blockSignals(False)
        self.filter_delete_table(self.delete_filter_le.text())

        # --- Tab 2: rename table ---
        self.table_widget.setRowCount(len(self.layer_names))
        for row, name in enumerate(self.layer_names):
            orig_item = QTableWidgetItem(name)
            orig_item.setFlags(orig_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            new_item = QTableWidgetItem("")
            self.table_widget.setItem(row, 0, orig_item)
            self.table_widget.setItem(row, 1, new_item)

    # ---------------- Tab 1 ----------------
    def filter_delete_table(self, text=""):
        text = (text or "").strip().lower()
        for row in range(self.delete_table.rowCount()):
            name = self.delete_table.item(row, 0).text().lower()
            self.delete_table.setRowHidden(row, bool(text) and text not in name)
        self.update_delete_count()

    def toggle_delete_row(self, row, _column):
        item = self.delete_table.item(row, 0)
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked)

    def checked_layers(self):
        names = []
        for row in range(self.delete_table.rowCount()):
            item = self.delete_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                names.append(item.data(Qt.ItemDataRole.UserRole))
        return names

    def update_delete_count(self, *_args):
        total = self.delete_table.rowCount()
        shown = sum(1 for r in range(total) if not self.delete_table.isRowHidden(r))
        picked = len(self.checked_layers())
        if shown == total:
            self.delete_count_lbl.setText("{0} of {1} layers ticked".format(picked, total))
        else:
            self.delete_count_lbl.setText(
                "{0} ticked  -  showing {1} of {2}".format(picked, shown, total))
        self.btn_delete.setText(
            "Delete Selected ({0})".format(picked) if picked else "Delete Selected")
        self.btn_delete.setEnabled(picked > 0)

    def select_all(self):
        """Tick every visible row."""
        self.delete_table.blockSignals(True)
        for row in range(self.delete_table.rowCount()):
            if not self.delete_table.isRowHidden(row):
                self.delete_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.delete_table.blockSignals(False)
        self.update_delete_count()

    def clear_all(self):
        self.delete_table.blockSignals(True)
        for row in range(self.delete_table.rowCount()):
            self.delete_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.delete_table.blockSignals(False)
        self.update_delete_count()

    def delete_layers(self):
        to_delete = self.checked_layers()
        if not to_delete:
            return

        preview = "\n".join("  - {0}".format(n) for n in to_delete[:15])
        if len(to_delete) > 15:
            preview += "\n  ... and {0} more".format(len(to_delete) - 15)

        reply = QMessageBox.question(
            self, "Confirm deletion",
            "Permanently delete {0} layer(s)?\n\n{1}\n\nThis cannot be undone.".format(
                len(to_delete), preview),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.No:
            return

        ds = open_ds(self.data_path, True)
        if not ds:
            QMessageBox.warning(self, "Error", "Cannot open dataset in write mode.")
            return

        for name in to_delete:
            for i in range(ds.GetLayerCount()):
                if ds.GetLayerByIndex(i).GetName() == name:
                    ds.DeleteLayer(i)
                    break
        ds = None

        QMessageBox.information(self, "Success",
                                "{0} layer(s) deleted.".format(len(to_delete)))
        self.load_layers()

    # ---------------- Tab 2 ----------------
    def rename_layers(self):
        renames = {}
        for row in range(self.table_widget.rowCount()):
            orig = self.table_widget.item(row, 0).text()
            new_item = self.table_widget.item(row, 1)
            new = new_item.text().strip() if new_item else ""
            if new and new != orig:
                renames[orig] = new

        if not renames:
            return
        reply = QMessageBox.question(self, "Confirm",
                                     "Rename {0} layer(s)?".format(len(renames)),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.No:
            return

        if self.data_path.lower().endswith('.gpkg'):
            try:
                conn = sqlite3.connect(self.data_path)
                cur = conn.cursor()
                for old, new in renames.items():
                    cur.execute('ALTER TABLE {0} RENAME TO {1}'.format(
                        quote_ident(old), quote_ident(new)))
                    cur.execute("UPDATE gpkg_contents SET table_name = ?, identifier = ? "
                                "WHERE table_name = ?", (new, new, old))
                    for statement in RENAME_SIDE_TABLE_SQL:
                        try:
                            cur.execute(statement, (new, old))
                        except sqlite3.OperationalError:
                            pass
                conn.commit()
                conn.close()
                QMessageBox.information(self, "Success", "GeoPackage layers renamed.")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

        elif self.data_path.lower().endswith('.gdb'):
            QMessageBox.warning(self, "Unsupported",
                                "Renaming File Geodatabase feature classes is disabled.")

        elif os.path.isdir(self.data_path):
            success = True
            for old, new in renames.items():
                for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.qmd', '.qix', '.sbn', '.sbx']:
                    old_f = os.path.join(self.data_path, old + ext)
                    new_f = os.path.join(self.data_path, new + ext)
                    if os.path.exists(old_f):
                        try:
                            os.rename(old_f, new_f)
                        except Exception:
                            success = False
            if success:
                QMessageBox.information(self, "Success", "Shapefiles renamed.")
            else:
                QMessageBox.warning(self, "Warning", "Some files could not be renamed.")
        self.load_layers()

    # ---------------- Tab 3: queue building ----------------
    def _queued(self):
        """Set of (path.lower(), layer.lower()) already in the table."""
        out = set()
        for row in range(self.import_table.rowCount()):
            data = self.import_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            out.add((data['path'].lower(), (data['layer'] or '').lower()))
        return out

    def add_queue_entry(self, path, layer_name, existing=None):
        if existing is None:
            existing = self._queued()
        key = (path.lower(), (layer_name or '').lower())
        if key in existing:
            return False
        existing.add(key)

        row = self.import_table.rowCount()
        self.import_table.insertRow(row)

        display = layer_name or os.path.splitext(os.path.basename(path))[0]
        src_item = QTableWidgetItem(display)
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        src_item.setData(Qt.ItemDataRole.UserRole, {'path': path, 'layer': layer_name})

        out_item = QTableWidgetItem(display)          # editable target name
        out_item.setToolTip("Double-click to change the name used in the target database.")

        path_item = QTableWidgetItem(path)
        path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        path_item.setToolTip(path)

        self.import_table.setItem(row, 0, src_item)
        self.import_table.setItem(row, 1, out_item)
        self.import_table.setItem(row, 2, path_item)
        return True

    def add_sources(self, sources):
        """sources = [(path, layer_name_or_None), ...] - expand containers if needed."""
        existing = self._queued()
        added = 0
        for path, layer in sources:
            if not os.path.exists(path):
                continue
            if layer:
                added += int(self.add_queue_entry(path, layer, existing))
                continue
            layers = list_layers(path)
            if not layers:
                continue
            if len(layers) == 1:
                added += int(self.add_queue_entry(path, layers[0][0], existing))
            else:
                for name, _g, _c in layers:
                    added += int(self.add_queue_entry(path, name, existing))
        if added == 0 and sources:
            QMessageBox.information(self, "Nothing added",
                                    "No new readable layers were found in the dropped item(s).")

    def add_import_files(self):
        """Plain vector files - the Shapefile filter is the default."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Vector Files to Import", self._start_dir(), FILE_FILTER)
        if files:
            self.add_sources([(f, None) for f in files])

    def add_import_database_file(self):
        """GeoPackage / SQLite - native dialog, then pick the layers."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Database File(s)", self._start_dir(), DB_FILTER)
        for path in files:
            self._pick_from_container(path)

    def add_import_gdb(self):
        """File Geodatabase - native folder dialog, then pick the feature classes."""
        path = QFileDialog.getExistingDirectory(
            self, "Select a File Geodatabase (.gdb)", self._start_dir())
        if not path:
            return
        if not path.lower().rstrip('/\\').endswith('.gdb'):
            reply = QMessageBox.question(
                self, "Not a .gdb",
                "'{0}' is not a .gdb folder.\n\nRead it as a folder of "
                "shapefiles instead?".format(os.path.basename(path)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return
        self._pick_from_container(path)

    def add_import_folder(self):
        """Folder of shapefiles - native folder dialog, then pick the files."""
        path = QFileDialog.getExistingDirectory(
            self, "Select a Folder of Shapefiles", self._start_dir())
        if path:
            self._pick_from_container(path)

    def _start_dir(self):
        """Open browse dialogs next to the target, when there is one."""
        if not self.data_path:
            return ""
        if os.path.isdir(self.data_path):
            return self.data_path
        return os.path.dirname(self.data_path)

    def _pick_from_container(self, path):
        picker = LayerPickerDialog(path, self)
        if not picker.has_layers():
            QMessageBox.warning(self, "No layers",
                                "No readable layers found in:\n{0}".format(path))
            return
        if picker.exec_() != QDialog.DialogCode.Accepted:
            return
        chosen = picker.selected_layers()
        if chosen:
            self.add_sources([(path, name) for name in chosen])

    def remove_import_files(self):
        rows = sorted({i.row() for i in self.import_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.import_table.removeRow(r)

    # ---------------- Tab 3: execution ----------------
    def run_import(self):
        if not self.data_path or not os.path.exists(self.data_path):
            QMessageBox.warning(self, "Error", "Please select a Target Database at the top first.")
            return

        total = self.import_table.rowCount()
        if total == 0:
            QMessageBox.warning(self, "Error", "The import list is empty.")
            return

        out_ds = open_ds(self.data_path, True)
        if not out_ds:
            QMessageBox.warning(self, "Error",
                                "Cannot open the Target Database in write mode.\n"
                                "It may be locked, read-only, or an unsupported format.")
            return

        existing = {out_ds.GetLayerByIndex(i).GetName().lower()
                    for i in range(out_ds.GetLayerCount())}
        overwrite = self.chk_overwrite.isChecked()

        # Group by source file so each one is opened only once
        grouped = {}
        for row in range(total):
            data = self.import_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            out_name = self.import_table.item(row, 1).text().strip()
            if not out_name:
                out_name = data['layer'] or os.path.splitext(
                    os.path.basename(data['path']))[0]
            grouped.setdefault(data['path'], []).append((data['layer'], out_name))

        progress = QProgressDialog("Importing layers...", "Cancel", 0, total, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        success, failed, done = 0, 0, 0
        errors = []
        gdal.PushErrorHandler('CPLQuietErrorHandler')
        try:
            for path, entries in grouped.items():
                in_ds = open_ds(path, False)
                if in_ds is None:
                    failed += len(entries)
                    done += len(entries)
                    errors.append("Cannot read: {0}".format(path))
                    progress.setValue(done)
                    continue

                for src_layer, out_name in entries:
                    if progress.wasCanceled():
                        break

                    progress.setLabelText("Importing {0}...".format(out_name))
                    QApplication.processEvents()

                    in_layer = (in_ds.GetLayerByName(src_layer) if src_layer
                                else in_ds.GetLayerByIndex(0))
                    if in_layer is None:
                        failed += 1
                        done += 1
                        errors.append("Layer not found: {0} in {1}".format(src_layer, path))
                        progress.setValue(done)
                        continue

                    final_name = out_name
                    if final_name.lower() in existing:
                        if overwrite:
                            for i in range(out_ds.GetLayerCount()):
                                if out_ds.GetLayerByIndex(i).GetName().lower() == final_name.lower():
                                    out_ds.DeleteLayer(i)
                                    break
                            existing.discard(final_name.lower())
                        else:
                            final_name = unique_name(final_name, existing)

                    try:
                        result = out_ds.CopyLayer(in_layer, final_name)
                    except Exception as e:
                        result = None
                        errors.append("{0}: {1}".format(final_name, e))

                    if result is not None:
                        success += 1
                        existing.add(final_name.lower())
                    else:
                        failed += 1
                        msg = gdal.GetLastErrorMsg()
                        errors.append("{0}: {1}".format(final_name, msg or "copy failed"))

                    done += 1
                    progress.setValue(done)

                in_ds = None
                if progress.wasCanceled():
                    break
        finally:
            gdal.PopErrorHandler()
            progress.setValue(total)
            out_ds = None

        if success:
            self.import_table.setRowCount(0)
        self.load_layers()

        summary = "Successfully imported {0} layer(s).\nFailed: {1}".format(success, failed)
        box = QMessageBox(self)
        box.setWindowTitle("Import Complete")
        box.setIcon(QMessageBox.Icon.Information if failed == 0 else QMessageBox.Icon.Warning)
        box.setText(summary)
        if errors:
            box.setDetailedText("\n".join(errors[:50]))
        box.exec_()


    # ---------------- Tab 4: export from the Layers panel ----------------
    def on_tab_changed(self, index):
        """Populate the export list the first time the tab is opened."""
        if self.tabs.widget(index) is self.tab_export and self.export_table.rowCount() == 0:
            self.load_project_layers()

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
                    self.export_table.item(row, 4).text())

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

            try:
                geom = QgsWkbTypes.displayString(lyr.wkbType())
            except Exception:
                geom = ""
            geom_item = QTableWidgetItem(geom or "-")
            geom_item.setFlags(geom_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            try:
                count = lyr.featureCount()
            except Exception:
                count = -1
            count_item = QTableWidgetItem("{:,}".format(count) if count >= 0 else "-")
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            names = [n for n, _s in styles_of(lyr)]
            style_item = QTableWidgetItem(str(len(names)) if names else "-")
            style_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            style_item.setFlags(style_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if names:
                style_item.setToolTip("Named styles exported:\n  " + "\n  ".join(names))

            out_item = QTableWidgetItem(old_name or safe_layer_name(lyr.name()))
            out_item.setToolTip("Double-click to change the name used in the target.")

            self.export_table.setItem(row, 0, name_item)
            self.export_table.setItem(row, 1, geom_item)
            self.export_table.setItem(row, 2, count_item)
            self.export_table.setItem(row, 3, style_item)
            self.export_table.setItem(row, 4, out_item)
        self.export_table.blockSignals(False)

        self.export_skipped = skipped
        self.update_export_count()

    def toggle_export_row(self, row, column):
        if column == 4:          # let the name column be edited normally
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

    def _sync_style_options(self, enabled):
        """The .qml option only means anything while styles are exported."""
        self.chk_style_qml.setEnabled(bool(enabled))

    def _target_driver(self):
        """Return (driver_name, datasource_options, writes_into_container)."""
        low = self.data_path.lower()
        if low.endswith('.gpkg'):
            return 'GPKG', [], True
        if low.endswith(('.sqlite', '.db', '.spatialite')):
            return 'SQLite', ['SPATIALITE=YES'], True
        if low.endswith('.gdb'):
            return 'OpenFileGDB', [], True
        return 'ESRI Shapefile', [], False

    def run_export(self):
        if not self.data_path or not os.path.exists(self.data_path):
            QMessageBox.warning(self, "Error",
                                "Please select a Target Database at the top first.")
            return

        rows = self.checked_export_rows()
        if not rows:
            QMessageBox.warning(self, "Error", "No layers are ticked.")
            return

        driver, ds_options, into_container = self._target_driver()
        overwrite = self.chk_export_overwrite.isChecked()
        only_selected = self.chk_selected_only.isChecked()
        with_styles = self.chk_export_styles.isChecked()
        # A shapefile folder has nowhere to keep a style but a sidecar, so the
        # .qml is forced on there.
        want_qml = with_styles and (self.chk_style_qml.isChecked() or not into_container)

        # What is already in the target?
        existing = {n.lower() for n, _g, _c in list_layers(self.data_path)}

        project = QgsProject.instance()
        layer_map = {lyr.id(): lyr for lyr in self.project_vector_layers()[0]}

        progress = QProgressDialog("Exporting layers...", "Cancel", 0, len(rows), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        success, failed, done = 0, 0, 0
        styled, style_failed = 0, 0
        errors = []

        for row in rows:
            if progress.wasCanceled():
                break

            layer_id = self.export_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            layer = layer_map.get(layer_id) or project.mapLayer(layer_id)
            source_name = self.export_table.item(row, 0).text()

            out_name = self.export_table.item(row, 4).text().strip() or source_name
            if not into_container:
                out_name = safe_layer_name(out_name)

            progress.setLabelText("Exporting {0}...".format(out_name))
            QApplication.processEvents()

            if layer is None:
                failed += 1
                errors.append("{0}: no longer in the project (press Refresh)".format(source_name))
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
            if ds_options:
                options.datasourceOptions = ds_options

            if into_container:
                options.layerName = final_name
                dest = self.data_path
                try:
                    options.actionOnExistingFile = \
                        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                except AttributeError:
                    pass
            else:
                dest = os.path.join(self.data_path, final_name + '.shp')

            ok, message = self._write_layer(layer, dest, options)
            if ok:
                success += 1
                existing.add(final_name.lower())
            else:
                failed += 1
                errors.append("{0}: {1}".format(source_name, message or "write failed"))

            if ok and with_styles:
                s_ok, s_msg = self._export_style(
                    layer, final_name, dest, into_container, want_qml)
                if s_ok:
                    styled += 1
                    if s_msg:
                        errors.append("{0}: {1}".format(source_name, s_msg))
                else:
                    style_failed += 1
                    errors.append("{0}: style not exported - {1}".format(
                        source_name, s_msg or "unknown error"))

            done += 1
            progress.setValue(done)

        progress.setValue(len(rows))
        self.load_layers()      # target changed - refresh tabs 1 and 2

        summary = "Exported {0} layer(s).\nFailed: {1}".format(success, failed)
        if with_styles:
            summary += "\nStyles exported: {0}".format(styled)
            if style_failed:
                summary += "  (failed on {0})".format(style_failed)
            if styled and into_container and driver in ('GPKG', 'SQLite'):
                summary += ("\n\nThe styles live in the target's layer_styles table, so "
                            "QGIS re-applies them\nautomatically when these layers are "
                            "added to another project.")
            elif styled and not into_container:
                summary += ("\n\nEach shapefile got a .qml beside it, which QGIS loads "
                            "automatically.\nKeep the .qml next to the .shp when you "
                            "move the data.")
        if driver == 'OpenFileGDB' and failed:
            summary += ("\n\nNote: writing into a .gdb needs GDAL 3.6 or newer.")
        box = QMessageBox(self)
        box.setWindowTitle("Export Complete")
        box.setIcon(QMessageBox.Icon.Information if not (failed or style_failed)
                    else QMessageBox.Icon.Warning)
        box.setText(summary)
        if errors:
            box.setDetailedText("\n".join(errors[:50]))
        box.exec_()

    def _export_style(self, source, out_name, dest_path, into_container, want_qml):
        """Give the layer just written the styles of the source layer.

        The freshly written layer is re-opened and each named style of the
        source is played onto it, so what gets stored describes the *exported*
        data - fields, geometry type and all - rather than the original.

        Returns (ok, message).
        """
        styles = styles_of(source)
        if not styles:
            return False, "the layer has no style to export"

        if into_container:
            uri = "{0}|layername={1}".format(dest_path, out_name)
            base_for_qml = os.path.join(os.path.dirname(dest_path) or '.', out_name)
        else:
            uri = dest_path
            base_for_qml = os.path.splitext(dest_path)[0]

        dest = QgsVectorLayer(uri, out_name, 'ogr')
        if not dest.isValid():
            return False, "cannot re-open the exported layer to attach the style"

        if into_container:
            # Exporting the same layer twice would otherwise pile up rows.
            purge_container_styles(dest)

        problems = []
        stored = 0
        current = styles[-1][0]                     # styles_of() puts it last

        for name, style in styles:
            try:
                style.writeToLayer(dest)
            except Exception as e:
                problems.append("{0}: {1}".format(name, e))
                continue

            is_default = (name == current)
            # One style is stored under the layer's own name so the entry is
            # recognisable in the Style Manager; the extras keep theirs.
            row_name = out_name if is_default else "{0} - {1}".format(out_name, name)

            if into_container:
                ok, msg = save_style_to_container(dest, row_name, is_default)
                if ok:
                    stored += 1
                else:
                    problems.append(msg)

            if want_qml:
                qml = (base_for_qml + '.qml') if is_default else \
                      "{0}_{1}.qml".format(base_for_qml, safe_layer_name(name))
                ok, msg = write_qml(dest, qml)
                if ok:
                    stored += 1
                else:
                    problems.append("{0}: {1}".format(os.path.basename(qml), msg))

        if stored:
            return True, ""

        # Nothing could go into the container - a File Geodatabase, or a file
        # opened read-only.  Rather than lose the style, drop it beside the
        # target as a .qml the user can load by hand.
        if into_container and not want_qml:
            fallback = base_for_qml + '.qml'
            ok, msg = write_qml(dest, fallback)
            if ok:
                return True, ("the target cannot store styles, so it was written to "
                              "{0} instead".format(os.path.basename(fallback)))
            problems.append(msg)

        return False, "; ".join(problems[:3]) or "nothing was written"

    def _write_layer(self, layer, dest_path, options):
        """Run QgsVectorFileWriter across API versions. Returns (ok, message)."""
        ctx = QgsProject.instance().transformContext()
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


# ---------------------------- launcher: the entry the toolbox and toolbar see
# Module-level reference keeps the modeless dialog alive after
# processAlgorithm() returns (otherwise Python garbage-collects it).
DIALOG_INSTANCE = None


class GpkgManagerAlgorithm(QgsProcessingAlgorithm):
    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return GpkgManagerAlgorithm()

    def name(self):
        return 'spatialdatamanager'

    def displayName(self):
        return self.tr('Spatial Data Manager')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('spatialdatamanager')

    def shortHelpString(self):
        return self.tr(
            "Bulk delete, rename or import layers in a GeoPackage, File Geodatabase, "
            "SpatiaLite database or shapefile folder.\n\n"
            "The window opens modelessly, so you can drag a dataset from the Browser "
            "panel onto the 'Target Database' box, and drag layers onto the Import list.\n\n"
            "Tab 4 exports the layers of the current project into the target and can "
            "carry their styles along - symbology, labels, aliases, forms and any extra "
            "named styles. Stored in the GeoPackage itself, they are re-applied "
            "automatically the next time the layers are added to a project."
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
        # with the previous target, ticks and import queue still filled in.
        # Re-running while the window is still open just brings it forward;
        # re-running after it was closed throws the old one away and starts clean.
        if DIALOG_INSTANCE is not None:
            try:
                still_open = DIALOG_INSTANCE.isVisible()
            except RuntimeError:        # Qt already destroyed the widget
                DIALOG_INSTANCE = None
                still_open = False
            if not still_open:
                try:
                    DIALOG_INSTANCE.deleteLater()
                except RuntimeError:
                    pass
                DIALOG_INSTANCE = None

        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = SpatialDataManagerDialog(parent)

        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo("Spatial Data Manager opened. "
                          "You can close this Processing dialog and keep working.")
        return {}

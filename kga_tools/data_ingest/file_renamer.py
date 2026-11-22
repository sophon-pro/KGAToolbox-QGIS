# -*- coding: utf-8 -*-
"""File Renamer: batch-rename GIS datasets in place, or write renamed copies to another folder.

The "Rename" tab of the Data Explorer (opened by the Rename button next to Create). It lists the
datasets selected in the Data Explorer's file list. Shapefile components and common SQLite / raster
sidecars move together, and matching layers in the project are re-pointed at the new path.

Adapted from the stand-alone KGA File Renamer 2.3.0.
"""
import os
import re
import shutil
import time
from pathlib import Path

from osgeo import gdal, ogr
from qgis.core import QgsApplication, QgsDataProvider, QgsProject, QgsProviderRegistry
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QRadioButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from . import inspector
from . import type_icons as ICONS
from .compat import ignore

SUPPORTED_EXTENSIONS = {
    ".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml", ".csv",
    ".sqlite", ".db", ".fgb", ".tif", ".tiff", ".img", ".vrt",
    ".jp2", ".ecw", ".asc", ".grd", ".nc", ".dem",
}
SHAPEFILE_SIDECARS = {
    ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx",
    ".ain", ".aih", ".atx", ".ixs", ".mxs",
}
RASTER_SUFFIXES = (".aux.xml", ".ovr", ".tfw", ".tifw", ".wld", ".prj", ".xml", ".rrd", ".ige")
SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
# Sidecars are never listed as datasets of their own.
_EXCLUDED_SUFFIXES = (".shp.xml", ".aux.xml", ".ovr", ".tfw", ".tifw", ".wld",
                      ".prj", ".cpg", ".shx", ".dbf", ".qix", "-wal", "-shm", "-journal")

_FILE_FILTER = ("GIS datasets (*.shp *.gpkg *.geojson *.json *.kml *.gml *.csv *.sqlite *.db "
                "*.fgb *.tif *.tiff *.img *.vrt *.jp2 *.ecw *.asc *.grd *.nc *.dem);;All files (*.*)")

_CHECKED = Qt.CheckState.Checked
_ROLE = Qt.ItemDataRole.UserRole           # container / file path
_ROLE_LAYER = int(Qt.ItemDataRole.UserRole) + 1   # layer name, for rows inside a database
_ROLE_OPEN = int(Qt.ItemDataRole.UserRole) + 2    # True while a database row is expanded
#: databases whose layers can be listed, renamed and copied in place (GDAL's layer Rename)
CONTAINER_EXTENSIONS = (".gpkg", ".gdb")


def norm(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def safe_stem(value):
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    return value.rstrip(" .")


def dataset_files(source, destination):
    """[(old, new)] for every physical file that makes up the dataset at `source`."""
    ext = Path(source).suffix.lower()
    src_dir = os.path.dirname(source)
    src_stem = Path(source).stem
    dst_dir = os.path.dirname(destination)
    dst_stem = Path(destination).stem

    if ext == ".shp":
        known = {s.lower() for s in SHAPEFILE_SIDECARS}
        pairs = [(source, destination)]
        for filename in os.listdir(src_dir):
            if not filename.lower().startswith(src_stem.lower() + "."):
                continue
            suffix = filename[len(src_stem):]
            if suffix.lower() == ".shp":
                continue
            if suffix.lower() in known or suffix.lower().startswith(".shp.xml"):
                pairs.append((os.path.join(src_dir, filename), os.path.join(dst_dir, dst_stem + suffix)))
        return pairs

    pairs = [(source, destination)]
    for suffix in SQLITE_SUFFIXES + RASTER_SUFFIXES:
        companion = source + suffix
        if os.path.isfile(companion):
            pairs.append((companion, destination + suffix))
    return pairs


def is_container(path):
    return path.lower().rstrip("/\\").endswith(CONTAINER_EXTENSIONS)


def path_exists(path):
    """A dataset on disk: a file, or a File Geodatabase folder."""
    return os.path.isfile(path) or (os.path.isdir(path) and path.lower().rstrip("/\\").endswith(".gdb"))


def list_layers(path):
    """[(name, category, is_table)] for the layers and tables inside a GeoPackage / File Geodatabase."""
    ds = ogr.Open(path)
    if ds is None:
        raise RuntimeError("Cannot open %s" % os.path.basename(path))
    out = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        gt = lyr.GetGeomType()
        if gt == ogr.wkbNone:
            out.append((lyr.GetName(), None, True))
        else:
            out.append((lyr.GetName(), ICONS.geometry_category(ogr.GeometryTypeToName(gt)), False))
    ds = None
    return out


def clean_layer_name(path, text):
    """The table name that will really be used (what the preview shows)."""
    name = text.strip()
    if path.lower().rstrip("/\\").endswith(".gdb"):       # ArcGIS-safe: letters, digits, underscore
        name = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if name and not name[0].isalpha():
            name = "L_" + name
        return name
    return re.sub(r'[\x00-\x1f"\'/\\]', "_", name).rstrip(" .")


def _open_for_write(path):
    ds = ogr.Open(path, 1)
    if ds is None:
        raise RuntimeError("Cannot open %s for writing: %s" % (os.path.basename(path), gdal.GetLastErrorMsg()))
    return ds


def rename_layer(path, old, new):
    """Rename table `old` to `new` inside the database at `path` (GDAL layer Rename; no SQL)."""
    ds = _open_for_write(path)
    lyr = ds.GetLayerByName(old)
    if lyr is None:
        raise RuntimeError("Layer '%s' not found." % old)
    if not lyr.TestCapability(ogr.OLCRename):
        raise RuntimeError("This database format cannot rename layers.")
    gdal.ErrorReset()
    if lyr.Rename(new) != 0:
        raise RuntimeError("Rename failed: %s" % (gdal.GetLastErrorMsg() or "unknown error"))
    lyr = ds = None


def copy_layer(path, old, new):
    """Copy table `old` as `new` inside the same database."""
    ds = _open_for_write(path)
    lyr = ds.GetLayerByName(old)
    if lyr is None:
        raise RuntimeError("Layer '%s' not found." % old)
    gdal.ErrorReset()
    if ds.CopyLayer(lyr, new) is None:
        raise RuntimeError("Copy failed: %s" % (gdal.GetLastErrorMsg() or "unknown error"))
    ds.FlushCache()
    lyr = ds = None


class DatasetTable(QTableWidget):
    """Dataset table with a centred empty-state message that follows the viewport."""

    def __init__(self, rows, columns, parent=None):
        super().__init__(rows, columns, parent)
        self.empty_label = QLabel(
            "Select datasets in the file list to rename them here.\n"
            "Double-click a GeoPackage or geodatabase\nto rename the layers inside it.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setForegroundRole(QPalette.ColorRole.Mid)
        self.empty_label.setParent(self.viewport())
        self.update_empty_state()

    def update_empty_state(self):
        self.empty_label.setGeometry(self.viewport().rect())
        self.empty_label.setVisible(self.rowCount() == 0)
        self.empty_label.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_empty_state()


class RenamePanel(QWidget):
    """Rename tab: edit new names for the selected datasets, then rename them or write copies."""
    C_USE, C_SOURCE, C_NEW, C_DEST, C_FORMAT, C_LAYERS, C_STATUS = range(7)
    #: emitted after a run that changed files on disk, so the Data Explorer can re-read its folder
    changed = pyqtSignal()

    def __init__(self, iface, parent=None, paths=None):
        super().__init__(parent)
        self.iface = iface
        self._building = False
        self._did_change = False
        #: set by the Data Explorer: drops the preview layer / mini windows that keep files open
        self.release_handles = None
        self._build_ui()
        self.add_paths(paths or [])

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        row = QHBoxLayout()
        row.addWidget(QLabel("Operation:"))
        self.mode_inplace = QRadioButton("Rename in place")
        self.mode_inplace.setToolTip("Rename the original datasets in their current folders.")
        self.mode_copy = QRadioButton("Create copies")
        self.mode_copy.setToolTip("Keep the originals and write renamed copies to another folder.")
        self.mode_inplace.setChecked(True)
        row.addWidget(self.mode_inplace)
        row.addWidget(self.mode_copy)
        row.addStretch(1)
        self.add_layers_btn = QToolButton()
        self.add_layers_btn.setIcon(QgsApplication.getThemeIcon("/mActionAddLayer.svg"))
        self.add_layers_btn.setText("Loaded Layers")
        self.add_layers_btn.setAutoRaise(True)
        self.add_layers_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_layers_btn.setToolTip("Add the file-based layers in the project to the list")
        self.add_layers_btn.clicked.connect(self.add_loaded_layers)
        row.addWidget(self.add_layers_btn)
        lay.addLayout(row)

        self.output_row = QWidget()          # only shown in "Create copies" mode
        row = QHBoxLayout(self.output_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.output_label = QLabel("Output folder:")
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Where the copies will be saved")
        self.output_browse = QPushButton("…")
        self.output_browse.setFixedWidth(32)
        row.addWidget(self.output_label)
        row.addWidget(self.output_edit, 1)
        row.addWidget(self.output_browse)
        lay.addWidget(self.output_row)

        self.table = DatasetTable(0, 7, self)
        self.table.setHorizontalHeaderLabels(["Use", "Current", "New name", "Destination", "Format",
                                              "Layers", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in ((self.C_USE, 36), (self.C_SOURCE, 190), (self.C_NEW, 170)):
            self.table.setColumnWidth(col, width)
        for col in (self.C_DEST, self.C_FORMAT, self.C_LAYERS):   # kept as data, shown below the list
            self.table.setColumnHidden(col, True)
        self.table.setMinimumHeight(160)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        lay.addWidget(self.table, 1)

        self.dest_label = QLabel()
        self.dest_label.setWordWrap(True)
        self.dest_label.setForegroundRole(QPalette.ColorRole.Mid)
        lay.addWidget(self.dest_label)

        self.update_paths_check = QCheckBox("Update project layers")
        self.update_paths_check.setChecked(True)
        self.update_paths_check.setToolTip(
            "In place: point layers at the renamed file. Copies: point layers at the copy.")
        self.overwrite_check = QCheckBox("Overwrite existing files")
        self.overwrite_check.setToolTip("Existing destination files are deleted first. Use with care.")
        row = QHBoxLayout()
        row.addWidget(self.update_paths_check)
        row.addSpacing(12)
        row.addWidget(self.overwrite_check)
        row.addStretch(1)
        lay.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.run_btn = QPushButton()
        self.run_btn.setDefault(True)
        lay.addWidget(self.run_btn)

        self.output_browse.clicked.connect(self.browse_output)
        self.mode_inplace.toggled.connect(self.mode_changed)
        self.mode_copy.toggled.connect(self.mode_changed)
        self.output_edit.textChanged.connect(self.update_all_previews)
        self.run_btn.clicked.connect(self.run_operations)
        self.table.itemChanged.connect(self.table_item_changed)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.currentCellChanged.connect(lambda *_: self._update_dest_label())
        self.mode_changed()

    def _checked_rows(self):
        return [r for r in range(self.table.rowCount())
                if self.table.item(r, self.C_USE).checkState() == _CHECKED]

    def _update_dest_label(self):
        row = self.table.currentRow()
        item = self.table.item(row, self.C_DEST) if row >= 0 else None
        self.dest_label.setText(("→ " + item.text()) if item and item.text() else "")

    # ------------------------------------------------------------------ mode
    def mode(self):
        return "copy" if self.mode_copy.isChecked() else "in_place"

    def mode_changed(self, *_):
        is_copy = self.mode() == "copy"
        self.output_row.setVisible(is_copy)
        self.update_all_previews()
        self._update_run_button()

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    # ------------------------------------------------------------------ project layers
    def _layer_path(self, layer):
        source = layer.source()
        try:
            meta = QgsProviderRegistry.instance().providerMetadata(layer.providerType())
            decoded = meta.decodeUri(source) if meta else None
            if isinstance(decoded, dict):
                for key in ("path", "database", "dbname"):
                    value = decoded.get(key)
                    if isinstance(value, str) and os.path.isfile(value):
                        return os.path.abspath(value)
        except Exception as exc:  # provider can't decode this URI: fall through to the plain parse
            ignore(exc)
        candidate = source.split("|", 1)[0].strip().strip('"')
        if candidate.lower().startswith("file://"):
            candidate = QUrl(candidate).toLocalFile()
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        return None

    def _layers_for(self, path):
        target = norm(path)
        result = []
        for layer in QgsProject.instance().mapLayers().values():
            layer_path = self._layer_path(layer)
            if layer_path and norm(layer_path) == target:
                result.append(layer)
        return result

    def _layer_uri_name(self, layer):
        """(path, layerName) of a project layer that comes from a database, else (None, None)."""
        try:
            meta = QgsProviderRegistry.instance().providerMetadata(layer.providerType())
            decoded = meta.decodeUri(layer.source()) if meta else None
            if isinstance(decoded, dict):
                path = decoded.get("path")
                if isinstance(path, str):
                    return norm(path), decoded.get("layerName")
        except Exception as exc:  # provider can't decode this URI
            ignore(exc)
        return None, None

    def _layers_in(self, path, name):
        target = norm(path)
        return [lyr for lyr in QgsProject.instance().mapLayers().values()
                if self._layer_uri_name(lyr) == (target, name)]

    def _rewrite_layer_name(self, layer, new_name):
        meta = QgsProviderRegistry.instance().providerMetadata(layer.providerType())
        decoded = meta.decodeUri(layer.source())
        decoded["layerName"] = new_name
        decoded.pop("layerId", None)
        return meta.encodeUri(decoded)

    # ------------------------------------------------------------------ rows
    def _is_layer_row(self, row):
        return self.table.item(row, self.C_SOURCE).data(_ROLE_LAYER) is not None

    def _row_name(self, row):
        item = self.table.item(row, self.C_SOURCE)
        layer = item.data(_ROLE_LAYER)
        return "%s › %s" % (os.path.basename(item.data(_ROLE)), layer) if layer is not None \
            else os.path.basename(item.data(_ROLE))

    @staticmethod
    def _file_label(path, open_=None):
        base = os.path.basename(path.rstrip("/\\"))
        if open_ is None:
            return base
        return ("▾ " if open_ else "▸ ") + base

    def refresh_loaded_layers(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.C_SOURCE)
            if not item:
                continue
            layer = item.data(_ROLE_LAYER)
            count = len(self._layers_in(item.data(_ROLE), layer)) if layer is not None \
                else len(self._layers_for(item.data(_ROLE)))
            self.table.setItem(row, self.C_LAYERS, QTableWidgetItem(str(count)))

    def _new_row(self, row, check):
        self.table.insertRow(row)
        box = QTableWidgetItem()
        box.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        box.setCheckState(_CHECKED if check else Qt.CheckState.Unchecked)
        self.table.setItem(row, self.C_USE, box)

    def add_paths(self, paths):
        """Add datasets by path; sidecars, unsupported types and duplicates are skipped.
        Returns how many rows were added."""
        before = self.table.rowCount()
        for path in paths:
            self._add_path(path)
        self._update_run_button()
        return self.table.rowCount() - before

    def _add_path(self, path):
        path = os.path.abspath(path)
        if not self.is_renamable(path):
            return
        ext = Path(path).suffix.lower()
        for row in range(self.table.rowCount()):
            if not self._is_layer_row(row) and norm(self.table.item(row, self.C_SOURCE).data(_ROLE)) == norm(path):
                return

        row = self.table.rowCount()
        self._building = True
        self._new_row(row, True)
        source_item = QTableWidgetItem(self._file_label(path, False if is_container(path) else None))
        source_item.setData(_ROLE, path)
        source_item.setData(_ROLE_OPEN, False)
        source_item.setToolTip(path + ("\nDouble-click to show the layers inside" if is_container(path) else ""))
        kind = inspector.KIND_UNKNOWN if os.path.isdir(path) else inspector.guess_kind_from_extension(path)
        source_item.setIcon(ICONS.file_icon(kind, path, top=True))
        source_item.setFlags(source_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, self.C_SOURCE, source_item)
        self.table.setItem(row, self.C_NEW, QTableWidgetItem(Path(path).stem))
        self.table.setItem(row, self.C_DEST, QTableWidgetItem(""))
        self.table.setItem(row, self.C_FORMAT, QTableWidgetItem(ext.upper().lstrip(".")))
        self.table.setItem(row, self.C_LAYERS, QTableWidgetItem(str(len(self._layers_for(path)))))
        self.table.setItem(row, self.C_STATUS, QTableWidgetItem("Ready"))
        self._building = False
        self.table.update_empty_state()
        self.update_row_preview(row)

    def add_loaded_layers(self):
        paths = []
        for layer in QgsProject.instance().mapLayers().values():
            p = self._layer_path(layer)
            if p:
                paths.append(p)
        added = self.add_paths(paths)
        self.iface.messageBar().pushInfo(
            "Rename", "Added %d unique dataset(s) from loaded layers." % added)

    @staticmethod
    def is_renamable(path):
        path = os.path.abspath(path)
        if not path_exists(path):
            return False
        if is_container(path):
            return True
        return (Path(path).suffix.lower() in SUPPORTED_EXTENSIONS
                and not path.lower().endswith(_EXCLUDED_SUFFIXES))

    # ------------------------------------------------------------------ database rows
    def _cell_double_clicked(self, row, col):
        if col not in (self.C_SOURCE, self.C_USE) or self._is_layer_row(row):
            return
        item = self.table.item(row, self.C_SOURCE)
        if not is_container(item.data(_ROLE)):
            return
        if item.data(_ROLE_OPEN):
            self.collapse(row)
        else:
            self.expand(row)

    def expand(self, row, restore=None):
        """Insert the layers and tables of the database at `row` below it. `restore` maps layer name
        to (new name, checked) so names already typed survive a rebuild."""
        item = self.table.item(row, self.C_SOURCE)
        path = item.data(_ROLE)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            layers = list_layers(path)
        except Exception as exc:  # unreadable database: say so in its status cell
            self.table.setItem(row, self.C_STATUS, QTableWidgetItem("Cannot list layers · %s" % exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._building = True
        item.setData(_ROLE_OPEN, True)
        item.setText(self._file_label(path, True))
        for i, (name, cat, is_table) in enumerate(layers, 1):
            r = row + i
            self._new_row(r, False)
            li = QTableWidgetItem("   " + name)
            li.setData(_ROLE, path)
            li.setData(_ROLE_LAYER, name)
            li.setToolTip("%s › %s" % (path, name))
            li.setIcon(ICONS.file_icon(inspector.KIND_TABLE if is_table else inspector.KIND_VECTOR,
                                       category=cat))
            li.setFlags(li.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, self.C_SOURCE, li)
            typed, checked = (restore or {}).get(name, (name, False))
            self.table.setItem(r, self.C_NEW, QTableWidgetItem(typed))
            self.table.item(r, self.C_USE).setCheckState(_CHECKED if checked else Qt.CheckState.Unchecked)
            self.table.setItem(r, self.C_DEST, QTableWidgetItem(""))
            self.table.setItem(r, self.C_FORMAT, QTableWidgetItem("Table" if is_table else "Layer"))
            self.table.setItem(r, self.C_LAYERS, QTableWidgetItem(str(len(self._layers_in(path, name)))))
            self.table.setItem(r, self.C_STATUS, QTableWidgetItem("Ready"))
        self._building = False
        for i in range(len(layers)):
            self.update_row_preview(row + 1 + i)
        self._update_run_button()

    def collapse(self, row):
        item = self.table.item(row, self.C_SOURCE)
        path = item.data(_ROLE)
        while row + 1 < self.table.rowCount() and self._is_layer_row(row + 1) \
                and norm(self.table.item(row + 1, self.C_SOURCE).data(_ROLE)) == norm(path):
            self.table.removeRow(row + 1)
        self._building = True
        item.setData(_ROLE_OPEN, False)
        item.setText(self._file_label(path, False))
        self._building = False
        self.table.update_empty_state()
        self._update_run_button()

    def _expanded_state(self):
        """{container path: {layer: (typed name, checked)}} for every expanded database."""
        state = {}
        for r in range(self.table.rowCount()):
            if self._is_layer_row(r):
                it = self.table.item(r, self.C_SOURCE)
                state.setdefault(norm(it.data(_ROLE)), {})[it.data(_ROLE_LAYER)] = (
                    self.table.item(r, self.C_NEW).text(),
                    self.table.item(r, self.C_USE).checkState() == _CHECKED)
        return state

    def sync_selection(self, paths):
        """Make the list follow the Data Explorer's selection, keeping names already typed and the
        databases already opened. An empty selection leaves the list alone (results stay visible)."""
        wanted = list(dict.fromkeys(os.path.abspath(p) for p in paths if self.is_renamable(p)))
        if not wanted:
            return
        files = [r for r in range(self.table.rowCount()) if not self._is_layer_row(r)]
        current = [self.table.item(r, self.C_SOURCE).data(_ROLE) for r in files]
        if {norm(p) for p in wanted} == {norm(p) for p in current}:
            return
        typed = {norm(self.table.item(r, self.C_SOURCE).data(_ROLE)): self.table.item(r, self.C_NEW).text()
                 for r in files}
        opened = self._expanded_state()
        self.table.setRowCount(0)
        self.add_paths(wanted)
        self._building = True
        for r in range(self.table.rowCount()):
            old = typed.get(norm(self.table.item(r, self.C_SOURCE).data(_ROLE)))
            if old is not None:
                self.table.item(r, self.C_NEW).setText(old)
        self._building = False
        r = 0
        while r < self.table.rowCount():
            key = norm(self.table.item(r, self.C_SOURCE).data(_ROLE))
            if key in opened and is_container(self.table.item(r, self.C_SOURCE).data(_ROLE)):
                self.expand(r, opened[key])
            r += 1
        self.update_all_previews()
        self._update_run_button()

    # ------------------------------------------------------------------ previews
    def table_item_changed(self, item):
        if self._building:
            return
        if item.column() == self.C_NEW:
            self.update_row_preview(item.row())
        if item.column() in (self.C_USE, self.C_NEW):
            self._update_run_button()

    def destination_for(self, source, new_stem):
        parent = self.output_edit.text().strip() if self.mode() == "copy" else os.path.dirname(source)
        if not parent:
            return ""
        return os.path.join(parent, safe_stem(new_stem) + Path(source).suffix)

    def _layer_target(self, row):
        item = self.table.item(row, self.C_SOURCE)
        return clean_layer_name(item.data(_ROLE), self.table.item(row, self.C_NEW).text())

    def update_row_preview(self, row):
        if row < 0 or row >= self.table.rowCount():
            return
        source_item = self.table.item(row, self.C_SOURCE)
        new_item = self.table.item(row, self.C_NEW)
        if not source_item or not new_item:
            return
        if self._is_layer_row(row):
            destination = "%s › %s" % (source_item.data(_ROLE), self._layer_target(row))
        else:
            destination = self.destination_for(source_item.data(_ROLE), new_item.text())
        item = QTableWidgetItem(destination)
        item.setToolTip(destination)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, self.C_DEST, item)
        if row == self.table.currentRow():
            self._update_dest_label()

    def update_all_previews(self, *_):
        for row in range(self.table.rowCount()):
            self.update_row_preview(row)
        self._update_dest_label()

    # ------------------------------------------------------------------ doing the work
    def _rewrite_uri(self, layer, old_path, new_path):
        source = layer.source()
        try:
            meta = QgsProviderRegistry.instance().providerMetadata(layer.providerType())
            decoded = meta.decodeUri(source) if meta else None
            if isinstance(decoded, dict):
                for key in ("path", "database", "dbname"):
                    value = decoded.get(key)
                    if isinstance(value, str) and norm(value) == norm(old_path):
                        decoded[key] = new_path
                        return meta.encodeUri(decoded)
        except Exception as exc:  # fall back to the textual replacement below
            ignore(exc)
        old_abs = os.path.abspath(old_path)
        if source.startswith(old_abs):
            return new_path + source[len(old_abs):]
        old_slash = old_abs.replace("\\", "/")
        source_slash = source.replace("\\", "/")
        if source_slash.startswith(old_slash):
            return new_path.replace("\\", "/") + source_slash[len(old_slash):]
        return None

    @staticmethod
    def _set_source(layer, uri, name, provider):
        # Once features have been read, the OGR provider keeps a pooled connection that holds the
        # file open even after the layer is re-pointed; reloadData() drops it first.
        try:
            prov = layer.dataProvider()
            if prov is not None:
                prov.reloadData()
        except Exception as exc:  # layer without a usable provider: nothing to release
            ignore(exc)
        layer.setDataSource(uri, name, provider, QgsDataProvider.ProviderOptions())

    def _changed_rows(self):
        """Checked rows that would do something: in place, a row whose name did not change is skipped."""
        rows = []
        for r in self._checked_rows():
            if self.mode() == "copy":
                rows.append(r)
                continue
            if self._is_layer_row(r):
                if self._layer_target(r) != self.table.item(r, self.C_SOURCE).data(_ROLE_LAYER):
                    rows.append(r)
            else:
                src = self.table.item(r, self.C_SOURCE).data(_ROLE)
                dest = self.destination_for(src, self.table.item(r, self.C_NEW).text())
                if dest and norm(dest) != norm(src):
                    rows.append(r)
        return rows

    def _update_run_button(self):
        n = len(self._changed_rows())
        if self.mode() == "copy":
            text = "Create %d cop%s" % (n, "y" if n == 1 else "ies") if n else "Create copies"
        else:
            text = "Rename %d item%s" % (n, "" if n == 1 else "s") if n else "Rename"
        self.run_btn.setText(text)
        self.run_btn.setEnabled(n > 0)

    def _preflight(self, selected):
        destinations = {}
        errors = []
        overwrite = self.overwrite_check.isChecked()
        file_rows = [r for r in selected if not self._is_layer_row(r)]
        if self.mode() == "copy" and file_rows:
            output_folder = self.output_edit.text().strip()
            if not output_folder:
                return ["Choose an output folder before creating renamed copies."]
            if not os.path.isdir(output_folder):
                return ["Output folder does not exist: %s" % output_folder]

        layer_names = {}      # container -> lower-case names that exist now
        layer_targets = {}    # (container, lower-case target) -> row
        for row in selected:
            source = self.table.item(row, self.C_SOURCE).data(_ROLE)
            if self._is_layer_row(row):
                old = self.table.item(row, self.C_SOURCE).data(_ROLE_LAYER)
                new = self._layer_target(row)
                if not new:
                    errors.append("Row %d: new name is empty or invalid." % (row + 1))
                    continue
                key = norm(source)
                if key not in layer_names:
                    try:
                        layer_names[key] = {n.lower() for n, _c, _t in list_layers(source)}
                    except Exception as exc:  # unreadable database
                        errors.append("Row %d: %s" % (row + 1, exc))
                        layer_names[key] = set()
                case_only = new.lower() == old.lower() and new != old and self.mode() == "in_place"
                if new.lower() in layer_names[key] and not case_only:
                    errors.append("Row %d: '%s' already exists in %s." % (row + 1, new, os.path.basename(source)))
                tk = (key, new.lower())
                if tk in layer_targets:
                    errors.append("Rows %d and %d would both be named '%s'." % (layer_targets[tk] + 1, row + 1, new))
                layer_targets[tk] = row
                continue

            stem = safe_stem(self.table.item(row, self.C_NEW).text())
            if not stem:
                errors.append("Row %d: new name is empty or invalid." % (row + 1))
                continue
            if not path_exists(source):
                errors.append("Row %d: the source no longer exists:\n%s" % (row + 1, source))
                continue
            destination = self.destination_for(source, stem)
            if not destination:
                errors.append("Row %d: could not build destination path." % (row + 1))
                continue
            key = norm(destination)
            if key in destinations:
                errors.append("Rows %d and %d have the same destination:\n%s"
                              % (destinations[key] + 1, row + 1, destination))
            else:
                destinations[key] = row
            if self.mode() == "copy" and norm(source) == key:
                errors.append("Row %d: output destination is the same as the source file." % (row + 1))
            try:
                for old, new in dataset_files(source, destination):
                    if os.path.exists(new) and norm(old) != norm(new) and not overwrite:
                        errors.append("Row %d: destination already exists:\n%s" % (row + 1, new))
            except OSError as exc:
                errors.append("Row %d: cannot inspect dataset companions: %s" % (row + 1, exc))
        return errors

    def _remove_path(self, path):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    def _copy_dataset(self, pairs):
        copied = []
        try:
            for old, new in pairs:
                os.makedirs(os.path.dirname(new), exist_ok=True)
                if os.path.exists(new) and self.overwrite_check.isChecked():
                    self._remove_path(new)
                if os.path.isdir(old):
                    shutil.copytree(old, new)
                else:
                    shutil.copy2(old, new)
                copied.append(new)
        except Exception:
            for path in reversed(copied):
                try:
                    self._remove_path(path)
                except OSError as exc:
                    ignore(exc)
            raise

    @staticmethod
    def _replace(old, new):
        """os.replace, retrying for a few seconds: a provider that was just closed lets go of the
        file a moment later (Windows refuses to rename a file another handle still has open)."""
        for attempt in range(25):
            try:
                os.replace(old, new)
                return
            except PermissionError as exc:
                if attempt == 24:
                    raise PermissionError(
                        "%s is open in another program (or in QGIS). Close it there, e.g. remove the layer "
                        "from other projects, ArcGIS, or an Explorer preview pane, then try again. (%s)"
                        % (os.path.basename(old), exc)) from exc
                QApplication.processEvents()
                time.sleep(0.2)

    def _rename_dataset(self, pairs):
        """Move every file; the main one (first pair) goes last. Undone if any move fails."""
        moved = []
        ordered = list(pairs[1:]) + [pairs[0]]
        try:
            for old, new in ordered:
                if os.path.exists(new) and norm(old) != norm(new) and self.overwrite_check.isChecked():
                    self._remove_path(new)
                self._replace(old, new)
                moved.append((new, old))
        except Exception:
            for new, old in reversed(moved):
                try:
                    if os.path.exists(new):
                        os.replace(new, old)
                except OSError as exc:
                    ignore(exc)
            raise

    def _run_layer_row(self, row):
        """Rename (in place) or copy (copies mode) one table inside its database."""
        item = self.table.item(row, self.C_SOURCE)
        path, old = item.data(_ROLE), item.data(_ROLE_LAYER)
        new = self._layer_target(row)
        if self.mode() == "copy":
            copy_layer(path, old, new)
            self._did_change = True
            self.table.setItem(row, self.C_STATUS, QTableWidgetItem("Copied as '%s'" % new))
            return
        layers = self._layers_in(path, old) if self.update_paths_check.isChecked() else []
        states = [(lyr, lyr.source(), lyr.name(), lyr.providerType(), self._rewrite_layer_name(lyr, new))
                  for lyr in layers]
        rename_layer(path, old, new)
        try:
            for lyr, _old, name, provider, uri in states:
                self._set_source(lyr, uri, name, provider)
        except Exception:
            for lyr, old_uri, name, provider, _new in states:
                try:
                    self._set_source(lyr, old_uri, name, provider)
                except Exception as exc:  # best effort: the original failure is what gets reported
                    ignore(exc)
            rename_layer(path, new, old)
            raise
        self._did_change = True
        QgsProject.instance().setDirty(True)
        self._building = True
        item.setData(_ROLE_LAYER, new)
        item.setText("   " + new)
        item.setToolTip("%s › %s" % (path, new))
        self.table.item(row, self.C_NEW).setText(new)
        self._building = False
        self.table.setItem(row, self.C_STATUS, QTableWidgetItem("Done · %d layer(s) updated" % len(layers)))
        self.update_row_preview(row)

    def _run_row(self, row):
        if self._is_layer_row(row):
            return self._run_layer_row(row)
        source = self.table.item(row, self.C_SOURCE).data(_ROLE)
        new_stem = safe_stem(self.table.item(row, self.C_NEW).text())
        destination = self.destination_for(source, new_stem)
        pairs = dataset_files(source, destination)

        layers = self._layers_for(source) if self.update_paths_check.isChecked() else []
        states = []
        for layer in layers:
            uri = self._rewrite_uri(layer, source, destination)
            if not uri:
                raise RuntimeError("Cannot safely update the source URI for layer '%s'." % layer.name())
            states.append((layer, layer.source(), layer.name(), layer.providerType(), uri))

        def restore():
            for layer, old_uri, name, provider, _new in states:
                try:
                    self._set_source(layer, old_uri, name, provider)
                except Exception as exc:  # best effort: report the original failure, not this one
                    ignore(exc)

        if self.mode() == "copy":
            self._copy_dataset(pairs)
            try:
                for layer, _old, name, provider, new_uri in states:
                    self._set_source(layer, new_uri, name, provider)
            except Exception:
                restore()
                for _o, copied in pairs:
                    if os.path.exists(copied):
                        try:
                            self._remove_path(copied)
                        except OSError as exc:
                            ignore(exc)
                raise
        else:
            # Point layers away first so the provider releases its file handle (Windows).
            try:
                for layer, _old, name, provider, new_uri in states:
                    self._set_source(layer, new_uri, name, provider)
                self._rename_dataset(pairs)
                for layer, _old, name, provider, new_uri in states:
                    self._set_source(layer, new_uri, name, provider)
            except Exception:
                restore()
                raise

        self._did_change = True
        QgsProject.instance().setDirty(True)
        self._building = True
        source_item = self.table.item(row, self.C_SOURCE)
        if self.mode() == "in_place":                  # the row (and the layers listed under it) now live here
            for r in range(row, self.table.rowCount()):
                it = self.table.item(r, self.C_SOURCE)
                if r > row and not (self._is_layer_row(r) and norm(it.data(_ROLE)) == norm(source)):
                    break
                it.setData(_ROLE, destination)
                it.setToolTip(destination if r == row else "%s › %s" % (destination, it.data(_ROLE_LAYER)))
            source_item.setText(self._file_label(destination, source_item.data(_ROLE_OPEN)
                                                 if is_container(destination) else None))
        self._building = False
        self.table.setItem(row, self.C_STATUS, QTableWidgetItem("Done · %d layer(s) updated" % len(layers)))
        self.update_row_preview(row)

    def run_operations(self):
        selected = self._changed_rows()
        if not selected:
            QMessageBox.information(self, "Nothing to do", "Check a row and give it a new name.")
            return
        errors = self._preflight(selected)
        if errors:
            QMessageBox.warning(self, "Please review destinations",
                                "The operation has not started. Fix these issues first:\n\n"
                                + "\n\n".join(errors[:12]))
            return

        what = ("rename the original datasets and tables in place" if self.mode() == "in_place"
                else "create renamed copies (tables are copied inside their own database)")
        Btn = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self, "Confirm operation",
            "You are about to %s for %d item(s).\n\n"
            "A backup of your data and QGIS project is recommended. Continue?" % (what, len(selected)),
            Btn.Yes | Btn.No, Btn.No)
        if answer != Btn.Yes:
            return

        if self.release_handles:
            self.release_handles()      # the Data Explorer's own preview keeps the selected file open
        # Tables go first: renaming a database file would otherwise move them out from under their rows.
        selected.sort(key=lambda r: not self._is_layer_row(r))
        self.run_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setMaximum(len(selected))
        self.progress.setValue(0)
        succeeded, failed = 0, []
        for index, row in enumerate(selected, 1):
            try:
                self._run_row(row)
                succeeded += 1
            except Exception as exc:  # one bad dataset must not stop the batch
                self.table.setItem(row, self.C_STATUS, QTableWidgetItem("Failed · %s" % exc))
                self.table.item(row, self.C_STATUS).setForeground(QColor("#b42318"))
                failed.append("%s: %s" % (self._row_name(row), exc))
            self.progress.setValue(index)
            QApplication.processEvents()

        self.progress.setVisible(False)
        self._update_run_button()
        self.refresh_loaded_layers()
        if self._did_change:
            self._did_change = False
            self.changed.emit()
        summary = "Successful: %d of %d." % (succeeded, len(selected))
        if failed:
            QMessageBox.warning(self, "Operation completed with errors",
                                summary + "\n\nFailed:\n" + "\n".join(failed[:8]))
        else:
            self.iface.messageBar().pushSuccess("Rename", summary)

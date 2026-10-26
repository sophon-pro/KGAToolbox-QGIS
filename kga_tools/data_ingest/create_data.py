# -*- coding: utf-8 -*-
"""Create an empty point / line / polygon layer or table in a folder, GeoPackage or
File Geodatabase from Data Explorer.

`create_data(spec)` does the writing with GDAL/OGR (no GUI, easy to test);
`CreateDataDialog` is the small window that collects the answers.
"""

import os
import re
import unicodedata

from qgis.core import QgsCoordinateReferenceSystem, QgsProject
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from osgeo import gdal, ogr, osr
except ImportError:  # pragma: no cover
    gdal = ogr = osr = None

POINT, LINE, POLYGON, TABLE = "point", "line", "polygon", "table"
TITLES = {POINT: "Point layer", LINE: "Line layer", POLYGON: "Polygon layer", TABLE: "Table"}

T_GPKG, T_GDB, T_FOLDER = "gpkg", "gdb", "folder"
FMT_GPKG, FMT_SHP = "GPKG", "SHP"

# label -> (key, OGR type name)
FIELD_TYPES = [
    ("Text", "text"),
    ("Integer (32 bit)", "int"),
    ("Integer (64 bit)", "int64"),
    ("Decimal (double)", "double"),
    ("Date", "date"),
    ("Date & time", "datetime"),
    ("Yes / No", "bool"),
]
_ASCII_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_RESERVED = {"fid", "geom", "ogc_fid", "objectid", "shape", "oid"}


# ---------------------------------------------------------------- targets

def target_for_path(path):
    """('gpkg' | 'gdb', path) when `path` is a database we can add to, else None."""
    clean = (path or "").rstrip("/\\")
    ext = os.path.splitext(clean)[1].lower()
    if ext == ".gpkg" and os.path.isfile(clean):
        return T_GPKG
    if ext == ".gdb" and os.path.isdir(clean):
        return T_GDB
    return None


def gdb_writable():
    """OpenFileGDB can create layers from GDAL 3.6."""
    if ogr is None:
        return False
    drv = ogr.GetDriverByName("OpenFileGDB")
    return drv is not None and drv.TestCapability(ogr.ODrCCreateDataSource)


# ---------------------------------------------------------------- checks

def _valid_ident(name, ascii_only):
    """Letters, digits and underscores, starting with a letter (Khmer vowel signs count as letters)."""
    if ascii_only:
        return bool(_ASCII_RE.match(name))
    if not name or not name[0].isalpha():
        return False
    return all(c == "_" or c.isalnum() or unicodedata.category(c).startswith("M") for c in name)


def name_problem(name, target_type, fmt=FMT_GPKG, existing=()):
    """Why `name` cannot be used for a layer/table here, or ''."""
    if not name:
        return "Enter a name."
    if not _valid_ident(name, target_type == T_GDB):
        return ("A name starts with a letter and has only letters, digits and underscores"
                + (" (A-Z only for a geodatabase)." if target_type == T_GDB else "."))
    if len(name) > 63:
        return "That name is too long (63 characters at most)."
    if name.lower().startswith(("gpkg_", "sqlite_", "rtree_")):
        return "That name is reserved by the database."
    if name.lower() in {e.lower() for e in existing}:
        return "'%s' already exists here." % name
    return ""


def field_problems(fields, target_type, fmt=FMT_GPKG):
    """List of readable problems in the field definitions (empty if fine)."""
    problems, seen = [], set()
    shp = target_type == T_FOLDER and fmt == FMT_SHP
    for i, f in enumerate(fields, 1):
        n = f["name"]
        p = name_problem(n, T_GDB if target_type == T_GDB else T_GPKG)
        if p:
            problems.append("Field %d: %s" % (i, p))
            continue
        if n.lower() in _RESERVED:
            problems.append("Field %d: '%s' is reserved." % (i, n))
        if n.lower() in seen:
            problems.append("Field %d: '%s' is used twice." % (i, n))
        seen.add(n.lower())
        if shp and len(n) > 10:
            problems.append("Field %d: a Shapefile field name has 10 characters at most ('%s')." % (i, n))
        if shp and f["type"] == "datetime":
            problems.append("Field %d: a Shapefile cannot store date & time; use Date or Text." % i)
    return problems


def existing_names(path):
    """Layer names already in the database at `path`."""
    if ogr is None:
        return []
    ds = ogr.Open(path)
    if ds is None:
        return []
    names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    ds = None
    return names


# ---------------------------------------------------------------- writing

def _srs(crs):
    srs = osr.SpatialReference()
    auth = crs.authid() if crs is not None and crs.isValid() else ""
    if auth.upper().startswith("EPSG:") and srs.ImportFromEPSG(int(auth.split(":")[1])) == 0:
        pass
    else:
        srs.ImportFromWkt(crs.toWkt())
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _default_sql(ftype, text):
    text = (text or "").strip()
    if not text:
        return None
    if ftype == "text":
        return "'%s'" % text.replace("'", "''")
    if ftype in ("int", "int64", "bool"):
        return str(int(float(text)))
    if ftype == "double":
        return str(float(text))
    return text  # date / date-time keywords such as CURRENT_DATE


def _add_fields(layer, fields):
    for f in fields:
        t = f["type"]
        otype = {"text": ogr.OFTString, "int": ogr.OFTInteger, "int64": ogr.OFTInteger64,
                 "double": ogr.OFTReal, "date": ogr.OFTDate, "datetime": ogr.OFTDateTime,
                 "bool": ogr.OFTInteger}[t]
        d = ogr.FieldDefn(f["name"], otype)
        if t == "bool":
            d.SetSubType(ogr.OFSTBoolean)
        if t == "text" and f.get("length"):
            d.SetWidth(int(f["length"]))
        if t == "double":
            if f.get("length"):
                d.SetWidth(int(f["length"]))
            if f.get("precision"):
                d.SetPrecision(int(f["precision"]))
        d.SetNullable(1 if f.get("nullable", True) else 0)
        default = _default_sql(t, f.get("default"))
        if default is not None:
            d.SetDefault(default)
        if layer.CreateField(d) != 0:
            raise ValueError("Could not add the field '%s': %s" % (f["name"], gdal.GetLastErrorMsg()))


_GEOM = {POINT: "wkbPoint", LINE: "wkbMultiLineString", POLYGON: "wkbMultiPolygon", TABLE: "wkbNone"}


def create_data(spec):
    """Create the layer/table described by `spec`; returns {'path', 'layer', 'uri', 'spatial'}.

    spec: target_type, path (database file/folder), format (folders only), name, geom
    (point/line/polygon/table), crs (QgsCoordinateReferenceSystem), fields [{name, type,
    length, precision, nullable, default}].  Raises ValueError with a readable message.
    """
    if ogr is None:
        raise ValueError("GDAL/OGR is not available.")
    tt, name, geom = spec["target_type"], spec["name"], spec["geom"]
    fmt = spec.get("format", FMT_GPKG)
    gtype = getattr(ogr, _GEOM[geom])
    spatial = geom != TABLE
    srs = _srs(spec["crs"]) if spatial else None
    fields = spec.get("fields") or []

    if tt == T_FOLDER and fmt == FMT_SHP:
        out = os.path.join(spec["path"], name + (".shp" if spatial else ".dbf"))
        if os.path.exists(out):
            raise ValueError("%s already exists." % os.path.basename(out))
        shp_type = {LINE: ogr.wkbLineString, POLYGON: ogr.wkbPolygon}.get(geom, gtype)
        ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(out)
        if ds is None:
            raise ValueError("Could not create %s: %s" % (os.path.basename(out), gdal.GetLastErrorMsg()))
        layer = ds.CreateLayer(name, srs, shp_type if spatial else ogr.wkbNone, ["ENCODING=UTF-8"])
        if layer is None:
            raise ValueError("Could not create the layer: %s" % gdal.GetLastErrorMsg())
        _add_fields(layer, fields)
        ds = layer = None
        return {"path": out, "layer": name, "uri": out, "spatial": spatial}

    if tt == T_FOLDER:
        path = os.path.join(spec["path"], name + ".gpkg")
        if os.path.exists(path):
            raise ValueError("%s already exists in this folder." % os.path.basename(path))
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    elif tt == T_GPKG:
        path = spec["path"]
        ds = ogr.Open(path, 1)
    else:
        path = spec["path"]
        if not gdb_writable():
            raise ValueError("This QGIS cannot write File Geodatabases (needs GDAL 3.6 or newer).")
        ds = ogr.Open(path, 1)
    if ds is None:
        raise ValueError("Could not open %s for editing: %s" % (os.path.basename(path), gdal.GetLastErrorMsg()))
    opts = []
    if tt != T_GDB:
        opts = ["SPATIAL_INDEX=YES"] if spatial else []
    layer = ds.CreateLayer(name, srs, gtype, opts)
    if layer is None:
        msg = gdal.GetLastErrorMsg()
        ds = None
        raise ValueError("Could not create '%s': %s" % (name, msg))
    try:
        _add_fields(layer, fields)
    finally:
        layer = ds = None
    uri = "%s|layername=%s" % (path, name)
    return {"path": path, "layer": name, "uri": uri, "spatial": spatial}


def add_to_project(result):
    """Open the new layer in the project (at the top level, never inside a group)."""
    from qgis.core import QgsVectorLayer
    layer = QgsVectorLayer(result["uri"], result["layer"], "ogr")
    if layer.isValid():
        QgsProject.instance().addMapLayer(layer)
        return layer
    return None


# ---------------------------------------------------------------- dialog

class CreateDataDialog(QDialog):
    COLS = ["Name", "Type", "Length", "Precision", "Allow null", "Default"]

    def __init__(self, geom, target_type, path, existing=(), parent=None):
        super().__init__(parent)
        self.geom, self.target_type, self.path = geom, target_type, path
        self.existing = list(existing)
        self.setWindowTitle("Create " + TITLES[geom])
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)

        where = os.path.basename(path.rstrip("/\\")) or path
        kind = {T_GPKG: "GeoPackage", T_GDB: "File Geodatabase", T_FOLDER: "Folder"}[target_type]
        form = QFormLayout()
        loc = QLabel("%s: %s" % (kind, where))
        loc.setToolTip(path)
        form.addRow("Create in:", loc)

        self.fmt = QComboBox()
        if target_type == T_FOLDER:
            self.fmt.addItem("GeoPackage (.gpkg)", FMT_GPKG)
            self.fmt.addItem("Shapefile (.shp)" if geom != TABLE else "DBF table (.dbf)", FMT_SHP)
            self.fmt.currentIndexChanged.connect(self._fmt_changed)
            form.addRow("Format:", self.fmt)

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. %s" % {POINT: "Wells", LINE: "Canals", POLYGON: "Parcels",
                                                 TABLE: "Survey_Records"}[geom])
        form.addRow("Name:", self.name)

        self.crs = QgsProjectionSelectionWidget()
        project_crs = QgsProject.instance().crs()
        self.crs.setCrs(project_crs if project_crs.isValid() else QgsCoordinateReferenceSystem("EPSG:4326"))
        if geom != TABLE:
            form.addRow("Coordinate system:", self.crs)
        lay.addLayout(form)

        self.add_fields = QCheckBox("Add fields now")
        self.add_fields.toggled.connect(self._toggle_fields)
        lay.addWidget(self.add_fields)

        self.field_box = QWidget()
        fl = QVBoxLayout(self.field_box)
        fl.setContentsMargins(18, 0, 0, 0)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        for i, w in enumerate((140, 130, 60, 70, 70)):
            self.table.setColumnWidth(i, w)
        self.table.setMinimumHeight(150)
        fl.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("Add field")
        add.clicked.connect(lambda: self._add_row())
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove_rows)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        fl.addLayout(row)
        lay.addWidget(self.field_box)
        self.field_box.setVisible(False)

        self.add_to_map = QCheckBox("Add to the map after creating")
        self.add_to_map.setChecked(True)
        lay.addWidget(self.add_to_map)

        bb = QDialogButtonBox()
        bb.addButton("Create", QDialogButtonBox.ButtonRole.AcceptRole).setDefault(True)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.name.setFocus()

    # ---- fields
    def _toggle_fields(self, on):
        self.field_box.setVisible(on)
        if on and self.table.rowCount() == 0:
            self._add_row()
        self.adjustSize()

    def _fmt_changed(self, _i):
        shp = self.fmt.currentData() == FMT_SHP
        self.name.setToolTip("Shapefile field names are limited to 10 characters." if shp else "")

    def _add_row(self, name=""):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        combo = QComboBox()
        for label, key in FIELD_TYPES:
            combo.addItem(label, key)
        self.table.setCellWidget(r, 1, combo)
        for c, hi in ((2, 254), (3, 15)):
            sp = QSpinBox()
            sp.setRange(0, hi)
            sp.setSpecialValueText("default")
            self.table.setCellWidget(r, c, sp)
        nul = QTableWidgetItem()
        nul.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        nul.setCheckState(Qt.CheckState.Checked)
        self.table.setItem(r, 4, nul)
        self.table.setItem(r, 5, QTableWidgetItem(""))
        self.table.setCurrentCell(r, 0)
        self.table.editItem(self.table.item(r, 0))

    def _remove_rows(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def fields(self):
        out = []
        if not self.add_fields.isChecked():
            return out
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text() or "").strip()
            if not name:
                continue
            out.append({"name": name, "type": self.table.cellWidget(r, 1).currentData(),
                        "length": self.table.cellWidget(r, 2).value(),
                        "precision": self.table.cellWidget(r, 3).value(),
                        "nullable": self.table.item(r, 4).checkState() == Qt.CheckState.Checked,
                        "default": self.table.item(r, 5).text()})
        return out

    # ---- result
    def spec(self):
        return {"target_type": self.target_type, "path": self.path, "format": self.fmt.currentData() or FMT_GPKG,
                "name": self.name.text().strip(), "geom": self.geom, "crs": self.crs.crs(), "fields": self.fields()}

    def _accept(self):
        spec = self.spec()
        existing = self.existing
        if self.target_type == T_FOLDER and spec["format"] == FMT_GPKG:
            existing = [os.path.splitext(f)[0] for f in os.listdir(self.path) if f.lower().endswith(".gpkg")]
        elif self.target_type == T_FOLDER:
            existing = [os.path.splitext(f)[0] for f in os.listdir(self.path)
                        if f.lower().endswith((".shp", ".dbf"))]
        problem = name_problem(spec["name"], self.target_type, spec["format"], existing)
        if not problem:
            problems = field_problems(spec["fields"], self.target_type, spec["format"])
            problem = "\n".join(problems)
        if not problem and self.geom != TABLE and not spec["crs"].isValid():
            problem = "Choose a coordinate system."
        if problem:
            QMessageBox.warning(self, self.windowTitle(), problem)
            return
        self.accept()


# ---------------------------------------------------------------- databases

# key -> (menu label, extension, OGR driver, creation options)
DATABASES = {
    "gpkg": ("Create GeoPackage", ".gpkg", "GPKG", []),
    "gdb": ("Create File Geodatabase", ".gdb", "OpenFileGDB", []),
    "sqlite": ("Create SpatiaLite database", ".sqlite", "SQLite", ["SPATIALITE=YES"]),
}


def database_supported(key):
    """Whether this GDAL can create that kind of database."""
    if ogr is None:
        return False
    drv = ogr.GetDriverByName(DATABASES[key][2])
    return drv is not None and bool(drv.TestCapability(ogr.ODrCCreateDataSource))


def create_database(key, folder, name):
    """Create an empty database called `name` in `folder`; returns its path."""
    label, ext, driver, options = DATABASES[key]
    problem = name_problem(name, T_GDB if key == "gdb" else T_GPKG)
    if problem:
        raise ValueError(problem)
    path = os.path.join(folder, name + ext)
    if os.path.exists(path):
        raise ValueError("%s already exists in this folder." % os.path.basename(path))
    if not database_supported(key):
        raise ValueError("This QGIS cannot create that kind of database.")
    ds = ogr.GetDriverByName(driver).CreateDataSource(path, options)
    if ds is None:
        raise ValueError("Could not create %s: %s" % (os.path.basename(path), gdal.GetLastErrorMsg()))
    ds = None
    return path


# ---------------------------------------------------------------- folders

_BAD_FOLDER_CHARS = set('<>:"/|?*') | {chr(92)}
_RESERVED_WIN = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}


def folder_name_problem(name):
    """Why `name` cannot be a folder name (Windows rules, which are the strictest), or ''."""
    if not name or not name.strip():
        return "Enter a name."
    if any(c in _BAD_FOLDER_CHARS or ord(c) < 32 for c in name):
        return "A folder name cannot contain  < > : \" / \\ | ? *"
    if name != name.strip() or name.endswith("."):
        return "A folder name cannot start or end with a space, or end with a dot."
    if name.split(".")[0].lower() in _RESERVED_WIN:
        return "'%s' is a reserved name." % name
    return ""


def create_folder(parent, name):
    """Create the sub-folder `name` in `parent`; returns its path."""
    name = (name or "").strip()
    problem = folder_name_problem(name)
    if problem:
        raise ValueError(problem)
    path = os.path.join(parent, name)
    if os.path.exists(path):
        raise ValueError("'%s' already exists here." % name)
    try:
        os.mkdir(path)
    except OSError as exc:
        raise ValueError("Could not create the folder: %s" % exc)
    return path

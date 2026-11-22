# -*- coding: utf-8 -*-
"""
Layer Path Manager - shared utilities.

This module holds the plain data structures used across the plugin
(``LayerRecord``, ``ChangeProposal``), helpers for decoding/encoding provider
URIs while preserving every provider-specific parameter, file-format and
file-filter tables, Qt/QGIS version compatibility shims and a small
background-task wrapper.

Nothing in this module modifies GIS datasets on disk.
"""

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsFeedback,
    QgsMapLayer,
    QgsMessageLog,
    QgsProviderRegistry,
    QgsTask,
)
from qgis.PyQt.QtCore import QDir, pyqtSignal

from ...core.compat import scoped

PLUGIN_NAME = "Layer Path Manager"
LOG_TAG = PLUGIN_NAME

logger = logging.getLogger("LayerPathManager")

IS_WINDOWS = sys.platform.startswith("win")

# --------------------------------------------------------------------------
# Logging -> QGIS message log bridge
# --------------------------------------------------------------------------


class QgsMessageLogHandler(logging.Handler):
    """Forward Python ``logging`` records to the QGIS Log Messages panel."""

    _LEVELS = {
        logging.DEBUG: Qgis.MessageLevel.Info,
        logging.INFO: Qgis.MessageLevel.Info,
        logging.WARNING: Qgis.MessageLevel.Warning,
        logging.ERROR: Qgis.MessageLevel.Critical,
        logging.CRITICAL: Qgis.MessageLevel.Critical,
    }

    def emit(self, record):
        try:
            level = self._LEVELS.get(record.levelno, Qgis.MessageLevel.Info)
            QgsMessageLog.logMessage(self.format(record), LOG_TAG, level)
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


def setup_logging():
    """Attach the QGIS message-log handler once."""
    if not any(isinstance(h, QgsMessageLogHandler) for h in logger.handlers):
        handler = QgsMessageLogHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

STATUS_VALID = "Valid"
STATUS_BROKEN = "Broken"
STATUS_REMOTE = "Remote"
STATUS_UNSUPPORTED = "Unsupported"

KIND_FILE = "File"
KIND_DIRECTORY = "Directory dataset"
KIND_DATABASE = "Database"
KIND_REMOTE = "Remote service"
KIND_MEMORY = "In-memory / virtual"
KIND_OTHER = "Other"

STORAGE_RELATIVE = "Relative"
STORAGE_ABSOLUTE = "Absolute"
STORAGE_NA = "N/A"

REMOTE_PROVIDERS = {
    "wms", "wfs", "oapif", "wcs", "arcgisfeatureserver", "arcgismapserver",
    "arcgisvectortileservice", "xyzvectortiles", "vectortile", "cesiumtiles",
    "tiledscene", "sensorthings", "geonode",
}
DATABASE_PROVIDERS = {
    "postgres", "postgresraster", "mssql", "oracle", "hana", "db2",
    "postgres_raster",
}
MEMORY_PROVIDERS = {"memory", "virtual", "mesh_memory", "virtualraster"}

# Providers whose URI carries a local file path we can repair.
FILE_PROVIDERS = {
    "ogr", "gdal", "delimitedtext", "spatialite", "gpx", "mdal", "pdal",
    "copc", "ept", "vpc", "mbtilesvectortiles", "vtpkvectortiles",
}

# extension -> human readable format
FORMAT_NAMES = {
    ".shp": "ESRI Shapefile", ".gpkg": "GeoPackage", ".geojson": "GeoJSON",
    ".json": "GeoJSON/JSON", ".kml": "KML", ".kmz": "KMZ", ".gml": "GML",
    ".tab": "MapInfo TAB", ".mif": "MapInfo MIF", ".csv": "CSV",
    ".txt": "Delimited text", ".sqlite": "SQLite/SpatiaLite",
    ".db": "SQLite", ".fgb": "FlatGeobuf", ".parquet": "GeoParquet",
    ".gpx": "GPX", ".dxf": "DXF", ".dwg": "DWG", ".gdb": "File Geodatabase",
    ".mdb": "Personal Geodatabase", ".xlsx": "Excel", ".xls": "Excel",
    ".ods": "OpenDocument Spreadsheet", ".osm": "OSM", ".pbf": "OSM PBF",
    ".tif": "GeoTIFF", ".tiff": "GeoTIFF", ".img": "ERDAS Imagine",
    ".vrt": "GDAL VRT", ".jp2": "JPEG 2000", ".ecw": "ECW", ".sid": "MrSID",
    ".asc": "ESRI ASCII Grid", ".nc": "NetCDF", ".hdf": "HDF4",
    ".h5": "HDF5", ".hdf5": "HDF5", ".grib": "GRIB", ".grb": "GRIB",
    ".grb2": "GRIB2", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
    ".bil": "BIL", ".dem": "USGS DEM", ".hgt": "SRTM HGT", ".adf": "ESRI Grid",
    ".mbtiles": "MBTiles", ".vtpk": "Vector Tile Package", ".las": "LAS",
    ".laz": "LAZ", ".copc.laz": "COPC", ".zip": "ZIP archive",
    ".gz": "GZip archive", ".2dm": "2DM mesh", ".rst": "Idrisi Raster",
    ".sdat": "SAGA Grid", ".grd": "Grid", ".ers": "ER Mapper", ".dt0": "DTED",
    ".dt1": "DTED", ".dt2": "DTED",
}

# Extensions that are treated as equivalent when "allow extension variants"
EXTENSION_FAMILIES = [
    {".tif", ".tiff", ".gtiff"},
    {".jpg", ".jpeg"},
    {".geojson", ".json"},
    {".h5", ".hdf5"},
    {".grb", ".grib", ".grb2"},
    {".sqlite", ".db", ".spatialite"},
]

VECTOR_EXTS = [
    "shp", "gpkg", "geojson", "json", "kml", "kmz", "gml", "tab", "mif",
    "csv", "txt", "sqlite", "db", "fgb", "parquet", "gpx", "dxf", "xlsx",
    "xls", "ods", "osm", "pbf", "mdb", "zip",
]
RASTER_EXTS = [
    "tif", "tiff", "img", "vrt", "jp2", "ecw", "sid", "asc", "nc", "hdf",
    "h5", "hdf5", "grib", "grb", "grb2", "png", "jpg", "jpeg", "bil", "dem",
    "hgt", "adf", "mbtiles", "gpkg", "rst", "sdat", "grd", "ers", "dt0",
    "dt1", "dt2", "zip",
]

# Shapefile and similar formats are made of several sidecar files.
SIDECAR_EXTENSIONS = {
    ".shp": [".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qmd"],
    ".tab": [".dat", ".map", ".id", ".ind"],
    ".tif": [".tfw", ".aux.xml", ".ovr", ".msk"],
    ".tiff": [".tfw", ".aux.xml", ".ovr", ".msk"],
    ".img": [".rrd", ".aux.xml", ".ige"],
    ".jpg": [".jgw", ".aux.xml"],
    ".png": [".pgw", ".aux.xml"],
    ".asc": [".prj", ".aux.xml"],
}

# Regex for GDAL subdataset style sources:  NETCDF:"/path/file.nc":variable
_SUBDATASET_RE = re.compile(r'^(?P<drv>[A-Za-z0-9_]+):"(?P<path>.+?)"(?P<rest>:.*)?$')
# Windows drive or UNC detection for "is absolute" on any platform
_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------


@dataclass
class LayerRecord:
    """Snapshot of one project layer, as produced by the inspector."""

    layer_id: str
    name: str
    layer_type: str = "Other"          # Vector / Raster / Mesh / Point cloud / ...
    provider: str = ""
    geometry: str = "N/A"
    source: str = ""                    # full provider URI (as QGIS holds it)
    path: str = ""                      # resolved local file/dir path ('' if none)
    file_format: str = ""
    source_kind: str = KIND_OTHER
    status: str = STATUS_UNSUPPORTED
    path_storage: str = STORAGE_NA      # how the path is written to the project file
    stored_path: str = ""               # the path string as it will be written
    error: str = ""
    notes: str = ""
    layer_valid: bool = False           # QgsMapLayer.isValid()
    exists: Optional[bool] = None       # filesystem check (None = not applicable)
    size_bytes: int = 0
    is_directory: bool = False
    sublayer: str = ""                  # e.g. GPKG layer name / subdataset
    repaired: bool = False              # repaired during this session
    group_path: str = ""                # position in the layer tree

    @property
    def is_file_based(self) -> bool:
        return bool(self.path) and self.source_kind in (KIND_FILE, KIND_DIRECTORY)

    @property
    def folder(self) -> str:
        """Folder that contains the dataset ('' for non-file layers)."""
        return os.path.dirname(self.path.rstrip("/")) if self.is_file_based else ""

    @property
    def file_name(self) -> str:
        return os.path.basename(self.path.rstrip("/")) if self.is_file_based else ""

    def as_dict(self) -> Dict:
        return {
            "layer_name": self.name, "layer_id": self.layer_id,
            "layer_type": self.layer_type, "provider": self.provider,
            "geometry_type": self.geometry, "source_uri": self.source,
            "resolved_path": self.path, "folder": self.folder, "file_format": self.file_format,
            "source_kind": self.source_kind, "status": self.status,
            "path_storage": self.path_storage, "stored_path": self.stored_path,
            "error": self.error, "notes": self.notes,
            "size_bytes": self.size_bytes, "group": self.group_path,
        }


@dataclass
class ChangeProposal:
    """A proposed change of one layer's data source, shown in the preview."""

    layer_id: str
    layer_name: str
    provider: str
    layer_type: str
    original_source: str
    original_path: str
    new_path: str = ""
    new_source: str = ""
    operation: str = ""
    match_type: str = ""
    confidence: int = 0                 # 0..100
    candidates: List[str] = field(default_factory=list)
    accepted: bool = False
    validation_state: str = ""          # "", "OK", "Warning", "Failed"
    validation_message: str = ""
    expected_geometry: str = ""
    is_directory: bool = False

    @property
    def is_change(self) -> bool:
        return bool(self.new_source) and self.new_source != self.original_source

    @property
    def needs_review(self) -> bool:
        return (not self.new_path) or self.confidence < 80 or self.validation_state == "Failed"


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------


def clean_path(path: str) -> str:
    """Normalise separators to '/' and remove '.', '..' and duplicate slashes.

    Uses Qt's cleanPath (the convention QGIS itself uses for layer sources).
    UNC prefixes ('//server') are preserved by QDir.cleanPath.
    """
    if not path:
        return path
    return QDir.cleanPath(QDir.fromNativeSeparators(path))


def paths_equal(a: str, b: str) -> bool:
    """Compare paths, case-insensitively on Windows."""
    a, b = clean_path(a), clean_path(b)
    return a.lower() == b.lower() if IS_WINDOWS else a == b


def looks_absolute(path: str) -> bool:
    return bool(path) and (path.startswith("/") or bool(_WIN_ABS_RE.match(path)))


def path_extension(path: str) -> str:
    """Lower-case extension, handling the double '.copc.laz' and '.aux.xml'."""
    low = path.lower().rstrip("/\\")
    for dbl in (".copc.laz",):
        if low.endswith(dbl):
            return dbl
    return os.path.splitext(low)[1]


def format_for_path(path: str, provider: str = "", is_dir: bool = False) -> str:
    ext = path_extension(path)
    if ext in FORMAT_NAMES:
        return FORMAT_NAMES[ext]
    if is_dir:
        return "Directory dataset"
    if ext:
        return ext.lstrip(".").upper()
    return provider.upper() if provider else ""


def extension_family(ext: str):
    for fam in EXTENSION_FAMILIES:
        if ext in fam:
            return fam
    return {ext}


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


def dataset_size(path: str) -> int:
    """Approximate on-disk size of a dataset including known sidecar files."""
    try:
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            return total
        total = os.path.getsize(path)
        base, ext = os.path.splitext(path)
        for side in SIDECAR_EXTENSIONS.get(ext.lower(), []):
            for cand in (base + side, path + side):
                if os.path.isfile(cand):
                    try:
                        total += os.path.getsize(cand)
                    except OSError:
                        pass
        return total
    except OSError:
        return 0


def missing_sidecars(path: str) -> List[str]:
    """Return required sidecar files that are missing (currently Shapefile .shx/.dbf)."""
    base, ext = os.path.splitext(path)
    if ext.lower() != ".shp":
        return []
    missing = []
    for side in (".shx", ".dbf"):
        if not (os.path.exists(base + side) or os.path.exists(base + side.upper())):
            missing.append(os.path.basename(base + side))
    return missing


# --------------------------------------------------------------------------
# URI helpers
# --------------------------------------------------------------------------


def decode_source(provider: str, source: str) -> Dict:
    """Decode a provider URI into parts.

    Returns a dict that always contains ``path`` (possibly '') and, when the
    path is wrapped in a GDAL subdataset string, ``_subdataset`` information so
    that it can be re-assembled exactly.
    """
    parts: Dict = {}
    try:
        parts = dict(QgsProviderRegistry.instance().decodeUri(provider, source) or {})
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("decodeUri failed for %s: %s", provider, exc)
        parts = {}

    path = parts.get("path") or ""
    if not isinstance(path, str):
        path = str(path) if path else ""

    # GDAL subdatasets e.g. NETCDF:"/p/file.nc":var are not split by decodeUri
    m = _SUBDATASET_RE.match(path) if path else None
    if m:
        parts["_subdataset"] = {"driver": m.group("drv"), "rest": m.group("rest") or ""}
        path = m.group("path")
    parts["path"] = path
    return parts


def sublayer_description(parts: Dict) -> str:
    bits = []
    for key in ("layerName", "layerId"):
        val = parts.get(key)
        if val not in (None, "") and str(val) != "NULL":
            bits.append(f"{key}={val}")
    sub = parts.get("_subdataset")
    if sub:
        bits.append(f"subdataset={sub['driver']}{sub['rest']}")
    if parts.get("vsiSuffix"):
        bits.append(f"inside archive: {parts['vsiSuffix']}")
    if parts.get("subset"):
        bits.append(f"filter: {parts['subset']}")
    return "; ".join(bits)


def replace_path_in_source(provider: str, source: str, old_path: str, new_path: str) -> str:
    """Build a new URI where only the file path changes.

    Every other URI component (GeoPackage layer names, subset filters, open
    options, subdataset identifiers, archive members, geometry columns ...)
    is kept untouched.  Strategy:

    1. If the old path appears verbatim in the URI, replace that substring
       only (most precise - preserves the URI byte for byte).
    2. Otherwise (e.g. URL-encoded delimited-text URIs) re-encode with the
       provider's own ``encodeUri`` after swapping the ``path`` part.
    """
    new_path = clean_path(new_path)
    if old_path and old_path in source:
        idx = source.find(old_path)
        return source[:idx] + new_path + source[idx + len(old_path):]

    parts = decode_source(provider, source)
    sub = parts.pop("_subdataset", None)
    if sub:
        parts["path"] = f'{sub["driver"]}:"{new_path}"{sub["rest"]}'
    else:
        parts["path"] = new_path
    try:
        encoded = QgsProviderRegistry.instance().encodeUri(provider, parts)
    except Exception as exc:
        raise ValueError(f"Provider '{provider}' cannot encode a new URI: {exc}")
    if not encoded:
        raise ValueError(f"Provider '{provider}' does not support URI re-encoding")
    return encoded


def classify_provider(provider: str, source: str, path: str) -> str:
    p = (provider or "").lower()
    s = (source or "").lower()
    if p in MEMORY_PROVIDERS:
        return KIND_MEMORY
    if p in DATABASE_PROVIDERS:
        return KIND_DATABASE
    if p in REMOTE_PROVIDERS:
        return KIND_REMOTE
    if path:
        pl = path.lower()
        if pl.startswith(("http://", "https://", "ftp://", "/vsicurl", "/vsis3",
                          "/vsigs", "/vsiaz", "/vsiadls", "/vsioss", "/vsiswift")):
            return KIND_REMOTE
        if pl.startswith(("pg:", "mssql:", "oci:")):
            return KIND_DATABASE
        if pl.endswith(".gdb") or pl.endswith(".gdb/"):
            return KIND_DIRECTORY
        return KIND_FILE
    if "url=" in s or s.startswith(("http://", "https://")):
        return KIND_REMOTE
    if "dbname=" in s or "service=" in s:
        return KIND_DATABASE
    return KIND_OTHER


def layer_type_name(layer: QgsMapLayer) -> str:
    t = layer.type()
    LT = getattr(Qgis, "LayerType", None)
    mapping = {}
    if LT is not None:
        mapping = {
            LT.Vector: "Vector", LT.Raster: "Raster", LT.Mesh: "Mesh",
            LT.VectorTile: "Vector tile", LT.PointCloud: "Point cloud",
            LT.Annotation: "Annotation", LT.Plugin: "Plugin",
        }
        if hasattr(LT, "Group"):
            mapping[LT.Group] = "Group"
        if hasattr(LT, "TiledScene"):
            mapping[LT.TiledScene] = "Tiled scene"
    else:  # QGIS < 3.30
        LT = QgsMapLayer
        mapping = {scoped(QgsMapLayer, 'LayerType', 'VectorLayer'): "Vector",
                   scoped(QgsMapLayer, 'LayerType', 'RasterLayer'): "Raster"}
        for attr, label in (("MeshLayer", "Mesh"), ("VectorTileLayer", "Vector tile"),
                            ("PointCloudLayer", "Point cloud"), ("AnnotationLayer", "Annotation")):
            if hasattr(QgsMapLayer, attr):
                mapping[getattr(QgsMapLayer, attr)] = label
    return mapping.get(t, "Other")


def is_vector(layer) -> bool:
    return layer_type_name(layer) == "Vector"


def is_raster(layer) -> bool:
    return layer_type_name(layer) == "Raster"


def file_dialog_filter(provider: str, layer_type: str, original_path: str) -> str:
    """File dialog filter string matching the original layer's provider/type."""
    ext = path_extension(original_path).lstrip(".")
    filters = []
    if ext:
        fmt = FORMAT_NAMES.get("." + ext, ext.upper())
        variants = sorted(e.lstrip(".") for e in extension_family("." + ext))
        pats = " ".join(f"*.{e} *.{e.upper()}" for e in variants)
        filters.append(f"Same format - {fmt} ({pats})")
    if layer_type == "Raster" or provider == "gdal":
        filters.append("Raster datasets (" + " ".join("*." + e for e in RASTER_EXTS) + ")")
    elif provider == "delimitedtext":
        filters.append("Delimited text (*.csv *.txt *.tsv *.dat)")
    else:
        filters.append("Vector datasets (" + " ".join("*." + e for e in VECTOR_EXTS) + ")")
    filters.append("All files (*)")
    return ";;".join(filters)


# --------------------------------------------------------------------------
# Qt / QGIS compatibility
# --------------------------------------------------------------------------


def project_uses_relative_paths(project) -> bool:
    if hasattr(project, "filePathStorage"):
        return project.filePathStorage() == Qgis.FilePathType.Relative
    absolute, _ok = project.readBoolEntry("Paths", "/Absolute", False)
    return not absolute


def set_project_relative_paths(project, relative: bool):
    if hasattr(project, "setFilePathStorage"):
        project.setFilePathStorage(
            Qgis.FilePathType.Relative if relative else Qgis.FilePathType.Absolute)
    else:  # pragma: no cover - QGIS < 3.22
        project.writeEntry("Paths", "/Absolute", not relative)
    project.setDirty(True)


def geometry_type_name(layer) -> str:
    try:
        from qgis.core import QgsWkbTypes
        gt = layer.geometryType()
        names = {}
        GT = getattr(Qgis, "GeometryType", None)
        if GT is not None:
            names = {GT.Point: "Point", GT.Line: "Line", GT.Polygon: "Polygon",
                     GT.Null: "No geometry (table)", GT.Unknown: "Unknown"}
        else:  # pragma: no cover
            names = {scoped(QgsWkbTypes, 'GeometryType', 'PointGeometry'): "Point",
                     scoped(QgsWkbTypes, 'GeometryType', 'LineGeometry'): "Line",
                     scoped(QgsWkbTypes, 'GeometryType', 'PolygonGeometry'): "Polygon",
                     scoped(QgsWkbTypes, 'GeometryType', 'NullGeometry'): "No geometry (table)",
                     scoped(QgsWkbTypes, 'GeometryType', 'UnknownGeometry'): "Unknown"}
        name = names.get(gt, "Unknown")
        wkb = QgsWkbTypes.displayString(layer.wkbType())
        if name not in ("Unknown",) and wkb and wkb.lower() not in ("unknown", "nogeometry"):
            return f"{name} ({wkb})"
        return name
    except Exception:
        return "Unknown"


def error_summary(obj) -> str:
    """Safe ``obj.error().summary()``.

    QGIS 3.34 segfaults when ``summary()`` is called on an *empty* QgsError,
    so emptiness is always checked first.
    """
    try:
        if obj is None or not hasattr(obj, "error"):
            return ""
        err = obj.error()
        if err is None or err.isEmpty():
            return ""
        return (err.summary() or "").strip()
    except Exception:
        return ""


def theme_icon(name: str):
    return QgsApplication.getThemeIcon("/" + name)


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------


class BackgroundJob(QgsTask):
    """Run a pure-Python callable in the QGIS task manager.

    The callable receives a ``QgsFeedback``; it must only touch plain Python
    data (never QgsMapLayer objects - they are not thread safe).
    Results are delivered on the main thread through ``jobFinished``.
    """

    jobFinished = pyqtSignal(bool, object, str)  # success, result, error

    def __init__(self, description, func, *args, **kwargs):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self.feedback = QgsFeedback()
        self.result_value = None
        self.error_message = ""
        self.feedback.progressChanged.connect(self.setProgress)

    def run(self):
        try:
            self.result_value = self._func(self.feedback, *self._args, **self._kwargs)
            return not self.feedback.isCanceled()
        except Exception as exc:  # report, never crash QGIS
            logger.exception("Background job failed")
            self.error_message = str(exc)
            return False

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def finished(self, result):
        err = self.error_message
        if not result and not err and self.feedback.isCanceled():
            err = "Cancelled by user"
        self.jobFinished.emit(bool(result), self.result_value, err)

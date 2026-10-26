"""Discovery and inspection of geodata files.

Uses QgsProviderRegistry.querySublayers() so every format QGIS can read
(GDAL/OGR, PDAL, MDAL, ...) is supported, including multi-layer
containers such as GeoPackage, KML, DXF, File Geodatabase or NetCDF.
"""

import csv
import os
import threading
import time

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsMeshLayer,
    QgsPointCloudLayer,
    QgsProject,
    QgsProviderRegistry,
    QgsProviderSublayerDetails,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QUrl, QUrlQuery
from .compat import ignore

KIND_VECTOR = "vector"
KIND_RASTER = "raster"
KIND_TABLE = "table"
KIND_POINTCLOUD = "pointcloud"
KIND_MESH = "mesh"
KIND_UNKNOWN = "unknown"

KIND_LABELS = {
    KIND_VECTOR: "Vectors",
    KIND_RASTER: "Rasters",
    KIND_TABLE: "Tables",
    KIND_POINTCLOUD: "Point clouds",
    KIND_MESH: "Meshes",
    KIND_UNKNOWN: "Other",
}

VECTOR_EXT = {
    ".shp", ".gpkg", ".geojson", ".json", ".kml", ".kmz", ".gpx", ".gml",
    ".fgb", ".tab", ".mif", ".dxf", ".sqlite", ".parquet", ".geoparquet",
    ".gdb", ".osm", ".pbf", ".topojson", ".csvt",
}
RASTER_EXT = {
    ".tif", ".tiff", ".vrt", ".jp2", ".img", ".ecw", ".sid", ".asc", ".dem",
    ".hgt", ".nc", ".grib", ".grb", ".grb2", ".hdf", ".h5", ".he5", ".bil",
    ".dt0", ".dt1", ".dt2", ".png", ".jpg", ".jpeg", ".webp", ".sdat", ".rst",
    ".adf", ".mbtiles", ".zarr",
}
TABLE_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".ods", ".dbf"}
POINTCLOUD_EXT = {".las", ".laz", ".copc", ".e57", ".ept"}
MESH_EXT = {".2dm", ".slf", ".msh", ".3dm"}

ALL_EXT = VECTOR_EXT | RASTER_EXT | TABLE_EXT | POINTCLOUD_EXT | MESH_EXT
DIR_DATASETS = {".gdb", ".zarr"}  # folders that are datasets

# Sidecar files that travel with a dataset when copied
SIDECAR_EXT = {
    ".shp": [".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".qmd",
             ".qml", ".shp.xml", ".fix", ".aih", ".ain", ".atx", ".mxs"],
    "*": [".aux.xml", ".ovr", ".qml", ".qmd", ".prj", ".tfw", ".tifw", ".wld",
          ".jgw", ".pgw", ".j2w", ".xml", ".csvt", ".msk", ".rpb", ".imd"],
}

PROVIDER_KIND = {
    "ogr": KIND_VECTOR,
    "gdal": KIND_RASTER,
    "pdal": KIND_POINTCLOUD,
    "copc": KIND_POINTCLOUD,
    "ept": KIND_POINTCLOUD,
    "mdal": KIND_MESH,
    "delimitedtext": KIND_VECTOR,
}

X_NAMES = ["x", "lon", "long", "lng", "longitude", "easting", "east", "xcoord",
           "x_coord", "point_x", "coord_x", "lon_dd", "long_dd"]
Y_NAMES = ["y", "lat", "latitude", "northing", "north", "ycoord", "y_coord",
           "point_y", "coord_y", "lat_dd"]
WKT_NAMES = ["wkt", "geom", "geometry", "the_geom", "shape", "wkt_geom"]


CONTAINER_LABELS = {
    ".gdb": "File Geodatabase",
    ".gpkg": "GeoPackage",
    ".sqlite": "SpatiaLite / SQLite",
    ".zip": "Zip archive",
    ".kml": "KML",
    ".kmz": "KMZ",
    ".nc": "NetCDF",
    ".h5": "HDF5",
    ".hdf": "HDF",
    ".mbtiles": "MBTiles",
    ".xlsx": "Excel workbook",
    ".ods": "Spreadsheet",
}


def _ext(path):
    return os.path.splitext(path.rstrip("/\\"))[1].lower()


def is_dataset_folder(path):
    """A folder that is really one dataset (e.g. an Esri File Geodatabase)."""
    return bool(path) and os.path.isdir(path) and _ext(path) in DIR_DATASETS


def is_container(path):
    """Files/folders that can hold several layers of different types."""
    return _ext(path) in (".gdb", ".gpkg", ".sqlite", ".zip", ".kmz", ".nc",
                          ".h5", ".hdf", ".mbtiles")


def container_label(path):
    return CONTAINER_LABELS.get(_ext(path), "")


def guess_kind_from_extension(path):
    ext = _ext(path)
    if ext in VECTOR_EXT:
        return KIND_VECTOR
    if ext in RASTER_EXT:
        return KIND_RASTER
    if ext in TABLE_EXT:
        return KIND_TABLE
    if ext in POINTCLOUD_EXT:
        return KIND_POINTCLOUD
    if ext in MESH_EXT:
        return KIND_MESH
    return KIND_UNKNOWN


def is_geodata_candidate(path):
    ext = os.path.splitext(path)[1].lower()
    if os.path.isdir(path):
        return ext in DIR_DATASETS
    if path.lower().endswith(".shp.xml") or path.lower().endswith(".aux.xml"):
        return False
    if ext == ".zip":
        return True
    return ext in ALL_EXT


SINGLE_LAYER_VECTOR = {".shp", ".geojson", ".json", ".fgb", ".topojson", ".geojsonl", ".geojsons"}
SINGLE_LAYER_RASTER = {".tif", ".tiff", ".jp2", ".img", ".vrt", ".asc", ".ecw", ".sid", ".dem",
                       ".hgt", ".bil", ".png", ".jpg", ".jpeg", ".webp", ".dt0", ".dt1", ".dt2",
                       ".sdat", ".rst"}


def direct_source(path):
    """(provider, uri, kind) for single-layer formats, so they can be opened
    without a sublayer scan (which parses a whole GeoJSON a second time)."""
    if os.path.isdir(path):
        return None
    ext = _ext(path)
    if ext in SINGLE_LAYER_VECTOR:
        return "ogr", path, KIND_VECTOR
    if ext in SINGLE_LAYER_RASTER:
        return "gdal", path, KIND_RASTER
    return None


_OGR_SEPARATORS = {",": "COMMA", ";": "SEMICOLON", "\t": "TAB", " ": "SPACE", "|": "PIPE"}


def csv_preview_uri(path, delimiter=",", x_field="", y_field="", wkt_field=""):
    """OGR (GDAL CSV driver) URI for fast read-only previews. It streams the
    file instead of indexing it, ~5x faster than the delimited-text provider."""
    uri = path + "|option:AUTODETECT_TYPE=YES"
    sep = _OGR_SEPARATORS.get(delimiter)
    if sep and not path.lower().endswith(".tsv"):
        uri += "|option:SEPARATOR=" + sep
    if wkt_field:
        uri += "|option:GEOM_POSSIBLE_NAMES=%s|option:KEEP_GEOM_COLUMNS=NO" % wkt_field
    elif x_field and y_field:
        uri += "|option:X_POSSIBLE_NAMES=%s|option:Y_POSSIBLE_NAMES=%s|option:KEEP_GEOM_COLUMNS=YES" % (
            x_field, y_field)
    return uri


def is_delimited_text(path):
    return os.path.splitext(path)[1].lower() in (".csv", ".tsv", ".txt")


def human_size(path):
    try:
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        else:
            total = os.path.getsize(path)
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024 or unit == "TB":
            return "%.0f %s" % (total, unit) if unit == "B" else "%.1f %s" % (total, unit)
        total /= 1024.0
    return ""


def dataset_size_bytes(path):
    try:
        if os.path.isdir(path):
            return sum(os.path.getsize(os.path.join(r, f))
                       for r, _d, fs in os.walk(path) for f in fs)
        return os.path.getsize(path)
    except OSError:
        return 0


def source_uri_for(path):
    """URI to hand to the provider registry (handles zip archives)."""
    if path.lower().endswith(".zip") and not path.startswith("/vsizip/"):
        return "/vsizip/" + path
    return path


_SUBLAYER_CACHE = {}
_SUBLAYER_LOCK = threading.Lock()
_SUBLAYER_TTL = 300.0


def _signature(path):
    try:
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size)
        if os.path.isdir(path):  # GDB: a table file changes, not the folder
            newest = 0.0
            with os.scandir(path) as it:
                for e in it:
                    if e.name.endswith((".gdbtable", ".gdbtablx")):
                        try:
                            newest = max(newest, e.stat().st_mtime)
                        except OSError:
                            pass
            sig = (st.st_mtime, newest)
        return sig
    except OSError:
        return None


def clear_sublayer_cache():
    with _SUBLAYER_LOCK:
        _SUBLAYER_CACHE.clear()


def forget_sublayers(path=None):
    """Drop the cached layer list of one dataset (or all), so the next look re-reads the file."""
    if path is None:
        clear_sublayer_cache()
        return
    with _SUBLAYER_LOCK:
        for key in [k for k in _SUBLAYER_CACHE if k[0] == path]:
            del _SUBLAYER_CACHE[key]


_DATASET_LOCKS = {}


def _dataset_lock(path):
    key = os.path.normcase(os.path.abspath(path.split("|")[0].replace("/vsizip/", "").rstrip("/\\")))
    with _SUBLAYER_LOCK:
        lock = _DATASET_LOCKS.get(key)
        if lock is None:
            lock = _DATASET_LOCKS[key] = threading.Lock()
        return lock


def query_sublayers(path, fast=False):
    """List of QgsProviderSublayerDetails for a path (thread-safe, cached).

    * Opening a big File Geodatabase or a remote file is the slowest step of
      browsing, so results are cached until the dataset changes on disk.
    * Two simultaneous QgsProviderRegistry.querySublayers() calls on the same
      File Geodatabase deadlock in QGIS (reproduced on 3.34), so queries are
      serialised per dataset; a second caller waits and reuses the result."""
    key = (path, bool(fast))

    def cached():
        sig = _signature(path)
        with _SUBLAYER_LOCK:
            hit = _SUBLAYER_CACHE.get(key)
            if hit is not None and hit[0] == sig and time.time() - hit[1] < _SUBLAYER_TTL:
                return list(hit[2])
        return None

    hit = cached()
    if hit is not None:
        return hit
    with _dataset_lock(path):
        hit = cached()  # another thread may have filled it while we waited
        if hit is not None:
            return hit
        sig = _signature(path)
        details = _query_sublayers(path, fast)
        with _SUBLAYER_LOCK:
            if len(_SUBLAYER_CACHE) > 500:
                _SUBLAYER_CACHE.clear()
            _SUBLAYER_CACHE[key] = (sig, time.time(), list(details))
    return details


def cached_sublayers(path):
    """Cached details if available, else None (never touches the disk)."""
    with _SUBLAYER_LOCK:
        hit = _SUBLAYER_CACHE.get((path, False))
    return list(hit[2]) if hit is not None else None


def _query_sublayers(path, fast=False):
    path = path.rstrip("/\\") if os.path.isdir(path) else path
    uri = source_uri_for(path)
    reg = QgsProviderRegistry.instance()
    try:
        from qgis.core import Qgis
        flags = Qgis.SublayerQueryFlag.FastScan if fast else Qgis.SublayerQueryFlags()
        details = reg.querySublayers(uri, flags)
    except Exception:  # very old QGIS
        details = reg.querySublayers(uri)
    if not details and path.lower().endswith(".zip"):
        # Some archives need explicit listing of their members
        details = reg.querySublayers(path)
    return list(details)


def kind_of_details(details, path=""):
    kind = PROVIDER_KIND.get(details.providerKey(), KIND_UNKNOWN)
    if kind == KIND_VECTOR and _is_null_geometry(details.wkbType()):
        return KIND_TABLE
    return kind


def _enum_value(e):
    return getattr(e, "value", e)


def _is_null_geometry(wkb):
    """True for attribute-only tables (works on QGIS 3.x and 4.x enums)."""
    try:
        if int(_enum_value(wkb)) == 100:  # WKB NoGeometry
            return True
        gt = QgsWkbTypes.geometryType(wkb)
        return "Null" in str(getattr(gt, "name", gt)) or int(_enum_value(gt)) == 4
    except Exception:
        return False


# --------------------------------------------------------------------------
# Delimited text (CSV) intelligence
# --------------------------------------------------------------------------

def sniff_csv(path, max_bytes=65536):
    """Detect delimiter, header and likely X/Y or WKT columns."""
    result = {"delimiter": ",", "fields": [], "x_field": "", "y_field": "",
              "wkt_field": "", "rows": []}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            sample = fh.read(max_bytes)
    except OSError:
        return result
    if not sample:
        return result
    try:
        dialect = csv.Sniffer().sniff(sample.split("\n", 20)[0] + "\n" +
                                      "\n".join(sample.split("\n")[1:20]),
                                      delimiters=",;\t|")
        result["delimiter"] = dialect.delimiter
    except csv.Error:
        if path.lower().endswith(".tsv") or sample.count("\t") > sample.count(","):
            result["delimiter"] = "\t"
        elif sample.count(";") > sample.count(","):
            result["delimiter"] = ";"
    reader = csv.reader(sample.splitlines(), delimiter=result["delimiter"])
    rows = list(reader)
    if not rows:
        return result
    fields = [f.strip() for f in rows[0]]
    result["fields"] = fields
    result["rows"] = rows[1:11]
    lower = {f.lower(): f for f in fields}
    for name in WKT_NAMES:
        if name in lower:
            result["wkt_field"] = lower[name]
            break
    if not result["wkt_field"]:
        for name in X_NAMES:
            if name in lower:
                result["x_field"] = lower[name]
                break
        for name in Y_NAMES:
            if name in lower:
                result["y_field"] = lower[name]
                break
        if not (result["x_field"] and result["y_field"]):
            result["x_field"] = result["y_field"] = ""
    return result


def delimited_text_uri(path, delimiter=",", x_field="", y_field="",
                       wkt_field="", crs="", detect_types=True, encoding="UTF-8", spatial_index=True):
    url = QUrl.fromLocalFile(path)
    query = QUrlQuery()
    query.addQueryItem("type", "csv")
    query.addQueryItem("delimiter", delimiter)
    query.addQueryItem("encoding", encoding or "UTF-8")
    query.addQueryItem("detectTypes", "yes" if detect_types else "no")
    query.addQueryItem("trimFields", "yes")
    query.addQueryItem("spatialIndex", "yes" if spatial_index else "no")
    query.addQueryItem("subsetIndex", "no")
    query.addQueryItem("watchFile", "no")
    if wkt_field:
        query.addQueryItem("wktField", wkt_field)
    elif x_field and y_field:
        query.addQueryItem("xField", x_field)
        query.addQueryItem("yField", y_field)
    else:
        query.addQueryItem("geomType", "none")
    if crs and (wkt_field or (x_field and y_field)):
        query.addQueryItem("crs", crs)
    url.setQuery(query)
    return bytes(url.toEncoded()).decode("utf-8")


# --------------------------------------------------------------------------
# Building layers
# --------------------------------------------------------------------------

def make_layer(provider, uri, name):
    if provider in ("ogr", "delimitedtext", "spatialite", "postgres", "memory"):
        return QgsVectorLayer(uri, name, provider)
    if provider == "gdal":
        return QgsRasterLayer(uri, name, provider)
    if provider in ("pdal", "copc", "ept"):
        return QgsPointCloudLayer(uri, name, provider)
    if provider == "mdal":
        return QgsMeshLayer(uri, name, provider)
    return QgsVectorLayer(uri, name, provider)


def layer_from_details(details):
    try:
        options = QgsProviderSublayerDetails.LayerOptions(
            QgsProject.instance().transformContext())
        layer = details.toLayer(options)
        if layer is not None:
            return layer
    except Exception as exc:
        ignore(exc)
    return make_layer(details.providerKey(), details.uri(), details.name())


def describe_layer(layer):
    """Return an ordered list of (label, value) for the inspector."""
    rows = []
    if layer is None or not layer.isValid():
        return [("Status", "Invalid layer - QGIS cannot read this source")]
    rows.append(("Provider", layer.providerType()))
    crs = layer.crs()
    if crs.isValid():
        rows.append(("CRS", "%s - %s" % (crs.authid() or "custom", crs.description())))
        try:
            rows.append(("Units", QgsUnitTypes_to_string(crs)))
        except Exception as exc:
            ignore(exc)
    else:
        rows.append(("CRS", "Unknown - use Interpret to assign one"))
    ext = layer.extent()
    if not ext.isEmpty():
        rows.append(("Extent", "%.4f, %.4f : %.4f, %.4f" % (
            ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())))
    if isinstance(layer, QgsVectorLayer):
        rows.append(("Geometry", QgsWkbTypes.displayString(layer.wkbType()) or "None (table)"))
        rows.append(("Features", "{:,}".format(layer.featureCount())))
        rows.append(("Fields", ", ".join(f.name() for f in layer.fields())[:400]))
        try:
            rows.append(("Encoding", layer.dataProvider().encoding()))
        except Exception as exc:
            ignore(exc)
    elif isinstance(layer, QgsRasterLayer):
        prov = layer.dataProvider()
        rows.append(("Size", "%d x %d px, %d band(s)" % (
            layer.width(), layer.height(), layer.bandCount())))
        rows.append(("Pixel size", "%.6g x %.6g" % (
            layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY())))
        dtypes = []
        nodata = []
        for b in range(1, layer.bandCount() + 1):
            try:
                dtypes.append(_dtype_name(prov.dataType(b)))
            except Exception as exc:
                ignore(exc)
            if prov.sourceHasNoDataValue(b):
                nodata.append(str(prov.sourceNoDataValue(b)))
        if dtypes:
            rows.append(("Data type", ", ".join(sorted(set(dtypes)))))
        rows.append(("NoData", ", ".join(sorted(set(nodata))) or "not set"))
        try:
            rows.append(("Overviews", "yes" if prov.hasPyramids() else "none (slow at small scales)"))
        except Exception as exc:
            ignore(exc)
    elif isinstance(layer, QgsPointCloudLayer):
        try:
            rows.append(("Points", "{:,}".format(layer.pointCount())))
        except Exception as exc:
            ignore(exc)
    md = layer.metadata()
    if md.title():
        rows.append(("Title", md.title()))
    if md.abstract():
        rows.append(("Abstract", md.abstract()[:400]))
    return rows


def QgsUnitTypes_to_string(crs):
    from qgis.core import QgsUnitTypes
    return QgsUnitTypes.toString(crs.mapUnits())


def _dtype_name(dtype):
    text = str(dtype)
    return text.split(".")[-1]


def crs_from_text(text):
    crs = QgsCoordinateReferenceSystem(text) if text else QgsCoordinateReferenceSystem()
    return crs

"""Background tasks that keep the QGIS interface responsive.

Rule of thumb applied across the plugin: anything that touches the disk or
iterates features (opening layers, discovering sublayers, computing extents,
reading attribute rows, scanning for unique values, evaluating selection
expressions, writing exports) runs in a QgsTask. The GUI thread only fills
widgets with the results.

Layers created in a worker are moved to the main thread before the task
finishes (QObject.moveToThread), the pattern QGIS itself uses for layers
loaded by background Processing algorithms.
"""

import os

from qgis.core import (
    QgsApplication,
    QgsExpression,
    QgsFeatureRequest,
    QgsMapLayer,
    QgsMessageLog,
    QgsProviderSublayerDetails,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
)

from . import compat
from . import inspector
from . import loader
from .compat import ignore

LOG_TAG = "Data Explorer"


def _flags(hidden=True):
    flags = QgsTask.Flag.CanCancel
    h = getattr(QgsTask.Flag, "Hidden", None)
    if hidden and h is not None:
        flags = flags | h
    return flags


def to_main_thread(layer):
    """Hand a layer created in a worker thread to the GUI thread."""
    if layer is not None:
        layer.moveToThread(QgsApplication.instance().thread())
    return layer


class _Disposer:
    """Destroys layers on a dedicated thread.

    QGIS's OGR provider guards opening/closing datasets with one global lock,
    so deleting a layer on the GUI thread blocks while any background task is
    still opening another OGR dataset (a big GeoJSON or a GDB on a network
    share can take many seconds). Layers we drop are moved here instead."""

    def __init__(self):
        self.thread = None

    def _ensure(self):
        if self.thread is None:
            from qgis.PyQt.QtCore import QThread
            self.thread = QThread()
            self.thread.setObjectName("KgaDataIngestDisposer")
            self.thread.start()  # runs an event loop for deleteLater()
        return self.thread

    def dispose(self, obj):
        if obj is None:
            return
        try:
            from qgis.PyQt import sip
            if sip.isdeleted(obj):
                return
            obj.moveToThread(self._ensure())
            sip.transferto(obj, None)  # C++ now owns it; Python won't delete it here
            obj.deleteLater()          # runs in the disposer thread's event loop
        except Exception as exc:
            QgsMessageLog.logMessage("Could not dispose object: %s" % exc, LOG_TAG)

    def shutdown(self):
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(5000)
            self.thread = None


DISPOSER = _Disposer()


def dispose(obj):
    DISPOSER.dispose(obj)


def set_read_only(layer):
    """Non-destructive previews: vector layers can't be put in edit mode."""
    if isinstance(layer, QgsVectorLayer):
        layer.setReadOnly(True)
    return layer


class _Task(QgsTask):
    def __init__(self, description, on_done, hidden=True):
        super().__init__(description, _flags(hidden))
        self.on_done = on_done
        self.error = ""

    def finished(self, result):  # GUI thread
        if self.on_done is not None:
            try:
                self.on_done(self)
            except Exception as exc:  # never let a callback kill the task manager
                QgsMessageLog.logMessage("Task callback failed: %s" % exc, LOG_TAG)


# ---------------------------------------------------------------- sublayers
class SublayerTask(_Task):
    """Discovers the layers inside a file/container (GDB, GPKG, ...)."""

    def __init__(self, path, on_done, payload=None):
        super().__init__("Reading %s" % os.path.basename(path.rstrip("/\\")), on_done)
        self.path = path
        self.payload = payload
        self.details = []

    def run(self):
        try:
            self.details = inspector.query_sublayers(self.path)
        except Exception as exc:
            self.error = str(exc)
        return True


class PrepareImportTask(_Task):
    """Resolves sublayers for everything the user asked to import."""

    def __init__(self, datas, on_done, context=None):
        super().__init__("Preparing import", on_done, hidden=len(datas) < 20)
        self.datas = datas
        self.context = context or {}
        self.details = {}  # index -> list of details (None for CSV / resolved items)

    def run(self):
        n = max(len(self.datas), 1)
        for i, data in enumerate(self.datas):
            if self.isCanceled():
                return False
            if data.get("details") is not None or inspector.is_delimited_text(data["path"]):
                self.details[i] = None
            elif not data.get("layer_name") and inspector.direct_source(data["path"]) is not None:
                self.details[i] = "direct"  # single-layer file: no scan needed
            else:
                try:
                    details = inspector.query_sublayers(data["path"])
                except Exception:
                    details = []
                if data.get("layer_name"):
                    wanted = data["layer_name"].casefold()
                    match = [d for d in details if (d.name() or "").casefold() == wanted]
                    if match:
                        data["details"] = match[0]
                        self.details[i] = None
                        continue
                self.details[i] = details
            self.setProgress(100.0 * (i + 1) / n)
        return True


class FolderScanTask(_Task):
    """Walks a folder tree for datasets (slow on network drives)."""

    def __init__(self, root, on_done):
        super().__init__("Scanning %s" % os.path.basename(root.rstrip("/\\")), on_done, hidden=False)
        self.root = root
        self.paths = []

    def run(self):
        from .search_index import _walk
        for p in _walk(self.root, self.isCanceled):
            self.paths.append(p)
        self.paths.sort()
        return True


# ------------------------------------------------------------------ preview
class PreviewTask(_Task):
    """Opens one dataset and gathers everything the inspector shows."""

    def __init__(self, data, transform_context, on_done, gen=0, attr_limit=50):
        super().__init__("Preview %s" % data.get("name", ""), on_done)
        self.data = dict(data)
        self.context = transform_context
        self.gen = gen
        self.attr_limit = attr_limit
        self.layer = None
        self.rows = []
        self.attr_fields = []
        self.attr_rows = []
        self.note = ""

    def _open(self):
        d = self.data
        if d["kind"] == inspector.KIND_POINTCLOUD:
            self.note = "pointcloud"
            return None
        details = d.get("details")
        if details is None and d.get("layer_name"):
            wanted = d["layer_name"].casefold()
            for sd in inspector.query_sublayers(d["path"]):
                if (sd.name() or "").casefold() == wanted:
                    details = sd
                    break
        if details is not None:
            return self._from_details(details)
        if d.get("preview_uri"):
            layer = inspector.make_layer(d.get("preview_provider", "delimitedtext"),
                                         d["preview_uri"], d["name"])
            if d.get("preview_crs") and layer is not None and layer.isValid() and layer.isSpatial():
                from qgis.core import QgsCoordinateReferenceSystem
                layer.setCrs(QgsCoordinateReferenceSystem(d["preview_crs"]))
            return layer
        direct = inspector.direct_source(d["path"])
        if direct is not None:
            layer = inspector.make_layer(direct[0], direct[1], d["name"])
            if layer is not None and layer.isValid():
                return layer
        for sd in inspector.query_sublayers(d["path"]):
            if self.isCanceled():
                return None
            layer = self._from_details(sd)
            if layer is not None and layer.isValid():
                return layer
        return None

    def _from_details(self, details):
        try:
            options = QgsProviderSublayerDetails.LayerOptions(self.context)
            options.loadDefaultStyle = False
            layer = details.toLayer(options)
            if layer is not None:
                return layer
        except Exception as exc:
            ignore(exc)
        return inspector.make_layer(details.providerKey(), details.uri(), details.name())

    def run(self):
        d = self.data
        try:
            self.rows = [("File", d["path"]), ("Size on disk", inspector.human_size(d["path"]))]
            layer = self._open()
            if self.isCanceled():
                return False
            if layer is None:
                return True
            set_read_only(layer)
            self.rows += inspector.describe_layer(layer)  # extent/count computed here
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and self.attr_limit:
                fields = layer.fields()
                self.attr_fields = [f.name() for f in fields]
                req = QgsFeatureRequest()
                req.setLimit(self.attr_limit)
                req.setFlags(compat.request_flag("NoGeometry"))
                for feat in layer.getFeatures(req):
                    self.attr_rows.append(["" if v is None else str(v) for v in feat.attributes()])
            self.layer = to_main_thread(layer)
        except Exception as exc:
            self.error = str(exc)
        return True


# -------------------------------------------------------------- loading
class LoadLayersTask(_Task):
    """Builds layers for 'link in place' imports without blocking QGIS."""

    def __init__(self, results, settings, on_done):
        super().__init__("Data Explorer: loading %d layer(s)" % len(results), on_done,
                         hidden=len(results) < 3)
        self.results = results
        self.settings = settings
        self.errors = []

    def run(self):
        n = max(len(self.results), 1)
        for i, res in enumerate(self.results):
            if self.isCanceled():
                return False
            try:
                layer, err = loader.build_layer(res, self.settings)
            except Exception as exc:
                layer, err = None, str(exc)
            res["layer"] = to_main_thread(layer)
            res["error"] = err
            self.setProgress(100.0 * (i + 1) / n)
        return True


# --------------------------------------------- feature-source based tasks
class UniqueValuesTask(_Task):
    """Distinct values of one field, read from a thread-safe feature source."""

    def __init__(self, source, field_index, on_done, limit=1000, max_features=2000000):
        super().__init__("Reading values", on_done)
        self.source = source
        self.field_index = field_index
        self.limit = limit
        self.max_features = max_features
        self.values = []
        self.truncated = False

    def run(self):
        seen = set()
        req = QgsFeatureRequest()
        req.setFlags(compat.request_flag("NoGeometry"))
        req.setSubsetOfAttributes([self.field_index])
        for i, feat in enumerate(self.source.getFeatures(req)):
            if self.isCanceled():
                return False
            v = feat.attribute(self.field_index)
            if v is None:
                continue
            seen.add(v if isinstance(v, (int, float, str)) else str(v))
            if len(seen) >= self.limit or i >= self.max_features:
                self.truncated = True
                break
        try:
            self.values = sorted(seen)
        except TypeError:
            self.values = sorted(seen, key=str)
        return True


class ExpressionSelectTask(_Task):
    """Evaluates a filter expression over a feature source, returns ids."""

    def __init__(self, source, expression, context, on_done, rect=None):
        super().__init__("Selecting features", on_done)
        self.source = source
        self.expression = expression
        self.context = context
        self.rect = rect
        self.ids = []

    def run(self):
        req = QgsFeatureRequest()
        if self.expression:
            exp = QgsExpression(self.expression)
            if exp.hasParserError():
                self.error = exp.parserErrorString()
                return True
            req.setFilterExpression(self.expression)
            req.setExpressionContext(self.context)
            cols = exp.referencedColumns()
            fields = self.source_fields()
            if fields is not None and QgsFeatureRequest.ALL_ATTRIBUTES not in cols:
                req.setSubsetOfAttributes(cols, fields)
            if not exp.needsGeometry() and self.rect is None:
                req.setFlags(compat.request_flag("NoGeometry"))
        else:
            req.setNoAttributes()
        if self.rect is not None:
            req.setFilterRect(self.rect)
            req.setFlags(req.flags() | compat.request_flag("ExactIntersect"))
        try:
            for i, feat in enumerate(self.source.getFeatures(req)):
                if i % 2000 == 0 and self.isCanceled():
                    return False
                self.ids.append(feat.id())
        except Exception as exc:
            self.error = str(exc)
        return True

    def source_fields(self):
        try:
            return self.source.fields()
        except Exception:
            return None


def crs_for_gdal(crs):
    """A CRS string GDAL always accepts: 'EPSG:xxxx' when known, else PROJ."""
    auth = crs.authid()
    return auth if auth else crs.toProj()


class RasterExportTask(_Task):
    """Writes the visible part of a raster to a NEW GeoTIFF (gdal_translate)."""

    def __init__(self, src, dst, bbox, on_done, cutline=None):
        super().__init__("Exporting raster view", on_done, hidden=False)
        self.src, self.dst, self.bbox = src, dst, bbox
        self.cutline = cutline  # (polygon WKT, CRS WKT): mask the pixels outside the polygon

    def run(self):
        try:
            from osgeo import gdal
            xmin, ymin, xmax, ymax = self.bbox

            def cb(pct, _msg, _data):
                self.setProgress(pct * 100)
                return 0 if self.isCanceled() else 1
            creation = ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"]
            if self.cutline:
                wkt, crs_wkt = self.cutline
                ds = gdal.Open(self.src)
                nodata = ds.GetRasterBand(1).GetNoDataValue() if ds is not None else None
                ds = None
                extra = {"dstNodata": nodata} if nodata is not None else {"dstAlpha": True}
                out = gdal.Warp(self.dst, self.src, format="GTiff", cutlineWKT=wkt, cutlineSRS=crs_wkt,
                                cropToCutline=True, creationOptions=creation, callback=cb, **extra)
            else:
                out = gdal.Translate(self.dst, self.src, projWin=[xmin, ymax, xmax, ymin], format="GTiff",
                                     creationOptions=creation, callback=cb)
            if out is None:
                self.error = "GDAL could not write %s" % self.dst
            out = None
        except Exception as exc:
            self.error = str(exc)
        return True


def is_vector(layer):
    return isinstance(layer, QgsVectorLayer) or (
        isinstance(layer, QgsMapLayer) and layer.type() == compat.layer_type("Vector"))


def is_raster(layer):
    return isinstance(layer, QgsRasterLayer)

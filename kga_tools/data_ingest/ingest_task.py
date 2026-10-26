"""Background ingest (the equivalent of Premiere Pro handing jobs to
Adobe Media Encoder): copy, convert ("transcode") and proxy creation run
in a QgsTask so the user can keep working while data is prepared.

Nothing in run() touches the project or the GUI - only files and layers
created inside the worker thread. Adding layers happens in finished(),
which QGIS calls back on the main thread.
"""

import os
import re
import shutil

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import compat
from . import inspector
from . import presets as P

try:
    from osgeo import gdal
except ImportError:  # pragma: no cover - GDAL always ships with QGIS
    gdal = None

LOG_TAG = "Data Explorer"

VECTOR_DRIVERS = {
    "GPKG": ".gpkg",
    "ESRI Shapefile": ".shp",
    "FlatGeobuf": ".fgb",
    "GeoJSON": ".geojson",
    "Parquet": ".parquet",
}


def safe_name(text):
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text or "layer"


def unique_path(folder, filename):
    base, ext = os.path.splitext(filename)
    if filename.lower().endswith(".shp.xml"):
        base, ext = filename[:-8], ".shp.xml"
    candidate = os.path.join(folder, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, "%s_%d%s" % (base, i, ext))
        i += 1
    return candidate


def sidecars_for(path):
    folder, fname = os.path.split(path)
    stem, ext = os.path.splitext(fname)
    ext = ext.lower()
    wanted = set(P_SIDECAR(ext))
    out = []
    try:
        for other in os.listdir(folder):
            if other == fname:
                continue
            lower = other.lower()
            if lower.startswith(stem.lower() + ".") or lower.startswith(fname.lower() + "."):
                suffix = other[len(stem):].lower()
                suffix2 = other[len(fname):].lower()
                if suffix in wanted or suffix2 in wanted:
                    out.append(os.path.join(folder, other))
    except OSError:
        pass
    return out


def P_SIDECAR(ext):
    return inspector.SIDECAR_EXT.get(ext, []) + inspector.SIDECAR_EXT["*"]


class IngestTask(QgsTask):
    """Processes a list of ingest jobs with one set of ingest settings."""

    def __init__(self, jobs, settings, destination, transform_context,
                 project_crs_wkt, on_done):
        super().__init__("Data Explorer: %d item(s)" % len(jobs),
                         QgsTask.Flag.CanCancel)
        self.jobs = jobs
        self.settings = settings
        self.destination = destination
        self.context = transform_context
        self.project_crs = QgsCoordinateReferenceSystem.fromWkt(project_crs_wkt) \
            if project_crs_wkt else QgsCoordinateReferenceSystem()
        self.on_done = on_done
        self.results = []
        self.errors = []
        self._copied = {}      # source path -> copied path
        self._vector_out = {}  # source path -> output container (GPKG)
        self._job_index = 0

    # ------------------------------------------------------------------ util
    def _log(self, msg):
        QgsMessageLog.logMessage(msg, LOG_TAG)

    def _progress(self, fraction):
        n = max(len(self.jobs), 1)
        self.setProgress(100.0 * (self._job_index + max(0.0, min(fraction, 1.0))) / n)

    def _gdal_callback(self, complete, _message, _data):
        self._progress(complete)
        return 0 if self.isCanceled() else 1

    def _subdir(self, name):
        path = os.path.join(self.destination, name)
        os.makedirs(path, exist_ok=True)
        return path

    # ------------------------------------------------------------------ run
    def run(self):
        os.makedirs(self.destination, exist_ok=True)
        mode = self.settings["mode"]
        for i, job in enumerate(self.jobs):
            self._job_index = i
            if self.isCanceled():
                return False
            try:
                self.results.append(self._process(job, mode))
            except Exception as exc:  # keep going with the other items
                msg = "%s: %s" % (job.get("name"), exc)
                self.errors.append(msg)
                self._log(msg)
        # open the resulting layers here too, so the GUI thread only adds them
        from . import loader
        from .tasks import to_main_thread
        for res in self.results:
            if self.isCanceled():
                break
            try:
                layer, err = loader.build_layer(res, self.settings)
            except Exception as exc:
                layer, err = None, str(exc)
            res["layer"] = to_main_thread(layer)
            res["error"] = err
        self.setProgress(100)
        return True

    def _process(self, job, mode):
        res = {
            "job": job,
            "provider": job["provider"],
            "uri": job["uri"],
            "full_uri": None,
            "proxy_uri": None,
            "notes": [],
        }
        kind = job["kind"]

        if mode in (P.MODE_COPY, P.MODE_COPY_PROXY):
            res["uri"] = self._copy(job)
            res["notes"].append("Copied to %s" % self.destination)

        if mode in (P.MODE_CONVERT, P.MODE_CONVERT_PROXY):
            if kind in (inspector.KIND_VECTOR, inspector.KIND_TABLE):
                res["provider"], res["uri"] = self._convert_vector(job)
                res["notes"].append("Converted to %s" % self.settings["vector_format"])
            elif kind == inspector.KIND_RASTER:
                res["uri"] = self._convert_raster(job)
                res["notes"].append("Converted to %s" % self.settings["raster_format"])
            else:
                res["uri"] = self._copy(job)
                res["notes"].append("No conversion available for %s - copied instead" % kind)

        if mode in (P.MODE_PROXY, P.MODE_COPY_PROXY, P.MODE_CONVERT_PROXY):
            self._make_proxies(job, res, mode)

        if self.isCanceled():
            raise RuntimeError("cancelled")
        return res

    # ------------------------------------------------------------------ copy
    def _copy(self, job):
        src = job["source_path"]
        if src in self._copied:
            new_path = self._copied[src]
        else:
            folder = self._subdir("Media")
            new_path = unique_path(folder, os.path.basename(src.rstrip("/\\")))
            if os.path.isdir(src):
                shutil.copytree(src, new_path)
            else:
                self._copy_file(src, new_path)
                stem_new = os.path.splitext(os.path.basename(new_path))[0]
                stem_old = os.path.splitext(os.path.basename(src))[0]
                for side in sidecars_for(src):
                    side_name = os.path.basename(side)
                    target = os.path.join(folder, stem_new + side_name[len(stem_old):])
                    if not os.path.exists(target):
                        shutil.copy2(side, target)
            self._copied[src] = new_path
        return job["uri"].replace(src, new_path)

    def _copy_file(self, src, dst, chunk=8 * 1024 * 1024):
        total = max(os.path.getsize(src), 1)
        done = 0
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if self.isCanceled():
                    raise RuntimeError("cancelled")
                buf = fin.read(chunk)
                if not buf:
                    break
                fout.write(buf)
                done += len(buf)
                self._progress(0.9 * done / total)
        shutil.copystat(src, dst)

    # --------------------------------------------------------------- vectors
    def _target_crs(self):
        if self.settings.get("reproject") and self.project_crs.isValid():
            return self.project_crs
        return None

    def _convert_vector(self, job):
        driver = self.settings.get("vector_format", "GPKG")
        ext = VECTOR_DRIVERS.get(driver, ".gpkg")
        folder = self._subdir("Converted")
        src = job["source_path"]
        layer_name = safe_name(job.get("sublayer_name") or job["name"])

        layer = inspector.make_layer(job["provider"], job["uri"], job["name"])
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            raise RuntimeError("cannot open vector source")
        ov = job.get("overrides", {})
        if ov.get("encoding"):
            layer.setProviderEncoding(ov["encoding"])
        if ov.get("crs"):
            layer.setCrs(QgsCoordinateReferenceSystem(ov["crs"]))

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver
        options.fileEncoding = "UTF-8"
        options.layerName = layer_name

        if driver == "GPKG":
            if src in self._vector_out:
                out_path = self._vector_out[src]
                options.actionOnExistingFile = \
                    compat.writer_action("CreateOrOverwriteLayer")
            else:
                stem = safe_name(os.path.splitext(os.path.basename(src.rstrip("/\\")))[0])
                out_path = unique_path(folder, stem + ext)
                self._vector_out[src] = out_path
        else:
            stem = safe_name(os.path.splitext(os.path.basename(src.rstrip("/\\")))[0])
            name = stem if stem == layer_name else "%s_%s" % (stem, layer_name)
            out_path = unique_path(folder, name + ext)

        target = self._target_crs()
        if target is not None and layer.crs().isValid() and layer.crs() != target:
            options.ct = QgsCoordinateTransform(layer.crs(), target, self.context)

        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, out_path, self.context, options)
        err, msg = result[0], result[1]
        if compat.enum_int(err) != 0:  # WriterError.NoError
            raise RuntimeError("vector conversion failed: %s" % msg)
        written = result[2] if len(result) > 2 and result[2] else out_path

        if driver == "GPKG":
            return "ogr", "%s|layername=%s" % (written, layer_name)
        return "ogr", written

    # --------------------------------------------------------------- rasters
    def _raster_creation(self, fmt):
        comp = self.settings.get("raster_compress", "DEFLATE")
        opts = ["BIGTIFF=IF_SAFER"]
        if comp and comp != "NONE":
            opts.append("COMPRESS=%s" % comp)
        if fmt == "GTiff":
            opts += ["TILED=YES"]
        return opts

    def _nodata_override(self, job):
        value = job.get("overrides", {}).get("nodata")
        return None if value in (None, "") else float(value)

    def _convert_raster(self, job):
        if gdal is None:
            raise RuntimeError("GDAL Python bindings not available")
        fmt = self.settings.get("raster_format", "COG")
        if fmt == "COG" and gdal.GetDriverByName("COG") is None:
            fmt = "GTiff"
        folder = self._subdir("Converted")
        stem = safe_name(job.get("sublayer_name") or job["name"])
        out_path = unique_path(folder, stem + ".tif")

        src_ds = gdal.Open(job["uri"])
        if src_ds is None:
            raise RuntimeError("GDAL cannot open %s" % job["uri"])

        ov = job.get("overrides", {})
        target = self._target_crs()
        if target is not None:
            warp_kwargs = dict(format="VRT", dstSRS=target.toWkt(), resampleAlg="bilinear")
            if ov.get("crs"):
                warp_kwargs["srcSRS"] = QgsCoordinateReferenceSystem(ov["crs"]).toWkt()
            src_ds = gdal.Warp("", src_ds, **warp_kwargs)
            if src_ds is None:
                raise RuntimeError("reprojection failed")

        kwargs = dict(format=fmt, creationOptions=self._raster_creation(fmt),
                      callback=self._gdal_callback)
        nodata = self._nodata_override(job)
        if nodata is not None:
            kwargs["noData"] = nodata
        if ov.get("crs") and target is None:
            kwargs["outputSRS"] = QgsCoordinateReferenceSystem(ov["crs"]).toWkt()
        out = gdal.Translate(out_path, src_ds, **kwargs)
        if out is None:
            raise RuntimeError("raster conversion failed")
        out = None  # flush
        if fmt == "GTiff" and self.settings.get("build_overviews"):
            self._build_overviews(out_path)
        return out_path

    def _build_overviews(self, path, external=False):
        ds = gdal.Open(path, gdal.GA_ReadOnly if external else gdal.GA_Update)
        if ds is None:
            return False
        w, h = ds.RasterXSize, ds.RasterYSize
        levels, f = [], 2
        while max(w, h) / f >= 256:
            levels.append(f)
            f *= 2
        if levels:
            if external:
                gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
            ds.BuildOverviews("AVERAGE", levels, callback=self._gdal_callback)
        ds = None
        return True

    # --------------------------------------------------------------- proxies
    def _make_proxies(self, job, res, mode):
        kind = job["kind"]
        if kind == inspector.KIND_RASTER and gdal is not None:
            proxy_dir = self._subdir("Proxies")
            stem = safe_name(job.get("sublayer_name") or job["name"])
            current = res["uri"]

            # 1. Overviews ("pyramids"): fast display at every scale.
            if self.settings.get("build_overviews"):
                if mode == P.MODE_PROXY:
                    # Never modify the user's originals: wrap them in a VRT
                    # inside the proxy folder and build overviews on that.
                    vrt_path = unique_path(proxy_dir, stem + "_fullres.vrt")
                    vrt = gdal.Translate(vrt_path, current, format="VRT")
                    vrt = None
                    if self._build_overviews(vrt_path, external=True):
                        res["uri"] = vrt_path
                        res["notes"].append("Overviews built (originals untouched)")
                elif not str(current).lower().endswith(".tif") or \
                        self.settings.get("raster_format") != "COG":
                    if self._build_overviews(current, external=False):
                        res["notes"].append("Overviews built")

            # 2. Low-resolution proxy, toggled with "Toggle Proxies".
            pct = int(self.settings.get("proxy_raster_percent") or 0)
            if 0 < pct < 100:
                proxy_path = unique_path(proxy_dir, "%s_proxy%d.tif" % (stem, pct))
                out = gdal.Translate(
                    proxy_path, res["uri"], format="GTiff",
                    widthPct=pct, heightPct=pct, resampleAlg="average",
                    creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
                    callback=self._gdal_callback)
                if out is not None:
                    out = None
                    res["full_uri"] = res["uri"]
                    res["proxy_uri"] = proxy_path
                    res["notes"].append("%d%% proxy created" % pct)

        elif kind in (inspector.KIND_VECTOR, inspector.KIND_TABLE):
            if not self.settings.get("spatial_index") or mode == P.MODE_PROXY:
                return  # don't write index files next to originals
            path = res["uri"].split("|")[0]
            if path.lower().endswith(".shp"):
                lyr = QgsVectorLayer(res["uri"], "tmp", "ogr")
                if lyr.isValid() and lyr.dataProvider().createSpatialIndex():
                    res["notes"].append("Spatial index (.qix) built")

    # -------------------------------------------------------------- finished
    def finished(self, result):
        if self.on_done is not None:
            self.on_done(self, bool(result))

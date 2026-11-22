# -*- coding: utf-8 -*-
"""
Background QgsTasks: scene search, thumbnail fetching and downloading.

``run()`` executes in a worker thread and never touches widgets; results are
handed back through Qt signals which are delivered on the main thread.
"""

import json
import os
import re
import shutil
import tempfile
import traceback
from datetime import datetime, timezone

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .compat import MSG_CRITICAL, MSG_WARNING, TASK_CAN_CANCEL, log
from . import http, quality, raster
from .providers import SearchCanceled, compute_aoi_coverage

PLUGIN_VERSION = "1.1.0"


def friendly_error(exc, provider_name=""):
    """Map an exception to a (title, message) pair suitable for a message box."""
    src = provider_name or "the imagery server"
    if isinstance(exc, http.HttpError):
        if exc.bad_response:
            return ("Unexpected server response",
                    "{} answered, but not with valid data – it may be under maintenance or the URL "
                    "may be wrong. Try again later or check the catalog URL in Settings.\n\n{}".format(
                        src, exc.message))
        if exc.is_rate_limited:
            return ("Rate limit reached",
                    "{} is receiving too many requests from you (HTTP 429).\n\n"
                    "Wait a minute and try again, request fewer results, or – for Microsoft "
                    "Planetary Computer – add a free subscription key in Settings.".format(src))
        if exc.is_auth_error:
            return ("Access denied – API key or login needed",
                    "{} refused the request (HTTP {}).\n\nCheck the API key / project id in "
                    "Settings, or that the collection is public.\n\nServer said: {}".format(
                        src, exc.status, exc.message))
        if exc.status == 0:
            return ("Network problem",
                    "Could not reach {}.\n\nCheck your internet connection and the proxy "
                    "settings in QGIS (Settings ▸ Options ▸ Network).\n\nDetails: {}".format(
                        src, exc.message))
        if exc.status >= 500:
            return ("Server error",
                    "{} returned a server error (HTTP {}). The service may be temporarily "
                    "unavailable – please try again later.\n\n{}".format(src, exc.status, exc.message))
        return ("Request rejected", "{} rejected the request (HTTP {}):\n\n{}".format(
            src, exc.status, exc.message))
    if isinstance(exc, raster.OutputTooLarge):
        return ("Output too large", str(exc))
    if isinstance(exc, RuntimeError):
        return ("Error", str(exc))
    return ("Unexpected error", "{}: {}".format(type(exc).__name__, exc))


# ---------------------------------------------------------------------------
class SearchTask(QgsTask):
    searchFinished = pyqtSignal(list)       # list[Scene]
    searchFailed = pyqtSignal(str, str)     # title, message
    searchCanceled = pyqtSignal()

    def __init__(self, provider, aoi, start, end, max_cloud, limit, variant,
                 min_coverage=0.0, sort_mode="newest"):
        super().__init__("Satellite imagery archive search – {}".format(provider.name), TASK_CAN_CANCEL)
        self.provider = provider
        self.aoi = aoi
        self.start = start
        self.end = end
        self.max_cloud = max_cloud
        self.limit = limit
        self.variant = variant
        self.min_coverage = min_coverage
        self.sort_mode = sort_mode
        self.scenes = []
        self.exception = None

    def run(self):
        try:
            self.setProgress(5)
            scenes = self.provider.search(self.aoi, self.start, self.end, self.max_cloud,
                                          self.limit, self.variant, self.isCanceled)
            self.setProgress(80)
            compute_aoi_coverage(scenes, self.aoi)
            if self.min_coverage > 0:
                scenes = [s for s in scenes
                          if s.aoi_coverage is None or s.aoi_coverage >= self.min_coverage]
            if self.sort_mode == "oldest":
                scenes.sort(key=lambda s: s.datetime)
            elif self.sort_mode == "cloud":
                scenes.sort(key=lambda s: (s.cloud_cover if s.cloud_cover is not None else 999,
                                           s.datetime))
            elif self.sort_mode == "coverage":
                scenes.sort(key=lambda s: -(s.aoi_coverage or 0))
            else:
                scenes.sort(key=lambda s: s.datetime, reverse=True)
            self.scenes = scenes
            self.setProgress(100)
            return True
        except SearchCanceled:
            return False
        except Exception as exc:  # noqa: BLE001
            self.exception = exc
            log("Search failed:\n" + traceback.format_exc(), MSG_CRITICAL)
            return False

    def finished(self, result):
        if result:
            self.searchFinished.emit(self.scenes)
        elif self.exception is not None:
            title, msg = friendly_error(self.exception, self.provider.name)
            self.searchFailed.emit(title, msg)
        else:
            self.searchCanceled.emit()


# ---------------------------------------------------------------------------
class ThumbnailTask(QgsTask):
    thumbnailReady = pyqtSignal(str, bytes)  # scene uid, image bytes (emitted from worker)

    def __init__(self, provider, scenes):
        super().__init__("Satellite imagery archive thumbnails", TASK_CAN_CANCEL)
        self.provider = provider
        self.scenes = list(scenes)

    def run(self):
        total = max(len(self.scenes), 1)
        for i, scene in enumerate(self.scenes):
            if self.isCanceled():
                return False
            try:
                url = self.provider.get_thumbnail_url(scene)
                if url:
                    data = http.get_bytes(url, timeout=30)
                    if data:
                        self.thumbnailReady.emit(scene.uid, data)
            except Exception as exc:  # noqa: BLE001 - a missing thumbnail is not fatal
                log("Thumbnail unavailable for {}: {}".format(scene.scene_id, exc), MSG_WARNING)
            self.setProgress(100.0 * (i + 1) / total)
        return True

    def finished(self, result):
        pass


# ---------------------------------------------------------------------------
def _slug(text, maxlen=40):
    text = re.sub(r"\(.*?\)", "", text or "")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text[:maxlen] or "img"


def _unique(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("{}_{}{}".format(root, n, ext)):
        n += 1
    return "{}_{}{}".format(root, n, ext)


class DownloadTask(QgsTask):
    statusChanged = pyqtSignal(str)                    # emitted from worker thread
    downloadFinished = pyqtSignal(list, list, bool)    # outputs, errors, canceled

    def __init__(self, provider, scenes, preset, variant, aoi, res_kind, res_value, res_label,
                 fmt, clip_to_polygon, out_dir, write_sidecar, max_megapixels):
        super().__init__("Satellite imagery archive download ({} scene{})".format(
            len(scenes), "" if len(scenes) == 1 else "s"), TASK_CAN_CANCEL)
        self.provider = provider
        self.scenes = list(scenes)
        self.preset = preset
        self.variant = variant
        self.aoi = aoi
        self.res_kind = res_kind
        self.res_value = res_value
        self.res_label = res_label
        self.fmt = fmt
        self.clip = clip_to_polygon
        self.out_dir = out_dir
        self.write_sidecar = write_sidecar
        self.max_mp = max_megapixels
        self.outputs = []  # dicts: path, name, scene, info
        self.errors = []   # (scene_id, title, message)

    def run(self):
        n = len(self.scenes)
        workdir = tempfile.mkdtemp(prefix="hid_")
        try:
            try:
                os.makedirs(self.out_dir, exist_ok=True)
            except OSError as exc:
                self.errors.append(("", "Output folder", "Cannot create output folder:\n{}".format(exc)))
                return False
            for i, scene in enumerate(self.scenes):
                if self.isCanceled():
                    return False
                self.statusChanged.emit("Downloading {}/{} – {} {}".format(
                    i + 1, n, scene.date, scene.scene_id))
                base = 100.0 * i / n

                def progress(pct, base=base):
                    self.setProgress(min(99.9, base + pct / n))

                try:
                    out = self._process_scene(scene, workdir, progress)
                    self.outputs.append(out)
                except (raster.ProcessingCanceled, SearchCanceled):
                    return False
                except Exception as exc:  # noqa: BLE001
                    title, msg = friendly_error(exc, self.provider.name)
                    self.errors.append((scene.scene_id, title, msg))
                    log("Download failed for {}:\n{}".format(scene.scene_id, traceback.format_exc()),
                        MSG_CRITICAL)
            self.setProgress(100)
            return True
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _process_scene(self, scene, workdir, progress):
        sources, processed = self.provider.prepare_sources(
            scene, self.preset, self.variant, self.aoi, self.res_kind, self.res_value,
            workdir, self.isCanceled)
        name = "{}_{}_{}_{}".format(self.provider.short, scene.date.replace("-", ""),
                                    _slug(scene.scene_id, 60), _slug(self.preset, 24))
        out_path = _unique(os.path.join(self.out_dir, name + ".tif"))
        meta = {
            "scene_id": scene.scene_id,
            "acquisition_date": scene.datetime,
            "satellite": scene.platform,
            "cloud_cover": scene.cloud_cover,
            "provider": self.provider.name,
            "band_combination": self.preset,
        }
        info = raster.build_raster(
            sources, self.aoi, out_path, res_kind=self.res_kind, res_value=self.res_value,
            fmt=self.fmt, nodata=self.provider.nodata, clip_to_polygon=self.clip,
            already_processed=processed, max_megapixels=self.max_mp, metadata=meta,
            progress=progress, is_canceled=self.isCanceled)
        # ---- quality control on the finished file ---------------------------
        progress(100.0)
        self.statusChanged.emit("Checking quality of {} …".format(os.path.basename(out_path)))
        qa = quality.assess_output(out_path, expected_bands=info["band_count"],
                                   nodata=info.get("nodata"), clipped=info["clipped_to_polygon"])
        if info["resampling"] == "near" and qa["status"] != quality.FAIL:
            try:
                fid = quality.fidelity_check(out_path, info["sources"])
            except Exception as exc:  # noqa: BLE001
                fid = {"status": quality.WARN, "compared": 0, "mismatches": 0,
                       "message": "Fidelity check could not run: {}".format(exc)}
            qa["fidelity"] = fid
            qa["checks"].append({"level": fid["status"], "message": "Source fidelity: " + fid["message"]})
            if quality._RANK[fid["status"]] > quality._RANK[qa["status"]]:
                qa["status"] = fid["status"]
        else:
            qa["fidelity"] = {"status": "N/A", "message": "Resampled output ({}) – pixel-identical "
                              "comparison not applicable.".format(info["resampling"])}
        info.pop("sources", None)
        if qa["valid_percent"] == 0:
            # A file with no data is useless: remove it instead of cluttering the folder.
            raster._remove(out_path)
            raise RuntimeError("The output was empty (the scene has no valid pixels inside your "
                               "AOI) and was not kept.")

        sidecar = None
        if self.write_sidecar:
            sidecar = os.path.splitext(out_path)[0] + ".json"
            with open(sidecar, "w", encoding="utf-8") as fh:
                json.dump(self._sidecar(scene, info, qa), fh, indent=2, ensure_ascii=False, default=str)
        return {"path": out_path, "name": os.path.splitext(os.path.basename(out_path))[0],
                "scene": scene, "info": info, "sidecar": sidecar, "preset": self.preset, "qa": qa}

    def _sidecar(self, scene, info, qa=None):
        return {
            "generator": "KGA Toolbox Satellite Imagery Archive {}".format(PLUGIN_VERSION),
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": {
                "provider": self.provider.name,
                "provider_homepage": self.provider.homepage,
                "collection": scene.collection,
                "scene_id": scene.scene_id,
                "item_url": http.strip_query(scene.self_url) if scene.self_url else None,
                "source_urls": [http.strip_query(u) for u in
                                self.provider.public_source_urls(scene, self.preset, self.variant)],
            },
            "acquisition": {
                "datetime": scene.datetime,
                "date": scene.date,
                "satellite_id": scene.platform,
                "tile": scene.tile,
                "cloud_cover_percent": scene.cloud_cover,
                "aoi_coverage_percent": (round(scene.aoi_coverage, 2)
                                         if scene.aoi_coverage is not None else None),
                "native_gsd_m": scene.gsd,
            },
            "processing": {
                "band_combination": self.preset,
                "bands": info["band_names"],
                "output_file": os.path.basename(info["path"]),
                "output_format": info["driver"],
                "resolution_option": self.res_label,
                "pixel_size": info["pixel_size"],
                "crs": info["crs"],
                "width": info["width"],
                "height": info["height"],
                "bounds": info["bounds"],
                "resampling": info["resampling"],
                "clipped_to_polygon": info["clipped_to_polygon"],
                "aoi_bbox_wgs84": list(self.aoi.bbox),
                "nodata": info.get("nodata"),
                "scale_offset": info.get("scale_offset"),
                "reflectance_formula": ("value * scale + offset (per band, see scale_offset)"
                                        if any(so[0] is not None or so[1] is not None
                                               for so in info.get("scale_offset") or []) else None),
            },
            "quality": qa,
            "properties": scene.properties,
        }

    def finished(self, result):
        self.downloadFinished.emit(self.outputs, self.errors, self.isCanceled())

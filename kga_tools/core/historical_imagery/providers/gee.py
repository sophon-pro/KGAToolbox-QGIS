# -*- coding: utf-8 -*-
"""
Google Earth Engine provider (optional).

Requirements on the user's machine:
  1. ``pip install earthengine-api`` into the QGIS Python environment
     (or install the "Google Earth Engine" QGIS plugin, which bundles it).
  2. A Google Cloud project registered for Earth Engine (free for
     non-commercial/academic use) – enter its id in the plugin Settings.
  3. One-time authentication: in the QGIS Python console run
     ``import ee; ee.Authenticate()``.

Downloads use ``getDownloadURL`` which Google limits to ~32 MB per request,
so large AOIs need a coarser output resolution.
"""

import os
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

from .. import http, settings
from ..models import BandSource, Scene
from .base import ImageryProvider, SearchCanceled, bbox_from_geojson

INSTALL_HELP = (
    "Google Earth Engine needs the 'earthengine-api' Python package.\n\n"
    "Install it into the QGIS Python environment (OSGeo4W Shell on Windows: "
    "'python -m pip install earthengine-api'), or install the 'Google Earth Engine' "
    "plugin from the QGIS plugin manager, then restart QGIS."
)
AUTH_HELP = (
    "Earth Engine could not be initialised.\n\n"
    "1. Make sure your Google Cloud project is registered for Earth Engine and its id is set "
    "in Satellite Imagery Archive ▸ Settings.\n"
    "2. Authenticate once from the QGIS Python console:\n"
    "      import ee; ee.Authenticate()\n\n"
    "Details: {}"
)

_S2_PRESETS = OrderedDict([
    ("True Color (B4-B3-B2)", ["B4", "B3", "B2"]),
    ("False Color Infrared (B8-B4-B3)", ["B8", "B4", "B3"]),
    ("Agriculture (B11-B8-B2)", ["B11", "B8", "B2"]),
    ("SWIR / Geology (B12-B8A-B4)", ["B12", "B8A", "B4"]),
])
_OLI_PRESETS = OrderedDict([
    ("True Color (SR_B4-B3-B2)", ["SR_B4", "SR_B3", "SR_B2"]),
    ("Color Infrared (SR_B5-B4-B3)", ["SR_B5", "SR_B4", "SR_B3"]),
    ("SWIR / Geology (SR_B7-B5-B4)", ["SR_B7", "SR_B5", "SR_B4"]),
])
_TM_PRESETS = OrderedDict([
    ("True Color (SR_B3-B2-B1)", ["SR_B3", "SR_B2", "SR_B1"]),
    ("Color Infrared (SR_B4-B3-B2)", ["SR_B4", "SR_B3", "SR_B2"]),
    ("SWIR / Geology (SR_B7-B4-B3)", ["SR_B7", "SR_B4", "SR_B3"]),
])

COLLECTIONS = OrderedDict([
    ("s2sr", dict(label="Sentinel-2 SR Harmonized (2017–)", id="COPERNICUS/S2_SR_HARMONIZED",
                  cloud="CLOUDY_PIXEL_PERCENTAGE", platform="SPACECRAFT_NAME", res=10,
                  presets=_S2_PRESETS, vis=(0, 3000), min_date=date(2017, 3, 28), scale=(0.0001, 0.0))),
    ("s2toa", dict(label="Sentinel-2 L1C TOA Harmonized (2015–)", id="COPERNICUS/S2_HARMONIZED",
                   cloud="CLOUDY_PIXEL_PERCENTAGE", platform="SPACECRAFT_NAME", res=10,
                   presets=_S2_PRESETS, vis=(0, 3000), min_date=date(2015, 6, 27), scale=(0.0001, 0.0))),
    ("l9", dict(label="Landsat 9 C2 L2 (2021–)", id="LANDSAT/LC09/C02/T1_L2", cloud="CLOUD_COVER",
                platform="SPACECRAFT_ID", res=30, presets=_OLI_PRESETS, vis=(7300, 18000), scale=(0.0000275, -0.2),
                min_date=date(2021, 10, 31))),
    ("l8", dict(label="Landsat 8 C2 L2 (2013–)", id="LANDSAT/LC08/C02/T1_L2", cloud="CLOUD_COVER",
                platform="SPACECRAFT_ID", res=30, presets=_OLI_PRESETS, vis=(7300, 18000), scale=(0.0000275, -0.2),
                min_date=date(2013, 3, 18))),
    ("l7", dict(label="Landsat 7 C2 L2 (1999–)", id="LANDSAT/LE07/C02/T1_L2", cloud="CLOUD_COVER",
                platform="SPACECRAFT_ID", res=30, presets=_TM_PRESETS, vis=(7300, 18000), scale=(0.0000275, -0.2),
                min_date=date(1999, 5, 28))),
    ("l5", dict(label="Landsat 5 C2 L2 (1984–2012)", id="LANDSAT/LT05/C02/T1_L2", cloud="CLOUD_COVER",
                platform="SPACECRAFT_ID", res=30, presets=_TM_PRESETS, vis=(7300, 18000), scale=(0.0000275, -0.2),
                min_date=date(1984, 3, 16))),
])


def _ee():
    try:
        import ee  # noqa: WPS433
    except ImportError:
        raise RuntimeError(INSTALL_HELP)
    return ee


class EarthEngineProvider(ImageryProvider):
    id = "gee"
    name = "Google Earth Engine (needs account)"
    short = "GEE"
    description = ("Sentinel-2 and Landsat collections via the Earth Engine API. Requires the "
                   "earthengine-api package, a registered Cloud project and one-time authentication.")
    homepage = "https://earthengine.google.com"
    nodata = 0
    _initialized = False

    def variants(self):
        return [(c["label"], key) for key, c in COLLECTIONS.items()]

    def _coll(self, variant):
        return COLLECTIONS.get(variant) or next(iter(COLLECTIONS.values()))

    def variant_tooltip(self, variant):
        return "Earth Engine asset: " + self._coll(variant)["id"]

    def min_date(self, variant):
        return self._coll(variant)["min_date"]

    def band_presets(self, variant):
        return self._coll(variant)["presets"]

    def check_available(self):
        try:
            import ee  # noqa: F401
        except ImportError:
            return INSTALL_HELP
        if not settings.get("gee_project").strip():
            return ("Enter your Google Cloud project id (registered for Earth Engine) in "
                    "Settings before using Google Earth Engine.")
        return None

    def _init(self):
        ee = _ee()
        if not EarthEngineProvider._initialized:
            try:
                ee.Initialize(project=settings.get("gee_project").strip() or None)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(AUTH_HELP.format(exc))
            EarthEngineProvider._initialized = True
        return ee

    def search(self, aoi, start, end, max_cloud, limit, variant, is_canceled):
        ee = self._init()
        coll = self._coll(variant)
        geom = ee.Geometry(aoi.search_geometry())
        col = (ee.ImageCollection(coll["id"]).filterBounds(geom)
               .filterDate(start.isoformat(), (end + timedelta(days=1)).isoformat()))
        if max_cloud < 100:
            col = col.filter(ee.Filter.lte(coll["cloud"], max_cloud))
        col = col.sort("system:time_start", False).limit(limit)

        def to_feature(img):
            return (ee.Feature(img.geometry(), img.toDictionary())
                    .set("ee_id", img.get("system:id"))
                    .set("ee_time", img.get("system:time_start")))

        if is_canceled():
            raise SearchCanceled()
        try:
            info = ee.FeatureCollection(col.map(to_feature)).getInfo()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Earth Engine search failed: {}".format(exc))

        scenes = []
        for feat in info.get("features", []):
            p = feat.get("properties") or {}
            ms = p.get("ee_time")
            dt = (datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                  if ms else "")
            if "MGRS_TILE" in p:
                tile = "MGRS " + str(p["MGRS_TILE"])
            elif "WRS_PATH" in p:
                tile = "WRS {}/{}".format(p.get("WRS_PATH"), p.get("WRS_ROW"))
            else:
                tile = ""
            cloud = p.get(coll["cloud"])
            scenes.append(Scene(
                provider_id=self.id,
                scene_id=str(p.get("ee_id")),
                collection=coll["id"],
                datetime=dt,
                platform=str(p.get(coll["platform"], "")),
                cloud_cover=float(cloud) if cloud is not None else None,
                gsd=coll["res"],
                tile=tile,
                footprint=feat.get("geometry"),
                bbox=bbox_from_geojson(feat.get("geometry")),
                thumbnail_url=None,
                assets={},
                self_url="https://code.earthengine.google.com/?asset=" + str(p.get("ee_id")),
                properties={k: v for k, v in p.items()
                            if isinstance(v, (str, int, float, bool)) and len(str(v)) < 200},
            ))
        return scenes

    def get_thumbnail_url(self, scene):
        ee = self._init()
        coll = next((c for c in COLLECTIONS.values() if c["id"] == scene.collection),
                    self._coll(""))
        bands = next(iter(coll["presets"].values()))
        vmin, vmax = coll["vis"]
        img = ee.Image(scene.scene_id).select(bands)
        return img.getThumbURL({"min": vmin, "max": vmax, "dimensions": 256, "format": "png"})

    def prepare_sources(self, scene, preset, variant, aoi, res_kind, res_value, workdir, is_canceled):
        ee = self._init()
        coll = next((c for c in COLLECTIONS.values() if c["id"] == scene.collection), self._coll(variant))
        bands = coll["presets"].get(preset) or next(iter(coll["presets"].values()))
        scale = coll["res"] / res_value if res_kind == "pct" else res_value
        params = {
            "bands": bands,
            "region": aoi.search_geometry(),
            "scale": scale,
            "crs": "EPSG:{}".format(aoi.utm_epsg()),
            "format": "GEO_TIFF",
        }
        try:
            url = ee.Image(scene.scene_id).select(bands).getDownloadURL(params)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "must be less than" in msg or "too large" in msg.lower():
                raise RuntimeError(
                    "Earth Engine refused the request because it is too large (≈32 MB limit per "
                    "download). Use a smaller AOI or a coarser resolution (e.g. 30 m or 60 m).\n\n"
                    + msg)
            raise RuntimeError("Earth Engine download failed: " + msg)
        if is_canceled():
            raise SearchCanceled()
        data = http.get_bytes(url, timeout=600)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", scene.scene_id)
        path = os.path.join(workdir, "gee_{}.tif".format(safe))
        with open(path, "wb") as fh:
            fh.write(data)
        sc, off = coll.get("scale", (None, None))
        return [BandSource(path=path, bands=None, names=bands, scales=[sc] * len(bands),
                           offsets=[off] * len(bands))], True

    def public_source_urls(self, scene, preset, variant):
        return [scene.self_url] if scene.self_url else []

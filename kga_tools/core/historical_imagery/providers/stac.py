# -*- coding: utf-8 -*-
"""
STAC API providers.

* Sentinel-2 L2A from Element 84 Earth Search (public COGs on AWS, no account).
* Landsat 4/5/7/8/9 Collection 2 Level-2 from Microsoft Planetary Computer
  (public, URLs are signed with a free anonymous SAS token).
* A user-configurable "Custom STAC catalog".

The STAC API is queried with plain HTTP POST /search requests so the plugin
works without installing pystac-client.
"""

import time
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from .. import http, settings
from ..models import AOI, BandSource, Scene
from .base import ImageryProvider, SearchCanceled, bbox_from_geojson

def _bbox2d(bbox):
    """STAC bbox may be 2-D (4 values) or 3-D (6 values); return (minx, miny, maxx, maxy)."""
    if not bbox:
        return None
    if len(bbox) == 6:
        return (bbox[0], bbox[1], bbox[3], bbox[4])
    if len(bbox) == 4:
        return tuple(bbox)
    return None


THUMBNAIL_KEYS = ("thumbnail", "rendered_preview", "preview", "overview", "browse", "quicklook")
PAGE_SIZE = 100


class StacProvider(ImageryProvider):
    """Generic STAC API provider; subclasses supply variants and presets."""

    api_url = ""
    cloud_property = "eo:cloud_cover"

    # variant key -> dict(label, collection, query, min_date, presets, tooltip)
    _variants: "OrderedDict[str, dict]" = OrderedDict()

    # ---- metadata ------------------------------------------------------
    def variants(self):
        return [(v["label"], key) for key, v in self._variants.items()]

    def _variant(self, variant):
        if variant in self._variants:
            return self._variants[variant]
        return next(iter(self._variants.values()))

    def variant_tooltip(self, variant):
        return self._variant(variant).get("tooltip", "")

    def min_date(self, variant):
        return self._variant(variant).get("min_date", date(1972, 7, 23))

    def band_presets(self, variant):
        return self._variant(variant)["presets"]

    # ---- signing hook (Planetary Computer) ----------------------------
    def sign(self, href: str, scene: Scene) -> str:
        return href

    def request_headers(self) -> Dict[str, str]:
        return {}

    # ---- search ----------------------------------------------------------
    def build_search_body(self, aoi: AOI, start: date, end: date, max_cloud: float,
                          limit: int, variant: str) -> dict:
        var = self._variant(variant)
        body = {
            "collections": [var["collection"]],
            "datetime": "{}T00:00:00Z/{}T23:59:59Z".format(start.isoformat(), end.isoformat()),
            "limit": max(1, min(limit, PAGE_SIZE)),
        }
        if aoi.is_polygon:
            body["intersects"] = aoi.geometry
        else:
            body["bbox"] = [round(v, 6) for v in aoi.bbox]
        query = dict(var.get("query") or {})
        if self.supports_cloud_filter and self.cloud_property and max_cloud < 100:
            query[self.cloud_property] = {"lte": max_cloud}
        if query:
            body["query"] = query
        body["sortby"] = [{"field": "properties.datetime", "direction": "desc"}]
        return body

    def search(self, aoi, start, end, max_cloud, limit, variant, is_canceled):
        url = self.api_url.rstrip("/") + "/search"
        body = self.build_search_body(aoi, start, end, max_cloud, limit, variant)
        headers = self.request_headers()
        timeout = settings.get("http_timeout")

        try:
            page = http.post_json(url, body, headers=headers, timeout=timeout)
        except http.HttpError as exc:
            # Some STAC servers reject sortby or the polygon 'intersects'; retry simpler.
            if exc.status not in (400, 422):
                raise
            body.pop("sortby", None)
            if "intersects" in body:
                body.pop("intersects")
                body["bbox"] = [round(v, 6) for v in aoi.bbox]
            try:
                page = http.post_json(url, body, headers=headers, timeout=timeout)
            except http.HttpError as exc2:
                # Last resort: no 'query' extension; cloud cover is filtered client-side below.
                if exc2.status not in (400, 422) or "query" not in body:
                    raise
                body.pop("query")
                page = http.post_json(url, body, headers=headers, timeout=timeout)

        scenes: List[Scene] = []
        seen = set()
        while True:
            if is_canceled():
                raise SearchCanceled()
            for feature in page.get("features", []):
                scene = self.parse_item(feature, variant)
                if scene is None or scene.scene_id in seen:
                    continue
                # Client-side safety net in case the server ignored the 'query' extension.
                if (self.supports_cloud_filter and max_cloud < 100 and scene.cloud_cover is not None
                        and scene.cloud_cover > max_cloud):
                    continue
                seen.add(scene.scene_id)
                scenes.append(scene)
                if len(scenes) >= limit:
                    return scenes
            nxt = next((lk for lk in page.get("links", []) if lk.get("rel") == "next"), None)
            if not nxt or not page.get("features"):
                return scenes
            method = (nxt.get("method") or "GET").upper()
            if method == "POST":
                next_body = nxt.get("body") or {}
                if nxt.get("merge"):
                    merged = dict(body)
                    merged.update(next_body)
                    next_body = merged
                page = http.post_json(nxt["href"], next_body, headers=headers, timeout=timeout)
            else:
                page = http.get_json(nxt["href"], headers=headers, timeout=timeout)

    def parse_item(self, item: dict, variant: str) -> Optional[Scene]:
        props = item.get("properties") or {}
        assets = {}
        for key, asset in (item.get("assets") or {}).items():
            href = asset.get("href") if isinstance(asset, dict) else None
            if href:
                assets[key] = http.s3_to_https(href)
        thumb = next((assets[k] for k in THUMBNAIL_KEYS if k in assets), None)
        scaling = {}
        for key, asset in (item.get("assets") or {}).items():
            rb = asset.get("raster:bands") if isinstance(asset, dict) else None
            if isinstance(rb, list) and rb and isinstance(rb[0], dict):
                sc, off = rb[0].get("scale"), rb[0].get("offset")
                if sc is not None or off is not None:
                    scaling[key] = (sc, off)
        self_link = next((lk.get("href") for lk in item.get("links", []) if lk.get("rel") == "self"), None)
        cloud = props.get(self.cloud_property, props.get("eo:cloud_cover"))
        try:
            cloud = float(cloud) if cloud is not None else None
        except (TypeError, ValueError):
            cloud = None
        dt = props.get("datetime") or props.get("start_datetime") or ""
        bbox = item.get("bbox")
        return Scene(
            provider_id=self.id,
            scene_id=item.get("id", ""),
            collection=item.get("collection") or self._variant(variant)["collection"],
            datetime=dt,
            platform=str(props.get("platform", "")),
            cloud_cover=cloud,
            gsd=props.get("gsd") or self._variant(variant).get("gsd"),
            tile=self.tile_name(props),
            footprint=item.get("geometry"),
            bbox=_bbox2d(bbox) or bbox_from_geojson(item.get("geometry")),
            thumbnail_url=thumb,
            assets=assets,
            self_url=self_link,
            properties={k: v for k, v in props.items() if isinstance(v, (str, int, float, bool))},
            scaling=scaling,
        )

    @staticmethod
    def tile_name(props: dict) -> str:
        if props.get("grid:code"):
            return str(props["grid:code"])
        if props.get("s2:mgrs_tile"):
            return "MGRS " + str(props["s2:mgrs_tile"])
        if props.get("mgrs:utm_zone") and props.get("mgrs:latitude_band"):
            return "MGRS {}{}{}".format(props["mgrs:utm_zone"], props["mgrs:latitude_band"],
                                        props.get("mgrs:grid_square", ""))
        if props.get("landsat:wrs_path"):
            return "WRS {}/{}".format(props["landsat:wrs_path"], props.get("landsat:wrs_row", ""))
        return str(props.get("title", ""))

    # ---- download --------------------------------------------------------
    def prepare_sources(self, scene, preset, variant, aoi, res_kind, res_value, workdir, is_canceled):
        keys = self.band_presets(variant).get(preset)
        if not keys:
            raise ValueError("Unknown band combination '{}'.".format(preset))
        missing = [k for k in keys if k not in scene.assets]
        if missing:
            raise ValueError("Scene {} has no asset(s): {}. Available: {}".format(
                scene.scene_id, ", ".join(missing), ", ".join(sorted(scene.assets))))
        sources = []
        for key in keys:
            href = self.sign(scene.assets[key], scene)
            path = href if not href.startswith("http") else "/vsicurl/" + href
            sc, off = scene.scaling.get(key, (None, None))
            sources.append(BandSource(path=path, bands=None, names=[key], scales=[sc], offsets=[off]))
        return sources, False


# ---------------------------------------------------------------------------
# Sentinel-2 via Element 84 Earth Search
# ---------------------------------------------------------------------------
S2_PRESETS = OrderedDict([
    ("True Color (B4-B3-B2)", ["red", "green", "blue"]),
    ("False Color Infrared (B8-B4-B3)", ["nir", "red", "green"]),
    ("Agriculture (B11-B8-B2)", ["swir16", "nir", "blue"]),
    ("SWIR / Geology (B12-B8A-B4)", ["swir22", "nir08", "red"]),
    ("10 m bands stack (B2-B3-B4-B8)", ["blue", "green", "red", "nir"]),
    ("TCI visual 8-bit RGB", ["visual"]),
])


class EarthSearchSentinel2(StacProvider):
    id = "earth_search_s2"
    name = "ESA Sentinel-2 L2A (10 m, 2015–present)"
    short = "S2"
    description = ("Copernicus Sentinel-2 surface reflectance served as Cloud-Optimized GeoTIFFs by "
                   "Element 84 Earth Search on AWS. Free, no account or API key needed.")
    homepage = "https://earth-search.aws.element84.com/v1"
    api_url = "https://earth-search.aws.element84.com/v1"
    native_resolution = 10.0
    nodata = 0
    _variants = OrderedDict([
        ("c1", dict(label="Sentinel-2 L2A Collection 1 (reprocessed, recommended)",
                    collection="sentinel-2-c1-l2a", min_date=date(2015, 6, 27), gsd=10,
                    presets=S2_PRESETS,
                    tooltip="ESA Collection-1 reprocessed archive with consistent processing baseline.")),
        ("legacy", dict(label="Sentinel-2 L2A (legacy archive)",
                        collection="sentinel-2-l2a", min_date=date(2015, 6, 27), gsd=10,
                        presets=S2_PRESETS,
                        tooltip="Original Earth Search Sentinel-2 L2A collection.")),
    ])


# ---------------------------------------------------------------------------
# Landsat via Microsoft Planetary Computer
# ---------------------------------------------------------------------------
LANDSAT_PRESETS = OrderedDict([
    ("True Color (Red-Green-Blue)", ["red", "green", "blue"]),
    ("Color Infrared (NIR-Red-Green)", ["nir08", "red", "green"]),
    ("Agriculture (SWIR1-NIR-Blue)", ["swir16", "nir08", "blue"]),
    ("SWIR / Geology (SWIR2-NIR-Red)", ["swir22", "nir08", "red"]),
    ("All reflective bands (B-G-R-NIR-SWIR1-SWIR2)", ["blue", "green", "red", "nir08", "swir16", "swir22"]),
])

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{}"  # nosec B105 - a URL template, not a secret


class PlanetaryComputerSigner:
    """Caches free anonymous SAS tokens used to read Planetary Computer blobs."""

    _cache: Dict[str, tuple] = {}

    @classmethod
    def sign(cls, href: str, collection: str) -> str:
        if not href or "blob.core.windows.net" not in href or "sig=" in href:
            return href
        token = cls.token(collection)
        return href + ("&" if "?" in href else "?") + token

    @classmethod
    def token(cls, collection: str) -> str:
        cached = cls._cache.get(collection)
        if cached and cached[1] - 300 > time.time():
            return cached[0]
        headers = {}
        key = settings.get("pc_subscription_key")
        if key:
            headers["Ocp-Apim-Subscription-Key"] = key
        data = http.get_json(PC_TOKEN.format(collection), headers=headers)
        token = data.get("token")
        if not token:
            raise http.HttpError(0, "Planetary Computer did not return a SAS token.")
        expiry = time.time() + 45 * 60
        raw = data.get("msft:expiry")
        if raw:
            try:
                expiry = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
        cls._cache[collection] = (token, expiry)
        return token


class PlanetaryComputerLandsat(StacProvider):
    id = "pc_landsat"
    name = "USGS Landsat 4–9 Collection 2 L2 (30 m, 1982–present)"
    short = "LS"
    description = ("USGS Landsat Collection 2 Level-2 surface reflectance (TM, ETM+, OLI/OLI-2), "
                   "hosted by Microsoft Planetary Computer. Free, no account needed (an optional "
                   "subscription key in Settings raises rate limits).")
    homepage = "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2"
    api_url = PC_STAC
    native_resolution = 30.0
    nodata = 0
    _variants = OrderedDict([
        ("all", dict(label="All missions (Landsat 4, 5, 7, 8, 9)", collection="landsat-c2-l2",
                     query={"platform": {"in": ["landsat-4", "landsat-5", "landsat-7",
                                                "landsat-8", "landsat-9"]}},
                     min_date=date(1982, 8, 22), gsd=30, presets=LANDSAT_PRESETS,
                     tooltip="Full Level-2 archive, 1982 to today.")),
        ("oli", dict(label="Landsat 8 & 9 OLI (2013–present)", collection="landsat-c2-l2",
                     query={"platform": {"in": ["landsat-8", "landsat-9"]}},
                     min_date=date(2013, 3, 18), gsd=30, presets=LANDSAT_PRESETS,
                     tooltip="Operational Land Imager, 12-bit, best radiometry.")),
        ("etm", dict(label="Landsat 7 ETM+ (1999–2022)", collection="landsat-c2-l2",
                     query={"platform": {"in": ["landsat-7"]}},
                     min_date=date(1999, 5, 28), gsd=30, presets=LANDSAT_PRESETS,
                     tooltip="Note: scenes after 31 May 2003 have SLC-off data gaps (stripes).")),
        ("tm", dict(label="Landsat 4 & 5 TM (1982–2013)", collection="landsat-c2-l2",
                    query={"platform": {"in": ["landsat-4", "landsat-5"]}},
                    min_date=date(1982, 8, 22), gsd=30, presets=LANDSAT_PRESETS,
                    tooltip="Thematic Mapper – the deep historical archive.")),
    ])

    def request_headers(self):
        key = settings.get("pc_subscription_key")
        return {"Ocp-Apim-Subscription-Key": key} if key else {}

    def sign(self, href, scene):
        return PlanetaryComputerSigner.sign(href, scene.collection)

    def get_thumbnail_url(self, scene):
        # rendered_preview is served by the PC data API and needs no signing.
        url = scene.assets.get("rendered_preview") or scene.thumbnail_url
        return self.sign(url, scene) if url else None


# ---------------------------------------------------------------------------
# Custom STAC catalog configured in Settings
# ---------------------------------------------------------------------------
class CustomStacProvider(StacProvider):
    id = "custom_stac"
    name = "Custom / public STAC catalog"
    short = "STAC"
    description = ("Any STAC API (e.g. Earth Search, Planetary Computer, USGS LandsatLook, "
                   "national catalogs). Configure URL, collection and band asset keys in Settings.")
    homepage = "https://stacindex.org/catalogs"

    @property
    def api_url(self):  # type: ignore[override]
        return settings.get("custom_stac_url").strip()

    @property
    def cloud_property(self):  # type: ignore[override]
        return settings.get("custom_stac_cloud_property").strip()

    @property
    def _variants(self):  # type: ignore[override]
        coll = settings.get("custom_stac_collection").strip()
        keys = [k.strip() for k in settings.get("custom_stac_bands").split(",") if k.strip()]
        presets = OrderedDict([("Configured bands ({})".format(", ".join(keys)), keys)])
        return OrderedDict([("custom", dict(label="Collection: {}".format(coll or "(not set)"),
                                            collection=coll, presets=presets,
                                            min_date=date(1972, 7, 23)))])

    def check_available(self):
        if not self.api_url or not settings.get("custom_stac_collection").strip():
            return ("No custom STAC catalog configured. Open Settings (gear icon) and enter "
                    "the STAC API URL, collection id and band asset keys.")
        return None

    def request_headers(self):
        key = settings.get("pc_subscription_key")
        if settings.get("custom_stac_sign_pc") and key:
            return {"Ocp-Apim-Subscription-Key": key}
        return {}

    def sign(self, href, scene):
        if settings.get("custom_stac_sign_pc"):
            return PlanetaryComputerSigner.sign(href, scene.collection)
        return href

# -*- coding: utf-8 -*-
"""OpenAerialMap provider – openly licensed aerial, drone and satellite imagery."""

from collections import OrderedDict
from datetime import date

from .. import http, settings
from ..models import BandSource, Scene
from .base import ImageryProvider, SearchCanceled, bbox_from_geojson

OAM_API = "https://api.openaerialmap.org/meta"


class OpenAerialMapProvider(ImageryProvider):
    id = "oam"
    name = "OpenAerialMap (aerial & drone, CC-BY 4.0)"
    short = "OAM"
    description = ("Community-contributed openly licensed aerial, UAV and satellite imagery "
                   "(Open Imagery Network). Very high resolution, coverage is patchy. No API key.")
    homepage = "https://openaerialmap.org"
    supports_cloud_filter = False
    native_resolution = 0.5
    nodata = None  # OAM GeoTIFFs usually carry an alpha band or internal mask

    _PLATFORMS = OrderedDict([
        ("all", ("All platforms", None)),
        ("uav", ("UAV / drone", "uav")),
        ("aircraft", ("Aircraft", "aircraft")),
        ("satellite", ("Satellite", "satellite")),
    ])
    _PRESETS = OrderedDict([("Original bands (RGB / RGBA)", ["image"])])

    def variants(self):
        return [(label, key) for key, (label, _) in self._PLATFORMS.items()]

    def min_date(self, variant):
        return date(1990, 1, 1)

    def band_presets(self, variant):
        return self._PRESETS

    def search(self, aoi, start, end, max_cloud, limit, variant, is_canceled):
        params = {
            "bbox": ",".join("{:.6f}".format(v) for v in aoi.bbox),
            "acquisition_from": start.isoformat(),
            "acquisition_to": end.isoformat() + "T23:59:59Z",
            "limit": min(limit, 100),
            "order_by": "acquisition_end",
            "sort": "desc",
        }
        platform = self._PLATFORMS.get(variant, (None, None))[1]
        if platform:
            params["platform"] = platform
        timeout = settings.get("http_timeout")

        scenes, page = [], 1
        while len(scenes) < limit:
            if is_canceled():
                raise SearchCanceled()
            params["page"] = page
            data = http.get_json(OAM_API, params=params, timeout=timeout)
            results = data.get("results") or []
            for res in results:
                scene = self._parse(res)
                if scene:
                    scenes.append(scene)
            found = (data.get("meta") or {}).get("found", 0)
            if not results or page * params["limit"] >= found:
                break
            page += 1
        return scenes[:limit]

    def _parse(self, res):
        url = res.get("uuid")
        if not url or not str(url).lower().split("?")[0].endswith((".tif", ".tiff")):
            return None
        props = res.get("properties") or {}
        bbox = res.get("bbox")
        geom = res.get("geojson") or res.get("footprint")
        if isinstance(geom, str):
            geom = None  # footprint is sometimes WKT; geojson is preferred
        gsd = res.get("gsd")
        try:
            gsd = float(gsd) if gsd else None
        except (TypeError, ValueError):
            gsd = None
        return Scene(
            provider_id=self.id,
            scene_id=str(res.get("_id") or url),
            collection="openaerialmap",
            datetime=res.get("acquisition_start") or res.get("acquisition_end") or "",
            platform=str(res.get("platform") or ""),
            cloud_cover=None,
            gsd=gsd,
            tile=str(res.get("title") or "")[:60],
            footprint=geom,
            bbox=tuple(bbox) if bbox and len(bbox) == 4 else bbox_from_geojson(geom),
            thumbnail_url=props.get("thumbnail"),
            assets={"image": url},
            self_url=url,
            properties={
                "title": res.get("title"),
                "provider": res.get("provider"),
                "license": "CC-BY 4.0 (Open Imagery Network)",
                "sensor": props.get("sensor"),
                "file_size": res.get("file_size"),
                "contact": res.get("contact"),
            },
        )

    def prepare_sources(self, scene, preset, variant, aoi, res_kind, res_value, workdir, is_canceled):
        return [BandSource(path="/vsicurl/" + scene.assets["image"], bands=None, names=None)], False

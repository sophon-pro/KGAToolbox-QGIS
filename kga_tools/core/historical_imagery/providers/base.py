# -*- coding: utf-8 -*-
"""Abstract imagery provider interface."""

import json
from collections import OrderedDict
from datetime import date
from typing import Callable, List, Optional, Tuple

from ..models import AOI, BandSource, Scene

IsCanceled = Callable[[], bool]


class SearchCanceled(Exception):
    """Raised when the user cancels a running search."""


class ImageryProvider:
    """
    Base class for all imagery sources.

    Subclasses implement :meth:`search` and :meth:`prepare_sources`. All
    methods except the cheap metadata ones are called from background threads,
    so they must not touch Qt widgets.
    """

    id = "base"
    name = "Base provider"
    short = "img"
    description = ""
    homepage = ""
    supports_cloud_filter = True
    native_resolution = 30.0  # metres; used for size estimates
    nodata: Optional[float] = 0

    # ---- metadata used by the GUI ------------------------------------
    def variants(self) -> List[Tuple[str, str]]:
        """(label, key) pairs shown in the 'Collection / mission' combo."""
        return []

    def variant_tooltip(self, variant: str) -> str:
        return ""

    def min_date(self, variant: str) -> date:
        return date(1972, 7, 23)

    def band_presets(self, variant: str) -> "OrderedDict[str, List[str]]":
        return OrderedDict()

    def check_available(self) -> Optional[str]:
        """Return None if usable, or a human-readable reason why not."""
        return None

    # ---- worker-thread methods ----------------------------------------
    def search(self, aoi: AOI, start: date, end: date, max_cloud: float, limit: int,
               variant: str, is_canceled: IsCanceled) -> List[Scene]:
        raise NotImplementedError

    def get_thumbnail_url(self, scene: Scene) -> Optional[str]:
        return scene.thumbnail_url

    def prepare_sources(self, scene: Scene, preset: str, variant: str, aoi: AOI,
                        res_kind: str, res_value: float, workdir: str,
                        is_canceled: IsCanceled) -> Tuple[List[BandSource], bool]:
        """
        Return the rasters to stack for ``scene``/``preset`` and a flag telling
        whether they are already clipped & resampled (e.g. Earth Engine exports).
        """
        raise NotImplementedError

    def public_source_urls(self, scene: Scene, preset: str, variant: str) -> List[str]:
        """Unsigned URLs written to the metadata sidecar."""
        keys = self.band_presets(variant).get(preset, [])
        return [scene.assets[k] for k in keys if k in scene.assets]


def compute_aoi_coverage(scenes: List[Scene], aoi: AOI) -> None:
    """Fill Scene.aoi_coverage (% of AOI inside the footprint) using OGR."""
    try:
        from osgeo import ogr
    except ImportError:
        return
    try:
        aoi_geom = ogr.CreateGeometryFromJson(json.dumps(aoi.search_geometry()))
        aoi_area = aoi_geom.Area() if aoi_geom else 0
    except Exception:  # noqa: BLE001
        return
    if not aoi_area:
        return
    for scene in scenes:
        if not scene.footprint:
            continue
        try:
            fp = ogr.CreateGeometryFromJson(json.dumps(scene.footprint))
            if fp is None:
                continue
            if not fp.IsValid():
                fp = fp.Buffer(0)
            inter = fp.Intersection(aoi_geom)
            scene.aoi_coverage = min(100.0, 100.0 * (inter.Area() if inter else 0) / aoi_area)
        except Exception:  # noqa: BLE001 - coverage is informative only
            scene.aoi_coverage = None


def bbox_from_geojson(geom: Optional[dict]):
    """Compute (minx, miny, maxx, maxy) for a GeoJSON geometry."""
    if not geom:
        return None
    xs, ys = [], []

    def walk(coords):
        if coords and isinstance(coords[0], (int, float)):
            xs.append(coords[0])
            ys.append(coords[1])
        else:
            for c in coords or []:
                walk(c)

    walk(geom.get("coordinates"))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))

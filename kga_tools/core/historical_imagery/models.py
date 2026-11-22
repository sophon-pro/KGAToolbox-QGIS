# -*- coding: utf-8 -*-
"""Plain data classes shared by providers, tasks and the GUI (no Qt imports)."""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

BBox = Tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat), EPSG:4326


@dataclass
class AOI:
    """Area of interest, always expressed in EPSG:4326."""

    bbox: BBox
    geometry: Optional[dict] = None  # GeoJSON Polygon/MultiPolygon (only for polygon-layer AOIs)
    source: str = "extent"           # "extent" | "rectangle" | "polygon"

    @property
    def is_polygon(self) -> bool:
        return self.geometry is not None

    def bbox_geometry(self) -> dict:
        x0, y0, x1, y1 = self.bbox
        return {
            "type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
        }

    def search_geometry(self) -> dict:
        """Geometry sent to APIs that accept 'intersects'."""
        return self.geometry or self.bbox_geometry()

    def approx_area_km2(self) -> float:
        x0, y0, x1, y1 = self.bbox
        mid_lat = math.radians((y0 + y1) / 2.0)
        return abs(x1 - x0) * 111.32 * math.cos(mid_lat) * abs(y1 - y0) * 110.57

    def utm_epsg(self) -> int:
        """EPSG code of the UTM zone at the AOI centre (WGS 84)."""
        x0, y0, x1, y1 = self.bbox
        lon, lat = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        zone = int((lon + 180.0) // 6.0) + 1
        zone = min(max(zone, 1), 60)
        return (32600 if lat >= 0 else 32700) + zone

    def summary(self) -> str:
        x0, y0, x1, y1 = self.bbox
        return "{:.4f}, {:.4f} → {:.4f}, {:.4f}  (≈ {:,.1f} km²)".format(
            x0, y0, x1, y1, self.approx_area_km2()
        )


@dataclass
class BandSource:
    """One GDAL-readable raster that contributes bands to the output stack."""

    path: str                          # GDAL path, e.g. /vsicurl/https://... or a local file
    bands: Optional[List[int]] = None  # 1-based band indexes to take; None = all bands
    names: Optional[List[str]] = None  # descriptions written to the output bands
    scales: Optional[List[Optional[float]]] = None   # per-band scale (reflectance = DN*scale+offset)
    offsets: Optional[List[Optional[float]]] = None  # per-band offset


@dataclass
class Scene:
    """One searchable/downloadable image (STAC item, OAM image, GEE image...)."""

    provider_id: str
    scene_id: str
    collection: str
    datetime: str                     # ISO 8601 string
    platform: str = ""
    cloud_cover: Optional[float] = None
    gsd: Optional[float] = None       # ground sample distance, metres
    tile: str = ""                    # MGRS tile / WRS path-row / title
    footprint: Optional[dict] = None  # GeoJSON geometry, EPSG:4326
    bbox: Optional[BBox] = None
    thumbnail_url: Optional[str] = None
    assets: Dict[str, str] = field(default_factory=dict)  # asset key -> unsigned href
    self_url: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    aoi_coverage: Optional[float] = None  # % of AOI covered by footprint
    scaling: Dict[str, Any] = field(default_factory=dict)  # asset key -> (scale, offset)

    @property
    def date(self) -> str:
        return (self.datetime or "")[:10]

    @property
    def year(self) -> Optional[int]:
        try:
            return int(self.datetime[:4])
        except (TypeError, ValueError):
            return None

    @property
    def uid(self) -> str:
        return "{}::{}".format(self.provider_id, self.scene_id)

    def label(self, short: bool = False) -> str:
        """Three-line list label: date · satellite / quality figures / tile or id."""
        line1 = "  ·  ".join(p for p in (self.date, self.platform) if p)
        bits = []
        if self.cloud_cover is not None:
            bits.append("☁ {:.1f}%".format(self.cloud_cover))
        if self.aoi_coverage is not None:
            bits.append("AOI {:.0f}%".format(self.aoi_coverage))
        if self.gsd:
            bits.append("{:g} m".format(round(self.gsd, 2)))
        line3 = self.tile or self.scene_id
        if short and len(line3) > 34:
            line3 = line3[:33] + "…"
        if not short and self.tile:
            line3 = "{}  ·  {}".format(self.tile, self.scene_id)
        return "\n".join((line1, "  ·  ".join(bits) or "–", line3))


# Output resolution choices: (label, kind, value). kind "pct" = fraction of native, "m" = metres.
RESOLUTION_CHOICES = [
    ("Native / full resolution", "pct", 1.0),
    ("50 % of native", "pct", 0.5),
    ("25 % of native", "pct", 0.25),
    ("10 m (resampled)", "m", 10.0),
    ("30 m (resampled)", "m", 30.0),
    ("60 m (resampled)", "m", 60.0),
]

OUTPUT_FORMATS = [
    ("GeoTIFF (tiled, DEFLATE)", "GTiff"),
    ("Cloud-Optimized GeoTIFF (COG)", "COG"),
]

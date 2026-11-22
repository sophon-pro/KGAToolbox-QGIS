# Satellite Imagery Archive

|  |  |
|---|---|
| **Algorithm ID** | `kga:satellite_imagery_archive` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/satellite_imagery_archive_alg.py` (panel in `kga_tools/gui/historical_imagery/`, engine in `kga_tools/core/historical_imagery/`) |
| **Icon** | `icons/imagery_basemaps/historical_imagery.png` |
| **Type** | Interactive dock panel, opened as a tab beside the Catalog panel — shares the Imagery Downloader toolbar drop-down; needs the QGIS window and an internet connection |

## Overview

Searches open imagery archives for an area and a date range, shows scene
thumbnails and footprints, and downloads the chosen scenes clipped to the area
as GeoTIFF or Cloud-Optimized GeoTIFF with a JSON metadata file. Only the area
is transferred: cloud-optimised rasters are read with HTTP range requests, so a
10 × 10 km Sentinel-2 subset takes seconds rather than a whole 100 km tile.

Searching, thumbnails and downloads run as background QGIS tasks.

It complements **Imagery Downloader**, which stitches current web-map tiles
(Google Satellite); this tool is for dated scenes with cloud and coverage
figures.

## Sources

| Source | Archive | Resolution | Account |
|---|---|---|---|
| ESA Sentinel-2 L2A (Element 84 Earth Search, AWS) | 2015 → | 10 m | No |
| USGS Landsat 4–9 Collection 2 Level-2 (Microsoft Planetary Computer) | 1982 → | 30 m | No |
| Google Earth Engine | 1984 → | 10–30 m | Yes — `earthengine-api`, a registered Cloud project, one-time sign-in |
| OpenAerialMap | varies | cm – m | No (CC-BY 4.0) |
| Custom STAC catalog | any | any | depends |

## Parameters (panel tabs)

| Tab | Controls |
|---|---|
| **Search** | Source and collection; area as *Map view*, *Draw box* or *Polygon* (a polygon layer, optionally selected features only; with Map Tabs on, the list shows only the layers of the active map); *Year* or *From/To*; *Max cloud*; *Min coverage* of the area; number of results. |
| **Results** | Thumbnails with date, satellite, cloud %, area coverage, pixel size and tile. Filter by year, sort (newest, oldest, least cloudy, best coverage); footprints are outlined on the map; zoom to, select all, add a footprints layer. The **Preview on map** button draws the selected scene(s) (up to 4) on the map as a temporary layer in an *Imagery Preview* group. The part of the scene in view is read once at screen resolution (a few seconds) into a temporary local file; zoom in or pan out of it and the preview re-reads itself. Turn the button off to remove the layers. Not available for Earth Engine. |
| **Download** | Band combination, resolution (native, 50 %, 25 %, 10/30/60 m), GeoTIFF or COG, *Clip to polygon shape*, output folder, add to map, `.json` metadata, quality report. A pre-download check shows expected size and warns about partial coverage, cloud and the size limit. |
| **Settings** | Earth Engine project, Planetary Computer key (optional), custom STAC catalog, size limit, timeout. |

## Outputs

`<SRC>_<YYYYMMDD>_<scene_id>_<bands>.tif` plus a `.json` sidecar (source,
acquisition, processing, quality). Existing files are never overwritten. Alpha
bands and masks become NoData, reflectance scale and offset are written into the
file, and each file is checked after download (corrupt tiles, missing
georeferencing, empty output, saturation, and a pixel comparison with the source
at native resolution). A report dialog lists the results.

## Notes and limits

* Landsat and Sentinel-2 requests go to third-party servers; their terms and
  rate limits apply. Cite the data providers (Copernicus, USGS, OpenAerialMap).
* Google Earth Engine is optional; downloads are limited to about 32 MB each.
* Downloads read the area in many parallel pieces; on a slow or distant connection the
  archive's server speed is still the limit. Reprojected, resampled or clipped
  outputs use GDAL's warper and are slower.
* Output larger than the size limit (400 MP per band by default) is blocked.
* Settings are stored under `kga_tools/historical_imagery/` in the QGIS profile.
* Tests: `tests/historical_imagery/` (`test_core.py` offline unit tests,
  `test_integration.py` wiring and drop-down). The folder is not part of the
  repository.

# Imagery Downloader

|  |  |
|---|---|
| **Algorithm ID** | `kga:imagery_downloader` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/imagery_downloader_alg.py` (dialog in `kga_tools/gui/imagery_downloader_dialog.py`, engine in `kga_tools/core/imagery/`) |
| **Icon** | `icons/imagery_download.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window and an internet connection |

## Overview

Downloads web-map raster tiles for an area, stitches them into one
georeferenced image, saves it in the chosen format and CRS, and adds it to the
map below the vector layers. The source list is data-driven
(`core/imagery/sources.py`); the only source shipped is **Google Satellite**.

Tiles are fetched through QGIS's network stack (proxy and authentication
honoured) by a background task with a thread pool, retries with back-off and
automatic slowing down when the server throttles. Tiles are written into a
sparse tiled GeoTIFF work file as they arrive, and a checkpoint records which
are done, so an interrupted job resumes from the same folder without
re-downloading anything.

## Parameters

| Control | Meaning |
|---|---|
| **Imagery source** | Entry from the source registry. |
| **Download extent** | Map canvas, layer extent, box drawn on the map, or polygon layer (bounding box downloaded, pixels outside masked; optional *Selected features only*). |
| **Zoom level** | Source min–max zoom; the ground resolution at the area's latitude is shown. *Auto* matches the canvas scale. |
| **Output CRS** | Project CRS (default) or a selected CRS. EPSG:3857 is pixel-exact; others are reprojected (bilinear). |
| **Format** | GeoTIFF, JPEG, PNG, JPEG 2000, ECW — only those the local GDAL can write. |
| **Build pyramids** | Internal overviews for GeoTIFF. |
| **Transparent outside polygon** | Clip mode: transparent where the format has alpha, otherwise white. |
| **Connections** | Parallel downloads, 1–16. |
| **Output folder / File name** | Where the result and, if enabled, the checkpoint go. |
| **Enable checkpoint** | Keep `<name>.kga_checkpoint.json` and `<name>_work.tif` so a stopped job can resume. |

## Outputs

The finished image, georeferenced: embedded for GeoTIFF / JPEG 2000 / ECW; a
world file (`.jgw` / `.pgw`), `.prj` and `.aux.xml` for JPEG / PNG. The layer is
added to the project with the source attribution in its metadata. The work file
and checkpoint are removed after a successful export.

## Notes and limits

* Google's terms restrict bulk downloading and offline use of map tiles. The
  tool shows a notice and asks for a one-time-per-session acknowledgement; the
  user is responsible for having the right to use the imagery.
* Extents crossing the antimeridian are rejected. Latitudes are clamped to
  ±85.0511°. Above 5,000,000 tiles Download is disabled.
* JPEG is limited to 65,500 px per side and has no alpha (empty areas white).
* ECW is offered only when GDAL has an ECW writer with an encode licence key.
* The work file needs about 3 bytes per pixel of the tile mosaic in free disk
  space in addition to the final image.
* Tests: `tests/imagery_downloader/` (tile maths, estimator, checkpoint, engine
  and dialog against a local fake tile server).

# Generate Contour for ROI

|  |  |
|---|---|
| **Algorithm ID** | `kga:generate_contour_roi` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/generate_contour_roi_alg.py` (dialog in `kga_tools/gui/contour_roi_dialog.py`, engine in `kga_tools/core/contours.py`, symbology in `kga_tools/core/contour_style.py`) |
| **Icon** | `icons/generateContour.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window and an internet connection |

## Overview

Makes contour lines for any area of interest in one step. The tool downloads the
free **Copernicus DEM** (30 m or 90 m, from the AWS open-data bucket — no account
and no API key), cuts it to the area, traces the contours at the chosen interval
and adds them to the map, dark and thick for the main contours and thin and grey
for the rest, with elevation labels.

The job runs on a background task, so QGIS stays usable. **One progress bar
covers all of it** — reading and cutting the DEM, tracing the lines, marking main
and sub contours and, if ticked, smoothing.

## Parameters

| Control | Meaning |
|---|---|
| **DEM** | Copernicus GLO-30 (1 arc-second, about 30 m) or GLO-90 (about 90 m), or **Local DEM** to use one you already have: pick a raster layer from the project or browse for any GDAL raster (a file takes precedence over the layer). It may be in any CRS, but must have one and be north-up. |
| **Area** | **Whole DEM** (local DEM only: no cutting), **Current map extent**, **Extent of a layer**, **Boundary of a polygon layer** (the DEM is cut to the shape; optional *Selected features only*), or **Draw a rectangle or polygon** on the map. |
| **Interval** | Vertical distance between contours, 0.1–5000 m. Lines fall on whole multiples of it (0, 10, 20 … for 10 m). |
| **Main contour** | Every Nth contour (2–20) is a main contour. |
| **Smooth the lines** | Rounds off the pixel steps (one round of corner cutting). Better looking, about twice the vertices, and the lines move by less than a pixel. |
| **Symbolize** | Main contours dark and thick (0.5 mm), sub contours grey and thin (0.15 mm). Untick for QGIS's default line style. |
| **Labels** | **Main contours only**, **All contours**, or **No labels**. |
| **Terrain surfaces** | Optionally derive a **Hillshade** (lit from the north-west), **Slope** (degrees) and **Aspect** (0-360 degrees clockwise from north) from the DEM. |
| **Add random spot height** | Scatter spot heights at random over the area, spread evenly rather than clumped (set how many). Each is a small black dot with its height beside it, as on a topographic map, saved as the `spot_heights` layer in the contours GeoPackage. |
| **Output CRS** | **Current project CRS** (default), **Same as the DEM** (no reprojection) or a **Specified CRS**. The DEM is published in WGS 84 (EPSG:4326); for any other CRS it is warped (bilinear) and the contours are made in that CRS. |
| **Output folder** | Where the DEM and contours go. Empty means a temporary folder, which QGIS deletes when it closes. |
| **Name** | Base name: `<name>_DEM.tif` and `<name>_contours.gpkg`. |
| **Also add the DEM to the map** | Adds the clipped DEM with a pale elevation tint, below the vector layers. |

## How to use

1. Open **KGA Irrigation Tools > Generate Contour for ROI**.
2. Choose the area. For a drawn area, click *Draw rectangle* or *Draw polygon*
   and draw on the map.
3. Set the interval, the main-contour step, and the smoothing, symbology and
   label options.
4. Pick an output folder, or leave it empty for a temporary one.
5. Click **Generate contours**. Click **Stop** to cancel; nothing is added to the
   map.

## Outputs

* `<name>_contours.gpkg` — layer `contours`, in the output CRS, with the fields `ID`,
  `ELEV` (metres) and `TYPE` (`main` or `sub`).
* `<name>_DEM.tif` — the clipped DEM, compressed, in the output CRS. Outside a drawn
  shape or a layer boundary the DEM is NoData.

* `<name>_hillshade.tif`, `<name>_slope.tif`, `<name>_aspect.tif` — only the ones
  ticked, in the same CRS as the DEM, NoData -9999 (hillshade is Byte). They are
  added below the contours: the hillshade on top of the DEM at 60% opacity, slope
  and aspect switched off so they do not hide it. Slope and aspect use the Horn
  method; for a DEM in degrees the horizontal scale is worked out from the
  latitude, so the values match those from a projected CRS. Flat ground has no
  aspect and is NoData.

Layers made from Copernicus data carry its attribution in their metadata.

## Notes and limits

* **Large areas.** Above about 25 million DEM pixels (roughly 150 km square at
  30 m) the tool asks before it starts, with a sterner warning above 150 million,
  and also when the output drive is short of space. A big DEM is read and
  contoured in memory and can make a modest PC slow or freeze. A temporary
  output adds to the warning because it lands on the system drive. One request
  is limited to 700 million pixels.
* **Too many levels.** More than 20,000 levels (an interval of 0.1 m across a
  mountain range) is refused; the DEM is kept and the message says where.
* The Copernicus DEM is a surface model: it follows tree tops and buildings, not
  bare ground. Elevations are metres above the EGM2008 geoid.
* Attribution is required: Copernicus DEM © DLR e.V. 2010-2014 and © Airbus
  Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European
  Union and ESA.
* Open sea has no tiles; an area entirely over open sea is reported as having
  nothing to contour.

# CSV to DEM and Contour

|  |  |
|---|---|
| **Algorithm ID** | `kga:csvtodemcontour` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/csv_to_dem_contour_qgis.py` |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

Converts a surveyed point CSV straight into a DEM GeoTIFF and contour lines, in
one pass.

This is the small-job version of the workflow: one survey file, one interpolation
pass, contours out the other end. For a large survey, a resumable run, or a DEM
you already have, use **[DEM and Contour Tool](demcontourtool.md)** instead —
it is the same job with tiling, checkpointing and a DEM-only mode.

**CSV columns:** `No`, `Easting`, `Northing`, `Elevation`, `Code`. A dual
side-by-side layout — two blocks of columns on one sheet, as some total stations
export — is detected automatically.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input Point CSV** | File (`*.csv`) | — | The survey file. |
| **Output Folder** | Folder destination | — | |
| **DEM Resolution (m) [0 = auto-detect]** | Number, optional | `0.0` | Auto-detect derives a cell size from the point spacing. Set it when you need a specific grid. |
| **Interpolation Method** | Enum | `IDW` | `IDW`, `Linear`, `Cubic`, `Nearest Neighbour`. |
| **CRS – EPSG Code** | Integer | `32648` | The CRS the Easting/Northing values are already in. `32648` is WGS 84 / UTM zone 48N, which covers most of Cambodia. |
| **Generate Contour Lines** | Boolean | `True` | Off, only the DEM is produced. |
| **Contour Interval (m)** | Number (min 0.01) | `1.0` | |
| **Contour Smoothing – Gaussian σ (0 = off)** | Number (min 0.0) | `1.0` | Smooths the surface before contouring, so the lines are not ragged. `0` contours the raw grid. |
| **Contour Output Format** | Enum | `Shapefile (.shp)` | Or `GeoPackage (.gpkg)`. |
| **Add output layers to QGIS map canvas** | Boolean | `True` | |

## How to use

1. Open **KGA Irrigation Tools > CSV to DEM and Contour**.
2. Pick the **Input Point CSV** and an **Output Folder**.
3. Set the **CRS – EPSG Code** to the system the survey was recorded in. Getting
   this wrong is what puts the DEM in the wrong hemisphere.
4. Leave **DEM Resolution** at `0` for the first run and look at what the
   auto-detected cell size gives you.
5. Choose the **Interpolation Method** — `IDW` is the safe default for scattered
   survey points; `Linear` and `Cubic` assume a denser, more even sample.
6. Set the **Contour Interval** and press **Run**.

## Outputs

In the output folder, named after the input CSV:

- `{name}_DEM.tif` — the interpolated DEM as a GeoTIFF.
- `{name}_Contour_{interval}m.shp` (or `.gpkg`) — the contour lines, when
  *Generate Contour Lines* is on.

Both are added to the map canvas when that box is ticked.

## Notes and limits

- **Requires numpy, pandas and scipy**, all bundled with QGIS.
- **The EPSG code is the CRS the numbers are already in**, not one to reproject
  to.
- **Smoothing changes the surface, not just the lines.** A Gaussian σ of 1 is a
  light touch; raise it only if the survey is noisy, and remember the contours
  then no longer pass exactly through the surveyed elevations.
- **`Cubic` can overshoot** beyond the range of the input points at the edges of
  the survey, producing elevations no one measured. Check the DEM's min and max
  against the CSV.
- For a large survey, a run you need to resume, or a DEM you already have, use
  **[DEM and Contour Tool](demcontourtool.md)**.

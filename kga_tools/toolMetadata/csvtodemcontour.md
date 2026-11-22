# CSV to DEM and Contour

|  |  |
|---|---|
| **Algorithm ID** | `kga:csvtodemcontour` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/csv_to_dem_contour_qgis.py` |
| **Icon** | group icon (`icons/toolbox_groups/irrigation.png`) |
| **Type** | Window (Toolbox opens a dedicated dialog); the algorithm is still scriptable and usable in the modeller and batch |

## Overview

Converts a surveyed point CSV straight into a DEM GeoTIFF and contour lines, in
one pass.

This is the small-job version of the workflow: one survey file, one interpolation
pass, contours out the other end.

**CSV columns:** `No`, `Easting`, `Northing`, `Elevation`, `Code`. A dual
side-by-side layout — two blocks of columns on one sheet, as some total stations
export — is detected automatically.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input · Point CSV** | File (`*.csv`) | — | Surveyed points, one per row. |
| **Input · X (Easting) field** | String | auto | Pick the easting column from the CSV's header, or type a name or column number. Blank = auto-detect. |
| **Input · Y (Northing) field** | String | auto | As above, for northing. |
| **Input · Z (Elevation) field** | String | auto | As above, for elevation. Fill all three of X, Y and Z, or none. |
| **DEM · Interpolation method** | Enum | `IDW – Inverse Distance Weighting` | Also `Linear – Delaunay triangulation`, `Cubic – smooth surface`, `Nearest Neighbour`, `RBF – Radial basis functions (smooth)`. Each has its own settings, below. |
| **DEM · Cell size (m)  [0 = auto]** | Number | `0.0` | Auto derives a cell size from the point density (minimum 0.5 m). Set it when you need a specific grid. Halving it quadruples the cell count. |
| **DEM · CRS (EPSG code)** | Integer | `32648` | The CRS the Easting/Northing values are already in; it only labels the outputs and nothing is reprojected. `32648` is WGS 84 / UTM zone 48N, which covers most of Cambodia. |
| **Contours · Generate contour lines** | Boolean | `True` | Off, only the DEM is produced. |
| **Contours · Interval (m)** | Number (min 0.01) | `1.0` | |
| **Contours · Smoothing, Gaussian σ (0 = off)** | Number (min 0.0), advanced | `1.0` | Smooths the surface before contouring, so the lines are not ragged. `0` contours the raw grid. The DEM itself is unchanged. |
| **Contours · Output format** | Enum, advanced | `Shapefile (.shp)` | Or `GeoPackage (.gpkg)`. |
| **Output · Folder** | Folder destination | — | The DEM and contours are written here, named after the CSV. |
| **Output · Add results to the map** | Boolean, advanced | `True` | Loads the DEM and contours into the current project. |

### Settings that change with the method

The window shows only the settings of the chosen method; in the Processing form
they are under *Advanced Parameters*.

| Method | Setting | Default | Meaning |
|---|---|---|---|
| **IDW** | IDW · Power | `2` | How fast influence falls with distance. |
| | IDW · Neighbours | `16` | Nearest points averaged per cell. |
| | IDW · Search radius (m)  [0 = no limit] | `0` | Points further away are ignored; a cell with none in range takes the nearest point's height. |
| **Linear** | Linear/Cubic · Fill outside the point outline | on | Off leaves the area outside the points as NoData. |
| **Cubic** | Linear/Cubic · Fill outside the point outline | on | As Linear. |
| | Cubic · Clamp to the surveyed height range | off | Stops overshoot above the highest or below the lowest point. |
| **Nearest** | — | | No settings. |
| **RBF** | RBF · Kernel | `thin_plate_spline` | Also `cubic`, `quintic`, `linear`. |
| | RBF · Neighbours | `50` | Nearest points per local fit. |
| | RBF · Smoothing | `0` | `0` passes through every point; raise it for noisy or duplicated points. |

## How to use

1. Open **KGA Irrigation Tools > CSV to DEM and Contour**. A window opens.
2. Pick the point CSV. The X, Y and Z boxes fill from its header and the window reports how many points it read and their elevation range; check that range against what you expect. Choose an output folder.
3. Set the **Coordinate system** to the system the survey was recorded in. Getting
   this wrong is what puts the DEM in the wrong hemisphere.
4. Leave **Cell size** on `0` (auto) for the first run and look at what the
   auto-detected cell size gives you.
5. Choose the **Interpolation method** — `IDW` is the safe default for scattered
   survey points; `Linear` and `Cubic` assume a denser, more even sample.
6. Set the contour **Interval** and press **Run**.

## Outputs

In the output folder, named after the input CSV:

- `{name}_DEM.tif` — the interpolated DEM as a GeoTIFF.
- `{name}_Contour_{interval}m.shp` (or `.gpkg`) — the contour lines, when
  *Generate contour lines* is on.

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

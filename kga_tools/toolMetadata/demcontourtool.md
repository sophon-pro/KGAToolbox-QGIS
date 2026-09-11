# DEM and Contour Tool

|  |  |
|---|---|
| **Algorithm ID** | `kga:demcontourtool` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/dem_contour_tool_qgis.py` (fact sheet: `kga_tools/algorithms/dem_contour_tool_qgis_notes.txt`) |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

The production version of the CSV-to-contour workflow: two modes, tiled contour
generation, and a checkpoint file so an interrupted run picks up where it left
off.

- **Mode A — CSV → DEM → Contour.** Interpolates surveyed points into a DEM
  GeoTIFF, then contours it.
- **Mode B — DEM → Contour only.** Skips interpolation and contours a DEM you
  already have.

Contours are generated in row tiles and progress is written to
`checkpoint.json` in the output folder after every tile. A run that is
interrupted — a crash, a closed laptop, a cancelled job — continues
automatically when you run it again with the same DEM, output folder and
parameters. And every field is remembered between runs, so resuming needs no
retyping.

For a small one-off survey, **[CSV to DEM and Contour](csvtodemcontour.md)** is
the simpler tool.

## Parameters

Every parameter except *Force re-run* is remembered from your last run and
pre-filled. The defaults below are what you get on a first run.

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input Mode** | Enum | `Mode A — CSV → DEM → Contour` | Or `Mode B — DEM → Contour only`. |
| **Input Point CSV [Mode A only]** | File (`*.csv`), optional | remembered | Single-layout and dual side-by-side CSV formats are both supported. |
| **DEM Resolution (m) [Mode A — 0 = auto-detect]** | Number, optional | `0.0` | |
| **Interpolation Method [Mode A only]** | Enum | `Auto-detect` | `Auto-detect` (analyses point count and density), `IDW` (dense surveys), `Linear` (fast, moderate point counts), `Cubic` (smooth surfaces, wide spacing), `Nearest` (very sparse or classified data). |
| **CRS — EPSG Code [Mode A only]** | Integer | `32648` | WGS 84 / UTM zone 48N — Cambodia. |
| **Input DEM Raster [Mode B only]** | Raster layer, optional | remembered | |
| **Output Folder** | Folder destination | remembered | Also where `checkpoint.json` lives. |
| **Generate Contour Lines** | Boolean | `True` | |
| **Contour Interval (m)** | Number (min 0.01) | `1.0` | |
| **Contour Smoothing — Gaussian σ (0 = off)** | Number (min 0.0) | `1.0` | |
| **Contour Output Format** | Enum | `GeoPackage` | Shapefile or GeoPackage. |
| **Tile height (rows)** | Integer (min 100) | `3000` | Rows per tile. Lower it if memory is tight; raise it for fewer, larger tiles. |
| **Tile overlap (rows)** | Integer (min 1) | `30` | Overlap between tiles, so contour lines join cleanly across the seams. |
| **Force re-run everything (ignore checkpoint.json in output folder)** | Boolean | **`False`, always** | The one field that is never remembered. |
| **Add output layers to QGIS map canvas** | Boolean | `True` | |

## How to use

1. Open **KGA Irrigation Tools > DEM and Contour Tool**.
2. Pick the **Input Mode**. Mode A starts from a survey CSV; Mode B starts from
   an existing DEM.
3. Fill in the parameters that mode needs — the others are ignored.
4. Press **Run**. Progress is reported tile by tile.
5. **If the run is interrupted, simply run it again** with the same DEM, output
   folder and parameters. The log says how many tiles were already done.
6. If you changed something the checkpoint should not survive, tick **Force
   re-run**.

## Outputs

In the output folder:

- `{name}_DEM.tif` — the interpolated DEM (Mode A only).
- `{name}_Contour_{interval}m.gpkg` or `.shp` — the contour lines.
- `checkpoint.json` — resume state: a fingerprint of the inputs plus the tiles
  completed. Safe to delete once you are finished.

Layers are added to the canvas when that box is ticked.

## Notes and limits

- **The checkpoint is keyed on a fingerprint of the inputs.** Change the DEM, the
  interval, the smoothing or the tiling and the checkpoint is treated as stale
  and the work restarts. A checkpoint written by an older version of the script
  is also discarded.
- **Force re-run always resets to unchecked**, so a resumed run never silently
  throws away completed tiles.
- **Requires numpy, pandas, scipy and GDAL/OGR/OSR**, all bundled with QGIS.
  Contours use a tiled `gdal.ContourGenerateEx` with row-block streaming, which
  is what makes a large DEM possible at all.
- **This is the one algorithm with no `helpUrl()`**, so the Help button in the
  Processing dialog has nothing to open.
- Settings are remembered through QSettings, per QGIS profile — a different
  profile starts with the plain defaults.

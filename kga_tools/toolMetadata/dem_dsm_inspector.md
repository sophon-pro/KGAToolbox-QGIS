# DEM / DSM Inspector

|  |  |
|---|---|
| **Algorithm ID** | `kga:dem_dsm_inspector` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/dem_dsm_inspector_alg.py` (panel in `kga_tools/gui/dem_dsm_inspector/`, engine in `kga_tools/core/dem_dsm_inspector/`) |
| **Icon** | `icons/terrain_water/demDsmInspector.png` |
| **Type** | Interactive dock panel, opened as a tab beside the Catalog panel — needs the QGIS window; no internet |

## Overview

Diagnoses whether an elevation raster is a **bare-earth DEM/DTM** or a **DSM**
(a surface with buildings and vegetation). It returns a classification
(DEM, DSM, PARTIALLY FILTERED DEM or INCONCLUSIVE), a confidence score and a
report with eight indicators. Use it before trusting a DEM for canal levels,
contours or flow routing. It runs on a background task and can be cancelled.

Uses only NumPy and GDAL, which ship with QGIS (SciPy is optional and only
speeds up blob labelling).

## Using the panel

| Page | What it holds |
|---|---|
| **Layer** | The raster, its band and a summary (real resolution, matching orthophoto). Selecting a layer clears the previous result and probes in the background whether the data are native, interpolated or resampled. |
| **Orthophoto** | An image of the same area and its bands (NIR optional) and a building-footprint layer. |
| **Compare** | A second elevation layer of the same area. |
| **Filter** | Multi-scale filter windows, presets, a live preview of the windows used, and the filtered-surface / dZ layers. |
| **Settings** | Scope (whole layer or current map extent), elevation units, tile sampling, thresholds, block grid, **Restore defaults**. |
| **Summary, Indicators, Blocks, Filter chart, Log** | The verdict card, the report, the eight indicators, the block grid (click a block on the map to open the Block Inspector), the filter chart and the run log. Greyed out until the selected layer has a result. |

The footer is always visible: result chip (e.g. "DSM 95%"), progress and **Run**.

## The indicators

Each scores from -1 (bare earth) to +1 (surface objects) with a reliability weight.

| # | Indicator | DSM signature |
|---|---|---|
| 1 | Metadata and naming (product registry, DSM/DTM keywords) | Known surface product or a "DSM" keyword |
| 2 | Local height above ground (flat terrain only) | Many pixels above the threshold |
| 3 | Slope variance / steep edges | Wall-like edges in flat areas |
| 4 | Roughness asymmetry | Skewed upward |
| 5 | Profile spikes | Frequent narrow spikes |
| 6 | Structure / vegetation footprints | Most raised blobs are sharp-edged |
| 7 | Pairwise nDSM *(optional)* | The DSM is higher wherever objects stand |
| 8 | Orthophoto land-cover height test *(optional)* | Trees and roofs stand metres above adjacent open ground; 0.5–2 m means a partially filtered DTM |

Thresholds adapt to pixel size and sensor noise. Confidence is capped by what
the data can prove: ≤2 m pixels 97%, ≤5 m 94%, ≤10 m 90%, ≤20 m 85%, ≤35 m 78%,
coarser 65%; a DEM verdict rests on the absence of objects and is capped at 85%
(95% with a clear orthophoto result). Below 62% confidence the result is
INCONCLUSIVE; the report lists every cap applied.

## Outputs

* Reports: HTML, PDF, JSON, CSV, or **Pop out** for a full-size report.
* Optional map layers: object-height rasters, filtered surface and dZ, a colour-coded
  block grid, and a **Highest cells** point layer (red ≥ 4.5 m, orange 2–4.5 m,
  green < 2 m) to check against the orthophoto. Written to a temporary folder.

## Notes and limits

* A DSM of bare land (desert, fresh fields, water) is physically identical to a
  DEM; no raster-only test separates them. Add an orthophoto or a second layer.
* Only file-based GDAL rasters are supported, not WMS/WCS.
* Mountainous scenes leave little flat context, so confidence drops; use the
  map-extent scope on a flatter area.
* Resampled rasters (e.g. Copernicus 30 m warped to 0.5 m) are detected and read
  at their real resolution. Raised house plots and mounds are treated as ground.
* Very large roofs or continuous canopy wider than the maximum object width are
  detected only at their edges; raise the width for dense forest.

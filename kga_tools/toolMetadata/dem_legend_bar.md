# DEM Legend Bar

|  |  |
|---|---|
| **Algorithm ID** | `kga:dem_legend_bar` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/HypsometricMap.py` |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

Generates a hypsometric legend bar as a PNG for one or more DEM rasters — the
elevation colour ramp with tick marks and labels that goes beside a map in a
report or on a plan sheet.

QGIS's own legend for a continuous raster is not built for print. This writes a
standalone image you place in a layout, a Word document or a drawing, at the DPI
you need.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **DEM Raster Layer(s)** | Multiple raster layers | — | One legend bar is produced per DEM. |
| **Color Scheme** | Enum | `from_qgis` | `from_qgis` (reuse the layer's own ramp), `YlGn_r`, `turbo`, `jet`, `RdYlBu_r`, `terrain`, `plasma`, `viridis`, `gist_earth`. |
| **Legend DPI (150=Fast \| 300=Balanced \| 600=HQ)** | Enum | `150 (Fast)` | Also `300 (Balanced)` and `600 (High Quality)`. Use 300 or 600 for anything that will be printed. |
| **Output Folder** | Folder destination | — | Where the PNGs are written. |
| **Number of Ticks** | Integer (3–20) | `7` | |
| **Legend Title** | String | `Elevation (m)` | |
| **Font Family** | String | `Times New Roman` | |
| **Font Tick Size** | Integer (6–24) | `9` | |
| **Font Title Size** | Integer (6–24) | `10` | |

## How to use

1. Load the DEM(s) and, if you want the map's own colours, style them first.
2. Open **KGA Irrigation Tools > DEM Legend Bar**.
3. Pick the DEMs and an output folder.
4. Leave **Color Scheme** on `from_qgis` to match the map exactly; pick a named
   ramp only when you want the legend to differ from the layer.
5. Set the **Legend DPI** to match the destination — 150 for screen, 300 or 600
   for print.
6. Press **Run**, then place the PNGs in your layout.

## Outputs

- One PNG legend bar per input DEM, in the output folder, at the chosen DPI.

## Notes and limits

- **Tick placement is auto-adaptive** from the DEM's elevation range, so the
  labels land on round numbers rather than on the exact minimum and maximum:
  - flat terrain (range ≤ 30 m) → 5 m steps
  - hills (≤ 300 m) → 50 m steps
  - mountain → 100, 200 or 500 m steps

  **Number of Ticks** is a target, not a guarantee: the result is clamped to
  between 4 and 10 ticks.
- `from_qgis` reads the layer's current renderer, so restyling the DEM changes
  the legend — regenerate it after any styling change.
- The font must be installed on this machine; an unavailable family falls back
  silently.

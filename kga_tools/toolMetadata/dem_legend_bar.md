# DEM Legend Bar

|  |  |
|---|---|
| **Algorithm ID** | `kga:dem_legend_bar` |
| **Group** | KGA Mapping |
| **Source** | `kga_tools/algorithms/HypsometricMap.py` |
| **Icon** | group icon (`icons/Mapping.png`) |
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
| **Input · DEM raster layer(s)** | Multiple raster layers | — | One legend bar is produced per DEM, from that layer's own minimum and maximum elevation. |
| **Style · Colour scheme** | Enum | `from_qgis` | `from_qgis` (copy the colour ramp of each layer's current QGIS style), or a fixed ramp: `YlGn_r`, `turbo`, `jet`, `RdYlBu_r`, `terrain`, `plasma`, `viridis`, `gist_earth`. |
| **Style · Legend title** | String | `Elevation (m)` | Text printed beside the bar. |
| **Style · Number of ticks (approx.)** | Number | `7` | About this many labels. Steps are round numbers, so the count can differ slightly. |
| **Font · Family** | String, advanced | `Times New Roman` | |
| **Font · Tick label size (pt)** | Number, advanced | `9` | |
| **Font · Title size (pt)** | Number, advanced | `10` | |
| **Output · Resolution (DPI)** | Enum | `150 (Fast)` | Also `300 (Balanced)` and `600 (High Quality)`. Use 300 or 600 for anything that will be printed; the image is 4 times larger at 600. |
| **Output · Folder** | Folder destination | — | Where the PNGs are written. |

## How to use

1. Load the DEM(s) and, if you want the map's own colours, style them first.
2. Open **KGA Mapping > DEM Legend Bar**.
3. Pick the DEMs and an output folder.
4. Leave **Colour scheme** on `from_qgis` to match the map exactly; pick a named
   ramp only when you want the legend to differ from the layer.
5. Set the **Resolution (DPI)** to match the destination — 150 for screen, 300 or
   600 for print.
6. Press **Run**, then place the PNGs in your layout.

## Outputs

- One PNG legend bar per input DEM, in the output folder, at the chosen DPI.

## Notes and limits

- **Tick placement is auto-adaptive** from the DEM's elevation range, so the
  labels land on round numbers rather than on the exact minimum and maximum:
  - flat terrain (range ≤ 30 m) → 5 m steps
  - hills (≤ 300 m) → 50 m steps
  - mountain → 100, 200 or 500 m steps

  **Number of ticks** is a target, not a guarantee: the result is clamped to
  between 4 and 10 ticks.
- `from_qgis` reads the layer's current renderer, so restyling the DEM changes
  the legend — regenerate it after any styling change.
- The font must be installed on this machine; an unavailable family falls back
  silently.

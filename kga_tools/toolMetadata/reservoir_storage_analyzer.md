# Reservoir Storage Analyzer

|  |  |
|---|---|
| **Algorithm ID** | `kga:reservoir_storage_analyzer` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/reservoir_storage_analyzer_alg.py` (window in `kga_tools/gui/reservoir_storage_dialog.py`, calculations in `kga_tools/core/reservoir_storage.py`) |
| **Icon** | `icons/storageCapacityCurve.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |
| **Replaces** | *Storage Capacity Curve* (`kga:storage_capacity_curve`) |

## Overview

Works out how much water a reservoir holds at each water level, from its DEM.
It builds the **elevation–area–volume** table, draws the elevation–volume and
elevation–area curves, and goes on from there to inundation maps, a
volume-to-level lookup, depth rasters, and a way to make a DEM from contours.

All calculation is in metres, square metres and cubic metres. The saved table
carries the volume in m³, 1,000 m³ and million m³, so the same file serves a
design report and a map label.

## The window

The left panel holds the inputs shared by every tab; the tabs on the right
use them.

### Input and settings

| Control | Meaning |
|---|---|
| **DEM** | Elevation raster in a projected CRS in metres. A geographic (degree) DEM is refused. The panel shows its size, cell size, CRS and elevation range. |
| **Boundary** | Optional polygon. Only cells inside it are used. |
| **Split line** / **Keep** | Optional line (for example a dam axis) that must cross the whole DEM. Keep the **west, east, north or south** side of it. A line that does not cut the DEM in two is ignored with a warning. |
| **From / To / Step** | Water levels to tabulate. **DEM range** fills From and To from the lowest and highest cell. Levels that miss the DEM entirely are moved onto it when a DEM is chosen. |
| **Or levels** | A list such as `40, 41.5, 43.8` used instead of From/To/Step. |
| **Surface** | **Natural ground** fills the DEM as it is. **Excavated to base level** lowers the ground inside the flooded area to the **base level** first, and reports the soil removed separately. |
| **Smoothing** | Gaussian smoothing before the calculation. Off by default; it changes volumes slightly, so leave it off for reported figures. |
| **Volume unit** | Unit on the curve and map labels (million m³, 1,000 m³ or m³). The CSV always has all three. |
| **Folder** | Where the table and image are saved after **Calculate Storage**. |

**Stop** ends the running job at the next step. Closing the window while a job
runs stops it first.

### Tabs

- **Curves** and **Table** — the result of **Calculate Storage**: the
  elevation–volume and elevation–area curves (whole-metre levels circled) and the
  table behind them. With the excavated surface the natural-ground volume and the
  soil removed are added. Save the table as CSV and the curve as PNG, PDF or SVG.
- **Inundation Maps** — one panel per water level on a hillshade, with water
  depth shaded, the flooded edge, background contours, scale bar, north arrow, a
  summary table (area, storage, maximum depth) and a footer. Pick a paper size
  (A4–A0, Letter, Tabloid, or fit to content), orientation, resolution and
  contour settings; save as PNG, PDF, TIFF, JPEG or SVG. Leave the levels empty to
  spread five over the DEM.
- **Volume Lookup** — the water level (and area) for volumes you give, read from
  the last calculation or from a CSV saved by this tool. Levels are interpolated
  linearly between rows.
- **Export Rasters** — one GeoTIFF per level, clipped to the flooded area: **water
  depth** and/or **bottom elevation**. Summing depth × cell area reproduces the
  Table's volume, which makes them useful for checking in other software.
- **Contours to DEM** — interpolates a DEM from elevation contour lines (linear,
  cubic, inverse distance or nearest), optionally clipped to a polygon in any CRS.
  The result is added to the map and selected as the input.
- **Log** — what each job did, and any warning.

## How the volume is counted

- **Flooded area** is the set of cells below the water level that are connected to
  the **lowest cell** of the DEM. Isolated pits are not counted.
- **Natural ground:** volume = Σ (water level − ground) over the flooded area ×
  cell area.
- **Excavated:** the bed is `min(ground, base level)`; volume = Σ max(level − bed,
  0) × cell area; soil removed = Σ max(ground − base level, 0) × cell area.

## Outputs

In the output folder, per DEM: `{name}_storage.csv` and
`{name}_storage_curve.png`. Maps are saved where you choose (default
`{name}_inundation.png`); rasters go to a `rasters` folder.

CSV columns: `Water_Level_m`, `Area_m2`, `Area_ha`, `Area_km2`, `Volume_m3`,
`Volume_1000m3`, `Volume_Mm3`, and with the excavated surface `Natural_Vol_m3` and
`Excavated_Soil_m3`.

## Watch out for

- **The lowest cell decides which water body is counted.** A single stray low
  pixel outside the reservoir (a DEM artefact, a borrow pit) takes over, and the
  reservoir itself then reads as empty. Use a boundary polygon or split line so
  the lowest cell is inside the reservoir.
- A DEM with an undeclared NoData value (such as `-9999`) is flagged in the Log
  when the lowest cell is below −500 m. Set the NoData value first.
- The whole DEM is read into memory as 64-bit numbers: a very large raster needs
  a boundary or a coarser copy.
- A DEM made from contours is a model. Interpolating between lines flattens hill
  tops and pits, and the area outside the outermost contour takes the nearest
  contour's height — clip it to the reservoir.
- Levels cost one pass over the DEM each; more than 2,000 levels are refused.

## Migrating from Storage Capacity Curve

The old tool took several DEMs in one run and wrote one chart per DEM with fixed
axis settings. This one works on one DEM at a time and adds the surface option,
maps, lookup and rasters. The CSV columns changed: `Elevation_m` is now
`Water_Level_m`, `Volume_1000m3` is unchanged in meaning, and `Area_ha` is the
flooded area (connected to the lowest cell) rather than every cell under the
level.

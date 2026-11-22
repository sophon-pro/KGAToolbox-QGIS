# Paddy Irrigation Tools

|  |  |
|---|---|
| **Algorithm ID** | `kga:highest_point_per_block` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/highest_point_per_block_alg.py` (window in `kga_tools/gui/block_elevation_dialog.py`; step 1 engine `kga_tools/core/block_elevation.py`, step 2 window `kga_tools/gui/gravity_command_page.py` and engine `kga_tools/core/gravity_command.py`) |
| **Icon** | `icons/terrain_water/highestPoint.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Two steps in one window

The window has two tabs. **Step 1 - High points** finds the high (and low) ground of
every paddy block from a DEM. **Step 2 - Gravity command** then asks whether every
block can be irrigated by gravity: Field → TC → SC → MC → Reservoir. After a run in
step 1, **Next: Step 2** opens step 2 with the same block layer and the new
`HighestPoints` layer filled in. Step 2 also works on its own with any block layer
and any point layer (one point per block, with an elevation field).

## Step 1 - High points


For every feature of the ticked block layers (polygons **or** lines) the tool finds
the highest cell of a DEM and adds **one point layer**, `HighestPoints`, with
`src_layer`, `block_fid`, the block attributes you tick, `elev`, `X`, `Y`, `Lon`,
`Lat` and `Top_cells` (plus `dist_along` for lines: distance from the line start to
the point). Tick **Lowest point** as well to get a blue `LowestPoints` layer.

- **Polygons** count the cells whose centre is inside the polygon, like QGIS zonal
  statistics. **Lines** count every cell the line passes through, and the point is
  placed *on* the line.
- **Combine features with the same value of** gives one point per group (for
  example per BM name) instead of one per feature.
- `Top_cells` above 1 means a flat top: several cells share the highest value.

## Method (polygon blocks)

For gravity irrigation design (field → TC → SC → MC → reservoir) the raw highest
cell is often a tree crown, hut or dike. The **High / low point method** list
chooses what "highest" means:

- **Top of general slope** (default) — the DEM inside each block is ground-filtered
  (a morphological opening removes trees, dikes and huts narrower than the
  *Ground filter* window), then smoothed (*Smoothing* window) so the point marks
  the high side of the field, not a spike. `elev` is the ground elevation at that
  point (trees and dikes removed, not the smoothed value, so the water level
  needed is never under-estimated); `Smooth_z` is the smoothed value and
  `Raw_cell` the highest raw cell, for comparison.
- **Highest ground** — ground filter only.
- **Highest DEM cell** — the raw cell, trees and dikes included (lines always use this).

The two terrain methods also give each block a robust plane fit of its ground:
`Slope_pct`, `Fall_m` (high–low drop across the block), `Aspect` / `Flow_dir`
(downslope direction) and `Fit_R2`. They are added to the point layers, and
`Slope_pct`, `Fall_m`, `Flow_dir`, `Aspect` to the block layers when you write
results into them. A block with a fall under 5 cm is almost level and its
high/low point is weakly defined (the log says so).

## Parameters

| Control | Meaning |
|---|---|
| **DEM raster / Band** | The elevation raster. A DEM in degrees works but warns; reproject it to a metre CRS first. |
| **Block layers** | Any number of polygon and line layers, ticked in a list (filter, All, None, Filtered). *Selected features only* uses the selection of layers that have one. |
| **Zones** | *Each feature separately*, or *Combine features with the same value of* a field. |
| **Clean blocks** | Works on a copy; your layers are not changed. Invalid geometry is always repaired. Optionally snap boundaries within a distance, remove small overlaps (the shared area stays with the larger block), and fill small gaps. Rings you drew inside a block (trees, ponds, houses) are kept as excluded areas and never filled, so the point is searched only in the remaining area; untick *Keep rings* to treat them as gaps. Larger overlaps and gaps are only reported. A `Topology_issues` layer lists what was found or fixed. |
| **Point attributes** | Fields copied from the block that holds the peak. |
| **High / low point method** | See *Method* above. *Ground filter* is the width of the widest tree crown or dike to remove; *Smoothing* (10–30 m) sets how "general" the slope is. Needs a DEM in metres. |
| **Analysis resolution** | Native, 0.5 m, 1 m, 30 m or custom. When coarsening choose *Maximum* (keeps peaks, recommended for the raw method), *Average* or *Nearest*; the terrain methods always use Average (1 m with Average is fast and accurate enough). Whole-number ratios are aggregated exactly, and with Maximum the point is traced back to the exact cell of the original DEM. |
| **Small-block safety** | (Raw method only.) Splits cells (values unchanged) for blocks only a few cells wide, so they still get cells. |
| **Inner buffer** | Shrinks polygons to ignore dikes, roads and canal banks on their edges (2–5 m is typical). Ignored for lines. |
| **Find** | Highest point (red ▲) and/or lowest point (blue ▼). |
| **Create** | *Point layers only*, or also write `Higher_elevation`, `Elev_Min`, `Elev_Mean`, `Elev_Cells`, `MaxElev_X/Y` (and `Lowest_elevation`, `MinElev_X/Y`) into the block layers, or into new copies. |
| **Save results to file** | GeoPackage, Shapefile (names shortened to 10 characters) and/or Excel. Existing files are never overwritten: `_1`, `_2` … is added. |

## Watch out for

- Block layers in edit mode cannot be written to: save and stop editing, or tick
  *Create new output layers*.
- Run time grows with the number of blocks and the DEM resolution; use a coarser
  analysis resolution for a first pass.
- Block cleaning needs a DEM in metres and is skipped for a DEM in degrees.
- The result layers are temporary until you tick a save format.

## Step 2 - Gravity command

Blocks and high points come from step 1 (you may move points by hand; tick
*Re-sample elevation from the DEM* so moved points get a fresh ground elevation).
Canals and a reservoir are optional.

1. **Field level** each block needs: `FSL_req` = top of block + field water depth.
2. **Plot-to-plot supply**: neighbouring blocks (dikes and gaps up to the tolerance
   are bridged). Block A can feed block B when `top_A - top_B` is at least the spill
   loss. *Head* blocks have no neighbour high enough and need a canal turnout.
3. **Turnouts**: the nearest point on a canal within the search distance. The canal
   water level needed there is `FSL_req` + turnout loss + field-channel gradient ×
   field-channel length.
4. **Service**: blocks are processed from high to low, each served directly from a
   canal or plot-to-plot from a served higher neighbour.
5. **Canal levels**. *DESIGN* needs no water levels: it gives the water level each canal
   needs at its upstream end (TC → SC → MC) and the reservoir level needed, and names
   the block that controls each canal. *CHECK* compares your canal water levels with
   the need at every turnout, junction and the reservoir.
6. With a DEM, the ground along each canal shows how far the design level is above
   (fill) or below (cut) the ground.

| Tab | Contents |
|---|---|
| **Blocks** | Block layer and name field, high-point layer and elevation field, optional DEM re-sampling, point-to-block tolerance. |
| **Field** | Field water depth, plot-to-plot spill loss, neighbour gap tolerance, minimum shared border, prefer plot-to-plot supply. |
| **Canals** | Canal layer, name and level field (MC / SC / TC or 1 / 2 / 3), flow direction, junction tolerance, turnout search distance, losses and gradients, DESIGN / CHECK mode. |
| **Reservoir** | Full supply level check and an intake point that orients the main canal. |
| **Save** | Save every result layer to a GeoPackage, with an HTML report beside it. |

Results: `Command_Blocks` (Top_z, FSL_req, Head, Service, Status, Canal, Req_WL,
Canal_WL, Margin_m, Supplier ...), `Command_Links` (plot-to-plot arrows), `Turnouts`,
`Field_channels` and `Canal_Command` (canals drawn upstream → downstream with the
levels, critical block, margins and fill / cut), plus a plain-language report you can
save as HTML.

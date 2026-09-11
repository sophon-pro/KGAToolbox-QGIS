# Storage Capacity Curve

|  |  |
|---|---|
| **Algorithm ID** | `kga:storage_capacity_curve` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/StorageCapacityCurve.py` |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

Calculates **volume and area against elevation** for a reservoir from its DEM,
and plots the pair of curves engineers call the storage capacity curve.

It is the first number anyone asks for about a reservoir: how much water does it
hold at each water level, and how much surface does it flood. The tool steps the
water level from the DEM's low point upward, counting cells below each level, and
writes both the table and the chart.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **DEM Raster Layer(s)** | Multiple raster layers | — | One curve is produced per DEM, so several design options can be compared in one run. |
| **Boundary Layer (optional — leave blank for full DEM)** | Vector layer, optional | empty | Restricts the calculation to the reservoir footprint. Leave it blank to use the whole DEM. |
| **Elevation Step (m)** | Number (min 0.01) | `0.1` | The vertical increment. Smaller gives a smoother curve and a longer run. |
| **Output Folder** | Folder destination | — | |

### Plot axis controls — all optional, `0` means auto

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Plot: Elevation Y-axis Minimum (m)** | Number | `0.0` (auto from DEM) | |
| **Plot: Elevation Y-axis Maximum (m)** | Number | `0.0` (auto from DEM) | |
| **Plot: Volume X-axis Maximum (1,000 m³)** | Number | `0.0` (auto) | |
| **Plot: Area X-axis Maximum (ha)** | Number | `0.0` (auto) | |
| **Plot: Elevation Y-axis Tick Interval (m)** | Number | `0.0` (auto) | |
| **Plot: Volume X-axis Tick Interval (1,000 m³)** | Number | `0.0` (auto) | |
| **Plot: Area X-axis Tick Interval (ha)** | Number | `0.0` (auto) | |

## How to use

1. Load the DEM, and a polygon of the reservoir footprint if you have one.
2. Open **KGA Irrigation Tools > Storage Capacity Curve**.
3. Pick the DEM(s), the **Boundary Layer**, and an output folder.
4. Leave **Elevation Step** at `0.1` m for a first run; raise it if the DEM is
   large and the run is slow.
5. Press **Run**.
6. To compare several DEMs on the same axes — or to match a chart you produced
   earlier — set the axis maxima and tick intervals by hand and run again. The
   info bar on the chart shows which axes are custom and which are auto.

## Outputs

Per DEM, in the output folder:

- `volume_{name}.csv` — elevation, area and volume at each step.
- `graph_{name}.png` — the chart, with volume and area plotted against elevation.

The chart carries an info bar saying which axes were set by hand and which were
derived automatically.

## Notes and limits

- **Units on the chart are engineering units, not SI**: volume in thousands of
  cubic metres, area in hectares. The CSV holds the same numbers.
- **The DEM's horizontal units must be metres.** Volume is computed from cell
  area, so a DEM in degrees gives a meaningless answer. Reproject to the UTM zone
  first.
- **The result is only as good as the DEM.** Voids, NoData holes inside the
  footprint, and pits all change the count. Consider running
  **[DEM Elevation Correction and Point Sample Report](demelevationcorrectionandpointsamplereport.md)**
  first if you are merging survey sources.
- A smaller **Elevation Step** multiplies the run time; `0.1` m is usually fine.
- The axis controls only affect the chart. The CSV always holds the full range.
- For the discharge side of the same reservoir — how much goes *out* at each
  level — see **[Reservoir Rating Curve](reservoir_rating_curve.md)**.

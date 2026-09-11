# DEM Point Sample Export to Excel

|  |  |
|---|---|
| **Algorithm ID** | `kga:dempointsampleexport` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/dem_elevation_correction_and_point_sample_report.py` |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

Samples two or more DEMs at a regular grid of points and exports one row per
point to Excel: X, Y, an elevation column per DEM, a **Zone** column saying which
DEMs have valid data there, and difference columns against the finest reference
DEM and against each DEM's immediately-finer neighbour.

The use case is checking agreement between elevation sources — a reference DEM
plus the corrected DEMs from
**[DEM Elevation Correction](demelevationcorrectionandpointsamplereport.md)** —
in a spreadsheet you can sort, filter and hand to someone who does not use QGIS.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **DEM layers to sample (2 or more)** | Multiple raster layers | — | The order you select them in is the column order, and the finest is used as the reference for the difference columns. |
| **NoData overrides (comma-separated, matching DEM order)** | String, optional | empty | One value per DEM, in selection order. Blank uses each raster's own NoData metadata. |
| **Pixel stride (sample every Nth pixel)** | Integer (min 1) | `20` | |
| **Max rows (Excel limit is ~1,048,576)** | Integer (min 1) | `1000000` | |
| **Output file name** | String | `dem_point_samples.xlsx` | |
| **Output folder** | Folder destination | — | |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Exported Excel (or CSV) file** | File | |
| **Run summary** | String | |

## How to use

1. Load the DEMs to compare — the reference first is the clearest habit.
2. Open **KGA Irrigation Tools > DEM Point Sample Export to Excel**.
3. Select the DEMs, set the **Pixel stride**, and name the output.
4. Press **Run**.
5. Open the workbook and sort on a difference column — the largest disagreements
   are where the elevation sources actually conflict.

## Outputs

One workbook in the output folder with a row per sampled point:

| Column | Meaning |
|---|---|
| `X`, `Y` | Sample location |
| one column per DEM | Elevation there, or blank where that DEM has NoData |
| `Zone` | Which DEMs have valid data at this point |
| difference columns | Against the finest reference DEM, and against each DEM's immediately-finer neighbour |

## Notes and limits

- **Excel caps at about 1,048,576 rows per sheet.** The **stride is increased
  automatically** if the requested sampling would exceed the row limit you set,
  so a run always produces a readable file — but the grid may then be coarser
  than you asked for. The run summary says what stride was actually used.
- **Very large outputs may be written as CSV instead**; the declared output is
  named "Excel (or CSV) file" for that reason.
- **NoData overrides must match the order you selected the DEMs in.**
- A blank elevation cell means NoData at that point, not zero. The `Zone` column
  is there so you can filter to points where every DEM has data before comparing.
- This tool only reports. It never modifies a DEM.

# DEM Elevation Correction and Point Sample Report

|  |  |
|---|---|
| **Algorithm ID** | `kga:demelevationcorrectionandpointsamplereport` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/dem_elevation_correction_and_point_sample_report.py` |
| **Icon** | group icon (`icons/irrigation.png`) |
| **Type** | Batch algorithm |

## Overview

Corrects one or more DEMs against a **Reference DEM** you choose — your LiDAR, or
whatever survey you trust — optionally merges them all into one seamless raster,
and builds a Statistics tab and a Visualization tab summarising correction
quality and post-correction agreement.

This is the tool for a project whose elevation data arrived from several sources
at different dates and different accuracies: a LiDAR block, a drone survey, an
older contour-derived grid. Each has its own vertical bias, and stitching them
untreated gives steps at every seam.

**The Reference DEM is never modified** — it is the elevation truth every other
DEM is corrected toward. The other DEMs are auto-sorted from finest to coarsest
resolution and **chained**: the finest is corrected directly against the
Reference, the next-finest against that already-corrected result, and so on.

The run is **resumable** via a `checkpoint.json` in the output folder.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Reference DEM (the elevation truth -- never modified)** | Raster layer | — | |
| **Reference DEM NoData override (blank = use its own NoData)** | String, optional | empty | |
| **DEMs to correct against the reference (1 or more)** | Multiple raster layers | — | Auto-sorted finest to coarsest and corrected in a chain. |
| **NoData overrides (comma-separated, matching DEM order)** | String, optional | empty | One value per DEM, in the order you selected them. Blank uses each raster's own NoData metadata. |
| **Correction method (all stages)** | Enum | `Auto - Compare All (Best CV)` | `Auto - Compare All (Best CV)`, `Global Offset`, `Polynomial Trend (degree 1)`, `Polynomial Trend (degree 2)`, `IDW (distance-decay)`. Auto tries them all and keeps whichever cross-validates best. |
| **IDW power** | Number | `2.0` | IDW method only. |
| **IDW neighbors (k)** | Integer | `12` | IDW method only. |
| **IDW full-strength distance (m)** | Number | `3000.0` | Beyond this the correction fades out. |
| **Cross-validation folds** | Integer | `5` | How the method comparison is scored. |
| **Calibration subsample stride** | Integer | `4` | Every Nth pixel is used to fit the correction. Raise it for a faster, coarser fit. |
| **Merge finest DEM + all corrected DEMs** | Boolean | `True` | |
| **Merge extent** | Enum | `Reference DEM extent` | Or `Coarsest DEM extent (full)`. |
| **Merge resolution (m)** | Number | `2.5` | |
| **Report sample stride (for Statistics/Visualization tabs)** | Integer (min 1) | `20` | |
| **Report max sample points** | Integer (min 100) | `200000` | |
| **Force re-run everything (ignore checkpoint.json)** | Boolean | `False` | |
| **Output folder** | Folder destination | — | |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Merged DEM (if merge was enabled)** | File | |
| **Run summary** | String | |
| **Statistics** | HTML | Appears as a tab in the Processing Results panel. |
| **Visualization** | HTML | Likewise. |

## How to use

1. Load the reference DEM and every DEM to correct.
2. Open **KGA Irrigation Tools > DEM Elevation Correction and Point Sample
   Report**.
3. Set the **Reference DEM** — the one you trust. It is never written to.
4. Select the DEMs to correct. Their order does not matter; they are sorted by
   resolution.
5. Leave the method on **Auto** for the first run and let the cross-validation
   pick. Look at what it chose in the Statistics tab before pinning a method.
6. Press **Run**.
7. **Open the Statistics and Visualization tabs in the Processing Results
   panel** — that is where the answer is, not in the log.
8. If the run is interrupted, run it again with the same settings to resume.

## Outputs

In the output folder:

- `corrected_{name}.tif` — one corrected DEM per input.
- `MERGED_FINAL_DEM.tif` — the seamless merge, when merging is enabled.
- `statistics_report.html` and `visualization_report.html` — the two report tabs,
  also on disk.
- `correction_report.json` — the machine-readable run record.
- `checkpoint.json` — resume state. Safe to delete when finished.

## Notes and limits

- **The chain matters.** Each DEM is corrected against the already-corrected one
  above it, so an error early in the chain propagates down. Check the Statistics
  tab stage by stage, not just at the end.
- **Correction does not create accuracy.** It removes systematic bias relative to
  the reference; it cannot fix a DEM that is simply wrong in detail.
- **The reports are sampled, not exhaustive.** *Report sample stride* and *Report
  max sample points* bound the cost; the statistics describe that sample.
- **Resumable via `checkpoint.json`.** Tick *Force re-run* to ignore it.
- **NoData overrides must match the order you selected the DEMs in.** Getting the
  order wrong silently corrects against the wrong nodata mask.
- For a per-point table across several DEMs, use
  **[DEM Point Sample Export to Excel](dempointsampleexport.md)**, which is the
  same sampling in spreadsheet form.

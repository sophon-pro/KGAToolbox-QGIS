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
| **Input · Reference DEM** | Raster layer | — | The trusted DEM. Never modified; every other DEM is corrected toward it. |
| **Input · DEMs to correct (1 or more)** | Multiple raster layers | — | Auto-sorted finest to coarsest and corrected in a chain. |
| **Input · Reference NoData override** | String, advanced | empty | A number to treat as NoData in the Reference. Blank = use its own NoData value. |
| **Input · NoData overrides for the DEMs** | String, advanced | empty | Comma-separated, one value per DEM in the order you selected them, e.g. `-9999,-32768`. The count must match. Blank uses each raster's own NoData metadata. |
| **Correction · Method (all stages)** | Enum | `Auto - Compare All (Best CV)` | `Auto - Compare All (Best CV)`, `Global Offset`, `Polynomial Trend (degree 1)`, `Polynomial Trend (degree 2)`, `IDW (distance-decay)`. Auto tries them all and keeps whichever cross-validates best. |
| **Correction · Calibration stride (pixels)** | Integer | `4` | Every Nth pixel of the difference raster is used as a calibration point. Higher is faster, with fewer points. |
| **Correction · Cross-validation folds** | Integer, advanced | `5` | How the method comparison is scored. |
| **IDW · Power** | Number, advanced | `2.0` | IDW method only (including inside Auto). |
| **IDW · Neighbours (k)** | Integer, advanced | `12` | Nearest calibration points averaged for each pixel. |
| **IDW · Full-strength distance (m)** | Number, advanced | `3000.0` | Map units. Full strength within this distance of a calibration point, fading to zero at twice the distance. |
| **Merge · Merge Reference + corrected DEMs** | Boolean | `True` | Writes `MERGED_FINAL_DEM.tif`. The Reference wins, then finest to coarsest. |
| **Merge · Extent** | Enum | `Reference DEM extent` | Or `Coarsest DEM extent (full)`. |
| **Merge · Cell size (m)** | Number | `2.5` | |
| **Report · Sample stride (pixels)** | Integer (min 1), advanced | `20` | The report compares all DEMs at every Nth pixel of the Reference grid. |
| **Report · Max sample points** | Integer (min 100), advanced | `200000` | Upper limit on sample points; the stride is raised to stay below it. |
| **Output · Force re-run (ignore checkpoint)** | Boolean, advanced | `False` | Redo every stage even if `checkpoint.json` says it is complete. |
| **Output · Folder** | Folder destination | — | Corrected DEMs, merged DEM, reports and `checkpoint.json` are written here. Reuse the same folder to resume. |

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
- **The reports are sampled, not exhaustive.** *Report · Sample stride* and *Report
  · Max sample points* bound the cost; the statistics describe that sample.
- **Resumable via `checkpoint.json`.** Tick *Output · Force re-run* to ignore it.
- **NoData overrides must match the order you selected the DEMs in.** Getting the
  order wrong silently corrects against the wrong nodata mask.

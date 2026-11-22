# Sliver and Spike Cleaner

|  |  |
|---|---|
| **Algorithm ID** | `kga:sliver_spike_cleaner` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/sliver_spike_cleaner_alg.py` (window in `kga_tools/gui/sliver_spike_dialog.py`, engine in `kga_tools/core/sliver_spike_cleaner.py`) |
| **Icon** | `icons/quality_control/sliverSpikeCleaner.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window; no internet |

## Overview

Cleans the overhangs, wedges and thin strips left after polygons are split with
lines. The input layer is **never modified**: the result is a new layer, plus an
optional `_removed` layer showing what was cut.

Each feature goes through the same steps, in order:

1. **Validate** — invalid geometry is repaired; only polygons are kept.
2. **Spikes & wedges** — corners sharper than the angle are trimmed square along
   the corner edge (or just the tip vertex is removed).
3. **Slivers & narrow parts** — any part narrower than the width is removed,
   however long it is. Normal corners stay square.
4. **Small pieces & holes** — parts and holes smaller than the area are removed.
5. **Finish** — vertices are snapped back to the original coordinates (so shared
   edges between neighbours stay identical), duplicate and extra collinear
   vertices are dropped, validity is checked again.

The work runs on the GUI thread with a progress bar; QGIS is busy until it
finishes.

## Parameters

### Input

| Control | Meaning |
|---|---|
| **Input layer** | Any polygon layer. Starts on the active layer. |
| **Selected features only** | Clean only the selected features. |
| **keep the others unchanged in output** | With a selection: copy the unselected features to the output untouched. On by default. |

### Clean tab

| Control | Meaning |
|---|---|
| **Strength** | Presets: *Light* (5°, 1 m, 5 m²), *Standard* (10°, 2 m, 10 m²), *Strong* (15°, 3 m, 25 m²). Editing a value switches to *Custom*. |
| **Spikes & wedges — Sharper than** | Corners sharper than this angle are spikes. Normal corners are about 90°. |
| **Slivers & narrow parts — Narrower than** | Width in layer units; the 1 / 2 / 3 chips set it quickly. |
| **Small pieces & holes — Smaller than** | Parts and holes below this area are removed. |
| **If a whole feature is narrow** | *Keep it and flag for review* (`cleaned = 2`) or *Delete it*. |

### Output tab

| Control | Meaning |
|---|---|
| **Save as** | Temporary layer, or a GeoPackage / Shapefile. The file starts in the project's default folder. |
| **Layer name** | Defaults to `<layer>_clean`. |
| **Add check fields** | Adds `cleaned` (0 unchanged, 1 changed, 2 narrow and kept), `area_old`, `area_new`, `area_rem`. |
| **Show what was removed** | Adds a `<name>_removed` layer with `src_fid`, `reason`, `area`. |
| **Add results to the map** | Loads the new layers. |

### Advanced tab

| Control | Meaning |
|---|---|
| **Spike method** | *Square trim along corner edge* (recommended) or *Remove the tip vertex only*. |
| **Max area cut per spike** | A spike is only cut if the piece removed is smaller than this. |
| **Snap to original within** | Result vertices this close to an original vertex are snapped back to it. 0 turns it off. |

### Log tab

Opens by itself when a run starts and lists what was done: features written,
changed, flagged and deleted, spikes trimmed, pieces removed. Errors also land
here. **Help** opens this page.

## How to use

1. Run the tool; the window stays open beside the map.
2. Choose the polygon layer (use a projected CRS, metres).
3. Pick a strength, or set the angle, width and area yourself.
4. Leave *If a whole feature is narrow* on **Keep and flag** for the first run.
5. Press **Clean polygons**, then check the `_removed` layer and any features
   with `cleaned = 2`.
6. Switch to **Delete** only if the flagged features are really slivers.

## Outputs

* **Cleaned layer** — same geometry type and attributes as the input, plus the
  check fields. Features that become empty are dropped.
* **`_removed` layer** — the pieces that were cut, with the reason.

## Notes and limits

* Widths and areas are in the **layer's units**; the tool warns about geographic
  (degree) layers.
* Real narrow features (bunds, canals, roads) are narrow too — check before deleting.
* Z and M values are dropped; the output is 2D. Curves become straight segments.
* A `fid` column is not copied (it would clash with the GeoPackage key).
* A single-part layer keeps only the biggest piece of a feature that splits in two;
  the rest goes to the `_removed` layer.
* Neighbouring polygons are cleaned one by one, so run the Topology checks afterwards.
* The output cannot be the input layer's own file.

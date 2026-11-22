# Multipart Line Repair

|  |  |
|---|---|
| **Algorithm ID** | `kga:multipart_line_repair` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/multipart_line_repair_alg.py` (window in `kga_tools/gui/multipart_repair_dialog.py`, engine in `kga_tools/core/multipart_repair.py`) |
| **Icon** | `icons/quality_control/multipartLineRepair.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window; no internet |

## Overview

Finds, explains and repairs **multipart line features**. Canals and drains that
were edited many times with Split, Trim/Extend or Reshape often end up as one
feature made of several parts that do not touch *exactly*, so *Merge Lines*
fails. The tool lists every true multipart feature with the reason it is
multipart, then joins what can be joined and separates what cannot.

Repair of each multipart feature, in order:

1. remove duplicate vertices;
2. drop parts shorter than **Min part length** (slivers left by cutting);
3. snap part end-points closer than **Snap tolerance** to their common average;
4. dissolve overlaps (union) and merge the lines.

If one part is left the feature is **fixed** — same feature, same attributes. If
several remain they are really separate (branches, distant pieces) and become
single features with the attributes copied.

The work runs on the GUI thread with a progress bar; QGIS is busy until it
finishes.

## The window

An **Input line layers** box sits on top and three tabs below it. The window
resizes to the tab you are on: Repair settings and Output are compact, Check is
tall for its two tables. The log and the progress bar are hidden until you run
Check or Repair; the log then stays until you close the window.

## Parameters

### Input line layers

| Control | Meaning |
|---|---|
| **Layer list** | Every line layer in the project, shown as `group / layer`. Tick the ones to check or repair. Visible layers start ticked. |
| **All / None / Visible / Refresh** | Tick all, untick all, tick only the visible layers, or re-read the project's layers. The list also refreshes each time the window opens. |

### Repair settings tab (used by both Check and Repair)

| Control | Meaning |
|---|---|
| **Snap tolerance** | Part end-points closer than this are snapped together. Layer CRS units. Default 0.5. |
| **Min part length** | Parts shorter than this are deleted; 0 keeps all. Layer CRS units. Default 0.1. |
| **Remove duplicate vertices** | Step 1 of the repair. On by default. |
| **Dissolve overlapping parts (union)** | Step 4 of the repair; without it the parts are only collected and merged where they already touch. On by default. |
| **Units** | Shown after a Check: the CRS units of the ticked layers, or a note that they differ. |

### Check tab

| Control | Meaning |
|---|---|
| **Check multipart** | Scans the ticked layers and fills the two tables. |
| **Select multipart features after check** | Selects the multipart features on the map. On by default. |
| **Layer table** | Per layer: features, multi-type, true multipart, *Fixable (→1)*, *Separate*, null/empty, maximum parts. Double-click a row to list its features, select them and zoom to them. |
| **Feature table** | Per multipart feature: ID, parts, shortest part, largest gap, **Cause**, parts after repair, result. Click a column to sort; double-click a row to zoom to and flash that feature. |

Causes: *overlap*, *tiny part*, *touching*, *small gap* (within the tolerance),
*gap > tolerance*. **Fixable** means repair gives one part; **Separate** means
the parts are really apart or branched and will be split into several features.

### Output tab

| Control | Meaning |
|---|---|
| **Format** | *New GeoPackage – one file per layer* (default); *New GeoPackage – all layers in one file*; *New Shapefile – one file per layer*; *Temporary layers (memory, not saved)*; *Fix in place*. |
| **Output folder** | Where new files go. Starts in the project's default folder. Not used for temporary layers or in place. |
| **GeoPackage name** | File name for the one-file GeoPackage option (`singlepart_lines.gpkg`). |
| **Layer suffix** | Added to each new layer's name (`_fixed`). |
| **Only layers that have multipart features** | Skip clean layers. Always on for in place. |
| **Copy symbology and labels** | The new layer takes the original's renderer and labelling. |
| **Add output to map (below original)** | Adds the new layer directly under the original in the layer tree. |

The **Repair / Fix multipart** button runs the repair; **Help** opens this page.

## How to use

1. Run the tool; the window stays open beside the map.
2. Tick the line layers.
3. On **Repair settings**, set the tolerance and minimum length. Use a projected CRS (metres).
4. On **Check**, press **Check multipart**. Read the *Cause* and *Result* columns; if a gap that should close says *gap > tolerance*, raise the tolerance and Check again.
5. On **Output**, choose where the result goes.
6. Press **Repair / Fix multipart**. The log lists what was done per layer.

## Outputs

* **New file or temporary layer:** one `<name><suffix>` layer per input, every feature
  single-part, with the original attributes. Joined features keep their attributes;
  parts that stay apart become extra features with the attributes copied. The log gives
  the feature count before and after and how many multipart features are left (0).
* **Fix in place:** the original layers are put in edit mode as one undoable edit. Check
  the result, then Save Layer Edits or Roll Back (Ctrl+Z undoes it). Split-off parts are
  added as new features with a fresh primary key.

## Notes and limits

* Tolerance and minimum length are in the **layer's units**. The tool warns about
  geographic (degree) layers.
* A tolerance larger than the real gaps can snap parts that should stay apart; Check
  first and read the *Max gap* column.
* Only line layers are listed. Curved geometries are converted to straight segments.
* Snapping averages the end-points that fall within the tolerance of each other, so a
  chain of close end-points moves together.
* A GeoPackage output drops the old `fid` field so split parts get new ids; an existing
  file asks before it is overwritten. A file loaded in QGIS cannot be overwritten.
* A layer filter (right-click › Filter) hides features from the copy: an empty result
  usually means a filter or an unreadable source.
* The result is not topology-checked: overlaps with *other* layers are untouched.

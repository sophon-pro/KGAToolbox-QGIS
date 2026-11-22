# Copy Parallel

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/parallel.py` (ParallelTool) |
| **Icon** | `kga_tools/icons/editing/copyParallel.png` |
| **Also as a window** | [copy_parallel](../copy_parallel.md) (`kga:copy_parallel`) |

## Overview

Copies the selected lines at a distance, on one or both sides, laid out like the Copy Parallel pane of ArcGIS Pro.

## How to use

1. Select the lines. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the **Template** (the line layer that receives the copies, and whether they keep the source attribute values).
3. Choose **Copy to separate features** or **Copy to a single feature**, set the **Distance** and **Repetitions**, then the **Side** and **Corners**.
4. Tick **Show Preview**, then press **Copy**.

## Options

| Option | Meaning |
|---|---|
| **Distance / Repetitions** | Repetition k is copied at k x Distance. |
| **Side** | Both, Left or Right of the line's direction. Arrows on the selected lines in the preview show which way Left and Right refer to. |
| **Corners** | Mitered, Rounded or Beveled. |
| **Use Template / Keep source attribute values** | Make the copies in the template layer, and carry the source attribute values across. |
| **Remove self-intersecting loops** | Drops the loops an offset makes on the inside of a tight bend. |
| **Align the direction of connected lines** | Ticked by default. |

## Notes and limits

* The preview draws the copies in olive. The new lines are selected afterwards.
* One undoable edit.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

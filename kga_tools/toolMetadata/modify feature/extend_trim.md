# Extend or Trim

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/reshape.py` (ExtendTrimTool) |
| **Icon** | `kga_tools/editor/modify/icons/extend_trim.svg` |

## Overview

Extends a line to the nearest feature or trims it back, laid out like the Extend or Trim pane of ArcGIS Pro.

## How to use

1. Select the lines. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose a tile: **Extend or Trim**, **Extend** or **Trim**.
3. Hover a line. A grey preview shows the part that would be removed (thick) or the extension (dashed).
4. Click to do it. Lines change to the nearest other feature shown on the map.

## Options

| Option | Meaning |
|---|---|
| **Recalculate COGO attributes** | After a change, rewrites the line's COGO fields (direction, distance...) from its geometry. |
| **Allow Extend or Trim without a selection** | With nothing selected, any editable line you click is changed. |

## Notes and limits

* Only selected lines change unless no selection is allowed.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

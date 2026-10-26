# Reshape

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/reshape.py` (ReshapeTool) |
| **Icon** | `kga_tools/editor/modify/icons/reshape.svg` |

## Overview

Draws a line over features to reshape them, laid out like the Reshape pane of ArcGIS Pro.

## How to use

1. Select the features. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Click points on the map to draw the new edge over the features.
3. Finish with the **Finish** button, right-click or Enter. Backspace removes the last point and Esc cancels.

## Options

| Option | Meaning |
|---|---|
| **Show Preview** | Draws the reshaped features while you move the mouse. |
| **Allow reshaping without a selection** | With nothing selected, reshapes every editable feature the line crosses. |
| **Reshape with single intersection** | Lets one crossing reshape a line: the shorter side is trimmed and the line continues along the sketch. Needs a selected line. |
| **Choose result on finish** | When the sketch cuts a polygon in pieces, click the piece to keep. Unticked, the piece with the longest perimeter is kept. |

## Notes and limits

* While sketching, right-click for **Parallel** and **Perpendicular** (keys **P** and **R**): the next segment is locked to an edge. See the Notes in [Replace Geometry](replace_geometry.md).
* Only editable layers shown on the map are changed. One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

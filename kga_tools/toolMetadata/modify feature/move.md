# Move

|  |  |
|---|---|
| **Category** | Modify Features > Alignment |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/transform.py` (MoveTool) |
| **Icon** | `kga_tools/editor/modify/icons/move.svg` |

## Overview

Moves the selected features by dragging them on the map, or by a typed offset.

## How to use

1. Turn **Edit Session** On and open **Modify Features > Alignment > Move**.
2. Press on a feature and drag. It is selected for you and follows the mouse; release to apply. Click empty map to clear the selection, or drag there to select several (Shift adds, Ctrl removes).
3. Or type **dX** and **dY** on the page and press **Move by this offset**.
4. To move from a set point, press **Set on the map** in the anchor group and click the map; **Reset** goes back to the centre of the selection.

## Options

| Option | Meaning |
|---|---|
| **dX (map units)** | Offset along X, in the map CRS. |
| **dY (map units)** | Offset along Y, in the map CRS. |
| **Anchor** | The centre of the selection unless set with **Set on the map**. Snaps to vertices, edges and midpoints. |

## Notes and limits

* A live preview is drawn on rubber bands before anything is written.
* Works on the selected features of every editable layer shown on the map; layers in other CRSs follow along.
* One undoable edit per layer. Ctrl+Z or **Discard Edits** undoes it.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

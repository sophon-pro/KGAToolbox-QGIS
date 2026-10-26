# Rotate

|  |  |
|---|---|
| **Category** | Modify Features > Alignment |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/transform.py` (RotateTool) |
| **Icon** | `kga_tools/editor/modify/icons/rotate.svg` |

## Overview

Rotates the selected features about an anchor, by dragging or by a typed angle.

## How to use

1. Select the features (or click one while the tool is active and nothing is selected).
2. A red anchor marks the centre of the selection. Drag around it on the map, or type an angle on the page.
3. Release to write the rotation, or press **Rotate by this angle** for a typed one.

## Options

| Option | Meaning |
|---|---|
| **Counter-clockwise** | The angle to rotate by, in degrees (-360 to 360). |
| **Anchor** | Centre of the selection unless **Set on the map** places it elsewhere; **Reset** goes back to the centre. |

## Notes and limits

* Works in the map CRS and is converted back to each layer's CRS.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

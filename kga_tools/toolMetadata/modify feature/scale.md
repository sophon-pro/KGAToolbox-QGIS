# Scale

|  |  |
|---|---|
| **Category** | Modify Features > Alignment |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/transform.py` (ScaleTool) |
| **Icon** | `kga_tools/editor/modify/icons/scale.svg` |

## Overview

Scales the selected features about an anchor, by dragging away from or towards it, or by a typed factor.

## How to use

1. Select the features.
2. Drag away from the red anchor to enlarge or towards it to shrink, or type a factor on the page.
3. Release to write the result, or press **Scale by this factor** for a typed one.

## Options

| Option | Meaning |
|---|---|
| **Factor (2 = twice as big)** | Scale factor; 0.5 halves the size. |
| **Anchor** | Centre of the selection unless **Set on the map** places it elsewhere; **Reset** goes back to the centre. |

## Notes and limits

* Works in the map CRS and is converted back to each layer's CRS.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

# Change Line Length

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Command in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/line_length.py` (ChangeLineLengthPage) |
| **Icon** | `kga_tools/editor/modify/icons/change_line_length.svg` |

## Overview

Sets the length of the selected lines, keeping one end where it is, laid out like the Change Line Length pane of ArcGIS Pro.

## How to use

1. Select the lines.
2. Choose **Change Length to:** (a new total) or **Change Length by:** (an amount), and give a value and unit.
3. Choose which end to preserve: **First Vertex** or **Last Vertex**.
4. Tick **Show Preview** to see the new lines dashed over the old, then press **Change Length**.

## Options

| Option | Meaning |
|---|---|
| **Preserve location of** | **First Vertex** or **Last Vertex** stays where it is; the other end is cut back along the line (shorter) or continued straight on in the direction of its last segment (longer). |
| **Proportion geometry using existing COGO** | Scales the whole line about the preserved vertex so every segment changes by the same factor. Needs the layer's COGO Distance field. |

## Notes and limits

* Only single-part lines change.
* Lengths are measured on the ground when the project has an ellipsoid, else flat in map units.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

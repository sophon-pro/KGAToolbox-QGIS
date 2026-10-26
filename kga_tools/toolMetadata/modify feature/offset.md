# Offset

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/offset.py` (OffsetTool) |
| **Icon** | `kga_tools/editor/modify/icons/offset.svg` |

## Overview

Creates points along a reference line, each at a distance along it and a distance to its side, laid out like the Offset pane of ArcGIS Pro.

## How to use

1. Choose the reference line: **Interactive** (draw it with the Two-Point Line tool, or pick a line with the Trace Line tool) or **By Feature** (the first selected line feature).
2. Optionally set the **Range**: *Start from* and *End at*, distances along the line. The bar shows the line's length.
3. Fill the **Values** table: one point per row.
4. Choose the **Template** (the editable point layer), tick **Show Preview**, then press **Create**.

## Options

| Option | Meaning |
|---|---|
| **Distance** | Distance along the line (counted from Start from). A row beyond End at is left out. |
| **Offset** | Distance to the side of the line. A negative offset goes to the other side. |
| **Side** | None puts the point on the line; Left or Right of the line's direction. |

## Notes and limits

* The reference line is drawn in blue with an arrow showing its direction.
* The new points take the template's default attribute values and are selected afterwards.
* One undoable edit.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

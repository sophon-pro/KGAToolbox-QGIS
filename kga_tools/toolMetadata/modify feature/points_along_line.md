# Points On Line

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/along_line.py` (PointsAlongLineTool) |
| **Icon** | `kga_tools/editor/modify/icons/points_along_line.svg` |

## Overview

Constructs points along a line - a number of points, a point every distance, or a table of distances - and puts them in a point layer you choose, laid out like the Create Points Along Line pane of ArcGIS Pro.

## How to use

1. Choose the line: **Select Line** (the first selected line feature; click or drag on the map, or use **Change the selection.**) or **Draw Line** (click the points of a path on the map; right-click, double-click or Enter finishes it).
2. Choose the **Target point layer** (an editable point layer).
3. Choose the unit, then one of the three tabs and fill it in. **Line Length** shows the length of the line.
4. Tick **At start of line** and / or **At end of line** for extra points at the ends.
5. Tick **Show Preview** to see the points on the map, then press **Create**.

## Options

| Option | Meaning |
|---|---|
| **Number of Points** | That many points, spaced equally between the two ends of the line (the ends are not counted; use the Additional Points for them). |
| **Equal Distance** | A point every *Distance* from the **Orientation** end, as many as fit on the line. A point exactly at the far end counts. |
| **Distances** | The table: each row is a *Distance* and a *Number of Points* - that many points, that far apart, one row after the other from the **Orientation** end. **Remaining Length** is what is left of the line (red when the rows run past its end). Click **Click to add new row.** to add a row. |
| **Orientation** | **From Start** or **From End**: which end the distances are counted from (Equal Distance and Distances). The arrows on the line show its direction. |
| **Additional Points** | Also a point at the start and / or at the end of the line. |
| **Unit** | The unit of Line Length and of the distances you type (m, km, ft, yd, mi). |

## Notes and limits

* **Draw Line:** Backspace removes the last point, Esc starts again, and a click after the path is finished starts a new one.
* Lengths and distances are measured on the ground when the project has an ellipsoid (what `$length` reports), else flat in the map's units. A line with true curves is measured flat.
* A multi-part line uses its first part.
* The new points take the target layer's default attribute values and are selected afterwards.
* One undoable edit. Needs **Edit Session** On; only editable layers shown on the map are changed.

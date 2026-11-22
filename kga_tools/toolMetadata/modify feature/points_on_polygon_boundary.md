# Points On Polygon Boundary

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/polygon_points.py` (PointsOnPolygonBoundaryTool) |
| **Icon** | `kga_tools/editor/modify/icons/points_on_polygon_boundary.svg` |

## Overview

Constructs points ON the boundary of a polygon - not at its vertices - a number of points all the way round, a point every distance, or a table of distances from a starting point, and puts them in a point layer you choose. The sister of [Points On Line](points_along_line.md).

## How to use

1. Select a polygon (**Change the selection.**: click or drag on the map).
2. Choose the **Target point layer** (an editable point layer) and the unit.
3. Choose one of the three tabs and fill it in. **Boundary Length** shows the length of the polygon's outer boundary.
4. For **Equal Distance** and **Distances**, set the starting point: click one of the polygon's vertices, or any point of its edge. The green square marks it. (For **Number of Points** it is optional.)
5. Choose the **Direction** - **Clockwise** or **Counter-clockwise** - which way round the boundary the distances are counted. Blue arrows on the boundary show it.
6. Tick **Show Preview** to see the points on the map, then press **Create**.

## Options

| Option | Meaning |
|---|---|
| **Number of Points** | That many points spaced equally all the way round the boundary. No starting point is needed: the first point is at the starting point if you picked one, else at the polygon's first vertex. |
| **Equal Distance** | A point every *Distance* from the starting point, going round the chosen way. |
| **Distances** | The table: each row is a *Distance* and a *Number of Points* - that many points, that far apart, one row after the other from the starting point. **Remaining Length** is what is left of the boundary (red when the rows run past it). Click **Click to add new row.** to add a row. |
| **Add a point at the starting point** | Also a point on the starting point itself (Equal Distance and Distances; with Number of Points the first point is always there). |
| **Clear Start** | Forgets the starting point. |
| **Unit** | The unit of Boundary Length and of the distances you type (m, km, ft, yd, mi). |

## Notes and limits

* **Snapping the starting point:** while you hover the selected polygon a magenta mark shows what a click would pick - a square on a vertex, a double triangle on the middle of an edge, an X on the edge itself. This works whether or not the project's snapping is on, and only the selected polygon is snapped to.
* Only the **outer boundary** is used. Holes in the polygon are ignored (the first part of a multi-part polygon).
* A point that would land back on the starting point after the full circle is not made twice.
* Lengths and distances are measured on the ground when the project has an ellipsoid (what `$length` reports), else flat in the map's units.
* The new points take the target layer's default attribute values and are selected afterwards.
* One undoable edit. Needs **Edit Session** On; only editable layers shown on the map are changed.

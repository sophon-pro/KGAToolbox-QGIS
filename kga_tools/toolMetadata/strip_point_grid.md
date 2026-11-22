# Strip Lines and Point Grid

|  |  |
|---|---|
| **Algorithm ID** | `kga:strip_point_grid` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/strip_point_grid_alg.py` (window in `kga_tools/gui/strip_grid_dialog.py`, engine in `kga_tools/core/strip_grid.py`) |
| **Icon** | `icons/conversion/stripPointGrid.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window; no internet |

## Overview

Lays **parallel strip lines**, a **regular point grid** or a **grid of regular shapes** over an area, from one
small window. Typical uses are survey transects, sampling plots, planting or
soil-sampling grids, and field strips.

* **Strip lines** are equally spaced, or follow a **pattern of gaps** that
  repeats (`10, 20, 30` gives gaps of 10, 20, 30, 10, 20, 30 …).
* **Point grid** places points a fixed distance apart, with an optional
  different distance between rows.
* **Shape grid** fills the area with squares, rectangles, hexagons or triangles that fit edge to edge, or with regular polygons of any number of sides placed at a set spacing, and can add a **centre point** for every cell.
* All of them are **cut to the area**: a polygon gives lines and points that stop at
  its edge, and a strip crossing a concave polygon becomes several pieces that
  share one strip number.

## Parameters

### Area

The same picker as the other area tools.

| Choice | What it uses |
|---|---|
| **Current map extent** | The canvas as it is now. |
| **Draw a rectangle or polygon on the map** | A shape you draw; press **Draw rectangle** or **Draw polygon**. |
| **Boundary of a polygon layer** | The union of the layer's polygons (or only the selected ones). |
| **Extent of a layer** | The bounding box of any layer. |

### Settings

| Control | Default | Description |
|---|---|---|
| **Make** | Strip lines | Strip lines, Point grid or Shape grid. |
| **Shape** | Square | Square, Rectangle, Hexagon, Triangle (these tile the area with no gaps) or Regular polygon. |
| **Side / Width / Diameter** | 50 m | The side of a square, hexagon or triangle; the width of a rectangle; the width of a regular polygon across its corners. |
| **Height** | 50 m | Rectangles only. |
| **Sides** | 8 | Regular polygon only, 3 to 64. Spacing between the centres is the **Spacing** / **Row spacing** boxes, so shapes can touch, overlap or leave gaps. |
| **Keep** | Cells that touch the area | *Cells that touch the area*, *Only cells wholly inside the area*, or *Cells cut at the edge of the area* (trimmed to the shape of the area). |
| **Also make centre points** | Off | Adds a second layer, `<name> centres`, with a point at the centre of every cell (for *Cells cut at the edge* the point is moved inside the trimmed cell, so it never falls outside the area) and the same `id`, `row_no` and `col_no`. |
| **Strips** | Equal spacing | *Equal spacing*, or *Varying spacing* for a repeating pattern. |
| **Spacing** | 50 m | Distance between lines (equal), or between points along a row (point grid). |
| **Gaps (m)** | — | Varying spacing: the gaps in order, separated by commas, repeated until the area is covered. |
| **Same distance between rows** | On | Point grid only. Untick to give rows their own **Row spacing**. |
| **Direction** | Vertical | Vertical (north–south), Horizontal (east–west) or Custom angle. A point grid is turned by the same angle. |
| **Angle** | 0° | Bearing of the lines: 0 is north–south, 90 east–west, 45 north-east. Type it, or fill it from the map with **Edge** or **Draw** (below). |
| **Edge** | — | Click an edge on the map (the area's own boundary, a road, a canal, any line or polygon layer) and the strips run **parallel to it**. Hover to preview the angle; Esc or right-click cancels. |
| **Draw** | — | Click the two ends of a line on the map and the strips run along it. The project's snapping applies. |
| **First one** | One spacing in | Where the first line or point goes: one spacing in, half a spacing in, or on the edge. Counted from the left edge (vertical lines) or the top edge (horizontal lines), measured on the area after the offset. |
| **Offset from edge** | 0 m | Keeps lines and points this far **inside** the edge of the area, so nothing is made in the outer band. The area is shrunk by this distance first; an offset larger than the area allows says so and makes nothing. |
| **Output** | Temporary layer | A temporary layer, or **Save to file** (a GeoPackage, starting in the project's default folder). |

A line below the form says how many strips or points to expect before you press
**Create**.

## Output

* **Strip lines** layer fields: `id`, `strip_no`, `gap_m` (distance from the
  previous line; for the first line, from the edge), `length_m`, `bearing`.
* **Point grid** layer fields: `id`, `row_no`, `col_no` (numbered from the top
  left), `x`, `y` (in the layer CRS) and `lon`, `lat`.

* **Shape grid** layer fields: `id`, `row_no`, `col_no`, `x`, `y` and `lon`, `lat` (the cell's centre), and `area_m2` (the cell's area, after cutting when *Cut at the edge* is chosen). Hexagon rows are counted down each column, since neighbouring columns are half a cell apart.
* The **centre points** layer has the point grid's fields.

Strips are orange, points red, cells blue outlines and centre points dark blue. **Direction** and **Offset from edge** turn and inset shapes the same way. Cells start at the top-left corner of the area, so **First one** is not used.

## Coordinate system

Distances are **metres**. When the map is in degrees or Web Mercator
(EPSG:3857) the tool measures in the **UTM zone under the middle of the area**
instead, because "50 m" in Web Mercator is only 50 m on the equator. The new
layer is in that UTM CRS and the window says which; QGIS reprojects it on the
fly. Any other projected CRS is used as it is, with its units converted.

## Notes and limits

* The tool stops at **one million** lines or points.
* **Hairline holes** in the area (slivers left where parcels were merged) are filled in before anything is made: holes smaller than 5 m² are ignored, so they do not cut strips in two or leave stray lines in cells. Real holes such as ponds are kept.
* A strip lying exactly on the far edge of the area is kept.
* Drawn areas, layer boundaries and extents work the same way; for a
  rectangle the strips simply run edge to edge.
* The window stays open so you can change a value and press **Create** again;
  each press makes a new layer.

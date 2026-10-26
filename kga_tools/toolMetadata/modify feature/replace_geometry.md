# Replace Geometry

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/reshape.py` (ReplaceGeometryTool) |
| **Icon** | `kga_tools/editor/modify/icons/replace_geometry.svg` |

## Overview

Replaces the shape of the selected features with a new one you draw, laid out like the Replace Geometry pane of ArcGIS Pro.

## How to use

1. Select the features. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Click the **Replace Polygon / Line / Point** tile and draw the new shape: click its points (a dashed outline and green vertices show it, with snapping).
3. Finish with right-click or Enter. The selected features take that shape.

## Options

| Option | Meaning |
|---|---|
| **Sketch menu** | Right-click while sketching: Finish Sketch, Parallel (P), Perpendicular (R), Clear Constraint, Delete Sketch. |

## Notes and limits

* **Parallel / Perpendicular** lock the next segment to an edge of any feature or of the sketch. Right-click with the mouse over an edge to use it at once, or hover and click the edge. A thin cyan guide shows the locked direction; the lock is used up by the next click.
* Needs at least 3 points for a polygon and 2 for a line.
* Attributes are kept; only the geometry changes.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

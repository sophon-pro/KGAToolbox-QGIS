# Generalize

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Command in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/generalize.py` (GeneralizePage) |
| **Icon** | `kga_tools/editor/modify/icons/generalize.svg` |

## Overview

Removes, smooths or adds vertices in the selected lines and polygons, laid out like the Generalize pane of ArcGIS Pro.

## How to use

1. Select lines or polygons (the Select tool is in hand from the start).
2. Choose a **Method** and set its value in a unit of your choice.
3. Tick **Show Preview** to see the result (blue line, vertices as dots), then press **Generalize**.

## Options

| Option | Meaning |
|---|---|
| **Simplify** | Removes vertices that move the shape by less than the **Maximum Allowable Offset**. |
| **Smooth** | A smooth curve through the vertices; the offset is how far a chord of it may lie from the curve. |
| **Densify** | Adds vertices: one at most a **Distance** apart, or, for curved segments, within an Offset / Angle. |

## Notes and limits

* Every run is one undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

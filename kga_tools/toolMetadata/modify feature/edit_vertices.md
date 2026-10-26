# Edit Vertices

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/vertices.py` (EditVerticesTool, VertexTable) |
| **Icon** | `kga_tools/editor/modify/icons/edit_vertices.svg` |

## Overview

Edits the vertices of a feature on the map or in a table, laid out like the Edit Vertices pane of ArcGIS Pro. It is KGA's own tool, so selecting and editing are one flow.

## How to use

1. Click a feature: it is selected and its vertices show as handles.
2. Drag a handle to move that vertex (it snaps to the features around). Double-click a segment to add a vertex.
3. Drag a box on empty map to choose the vertices inside it (hold Alt for a freehand lasso; Shift adds, Ctrl removes). Chosen vertices are orange.
4. Press **Delete** to remove the chosen vertices, else the vertex under the mouse, else the rows selected in the table.
5. Or edit the **table** at the bottom: type a new X or Y in a cell to move that vertex; select rows and press the red cross to delete them.

## Options

| Option | Meaning |
|---|---|
| **# / X / Y table** | The vertices of the feature picked in the list, in the map CRS. Edit X or Y in a cell. |
| **Copy** | Copies the table to the clipboard. |
| **Change the selection.** | Use this button to pick features by dragging; a plain drag on empty map picks vertices. |

## Notes and limits

* A line needs at least 2 vertices and a polygon ring 3; deleting more is refused.
* Z and M values are kept when a vertex moves; a polygon's closing vertex moves with the first.
* The table follows the map: a drag, undo or redo updates it, and the vertex of the selected row is circled on the map.
* Every change is one undoable edit written in the layer's own CRS. Esc first drops the chosen vertices.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

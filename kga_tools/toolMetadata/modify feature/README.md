# Modify Features — Tool Reference

One page for each of the **32 tools** in the Modify Features pane (KGA Editor toolbar, QGIS 3.44 or newer). The pane itself is described in [Modify Features](../modify_features.md); the tools are modelled on the ArcGIS Pro Modify Features pane.

## Common behaviour

* **Edit Session must be On.** Only editable layers shown on the map are changed.
* **Selecting.** Tools that act on a selection show a **Change the selection.** banner and the list of selected features. Click or drag on the map to select; Shift adds, Ctrl removes, empty map clears.
* **Map tool or command.** A *map tool* is armed on the canvas while its page is open. A *command* (Generalize, Change Line Length) acts on the selection when you press its button.
* **Undo.** Every change is one undoable edit per layer: Ctrl+Z or **Discard Edits**. Nothing is saved until you press Save on the toolbar.
* **Preview.** Tools with **Show Preview** draw the result before anything is written.
* **CRS.** Work is done in the map CRS and converted back to each layer's CRS. Lengths and areas are measured on the ground when the project has an ellipsoid, else flat in map units.
* **COGO.** Coordinate geometry stores a line as Direction, Distance, Radius and ArcLength fields. Traverse, Update COGO and Split Into COGO Lines share the same direction and distance settings.
* **Favorites.** Right-click a tile > Add to favorites to put a tool on the KGA Editor toolbar.

## Alignment — 4

| Tool | What it does |
|---|---|
| [Move](move.md) | Moves the selected features by dragging them on the map, or by a typed offset. |
| [Rotate](rotate.md) | Rotates the selected features about an anchor, by dragging or by a typed angle. |
| [Scale](scale.md) | Scales the selected features about an anchor, by dragging away from or towards it, or by a typed factor. |
| [Move To](move_to.md) | Moves the selection to a location, laid out like the Move To pane of ArcGIS Pro. |

## Reshape — 9

| Tool | What it does |
|---|---|
| [Edit Vertices](edit_vertices.md) | Edits the vertices of a feature on the map or in a table, laid out like the Edit Vertices pane of ArcGIS Pro. |
| [Reshape](reshape.md) | Draws a line over features to reshape them, laid out like the Reshape pane of ArcGIS Pro. |
| [Replace Geometry](replace_geometry.md) | Replaces the shape of the selected features with a new one you draw, laid out like the Replace Geometry pane of ArcGIS Pro. |
| [Extend or Trim](extend_trim.md) | Extends a line to the nearest feature or trims it back, laid out like the Extend or Trim pane of ArcGIS Pro. |
| [Fillet](fillet.md) | Joins two lines with an arc, laid out like the Fillet pane of ArcGIS Pro. |
| [Generalize](generalize.md) | Removes, smooths or adds vertices in the selected lines and polygons, laid out like the Generalize pane of ArcGIS Pro. |
| [Line Intersection](line_intersection.md) | Trims or extends two lines to meet at their corner, laid out like the Line Intersection pane of ArcGIS Pro. |
| [Reverse Direction](reverse_direction.md) | Turns a line around, laid out like the Reverse Direction pane of ArcGIS Pro. |
| [Change Line Length](change_line_length.md) | Sets the length of the selected lines, keeping one end where it is, laid out like the Change Line Length pane of ArcGIS Pro. |

## Divide — 5

| Tool | What it does |
|---|---|
| [Split](split.md) | Splits features with a drawn line or with other features, laid out like the Split pane of ArcGIS Pro. |
| [Divide](divide.md) | Divides polygons into parts by area, proportion or width, or lines by distance, equal parts or percentage. |
| [Clip](clip.md) | Clips lines and polygons with other features, laid out like the Clip pane of ArcGIS Pro. |
| [Explode](explode.md) | Breaks multi-part features into single parts, or pulls single parts out of them. |
| [Planarize](planarize.md) | Cuts the selected lines where they cross each other, laid out like the Planarize pane of ArcGIS Pro. |

## Construct — 8

| Tool | What it does |
|---|---|
| [Merge](merge.md) | Merges features into one, laid out like the Merge pane of ArcGIS Pro. |
| [Buffer](buffer.md) | Buffers the selected features into polygons or outlines, laid out like the Buffer pane of ArcGIS Pro. |
| [Copy Parallel](copy_parallel.md) | Copies the selected lines at a distance, on one or both sides, laid out like the Copy Parallel pane of ArcGIS Pro. |
| [Construct Polygons](construct_polygons.md) | Builds polygons from the selected lines that enclose an area, laid out like the Construct Polygons pane of ArcGIS Pro. |
| [Offset](offset.md) | Creates points along a reference line, each at a distance along it and a distance to its side, laid out like the Offset pane of ArcGIS Pro. |
| [Points On Line](points_along_line.md) | Constructs points along a selected or drawn line: a number of points, an equal distance, or a table of distances, laid out like the Create Points Along Line pane of ArcGIS Pro. |
| [Points On Polygon Boundary](points_on_polygon_boundary.md) | Constructs points on the boundary of a selected polygon: a number all the way round, an equal distance or a table of distances from a starting point, clockwise or counter-clockwise. |
| [Copy Paste](copy_paste.md) | The Copy-Paste Feature tool inside the pane. |

## Attributes — 2

| Tool | What it does |
|---|---|
| [Sequential Numbering](sequential_numbering.md) | Numbers features in the order a drawn line crosses them, laid out like the Sequential Numbering pane of ArcGIS Pro. |
| [Transfer Attributes](transfer_attributes.md) | Copies attribute values from one feature to another, laid out like the Transfer Attributes pane of ArcGIS Pro. |

## COGO — 4

| Tool | What it does |
|---|---|
| [Traverse](traverse.md) | Draws COGO lines from typed directions and distances, laid out like the Traverse pane of ArcGIS Pro. |
| [Update COGO](update_cogo.md) | Rewrites the COGO fields of the selected lines from their geometry, laid out like the Update COGO pane of ArcGIS Pro. |
| [Cul-de-sac](cul_de_sac.md) | Adds a turnaround at the end of a street, laid out like the Cul-de-sac pane of ArcGIS Pro. |
| [Split Into COGO Lines](split_cogo_lines.md) | Splits lines, or polygon boundaries, at their vertices into COGO lines, laid out like the Split Into COGO Lines pane of ArcGIS Pro. |

Nine of these tools also exist as stand-alone windows with their own Processing page: [Divide](../divide_features.md), [Clip](../clip_features.md), [Planarize](../planarizelines.md), [Merge](../merge_features.md), [Buffer](../buffer_features.md), [Copy Parallel](../copy_parallel.md), [Construct Polygons](../construct_polygon.md), [Sequential Numbering](../sequential_numbering.md) and [Split Into COGO Lines](../split_into_cogo_lines.md). Their pane versions are described here.

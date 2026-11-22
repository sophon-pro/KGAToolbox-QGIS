# Modify Features

|  |  |
|---|---|
| **Algorithm ID** | none: a docked pane on the KGA Editor toolbar (not a Processing algorithm) |
| **Group** | KGA Editor toolbar |
| **Source** | `kga_tools/editor/modify/` (`controller.py` button and docking, `panel.py` the pane, `tools.py` the tool list, `favorites.py`, one module per tool family) |
| **Icon** | `icons/editing/modifyFeature.png` |
| **Type** | Docked pane with map tools and commands. Needs QGIS 3.44 or newer; follows the Edit Session switch like Create Features |

## Overview

A replica of ArcGIS Pro's Modify Features pane. Tools are grouped in categories of icon
tiles, with a search box and **All Tools** / **Favorites** tabs. Clicking a tile opens the
tool inside the pane (options, buttons, a back arrow), and arms its map tool on the canvas. The buttons at the
right of the tabs show the tools as a grid of tiles (default) or as a list; the choice is remembered.

## Tools (32)

| Category | Tools |
|---|---|
| **Alignment** | Move, Rotate, Scale, Move To |
| **Reshape** | Edit Vertices, Reshape, Replace Geometry, Extend or Trim, Fillet, Generalize, Line Intersection, Reverse Direction, Change Line Length |
| **Divide** | Split, Divide, Clip, Explode, Planarize |
| **Construct** | Merge, Buffer, Copy Parallel, Construct Polygons, Offset, Points On Line, Points On Polygon Boundary, Copy Paste |
| **Attributes** | Sequential Numbering, Transfer Attributes |
| **COGO** | Traverse, Update COGO, Cul-de-sac, Split Into COGO Lines |

Divide, Clip, Planarize, Merge, Buffer, Copy Parallel, Construct Polygons, Sequential Numbering
and Split Into COGO Lines are the KGA tools of the same names, opened inside the pane.

**Tool reference:** one page for each tool is in [modify feature](modify%20feature/README.md).

## How to use

1. Turn **Edit Session** On, then press **Modify Features** on the KGA Editor toolbar.
2. Find a tool by category or by the search box, and click its tile.
3. Select features if the page asks you to. A banner reads **Change the selection.**; click it to
   pick features again, and the list below shows what is selected.
4. Set the options and press the tool's button (for example **Move To**), or draw on the map.
5. Press the back arrow to return to the tile list.

**Favorites.** Right-click a tile > *Add to favorites* and the tool gets a button on the KGA Editor
toolbar; right-click that button > *Remove from favorites*. Drag a favorite to reorder. The list is
kept in your QGIS settings, so it is the same in every project.

## Notes and limits

* Edits go to the edit buffer; nothing is saved until you press Save on the toolbar.
* Move, Rotate and Scale: a plain click selects a feature, a drag anywhere transforms the selection.
  Move To takes absolute, delta, or direction-and-distance values; a map click only fills them in.
* Edit Vertices lists the vertices of the selected feature in the map CRS and lets you type new X and Y.
* Distances and areas on a layer in degrees are worked in the matching UTM zone.

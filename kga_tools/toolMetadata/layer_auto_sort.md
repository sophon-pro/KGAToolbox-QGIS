# Layer Auto-Sort

|  |  |
|---|---|
| **Algorithm ID** | none: a background feature with a submenu of the KGA Toolbox (not a Processing algorithm) |
| **Group** | KGA Toolbox menu > Layer Auto-Sort |
| **Source** | `kga_tools/core/layer_sort.py` (ordering logic and settings), `kga_tools/gui/layer_sort_controller.py` (actions, new-layer hook, Layers panel menu), `kga_tools/gui/layer_sort_dialog.py` (settings window) |
| **Icon** | `icons/sortLayer.png` |
| **Type** | Background feature with a menu and a settings window; no toolbar button. Needs QGIS 3.30 or newer; left out on older versions. |

## Overview

ArcGIS Pro keeps its table of contents tidy by data type: points on top, then lines,
polygons, and rasters at the bottom, so nothing is hidden under a basemap. Layer
Auto-Sort does the same for the QGIS Layers panel, in the background. Each new layer is placed
at the top of its own type band as it is added, so a basemap or other web raster added later
lands at the bottom instead of on top. What is already there can be sorted on demand.

Default order, top to bottom: annotation layers, tables (no geometry), points, lines,
polygons, vector layers of unknown geometry, vector tiles, point clouds, 3D tiles, mesh
layers, local rasters (DEM, orthophoto, imagery), and last web rasters and basemaps
(WMS, WMTS, XYZ, WCS, ArcGIS).

## Parameters

| Control | Meaning |
|---|---|
| **Sort Layers by Type** | KGA Toolbox > Layer Auto-Sort and the Layers panel right-click menu (no toolbar button). Sorts every group and the top level. |
| **Sort Group by Type** | Layers panel right-click menu on a group. Sorts only that group and the groups inside it. |
| **Auto-sort New Layers** | Switch (KGA Toolbox > Layer Auto-Sort). While On (the default), a layer you add goes to the top of its type band inside the group it was added to. |
| **Layer Sort Settings** | Opens the settings window. |
| **Order list** | Drag the types up or down; the top of the list is the top of the Layers panel (drawn on top). Restore Defaults resets it. |
| **Automatically place new layers when they are added** | The same switch as Auto-sort New Layers. |
| **Also sort the Layer Order panel when 'Control rendering order' is on** | Applies the same order to the Layer Order panel. On by default. |

## How to use

1. Nothing to do for new layers: they are placed as they are added. To tidy what is already there, choose **Sort Layers by Type** from the Layers panel right-click menu or the Layer Auto-Sort submenu. The Layers panel is reordered and a
   message says whether anything moved.
2. To stop the automatic placement, untick **Auto-sort New Layers** in the Layer Auto-Sort
   submenu.
3. To use a different order, open **Layer Sort Settings**, drag the types, and press OK.

## Notes and limits

* Sorting is stable: layers of the same type keep the order they already had. Groups keep
  their position; only the layers around them are arranged.
* A saved project is never reordered when it opens. Only layers added afterwards are placed.
* Every way of adding a layer is covered: the Browser and Catalog, Add Data, Processing results and
  other plugins. Moving a layer yourself (drag and drop in the Layers panel) is never undone, and opening a
  layer package keeps the order recorded in the package.
* Moving a layer never removes it from the project. Its visibility, expansion and
  properties are kept, and the selected layer stays selected.
* When several layers are added at once (a multi-file drop), the layers of one type may
  end up in reverse of the order they were added in.
* Sorting changes only the Layers panel and, if the option is on, the Layer Order panel.
  The data and the styles are not touched.
* The order and the switches are stored in the QGIS user profile, not in the project.
* If the stand-alone plugin "Layer Auto-Sort" (`layer_autosort`) is installed, KGA Toolbox
  leaves its own copy out and says so, so that layers are not sorted twice.

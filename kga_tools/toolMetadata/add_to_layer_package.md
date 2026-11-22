# Add Layers to Layer Package

|  |  |
|---|---|
| **Algorithm ID** | `kga:add_to_layer_package` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/add_to_layer_package.py` (window in `kga_tools/gui/add_to_package_dialog.py`, engine in `kga_tools/core/packaging.py`) |
| **Icon** | `icons/data_management/addToLayerPackage.png` |
| **Type** | Interactive (opens its own window; no Processing parameters) |

## Overview

Adds one or more layers to a `.kgalp` that already exists, with the same
treatment **[Create Layer Package](create_layer_package.md)** gives them: vector
data goes into the package GeoPackage (field domains included), rasters are
copied with their sidecar files, each style is saved as a QML, and the SVG and
image resources the symbology uses are collected.

## Window

| Input | Description |
|---|---|
| **Input layers** | A list of the project's vector and raster layers, each with a tick box. **Select all** / **Clear** tick or untick the lot. **Browse...** adds layers from files on disk (GeoPackage, shapefile, GeoJSON, raster...) that are not in the project; a container with several layers contributes each one, ticked. |
| **Use only the selected features** | For every ticked vector layer that has a selection, only the selected features are added. Layers without a selection are added whole. The label counts how many ticked layers have a selection. |
| **Layer package (.kgalp)** | The existing package to add to. |

## How to use

1. Open **KGA Data Management > Add Layers to Layer Package**.
2. Tick the layers to add, or press **Browse...** for layers on disk.
3. Tick **Use only the selected features** if only a selection should go in.
4. Choose the target `.kgalp`.
5. Press **Add**. The window stays open for the next batch.

## Notes and limits

- Layers already in the package are never changed or replaced. A layer with the
  same name as an existing one is stored beside it (`name_2`).
- A zip cannot be appended to in place, so the package is rebuilt into a
  temporary file and swapped in only once complete. A failure (or a package
  open in another program) leaves the original untouched.
- Groups the new layers belong to in the Layers panel are merged into the
  package's recorded structure; a group with the same name is shared. Layers
  browsed from disk have no group and open at the top of the package group.
- Fonts cannot be packaged; a layer using a font marker gives a warning naming
  the font.
- The window can also be opened from the Browser: right-click a `.kgalp` and
  choose **Add Layers to Layer Package...** to start with that package chosen.
  If a layer read straight from the package is still in the project, Windows
  holds the file and the update is refused with a message; remove the layer and
  try again.

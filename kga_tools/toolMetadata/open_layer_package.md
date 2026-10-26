# Open Layer Package

|  |  |
|---|---|
| **Algorithm ID** | `kga:open_layer_package` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/layer_package.py` (window in `kga_tools/gui/open_package_dialog.py`, engine in `kga_tools/core/packaging.py`) |
| **Icon** | `icons/alg_package_in.png` |
| **Type** | Interactive (opens its own window; no Processing parameters) |

## Overview

Unpacks a `.kgalp` written by **[Create Layer Package](create_layer_package.md)**
and adds its layers, styles and group structure to the current project.

The package is extracted to a real folder on disk, never to a temporary one,
because the layers point at the extracted files — a temp folder would leave a
project full of broken layers after the next restart. SVG and image paths inside
each style are rewritten to the extracted copies, so the symbology renders the
same as it did on the machine that built the package.

## Window

| Input | Description |
|---|---|
| **Layer package (.kgalp)** | The package to open. |
| **Extract to** | Where the contents are unpacked. Leave it empty and a `KGA Packages/<package name>` folder is created beside the project file (or in your home folder if the project has never been saved); the box shows where. |
| **Add the layers to the project** | Untick to extract only, without touching the Layers panel. |
| **Group name** | The Layers-panel group the layers are placed in. Defaults to the package file name. |

## How to use

1. Open **KGA Data Management > Open Layer Package**.
2. Choose the `.kgalp` you received under **Layer package**.
3. Choose an **Extract to** folder somewhere permanent — beside the project is
   the usual choice. Skipping this is fine; the default is also permanent.
4. Press **Open**.
5. The layers appear in a group named after the package, styled, with the group
   structure of the original project rebuilt.
6. Read the message under the options for warnings carried over from the packaging machine — a missing
   font is the common one.

## Outputs

- The extracted package on disk: a GeoPackage of vector layers, raster files,
  QML styles, and the collected SVG/marker resources.
- The layers in the project, inside one group, with their styles applied and
  their symbol paths repointed at the extracted resources.

## Notes and limits

- **Do not extract to a temporary folder.** The layers reference the extracted
  files; if the folder disappears the project breaks. This is why the tool
  never uses a temp folder even when you leave the parameter empty.
- **Fonts still have to be installed by hand.** The package carries a warning
  naming any font marker it could not include; it is repeated in the log here.
- A package whose writing was interrupted has no manifest and is refused with a
  clear error, rather than opening half-loaded.
- Archive entries that would be written outside the extraction folder are
  refused — a package cannot write elsewhere on your disk.
- Layers listed in the manifest but missing from the archive are warned about and
  skipped; anything not mentioned by the recorded group tree is still hung off
  the group directly, so no layer is silently dropped.

## Browsing a package without opening it

A `.kgalp` also appears in the QGIS **Browser** panel (and the Data Source
Manager) like a database: expand it to list its layers, then drag a layer onto
the map or double-click it. Nothing is unpacked for this — layers are read in
place from inside the package (GDAL `/vsizip/`), so they are read-only and
arrive with QGIS's default style.

Right-click menu:

- On the package: **Open Layer Package...** (this tool, already pointed at the
  file) and **[Add Layers to Layer Package...](add_to_layer_package.md)**.
- On a layer: **Add to Project (with style)** unpacks the package once into
  `kga_package_cache` in the QGIS profile folder and loads the layer with its
  packaged style and SVG symbols.

A layer loaded straight from the Browser keeps the package file open, so
Windows will not let **Add Layers to Layer Package** replace it until that layer
is removed from the project.

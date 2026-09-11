# Create Layer Package

|  |  |
|---|---|
| **Algorithm ID** | `kga:create_layer_package` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/layer_package.py` |
| **Icon** | `icons/alg_package_out.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it reads the project layer tree) |

## Overview

Packages layers, their styles and everything those styles need into a single
`.kgalp` file that opens on a machine that has never seen the data.

A QLR file only *points at* data that has to travel beside it. A layer package
*contains* it. Vector layers are copied into one GeoPackage inside the archive —
field domains included — rasters are copied as their own files with their
sidecars, each style is saved as a QML, and every SVG symbol and raster marker
image the symbology references is collected and de-duplicated. The group
structure of the Layers panel is recorded so the receiving end rebuilds it.

Reach for it when you need to hand a styled map to a colleague, a client or a
field team as one file.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Layers to package** | Multiple map layers | — | The vector and raster layers to include. Anything that is neither is skipped with a warning. |
| **Package only the selected features** | Boolean | `False` | Package just the current selection of each vector layer instead of all its features. |
| **Collect SVG symbols and marker images** | Boolean | `True` | Copies the SVG and image files the symbology points at into the package and records where they came from, so the symbols still render on the far side. |
| **Include layer metadata** | Boolean | `True` | Stores each layer's title, abstract and identifier in the manifest. |
| **Layer package** | File destination | — | The `.kgalp` to write. The extension is appended if you leave it off. |
| **Package report** | File destination (HTML) | not created | Optional run report: counts, a per-layer table and every warning. Tick it in the Processing dialog to produce one. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Layers packaged** | Number | Vector plus raster layers actually written (skipped layers are not counted). |

## How to use

1. Load and style the layers in QGIS exactly as you want them to arrive.
2. Open **KGA Data Management > Create Layer Package**.
3. Click the **…** button beside *Layers to package* and tick the layers. Group
   structure comes from the Layers panel, not from this list.
4. If you only want part of the data, select those features on the map first and
   tick **Package only the selected features**.
5. Choose where the `.kgalp` goes and press **Run**.
6. Read the log. Warnings here are the ones that matter — especially fonts.
7. Send the single `.kgalp` file. The recipient opens it with
   **[Open Layer Package](open_layer_package.md)**.

## Outputs

- One `.kgalp` archive at the chosen path, holding:
  - a GeoPackage with every vector layer (field domains preserved),
  - each raster copied as its own file with its sidecars,
  - a QML style per layer,
  - the collected SVG and marker image files, de-duplicated,
  - a manifest recording the layers, CRSs, styles, resources and group tree.
- An optional HTML **Package report**.
- The **Layers packaged** count in the Processing results panel.

## Notes and limits

- **Fonts cannot be packaged.** If a layer uses a font marker you get a warning
  naming the font, so you can tell the recipient what to install. Nothing else
  in the styling has this problem.
- **The manifest is written last, on purpose.** A package interrupted halfway
  through has no manifest, and Open Layer Package refuses it rather than opening
  a half-loaded project. An interrupted run leaves a file you should delete.
- Invalid layers, and layers that are neither vector nor raster, are skipped —
  each with a warning naming the layer. Check the count in the log against the
  number you ticked.
- Package size is reported in the change report. Rasters dominate it; a large
  DEM makes a large package.
- Pairs with **[Open Layer Package](open_layer_package.md)**, which is why the
  two sit at the top of the Data Management menu together.

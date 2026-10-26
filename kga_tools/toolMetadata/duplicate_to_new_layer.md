# Duplicate to New Layer

|  |  |
|---|---|
| **Algorithm ID** | `kga:duplicate_to_new_layer` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/duplicate_to_new_layer.py` (dialog in `kga_tools/gui/duplicate_layer_dialog.py`, engine in `kga_tools/core/duplicate_layer.py`, right-click entry in `kga_tools/gui/layer_menu.py`) |
| **Icon** | `icons/duplicateLayer.png` |
| **Type** | Interactive dialog, modeless; also on the right-click menu of vector and raster layers, below QGIS's Duplicate Layer |

## Overview

QGIS's built-in *Duplicate Layer* adds a second layer pointing at the **same**
data source, so editing or deleting features in the copy changes the original.
This tool copies the data as well, so the copy is fully independent.

* **Vector layers** are written to a GeoPackage, shapefile, File Geodatabase, GeoJSON, KML or KMZ, as you choose.
* **Rasters** are written to a new GeoTIFF (LZW, tiled) with the same values,
  no-data value and data type.
* **Memory (scratch) layers** are cloned in memory; no file is needed.

Symbology, labels, aliases and form settings are copied. The new layer is named
`<layer> (copy)` and sits directly above the original.

## Parameters

| Control | Meaning |
|---|---|
| **Original Layer** | A vector or raster layer in the project. |
| **Output Location** | The **folder** to save the copy in. Empty saves it beside the original. The file is named `<layer>_copy` (numbered if taken), so a copy never replaces an existing file. Disabled for a memory layer. |
| **Output type** | GeoPackage (default), Shapefile, File Geodatabase, GeoJSON, KML or KMZ. For a raster the list is GeoTIFF, COG, IMG, PNG, JPEG, JPEG 2000 and ASCII Grid. A format this QGIS cannot write is greyed out. |
| **Extent to copy** *(rasters only)* | **Whole raster** (default), **Current map extent**, **Extent of a layer**, **Draw a rectangle or polygon** on the map, or **Boundary of a polygon layer** (optionally selected features only). |
| **Selected features only** *(vectors)* | Copy just the features selected on the original layer. Available only when a vector layer has a selection; the box shows how many. |

## How to use

Right-click a layer in the Layers panel and choose **Duplicate to New Layer…**
(just below QGIS's *Duplicate Layer*), or open **KGA Data Management > Duplicate
to New Layer**. The window stays open, so several layers can be copied in a row.

## Notes and limits

* A layer **filter** limits what is copied; the copy has no filter.
* **Unsaved edits** in an edit-mode layer are included in the copy.
* Rasters must be files on disk; WMS, XYZ and other web services are refused.
* **Cutting a raster.** A map or layer extent, or a drawn rectangle, copies that
  box of whole pixels. A drawn polygon or a layer boundary also sets every pixel
  outside the shape to NoData: the raster's own NoData value, or `-9999` for
  floating-point data and `0` otherwise when it has none. An area that does not
  overlap the raster is refused.
* **Format limits** are checked before anything is written: PNG takes 8- or
  16-bit data and up to 4 bands, JPEG 8-bit and up to 3 bands, JPEG 2000 integer
  data, ASCII Grid one band. JPEG is lossy. Use GeoTIFF for anything else.
* **Shapefile** field names are cut to 10 characters. **KML and KMZ** are written
  in WGS 84 (EPSG:4326), as Google Earth requires. A **File Geodatabase** copy is
  a new `.gdb` folder holding one layer, and its layer name is limited to
  letters, digits and underscores.
* A GeoPackage source that already has its own `fid` attribute keeps it; the
  copy's feature-id column is called `fid_gpkg`.

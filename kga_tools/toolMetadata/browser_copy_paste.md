# Browser Copy and Paste

|  |  |
|---|---|
| **Algorithm ID** | none: right-click menu items in the Browser panel, the Catalog and the Data Source Manager |
| **Group** | Browser / Catalog right-click menu |
| **Source** | `kga_tools/gui/browser_paste.py`, `kga_tools/gui/paste_dialog.py`, `kga_tools/core/data_paste.py` |
| **Icon** | `icons/data_management/copyInBrowser.png`, `icons/data_management/pasteInBrowser.png`, `icons/data_management/addToMap.png` |
| **Type** | Context-menu commands with a small options window |

## Overview

Copy data in the Browser and paste it somewhere else, the way ArcGIS Pro's Catalog does. File Geodatabase
domains, aliases and OBJECTID travel with the data.

## Menu items

| Item | Where | What it does |
|---|---|---|
| **Add to Map** | Layers, GeoPackages, File Geodatabases | Adds the selection to the project. Layers that cannot be copied (web services, meshes, point clouds) can still be added |
| **Copy** | Layers, GeoPackages, File Geodatabases | Holds the selection. A whole GeoPackage or geodatabase copies every layer in it |
| **Paste** | Folders, GeoPackages, File Geodatabases | Writes the held data. Greyed out until something is copied |
| **Paste as Shapefile** | Folders | Pastes straight to shapefiles |

## Paste targets

* **Folder:** as shapefile, GeoJSON, GeoPackage or File Geodatabase (you choose).
* **GeoPackage or File Geodatabase:** added as new tables.
* Rasters paste only into folders (GeoTIFF).

When a name already exists you choose **Keep both** (adds `_2`), **Replace the existing one** or **Skip it**.
The choices are remembered.

## Notes and limits

* A layer with a subset filter, or from PostGIS or delimited text, is written through a scratch GeoPackage first.
* Shapefile field names are cut to 10 characters, so a long name is shortened (GDAL says so in the log).
* Pasting into a File Geodatabase that ArcGIS has locked is refused.

# Layer Export / Import

|  |  |
|---|---|
| **Algorithm ID** | `kga:layerexportimport` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/LayerExportImport.py` |
| **Icon** | `icons/import_export.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

Bulk-exports the vector layers of the current project to GeoPackage, Shapefile,
GeoJSON, KML or CSV, and pulls layers out of any vector file back into the
project — in one window, with a tick box per layer.

QGIS can export one layer at a time through *Export > Save Features As*. This is
the same job for twenty layers, with renaming on the way out and a single output
folder. Because the window is **modeless** you can drag a file straight from the
Browser panel onto it.

The tool is promoted onto the KGA toolbar as its own button.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's.

### Tab 1 — Export Layers

| Control | Type | Default | Description |
|---|---|---|---|
| Project layer table | Checkable table | nothing ticked | The vector layers in the QGIS Layers panel. Columns: *Layer*, *Geometry*, *Features*, *Export As*. *Export As* is editable, so you can rename on the way out. Double-click a row to toggle it. |
| **Select All** / **Clear All** | Buttons | — | Tick or untick every row. |
| **Refresh from Layers Panel** | Button | — | Re-read the Layers panel after adding or removing layers in QGIS. |
| **Output Format** | Combo box | GeoPackage | `GeoPackage (.gpkg)` — all layers in one file; `ESRI Shapefile (.shp)`, `GeoJSON (.geojson)`, `KML (.kml)`, `CSV (.csv)` — one file per layer. CSV writes attributes with geometry as WKT. |
| **GeoPackage File** | Text box | `export_layers.gpkg` | Name of the single output container. Only shown for the GeoPackage format. |
| **Output Folder** | Read-only path box | empty | Where everything is written. Set it with **Browse Folder**. |
| **Export selected features only** | Checkbox | unticked | Exports the current map selection of each layer instead of all its features. |
| **Overwrite layers that already exist in the target (otherwise a numbered name is used)** | Checkbox | unticked | Off, a name clash gets a numbered suffix rather than replacing anything. |
| **Export Checked Layers** | Button | — | Runs the export and reports a summary. |

### Tab 2 — Import Layers

| Control | Type | Default | Description |
|---|---|---|---|
| **Source File** | Read-only path box, drop target | empty | The file to read. Drag a `.gpkg`, `.shp`, `.geojson`, `.kml` and so on here from the Browser panel, or use **Browse File**. |
| Layer table | Checkable table | nothing ticked | The layers found inside the source. Columns: *Layer*, *Geometry*, *Features*, *Add As*. *Add As* is editable, so you can rename on the way in. |
| **Select All** / **Clear All** | Buttons | — | Tick or untick every row. |
| **Rescan File** | Button | — | Re-read the source after it changed on disk. |
| **Add the imported layers to the project** | Checkbox | **ticked** | Untick to scan without loading anything. |
| **Reproject to the project CRS on import (creates a memory layer)** | Checkbox | unticked | On, each layer arrives reprojected — as a memory layer, so save it if you want to keep it. |
| **Import Checked Layers** | Button | — | Runs the import and reports a summary. |

## How to use

**To export:**

1. Open **KGA Data Management > Layer Export / Import**, or press its button on
   the KGA toolbar. Close the Processing window once the pane appears.
2. On **1. Export Layers**, tick the layers. Edit the *Export As* cell to rename
   one on the way out.
3. Choose the **Output Format**. GeoPackage puts everything in one file; the
   others write one file per layer.
4. Set the **Output Folder** with **Browse Folder**, then press **Export Checked
   Layers**.

**To import:**

1. Switch to **2. Import Layers**.
2. Drag the source file onto **Source File** from the Browser panel, or use
   **Browse File**. The layers it holds are listed.
3. Tick the ones you want, edit the *Add As* names if needed.
4. Tick **Reproject to the project CRS** only if you need it â€” the result is a
   memory layer you will have to save.
5. Press **Import Checked Layers**.

## Outputs

- **Export:** the ticked layers written to the chosen folder — one GeoPackage
  holding all of them, or one file per layer for the other formats. A summary
  message box lists what was written, skipped and failed.
- **Import:** the ticked layers added to the project, renamed as you asked,
  optionally reprojected.

Nothing is returned to Processing.

## Notes and limits

- **The window stays open.** Close the Processing dialog behind it and keep
  working. Running the tool again while the window is open brings it forward;
  running it after you closed it starts clean, with the ticks, output folder and
  scanned file cleared.
- **Reprojected imports are memory layers.** They disappear when the project
  closes unless you save them to a file.
- **Styles do not travel here.** This tool moves data. To carry symbology,
  labels, aliases and forms along, use **[Spatial Data Manager](spatialdatamanager.md)**
  (its Export tab writes styles into the target) or
  **[Create Layer Package](create_layer_package.md)**.
- Shapefile output truncates field names to 10 characters; CSV output keeps
  attributes only, with the geometry as a WKT column.
- `layer_styles` and `qgis_projects` are QGIS housekeeping tables and never
  appear in the import list.
- The tool cannot run headless or inside a model — it needs the QGIS main window.

# Spatial Data Manager

|  |  |
|---|---|
| **Algorithm ID** | `kga:spatialdatamanager` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/GeoPackage_Data_Manager.py` |
| **Icon** | `icons/spatialDataManager.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

Bulk delete, rename, import and export the layers of a container: a GeoPackage,
a File Geodatabase, a SpatiaLite/SQLite database or a folder of shapefiles.

QGIS has no single place to do housekeeping on a container. Deleting twelve
layers out of a GeoPackage means twelve trips through the Browser panel. This
window does the whole batch against one target, and because it is **modeless**
you can drag a dataset or a layer straight from the Browser panel onto it while
it is open.

The tool is promoted onto the KGA toolbar as its own button rather than living
in the Data Management dropdown, because it is where most container work starts.

## Parameters

The algorithm itself takes no Processing parameters — running it opens the
window. The controls below are the window's.

### Target

| Control | Type | Default | Description |
|---|---|---|---|
| **Target Database** | Read-only path box, drop target | empty | The container everything acts on. Drag a `.gpkg`, `.gdb`, `.sqlite` or a folder here from the Browser panel, or use **Browse Target**. |
| **Browse Target** | Menu button | — | Three ways to pick: *GeoPackage / SQLite file…*, *File Geodatabase (.gdb)…*, *Shapefile folder…*. |

### Tab 1 — Delete Data

| Control | Type | Default | Description |
|---|---|---|---|
| **Filter layers** | Text box | empty | Narrows the table as you type. |
| Layer table | Checkable table | nothing ticked | Columns: *Layer*, *Geometry*, *Features*. Double-click a row to toggle its tick. |
| **Select All** / **Clear All** | Buttons | — | Tick or untick everything currently shown. |
| **Delete Selected** | Button | — | Drops the ticked layers from the container. Asks for confirmation. |

### Tab 2 — Rename Data

| Control | Type | Default | Description |
|---|---|---|---|
| Rename table | Editable table | — | Two columns: *Original Name* and *New Name*. Type over the second column for the layers you want renamed; leave the rest alone. |
| **Apply Renames** | Button | — | Applies every changed row in one pass. |

### Tab 3 — Import Data

| Control | Type | Default | Description |
|---|---|---|---|
| Import queue | Table, drop target | empty | Columns: *Source Layer*, *Import As*, *Source File*. Drag layers or whole datasets here from the Browser panel. The *Import As* cell is editable, so you can rename on the way in. |
| **Add Data to Import List ▾** | Menu button | — | *Vector files (Shapefile, GeoJSON, KML…)*, *GeoPackage / SQLite database…*, *File Geodatabase (.gdb)…*, *Folder of shapefiles…*. Picking a container opens a layer chooser. |
| **Remove Selected** / **Clear List** | Buttons | — | Take rows out of the queue. |
| **Overwrite layers that already exist in the target** | Checkbox | unticked | Off means a name clash is skipped rather than replaced. |
| **Run Import** | Button | — | Writes the whole queue into the target and reports a summary. |

### Tab 4 — Export Layers

| Control | Type | Default | Description |
|---|---|---|---|
| Project layer table | Checkable table | nothing ticked | The layers currently in the QGIS Layers panel. Columns: *Layer*, *Geometry*, *Features*, *Styles*, *Export As*. *Export As* is editable. Double-click a row to toggle it. |
| **Refresh Layer List** | Button | — | Re-reads the Layers panel after you add or remove layers in QGIS. |
| **Select All** / **Clear All** | Buttons | — | Tick or untick every row. |
| **Export only the selected features of each layer** | Checkbox | unticked | Exports the current map selection instead of the whole layer. |
| **Overwrite layers that already exist in the target** | Checkbox | unticked | As above, for the export direction. |
| **Export the styles too (symbology, labels, aliases, forms, …)** | Checkbox | **ticked** | Stores every named style of each layer in the target. |
| **Also drop a .qml copy of each style beside the target** | Checkbox | unticked | A portable backup of the style. Always written for shapefile targets, which have nowhere else to keep one. |
| **Export Checked Layers** | Button | — | Runs the export and reports a summary. |

## How to use

1. Open **KGA Data Management > Spatial Data Manager**, or press its button on
   the KGA toolbar. Close the Processing window once the pane appears.
2. Set the **Target Database**: drag a `.gpkg`, `.gdb`, `.sqlite` or a shapefile
   folder onto the box from the Browser panel, or use **Browse Target**.
3. Pick the tab for the job:
   - **1. Delete Data** â€” filter the list, tick the layers, press **Delete
     Selected**. Copy the container first; this cannot be undone.
   - **2. Rename Data** â€” type the new names into the second column and press
     **Apply Renames**.
   - **3. Import Data** â€” drag layers or datasets onto the queue, or use **Add
     Data to Import List**, edit the *Import As* names, then **Run Import**.
   - **4. Export Layers** â€” tick the project layers, leave **Export the styles
     too** on, then **Export Checked Layers**.
4. Read the summary box after each import or export before moving on.

## Outputs

Nothing is returned to Processing — the tool writes directly into the target
container:

- **Delete** removes the ticked layers from the container.
- **Rename** renames them in place.
- **Import** adds the queued layers to the target.
- **Export** writes the ticked project layers into the target and, when styles
  are included:
  - *GeoPackage / SpatiaLite:* into the `layer_styles` table, so QGIS re-applies
    the style by itself the next time the layer is added to a project;
  - *Shapefile folder:* as a `.qml` beside each shapefile.
- A summary message box after each import and export lists what was written,
  skipped and failed.

## Notes and limits

- **The window stays open.** Once it appears you can close the Processing dialog
  behind it and carry on working on the map. Running the tool again while the
  window is open just brings it forward; running it after you closed it starts
  clean, with the target, ticks and import queue empty.
- **Deletes are not undoable.** There is no edit session behind this — the layer
  is dropped from the container. Copy the container first if you are unsure.
- `layer_styles` and `qgis_projects` are QGIS housekeeping tables, not user
  layers, so they never appear in the delete or rename lists.
- Close the container in any other application first. A File Geodatabase still
  open in ArcGIS has a `.lock` file in its folder and cannot be modified.
- The tool cannot run from a headless Processing session or inside a model — it
  needs the QGIS main window.
- For the reverse direction — pulling layers *out* of a container into standalone
  files — see **[Layer Export / Import](layerexportimport.md)**,
  **[GeoPackage to Shapefiles](gpkgtoshapefiles.md)** and
  **[GeoPackage to File Geodatabase](geopackage_to_filegdb.md)**.

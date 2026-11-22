# Data Explorer – a Premiere Pro–style importer for QGIS

Requires QGIS 3.44 or newer, including QGIS 4.x (Qt6).

## Install
Plugins ▸ Manage and Install Plugins ▸ Install from ZIP ▸ choose `geo_media_ingest.zip`.
Open it from the toolbar icon, Layer ▸ Add Layer, Plugins ▸ Data Explorer, or **Ctrl+Alt+I**.

## How Premiere Pro concepts map to QGIS

| Premiere Pro | Data Explorer | QGIS machinery used |
|---|---|---|
| Media Browser panel | **Data Explorer** dock: folder tree, file list, preview pane | `QFileSystemModel`, `QgsProviderRegistry.querySublayers()` |
| Hover-scrub / Source Monitor | Live thumbnail, Metadata tab, first 50 rows in Attributes tab | `QgsMapRendererParallelJob` (async), `QgsFeatureRequest.setLimit` |
| Multi-clip camera cards | Expand GeoPackage / KML / GDB / NetCDF / Excel to pick sublayers | `QgsProviderSublayerDetails` |
| Ingest checkbox + wrench | **Ingest** checkbox + **Ingest Settings** | `QgsSettings` |
| Copy | Copy dataset + all sidecars (.prj, .dbf, .aux.xml, .qml…) | chunked copy in `QgsTask` |
| Transcode | Convert vectors → GeoPackage / FlatGeobuf / Shapefile / GeoJSON / Parquet; rasters → COG or tiled GeoTIFF; optional reprojection to project CRS | `QgsVectorFileWriter`, `gdal.Translate`, `gdal.Warp` |
| Create Proxies | Overviews (pyramids) + optional low-res raster proxy; originals never modified (VRT + .ovr) | `gdal.BuildOverviews` |
| Toggle Proxies button | **Proxies** button / menu action swaps proxy ↔ full resolution, keeping the style | `QgsMapLayer.setDataSource` |
| Ingest presets (AME) | Built-in + user presets; export/import as JSON to share with a class | JSON in `QgsSettings` |
| Interpret Footage | **Interpret…**: CRS override, encoding, NoData, feature filter, layer name, bin, CSV delimiter / X-Y / WKT with preview | provider-level overrides |
| Bins / import folder as bin | Bins = layer groups (by type, by folder, custom); **Import Folder as Bin** mirrors sub-folders | `QgsLayerTreeGroup` |
| Background ingest while editing | All heavy work runs in the QGIS task manager, cancellable | `QgsTask` |
| Clip metadata / XMP | Provenance written to each layer's Metadata ▸ History | `QgsLayerMetadata.addHistoryItem` |
| Drag & drop | Drop files on the list to import; drag items to the map | Qt drag & drop |

## Search (v1.1)
Type in the search box; results appear as you type, ranked by relevance, with a **Why it matched** column.
A background index (stored in your QGIS profile) records every dataset's layers, fields, CRS and extent,
so you can find a feature class inside a geodatabase or a column inside a GeoPackage, not just file names.
Re-indexing only re-reads files that changed.

| Example | Finds |
|---|---|
| `roads river` | both words, anywhere (name, layer, field, CRS, folder) |
| `roads OR streets`, `roads -draft`, `"land use"`, `(a OR b) c` | Boolean logic, phrases, grouping |
| `dem*`, `*_utm48`, `s2?_b04` | wildcards |
| `layer:villages`, `field:NAME_KH`, `crs:32648`, `type:raster`, `ext:gpkg`, `in:fieldwork` | targeted fields |
| `size>100mb`, `modified:7d`, `modified>2025-01-01`, `features>10000` | size, date, feature-count filters |
| `provnces` | typo-tolerant: finds *Provinces* when nothing matches exactly |

Scope: this folder, folder + subfolders, favourite folders, or all indexed locations.
**In map extent** keeps datasets overlapping the current map view. **Save search as Search Bin** stores a
query for reuse, like Premiere Pro's Search Bins. Press **Ctrl+F** to search and **F1** for syntax help.
Khmer and other non-Latin names are fully supported.

## Mini Preview (v1.2)
Select a dataset or layer and press **Open Mini Preview…** (or right-click ▸ *Open in Mini Preview…*).
A separate read-only map window opens:

| Tool | How |
|---|---|
| Pan / Zoom in / Zoom out | `P`, `+`, `-`, mouse wheel; full extent `Ctrl+Shift+F`; previous / next view |
| Select | `S`, then click or drag a box. **Shift** adds, **Ctrl** removes, **Ctrl+Shift** keeps the overlap. Select all, invert, clear |
| Find / Select by attribute | Field + condition (equals, contains, starts with, >, <, is one of, is empty…) + a value picked from the data's own values; New / Add / Remove / Within selection; or a full expression |
| Identify | `I`, then click. Shows the fields ticked in **Fields**, labelled by the field you choose, plus derived area/length/coordinates. Rasters show pixel values |
| Selection table | Selected features (first 1,000) with the chosen fields; double-click a row to zoom |
| Export ▾ | Drop-down: **Current preview canvas** (`Ctrl+E`), **Draw rectangle**, **Draw polygon**, **Layer extent**, **Polygon boundary…** (the selected, else all, polygons of a project polygon layer). Vectors: selected / all / inside-the-area features → GeoPackage, Shapefile, GeoJSON, FlatGeobuf, CSV, Excel, KML or GeoParquet, with field choice and CRS. Rasters: the area (masked to a polygon) → a new GeoTIFF |

**Non-destructive guarantee:** the layer is opened read-only (editing is impossible), it is never added to
your project, and exports only create new files. Writing to the source file, inside its geodatabase, or over
a Shapefile's companion files is refused. New files go to `<project folder>/exports/` by default and appear in
an **Exports** group.

## Rename tab
The **Rename** button beside **Create** opens the **Rename** tab in the right-hand pane. It lists the datasets
selected in the file list and follows the selection (names you have typed are kept; a selected folder adds its
datasets; **Loaded Layers** adds the project's file-based layers). Choose **Rename in place** or **Create copies**
(to an output folder), edit the new names, and click the button at the bottom; the path under the list shows
the exact destination of the highlighted row. Shapefile components and SQLite / raster sidecars move together,
**Double-click a GeoPackage or File Geodatabase row** to list the tables inside it; tick and rename them
(GDAL layer rename, so project layers pointing at the table follow it). In **Create copies** mode a table is
copied inside its own database. Table names are made ArcGIS-safe in a geodatabase. Name clashes are caught before anything is touched, a failed move is undone, and project layers are re-pointed
at the new file. Source: `file_renamer.py`; test: `tests/data_ingest/test_file_renamer.py`.

## Performance (v1.2)
All disk work (opening layers, listing a geodatabase's layers, reading attributes, computing extents, folder
scans, selections, exports) runs in background tasks, so QGIS stays responsive with very large data.
Results are cached until the data changes on disk. The plugin also works around a QGIS deadlock that occurs
when one File Geodatabase is queried twice at the same time.

## Output folder
Default destination is `<project folder>/ingest/` with `Media/` (copies), `Converted/` and `Proxies/`.
If the project isn't saved yet, `~/GeoMediaIngest/` is used. Set your own in Ingest Settings.

## Notes
- Point clouds are not previewed (opening one starts indexing); they are imported normally.
- Smart CSV detection recognises lon/lat, x/y, easting/northing and WKT/geometry columns and `, ; tab |` delimiters.
- Progress and errors appear in the task manager and in Log Messages ▸ "Data Explorer".

## Copy, Cut, Paste and drag-and-drop move
Right-click data in the file list: **Copy data**, **Cut data (move)**, then **Paste** / **Move here** on a folder, GeoPackage or File Geodatabase (right-click it in the list or the tree; `Ctrl+C` / `Ctrl+X` / `Ctrl+V` in the list). Shapefile, GeoPackage and geodatabase tables convert between each other; rasters go to folders as GeoTIFF. Dragging data onto a folder or database in the tree (or a folder/database row in the list) **moves** it; hold `Ctrl` to copy. A move deletes an original (to the Recycle Bin) only after it was written in full, and removes project layers that used it. Engine: `transfer.py` over `core/data_paste.py`.

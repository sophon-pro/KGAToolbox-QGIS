# Data Explorer

|  |  |
|---|---|
| **Algorithm ID** | none: a dock panel (not a Processing algorithm) |
| **Group** | KGA Toolbox toolbar, next to Catalog |
| **Source** | `kga_tools/data_ingest/` (full reference: `kga_tools/data_ingest/README.md`) |
| **Icon** | `icons/dataIngest.png` |
| **Type** | Dock panel. Needs QGIS 3.44 or newer; shortcut **Ctrl+Alt+I** |

## Overview

A media browser for geodata, modelled on Premiere Pro's Media Browser. Browse a folder tree, preview a
dataset, read its metadata, then bring it into the project.

## Key points

* **Browse and preview:** folder tree, file list and a preview with a live thumbnail, Metadata tab and the
  first 50 rows of the Attributes tab. GeoPackages, KML, geodatabases and Excel files expand to pick layers.
* **Search:** results appear as you type, ranked, with a *Why it matched* column. A background index of
  layers, fields, CRS and extent finds a layer inside a geodatabase or a column inside a GeoPackage.
  Supports `OR`, `-word`, `"phrases"`, wildcards and filters such as `type:raster`, `crs:32648`,
  `size>100mb`, `modified:7d`. Ctrl+F searches, F1 shows the syntax.
* **Ingest:** copy a dataset with its sidecar files, or convert it (vectors to GeoPackage, FlatGeobuf,
  Shapefile, GeoJSON or Parquet; rasters to Cloud Optimized GeoTIFF or tiled GeoTIFF), with optional
  reprojection to the project CRS. Presets can be saved and shared as JSON.
* **Proxies:** build overviews and a low-resolution proxy for big rasters without touching the original,
  and toggle between proxy and full resolution.
* **Interpret:** override CRS, encoding, NoData, feature filter and layer name, or set the CSV delimiter and
  X/Y or WKT columns, with a preview.
* **Bins:** group layers by type, by folder or your own; **Import Folder as Bin** mirrors sub-folders.
* **Background work:** ingest runs as a cancellable QGIS task. Provenance is written to the layer's
  metadata history.

## Notes and limits

* The original data is never modified.
* The search index lives in the QGIS profile and re-reads only files that changed.

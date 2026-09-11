# Shapefiles to GeoPackage

|  |  |
|---|---|
| **Algorithm ID** | `kga:shapefilestogpkg` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/shapefiles_to_gpkg.py` |
| **Icon** | `icons/gpkg.png` |
| **Type** | Batch algorithm |

## Overview

Collects every shapefile in a folder and packs them into a single GeoPackage, one
layer per file, using each file name as the layer name.

The usual reason to run it: a folder of shapefiles arrived from somewhere, and
you want one file to work with, back up and hand on. It can also reproject
everything to a common CRS on the way in, which is what you want when the folder
is a mix of UTM zones and degrees.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input folder** | Folder | — | The folder holding the shapefiles. |
| **Output GeoPackage** | File destination (`*.gpkg`) | — | The container to write. |
| **File extensions (comma separated)** | String, optional | `shp` | Which files to pick up. Widen it to pull in other OGR formats sitting in the same folder. |
| **Source encoding** | String, optional | `UTF-8` | Applied on **read**. This is the one to get right — see the notes. |
| **Reproject all layers to (optional)** | CRS, optional | empty | Leave empty to keep each layer in its own CRS. Set it to bring a mixed folder into one system. |
| **Search subfolders** | Boolean | `False` | On, walks the whole tree below the input folder. |
| **Append to existing GeoPackage (keep current layers)** | Boolean | `False` | On, adds to a GeoPackage that is already there instead of replacing it. |
| **Skip layers with no features** | Boolean | `False` | On, an empty shapefile produces no layer. |

## How to use

1. Open **KGA Data Management > Shapefiles to GeoPackage**.
2. Point **Input folder** at the folder and name the **Output GeoPackage**.
3. Set **Source encoding** to whatever the shapefiles actually are. If the
   attributes contain Khmer and there is no `.cpg` beside the `.shp`, this is
   almost always the setting that decides whether the text survives.
4. Set **Reproject all layers to** only if you want a single common CRS.
5. Press **Run**. The log reports each file as `ok`, `skip` or a failure, and
   finishes with a `Packed | Skipped | Failed` tally.

## Outputs

- One GeoPackage containing every shapefile as a layer.
- The **Output GeoPackage** path is returned to Processing.

## Notes and limits

- **Encoding is applied on read, not on write.** QGIS falls back to the system
  encoding when a `.cpg` sidecar is missing, and that is the usual cause of
  unreadable Khmer attributes. Setting it wrong here mangles the text
  permanently in the output.
- **Truncated field names cannot be recovered.** If a previous shapefile export
  cut `Construction_Year` down to `Constructi`, that is the name that arrives in
  the GeoPackage. Nothing here can restore it.
- Duplicate layer names are renamed rather than overwritten; the log says
  `renamed duplicate to …` when it happens.
- The reverse direction is **[GeoPackage to Shapefiles](gpkgtoshapefiles.md)**.

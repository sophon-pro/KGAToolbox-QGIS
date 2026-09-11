# GeoPackage to File Geodatabase

|  |  |
|---|---|
| **Algorithm ID** | `kga:geopackage_to_filegdb` |
| **Group** | KGA Data Conversion |
| **Source** | `kga_tools/algorithms/gdb_gpkg.py` (engine in `kga_tools/core/gdb_convert.py`) |
| **Icon** | `icons/alg_gpkg_to_gdb.png` |
| **Type** | Batch algorithm |

## Overview

Writes a whole GeoPackage back out as an ArcGIS File Geodatabase, ready to hand
to someone working in ArcMap or ArcGIS Pro.

This is the outbound half of the ArcGIS round trip. The geodatabase gets what
ArcGIS expects: an **OBJECTID** column, optional **Shape_Length** and
**Shape_Area** fields, coded and range **field domains** carried over intact, and
lines and polygons stored as multi-part — which is what every ArcGIS feature
class is.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **GeoPackage** | File (`.gpkg`) | — | The container to convert. |
| **Output File Geodatabase (.gdb folder)** | Folder destination | — | The `.gdb` to write. It is a folder, not a file. |
| **Readable by** | Enum | `Every ArcGIS version (64-bit integers become decimals)` | Or `ArcGIS Pro 3.2 and later (keeps 64-bit integers)`. Leave it on the compatible setting unless you know the recipient is on Pro 3.2 or newer. |
| **Put the layers in this feature dataset** | String, optional | empty | Creates a feature dataset of that name and puts the layers in it. |
| **Create Shape_Length and Shape_Area fields** | Boolean | `True` | The fields ArcGIS maintains itself. |
| **Carry QGIS layer styles across (adds a KGA_layer_styles table)** | Boolean | `True` | ArcGIS ignores the table; the reverse tool uses it to give you your symbology back. |

### Advanced

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Only these layers (comma separated)** | String, optional | empty (all) | e.g. `Roads, Canals`. |
| **Skip layers with no features** | Boolean | `False` | |
| **Convert curved geometries to straight segments** | Boolean | `False` | |
| **Add to the existing output instead of replacing it** | Boolean | `False` | |
| **Keep the original layer name as the ArcGIS alias** | Boolean | `True` | See *Names* below. |
| **Keep GeoPackage feature ids as ObjectID values** | Boolean | `True` | |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Layers converted** | Number | |

## How to use

1. Finish the editing in QGIS and close anything holding the output folder.
2. Open **KGA Data Conversion > GeoPackage to File Geodatabase**.
3. Pick the **GeoPackage** and name the output `.gdb` **folder**.
4. Leave **Readable by** on *Every ArcGIS version* unless you have been told the
   recipient is on Pro 3.2 or later.
5. Press **Run** and read the log — every layer rename is listed there.

## Outputs

- A `.gdb` folder holding every layer, with OBJECTID, optional Shape_Length and
  Shape_Area, field domains and multi-part geometry.
- A `KGA_layer_styles` table when styles are carried across.
- The **Layers converted** count.

## Notes and limits

- **Names get rewritten.** A geodatabase table name may hold only letters, digits
  and underscores and may not start with a digit, so `2024 roads-final` becomes
  `T_2024_roads_final`. The name you started with is kept as the ArcGIS **alias**
  — what the coworker sees in the table of contents, and what
  **[File Geodatabase to GeoPackage](filegdb_to_geopackage.md)** restores when
  the data comes back. Every rename is listed in the log.
- **The compatibility setting costs you 64-bit integer precision.** On *Every
  ArcGIS version*, 64-bit integer fields become decimals — a problem for very
  large ID numbers and for nothing else.
- **Lines and polygons become multi-part**, because that is what an ArcGIS
  feature class is.
- **Replacing a `.gdb` the project still has open** fails on Windows with a bare
  permission error. The tool checks first and names the layers holding it.
- The reverse direction is
  **[File Geodatabase to GeoPackage](filegdb_to_geopackage.md)**.

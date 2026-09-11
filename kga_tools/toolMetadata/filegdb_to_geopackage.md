# File Geodatabase to GeoPackage

|  |  |
|---|---|
| **Algorithm ID** | `kga:filegdb_to_geopackage` |
| **Group** | KGA Data Conversion |
| **Source** | `kga_tools/algorithms/gdb_gpkg.py` (engine in `kga_tools/core/gdb_convert.py`) |
| **Icon** | `icons/alg_gdb_to_gpkg.png` |
| **Type** | Batch algorithm |

## Overview

Copies **every layer and table** of an ArcGIS File Geodatabase into one
GeoPackage you can actually edit in QGIS.

This is the inbound half of the ArcGIS round trip: bring the data in, work on it,
then hand it back with
**[GeoPackage to File Geodatabase](geopackage_to_filegdb.md)**.

**What travels with the data**

- Coded and range **field domains**, so the drop-down lists still work.
- Non-spatial tables, Z and M values, and true curves.
- Layer **aliases**: an ArcGIS class called `Canal_Alignment` with the alias
  *Canal Alignment* arrives under the readable name.

**What changes** — a GeoPackage is flat, so feature datasets disappear.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **File Geodatabase (.gdb folder)** | Folder | — | The `.gdb` to read. It is a folder, not a file. |
| **Output GeoPackage** | File destination (`*.gpkg`) | — | The container to write. |
| **Use layer aliases as layer names** | Boolean | `True` | Off, the raw class names are used instead of the readable aliases. |
| **Prefix layer names with their feature dataset** | Boolean | `False` | Feature datasets do not survive into a GeoPackage. Turn this on to keep their names visible as a prefix — useful when two datasets hold same-named classes. |
| **Add the converted layers to the project** | Boolean | `False` | |
| **Restore QGIS layer styles the geodatabase carries** | Boolean | `True` | Reads the `KGA_layer_styles` table the reverse tool writes, so symbology comes back on a round trip. |

### Advanced

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Only these layers (comma separated)** | String, optional | empty (all) | e.g. `Roads, Canals`. |
| **Skip layers with no features** | Boolean | `False` | |
| **Convert curved geometries to straight segments** | Boolean | `False` | Off, true curves are preserved — a GeoPackage can hold them. |
| **Add to the existing output instead of replacing it** | Boolean | `False` | Off, the output is replaced. |
| **Keep ObjectID values as GeoPackage feature ids** | Boolean | `True` | Keeps the ArcGIS ObjectID as the GeoPackage fid, so a record keeps its identity across the round trip. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Layers converted** | Number | |

## How to use

1. **Close the geodatabase in ArcGIS first.**
2. Open **KGA Data Conversion > File Geodatabase to GeoPackage**.
3. Point the input at the `.gdb` **folder** and name the output GeoPackage.
4. Leave the defaults unless you want only part of the data — then use *Only
   these layers* under Advanced.
5. Press **Run** and read the log: it names every layer converted, renamed or
   skipped.

## Outputs

- One GeoPackage holding every layer and table of the geodatabase, with field
  domains, Z/M values and true curves intact.
- Layer styles restored when the geodatabase carries a `KGA_layer_styles` table.
- The layers added to the project when that box is ticked.
- The **Layers converted** count.

## Notes and limits

- **Close the geodatabase in ArcGIS first.** A `.lock` file in the folder means
  it is still open there, and the log will say so.
- **Feature datasets disappear.** A GeoPackage is flat. Use *Prefix layer names
  with their feature dataset* if the grouping carries meaning.
- **Replacing a GeoPackage the project still has open** fails on Windows with a
  bare permission error. The tool checks first and names the layers holding it.
- The reverse direction is
  **[GeoPackage to File Geodatabase](geopackage_to_filegdb.md)**. The two are
  pinned together at the top of the Data Conversion menu because neither makes
  sense read apart.

# GeoPackage to Shapefiles

|  |  |
|---|---|
| **Algorithm ID** | `kga:gpkgtoshapefiles` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/gpkg_to_shapefiles.py` |
| **Icon** | `icons/shapefile.png` |
| **Type** | Batch algorithm |

## Overview

Exports every spatial layer inside a GeoPackage to its own ESRI Shapefile in a
folder, in one pass.

This is the format-of-last-resort direction: you run it when something downstream
— an old survey package, a government submission template, a consultant workflow
— will only accept shapefiles. Keep the GeoPackage as the master copy.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input GeoPackage** | File (`*.gpkg`) | — | The container to unpack. Every spatial sublayer inside it is exported. |
| **Output folder** | Folder destination | — | Where the shapefiles are written. Created if it does not exist. |
| **Field encoding** | String, optional | `UTF-8` | The encoding written into each shapefile and its `.cpg` sidecar. Leave it at UTF-8 unless the consumer demands otherwise — this is what keeps Khmer attributes readable. |
| **Filename prefix (optional)** | String, optional | empty | Prepended to every output file name, e.g. `2024_` giving `2024_Canal.shp`. |
| **Skip layers with no features** | Boolean | `False` | On, empty layers produce no file at all. |
| **Overwrite existing shapefiles** | Boolean | `True` | Off, a layer whose `.shp` already exists in the folder is skipped rather than replaced. |

## How to use

1. Open **KGA Data Management > GeoPackage to Shapefiles**.
2. Pick the **Input GeoPackage** and an empty **Output folder**.
3. Leave **Field encoding** at `UTF-8` unless you have been told otherwise.
4. Press **Run** and read the log: it names every layer as `ok`, `skip` (with the
   reason) or `FAILED`, and finishes with an `Exported | Skipped | Failed` tally.

## Outputs

- One `.shp` per spatial layer in the chosen folder, with its `.dbf`, `.shx`,
  `.prj` and a `.cpg` sidecar recording the encoding.
- Each layer keeps its own CRS — nothing is reprojected.
- The **Output folder** path is returned to Processing.

## Notes and limits

- **Field names are truncated to 10 characters.** That is the DBF limit, not a
  choice this tool makes, and the original names cannot be recovered from the
  shapefile afterwards.
- **Non-spatial tables are skipped** — a shapefile must have geometry. The log
  says `skip (no geometry)` for each one.
- A shapefile holds **one geometry type** and stores date-times without full
  precision. Layers that rely on either lose something in the trip.
- Layer names are sanitised for the file system: anything that is not a letter,
  digit, underscore or hyphen becomes an underscore, and runs of underscores are
  collapsed. Two layers whose names differ only in punctuation therefore collide —
  check the log.
- The reverse direction is **[Shapefiles to GeoPackage](shapefilestogpkg.md)**.

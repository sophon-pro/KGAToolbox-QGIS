# Update X/Y or Lat/Lon Fields

|  |  |
|---|---|
| **Algorithm ID** | `kga:updatexyfields` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/update_xy_fields.py` |
| **Icon** | `icons/updateXYFields.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it edits the layer in place) |

## Overview

Writes coordinate fields onto a layer: either `POINT_X` / `POINT_Y` in the
layer's own CRS, or `LONGITUDE` / `LATITUDE` in WGS 84. The fields are created if
they are missing.

This is the field-book direction: a point layer that has to go into a report, a
handover spreadsheet or a GPS device needs its coordinates as attributes, not
only as geometry. Lines and polygons get their centroid, which is what a
"location of this feature" column usually means.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input Layer** | Vector layer (any geometry) | — | The layer written to. It is edited in place; there is no output layer. |
| **Coordinate Format** | Enum | `X / Y (Layer CRS)` | `X / Y (Layer CRS)` writes `POINT_X` / `POINT_Y` in the layer's own CRS. `Longitude / Latitude (WGS 84 / EPSG:4326)` writes `LONGITUDE` / `LATITUDE`, transforming each coordinate. |

## How to use

1. Open **KGA Geometry Utilities > Update X/Y or Lat/Lon Fields**.
2. Pick the **Input Layer**.
3. Choose the **Coordinate Format**. Pick Lat/Lon when the numbers are going
   somewhere outside this project — a report, a GPS, a spreadsheet a colleague
   will open.
4. Press **Run**. A message box reports what happened.

## Outputs

- `POINT_X` and `POINT_Y` (double), or `LONGITUDE` and `LATITUDE` (double),
  created if missing and filled for every feature.
- The changes are **committed** to the layer.

## Notes and limits

- **Lines and polygons get their centroid**, which for a concave or ring-shaped
  polygon can fall outside the feature. If the point has to be inside, use
  **[Geometry Conversion](geometry_conversion_dynamic.md)** with *Point on
  Surface* instead.
- **The changes are committed immediately** — no dry run, no edit session left to
  undo from.
- **Existing values are overwritten.** The field names are fixed; the tool does
  not ask before reusing `POINT_X` or `LONGITUDE` if the layer already has one.
- Running it twice with different formats leaves **both** pairs of fields on the
  layer.
- Features with null or empty geometry are skipped.
- A coordinate that cannot be transformed is reported in the log and that feature
  is skipped, rather than stopping the run.
- The values are a snapshot. Move a feature afterwards and the fields are stale —
  run it again.
- The tool needs `iface` for its message boxes, so it cannot run headless.

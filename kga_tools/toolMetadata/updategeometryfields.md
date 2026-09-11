# Update Geometry Fields

|  |  |
|---|---|
| **Algorithm ID** | `kga:updategeometryfields` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/update_geometry_tool.py` |
| **Icon** | `icons/updateGeomFields.png` |
| **Type** | Batch algorithm with **no parameters** — acts on the active layer |

## Overview

Fills `Shape_Length` and `Shape_Area` on the layer currently highlighted in the
Layers panel, creating the fields if they are missing.

These are the fields ArcGIS maintains automatically. A GeoPackage does not, so
after any edit — a split, a merge, a planarize, a reshaped boundary — they are
stale. This is the one-click way to bring them back in line before data goes back
to an ArcGIS colleague.

## Parameters

**None.** The tool takes no parameters at all: it reads
`iface.activeLayer()` — whichever layer is highlighted in the Layers panel — and
runs straight away, without showing a Processing dialog.

What it computes follows from the geometry type:

| Layer type | `Shape_Length` | `Shape_Area` |
|---|---|---|
| Line | length | not created |
| Polygon | perimeter | area |
| Point | — refused — | — |

## How to use

1. **Click the layer in the Layers panel** so it is the active layer. This is the
   only way to choose the layer.
2. Open **KGA Geometry Utilities > Update Geometry Fields**. It runs immediately.
3. A message box reports success, or says why it refused.

## Outputs

- `Shape_Length` (double) created if missing and filled for every feature —
  length for lines, perimeter for polygons.
- `Shape_Area` (double) created if missing and filled, for polygon layers.
- The changes are **committed** to the layer.

## Notes and limits

- **It acts on the active layer, with no confirmation.** Check which layer is
  highlighted before running it.
- **The changes are committed immediately** — there is no dry run and no edit
  session left open to undo from.
- **Point layers are refused** with a message: there is nothing to measure. Use
  **[Update X/Y or Lat/Lon Fields](updatexyfields.md)** for those.
- **Measurements are ellipsoidal**, using the project CRS's ellipsoid, with the
  layer CRS as the source. The numbers therefore follow the *project*
  configuration, not only the layer — set the project ellipsoid deliberately.
- Features with null or empty geometry are skipped and left as they are.
- Existing `Shape_Length` / `Shape_Area` values are overwritten.
- The tool cannot run headless: it needs `iface` for both the active layer and
  its message boxes.

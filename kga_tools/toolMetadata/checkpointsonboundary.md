# Check Points on Boundary Vertices

|  |  |
|---|---|
| **Algorithm ID** | `kga:checkpointsonboundary` |
| **Group** | KGA Topology |
| **Source** | `kga_tools/algorithms/point_on_boundary_check.py` |
| **Icon** | `icons/pointOnBoundary.png` |
| **Type** | Batch algorithm |

## Overview

Flags points that do not sit on a vertex of a boundary layer.

This is the survey-reconciliation check: corner points from a field survey are
supposed to coincide with the vertices of the parcel boundary drawn from them. A
point that has drifted — because the boundary was edited afterwards, or because
the point was digitized separately — is an error you want to find before the plan
is issued.

Every point is tested against the vertices of the polygon or line boundary;
anything further away than the snapping tolerance is written to an error layer.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Boundary layer (Polygon or Line)** | Feature source (polygon or line) | — | The layer whose **vertices** define the valid positions. |
| **Point layer to check** | Feature source (point) | — | The points tested. |
| **Snapping tolerance (map units)** | Number | `0.001` | How far a point may sit from the nearest boundary vertex and still pass. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Errors found** | Number | Points further from a vertex than the tolerance. |

## How to use

1. Load the boundary layer and the point layer.
2. Open **KGA Topology > Check Points on Boundary Vertices**.
3. Set the **Snapping tolerance** to match the precision of the survey — for a
   metric CRS, `0.001` is one millimetre.
4. Press **Run**. A message box reports the outcome.
5. Review the results with **[Open Error Inspector](errorinspector.md)**, which
   zooms to each error and can re-run this check once the points are moved.

## Outputs

- A memory layer **Topology Errors - Points Not on Boundary**, with one point per
  failure. Fields: `error` (always `Point not on boundary`) and `point_fid`, the
  id of the offending point in its source layer.
- A message box reporting how many errors were found, or saying there were none.
- The **Errors found** count in the Processing results panel.

## Notes and limits

- **The tolerance is in the layer's map units.** A projected CRS means metres and
  a geographic CRS means degrees — `0.001` degrees is about 110 m, which is not
  a tolerance, it is a free pass. Check the CRS before trusting a clean result.
- **It tests against vertices, not against the boundary line.** A point sitting
  exactly on a boundary *segment*, halfway between two vertices, is reported as
  an error. That is the intended behaviour for survey corners, and the wrong
  behaviour if you meant "on the line".
- **The error layer is a memory layer**; it disappears when the project closes
  unless you save it. Re-running replaces the previous one rather than stacking
  another beside it.
- The check is one-directional: it finds points with no vertex, not vertices with
  no point.

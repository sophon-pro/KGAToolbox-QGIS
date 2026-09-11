# Check Overlaps and Gaps

|  |  |
|---|---|
| **Algorithm ID** | `kga:checkoverlapsandgaps` |
| **Group** | KGA Topology |
| **Source** | `kga_tools/algorithms/topology_checker.py` |
| **Icon** | `icons/alg_topology.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it adds error layers to the project) |

## Overview

Checks polygon layers for the two errors that matter in a parcel or land-use
dataset: polygons that **overlap** each other, and enclosed **gaps** left between
them.

Overlaps are reported for every pair of polygons whose intersection has an area —
within one layer and, when several layers are supplied, between them as well.
Gaps are the enclosed holes left once every input feature is dissolved together.

When errors are found, two memory layers are added at the top of the Layers panel
and symbolized so you can see them at a glance; **[Open Error Inspector](errorinspector.md)**
then walks you through them one by one.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input polygon layer(s)** | Multiple vector layers (polygon) | — | The layers to check. Non-polygon layers are skipped with a warning. The **first** layer's CRS is the one the check runs in; the rest are reprojected to it. |
| **Use selected features only (when a selection exists)** | Boolean | `False` | On, a layer with a selection contributes only those features; a layer with no selection still contributes all of them. |
| **Check overlaps** | Boolean | `True` | |
| **Check gaps** | Boolean | `True` | |
| **Ignore errors smaller than (square map units)** | Number | `0.000001` | The sliver threshold. Errors at or below it are dropped. |
| **Ignore gaps larger than (square map units, 0 = no limit)** | Number | `0.0` | Upper bound for gaps, so a genuine untouched area — a lake, a forest block — is not reported as an error. `0` means no limit. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Overlaps found** | Number | |
| **Gaps found** | Number | |

## How to use

1. Load the polygon layers. Put the one whose CRS the check should use **first**.
2. Open **KGA Topology > Check Overlaps and Gaps**.
3. Pick the layers, and set **Ignore errors smaller than** to the precision your
   survey actually has — this is what separates a real overlap from floating
   point noise.
4. If the dataset has legitimate untouched areas, set **Ignore gaps larger than**
   above the largest sliver you care about and below the smallest real void.
5. Press **Run**.
6. If errors are found, open **[Open Error Inspector](errorinspector.md)** to step
   through them.

## Outputs

When errors are found, two memory layers are added to the top of the Layers
panel:

- **Topology Errors - Overlaps** — one polygon per overlapping pair, fill
  `#FF8080` with a red outline. Fields: `error`, `layer_a`, `fid_a`, `layer_b`,
  `fid_b`, `area`.
- **Topology Errors - Gaps** — one polygon per enclosed gap, no fill with a
  `#FF8080` outline. Fields: `error`, `gap_id`, `area`.

When **no** errors are found, a message box says so and any error layers left
over from a previous run are removed from the Layers panel.

## Notes and limits

- **Gaps that open onto the outer edge of the dataset cannot be detected.** A gap
  is found as an interior ring of the dissolved geometry, so a void on the
  boundary of the study area is not enclosed and is invisible to this check.
- **Areas are in the square map units of the first input layer**, which also
  defines the CRS the check runs in. Both thresholds are in those units — square
  degrees if that layer is geographic, which is almost never what you want.
- **The error layers are memory layers.** They vanish when the project closes
  unless you save them.
- Invalid geometries are repaired before the check (`makeValid`, falling back to
  a zero buffer). Features that cannot be repaired are counted in a warning and
  left out.
- Every pair is compared, so the overlap check grows with the square of the
  feature count in dense areas. A large dataset takes time.

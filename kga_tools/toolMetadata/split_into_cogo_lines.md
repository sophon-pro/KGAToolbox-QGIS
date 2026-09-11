# Split into COGO Lines

|  |  |
|---|---|
| **Algorithm ID** | `kga:split_into_cogo_lines` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/split_into_cogo_lines.py` (base dialog in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/splitCogoLines.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Split into COGO Lines* pane. Take a
boundary — a parcel polygon, a right of way, a surveyed traverse — and break it
into one line feature per course, each carrying the four COGO values a deed is
written in: **Direction**, **Distance**, **Radius** and **ArcLength**.

The parts that decide whether the result matches the survey it came from:

- **Arcs stay arcs.** A File Geodatabase parcel boundary holds true circular
  arcs. Segmenting one into chords would lengthen the boundary and lose the curve
  entirely, so an arc comes through as a single COGO line whose radius and arc
  length describe it, with Direction and Distance giving the chord — which is
  what chord bearing and chord distance mean on a plat.
- **The radius is signed.** Positive turns clockwise from the start of the
  course, negative counter-clockwise. Reading a description back without the sign
  gives a mirror image of the curve.
- **Directions say which convention they are in.** A number with no convention
  beside it is not a direction.
- **Bearings are grid bearings**, measured in the projected CRS the lengths are
  measured in.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | The lines or polygons to split. |
| **Template** | Line layer combo | — | The line layer the COGO lines are created in. A polygon boundary has to go somewhere else; a line layer can split into itself. |
| **Direction type** | Combo box | `North azimuth` | North azimuth, south azimuth, polar or quadrant bearing. North azimuth is clockwise from north; polar is counter-clockwise from east. |
| **Direction units** | Combo box | `Degrees minutes seconds` | Decimal degrees, degrees-minutes-seconds, gradians or radians. DMS is stored **packed** in a numeric field — 45°30'15" is the number `45.3015` — and written out in full (`N45-30-15.00E`) in a text field. |
| **Distance units** | Combo box | `Meters` | The unit Distance, Radius and ArcLength are written in. |
| **Keep circular arcs as single courses** | Checkbox | **ticked** | On, an arc is one COGO line with a radius and an arc length. Off, it is broken into straight chords, which lengthens the boundary. |
| **Create the COGO fields if they are missing** | Checkbox | **ticked** | Adds Direction, Distance, Radius and ArcLength to the template layer. Existing fields are matched by name, ignoring case and underscores, and left alone. |
| **Copy the source attributes** | Checkbox | **ticked** | Carries the boundary's values onto every course. |
| **Delete the original feature** | Checkbox | **ticked** | On — the ArcGIS default — the boundary is replaced by its courses. Off, the original is kept alongside them. |
| **Split into COGO Lines** | Toggle button | up | Puts the tool in hand. |
| **Reset** | Button | — | Sets the running count back to zero. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Split into COGO Lines**. Close the Processing
   window once the pane appears.
2. Pick the source **Layer** and the **Template** line layer.
3. Set the direction convention — **Direction type**, **Direction units** — and
   the **Distance units**. Get these right before you start: a number with the
   wrong convention behind it is not recoverable by inspection.
4. Decide whether the original boundary is replaced or kept.
5. Press **Split into COGO Lines**, then hover a boundary to see the courses in
   cyan and click to write them. Drag across several to do them all.

## Outputs

- One line feature per course in the template layer, with **Direction**,
  **Distance**, **Radius** and **ArcLength** filled in.
- The four COGO fields created on the template layer if they were missing.
- The original boundary deleted, unless *Delete the original feature* is
  unticked.
- Source attributes on every course when the box is ticked.

## Notes and limits

- **A radius sign is meaningful.** Positive is clockwise from the start of the
  course. A description read back without it describes the mirror image.
- **A text Direction field gets the full form** (`N45-30-15.00E`); a numeric one
  gets the packed number (`45.3015`). Choose the field type deliberately.
- **Bearings are grid bearings**, measured in the same projected CRS as the
  lengths — a layer stored in degrees is taken through its UTM zone rather than
  having angles read off a plate carrée.
- One press is one undo step per layer. The pane stays open while you work, and
  it owns the map tool.
- The tool cannot run headless or inside a model.

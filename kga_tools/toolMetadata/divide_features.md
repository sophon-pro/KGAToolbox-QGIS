# Divide

|  |  |
|---|---|
| **Algorithm ID** | `kga:divide_features` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/divide_features.py` (base dialog and `AngleMapTool` in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/divideFeatures.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Divide* pane. Cut features into
parts: a parcel into four equal shares, a long block into one-hectare plots, a
canal into 500 m maintenance reaches.

The pane reads differently for a line and for a polygon, exactly as Pro's does,
because dividing a line is about length and dividing a polygon is about area.

- **Lines** divide into equal parts, into parts of a specified length, or into
  parts of a percentage of the total. A multipart line is measured end to end as
  one run, so a cut can fall in any of its parts.
- **Polygons** divide into parallel strips of the asked-for area, along a
  direction you set. The cut positions are found by bisection rather than by
  formula: a real parcel has notches and holes, and no formula says where the
  line that leaves exactly one hectare behind it falls.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's; the visible set changes with the geometry type
and the chosen method.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | The features to divide. |
| **Divide into** | Combo box | `Equal parts` / `Equal areas` | Lines: `Equal parts`, `Specified length`, `Percentage of length`. Polygons: `Equal areas`, `Specified area`, `Percentage of area`. |
| **Number of parts** | Spin box (2–1000) | `2` | Shown for the equal-parts method. |
| **Size of each part** | Number + unit combo | `1.0`, hectares (polygon) / metres (line) | Shown for the specified-size method. One row that means length on a line and area on a polygon; only the matching unit combo is on screen. |
| **Percentage** | Number (0.0001–100) | `25.0 %` | Shown for the percentage method. |
| **Leftover** | Combo box | `Leave it as a final part` | `Leave it as a final part`, `Leave it as a first part`, `Spread it across the parts`. Spreading makes every part slightly larger than asked for, but equal. |
| **Division angle** | Number (−360…360 deg) + **Edge** + **Draw** | `0 deg` | The direction the cuts run in, counter-clockwise from east — 0 cuts along horizontal lines, 90 along vertical. **Edge** takes the angle off a boundary you click on the map; **Draw** takes it off a two-click line with snapping. Both only fill in this box, so a picked angle can still be nudged by hand. Polygons only. |
| *Angle note* | Label | `Typed.` | Says whether the current angle was typed, picked off an edge, or drawn. |
| **Copy the source attributes to every part** | Checkbox | **ticked** | Every part carries the original's values. |
| **Divide** | Toggle button | up | Puts the tool in hand. |
| **Reset** | Button | — | Sets the running count back to zero. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Divide**. Close the Processing window once the pane
   appears.
2. Pick the **Layer**. The pane rearranges itself for lines or for polygons.
3. Choose **Divide into** and fill in the number, size or percentage it asks for.
4. For a polygon, set the **Division angle**. Almost nobody knows a parcel's
   angle as a number, so press **Edge** and click any boundary — this parcel's,
   the road it fronts, the neighbour it abuts — or press **Draw** and click the
   two ends of a line. **Esc** goes back.
5. Decide where the **Leftover** goes. Twelve hectares divided into five-hectare
   plots is two plots and two hectares over; whether that is a small plot at the
   end, a small plot at the start, or spread so all three come out at four
   hectares is the difference between a legal subdivision and a redraft.
6. Press **Divide**, then hover a feature to see the cuts in cyan and click to
   apply them.

## Outputs

- The feature replaced by its parts, in the same layer.
- **The first part keeps the original feature's identity** and the rest are added
  beside it, so joins and relates still resolve.
- The parts end up selected, ready for the next tool.
- Source attributes on every part when the box is ticked.

## Notes and limits

- **The angle is read in the CRS the cut is made in, not the one on screen.** For
  a layer stored in degrees, drawn in Web Mercator and divided in hectares, those
  are three different things — which is why a picked edge is re-read when you
  switch between a real area unit and map units.
- **Cut positions are searched for, not calculated.** Notches and holes are
  handled correctly because the area behind a sweeping line only ever grows, so
  bisection always converges.
- One press is one undo step. The pane stays open while you work, and it owns the
  map tool.
- The tool cannot run headless or inside a model.

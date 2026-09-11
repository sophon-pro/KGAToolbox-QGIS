# Construct Polygon

|  |  |
|---|---|
| **Algorithm ID** | `kga:construct_polygon` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/construct_polygon.py` (base dialog in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/constructPolygon.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Construct Polygons* pane. Take the
lines that bound something — the courses of a parcel, the banks and end walls of
a reservoir, the centrelines of a block of streets — and turn the area they
enclose into a polygon feature.

**The lines have to close.** That is the whole condition, and the one thing this
tool refuses on: courses that stop short of one another enclose nothing, and
rather than writing a polygon that is not the boundary anybody drew, the tool
names the loose ends and flashes them on the map.

What it offers beyond Pro's pane is the running verdict above the buttons: how
many lines are in hand, whether they currently close, and how many polygons they
would make. Whether a boundary closes is otherwise a question you only get
answered by pressing the button.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | The line layer the courses are read from. |
| **Template** | Polygon layer combo | — | The polygon layer the constructed polygons are created in. ArcGIS calls this the template. |
| **Tolerance** | Number + unit combo | `0.0` | How far apart two line ends may be and still count as meeting. Zero demands they touch exactly, which is what a boundary built by snapping does. Raise it to close the hairline gaps a survey import leaves — and no further, or corners that are genuinely apart get pulled together. |
| **Combine the polygons into one feature** | Checkbox | unticked | Only means anything when the lines enclose more than one area — a block of parcels sharing walls. Ticked they become one feature; unticked each enclosed area is its own feature, which is what ArcGIS does. |
| **Copy the attributes of the first line** | Checkbox | unticked | Off by default, as in ArcGIS: a boundary is made of several courses and no one of them owns the parcel, so the polygon takes the template layer's own defaults. Tick it to carry the first clicked line's values across by field name. |
| *Verdict line* | Bold label | — | Live: how many lines are in hand, whether they close, how many polygons they would make. Updates as you click **and as you change the Tolerance**, which is how you find the tolerance a boundary actually needs. |
| **Construct** | Toggle button | up | Puts the tool in hand. This tool **collects**: each click adds a line to the set in hand. |
| **Reset** | Button | — | Sets the running count back to zero. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Construct Polygon**. Close the Processing window
   once the pane appears.
2. Pick the source line **Layer** and the **Template** polygon layer.
3. Press **Construct**. Lines already selected in the layer are taken in hand
   straight away.
4. Click each line of the boundary — or drag a path across them all in one
   gesture. Hovering a line draws in cyan what it would close together with the
   lines already in hand.
5. Watch the verdict line. When it says the lines close, press **Enter**.
6. If it refuses, read where the loose ends are, raise the **Tolerance** until
   the verdict turns, and press Enter again. **Esc** starts over.

## Outputs

- One polygon per enclosed area in the template layer — or a single combined
  feature when *Combine the polygons into one feature* is ticked.
- Attributes from the template layer's defaults, unless *Copy the attributes of
  the first line* is ticked.
- **The lines themselves are not changed.**

## Notes and limits

- **It refuses rather than guessing.** Loose ends are named and flashed on the
  map, and the lines stay in hand so the gap can be fixed and Enter pressed
  again.
- **Tolerance is measured in the matching UTM zone** on a layer stored in
  degrees, so metres mean metres.
- **Lines that cross are noded first**, so a boundary enclosing several areas
  gives one polygon per area.
- **Circular arcs come back segmented.** The construction is GEOS work and GEOS
  has no arcs. If the arcs matter, this is the wrong tool.
- One press is one undo step. The pane stays open while you work, and owns the
  map tool.
- The tool cannot run headless or inside a model.

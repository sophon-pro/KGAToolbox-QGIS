# Buffer

|  |  |
|---|---|
| **Algorithm ID** | `kga:buffer_features` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/buffer_features.py` (base dialog in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/bufferFeature.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Buffer* pane. Hover a feature of any
geometry type, see its buffer drawn in cyan, and click to write it into a polygon
layer of your choosing.

A well becomes its protection zone; a canal becomes its right of way; a village
point becomes its service area.

Pro's pane is short — a template, a distance and a unit — and so is the top of
this one. The end-cap and corner controls below it are QGIS's own, and they are
on the pane rather than buried because a right of way that overshoots its channel
by the buffer radius is a real error, not a cosmetic one.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | The source features are read from here. Any vector layer. |
| **Template** | Polygon layer combo | the source layer when it is a polygon layer | The polygon layer the buffers are created in. ArcGIS calls this the template. There is no fall-back to the source: buffers are polygons and the source may be lines or points. |
| **Distance** | Number + unit combo | `10.0` | How far out the buffer reaches. Must be greater than zero. |
| **Side** | Combo box | full buffer | One-sided buffers only mean anything for a line; a point or a polygon is always buffered all round. |
| **End caps** | Combo box | `Round` | How a buffered line ends. Round overshoots the last vertex by the buffer distance; flat stops on it. |
| **Corners** | Combo box | `Round` | How the buffer turns a corner. |
| **Segments per quarter** | Spin box (1–64) | `8` | Straight segments used to draw a quarter circle. Higher is smoother and heavier. |
| **Combine the buffers into one feature** | Checkbox | unticked | Only means anything when a drag catches several features at once: their buffers are merged, so a chain gives one corridor rather than a stack of overlapping polygons. |
| **Copy the source attributes** | Checkbox | **ticked** | Carries the source values across by field name. |
| **Buffer** | Toggle button | up | Puts the tool in hand. It stays down while the map is in Buffer mode. |
| **Reset** | Button | — | Sets the running count back to zero, and stops what has been written so far being off limits — so a buffer can itself be buffered. |
| **Close** | Button | — | Closes the pane and gives the canvas back to the previous tool. |

## How to use

1. Open **KGA Editing Tools > Buffer**. Close the Processing window once the pane
   appears.
2. Pick the source **Layer** and the **Template** polygon layer.
3. Set the **Distance** and its unit.
4. Press **Buffer** — the button stays down.
5. Hover a feature: its buffer is drawn in cyan. Click to write it.
6. Or press, drag a path across several features, and release to buffer them all
   in one go.
7. Press **Buffer** again, or **Close**, when you are done.

## Outputs

- Buffer polygons written into the template layer, one per source feature (or one
  merged corridor when *Combine* is ticked and a drag caught several).
- Source attributes carried across by field name when *Copy the source
  attributes* is on.
- A running count and a status line in the pane after each press.

## Notes and limits

- **Distances are honest on a layer stored in degrees.** The buffer is computed
  in the matching UTM zone and brought back, so a 30 m setback is 30 m at the
  equator and 30 m in the north.
- **The tool skips its own output.** A buffer contains the feature it came from,
  so without this the next hover would buffer that buffer, and the click after it
  would buffer *that* — one ring wider every time. **Reset** clears the exclusion
  when you genuinely want to buffer a result.
- **One press is one undo step.** A drag across several features is still one
  step.
- A combined buffer whose parts do not all overlap has to be split when the
  target is a single-part layer; the status line says so rather than leaving the
  tick box looking like it did nothing.
- The pane stays open while you work, and it owns the map tool — the QGIS select
  tool is not involved.
- The tool cannot run headless or inside a model.

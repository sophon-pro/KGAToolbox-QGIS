# Copy Parallel

|  |  |
|---|---|
| **Algorithm ID** | `kga:copy_parallel` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/copy_parallel.py` (base dialog in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/copyParallel.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Copy Parallel* pane. Set an offset
distance, press **Copy Parallel**, then hover a line to see its parallel copies
in cyan and click to write them.

A canal centre line becomes its two banks; a road centre line becomes its edge of
pavement; a boundary becomes its setback.

What this gets right that a plain `offsetCurve` does not:

- **Left and right mean what they mean in Pro** — relative to the direction the
  line was digitized in. GEOS hands a left-hand offset back walking the other
  way, which without correction would reverse the COGO description of half the
  copies in a run.
- **A distance is a real distance.** On a layer stored in degrees the offset is
  computed in the right UTM zone and brought back, so "5 meters" is five metres
  and not five degrees.
- **Copies can land in another layer.** Pro calls that choosing a template.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | The line layer the sources are read from. |
| **Template** | Line layer combo | the source layer | Where the copies are created — the source layer by default, which is what Copy Parallel does in ArcGIS. Sending them elsewhere matches the attributes by field name. |
| **Distance** | Number + unit combo | `10.0` | The offset. |
| **Side** | Combo box | both sides | Left, right or both — relative to the direction each line was digitized in. |
| **Corners** | Combo box | `Miter` | How the copy turns a corner. Mitered keeps it sharp, beveled cuts it off, rounded arcs it. |
| **Number of copies** | Spin box (1–100) | `1` | More than one steps outwards: the second copy sits at twice the distance, the third at three times it. |
| **Copy the source attributes** | Checkbox | **ticked** | Carries the source values across by field name. |
| **Copy Parallel** | Toggle button | up | Puts the tool in hand. |
| **Reset** | Button | — | Sets the running count back to zero. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Copy Parallel**. Close the Processing window once
   the pane appears.
2. Pick the source **Layer**, and a **Template** layer if the copies belong
   somewhere else.
3. Set the **Distance**, the **Side** and the **Corners**.
4. Press **Copy Parallel**.
5. Hover a line — its copies are drawn in cyan. Click to write them.
6. Or press, drag a path across several lines, and release to copy them all.

## Outputs

- One copy per side per step, written into the template layer.
- Source attributes carried across by field name when the box is ticked;
  matched by name when the template is a different layer.
- A running count and a status line after each press.

## Notes and limits

- **Left and right follow the digitizing direction**, not the screen. A line
  drawn the other way offsets the other way — which is correct, and is what
  ArcGIS does.
- **Metres mean metres** on a degrees-based layer: the offset is computed in the
  matching UTM zone.
- **Each click or drag is one undo step.**
- The pane stays open while you work, and it owns the map tool.
- The tool cannot run headless or inside a model.

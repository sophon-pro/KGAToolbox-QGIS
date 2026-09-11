# Sequential Numbering

|  |  |
|---|---|
| **Algorithm ID** | `kga:sequential_numbering` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/sequential_numbering_dialog.py` |
| **Icon** | `icons/seqNumbering.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Sequential Numbering* pane. Pick a
layer, a field, a start value and an increment, then click features on the map to
number them.

This is the tool for putting manhole numbers on a sewer run, chainage on a canal,
or plot numbers around a block — in the order you walk them, not the order the
table happens to be in. Dragging a path across several features numbers them in
the order the path crosses them, and the dashed rubber band mirrors Pro's line.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | Any vector layer. |
| **Field** | Field combo | first suitable field | The field the value is written to. Text and numeric fields only. |
| **Format** | Label | `#` | A live preview of what the next value will look like, including prefix, suffix and padding. |
| **Start value** | Number (−1e9…1e9) | `1` | Where the sequence begins. |
| **Increment** | Number (−1e6…1e6) | `1` | Step between values. Negative counts down. |
| **Next value** | Number (−1e9…1e9) | `1` | The value the next click will write. Editable, so you can jump the sequence mid-run. |
| **Text field options › Prefix** | Text | empty | Text fields only. |
| **Text field options › Suffix** | Text | empty | Text fields only. |
| **Text field options › Pad to digits** | Spin box (0–20) | `0` | Zero-pads the number, so `MH-`, `7`, pad 3 gives `MH-007`. |
| **Skip features that already have a value** | Checkbox | unticked | On, renumbers only the gaps and leaves existing values alone. |
| **Sequential Numbering** | Toggle button | up | Puts the tool in hand. Stays down while the map is in this mode. |
| **Reset** | Button | — | Sets **Next value** back to **Start value**. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Sequential Numbering**. Close the Processing window
   once the pane appears.
2. Pick the **Layer** and the **Field**.
3. Set the **Start value** and **Increment**. For a text field, add a **Prefix**,
   **Suffix** and **Pad to digits** — the *Format* line shows the result.
4. Tick **Skip features that already have a value** if you are filling gaps.
5. Press **Sequential Numbering**.
6. Click a feature to give it the next value, or press, drag a path across
   several, and release to number them in the order the path crosses them.
7. Watch **Next value** — it advances as you go, and you can type over it to jump.

## Outputs

- The chosen field written on each clicked feature, in sequence.
- Nothing else is changed; geometry is untouched.
- A status line reporting what each stroke numbered.

## Notes and limits

- **The layer has to be in edit mode.** The tool offers to start editing if it is
  not.
- **Each click or drag is one undo step**, so Ctrl+Z takes back a whole stroke —
  not one feature of it.
- **Text field options are only used for a text field.** Writing to a numeric
  field ignores prefix, suffix and padding.
- The pane stays open while you work and owns its map tool; this is the tool the
  other Editing Tools were modelled on.
- The tool cannot run headless or inside a model.

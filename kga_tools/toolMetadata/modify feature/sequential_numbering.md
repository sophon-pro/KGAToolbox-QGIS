# Sequential Numbering

|  |  |
|---|---|
| **Category** | Modify Features > Attributes |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/numbering.py` (NumberingTool) |
| **Icon** | `kga_tools/icons/editing/seqNumbering.png` |
| **Also as a window** | [sequential_numbering](../sequential_numbering.md) (`kga:sequential_numbering`) |

## Overview

Numbers features in the order a drawn line crosses them, laid out like the Sequential Numbering pane of ArcGIS Pro.

## How to use

1. Choose the **Layer** (editable) and the **Field** (number or text).
2. Set the **Format**, the **Start value** and the **Increment**.
3. Draw a line across the features: click its points, then right-click, double-click or Enter. A single click numbers the one feature under it.

## Options

| Option | Meaning |
|---|---|
| **Format** | For a text field: the text around the number, with # standing for it. "A-###" gives A-001, A-002 ... (as many # as digits; a lone # means no padding). Greyed out for a number field. |
| **Start value** | The first number. It moves on to the next number after each run, so the next line carries on from there. |
| **Increment** | The step; a negative value counts down. |

## Notes and limits

* Values longer than a text field allows are cut to its length.
* One undoable edit per run.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

# Planarize Lines

|  |  |
|---|---|
| **Algorithm ID** | `kga:planarizelines` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/planarize_line.py` |
| **Icon** | `icons/planarizeTool.png` |
| **Type** | Batch algorithm (runs on the GUI thread — the in-place branch writes into a project layer) |

## Overview

Splits lines where they intersect, producing planarized single-part lines with no
cross-overs. Attributes from the original lines are retained on every segment.

This is the step a network needs before it is a network: a canal layer digitized
as long runs that cross each other has no node where they meet, so routing,
topology checks and length-per-reach all give the wrong answer. Planarizing puts
a break at every crossing.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input Line Layer** | Feature source (line) | — | The lines to planarize. |
| **Planarize the input layer in place (no output layer)** | Boolean | `False` | Off, a new layer is written and the input is untouched. On, the input layer's features are replaced by the planarized ones and no output layer is produced. |
| **Planarized Output** | Feature sink (line), optional | temporary layer | Where the result goes. Ignored when the in-place box is ticked. |

## How to use

1. Open **KGA Geometry Utilities > Planarize Lines**.
2. Pick the **Input Line Layer**.
3. Leave the in-place box off for the first run — check the result, then decide.
4. Press **Run**.
5. To rewrite the source instead, load it in the project, tick **Planarize the
   input layer in place**, and run again.

## Outputs

- A line layer of single-part, non-crossing segments, each carrying the
  attributes of the line it came from.
- In place: the input layer's features replaced by those segments, and **no**
  output layer.

## Notes and limits

- **Feature ids are not preserved**, in either mode. One input line becomes
  several output segments, so there is no id to keep. Joins and relates keyed on
  the old ids will not survive.
- **In place needs a real project layer** whose provider can add and delete
  features — not a file path typed into the parameter, and not a read-only
  source.
- **In place respects an edit session.** If the layer is already in edit mode the
  change is left in the edit buffer so you can undo it; otherwise it is
  committed.
- The original attributes are duplicated onto every segment. A length field
  carried across this way is now wrong for each piece — recompute it with
  **[Update Geometry Fields](updategeometryfields.md)**.

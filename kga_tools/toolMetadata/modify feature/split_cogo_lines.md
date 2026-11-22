# Split Into COGO Lines

|  |  |
|---|---|
| **Category** | Modify Features > COGO |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/split_cogo.py` (SplitCogoTool) |
| **Icon** | `kga_tools/icons/editing/splitCogoLines.png` |
| **Also as a window** | [split_into_cogo_lines](../split_into_cogo_lines.md) (`kga:split_into_cogo_lines`) |

## Overview

Splits lines, or polygon boundaries, at their vertices into COGO lines, laid out like the Split Into COGO Lines pane of ArcGIS Pro.

## How to use

1. Select lines or polygons. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the **Template** if the new lines should go into a line layer (a polygon boundary needs one).
3. Press **Split**.

## Options

| Option | Meaning |
|---|---|
| **Use Template** | On: the new lines go into the template layer (an editable line layer). Off: lines are split in their own layer. |
| **Split lines with existing COGO attributes** | Lines that already have a Direction and a Distance are left alone unless this is ticked. |

## Notes and limits

* Each straight run and each arc becomes a line of its own carrying its Direction, Distance, Radius and ArcLength. The direction convention and units are the shared COGO settings.
* A line that was split is replaced by its courses; a polygon is kept. New lines copy the attributes of the feature they came from.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

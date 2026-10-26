# Move To

|  |  |
|---|---|
| **Category** | Modify Features > Alignment |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/transform.py` (MoveToTool) |
| **Icon** | `kga_tools/editor/modify/icons/move_to.svg` |

## Overview

Moves the selection to a location, laid out like the Move To pane of ArcGIS Pro. A map click only fills in the values; the **Move To** button applies them.

## How to use

1. Select the features. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose a **Method** and type its values, or click the map to fill them in. A preview shows the result.
3. Press **Move To** (it stays disabled until there is a non-zero move).

## Options

| Option | Meaning |
|---|---|
| **Absolute** | Puts the anchor at **X, Y**. The anchor is the centre of the selection unless you set it. |
| **Delta** | Moves by **dX, dY**. |
| **Direction/Distance** | Moves a **Distance** along a **Direction** (degrees clockwise from north). Units show as the CRS abbreviation. |

## Notes and limits

* The default X, Y are refilled when the selection or anchor changes.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

# Divide

|  |  |
|---|---|
| **Category** | Modify Features > Divide |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/divide.py` (DivideTool) |
| **Icon** | `kga_tools/icons/divideFeatures.png` |
| **Also as a window** | [divide_features](../divide_features.md) (`kga:divide_features`) |

## Overview

Divides polygons into parts by area, proportion or width, or lines by distance, equal parts or percentage. The page changes with what is selected.

## How to use

1. Select polygons or lines. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose a **Method**, then the **Direction** (a polygon edge or a drawn line; **Switch** picks the side to start from).
3. Enter the values, check the information bar (it shows the areas), tick **Show Preview**, then press **Divide**.

## Options

| Option | Meaning |
|---|---|
| **Proportional Area** | N parts that each hold 1/N of the polygon. |
| **Equal Area** | Parts of a given area from the start side; the rest stays as the remaining polygon, or is shared out equally between the parts. |
| **Equal Width** | Parts of a given width from the start side; the rest stays, or is merged into the last part. |
| **Lines: Distance** | Cuts the line at the distances in a table, with repetitions; the rest can be shared out. |
| **Lines: Equal Parts / Percentage** | Number of parts, or a percentage, from the start or the end of the line. |

## Notes and limits

* The dividing lines run parallel to the direction. The first part keeps the original feature; the others are new features with the same attributes.
* Areas and lengths are measured on the ground when the project has an ellipsoid, else flat.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

# Planarize

|  |  |
|---|---|
| **Category** | Modify Features > Divide |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/planarize.py` (PlanarizeTool) |
| **Icon** | `kga_tools/icons/editing/planarizeTool.png` |
| **Also as a window** | [planarizelines](../planarizelines.md) (`kga:planarizelines`) |

## Overview

Cuts the selected lines where they cross each other, laid out like the Planarize pane of ArcGIS Pro.

## How to use

1. Select the lines. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Set the **Cluster Tolerance** in the project's distance unit.
3. Press **Planarize**.

## Options

| Option | Meaning |
|---|---|
| **Cluster Tolerance** | Lines cross or end on another selected line within this distance are cut there. |

## Notes and limits

* Every selected line is cut where it crosses another selected line, or where another selected line ends on it, so no two lines cross without a vertex.
* The first piece keeps the original feature; the other pieces are new features with the same attributes (primary key and unique fields get fresh values).
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

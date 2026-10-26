# Update COGO

|  |  |
|---|---|
| **Category** | Modify Features > COGO |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/update_cogo.py` (UpdateCogoTool) |
| **Icon** | `kga_tools/editor/modify/icons/update_cogo.svg` |

## Overview

Rewrites the COGO fields of the selected lines from their geometry, laid out like the Update COGO pane of ArcGIS Pro.

## How to use

1. Select the lines. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the COGO settings: **Direction type**, **Direction units** and **Distance units**.
3. Press **Update**.

## Options

| Option | Meaning |
|---|---|
| **Create the COGO fields if they are missing** | Adds Direction, Distance, Radius and ArcLength when the layer lacks them. |

## Notes and limits

* A single course gives its own direction and distance (an arc also its radius and arc length). A line with several vertices gets the chord from its first to its last point and its full length.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

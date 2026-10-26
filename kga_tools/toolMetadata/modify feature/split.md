# Split

|  |  |
|---|---|
| **Category** | Modify Features > Divide |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/split.py` (SplitTool) |
| **Icon** | `kga_tools/editor/modify/icons/split.svg` |

## Overview

Splits features with a drawn line or with other features, laid out like the Split pane of ArcGIS Pro.

## How to use

1. **Interactive:** select features, then click the points of a line across them. Right-click, double-click or Enter splits; Backspace removes the last point; Esc cancels.
2. **By Feature:** pick **Input Features** (lines, or polygons whose outline cuts) and **Target Features** (what gets split) with their **Select** buttons, then press **Split**.

## Options

| Option | Meaning |
|---|---|
| **Allow splitting without a selection** | With nothing selected, splits every editable feature the line crosses. |

## Notes and limits

* Pieces keep the attributes of the feature they came from (QGIS's own split).
* One undoable edit per layer. Ctrl+Z and Ctrl+Y undo and redo the splits in every layer they touched; the split layer becomes the active layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

# Clip

|  |  |
|---|---|
| **Category** | Modify Features > Divide |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/clip.py` (ClipTool) |
| **Icon** | `kga_tools/icons/editing/clipFeatures.png` |
| **Also as a window** | [clip_features](../clip_features.md) (`kga:clip_features`) |

## Overview

Clips lines and polygons with other features, laid out like the Clip pane of ArcGIS Pro.

## How to use

1. Pick the **Input Features** (the clip features: points, lines or polygons) and the **Target Features** (lines or polygons that get clipped) with their Select buttons.
2. Set a **Buffer Distance** and choose **When clipping features**.
3. Press **Clip**.

## Options

| Option | Meaning |
|---|---|
| **Buffer Distance** | Points and lines clip through the buffer around them, so they need a distance above 0; polygons may be grown by it too. |
| **Discard (Remainder)** | Removes the part of each target inside the clip area. **Keep coincident** keeps a line's part that runs along the clip boundary. |
| **Preserve** | Keeps only the part inside the clip area. |
| **Contain** | Deletes the targets outside the clip area; the others stay as they are. |
| **Split** | Keeps both: the part inside and the part outside become separate features. |
| **Clip all editable features** | Clips every editable line and polygon the clip area touches instead of the Target Features. |

## Notes and limits

* New pieces copy the attributes. One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

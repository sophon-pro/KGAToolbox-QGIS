# Buffer

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/buffer.py` (BufferTool) |
| **Icon** | `kga_tools/icons/editing/bufferFeature.png` |
| **Also as a window** | [buffer_features](../buffer_features.md) (`kga:buffer_features`) |

## Overview

Buffers the selected features into polygons or outlines, laid out like the Buffer pane of ArcGIS Pro.

## How to use

1. Select the features. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the **Template**: the layer that receives the buffers. A polygon layer gets polygons; a line layer gets the outlines.
3. Set the **Buffer Distance**, **Rings** and **Dissolve**, tick **Show Preview**, then press **Buffer**.

## Options

| Option | Meaning |
|---|---|
| **Buffer Distance** | How far out from each selected feature (points, lines and polygons), in a unit of your choice. |
| **Rings** | Several buffers, each one Buffer Distance further out. A polygon template gets them as separate bands (ring 2 is the area between the first and second buffer). |
| **Dissolve** | The buffers of all selected features become one shape. Off: one shape per feature. |

## Notes and limits

* The new features take the template layer's default attribute values and are selected afterwards.
* One undoable edit.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

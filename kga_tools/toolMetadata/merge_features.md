# Merge

|  |  |
|---|---|
| **Algorithm ID** | `kga:merge_features` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/merge_features.py` (base dialog in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/mergeFeatures.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Merge* pane. Click two or more
features of one layer and combine them into a single feature. Parts that touch
dissolve into one; parts that do not stay on as a multipart feature.

The part that matters, and that a plain dissolve leaves out, is the **attribute
side**. Merging three parcels means deciding whose parcel number the survivor
keeps, whose owner name, whose land use code. Pro shows the selected features,
lets you pick one to preserve the attributes from, and then lets you override any
single field from any of the others. So does this.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | The features must all come from one layer. |
| **Preserve the attributes of** | Single-select list | the first feature clicked | The features clicked so far. The highlighted entry is marked **(preserved)**: the merged feature keeps that feature's identity and starts from its values. Clicking an entry flashes it on the map, so you know which parcel you are looking at. |
| Field table | Table (field, value) | the preserved feature's values | Every field, with each distinct value any of the clicked features holds. Pick a different one and only that field changes. |
| **Merge** | Toggle button | up | Puts the tool in hand. This tool **collects**: it needs at least two features, so each click adds to the set in hand and Enter commits. |
| **Reset** | Button | — | Sets the running count back to zero. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Merge**. Close the Processing window once the pane
   appears.
2. Pick the **Layer**.
3. Press **Merge**, then click each feature to combine — or drag a path across
   them all in one gesture.
4. In the list, highlight the feature whose identity and starting values the
   survivor should keep. Click entries to flash them on the map if you are not
   sure which is which.
5. Override any individual field from the table below.
6. Press **Enter** to commit. **Esc** starts over.

## Outputs

- One feature in place of the clicked ones.
- **The survivor keeps the preserved feature's id**, not a new one, so joins,
  relates and anything keyed on that id still point at something afterwards.
- Attributes from the preserved feature, with the per-field overrides you chose.

## Notes and limits

- **At least two features are needed** — the tool collects until Enter.
- **All the features must be in one layer.** This is a merge, not a union across
  layers.
- Features that do not touch produce a **multipart** feature, not an error. That
  is what `unaryUnion` does and what Pro does.
- One press is one undo step. The pane stays open while you work, and it owns the
  map tool.
- The tool cannot run headless or inside a model.

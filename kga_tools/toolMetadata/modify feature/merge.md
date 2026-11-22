# Merge

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/merge.py` (MergeTool) |
| **Icon** | `kga_tools/icons/editing/mergeFeatures.png` |
| **Also as a window** | [merge_features](../merge_features.md) (`kga:merge_features`) |

## Overview

Merges features into one, laid out like the Merge pane of ArcGIS Pro.

## How to use

1. Select the features. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the tab: **Existing Feature** or **New Feature**.
3. Check the **Merged Feature Attributes** (type a value to change it), then press **Merge**.

## Options

| Option | Meaning |
|---|---|
| **Existing Feature** | The features become the preserved one: it gets the combined shape and the attribute values shown, and the others are deleted. Click a row to preserve another. |
| **New Feature** | A new feature with the combined shape and those values is added (to the template layer if **Use Template** is on). |
| **Keep original features** | New Feature tab: leaves the originals in place. |
| **Features to merge** | One row per feature, with buttons to flash it on the map or unselect it. |

## Notes and limits

* The shape is the union: touching parts dissolve, others stay as parts of a multi-part feature.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

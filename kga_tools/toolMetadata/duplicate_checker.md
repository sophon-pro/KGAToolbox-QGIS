# Duplicate Checker

|  |  |
|---|---|
| **Algorithm ID** | `kga:duplicate_checker` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/duplicate_checker.py` |
| **Icon** | `icons/checkDuplicate.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

Finds features that repeat — either the same value in a chosen field, or the same
geometry — and shows you which ones they are, by selecting them or by colouring
them on the map.

The usual use: a parcel number that was issued twice, or a point that was
digitized twice on top of itself after a bad import.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Input Layer** | Layer combo | the active layer | Any vector layer. |
| **Mode** | Combo box | `Check Duplicate Field Value` | `Check Duplicate Field Value` compares one field; `Check Duplicate Geometry` compares the geometries themselves. |
| **Field** | Field combo | first field | The field compared. Enabled only in field mode. |
| **Highlight Action** | Combo box | `Select Duplicate Features` | `Select Duplicate Features` selects them; `Symbolize (Categorize)` recolours the whole layer. |
| **Detect and Highlight Duplicates** | Button | — | Runs the check and reports the count in a message box. |

## How to use

1. Open **KGA Geometry Utilities > Duplicate Checker**. Close the Processing
   window once the pane appears.
2. Pick the **Input Layer**.
3. Choose the **Mode**: compare one field's values, or compare the geometries.
4. In field mode, pick the **Field** to compare.
5. Leave **Highlight Action** on `Select Duplicate Features` unless you
   deliberately want the layer restyled â€” the symbolize option writes a field
   and replaces your symbology.
6. Press **Detect and Highlight Duplicates**. A message box gives the count.
7. Open the attribute table filtered to the selection to see which records
   collide.

## Outputs

Depends on the **Highlight Action**:

- **Select Duplicate Features** — the duplicates end up as the layer's selection.
  Nothing is written; clear the selection and the run leaves no trace.
- **Symbolize (Categorize)** — the layer gets a text field `is_dup_temp` holding
  `Duplicate` or `Unique` for every feature, and a categorized renderer showing
  duplicates in **red** and the rest in **grey**. This is a real, committed edit.

A message box reports how many duplicates were found, or says none were.

## Notes and limits

- **`Symbolize (Categorize)` writes to the layer.** It adds the `is_dup_temp`
  field, fills it for **every** feature, commits the change, and replaces the
  layer's renderer. The existing symbology is lost. Use *Select* instead if you
  only want to look — and if you do symbolize, remember to delete `is_dup_temp`
  and restore your styling afterwards.
- **All features of a duplicate group are flagged**, including the first one —
  the tool tells you which records collide, not which to delete.
- **Geometry mode compares WKT exactly.** Two geometries that differ in vertex
  order, precision or ring direction are not reported as duplicates even when
  they draw identically.
- Null values are grouped together: every feature with an empty field counts as
  a duplicate of every other one. The same applies to null geometries.
- The whole layer is read, regardless of any selection or filter.
- **The window stays open.** Close the Processing dialog behind it and keep
  working.
- The tool cannot run headless or inside a model.

# Quick Symbology

|  |  |
|---|---|
| **Algorithm ID** | none: a button on the KGA Layer toolbar (not a Processing algorithm) |
| **Group** | KGA Layer toolbar, right after Quick Labeling (it was on the KGA Editor toolbar before 1.1.0) |
| **Source** | `kga_tools/editor/quicksymbol/` (`controller.py` button, `dialog.py` the window and its three pages, `widgets.py` switch and preview, `engine.py` renderer logic) |
| **Icon** | `icons/mapping/quickSymbol.png` |
| **Type** | Button that opens a small window. Needs QGIS 3.44 or newer. Does not need the KGA Editor or Edit Session and never edits data |

## Overview

Restyles the layer selected in the Layers panel in a small window: one symbol, one colour per value of a field,
or just one class of a unique-value layer. Changes apply as you make them; there is no OK button. Click the
button again to close the window, which follows the active layer.

## How to use

1. Select a layer and click **Quick Symbology** (palette icon).
2. For a plain layer the window shows **Fill Color**, **Outline Color**, **Outline width** (points) and a
   **Preview**. Line layers show just Color and Width.
3. Turn on **Unique Value symbol** for one class per value of a field.

| Part | What it does |
|---|---|
| **Layer** | The layer being restyled, at the top of the window. It follows the layer selected in the Layers panel; pick another layer in the list to restyle it without selecting it there. Untick the Layer checkbox to hide the list (the window still follows the panel; the choice is remembered). With Map Tabs on, the list holds only the layers of the current map. |
| **Field** | The field to classify, or the expression button. |
| **Color Scheme** | The colour ramp the classes take their colours from. |
| **Classes** | Class, Value and Label. Double-click a label to rename it, or a swatch to edit that class's symbol. |
| **+ (green)** | Opens *Select values to add*: the values that are not classes yet, each with the colour it would get. Pick some (Ctrl or Shift for several) and press **Add**. **Options** selects all, clears the selection or adds every value. |
| **Down / up arrows** | Move the selected classes. **All other values** is always last and cannot be moved or removed. |
| **Delete** | Removes the selected classes (also in the right-click menu). |
| **Drop shadow** | A soft shadow under the whole layer's shapes, as in Layer Properties > Symbology > Draw effects. Kept when you switch between single symbol and unique values. An existing drop shadow keeps its settings. |

## Notes and limits

* Select one class of a unique-value layer in the Layers panel and the button opens straight on that class's
  symbol; the back arrow returns to the class list.
* Switching **Unique Value symbol** off gives back the single symbol the layer had.
* Up to 100 classes are made automatically; if the field has more values, add the rest with **+**.
* A layer with another renderer (graduated, rule-based...) is left alone until you turn Unique Value symbol on
  or press **Use a single symbol**. Symbols built from layers other than simple fill, marker or line only take
  a fill colour here; use Layer Properties for the rest.

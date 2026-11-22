# Quick Labeling

|  |  |
|---|---|
| **Algorithm ID** | none: a button on the KGA Layer toolbar (not a Processing algorithm) |
| **Group** | KGA Layer toolbar, right after Layer Bag (it was on the KGA Editor toolbar before 1.1.0) |
| **Source** | `kga_tools/editor/quicklabel/` (`controller.py` button, `dialog.py` the small window, `engine.py` labeling logic) |
| **Icon** | `icons/mapping/quickLabel.png` |
| **Type** | Toggle button with a right-click window. Needs QGIS 3.44 or newer. Does not need the KGA Editor or Edit Session and never edits data |

## Overview

Labels the active layer with one click. The button is a switch for the layer's labels, whoever set them:
it is pressed whenever the active layer shows labels (from this button, Layer Properties > Labels, the Layer
Styling panel or Callout Label Studio) and follows them when they are switched on or off there.

## How to use

1. Select a layer in the Layers panel and click **Quick Labeling**. The layer is labelled with a random field
   (a text field when there is one), in the font, size and colour you used last.
2. **Right-click** the button, only while a quick label is on, to open the small window. Every change applies to
   the layer as you make it; there is no OK button.
3. Click the pressed button again to turn the labels off.

| Setting | What it does |
|---|---|
| **Layer** | The layer being labelled. Untick the Layer checkbox to hide the list (the window still follows the Layers panel; the choice is remembered). With Map Tabs on, the list holds only the layers of the current map. It follows the layer selected in the Layers panel (a layer with no label yet is greyed out; selecting never labels it). Pick another layer here to quick-label it without selecting it in the panel. Layers whose labels come from Layer Properties or Callout Label Studio are not replaced. |
| **Field** | The label field, or the expression button to write an expression. |
| **Font, Style, Size, Colour** | Font family, style (default Regular), size in points and colour. |
| **Halo** | Tick the box, then choose the halo size and colour. White, 1 pt by default; off until ticked. |
| **Position** | Choices follow the geometry. Points: Around point, Above, Below, Left, Right, Over point, Cartographic. Lines: Parallel, Above line, Below line, On line, Curved, Horizontal. Polygons: Around centroid, Over centroid, Horizontal, Free (angled), Along perimeter, Outside polygon. The last choice is remembered separately for points, lines and polygons. |
| **Offset** | Pushes the label away from the feature, in points. Greyed out for positions with nothing to offset (Over point, On line, Horizontal, Free, Over centroid). |
| **Offset X / Y** | Point layers only (points; X right, Y up). Shifts a label placed Above, Below, Left, Right or Over the point, added to Offset. Greyed out for Around point and Cartographic. |
| **Drop shadow** | Tick the box to draw a soft black shadow under the label (1.5 pt down and to the right, slightly blurred). Works for points, lines and polygons; off until ticked and remembered for the next layer. |
| **Round to 2 decimals, Thousand separator, Add length unit, Add area unit** | Shown when the field is numeric. Thousand separator groups digits with commas (9999.00 becomes 9,999.00). Add length unit has a drop-down (m by default, km, ft, mi) and Add area unit one (m² by default, km², ha, ft², yd², ac, mi²); each is enabled only while its box is ticked and the two exclude each other. They follow the layer: a line layer greys out the area unit, a polygon layer the length unit, and a point layer both. The number is converted to the unit (a length field is assumed to be in metres, an area field in square metres, as Shape_Length and Shape_Area are) and the unit text follows it. The label expression is written into the Field box for you, for example `round("Shape_Area",2)\|\|'m'\|\|char(178)`. An empty value shows no label, not a lone unit. Editing the expression yourself unticks the boxes and keeps your expression. |

## Notes and limits

* **Turning off.** A quick label gives back the labels the layer had before. Labels from Layer Properties or
  Callout Label Studio are only switched off; their settings stay on the layer. An open Callout Label Studio
  panel is told it was done on purpose and does not put them back.
* Each layer keeps its own state; the window follows you to the next layer that has a quick label.
* A quick label is an ordinary single-label setting, so Layer Properties > Labels shows and refines it. A layer
  saved in a project reopens with the button pressed.
* Layers without attributes (tables, rasters) cannot be labelled; the button is greyed out for them.
* The superscript 2 is `char(178)`. Use a Unicode font: a legacy-encoded Khmer font such as Khmer MEF1 draws
  a stray letter for that character.

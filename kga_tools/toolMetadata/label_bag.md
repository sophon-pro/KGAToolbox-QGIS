# Label Bag

|  |  |
|---|---|
| **Algorithm ID** | none: a floating bag with its own button on the KGA Layer toolbar and in the Bags submenu of the KGA Toolbox menu (not a Processing algorithm) |
| **Group** | KGA Layer toolbar, with the Tool Bag, Symbol Bag and Layer Colors |
| **Source** | `kga_tools/gui/label_bag/` (`controller.py` button, `dock.py` the window); the label controls are the [Layer Bag](layer_bag.md)'s Labels section (`gui/layer_mini_panel/labels.py`) |
| **Icon** | `icons/interface/labelBag.png` (bubble: `icons/bubble/label_bag.png`) |
| **Type** | Floating bag. Works on point, line and polygon layers |

## Overview

A small bag over the map for the labels of the layer you select in the Layers panel, without opening Layer
Properties. It is the Layer Bag's **Labels** section on its own, in the same bag window:

* **Off / All / Selected features only** - turn labels off, label every feature, or only the selected ones;
* **Field or expression** for the label text;
* **Font, size, bold, italic and colour**, with a live preview;
* **Buffer** (halo) on or off, its size and colour;
* **Drop shadow** - a soft black shadow behind the text;
* **Number** (shown only for a numeric field) - round to 2 decimals, a comma thousands separator, and a unit after
  the number (a line layer offers m, km, ft, mi; a polygon layer m², km², ha, ft², yd², ac, mi²). A unit converts
  the number first: a length field is taken to be in metres, an area field in square metres;
* **Placement** with the same positions as [Quick Labeling](quick_labeling.md) (for a point: around, above, below,
  left, right, over, cartographic; for a line: parallel, above, below, on, curved, horizontal; for a polygon: around
  or over the centroid, horizontal, free, along the perimeter, outside), and an **Offset** in points, plus **X / Y**
  for a point label placed beside its point.

Every change is written onto a copy of the layer's current label settings, so callouts, backgrounds, shadows,
data-defined overrides and anything else set in Layer Properties stay as they are. Rule-based labeling is never
converted: the bag shows its state and offers the native editor.

The header shows the layer; the button after its name switches to another vector layer of the current map.

## Opening it

Click **Label Bag** on the KGA Layer toolbar (or pick it in the KGA Toolbox menu > Bags). The bag
starts closed, opens at the top-right of the map the first time and then where you left it.

**Minimize** shrinks it into a bubble on the map, stacked in the same column as the other bags' bubbles; click
the bubble to bring it back. Bubble, animation and sound options are in the bag's gear menu and in KGA
Settings > Bubbles ("Label Bag").

The bag shares its colours and window style with the [Layer Bag](layer_bag.md), and both can be open at once;
they follow the same layer and show each other's changes.

## Together with Quick Labeling

The Label Bag and the [Quick Labeling](quick_labeling.md) button work on the same labels and stay in step:

* Quick-label a layer and the bag shows it at once (and shows it off when you switch it off); label a layer in the
  bag and the Quick Labeling button is pressed, a click turns the labels off.
* Change something in the bag while Quick Labeling's window is open and the window shows the new value; change
  it in the window and the bag follows as you type.
* The window keeps whatever it does not manage (text opacity, background, masks, placement tweaks made in the
  bag) instead of resetting it when you change the field, font or size.

The bag's height always matches its content, so nothing scrolls: it grows when rows appear (the Number options for a numeric field, X / Y for a point) and shrinks when they go. Drag its edge to change the width.

# Callout Label Studio

|  |  |
|---|---|
| **Algorithm ID** | `kga:callout_label_studio` |
| **Group** | KGA Mapping |
| **Source** | `kga_tools/algorithms/callout_label_studio_alg.py` (window in `kga_tools/gui/callout_label_dialog.py`, engine in `kga_tools/core/callout_studio/`) |
| **Icon** | `icons/calloutLabelStudio.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

Style a layer's labels as **callouts** from one window: simple leader lines,
Manhattan elbows, curved arrows and speech balloons, with the map updating as
you change a setting.

QGIS has all four callout types, but they live in the Layer Styling panel under
Labels > Callouts, with the text, buffer, placement and rendering settings in
other tabs. This puts them behind one form, with quick styles to start from,
an organized layout that stacks the labels so leader lines never cross, and a
number key for small polygons.

## Parameters

The algorithm takes no Processing parameters — running it opens the window.

### 1 Layer and label text

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | The active layer | Point, line or polygon layers. |
| **Label with** | Field / expression | The layer's current label field | A field or an expression (ε button). |
| **Only where** | Expression | — | Only matching features are labelled, e.g. `"pop" > 100000`. |

### 2 Quick styles

Click a thumbnail: **Classic Leader**, **Manhattan Elbow**, **Curved Arrow**,
**Speech Balloon**, **Dark Badge**, **Imagery Overlay** (yellow line, dark
halo), **Survey Dashed**, **Map Inset Box**. Your own presets appear as ★
thumbnails; right-click one to delete it.

### 3 Fine-tune

The **Callout**, **Text** and **Placement** tabs hold the line, balloon, font,
buffer and placement settings. A callout draws whenever a label sits away from
its feature.

**Organized layout** (Placement tab) stacks labels in columns (left, right,
both sides) or rows (top, bottom, both) beside the data, sorted and swapped so
leader lines never cross.

| Control | Description |
|---|---|
| **Distance from data**, **Space between labels** | In millimetres on screen, or on paper when fitted to a layout. |
| **Distribute evenly** | Even spacing along the whole side. Off keeps each label level with its feature. |
| **Vertical text in rows** | Compact rows for long names. |
| **Fit for** | Screen, or a print layout map: spacing is then calculated in real paper millimetres and follows the map frame's extent. |

Positions are computed for the current zoom; set the zoom you will print at,
then Apply.

**Number key** tab: each feature gets a number or letter (1, 2, 3 / A, B, C)
and the names are listed in key boxes outside the data. Numbering order is
reading order, west to east, north to south or alphabetical; the number is a
circle badge or bold text, inside the feature or outside on a leader line;
the key goes right, left, below or above, in 1–12 columns, with optional title
and frame. **Put key in Print Layout** sends the key to a layout as grouped,
editable text boxes beside the map frame; re-sending replaces the old one. With
no layout yet it creates an A4 landscape one.

### Buttons

| Button | Description |
|---|---|
| **Apply** | Keeps the result and leaves the window open. |
| **OK** | Applies and closes. |
| **Cancel / close** | Puts the layer's original labels back, unless Apply was pressed. |
| **More** | *Save as my preset*, *Copy equivalent PyQGIS code*, *Export label style (.qml)*. |

## How to use

1. Select the layer in the Layers panel and run **KGA Mapping > Callout Label Studio**.
2. Choose the label field or expression.
3. Click a quick style.
4. Adjust the Callout, Text and Placement tabs; watch the map.
5. Press **Apply**.

## Outputs

- The layer's labeling is replaced by a rule-based or single labeling with
  callouts enabled. Nothing is written to disk; the style is saved with the
  project.
- **Export label style (.qml)** writes the labeling category only, to reuse on
  other layers via *Load Style*.

## Notes and limits

- **Cancel restores the original labels**, but only until Apply or OK. After
  that the change is yours to undo.
- **Organized layout positions are fixed for the zoom they were computed at.**
  Zoom to print scale first, or use *Fit for > Layout* so the spacing is taken
  from the layout's map frame.
- **The number key is rule-based labelling on the layer itself**, so it prints
  in layouts and saves with the style.
- Moving a label by hand with the Label toolbar's *Move Label* tool is
  compatible with callouts; *Move Callout* drags the feature end.
- For polygons, *Polygon anchor > Pole of inaccessibility* keeps the pointer
  well inside odd shapes.
- The last style used is remembered between sessions (setting
  `kga_tools/callout_label_studio/last_config`), as are your presets.

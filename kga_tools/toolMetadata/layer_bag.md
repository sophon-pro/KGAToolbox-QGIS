# Layer Bag

|  |  |
|---|---|
| **Algorithm ID** | none: a floating panel on the KGA Layer toolbar and the KGA Toolbox menu (not a Processing algorithm) |
| **Group** | KGA Layer toolbar, first button |
| **Shortcut** | Ctrl+Alt+L (change it in Settings > Keyboard Shortcuts) |
| **Source** | `kga_tools/gui/layer_mini_panel/` (`controller.py` button, `dock.py` the panel, `attributes.py`, `stats.py`, `labels.py`, `symbols.py` the three sections, `watcher.py` follows the Layers panel) and `kga_tools/gui/layer_toolbar.py` (the KGA Layer toolbar) |
| **Icon** | `icons/interface/layerMiniPanel.png` |
| **Type** | Floating panel. Works on point, line, polygon and table layers |

## Overview

A small panel over the map that follows the layer you select in the Layers panel and shows, without opening
Layer Properties or the attribute table:

* **Attributes** - the layer's attribute table, its fields, and statistics of one field;
* **Labels** - labels on or off, the field or expression, font, size, colour, buffer and placement;
* **Colors** - the layer's current symbology and the [Layer Colors](symbol_bag.md) colour wheel, to recolour
  the layer or its classes live on the map.

The header shows the layer's name, geometry, provider, CRS and whether it is being edited, read-only or
invalid. Select another layer and everything below switches to it.

**Switch layer:** the button right after the layer name lists the vector layers of the current map (the current
map tab when Map Tabs is on), in Layers panel order and with their usual icons; the layer shown now is ticked.
Pick one and it becomes the selected layer in the Layers panel, so the whole Layer Bag switches to it.

The panel starts closed. Click **Layer Bag** on the KGA Layer toolbar (or in the KGA Toolbox menu, or
press Ctrl+Alt+L) to open it; it opens at the top-right of the map the first time and then where you left it,
also after restarting QGIS if it was open when you closed QGIS.

## The KGA Layer toolbar

The panel's button leads a toolbar of tools that work on the selected layer:

| Button | What it does |
|---|---|
| **Layer Bag** | Shows or hides this panel |
| **[Quick Labeling](quick_labeling.md)** | Labels the active layer in one click; right-click for its small window (QGIS 3.44+) |
| **[Quick Symbology](quick_symbology.md)** | Restyles the active layer: one symbol, unique values or one class (QGIS 3.44+) |
| **Tool Bag**, **Symbol Bag**, **Layer Colors**, **Label Bag** | One button each: [Tool Bag](tool_bag.md), [Symbol Bag and Layer Colors](symbol_bag.md), [Label Bag](label_bag.md) |

Quick Labeling and Quick Symbology were on the KGA Editor toolbar, and the Bags button on the KGA Toolbox
toolbar, before 1.1.0. Show or hide the toolbar with **KGA Layer Toolbar** in the KGA Toolbox menu.

## Attributes

Three views, chosen with the pills at the top. The chips show the number of features, the number selected (with
**Clear** to clear the selection) and the number of fields.

**Table**
* The attribute table. Rows load as you scroll, so a layer with a million features opens by reading 200 rows.
* **All / Selected**: every feature, or only the ones selected on the map. Selecting rows selects the features
  on the map and the other way round. Double-click a row to zoom to it.
* Click a column header to sort. GeoPackage and SpatiaLite layers, and any layer up to 100,000 features, sort
  at once; larger layers sort in the background with progress and Cancel in the status line.
* **Filter rows...** keeps rows whose text fields contain what you type (or whose number fields equal it).
* Right-click a cell: copy the value, the rows or the column name, zoom or flash, use the column as the label
  field, or show the column's statistics. The table button (top right) opens the QGIS attribute table for
  editing values.

**Fields** - names, aliases and types (length and precision); hidden fields are marked. Search, right-click to
copy or use as the label field.

* **+ New field**: name, type (text, whole number, 64-bit whole number, decimal, date, date and time, boolean;
  only the types the format can store), length or decimals where they apply, and an alias. While the layer is
  being edited the field goes into the edit buffer (save the edits to keep it; Undo works). Otherwise it is
  **written to the data source at once**; the form says which before you press **Add field**. Duplicate names,
  invalid characters and the shapefile 10-character limit are caught as you type.

**Statistics** - pick a field and All features or Selected.

| Field type | Results |
|---|---|
| Numbers | Count, NULL, sum, mean, median, standard deviation, minimum, maximum, range, Q1, Q3, unique values and a small histogram |
| Text | Count, NULL, unique, shortest and longest, the most common values with counts and percentages |
| Dates | Earliest, latest, unique |

Statistics run in the background with a progress bar and Cancel. Up to 100,000 features they run by
themselves; above that press **Calculate**. The copy button copies the results.

## Labels

**Off / All / Selected** at the top. *Selected* labels only the features selected on the map and follows the
selection as it changes; it uses QGIS's data-defined *Show label* setting (`is_selected()`), so it is saved
with the project. A *Show label* rule the layer already had is kept aside and put back when you switch to *All*.
*Off* keeps every label setting. Rule-based labeling offers only Off and All.

Below it: the value (a field or an expression), font, size, bold, italic, colour, the text buffer (size
and colour), a placement suited to the geometry, and a preview with the real label text. Each change touches
only that one setting, so data-defined settings, backgrounds, shadows, callouts, scale ranges and priorities
stay as they were. Switching labels off keeps the settings. Rule-based labeling is never converted:
**More...** opens Layer Properties > Labels.

## Colors

* The current symbology (renderer and number of classes) with **Undo** and **Layer Properties**.
* Below it, the **Layer Colors** wheel of the Symbol Bag, inside the panel. It recolours the layer, or the
  classes you select in the Layers panel, live on the map: opacity, palettes, favourites, HEX / RGB / HSV and
  a dial for size or width. The choice buttons adapt to the geometry: points show **Size** and **Outline**,
  polygons **Fill color**, **Outline** and **Fill + outline** (details in [Symbol Bag](symbol_bag.md)).
  Categorized, graduated and rule-based layers keep their classes. **Keep colors** sets an undo point; undo
  is shared with the Symbol Bag.
* **Symbol library** opens the Symbol Bag to apply, create or edit symbols.

If the Symbol Bag is off (for example because the stand-alone Symbol Bag plugin is installed), the Colors view
says so; with the stand-alone plugin it offers its floating Layer Colors instead. Attributes and Labels keep
working.

## The window and its bubble

The panel is a bag window like the Tool Bag and the Symbol Bag: a rounded body, a coloured header with a zipper
band under it, and three buttons in the header:

| Button | What it does |
|---|---|
| **Gear** | The menu below |
| **Minimize** | Folds the panel into a bubble on the map, in the same column as the Tool Bag, Symbol Bag, Catalog and other bubbles, with the shared Genie or Classic animation. Click the bubble (or the Layer Bag button) to bring it back; right-click the bubble to hide it, which closes the panel. Hidden when bubbles are switched off (Settings > Bubbles, where the Layer Bag can also be switched off on its own) |
| **Close** | Closes the panel; it folds into its toolbar button |

Drag the header to move the panel; drag an edge to resize it.

## The gear menu

| Menu | Choices |
|---|---|
| **Bag Style (colour)** | Follow Symbol Bag (default: the bag's style), the nine bag styles, QGIS Neutral, Graphite (dark) |
| **Window style** | Header: Gradient, Solid or Minimal (a plain header, no zipper). Corners: Rounded or Square. Density: Comfortable or Compact |
| **Bubble, Animation, Sound Effects** | The same menus as the Tool Bag and the Symbol Bag: bubbles on or off, this bubble's size and position, Genie or Classic and the speed, sound (shared by all the bags) |
| **Allow docking** | Off (default): the panel always floats over the map. On: you can drag it into a dock area like any panel. Either way it opens as a floating window, never docked beside another panel |
| **Auto-fit size to content** | On (default): the panel sizes itself to what it shows when you switch views. Drag to resize any time. A floating Layer Bag is never wider than 1100 px on a screen 1600 px wide or more, 950 px on a smaller screen (e.g. 1366 x 768, or 1920 at 150 % scaling), and never more than three quarters of the screen; a table with more columns scrolls sideways |
| **Fit to content now** | Fits once |
| **Help** | Opens this page |
| (last line) | Which Symbol Bag the Colors view uses, e.g. "Symbol Bag 1.16.0 (built in)" |

All choices are remembered (QGIS settings `LayerMiniPanel/`, shared with the stand-alone plugin).

## Notes

* Only the visible view is refreshed when you switch layers; the others catch up when you open them, so
  switching layers stays fast on large layers.
* The table is for viewing and selecting. Editing values, forms and the field calculator stay in the QGIS
  attribute table, one click away.
* Background sorting keeps one value per feature in memory and is offered up to 3,000,000 features; statistics
  keep the values of one field (about 8 MB per million numbers).
* The panel's colours follow a changed Symbol Bag style the next time you show the panel or open Colors.
* If the stand-alone Layer Mini Panel plugin is installed, KGA Toolbox leaves its own panel out (a message says
  so).
* Ported from the stand-alone *Layer Mini Panel* plugin (1.6.0; the built-in Symbol Bag is 1.16.0). Its Ctrl+Shift+M shortcut is already used by
  QGIS, so the built-in panel uses Ctrl+Alt+L.

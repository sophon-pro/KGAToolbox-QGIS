# Symbol Bag and Layer Colors

|  |  |
|---|---|
| **Algorithm ID** | none: two floating windows on the KGA Layer toolbar and the KGA Toolbox menu (not Processing algorithms) |
| **Group** | KGA Layer toolbar (one button each), KGA Toolbox menu > Bags |
| **Source** | `kga_tools/gui/symbol_bag/` (`plugin.py`, `ui/panel.py`, `ui/color_picker.py`, `core/`) and `kga_tools/gui/bag_group.py` |
| **Icons** | `gui/symbol_bag/icons/symbol_bag.svg`, `symbol_bag_colors.svg` |
| **Type** | Floating windows |

## Overview

The Tool Bag, Symbol Bag, Layer Colors and Label Bag each have a button of their own on the **KGA Layer** toolbar,
and share one **Bags** submenu in the KGA Toolbox menu. Layer Colors also
sits inside the [Layer Bag](layer_bag.md), under Colors.
The two added here are:

* **Symbol Bag** is a floating library of your favourite point, line, polygon and raster styles. Collect
  symbols from the Style Manager, the project, layers, QML, XML, SLD, SVG and style databases; keep them in
  collections with favourites, tags and search; apply one by double-click or by dragging it onto a layer in
  the Layers panel. Classified layers (categorized, graduated, rule-based) keep their classes. Terrain and
  raster styles (DEM ramps, hillshade, slope, aspect, NDVI, composites) are kept apart and can stretch to the
  value range of the layer they are applied to. The library is a SQLite file in your QGIS profile
  (`symbol_bag/`) with automatic backups; collections can be exported and shared.
* **Layer Colors** recolours the layer, or the classes, selected in the Layers panel, live on the map: colour
  wheel, hex / RGB / HSV, palettes, recent and favourite colours, a thickness wheel for line and outline
  widths, undo and redo. **Symbol Colors**, inside the bag, does the same for bag symbols.

  The choice buttons above the wheel ("smart chips") follow the geometry:

  | Layer | Chips | The dial sets |
  |---|---|---|
  | Points | **Size**, **Outline**, **More** (Fill + outline, and each layer of a multi-layer point) | Size: the point size (presets 2, 3, 4, 6 mm; multi-layer points keep their proportions). Outline: the outline width |
  | Lines | none for a simple line; Whole, All lines, Layers for a double line | The line width (casings stay wider than the centre line; arrows keep their taper) |
  | Polygons | **Fill color**, **Outline**, **Fill + outline** (Whole only for multi-layer symbols) | The outline width |

  The chip row wraps onto a second line when a symbol has many parts or the window is narrow, and the wheel
  shrinks to fit. **Fill color** on an outline-only polygon (no fill, e.g. parcel boundaries over imagery) now
  fills it; lower Opacity to keep the imagery visible. On layers with many small features a note offers
  Fill + outline, and slow layers draw the new colour in instead of freezing the map.

Right-click a layer in the Layers panel for **Add Layer Symbol to Symbol Bag** and **Recolor Layer Colors...**.

## Using KGA Layer Symbol symbols

The + button (and gear > Import) has **Import from KGA Layer Symbol...**. It lists the categories of the
[KGA Layer Symbol](kga_layer_symbol.md) gallery (built-in symbols, icon libraries and style packs, with their
counts); tick the ones you want, review them in the usual import list, and they are added to the bag in a
collection per category under **KGA Layer Symbol**. Colour ramps are left out. Symbols already in the bag are
skipped.

## The bubbles

Minimize a window and it folds into a round bubble on the map; click the bubble to open it again. The bubbles
of the **Tool Bag**, the **Symbol Bag** (its number is how many symbols it holds) and **Layer Colors** (a colour
wheel) sit together as one column, in that order, top to bottom. Only the bags that are minimized take a
place in the column; when one opens or closes the others glide to their new places.

The column goes where the Tool Bag bubble's position setting puts it (nine places on the map) and uses its
size (28 to 64 px). Change them by right-clicking the Tool Bag bubble. The Symbol Bag's own size and position
options are replaced by this.

## Shared settings

The Tool Bag, the Symbol Bag and Layer Colors (gear button in its title bar) offer the same four submenus,
and a change in one is made in all three at once:

| Submenu | Meaning |
|---|---|
| **Bag Style (colour)** | One of the nine colourways, used by all three windows and their bubbles |
| **Bubble** | Minimize to a bubble on or off, bubble size and position (the shared column) |
| **Animation** | Genie or Classic, and the speed (Off, Snappy, Normal, Relaxed) |
| **Sound Effects** | On / off, volume and style of the Genie sound, now also played by the Tool Bag |

Each bag keeps its own extras (the Tool Bag's handle and transparency, the Symbol Bag's layout and library,
Layer Colors' wheel size) in its own menu and in **Bag Style...**.

## Animation style

Settings > Bubbles > **Open / minimize animation** applies to the Tool Bag, the Symbol Bag
and Layer Colors:

| Style | What you see |
|---|---|
| **Genie** (default) | The window is sliced into strips that bend into a funnel and pour into the bubble, and flow back out when it is clicked (as the macOS Dock does) |
| **Classic** | The Tool Bag shrinks into the bubble, the Symbol Bag is tossed into it along an arc, and Layer Colors fades |

The Symbol Bag and Layer Colors also play a short sound with the Genie (Whoosh, Bubble, Chime or Magic; turn
it off or change the volume from the speaker button or the bag's gear menu).

## Notes

* If the stand-alone **Symbol Bag** plugin is installed, KGA Toolbox does not add its own and says so.
* The library in `<profile>/symbol_bag/` is shared with the stand-alone plugin, so an existing library carries over.

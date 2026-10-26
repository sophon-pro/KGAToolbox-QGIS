# Map Tabs and Layout Tools

|  |  |
|---|---|
| **Algorithm ID** | none: window features, not Processing algorithms |
| **Group** | Settings > Appearance > Map tabs; KGA Layout toolbar in the Layout Designer |
| **Source** | `kga_tools/gui/map_tabs.py`, `layout_contents.py`, `new_layout_dialog.py`, `map_picker_dialog.py`, `layout_guide_dock.py`, `north_arrow_dock.py`; `kga_tools/core/layout_guides/` |
| **Icon** | `icons/layoutGuides.png` |
| **Type** | Window features. Map tabs are **Off** by default |

## Map tabs

ArcGIS Pro's maps and layouts in one tab bar above the canvas. Turn on in **Settings > Appearance > Map tabs**.

* One project can hold several **maps**, each with its own layers, extent, rotation and CRS. A map is a
  top-level group in the Layers panel; new layers land in the active map. **Organize this project into Map
  tabs** (on the tab bar) turns an existing project into maps.
* Print layouts open as **tabs** in the main window instead of a separate Layout Designer
  (*Open print layouts as tabs in the main window*).
* Each map is also saved as a map theme (*Keep a map theme for each map*), so a layout map frame can show
  any map. Adding a map frame asks which map it should show, with a preview card per map.
* A layout tab shows a **Contents** panel in place of the Layers panel: every item on the page, and under a
  map frame the layers of its map, which can be switched on and off without leaving the layout.
* Maps and layouts can be brought in from another project. The Catalog lists them as `.kgamap` and
  `.kgalayout` shortcuts.
* Everything is stored in the `.qgz`, so a project opened without the plugin shows one group per map.

**New layout dialog.** Name, page size (or custom), orientation, background colour and an optional neatline.

## KGA Layout toolbar (Layout Designer)

| Button | What it does |
|---|---|
| **Layout Guides** | A panel that lays guides on the page. Step 1: margins from the page edge (one value for all four sides, or each). Step 2: divide the area inside the margins into columns and rows, with an optional space between and centre lines. Or give exact distances such as `50, 120` from a margin. Guides update while you type; *Show guides* and *Snap to guides* switch them. Values are in millimetres |
| **Add Grid** | Opens [Add Grid to Layout](add_grid.md) |
| **Khmer North Arrows** | Pick a group, topic and colour, then double-click an arrow: it is inserted in the layout and linked to the layout's map, so it follows the map rotation. Styles: Khmer North Arrow, Esri North, Esri North Khmer. Placed in a corner of your choice |

## Notes and limits

* Map tabs move the Layout Designer into the main window. Keyboard shortcuts that exist in both (Ctrl+S,
  Ctrl+Z, Delete) are switched off for whichever one is not on screen.
* Switching map tabs turns the other maps' groups off and hides them in the Layers panel.

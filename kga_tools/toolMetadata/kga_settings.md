# KGA Settings and Check for Update

|  |  |
|---|---|
| **Algorithm ID** | none: KGA Toolbox menu windows (not Processing algorithms) |
| **Group** | KGA Toolbox menu > Settings, Check for Update |
| **Source** | `kga_tools/gui/settings_dialog.py`, `kga_tools/core/kga_settings.py`, `kga_tools/gui/update_dialog.py`, `kga_tools/core/update_check.py` |
| **Icon** | `icons/interface/checkForUpdate.png` |
| **Type** | Dialogs. Settings are stored in the QGIS profile (`KGA/settings/`), not in the project |

## Settings

**Projects**

| Setting | Default |
|---|---|
| Default location for new workspaces | Documents\KGA Toolbox Project |
| Default CRS of new projects | EPSG:32648, WGS 84 / UTM zone 48N |
| Save tool output in the project default database and folder | On |
| Keep Shape_Length and Shape_Area up to date on project database layers | On |
| Add a basemap to every new project (and which) | On, Google Terrain Hybrid |
| Zoom new projects to a country (type to search) | On, Cambodia |

**Catalog**

| Setting | Default |
|---|---|
| Ask to convert a traditional project when it is opened | On |
| Save / Save As: ask each time, always as a workspace, or always as a `.qgz` | Ask |

**Bubbles**

One page for every bubble on the map: the Tool Bag, Symbol Bag, Layer Colors, Catalog, Data Explorer, attribute tables and the KGA panels with a Minimize button (KGA Geoprocessing, Panels and Toolbars Manager, KGA Layer Symbol, Create Features, Modify Features, Open Feature Attribute, DEM / DSM Inspector, Layer Bag). A live preview shows the column the way the map will.

| Setting | Default |
|---|---|
| Minimize panels and bags to a bubble on the map (off: minimizing just closes the panel) | On |
| Position on the map: nine places (the bubbles stack in one column there) | Bottom right |
| Size of the bubbles, 28 to 64 px | 40 px |
| Space between bubbles, 2 to 30 px | 10 px |
| Distance from the map edge, 4 to 40 px | 14 px |
| Bubble colour; also recolours the Tool Bag, Symbol Bag and Layer Colors | Mint |
| Open / minimize animation, **Genie** (pours into the bubble) or **Classic**, with a speed of Off, Snappy, Normal or Relaxed | Genie, Normal |
| Open tool windows and panels out of their toolbar button, and fold them back on close (follows the animation and speed above). Menu, shortcut and Geoprocessing-panel starts just open the window | On |
| Sound effects: on or off, style (Whoosh, Bubble, Chime, Magic) and volume | On, Whoosh, 70 % |
| Each bubble on its own: a table with a colour, position and size for every bubble ("Same as others" follows the settings above), and a tick to take a panel's Minimize button and bubble away | All follow the shared settings, all ticked |

Right-click any bubble on the map for **Style**, **Size** and **Position** (panel bubbles), or **Size** and **Position** under Bubble (bag bubbles): the choice applies to that bubble alone, **Reset This Bubble** makes it follow the shared settings again, and **All Bubble Settings...** opens this page. Bubbles that share a position stack in one column; a bubble with its own position starts a column of its own.

**Appearance**

| Setting | Default |
|---|---|
| Match QGIS to the computer's light or dark mode | Off |
| Show selected features in ArcGIS Pro style: symbol kept, bright cyan outline | On |
| Show the Layers panel in ArcGIS Pro style: filled-triangle expanders, roomier rows, light-blue selection | On |
| Map tabs, with three sub-options (see [Map Tabs](maps_and_layouts.md)) | On |
| Smooth navigation (ArcGIS Pro style): animated wheel zoom under the cursor, kinetic pan glide after a Pan-tool or middle-button drag, continuous arrow-key pan and smooth Page Up / Page Down zoom, and an underlay that fills the edges while zooming out. Sub-options: zoom smoothness (110 ms), glide length (325 ms), keyboard, underlay | On |
| Temporary pan while digitizing: hold a key (default **C**, changeable) and drag with the left button to move the map without adding a vertex | On |
| Show the KGA start page instead of the QGIS welcome page; optionally hide the menu bar while it shows | On |
| Remember the toolbars and panels that are enabled, with **Remember current layout** and **Reset toolbars and panels** | Off |

**Editor**

| Setting | Default |
|---|---|
| Show vertices and nodes on the map (see [Vertices and Nodes](vertices_and_nodes.md)), with what to show (vertices, nodes, midpoints), which layers, marker sizes and colours, and the vertex limit | Off |
| Show a grid on the map (see [Grid](grid.md)), with its spacing, unit (m, ft or map units) and rotation | Off, 10 m, 0 degrees |

## Start page

Shown when no project is open. **New:** Map, Workspace, Import Package, and templates from the profile's
`project_templates` folder. **Open:** recent projects as cards, with find, sort, grid or list, and a "..."
menu (Open, Pin, Show in folder, Copy path, Remove). Turn it off for the standard QGIS welcome page.

## Smooth navigation

With the Pan tool, hold the right button and drag up to zoom in or down to zoom out, about the point you
pressed (about 2x per 115 px). A right-click without dragging does nothing, as before.

The animation runs at the refresh rate of the screen the map is on (up to 240 frames per second, never
below 60), and timing is measured in fractions of a millisecond, so motion stays even at any rate.

Works through event filters on each map canvas; turning it off removes them and QGIS navigates as before.
Ctrl, Shift or Alt with the wheel, Space-bar pan, a locked map scale and the keys of editing tools keep their
QGIS behaviour. Source: `kga_tools/gui/smooth_navigation/`.

## Temporary pan

While a digitizing tool is active (Add Line, Add Polygon, ...), hold the pan key and drag with the left
button to move the map, then release the key and keep clicking vertices. The map tool is never switched, so
the vertices, rubber band, snapping and Advanced Digitizing state are untouched. While the key is held the
click is not passed to the tool and right-clicks are ignored, so a feature cannot be finished by accident.
The wheel, the middle button and Space keep working as usual.

Pick the key in Settings > Appearance > Map navigation. Refused: Esc, Backspace, Delete, Enter, Tab, Shift,
Ctrl, Alt and Space (QGIS's own pan). A key that QGIS or the Advanced Digitizing panel also uses is allowed
with a note; on the map the pan key wins (choose another, such as V, if you use construction mode). The map
needs the keyboard focus (click it once); Ctrl+C and typing in forms are never intercepted.
Source: `kga_tools/gui/temporary_pan/`.

## Shape_Length and Shape_Area

On line and polygon layers in the project databases, these fields are added and recalculated whenever a
feature is created or edited, as a File Geodatabase does. Values are planar, in the units of the layer's CRS.
**Layer > Add Shape_Length / Shape_Area Fields** does it for the selected layer on demand.

## Check for Update

Opens a window that asks api.github.com for the latest release of the plugin's repository and says whether
yours is current. Only when you open it; no project data is sent.

## Notes and limits

* New-project settings apply to new projects only; an opened project is never changed.
* The basemap needs an internet connection.
* The cyan selection and Pro Layers panel styles can be turned off to get the standard QGIS look back.

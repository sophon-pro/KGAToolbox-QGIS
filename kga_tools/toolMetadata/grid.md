# Grid

|  |  |
|---|---|
| **Algorithm ID** | none: a button in the status bar and an entry on the map's right-click menu (not a Processing algorithm) |
| **Group** | Status bar, left of the coordinates, after Vertices and Nodes; right-click menu of the map; Settings > Editor |
| **Source** | `kga_tools/gui/grid_view/` (`engine.py` maths, `overlay.py` canvas item, `popup.py` panel, `controller.py`) and `kga_tools/gui/status_buttons.py` |
| **Icon** | `icons/interface/grid.png` |
| **Type** | Toggle button with a drop-down panel. Display only: it never edits data and needs neither the KGA Editor nor Edit Session |

## Overview

Draws a grid of lines over the map, like ArcGIS Pro's Grid. Every tenth line is drawn stronger.

## How to use

1. Click the **Grid** button in the status bar, just left of the coordinate box (it sits beside Vertices and Nodes),
   or right-click the map and choose **Enable Grid**. Click the button again, or choose **Disable Grid**, to hide it.
2. Click the **arrow** next to the button to open the panel:

| Control | What it does |
|---|---|
| **On** | The same switch as the button |
| **Spacing** and its unit | Distance between lines, in **m**, **ft** or **map units** (the units of the map's CRS). Default 10 m. A geographic CRS counts a degree as 111 320 m |
| **Rotation** | Turns the grid counter-clockwise about the map origin, in degrees. Default 0 |
| **Grid Settings...** | Opens Settings > Editor, where the same values are kept |
| **Reset** | Back to 10 m and no rotation |

3. A blue note shows "Grid is dynamically scaled to: ..." when the lines would be closer than about 14 pixels:
   the spacing is multiplied by 10 until they are not, and goes back to the typed value as you zoom in.

The switch, spacing, unit and rotation are remembered when QGIS is restarted. Changes apply to the map at once.

## Performance

At most a couple of hundred lines are drawn, because dynamic scaling keeps them at least 14 pixels apart, so
the grid costs almost nothing even on a big project. It follows Smooth Navigation's zoom and pan.

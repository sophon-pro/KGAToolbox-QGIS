# Vertices and Nodes

|  |  |
|---|---|
| **Algorithm ID** | none: a button in the status bar and an entry on the map's right-click menu (not a Processing algorithm) |
| **Group** | Status bar, left of the coordinates; right-click menu of the map; Settings > Editor |
| **Source** | `kga_tools/gui/node_view/` (`engine.py` numpy analysis, `overlay.py` canvas item, `controller.py` switch and worker) |
| **Icon** | `icons/interface/nodeView.png` |
| **Type** | Toggle button. Display only: it never edits data, and it needs neither the KGA Editor nor Edit Session. Any QGIS version the plugin supports |

## Overview

Shows the vertices and nodes of the lines and polygons on the map, the way ArcGIS Pro's vertex and node display
does:

| Marker | Meaning |
|---|---|
| Hollow grey **square** | A vertex that belongs to one feature only (a line end that is not connected, a polygon corner, a bend) |
| Magenta **circle** | A node: a vertex shared by two features, a line that ends on another feature, a polygon corner on a neighbour's edge, or the point where two features cross without a vertex |
| Blue **diamond** | The midpoint of a segment (off by default) |

Unconnected line ends stand out as squares among the circles, which makes it quick to find gaps, overshoots and
lines that were not snapped. A feature never makes a node with itself (a polygon's closing vertex, the two parts
of one multipart feature).

## How to use

1. Click the **Vertices and Nodes** button in the status bar, just left of the coordinate box, **or** right-click
   the map and choose **Enable Vertices and Node View**, **or** tick it in KGA Toolbox > Settings > Editor.
2. Pan and zoom as usual. The markers stay on the map; zoom in for the vertices of a dense layer.
3. Click the button again, or right-click the map and choose **Disable Vertices and Node View**, to hide them.

The switch is remembered when QGIS is restarted.

## Settings (KGA Toolbox > Settings > Editor)

| Setting | Default |
|---|---|
| Show vertices and nodes on the map (the same switch as the button) | Off |
| Vertices (squares) | On |
| Nodes (circles) | On |
| Midpoints of segments (diamonds) | Off |
| Layers: all visible line and polygon layers, only the layer selected in the Layers panel, or only layers in edit mode | All visible |
| Vertex size, node size | 9 px, 12 px |
| Vertex, node and midpoint colours | Grey, magenta, blue |
| Stop above this many vertices in the view | 80 000 |

## Performance

* The analysis runs in a background thread, so panning and zooming never wait for it. It covers an area a quarter
  larger than the view on every side: panning inside it, or zooming a little, costs only a repaint, and the
  markers follow Smooth Navigation's zoom and pan animation. Leaving the area, zooming a lot, editing or changing
  layers starts a new run after a short pause; the old markers stay until the new ones are ready.
* Markers closer than a few pixels are drawn once, so a zoomed-out view stays light.
* Above the vertex limit nothing is drawn and a small note asks you to zoom in.
* Layers outside their scale range, hidden layers and point layers are skipped.

# Clip Basemap by Boundary

|  |  |
|---|---|
| **Algorithm ID** | none: a window on the KGA Toolbox toolbar and menu (not a Processing algorithm) |
| **Group** | KGA Toolbox toolbar |
| **Source** | `kga_tools/gui/clip_basemap_dialog.py`, `kga_tools/core/clip_basemap.py` |
| **Icon** | `icons/imagery_basemaps/clipBasemap.png` |
| **Type** | Window |

## Overview

Show a basemap only inside a boundary, so a map keeps its satellite or street background just where you need it.
Nothing is downloaded or written to disk: a mask layer, filled with the map background colour, covers
everything outside the boundary, just above the basemap in the Layers panel.

## Controls

| Control | Meaning |
|---|---|
| **Boundary** | A polygon layer, a layer or map extent, or a rectangle or polygon you draw on the map |
| **Basemap** | A preset such as Google Satellite or OpenStreetMap, or a raster layer already in the project |
| **Clip** | Applies the boundary to the basemap |
| **Remove Clip** | Shows the whole basemap again |

## Quick basemaps

**Add Quick Basemap** (toolbar drop-down and menu) adds an OSM, Google or ESRI basemap in one click. The same
XYZ Tiles connections are listed in the Browser. A connection you edit or delete is not put back.

## Notes and limits

* Needs an internet connection for the basemap tiles.
* The window stays open beside the map, so the boundary can be changed and clipped again.

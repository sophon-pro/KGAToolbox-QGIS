# Add Quick Template

|  |  |
|---|---|
| **Algorithm ID** | none — a toolbar window, not a Processing algorithm |
| **Source** | `kga_tools/gui/quick_template_dialog.py` (logic in `kga_tools/core/quick_template.py`) |
| **Icon** | `icons/editing/quickTemplate.png` |
| **Type** | Interactive dialog — modeless |

## Overview

Makes an empty point, line or polygon layer in a few clicks. The window stays
open, so several layers can be made one after another.

## Parameters

| Control | Meaning |
|---|---|
| **Layer name** | Defaults to *Point Template*, *Line Template* or *Polygon Template* to match the geometry, until you type your own. |
| **Geometry** | Point, Line or Polygon. The layer is always multipart (MultiPoint, MultiLineString, MultiPolygon), as in ArcGIS Pro, so a feature can have several parts whenever it needs them. A single-part feature drawn into it is stored as a one-part multipart. |
| **CRS** | WGS 84 / UTM zone 48N (EPSG:32648) by default; use the button beside it to choose another. |
| **Use the default fields** | Adds `name`, `id`, `note` and one measurement: `xy` (point), `length_m` (line) or `area_m2` (polygon). The measurement fills itself in when a feature is drawn, using `$x`/`$y`, `$length` or `$area`. |
| **Copy the field format from a layer** | Shows a layer input. Pick a project layer, drag one in from the Layers panel, the Browser panel or a file manager, or use *Browse…* (a file) or *Browse GDB…* (a File Geodatabase folder). Its fields are listed with checkboxes; only the ticked ones are created. Fields the format maintains itself (`fid`, `OBJECTID`, `Shape_Area` …) are listed but off. |
| **Save as** | *Temporary layer* (default), Shapefile, GeoPackage or File Geodatabase, with a file path. File Geodatabase needs GDAL 3.6 or newer. |

The two field options switch each other off; with both off the layer has no
attribute fields.

## Notes and limits

* `$length` and `$area` follow the project's distance and area units, so the
  `_m` / `_m2` fields are in metres only when the project is set to metres.
* In a GeoPackage, Shapefile or File Geodatabase the draw-time expressions are
  stored in the QGIS project, not in the file.
* A Shapefile cuts field names to 10 characters; the window warns first.
* Saving into an existing GeoPackage or geodatabase adds the layer, replacing
  one of the same name.

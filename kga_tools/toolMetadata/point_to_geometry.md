# Point to Geometry Converter

|  |  |
|---|---|
| **Algorithm ID** | `kga:point_to_geometry` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/point_to_geometry_alg.py` (window in `kga_tools/gui/point_geometry_dialog.py`, engine in `kga_tools/core/point_geometry/`) |
| **Icon** | `icons/conversion/pointToGeometry.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window; no internet |

## Overview

Builds **lines** or **polygons** from an ordered point layer, without
digitizing: GPS tracks, survey traverses, boundary pegs, canal chainage points.
The input layer is **never modified**.

* Points are joined in the order of their **feature ID**, a **field** or any
  **QGIS expression** (optionally descending). The order of the rows in the
  file is never used.
* An optional **Group by** field makes one feature per value.
* Lines come as one **continuous polyline** per group or as **individual
  segments** (one line per consecutive pair).
* Polygons are **closed automatically** and can be written with a matching
  **boundary line** layer, or as the closed boundary line only.
* Geometries are **validated** and self-intersecting polygons can be
  **repaired**. Nothing is dropped silently: every skipped, excluded or repaired
  feature is listed in the Report tab.

The work runs as a background task: the map stays usable, the progress bar
moves, and **Stop** cancels it. Z and M values are kept.

## Parameters

### Input

| Control | Meaning |
|---|---|
| **Point layer** | Any point or multipoint layer of the active map. Starts on the active layer. A multipoint gives its parts as consecutive vertices. |
| **Refresh** | Updates the layer list and the feature and selection counts. |
| **Selected features only (n)** | Use only the selected points. The count follows the map selection while the window is open. |

### Build tab

| Control | Meaning |
|---|---|
| **Create** | *Line* or *Polygon*. |
| **Lines** (Line) | *Continuous polyline*: one line per group, P1 - P2 - P3. *Individual segments*: one line per consecutive pair, with `seg_seq`, `from_fid`, `to_fid`. |
| **Output** (Polygon) | *Polygon*; *Polygon + boundary line* (a second line layer `<name>_boundary`, joined on `out_id`); *Boundary line only (closed line)* — no polygon layer. |
| **Order by** | *Feature ID*, *Field* or *Expression*, plus *Descending*. A text field shows a warning: it sorts alphabetically (`'10'` before `'2'`); use `to_real("field")`. |
| **Group by** | Optional field; each distinct value becomes its own feature. Empty = one feature from all points. |
| **Missing group value** | For NULL, empty or blank group values: *Form one group* (written last, group value NULL) or *Skip those points* (listed in the report). |

### Options tab

| Control | Meaning |
|---|---|
| **Validate geometries** | Rejects empty, zero-length and zero-area results; checks polygons with GEOS. |
| **Repair invalid polygons** | Polygon only. Uses QGIS *make valid*; repaired features get `repaired = 1`. If any result is multipart the layer is written as MultiPolygon. |
| **Remove duplicate consecutive vertices** | Consecutive points with exactly equal X and Y become one vertex (the first is kept). Off: they are kept and reported. |
| **Keep attributes shared by all points of a feature** | A value is copied only when every contributing point has it; otherwise NULL. Fields that differ in every feature are left out. The order field, the group field's copy and `fid` are not copied; list, map and binary fields neither. |

### Output tab

| Control | Meaning |
|---|---|
| **Save as** | *GeoPackage*, *Shapefile* or *Temporary layer*. |
| **File** | GeoPackage starts on the project's default database, Shapefile on a new file in the project folder; without a project, the input file's folder. A path you type is kept. |
| **Layer name** | Defaults to `<layer>_line`, `_segments`, `_polygon` or `_boundary`, made unique in the GeoPackage. |
| **Add the result to the map** | Loads the new layer(s). Always on for a temporary layer. |

An existing output is never replaced without asking. Project layers reading it
are removed from the project first (you are told which), and writing over the
input layer is refused.

### Report tab

Opens when a run starts: the progress bar, then the summary — points read, used
and skipped, groups (and how many failed), features made and repaired, the files
written, and every message (errors first). The same messages go to the
*KGA Toolbox* tab of the Log Messages panel. **Help** opens this page.

## Output fields

| Field | In | Meaning |
|---|---|---|
| `out_id` | all | 1, 2, 3 ... |
| *group field* | when grouping | the group value, original type |
| `pt_count` | polylines, polygons | vertices used (the polygon's closing vertex not counted) |
| `seg_seq` | segments | segment number within the group |
| `from_fid`, `to_fid` | segments | feature IDs of the points at each end |
| `repaired` | polygons, with validate + repair | 1 if the geometry was repaired |

A source field already named like a generated one gets the generated field a
suffix (`out_id_1`).

## How to use

1. Select the point layer in the Layers panel and run the tool.
2. Choose *Line* or *Polygon* and how the result is shaped.
3. Set **Order by** — usually a sequence or time field — and **Group by** if
   the layer holds several lines or parcels.
4. Check **Output**, press **Run**, then read the Report tab.

## Notes and limits

* Ties in the order value are broken by feature ID and counted in the report;
  points whose order value is NULL (or whose expression fails) are left out.
* Duplicate detection is exact (no snapping tolerance).
* Polygons are a single exterior ring; holes are not made from points.
* A repaired polygon keeps Z (interpolated at new vertices) but loses M; the
  report flags it.
* Shapefile field names longer than 10 characters are shortened by GDAL
  (reported); a boundary layer is written as `<file>_boundary.shp`.
* Once writing has begun a Stop waits for it to finish, so no half-written file
  is left.
* Ported from the stand-alone *Point to Geometry Converter* plugin (1.7.0); its
  own window theme and animations were replaced by the KGA window and the
  toolbar open / close animation.

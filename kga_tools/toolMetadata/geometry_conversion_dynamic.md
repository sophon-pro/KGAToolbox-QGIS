# Geometry Conversion

|  |  |
|---|---|
| **Algorithm ID** | `kga:geometry_conversion_dynamic` |
| **Group** | KGA Geometry Utilities |
| **Source** | `kga_tools/algorithms/geometry_conversion.py` |
| **Icon** | `icons/alg_geometry.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

Explodes polygon and line layers into simpler geometries: boundary segments,
central points, boundary vertices or line vertices — and writes the result
wherever you want it, including straight into a GeoPackage or a File Geodatabase.

The four conversions cover the jobs that keep coming back: getting one line per
parcel course, getting a label point that is guaranteed to fall inside its
polygon, getting the vertices of a boundary as points to compare against a survey.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's; the option rows shown change with the mode.

| Control | Type | Default | Description |
|---|---|---|---|
| **Input Layer** | Layer combo | the active layer | The polygon or line layer to convert. |
| **Conversion Mode** | Combo box | `Polygon → Two-Point Lines` | `Polygon → Two-Point Lines` (one 2-vertex line per boundary segment), `Polygon → Central Point`, `Polygon → Boundary Points` (one point per ring vertex), `Line → Vertices` (one point per vertex). |
| **Include holes** | Checkbox | **ticked** | Whether interior rings contribute segments or vertices. Polygon modes. |
| **Point Method** | Combo box | `Point on Surface` | `Point on Surface` is guaranteed to fall inside the polygon; `Centroid` may not, for a concave or ring-shaped one. Central-point mode. |
| **One central point per part** | Checkbox | unticked | Multipart polygons get a point per part instead of one per feature. Central-point mode. |
| **Selected features only** | Checkbox | unticked | Convert just the current selection. |
| **Remove duplicate geometries** | Checkbox | unticked | Drops repeats — useful for boundary segments shared by two parcels, which otherwise come out twice. |
| **Add geometry fields (XY/LatLon or Length)** | Checkbox | **ticked** | Adds coordinate fields to point output, and `length_m` to line output. |
| **Store Output In** | Combo box | `Temporary layer` | `Temporary layer` (in memory, nothing on disk), `Folder (ESRI Shapefile)`, `GeoPackage (.gpkg)`, `File Geodatabase (.gdb)`. |
| **Folder / Database** | File widget | empty | The folder or container. Not used for a temporary layer. |
| **Output Name** | Text | derived from the input | The shapefile base name, or the layer name inside the container. |
| **Run Conversion** | Button | — | Runs it. A progress bar reports the pass. |

## How to use

1. Open **KGA Geometry Utilities > Geometry Conversion**. Close the Processing
   window once the pane appears.
2. Pick the **Input Layer** and the **Conversion Mode**. The option rows below
   change to match the mode.
3. Set the options that matter for that mode â€” **Include holes** for the polygon
   modes, **Point Method** for central points, **Remove duplicate geometries**
   when neighbouring polygons share boundaries.
4. Choose **Store Output In**. Start with `Temporary layer` to check the result;
   switch to a GeoPackage or a folder once you are happy.
5. Fill in **Folder / Database** and **Output Name** for the non-temporary
   destinations.
6. Press **Run Conversion**. The result is added to the project.

## Outputs

- A new layer holding the converted geometries, in the chosen destination, added
  to the project.
- Point output carries X/Y or latitude/longitude fields, line output carries
  `length_m`, when *Add geometry fields* is ticked.
- Existing layers in a GeoPackage or File Geodatabase container are left
  untouched — a new layer is added beside them.

## Notes and limits

- **`length_m` is an ellipsoidal length in metres**, using the project ellipsoid;
  it falls back to planar CRS units converted to metres when no ellipsoid is set.
- **Curved geometries are segmentised first.** A CircularString or CurvePolygon
  becomes straight segments before anything is exploded.
- **Shapefile output truncates field names to 10 characters.** The generated
  field names are shortened up front so they stay unique, and a `.cpg` sidecar is
  written so UTF-8 and Khmer attributes survive the DBF.
- **File Geodatabase output needs GDAL's OpenFileGDB driver (GDAL 3.6 or newer).**
  An older GDAL cannot write one.
- **The window stays open.** Close the Processing dialog behind it and keep
  working.
- The tool cannot run headless or inside a model.

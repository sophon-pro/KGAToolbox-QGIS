# KGA Toolbox — Tool Metadata

Reference documentation for all **41 tools** the KGA Toolbox registers, one file
per tool.

Each file is named after the algorithm's `name()` — the same key used by
`branding.ALG_ICONS` and by each algorithm's `helpUrl()`
(`https://khmergrs.com/docs/qgis/<name>`) — and carries six sections: an identity
header, **Overview**, **Parameters**, **How to use**, **Outputs**, and **Notes
and limits**.

Tools are listed below in the order they appear on the KGA toolbar and in the
Geoprocessing panel (`branding.ordered_groups` / `ordered_algorithms`), not
alphabetically.

**Reading the Type field.** A *batch algorithm* runs from the Processing dialog
with the parameters documented. An *interactive dialog* takes no Processing
parameters at all — running it opens a window, and that window's controls are
what the Parameters section documents. Interactive tools need the QGIS canvas and
cannot run headless or inside a model.

---

## KGA Data Management — 7

| Tool | What it does |
|---|---|
| [Create Layer Package](create_layer_package.md) | Package layers, styles and resources into one `.kgalp` that opens anywhere. |
| [Open Layer Package](open_layer_package.md) | Unpack a `.kgalp` and rebuild its layers, styles and group structure. |
| [Create Points From Table](create_points_from_table.md) | Build a point layer from the XY columns of a CSV, Excel or ODS table. |
| [GeoPackage to Shapefiles](gpkgtoshapefiles.md) | Export every spatial layer of a GeoPackage as its own shapefile. |
| [Layer Export / Import](layerexportimport.md) | Bulk-export project layers to file, and pull layers back in. *(Interactive)* |
| [Shapefiles to GeoPackage](shapefilestogpkg.md) | Pack a folder of shapefiles into one GeoPackage. |
| [Spatial Data Manager](spatialdatamanager.md) | Bulk delete, rename, import and export the layers of a container. *(Interactive)* |

## KGA Schema Tools — 5

| Tool | What it does |
|---|---|
| [Append with Field Mapping](append_with_mapping.md) | Append one layer into another through an explicit field mapping. |
| [Apply Domain Library](apply_domain_library.md) | Import a JSON domain library into a GeoPackage and attach its domains. |
| [Domain & Schema Manager](domain_manager.md) | Create, edit, assign and move field domains. *(Interactive)* |
| [Generate Field Mapping](generate_field_mapping.md) | Match two schemas and save the mapping as reusable JSON. |
| [Validate Against Domains](validate_against_domains.md) | List every attribute value its field's domain rejects. |

## KGA Editing Tools — 9

All nine are interactive: they open a modeless pane that owns its own map tool.

| Tool | What it does |
|---|---|
| [Copy-Paste Feature](copy_paste_feature.md) | Copy features and paste them into another layer, mapping attributes. |
| [Construct Polygon](construct_polygon.md) | Build a polygon from the lines that bound it. |
| [Copy Parallel](copy_parallel.md) | Copy lines to one side or the other at a set offset. |
| [Buffer](buffer_features.md) | Buffer features into a polygon layer, by hover and click. |
| [Split into COGO Lines](split_into_cogo_lines.md) | Break a boundary into one line per course with its COGO values. |
| [Divide](divide_features.md) | Cut features into equal parts, sized parts or percentages. |
| [Merge](merge_features.md) | Combine features into one, choosing whose attributes survive. |
| [Clip](clip_features.md) | Clip chosen layers with the features you pick. |
| [Sequential Numbering](sequential_numbering.md) | Number features by clicking or dragging across them. |

## KGA Geometry Utilities — 5

| Tool | What it does |
|---|---|
| [Duplicate Checker](duplicate_checker.md) | Find features repeating a field value or a geometry. *(Interactive)* |
| [Geometry Conversion](geometry_conversion_dynamic.md) | Explode polygons and lines into segments, points or vertices. *(Interactive)* |
| [Planarize Lines](planarizelines.md) | Split lines at their intersections. |
| [Update Geometry Fields](updategeometryfields.md) | Fill `Shape_Length` and `Shape_Area` on the active layer. |
| [Update X/Y or Lat/Lon Fields](updatexyfields.md) | Write coordinate columns onto a layer. |

## KGA Topology — 3

| Tool | What it does |
|---|---|
| [Check Overlaps and Gaps](checkoverlapsandgaps.md) | Find overlapping polygons and enclosed gaps between them. |
| [Check Points on Boundary Vertices](checkpointsonboundary.md) | Flag points that do not sit on a boundary vertex. |
| [Open Error Inspector](errorinspector.md) | Step through the errors, zoom to each, re-run every check. *(Interactive)* |

## KGA Data Conversion — 5

| Tool | What it does |
|---|---|
| [File Geodatabase to GeoPackage](filegdb_to_geopackage.md) | Bring a whole `.gdb` in as an editable GeoPackage. |
| [GeoPackage to File Geodatabase](geopackage_to_filegdb.md) | Hand a GeoPackage back out as an ArcGIS `.gdb`. |
| [Export Attributes to Excel](attributes_to_xlsx.md) | Write a layer's attributes to a workbook a colleague can edit. |
| [Import Attributes from Excel](xlsx_to_attributes.md) | Apply the edited workbook back, with a diff first. |
| [Export Layers to KML](export_layers_to_kml.md) | Write layers to KML/KMZ for Google Earth, labels included. |

## KGA Irrigation Tools — 7

| Tool | What it does |
|---|---|
| [CSV to DEM and Contour](csvtodemcontour.md) | Turn a surveyed point CSV into a DEM and contour lines. |
| [DEM Elevation Correction and Point Sample Report](demelevationcorrectionandpointsamplereport.md) | Correct DEMs against a reference, merge them, and report the result. |
| [DEM Legend Bar](dem_legend_bar.md) | Render a hypsometric legend bar as a PNG for print. |
| [DEM Point Sample Export to Excel](dempointsampleexport.md) | Sample several DEMs on a grid and export the comparison to Excel. |
| [DEM and Contour Tool](demcontourtool.md) | The resumable, tiled version of the CSV/DEM-to-contour workflow. |
| [Reservoir Rating Curve](reservoir_rating_curve.md) | Stage-discharge curve from weir, orifice and pipe outlets. |
| [Storage Capacity Curve](storage_capacity_curve.md) | Volume and area against elevation for a reservoir. |

---

## Things worth knowing across the toolbox

- **Three tools default to a dry run.** *Append with Field Mapping*, *Import
  Attributes from Excel* and *Apply Domain Library* all start with **Dry run
  ticked**: the first run writes nothing and produces a report. That is the
  intended workflow, not an obstacle.
- **Two tools are resumable.** *DEM and Contour Tool* and *DEM Elevation
  Correction* write a `checkpoint.json` to the output folder and continue from it
  when re-run with the same parameters. *Force re-run* ignores it.
- **Sixteen algorithms take no Processing parameters.** They open a window
  instead, and the plugin runs them directly rather than showing an empty
  Processing dialog. They need the QGIS canvas: they cannot run headless or
  inside a model.
- **Two tools act on the active layer.** *Update Geometry Fields* takes no
  parameters at all and works on whatever is highlighted in the Layers panel.
  Check which layer that is before running it.
- **Close containers elsewhere first.** A `.gdb` open in ArcGIS has a `.lock`
  file, and a GeoPackage the project still has open cannot be replaced on
  Windows. The conversion tools check and say so.
- **`demcontourtool` is the only algorithm without a `helpUrl()`**, so its Help
  button in the Processing dialog has nothing to open.

## Maintaining these files

- One file per algorithm, named `<alg.name()>.md`. Adding an algorithm means
  adding a file here and a row above.
- **Never name a file so it ends in `metadata.txt`.** `package.py` refuses to
  build if anything but `kga_tools/metadata.txt` does, because QGIS reads the
  plugin name from the alphabetically first such entry in the zip. `.md` files
  are safe.
- `package.py` walks the whole plugin tree, so these files ship with the plugin
  automatically.
- Nothing in the plugin reads this folder yet. The text is written to be pasted
  to `khmergrs.com/docs/qgis/<name>` and, later, to be loaded back into the
  plugin's own help strings.

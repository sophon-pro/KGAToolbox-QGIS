# KGA Toolbox — Tool Metadata

Reference documentation for all **58 tools** the KGA Toolbox registers, one file
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

## KGA Data Management — 12

| Tool | What it does |
|---|---|
| [Add Open Data & Basemaps](add_data.md) | Pull admin boundaries, basemaps, DEMs and land cover in from open data sources. *(Interactive)* |
| [Imagery Downloader](imagery_downloader.md) | Download web-map imagery tiles for an area, stitch them into one georeferenced image and add it to the map. *(Interactive)* |
| [Satellite Imagery Archive](satellite_imagery_archive.md) | Search free Sentinel-2, Landsat, OpenAerialMap and STAC archives by date and cloud cover; download scenes clipped to an area. *(Interactive)* |
| [Create Layer Package](create_layer_package.md) | Package layers, styles and resources into one `.kgalp` that opens anywhere. |
| [Open Layer Package](open_layer_package.md) | Unpack a `.kgalp` and rebuild its layers, styles and group structure. |
| [Add Layers to Layer Package](add_to_layer_package.md) | Add project layers or layers browsed from disk to an existing `.kgalp`. *(Interactive)* |
| [Duplicate to New Layer](duplicate_to_new_layer.md) | Copy a layer and its data so the copy is independent of the original; also on the layer right-click menu. *(Interactive)* |
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

## KGA Editing Tools — 10

The first nine are interactive: they open a modeless pane that owns its own map tool. Planarize Lines runs as a normal Processing tool.

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
| [Planarize Lines](planarizelines.md) | Split lines at their intersections. |

## KGA Geometry Utilities — 7

| Tool | What it does |
|---|---|
| [Duplicate Checker](duplicate_checker.md) | Find features repeating a field value or a geometry. *(Interactive)* |
| [Geometry Conversion](geometry_conversion_dynamic.md) | Explode polygons and lines into segments, points or vertices. *(Interactive)* |
| [Point to Geometry Converter](point_to_geometry.md) | Build lines, segments or polygons from an ordered point layer. *(Interactive)* |
| [Sliver and Spike Cleaner](sliver_spike_cleaner.md) | Remove spikes, wedges, narrow strips and tiny parts left after splitting polygons. *(Interactive)* |
| [Strip Lines and Point Grid](strip_point_grid.md) | Lay equal or varying strip lines, a point grid or a grid of squares, hexagons and other regular shapes, over a map extent, drawn area, layer extent or polygon boundary. *(Interactive)* |
| [Update Geometry Fields](updategeometryfields.md) | Fill `Shape_Length` and `Shape_Area` on the active layer. |
| [Update X/Y or Lat/Lon Fields](updatexyfields.md) | Write coordinate columns onto a layer. |

## KGA Topology — 3

| Tool | What it does |
|---|---|
| [Check Overlaps and Gaps](checkoverlapsandgaps.md) | Find overlapping polygons and enclosed gaps between them. |
| [Check Points on Boundary Vertices](checkpointsonboundary.md) | Flag points that do not sit on a boundary vertex. |
| [Open Error Inspector](errorinspector.md) | Step through the errors, zoom to each, re-run every check. *(Interactive)* |

## KGA GeoAI — 2

Both sit on their own **KGA GeoAI** toolbar rather than the KGA Toolbox one, and
are also in the KGA Toolbox menu, the Processing Toolbox and the KGA
Geoprocessing panel.

| Tool | What it does |
|---|---|
| [Detect Roads from Imagery](detect_roads_imagery.md) | Find roads in satellite, aerial or drone imagery with an open deep-learning model, as centerlines, edges and surface polygons. *(Interactive)* |
| [Detect Paddy Field Plots from Imagery](detect_paddy_plots_imagery.md) | Outline paddy field plots in imagery with an open deep-learning model, as plot polygons and the bund lines between them. *(Interactive)* |

## KGA Data Conversion — 6

| Tool | What it does |
|---|---|
| [File Geodatabase to GeoPackage](filegdb_to_geopackage.md) | Bring a whole `.gdb` in as an editable GeoPackage. |
| [GeoPackage to File Geodatabase](geopackage_to_filegdb.md) | Hand a GeoPackage back out as an ArcGIS `.gdb`. |
| [Export Attributes to Excel](attributes_to_xlsx.md) | Write a layer's attributes to a workbook a colleague can edit. |
| [Import Attributes from Excel](xlsx_to_attributes.md) | Apply the edited workbook back, with a diff first. |
| [Export Layers to KML](export_layers_to_kml.md) | Write layers to KML/KMZ for Google Earth, labels included. |
| [Raster Format Converter](raster_format_converter.md) | Convert a raster between ECW, GeoTIFF, JPEG 2000, IMG, PNG and JPEG — resumably. |

## KGA Irrigation Tools — 9

| Tool | What it does |
|---|---|
| [CSV to DEM and Contour](csvtodemcontour.md) | Turn a surveyed point CSV into a DEM and contour lines. |
| [DEM Elevation Correction and Point Sample Report](demelevationcorrectionandpointsamplereport.md) | Correct DEMs against a reference, merge them, and report the result. |
| [Reservoir Rating Curve](reservoir_rating_curve.md) | Stage-discharge curve from weir, orifice and pipe outlets. |
| [Reservoir Storage Analyzer](reservoir_storage_analyzer.md) | Elevation-area-volume curves, inundation maps, volume-to-level lookup and depth rasters for a reservoir, with an excavated-pond option. *(Interactive)* |
| [Paddy Irrigation Tools](highest_point_per_block.md) | Two steps: the high (and low) ground of every paddy block from a DEM (trees and dikes filtered out, with slope and flow direction), then a gravity command check Field - TC - SC - MC - Reservoir with canal water levels. *(Interactive)* |
| [Generate Contour for ROI](generate_contour_roi.md) | Download the free Copernicus DEM for an area and contour it, styled and labelled. *(Interactive)* |
| [Generate River or Stream for ROI](generate_stream_roi.md) | Download the free Copernicus DEM for an area and extract its river and stream network, graded and checked. *(Interactive)* |
| [Multipart Line Repair](multipart_line_repair.md) | Find multipart canal and drain lines, explain why they are multipart (gap, overlap, sliver) and repair them into single lines by snapping, dissolving and merging. *(Interactive)* |
| [DEM / DSM Inspector](dem_dsm_inspector.md) | Tell whether an elevation raster is a bare-earth DEM or a surface model (DSM), with a confidence score, block map and report. *(Interactive)* |

## KGA Mapping — 4

| Tool | What it does |
|---|---|
| [Callout Label Studio](callout_label_studio.md) | Callout labels — leader lines, elbows, curved arrows, balloons — with organized layouts and a number key, from one window. *(Interactive)* |
| [KGA Layer Symbol](kga_layer_symbol.md) | Gallery of ready-made symbols, icons, textures and colour ramps; also a tab in Layer Properties under Symbology. *(Interactive)* |
| [Add Grid to Layout](add_grid.md) | Put a coordinate grid — including a full MGRS grid — on a layout map, from one window. *(Interactive)* |
| [DEM Legend Bar](dem_legend_bar.md) | Render a hypsometric legend bar as a PNG for print. |

---

## KGA Editor toolbar (not a Processing tool)

The second KGA toolbar holds **Edit Session**, **Create Features**, **Modify Features**, **Open Feature Attribute**, **Quick Labeling** and **Quick Symbology**.
They are map tools, panes and switches, not Processing algorithms, so they do not appear in the
Geoprocessing panel or a model and are not counted above. Their reference is
`kga_tools/editor/README.md`, with a page each for [Create Features](create_features.md),
[Modify Features](modify_features.md) (with a page for each of its 32 tools in [modify feature](modify%20feature/README.md)) [Open Feature Attribute](open_feature_attribute.md), [Quick Labeling](quick_labeling.md) and [Quick Symbology](quick_symbology.md).
QGIS 3.44 or newer; Edit Session starts **Off**.

## Layer Auto-Sort (not a Processing tool)

[Layer Auto-Sort](layer_auto_sort.md) orders the Layers panel by data type (points, lines, polygons,
rasters, web basemaps last) in the background. It is a menu and a settings window with no toolbar button,
not a Processing algorithm, so it does not appear in the Geoprocessing panel or a model and is not counted
above. QGIS 3.30 or newer; Auto-sort New Layers is **On** by default and can be switched off in the KGA
Toolbox menu.

## Other features (not Processing tools)

Windows, panels and menu commands. They are not counted above and do not appear in the Geoprocessing panel
or a model.

| Feature | What it does |
|---|---|
| [Catalog and Workspaces](catalog_and_workspaces.md) | ArcGIS Pro's Catalog pane, workspace folders, scratch Untitled project, tool output in the project, Save choice. QGIS 3.36+ |
| [Project Packages](project_packages.md) | Share Workspace, Import Project Package and Convert Project to Workspace |
| [Browser Copy and Paste](browser_copy_paste.md) | Copy and Paste on the Browser and Catalog right-click menu, plus Add to Map |
| [Data Explorer](data_explorer.md) | Media-browser style dock to browse, search, preview and ingest geodata. QGIS 3.44+ |
| [Panels and Toolbars Manager](ui_manager.md) | Visual, searchable switcher for every panel and toolbar, with saved layouts. |
| [Map Tabs and Layout Tools](maps_and_layouts.md) | Maps and layouts as tabs, Contents panel, Layout Guides, Khmer North Arrows |
| [Clip Basemap by Boundary](clip_basemap.md) | Show a basemap only inside a boundary; Add Quick Basemap |
| [Zoom to Location](zoom_to_location.md) | Zoom the map to a continent, country, province, district, commune, village or typed coordinate |
| [Go To XY](go_to_xy.md) | Type a coordinate (meters, feet, degrees, UTM, MGRS), zoom to it and mark it with a temporary point |
| [Map Cursor](cursor_style_manager.md) | Choose a custom cursor for the map tools (KGA Toolbox menu > Map Cursor) |
| [Vertices and Nodes](vertices_and_nodes.md) | Show the vertices, nodes (where features connect or cross) and midpoints of lines and polygons on the map, like ArcGIS Pro; status-bar button, right-click menu, Settings > Editor |
| [Grid](grid.md) | Map grid with spacing and rotation, like ArcGIS Pro; status-bar button with a drop-down, right-click menu, Settings > Editor |
| [Label Bag](label_bag.md) | Floating bag for the labels of the selected layer (field, font, size, colour, buffer, placement); on the KGA Layer toolbar |
| [Layer Bag](layer_bag.md) | Floating panel for the selected layer: attribute table, statistics, labels and the Layer Colors wheel; leads the KGA Layer toolbar |
| [Tool Bag](tool_bag.md) | Floating bag of your favourite tools that shrinks into a bubble on the map; list or grid layout, up to three columns |
| [Symbol Bag and Layer Colors](symbol_bag.md) | Floating symbol library and live layer recolouring; their bubbles sit with the Tool Bag's, with a Genie or Classic animation |
| [KGA Settings and Check for Update](kga_settings.md) | New-project defaults, Pro-style selection and Layers panel, start page, theme, Bubbles, Smooth navigation, temporary pan (hold C), toolbar magnify, scratch layers on close, Shape_Length and Shape_Area |
| [Add Quick Template](add_quick_template.md) | Create an empty point, line or polygon layer with standard fields |

## Things worth knowing across the toolbox

- **Three tools default to a dry run.** *Append with Field Mapping*, *Import
  Attributes from Excel* and *Apply Domain Library* all start with **Dry run
  ticked**: the first run writes nothing and produces a report. That is the
  intended workflow, not an obstacle.
- **Two tools are resumable.** *DEM Elevation
  Correction* and *Raster Format Converter* write a `checkpoint.json` beside
  their output and continue from it when re-run with the same parameters.
  *Force re-run*, or *Resume* switched off, ignores it.
- **Thirty-six algorithms take no Processing parameters.** They open a window
  instead, and the plugin runs them directly rather than showing an empty
  Processing dialog. They need the QGIS canvas: they cannot run headless or
  inside a model.
- **Two tools act on the active layer.** *Update Geometry Fields* takes no
  parameters at all and works on whatever is highlighted in the Layers panel.
  Check which layer that is before running it.
- **Close containers elsewhere first.** A `.gdb` open in ArcGIS has a `.lock`
  file, and a GeoPackage the project still has open cannot be replaced on
  Windows. The conversion tools check and say so.

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

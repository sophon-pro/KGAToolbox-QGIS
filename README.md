# KGA Toolbox

A QGIS plugin from [Khmer GRS Academy (KGA)](https://khmergrs.com/) that bundles
the KGA processing algorithms into a single provider with a toolbar of its own.
It adds the things QGIS leaves out:

- **Open data** — Add Open Data & Basemaps pulls administrative boundaries
  (geoBoundaries, HDX/OCHA COD-AB, Natural Earth, or any GeoJSON, ArcGIS REST or
  WFS address), XYZ basemaps, open elevation models (Copernicus DEM, no account;
  OpenTopography with a free key) and open land cover (Esri 10 m, ESA
  WorldCover) straight into the project, showing publisher, licence and size
  first. Imagery Downloader stitches web-map tiles for an area into one
  georeferenced GeoTIFF, JPEG, PNG or JPEG 2000 and resumes after an
  interruption. Generate Contour for ROI and Generate River or Stream for ROI (see
  Irrigation) download the free Copernicus DEM the same way. For Cambodia, a
  **KGA Database** source adds province, district, commune and village boundaries and
  centres from a private KGA data release. Clip Basemap by Boundary shows a
  basemap only inside a boundary. These tools, Check for Update and the
  KGA Database are the only ones that use the network, and only when asked.
- **Data Explorer** - a Premiere Pro style media browser dock (Ctrl+Alt+I) to
  browse, search, preview and ingest geodata, with proxies for big rasters.
- **Conversion** — a two-way converter between an ArcGIS File Geodatabase and a
  GeoPackage, a whole container at a time, carrying field domains, layer
  aliases, non-spatial tables, Z/M values and curves across. Also shapefile and
  GeoPackage converters, KML export with labels, and a self-contained layer
  package format. The QGIS **Browser** panel gets **Copy** and **Paste** on its
  right-click menu: select layers (or a whole GeoPackage or geodatabase), Copy,
  then right-click a folder, GeoPackage or File Geodatabase and Paste — to
  shapefile, GeoPackage, File Geodatabase or GeoJSON, with a choice of what to do
  when a name already exists.
- **Schema** — a Domain & Schema Manager for GeoPackage field domains, Append
  with Field Mapping, and a safe attribute round-trip to Excel.
- **Topology** — line and point checks with an Error Inspector that lists the
  errors a check wrote, zooms to each one and re-runs the check. Planarize
  Lines, Duplicate Checker, Update Geometry Fields and Update X/Y or Lat/Lon
  Fields.
- **Editing** — the ArcGIS Pro *Modify Features* pane for QGIS: 30 tools in six
  categories (Alignment, Reshape, Divide, Construct, Attributes, COGO), docked on the
  KGA Editor toolbar with favorites. The KGA tools behind it (Copy Parallel, Buffer,
  Split into COGO Lines, Merge, Divide, Clip, Construct Polygon, Sequential Numbering and
  Copy-Paste Feature) also work as stand-alone windows: hover a feature to see the result
  drawn in cyan, click to write it, or drag across several to do them all.
- **Irrigation** — CSV to DEM/contour, reservoir rating
  curve, reservoir storage analyzer, and DEM elevation correction with a point
  sample report. Generate Contour for ROI downloads the free Copernicus DEM for
  any area (map extent, layer extent or boundary, or a drawn shape) and makes
  styled, labelled contour lines from it in one step. Generate River or Stream
  for ROI does the same for the drainage network: pick how much stream you want,
  and every segment comes back graded for confidence, optionally checked against
  OpenStreetMap or your own stream layer.
- **Mapping** — Add Grid to Layout, which puts a coordinate grid on a print
  layout map from one window instead of five panels, works the interval out
  from the map's own extent, and edits the grids a map already has. It also
  draws a full **MGRS** grid — zone and band, 100 km square letters and
  principal digits, laid down as four linked grids in one press — which QGIS has no
  support for at all. DEM Legend Bar writes the hypsometric legend bar as a PNG for a
  layout or a report. Callout Label Studio makes callout labels (leader lines,
  elbows, balloons, number keys) from one window and as a mode in Layer Properties >
  Labels. KGA Layer Symbol is a gallery of ready-made symbols, icons, textures and
  colour ramps, also a tab under Symbology. In the Layout Designer, a KGA Layout toolbar
  adds Layout Guides and Khmer North Arrows.
- **KGA Editor** — a second toolbar for editing. *Edit Session* removes
  Start/Stop Editing: layers you allow stay in edit mode, you select, move,
  split and delete across every editable layer from one tool, QGIS's own
  advanced editing tools work on the layer you click, and one Save button
  writes everything (or Discard throws it away). Where several features overlap
  under a click, a small popup switches the selection between them. *Create
  Features* is a templates pane that digitizes points, lines and polygons with
  construction tools and a right-click construction menu (parallel,
  perpendicular, direction, distance, absolute and delta X,Y). *Modify
  Features* (above) holds the editing tools. *Open Feature Attribute* is a pane
  to select features and type into their attributes. Edit Session starts Off
  and changes nothing until you turn it on. See `kga_tools/editor/README.md`.

- **Layer Auto-Sort** — ArcGIS Pro's order for the Layers panel: points on
  top, then lines, polygons, and rasters at the bottom. Works in the background, with no
  toolbar button: each new layer is put at the top of its type band, so a
  basemap added later lands at the bottom, not on top. *Sort Layers by Type*
  (and *Sort Group by Type* on a group) are on the Layers panel right-click
  menu to tidy what is there; the KGA Toolbox > Layer Auto-Sort menu switches
  it off and opens *Layer Sort Settings* to change the order. A saved project
  is never reordered when it opens. Needs QGIS 3.30 or newer.

- **Catalog** — ArcGIS Pro's Catalog pane for QGIS, a dock panel on the right
  that replaces the Browser panel (which is switched off the first time; switch
  it back on whenever you like). Four tabs: **Project** (Toolboxes, Databases,
  Folders), **Connections** (GeoPackage, PostgreSQL, WMS, XYZ Tiles, ArcGIS REST
  ... the QGIS data-source connections), **Computer** (Home, This PC, Network)
  and **Favorites**. Every item is a real QGIS Browser item, so layers drag onto
  the map or add with right-click > Add Layer to Project, and QGIS's own menus
  and KGA's Copy / Paste work in it. **Project > Set Workspace** picks a folder
  and a project name and creates everything the project owns there:
  `<Name>.qgz`, `Default.gpkg`, a `Toolbox` folder and a `Layouts` folder
  (default `Documents\KGA Toolbox Project\<Name>`; until you set one you work in
  a per-session *Untitled* scratch project that Set Workspace moves in, and that
  can be recovered after a crash). **Project > Open Workspace** opens one again, and opening a
  `.qgz` that lives in a workspace switches to it. Scripts dropped in the
  workspace's `Toolbox` folder are read as QGIS script tools and listed under
  Toolboxes (double-click to run). Right-click Folders to attach any folder to
  the project. The Catalog button sits after KGA Geoprocessing on the toolbar.
  Needs QGIS 3.36 or newer.

  **Output goes to the project.** Like Pro's default geodatabase, every tool's
  output starts in the project: layers go to a new table in the project's
  default database (`Default.gpkg`, or the one you made default with
  right-click > Make Default), files and folders to the project's default folder.
  Names are `<Tool>_<Input>` (e.g. `Buffer_Roads`), never reuse an existing
  table or file, and follow the input layer while you pick it. This covers QGIS's
  own Processing tools as well as KGA's; change any field and your choice is
  kept. The Browse and Save As dialogs also open in the project folder.

- **Convert a plain project** — **Project > Convert Project to Workspace** turns the
  open `.qgz` into a KGA workspace: it shows every layer and what will happen to
  its data, then builds a new workspace folder where data beside the project keeps
  its place and data from anywhere else is copied into `Default.gpkg` (vector) or
  `commondata/` (rasters and other files). The project is rewritten with relative
  paths; the original project and data are left untouched; layers whose source is
  lost are flagged and kept.
  Opening a traditional project (Project > Open Recent, and also File > Open, drag and
  drop or the welcome page) offers to convert it first: **Yes** asks where to put the
  workspace (alongside the original project, or in a folder you choose), converts, and
  opens the workspace; **No** opens it as a traditional project. "Don't ask me again"
  and a switch in the Catalog menu turn the question off.
- **Project packages** — **Project > Share Workspace** writes the open project as
  one `.kgapp` file, the way ArcGIS Pro writes a project package, and **Project >
  Import Project Package** unpacks it into a workspace of the recipient's choice.
  Two modes: *Everything* (the map, layouts, and every file and table in the
  project folder and databases) or *Map data only* (the layers in the map plus the
  data they need: joined and related tables, value-relation lookups, layout atlas
  and table layers, virtual-layer inputs, layers named in expressions). Data from
  outside the project is copied in (vector data into `Default.gpkg`, rasters into
  `commondata/`); scratch and CSV layers are saved as tables; symbols' SVGs and
  layout pictures travel too. Before anything is written an analysis table lists
  every layer, what will happen to it and why; layers whose source is lost are
  flagged *not shared* and kept in the project so styles and layouts survive.
  Passwords are never packaged. Packages carry a name, summary, tags, author,
  credits and use limitations, a thumbnail, and a checksum for every file; import
  verifies them, refuses unsafe paths and newer formats, and reports layers that
  still do not load. A `.kgapp` file in the Browser or Catalog has Import Project
  Package on its right-click menu. Structure: `manifest.json`, `thumbnail.png`,
  and `project/` (the workspace folder: `<Name>.qgz` with relative paths,
  `Default.gpkg`, `Toolbox/`, `Layouts/`, `resources/`, `commondata/`).

- **Look and feel** — **KGA Toolbox > Settings** holds the defaults for new
  projects (location, CRS, basemap, start extent by country), tool output in the
  project, Save / Save As choice, and an Appearance page: ArcGIS Pro style
  selection (cyan outline) and Layers panel, a start page with recent-project
  cards and templates, light/dark theme to match the computer, remembered
  toolbars and panels, and optional **Map tabs** (several maps and print layouts
  as tabs, with a Contents panel on layouts).
- **Also** — Duplicate to New Layer, Quick Template (an empty layer with standard
  fields), Shape_Length / Shape_Area kept up to date on project database layers,
  and Check for Update.

Distances and areas are honest on a layer stored in degrees: the work is done in
the matching UTM zone and brought back.

Requires QGIS 3.28 or newer (the KGA Editor toolbar needs 3.44 or newer and
is left out on older versions).

## Install

Get `kga_tools.zip` — from the
[releases page](https://github.com/sophon-pro/KGAToolbox-QGIS/releases), or
build it yourself with `python package.py` (see [Packaging](#packaging)).

In QGIS: Plugins > Manage and Install Plugins > Install from ZIP, pick the file,
then Installed > enable "KGA Toolbox".

The tools appear in the Processing Toolbox under "KGA Toolbox", and on a toolbar
of their own. Every algorithm carries a Help button that lands on the KGA docs.

## Install (development)

Symlink or copy the `kga_tools` folder into your QGIS profile plugins
directory:

Windows:
    %APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\kga_tools

Linux:
    ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/kga_tools

macOS:
    ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/kga_tools

Windows symlink (run as administrator):

    mklink /D "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\kga_tools" "C:\dev\kga_tools"

Then: Plugins > Manage and Install Plugins > Installed > enable "KGA Toolbox".

## Packaging

From the repository root:

    python package.py

Writes `kga_tools.zip` next to the plugin folder, for Plugins > Install from ZIP.

Only one file in the whole plugin may be named so that it ends in
`metadata.txt`, and that is the plugin's own `metadata.txt`. QGIS works out the
plugin name from the archive by sorting every entry ending in `metadata.txt`
*alphabetically* and splitting the first one, assuming the shortest path sorts
first. A stray `algorithms/foo_metadata.txt` sorts ahead of
`kga_tools/metadata.txt`, so QGIS extracts the plugin correctly and then fails
with `No module named 'kga_tools/algorithms'`. Name per-algorithm notes
`foo_notes.txt`. `package.py` refuses to build if anything else matches.

## Adding algorithms

Copy each script from
    ...\profiles\default\processing\scripts\
into
    kga_tools\algorithms\

No registration step is needed. The provider imports every module in that
folder and registers any QgsProcessingAlgorithm subclass it finds.

One exception: a class whose name starts with an underscore is skipped. Give a
shared base class for two related algorithms a leading underscore, or it is
registered too - as an algorithm with no name, which shows up as a blank entry
in the toolbar menu and the toolbox tree. See `algorithms/gdb_gpkg.py`.

The `group()` string on each algorithm becomes both the toolbox category and
a dropdown button on the KGA Toolbox toolbar. Adjust GROUP_ORDER and
GROUP_ICONS in branding.py to control button order and icons.

### Icons

Icons live in `icons/` and are wired up in branding.py, not in the algorithm:

- `GROUP_ICONS` maps a group name to the icon on its toolbar button.
- `ALG_ICONS` maps `alg.name()` to a per-algorithm icon, and can stay sparse:
  an algorithm with no entry there falls back to its group's icon, and only
  then to the generic QGIS one. So a new algorithm looks right on the toolbar
  and in the Geoprocessing panel without an icon of its own.
- `ALG_ORDER` pins entries to the top of their group's menu; anything else is
  sorted by display name.

### Windows that outlive the run

An algorithm that opens a non-modal dialog parented to the QGIS main window
must expose a close helper, and `KgaToolsPlugin.unload` must call it — see
`error_inspector.close_error_inspector` and
`duplicate_checker.close_duplicate_checker`. Otherwise the window is still on
screen after the plugin is disabled.

The same modeless pattern is used throughout: a module-level `DIALOG_INSTANCE`,
a `show_*` helper that raises the open window instead of stacking a second one,
and a `close_*` helper that disconnects its signals before deleting it. The
Error Inspector follows it too — it is a plain window, not a dock, so it can be
moved off the QGIS window while the user edits.

### Modify Features tools

The ArcGIS Pro *Modify Features* replicas — Construct Polygon, Copy Parallel,
Buffer, Split into COGO Lines, Merge, Divide and Clip — are modeless dialogs
sharing `gui/modify_dialog.py`. To add another, subclass
`ModifyFeaturesDialog`, set `TITLE` / `ACTION_LABEL` / `HINT` / `ALG_NAME`,
build the parameters into the form it hands you, and answer
`result_for(feature)` and
`apply_to(features)`. The base handles the layer combo, the map tool, the hover
preview, the edit-session prompt and the single undo step.

They own their map tool, and that is the whole design. An earlier version was
six docked panes that read a selection the user made with the QGIS select tool
and acted on it when a button was pressed. That meant depending on a chain of
things the pane did not own — whether `iface.actionSelect()` triggered, which
layer the tool it installed was reading, whether `selectionChanged` came back —
and when a link in that chain broke, the pane went on looking correct while
doing nothing at all. `sequential_numbering_dialog.py` never had that problem
because it owns its map tool; these follow it.

The rules they rely on:

- Each tool exposes a `close_*` helper calling `close_dialog(YourDialog)`, and
  `KgaToolsPlugin.unload` must call it. Otherwise the dialog — and the map tool
  it has on the canvas — survives the plugin being disabled.
- A tool needing more than one feature at a time sets `MIN_FEATURES`; the base
  then collects on each click and commits on Enter, which is what Merge does.
  `COLLECTS = True` asks for the same gathering with a `MIN_FEATURES` of one —
  Construct Polygon, where a single closed line already encloses something but
  a boundary usually arrives as several courses. `on_activated()` runs when the
  tool is put in hand, which is where Construct Polygon picks up a selection
  the user made with QGIS's own tools.
- A tool with a direction in it borrows `AngleMapTool` rather than leaving the
  user to type one. It hands back two points in canvas CRS - off the nearest
  edge under the cursor, or off a two-click line with the project's snapping -
  and the dialog turns them into a number, because only the dialog knows the
  CRS the operation is carried out in. Divide is the one that uses it; the
  contract is the four `angle_*` methods listed on the tool.
- `ALG_NAME` is the owning algorithm's `name()`, and it is what the dialog's
  Help button is keyed by. Leave it unset and the dialog simply has no Help
  button - these tools never show the Processing parameters dialog, so its own
  Help button is not there to fall back on.
- The geometry itself belongs in `core/modify_features.py`, which imports no Qt
  widgets and can be exercised from `python-qgis-ltr.bat` without a canvas.
  What has to know about a canvas — the working CRS for a typed distance, the
  features under a click — lives in `gui/modify_base.py` as plain functions.

## Notes

- Algorithm ids change from `script:<name>` to `kga:<name>`. Any saved models
  or Python scripts that call the old ids need updating.
- Errors during import are written to the Log Messages panel under "KGA Toolbox".
- Install the "Plugin Reloader" plugin for fast iteration.

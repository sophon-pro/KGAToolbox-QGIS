# KGA Toolbox

## Install (development)

Symlink or copy this folder into your QGIS profile plugins directory:

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
`ModifyFeaturesDialog`, set `TITLE` / `ACTION_LABEL` / `HINT`, build the
parameters into the form it hands you, and answer `result_for(feature)` and
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

Three rules they rely on:

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
- The geometry itself belongs in `core/modify_features.py`, which imports no Qt
  widgets and can be exercised from `python-qgis-ltr.bat` without a canvas.
  What has to know about a canvas — the working CRS for a typed distance, the
  features under a click — lives in `gui/modify_base.py` as plain functions.

## Notes

- Algorithm ids change from `script:<name>` to `kga:<name>`. Any saved models
  or Python scripts that call the old ids need updating.
- Errors during import are written to the Log Messages panel under "KGA Toolbox".
- Install the "Plugin Reloader" plugin for fast iteration.

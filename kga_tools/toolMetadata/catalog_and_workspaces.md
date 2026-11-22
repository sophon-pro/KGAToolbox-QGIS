# Catalog and Workspaces

|  |  |
|---|---|
| **Algorithm ID** | none: a dock panel and Project menu items (not a Processing algorithm) |
| **Group** | KGA Toolbox toolbar > Catalog; Project menu |
| **Source** | `kga_tools/gui/catalog_*.py`, `kga_tools/core/workspace.py`, `kga_tools/core/output_defaults.py`, `kga_tools/gui/save_guard.py` |
| **Icon** | `icons/workspace/catalog.png` |
| **Type** | Dock panel. Needs QGIS 3.36 or newer; left out on older versions |

## Overview

ArcGIS Pro's Catalog for QGIS. It replaces the Browser panel (switched off the first time; switch it back
on whenever you like) and keeps a project's data together in one **workspace** folder.

## Catalog tabs

| Tab | Holds |
|---|---|
| **Project** | Maps, Toolboxes, Databases, Layouts and Folders of the open project |
| **Connections** | GeoPackage, PostgreSQL, WMS, XYZ Tiles, ArcGIS REST and the other QGIS connections |
| **Computer** | Home, This PC, Network |
| **Favorites** | Items you pinned |

Every item is a real QGIS Browser item: drag layers onto the map, double-click to add, and use QGIS's own
right-click menus plus KGA's Add to Map, Copy and Paste.

To copy data between databases, drag layers (or a whole GeoPackage / File Geodatabase) onto another
GeoPackage, File Geodatabase or folder - or onto any layer inside the target database. A tip beside the
pointer names where the copy goes; the copy works like Copy then Paste. Dropping back on the database the
layers came from does nothing. Layers dragged in from the QGIS Browser or the Data Explorer can be dropped
the same way.

## Workspace

A workspace is a folder holding `<Name>.qgz`, `Default.gpkg`, a `Toolbox` folder and a `Layouts` folder.

| Command | What it does |
|---|---|
| **Project > Set Workspace** | Pick a folder and a name; creates the workspace for the open project. Untitled work is moved in |
| **Project > Open Workspace** | Open an existing workspace |
| **Make Default** (right-click a database or folder) | Sets where tool output goes |
| **Folders > Attach Folder** / **Databases > Attach Database** | Attach any folder or database to the project; **Remove from Project** detaches it |

* **Toolbox:** scripts placed there are read as QGIS script tools and listed under Toolboxes.
* **Layouts and Maps:** the project's print layouts and maps are listed as shortcuts (`.kgalayout`,
  `.kgamap`); double-click to open one.
* **Untitled:** a project that has no workspace yet works in a scratch folder
  (`Documents\KGA Toolbox Project\_scratch`). **Set Workspace** moves its data into the new workspace. At
  start-up, unsaved scratch data from an earlier session is offered back with Recover or Discard, and
  scratch folders older than 7 days are removed. Only the data comes back, not the unsaved map layout.

## Output goes to the project

Like Pro's default geodatabase, every tool's output starts in the project: layers in a new table of the
default database, files and folders in the default folder. Names are `<Tool>_<Input>`, never reuse an
existing table or file, and follow the input layer. This covers QGIS's Processing tools as well as KGA's.
Change any field and your choice is kept. Turn it off in **Settings > Projects**.

## Saving

**Save** and **Save As** ask whether to save as a workspace or as a traditional `.qgz` when the project is
new or not in a workspace yet. Settings > Catalog can make the answer fixed. A project that already has a
file is simply saved.

## Notes and limits

* Closing or opening another project through QGIS's own prompts still saves a plain `.qgz`.
* Converting an existing `.qgz` and sharing a workspace are in [Project Packages](project_packages.md).

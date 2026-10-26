# Project Packages

|  |  |
|---|---|
| **Algorithm ID** | none: Project menu and Catalog menu commands (not Processing algorithms) |
| **Group** | Project menu > Share Workspace, Import Project Package, Convert Project to Workspace |
| **Source** | `kga_tools/core/projectpkg.py`, `project_share.py`, `project_analysis.py`; `kga_tools/gui/share_workspace_dialog.py`, `import_package_dialog.py`, `convert_project_dialog.py`, `project_open_guard.py` |
| **Icon** | `icons/shareWorkspace.png`, `icons/importProjectPackage.png`, `icons/convertProject.png` |
| **Type** | Dialogs. Part of the Catalog (QGIS 3.36 or newer) |

## Overview

Move a QGIS project between computers, or into a workspace, without broken layer paths. The model is
ArcGIS Pro's project package.

## Commands

| Command | What it does |
|---|---|
| **Share Workspace** | Writes the open project as one `.kgapp` file |
| **Import Project Package** | Unpacks a `.kgapp` into a workspace you choose. Also on the right-click menu of a `.kgapp` in the Browser or Catalog |
| **Convert Project to Workspace** | Turns the open `.qgz` into a workspace, with relative paths |

## Share Workspace

* **Everything:** the map, layouts, and every file and table in the project folder and databases.
* **Map data only:** the layers in the map plus the data they need: joined and related tables,
  value-relation lookups, layout atlas and table layers, virtual-layer inputs, layers named in expressions.
* Data from outside the project is copied in (vector into `Default.gpkg`, rasters into `commondata/`).
  Scratch and CSV layers are saved as tables. Symbol SVGs and layout pictures travel too.
* An **analysis table** lists every layer, what will happen to it and why, before anything is written.
  Layers whose source is lost are flagged *not shared* and kept in the project so styles and layouts survive.
* Packages carry a name, summary, tags, author, credits, use limitations and a thumbnail. Passwords are
  never packaged.

## Import

Every file is checked against its checksum. Unsafe paths and newer package formats are refused, and layers
that still do not load are reported.

## Convert Project to Workspace

Shows every layer and what will happen to its data, then builds a new workspace folder: data beside the
project keeps its place, data from anywhere else is copied into `Default.gpkg` (vector) or `commondata/`
(rasters and other files). The original project and data are left untouched. A project already in a workspace
is refused.

**Ask on open.** Opening a traditional project (Open Recent, Open, drag and drop) asks whether to convert it
first. **Yes** asks where to put the workspace and opens it; **No** opens the project as before. Turn the
question off in **Settings > Catalog** or the Catalog menu.

## Package layout

`manifest.json` (per-file size and sha256), `thumbnail.png`, and `project/` (`<Name>.qgz`, `Default.gpkg`,
`Toolbox/`, `Layouts/`, `resources/`, `commondata/`).

## Notes and limits

* PostGIS, WFS and other remote layers stay connections unless you tick the option to copy a snapshot of their data.
* A layer that cannot be shared is listed in the analysis; nothing is silently dropped.

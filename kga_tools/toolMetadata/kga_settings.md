# KGA Settings and Check for Update

|  |  |
|---|---|
| **Algorithm ID** | none: KGA Toolbox menu windows (not Processing algorithms) |
| **Group** | KGA Toolbox menu > Settings, Check for Update |
| **Source** | `kga_tools/gui/settings_dialog.py`, `kga_tools/core/kga_settings.py`, `kga_tools/gui/update_dialog.py`, `kga_tools/core/update_check.py` |
| **Icon** | `icons/checkForUpdate.png` |
| **Type** | Dialogs. Settings are stored in the QGIS profile (`KGA/settings/`), not in the project |

## Settings

**Projects**

| Setting | Default |
|---|---|
| Default location for new workspaces | Documents\KGA Toolbox Project |
| Default CRS of new projects | EPSG:32648, WGS 84 / UTM zone 48N |
| Save tool output in the project default database and folder | On |
| Keep Shape_Length and Shape_Area up to date on project database layers | On |
| Add a basemap to every new project (and which) | On, Google Terrain Hybrid |
| Zoom new projects to a country (type to search) | On, Cambodia |

**Catalog**

| Setting | Default |
|---|---|
| Ask to convert a traditional project when it is opened | On |
| Save / Save As: ask each time, always as a workspace, or always as a `.qgz` | Ask |

**Appearance**

| Setting | Default |
|---|---|
| Match QGIS to the computer's light or dark mode | Off |
| Show selected features in ArcGIS Pro style: symbol kept, bright cyan outline | On |
| Show the Layers panel in ArcGIS Pro style: filled-triangle expanders, roomier rows, light-blue selection | On |
| Map tabs, with three sub-options (see [Map Tabs](maps_and_layouts.md)) | Off |
| Show the KGA start page instead of the QGIS welcome page; optionally hide the menu bar while it shows | On |
| Remember the toolbars and panels that are enabled, with **Remember current layout** and **Reset toolbars and panels** | Off |

## Start page

Shown when no project is open. **New:** Map, Workspace, Import Package, and templates from the profile's
`project_templates` folder. **Open:** recent projects as cards, with find, sort, grid or list, and a "..."
menu (Open, Pin, Show in folder, Copy path, Remove). Turn it off for the standard QGIS welcome page.

## Shape_Length and Shape_Area

On line and polygon layers in the project databases, these fields are added and recalculated whenever a
feature is created or edited, as a File Geodatabase does. Values are planar, in the units of the layer's CRS.
**Layer > Add Shape_Length / Shape_Area Fields** does it for the selected layer on demand.

## Check for Update

Opens a window that asks api.github.com for the latest release of the plugin's repository and says whether
yours is current. Only when you open it; no project data is sent.

## Notes and limits

* New-project settings apply to new projects only; an opened project is never changed.
* The basemap needs an internet connection.
* The cyan selection and Pro Layers panel styles can be turned off to get the standard QGIS look back.

# Open Feature Attribute

|  |  |
|---|---|
| **Algorithm ID** | none: a button on the KGA Editor toolbar (not a Processing algorithm). The Help (`?`) button in the pane opens `https://khmergrs.com/docs/qgis/open_feature_attribute` |
| **Group** | KGA Editor toolbar, right after Create Features |
| **Source** | `kga_tools/editor/attributes/` (`controller.py` button and docking, `panel.py` pane, `fields.py` field list and editors, `layer_browser.py` Layers tab, `geometry.py` Geometry tab, `select_tool.py` map tool) |
| **Icon** | `editor/attributes/icons/attributes.svg` |
| **Type** | Interactive dockable pane. Needs QGIS 3.44 or newer, like the rest of the KGA Editor toolbar. Works with Edit Session On or Off. |

## Overview

A docked pane inspired by the attribute-editing workflow in desktop GIS such as
ArcGIS Pro. Select a feature on the map and type into its attributes right
there: no attribute table, no hunting for the row.
The pane docks on the right, in the same tab group as the Processing Toolbox and
KGA Geoprocessing.

## Parameters

| Control | Meaning |
|---|---|
| **Selection tab** | The selected features, grouped by layer, from any selection tool. Click a feature to edit it (it is outlined on the map). Click the layer line to edit all its selected features at once. |
| **Layers tab** | Choose a layer, then pick any feature in the list of all its features. The arrows step to the previous or next feature; the play button steps through them automatically; its arrow sets what happens on the map (nothing, Flash, Pan To, Zoom To). Browsing never changes the map selection. |
| **Change the selection.** | The pane's own map tool. Click a feature or drag a rectangle; Shift adds, Ctrl removes, N picks the next overlapping feature. The arrow offers Rectangle, Polygon, Freehand, Circle and Line. |
| **Attributes tab** | The fields of the chosen feature. Click a value to type; Enter or Tab saves and moves to the next editable field; Esc cancels. Domains and booleans use a drop-down, dates a date picker. Empty means `<Null>`. Read-only fields are grey. |
| **Geometry tab** | The vertices of one feature, grouped by part and ring. Edit X, Y (Z, M) in place; the vertex is marked on the map. The copy button in the header copies the selected vertex (or all vertices) as tab-separated Vertex, X, Y columns. |
| **Auto Apply** | On (default): each value goes into the layer's edit buffer as soon as you finish typing it. Off: **Apply** and **Cancel** appear, and pending values are also applied when you pick another feature. |

## How to use

1. Click **Open Feature Attribute** on the KGA Editor toolbar (or KGA Toolbox >
   KGA Editor). The pane opens and the selection tool is armed.
2. Click a feature on the map, or right-click a feature and choose **Attribute**
   (above Copy Coordinate) to open the pane on it.
3. Click a value, type, press Enter.
4. Save with **Save Edits** (KGA Editor toolbar, when Edit Session is On) or
   QGIS's Save Layer Edits. Ctrl+Z undoes a value.

## Notes and limits

* If the layer is not in edit mode the pane starts editing it; nothing is saved
  until you save.
* Wrong input (letters in a number, text longer than the field, a broken layer
  rule) turns the row red and is not written.
* Editing several features at once shows a value only when all of them share it;
  differing values are blank, and a value you type is set on every feature.
* A layer whose source cannot be edited is shown read-only.
* Very large selections list the first 1000 features per layer; the count beside
  the layer is the real total.

*Not affiliated with or endorsed by Esri. ArcGIS is a trademark of Esri.*

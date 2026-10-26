# Construct Polygons

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/polygons.py` (PolygonTool) |
| **Icon** | `kga_tools/icons/constructPolygon.png` |
| **Also as a window** | [construct_polygon](../construct_polygon.md) (`kga:construct_polygon`) |

## Overview

Builds polygons from the selected lines that enclose an area, laid out like the Construct Polygons pane of ArcGIS Pro.

## How to use

1. Select the lines. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the **Template**: the editable polygon layer that receives the polygons.
3. Set the **Cluster Tolerance**, then press **Construct**.

## Options

| Option | Meaning |
|---|---|
| **Cluster Tolerance** | Line ends that stop short of another line by no more than this are joined to it. |
| **Use existing features in target** | Lets the outlines of polygons already in the template layer close areas too, and leaves out any polygon that is already there. |

## Notes and limits

* The lines are joined where they cross or meet and every closed area becomes a polygon. Lines that do not close an area are ignored.
* New polygons take the template's default attribute values and are selected afterwards.
* One undoable edit.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

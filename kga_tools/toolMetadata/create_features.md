# Create Features

|  |  |
|---|---|
| **Algorithm ID** | none: a button on the KGA Editor toolbar (not a Processing algorithm). The Help entry in the pane's view menu opens `https://khmergrs.com/docs/qgis/create_features` |
| **Group** | KGA Editor toolbar, in the place Construction Sketch used to have |
| **Source** | `kga_tools/editor/create/` (`controller.py` button and docking, `panel.py` pane, `templates.py` templates, `tools.py` tool list, `properties.py` template properties) and `kga_tools/editor/sketch/construction_tool.py` (the map tool) |
| **Icon** | `icons/editing/createFeature.png` |
| **Type** | Interactive dockable pane. Needs QGIS 3.44 or newer, like the rest of the KGA Editor toolbar. The button is enabled only while Edit Session is On; turning it Off closes the pane. |

## Overview

A docked pane in the style of ArcGIS Pro's Create Features pane. Every point,
line and polygon layer that can take new features lists its **templates**; pick
one, pick a construction tool, and draw on the map. New features get the
template's attribute values and look. The pane docks on the right, in the same
tab group as the Processing Toolbox and the Catalog.

## Parameters

| Control | Meaning |
|---|---|
| **Templates tab** | One group per layer that is switched on in the Layers panel (switching a layer or its group off removes it from the list). A Categorized layer has one template per category, a Rule-based layer one per simple rule, any other layer a single template. |
| **Favorites tab** | Templates you starred (right-click > Add to Favorites). Saved with the project. |
| **Search** | Matches the template name, layer name, description and tags. |
| **Funnel button** | Show all templates, or only point, line or polygon templates. |
| **View button** | Group by Layer, Geometry Type, Tag or nothing; collapse or expand all groups; Help. |
| **Construction tools** | Shown under the picked template. Point; Line, Right Angle Line, Freehand Line; Polygon, Rectangle, Right Angle Polygon, Circle, Ellipse, Freehand Polygon. |
| **Blue arrow** | Template properties: name, description, tags, default tool, default attribute values. |
| **Green strip** | "Create <layer> completed" after each new feature. |

## How to use

1. Turn **Edit Session** On, then click **Create Features** on the KGA Editor toolbar. The pane opens.
2. Click a template. Its construction tools appear under it and the first one is armed.
3. Click on the map. Right-click for the construction menu (Parallel, Perpendicular,
   Direction, Distance, Absolute X,Y, Delta X,Y, ...). Double-click, Enter or F2 finishes a
   line or polygon; Rectangle, Circle and Ellipse finish by themselves; Freehand finishes when
   you release the mouse.
4. Use the **sketch toolbar** that floats over the map: change the segment type (Straight, Right Angle,
   Intersection, Line-Circle, Circle-Circle, Midpoint, Bezier, Tangent Curve, End Point Arc, Three-Point Arc, Trace), drag vertices with the
   Pointer or the whole sketch with Move Sketch, and press **Finish** or **Cancel**. The gear moves the
   toolbar to the bottom, top, left or right of the map.
5. Save with **Save Edits** (KGA Editor toolbar, when Edit Session is On) or QGIS's Save Layer
   Edits.

## Notes and limits

* The pane draws on the template's layer; it never changes the active layer.
* Creating a feature starts editing the layer when it is not in edit mode.
* A category with no value is listed as <Null> and leaves the field empty; graduated renderers give one
  template for the layer.
* Template edits are stored in the layer (so they travel with the project); favorites in the project.
* Arcs and Bezier curves are saved as many short straight segments (no true curve geometry).
* Not supported: "Avoid overlap", topological editing, Z values (saved as 0).
* Closing the pane puts the tool away; switching to another tab does not.

*Not affiliated with or endorsed by Esri. ArcGIS is a trademark of Esri.*

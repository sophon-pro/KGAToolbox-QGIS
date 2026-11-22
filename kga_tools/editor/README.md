# KGA Editor

The second KGA toolbar: **Edit Session**, **Create Features**, **Modify Features** and **Open Feature Attribute** in one place.
**Quick Labeling** and **Quick Symbology** (below; their code still lives here) moved to the **KGA Layer** toolbar in
1.1.0 (`gui/layer_toolbar.py`), beside the Layer Bag and the Bags.
Needs QGIS 3.44 or newer; on older QGIS the rest of KGA Toolbox works and this toolbar is
simply not added (the reason is written to the KGA Toolbox log).

Show or hide it from **KGA Toolbox > KGA Editor > KGA Editor Toolbar** (or right-click any
toolbar). The same commands are in the **KGA Toolbox > KGA Editor** menu.

> **Edit Session starts Off.** Nothing about how QGIS edits changes until you click
> **Edit Session: Off** to turn it On. The choice is remembered between sessions.
> Create Features is greyed out until the switch is On; switching Off closes its pane.

## Edit Session

Select and edit on the map without choosing a layer first, and without Start/Stop Editing.

* **On / Off switch** - On: the layers you allow are always in edit mode (pencil in the Layers
  panel). Off: back to normal QGIS; you choose Save / Discard / Cancel if edits are pending.
  Turning it On also **switches snapping on with every snapping type** (vertex, segment, area,
  centroid, middle of segments, line endpoints), including when QGIS starts or a project is opened
  with the switch On. Turning it Off does not change snapping back.
  It also **opens the Editable Layers panel**, so you can see at once which layers are editable and
  selectable (every layer at first).
* **Save button** - "All saved" (green tick) or "Save Edits (N layers)". Nothing is saved
  automatically. **Discard Edits** throws the unsaved edits away (asks first). Layers stay editable.
* **Select (all layers)** - click or drag on the map; selects from every visible, selectable layer.
  Shift = add, Ctrl = remove, Shift+Ctrl = select from the selection, **N** = next overlapping
  feature, click empty map = clear. The **Clear Selection** button next to it clears the selection
  in all layers. These two buttons work whether Edit Session is On or Off; the Delete key in the
  Select tool only deletes while Edit Session is On.
* **Overlapping features** - after a plain click that could have picked several features, a small
  button appears beside the click. Open it for a list grouped by layer and choose a row to switch the
  selection to that feature (**N** does the same, one step at a time). It goes away when you pan, zoom,
  click again, select with a rectangle, or change the selection another way. Open Feature Attribute uses
  the same tool.
* **Delete Selected (all layers)** acts on every editable layer at once (only layers shown on the map).
* **Move**, **Split** and **Edit Vertices** are no longer on the toolbar: they are tiles of the
  **Modify Features** pane (Move = the old Move Selected; Split = Split (all layers); Edit Vertices =
  QGIS's Vertex Tool in its All Layers mode).
* **Five drop-down buttons** hold all of QGIS's advanced editing tools - Move / Rotate / Scale,
  Reshape, Rings and Parts, Split and Merge, Point Symbols - plus the Advanced Digitizing panel
  switch. Each button remembers the tool used last.
* **Layer under the mouse** - QGIS's own editing tools act on the editable layer you click, whatever
  layer is highlighted (never other plugins' tools; never while a two-click operation is running;
  never on Ctrl/Alt+click). Turn it off in Options.
* **Editable Layers panel** - Editable and Selectable checkboxes per layer, saved with the project.
  Right-click for "Make this the only editable layer", save or discard one layer, all / none.
* **Options** (wrench): newly added layers editable, save edits when saving the project, confirm
  before saving, confirm before discarding, selecting makes the layer active, layer-under-mouse.

Good to know

* Unsaved edits live in memory until you click Save. GDAL tools and other programs read the file,
  so save first.
* Two layers on the same data source are never edited together; tick the copy to move editing to it.
* If you switch editing off with QGIS's own Toggle Editing, Edit Session switches it back on.
  Use the Editable Layers panel instead.
* A QGIS project transaction mode (Project Properties > Data Sources) takes over editing; Edit
  Session stays out of the way. To change that mode, turn Edit Session Off first.
* The layer filter is greyed out in QGIS while a layer is being edited: right-click the layer >
  **Filter (pause editing)...**.
* Projects saved with the stand-alone Edit Session plugin keep their editable / selectable choices.

## Create Features

A docked pane in the style of ArcGIS Pro's Create Features: pick a template, pick a construction
tool, draw. The button sits where Construction Sketch used to be; the pane opens on the right, in
the same tab group as the Processing Toolbox and Catalog.

* **Templates tab**: one group per layer that is switched on in the Layers panel (points, lines and polygons that can take new features; switch a layer or its group off and it leaves the list),
  listed A-Z. A layer drawn with a *Categorized* renderer gets one template per category, a
  *Rule-based* layer one per simple rule; any other layer gets a single template. New features take
  the category's value and symbol. A category with no value is listed as <Null> and leaves the field empty.
* **Pick a template**: click it. Its construction tools appear underneath and the first one is armed
  on the map. Click another tool to switch (an unfinished sketch is discarded). The pane does not
  change the active layer: it draws on the template's layer.
* **Tools**: Point | Line, Right Angle Line, Freehand Line | Polygon, Rectangle, Right Angle Polygon,
  Circle, Ellipse, Freehand Polygon. Rectangle has Horizontal / Grid Aligned (two clicks, default) and Square toggles; with Horizontal off it takes a side then its width. Circle takes two clicks, Ellipse three (centre, end
  of the major axis, minor axis); they finish by themselves. Right Angle makes every segment after
  the first turn exactly 90 degrees. Freehand: press, drag, release.
* **Right-click on the map** keeps the construction menu: Parallel, Perpendicular, Perpendicular to
  Last Segment, Direction, Deflection, Distance, Length, Direction/Distance, Absolute X,Y, Delta X,Y,
  Clear Constraints, Undo Last Vertex, Square and Finish, Finish Sketch, Delete Sketch. Double-click,
  Enter or F2 finishes; Backspace undoes a vertex; Esc clears a constraint or deletes the sketch.
  Rectangle, Circle and Ellipse offer Absolute X,Y, Delta X,Y and Direction/Distance (Circle also
  Distance for an exact radius).
* **Sketch toolbar**: while a template's tool is armed, a dark toolbar floats over the map at the
  centre of the bottom edge (the gear at its corner moves it to the top, left or right edge; the
  choice is remembered). Lines and polygons: **Straight Segment** | **construction** list (Right
  Angle, Intersection, Line-Circle, Circle-Circle, Midpoint) | **curve** list (Bezier, Tangent Curve, End Point Arc, Three-Point
  Arc) | **Trace** button | **Pointer** (add vertices, drag one to move it) and **Move Sketch**
  (drag the whole sketch) | **Finish** and **Cancel**. A button with a small arrow opens its list; the
  method you pick becomes the button's face. Points get Point and the constructions only; Rectangle,
  Ellipse get Finish and Cancel only; Circle adds two buttons, Centre and Radius (default) and Three-Point Circle. Intersection takes two lines (click an edge, or click a point and move to aim the line; the blue dot
  previews the vertex), Line-Circle a line and a circle (click its centre, move to set the radius), Circle-Circle two circles; the vertex goes at the hit nearest the cursor. Midpoint: click the two ends of a segment (a dashed line and a blue dot follow the cursor) and the vertex goes at its middle. Bezier: press at the next vertex and drag to pull its handle (the next curve leaves along
  it). Three-Point Arc: click a point on the arc, then the end - or press at the end point and drag to bow the curve. End Point Arc: click the end, then move to set the bow and click; R types a radius. Trace follows the
  edges of the lines and polygons on the map. Esc or a right-click drops a construction or arc that is
  waiting for its next click.
* **Green strip** "Create <layer> completed" after each new feature; the X dismisses it.
* **Search, filter, view** (top row): search by template, layer, description or tag; the funnel
  shows all templates or one geometry type; the view button groups by
  Layer, Geometry Type, Tag or nothing, and collapses or expands all groups.
* **Favorites tab**: right-click a template > Add to Favorites. Saved with the project.
* **Template properties** (blue arrow on the picked template): name, description, tags, default
  tool, and default attribute values. Saved in the layer, so they travel with the project. A value
  that does not fit its field blocks Save.
* The pane stays open while you work. Closing it puts the tool away; switching to another tab does
  not. Creating a feature starts editing the layer if it is not in edit mode (save with Save Edits
  or QGIS's Save Layer Edits). The attribute form opens as usual unless your layer or QGIS suppresses it.

Details

* **Snapping** follows your QGIS snapping settings, including the sketch's own vertices, edges
  and middles.
* **Angles**: azimuth is degrees clockwise from north (`45`, `45.5`, `45 30 15`, `45-30-15`,
  `N45E`); deflection + turns right, - turns left. Distances are true metres (the layer CRS if
  projected, otherwise the UTM zone at the map centre).
* **Points**: click to add; right-click for Absolute X,Y, Direction/Distance or Delta X,Y from the
  last point.
* Not supported: "Avoid overlap", topological editing, curved segments, Z values (saved as 0),
  graduated renderers (one template for the whole layer).

## Modify Features

A docked pane in the style of ArcGIS Pro's Modify Features: a search box, **All Tools** and **Favorites**
tabs, and collapsible categories of icon tiles (a grid, or a list with the buttons at the right of the tabs). The button follows the Edit Session switch like Create
Features, and the pane opens in the same tab group on the right.

* **32 tools** in six categories: Alignment (Move, Rotate, Scale, Move To), Reshape (Edit Vertices, Reshape,
  Replace Geometry, Extend or Trim, Fillet, Generalize, Line Intersection, Reverse Direction, Change Line
  Length), Divide (Split, Divide, Clip, Explode, Planarize), Construct (Merge, Buffer, Copy Parallel,
  Construct Polygons, Offset, Points On Line, Points On Polygon Boundary, Copy Paste), Attributes (Sequential Numbering, Transfer Attributes) and COGO
  (Traverse, Update COGO, Cul-de-sac, Split Into COGO Lines).
* **Click a tile** to open the tool inside the pane (options, buttons, a back arrow) and arm its map tool.
  Pages that act on a selection show a **Change the selection.** banner and the list of selected features.
* **Favorites**: right-click a tile > Add to favorites, and the tool gets a button on this toolbar (drag to
  reorder; right-click > Remove from favorites). The list is kept in your QGIS settings.
* Move, Rotate and Scale: a plain click selects, a drag transforms. Move To takes absolute, delta, or
  direction and distance values. Edit Vertices lists the vertices of the selected feature to edit X and Y.
* Edits go to the edit buffer; save with the toolbar's Save button.

## Open Feature Attribute

A docked pane for editing attributes, inspired by the attribute-editing workflow in desktop GIS
such as ArcGIS Pro: select a feature on the map and type into its attributes, with no attribute
table and no hunting for the row. The button is right after
Create Features. It does not depend on the Edit Session switch.

* The pane opens on the right, in the same tab group as the Processing Toolbox and KGA Geoprocessing.
  Opening it also arms the blue **Change the selection.** tool; its arrow offers Rectangle (click or
  drag), Polygon, Freehand, Circle and Line. Shift adds, Ctrl removes, N = next overlapping feature.
* **Selection tab**: the selected features, grouped by layer, from any selection tool. Click a
  feature to edit it (it is outlined on the map). Click the layer line to edit all of its selected
  features at once; values that differ are blank, and a value you type goes to every feature.
  Right-click: Flash, Zoom To, Pan To, Clear, Select only this.
* **Layers tab**: every layer; pick one to edit its selected features.
* **Attributes**: click a value to type; Enter or Tab saves and moves to the next editable field,
  Esc cancels. Domains (value maps), booleans and dates get a drop-down or date picker. Empty = `<Null>`.
  Read-only fields are grey. Wrong input (letters in a number, text too long, a broken layer rule)
  turns the row red and is not written.
* **Geometry**: the vertices of the selected feature; edit X, Y (Z, M) in place; the vertex is marked
  on the map.
* **Auto Apply** (on): every value goes into the layer's edit buffer at once. Off: **Apply** /
  **Cancel** appear, and pending values are also applied when you pick another feature.
* **Typing a value turns Edit Session on.** If the switch is Off when you confirm a value (Enter, or
  Apply with Auto Apply off) or edit a vertex, the pane turns Edit Session On first - the same as
  clicking the toggle - and a message tells you so: Save Edits writes the change, Discard Edits
  throws it away. A rejected or unchanged value does not turn it on. Browsing and selecting never do.
* Save with **Save Edits** or QGIS's Save Layer Edits; Ctrl+Z undoes a value.

_Not affiliated with or endorsed by Esri. ArcGIS is a trademark of Esri._

## Quick Labeling

One button on the KGA Layer toolbar, right after Layer Bag, that labels the active layer with a single field.

* **Click** to switch it on: the active layer is labelled with a random field (a text field when there
  is one) using the font, size and colour you used last.
* **Layer list.** The window opens with a **Layer** checkbox and list above Field (untick the checkbox to hide the
  list; the window still follows the Layers panel, and the choice is remembered). With Map Tabs on, the list holds only the layers of the map you are on, not those of the
  other maps. It follows the layer selected in the
  Layers panel (a layer that has no label yet is shown greyed out; selecting a layer never labels it by itself).
  Pick another layer in the list to quick-label it without selecting it in the panel; the window then edits
  that layer. A layer whose labels come from Layer Properties or Callout Label Studio is not replaced: its
  settings stay greyed out until you turn those labels off.
* **The button is a switch for the layer's labels, whoever set them.** It is pressed whenever the active
  layer shows labels - from this button, from Layer Properties > Labels (or the Layer Styling panel) or from
  Callout Label Studio - and it follows them when they are switched on or off there. **Click while it is
  pressed** to turn the labels off: a quick label gives back the labels the layer had before; labels from
  Layer Properties or Callout Label Studio are only switched off and their settings stay on the layer, so
  you can turn them on again in the Labels tab. A Callout Label Studio panel that is open is told the
  labels were removed on purpose and does not put them back. Click once more for a quick label.
* **Right-click, only while a quick label is on**, opens a small window: the **field** (or the ε button to write an
  expression), **font**, **style** (default Regular), **size** and **colour**, and a **Halo** row (tick the box, then
  choose the halo size and colour; white, 1 pt by default; off until you tick it), then a **Position**
  drop-down whose choices follow the layer's geometry: points (Around point, Above, Below, Left, Right,
  Over point, Cartographic), lines (Parallel, Above line, Below line, On line, Curved, Horizontal) and
  polygons (Around centroid, Over centroid, Horizontal, Free (angled), Along perimeter, Outside polygon).
  Below it, **Offset** (in points) pushes the label away from the feature; it is greyed out for positions
  with nothing to offset (Over point, On line, Horizontal, Free, Over centroid).
  Point layers also get **Offset X** and **Y** (points; X right, Y up) to shift a label placed Above, Below,
  Left, Right or Over the point; they add to the Offset and are greyed out for Around point and Cartographic.
  A **Drop shadow** box at the bottom adds a soft black text shadow (1.5 pt, blur 1.5 pt, 70 % opacity, 135°)
  for any geometry; it is remembered (`kga_editor/quick_label_shadow`).
  The last choice is remembered separately for points, lines and polygons. Every change applies to
  the layer as you make it; there is no OK button.
* **Numbers.** When the chosen field is numeric, four boxes appear under it: **Round to 2 decimals**,
  **Thousand separator** (commas: 9999.00 becomes 9,999.00; the decimals are fixed at 2 when Round is also
  ticked, otherwise the number keeps its own),
  **Add length unit** (drop-down: m by default, km, ft, mi) and **Add area unit** (m² by default, km², ha,
  ft², yd², ac, mi²). Each drop-down is enabled only while its box is ticked, and the two exclude each other. The boxes also
  follow the layer: a line layer greys out the area unit, a polygon layer greys out the length unit, a point
  layer greys out both (a box that is already ticked stays enabled so it can be unticked).
  The number is converted to the unit first, taking the field to be in metres (length) or square metres (area),
  as Shape_Length / Shape_Area are; e.g. km shows value x 0.001. Tick any of them and
  the label expression is written into the **Field box** for you - for example
  `round("Shape_Area",2)||'m'||char(178)` - so the expression button opens with it and there is nothing to
  type. Untick everything and the plain field comes back. An empty value shows no label, not a lone unit. If
  you edit the expression yourself in the expression builder, the boxes let go (they untick) and your
  expression is kept. The boxes are hidden for text fields; picking another numeric field applies the same
  ticks to it. Reopening the window shows the expression and the ticks.
  The superscript 2 is `char(178)` (the character ²). Use a Unicode font for the label: a legacy-encoded
  Khmer font such as Khmer MEF1 draws its own symbol for that character, so the label shows a stray letter
  instead of the 2.
* The button follows the active layer, so each layer keeps its own state, and the window follows you to
  the next layer that has a quick label. Right-clicking while the labels are not a quick label only explains
  how to get one.
* It works whether Edit Session is On or Off, and never edits data. Layers without attributes (and tables
  or rasters) cannot be labelled, so the button is greyed out for them.
* It is an ordinary single-label setting, so Layer Properties > Labels shows and can refine it. A layer
  saved in a project reopens with the button pressed; switching off then simply turns its labels off.

## Quick Symbology

A button right after Quick Labeling (palette icon) that restyles the layer selected in the Layers panel
in a small window. Changes apply as you make them; there is no OK button. Click the button again to close
the window, which follows the active layer.

* **Layer list** at the top, like Quick Labeling's: it follows the layer selected in the Layers panel, and picking
  another layer there restyles that layer without selecting it in the panel. Untick the **Layer** checkbox to hide
  the list (the window still follows the panel; the choice is remembered).
  With Map Tabs on it lists only the layers of the current map.
* **Single symbol** (the layer's normal state): **Fill Color**, **Outline Color**, **Outline width** (in
  points) and a **Preview** of the symbol. Line layers show just Color and Width.
* **Unique Value symbol** switch: turns the layer into one class per value of a field. The window shows the
  **Field** (or the ε button to write an expression), a **Color Scheme**, and the classes as Class | Value |
  Label. Switching it off gives back the single symbol the layer had.
  * The green **+** opens **Select values to add**: the values that are not classes yet, each with the colour
    it would get. Pick some (Ctrl or Shift for several) and press **Add**: they become classes and the window returns to
    the class list. The back arrow returns without adding anything. **Options** can select all, clear the
    selection, or add every value.
  * The blue **down** and **up** arrows move the selected classes. **All other values** is always last and
    cannot be moved or removed. Delete (or the right-click menu) removes the selected classes.
  * Double-click a label to rename it, or a swatch to edit that class's symbol.
* **Editing one class**: when the layer already uses unique values and you select one of its classes in the
  Layers panel, the button opens the window straight on that class's symbol (fill, outline, width, preview);
  the back arrow returns to the class list.
* **Drop shadow** checkbox (under the preview, and under the class list): draws a soft shadow under the whole
  layer's shapes, the same effect as Layer Properties > Symbology > Draw effects. It keeps working when you
  switch between single symbol and unique values or change the field. A drop shadow you already set up in Layer
  Properties keeps its settings. It is not shown while editing one class or choosing values to add.
* **Symbol Bag** button (right of the colour buttons on the single-symbol page): opens the Symbol Bag and
  hands it this window's layer, or the class being edited. Double-click a symbol in the bag and it is applied there
  (not to whatever is selected in the Layers panel) and shown here at once. Closing this window releases the bag.
* Up to 100 classes are made automatically; if the field has more values, the rest are added with +.
* A layer drawn with another renderer (graduated, rule-based ...) is not changed until you either turn
  Unique Value symbol on or press **Use a single symbol**. Symbols built from layers other than simple
  fill, marker or line only take a fill colour here; use Layer Properties for the rest.
* It works whether Edit Session is On or Off, and never edits data.

## Stand-alone plugins

If the stand-alone **Edit Session** or **Construction Sketch** plugin is installed, KGA Toolbox
does not add this toolbar (two copies would give duplicate buttons) and says so on the message
bar. Uninstall the stand-alone plugin(s) to use the KGA Editor.

## For developers

* Code: `editor/controller.py` (toolbar and actions), `panel.py` (Editable Layers panel and
  dialogs), `manager.py` (edit-mode bookkeeping, no UI), `selection.py`, `edit_tools.py`,
  `follow.py` (layer under the mouse), `advanced.py` (drop-down groups), `sketch/` (the construction map
  tool), `modify/` (Modify Features: `controller.py` button and pane docking, `panel.py` tiles, `tools.py` the tool list, `favorites.py`, one module per tool family), `overlap_switcher.py` (the switch-selection popup),
  `create/` (Create Features: `controller.py` button and docking, `sketch_toolbar.py` floating sketch toolbar, `panel.py` pane, `templates.py`
  templates from the symbology + saved edits, `tools.py` tool list, `properties.py` template
  properties page), `attributes/`
  (Open Feature Attribute: `panel.py` pane, `fields.py` model/editors, `geometry.py`, `select_tool.py`),
  `quicksymbol/` (Quick Symbology: `engine.py` renderer logic, `dialog.py` the window and its three pages,
  `widgets.py` switch and preview, `controller.py` the button),
  `quicklabel/` (Quick Labeling: `engine.py` labeling logic, `dialog.py` the small window, `controller.py` the button).
* Loaded by `kga_plugin.py: init_editor`, wrapped so a failure never stops the rest of KGA Toolbox.
* QSettings group `kga_editor/`; layer custom properties `edit_session/editable` and
  `edit_session/selectable` keep the stand-alone plugin's names on purpose. Create Features stores
  template edits in the layer property `kga_create/templates` and favorites in the project entry
  `kga_create/favorites`.
* Tests: `tests/editor/` (`test_editor.py`, `test_create.py`, `test_attributes.py`, `test_modify_pane.py`) and `tests/overlap_switcher/` (QGIS 3.44 interpreter).
* Origin: Edit Session and Construction Sketch were written by a KGA co-founder and merged into
  KGA Toolbox 0.3.0.

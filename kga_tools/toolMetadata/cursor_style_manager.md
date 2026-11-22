# Cursor Style Manager

|  |  |
|---|---|
| **Algorithm ID** | none: a panel opened from the KGA Toolbox menu (not a Processing algorithm) |
| **Group** | KGA Toolbox menu > Map Cursor |
| **Source** | `kga_tools/gui/cursor_style/` |
| **Icon** | `icons/navigation/cursorStyleManager.png` |
| **Type** | Dock panel |

## Overview

Choose, customise, preview and save the **map cursor** used while digitizing, editing, selecting, identifying and
measuring. A clearer cursor makes it easier to snap to a vertex or follow a road on dark imagery.

Open it from **KGA Toolbox > Map Cursor > Cursor Style Manager**. It has no toolbar button. The same submenu has
**Use Custom Map Cursor**, the master on/off switch. You can give that switch a shortcut in
*Settings > Keyboard Shortcuts*.

A new profile starts on **QGIS Default**, so nothing changes until you pick a style. Click a preset and it becomes the
**main style**, used at once. Then pick a digitizing or selection tool and move over the map.

## The panel

The top of the panel (the on/off switch, status line, live preview and **Reset** / **Update map while editing**) is always
visible. Below it are four tabs: **Presets** (the presets and **My styles**), **Customize**, **Advanced** and
**Tools** (the cursor for each kind of tool).

| Area | Meaning |
|---|---|
| **On** | Master switch. Off means every tool shows its own QGIS cursor again |
| Status line | The active map tool and the style it is using. If it turns **amber**, that kind of tool is set to the QGIS cursor; **Use here** gives it the main style |
| **Live preview** | Draws the exact image the map will use, at true size, on a Light, Dark, Map or Split background. **Zoom** shows the pixels with the hotspot outlined in blue. Hover the preview to try the real cursor |
| **Update map while editing** | On by default: every change goes straight to the map cursor. Untick it to try changes in the preview first, then press **Apply changes**. **Reset** goes back to the selected style |
| **Presets** | 12 styles: QGIS Default, Standard Pointer, Crosshair, Precision Crosshair, Circle, Crosshair + Circle, Target, Dot + Ring, Precision Digitizing, High Visibility, Dark Background, Light Background. Each tile is half light, half dark, to judge visibility at a glance |
| **My styles** | **Save as…** keeps the current settings under a name (up to 100). **Save** overwrites the selected style. **More** renames or deletes a style, or restores the default settings |
| **Customize** | Cursor type (drawn, system crosshair, system arrow, QGIS tool cursors); shapes you can combine (crosshair, circle, inner ring, centre dot); main, outline and dot colours (**Auto** picks a black or white outline); size, arm length, circle diameter, dot size, line and outline thickness |
| **Advanced** | Centre gap (an open centre to see what is under the hotspot), inner ring, opacity, dashed lines, and scaling with the QGIS interface size |
| **Cursor for each kind of tool** | Which cursor each kind of map tool uses (below) |

A setting that cannot work is corrected for you, and the panel says what changed. For example, a cursor with no visible
part gets its crosshair back.

## A cursor for each kind of tool

Each kind of map tool has a drop-down under **Cursor for each kind of tool**:

* **QGIS cursor**: the tool keeps its own QGIS cursor.
* **Main style**: the style selected in the panel (its name is shown in brackets).
* **A preset or one of your saved styles**: that kind of tool always uses it, whatever the main style is.

For example, to have one cursor for digitizing and another for editing: design a cursor, **Save as…** "Digitizing
red", and pick it for **Digitizing**. Do the same for **Editing** with another style. Switching between Add Feature
and the Vertex Tool (or Create Features and Modify Features) then changes the cursor by itself.

While you edit the style that is selected in the panel, every kind of tool using it follows your changes at once. A
kind of tool set to a saved style uses the saved version until you press **Save**. Renaming a saved style keeps it
picked; deleting it moves those kinds of tool to the main style, and the panel says so.

| Kind of tool (default) | Includes |
|---|---|
| Digitizing (Main style) | Add feature/part/ring, split, reshape, shape tools, and the KGA Create Features sketch tool |
| Editing (Main style) | Vertex tool, move/rotate, simplify, delete ring/part, and the KGA Modify Features tools |
| Selection (Main style) | Select by click/rectangle/polygon/freehand/radius, and the KGA Editor select tool |
| Identify & measure (Main style) | Identify, measure line/area/angle, run feature action |
| Pan & zoom (**QGIS cursor**) | Pan, zoom in/out, so you keep the hand and magnifier |
| Other tools (QGIS cursor) | Map tools from Processing and other plugins |

The main map and extra 2D map views are both covered.

## How it works

Every QGIS map tool has a base cursor. The panel replaces only that base cursor, through QGIS's own API, after the
tool is switched on. So QGIS's short-lived feedback still works: the closed hand while dragging, wheel zoom, space-bar
pan and the busy cursor. When a tool is switched off, or the switch is turned off, or the plugin is unloaded, every
tool gets its own cursor back.

Settings and your styles are saved in the QGIS user profile under `KGA/cursor_style/`.

## Notes and limits

* **Tools that change their own cursor while in use** show their own cursor until you pick the tool again. Some KGA
  Modify Features tools do this when you hover a vertex or a handle. The panel does not fight a tool's own choice.
* **Zoom tool + Alt** (with Pan & zoom on) brings back the QGIS magnifier until you pick the tool again.
* **Outside the map** (layouts, 3D views, the georeferencer, dialogs) cursors are not changed.
* **Very large cursors** are limited to about 129 × 129 pixels and scaled down. Some remote-desktop clients and screen
  recorders show a plain pointer instead of a coloured cursor.
* Do not run the stand-alone *Cursor Style Manager* plugin at the same time; both would set the same cursors.

Ported from Cursor Style Manager 1.0.1 (GPL-2.0-or-later).

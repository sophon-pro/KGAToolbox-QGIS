# Tool Bag

|  |  |
|---|---|
| **Algorithm ID** | none: a floating panel on the KGA Layer toolbar and the KGA Toolbox menu (not a Processing algorithm) |
| **Group** | KGA Layer toolbar (own button), KGA Toolbox menu > Bags |
| **Source** | `kga_tools/gui/tool_bag/` (`plugin.py`, `panel.py`, `bubble.py`, `picker.py`, `registry.py`, `themes.py`, `add_dialog.py`) |
| **Icon** | `icons/interface/toolBag.png` |
| **Type** | Floating panel |

## Overview

A small panel that floats above the QGIS window and holds the tools you use most, like a pencil case beside
your map. Put tools from anywhere in QGIS into it (digitizing, selection, the Vector and Raster menus, plugin
tools, KGA tools, Processing algorithms) and run them from one place. The tools in the bag are the real QGIS
actions, so they behave exactly as they do on their own toolbar, including being greyed out when they need
something such as a layer in edit mode.

The button sits on the **KGA Layer** toolbar, and is the first entry of the Bags
submenu in the KGA Toolbox menu. It shows or hides the bag.

## Putting tools in the bag

* Click **+** in the bag's title bar, then click any QGIS toolbar button or menu item. It goes into the bag
  instead of running, and QGIS returns to normal. Click anywhere else, or press Esc, to cancel.
* **Shift + right-click** any button on a QGIS toolbar.
* **Settings (gear) > Add Tool from List...** searches every tool, including Processing algorithms.

Right-click a tool to take it out of the bag or move it. Drag tools to reorder them. Removing a tool from the
bag never removes or changes it in QGIS. If the plugin that provides a tool is switched off, the tool shows a
warning sign and comes back by itself when the plugin is on again.

## The bubble

Press **-** (minimize, between the gear and the cross) and the bag shrinks into a round bubble on the map,
like a chat head. Click the bubble and the bag opens out of it, right at the bubble's spot, with a short
animation; the bubble goes away while the bag is open. The small number on the bubble is how many tools are in
the bag. The cross hides the bag completely; the toolbar button brings it back.

| Setting | Where | Meaning |
|---|---|---|
| **Minimize to a bubble** | Bag Style, or gear > Bubble | When off, **-** simply hides the bag |
| **Position** | Bag Style (3 x 3 grid), or gear > Bubble > Bubble Position | Top, middle or bottom, each left, centre or right. The bag opens anchored to that spot (a top-left bubble opens a bag whose top-left corner is there) and stays inside the map |
| **Size** | Bag Style, or gear > Bubble > Bubble Size | 28 to 64 px, 40 by default |

Right-click the bubble for Open, Hide and the same options.

The bubbles of the [Symbol Bag and Layer Colors](symbol_bag.md) sit in the same column as this one, which
uses this position and size. Settings > Bubbles (position, size, spacing, colour, animation, sound) is the one
place that controls every bubble, bags and panels alike. The animation chooses how the bag opens and closes:
**Genie** (it pours into the bubble) or **Classic** (it shrinks into it).

## Layout

The bag is only as tall as its tools need, up to a fixed maximum (520 px); past that the list scrolls. Drag the
title bar to move it and the bottom-right corner to change its width.

| Setting | Meaning |
|---|---|
| **Show Tools As: List** | One row per tool with its name (the original design) |
| **Show Tools As: Grid, icons only** | Compact tiles |
| **Show Tools As: Grid, icons with names** | Icon with the name underneath; long names wrap to two lines. The full name is in the tooltip |
| **Columns** | 1, 2 or 3, in the list as well as the grid. The bag widens to fit |
| **Show Tool Names** | List only: off gives a slim icon-only bag |
| **Icon Size** | 16 to 40 px |

## Bag Style

Gear > **Bag Style...** opens a gallery of nine designs (Sky Blue, Peach, Mint, Lavender, Sakura, Lemon, Kraft,
Classic White and Midnight) and:

* **Background**: makes only the bag's fabric see-through, so the map shows behind the tools while the tools
  stay solid.
* **Fade all**: fades the whole bag, tools included.
* **Top**: how the bag hangs: Handle, Strap, Pin, Clip, Hook, Ring, Tape or None.
* **Compact**: tighter rows.

## Good to know

* The bag never takes keyboard focus, so Esc, Backspace and Enter keep working on the map while you digitize.
* Tools, position, size, layout, design and bubble settings are saved automatically in the QGIS settings and
  come back when QGIS starts.
* Rounded corners, the shadow and the top fixtures need a compositing window system (Windows, macOS, Wayland);
  classic X11 without one gets square corners.
* If the stand-alone **Tool Bag** (or Custom Tool Workspace) plugin is installed, the built-in one is not
  added and a message says so. Uninstall the stand-alone plugin to use the built-in one; its tools and
  settings carry over.
* Embedded toolbar widgets (the scale box, the snapping tolerance box) are not tools and cannot be added.

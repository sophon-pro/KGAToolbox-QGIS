# Copy-Paste Feature

|  |  |
|---|---|
| **Algorithm ID** | `kga:copy_paste_feature` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/copy_paste_feature.py` |
| **Icon** | `icons/copyPaste.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Copy* / *Paste Special* pair. Put features on the KGA
clipboard — from the layer's selection or by clicking them on the map — then
press **Paste Special** and decide where they land and what happens to their
attributes and geometry on the way in.

Why not the QGIS clipboard? `Edit > Paste Features As` writes a *new* scratch
layer, and pasting into an existing layer only works when the two schemas already
line up — values under a differently-spelled field name are dropped without a
word. This tool never drops a value quietly: every field that could not be
carried across is either mapped explicitly or named in the paste report.

It leads the Editing Tools menu because it is the one that puts a feature on the
map in the first place; the rest of the group works on features already there.

## Parameters

The algorithm takes no Processing parameters — running it opens the Copy window.
The controls below belong to the two windows.

### Copy window

| Control | Type | Default | Description |
|---|---|---|---|
| **Copy from** | Layer combo | the active layer | The source layer. |
| **Copy Selected** | Button | — | Puts the layer's selected features on the KGA clipboard. |
| **Copy by Clicking** | Toggle button | up | Click a feature on the map to copy it. Hold **Ctrl** or **Shift** to add to what is already on the clipboard. |
| **Clear** | Button | — | Empties the clipboard. |
| Clipboard box | Label | empty | What is currently held. The contents are outlined on the canvas. |
| **Paste Special…** | Button | — | Opens the paste window below. |
| **Paste Again** | Button | — | Repeats the last paste: same target layer, same field mapping. |
| **Close** | Button | — | Closes the window. |

### Paste Special window

| Control | Type | Default | Description |
|---|---|---|---|
| **Paste into** | Layer combo | — | The target layer. |
| **Attributes › Handling** | Combo box | `Match target fields by name` | `Match target fields by name`, `Map fields manually`, `Do not copy attributes (use the target defaults)`. |
| **Attributes › Name matching** | Combo box | `Names must match exactly` | `Names must match exactly`, `Ignore upper/lower case`, `Ignore case, spaces and underscores`. Used by the matching mode. |
| Mapping table | Table (`Target field`, `Type`, `Take value from`, `Fixed value`) | derived | In manual mode, pick a source field per target field — or `(fixed value)` and type one, or `(leave to the target default)`. |
| **Geometry › Reproject to the target layer CRS** | Checkbox | **ticked** | |
| **Geometry › Multipart into single** | Combo box | `Explode it into one feature per part` | Or `Skip the feature`. Only used when a copied feature is multipart and the target holds single-part geometry. |
| **After pasting › Select the pasted features** | Checkbox | **ticked** | |
| **After pasting › Zoom the map to them** | Checkbox | unticked | |
| **After pasting › Keep this window open after pasting** | Checkbox | unticked | |
| **Paste** / **Close** | Buttons | — | |

## How to use

1. Open **KGA Editing Tools > Copy-Paste Feature**. Close the Processing window
   once the pane appears.
2. Pick the **Copy from** layer.
3. Either select features and press **Copy Selected**, or press **Copy by
   Clicking** and pick them off the map (Ctrl/Shift adds).
4. Press **Paste Special…**, choose the **Paste into** layer.
5. Decide the attribute handling. Start with *Match target fields by name* and
   loosen the *Name matching* if the schemas differ only in spelling; drop to
   *Map fields manually* when they differ in substance.
6. Press **Paste**, then read the paste report: it names every value that had to
   change and every field that could not be carried.
7. Use **Paste Again** to repeat the same paste into the same target.

## Outputs

- The copied features written into the target layer, with attributes as mapped
  and geometry fitted to the target.
- A **paste report** naming every converted value and every unmapped field.
- The pasted features selected, and the map zoomed to them, if those boxes are
  ticked.

## Notes and limits

- **The target layer has to be in edit mode.** The tool offers to start editing
  if it is not.
- **The clipboard holds copies, not references.** A source layer closed, edited
  or rolled back after the copy cannot change what gets pasted.
- **Nothing is written outside an edit command.** One paste is one undo step.
- **Nothing is dropped quietly.** Values that will not fit the target field are
  converted where that is lossless and reported where it is not — never turned
  into a silent NULL.
- Geometry is fitted on the way in: reprojected, promoted to multipart or
  exploded into single parts, curves segmented, Z and M added or dropped. A
  target with no geometry column takes the attributes alone.
- Copying more than about 2,000 features at once is flagged: it is usually a
  slip, and the clipboard holds them in memory until cleared.
- The tool cannot run headless or inside a model.

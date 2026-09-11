# Clip

|  |  |
|---|---|
| **Algorithm ID** | `kga:clip_features` |
| **Group** | KGA Editing Tools |
| **Source** | `kga_tools/algorithms/clip_features.py` (base dialog in `kga_tools/gui/modify_dialog.py`) |
| **Icon** | `icons/clipFeatures.png` |
| **Type** | Interactive dialog — modeless, owns a map tool, needs the QGIS canvas |

## Overview

Replica of the ArcGIS Pro *Modify Features > Clip* pane. Pick the features that
define the cut — a road corridor, a reservoir footprint, a proposed canal — give
them a buffer distance if the cut is wider than they are, and then either take
that area out of everything around them or keep only that area.

Two things this pane insists on, because getting either wrong is expensive:

- **You choose what gets clipped.** Pro clips every editable layer, which is fine
  when one layer is open for editing and alarming when six are. The list is
  explicit, and every ticked layer is opened for editing before a single feature
  is touched — half a clip is worse than none of one.
- **The clipping features are never clipped.** When the corridor and the parcels
  live in the same layer, the selected features are skipped, so the corridor does
  not eat itself.

## Parameters

The algorithm takes no Processing parameters — running it opens the pane. The
controls below are the pane's.

| Control | Type | Default | Description |
|---|---|---|---|
| **Layer** | Layer combo | the active layer | Where the clipping features are picked from. |
| **Buffer distance** | Number + unit combo | `0.0` | How far beyond the selected features the cut reaches. Leave it at zero to clip with the features themselves. |
| **Clip** | Combo box | `Discard the area that intersects` | `Discard the area that intersects` — the clip is a hole; features lose the part inside it and a feature entirely inside is removed. `Preserve the area that intersects` — the clip is a cookie cutter; each feature is reduced to the part inside it. |
| **Layers to clip** | Checkable list | the layers already in edit mode | The layers whose features are clipped. Each ticked layer is opened for editing before anything is written. The list follows layers added to and removed from the project. |
| **Clip** | Toggle button | up | Puts the tool in hand. |
| **Reset** | Button | — | Sets the running count back to zero. |
| **Close** | Button | — | Closes the pane and gives the canvas back. |

## How to use

1. Open **KGA Editing Tools > Clip**. Close the Processing window once the pane
   appears.
2. Pick the **Layer** the clipping features live in.
3. Tick the **Layers to clip**. Check this list carefully — it is the difference
   between clipping one layer and clipping six.
4. Set a **Buffer distance** if the cut is wider than the features themselves —
   a 12 m road corridor from a centreline means 6 m here.
5. Choose **Discard** or **Preserve**.
6. Press **Clip**, then hover a feature to see the cut boundary in cyan and click
   to apply it. Drag a path across several to use them all.

## Outputs

- The ticked layers modified in place: features lose the overlap, or are reduced
  to it, depending on the mode.
- Features left with nothing after a *Discard* are removed.
- A status line reporting what each press changed.

## Notes and limits

- **Every ticked layer is opened for editing before anything is written.** If one
  of them cannot be opened, nothing is touched — a half-applied clip across six
  layers is far worse than a refusal.
- **The clipping features themselves are never clipped**, so a boundary sharing a
  layer with its neighbours does not eat itself.
- **Each layer's changes are one undo step** per press.
- The cut boundary is drawn in cyan before anything is written, so you can see
  what a buffer distance actually produces.
- The pane stays open while you work, and it owns the map tool.
- The tool cannot run headless or inside a model.

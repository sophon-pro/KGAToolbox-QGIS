# Cul-de-sac

|  |  |
|---|---|
| **Category** | Modify Features > COGO |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/culdesac.py` (CulDeSacTool) |
| **Icon** | `kga_tools/editor/modify/icons/cul_de_sac.svg` |

## Overview

Adds a turnaround at the end of a street, laid out like the Cul-de-sac pane of ArcGIS Pro.

## How to use

1. Select a line. The **Change the selection.** banner switches to select mode: click or drag on the map (Shift adds, Ctrl removes, empty map clears). The list under it shows what is selected.
2. Choose the **Template** (the editable layer the outline is made in) and the **Side**: **Start of line** or **End of line**. Black arrows on the selected lines show which way they run.
3. Set **Street width**, **Radius** and **Return radius** (each in its own distance unit), tick **Show Preview**, then press **Create**.

## Options

| Option | Meaning |
|---|---|
| **Street width** | The street's two edges (kerbs) lie half this width either side of the line. |
| **Radius** | Radius of the turnaround circle, centred on the end of the street. |
| **Return radius** | Radius of the curve joining the circle to each kerb, tangent to both. |
| **Use Template** | Creates the outline in the template layer. Off: in the selected line's own layer. |

## Notes and limits

* The outline is one line (a closed ring in a polygon layer).
* Needs **Edit Session** On; only editable layers shown on the map are changed.

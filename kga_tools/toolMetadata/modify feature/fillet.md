# Fillet

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/reshape.py` (FilletTool) |
| **Icon** | `kga_tools/editor/modify/icons/fillet.svg` |

## Overview

Joins two lines with an arc, laid out like the Fillet pane of ArcGIS Pro.

## How to use

1. Click the first line, then the second. Both lines and the arc are previewed (the arc dashed).
2. With **Fixed Radius** off the mouse sets the radius: the arc touches the second line where you click.
3. Click to finish.

## Options

| Option | Meaning |
|---|---|
| **Fixed Radius** | Rounds every corner with the same radius, typed in the project's distance unit. Unticked, the mouse sets the radius. |
| **Trim or extend the lines to the arc** | Trims or extends both lines to meet the arc. |
| **Add the arc as a new feature** | Adds the connecting arc as a new feature. If both options are off, nothing happens. |
| **Start over** | Forgets the first line (Esc or right-click does the same). |

## Notes and limits

* Only single-part lines (or polygon edges) can be joined.
* Click the part of each line you want to keep.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

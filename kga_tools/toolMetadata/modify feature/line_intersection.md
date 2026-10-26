# Line Intersection

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/reshape.py` (LineIntersectionTool) |
| **Icon** | `kga_tools/editor/modify/icons/line_intersection.svg` |

## Overview

Trims or extends two lines to meet at their corner, laid out like the Line Intersection pane of ArcGIS Pro.

## How to use

1. Click the first line (grey), then hover the second: the result is previewed, with a blue dot at the intersection.
2. Lines that cross are cut in two there, so two crossing lines become four. Lines that do not meet are extended until they do.
3. Press **Tab** to try another crossing when there are several, then click or press Enter.

## Options

| Option | Meaning |
|---|---|
| **Recalculate COGO attributes** | Rewrites the lines' COGO fields from their geometry afterwards. |
| **Create new lines when extending** | An extended line keeps its shape; the extension is added as a new line instead. |

## Notes and limits

* Only single-part editable lines can be used.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

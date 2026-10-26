# Reverse Direction

|  |  |
|---|---|
| **Category** | Modify Features > Reshape |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/reshape.py` (ReverseDirectionTool) |
| **Icon** | `kga_tools/editor/modify/icons/reverse_direction.svg` |

## Overview

Turns a line around, laid out like the Reverse Direction pane of ArcGIS Pro.

## How to use

1. Hover an editable line: arrows show the way it runs.
2. Click it to reverse it. Ctrl+Z undoes it.

## Options

| Option | Meaning |
|---|---|
| **Reverse topmost line** | Reverses the line on top where lines overlap. |
| **Choose from overlapping lines** | Where lines overlap, pick which one to reverse from a list. |
| **Choose component part of a multipart line** | Reverses only the part you click, not all parts. |
| **Recalculate COGO Direction attribute** | Rewrites the line's COGO fields from its new direction. |

## Notes and limits

* Only editable lines are listed.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

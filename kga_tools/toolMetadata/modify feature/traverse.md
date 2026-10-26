# Traverse

|  |  |
|---|---|
| **Category** | Modify Features > COGO |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/traverse.py` (TraverseTool) |
| **Icon** | `kga_tools/editor/modify/icons/traverse.svg` |

## Overview

Draws COGO lines from typed directions and distances, laid out like the Traverse pane of ArcGIS Pro.

## How to use

1. Choose the **Template**: the editable line layer the lines are made in.
2. Click the **start point** on the map (it snaps; a green square marks it) or type it.
3. Fill in the **Courses** table, one row per course: Direction, Distance and, for a curve, Radius. "Click to add new row." adds one. Each course starts where the one before ends.
4. Set the COGO settings, tick **Show Preview** to see the traverse in blue, then press **Create**.

## Options

| Option | Meaning |
|---|---|
| **Direction** | In the direction type and units of the COGO settings (north azimuth 45-30-00, quadrant N45-30-00E ...). |
| **Distance** | In the distance unit of the settings (map units when none is chosen). With a Radius it is the length of the chord. |
| **Radius** | Positive turns clockwise, negative counter-clockwise; empty = straight. |
| **One feature per course, with its COGO values** | Each course is a line carrying its Direction, Distance, Radius and ArcLength. Off, the traverse is one connected line. |

## Notes and limits

* The new lines are selected afterwards. One undoable edit.
* Needs **Edit Session** On; only editable layers shown on the map are changed.

# Open Error Inspector

|  |  |
|---|---|
| **Algorithm ID** | `kga:errorinspector` |
| **Group** | KGA Topology |
| **Source** | `kga_tools/algorithms/error_inspector.py` |
| **Icon** | `icons/errorInspector.png` |
| **Type** | Interactive window — modeless, needs the QGIS window |

## Overview

Opens the Error Inspector: a window listing every feature on every KGA Topology
error layer in the project, which stays in step as you fix errors and re-run
checks.

Finding errors is the easy half. Working through two hundred slivers one at a
time, zooming to each, fixing it, and re-running the check to see what is left —
that is the half this window is for. Select a row and the map zooms to that
error; press **Validate All** and every check that produced an error layer is run
again with the parameters it originally used.

It is a plain window rather than a dock, so it can be parked beside QGIS,
maximised, and left open while you edit.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's.

| Control | Type | Description |
|---|---|---|
| Error table | Table (`FID`, `Error Type`, `Layer Name`) | Every feature on every KGA error layer in the project. **Selecting a row zooms the map to that error** — no button press needed. |
| Status line | Label | What is currently listed, and any note about layers that cannot be re-run. |
| **Zoom to Error** | Button | Zooms to the selected row, for when you want to re-zoom without changing the selection. |
| **Refresh** | Button | Rebuilds the list by hand. It normally rebuilds itself. |
| **Validate All** | Button | Re-runs every check that produced an error layer, using the parameters it was run with. |

## How to use

1. Run **[Check Overlaps and Gaps](checkoverlapsandgaps.md)** or
   **[Check Points on Boundary Vertices](checkpointsonboundary.md)** first, with
   the inputs loaded **in the project** â€” see the note about repeatability below.
2. Open **KGA Topology > Open Error Inspector**. Close the Processing window once
   the inspector appears; move it beside QGIS so you can see the map.
3. Click a row. The map zooms to that error.
4. Fix it on the map â€” the list keeps itself up to date as you edit.
5. When you have worked through a batch, press **Validate All**. Every check is
   re-run and the list shows what is left.
6. Repeat until the list is empty.

## Outputs

Nothing is written by the window itself. **Validate All** re-runs the underlying
checks, which replace their error layers — so the list, and the map, show what is
left after your fixes.

A success message appears on the QGIS message bar when every check completes; a
failure in one check is reported without hiding the checks after it.

## Notes and limits

- **The list keeps itself current.** It follows layers added to and removed from
  the project and edits to the error layers, collapsing bursts of signals into
  one rebuild so the window does not fight you while you work.
- **Validate All needs the check to be repeatable.** Each error layer carries the
  algorithm id and parameters it came from. A check run over files picked
  straight off disk records no parameters — the layer ids would die with the
  Processing run — so those layers are named in the message instead. **Add the
  inputs to the project and run the check again** to make it repeatable from
  here.
- Overlap and gap layers come from the same topology check, so **Validate All**
  runs it once, not twice.
- It only lists **KGA** topology error layers — the ones written by
  **[Check Overlaps and Gaps](checkoverlapsandgaps.md)** and
  **[Check Points on Boundary Vertices](checkpointsonboundary.md)**. QGIS's own
  Topology Checker results are not picked up.
- The window survives the Processing run that opened it; closing the plugin
  closes it.
- The tool cannot run headless or inside a model.

# Generate River or Stream for ROI

|  |  |
|---|---|
| **Algorithm ID** | `kga:generate_stream_roi` |
| **Group** | KGA Irrigation Tools |
| **Source** | `kga_tools/algorithms/generate_stream_roi_alg.py` (dialog in `kga_tools/gui/stream_roi_dialog.py`, area picker in `kga_tools/gui/roi_picker.py`, engine in `kga_tools/core/streams.py` and `kga_tools/core/hydrology.py`, checks in `kga_tools/core/stream_check.py`, symbology in `kga_tools/core/stream_style.py`) |
| **Icon** | `icons/generateStream.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window and an internet connection |

## Overview

Extracts the river and stream network for any area of interest in one step. The
tool downloads the free **Copernicus DEM** (30 m or 90 m, from the AWS open-data
bucket — no account and no API key), cuts it to the area, fills its depressions,
routes the water downhill, and draws a stream wherever enough ground drains
through a cell. Each stream segment is numbered (Strahler order), measured, and
**graded for confidence**; an optional comparison with OpenStreetMap or your own
layer finds streams that only one network has.

The job runs on a background task, so QGIS stays usable. **One progress bar
covers all of it** — downloading and cutting the DEM, loading it, routing the
water, accumulating flow, tracing the streams, checking them and writing the
results.

## How much stream? (Detail)

A cell becomes stream when at least a certain area of ground drains through it.
That one number controls the whole network:

| Detail | Contributing area | Use it for |
|---|---|---|
| **Major rivers only** | 25 km² | The skeleton of the drainage. Needs a large area; below about 100 km² there may be nothing this big. |
| **Main streams** (default) | 5 km² | The streams a map would normally show. |
| **Detailed network** | 1 km² | Adds smaller tributaries; good for a catchment of tens of km². |
| **Very detailed** | 0.2 km² | Every gully that gathers water. Includes channels dry most of the year and more false streams on flat ground. |
| **Custom** | your value (0.01–5000 km²) | Anything in between. |
| **Match a drainage density** | target km of stream per km² (0.1–5) | Lets the tool pick the area. Natural catchments run roughly 0.5 (sparse) to 2 (dense). |

At 30 m, 1 km² is about 1,100 cells. A threshold below 3 cells is raised to 3, and
one that would make a quarter of the area "stream" is refused as a typo.

## Parameters

| Control | Meaning |
|---|---|
| **DEM** | Copernicus GLO-30 (about 30 m, the one to use), GLO-90 (about 90 m, for a very large area), or **Local DEM**: a raster layer from the project or a DEM file (GeoTIFF, IMG, ASC, VRT, HGT...). A local DEM is read from band 1 as elevation in metres, must cover the area, and needs no internet. If it is in a metric CRS and the output CRS is left on *UTM zone of the area*, its own grid is kept. No Copernicus attribution is added to its layers. |
| **Area** | **Current map extent**, **Extent of a layer**, **Boundary of a polygon layer** (the DEM is cut to the shape; optional *Selected features only*), or **Draw a rectangle or polygon** on the map. |
| **Detail** | See above. |
| **Include upstream area** | Off, 2, 5 or 10 km. Streams that rise outside the area are otherwise cut short at its edge and look smaller than they are. With a margin the DEM is downloaded for the area plus the margin, and the streams are cut back to the area afterwards. The saved DEM then includes the margin. |
| **Smooth the lines** | Rounds off the cell-by-cell steps (one round of corner cutting). `LENGTH_M` is measured along the cell path before smoothing. |
| **Symbology** | **By stream order** (blue, wider = higher order) or **By check confidence** (green High, amber Medium, red Low). |
| **Compare with** | **None**, **OpenStreetMap rivers and streams** (needs internet), or **A line layer of my own** (with optional *Selected features only*). |
| **Match distance** | How close the other network must be for a stretch to count as matched. Default about three DEM cells (90 m at 30 m). |
| **Output CRS** | **UTM zone of the area** (default; streams, slopes and catchment areas are measured in metres), **Current project CRS**, **WGS 84 as published**, or a **Specified CRS**. |
| **Output folder** | Where the DEM, streams and report go. Empty means a temporary folder, which QGIS deletes when it closes. |
| **Name** | Base name: `<name>_DEM.tif`, `<name>_streams.gpkg`, `<name>_report.txt`, and optionally `<name>_flow_accumulation.tif`. |
| **Also add the DEM to the map** | Adds the DEM with a pale elevation tint, below the vector layers. |
| **Also save the flow accumulation raster** | Contributing area in km² for every cell. Useful to judge the threshold or pick catchment outlets. Added to the map switched off. |

## How to use

1. Open **KGA Irrigation Tools > Generate River or Stream for ROI**.
2. Choose the area. For a drawn area, click *Draw rectangle* or *Draw polygon*
   and draw on the map.
3. Pick the detail level. Optionally add an upstream margin and a reference to
   compare with.
4. Pick an output folder, or leave it empty for a temporary one.
5. Click **Generate streams**. Click **Stop** to cancel; nothing is added to the
   map.
6. Read the report that opens when the run finishes (also under **Last report**).

## Outputs

`<name>_streams.gpkg` — layer `streams`, in the output CRS, one line per segment
between junctions:

| Field | Meaning |
|---|---|
| `STREAM_ID`, `NEXT_ID` | The segment, and the one it flows into (0 at an outlet). |
| `STRAHLER` | Strahler order: 1 for a source, two equal orders meeting make the next one up. |
| `LENGTH_M` | Length along the cell path, before smoothing. |
| `UP_KM2` | Ground draining to the end of the segment, in km². |
| `ELEV_UP`, `ELEV_DN` | Elevation at the start and end, from the depression-filled DEM. |
| `SLOPE_PCT` | Fall along the segment, in percent. |
| `FILL_M` | The most water the DEM had to pour into a depression to carry this stream across it. |
| `RELIEF_M` | How far the stream sits below the ground around it (7 × 7 cells). |
| `CONF`, `FLAGS` | Confidence grade and the reasons behind it — see below. |

`<name>_streams.gpkg` also holds a layer `stream_check` when a reference was used
and the networks differ: lines of `KIND` `extra` (in the DEM, not in the
reference) and `missed` (in the reference, not in the DEM).

`<name>_DEM.tif` is the DEM, compressed, in the output CRS; `<name>_report.txt`
is the text of the report.

## Is the result any good? (Checks)

A network traced from a DEM is a model of where water runs, not a survey of where
streams are. Two kinds of check say how far to trust it.

**Internal checks** run on every job and need nothing but the DEM. A segment is
flagged when it:

| Flag | Meaning |
|---|---|
| `FLAT` | Falls less than 0.1 % — smaller than a 30 m DEM's error can resolve. The trace is a best guess and tends to run straight or in parallel lines. |
| `FILLED` | Crosses a depression the DEM had to fill by 8 m or more (twice the DEM's stated 4 m vertical accuracy): a reservoir, embankment, bridge or real sink. |
| `NOVALLEY` | Lies on open ground — it sits less than 1 m below the average of the 7 × 7 cells around it. |
| `EDGE` | Starts at the edge of the data, so its real source is probably outside. Does not lower the grade by itself. Use *Include upstream area*. |

Two or more of the first three give **Low** confidence, one gives **Medium**, none
**High**. Flat country grades low by nature — that is the DEM's limit, not a fault in
the tool.

**Reference comparison** (optional) sets the DEM network against an independent one.
A stretch is *matched* when the other network has a line within the match distance;
only unmatched stretches of twice that distance or more are reported, so ragged
junctions are not counted as faults. The report gives:

* the share of DEM streams that have a reference stream nearby, and of reference
  streams that have a DEM stream nearby — overall and by stream order;
* the length "in the DEM, not in the reference" and "in the reference, not in the DEM",
  which are also drawn on the `stream_check` layer so each can be checked against
  imagery;
* how much of the DEM-only length the internal checks had already graded Low.

OpenStreetMap supplies natural rivers and streams only (`waterway=river|stream`).
Canals, drains and ditches are left out: they are dug where water is wanted, not
where the ground runs downhill, so they do not test a DEM-derived network.

**Read the numbers with care.** A reference can be incomplete — OpenStreetMap often
is in the countryside — and a DEM stream can be a real gully that carries water only
after rain. Agreement normally rises with stream size: the biggest streams match well,
the smallest hardly at all. Not matching does not prove a stream wrong.

## Notes and limits

* **Large areas.** Routing a DEM needs about 110 bytes of memory per pixel. Above about
  6 million pixels (roughly 75 km square at 30 m) the tool asks before it starts, with a
  sterner warning above 30 million, and also when the output drive is short of space. A
  temporary output adds to the warning because it lands on the system drive. Above
  100 million pixels it refuses; use a smaller area or the 90 m DEM.
* **Depressions are filled.** Every cell drains out of the area: a lake or a real closed
  basin is routed across, not stopped. Streams therefore cross reservoirs; look at the
  `FILLED` flag.
* **Edges.** Water leaves the area at its edge, and a cut-to-shape area treats the shape's
  outline the same way. Streams near the edge may be missing upstream length unless an
  upstream margin is used.
* The Copernicus DEM is a surface model: it follows tree tops and buildings, not bare
  ground, so a stream under dense canopy or through a built-up area can be displaced.
  Elevations are metres above the EGM2008 geoid.
* Over open sea there are no tiles; an area entirely over sea is reported as having
  nothing to trace.
* Network use: the Copernicus DEM bucket on Amazon S3, and — only if the OpenStreetMap
  comparison is chosen — the Overpass servers overpass-api.de and overpass.kumi.systems
  (the second is tried if the first fails). Only the area's bounding box is sent.
* Attribution is required: Copernicus DEM © DLR e.V. 2010-2014 and © Airbus Defence and
  Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA.
  OpenStreetMap data is © OpenStreetMap contributors (ODbL).

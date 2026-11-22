# Detect Roads from Imagery

|  |  |
|---|---|
| **Algorithm ID** | `kga:detect_roads_imagery` |
| **Group** | KGA GeoAI — on its own **KGA GeoAI** toolbar (not the KGA Toolbox toolbar); also in the KGA Toolbox menu, the Processing Toolbox and the KGA Geoprocessing panel |
| **Icon** | `icons/geoai/detectRoads.png` (group: `icons/toolbox_groups/geoai.png`) |
| **Source** | `kga_tools/algorithms/detect_roads_imagery_alg.py` (dialog in `kga_tools/gui/road_detect_dialog.py`, area picker in `kga_tools/gui/roi_picker.py`, engine in `kga_tools/core/roads/`: `runtime.py` model and onnxruntime, `sampler.py` imagery, `infer.py` model, `skeleton.py` centerlines, `vectorize.py` surface and edges, `task.py` the job and report, `style.py` symbology) |
| **Type** | Interactive dialog — modeless, needs the QGIS window; internet for tile imagery, the one-time setup and the OpenStreetMap check |

## Overview

Finds the roads in satellite, aerial or drone imagery with an open
deep-learning model and turns them into GIS layers:

* **Road centerlines** — one line per stretch between junctions, with width,
  confidence and a paved / unpaved guess;
* **Road edges** — the outline of the road surface, as lines;
* **Road surface** — polygons of the area the model sees as road.

The imagery is any raster layer in the project — an XYZ, WMS, WMTS or ArcGIS
tile service (Esri World Imagery, Bing...), a satellite GeoTIFF, or a drone
orthomosaic. The area is the map extent, a layer's extent, a polygon layer's
boundary, or a rectangle or polygon drawn on the map.

The job runs on a background task, so QGIS stays usable, with **one progress
bar** across reading the imagery, detecting, tracing and writing.

## The model, and credit

| | |
|---|---|
| **Model** | `unet-roads`, a fAIr base model published by **Rafael990** on Hugging Face: <https://huggingface.co/Rafael990/unet-roads> |
| **Licence** | CC BY-SA 4.0 |
| **Network** | U-Net with a ResNet-34 encoder (segmentation-models-pytorch), exported to ONNX |
| **Training data** | SpaceNet 3 road centerlines, Khartoum and Paris (Van Etten et al., 2018, arXiv:1807.01232; CC BY-SA 4.0), fine-tuned on OpenAerialMap drone imagery (CC BY 4.0) with OpenStreetMap highways (© OpenStreetMap contributors, ODbL) over Maputo, Freetown and Dar es Salaam |
| **Input** | RGB, 256 × 256 pixels, about 0.3 m per pixel |

The file is downloaded from a pinned commit and checked byte for byte
(SHA-256), so what runs is always the file that was tested. The credit is
shown in the window, written into every layer's metadata and into the report.

## First-time setup

Neither part ships with QGIS or with the plugin. The **Road model** group at
the top of the window fetches both, once, on request:

1. **Install AI runtime** — installs `onnxruntime` (about 40 MB, from
   pypi.org) with pip into `<QGIS profile>/python/kga_ml`, a folder of its own.
   `--no-deps` is used, so nothing QGIS already has (numpy above all) is
   replaced.
2. **Download model** — the model file (about 98 MB, from huggingface.co) into
   `<QGIS profile>/kga_models`.

If the automatic install cannot run (a proxy, a locked-down PC), **Copy install
command** puts the exact pip command on the clipboard: run it in the OSGeo4W
Shell or a terminal, then reopen the window.

**On Windows, onnxruntime 1.20.1 is installed on purpose.** QGIS for Windows
bundles its own, older copy of the Microsoft C++ runtime; onnxruntime 1.21 and
later are built against a newer one — 1.22 refuses to load and 1.32 crashes
QGIS outright. Because a crash cannot be caught, an onnxruntime the plugin did
not just install (one another plugin put in, say) is first imported in a
separate Python process; only one that survives that is ever loaded into QGIS.

## Parameters

| Control | Meaning |
|---|---|
| **Imagery layer** | Any raster layer. It is read **as it shows on the map** — its band choice and contrast stretch — so a 16-bit or multispectral GeoTIFF works once it looks right on the map. Choose plain imagery: a hybrid or map layer has roads and labels drawn on it, and those would be "detected" (the tool warns when the layer's name suggests that). |
| **Area** | **Current map extent**, **Extent of a layer**, **Boundary of a polygon layer** (results are cut to the shape; optional *Selected features only*), or **Draw a rectangle or polygon**. 20 m of imagery around it is read for context. |
| **Sensitivity** | How sure the model must be before a pixel counts as road: Strict (0.65), **Normal** (0.5), Sensitive (0.35), Very sensitive (0.2). See the note below on what it can and cannot recover. |
| **Join gaps up to** | A road end that points at another road this close (within 35° of straight ahead) is joined to it — across trees, shadow or a bridge deck. Joins are marked `GAP_JOIN = 1` and drawn dashed. 0 turns it off. Default 30 m. |
| **Drop pieces shorter than** | A road network whose whole length is shorter than this is dropped; such pieces are mostly roofs, yards and paths. Default 25 m. |
| **Simplify** (Generalize) | Douglas–Peucker simplification of all three outputs: a vertex is dropped when the line stays within the tolerance of where it was. **Off** keeps every traced pixel step; **Light (0.5 m, default)** only takes the steps off and follows every bend; **Medium (1.5 m)** gives straight streets and keeps real curves; **Strong (3 m)** suits mapping scale, with the fewest vertices; **Custom** takes any tolerance from 0.1 to 20 m. On a 0.6 km² test the centerlines went from 28,586 vertices (Off) to 441 (Light), 166 (Medium) and 116 (Strong). |
| **Trim dead ends shorter than** | Dead-end stubs shorter than this, where they meet another road, are removed — driveways, yard entrances, the whiskers a wide junction leaves. Default 8 m; raise it (15–25 m) for a cleaner network, 0 keeps them all. |
| **Smooth the lines and outlines** | Rounds off the pixel steps *before* simplifying, so curves stay round and junction corners stay sharp. (Smoothing after a strong simplify would cut every corner by a quarter of the segment on either side.) |
| **Compare with** | **None**, **OpenStreetMap roads** (needs internet; motorway to track and service), or **A line layer of my own**. |
| **Match distance** | How close the other network must be for a stretch to count as matched. Default 8 m. |
| **Output** | Tick any of centerlines, edges, surface. Optionally also the **road probability raster** (0–100 per pixel) and the **imagery that was read** (GeoTIFF, 0.3 m, JPEG-compressed) — the exact image the model saw. |
| **Output CRS** | **UTM zone of the area** (default; widths and lengths in metres) or the **current project CRS**. |
| **Output folder / Name** | `<name>_roads.gpkg` and `<name>_roads_report.txt`, plus `<name>_road_probability.tif` and `<name>_imagery.tif` when ticked. An empty folder means a temporary one, deleted when QGIS closes. |

## How it works

1. **Reading the imagery.** The area is laid on a grid in its UTM zone at
   0.3 m per pixel, and the layer is drawn into it in blocks of 1024 × 1024
   pixels (with 64 pixels of context around each) by QGIS's own renderer, on
   the task's thread. A tile service is asked for whatever zoom level fits; a
   local raster is averaged down when it is finer, so a 3 cm drone mosaic
   becomes a clean 0.3 m image.
2. **No imagery at the full zoom.** Tile services answer a zoom level they do
   not cover with a stock "no imagery" tile (Esri's is flat grey with *Map data
   not yet available*). A block that is mostly one exact colour is read again
   one zoom level coarser, then two, and the rest of the run keeps that level;
   the report says so. Esri World Imagery over much of rural Cambodia stops at
   zoom 18 (about 0.6 m).
3. **Detecting.** Each block is cut into 256 × 256 chips overlapping by a
   quarter; the model's answers are weighted towards each chip's middle and
   averaged, so no seams show.
4. **Cleaning.** The probability is kept at 0.6 m, cut at the sensitivity,
   opened once to knock off fringes; specks under 60 m² are dropped and holes
   under 20 m² filled.
5. **Centerlines.** The mask's outline is softened, then thinned to a
   one-pixel skeleton (Zhang–Suen), traced into junctions and stretches, spurs
   under the *Trim dead ends* length pruned, gaps joined, small networks
   dropped, and the lines smoothed, then simplified to the *Simplify*
   tolerance. The surface polygons get the same treatment, and the edges are
   taken from the generalized surface.
6. **Surface and edges.** The mask is polygonised, tidied, and cut to the area;
   the edges are the polygons' outlines, less the parts that run along the
   area's boundary (that edge is the tool's, not the road's).
7. **Checking.** Optionally, the centerlines and the reference are sampled every
   few metres and matched within the match distance.

## Outputs

`<name>_roads.gpkg`, added to the map in a group `<name> roads`:

**`road_centerlines`** (lines)

| Field | Meaning |
|---|---|
| `ROAD_ID` | Number of the stretch. |
| `NETWORK` | Connected network it belongs to (1 = the first one found). |
| `LENGTH_M` | Length in metres. |
| `WIDTH_M` | Twice the median distance from the centerline to the mask's edge. See *Limits*. 0 for a joined gap. |
| `CONF` | The model's mean probability along the line, 0–1. |
| `SURFACE` | `paved`, `unpaved` or `unknown` — a guess from colour: grey reads as paved, red-brown or tan (laterite, earth) as unpaved. |
| `GAP_JOIN` | 1 for a line the tool drew across a gap, 0 for one the model saw. |

Styled by `SURFACE` (red paved, orange unpaved, yellow unknown), joined gaps
dashed.

**`road_edges`** (lines) — `EDGE_ID`, `SURF_ID` (the surface polygon it
outlines), `LENGTH_M`. Cyan.

**`road_surface`** (polygons) — `SURF_ID`, `AREA_M2`, `CONF`. Translucent red.

**`road_check`** (lines, only with a comparison) — `KIND` = `extra` (detected,
not in the reference) or `missed` (in the reference, not detected), `ROAD_ID`,
`LENGTH_M`. Purple and dashed pink.

The **report** (window and `<name>_roads_report.txt`) gives the generalization used, the length, road
density, gaps joined, the surface split, the median width, what the comparison
found, and the credits.

## Limits — read before relying on the result

* **It is a draft.** Precision is high on main and town roads; recall is not.
  On Esri World Imagery over Pursat town (a 2.3 km² test, zoom 18), **97 %** of
  the detected centerline length matched an OpenStreetMap road, but only
  **46 %** of OpenStreetMap's roads were found: most misses were 3–4 m lanes
  between houses. Lowering the sensitivity to 0.25 added only about 9 % more
  centerline — the misses come from the model and the imagery resolution, not
  the threshold.
* **Missed:** roads under tree canopy, in deep shadow, narrower than about 3 m,
  or earth tracks that look like the fields around them.
* **False roads:** railways (the model was not taught the difference), bare
  plots, field bunds, parking lots, river banks, and flat grey roofs.
* **Widths and edges are approximate.** The model was trained on centerlines
  buffered to 6 m, so it tends to paint roads about that wide whatever their
  real width. Treat `WIDTH_M` under 4 m or over 10 m, and the edge lines, as
  indicative; a 20 m boulevard is drawn narrower than it is.
* **Resolution matters most.** 0.3–0.5 m is what the model expects. At 0.6 m
  (Esri over rural Cambodia) narrow roads fade; at 1 m and coarser only main
  roads survive. Sentinel-2 (10 m) is useless here.
* **Drone imagery** works once averaged down to 0.3 m (automatic). Very oblique
  or tree-heavy mosaics give fragmented roads.
* **Speed and size.** About 30 seconds per km² on a laptop CPU for the
  detection itself, plus however long the tile service takes. The tool warns
  above 40 million pixels (about 3.6 km²) and refuses above 600 million (about
  54 km²) — split a district into pieces.
* **Imagery terms.** The roads are derived from the imagery. Google's terms
  forbid deriving data from its tiles; Esri's and Bing's allow some uses (for
  example OpenStreetMap tracing) but not every one. Your own GeoTIFF or drone
  imagery has no such limit. Check before you share.
* **CPU only.** The model runs on the CPU, which is fast enough (about 13 ms per
  chip); GPU support is not included.

## Network use

Only when you ask: **pypi.org / files.pythonhosted.org** (installing
onnxruntime, once), **huggingface.co** (the model, once), the **tile service**
of the imagery layer you choose, and **Overpass** (`overpass-api.de`,
`overpass.kumi.systems`) only for the OpenStreetMap comparison — it receives
the area's bounding box.

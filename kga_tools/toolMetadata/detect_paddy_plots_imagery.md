# Detect Paddy Field Plots from Imagery

|  |  |
|---|---|
| **Algorithm ID** | `kga:detect_paddy_plots_imagery` |
| **Group** | KGA GeoAI — on its own **KGA GeoAI** toolbar (not the KGA Toolbox toolbar), after Detect Roads from Imagery; also in the KGA Toolbox menu, the Processing Toolbox and the KGA Geoprocessing panel |
| **Icon** | `icons/geoai/detectPaddy.png` (group: `icons/toolbox_groups/geoai.png`) |
| **Source** | `kga_tools/algorithms/detect_paddy_plots_imagery_alg.py` (dialog in `kga_tools/gui/paddy_detect_dialog.py`, area picker in `kga_tools/gui/roi_picker.py`, engine in `kga_tools/core/paddy/`: `models.py` the two models, `detect.py` one chip through the model, `task.py` the job and report, `style.py` symbology; the AI runtime, the imagery sampler and the polygon helpers are the road tool's, in `kga_tools/core/roads/`) |
| **Type** | Interactive dialog — modeless, needs the QGIS window; internet for tile imagery, the one-time setup and the farmland check |

## Overview

Outlines the paddy field plots in satellite, aerial or drone imagery with an
open deep-learning model and turns them into GIS layers:

* **Paddy plots** — one polygon per plot, with its area in m² and hectares,
  perimeter, the model's confidence and a shape index;
* **Plot boundaries** — the bund lines between plots, each shared bund drawn
  once.

The imagery is any raster layer in the project — an XYZ, WMS, WMTS or ArcGIS
tile service (Esri World Imagery, Bing...), a satellite GeoTIFF, or a drone
orthomosaic. The area is the map extent, a layer's extent, a polygon layer's
boundary, or a rectangle or polygon drawn on the map.

The job runs on a background task, so QGIS stays usable, with **one progress
bar** across reading the imagery, finding the fields, assembling and writing.

## The models, and credit

| | |
|---|---|
| **Models** | **Delineate Anything** by M. Lavreniuk et al. — *Delineate Anything: Resolution-Agnostic Field Boundary Delineation on Satellite Imagery* (arXiv:2504.02534) and v2 (arXiv:2607.19069). Code and weights: <https://github.com/Lavreniuk/Delineate-Anything>, <https://huggingface.co/MykolaL/DelineateAnything> |
| **Licence** | AGPL-3.0 |
| **Network** | YOLO11 instance segmentation (one class, *field*): **S** is the nano size, **v2** the extra-large one |
| **Training data** | The authors' FBIS field-boundary dataset — for v2, 73 million fields in 61 countries — from imagery of 0.25 to 10 m per pixel |
| **Input** | RGB, 512 × 512 pixels, any resolution in that range |
| **Files** | `DelineateAnything-S.onnx` (11.6 MB) and `DelineateAnythingv2.onnx` (248 MB), exported from the authors' PyTorch checkpoints with Ultralytics (opset 17) without changing the weights, and published on the KGA Toolbox releases (`paddy-models-v1`) |

Each file is checked byte for byte (SHA-256) after download, so what runs is
always the file that was tested. The credit is shown in the window, written
into every layer's metadata and into the report.

**Which model?** Neither is best everywhere. On the test areas below, **S**
separated more plots in the large, regular paddy blocks of Battambang (160
plots against v2's 102), while **v2** separated more of the small plots mixed
with villages in Takeo (96 against 70). S is about four times faster. Try both
on a small area before a large run.

## First-time setup

The **Field model** group at the top of the window fetches what is missing,
once, on request:

1. **Install AI runtime** — `onnxruntime`, the same install Detect Roads from
   Imagery uses (about 40 MB, pinned to 1.20.1 on Windows; see that tool's
   page for why). If the road tool is already set up, there is nothing to do.
2. **Download model** — the chosen model from github.com into
   `<QGIS profile>/kga_models`. Only the model chosen in the drop-down is
   downloaded.
3. **Use a model file...** — for a PC without internet: pick the `.onnx` file
   (from a shared drive or a USB stick); it is checked against the same
   SHA-256 and copied into the profile.

## Parameters

| Control | Meaning |
|---|---|
| **Model** | **Delineate Anything S** (small, fast; default) or **v2** (large, slower). See *Which model?* above. |
| **Imagery layer** | Any raster layer, read **as it shows on the map**. Choose plain imagery, not a hybrid or map layer. |
| **Area** | **Current map extent**, **Extent of a layer**, **Boundary of a polygon layer** (results are cut to the shape; optional *Selected features only*), or **Draw a rectangle or polygon**. 30 m of imagery around it is read for context. |
| **Resolution** | Metres per pixel the imagery is read at: **0.5 m** for very small plots, **1 m** (default) for most paddies, **2 m** for large fields or big areas. Each halving takes four times as long. |
| **Sensitivity** | The score a field needs: Strict (0.35), **Normal** (0.2), Sensitive (0.1). |
| **Keep only plots on farmland** | On by default. Drops plots with less than half their area on cropland, grassland or herbaceous wetland in **ESA WorldCover 2021** (10 m), read from its public bucket for your area only: mostly ponds, village yards, orchards and woodland. Without internet every plot is kept and the report says so. `FARM_SHARE` gives the share for the plots kept. |
| **Drop giant plots the model is unsure of** | On by default. The model sometimes boxes a whole block of plots, or a pond and the plots around it, as one. A plot over **5 ×** the run's median plot size is dropped when it scored under 0.3, when more confident plots overlapped more than 40 % of it, or when it was larger than the model could see at once and scored under 0.5. Real large fields — high score, seen whole — are kept. The plots inside a dropped block may be missing. |
| **Drop plots smaller than** | Default 100 m². |
| **Close gaps between plots up to** | The model leaves the bunds empty. Gaps up to this wide (default 2 m) are shared out to the plots on either side, so neighbours meet along the middle of the bund and share one boundary line. 0 keeps the model's own outlines. |
| **Simplify** (Generalize) | Douglas–Peucker tolerance for plots and boundaries: Off, Light (0.5 m), **Medium (1 m, default)**, Strong (2 m) or Custom. |
| **Fill holes smaller than** | Holes inside a plot under this size are filled (a tree, a hut, a puddle). Default 50 m². |
| **Smooth the outlines** | Rounds off the pixel steps before simplifying. |
| **Output** | Tick plots and/or boundaries; optionally also the **imagery that was read** (GeoTIFF, JPEG-compressed). |
| **Output CRS** | **UTM zone of the area** (default; areas in m²) or the **current project CRS**. |
| **Output folder / Name** | `<name>_paddy.gpkg` and `<name>_paddy_report.txt` (plus `<name>_imagery.tif` when ticked). An empty folder means a temporary one, deleted when QGIS closes. |

## How it works

1. **Reading the imagery.** As in Detect Roads: the area is laid on a grid in
   its UTM zone at the chosen resolution and the layer is drawn into it block
   by block by QGIS's own renderer. A tile service that has no imagery at the
   needed zoom is read a level or two coarser (the report says so).
2. **Finding the fields.** Each block is cut into 512 × 512 chips placed every
   256 pixels, so every pixel lies in the middle half of exactly one chip. The
   model returns boxes, scores and masks; the masks are decoded with numpy
   (no PyTorch), the same way as the authors' own ONNX code. A field is taken
   from the chip whose middle holds its centre — every field narrower than 256
   pixels is therefore seen whole, with context on all sides. Where two fields
   claim a pixel, the more confident one gets it.
3. **Assembling.** Pixels without imagery or outside the area are cleared, and
   gaps up to the *close gaps* width are given to the nearest plot.
4. **Farmland.** Optionally, ESA WorldCover is read for the area and each
   plot's share on farmland classes is measured.
5. **Outlines.** The plot map is polygonised (each plot keeps its main piece),
   small holes filled, giants and small plots dropped, then smoothed,
   simplified and cut to the area. The boundary lines come from the
   unsimplified plots, whose shared edges coincide exactly, so each bund is one
   line.

## Outputs

`<name>_paddy.gpkg`, added to the map in a group `<name>`:

**`paddy_plots`** (polygons), largest first

| Field | Meaning |
|---|---|
| `PLOT_ID` | Number of the plot (1 = the largest). |
| `AREA_M2`, `AREA_HA` | Area in square metres and hectares (in the output CRS; use UTM for true areas). |
| `PERIM_M` | Perimeter in metres. |
| `CONF` | The model's score for the plot, 0–1. |
| `COMPACT` | 4π·area / perimeter²: 1 for a circle, about 0.785 for a square, low for long thin strips. |
| `EDGE` | 1 when the plot touches the area's edge or was larger than the model could see at once — it may be cut short. |
| `FARM_SHARE` | Share of the plot on WorldCover farmland, 0–1 (only when the farmland check ran). |

Styled with a light green fill and a yellow outline; plots with `CONF < 0.3`
or `EDGE = 1` are outlined dashed — check those first.

**`plot_boundaries`** (lines) — `BOUND_ID`, `LENGTH_M`. Orange.

The **report** (window and `<name>_paddy_report.txt`) gives the plot count,
total area and its share of the area, the plot size distribution, what was
dropped and why, and the credits.

## Tested

* **Decoder.** On real Esri chips over Takeo and Battambang, the plugin's
  numpy decoding gives the same fields with the same scores as Ultralytics'
  own pipeline on the same ONNX file, and 98–99 % the same mask pixels.
* **Takeo** (about 1 km², fields mixed with villages, Esri World Imagery,
  1 m, farmland check on): S found 70 plots, v2 96 (median 0.23 ha); 12 plots
  were dropped as not on farmland and 2 giants.
* **Battambang** (about 1 km² of large paddy blocks): S found 160 plots
  covering 93 % of the area; v2 102.
* **Speed** on a laptop CPU, 1 m: reading the tiles and running the model takes
  about **2–3 s per km² with S** and **6–7 s with v2**; 0.5 m takes about four
  times as long. The farmland check adds about 10 s (WorldCover is read over
  the internet). The tool warns above 30 million pixels (30 km² at 1 m) and
  refuses above 250 million.
* The window was run headlessly on QGIS 3.44 and 4.2 (Qt 6): setup states,
  model switching, a run from the button, the layers added, the report, Stop,
  and unload.

## Limits — read before relying on the result

* **It is a draft.** Treat the plots as a starting point to check and edit, not
  as a survey or a cadastre.
* **Every field, not only rice.** The model outlines fields in general; in the
  dry season, or around villages, check that the plots are paddies.
* **Look-alike neighbours merge.** Where two plots have the same crop and the
  same water, the bund between them may be missed and they come out as one. A
  finer resolution or the other model often separates them.
* **False plots:** ponds, bare yards, sports grounds, and large flat roofs
  (green or light ones especially) — the farmland check removes many, not all.
* **Missed plots:** plots under trees, in cloud or shadow, and some brown
  fallow plots with faint bunds.
* **Areas** depend on the imagery's own georeferencing; tile imagery can be
  several metres off.
* **Imagery terms.** The plots are derived from the imagery. Google's terms
  forbid deriving data from its tiles; Esri's and Bing's allow some uses but
  not every one. Your own GeoTIFF or drone imagery has no such limit. Check
  before you share.
* **Licence.** The models are AGPL-3.0. Using them to make plots is not
  restricted; redistributing the model files must keep that licence and the
  credit.
* **CPU only.** GPU support is not included.

## Network use

Only when you ask: **pypi.org / files.pythonhosted.org** (installing
onnxruntime, once — shared with Detect Roads), **github.com** (the chosen
model, once), the **tile service** of the imagery layer you choose, and the
**ESA WorldCover** bucket on Amazon S3 when *Keep only plots on farmland* is
ticked — only the part over your area is read.

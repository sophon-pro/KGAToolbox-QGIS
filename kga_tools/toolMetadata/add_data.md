# Add Open Data & Basemaps

|  |  |
|---|---|
| **Algorithm ID** | `kga:add_data` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/add_data_alg.py` (dialog in `kga_tools/gui/add_data_dialog.py`, sources in `kga_tools/core/data_sources.py`) |
| **Icon** | `icons/addData.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window and an internet connection |

## Overview

Pull **administrative boundaries**, **basemaps**, **elevation** and **land
cover** straight into the project,
without the browser trip. Pick a country and an admin level; the tool asks the
data source what it publishes, shows you who produced it, which year it
represents, under what licence and how many features it holds, then downloads it
and adds it to the map.

Four sources ship with the tool, plus an escape hatch for anything else with a
public address:

| Source | Coverage | Licence |
|---|---|---|
| **geoBoundaries** | Global, ADM0 to ADM5 where it exists. Each country's own source and licence carried through. | Varies by country — shown before you download |
| **HDX / OCHA COD-AB** | The p-coded boundaries the humanitarian community agrees on, ADM0 to ADM3 or 4. | Usually CC BY-IGO |
| **Natural Earth** | Country and state outlines worldwide, at three generalisations, filtered to the country you pick. | Public domain |
| **Custom URL** | Any GeoJSON file, ArcGIS REST layer or WFS endpoint. | Set by whoever publishes it |
| **KGA Database (PostgreSQL)** | Listed but not connected yet. | — |

**Digital elevation models** come from two more sources, chosen under the
**Open digital elevation (DEM)** data type:

| Source | Datasets | Account |
|---|---|---|
| **Copernicus DEM (AWS open data)** | GLO-30 (~30 m), GLO-90 (~90 m) | None |
| **OpenTopography Global DEM API** | Copernicus GLO-30/90, SRTM GL1/GL3 (and ellipsoidal), NASADEM, ALOS AW3D30 (and ellipsoidal), EU DTM, SRTM15+, GEBCO, GEDI L3 | Free API key |

**Land cover** comes from two more, chosen under the **Open land cover (LULC)**
data type. Both are 10 m, CC BY 4.0 and need no account:

| Source | Years | Classes |
|---|---|---|
| **Esri Land Cover** (Impact Observatory / Microsoft / Esri, Sentinel-2) - the data behind [livingatlas.arcgis.com/landcover](https://livingatlas.arcgis.com/landcover/) | Every year from 2017; the list is read from Esri's service, so a newly published year appears on its own | 1 Water, 2 Trees, 4 Flooded vegetation, 5 Crops, 7 Built area, 8 Bare ground, 9 Snow/ice, 10 Clouds, 11 Rangeland |
| **ESA WorldCover** | 2020 (v100), 2021 (v200) | 10 Tree cover, 20 Shrubland, 30 Grassland, 40 Cropland, 50 Built-up, 60 Bare / sparse vegetation, 70 Snow and ice, 80 Permanent water bodies, 90 Herbaceous wetland, 95 Mangroves, 100 Moss and lichen |

The tool is promoted onto the KGA toolbar as its own button, next to the Domain
& Schema Manager.

> **Vector data arrives as a temporary scratch layer.** It lives in memory and
> is lost when the project closes. Export it to a GeoPackage or shapefile to
> keep it — the tool says so, and offers a **Save layer…** button, every time.
> Basemaps are the exception: an XYZ layer is a reference to a URL, so it saves
> into the project file normally.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's, and the form rearranges itself so only the ones
the current choice needs are on screen.

| Control | Type | Default | Description |
|---|---|---|---|
| **Data type** | Combo box | Administrative boundaries | `Administrative boundaries`, `Other vector data`, `Basemap tile layer`, `Open digital elevation (DEM)`, `Open land cover (LULC)`. Decides which sources are offered and which rows appear. |
| **Data source** | Combo box | geoBoundaries | The portal to read. A source that is listed but greyed out is one the tool knows about and cannot reach yet; its tooltip says why. |
| **Country** | Editable combo box | Cambodia (KHM) | Type to filter. Hidden for the custom sources, and for Natural Earth when its **Extent** is set to the whole world. |
| **Admin level** | Combo box | ADM1 | `ADM0` country, `ADM1` province/state, `ADM2` district, `ADM3` commune/sub-district, `ADM4` village, `ADM5`. Levels the country does not publish are **shown but disabled**, so a missing ADM4 is visibly missing rather than silently absent. |
| **Release** | Combo box | gbOpen | *geoBoundaries only.* `gbOpen` has the widest coverage; `gbHumanitarian` is UN OCHA sourced; `gbAuthoritative` is UN SALB official data. |
| **Geometry** | Combo box | Simplified | *geoBoundaries only.* Simplified is right for mapping and context. Take **Full detail** only when the exact boundary line matters — it can be many times larger. |
| **Extent** | Combo box | Selected country only | *Natural Earth only.* Its files are worldwide; this cuts the chosen country out as the layer loads. Set it to **Whole world** for every country at once. |
| **Scale** | Combo box | 1:10m | *Natural Earth only.* `1:10m`, `1:50m`, `1:110m`. See the Natural Earth note below - admin 1 is only worldwide at 1:10m. The 1:10m admin 1 file is about 40 MB, downloaded once and cached. |
| **Address** | Text | empty | *Custom sources only.* A `https://` address. See **Custom addresses** below. |
| **Zoom levels** | Two spin boxes | the service's own range | *Basemaps only.* Narrow the range QGIS will request tiles for. |
| **Also add it to the Browser panel** | Checkbox | off | *Basemaps only.* Writes the tile service into **XYZ Tiles** in the Browser, so it is there in every project without coming back here. |
| **Dataset** | Combo box | GLO-30 / COP30 | *DEM only.* The elevation model to read. |
| **Year** | Combo box | the latest published | *Land cover only.* The year the map represents. Each map summarises that whole year. |
| **API key** | Password + **Remember** | empty | *OpenTopography only.* Get one free from OpenTopography. **Remember** keeps it encrypted in the QGIS password store (QGIS may ask for its master password), never in plain settings. |
| **Area** | Combo box | Current map extent | *DEM and land cover.* See **Area (DEM and land cover)** below. |
| **Draw rectangle / Draw polygon / Clear** | Buttons | — | *DEM and land cover, drawn area only.* Arms a drawing tool on the map. |
| **Layer** | Layer picker | the active layer | *DEM and land cover, layer areas only.* Polygon layers for a boundary, any layer for an extent. |
| **Selected features only** | Checkbox | off | *DEM and land cover, boundary only.* Clip to the selected polygons rather than all of them. |
| **Reproject to the project CRS** | Checkbox | off | *DEM and land cover.* A DEM is warped with bilinear resampling; land cover with nearest-neighbour, so no class is ever invented. Off keeps the published grid exactly — EPSG:4326 for a DEM and WorldCover, the area's UTM zone for Esri Land Cover. |
| **Also add as polygons (lc_code, lc_class)** | Checkbox | off | *Land cover only.* Also turn the classes into polygons, written to a GeoPackage beside the GeoTIFF (numbered, never overwriting an earlier one). Limited to 60 million pixels — about 6,000 km² at 10 m. |
| **Save to** | File | empty | *DEM and land cover.* Where to write the GeoTIFF. Empty means a temporary file, deleted when QGIS closes. |
| **Layer name** | Text | the source's own name | Auto-fills from the selection and keeps following it — until you type in it, after which your text is kept. |

### Buttons

| Button | Description |
|---|---|
| **Add to Map** | Download if needed, then add the layer. |
| **Refresh** | Forget what is cached for the current selection and re-read the source. Use this if a download was interrupted or the source has republished. |
| **Clear cache** | Delete everything the tool has downloaded. Layers already on the map are unaffected — they hold their own copy in memory. |
| **Help** | This page. |

### Area (DEM and land cover)

| Area | What you get |
|---|---|
| **Current map extent** | The bounding box of what the map shows. It follows the map as you pan. |
| **Draw a rectangle or polygon on the map** | Click **Draw rectangle** and drag, or **Draw polygon** and click each corner, then double-click, right-click or press Enter. Backspace removes the last corner, Escape starts again. The raster is **cut to the shape**, with NoData outside it. The shape stays on the map, dashed red, until you clear it or close the window. |
| **Clip to a polygon layer's boundary** | The layer's polygons (or only the selected ones) are merged and the raster is **cut to them**, with NoData outside. |
| **Extent of a layer** | The bounding box of any vector or raster layer. |

The details panel shows the area in km², its bounds, the resolution and an
estimate of the output size before anything is downloaded. Requests over 700
million pixels are refused. At GLO-30 that is about 54 square degrees, or
roughly 650,000 km² near the equator. OpenTopography's own limits are checked too — 450,000 km² for
the 30 m datasets and 4,050,000 km² for SRTM GL3 and COP90, measured on the
area's bounding box.

For land cover the limit is 500 million pixels — about 50,000 km² at 10 m, a
large province. Esri's service hands out at most 4000 × 4000 pixels per
request, so the details panel also says how many requests the area takes.

### Custom addresses

Three shapes, told apart by the address itself:

| You paste | What happens |
|---|---|
| `https://…/data.geojson` (or `.zip`, `.gpkg`, `.kml`) | Downloaded and opened with GDAL/OGR. |
| `https://…/FeatureServer/0` | Queried as GeoJSON and **paged through**. A service that caps each reply at 1000 or 2000 records still arrives complete. A service that ignores paging, or has more than 50 pages of records, is refused with a message rather than returned truncated or repeated. |
| `https://…/geoserver/wfs?service=WFS` | Opened live by the QGIS WFS provider. This one is *not* a scratch layer. |

Local file paths are refused on purpose — use **Layer Export / Import** for
those.

The address is used as typed for the request, but the layer's source address
and attribution are written **without a user name, password or query string**,
so a token pasted as `?token=…` never travels in the project file or in an
export of the layer.

## Outputs

| Data type | What lands in the project |
|---|---|
| Administrative boundaries, other vector data | A **memory (scratch) layer** in the source's own CRS, carrying the publisher, licence and source address in the layer's metadata. A layer whose name is already taken becomes `name (2)`. |
| WFS address | A live **WFS layer**, read from the service on demand. |
| DEM | A **GeoTIFF raster layer** (Float32 or Int16, DEFLATE-compressed, NoData −32767) in EPSG:4326 or the project CRS, carrying its licence in the layer metadata. Written to **Save to**, or to a temporary file that QGIS deletes on exit — the message bar offers **Save layer…** for those. |
| Land cover | A **classified GeoTIFF** (Byte, DEFLATE-compressed, NoData 0) whose pixel values are the publisher's class codes, with the **class names**, the publisher's **colour table** and a **raster attribute table** — `Value`, `Count`, `lc_code`, `lc_class`, `Red`, `Green`, `Blue` — in the `.tif.aux.xml` beside it, which ArcGIS reads as well as QGIS. It opens with a paletted legend such as `2 - Trees`, saved as a `.qml` beside the file so it opens styled next time too. Written to **Save to**, or to a temporary file. |
| Land cover polygons | *Optional.* A **GeoPackage** layer `landcover` with `lc_code` (integer) and `lc_class` (text), one polygon per connected patch of a class, styled with the same legend. |
| Basemap | An **XYZ raster layer** in EPSG:3857, placed at the bottom of the layer tree. Optionally also a permanent Browser entry. |

## Notes and limits

- **Downloads are cached.** Adding the same boundary twice costs one download,
  and the second one works with the network unplugged. The cache lives under the
  QGIS profile directory (`kga_tools/data_cache`) and is trimmed, least recently
  used first, once it passes 500 MB.
- **Natural Earth is worldwide files, cut down on the way in.** One download
  serves every country, so picking a second country afterwards costs nothing.
  Its admin 1 coverage is not the same at every scale, and this is the
  publisher's doing rather than the tool's:

  | ADM1 scale | Units | Countries covered |
  |---|---|---|
  | 1:10m | 4,596 | 251 — effectively everywhere. Cambodia has 24. |
  | 1:50m | 294 | 9 only: Australia, Brazil, Canada, China, Indonesia, India, Russia, USA, South Africa |
  | 1:110m | 51 | the USA only |

  So ADM1 is greyed out at 1:50m and 1:110m for a country those files do not
  hold, the same way a missing geoBoundaries level is. Admin 0 is complete at
  all three scales. Note that Natural Earth's 24 Cambodian units are its own
  generalisation, not the official 25 — use geoBoundaries or HDX when the
  province count has to be right.

- **One HDX download carries every level.** COD-AB publishes a single zipped
  shapefile holding ADM0 to ADM3 together, so switching level after the first
  add costs nothing. The level is picked out of the container by name, and
  publishers do not all agree on that name — both `khm_admin2.shp` and
  `xxx_admbnda_adm2_gov_20181004` are understood.
- **Attribution is required by every source here**, including the basemaps. The
  licence is shown before the download, written into the layer's metadata, and
  travels with the layer when it is exported.
- **Copernicus tiles are streamed, not downloaded.** They are Cloud Optimised
  GeoTIFFs in a public AWS bucket, so only the part inside your area crosses the
  network — a district out of a 48 MB tile is a few MB. Tiles over open sea do
  not exist; the tool knows which do from the bucket's own tile list, cached
  for a week. This goes through GDAL rather than QGIS's network stack; the
  proxy set in QGIS Options is passed across to it.
- **OpenTopography downloads are cached** like the vector sources, keyed by
  dataset and bounding box, so asking for the same area again costs nothing.
  The API key is kept in the QGIS authentication database (never in plain
  settings), is trimmed and percent-encoded into the request, and is sent only
  to OpenTopography.
- **A file you name is never replaced silently.** Typing the path of an
  existing DEM or land-cover file asks first; a file that is open in the
  project as a layer is refused outright.
- **A polygon failure keeps the raster.** If turning land cover into polygons
  fails, the finished raster is still added and the message says what went wrong
  with the polygons.
- **Land cover is kept on its own grid.** Esri Land Cover is asked for in the
  UTM zone of the area's centre at exactly 10 m, which is how it is published,
  so nothing is resampled. An area crossing a zone boundary is still one
  raster, in the centre's zone. WorldCover stays on its 1/12000° grid,
  streamed from the AWS bucket like the Copernicus DEM. Esri requests are
  cached per chunk, so asking again for an overlapping area costs little.
- **A land-cover year that is not published yet** comes back from Esri's
  service as all NoData; the tool says so rather than adding an empty raster.
- **The two land-cover products do not share a legend.** Esri's codes run 1 to
  11 with gaps, WorldCover's 10 to 100 in tens, and their classes are defined
  differently. WorldCover 2020 and 2021 were also made with different algorithm
  versions, so a difference between them is not necessarily real change.
- **Copernicus, SRTM, NASADEM and ALOS are surface models.** They follow the
  tops of trees and buildings. EU DTM and GEDI L3 are bare-earth. The
  `_E` datasets give heights above the WGS84 ellipsoid, not above sea level.
- **Basemap list.** Eleven services ship with the tool:

  | Basemap | Deepest zoom |
  |---|---|
  | OpenStreetMap | 19 |
  | Google Satellite, Google Satellite Hybrid | 21 |
  | Google Map | 20 |
  | Google Terrain, Google Terrain Hybrid | 18 |
  | ESRI Topography, ESRI Imagery | 19 |
  | ESRI National Geographic, ESRI Grey (Dark), ESRI Grey (Light) | 16 |

  Zooming past a service's deepest level is not an error — QGIS scales the last
  real level up, which looks soft but keeps working. Anything not listed can be
  added through **Custom XYZ URL…**.

- **The Google basemaps come with a licensing caveat.** Google's tile endpoints
  are not licensed for use outside Google's own APIs and SDKs, so whether a
  particular map or deliverable may use them is a question for whoever makes it.
  They are included because they are what people here use; this note is here so
  the decision is an informed one rather than an assumption.
- **Coverage is the publisher's, not the tool's.** If geoBoundaries has no ADM4
  for a country, the level is greyed out; that is the source saying it has
  nothing, not a failure here. Trying another release or another source is
  often the answer.
- **The window needs the QGIS canvas** and cannot run headless or inside a
  Processing model.
- **Network errors are reported as sentences**, not tracebacks. Everything the
  tool fetches through QGIS honours the proxy, SSL and authentication settings
  configured in QGIS Options. The tiles streamed straight from AWS (Copernicus,
  WorldCover) go through GDAL, which is handed the proxy only. If the tool
  cannot reach a source but a browser can, QGIS Options is the first place to
  look.

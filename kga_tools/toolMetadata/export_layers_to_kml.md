# Export Layers to KML

|  |  |
|---|---|
| **Algorithm ID** | `kga:export_layers_to_kml` |
| **Group** | KGA Data Conversion |
| **Source** | `kga_tools/algorithms/ExportToKML_With_Label.py` |
| **Icon** | `icons/alg_kml.png` |
| **Type** | Batch algorithm |

## Overview

Writes point, line and polygon layers to **KML** or **KMZ** for Google Earth.
Layer colours, the label the layer already shows, and the attribute table all
come across. Any CRS is reprojected to WGS 84 on the way out; the layers
themselves are untouched.

The reason this exists rather than *Save Features As… KML*: **rotated labels**.
Plain KML always draws text horizontally. This tool can render each label as a
transparent PNG laid on the ground and turned to match the line, so a canal name
reads along the canal the way it does on the map.

### Two ways to label

|  | Placemark labels | Rotated ground overlays |
|---|---|---|
| Drawn as | KML text | Transparent PNG on the ground |
| Angle | Always horizontal | Turned to match the line |
| Size | Constant on screen | Fixed on the ground, in metres |
| File | `.kml` | `.kmz` — the images ride inside |
| Applies to | Everything | Lines; points and polygons still get placemarks |

Placemark labels are the safe choice and always legible. Reach for rotated
overlays when the text has to read along a canal, a road or a parcel boundary.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input Layers** | Multiple vector layers (any geometry) | — | |
| **Export Selected Features Only** | Boolean | `False` | |
| **Export Mode** | Enum | `One file per layer` | `One file per layer` names each file after its layer. `Single combined file` puts every layer in one file, one folder each — what you want when the whole lot is going to somebody as an attachment. |
| **Combined File Name** | String, optional | `Combined_Export` | Used by the combined mode. |
| **Carry Layer Labels Across** | Boolean | `True` | |
| **Label Style** | Enum | `Placemark labels — horizontal, constant screen size (.kml)` | Or `Rotated ground overlays along lines (.kmz)`. |
| **Placemark Labels · Text Scale** | Number (0.0–10.0) | `1.0` | Placemark mode only. |
| **Rotated Labels · Text Height on Ground (m)** | Number (0.05–10000) | `4.0` | The one control that decides how big the text looks. |
| **Rotated Labels · Perpendicular Offset (m)** | Number (−10000…10000) | `0.0` | Pushes the label clear of the stroke — positive to the left of the direction the line runs, negative to the right. Scales with each tier, so the gap looks the same at every zoom. |
| **Rotated Labels · Zoom Tiers** | Integer (1–6) | `3` | Each tier is a second copy 4× larger that switches on as you zoom out, keeping the text roughly the same size on screen. `1` = one fixed scale (printed-map look); `3` = parcel to district; `5` = province-wide browsing. All tiers point at the same PNG, so they cost XML, not image weight. |
| **Rotated Labels · Text Colour** | Colour (with opacity) | white | |
| **Rotated Labels · Halo Colour** | Colour (with opacity) | black | |
| **Rotated Labels · Keep Text Right-Way-Up** | Boolean | `True` | Spins any label turned past vertical, so westward lines do not come out upside down. |
| **Output Folder** | Folder destination | — | |

### Advanced

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Rotated Labels · Render Resolution (px)** | Integer (8–256) | `48` | Sharpness only. How big the text *looks* is set entirely by text height on ground. |
| **Rotated Labels · Font Family** | String, optional | `Arial` | |
| **Rotated Labels · Bold Text** | Boolean | `True` | |
| **Include Attribute Balloon** | Boolean | `True` | Every non-empty field goes into the balloon that opens when a feature is clicked. Turn it off for a much smaller file. |
| **Line Width (px)** | Integer (1–10) | `2` | |

## How to use

1. Style and label the layers in QGIS first — this tool reads what is already
   there.
2. Open **KGA Data Conversion > Export Layers to KML**.
3. Pick the **Input Layers** and the **Output Folder**.
4. Choose the **Export Mode**: one file per layer, or one combined file to email.
5. Leave **Label Style** on placemark labels unless the text has to follow a
   line. If it does, set **Text Height on Ground** first — it is the control that
   matters — then adjust **Zoom Tiers** for the range you will browse at.
6. Press **Run**, then open the result in Google Earth and check the label size
   at the zoom you actually work at.

## Outputs

- `.kml` files (placemark labels) or `.kmz` files (rotated overlays, with the
  label PNGs inside), in the output folder — one per layer, or one combined.
- Line and fill colours read from the layer's symbology.
- An attribute balloon per feature, unless turned off.

## Notes and limits

- **One colour per layer.** A categorised or graduated renderer collapses to its
  first symbol. Split the layer if the classes have to stay apart in Google
  Earth.
- **Everything is clamped to the ground** at altitude 0. Z values are not carried
  across.
- **Rotated labels are images.** Thousands of *distinct* label texts mean
  thousands of PNGs inside the `.kmz`. Repeated text is rendered once and shared,
  so it is the number of different strings that matters.
- **Files are overwritten without asking** when the name already exists in the
  output folder.
- Labels come from whatever the layer's Labels tab is set to: a field, an
  expression, or the first labelled rule of a rule-based setup. KML prints HTML
  literally, so tags are stripped and `<sup>2</sup>` becomes a real ².
- Each rotated label sits on the midpoint of its line, turned to the bearing of
  the segment containing that midpoint.

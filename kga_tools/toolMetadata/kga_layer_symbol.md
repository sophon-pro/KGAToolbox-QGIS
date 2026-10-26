# KGA Layer Symbol

|  |  |
|---|---|
| **Algorithm ID** | `kga:kga_layer_symbol` |
| **Group** | KGA Mapping |
| **Source** | `kga_tools/algorithms/kga_layer_symbol_alg.py` (gallery in `kga_tools/symbol_gallery/`) |
| **Icon** | `icons/kgaLayerSymbol.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

A searchable gallery of ready-made **symbols, icons, textures and colour ramps**
organised by field (hydraulics, irrigation, agriculture, land cover, cadastre,
transport, geology, remote sensing, tactical and more). Pick a thumbnail and apply
it to a layer; everything is built from native QGIS symbol layers, so the result
stays editable in Layer Properties.

It opens two ways:

* **KGA Mapping > KGA Layer Symbol** — a stand-alone window that follows the layer
  selected in the Layers panel.
* **Layer Properties > KGA Layer Symbol** — a tab listed right under Symbology
  (vector layers) or after the Symbology page (rasters), pinned to the layer whose
  properties are open.

## Parameters

The algorithm takes no Processing parameters — running it opens the window.

| Control | Description |
|---|---|
| Search / field / kind | Narrow the gallery by text, by field and by Point, Line, Polygon or Ramp. |
| Apply to Layer / Classes | Styles the whole layer; in the window, legend classes selected in the Layers panel style just those classes. Categorized and graduated layers keep their classes. |
| Current style page | Applying a symbol or DEM ramp opens this page inside the panel: fine-tune colours, sizes, spacing and angles live. **‹ Back to styles** returns to the gallery. |
| Undo | Puts back the style the layer had before the last apply. |
| Rasters | Colour ramps (a DEM can get a hillshade); the matching field opens by the layer's name. |
| More menu | Style packs (QGIS .xml, ArcGIS Pro .stylx), save to Style Manager, hillshade options, credits. |

## How to use

1. Select a layer, then open **KGA Layer Symbol** from the KGA Mapping group, or
   open the layer's properties and choose the **KGA Layer Symbol** tab.
2. Pick a symbol and press **Apply to Layer** (in the Layer Properties tab, the dialog's **Apply** or **OK**).
3. Adjust on the Current style page, or press **Undo**.

## Outputs

None. The layer's renderer is changed in place.

## Notes and limits

* In Layer Properties the tab opens on the layer's current symbol (single-symbol
  layers). Edit it there, or press **Change style** to pick another; nothing
  changes on the layer until the dialog's own **Apply** or **OK**. Undo in the tab
  reverts the last apply.
* Class-level styling needs the Layers panel, so it is available in the window only.
* Symbols and ramps are original works or open-licence sets; see **More > Credits
  & Licences** for the icon libraries.

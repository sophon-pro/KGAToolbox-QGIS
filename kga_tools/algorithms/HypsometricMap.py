# -*- coding: utf-8 -*-
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterEnum,
    QgsProcessing,
)
from qgis.PyQt.QtCore import QCoreApplication
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl
import matplotlib.colors as mcolors
import os
from ..branding import docs_url
from ..core.compat import mark_advanced


def nice_ticks(vmin, vmax, target):
    """Round-number ticks (whole metres) numbering close to `target`.

    The step is the 1/2/5 x 10^n value whose tick count is nearest the
    target. The true minimum and maximum are added as end ticks when no
    round tick falls near them, so the bar is always labelled end to end.
    """
    span = vmax - vmin
    if span <= 0:
        return np.array([vmin])
    raw = span / max(target - 1, 1)
    mag = 10 ** np.floor(np.log10(raw))
    best = None
    for step in sorted({max(1.0, m * mag) for m in (1, 2, 5, 10)}):
        ticks = np.arange(np.ceil(vmin / step) * step, vmax + 1e-9, step)
        if best is None or abs(len(ticks) - target) < abs(len(best[1]) - target):
            best = (step, ticks)
    step, ticks = best
    ticks = list(ticks)
    if not ticks or abs(ticks[0] - vmin) > step * 0.3:
        ticks = [vmin] + ticks
    if abs(ticks[-1] - vmax) > step * 0.3:
        ticks = ticks + [vmax]
    return np.array(sorted(set(ticks)))


class HypsometricMap(QgsProcessingAlgorithm):

    INPUT_LAYERS  = "INPUT_LAYERS"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    COLOR_SCHEME  = "COLOR_SCHEME"
    LEGEND_DPI    = "LEGEND_DPI"
    TICK_COUNT    = "TICK_COUNT"
    LABEL_TITLE   = "LABEL_TITLE"
    FONT_FAMILY   = "FONT_FAMILY"
    FONT_TICK     = "FONT_TICK"
    FONT_TITLE    = "FONT_TITLE"

    COLOR_OPTIONS = [
        "from_qgis", "YlGn_r", "turbo", "jet",
        "RdYlBu_r", "terrain", "plasma", "viridis", "gist_earth",
    ]
    DPI_OPTIONS = ["150 (Fast)", "300 (Balanced)", "600 (High Quality)"]
    DPI_VALUES  = [150, 300, 600]

    def _add(self, param, help_text, advanced=False):
        """Add a parameter, with its help shown as the widget tooltip."""
        param.setHelp(help_text)
        if advanced:
            mark_advanced(param)
        self.addParameter(param)

    def initAlgorithm(self, config=None):
        self._add(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS, "Input · DEM raster layer(s)",
            layerType=QgsProcessing.SourceType.TypeRaster),
            "One legend is made per layer, from that layer's own minimum and "
            "maximum elevation.")
        self._add(QgsProcessingParameterEnum(
            self.COLOR_SCHEME, "Style · Colour scheme",
            options=self.COLOR_OPTIONS, defaultValue=0),
            "from_qgis copies the colour ramp of each layer's current QGIS "
            "style. The others are fixed ramps (matplotlib names).")
        self._add(QgsProcessingParameterString(
            self.LABEL_TITLE, "Style · Legend title",
            defaultValue="Elevation (m)"),
            "Text printed beside the bar.")
        self._add(QgsProcessingParameterNumber(
            self.TICK_COUNT, "Style · Number of ticks (approx.)",
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=7, minValue=3, maxValue=20),
            "Aim for about this many labels. Steps are round numbers (1, 2, 5, "
            "10, 20, 50 ... m), so the count can differ slightly. The lowest "
            "and highest elevations get their own label unless a round "
            "tick falls right next to them.")
        self._add(QgsProcessingParameterString(
            self.FONT_FAMILY, "Font · Family",
            defaultValue="Times New Roman"),
            "Name of an installed font. If it is not found a default font is "
            "used.", advanced=True)
        self._add(QgsProcessingParameterNumber(
            self.FONT_TICK, "Font · Tick label size (pt)",
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=9, minValue=6, maxValue=24),
            "Size of the elevation numbers.", advanced=True)
        self._add(QgsProcessingParameterNumber(
            self.FONT_TITLE, "Font · Title size (pt)",
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=10, minValue=6, maxValue=24),
            "Size of the legend title.", advanced=True)
        self._add(QgsProcessingParameterEnum(
            self.LEGEND_DPI, "Output · Resolution (DPI)",
            options=self.DPI_OPTIONS, defaultValue=0),
            "Sharpness of the PNG. 150 for a quick look, 300 for print, 600 "
            "for large-format posters; the image is 4 times larger at 600.")
        self._add(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, "Output · Folder"),
            "Where the PNG files are saved.")

    def processAlgorithm(self, parameters, context, feedback):
        dem_layers    = self.parameterAsLayerList(parameters, self.INPUT_LAYERS,  context)
        color_idx     = self.parameterAsEnum(parameters,      self.COLOR_SCHEME,  context)
        dpi_idx       = self.parameterAsEnum(parameters,      self.LEGEND_DPI,    context)
        output_folder = self.parameterAsString(parameters,    self.OUTPUT_FOLDER, context)
        tick_count    = self.parameterAsInt(parameters,       self.TICK_COUNT,    context)
        label_title   = self.parameterAsString(parameters,    self.LABEL_TITLE,   context)
        font_family   = self.parameterAsString(parameters,    self.FONT_FAMILY,   context)
        font_tick     = self.parameterAsInt(parameters,       self.FONT_TICK,     context)
        font_title    = self.parameterAsInt(parameters,       self.FONT_TITLE,    context)

        color_scheme = self.COLOR_OPTIONS[color_idx]
        legend_dpi   = self.DPI_VALUES[dpi_idx]

        os.makedirs(output_folder, exist_ok=True)

        feedback.pushInfo("=" * 50)
        feedback.pushInfo(" DEM Legend Bar Generator")
        feedback.pushInfo("=" * 50)

        results = []

        for i, layer in enumerate(dem_layers):
            if feedback.isCanceled():
                break

            feedback.pushInfo(f"\n[{i+1}/{len(dem_layers)}] {layer.name()}")

            try:
                # ------------------------------------------------ Get min/max
                provider = layer.dataProvider()
                stats    = provider.bandStatistics(1)
                vmin     = stats.minimumValue
                vmax     = stats.maximumValue
                feedback.pushInfo(f"   Min: {vmin:.2f} | Max: {vmax:.2f}")

                # --------------------------------------------- Build colormap
                if color_scheme == "from_qgis":
                    try:
                        renderer    = layer.renderer()
                        shader      = renderer.shader()
                        ramp_shader = shader.rasterShaderFunction()
                        all_items   = ramp_shader.colorRampItemList()
                        if len(all_items) == 0:
                            raise ValueError("No color items")
                        MAX_STOPS = 50
                        if len(all_items) > MAX_STOPS:
                            step  = max(1, len(all_items) // MAX_STOPS)
                            items = all_items[::step]
                            if all_items[-1] not in items:
                                items = list(items) + [all_items[-1]]
                        else:
                            items = all_items
                        values     = [item.value for item in items]
                        v_min_item = min(values)
                        v_max_item = max(values)
                        v_range    = v_max_item - v_min_item
                        if v_range == 0:
                            raise ValueError("Color range is zero")
                        color_list = []
                        for item in items:
                            pos = (item.value - v_min_item) / v_range
                            pos = max(0.0, min(1.0, pos))
                            color_list.append((pos, (
                                item.color.redF(),
                                item.color.greenF(),
                                item.color.blueF()
                            )))
                        color_list.sort(key=lambda x: x[0])
                        if color_list[0][0] != 0.0:
                            color_list.insert(0, (0.0, color_list[0][1]))
                        if color_list[-1][0] != 1.0:
                            color_list.append((1.0, color_list[-1][1]))
                        cmap = mcolors.LinearSegmentedColormap.from_list(
                            "qgis_ramp", [(c[0], c[1]) for c in color_list])
                        feedback.pushInfo(f"   Color stops: {len(items)}")
                    except Exception as e:
                        feedback.pushInfo(f"   Fallback to YlGn_r: {e}")
                        cmap = plt.get_cmap("YlGn_r")
                else:
                    cmap = plt.get_cmap(color_scheme)

                # -------------------------------------------- Draw legend bar
                plt.switch_backend("Agg")
                mpl.rcParams.update({"font.family": font_family, "font.size": font_tick})
                fig = plt.figure(figsize=(1.2, 6.0), dpi=legend_dpi)
                fig.patch.set_facecolor("#E8E8E8")
                cax  = fig.add_axes([0.15, 0.05, 0.25, 0.90])
                norm = mpl.colors.Normalize(vmin=vmin, vmax=vmax)
                cb   = mpl.colorbar.ColorbarBase(cax, cmap=cmap, norm=norm, orientation="vertical")

                # ------------------------------------------------------- Ticks
                ticks = nice_ticks(vmin, vmax, tick_count)

                cb.set_ticks(ticks)
                cb.set_ticklabels([f"{int(round(v))}" for v in ticks])
                cb.ax.tick_params(labelsize=font_tick, length=4, width=0.8)
                cb.ax.yaxis.set_ticks_position("right")
                cb.ax.yaxis.set_label_position("right")
                cb.set_label(label_title, fontsize=font_title, labelpad=12)
                cb.outline.set_linewidth(0.8)
                cb.outline.set_edgecolor("black")

                scheme_name = "qgis" if color_scheme == "from_qgis" else color_scheme
                output_path = os.path.join(output_folder, f"{layer.name()}_{scheme_name}_legend_bar.png")
                fig.savefig(output_path, dpi=legend_dpi, bbox_inches="tight",
                            pad_inches=0.2, facecolor=fig.get_facecolor())
                plt.close(fig)
                feedback.pushInfo(f"   ✅ Saved: {output_path}")
                results.append(output_path)

            except Exception as e:
                feedback.pushInfo(f"   ❌ Failed: {str(e)}")

            feedback.setProgress(int(((i + 1) / len(dem_layers)) * 100))

        feedback.pushInfo(f"\n✅ Done! {len(results)}/{len(dem_layers)} saved to: {output_folder}")
        return {"OUTPUT_FOLDER": output_folder}

    def name(self):            return "dem_legend_bar"
    def displayName(self):     return "DEM Legend Bar"
    def group(self):           return "KGA Mapping"
    def groupId(self):         return "kgamapping"

    def helpUrl(self):
        return docs_url('dem_legend_bar')
    def createInstance(self):  return HypsometricMap()
    def shortHelpString(self):
        return (
            "<p>Draws a vertical <b>colour bar legend</b> for each DEM as a "
            "PNG image, ready to place beside a map in a layout, report or "
            "slide.</p>"

            "<h3>What it does</h3>"
            "<ol>"
            "<li>Reads the minimum and maximum elevation of each DEM.</li>"
            "<li>Takes the colours from the layer's current QGIS style, or "
            "from the scheme you choose.</li>"
            "<li>Draws the bar from minimum (bottom) to maximum (top) with "
            "round-number tick labels and your title.</li>"
            "<li>Saves <i>&lt;layer name&gt;_&lt;scheme&gt;_legend_bar.png</i> "
            "in the output folder, one file per layer. A layer that fails is "
            "logged and the rest still run.</li>"
            "</ol>"

            "<h3>Colour scheme</h3>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>Scheme</th><th>Look</th></tr>"
            "<tr><td><b>from_qgis</b></td><td>Copies the colour ramp of the "
            "layer's style so the legend matches the map. Needs the layer "
            "styled as <i>Singlebanded pseudocolor</i>; otherwise it falls "
            "back to YlGn_r and says so in the log.</td></tr>"
            "<tr><td>YlGn_r</td><td>Dark green (low) to pale yellow (high); "
            "gentle, good for terrain.</td></tr>"
            "<tr><td>terrain / gist_earth</td><td>Blue-green lowlands to "
            "brown and white peaks; natural looking.</td></tr>"
            "<tr><td>viridis / plasma</td><td>Perceptually even and "
            "colour-blind friendly; best for accurate reading of values.</td></tr>"
            "<tr><td>turbo / jet</td><td>Full rainbow; strong contrast but "
            "exaggerates small differences.</td></tr>"
            "<tr><td>RdYlBu_r</td><td>Blue (low) through yellow to red "
            "(high); suits a before/after or hot-cold feel.</td></tr>"
            "</table>"

            "<h3>Ticks</h3>"
            "<p>Labels fall on round numbers (steps of 1, 2, 5, 10, 20, 50 m "
            "and so on) chosen to give about the number of ticks you ask for. "
            "The exact lowest and highest elevations are labelled too, unless a "
            "round tick falls right beside them, so the bar reads end to end. "
            "Labels are whole metres.</p>"

            "<h3>Resolution</h3>"
            "<p>The bar is 1.2 by 6 inches. 150 DPI gives about 180 x 900 "
            "pixels, 300 DPI about 360 x 1800, 600 DPI about 720 x 3600. "
            "Use 300 or more for print.</p>"

            "<h3>Watch out for</h3>"
            "<ul>"
            "<li><b>Match the map.</b> The bar spreads the ramp over the "
            "DEM's full minimum-to-maximum range. If the layer's QGIS style "
            "uses a narrower range (for example clipped to 2 to 98 percent), "
            "the colours on the map will not line up with this legend. Set "
            "the layer's style to the full Min / Max first.</li>"
            "<li>Classified styles (Discrete or Exact interpolation) are "
            "drawn as a smooth gradient, not as steps.</li>"
            "<li>Labels are rounded to whole metres. Very flat sites with a "
            "range of only a few metres will show few distinct labels.</li>"
            "<li>The default font, Times New Roman, must be installed; "
            "otherwise a standard font is used instead.</li>"
            "<li>Files with the same name in the output folder are "
            "overwritten.</li>"
            "</ul>"
        )

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

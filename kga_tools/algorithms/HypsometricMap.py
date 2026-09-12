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

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT_LAYERS, "DEM Raster Layer(s)",
            layerType=QgsProcessing.TypeRaster))
        self.addParameter(QgsProcessingParameterEnum(
            self.COLOR_SCHEME, "Color Scheme",
            options=self.COLOR_OPTIONS, defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.LEGEND_DPI, "Legend DPI  (150=Fast | 300=Balanced | 600=HQ)",
            options=self.DPI_OPTIONS, defaultValue=0))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, "Output Folder"))
        self.addParameter(QgsProcessingParameterNumber(
            self.TICK_COUNT, "Number of Ticks",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=7, minValue=3, maxValue=20))
        self.addParameter(QgsProcessingParameterString(
            self.LABEL_TITLE, "Legend Title",
            defaultValue="Elevation (m)"))
        self.addParameter(QgsProcessingParameterString(
            self.FONT_FAMILY, "Font Family",
            defaultValue="Times New Roman"))
        self.addParameter(QgsProcessingParameterNumber(
            self.FONT_TICK, "Font Tick Size",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=9, minValue=6, maxValue=24))
        self.addParameter(QgsProcessingParameterNumber(
            self.FONT_TITLE, "Font Title Size",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=10, minValue=6, maxValue=24))

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

                # --------------------------------------- Smart adaptive ticks
                range_val = vmax - vmin
                if range_val <= 10:
                    step = 1
                elif range_val <= 30:
                    step = 5
                elif range_val <= 100:
                    step = 10
                elif range_val <= 300:
                    step = 50
                elif range_val <= 1000:
                    step = 100
                elif range_val <= 3000:
                    step = 200
                else:
                    step = 500

                start = int(np.floor(vmin / step) * step)
                end   = int(np.ceil(vmax  / step) * step)
                ticks = np.arange(start, end + step, step)
                ticks = ticks[(ticks >= vmin) & (ticks <= vmax)]

                while len(ticks) < 4 and step > 1:
                    step  = step // 2
                    start = int(np.floor(vmin / step) * step)
                    end   = int(np.ceil(vmax  / step) * step)
                    ticks = np.arange(start, end + step, step)
                    ticks = ticks[(ticks >= vmin) & (ticks <= vmax)]

                while len(ticks) > 10:
                    step  = step * 2
                    start = int(np.floor(vmin / step) * step)
                    end   = int(np.ceil(vmax  / step) * step)
                    ticks = np.arange(start, end + step, step)
                    ticks = ticks[(ticks >= vmin) & (ticks <= vmax)]

                ticks = list(ticks)
                if ticks and abs(ticks[0]  - vmin) > step * 0.3:
                    ticks = [vmin] + ticks
                if ticks and abs(ticks[-1] - vmax) > step * 0.3:
                    ticks = ticks + [vmax]
                ticks = np.array(sorted(set(ticks)))

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
    def group(self):           return "KGA Irrigation Tools"
    def groupId(self):         return "kgairrigationtools"

    def helpUrl(self):
        return docs_url('dem_legend_bar')
    def createInstance(self):  return HypsometricMap()
    def shortHelpString(self):
        return ("Generates legend bar PNG for DEM raster(s)\n\n"
                "OUTPUT: PNG saved to selected folder\n\n"
                "TICKS: Auto-adaptive\n"
                "  Flat  (<=30m)  -> step=5\n"
                "  Hills (<=300m) -> step=50\n"
                "  Mountain       -> step=100/200/500\n"
                "  Min=4, Max=10 ticks")
    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

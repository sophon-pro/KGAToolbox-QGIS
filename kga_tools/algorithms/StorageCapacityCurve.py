# -*- coding: utf-8 -*-
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessing
)
from qgis.PyQt.QtCore import QCoreApplication
import numpy as np
import pandas as pd
import os
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.gridspec import GridSpec
from osgeo import gdal, ogr, osr
from ..branding import docs_url


class StorageCapacityCurve(QgsProcessingAlgorithm):

    INPUT_DEMS     = "INPUT_DEMS"
    INPUT_BOUNDARY = "INPUT_BOUNDARY"
    OUTPUT_FOLDER  = "OUTPUT_FOLDER"
    ELEV_STEP      = "ELEV_STEP"
    PLOT_ELEV_MIN  = "PLOT_ELEV_MIN"
    PLOT_ELEV_MAX  = "PLOT_ELEV_MAX"
    PLOT_VOL_MAX   = "PLOT_VOL_MAX"
    PLOT_AREA_MAX  = "PLOT_AREA_MAX"
    PLOT_Y_TICK    = "PLOT_Y_TICK"
    PLOT_X_TICK_V  = "PLOT_X_TICK_V"
    PLOT_X_TICK_A  = "PLOT_X_TICK_A"

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_DEMS,
                "DEM Raster Layer(s)",
                layerType=QgsProcessing.SourceType.TypeRaster
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_BOUNDARY,
                "Boundary Layer (optional — leave blank for full DEM)",
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.ELEV_STEP,
                "Elevation Step (m)",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.1, minValue=0.01
            )
        )

        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER, "Output Folder"
            )
        )

        # ------------------------------------------------- Plot axis controls
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_ELEV_MIN,
                "Plot: Elevation Y-axis Minimum (m)  [0 = auto from DEM]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_ELEV_MAX,
                "Plot: Elevation Y-axis Maximum (m)  [0 = auto from DEM]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_VOL_MAX,
                "Plot: Volume X-axis Maximum (1,000 m³)  [0 = auto]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_AREA_MAX,
                "Plot: Area X-axis Maximum (ha)  [0 = auto]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_Y_TICK,
                "Plot: Elevation Y-axis Tick Interval (m)  [0 = auto]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_X_TICK_V,
                "Plot: Volume X-axis Tick Interval (1,000 m³)  [0 = auto]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PLOT_X_TICK_A,
                "Plot: Area X-axis Tick Interval (ha)  [0 = auto]",
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0, minValue=0.0, optional=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):

        dem_layers     = self.parameterAsLayerList(parameters, self.INPUT_DEMS, context)
        boundary_layer = self.parameterAsVectorLayer(parameters, self.INPUT_BOUNDARY, context)
        output_folder  = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        elev_step      = self.parameterAsDouble(parameters, self.ELEV_STEP, context)

        # ------------------------------------------------- Read plot controls
        plot_cfg = dict(
            elev_min  = self.parameterAsDouble(parameters, self.PLOT_ELEV_MIN,  context),
            elev_max  = self.parameterAsDouble(parameters, self.PLOT_ELEV_MAX,  context),
            vol_max   = self.parameterAsDouble(parameters, self.PLOT_VOL_MAX,   context),
            area_max  = self.parameterAsDouble(parameters, self.PLOT_AREA_MAX,  context),
            y_tick    = self.parameterAsDouble(parameters, self.PLOT_Y_TICK,    context),
            x_tick_v  = self.parameterAsDouble(parameters, self.PLOT_X_TICK_V,  context),
            x_tick_a  = self.parameterAsDouble(parameters, self.PLOT_X_TICK_A,  context),
        )

        os.makedirs(output_folder, exist_ok=True)

        feedback.pushInfo("=" * 55)
        feedback.pushInfo("  Storage Capacity Curve — Multi-DEM Mode")
        feedback.pushInfo("=" * 55)
        feedback.pushInfo(f"  DEMs      : {len(dem_layers)}")
        feedback.pushInfo(f"  Boundary  : {boundary_layer.name() if boundary_layer else 'None (Full DEM)'}")
        feedback.pushInfo(f"  Elev step : {elev_step} m")
        feedback.pushInfo(f"  Output    : {output_folder}")
        feedback.pushInfo("\n  Plot axis settings:")
        feedback.pushInfo(f"    Elev Y  : {plot_cfg['elev_min'] or 'auto'} – {plot_cfg['elev_max'] or 'auto'} m")
        feedback.pushInfo(f"    Vol  X  : 0 – {plot_cfg['vol_max']  or 'auto'} (1,000 m³)")
        feedback.pushInfo(f"    Area X  : 0 – {plot_cfg['area_max'] or 'auto'} ha")
        feedback.pushInfo(f"    Y tick  : {plot_cfg['y_tick']   or 'auto'} m")
        feedback.pushInfo(f"    Vol tick: {plot_cfg['x_tick_v'] or 'auto'} (1,000 m³)")
        feedback.pushInfo(f"    Area tick:{plot_cfg['x_tick_a'] or 'auto'} ha")

        results, failed = [], []

        for i, dem_layer in enumerate(dem_layers):
            if feedback.isCanceled():
                feedback.pushInfo("⚠ Cancelled by user")
                break

            feedback.pushInfo(f"\n{'─'*55}")
            feedback.pushInfo(f"[{i+1}/{len(dem_layers)}]  {dem_layer.name()}")
            feedback.pushInfo(f"{'─'*55}")

            try:
                result = self._process_single_dem(
                    dem_layer, boundary_layer, output_folder,
                    elev_step, plot_cfg, feedback, i, len(dem_layers)
                )
                if result:
                    results.append(result)
                    feedback.pushInfo(f"  ✅ Done : {dem_layer.name()}")
                else:
                    failed.append(dem_layer.name())
            except Exception as e:
                feedback.pushInfo(f"  ❌ Error: {dem_layer.name()} → {str(e)}")
                failed.append(dem_layer.name())

        feedback.pushInfo(f"\n{'═'*55}")
        feedback.pushInfo(f"  COMPLETED : {len(results)}/{len(dem_layers)}")
        if failed:
            feedback.pushInfo(f"  FAILED    : {', '.join(failed)}")
        feedback.pushInfo(f"  Output    : {output_folder}")
        feedback.pushInfo(f"{'═'*55}")
        return {"OUTPUT_FOLDER": output_folder}

    def _process_single_dem(self, dem_layer, boundary_layer,
                             output_folder, elev_step, plot_cfg,
                             feedback, idx, total):

        dem_ds = gdal.Open(dem_layer.source())
        if dem_ds is None:
            feedback.pushInfo(f"  ❌ Cannot open: {dem_layer.source()}")
            return False

        band   = dem_ds.GetRasterBand(1)
        arr    = band.ReadAsArray().astype(np.float64)
        gt     = dem_ds.GetGeoTransform()
        nodata = band.GetNoDataValue()
        if nodata is not None:
            arr[arr == nodata] = np.nan

        pixel_w    = abs(gt[1])
        pixel_h    = abs(gt[5])
        pixel_area = pixel_w * pixel_h
        feedback.pushInfo(f"  Pixel size : {pixel_w:.3f} x {pixel_h:.3f} m")

        if boundary_layer:
            feedback.pushInfo("  Rasterizing boundary ...")
            cols = dem_ds.RasterXSize
            rows = dem_ds.RasterYSize
            prj  = dem_ds.GetProjection()

            mask_ds = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
            mask_ds.SetGeoTransform(gt)
            mask_ds.SetProjection(prj)
            mask_ds.GetRasterBand(1).Fill(0)

            vec_path = boundary_layer.source().split("|")[0]
            vec_ds   = ogr.Open(vec_path)
            vec_lyr  = vec_ds.GetLayer()

            dem_srs = osr.SpatialReference()
            dem_srs.ImportFromWkt(prj)
            vec_srs = vec_lyr.GetSpatialRef()

            if vec_srs and not dem_srs.IsSame(vec_srs):
                feedback.pushInfo("  ⚠ CRS mismatch — reprojecting ...")
                mem_vec   = ogr.GetDriverByName("Memory").CreateDataSource("")
                out_lyr   = mem_vec.CreateLayer("reproj", dem_srs, ogr.wkbPolygon)
                transform = osr.CoordinateTransformation(vec_srs, dem_srs)
                for feat in vec_lyr:
                    geom = feat.GetGeometryRef().Clone()
                    geom.Transform(transform)
                    new_feat = ogr.Feature(out_lyr.GetLayerDefn())
                    new_feat.SetGeometry(geom)
                    out_lyr.CreateFeature(new_feat)
                vec_lyr = out_lyr

            geom_type = vec_lyr.GetGeomType()
            is_line   = geom_type in [
                ogr.wkbLineString, ogr.wkbMultiLineString,
                ogr.wkbLineString25D, ogr.wkbMultiLineString25D
            ]
            opts = ["ALL_TOUCHED=TRUE"] if is_line else []
            gdal.RasterizeLayer(mask_ds, [1], vec_lyr, burn_values=[1], options=opts)
            mask_ds.FlushCache()
            arr[mask_ds.GetRasterBand(1).ReadAsArray() != 1] = np.nan
            dem_ds = mask_ds = None
            feedback.pushInfo(f"  ✅ Boundary mask applied ({'Polyline' if is_line else 'Polygon'})")
        else:
            dem_ds = None
            feedback.pushInfo("  ✅ Full DEM extent (no boundary)")

        valid_pixels  = int(np.sum(~np.isnan(arr)))
        total_area_ha = valid_pixels * pixel_area / 10000
        elev_min_data = float(np.nanmin(arr))
        elev_max_data = float(np.nanmax(arr))
        feedback.pushInfo(f"  Elevation  : {elev_min_data:.3f} – {elev_max_data:.3f} m")
        feedback.pushInfo(f"  Area       : {total_area_ha:.4f} ha ({valid_pixels:,} px)")

        elevations = np.round(np.arange(elev_min_data, elev_max_data + elev_step, elev_step), 3)
        feedback.pushInfo(f"  Steps      : {len(elevations)} levels")
        feedback.pushInfo("  Calculating ...")

        volumes, inundated_areas = [], []
        for j, elev in enumerate(elevations):
            feedback.setProgress(int(((idx + j / len(elevations)) / total) * 100))
            if feedback.isCanceled():
                return False
            depth = np.where(np.isnan(arr), 0, np.maximum(elev - arr, 0))
            volumes.append(round(float(np.sum(depth) * pixel_area / 1e6), 6))
            inundated_areas.append(round(float(np.sum(depth > 0) * pixel_area / 10000), 6))

        df = pd.DataFrame({
            'Elevation_m'   : [round(float(e), 3) for e in elevations],
            'Volume_1000m3' : volumes,
            'Area_ha'       : inundated_areas,
        })

        boundary_name = boundary_layer.name() if boundary_layer else None
        suffix        = f"_{boundary_name}" if boundary_name else "_fullDEM"
        csv_path      = os.path.join(output_folder, f"volume_{dem_layer.name()}{suffix}.csv")
        df.to_csv(csv_path, index=False)
        feedback.pushInfo(f"  ✅ CSV  → {csv_path}")
        feedback.pushInfo(f"     Rows       : {len(df)}")
        feedback.pushInfo(f"     Max volume : {df['Volume_1000m3'].max():.4f} (1,000 m³)")
        feedback.pushInfo(f"     Max area   : {df['Area_ha'].max():.4f} ha")

        self._plot_graph(df, dem_layer.name(), boundary_name,
                         output_folder, elev_step, plot_cfg, feedback)
        return csv_path

    def _plot_graph(self, df, dem_name, boundary_name,
                    output_folder, elev_step, plot_cfg, feedback):

        elev_s = df["Elevation_m"]
        vol_s  = df["Volume_1000m3"]
        area_s = df["Area_ha"]

        # ------------------------------------- Resolve axis limits (0 = auto)
        y_min  = plot_cfg["elev_min"] if plot_cfg["elev_min"] > 0 else elev_s.min()
        y_max  = plot_cfg["elev_max"] if plot_cfg["elev_max"] > 0 else elev_s.max()
        vx_max = plot_cfg["vol_max"]  if plot_cfg["vol_max"]  > 0 else None
        ax_max = plot_cfg["area_max"] if plot_cfg["area_max"] > 0 else None

        title_suffix = f"({boundary_name})" if boundary_name else "(Full DEM)"
        fig = plt.figure(figsize=(14, 7), dpi=150)
        fig.patch.set_facecolor("#FFFFFF")
        fig.suptitle(
            f"Storage Capacity Curve — {dem_name} {title_suffix}",
            fontsize=14, fontweight="bold",
            fontfamily="Times New Roman", color="#1A1A2E", y=0.98
        )

        gs  = GridSpec(1, 2, figure=fig, wspace=0.38,
                       left=0.08, right=0.96, top=0.90, bottom=0.18)
        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[0, 1])

        # ----------------------------------------- LEFT : Elevation vs Volume
        ax1.fill_betweenx(elev_s, vol_s, alpha=0.15, color="#1565C0", zorder=2)
        ax1.plot(vol_s, elev_s, color="#1565C0", linewidth=2.2, zorder=3)
        ax1.set_facecolor("#F7F9FC")
        ax1.set_xlabel("Volume (1,000 m³)", fontsize=10,
                       fontfamily="Times New Roman", labelpad=8)
        ax1.set_ylabel("Elevation (m)", fontsize=10,
                       fontfamily="Times New Roman", labelpad=8)
        ax1.set_title("Elevation vs Volume", fontsize=11,
                      fontfamily="Times New Roman", fontweight="bold",
                      pad=10, color="#1A1A2E")
        ax1.tick_params(labelsize=8, direction="in", length=4)
        ax1.grid(True, linestyle="--", linewidth=0.6, alpha=0.5, color="#BBBBBB")
        ax1.set_axisbelow(True)
        for sp in ax1.spines.values():
            sp.set_linewidth(0.8); sp.set_color("#AAAAAA")

        # Apply Y / X limits
        ax1.set_ylim(y_min, y_max)
        if vx_max:
            ax1.set_xlim(0, vx_max)

        # Apply tick intervals
        if plot_cfg["y_tick"] > 0:
            ax1.yaxis.set_major_locator(ticker.MultipleLocator(plot_cfg["y_tick"]))
        if plot_cfg["x_tick_v"] > 0:
            ax1.xaxis.set_major_locator(ticker.MultipleLocator(plot_cfg["x_tick_v"]))

        ax1.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.1f}"))
        ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.1f}"))

        # + markers at Y grid intersections
        fig.canvas.draw()
        y_ticks = [t for t in ax1.get_yticks() if elev_s.min() <= t <= elev_s.max()]
        vol_at_yticks = np.interp(y_ticks, elev_s.values, vol_s.values)
        ax1.plot(vol_at_yticks, y_ticks, marker="+", markersize=10,
                 markeredgewidth=1.8, markeredgecolor="#E53935",
                 linestyle="none", zorder=5, label="Grid intersections")

        # Max annotation
        mi = vol_s.idxmax()
        ax1.annotate(
            f"  Max: {vol_s[mi]:,.3f} (1,000 m³)\n  Elev: {elev_s[mi]:.2f} m",
            xy=(vol_s[mi], elev_s[mi]),
            xytext=(vol_s[mi]*0.55, elev_s[mi]-(elev_s.max()-elev_s.min())*0.12),
            fontsize=8, fontfamily="Times New Roman", color="#1565C0",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#1565C0", lw=0.8, alpha=0.9),
            arrowprops=dict(arrowstyle="->", color="#1565C0", lw=1.0, connectionstyle="arc3,rad=0.2")
        )
        ax1.axhline(y=elev_s.min(), color="#E53935", linewidth=0.8, linestyle=":", alpha=0.7,
                    label=f"Min Elev: {elev_s.min():.2f} m")
        ax1.axhline(y=elev_s.max(), color="#1565C0", linewidth=0.8, linestyle=":", alpha=0.7,
                    label=f"Max Elev: {elev_s.max():.2f} m")
        ax1.legend(fontsize=7.5, loc="lower right",
                   prop={"family": "Times New Roman"}, framealpha=0.9, edgecolor="#CCCCCC")

        # ------------------------------------------ RIGHT : Elevation vs Area
        ax2.fill_betweenx(elev_s, area_s, alpha=0.15, color="#2E7D32", zorder=2)
        ax2.plot(area_s, elev_s, color="#2E7D32", linewidth=2.2, zorder=3)
        ax2.set_facecolor("#F7FCF7")
        ax2.set_xlabel("Inundated Area (ha)", fontsize=10,
                       fontfamily="Times New Roman", labelpad=8)
        ax2.set_ylabel("Elevation (m)", fontsize=10,
                       fontfamily="Times New Roman", labelpad=8)
        ax2.set_title("Elevation vs Area", fontsize=11,
                      fontfamily="Times New Roman", fontweight="bold",
                      pad=10, color="#1A1A2E")
        ax2.tick_params(labelsize=8, direction="in", length=4)
        ax2.grid(True, linestyle="--", linewidth=0.6, alpha=0.5, color="#BBBBBB")
        ax2.set_axisbelow(True)
        for sp in ax2.spines.values():
            sp.set_linewidth(0.8); sp.set_color("#AAAAAA")

        # Apply Y / X limits
        ax2.set_ylim(y_min, y_max)
        if ax_max:
            ax2.set_xlim(0, ax_max)

        # Apply tick intervals
        if plot_cfg["y_tick"] > 0:
            ax2.yaxis.set_major_locator(ticker.MultipleLocator(plot_cfg["y_tick"]))
        if plot_cfg["x_tick_a"] > 0:
            ax2.xaxis.set_major_locator(ticker.MultipleLocator(plot_cfg["x_tick_a"]))

        ax2.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.2f}"))
        ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.1f}"))

        # Max annotation
        ai = area_s.idxmax()
        ax2.annotate(
            f"  Max: {area_s[ai]:,.3f} ha\n  Elev: {elev_s[ai]:.2f} m",
            xy=(area_s[ai], elev_s[ai]),
            xytext=(area_s[ai]*0.45, elev_s[ai]-(elev_s.max()-elev_s.min())*0.12),
            fontsize=8, fontfamily="Times New Roman", color="#2E7D32",
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#2E7D32", lw=0.8, alpha=0.9),
            arrowprops=dict(arrowstyle="->", color="#2E7D32", lw=1.0, connectionstyle="arc3,rad=0.2")
        )
        ax2.axhline(y=elev_s.min(), color="#E53935", linewidth=0.8, linestyle=":", alpha=0.7,
                    label=f"Min Elev: {elev_s.min():.2f} m")
        ax2.axhline(y=elev_s.max(), color="#2E7D32", linewidth=0.8, linestyle=":", alpha=0.7,
                    label=f"Max Elev: {elev_s.max():.2f} m")
        ax2.legend(fontsize=7.5, loc="lower right",
                   prop={"family": "Times New Roman"}, framealpha=0.9, edgecolor="#CCCCCC")

        # ----------------------------------------------------------- Info bar
        # Show which axes are custom vs auto
        y_info  = (f"{y_min:.2f}–{y_max:.2f} m (custom)"
                   if plot_cfg["elev_min"] > 0 or plot_cfg["elev_max"] > 0
                   else f"{elev_s.min():.2f}–{elev_s.max():.2f} m (auto)")
        vx_info = (f"0–{vx_max:.2f} (custom)" if vx_max
                   else f"0–{vol_s.max():.2f} (auto)")
        ax_info = (f"0–{ax_max:.2f} ha (custom)" if ax_max
                   else f"0–{area_s.max():.2f} ha (auto)")

        info = (
            f"DEM: {dem_name}   |   "
            f"Boundary: {boundary_name or 'Full DEM'}   |   "
            f"Elev Y: {y_info}   |   "
            f"Vol X: {vx_info}   |   "
            f"Area X: {ax_info}   |   "
            f"Step: {elev_step} m"
        )
        fig.text(0.5, 0.055, info, ha="center", fontsize=7.5,
                 fontfamily="Times New Roman", color="#555555",
                 bbox=dict(boxstyle="round,pad=0.4", fc="#F0F0F0", ec="#CCCCCC", lw=0.8))
        fig.text(0.5, 0.01,
                 "Generated by QGIS Storage Capacity Tool  |  "
                 "Volume Unit: 1,000 m³  |  Area Unit: ha",
                 ha="center", fontsize=7, fontfamily="Times New Roman",
                 color="#999999", style="italic")

        suffix   = f"_{boundary_name}" if boundary_name else "_fullDEM"
        png_path = os.path.join(output_folder, f"graph_{dem_name}{suffix}.png")
        fig.savefig(png_path, dpi=150, bbox_inches="tight", facecolor="#FFFFFF")
        plt.close(fig)
        feedback.pushInfo(f"  ✅ Graph → {png_path}")

    # --------------------------------------------------------------- Metadata
    def name(self):        return "storage_capacity_curve"
    def displayName(self): return "Storage Capacity Curve"
    def group(self):       return "KGA Irrigation Tools"
    def groupId(self):     return "kgairrigationtools"

    def helpUrl(self):
        return docs_url('storage_capacity_curve')
    def createInstance(self): return StorageCapacityCurve()
    def tr(self, s): return QCoreApplication.translate("Processing", s)

    def shortHelpString(self):
        return (
            "<b>Storage Capacity Curve</b><br><br>"
            "Calculates Volume &amp; Area vs Elevation for one or more DEMs.<br><br>"
            "<b>Inputs:</b><br>"
            "• DEM raster(s)<br>"
            "• Boundary vector (optional)<br>"
            "• Elevation step (m)<br><br>"
            "<b>Plot axis controls</b> (all optional — leave 0 for auto):<br>"
            "• Elevation Y-axis min / max (m)<br>"
            "• Volume X-axis max (1,000 m³)<br>"
            "• Area X-axis max (ha)<br>"
            "• Y-axis tick interval (m)<br>"
            "• Volume X tick interval (1,000 m³)<br>"
            "• Area X tick interval (ha)<br><br>"
            "<b>Outputs per DEM:</b><br>"
            "• CSV → volume_{name}.csv<br>"
            "• PNG → graph_{name}.png<br><br>"
            "Info bar on the chart shows which axes are custom vs auto."
        )

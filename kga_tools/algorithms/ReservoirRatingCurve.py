"""Reservoir Rating Curve — elevation vs outflow.

Builds a static rating curve (water level vs total discharge) for one or more
reservoir DEMs. Outlets are defined either in three fixed form slots or, for
more than three, in a CSV table.

    Weir             Q = C  * L * H^1.5
    Orifice / Pipe   Q = Cd * A * sqrt(2 g H)
    H = water level - activation elevation, and Q = 0 while H <= 0.

The DEM is only used for the elevation range the curve is tabulated over; the
optional boundary clips it to the reservoir. Per DEM the tool writes
rating_{DEM}_{boundary}.csv and a matching .png.
"""

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
import numpy as np
import pandas as pd
import os
import math
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from osgeo import gdal, ogr, osr

GRAVITY      = 9.81
OUTLET_TYPES = ["Weir", "Orifice", "Pipe"]
INPUT_MODES  = ["Fixed slots — up to 3 outlets, defined below",
                "CSV file — any number of outlets, from a table"]
CSV_COLUMNS  = ["outlet_id", "type", "act_elev", "LA", "C_Cd"]
OUTLET_COLORS = {
    0: "#1565C0", 1: "#E65100", 2: "#6A1B9A",
    3: "#00695C", 4: "#F57F17", 5: "#880E4F",
    6: "#1B5E20", 7: "#BF360C",
}

# Defaults per outlet slot: (enabled, type index, size, coefficient).
SLOT_DEFAULTS = [
    (True,  0, 1.0, 1.84),
    (False, 1, 0.5, 0.61),
    (False, 2, 0.3, 0.61),
]


# ══════════════════════════════════════════════════════════════
#  Outlet helpers
# ══════════════════════════════════════════════════════════════
def compute_q(type_name, elev, act_elev, la, c_cd):
    """Discharge through one outlet at a given water level."""
    H = float(elev) - float(act_elev)
    if H <= 0.0:
        return 0.0
    t = type_name.strip().capitalize()
    if t == "Weir":
        return float(c_cd) * float(la) * (H ** 1.5)
    elif t in ["Orifice", "Pipe"]:
        return float(c_cd) * float(la) * math.sqrt(2.0 * GRAVITY * H)
    else:
        raise ValueError(f"Unknown type: '{type_name}'. Use Weir, Orifice or Pipe.")


def size_label(type_name):
    """The symbol the size column carries for this outlet type."""
    return "L" if type_name == "Weir" else "A"


def q_column(outlet):
    """Column name a single outlet's discharge is written under."""
    return (f"Q_Outlet{outlet['id']}_{outlet['type']}_"
            f"actElev{outlet['act_elev']:.2f}m_m3s")


def describe_outlet(outlet):
    """One-line summary used in the log, the legend and the figure footer."""
    return (f"Outlet {outlet['id']}: {outlet['type']} | "
            f"act.elev={outlet['act_elev']:.2f} m | "
            f"{size_label(outlet['type'])}={outlet['la']:g} | "
            f"C/Cd={outlet['c_cd']:g}")


# ══════════════════════════════════════════════════════════════
#  Load outlets from CSV
# ══════════════════════════════════════════════════════════════
def load_outlets_from_csv(csv_path, feedback=None):
    """Read an outlet table. Raises ValueError with a message fit for the UI."""
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        raise ValueError(f"Could not read the CSV: {exc}")

    df.columns = df.columns.str.strip()
    missing = [c for c in CSV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "CSV is missing the column(s): {}.\nRequired header: {}".format(
                ", ".join(missing), ",".join(CSV_COLUMNS)))
    if df.empty:
        raise ValueError("CSV has a header but no outlet rows.")

    outlets = []
    seen_ids = set()
    for line_no, (_, row) in enumerate(df.iterrows(), start=2):
        t = str(row["type"]).strip().capitalize()
        if t not in OUTLET_TYPES:
            raise ValueError(
                f"Row {line_no}: type='{row['type']}' is not valid. "
                f"Use Weir, Orifice or Pipe.")
        try:
            oid  = int(row["outlet_id"])
            act  = float(row["act_elev"])
            la   = float(row["LA"])
            c_cd = float(row["C_Cd"])
        except (TypeError, ValueError):
            raise ValueError(
                f"Row {line_no}: outlet_id, act_elev, LA and C_Cd must all be "
                f"numbers.")
        if oid in seen_ids:
            # Ids name the output columns, so duplicates would collide.
            raise ValueError(
                f"Row {line_no}: outlet_id={oid} is used more than once. "
                f"Ids must be unique.")
        if la <= 0:
            raise ValueError(f"Row {line_no}: LA must be greater than 0.")
        if c_cd <= 0:
            raise ValueError(f"Row {line_no}: C_Cd must be greater than 0.")
        seen_ids.add(oid)

        o = {"id": oid, "type": t, "act_elev": act, "la": la, "c_cd": c_cd}
        outlets.append(o)
        if feedback is not None:
            feedback.pushInfo(f"    {describe_outlet(o)}")
    return outlets


# ══════════════════════════════════════════════════════════════
#  Algorithm
# ══════════════════════════════════════════════════════════════
class ReservoirRatingCurve(QgsProcessingAlgorithm):

    INPUT_DEMS     = "INPUT_DEMS"
    INPUT_BOUNDARY = "INPUT_BOUNDARY"
    ELEV_STEP      = "ELEV_STEP"
    INPUT_MODE     = "INPUT_MODE"
    OUTLET_CSV     = "OUTLET_CSV"
    OUTPUT_FOLDER  = "OUTPUT_FOLDER"

    PLOT_ELEV_MIN = "PLOT_ELEV_MIN"
    PLOT_ELEV_MAX = "PLOT_ELEV_MAX"
    PLOT_Q_MAX    = "PLOT_Q_MAX"
    PLOT_Y_TICK   = "PLOT_Y_TICK"
    PLOT_X_TICK   = "PLOT_X_TICK"
    PLOT_DPI      = "PLOT_DPI"

    @staticmethod
    def slot_param(slot, key):
        """Per-slot parameter name, built the same way by the form and the
        reader so the two cannot drift apart. Keys: ENABLE TYPE ELEV LA C."""
        return "O{}_{}".format(slot, key)

    # ══════════════════════════════════════════
    # ── Parameters ────────────────────────────
    # ══════════════════════════════════════════
    def _add(self, param, help_text, advanced=False):
        """Add a parameter, with its help shown as the widget tooltip."""
        param.setHelp(help_text)
        if advanced:
            param.setFlags(
                param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)

    def initAlgorithm(self, config=None):

        # ── Reservoir ─────────────────────────
        self._add(QgsProcessingParameterMultipleLayers(
            self.INPUT_DEMS, "DEM Raster Layer(s)",
            layerType=QgsProcessing.TypeRaster),
            "One rating curve is produced per DEM. The DEM supplies the "
            "elevation range the curve is tabulated over: its lowest and "
            "highest cell inside the boundary become the first and last row "
            "of the table.")

        self._add(QgsProcessingParameterVectorLayer(
            self.INPUT_BOUNDARY,
            "Reservoir Boundary (optional — leave blank for full DEM)",
            optional=True),
            "Polygon or polyline limiting which DEM cells set the elevation "
            "range. Polylines are burned with ALL_TOUCHED. Reprojected to the "
            "DEM's CRS automatically. Leave empty to use the whole DEM.")

        self._add(QgsProcessingParameterNumber(
            self.ELEV_STEP, "Elevation Step (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.1, minValue=0.001),
            "Water-level increment between rows of the rating table. 0.10 m "
            "suits most reservoirs; 0.01 m gives a smoother curve and a much "
            "longer table.")

        # ── How outlets are supplied ──────────
        self._add(QgsProcessingParameterEnum(
            self.INPUT_MODE, "Outlet Definition Mode",
            options=INPUT_MODES, defaultValue=0),
            "Fixed slots: fill in up to three outlets on this form and leave "
            "the CSV field empty.\n"
            "CSV file: read any number of outlets from a table; the three "
            "slots below are then ignored.")

        self._add(QgsProcessingParameterFile(
            self.OUTLET_CSV, "CSV Mode · Outlet Table (.csv)",
            fileFilter="CSV files (*.csv *.CSV)", optional=True),
            "Only read when the mode above is set to CSV file.\n"
            "Header: outlet_id,type,act_elev,LA,C_Cd\n"
            "Example row: 2,Orifice,35.0,0.20,0.61\n"
            "outlet_id must be unique — it names the output column.")

        # ── Outlet slots ──────────────────────
        for slot in (1, 2, 3):
            enabled, type_idx, size, coeff = SLOT_DEFAULTS[slot - 1]
            tag = "Outlet {} ·".format(slot)

            self._add(QgsProcessingParameterBoolean(
                self.slot_param(slot, "ENABLE"), tag + " Enable",
                defaultValue=enabled),
                "Include this outlet in the curve. Unticked slots are skipped "
                "entirely and their other fields are ignored. Only used in "
                "Fixed slots mode.")

            self._add(QgsProcessingParameterEnum(
                self.slot_param(slot, "TYPE"), tag + " Type",
                options=OUTLET_TYPES, defaultValue=type_idx),
                "Weir — Q = C x L x H^1.5\n"
                "Orifice — Q = Cd x A x sqrt(2gH), A = width x height\n"
                "Pipe — Q = Cd x A x sqrt(2gH), A = pi x (D/2)^2\n"
                "The type decides how the two fields below are read.")

            self._add(QgsProcessingParameterNumber(
                self.slot_param(slot, "ELEV"),
                tag + " Activation Elevation (m)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0),
                "Crest level of a weir, or centreline of an orifice or pipe, "
                "in the DEM's vertical datum. The outlet stays dry (Q = 0) "
                "until the water level rises above it.")

            self._add(QgsProcessingParameterNumber(
                self.slot_param(slot, "LA"),
                tag + " Size — L (m) or A (m²)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=size, minValue=0.001),
                "Weir — L, the crest width in m.\n"
                "Orifice — A, the opening area in m²: width x height.\n"
                "Pipe — A, the bore area in m²: pi x (D/2)². A 300 mm pipe "
                "is 0.0707 m².\n"
                "Areas are not derived for you — enter the computed value.")

            self._add(QgsProcessingParameterNumber(
                self.slot_param(slot, "C"),
                tag + " Coefficient — C or Cd",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=coeff, minValue=0.001),
                "Weir, C (SI units): sharp-crested 1.84, broad-crested 1.70, "
                "ogee spillway 2.0–2.2.\n"
                "Orifice or pipe, Cd: sharp-edged 0.61, rounded entry "
                "0.80–0.90.")

        # ── Output ────────────────────────────
        self._add(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, "Output Folder"),
            "Receives rating_{DEM}_{boundary}.csv and a matching .png for "
            "every DEM. Files of the same name are overwritten.")

        # ── Plot appearance (advanced) ────────
        self._add(QgsProcessingParameterNumber(
            self.PLOT_ELEV_MIN,
            "Plot · Elevation Axis Minimum (m)  [0 = auto]",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, optional=True),
            "Pins the bottom of the elevation axis so several reservoirs can "
            "be compared side by side. 0 keeps the DEM minimum.", True)

        self._add(QgsProcessingParameterNumber(
            self.PLOT_ELEV_MAX,
            "Plot · Elevation Axis Maximum (m)  [0 = auto]",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, optional=True),
            "Pins the top of the elevation axis. 0 keeps the DEM maximum.",
            True)

        self._add(QgsProcessingParameterNumber(
            self.PLOT_Q_MAX,
            "Plot · Discharge Axis Maximum (m³/s)  [0 = auto]",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0, optional=True),
            "Pins the right-hand end of the discharge axis. 0 fits it to the "
            "computed peak.", True)

        self._add(QgsProcessingParameterNumber(
            self.PLOT_Y_TICK,
            "Plot · Elevation Tick Interval (m)  [0 = auto]",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0, optional=True),
            "Spacing of the horizontal grid lines. Q_total is marked with a "
            "cross at each of them, so this also sets where the read-off "
            "points sit.", True)

        self._add(QgsProcessingParameterNumber(
            self.PLOT_X_TICK,
            "Plot · Discharge Tick Interval (m³/s)  [0 = auto]",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0, optional=True),
            "Spacing of the vertical grid lines.", True)

        self._add(QgsProcessingParameterNumber(
            self.PLOT_DPI, "Plot · Resolution (DPI)",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=150, minValue=72, maxValue=600),
            "150 is fine on screen and in a report; 300 for print.", True)

    # ══════════════════════════════════════════
    # ── Validation, before the run starts ─────
    # ══════════════════════════════════════════
    def checkParameterValues(self, parameters, context):
        ok, msg = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, msg

        mode = self.parameterAsEnum(parameters, self.INPUT_MODE, context)

        if mode == 1:
            csv_path = self.parameterAsFile(
                parameters, self.OUTLET_CSV, context)
            if not csv_path:
                return False, self.tr(
                    "CSV mode is selected but no outlet table was chosen. "
                    "Pick a .csv file, or set the mode to Fixed slots.")
            if not os.path.isfile(csv_path):
                return False, self.tr(
                    "Outlet table not found: {}".format(csv_path))
            try:
                load_outlets_from_csv(csv_path)
            except ValueError as exc:
                return False, self.tr(str(exc))
        else:
            enabled = [s for s in (1, 2, 3)
                       if self.parameterAsBool(
                           parameters, self.slot_param(s, "ENABLE"), context)]
            if not enabled:
                return False, self.tr(
                    "No outlet is enabled. Tick at least one "
                    "'Outlet n · Enable', or set the mode to CSV file.")

        e_min = self.parameterAsDouble(parameters, self.PLOT_ELEV_MIN, context)
        e_max = self.parameterAsDouble(parameters, self.PLOT_ELEV_MAX, context)
        if e_min and e_max and e_max <= e_min:
            return False, self.tr(
                "Plot elevation axis maximum ({}) must be above the "
                "minimum ({}).".format(e_max, e_min))

        return True, None

    # ══════════════════════════════════════════
    # ── Main process ──────────────────────────
    # ══════════════════════════════════════════
    def processAlgorithm(self, parameters, context, feedback):

        dem_layers     = self.parameterAsLayerList(
            parameters, self.INPUT_DEMS, context)
        boundary_layer = self.parameterAsVectorLayer(
            parameters, self.INPUT_BOUNDARY, context)
        output_folder  = self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context)
        elev_step      = self.parameterAsDouble(
            parameters, self.ELEV_STEP, context)
        input_mode     = self.parameterAsEnum(
            parameters, self.INPUT_MODE, context)

        plot_opts = {
            "elev_min": self.parameterAsDouble(
                parameters, self.PLOT_ELEV_MIN, context),
            "elev_max": self.parameterAsDouble(
                parameters, self.PLOT_ELEV_MAX, context),
            "q_max": self.parameterAsDouble(
                parameters, self.PLOT_Q_MAX, context),
            "y_tick": self.parameterAsDouble(
                parameters, self.PLOT_Y_TICK, context),
            "x_tick": self.parameterAsDouble(
                parameters, self.PLOT_X_TICK, context),
            "dpi": self.parameterAsInt(parameters, self.PLOT_DPI, context),
        }

        if not dem_layers:
            raise QgsProcessingException(
                "No DEM selected. Pick at least one raster layer.")

        os.makedirs(output_folder, exist_ok=True)

        # ── Load outlets ──────────────────────
        feedback.pushInfo("=" * 60)
        feedback.pushInfo("  Reservoir Rating Curve — Elevation vs Q_out")
        feedback.pushInfo("=" * 60)
        feedback.pushInfo("  Outlets from : {}".format(INPUT_MODES[input_mode]))

        if input_mode == 1:
            csv_path = self.parameterAsFile(
                parameters, self.OUTLET_CSV, context)
            feedback.pushInfo("  CSV file     : {}".format(csv_path))
            try:
                outlets = load_outlets_from_csv(csv_path, feedback)
            except ValueError as exc:
                raise QgsProcessingException(str(exc))
        else:
            outlets = []
            for slot in (1, 2, 3):
                if not self.parameterAsBool(
                        parameters, self.slot_param(slot, "ENABLE"), context):
                    continue
                t_idx = self.parameterAsEnum(
                    parameters, self.slot_param(slot, "TYPE"), context)
                o = {
                    "id"      : slot,
                    "type"    : OUTLET_TYPES[t_idx],
                    "act_elev": self.parameterAsDouble(
                        parameters, self.slot_param(slot, "ELEV"), context),
                    "la"      : self.parameterAsDouble(
                        parameters, self.slot_param(slot, "LA"), context),
                    "c_cd"    : self.parameterAsDouble(
                        parameters, self.slot_param(slot, "C"), context),
                }
                outlets.append(o)
                feedback.pushInfo("    {}".format(describe_outlet(o)))

        if not outlets:
            # checkParameterValues normally catches this first.
            raise QgsProcessingException(
                "No outlets defined. Enable at least one outlet slot, or "
                "supply an outlet CSV.")

        feedback.pushInfo("  DEMs         : {}".format(len(dem_layers)))
        feedback.pushInfo("  Boundary     : {}".format(
            boundary_layer.name() if boundary_layer else "none (full DEM)"))
        feedback.pushInfo("  Elev. step   : {:g} m".format(elev_step))
        feedback.pushInfo("  Outlets      : {}".format(len(outlets)))
        feedback.pushInfo("  Output       : {}".format(output_folder))
        feedback.pushInfo("=" * 60)

        results = []
        failed  = []

        for i, dem_layer in enumerate(dem_layers):
            if feedback.isCanceled():
                feedback.pushInfo("⚠ Cancelled by user")
                break
            feedback.pushInfo("\n{}".format("─" * 60))
            feedback.pushInfo("[{}/{}] {}".format(
                i + 1, len(dem_layers), dem_layer.name()))
            feedback.pushInfo("─" * 60)
            try:
                result = self._process_single_dem(
                    dem_layer=dem_layer,
                    boundary_layer=boundary_layer,
                    output_folder=output_folder,
                    elev_step=elev_step,
                    outlets=outlets,
                    plot_opts=plot_opts,
                    feedback=feedback,
                    idx=i,
                    total=len(dem_layers))
                if result:
                    results.append(result)
                    feedback.pushInfo("✅ Completed : {}".format(
                        dem_layer.name()))
                else:
                    failed.append(dem_layer.name())
            except Exception as exc:
                # One bad DEM should not lose the curves already computed.
                feedback.reportError("❌ Failed : {} → {}".format(
                    dem_layer.name(), exc))
                failed.append(dem_layer.name())

        feedback.pushInfo("\n{}".format("═" * 60))
        feedback.pushInfo("  COMPLETED : {} / {} DEMs".format(
            len(results), len(dem_layers)))
        if failed:
            feedback.reportError("  FAILED    : {}".format(", ".join(failed)))
        feedback.pushInfo("  Output    : {}".format(output_folder))
        feedback.pushInfo("═" * 60)

        if not results:
            raise QgsProcessingException(
                "No rating curve could be produced. See the log above.")

        return {"OUTPUT_FOLDER": output_folder}

    # ══════════════════════════════════════════
    # ── Process single DEM ────────────────────
    # ══════════════════════════════════════════
    def _process_single_dem(self, dem_layer, boundary_layer,
                            output_folder, elev_step, outlets,
                            plot_opts, feedback, idx, total):

        dem_ds = gdal.Open(dem_layer.source())
        if dem_ds is None:
            raise QgsProcessingException(
                "Cannot open raster: {}".format(dem_layer.source()))

        band   = dem_ds.GetRasterBand(1)
        arr    = band.ReadAsArray().astype(np.float64)
        gt     = dem_ds.GetGeoTransform()
        nodata = band.GetNoDataValue()
        if nodata is not None:
            arr[arr == nodata] = np.nan

        feedback.pushInfo("   Pixel size  : {:.4f} × {:.4f}".format(
            abs(gt[1]), abs(gt[5])))

        # ── Boundary mask ─────────────────────
        if boundary_layer:
            feedback.pushInfo("   Rasterizing boundary...")
            cols = dem_ds.RasterXSize
            rows = dem_ds.RasterYSize
            prj  = dem_ds.GetProjection()

            mask_ds = gdal.GetDriverByName("MEM").Create(
                "", cols, rows, 1, gdal.GDT_Byte)
            mask_ds.SetGeoTransform(gt)
            mask_ds.SetProjection(prj)
            mask_ds.GetRasterBand(1).Fill(0)

            vec_path = boundary_layer.source().split("|")[0]
            vec_ds   = ogr.Open(vec_path)
            if vec_ds is None:
                raise QgsProcessingException(
                    "Cannot open boundary layer: {}".format(vec_path))
            vec_lyr = vec_ds.GetLayer()

            dem_srs = osr.SpatialReference()
            dem_srs.ImportFromWkt(prj)
            vec_srs = vec_lyr.GetSpatialRef()

            if vec_srs and not dem_srs.IsSame(vec_srs):
                feedback.pushInfo("   ⚠ CRS mismatch — reprojecting boundary...")
                mem_vec   = ogr.GetDriverByName("Memory").CreateDataSource("")
                out_lyr   = mem_vec.CreateLayer(
                    "reproj", dem_srs, ogr.wkbPolygon)
                transform = osr.CoordinateTransformation(vec_srs, dem_srs)
                for feat in vec_lyr:
                    geom = feat.GetGeometryRef().Clone()
                    geom.Transform(transform)
                    new_feat = ogr.Feature(out_lyr.GetLayerDefn())
                    new_feat.SetGeometry(geom)
                    out_lyr.CreateFeature(new_feat)
                vec_lyr = out_lyr
                feedback.pushInfo("   Reprojection done.")

            geom_type = vec_lyr.GetGeomType()
            is_line   = geom_type in [
                ogr.wkbLineString,    ogr.wkbMultiLineString,
                ogr.wkbLineString25D, ogr.wkbMultiLineString25D]
            if is_line:
                gdal.RasterizeLayer(
                    mask_ds, [1], vec_lyr, burn_values=[1],
                    options=["ALL_TOUCHED=TRUE"])
                feedback.pushInfo("   Boundary type : polyline")
            else:
                gdal.RasterizeLayer(mask_ds, [1], vec_lyr, burn_values=[1])
                feedback.pushInfo("   Boundary type : polygon")

            mask_ds.FlushCache()
            mask_arr = mask_ds.GetRasterBand(1).ReadAsArray()
            arr[mask_arr != 1] = np.nan
            dem_ds  = None
            mask_ds = None
            feedback.pushInfo("   ✅ Boundary mask applied")
        else:
            dem_ds = None
            feedback.pushInfo("   ✅ Full DEM extent (no boundary)")

        if not np.any(np.isfinite(arr)):
            raise QgsProcessingException(
                "No valid DEM cells left. The boundary probably does not "
                "overlap this DEM, or is in a CRS that could not be "
                "reprojected onto it.")

        elev_min = float(np.nanmin(arr))
        elev_max = float(np.nanmax(arr))
        feedback.pushInfo("   Elevation   : min={:.3f} m | max={:.3f} m".format(
            elev_min, elev_max))

        elevations = np.round(
            np.arange(elev_min, elev_max + elev_step, elev_step), 3)
        step_total = len(elevations)
        feedback.pushInfo("   Steps       : {} levels".format(step_total))

        highest = max(o["act_elev"] for o in outlets)
        if highest >= elev_max:
            feedback.pushWarning(
                "   ⚠ Every outlet activates at or above the DEM maximum "
                "({:.3f} m) — the curve will be all zeros. Check the "
                "activation elevations against the DEM's vertical datum."
                .format(elev_max))

        feedback.pushInfo("   Computing Q_out...")

        q_per_outlet = [[] for _ in outlets]
        q_totals     = []

        for j, elev in enumerate(elevations):
            feedback.setProgress(int(((idx + j / step_total) / total) * 100))
            if feedback.isCanceled():
                return False
            q_total = 0.0
            for k, o in enumerate(outlets):
                q = compute_q(o["type"], elev,
                              o["act_elev"], o["la"], o["c_cd"])
                q_per_outlet[k].append(round(q, 6))
                q_total += q
            q_totals.append(round(q_total, 6))

        # ── Table ─────────────────────────────
        dem_name = dem_layer.name()
        df = pd.DataFrame({
            "Elevation_m": [round(float(e), 3) for e in elevations]})
        for k, o in enumerate(outlets):
            df[q_column(o)] = q_per_outlet[k]
        df["Q_total_m3s"] = q_totals

        boundary_name = boundary_layer.name() if boundary_layer else None
        suffix        = "_{}".format(boundary_name) if boundary_name \
            else "_fullDEM"
        csv_path      = os.path.join(
            output_folder, "rating_{}{}.csv".format(dem_name, suffix))
        df.to_csv(csv_path, index=False)
        feedback.pushInfo("   ✅ CSV saved   : {}".format(csv_path))
        feedback.pushInfo("   Rows          : {}".format(len(df)))
        feedback.pushInfo("   Max Q_total   : {:.6f} m³/s".format(
            df["Q_total_m3s"].max()))

        self._plot_rating_curve(
            df=df, dem_name=dem_name, boundary_name=boundary_name,
            output_folder=output_folder, elev_step=elev_step,
            outlets=outlets, elev_min=elev_min, elev_max=elev_max,
            plot_opts=plot_opts, feedback=feedback)
        return csv_path

    # ══════════════════════════════════════════
    # ── Plot ──────────────────────────────────
    # ══════════════════════════════════════════
    def _plot_rating_curve(self, df, dem_name, boundary_name,
                           output_folder, elev_step, outlets,
                           elev_min, elev_max, plot_opts, feedback):

        title_suffix = "({})".format(boundary_name) if boundary_name \
            else "(Full DEM)"
        dpi = plot_opts["dpi"]

        elev_s = df["Elevation_m"]
        qtot_s = df["Q_total_m3s"]

        fig, ax = plt.subplots(figsize=(10, 8), dpi=dpi)
        fig.patch.set_facecolor("#FFFFFF")
        ax.set_facecolor("#FAFBFC")
        fig.suptitle("Reservoir Rating Curve — {} {}".format(
            dem_name, title_suffix),
            fontsize=14, fontweight="bold", fontfamily="Times New Roman",
            color="#1A1A2E", y=0.97)

        # ── Per outlet, stacked ───────────────
        q_stack = np.zeros(len(elev_s))
        for k, o in enumerate(outlets):
            q_vals = df[q_column(o)].values
            color  = OUTLET_COLORS.get(k % len(OUTLET_COLORS), "#555555")

            ax.fill_betweenx(elev_s, q_stack, q_stack + q_vals,
                             alpha=0.18, color=color, zorder=2)
            ax.plot(q_stack + q_vals, elev_s,
                    color=color, linewidth=1.8, linestyle="--", zorder=3,
                    label=describe_outlet(o))

            ax.axhline(y=o["act_elev"], color=color,
                       linewidth=0.9, linestyle=":", alpha=0.8, zorder=2)
            ax.text(0.01, o["act_elev"],
                    " ← Outlet {} ({}) activates at {:.2f} m".format(
                        o["id"], o["type"], o["act_elev"]),
                    transform=ax.get_yaxis_transform(),
                    fontsize=7, fontfamily="Times New Roman",
                    color=color, va="bottom")
            q_stack += q_vals

        # ── Q_total ───────────────────────────
        ax.plot(qtot_s, elev_s, color="#B71C1C", linewidth=2.8,
                zorder=5, label="Q_total (m³/s)")
        ax.fill_betweenx(elev_s, qtot_s, alpha=0.06,
                         color="#B71C1C", zorder=1)

        # ── Axis range and ticks ──────────────
        # Set before the draw below: the grid-line markers are read back off
        # the y ticks, so the locators have to be in place first.
        # Auto limits follow the table, not the DEM: the last row sits one
        # step above the highest cell, and an axis stopping at the DEM max
        # would clip the peak marker and its annotation out of the figure.
        y_lo = plot_opts["elev_min"] or float(elev_s.min())
        y_hi = plot_opts["elev_max"] or float(elev_s.max())
        ax.set_ylim(y_lo, y_hi)
        if plot_opts["q_max"]:
            ax.set_xlim(0, plot_opts["q_max"])
        else:
            ax.set_xlim(left=0)
        if plot_opts["y_tick"]:
            ax.yaxis.set_major_locator(
                ticker.MultipleLocator(plot_opts["y_tick"]))
        if plot_opts["x_tick"]:
            ax.xaxis.set_major_locator(
                ticker.MultipleLocator(plot_opts["x_tick"]))

        # ── Peak annotation ───────────────────
        max_idx = qtot_s.idxmax()
        if qtot_s[max_idx] > 0:
            ax.annotate(
                "  Peak Q_total\n  {:,.4f} m³/s\n  @ Elev {:.2f} m".format(
                    qtot_s[max_idx], elev_s[max_idx]),
                xy=(qtot_s[max_idx], elev_s[max_idx]),
                xytext=(qtot_s[max_idx] * 0.50,
                        elev_s[max_idx] - (elev_max - elev_min) * 0.10),
                fontsize=8.5, fontfamily="Times New Roman", color="#B71C1C",
                bbox=dict(boxstyle="round,pad=0.4", fc="white",
                          ec="#B71C1C", lw=1.0, alpha=0.92),
                arrowprops=dict(arrowstyle="->", color="#B71C1C",
                                lw=1.2, connectionstyle="arc3,rad=0.2"),
                zorder=6)

        # ── Read-off crosses on the grid lines ─
        fig.canvas.draw()
        # Only where the table actually has values: np.interp clamps outside
        # its range, which would plant a cross at a level never computed.
        data_lo, data_hi = float(elev_s.min()), float(elev_s.max())
        y_ticks = [t for t in ax.get_yticks()
                   if y_lo <= t <= y_hi and data_lo <= t <= data_hi]
        if y_ticks:
            q_at_yticks = np.interp(y_ticks, elev_s.values, qtot_s.values)
            ax.plot(q_at_yticks, y_ticks,
                    marker="+", markersize=11, markeredgewidth=1.8,
                    markeredgecolor="#B71C1C", linestyle="none",
                    zorder=7, label="Q_total at grid lines")

        # ── Style ─────────────────────────────
        ax.set_xlabel("Discharge  Q  (m³/s)", fontsize=12,
                      fontfamily="Times New Roman", labelpad=10)
        ax.set_ylabel("Elevation  (m)", fontsize=12,
                      fontfamily="Times New Roman", labelpad=10)
        ax.set_title("Elevation  vs  Q_out  —  Static Rating Curve",
                     fontsize=12, fontfamily="Times New Roman",
                     fontweight="bold", pad=12, color="#1A1A2E")
        ax.xaxis.set_major_formatter(
            ticker.FuncFormatter(lambda x, _: "{:,.4f}".format(x)))
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda x, _: "{:,.2f}".format(x)))
        ax.tick_params(labelsize=9, direction="in", length=5)
        ax.grid(True, linestyle="--", linewidth=0.6,
                alpha=0.5, color="#BBBBBB")
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_linewidth(0.8)
            spine.set_color("#AAAAAA")
        ax.legend(fontsize=7.5, loc="lower right",
                  prop={"family": "Times New Roman"},
                  framealpha=0.92, edgecolor="#CCCCCC")

        # ── Info bar ──────────────────────────
        outlet_lines = "  |  ".join(describe_outlet(o) for o in outlets)
        fig.text(
            0.5, 0.038,
            "DEM: {}  |  Boundary: {}  |  Elev: {:.2f}–{:.2f} m  |  "
            "Max Q_total: {:,.4f} m³/s  |  Step: {:g} m\n{}".format(
                dem_name, boundary_name if boundary_name else "Full DEM",
                elev_min, elev_max, qtot_s.max(), elev_step, outlet_lines),
            ha="center", fontsize=6.8,
            fontfamily="Times New Roman", color="#555555",
            bbox=dict(boxstyle="round,pad=0.45", fc="#F0F0F0",
                      ec="#CCCCCC", lw=0.8))
        fig.text(
            0.5, 0.005,
            "Generated by the KGA Reservoir Rating Curve tool  |  "
            "Weir: Q=C·L·H^1.5  |  Orifice/Pipe: Q=Cd·A·√(2gH)  |  "
            "H = water level − activation elevation",
            ha="center", fontsize=6.5, fontfamily="Times New Roman",
            color="#999999", style="italic")

        plt.subplots_adjust(bottom=0.20, top=0.91, left=0.10, right=0.97)

        suffix   = "_{}".format(boundary_name) if boundary_name \
            else "_fullDEM"
        png_path = os.path.join(
            output_folder, "rating_{}{}.png".format(dem_name, suffix))
        fig.savefig(png_path, dpi=dpi,
                    bbox_inches="tight", facecolor="#FFFFFF")
        plt.close(fig)
        feedback.pushInfo("   ✅ Graph saved : {}".format(png_path))

    # ══════════════════════════════════════════
    # ── Metadata ──────────────────────────────
    # ══════════════════════════════════════════
    def name(self):
        return "reservoir_rating_curve"

    def displayName(self):
        return "Reservoir Rating Curve"

    def group(self):
        return "KGA Irrigation Tools"

    def groupId(self):
        return "kgairrigationtools"

    def tags(self):
        return ["reservoir", "rating curve", "discharge", "outflow", "spillway",
                "weir", "orifice", "pipe", "outlet", "hydrology", "hydraulics",
                "dam", "irrigation", "stage discharge", "dem"]

    def shortDescription(self):
        return ("Stage–discharge curve for a reservoir: total outflow through "
                "weir, orifice and pipe outlets at every water level in the "
                "DEM's range.")

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/reservoir_rating_curve'

    # The Processing help panel wraps every line of this string in its own
    # <p> and does not escape it, so spaces used for alignment collapse.
    # Keep each line a complete, self-contained piece of HTML.
    def shortHelpString(self):
        return (
            "<p>Tabulates the total outflow from a reservoir at every water "
            "level between the lowest and highest DEM cell, and draws the "
            "stage&ndash;discharge curve. One CSV and one PNG per DEM.</p>"
            "<p>The DEM only sets the elevation range and the boundary clips "
            "it. Discharge itself comes entirely from the outlet definitions "
            "&mdash; no storage routing is involved, so the curve is static.</p>"
            "<h3>Defining the outlets</h3>"
            "<p><b>Fixed slots</b> &mdash; tick up to three outlets on the "
            "form. <b>CSV file</b> &mdash; supply a table instead, for any "
            "number of outlets.</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>outlet_id</th><th>type</th><th>act_elev</th><th>LA</th>"
            "<th>C_Cd</th></tr>"
            "<tr><td>1</td><td>Weir</td><td>40.0</td><td>5.00</td>"
            "<td>1.84</td></tr>"
            "<tr><td>2</td><td>Orifice</td><td>35.0</td><td>0.20</td>"
            "<td>0.61</td></tr>"
            "<tr><td>3</td><td>Pipe</td><td>33.5</td><td>0.07</td>"
            "<td>0.61</td></tr></table>"
            "<p><b>act_elev</b> is the crest or centreline level. <b>LA</b> is "
            "the crest width L in m for a weir, or the opening area A in "
            "m&sup2; for an orifice or pipe &mdash; areas are not derived for "
            "you, enter the computed value. <b>outlet_id</b> must be unique; "
            "it names the output column.</p>"
            "<h3>Formulas</h3>"
            "<p>Weir: <b>Q = C &times; L &times; H<sup>1.5</sup></b></p>"
            "<p>Orifice and pipe: <b>Q = C<sub>d</sub> &times; A &times; "
            "&radic;(2gH)</b>, with g = 9.81 m/s&sup2;</p>"
            "<p>H = water level &minus; activation elevation. An outlet "
            "contributes nothing while H &le; 0, which is what puts the "
            "kinks in the total curve.</p>"
            "<h3>Typical coefficients</h3>"
            "<p>Sharp-crested weir C = 1.84 &nbsp;&middot;&nbsp; broad-crested "
            "weir C = 1.70 &nbsp;&middot;&nbsp; ogee spillway C = 2.0&ndash;2.2"
            "</p>"
            "<p>Sharp-edged orifice or pipe C<sub>d</sub> = 0.61 "
            "&nbsp;&middot;&nbsp; rounded entry C<sub>d</sub> = 0.80&ndash;0.90"
            "</p>"
            "<h3>Outputs</h3>"
            "<p><b>rating_{DEM}_{boundary}.csv</b> &mdash; elevation, one "
            "column per outlet, and Q_total.</p>"
            "<p><b>rating_{DEM}_{boundary}.png</b> &mdash; the curve, with "
            "each outlet's contribution shaded, its activation level marked, "
            "and Q_total crossed at every grid line for reading off.</p>"
            "<p>The <i>Advanced</i> section pins the axis ranges and tick "
            "intervals, which is what you want when several reservoirs have "
            "to be compared on identical axes.</p>"
            "<h3>Watch out for</h3>"
            "<p>Activation elevations must be in the same vertical datum as "
            "the DEM. If they all sit above the DEM's highest cell the curve "
            "comes out flat at zero, and the log warns about it.</p>"
        )

    def createInstance(self):
        return ReservoirRatingCurve()

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

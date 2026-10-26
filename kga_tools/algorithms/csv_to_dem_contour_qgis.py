# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
)

from pathlib import Path

from ..branding import docs_url
from ..core import csv_dem
from ..core.compat import mark_advanced
from ..gui.csv_column_widget import column_wrapper_class

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: The window, kept alive between opens so it is not garbage collected.
DIALOG_INSTANCE = None


def _alive(dialog):
    try:
        return dialog is not None and not dialog._torn_down
    except RuntimeError:                    # the Qt object is already gone
        return False


# -------------------------------------------------- QGIS PROCESSING ALGORITHM

class CSVtoDEMContour(QgsProcessingAlgorithm):

    INPUT_CSV      = "INPUT_CSV"
    X_FIELD        = "X_FIELD"
    Y_FIELD        = "Y_FIELD"
    Z_FIELD        = "Z_FIELD"
    OUTPUT_FOLDER  = "OUTPUT_FOLDER"
    RESOLUTION     = "RESOLUTION"
    METHOD         = "METHOD"
    EPSG           = "EPSG"
    DO_CONTOUR     = "DO_CONTOUR"
    CONTOUR_INT    = "CONTOUR_INT"
    CONTOUR_SMOOTH = "CONTOUR_SMOOTH"
    CONTOUR_FMT    = "CONTOUR_FMT"
    LOAD_LAYERS    = "LOAD_LAYERS"
    POWER          = "POWER"
    NEIGHBOURS     = "NEIGHBOURS"
    RADIUS         = "RADIUS"
    FILL_EDGES     = "FILL_EDGES"
    CLAMP          = "CLAMP"
    RBF_KERNEL     = "RBF_KERNEL"
    RBF_NEIGHBOURS = "RBF_NEIGHBOURS"
    RBF_SMOOTHING  = "RBF_SMOOTHING"

    def name(self):        return "csvtodemcontour"
    def displayName(self): return "CSV to DEM and Contour"
    def group(self):       return "KGA Irrigation Tools"
    def groupId(self):     return "kgairrigationtools"

    def helpUrl(self):
        return docs_url('csvtodemcontour')
    def tr(self, s):       return QCoreApplication.translate("Processing", s)
    def createInstance(self): return CSVtoDEMContour()

    def shortHelpString(self):
        return (
            "<p>Turns a table of surveyed points (easting, northing, elevation) "
            "into a <b>DEM GeoTIFF</b> and, optionally, <b>contour lines</b>.</p>"

            "<h3>The input CSV</h3>"
            "<p>One row per surveyed point. Once a CSV is chosen, the X, Y "
            "and Z boxes list its column headers; pick from the list (the "
            "obvious ones are pre-selected) or type a name or column number "
            "(1 = first column). Clear all three and the tool finds them "
            "itself:</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>If the file has</th><th>The tool uses</th></tr>"
            "<tr><td>Headers like Easting / Northing / Elevation, or X / Y / Z "
            "(also East, North, Lat, Lon, Height, RL, Alt)</td>"
            "<td>Those columns, wherever they sit</td></tr>"
            "<tr><td>3 columns, no known headers</td><td>X, Y, Z</td></tr>"
            "<tr><td>4 columns</td><td>No, X, Y, Z</td></tr>"
            "<tr><td>5 to 9 columns</td><td>No, X, Y, Z, Code</td></tr>"
            "<tr><td>10 or 11 columns</td><td>Two No, X, Y, Z, Code tables "
            "side by side, read as one</td></tr>"
            "</table>"
            "<p>Comma, semicolon and tab separated files all work. Rows whose "
            "X, Y or Z is not a number are skipped; the log reports how many "
            "points were read.</p>"

            "<h3>Interpolation method</h3>"
            "<p>The survey points are scattered; a DEM is a regular grid. The "
            "method decides how the height of each grid cell is estimated "
            "from the points around it, so it changes the surface and "
            "therefore every contour drawn on it.</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>Method</th><th>How it works</th><th>Use it when</th></tr>"
            "<tr><td><b>IDW</b></td><td>Weighted average of the 16 nearest "
            "points; nearer points count more (weight = 1 / distance&sup2;). "
            "The surface passes through every point and never goes above the "
            "highest or below the lowest value nearby.</td>"
            "<td>General purpose and the safest default. Fills the whole "
            "bounding box. Can leave small flat 'bullseyes' around isolated "
            "points.</td></tr>"
            "<tr><td><b>Linear</b></td><td>Joins the points into triangles "
            "(Delaunay) and tilts a flat plane across each one.</td>"
            "<td>Dense, evenly spread points such as a grid survey. Honest "
            "to the data but the contours look angular.</td></tr>"
            "<tr><td><b>Cubic</b></td><td>Like Linear, but the triangles are "
            "blended into a smooth curved surface.</td>"
            "<td>Gently rolling ground where smooth contours are wanted. "
            "Can overshoot, giving heights above or below your highest or "
            "lowest point, where points are sparse or uneven.</td></tr>"
            "<tr><td><b>Nearest</b></td><td>Each cell copies the height of "
            "the closest point.</td>"
            "<td>Blocky surface; for a quick look only. "
            "Not suited to contours.</td></tr>"
            "<tr><td><b>RBF</b></td><td>Radial basis functions: fits a smooth "
            "surface through the nearest points around each cell.</td>"
            "<td>Smooth natural ground from scattered points. Slower than the "
            "others and can overshoot.</td></tr>"
            "</table>"
            "<p>Each method has its own settings, shown only when it is "
            "chosen: IDW &ndash; power, neighbours, search radius; Linear "
            "&ndash; edge fill; Cubic &ndash; edge fill, clamp to surveyed "
            "range; RBF &ndash; kernel, neighbours, smoothing. In the window "
            "they appear beside the method; in the Processing form they sit "
            "under Advanced Parameters.</p>"
            "<p>Linear and Cubic only know the ground inside the outline of "
            "the points. Cells outside that outline are filled with the "
            "nearest point's height, which gives flat edges.</p>"

            "<h3>Resolution</h3>"
            "<p>The size of one DEM cell, in metres. Smaller is more detailed "
            "but the grid grows with the square of the number: halving the "
            "cell size makes four times as many cells and a slower, larger "
            "run. A cell much smaller than the point spacing adds no real "
            "detail, it only repeats the interpolation. <b>0</b> picks a size "
            "from the point density (about 1/500 of the site width, never "
            "below 0.5 m); the log reports the value used.</p>"

            "<h3>Contours</h3>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>Setting</th><th>Meaning</th></tr>"
            "<tr><td>Interval</td><td>Vertical distance between lines, in "
            "metres. Lines fall on multiples of the interval (0.5, 1.0, 1.5 "
            "for 0.5 m). A small interval on a large, steep site makes a "
            "very large file.</td></tr>"
            "<tr><td>Smoothing (&sigma;)</td><td>Blurs the grid, in cells, "
            "before the lines are traced, which removes the stair-steps and "
            "sharp kinks. 0 = off, 1 = light, 2 to 3 = strong. Higher values "
            "round off real features such as ditches and bunds and shift "
            "lines slightly. The DEM itself is not smoothed.</td></tr>"
            "<tr><td>Format</td><td>Shapefile or GeoPackage. Each line "
            "carries <i>elevation</i>, <i>elev_m</i> (rounded to whole "
            "metres) and <i>interval</i>.</td></tr>"
            "</table>"

            "<h3>Coordinate system</h3>"
            "<p>The EPSG code of the CSV's coordinates (default 32648, "
            "WGS 84 / UTM zone 48N). The tool does <b>not</b> reproject: it "
            "stamps this CRS on the outputs, so a wrong code puts the DEM in "
            "the wrong place. Use a projected CRS in metres, as the "
            "resolution and contour interval are in metres.</p>"

            "<h3>Outputs</h3>"
            "<p>Written to the output folder, named after the CSV: "
            "<i>name_DEM.tif</i> (Float32, NoData = -9999) and "
            "<i>name_Contour_&lt;interval&gt;m.shp / .gpkg</i>. Existing "
            "files with the same name are overwritten.</p>"

            "<h3>Watch out for</h3>"
            "<ul>"
            "<li>Heights in the wrong column: check the Elevation range in "
            "the log against what you expect.</li>"
            "<li>Duplicate points at the same X, Y with different heights "
            "make the surface ambiguous there; clean the data first.</li>"
            "<li>The DEM is only as good as the survey: it cannot show "
            "features between points.</li>"
            "<li>Very large point counts with IDW and a fine resolution are "
            "slow.</li>"
            "</ul>"
        )

    def _add(self, param, help_text, advanced=False):
        """Add a parameter, with its help shown as the widget tooltip."""
        param.setHelp(help_text)
        if advanced:
            mark_advanced(param)
        self.addParameter(param)

    def checkParameterValues(self, parameters, context):
        ok, msg = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, msg
        fields = [(self.parameterAsString(parameters, k, context) or "").strip()
                  for k in (self.X_FIELD, self.Y_FIELD, self.Z_FIELD)]
        if any(fields) and not all(fields):
            return False, ("Set all three of the X, Y and Z fields, or leave "
                           "all three blank to auto-detect.")
        if all(fields) and len({f.lower() for f in fields}) < 3:
            return False, "The X, Y and Z fields must be three different columns."
        epsg = self.parameterAsInt(parameters, self.EPSG, context)
        if not QgsCoordinateReferenceSystem.fromEpsgId(epsg).isValid():
            return False, f"EPSG:{epsg} is not a known coordinate system."
        return True, ""

    def initAlgorithm(self, config=None):

        self._add(QgsProcessingParameterFile(
            self.INPUT_CSV, self.tr("Input · Point CSV"),
            behavior=QgsProcessingParameterFile.Behavior.File,
            fileFilter="CSV Files (*.csv);;All Files (*.*)",
        ), "Surveyed points, one per row, with easting, northing and "
           "elevation columns.")

        for key, axis, label, hint in (
            (self.X_FIELD, "X", "Input · X (Easting) field",
             "Pick the easting column from the CSV's header (or type a name or "
             "column number)."),
            (self.Y_FIELD, "Y", "Input · Y (Northing) field",
             "Pick the northing column from the CSV's header."),
            (self.Z_FIELD, "Z", "Input · Z (Elevation) field",
             "Pick the elevation column from the CSV's header."),
        ):
            param = QgsProcessingParameterString(
                key, self.tr(label), optional=True)
            param.setMetadata({"widget_wrapper": {
                "class": column_wrapper_class()},
                "csv_param": self.INPUT_CSV, "axis": axis})
            self._add(param, hint + " Blank = auto-detect; fill all three or none.")

        self._add(QgsProcessingParameterEnum(
            self.METHOD, self.tr("DEM · Interpolation method"),
            options=["IDW – Inverse Distance Weighting",
                     "Linear – Delaunay triangulation",
                     "Cubic – smooth surface",
                     "Nearest Neighbour",
                     "RBF – Radial basis functions (smooth)"],
            defaultValue=0,
        ), "How cell heights are estimated from the points. IDW is the safe "
           "default; Linear for dense even points; Cubic for smooth ground "
           "(may overshoot); Nearest is blocky. See the help panel.")

        self._add(QgsProcessingParameterNumber(
            self.RESOLUTION, self.tr("DEM · Cell size (m)  [0 = auto]"),
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=0.0, minValue=0.0, optional=True,
        ), "Size of one DEM cell in metres. Halving it quadruples the cell "
           "count. 0 = chosen from the point density (min 0.5 m).")

        self._add(QgsProcessingParameterNumber(
            self.EPSG, self.tr("DEM · CRS (EPSG code)"),
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=32648,
        ), "EPSG code of the CSV coordinates, e.g. 32648 = WGS 84 / UTM 48N. "
           "Only labels the outputs; no reprojection is done.")

        d = csv_dem.DEFAULT_OPTIONS
        for key, label, hint, default, lo, kind in (
            (self.POWER, "IDW · Power",
             "How fast influence falls with distance. 2 is standard; higher "
             "makes the surface follow the nearest points more tightly.",
             d["power"], 0.1, "double"),
            (self.NEIGHBOURS, "IDW · Neighbours",
             "Nearest survey points averaged per cell.",
             d["neighbours"], 1, "int"),
            (self.RADIUS, "IDW · Search radius (m)  [0 = no limit]",
             "Ignore points further away than this. A cell with none in range "
             "takes the nearest point's height.",
             d["radius"], 0.0, "double"),
            (self.RBF_NEIGHBOURS, "RBF · Neighbours",
             "Nearest points used for each local fit. More is smoother and "
             "slower.", d["rbf_neighbours"], 5, "int"),
            (self.RBF_SMOOTHING, "RBF · Smoothing",
             "0 passes through every point. Raise it to relax the surface "
             "when points disagree (noisy survey, duplicates).",
             d["rbf_smoothing"], 0.0, "double"),
        ):
            kind_t = (QgsProcessingParameterNumber.Type.Integer if kind == "int"
                      else QgsProcessingParameterNumber.Type.Double)
            self._add(QgsProcessingParameterNumber(
                key, self.tr(label), type=kind_t,
                defaultValue=default, minValue=lo), hint, advanced=True)

        self._add(QgsProcessingParameterEnum(
            self.RBF_KERNEL, self.tr("RBF · Kernel"),
            options=list(csv_dem.RBF_KERNELS),
            defaultValue=csv_dem.RBF_KERNELS.index(d["rbf_kernel"]),
        ), "Shape of the fitted surface. thin_plate_spline is the usual "
           "choice for terrain.", advanced=True)

        for key, label, hint, default in (
            (self.FILL_EDGES, "Linear/Cubic · Fill outside the point outline",
             "Linear and Cubic only know the ground inside the outline of the "
             "points. On: the rest takes the nearest point's height. Off: "
             "left as NoData.", d["fill_edges"]),
            (self.CLAMP, "Cubic · Clamp to the surveyed height range",
             "Stops Cubic overshooting above the highest or below the lowest "
             "surveyed point.", d["clamp"]),
        ):
            self._add(QgsProcessingParameterBoolean(
                key, self.tr(label), defaultValue=default), hint, advanced=True)

        self._add(QgsProcessingParameterBoolean(
            self.DO_CONTOUR, self.tr("Contours · Generate contour lines"),
            defaultValue=True,
        ), "Untick to produce only the DEM.")

        self._add(QgsProcessingParameterNumber(
            self.CONTOUR_INT, self.tr("Contours · Interval (m)"),
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=1.0, minValue=0.01,
        ), "Vertical distance between contour lines, in metres.")

        self._add(QgsProcessingParameterNumber(
            self.CONTOUR_SMOOTH,
            self.tr("Contours · Smoothing, Gaussian σ (0 = off)"),
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=1.0, minValue=0.0,
        ), "Blur in cells applied before tracing lines. 0 = none, 1 = light, "
           "2-3 = strong. The DEM itself is unchanged.", advanced=True)

        self._add(QgsProcessingParameterEnum(
            self.CONTOUR_FMT, self.tr("Contours · Output format"),
            options=["Shapefile (.shp)", "GeoPackage (.gpkg)"],
            defaultValue=0,
        ), "File format of the contour layer.", advanced=True)

        self._add(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, self.tr("Output · Folder"),
        ), "DEM and contours are written here, named after the CSV.")

        self._add(QgsProcessingParameterBoolean(
            self.LOAD_LAYERS,
            self.tr("Output · Add results to the map"),
            defaultValue=True,
        ), "Load the DEM and contours into the current project.", advanced=True)

    # -------------------------------------------------------------------- run

    def createCustomParametersWidget(self, parent=None):
        """Open the CSV to DEM and Contour window instead of the plain form.

        The parameters above stay, so scripts (`processing.run`), the modeller
        and batch still work; only the Toolbox dialog is replaced.
        """
        global DIALOG_INSTANCE
        if iface is None:
            return None
        from ..gui.csv_dem_dialog import CsvDemDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = CsvDemDialog(iface.mainWindow())
        return DIALOG_INSTANCE

    def options(self, parameters, context):
        """The method-specific settings, keyed as in `csv_dem.DEFAULT_OPTIONS`."""
        return {
            "power": self.parameterAsDouble(parameters, self.POWER, context),
            "neighbours": self.parameterAsInt(parameters, self.NEIGHBOURS, context),
            "radius": self.parameterAsDouble(parameters, self.RADIUS, context),
            "fill_edges": self.parameterAsBoolean(parameters, self.FILL_EDGES, context),
            "clamp": self.parameterAsBoolean(parameters, self.CLAMP, context),
            "rbf_kernel": csv_dem.RBF_KERNELS[
                self.parameterAsEnum(parameters, self.RBF_KERNEL, context)],
            "rbf_neighbours": self.parameterAsInt(parameters, self.RBF_NEIGHBOURS, context),
            "rbf_smoothing": self.parameterAsDouble(parameters, self.RBF_SMOOTHING, context),
        }

    def processAlgorithm(self, parameters, context, feedback):
        def as_str(key):
            return self.parameterAsString(parameters, key, context) or ""

        cnt_int = self.parameterAsDouble(parameters, self.CONTOUR_INT, context)
        fmt = ["shp", "gpkg"][self.parameterAsEnum(parameters, self.CONTOUR_FMT, context)]
        try:
            result = csv_dem.run(
                self.parameterAsFile(parameters, self.INPUT_CSV, context),
                self.parameterAsString(parameters, self.OUTPUT_FOLDER, context),
                feedback,
                x_field=as_str(self.X_FIELD), y_field=as_str(self.Y_FIELD),
                z_field=as_str(self.Z_FIELD),
                resolution=self.parameterAsDouble(parameters, self.RESOLUTION, context),
                method=csv_dem.METHODS[self.parameterAsEnum(parameters, self.METHOD, context)],
                options=self.options(parameters, context),
                epsg=self.parameterAsInt(parameters, self.EPSG, context),
                contour=self.parameterAsBoolean(parameters, self.DO_CONTOUR, context),
                interval=cnt_int,
                smoothing=self.parameterAsDouble(parameters, self.CONTOUR_SMOOTH, context),
                fmt=fmt)
        except csv_dem.Canceled:
            return {}
        except ValueError as exc:
            raise QgsProcessingException(str(exc))

        dem_path, cnt_path, base = result["dem"], result["contour"], result["base"]
        if self.parameterAsBoolean(parameters, self.LOAD_LAYERS, context):
            rl = QgsRasterLayer(dem_path, f"{base} DEM")
            if rl.isValid():
                QgsProject.instance().addMapLayer(rl)
            else:
                feedback.pushWarning(f"Could not load DEM: {dem_path}")
            if cnt_path:
                vl = QgsVectorLayer(cnt_path, f"{base} Contour {cnt_int}m", "ogr")
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
                else:
                    feedback.pushWarning(f"Could not load contour: {cnt_path}")

        feedback.pushInfo(f"\nDone. DEM: {Path(dem_path).name}"
                          + (f"  Contour: {Path(cnt_path).name}" if cnt_path else ""))
        out = {"DEM": dem_path}
        if cnt_path:
            out["Contour"] = cnt_path
        return out


def close_csv_dem():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

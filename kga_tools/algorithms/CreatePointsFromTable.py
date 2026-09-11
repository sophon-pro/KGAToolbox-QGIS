# ============================================================
#  Create Points From Table - KGA Toolbox  (v2)
#
#  Processing Toolbox -> KGA Toolbox -> KGA Data Management
#  -> Create Points From Table
#
#  v2 CHANGES
#    - Every parameter carries a setHelp() tooltip, and the
#      file-format knobs moved behind Advanced.
#    - "Label Field" used to be read, matched, warned about and
#      then ignored - the tool never labelled anything. It now
#      styles the loaded layer through a post-processor.
#    - Numeric columns come out as numbers instead of text, so
#      graduated symbology and field calculations work on the
#      result. Turn it off with "Detect numeric columns".
#    - Optional Z column produces a PointZ layer.
#    - New: worksheet picker, header row, explicit delimiter and
#      encoding, all of which used to be guessed or hardcoded.
#    - Failures raise instead of returning an empty result, so a
#      broken run no longer reports success with no output.
#    - Warns when the coordinates look like degrees but the CRS
#      is projected, or the other way round.
#    - Skipped-row messages are capped, so a bad file no longer
#      floods the log with one line per row.
# ============================================================

import csv
import io
import os

from qgis.core import (
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsFeature,
    QgsFields,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsPoint,
    QgsPointXY,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QColor

from ..core.compat import (
    make_field,
    mark_advanced,
    source_type,
    wkb_type,
    T_DOUBLE,
    T_LONGLONG,
    T_STRING,
)


# ══════════════════════════════════════════════════════════
# ── Constants ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════

#: Column headers recognised when the named one is not found. Single
#: letters other than x/y/z are deliberately absent - an "N" column is
#: far more often a count than a northing.
X_ALIASES = ("x", "easting", "east", "lon", "long", "longitude",
             "x_easting", "x (easting)", "xcoord", "x_coord", "coord_x",
             "point_x", "utm_e", "utm_x", "e_utm")
Y_ALIASES = ("y", "northing", "north", "lat", "latitude",
             "y_northing", "y (northing)", "ycoord", "y_coord", "coord_y",
             "point_y", "utm_n", "utm_y", "n_utm")
Z_ALIASES = ("z", "elev", "elevation", "height", "alt", "altitude",
             "level", "rl", "z_elev", "point_z", "zcoord", "z_coord")

NULL_TOKENS = frozenset(
    ("", "null", "none", "nan", "n/a", "#n/a", "na", "-", "#null!"))

TEXT_EXT = (".csv", ".txt", ".tsv")
EXCEL_EXT = (".xlsx", ".xlsm", ".xls")
ODS_EXT = (".ods",)
SUPPORTED_EXT = TEXT_EXT + EXCEL_EXT + ODS_EXT

DELIMITERS = ["Auto-detect", "Comma  ,", "Semicolon  ;", "Tab",
              "Pipe  |", "Space"]
DELIMITER_CHARS = [None, ",", ";", "\t", "|", " "]

ENCODINGS = ["Auto-detect", "UTF-8", "Windows-1252 (Western)",
             "Windows-874 (Thai)", "UTF-16"]
ENCODING_CODECS = [None, "utf-8-sig", "cp1252", "cp874", "utf-16"]

#: Encodings tried in order when Auto-detect is chosen. latin-1 decodes
#: any byte at all, so it is the backstop that guarantees a result.
AUTO_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

#: Beyond this many skipped rows the log stops listing them one by one.
MAX_SKIP_MESSAGES = 25

try:
    LABEL_PLACEMENT = Qgis.LabelPlacement.AroundPoint
except AttributeError:                              # QGIS < 3.36
    LABEL_PLACEMENT = QgsPalLayerSettings.AroundPoint

#: setPostProcessor() transfers ownership to C++, but Python still has to
#: hold a reference or the object is collected before the layer loads.
_LIVE_POST_PROCESSORS = []


# ══════════════════════════════════════════════════════════
# ── Number parsing ────────────────────────────────────────
# ══════════════════════════════════════════════════════════

def _plain_text(value):
    """`value` as trimmed text, or None if it holds nothing usable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value).strip().replace("\u00a0", " ")
    if s.lower() in NULL_TOKENS:
        return None
    return s


def _group_after(text, separator):
    """Digits following the last `separator`, or None if that is not what
    follows it. "1,407,186.00" gives 2 for "." and None for ","."""
    at = text.rfind(separator)
    if at < 0:
        return None
    tail = text[at + 1:]
    return len(tail) if tail.isdigit() else None


def _decimal_separator(values, delimiter=None):
    """Which character is the decimal point in this column: "." or ",".

    A lone comma before exactly three digits is genuinely ambiguous:
    "1407186,000" is 1407186.0 to half the world and 1407186000 to the
    other half, and no amount of staring at that one value settles it.
    Looking at the whole column does - a single value that can only be
    read one way decides for all of them. That matters because survey
    coordinates in metres carry exactly three decimals, which is the one
    case a per-value guess always gets wrong.
    """
    ambiguous_comma = False

    for raw in values:
        text = _plain_text(raw)
        if text is None:
            continue

        # Both characters present: the last one is the decimal point, and
        # that settles the column - no later value can be more decisive.
        if "," in text and "." in text:
            return "," if text.rfind(",") > text.rfind(".") else "."

        after_comma = _group_after(text, ",")
        after_dot = _group_after(text, ".")
        # Repeated separators only ever group thousands.
        if text.count(",") >= 2:
            return "."
        if text.count(".") >= 2:
            return ","
        # A group that is not three digits cannot be a thousands group.
        if after_comma is not None and after_comma != 3:
            return ","
        if after_dot is not None and after_dot != 3:
            return "."
        if after_comma is not None:
            ambiguous_comma = True

    # Nothing decisive. A comma that survived in a file whose columns are
    # separated by something else is almost always a decimal comma - that
    # is exactly why Excel switches to semicolons in those locales.
    if ambiguous_comma and delimiter != ",":
        return ","
    return "."


def _to_float(value, decimal=None):
    """`value` as a float, or None if it is not a usable number.

    `decimal` is the separator the surrounding column uses; None works it
    out from this value alone.
    """
    text = _plain_text(value)
    if text is None:
        return None
    if decimal is None:
        decimal = _decimal_separator((text,))

    thousands = "," if decimal == "." else "."
    text = text.replace(thousands, "").replace(" ", "")
    if decimal != ".":
        text = text.replace(decimal, ".")

    try:
        return float(text)
    except ValueError:
        return None


def _looks_like_identifier(value):
    """True for "007" and "0812345678" - digits that are really a code.

    Typed as a number these lose their leading zero, which for a village
    code or a phone number is data loss, so the column stays text.
    """
    text = _plain_text(value)
    if text is None:
        return False
    text = text.lstrip("+-")
    return len(text) > 1 and text[0] == "0" and text[1] not in ".,"


def _column_type(values, decimal="."):
    """Narrowest field type that holds every non-empty value in a column."""
    saw_value = False
    integral = True

    for raw in values:
        text = _plain_text(raw)
        if text is None:
            continue
        saw_value = True

        if _looks_like_identifier(raw):
            return T_STRING
        number = _to_float(text, decimal)
        if number is None:
            return T_STRING

        if decimal in text or "e" in text.lower():
            integral = False
        elif not float(number).is_integer():
            integral = False
        elif abs(number) > 9.0e18:           # past what Integer64 holds
            integral = False

    if not saw_value:
        return T_STRING
    return T_LONGLONG if integral else T_DOUBLE


# ══════════════════════════════════════════════════════════
# ── Header handling ───────────────────────────────────────
# ══════════════════════════════════════════════════════════

def _unique_headers(raw_headers):
    """Usable, distinct field names.

    Blank headers become column_N, and duplicates get a numeric suffix -
    QgsFields.append() silently drops a repeated name, which would leave
    the attribute list shorter than the row and shift every value along.
    """
    out, used = [], set()
    for i, header in enumerate(raw_headers):
        name = "" if header is None else str(header).strip()
        if not name or name.lower() in NULL_TOKENS:
            name = "column_{}".format(i + 1)
        candidate, n = name, 1
        while candidate.lower() in used:
            n += 1
            candidate = "{}_{}".format(name, n)
        used.add(candidate.lower())
        out.append(candidate)
    return out


def _match_header(wanted, headers, aliases):
    """Header matching `wanted`, else the first alias present, else None."""
    lookup = {h.strip().lower(): h for h in headers}
    if wanted:
        hit = lookup.get(wanted.strip().lower())
        if hit is not None:
            return hit
    for alias in aliases:
        hit = lookup.get(alias)
        if hit is not None:
            return hit
    return None


# ══════════════════════════════════════════════════════════
# ── Labelling the loaded layer ────────────────────────────
# ══════════════════════════════════════════════════════════

class _PointLabelPostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Switches labelling on once QGIS has loaded the output layer.

    An algorithm cannot style its own sink - the sink may be a file that
    never enters the project, and styling has to happen on the GUI
    thread. This runs there, after the layer is loaded, which is the only
    point where labelling can actually be set.
    """

    def __init__(self, field_name):
        super().__init__()
        self._field = field_name

    def postProcessLayer(self, layer, context, feedback):
        try:
            fmt = QgsTextFormat()
            fmt.setSize(9)
            fmt.setColor(QColor(0, 0, 0))

            buffer_settings = QgsTextBufferSettings()
            buffer_settings.setEnabled(True)
            buffer_settings.setSize(0.8)
            buffer_settings.setColor(QColor(255, 255, 255))
            fmt.setBuffer(buffer_settings)

            settings = QgsPalLayerSettings()
            settings.fieldName = self._field
            settings.placement = LABEL_PLACEMENT
            settings.setFormat(fmt)
            try:
                settings.enabled = True
            except AttributeError:                  # read-only on some builds
                pass

            layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            layer.setLabelsEnabled(True)
            layer.triggerRepaint()
        except Exception as exc:                    # never break the run
            if feedback is not None:
                feedback.pushWarning(
                    "Could not switch labelling on: {}".format(exc))


# ══════════════════════════════════════════════════════════
# ── Algorithm ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════

class CreatePointsFromTable(QgsProcessingAlgorithm):

    INPUT_FILE    = "INPUT_FILE"
    X_FIELD       = "X_FIELD"
    Y_FIELD       = "Y_FIELD"
    Z_FIELD       = "Z_FIELD"
    CRS           = "CRS"
    LABEL_FIELD   = "LABEL_FIELD"
    DETECT_TYPES  = "DETECT_TYPES"
    SKIP_ERRORS   = "SKIP_ERRORS"
    SHEET         = "SHEET"
    HEADER_ROW    = "HEADER_ROW"
    DELIMITER     = "DELIMITER"
    ENCODING      = "ENCODING"
    OUTPUT        = "OUTPUT"

    # ══════════════════════════════════════════
    # ── Inputs ────────────────────────────────
    # ══════════════════════════════════════════
    def _add(self, param, help_text, advanced=False):
        """Add a parameter, with its help shown as the widget tooltip."""
        param.setHelp(help_text)
        if advanced:
            mark_advanced(param)
        self.addParameter(param)

    def initAlgorithm(self, config=None):

        # ── The table ─────────────────────────
        self._add(QgsProcessingParameterFile(
            self.INPUT_FILE, "Input Table",
            behavior=QgsProcessingParameterFile.File,
            fileFilter=(
                "All supported (*.csv *.txt *.tsv *.xlsx *.xlsm *.xls *.ods);;"
                "Text and CSV (*.csv *.txt *.tsv);;"
                "Excel (*.xlsx *.xlsm *.xls);;"
                "OpenDocument (*.ods);;"
                "All files (*.*)")),
            "The table to read: .csv, .txt, .tsv, .xlsx, .xlsm, .xls or "
            ".ods. The file is read straight from disk, so it does not have "
            "to be loaded in QGIS first. Multi-sheet workbooks use the first "
            "sheet unless you name another one under Advanced.")

        # ── Coordinates ───────────────────────
        self._add(QgsProcessingParameterString(
            self.X_FIELD, "X Column  (Easting or Longitude)",
            defaultValue="X", optional=True),
            "Header of the column holding the X coordinate. Matching "
            "ignores case. If the name is not found - or you leave this "
            "blank - the tool looks for a column called x, easting, east, "
            "lon, longitude, xcoord, coord_x, point_x or utm_e, and says in "
            "the log which one it used.")

        self._add(QgsProcessingParameterString(
            self.Y_FIELD, "Y Column  (Northing or Latitude)",
            defaultValue="Y", optional=True),
            "Header of the column holding the Y coordinate, matched the "
            "same way as X. The fallback names are y, northing, north, lat, "
            "latitude, ycoord, coord_y, point_y and utm_n.")

        self._add(QgsProcessingParameterString(
            self.Z_FIELD, "Z Column  (Elevation, optional)",
            defaultValue="", optional=True),
            "Fill this in to build a 3D point layer instead of a flat one. "
            "Blank leaves the output 2D. Fallback names are z, elev, "
            "elevation, height, altitude, level and rl. Rows whose Z will "
            "not parse get a Z of 0 rather than being thrown away - the log "
            "counts them.")

        self._add(QgsProcessingParameterCrs(
            self.CRS, "Coordinate Reference System",
            defaultValue="EPSG:32648"),
            "The CRS the numbers in the table are already in - not the one "
            "you want to end up in. Getting this wrong is what puts points "
            "in the Gulf of Guinea. EPSG:32648 is WGS 84 / UTM zone 48N, "
            "which covers most of Cambodia; use EPSG:4326 for degrees of "
            "longitude and latitude. The tool warns if the values look like "
            "the wrong kind for the CRS you picked.")

        # ── Output content ────────────────────
        self._add(QgsProcessingParameterString(
            self.LABEL_FIELD, "Label Points With  (optional)",
            defaultValue="", optional=True),
            "Column to label the points with. The output layer is loaded "
            "with labelling already switched on, in 9 pt black with a white "
            "buffer, placed around the point - restyle it afterwards like "
            "any other layer. Only applies when the result is added to the "
            "project; writing straight to a file carries no style.")

        self._add(QgsProcessingParameterBoolean(
            self.DETECT_TYPES, "Detect Numeric Columns",
            defaultValue=True),
            "Reads each column and types it as Integer, Real or Text "
            "instead of making everything Text, so the result can be used "
            "for graduated symbology, statistics and the field calculator "
            "without converting anything first. Columns that hold "
            "leading-zero codes such as 007 stay Text on purpose. Untick to "
            "keep every column as Text.")

        self._add(QgsProcessingParameterBoolean(
            self.SKIP_ERRORS, "Skip Rows With Unusable Coordinates",
            defaultValue=True),
            "Ticked, rows whose X or Y will not parse are counted and left "
            "out, and the run finishes. Unticked, the first such row stops "
            "the run and names the offending value - which is what you want "
            "when the table is meant to be complete.")

        # ── Advanced: how the file is read ────
        self._add(QgsProcessingParameterString(
            self.SHEET, "Workbook · Worksheet",
            defaultValue="", optional=True),
            "Which sheet to read from an Excel or ODS workbook: either its "
            "name, or its position as a number starting at 1. Blank means "
            "the first sheet. Ignored for text files.", True)

        self._add(QgsProcessingParameterNumber(
            self.HEADER_ROW, "Workbook · Header Row",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=1, minValue=1, maxValue=1000),
            "Row number the column headers are on, counting from 1. Raise "
            "it for tables that carry a title block, a project name or "
            "units above the real header. Everything above that row is "
            "discarded.", True)

        self._add(QgsProcessingParameterEnum(
            self.DELIMITER, "Text Files · Column Separator",
            options=DELIMITERS, defaultValue=0),
            "Auto-detect samples the first few kilobytes and picks between "
            "comma, semicolon, tab and pipe. Set it explicitly when that "
            "guess goes wrong - a one-column file or quoted text full of "
            "commas can fool it. Ignored for Excel and ODS.", True)

        self._add(QgsProcessingParameterEnum(
            self.ENCODING, "Text Files · Character Encoding",
            options=ENCODINGS, defaultValue=0),
            "Auto-detect tries UTF-8, then Windows-1252, then Latin-1, and "
            "logs which one worked. Set it explicitly if Khmer or accented "
            "text comes out as mojibake - CSVs saved from Excel on Windows "
            "are usually Windows-1252, not UTF-8. Ignored for Excel and "
            "ODS.", True)

        # ── Output ────────────────────────────
        self._add(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Output Point Layer",
            type=source_type('VectorPoint')),
            "The point layer to create. Leave it as a temporary layer to "
            "check the result first, or write straight to GeoPackage or "
            "Shapefile. Shapefile truncates field names to 10 characters "
            "and cannot hold more than 255 columns.")

    def checkParameterValues(self, parameters, context):
        ok, msg = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, msg

        path = self.parameterAsFile(parameters, self.INPUT_FILE, context)
        if path:
            ext = os.path.splitext(path)[1].lower()
            if ext not in SUPPORTED_EXT:
                return False, self.tr(
                    "'{}' is not a table format this tool reads. Supported: "
                    "{}.".format(ext or "(no extension)",
                                 " ".join(SUPPORTED_EXT)))

        x_field = (self.parameterAsString(
            parameters, self.X_FIELD, context) or "").strip()
        y_field = (self.parameterAsString(
            parameters, self.Y_FIELD, context) or "").strip()
        if x_field and y_field and x_field.lower() == y_field.lower():
            return False, self.tr(
                "The X and Y columns are both set to '{}'. They have to be "
                "different columns.".format(x_field))

        return True, ""

    # ══════════════════════════════════════════
    # ── Reading the file ──────────────────────
    # ══════════════════════════════════════════
    def _read_text(self, path, codec, feedback):
        """File contents as text, plus the encoding that decoded it."""
        candidates = (codec,) if codec else AUTO_ENCODINGS
        last_error = None
        for enc in candidates:
            try:
                with open(path, "r", encoding=enc, newline="") as handle:
                    return handle.read(), enc
            except UnicodeDecodeError as exc:
                last_error = exc
                continue
        raise QgsProcessingException(
            "Could not decode the file as {}. Pick the right encoding under "
            "Advanced.\n{}".format(
                codec or " / ".join(AUTO_ENCODINGS), last_error))

    def _sniff_delimiter(self, sample, feedback):
        try:
            return csv.Sniffer().sniff(
                sample, delimiters=",;\t|").delimiter
        except Exception:
            feedback.pushInfo(
                "   Note      : the separator could not be detected from "
                "the file, assuming comma")
            return ","

    def _read_grid(self, path, opts, feedback):
        """(rows of raw cell values, the column separator or None)."""
        ext = os.path.splitext(path)[1].lower()
        feedback.pushInfo("   Format    : {}".format(ext))

        if ext in TEXT_EXT:
            text, used_encoding = self._read_text(
                path, opts["encoding"], feedback)
            feedback.pushInfo("   Encoding  : {}".format(used_encoding))

            delimiter = opts["delimiter"]
            if delimiter is None:
                # Sniff from the header row down. A title block above it
                # carries no separators and talks the sniffer out of the
                # right answer.
                body = "\n".join(
                    text.splitlines()[opts["header_row"] - 1:][:200])
                delimiter = self._sniff_delimiter(body, feedback)
            feedback.pushInfo("   Separator : {}".format(
                "tab" if delimiter == "\t" else
                "space" if delimiter == " " else "'{}'".format(delimiter)))

            reader = csv.reader(io.StringIO(text), delimiter=delimiter,
                                skipinitialspace=True)
            return [list(row) for row in reader], delimiter

        if ext in EXCEL_EXT:
            return self._read_workbook(path, opts, feedback), None

        if ext in ODS_EXT:
            return self._read_with_pandas(path, opts, feedback, "odf"), None

        raise QgsProcessingException(
            "Unsupported file format '{}'. Supported: {}.".format(
                ext, " ".join(SUPPORTED_EXT)))

    def _read_workbook(self, path, opts, feedback):
        try:
            import openpyxl
        except ImportError:
            feedback.pushInfo("   Reader    : openpyxl missing, trying pandas")
            return self._read_with_pandas(path, opts, feedback, None)

        if path.lower().endswith(".xls"):
            # openpyxl handles the modern zip format only.
            feedback.pushInfo("   Reader    : legacy .xls, trying pandas")
            return self._read_with_pandas(path, opts, feedback, None)

        book = openpyxl.load_workbook(path, data_only=True, read_only=True)
        sheet = self._pick_sheet_name(book.sheetnames, opts["sheet"])
        feedback.pushInfo("   Worksheet : {}  (of {})".format(
            sheet, ", ".join(book.sheetnames)))
        return [list(row)
                for row in book[sheet].iter_rows(values_only=True)]

    def _read_with_pandas(self, path, opts, feedback, engine):
        try:
            import pandas as pd
        except ImportError:
            raise QgsProcessingException(
                "Reading {} needs pandas, which is not installed in this "
                "QGIS. Save the table as .csv and use that instead.".format(
                    os.path.splitext(path)[1]))

        sheet = opts["sheet"].strip()
        if sheet.isdigit():
            selector = int(sheet) - 1
        elif sheet:
            selector = sheet
        else:
            selector = 0

        try:
            frame = pd.read_excel(path, sheet_name=selector, header=None,
                                  dtype=object, engine=engine)
        except Exception as exc:
            raise QgsProcessingException(
                "Could not read {}: {}".format(os.path.basename(path), exc))

        feedback.pushInfo("   Reader    : pandas{}".format(
            " + " + engine if engine else ""))
        return [list(row) for row in frame.values.tolist()]

    @staticmethod
    def _pick_sheet_name(names, wanted):
        """Sheet name from a name or a 1-based position, else the first."""
        wanted = (wanted or "").strip()
        if not wanted:
            return names[0]
        for name in names:
            if name.strip().lower() == wanted.lower():
                return name
        if wanted.isdigit():
            index = int(wanted) - 1
            if 0 <= index < len(names):
                return names[index]
        raise QgsProcessingException(
            "No worksheet called '{}'. This workbook has: {}.".format(
                wanted, ", ".join(names)))

    # ══════════════════════════════════════════
    # ── Sanity check on the coordinates ───────
    # ══════════════════════════════════════════
    @staticmethod
    def _crs_warning(crs, x_min, x_max, y_min, y_max):
        """Message when the numbers do not suit the chosen CRS, else None."""
        looks_like_degrees = (-180.0 <= x_min and x_max <= 180.0
                              and -90.0 <= y_min and y_max <= 90.0)
        if crs.isGeographic() and not looks_like_degrees:
            return ("{} is a geographic CRS, so it expects degrees, but the "
                    "coordinates run to X {:g} and Y {:g}. These look like "
                    "projected metres - pick the UTM zone the survey was "
                    "recorded in.".format(crs.authid() or "The chosen CRS",
                                          x_max, y_max))
        if not crs.isGeographic() and looks_like_degrees:
            return ("{} is a projected CRS, so it expects metres, but every "
                    "coordinate fits inside the range of longitude and "
                    "latitude. If the table holds degrees, set the CRS to "
                    "EPSG:4326.".format(crs.authid() or "The chosen CRS"))
        return None

    # ══════════════════════════════════════════
    # ── Main ──────────────────────────────────
    # ══════════════════════════════════════════
    def processAlgorithm(self, parameters, context, feedback):

        path = self.parameterAsFile(parameters, self.INPUT_FILE, context)
        x_wanted = (self.parameterAsString(
            parameters, self.X_FIELD, context) or "").strip()
        y_wanted = (self.parameterAsString(
            parameters, self.Y_FIELD, context) or "").strip()
        z_wanted = (self.parameterAsString(
            parameters, self.Z_FIELD, context) or "").strip()
        label_wanted = (self.parameterAsString(
            parameters, self.LABEL_FIELD, context) or "").strip()
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        detect_types = self.parameterAsBoolean(
            parameters, self.DETECT_TYPES, context)
        skip_errors = self.parameterAsBoolean(
            parameters, self.SKIP_ERRORS, context)
        header_row = self.parameterAsInt(
            parameters, self.HEADER_ROW, context)

        opts = {
            "sheet": (self.parameterAsString(
                parameters, self.SHEET, context) or "").strip(),
            "delimiter": DELIMITER_CHARS[self.parameterAsEnum(
                parameters, self.DELIMITER, context)],
            "encoding": ENCODING_CODECS[self.parameterAsEnum(
                parameters, self.ENCODING, context)],
            "header_row": header_row,
        }

        feedback.pushInfo("=" * 60)
        feedback.pushInfo(" Create Points From Table")
        feedback.pushInfo("=" * 60)
        feedback.pushInfo("File      : {}".format(path))
        feedback.pushInfo("CRS       : {}  ({})".format(
            crs.authid() or "custom",
            "degrees" if crs.isGeographic() else "projected"))
        feedback.pushInfo("Columns   : X={}  Y={}  Z={}  label={}".format(
            x_wanted or "auto", y_wanted or "auto",
            z_wanted or "none", label_wanted or "none"))

        # ── Read ──────────────────────────────
        feedback.pushInfo("\nReading the table...")
        grid, file_delimiter = self._read_grid(path, opts, feedback)

        if len(grid) < header_row:
            raise QgsProcessingException(
                "The file has {} row(s), so there is nothing on header row "
                "{}.".format(len(grid), header_row))

        headers = _unique_headers(grid[header_row - 1])
        rows = [row for row in grid[header_row:]
                if any(cell is not None and str(cell).strip() != ""
                       for cell in row)]

        if header_row > 1:
            feedback.pushInfo("   Header    : row {} ({} row(s) above it "
                              "discarded)".format(header_row, header_row - 1))
        feedback.pushInfo("   Columns   : {}".format(len(headers)))
        feedback.pushInfo("   Data rows : {}".format(len(rows)))
        feedback.pushInfo("   Headers   : {}".format(", ".join(headers)))

        if not rows:
            raise QgsProcessingException(
                "The table has headers but no data rows below row "
                "{}.".format(header_row))

        # ── Resolve the columns ───────────────
        x_header = _match_header(x_wanted, headers, X_ALIASES)
        y_header = _match_header(y_wanted, headers, Y_ALIASES)
        if x_header is None or y_header is None:
            missing = []
            if x_header is None:
                missing.append("X ('{}')".format(x_wanted or "auto-detect"))
            if y_header is None:
                missing.append("Y ('{}')".format(y_wanted or "auto-detect"))
            raise QgsProcessingException(
                "Could not find the {} column(s).\nThe table has: {}.\nSet "
                "the column names on the form to match, or check the header "
                "row under Advanced.".format(
                    " and ".join(missing), ", ".join(headers)))

        z_header = (_match_header(z_wanted, headers, Z_ALIASES)
                    if z_wanted else None)
        if z_wanted and z_header is None:
            feedback.pushWarning(
                "No Z column called '{}' - the output stays 2D.".format(
                    z_wanted))

        label_header = None
        if label_wanted:
            label_header = _match_header(label_wanted, headers, ())
            if label_header is None:
                feedback.pushWarning(
                    "No label column called '{}' - the points are created "
                    "but not labelled.".format(label_wanted))

        for label, wanted, chosen in (("X", x_wanted, x_header),
                                      ("Y", y_wanted, y_header),
                                      ("Z", z_wanted, z_header)):
            if wanted and chosen and wanted.strip().lower() != chosen.lower():
                feedback.pushWarning(
                    "There is no column called '{}', so {} was taken from "
                    "'{}' instead - check that is the one you meant.".format(
                        wanted, label, chosen))

        feedback.pushInfo("   X column  : '{}'".format(x_header))
        feedback.pushInfo("   Y column  : '{}'".format(y_header))
        if z_header:
            feedback.pushInfo("   Z column  : '{}'".format(z_header))
        if label_header:
            feedback.pushInfo("   Label     : '{}'".format(label_header))

        # ── Field types ───────────────────────
        index_of = {name: i for i, name in enumerate(headers)}

        def cell(row, name):
            i = index_of[name]
            return row[i] if i < len(row) else None

        decimals = {
            name: _decimal_separator(
                (cell(row, name) for row in rows), file_delimiter)
            for name in headers}

        out_fields = QgsFields()
        types = {}
        for name in headers:
            type_id = T_STRING
            if detect_types:
                type_id = _column_type(
                    (cell(row, name) for row in rows), decimals[name])
            types[name] = type_id
            out_fields.append(make_field(name, type_id))

        if detect_types:
            numeric = [n for n in headers if types[n] != T_STRING]
            feedback.pushInfo("   Typed     : {} numeric, {} text".format(
                len(numeric), len(headers) - len(numeric)))

        european = sorted(n for n in headers
                          if decimals[n] == "," and types[n] != T_STRING)
        if european:
            feedback.pushInfo(
                "   Decimals  : comma-decimal column(s) {}".format(
                    ", ".join(european)))

        # ── Sink ──────────────────────────────
        geom_type = wkb_type('PointZ') if z_header else wkb_type('Point')
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, out_fields, geom_type, crs)
        if sink is None:
            raise QgsProcessingException(
                "Could not create the output layer. Check the output path "
                "is writable and not open in another program.")

        # ── Build the points ──────────────────
        feedback.pushInfo("\nBuilding points...")
        total = len(rows)
        created = skipped = bad_z = 0
        reported = 0
        x_min = y_min = float("inf")
        x_max = y_max = float("-inf")

        for i, row in enumerate(rows):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(i / float(total) * 95))

            spreadsheet_row = header_row + 1 + i
            x_raw = cell(row, x_header)
            y_raw = cell(row, y_header)
            x_val = _to_float(x_raw, decimals[x_header])
            y_val = _to_float(y_raw, decimals[y_header])

            if x_val is None or y_val is None:
                message = ("Row {}: X='{}' Y='{}' is not a usable "
                           "coordinate pair".format(
                               spreadsheet_row, x_raw, y_raw))
                if not skip_errors:
                    raise QgsProcessingException(
                        message + ".\nTick 'Skip rows with unusable "
                        "coordinates' to leave rows like this out instead.")
                skipped += 1
                if reported < MAX_SKIP_MESSAGES:
                    feedback.pushInfo("   Skipped - " + message)
                    reported += 1
                elif reported == MAX_SKIP_MESSAGES:
                    feedback.pushInfo(
                        "   Skipped - further skipped rows are counted but "
                        "not listed.")
                    reported += 1
                continue

            if z_header:
                z_val = _to_float(cell(row, z_header), decimals[z_header])
                if z_val is None:
                    z_val = 0.0
                    bad_z += 1
                geometry = QgsGeometry(QgsPoint(x_val, y_val, z_val))
            else:
                geometry = QgsGeometry.fromPointXY(QgsPointXY(x_val, y_val))

            attributes = []
            for name in headers:
                raw = cell(row, name)
                if types[name] == T_STRING:
                    attributes.append(
                        "" if raw is None else str(raw).strip())
                else:
                    attributes.append(_to_float(raw, decimals[name]))

            feature = QgsFeature(out_fields)
            feature.setGeometry(geometry)
            feature.setAttributes(attributes)
            sink.addFeature(feature)

            created += 1
            x_min, x_max = min(x_min, x_val), max(x_max, x_val)
            y_min, y_max = min(y_min, y_val), max(y_max, y_val)

        if not created:
            raise QgsProcessingException(
                "Not one row produced a point. All {} row(s) had an "
                "unusable X or Y - check the column names in the log against "
                "what is really in the file.".format(total))

        # ── Labelling ─────────────────────────
        if label_header and context.willLoadLayerOnCompletion(dest_id):
            processor = _PointLabelPostProcessor(label_header)
            _LIVE_POST_PROCESSORS.append(processor)
            context.layerToLoadOnCompletionDetails(
                dest_id).setPostProcessor(processor)
            feedback.pushInfo(
                "\nLabelling switched on, using '{}'.".format(label_header))
        elif label_header:
            feedback.pushInfo(
                "\nLabel column '{}' resolved, but the result is not being "
                "added to the project, so no style is applied.".format(
                    label_header))

        # ── Summary ───────────────────────────
        feedback.pushInfo("\n" + "=" * 60)
        feedback.pushInfo("COMPLETED : {} point(s) from {} row(s)".format(
            created, total))
        if skipped:
            feedback.pushWarning(
                "SKIPPED   : {} row(s) had an unusable X or Y".format(skipped))
        if bad_z:
            feedback.pushWarning(
                "Z DEFAULTS: {} row(s) had no readable Z and got 0".format(
                    bad_z))
        feedback.pushInfo("Extent    : X {:g} to {:g}".format(x_min, x_max))
        feedback.pushInfo("            Y {:g} to {:g}".format(y_min, y_max))
        feedback.pushInfo("CRS       : {}".format(crs.authid() or "custom"))

        warning = self._crs_warning(crs, x_min, x_max, y_min, y_max)
        if warning:
            feedback.pushWarning("\nCHECK THE CRS: " + warning)

        feedback.pushInfo("=" * 60)
        feedback.setProgress(100)

        return {self.OUTPUT: dest_id}

    # ══════════════════════════════════════════
    # ── Metadata ──────────────────────────────
    # ══════════════════════════════════════════
    def name(self):
        return "create_points_from_table"

    def displayName(self):
        return "Create Points From Table"

    def group(self):
        return "KGA Data Management"

    def groupId(self):
        return "kgadatamanagement"

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/create_points_from_table'

    def shortDescription(self):
        return ("Build a point layer from the XY columns of a CSV, Excel or "
                "ODS table, without loading the table into QGIS first.")

    def shortHelpString(self):
        return (
            "<p>Turns the coordinate columns of a table into a point layer. "
            "Reads <b>.csv</b>, <b>.txt</b>, <b>.tsv</b>, <b>.xlsx</b>, "
            "<b>.xlsm</b>, <b>.xls</b> and <b>.ods</b> straight from disk "
            "&mdash; the table does not have to be added to the project "
            "first, and nothing is written back to it.</p>"

            "<h3>Finding the coordinate columns</h3>"
            "<p>Type the header names, or leave them blank and let the tool "
            "look. Matching ignores case, and falls back to the names these "
            "columns usually carry:</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>X</th><td>x, easting, east, lon, longitude, xcoord, "
            "coord_x, point_x, utm_e</td></tr>"
            "<tr><th>Y</th><td>y, northing, north, lat, latitude, ycoord, "
            "coord_y, point_y, utm_n</td></tr>"
            "<tr><th>Z</th><td>z, elev, elevation, height, altitude, level, "
            "rl</td></tr>"
            "</table>"
            "<p>The log always names the column it settled on, so a wrong "
            "guess is visible rather than silent. Filling in <b>Z</b> makes "
            "the output a 3D PointZ layer.</p>"

            "<h3>Numbers that are not clean</h3>"
            "<p>Coordinates survive the usual damage on the way out of a "
            "survey package or a spreadsheet:</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>In the table</th><th>Read as</th></tr>"
            "<tr><td>1,407,186.000</td><td>1407186.0</td></tr>"
            "<tr><td>1 407 186,25</td><td>1407186.25</td></tr>"
            "<tr><td>1407186,000</td><td>1407186.0</td></tr>"
            "<tr><td>&nbsp;556120&nbsp;</td><td>556120.0</td></tr>"
            "<tr><td>NULL, N/A, blank</td><td>row skipped or the run "
            "stops</td></tr>"
            "</table>"
            "<p>The third row is the awkward one: on its own "
            "<code>1407186,000</code> is 1407186.0 in Europe and "
            "1,407,186,000 elsewhere. The decision is made once per "
            "column, from whichever value in it can only be read one way "
            "&mdash; so a column holding <code>12,5</code> anywhere is "
            "comma-decimal throughout. The log names any column read that "
            "way, which is worth a glance when a coordinate looks a "
            "thousand times too big.</p>"

            "<h3>Column types</h3>"
            "<p><b>Detect numeric columns</b> types each column as Integer, "
            "Real or Text from what is actually in it, so the result works "
            "with graduated symbology, statistics and the field calculator "
            "straight away. Codes with a leading zero &mdash; 007, a phone "
            "number &mdash; stay Text, because typing them as numbers would "
            "lose the zero. Untick it to get the old behaviour, where every "
            "column is Text.</p>"

            "<h3>Labels</h3>"
            "<p>Naming a <b>label column</b> loads the result with "
            "labelling already on: 9 pt black, white buffer, placed around "
            "the point. Restyle it afterwards like any other layer. This "
            "only applies to a layer added to the project &mdash; writing "
            "straight to a file carries geometry and attributes, never "
            "style.</p>"

            "<h3>Awkward files</h3>"
            "<p>The <i>Advanced</i> section handles the ones that do not "
            "come out of the box clean: a <b>worksheet</b> other than the "
            "first, a <b>header row</b> further down because the sheet "
            "starts with a title block, and an explicit <b>separator</b> or "
            "<b>encoding</b> when the guess goes wrong. Khmer or accented "
            "text arriving as mojibake means the encoding is wrong &mdash; "
            "CSVs saved from Excel on Windows are usually Windows-1252.</p>"

            "<h3>Watch out for</h3>"
            "<p><b>The CRS is the one the table is already in</b>, not the "
            "one you want. This is the single most common way to end up "
            "with points in the wrong hemisphere, so the tool checks the "
            "numbers against the CRS and warns when degrees are paired with "
            "a projected CRS or metres with a geographic one.</p>"
            "<p><b>Check the extent in the log.</b> It is the fastest way "
            "to catch X and Y being the wrong way round.</p>"
            "<p><b>Shapefile output truncates field names</b> to 10 "
            "characters and caps columns at 255. GeoPackage does neither.</p>"
        )

    def createInstance(self):
        return CreatePointsFromTable()

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

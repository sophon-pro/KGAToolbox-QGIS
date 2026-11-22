# -*- coding: utf-8 -*-
"""Reservoir Storage Analyzer: the window.

UI and wiring only; every calculation lives in ``core/reservoir_storage.py``.
The window is modeless and lives until closed. Long jobs run on a ``Worker``
thread that polls a stop flag, so closing the window or unloading the plugin
never leaves a thread running against deleted widgets.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

import os
import math
import time
import tempfile
from contextlib import suppress
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from qgis.core import (Qgis, QgsProject, QgsRasterLayer, QgsVectorLayer,
                       QgsVectorFileWriter, QgsMapLayerProxyModel, QgsSettings)
from qgis.gui import (QgsMapLayerComboBox, QgsFieldComboBox, QgsFileWidget,
                      QgsCollapsibleGroupBox, QgsMessageBar)
from qgis.PyQt.QtCore import Qt, QSize, QThread, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QFontDatabase, QPalette, QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QToolButton, QComboBox, QDoubleSpinBox, QSpinBox,
    QCheckBox, QFileDialog, QFrame, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar, QPlainTextEdit, QGroupBox,
    QScrollArea, QAbstractItemView, QSizePolicy, QSplitter,
)
try:
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT as NavToolbar)
except ImportError:                                     # older matplotlib
    from matplotlib.backends.backend_qt5agg import (
        FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT as NavToolbar)

from .window_reveal import revealed_preview
from ..branding import help_button
from ..core import output_defaults as od
from ..core.reservoir_storage import (
    UNIT_KEYS, VOLUME_UNITS, Cancelled, build_table, contours_to_dem,
    dem_info as probe_dem, draw_curves, find_seed, fmt_volume, level_for_volume,
    level_stats, load_dem, make_levels, page_inches, parse_levels,
    render_inundation_maps, smooth_dem, write_geotiff)

ALG_NAME = 'reservoir_storage_analyzer'
TITLE = 'Reservoir Storage Analyzer'


# QGIS moved several enums between 3.22 and 3.40; look them up either way.
def _enum(name, *owners):
    for owner in owners:
        if owner is not None and hasattr(owner, name):
            return getattr(owner, name)
    raise AttributeError(name)


def layer_filter(name):
    return _enum(name, getattr(Qgis, "LayerFilter", None), QgsMapLayerProxyModel)


def msg_level(name):
    return _enum(name, getattr(Qgis, "MessageLevel", None), Qgis)


def file_mode(name):
    return _enum(name, getattr(QgsFileWidget, "StorageMode", None), QgsFileWidget)


SETTINGS = "KgaToolbox/ReservoirStorage/"
ALIGN_R = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def in_project(name):
    """`name` inside the project folder, for a Save dialog to open there."""
    folder = od.project_folder()
    return os.path.join(folder, name) if folder else name


def note(text=""):
    """Small grey label, like the help text under QGIS dialog fields."""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    pal = lbl.palette()
    pal.setColor(QPalette.ColorRole.WindowText,
                 pal.color(QPalette.ColorGroup.Disabled,
                           QPalette.ColorRole.WindowText))
    lbl.setPalette(pal)
    f = lbl.font()
    f.setPointSizeF(f.pointSizeF() * 0.92)
    lbl.setFont(f)
    return lbl


def spin(val, lo=-9999.0, hi=9999.0, dec=2, suffix=" m", tip=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    s.setSuffix(suffix)
    s.setToolTip(tip)
    return s


def file_widget(mode, flt="", tip=""):
    w = QgsFileWidget()
    w.setStorageMode(file_mode(mode))
    if flt:
        w.setFilter(flt)
    w.setToolTip(tip)
    return w


def unit_combo(default="Mm3"):
    cb = QComboBox()
    for k in UNIT_KEYS:
        cb.addItem(VOLUME_UNITS[k]["label"], k)
    cb.setCurrentIndex(UNIT_KEYS.index(default))
    return cb


def setup_table(t):
    t.setAlternatingRowColors(True)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill_table(table, df, cols, headers, fmts):
    table.clear()
    table.setColumnCount(len(cols))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(df))
    for r, (_, row) in enumerate(df.iterrows()):
        for c, (col, f) in enumerate(zip(cols, fmts)):
            v = row[col]
            item = QTableWidgetItem(f.format(v) if pd.notna(v) else "")
            if f != "{}":
                item.setTextAlignment(ALIGN_R)
            table.setItem(r, c, item)


def layer_source(layer, what):
    """Path GDAL/OGR can open for a project layer."""
    if layer is None:
        raise ValueError(f"Select a {what}.")
    src = layer.source()
    if os.path.isfile(src.split("|")[0]):
        return src
    if isinstance(layer, QgsRasterLayer) and layer.providerType() == "gdal":
        return src                 # /vsizip/, GPKG: and other GDAL sources
    if isinstance(layer, QgsVectorLayer):              # memory / scratch layer
        path = os.path.join(tempfile.gettempdir(),
                            f"rsc_{layer.id()[:24]}_{int(time.time())}.gpkg")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsProject.instance().transformContext(), opts)
        err = res[0]
        if int(getattr(err, "value", err)) != 0:
            raise ValueError(f"Could not read '{layer.name()}': {res[1]}")
        return path
    raise ValueError(f"'{layer.name()}' is not a local file. "
                     "Export it to GeoTIFF first.")


class Worker(QThread):
    """Runs fn(log, progress, stop) off the GUI thread.

    ``stop()`` is polled by the long loops in the core module; asking the
    thread to stop therefore ends it at the next level / feature / chunk
    instead of killing it.
    """
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._stop = False

    def request_stop(self):
        self._stop = True

    def should_stop(self):
        return self._stop

    def run(self):
        try:
            self.done.emit(self.fn(self.log.emit, self.progress.emit,
                                   self.should_stop))
        except Cancelled:
            self.cancelled.emit()
        except Exception as e:                         # noqa: BLE001
            import traceback
            self.log.emit(traceback.format_exc())
            self.failed.emit(str(e) or e.__class__.__name__)


class LayerRow(QWidget):
    """Project layer combo with a '…' button that loads a file as a layer."""

    FILTERS = {"raster": "RasterLayer", "polygon": "PolygonLayer",
               "line": "LineLayer"}

    def __init__(self, kind, optional=False):
        super().__init__()
        self.kind = kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.combo = QgsMapLayerComboBox()
        self.combo.setFilters(layer_filter(self.FILTERS[kind]))
        self.combo.setAllowEmptyLayer(optional)
        if optional:
            self.combo.setLayer(None)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Fixed)
        btn = QToolButton()
        btn.setText("…")
        btn.setToolTip("Add a file to the project")
        btn.clicked.connect(self.browse)
        lay.addWidget(self.combo, 1)
        lay.addWidget(btn)

    def browse(self):
        last = QgsSettings().value(SETTINGS + "last_dir", "")
        if self.kind == "raster":
            flt = "Rasters (*.tif *.tiff *.img *.asc *.vrt *.ecw);;All files (*)"
        else:
            flt = "Vectors (*.shp *.gpkg *.geojson *.json *.kml);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Add layer", last, flt)
        if not path:
            return
        QgsSettings().setValue(SETTINGS + "last_dir", str(Path(path).parent))
        name = Path(path).stem
        lyr = (QgsRasterLayer(path, name) if self.kind == "raster"
               else QgsVectorLayer(path, name, "ogr"))
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            self.combo.setLayer(lyr)

    def layer(self):
        return self.combo.currentLayer()


# ─────────────────────────────────────────────────────────────────────────
#  Main dialog
# ─────────────────────────────────────────────────────────────────────────

@revealed_preview
class ReservoirStorageDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setObjectName("KgaReservoirStorageDialog")
        self.setWindowTitle(TITLE)
        self.resize(1200, 760)
        self.df = None
        self.meta = {}
        self.workers = []
        self._dem_range = None
        self._torn_down = False
        self._closing = False
        self._build()
        self._restore()
        self.update_level_info()
        QTimer.singleShot(0, self.update_dem_info)    # after the window shows

    # ── layout ───────────────────────────────────────────────────────────
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = QgsMessageBar()
        self.bar.setSizePolicy(QSizePolicy.Policy.Minimum,
                               QSizePolicy.Policy.Fixed)
        outer.addWidget(self.bar)

        body = QVBoxLayout()
        body.setContentsMargins(9, 9, 9, 9)
        outer.addLayout(body, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_params())
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 840])
        body.addWidget(split, 1)

        # Curves
        tab = QWidget()
        v = QVBoxLayout(tab)
        self.fig = Figure(figsize=(10, 5), facecolor="white")
        self.canvas = FigureCanvas(self.fig)
        v.addWidget(NavToolbar(self.canvas, tab))
        v.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_csv = QPushButton("Save Table…")
        self.btn_png = QPushButton("Save Image…")
        for b in (self.btn_csv, self.btn_png):
            b.setEnabled(False)
            row.addWidget(b)
        self.btn_csv.clicked.connect(self.save_csv)
        self.btn_png.clicked.connect(self.save_png)
        v.addLayout(row)
        self.tabs.addTab(tab, "Curves")

        self.table = setup_table(QTableWidget())
        self.tabs.addTab(self.table, "Table")

        self.map_tab = MapTab(self)
        self.tabs.addTab(self.map_tab, "Inundation Maps")
        self.lookup_tab = LookupTab(self)
        self.tabs.addTab(self.lookup_tab, "Volume Lookup")
        self.export_tab = ExportTab(self)
        self.tabs.addTab(self.export_tab, "Export Rasters")
        self.contour_tab = ContourTab(self)
        self.tabs.addTab(self.contour_tab, "Contours to DEM")

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont))
        self.tabs.addTab(self.log_box, "Log")

    def _build_params(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 6, 0)

        # Input
        g = QGroupBox("Input")
        f = QFormLayout(g)
        self.dem = LayerRow("raster")
        self.dem.combo.layerChanged.connect(self.update_dem_info)
        self.dem.setToolTip("Elevation raster in a projected CRS (metres).")
        f.addRow("DEM", self.dem)
        self.dem_info = note()
        f.addRow("", self.dem_info)
        self.boundary = LayerRow("polygon", optional=True)
        self.boundary.setToolTip("Only cells inside this polygon are used.")
        f.addRow("Boundary", self.boundary)
        self.split_line = LayerRow("line", optional=True)
        self.split_line.setToolTip(
            "Optional line (e.g. dam axis) crossing the whole DEM.\n"
            "Only the chosen side is used.")
        f.addRow("Split line", self.split_line)
        self.side = QComboBox()
        for text, key in (("West side of the line", "west"),
                          ("East side of the line", "east"),
                          ("North side of the line", "north"),
                          ("South side of the line", "south")):
            self.side.addItem(text, key)
        self.side.setToolTip("Which part of the DEM to keep when a split line "
                             "is chosen.")
        f.addRow("Keep", self.side)
        v.addWidget(g)

        # Water levels
        g = QGroupBox("Water Levels")
        f = QFormLayout(g)
        self.wl_min = spin(39.0)
        self.wl_max = spin(44.0)
        self.wl_step = spin(0.1, 0.01, 100)
        from_row = QHBoxLayout()
        from_row.addWidget(self.wl_min, 1)
        btn = QToolButton()
        btn.setText("DEM range")
        btn.setToolTip("Set From/To to the lowest and highest DEM cell")
        btn.clicked.connect(self.use_dem_range)
        from_row.addWidget(btn)
        f.addRow("From", from_row)
        f.addRow("To", self.wl_max)
        f.addRow("Step", self.wl_step)
        self.custom_levels = QLineEdit()
        self.custom_levels.setPlaceholderText("e.g. 40, 41.5, 43.8")
        self.custom_levels.setToolTip(
            "Leave empty to use From/To/Step. Otherwise these levels are used.")
        f.addRow("Or levels", self.custom_levels)
        self.lev_info = note()
        f.addRow("", self.lev_info)
        for s in (self.wl_min, self.wl_max, self.wl_step):
            s.valueChanged.connect(self.update_level_info)
        self.custom_levels.textChanged.connect(self.update_level_info)
        v.addWidget(g)

        # Method
        g = QGroupBox("Method")
        f = QFormLayout(g)
        self.mode = QComboBox()
        self.mode.addItems(["Natural ground", "Excavated to base level"])
        self.mode.setToolTip(
            "Excavated: ground inside the flooded area is lowered to the base\n"
            "level before filling. Soil removed is reported separately.")
        f.addRow("Surface", self.mode)
        self.base = spin(39.0, tip="Bottom level after excavation")
        self.base.setEnabled(False)
        self.mode.currentIndexChanged.connect(
            lambda i: self.base.setEnabled(i == 1))
        f.addRow("Base level", self.base)
        self.sigma = spin(0.0, 0, 10, 1, " px",
                          "Gaussian smoothing before calculating.\n"
                          "Leave at 0 for reported volumes; it changes them "
                          "slightly.")
        self.sigma.setSpecialValueText("Off")
        f.addRow("Smoothing", self.sigma)
        v.addWidget(g)

        # Output
        g = QgsCollapsibleGroupBox("Output")
        f = QFormLayout(g)
        self.unit = unit_combo()
        self.unit.setToolTip("Unit used on the curve and map labels.\n"
                             "The CSV always has m³, 1,000 m³ and million m³.")
        self.unit.currentIndexChanged.connect(self.redraw)
        f.addRow("Volume unit", self.unit)
        self.out_dir = file_widget("GetDirectory",
                                   tip="CSV and image are saved here after Run")
        f.addRow("Folder", self.out_dir)
        self.chk_csv = QCheckBox("Save table (CSV)")
        self.chk_png = QCheckBox("Save curve image (PNG)")
        f.addRow("", self.chk_csv)
        f.addRow("", self.chk_png)
        v.addWidget(g)
        v.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(w)
        scroll.setMinimumWidth(330)

        # Run sits under the settings it uses; progress shows only when busy
        panel = QWidget()
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(0, 0, 6, 0)
        pv.addWidget(scroll, 1)
        self.busy = QLabel()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(10)
        self.busy.hide()
        self.progress.hide()
        self.btn_run = QPushButton("Calculate Storage")
        self.btn_run.setDefault(True)
        self.btn_run.setMinimumHeight(30)
        self.btn_run.setToolTip("Build the elevation–area–volume table and curves")
        self.btn_run.clicked.connect(self.run)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setMinimumHeight(30)
        self.btn_stop.setToolTip("Stop the running job")
        self.btn_stop.clicked.connect(self.stop_jobs)
        self.btn_stop.hide()
        pv.addWidget(self.busy)
        pv.addWidget(self.progress)
        row = QHBoxLayout()
        row.addWidget(self.btn_run, 1)
        row.addWidget(self.btn_stop)
        row.addWidget(help_button(ALG_NAME, panel))
        pv.addLayout(row)
        return panel

    # ── remembered settings ──────────────────────────────────────────────
    def _restore(self):
        s = QgsSettings()
        self.wl_min.setValue(float(s.value(SETTINGS + "wl_min", 39.0)))
        self.wl_max.setValue(float(s.value(SETTINGS + "wl_max", 44.0)))
        self.wl_step.setValue(float(s.value(SETTINGS + "wl_step", 0.1)))
        unit = s.value(SETTINGS + "unit", "Mm3")
        if unit in UNIT_KEYS:
            self.unit.setCurrentIndex(UNIT_KEYS.index(unit))
        # In a project the results go to the project folder, not where the last
        # project left them.
        self.out_dir.setFilePath(od.project_folder()
                                 or s.value(SETTINGS + "out_dir", ""))
        self.chk_csv.setChecked(s.value(SETTINGS + "save_csv", "true") == "true")
        self.chk_png.setChecked(s.value(SETTINGS + "save_png", "true") == "true")

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    def shutdown(self):
        """Close or plugin unload: stop every job, then save the settings."""
        if self._torn_down:
            return
        self._torn_down = True
        self._closing = True
        for w in list(self.workers):
            w.request_stop()
        for w in list(self.workers):
            if w.wait(20000):                # ended: safe to let go of it
                self.workers.remove(w)
        self._save_settings()
        self.hide()

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS + "wl_min", self.wl_min.value())
        s.setValue(SETTINGS + "wl_max", self.wl_max.value())
        s.setValue(SETTINGS + "wl_step", self.wl_step.value())
        s.setValue(SETTINGS + "unit", self.unit.currentData())
        s.setValue(SETTINGS + "out_dir", self.out_dir.filePath())
        s.setValue(SETTINGS + "save_csv", "true" if self.chk_csv.isChecked() else "false")
        s.setValue(SETTINGS + "save_png", "true" if self.chk_png.isChecked() else "false")

    # ── messages ─────────────────────────────────────────────────────────
    def log(self, msg):
        self.log_box.appendPlainText(msg)

    def warn(self, text):
        self.bar.pushMessage("", text, msg_level("Warning"), 8)

    def ok(self, text):
        self.bar.pushMessage("", text, msg_level("Success"), 6)

    def fail(self, text):
        self.bar.pushMessage("Error", f"{text}  (details in the Log tab)",
                             msg_level("Critical"), 0)

    # ── parameter helpers ────────────────────────────────────────────────
    def update_dem_info(self, *_):
        self._dem_range = None
        lyr = self.dem.layer()
        if lyr is None:
            self.dem_info.setText("No raster layer in the project — use … "
                                  "to add one.")
            return
        try:
            src = layer_source(lyr, "DEM")
            info = probe_dem(src)            # approximate: instant on big DEMs
        except Exception as e:                         # noqa: BLE001
            self.dem_info.setText(f"<span style='color:#c0392b'>{e}</span>")
            return
        lo, hi = info["min"], info["max"]
        self._dem_range = (lo, hi)
        crs = lyr.crs()
        text = (f"{info['nx']} × {info['ny']} cells, {info['cell']:g} m, "
                f"{crs.authid() or 'no CRS'}<br>Elevation ≈ {lo:.2f} – "
                f"{hi:.2f} m")
        if crs.isValid() and crs.isGeographic():
            text += ("<br><span style='color:#c0392b'>Geographic CRS (degrees). "
                     "Reproject to UTM before running.</span>")
        self.dem_info.setText(text)
        if not self.out_dir.filePath() and os.path.isfile(src.split("|")[0]):
            self.out_dir.setFilePath(str(Path(src.split("|")[0]).parent))
        self._fit_levels_to_dem()

    def _fit_levels_to_dem(self):
        """Move levels that miss the DEM entirely onto it.

        The remembered From/To belong to whatever DEM was used last. A range
        that does not overlap this one would give an empty table, so replace
        it; a range that overlaps is the user's choice and is left alone.
        """
        if not self._dem_range:
            return
        lo, hi = self._dem_range
        if self.wl_max.value() <= lo or self.wl_min.value() >= hi:
            self._set_range(lo, hi)
        if not self.custom_levels.text().strip():
            return
        with suppress(ValueError):
            if all(v <= lo or v >= hi for v in parse_levels(
                    self.custom_levels.text())):
                self.custom_levels.clear()

    def _set_range(self, lo, hi):
        self.wl_min.setValue(math.floor(lo * 10) / 10)
        self.wl_max.setValue(math.ceil(hi * 10) / 10)
        self.base.setValue(math.floor(lo * 10) / 10)
        span = hi - lo
        self.wl_step.setValue(0.1 if span <= 30 else (0.5 if span <= 150
                                                      else 1.0))

    def use_dem_range(self):
        if self.dem.layer() is not None:
            with suppress(Exception):                  # exact range on demand
                info = probe_dem(layer_source(self.dem.layer(), "DEM"), True)
                self._dem_range = (info["min"], info["max"])
        if self._dem_range:
            self._set_range(*self._dem_range)

    def map_level_suggestion(self, n=5):
        """'a, b, c …' evenly spread over the DEM, for the empty level boxes."""
        if not self._dem_range:
            return ""
        lo, hi = self._dem_range
        pts = np.linspace(lo + (hi - lo) * 0.2, hi, n)
        return ", ".join(f"{v:.1f}".rstrip("0").rstrip(".") for v in pts)

    def levels(self):
        txt = self.custom_levels.text().strip()
        if txt:
            try:
                lv = parse_levels(txt)
            except ValueError:
                raise ValueError("Levels must be numbers separated by "
                                 "commas, e.g. 40, 41.5, 42") from None
        else:
            lv = list(make_levels(self.wl_min.value(), self.wl_max.value(),
                                  self.wl_step.value()))
        if len(lv) < 2:
            raise ValueError("At least two water levels are needed.")
        return lv

    def update_level_info(self, *_):
        try:
            lv = self.levels()
            self.lev_info.setText(f"{len(lv)} levels, {lv[0]:g} to {lv[-1]:g} m")
        except ValueError as e:
            self.lev_info.setText(f"<span style='color:#c0392b'>{e}</span>")

    def settings(self):
        """Current parameters as a dict, shared by every tab."""
        dem = self.dem.layer()
        if dem is not None and dem.crs().isValid() and dem.crs().isGeographic():
            raise ValueError("The DEM is in degrees. Reproject it to a "
                             "metric CRS (e.g. EPSG:32648) first.")
        bnd = self.boundary.layer()
        line = self.split_line.layer()
        exc = self.mode.currentIndex() == 1
        if dem is None:
            raise ValueError("Select a DEM.")
        return {
            "dem": layer_source(dem, "DEM"),
            "dem_name": dem.name(),
            "boundary": layer_source(bnd, "boundary") if bnd else None,
            "boundary_name": bnd.name() if bnd else "",
            "line": layer_source(line, "split line") if line else None,
            "side": self.side.currentData(),
            "sigma": self.sigma.value(),
            "excavation": exc,
            "base": self.base.value() if exc else None,
            "unit": self.unit.currentData(),
        }

    def default_folder(self, s):
        return self.out_dir.filePath() or str(Path(s["dem"].split("|")[0]).parent)

    def _progress(self, value):
        if self.progress.maximum() == 0:          # switch from busy to percent
            self.progress.setRange(0, 100)
        self.progress.setValue(value)

    def start(self, fn, on_done, buttons=(), on_log=None, label="Working…"):
        if self._torn_down:
            return
        w = Worker(fn)
        w.log.connect(on_log or self.log)
        w.progress.connect(self._progress)
        self.progress.setRange(0, 0)              # busy until first percent
        self.busy.setText(label)
        self.busy.show()
        self.progress.show()
        self.btn_stop.show()
        for b in buttons:
            b.setEnabled(False)

        def finish(result=None, err=None, stopped=False):
            for b in buttons:
                b.setEnabled(True)
            if not any(x.isRunning() and x is not w for x in self.workers):
                self.busy.hide()
                self.progress.hide()
                self.btn_stop.hide()
            if self._closing:
                return
            if stopped:
                self.warn("Stopped.")
            elif err is not None:
                self.fail(err)
            else:
                on_done(result)

        def forget():
            # Only once the thread has really ended: dropping the last
            # reference to a running QThread aborts QGIS.
            if w in self.workers:
                self.workers.remove(w)
            w.deleteLater()

        w.done.connect(lambda r: finish(r))
        w.failed.connect(lambda e: finish(err=e))
        w.cancelled.connect(lambda: finish(stopped=True))
        w.finished.connect(forget)
        self.workers.append(w)
        w.start()

    def stop_jobs(self):
        for w in self.workers:
            w.request_stop()
        self.busy.setText("Stopping…")

    # ── main calculation ─────────────────────────────────────────────────
    def run(self):
        try:
            s = self.settings()
            levels = self.levels()
        except ValueError as e:
            self.warn(str(e))
            return
        if s["excavation"] and s["base"] >= levels[-1]:
            self.warn("The base level is at or above the highest water "
                      "level, so nothing would be stored. Lower the base "
                      "level or raise To.")
            return
        self.bar.clearWidgets()
        self.log(f"\n── {time.strftime('%H:%M:%S')}  Storage calculation")
        self.log(f"  Levels : {levels[0]:g} – {levels[-1]:g} m "
                 f"({len(levels)} steps)")
        if s["excavation"]:
            self.log(f"  Surface: excavated to {s['base']:.2f} m")

        def job(log, progress, stop):
            d = load_dem(s, log)
            df = build_table(d["arr"], levels, d["px_area"], s["excavation"],
                             s["base"], progress, stop)
            last = df.iloc[-1]
            log(f"  Max volume {last.Volume_m3:,.0f} m³ "
                f"({last.Volume_Mm3:,.4f} million m³) at {last.Water_Level_m:g} m")
            return df, d["name"]

        self.start(job, lambda r: self.on_done(r, s), buttons=[self.btn_run],
                   label="Calculating storage…")

    def on_done(self, result, s):
        self.df, name = result
        surface = (f"excavated to {s['base']:.2f} m" if s["excavation"]
                   else "natural ground")
        parts = [f"Surface: {surface}"]
        if s["boundary_name"]:
            parts.append(f"Boundary: {s['boundary_name']}")
        if s["sigma"]:
            parts.append(f"Smoothing σ {s['sigma']:g}")
        self.meta = {"name": name,
                     "title": f"Storage Capacity Curve — {name}",
                     "subtitle": "   |   ".join(parts)}

        cols = ["Water_Level_m", "Area_ha", "Volume_m3", "Volume_1000m3",
                "Volume_Mm3"]
        hdrs = ["Level (m)", "Area (ha)", "Volume (m³)", "Volume (1,000 m³)",
                "Volume (million m³)"]
        fmts = ["{:.2f}", "{:,.2f}", "{:,.0f}", "{:,.2f}", "{:,.4f}"]
        if "Natural_Vol_m3" in self.df.columns:
            cols += ["Natural_Vol_m3", "Excavated_Soil_m3"]
            hdrs += ["Natural (m³)", "Soil removed (m³)"]
            fmts += ["{:,.0f}", "{:,.0f}"]
        fill_table(self.table, self.df, cols, hdrs, fmts)
        self.redraw()
        self.btn_csv.setEnabled(True)
        self.btn_png.setEnabled(True)
        self.tabs.setCurrentIndex(0)

        saved = []
        folder = self.out_dir.filePath()
        wants_files = self.chk_csv.isChecked() or self.chk_png.isChecked()
        if wants_files and not folder:
            self.warn("Choose an output folder to save the table / image "
                      "automatically (or use Save Table / Save Image).")
        if folder and wants_files:
            os.makedirs(folder, exist_ok=True)
            if self.chk_csv.isChecked():
                p = os.path.join(folder, f"{name}_storage.csv")
                self.df.to_csv(p, index=False)
                saved.append(p)
            if self.chk_png.isChecked():
                p = os.path.join(folder, f"{name}_storage_curve.png")
                self.fig.savefig(p, dpi=200, facecolor="white")
                saved.append(p)
        for p in saved:
            self.log(f"  Saved {p}")
        last = self.df.iloc[-1]
        self.ok(f"Max storage {fmt_volume(last.Volume_m3, s['unit'])} "
                f"at {last.Water_Level_m:g} m"
                + (f" — saved to {folder}" if saved else ""))

    def redraw(self, *_):
        if self.df is None:
            return
        draw_curves(self.fig, self.df, self.unit.currentData(),
                    self.meta.get("title", "Storage Capacity Curve"),
                    self.meta.get("subtitle", ""))
        self.canvas.draw_idle()

    def save_csv(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Save Table",
            in_project(f"{self.meta.get('name', 'storage')}_storage.csv"),
            "CSV (*.csv)")
        if p:
            self.df.to_csv(p, index=False)

    def save_png(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Save Image",
            in_project(f"{self.meta.get('name', 'storage')}_storage_curve.png"),
            "PNG (*.png);;PDF (*.pdf);;SVG (*.svg)")
        if p:
            self.fig.savefig(p, dpi=200, facecolor="white")


# ─────────────────────────────────────────────────────────────────────────
#  Inundation maps
# ─────────────────────────────────────────────────────────────────────────

class FitImage(QLabel):
    """Shows an image scaled to fit the widget, re-scaled on resize."""

    def __init__(self):
        super().__init__()
        self._pix = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background: white; border: 1px solid #c8c8c8;")
        self.setText("Maps appear here after Make Maps.")

    def set_image(self, path):
        self._pix = QPixmap(path)
        self._rescale()

    def _rescale(self):
        if self._pix and not self._pix.isNull():
            self.setPixmap(self._pix.scaled(
                self.size() - QSize(8, 8),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()


class MapTab(QWidget):
    PAGES = ["Fit to content", "A4", "A3", "A2", "A1", "A0", "Letter",
             "Tabloid"]

    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self.path = ""
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        f1, f2, f3 = QFormLayout(), QFormLayout(), QFormLayout()

        # what to map
        self.levels = QLineEdit()
        self.levels.setPlaceholderText("e.g. 40, 41, 42 - empty spreads "
                                       "five levels over the DEM")
        self.levels.setToolTip("One map panel per level, comma separated")
        self.cols = QSpinBox()
        self.cols.setRange(0, 4)
        self.cols.setSpecialValueText("Auto")
        self.cols.setToolTip("Auto picks the grid that gives the biggest "
                             "maps on the chosen page.")
        self.out = file_widget(
            "SaveFile",
            "PNG (*.png);;PDF (*.pdf);;TIFF (*.tif);;JPEG (*.jpg);;SVG (*.svg)",
            "Format follows the file extension. Leave empty to save a PNG "
            "in the output folder.")
        f1.addRow("Columns", self.cols)

        # page
        self.page = QComboBox()
        self.page.addItems(self.PAGES)
        self.page.setToolTip("Fit to content sizes the sheet to the maps.\n"
                             "A paper size scales text and lines with the page.")
        self.orient = QComboBox()
        self.orient.addItems(["Landscape", "Portrait"])
        page_row = QHBoxLayout()
        page_row.addWidget(self.page, 1)
        page_row.addWidget(self.orient, 1)
        self.page.currentIndexChanged.connect(self._page_changed)
        self.dpi = QSpinBox()
        self.dpi.setRange(72, 1200)
        self.dpi.setSingleStep(50)
        self.dpi.setSuffix(" dpi")
        self.dpi.setToolTip("Resolution for PNG, TIFF and JPEG. "
                            "PDF and SVG are vector and ignore it.")
        self.size_info = note()
        f2.addRow("Page", page_row)
        f2.addRow("Resolution", self.dpi)
        f2.addRow("", self.size_info)
        for w in (self.orient, self.dpi):
            (w.currentIndexChanged if isinstance(w, QComboBox)
             else w.valueChanged).connect(self._update_size_info)

        # contours
        self.ci = spin(0.5, 0.1, 100, 1, tip="Background contour interval")
        self.disp_sigma = spin(1.0, 0, 10, 1, " px",
                               "Smoothing for drawing contours only. "
                               "Does not affect the numbers.")
        self.min_len = spin(50, 0, 99999, 0,
                            tip="Hide contour pieces shorter than this")
        f3.addRow("Contour interval", self.ci)
        f3.addRow("Contour smoothing", self.disp_sigma)
        f3.addRow("Min. contour length", self.min_len)

        for f, stretch in ((f1, 3), (f2, 4), (f3, 3)):
            top.addLayout(f, stretch)
            top.addSpacing(10)
        save_row = QFormLayout()
        save_row.addRow("Water levels", self.levels)
        save_row.addRow("Save as", self.out)
        v.addLayout(save_row)
        v.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(note("Uses the input, surface and unit set on the left. "
                           "Figures on the maps match the Table."), 1)
        self.btn = QPushButton("Make Maps")
        self.btn.clicked.connect(self.generate)
        row.addWidget(self.btn)
        v.addLayout(row)

        self.preview = FitImage()
        v.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.path_lbl = note()
        self.btn_open = QPushButton("Open File")
        self.btn_folder = QPushButton("Open Folder")
        for b in (self.btn_open, self.btn_folder):
            b.setEnabled(False)
        self.btn_open.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.path)))
        self.btn_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(self.path).parent))))
        row.addWidget(self.path_lbl, 1)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_folder)
        v.addLayout(row)
        self._restore()

    # remembered between sessions
    def _restore(self):
        st = QgsSettings()
        self.page.setCurrentText(st.value(SETTINGS + "map_page", "A3"))
        self.orient.setCurrentText(st.value(SETTINGS + "map_orient",
                                            "Landscape"))
        self.dpi.setValue(int(st.value(SETTINGS + "map_dpi", 300)))
        self.cols.setValue(int(st.value(SETTINGS + "map_cols", 0)))
        self._page_changed()

    def _save(self):
        st = QgsSettings()
        st.setValue(SETTINGS + "map_page", self.page.currentText())
        st.setValue(SETTINGS + "map_orient", self.orient.currentText())
        st.setValue(SETTINGS + "map_dpi", self.dpi.value())
        st.setValue(SETTINGS + "map_cols", self.cols.value())

    def _page_size(self):
        return page_inches(self.page.currentText(),
                           self.orient.currentIndex() == 0)

    def _page_changed(self, *_):
        self.orient.setEnabled(self.page.currentIndex() != 0)
        self._update_size_info()

    def _update_size_info(self, *_):
        pg = self._page_size()
        if pg is None:
            self.size_info.setText("Sheet size follows the maps.")
        else:
            w, h = pg
            self.size_info.setText(
                f"{w * 25.4:.0f} × {h * 25.4:.0f} mm,  "
                f"{w * self.dpi.value():,.0f} × {h * self.dpi.value():,.0f} px")

    def generate(self):
        try:
            s = self.dlg.settings()
            lv = parse_levels(self.levels.text()
                              or self.dlg.map_level_suggestion())
            if not lv:
                raise ValueError("Enter at least one water level for the maps.")
            if len(lv) > 12:
                raise ValueError("A map sheet holds at most 12 panels; "
                                 "enter fewer water levels.")
        except ValueError as e:
            self.dlg.warn(str(e) if "could not convert" not in str(e)
                          else "Water levels must be numbers separated by "
                               "commas.")
            return
        out = self.out.filePath().strip()
        if not out:
            out = os.path.join(
                self.dlg.default_folder(s),
                f"{Path(s['dem'].split('|')[0]).stem}_inundation.png")
        elif not Path(out).suffix:
            out += ".png"
        if Path(out).suffix.lower() not in (".png", ".pdf", ".tif", ".tiff",
                                            ".jpg", ".jpeg", ".svg"):
            self.dlg.warn("Save as .png, .pdf, .tif, .jpg or .svg.")
            return
        self._save()
        page, dpi = self._page_size(), self.dpi.value()
        ci, dsig = self.ci.value(), self.disp_sigma.value()
        ml, nc = self.min_len.value(), self.cols.value()
        preview = os.path.join(tempfile.gettempdir(),
                               f"rsc_map_preview_{os.getpid()}.png")
        self.dlg.log(f"\n── {time.strftime('%H:%M:%S')}  Inundation maps "
                     f"({self.page.currentText()}"
                     f"{', ' + self.orient.currentText() if page else ''}, "
                     f"{dpi} dpi)")

        def job(log, progress, stop):
            d = load_dem(s, log)
            draw = smooth_dem(d["arr"], dsig) if dsig > 0 else d["arr"]
            render_inundation_maps(
                d["arr"], draw, d["gt"], lv, d["px_area"], s["excavation"],
                s["base"], s["unit"], ci, ml, nc,
                title=f"Inundation Extent — {d['name']}",
                meta={"name": d["name"], "cell": d["cell"], "crs": d["crs"]},
                page=page, dpi=dpi, out_path=out, preview_path=preview,
                log=log, stop=stop)
            return out, preview

        self.dlg.start(job, self.show_map, buttons=[self.btn],
                       label="Making maps…")

    def show_map(self, result):
        self.path, preview = result
        self.preview.set_image(preview)
        self.path_lbl.setText(self.path)
        self.btn_open.setEnabled(True)
        self.btn_folder.setEnabled(True)
        self.dlg.ok(f"Maps saved to {self.path}")


# ─────────────────────────────────────────────────────────────────────────
#  Volume → level lookup
# ─────────────────────────────────────────────────────────────────────────

class LookupTab(QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self.results = None
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.src = QComboBox()
        self.src.addItems(["Last calculation", "CSV file"])
        self.csv = file_widget("GetFile", "CSV (*.csv)",
                               "A table saved by this tool")
        self.csv.setEnabled(False)
        self.src.currentIndexChanged.connect(lambda i: self.csv.setEnabled(i == 1))
        self.unit = unit_combo()
        self.values = QLineEdit()
        self.values.setPlaceholderText("e.g. 1, 2.5, 5")
        f.addRow("Table", self.src)
        f.addRow("CSV file", self.csv)
        f.addRow("Volumes in", self.unit)
        f.addRow("Volumes", self.values)
        v.addLayout(f)
        row = QHBoxLayout()
        row.addWidget(note("Water level is interpolated linearly between "
                           "rows of the table."), 1)
        btn = QPushButton("Find Levels")
        btn.clicked.connect(self.lookup)
        self.values.returnPressed.connect(self.lookup)
        row.addWidget(btn)
        v.addLayout(row)
        self.table = setup_table(QTableWidget())
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addStretch()
        b = QPushButton("Save Table…")
        b.clicked.connect(self.save)
        row.addWidget(b)
        v.addLayout(row)

    def lookup(self):
        try:
            if self.src.currentIndex() == 0:
                if self.dlg.df is None:
                    raise ValueError("Run the calculation first, or choose "
                                     "a CSV file.")
                df = self.dlg.df
            else:
                if not self.csv.filePath():
                    raise ValueError("Choose a CSV file.")
                df = pd.read_csv(self.csv.filePath())
            targets = [float(x) for x in self.values.text().replace(";", ",")
                       .split(",") if x.strip()]
            if not targets:
                raise ValueError("Enter one or more volumes.")
        except ValueError as e:
            self.dlg.warn(str(e) if "could not convert" not in str(e)
                          else "Volumes must be numbers separated by commas.")
            return
        except Exception as e:                         # noqa: BLE001
            self.dlg.warn(f"Could not read the CSV: {e}")
            return
        unit = self.unit.currentData()
        f = VOLUME_UNITS[unit]["factor"]
        rows = []
        for t in targets:
            try:
                r = level_for_volume(df, t * f)
            except ValueError as e:                    # no volume column
                self.dlg.warn(str(e))
                return
            rows.append({"Input": t, "Volume_m3": t * f,
                         "Water_Level_m": r.get("elevation", np.nan),
                         "Area_ha": r.get("area_ha", np.nan),
                         "Note": r.get("error", "")})
        self.results = pd.DataFrame(rows)
        fill_table(self.table, self.results,
                   ["Input", "Volume_m3", "Water_Level_m", "Area_ha", "Note"],
                   [f"Volume ({VOLUME_UNITS[unit]['label']})", "Volume (m³)",
                    "Water level (m)", "Area (ha)", "Note"],
                   ["{:,.4g}", "{:,.0f}", "{:.3f}", "{:,.2f}", "{}"])

    def save(self):
        if self.results is None:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save Table",
                                           in_project("lookup.csv"),
                                           "CSV (*.csv)")
        if p:
            self.results.to_csv(p, index=False)


# ─────────────────────────────────────────────────────────────────────────
#  Depth / bottom rasters
# ─────────────────────────────────────────────────────────────────────────

class ExportTab(QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.levels = QLineEdit()
        self.levels.setPlaceholderText("e.g. 40, 42, 44 - empty spreads "
                                       "three levels over the DEM")
        self.folder = file_widget("GetDirectory",
                                  tip="Default: a 'rasters' folder in the "
                                      "output folder")
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("DEM name")
        self.chk_depth = QCheckBox("Water depth")
        self.chk_bed = QCheckBox("Bottom elevation")
        self.chk_depth.setToolTip("Water level minus ground, in metres")
        self.chk_bed.setToolTip(
            "Ground elevation under the water (m above datum).\n"
            "Same as the DEM in natural mode; in excavated mode the\n"
            "ground is lowered to the base level, so this is the\n"
            "design bottom of the pond.")
        self.chk_add = QCheckBox("Add to map")
        for c in (self.chk_depth, self.chk_add):
            c.setChecked(True)
        kinds = QHBoxLayout()
        kinds.addWidget(self.chk_depth)
        kinds.addWidget(self.chk_bed)
        kinds.addStretch()
        f.addRow("Water levels", self.levels)
        f.addRow("Rasters", kinds)
        f.addRow("Folder", self.folder)
        f.addRow("File prefix", self.prefix)
        f.addRow("", self.chk_add)
        v.addLayout(f)
        row = QHBoxLayout()
        row.addWidget(note(
            "One GeoTIFF per level, clipped to the flooded area. Summing the "
            "depth raster × cell area gives the same volume as the Table — "
            "useful for checking in Raster Surface Volume or ArcGIS Pro."), 1)
        self.btn = QPushButton("Export")
        self.btn.clicked.connect(self.generate)
        row.addWidget(self.btn)
        v.addLayout(row)
        self.table = setup_table(QTableWidget())
        v.addWidget(self.table, 1)

    def generate(self):
        try:
            s = self.dlg.settings()
            lv = parse_levels(self.levels.text()
                              or self.dlg.map_level_suggestion(3))
            if not lv:
                raise ValueError("Enter at least one water level.")
            if not (self.chk_depth.isChecked() or self.chk_bed.isChecked()):
                raise ValueError("Tick Water depth and/or Bottom elevation.")
        except ValueError as e:
            self.dlg.warn(str(e))
            return
        dem_file = s["dem"].split("|")[0]
        folder = self.folder.filePath() or os.path.join(
            self.dlg.default_folder(s), "rasters")
        prefix = self.prefix.text().strip() or Path(dem_file).stem
        depth_on, bed_on = self.chk_depth.isChecked(), self.chk_bed.isChecked()
        self.dlg.log(f"\n── {time.strftime('%H:%M:%S')}  Raster export")

        def job(log, progress, stop):
            d = load_dem(s, log)
            arr, seed = d["arr"], find_seed(d["arr"])
            rows = []
            for i, wl in enumerate(lv):
                if stop():
                    raise Cancelled()
                st = level_stats(arr, wl, seed, d["px_area"],
                                 s["excavation"], s["base"])
                z = st["zone"]
                ground = np.minimum(arr, s["base"]) if s["excavation"] else arr
                bed = np.where(z, ground, np.nan)
                tag = f"{wl:.2f}".replace(".", "_")
                files = []
                if depth_on:
                    p = os.path.join(folder, f"{prefix}_depth_{tag}.tif")
                    write_geotiff(p, np.where(z, wl - bed, np.nan),
                                  d["gt"], d["prj"])
                    files.append(p)
                if bed_on:
                    p = os.path.join(folder, f"{prefix}_bottom_{tag}.tif")
                    write_geotiff(p, bed, d["gt"], d["prj"])
                    files.append(p)
                log(f"  {wl:.2f} m  {st['volume_m3']:>14,.0f} m³  "
                    f"{st['area_m2'] / 1e4:>10,.2f} ha")
                rows.append({"Water_Level_m": wl, "Volume_m3": st["volume_m3"],
                             "Area_ha": st["area_m2"] / 1e4,
                             "Files": ", ".join(Path(p).name for p in files),
                             "_paths": files})
                progress(int((i + 1) / len(lv) * 100))
            return rows

        self.dlg.start(job, self.done, buttons=[self.btn],
                       label="Exporting rasters…")

    def done(self, rows):
        df = pd.DataFrame(rows)
        fill_table(self.table, df,
                   ["Water_Level_m", "Volume_m3", "Area_ha", "Files"],
                   ["Level (m)", "Volume (m³)", "Area (ha)", "Files"],
                   ["{:.2f}", "{:,.0f}", "{:,.2f}", "{}"])
        n = 0
        for r in rows:
            for p in r["_paths"]:
                n += 1
                if self.chk_add.isChecked():
                    lyr = QgsRasterLayer(p, Path(p).stem)
                    if lyr.isValid():
                        QgsProject.instance().addMapLayer(lyr)
        self.dlg.ok(f"{n} rasters written to {Path(rows[0]['_paths'][0]).parent}")


# ─────────────────────────────────────────────────────────────────────────
#  Contours → DEM
# ─────────────────────────────────────────────────────────────────────────

class ContourTab(QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.contours = LayerRow("line")
        self.field = QgsFieldComboBox()
        self.contours.combo.layerChanged.connect(self._layer_changed)
        self.cell = spin(1.0, 0.1, 1000, 2, tip="Output cell size")
        self.method = QComboBox()
        for txt, key in [("Linear", "linear"), ("Cubic", "cubic"),
                         ("Inverse distance", "idw"), ("Nearest", "nearest")]:
            self.method.addItem(txt, key)
        self.method.setToolTip(
            "Linear suits most contour sets.\n"
            "Inverse distance copes better with sparse or uneven contours.\n"
            "Cubic is smoother but can overshoot between lines.")
        self.clip = LayerRow("polygon", optional=True)
        self.out = file_widget("SaveFile", "GeoTIFF (*.tif)",
                               "Default: next to the contour file")
        f.addRow("Contours", self.contours)
        f.addRow("Elevation field", self.field)
        f.addRow("Cell size", self.cell)
        f.addRow("Method", self.method)
        f.addRow("Clip to", self.clip)
        f.addRow("Save as", self.out)
        v.addLayout(f)
        row = QHBoxLayout()
        row.addWidget(note("The new DEM is added to the map and selected as "
                           "the input on the left."), 1)
        self.btn = QPushButton("Create DEM")
        self.btn.clicked.connect(self.generate)
        row.addWidget(self.btn)
        v.addLayout(row)
        v.addStretch()
        self._layer_changed(self.contours.layer())

    def _layer_changed(self, lyr):
        self.field.setLayer(lyr)
        if lyr is None:
            return
        by_lower = {f.name().lower(): f.name() for f in lyr.fields()}
        for cand in ("elev", "elevation", "z", "height", "contour", "level",
                     "ele", "value"):
            if cand in by_lower:
                self.field.setField(by_lower[cand])
                break

    def generate(self):
        lyr = self.contours.layer()
        try:
            src = layer_source(lyr, "contour layer")
            if not lyr.crs().isValid():
                raise ValueError("The contour layer has no CRS. Set it in "
                                 "Layer Properties first.")
            if lyr.crs().isGeographic():
                raise ValueError("The contours are in degrees. Reproject "
                                 "them to a metric CRS first.")
            field = self.field.currentField()
            if not field:
                raise ValueError("Choose the elevation field.")
            clip = self.clip.layer()
            clip_src = layer_source(clip, "clip polygon") if clip else None
        except ValueError as e:
            self.dlg.warn(str(e))
            return
        out = self.out.filePath() or od.default_file(f"{lyr.name()}_dem", "tif")             or str(Path(src.split("|")[0]).with_name(f"{lyr.name()}_dem.tif"))
        if not out.lower().endswith((".tif", ".tiff")):
            out += ".tif"
        cell, method, crs_wkt = (self.cell.value(), self.method.currentData(),
                                 lyr.crs().toWkt())
        self.dlg.log(f"\n── {time.strftime('%H:%M:%S')}  Contours to DEM")
        self.dlg.tabs.setCurrentWidget(self.dlg.log_box)

        def job(log, progress, stop):
            return contours_to_dem(src, field, cell, method, out, clip_src,
                                   crs_wkt, log, progress, stop)

        self.dlg.start(job, self.done, buttons=[self.btn],
                       label="Creating DEM…")

    def done(self, path):
        lyr = QgsRasterLayer(path, Path(path).stem)
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            self.dlg.dem.combo.setLayer(lyr)
        self.dlg.ok(f"DEM saved to {path} and selected as input.")

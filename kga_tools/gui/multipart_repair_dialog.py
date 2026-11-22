# -*- coding: utf-8 -*-
"""Multipart Line Checker & Repair - the window.

Tick line layers, Check them (every true multipart feature is listed with the
reason it is multipart), then Repair. The geometry work is in
`core/multipart_repair`; it runs on the GUI thread with a progress bar.
"""

import os

from qgis.core import (
    QgsFeature, QgsProject, QgsUnitTypes, QgsVectorLayer,
)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .window_reveal import revealed_preview
from ..branding import open_docs
from ..core import output_defaults as od
from ..core.multipart_repair import (
    LINE_GEOM, bad_ids, build_repaired_layer, match_type, repair, scan_layer,
)

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
USER_ROLE = Qt.ItemDataRole.UserRole

OUT_INPLACE = "Fix in place (edit session: review, then Save or Roll back)"
OUT_GPKG_EACH = "New GeoPackage - one file per layer"
OUT_GPKG_ONE = "New GeoPackage - all layers in one file"
OUT_SHP = "New Shapefile - one file per layer"
OUT_TEMP = "Temporary layers (memory, not saved)"

GREEN = QColor(0, 140, 0)
ORANGE = QColor(210, 110, 0)
RED = QColor(200, 0, 0)


STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: #174592; }
"""


def group_path(node):
    parts, p = [], node.parent()
    while p is not None and p.parent() is not None:
        parts.insert(0, p.name())
        p = p.parent()
    return "/".join(parts) if parts else "(root)"


def safe_name(name):
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)


def num_item(v, fmt="{:.3f}"):
    it = QTableWidgetItem(fmt.format(v) if isinstance(v, float) else str(v))
    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return it


# ================================================================ dialog
@revealed_preview
class MultipartToolDialog(QDialog):
    CHECK_TAB = 1
    CHECK_HEIGHT = 720

    def __init__(self, parent=None):
        super().__init__(parent)
        self._torn_down = False
        self.setWindowTitle("Multipart Line Checker & Repair")
        self.resize(1050, 900)
        self.project = QgsProject.instance()
        self.root = self.project.layerTreeRoot()
        self.details = {}           # layer_id -> list of feature diagnostics
        self.detail_layer_id = None

        self.setStyleSheet(STYLE)
        main = QVBoxLayout(self)

        # ---------------- top: input | tabs (settings, check, output)
        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)

        self.in_box = in_box = QGroupBox("Input line layers")
        in_lay = QHBoxLayout(in_box)
        self.layer_list = QListWidget()
        self.layer_list.setMaximumHeight(110)
        in_lay.addWidget(self.layer_list)
        grid = QVBoxLayout()
        grid.setSpacing(4)
        for n, (text, slot) in enumerate((("All", self.select_all), ("None", self.select_none),
                                          ("Visible", self.select_visible),
                                          ("Refresh", self.populate_layers))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            b.setMinimumWidth(90)
            grid.addWidget(b)
        grid.addStretch()
        in_lay.addLayout(grid)
        top_lay.addWidget(in_box)

        self.tabs = QTabWidget()

        rep_box = QWidget()
        rep = QFormLayout(rep_box)
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setDecimals(3)
        self.sp_tol.setRange(0, 100000)
        self.sp_tol.setValue(0.5)
        self.sp_tol.setToolTip("Part end-points closer than this are snapped together (layer CRS units).")
        rep.addRow("Snap tolerance:", self.sp_tol)
        self.sp_min = QDoubleSpinBox()
        self.sp_min.setDecimals(3)
        self.sp_min.setRange(0, 100000)
        self.sp_min.setValue(0.1)
        self.sp_min.setToolTip("Parts shorter than this are deleted (slivers left by cutting). 0 = keep all.")
        rep.addRow("Min part length:", self.sp_min)
        self.chk_dedupe = QCheckBox("Remove duplicate vertices")
        self.chk_dedupe.setChecked(True)
        self.chk_union = QCheckBox("Dissolve overlapping parts (union)")
        self.chk_union.setChecked(True)
        rep.addRow(self.chk_dedupe)
        rep.addRow(self.chk_union)
        self.lbl_units = QLabel("<i>Units: layer CRS (shown after Check)</i>")
        rep.addRow(self.lbl_units)
        rep.addRow(QLabel("<i>Used by both Check and Repair.</i>"))
        self.tabs.addTab(self._top_aligned(rep_box), "Repair settings")

        out_box = QWidget()
        out = QFormLayout(out_box)
        self.cmb_format = QComboBox()
        self.cmb_format.addItems([OUT_GPKG_EACH, OUT_GPKG_ONE, OUT_SHP, OUT_TEMP, OUT_INPLACE])
        self.cmb_format.currentTextChanged.connect(self.update_output_widgets)
        out.addRow("Format:", self.cmb_format)
        self.folder = QgsFileWidget()
        self.folder.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        start = od.project_folder() or self.project.homePath()
        if start:
            self.folder.setFilePath(start)
        out.addRow("Output folder:", self.folder)
        self.txt_gpkg = QLineEdit("singlepart_lines.gpkg")
        out.addRow("GeoPackage name:", self.txt_gpkg)
        self.txt_suffix = QLineEdit("_fixed")
        out.addRow("Layer suffix:", self.txt_suffix)
        self.chk_only_bad = QCheckBox("Only layers that have multipart features")
        self.chk_only_bad.setChecked(True)
        self.chk_style = QCheckBox("Copy symbology and labels")
        self.chk_style.setChecked(True)
        self.chk_add = QCheckBox("Add output to map (below original)")
        self.chk_add.setChecked(True)
        for w in (self.chk_only_bad, self.chk_style, self.chk_add):
            out.addRow(w)
        self.tabs.addTab(self._top_aligned(out_box), "Output")

        top_lay.addWidget(self.tabs, 1)
        main.addWidget(top, 1)

        # ---------------- check: layer table + feature table
        chk_box = QWidget()
        chk_lay = QVBoxLayout(chk_box)
        hint = QLabel("<i>Double-click a layer row to list its features; "
                      "double-click a feature row to zoom to it.</i>")
        hint.setWordWrap(True)
        chk_lay.addWidget(hint)
        row = QHBoxLayout()
        self.btn_check = QPushButton("Check multipart")
        self.btn_check.clicked.connect(self.run_check)
        self.chk_select = QCheckBox("Select multipart features after check")
        self.chk_select.setChecked(True)
        row.addWidget(self.btn_check)
        row.addWidget(self.chk_select)
        row.addStretch()
        chk_lay.addLayout(row)

        tsplit = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Group", "Layer", "Features", "Multi-type", "True multipart",
             "Fixable (->1)", "Separate", "Null/empty", "Max parts"])
        self._setup_table(self.table, stretch_col=1)
        self.table.cellDoubleClicked.connect(self.show_layer_details)
        tsplit.addWidget(self.table)

        self.ftable = QTableWidget(0, 7)
        self.ftable.setHorizontalHeaderLabels(
            ["Feature ID", "Parts", "Shortest part", "Max gap", "Cause", "Parts after repair", "Result"])
        self._setup_table(self.ftable, stretch_col=4)
        self.ftable.setSortingEnabled(True)
        self.ftable.cellDoubleClicked.connect(self.zoom_to_feature)
        tsplit.addWidget(self.ftable)
        chk_lay.addWidget(tsplit)
        self.lbl_legend = QLabel(
            "<i><b>Fixable</b> = parts only had gaps/overlaps/slivers -> becomes ONE feature. "
            "<b>Separate</b> = parts are really apart or branched -> split into several features.</i>")
        self.lbl_legend.setWordWrap(True)
        chk_lay.addWidget(self.lbl_legend)
        self.tabs.insertTab(1, chk_box, "Check")

        # ---------------- log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFixedHeight(120)
        self.log.hide()                     # revealed by the first run
        main.addWidget(self.log)

        self.progress = QProgressBar()
        self.progress.hide()                # shown only while a run is going
        main.addWidget(self.progress)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_fix = QPushButton("Repair / Fix multipart")
        self.btn_fix.setStyleSheet("font-weight: bold;")
        self.btn_fix.clicked.connect(self.run_fix)
        btn_help = QPushButton("Help")
        btn_help.clicked.connect(lambda: open_docs('multipart_line_repair'))
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_fix)
        row.addWidget(btn_help)
        row.addWidget(btn_close)
        main.addLayout(row)

        self.populate_layers()
        self.update_output_widgets(self.cmb_format.currentText())
        self.tabs.currentChanged.connect(self._on_tab)

    # ---------------- dynamic size
    def _show_progress(self, maximum):
        self.progress.setRange(0, maximum)
        self.progress.setValue(0)
        self.progress.show()
        QApplication.processEvents()

    def _on_tab(self, index):
        QTimer.singleShot(0, self._fit)         # after the layout has taken the new page

    def _chrome_height(self):
        """Height of everything round the tab page, from size hints (not the
        current geometry, which is still the previous tab's while it changes)."""
        main = self.layout()
        gap = main.spacing()
        rows = [self.in_box.sizeHint().height(), self.tabs.tabBar().sizeHint().height() + 12,
                self.btn_fix.sizeHint().height()]
        if self.log.isVisible():
            rows.append(self.log.height())
        if self.progress.isVisible():
            rows.append(self.progress.sizeHint().height())
        margins = main.contentsMargins()
        return sum(rows) + gap * len(rows) + margins.top() + margins.bottom() + 8

    def _fit(self):
        """Height follows the current tab: the short Settings and Output tabs shrink
        the window instead of leaving a blank area the size of the Check tables."""
        if not self.isVisible():
            return
        self.layout().activate()
        page = self.tabs.currentWidget()
        if self.tabs.currentIndex() == self.CHECK_TAB:
            height = max(self.height(), self.CHECK_HEIGHT)
        else:
            height = self._chrome_height() + page.layout().sizeHint().height()
        self.resize(self.width(), height)

    # ---------------- small helpers
    @staticmethod
    def _top_aligned(form_widget):
        """A tab page whose form sits at the top instead of being stretched."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(form_widget)
        lay.addStretch()
        return page

    def _setup_table(self, t, stretch_col):
        h = t.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
        t.setMinimumHeight(50)
        t.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)  # columns never widen the window
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

    def settings(self):
        return {"tol": self.sp_tol.value(), "min_len": self.sp_min.value(),
                "dedupe": self.chk_dedupe.isChecked(), "union": self.chk_union.isChecked()}

    def msg(self, text):
        if self.log.isHidden():
            self.log.show()
            QTimer.singleShot(0, self._fit)
        self.log.appendPlainText(text)
        QApplication.processEvents()

    # ---------------- input list
    def populate_layers(self):
        self.layer_list.clear()
        for node in self.root.findLayers():
            lyr = node.layer()
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == LINE_GEOM:
                item = QListWidgetItem(f"{group_path(node)}  /  {lyr.name()}")
                item.setData(USER_ROLE, lyr.id())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(CHECKED if node.isVisible() else UNCHECKED)
                self.layer_list.addItem(item)

    def _set_all(self, func):
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            it.setCheckState(func(it))

    def select_all(self):
        self._set_all(lambda it: CHECKED)

    def select_none(self):
        self._set_all(lambda it: UNCHECKED)

    def select_visible(self):
        def vis(it):
            node = self.root.findLayer(it.data(USER_ROLE))
            return CHECKED if node and node.isVisible() else UNCHECKED
        self._set_all(vis)

    def checked_layers(self):
        out = []
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it.checkState() == CHECKED:
                lyr = self.project.mapLayer(it.data(USER_ROLE))
                if lyr:
                    out.append(lyr)
        return out

    def update_output_widgets(self, fmt):
        file_mode = fmt not in (OUT_TEMP, OUT_INPLACE)
        self.folder.setEnabled(file_mode)
        self.txt_gpkg.setEnabled(fmt == OUT_GPKG_ONE)
        new_layer = fmt != OUT_INPLACE
        for w in (self.txt_suffix, self.chk_only_bad, self.chk_style, self.chk_add):
            w.setEnabled(new_layer)

    def _check_units(self, layers):
        crs = layers[0].crs()
        units = QgsUnitTypes.toString(crs.mapUnits())
        same = all(x.crs() == crs for x in layers)
        self.lbl_units.setText(
            f"<i>Units: <b>{units}</b> ({crs.authid() or crs.description()})</i>" if same
            else "<i>Units: the ticked layers use different CRSs - each in its own units</i>")
        geo = [x.name() for x in layers if x.crs().isGeographic()]
        if geo:
            QMessageBox.warning(self, "Geographic CRS",
                                "These layers use degrees, so tolerance and min length are in degrees:\n"
                                + "\n".join(geo) + "\n\nUse a projected CRS (e.g. UTM 48N) for metres.")

    # ---------------- check
    def run_check(self):
        layers = self.checked_layers()
        if not layers:
            QMessageBox.warning(self, "No input", "Tick at least one line layer.")
            return
        self._check_units(layers)
        self.tabs.setCurrentIndex(self.CHECK_TAB)
        s = self.settings()
        self.table.setRowCount(0)
        self.ftable.setRowCount(0)
        self.details = {}
        self._show_progress(len(layers))
        self.msg(f"=== Check (tolerance {s['tol']}, min part {s['min_len']}) ===")
        for i, lyr in enumerate(layers, 1):
            total, multi_type, empty, det = scan_layer(lyr, s)
            self.details[lyr.id()] = det
            fixable = sum(1 for d in det if d["after"] <= 1)
            sep = len(det) - fixable
            node = self.root.findLayer(lyr.id())
            values = [group_path(node), lyr.name(), total, multi_type, len(det), fixable, sep, empty,
                      max((d["parts"] for d in det), default=0)]
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate(values):
                cell = num_item(v) if c >= 2 else QTableWidgetItem(str(v))
                cell.setData(USER_ROLE, lyr.id())
                if c == 4 and det:
                    cell.setForeground(RED)
                if c == 5 and fixable:
                    cell.setForeground(GREEN)
                if c == 6 and sep:
                    cell.setForeground(ORANGE)
                self.table.setItem(r, c, cell)
            if self.chk_select.isChecked():
                lyr.selectByIds([d["fid"] for d in det])
            if det:
                self.msg(f"{lyr.name()}: {len(det)} multipart  ->  {fixable} fixable, {sep} separate")
            self.progress.setValue(i)
        self.progress.hide()
        self.msg("Double-click a layer row to see each feature and the cause.\n")
        if self.table.rowCount():
            self.show_layer_details(0, 0)

    def show_layer_details(self, row, col):
        lid = self.table.item(row, 0).data(USER_ROLE)
        lyr = self.project.mapLayer(lid)
        if not lyr:
            return
        self.detail_layer_id = lid
        det = self.details.get(lid, [])
        self.ftable.setSortingEnabled(False)
        self.ftable.setRowCount(0)
        for d in det:
            r = self.ftable.rowCount()
            self.ftable.insertRow(r)
            fid_item = QTableWidgetItem()
            fid_item.setData(Qt.ItemDataRole.DisplayRole, d["fid"])
            fid_item.setData(USER_ROLE, d["fid"])
            self.ftable.setItem(r, 0, fid_item)
            self.ftable.setItem(r, 1, num_item(d["parts"]))
            self.ftable.setItem(r, 2, num_item(float(d["shortest"])))
            self.ftable.setItem(r, 3, num_item(float(d["max_gap"])))
            self.ftable.setItem(r, 4, QTableWidgetItem(d["cause"]))
            self.ftable.setItem(r, 5, num_item(d["after"]))
            res = QTableWidgetItem(d["status"])
            res.setForeground(GREEN if d["after"] <= 1 else ORANGE)
            self.ftable.setItem(r, 6, res)
        self.ftable.setSortingEnabled(True)
        if det:
            lyr.selectByIds([d["fid"] for d in det])
            if iface:
                iface.setActiveLayer(lyr)
                iface.mapCanvas().zoomToSelected(lyr)
        self.msg(f"Showing {len(det)} multipart feature(s) of {lyr.name()}")

    def zoom_to_feature(self, row, col):
        lyr = self.project.mapLayer(self.detail_layer_id) if self.detail_layer_id else None
        if not lyr:
            return
        fid = self.ftable.item(row, 0).data(USER_ROLE)
        lyr.selectByIds([fid])
        if not iface:
            return
        iface.setActiveLayer(lyr)
        canvas = iface.mapCanvas()
        canvas.zoomToFeatureIds(lyr, [fid])
        canvas.zoomByFactor(1.5)
        canvas.flashFeatureIds(lyr, [fid])

    # ---------------- fix
    def run_fix(self):
        layers = self.checked_layers()
        if not layers:
            QMessageBox.warning(self, "No input", "Tick at least one line layer.")
            return
        s = self.settings()
        fmt = self.cmb_format.currentText()
        folder = self.folder.filePath().strip()

        if fmt not in (OUT_TEMP, OUT_INPLACE):
            if not folder:
                QMessageBox.warning(self, "Output", "Choose an output folder.")
                return
            os.makedirs(folder, exist_ok=True)

        work = [(lyr, set(bad_ids(lyr))) for lyr in layers]
        if fmt == OUT_INPLACE or self.chk_only_bad.isChecked():
            work = [(x, b) for x, b in work if b]
            if not work:
                QMessageBox.information(self, "Nothing to fix",
                                        "None of the ticked layers has multipart features.")
                return

        if fmt == OUT_INPLACE:
            ans = QMessageBox.question(
                self, "Fix in place",
                "The original layers will be put in EDIT mode and changed.\n"
                "Nothing is saved until you click 'Save Layer Edits'.\n"
                "You can Undo (Ctrl+Z) or Roll Back. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return

        gpkg_one = None
        if fmt == OUT_GPKG_ONE:
            name = self.txt_gpkg.text().strip() or "singlepart_lines.gpkg"
            if not name.lower().endswith(".gpkg"):
                name += ".gpkg"
            gpkg_one = os.path.join(folder, name)
            if os.path.exists(gpkg_one):
                if not self._ask_overwrite(gpkg_one) or not self._remove(gpkg_one):
                    return

        self._show_progress(len(work))
        self.btn_fix.setEnabled(False)
        self.msg(f"=== Repair (tolerance {s['tol']}, min part {s['min_len']}) -> {fmt} ===")
        try:
            for i, (lyr, bad) in enumerate(work, 1):
                if fmt == OUT_INPLACE:
                    self._fix_in_place(lyr, bad, s)
                else:
                    self._fix_to_new(lyr, bad, s, fmt, folder, gpkg_one)
                self.progress.setValue(i)
        finally:
            self.btn_fix.setEnabled(True)
            self.progress.hide()
        if fmt == OUT_INPLACE:
            self.msg("Layers are in edit mode: check the result, then Save Layer Edits or Roll Back.")
        self.msg("Done.\n")
        if iface:
            iface.mapCanvas().refresh()

    def _ask_overwrite(self, path):
        ans = QMessageBox.question(self, "File exists", f"{path}\nalready exists. Overwrite?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return ans == QMessageBox.StandardButton.Yes

    def _remove(self, path):
        try:
            os.remove(path)
            return True
        except OSError as e:
            QMessageBox.critical(self, "Cannot overwrite",
                                 f"{path}\nis in use (maybe loaded in QGIS).\n{e}")
            return False

    def _fix_in_place(self, lyr, bad, s):
        self.msg(f"Editing {lyr.name()} in place ...")
        if not lyr.isEditable() and not lyr.startEditing():
            self.msg("  ! layer cannot be edited (read-only?)")
            return
        wkb = lyr.wkbType()
        prov = lyr.dataProvider()
        pk = lyr.primaryKeyAttributes()
        fixed = split = added = 0
        lyr.beginEditCommand("Repair multipart lines")
        try:
            for fid in bad:
                f = lyr.getFeature(fid)
                parts = repair(f.geometry(), s)
                if not parts:
                    continue
                lyr.changeGeometry(fid, match_type(parts[0], wkb))
                if len(parts) == 1:
                    fixed += 1
                    continue
                split += 1
                for p in parts[1:]:
                    attrs = list(f.attributes())
                    for idx in pk:  # new feature needs a new primary key (e.g. GeoPackage fid)
                        clause = prov.defaultValueClause(idx)
                        attrs[idx] = clause if clause else None
                    nf = QgsFeature(lyr.fields())
                    nf.setAttributes(attrs)
                    nf.setGeometry(match_type(p, wkb))
                    lyr.addFeature(nf)
                    added += 1
            lyr.endEditCommand()
        except Exception as e:
            lyr.destroyEditCommand()
            self.msg(f"  ! ERROR, changes for this layer undone: {e}")
            return
        lyr.triggerRepaint()
        self.msg(f"  {fixed} joined into one part, {split} split (+{added} new features). "
                 f"Multipart left: {len(bad_ids(lyr))}")

    def _fix_to_new(self, lyr, bad, s, fmt, folder, gpkg_one):
        out_name = f"{lyr.name()}{self.txt_suffix.text()}"
        self.msg(f"Processing {lyr.name()} ...")
        try:
            from qgis import processing
            src = build_repaired_layer(lyr, s, self.msg)
            if src.featureCount() == 0:
                self.msg("  ! the copy is empty, nothing written. Is a filter set on the layer "
                         "(right-click > Filter)? Is the source file readable?")
                return

            # GeoPackage: drop old 'fid', or split parts break its UNIQUE constraint
            if fmt in (OUT_GPKG_EACH, OUT_GPKG_ONE) and src.fields().indexOf("fid") != -1:
                src = processing.run("native:deletecolumn", {
                    "INPUT": src, "COLUMN": ["fid"], "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
                self.msg(f"  removed old 'fid' field ({src.featureCount()} features)")

            if fmt == OUT_TEMP:
                dest = "TEMPORARY_OUTPUT"
            elif fmt == OUT_GPKG_ONE:
                dest = f"ogr:dbname='{gpkg_one}' table=\"{safe_name(out_name)}\" (geom)"
            else:
                ext = ".gpkg" if fmt == OUT_GPKG_EACH else ".shp"
                dest = os.path.join(folder, safe_name(out_name) + ext)
                if os.path.exists(dest):
                    if not self._ask_overwrite(dest):
                        self.msg("  skipped (file exists)")
                        return
                    base = os.path.splitext(dest)[0]
                    files = ([base + e for e in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".qmd")]
                             if fmt == OUT_SHP else [dest])
                    if not all(self._remove(p) for p in files if os.path.exists(p)):
                        self.msg("  skipped (file in use)")
                        return

            # Anything still multipart (really separate parts) -> single features
            result = processing.run("native:multiparttosingleparts",
                                    {"INPUT": src, "OUTPUT": dest})["OUTPUT"]

            if fmt == OUT_TEMP:
                new = result
                new.setName(out_name)
            elif fmt == OUT_GPKG_ONE:
                new = QgsVectorLayer(f"{gpkg_one}|layername={safe_name(out_name)}", out_name, "ogr")
            else:
                new = QgsVectorLayer(result, out_name, "ogr")
            if not new or not new.isValid():
                self.msg("  ! output could not be loaded")
                return

            if new.featureCount() == 0:
                self.msg(f"  ! output is empty although the input had {src.featureCount()} features. "
                         f"Try 'Temporary layers' output to test, and send me this log.")
            self.msg(f"  {lyr.featureCount()} -> {new.featureCount()} features | "
                     f"multipart left: {len(bad_ids(new))}"
                     + ("" if fmt == OUT_TEMP else f" | saved: {gpkg_one or result}"))

            if self.chk_style.isChecked():
                if lyr.renderer():
                    new.setRenderer(lyr.renderer().clone())
                if lyr.labeling():
                    new.setLabeling(lyr.labeling().clone())
                    new.setLabelsEnabled(lyr.labelsEnabled())
            if self.chk_add.isChecked():
                node = self.root.findLayer(lyr.id())
                parent = node.parent()
                self.project.addMapLayer(new, False)
                parent.insertLayer(parent.children().index(node) + 1, new)
        except Exception as e:
            self.msg(f"  ! ERROR: {e}")

    # ---------------- lifecycle
    def showEvent(self, e):
        self.populate_layers()              # pick up layers added while it was hidden
        super().showEvent(e)
        QTimer.singleShot(0, self._fit)

    def closeEvent(self, e):
        self.log.clear()
        self.log.hide()
        self.progress.hide()
        super().closeEvent(e)

    def shutdown(self):
        """Plugin unload: hide the window."""
        self._torn_down = True
        self.hide()

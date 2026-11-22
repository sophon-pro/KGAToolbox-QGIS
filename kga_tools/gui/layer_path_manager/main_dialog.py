# -*- coding: utf-8 -*-
"""
Layer Path Manager - main dialog.

Design: the window shows the project's state in one status banner together
with the single action that fixes it ("3 layers can't find their data ->
Fix Broken Layers").  Everyday actions sit in one toolbar row; everything
else (validation, path format, reports, selection helpers, settings) lives in
the "More" menu, advanced filters behind "Filters" and logs behind "Details".

Engine modules used:
    inspection      -> layer_inspector.LayerInspector (+ BackgroundJob)
    repair/relocate -> path_repair.FileIndex / PathMatcher / SourceUpdater
    normalization   -> path_normalizer.PathNormalizer
    validation      -> source_validator.SourceValidator
    logging/reports -> audit_logger / report_exporter / project_health
"""

import html
import os
from datetime import datetime
from typing import List, Optional

from qgis.core import QgsApplication, QgsProject, QgsSettings
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QSize, QTimer, QUrl, Qt
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QDialog, QFileDialog, QHeaderView, QMenu,
    QMessageBox, QProgressDialog, QTableWidgetItem, QToolButton,
)

from .audit_logger import AuditLogger
from .dialogs import (
    SCOPE_ALL, SCOPE_BROKEN, ChangePreviewDialog, NormalizeDialog,
    RelocationDialog, ReportDialog, last_dir, remember_dir,
)
from ...branding import icon as toolbox_icon
from .layer_inspector import LayerInspector, active_map_layer_ids, summarize
from .path_normalizer import MODE_CLEAN, MODE_TO_RELATIVE, PathNormalizer
from .path_repair import (
    FileIndex, PathMatcher, SessionHistory, SourceUpdater, make_proposal, set_proposal_path,
)
from .project_health import build_health_report, build_migration_report
from .report_exporter import build_dashboard, export_audit
from .source_validator import VALID_FAIL, SourceValidator
from .table_models import LayerFilterProxy, LayerTableModel, MiddleElideDelegate
from .utils import (
    PLUGIN_NAME, STATUS_BROKEN, STATUS_REMOTE, STATUS_UNSUPPORTED, STATUS_VALID,
    BackgroundJob, ChangeProposal, LayerRecord, human_size, logger,
    project_uses_relative_paths, theme_icon,
)

PLUGIN_DIR = os.path.dirname(__file__)
FORM_CLASS, _ = uic.loadUiType(os.path.join(PLUGIN_DIR, "ui", "main_dialog.ui"))

# Background scans are used above this many layers (or always for network paths).
SYNC_SCAN_LIMIT = 60
# Columns shown by default - the rest are one click away in "Columns".
DEFAULT_HIDDEN = ["path", "layer_type", "provider", "geometry", "path_storage", "source", "notes",
                  "source_kind", "layer_id"]
SETTINGS = "LayerPathManager/"

STYLE = """
#titleLabel { font-size: 13pt; font-weight: 600; }
#projectInfoLabel { color: palette(dark); }
QFrame#statusBanner { border-radius: 8px; border: 1px solid rgba(128,128,128,0.30);
                      background: rgba(128,128,128,0.06); }
QFrame#statusBanner[state="error"] { background: rgba(207,34,46,0.08); border-color: rgba(207,34,46,0.40); }
QFrame#statusBanner[state="warn"]  { background: rgba(212,140,10,0.10); border-color: rgba(212,140,10,0.45); }
QFrame#statusBanner[state="ok"]    { background: rgba(46,160,67,0.08); border-color: rgba(46,160,67,0.40); }
#bannerTitle { font-size: 11.5pt; font-weight: 600; }
#bannerText { color: palette(text); }
QPushButton#bannerAction { background: #1f6feb; color: white; border: none; border-radius: 5px;
                           padding: 7px 18px; font-weight: 600; }
QPushButton#bannerAction:hover { background: #1a5fcc; }
QPushButton#bannerAction:disabled { background: rgba(31,111,235,0.4); }
QPushButton#bannerSecondary { padding: 6px 12px; }
QToolButton#btnShowAll, QToolButton#btnShowProblems { padding: 3px 10px; }
#selectionLabel { color: palette(mid); }
"""

LEVEL_COLORS = {"info": None, "success": "#2ea043", "warning": "#d48c0a", "error": "#cf222e"}


class LayerPathManagerDialog(QDialog, FORM_CLASS):
    """Main, non-modal window of the plugin."""

    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.project = QgsProject.instance()
        self.inspector = LayerInspector(self.project)
        self.history = SessionHistory()
        self.updater = SourceUpdater(self.project, self.history)
        self.normalizer = PathNormalizer(self.project, self.history)
        self.validator = SourceValidator(self.project)
        self.audit = AuditLogger(self)
        self.records: List[LayerRecord] = []
        self.job: Optional[BackgroundJob] = None
        self._scan_callbacks = []
        self._rescan_pending = False
        self._busy = False
        self._scanned_once = False
        self._last_repaired = 0     # layers fixed by the last operation (for the banner)

        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setStyleSheet(STYLE)
        self.iconLabel.hide()
        self.titleLabel.hide()
        self.setWindowIcon(toolbox_icon("data_management/layerPathManager.png"))

        self._setup_table()
        self._setup_toolbar()
        self._setup_menus()
        self._setup_filters()
        self._setup_audit_table()
        self._setup_details()
        self._setup_folder_panel()
        # banner text takes the free width (no spacer squeezing it)
        self.bannerLayout.setStretch(1, 1)
        self.progressBar.setVisible(False)
        self.btnCancel.setVisible(False)

        self.audit.messageAdded.connect(self._append_activity)
        self.audit.entryAdded.connect(self._append_audit_row)

        # debounce project changes
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(600)
        self._refresh_timer.timeout.connect(lambda: self.start_scan(auto=True))
        self._connect_project_signals()
        self._update_header()
        self._update_banner()
        self._update_history_label()

    # ==================================================================
    # Setup
    # ==================================================================
    def _setup_table(self):
        self.model = LayerTableModel(self)
        self.proxy = LayerFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        tv = self.layerTable
        tv.setModel(self.proxy)
        tv.verticalHeader().setVisible(False)
        tv.verticalHeader().setDefaultSectionSize(26)
        tv.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._path_delegate = MiddleElideDelegate(tv)  # keep file names visible
        for key in ("path", "source", "folder"):
            tv.setItemDelegateForColumn(LayerTableModel.COL[key], self._path_delegate)
        tv.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tv.customContextMenuRequested.connect(self._table_context_menu)
        tv.doubleClicked.connect(self._table_double_clicked)
        hh = tv.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setHighlightSections(False)
        widths = {"name": 200, "status": 100, "file_name": 190, "folder": 330, "layer_type": 90, "provider": 90, "geometry": 160,
                  "file_format": 120, "path_storage": 90, "path": 380, "error": 280,
                  "source": 300, "notes": 220, "source_kind": 120, "layer_id": 200}
        for key, w in widths.items():
            tv.setColumnWidth(LayerTableModel.COL[key], w)
        hidden = QgsSettings().value(SETTINGS + "hiddenColumnsV3", DEFAULT_HIDDEN)
        if isinstance(hidden, str):
            hidden = [hidden]
        for key in hidden or []:
            if key in LayerTableModel.COL:
                tv.setColumnHidden(LayerTableModel.COL[key], True)
        tv.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.model.checkedChanged.connect(self._update_selection_label)
        tv.selectionModel().selectionChanged.connect(lambda *_: self._update_selection_label())

    def _tool(self, button, text, icon, tip, primary=False):
        button.setText(text)
        button.setIcon(theme_icon(icon))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tip)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setAutoRaise(False)
        button.setMinimumHeight(30)
        # never squeeze the label ("Data ...Moved"); the search box gives way instead
        from qgis.PyQt.QtWidgets import QSizePolicy
        button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def _setup_toolbar(self):
        self._tool(self.btnScan, "Rescan", "mActionRefresh.svg",
                   "Check every layer's data source again")
        # Three plain actions, named by what the user wants to do:
        #   Find Missing Data  - only shown when layers lost their files
        #   Replace File       - one layer should use another file
        #   Copy / Move To     - put the selected layers' data in a folder
        self._tool(self.btnRepair, "Find Missing Data", "mIconWarning.svg",
                   "Some layers lost their files.\nShow the folder where the data is now and they "
                   "are reconnected.")
        self._tool(self.btnEdit, "Replace File...", "mActionFileOpen.svg",
                   "Make the selected layer use a different file (e.g. a newer version).\n"
                   "Tip: double-clicking a layer does the same.")
        self._tool(self.btnRelocate, "Data Was Moved", "mIconFolderOpen.svg",
                   "Point all layers to a folder that already contains their data.")
        self.btnRelocate.setVisible(False)   # advanced: available in More
        self._tool(self.btnCopy, "Copy / Move To...", "mActionFileSaveAs.svg",
                   "Put the selected layers' data into one folder.\n"
                   "Select layers (or none = all), choose a folder, choose Copy or Move - done.")
        self._tool(self.btnUndo, "Undo", "mActionUndo.svg",
                   "Put layers back to the source they had before this session")
        self._tool(self.btnMore, "More", "mActionOptions.svg",
                   "Reports, path format, validation and settings")
        for b in (self.btnUndo, self.btnMore, self.btnColumns, self.btnDetails):
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        self.btnScan.clicked.connect(lambda: self.start_scan())
        self.btnRepair.clicked.connect(lambda: self.relocate(SCOPE_BROKEN))
        self.btnEdit.clicked.connect(lambda: self.edit_selected_paths())
        self.btnRelocate.clicked.connect(lambda: self.relocate(SCOPE_ALL))
        self.btnCopy.clicked.connect(lambda: self.copy_data())
        self.btnCancel.clicked.connect(self.cancel_job)
        self.btnClose.clicked.connect(self.close)
        self.bannerAction.clicked.connect(self._banner_action)
        self.bannerSecondary.clicked.connect(self._banner_secondary)

    def _setup_menus(self):
        # ---- Undo
        undo = QMenu(self)
        self.actUndoChecked = undo.addAction(theme_icon("mActionUndo.svg"),
                                             "Undo changes of checked layers", self.restore_checked)
        self.actUndoAll = undo.addAction(theme_icon("mActionHistory.svg"),
                                         "Undo all changes made this session", self.restore_all)
        undo.addSeparator()
        self.actUndoInfo = undo.addAction("")
        self.actUndoInfo.setEnabled(False)
        self.btnUndo.setMenu(undo)

        # ---- More
        more = QMenu(self)
        more.addSection("Check & standardize")
        more.addAction(theme_icon("mIconSuccess.svg"), "Check that sources open...",
                       self.validate_sources)
        more.addAction(theme_icon("mActionOptions.svg"), "Relative / absolute paths...",
                       self.normalize_paths)
        more.addSection("Advanced")
        more.addAction(theme_icon("mIconFolderOpen.svg"),
                       "Reconnect all layers to a folder that already has the data...",
                       lambda: self.relocate(SCOPE_ALL))
        more.addSection("Reports")
        more.addAction(theme_icon("mActionNewReport.svg"), "Project health report",
                       self.show_health_report)
        more.addAction(theme_icon("mActionShowAllLayers.svg"), "Prepare project for moving...",
                       self.show_migration_report)
        more.addAction(theme_icon("mActionFileSave.svg"), "Export change log (CSV/TXT/JSON)...",
                       self.export_report)
        more.addSection("Select")
        more.addAction(theme_icon("mActionSelectAll.svg"), "Check all visible layers",
                       self.check_all_visible)
        more.addAction(theme_icon("mIconWarning.svg"), "Check broken layers",
                       lambda: self.model.set_checked(
                           [r.layer_id for r in self.records if r.status == STATUS_BROKEN], True))
        more.addAction(theme_icon("mActionDeselectAll.svg"), "Uncheck all",
                       lambda: self.model.set_checked([r.layer_id for r in self.model.records], False))
        more.addSection("Settings")
        self.actSelectedOnly = more.addAction("Scan only layers selected in the Layers panel")
        self.actSelectedOnly.setCheckable(True)
        self.actSelectedOnly.toggled.connect(lambda _v: self.start_scan(auto=True))
        self.actAutoRefresh = more.addAction("Rescan automatically when the project changes")
        self.actAutoRefresh.setCheckable(True)
        self.actAutoRefresh.setChecked(QgsSettings().value(SETTINGS + "autoRefresh", True, type=bool))
        self.actAutoRefresh.toggled.connect(lambda v: QgsSettings().setValue(SETTINGS + "autoRefresh", v))
        more.addSeparator()
        more.addAction(theme_icon("mActionHelpContents.svg"), "Help", self.show_help)
        self.btnMore.setMenu(more)

        # ---- Columns
        self.btnColumns.setIcon(theme_icon("mActionOptions.svg"))
        self.btnColumns.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btnColumns.clicked.connect(lambda: self._columns_menu(self.btnColumns))

    def _setup_filters(self):
        # All / Problems segmented switch
        self.viewGroup = QButtonGroup(self)
        for b in (self.btnShowAll, self.btnShowProblems):
            b.setCheckable(True)
            b.setAutoRaise(False)
            self.viewGroup.addButton(b)
        self.btnShowAll.setChecked(True)
        self.btnShowAll.clicked.connect(self._show_all)
        self.btnShowProblems.clicked.connect(lambda: self._filter(problems=True))
        # advanced filters, hidden by default
        self.btnFilters.setCheckable(True)
        self.btnFilters.setIcon(theme_icon("mActionFilter2.svg"))
        self.btnFilters.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btnFilters.setToolTip("More filters: status, layer type, provider and visible columns")
        self.btnFilters.toggled.connect(self.filterPanel.setVisible)
        self.btnFilters.clicked.connect(lambda: None)
        self.filterPanel.setVisible(False)
        self.scopeCombo.setVisible(False)   # replaced by the "Scan only selected" setting

        self.statusFilter.addItem("Any status", "")
        for s in (STATUS_VALID, STATUS_BROKEN, STATUS_REMOTE, STATUS_UNSUPPORTED):
            self.statusFilter.addItem(s, s)
        self.typeFilter.addItem("Any layer type", "")
        for t in ("Vector", "Raster", "Other"):
            self.typeFilter.addItem(t, t)
        self.providerFilter.addItem("Any provider", "")
        self.searchEdit.textChanged.connect(lambda t: self._filter(text=t))
        self.statusFilter.currentIndexChanged.connect(
            lambda: self._filter(status=self.statusFilter.currentData()))
        self.typeFilter.currentIndexChanged.connect(
            lambda: self._filter(layer_type=self.typeFilter.currentData()))
        self.providerFilter.currentIndexChanged.connect(
            lambda: self._filter(provider=self.providerFilter.currentData() or ""))

    def _show_all(self):
        self.folderTable.clearSelection()
        self._filter(problems=False, folder=None)

    def _setup_details(self):
        self.btnDetails.setCheckable(True)
        self.btnDetails.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        self.btnDetails.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btnDetails.setArrowType(Qt.ArrowType.RightArrow)
        self.btnDetails.setToolTip("Show activity messages, the change log and the session summary")
        self.btnDetails.toggled.connect(self._toggle_details)
        self.btnDetails.clicked.connect(lambda: None)
        self.logTabs.setVisible(False)
        clear = QToolButton(self.logTabs)
        clear.setText("Clear")
        clear.setIcon(theme_icon("mActionRemove.svg"))
        clear.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        clear.setAutoRaise(True)
        clear.setToolTip("Clear the activity messages (the change log is kept for reports)")
        clear.clicked.connect(self.clear_log)
        self.btnClearLog = clear
        self.logTabs.setCornerWidget(clear, Qt.Corner.TopRightCorner)
        self.verticalSplitter.setSizes([480, 170])

    def _toggle_details(self, show):
        self.logTabs.setVisible(show)
        self.btnDetails.setArrowType(Qt.ArrowType.DownArrow if show else Qt.ArrowType.RightArrow)

    def _setup_audit_table(self):
        cols = ["Time", "Operation", "Layer", "Result", "Before", "After",
                "Original Source", "New Source", "Details"]
        t = self.auditTable
        t.setColumnCount(len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate((70, 140, 150, 70, 60, 60, 260, 260)):
            t.setColumnWidth(i, w)

    def _connect_project_signals(self):
        p = self.project
        self._project_signals = [
            (p.layersAdded, self._project_changed), (p.layersRemoved, self._project_changed),
            (p.cleared, self._project_cleared), (p.readProject, self._project_changed),
            (p.fileNameChanged, self._update_header), (p.projectSaved, self._project_saved),
        ]
        for sig, slot in self._project_signals:
            sig.connect(slot)
        try:  # switching map tabs lists the layers of the new map
            from ..map_tabs import add_listener
            add_listener(self._map_changed)
            self._map_listener = self._map_changed
        except Exception:
            self._map_listener = None

    def cleanup(self):
        """Disconnect from QgsProject and stop background work (plugin unload)."""
        self.cancel_job()
        self._refresh_timer.stop()
        for sig, slot in getattr(self, "_project_signals", []):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._project_signals = []
        if getattr(self, "_map_listener", None) is not None:
            try:
                from ..map_tabs import remove_listener
                remove_listener(self._map_listener)
            except Exception:  # Map Tabs module unavailable
                self._map_listener = None
            self._map_listener = None

    # ==================================================================
    # Project change handling
    # ==================================================================
    def _project_changed(self, *args):
        self._update_header()
        if self.isVisible() and self.actAutoRefresh.isChecked() and not self._busy:
            self._refresh_timer.start()

    def _map_changed(self):
        """The active map changed: list the layers of that map."""
        self._update_header()
        if self.isVisible() and not self._busy:
            self._refresh_timer.start()

    def _project_saved(self, *args):
        self._last_repaired = 0
        self._update_header()
        self._update_banner()

    def _project_cleared(self):
        self.cancel_job()
        self.history.clear()
        self.records = []
        self._last_repaired = 0
        self._scanned_once = False
        self.model.set_records([], keep_checks=False)
        self._update_dashboard()
        self._update_history_label()
        self._update_header()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.records and (self.project.mapLayers() or active_map_layer_ids()):
            QTimer.singleShot(150, lambda: self.start_scan(auto=True))

    def closeEvent(self, event):
        self.cancel_job()
        super().closeEvent(event)

    # ==================================================================
    # Header, banner, labels
    # ==================================================================
    def _update_header(self, *args):
        p = self.project
        only = active_map_layer_ids()
        n = len(p.mapLayers()) if only is None else len(only)
        if p.fileName():
            mode = "relative" if project_uses_relative_paths(p) else "absolute"
            name = os.path.basename(p.fileName())
            dirty = " (unsaved changes)" if p.isDirty() else ""
            self._set_project_info(f"{name}{dirty}", f"{n} layer(s)  ·  paths saved as {mode}")
            self.projectInfoLabel.setToolTip(p.fileName())
        else:
            self._set_project_info("Unsaved project", f"{n} layer(s)")
            self.projectInfoLabel.setToolTip("")

    def _set_project_info(self, title, detail):
        """The project name large and bold, the details a little smaller below it."""
        self.projectInfoLabel.setTextFormat(Qt.TextFormat.RichText)
        self.projectInfoLabel.setText(
            f'<span style="font-size:15pt; font-weight:600;">{html.escape(title)}</span>'
            f'<br><span style="font-size:11pt;">{html.escape(detail)}</span>')

    def _set_banner(self, state, icon, title, text, action=None, secondary=None):
        """state: ok / warn / error / info. action/secondary: (label, key) or None."""
        self.statusBanner.setProperty("state", state)
        self.statusBanner.style().unpolish(self.statusBanner)
        self.statusBanner.style().polish(self.statusBanner)
        self.bannerIcon.setPixmap(theme_icon(icon).pixmap(28, 28))
        self.bannerTitle.setText(title)
        self.bannerText.setText(text)
        self.bannerText.setVisible(bool(text))
        for btn, spec in ((self.bannerAction, action), (self.bannerSecondary, secondary)):
            btn.setVisible(spec is not None)
            if spec is not None:
                btn.setText(spec[0])
                btn.setProperty("key", spec[1])

    def _update_banner(self):
        s = summarize(self.records)
        broken_file = s["broken_file"]
        broken_other = s["broken"] - broken_file
        pending = sum(1 for r in self.records if r.status == "Not checked")
        if self._busy and not self._scanned_once:
            self._set_banner("info", "mActionRefresh.svg", "Checking layers...",
                             "Looking at the data source of every layer.")
        elif not self.records and not (self.project.mapLayers() if active_map_layer_ids() is None
                                       else active_map_layer_ids()):
            self._set_banner("info", "mIconInfo.svg", "This project has no layers",
                             "Add layers or open a project, then press Rescan.")
        elif not self._scanned_once:
            self._set_banner("info", "mIconInfo.svg", "Not checked yet", "",
                             ("Check Layers", "scan"))
        elif broken_file:
            extra = (f" {broken_other} database/web layer(s) also can't connect."
                     if broken_other else "")
            self._set_banner(
                "error", "mIconWarning.svg",
                f"{broken_file} layer(s) can't find their data",
                "The files were moved, renamed or deleted. Show where the data is now and the "
                "layers will be reconnected." + extra,
                ("Find Missing Data...", "fix"), ("Show Them", "problems"))
        elif self._last_repaired and self.project.isDirty():
            done = "now use the copied data" if getattr(self, "_last_operation", "") == \
                "Switch to copied data" else "reconnected"
            self._set_banner(
                "ok", "mIconSuccess.svg", f"{self._last_repaired} layer(s) {done}",
                "Save the project to keep the new paths. You can still undo the change.",
                ("Save Project", "save"), ("Undo", "undo"))
        elif broken_other:
            self._set_banner(
                "warn", "mIconWarning.svg",
                f"{broken_other} database or web layer(s) can't connect",
                "All file-based layers are fine. Fix these connections in Layer Properties "
                "› Source (check host, login and network).", None, ("Show Them", "problems"))
        elif pending:
            self._set_banner("warn", "mIconWarning.svg", "The check was stopped early",
                             f"{pending} layer(s) were not checked.", ("Check Again", "scan"))
        else:
            self._set_banner(
                "ok", "mIconSuccess.svg", f"All {s['total']} layers are connected",
                "Nothing to fix. " + self._folder_sentence() + " To put data in one folder: "
                "select layers \u2192 Copy / Move To...", None,
                None if self.btnFolders.isChecked() else ("Show Folders", "folders"))

    def _banner_action(self):
        key = self.bannerAction.property("key")
        if key == "fix":
            self.relocate(SCOPE_BROKEN)
        elif key == "scan":
            self.start_scan()
        elif key == "save":
            if self.iface is not None and hasattr(self.iface, "actionSaveProject"):
                self.iface.actionSaveProject().trigger()
            elif self.project.fileName():
                self.project.write()
            else:
                QMessageBox.information(self, PLUGIN_NAME, "Use Project › Save As to save "
                                        "this new project first.")
            self._project_saved()

    def _banner_secondary(self):
        key = self.bannerSecondary.property("key")
        if key == "problems":
            self.btnShowProblems.setChecked(True)
            self._filter(problems=True)
        elif key == "undo":
            self.restore_all()
        elif key == "folders":
            self.btnFolders.setChecked(True)

    # ==================================================================
    # Folders summary
    # ==================================================================
    def _setup_folder_panel(self):
        from qgis.PyQt.QtWidgets import QLabel, QTableWidget, QVBoxLayout, QWidget
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.folderSummary = QLabel()
        lay.addWidget(self.folderSummary)
        t = QTableWidget(0, 5)
        t.setHorizontalHeaderLabels(["Folder", "Layers", "OK", "Problems", "Size"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.verticalHeader().setDefaultSectionSize(24)
        t.setItemDelegateForColumn(0, MiddleElideDelegate(t))
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(self._folder_context_menu)
        t.itemSelectionChanged.connect(self._folder_selected)
        t.cellDoubleClicked.connect(lambda row, _c: self._check_folder(row))
        lay.addWidget(t)
        tip = QLabel("Click a folder to list its layers \u00b7 double-click to tick them "
                     "\u00b7 right-click for Copy / Move")
        tip.setStyleSheet("color: palette(mid); font-size: 8pt;")
        lay.addWidget(tip)
        self.folderTable, self.folderPanel = t, panel
        self.verticalSplitter.insertWidget(0, panel)
        self.verticalSplitter.setStretchFactor(0, 0)
        self.verticalSplitter.setStretchFactor(1, 1)
        self.verticalSplitter.setSizes([170, 430, 170])
        self.btnFolders.setCheckable(True)
        self.btnFolders.setIcon(theme_icon("mIconFolderOpen.svg"))
        self.btnFolders.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btnFolders.setToolTip("Summary of the folders your layers' data is stored in")
        self.btnFolders.toggled.connect(self._toggle_folders)
        self.btnFolders.clicked.connect(lambda: None)
        show = QgsSettings().value(SETTINGS + "showFolders", True, type=bool)
        self.btnFolders.setChecked(show)
        panel.setVisible(show)

    def _toggle_folders(self, show):
        self.folderPanel.setVisible(show)
        QgsSettings().setValue(SETTINGS + "showFolders", show)
        self._update_banner()
        if not show:
            self.folderTable.clearSelection()

    def _folder_groups(self):
        groups = {}
        for r in self.records:
            groups.setdefault(r.folder, []).append(r)
        return groups

    def _folder_sentence(self) -> str:
        n = len([f for f in self._folder_groups() if f])
        return f"The data is stored in {n} folder(s)." if n else ""

    def _update_folder_panel(self):
        from qgis.PyQt.QtGui import QColor
        groups = self._folder_groups()
        files = {k: v for k, v in groups.items() if k}
        t = self.folderTable
        current = self.proxy.folder
        t.blockSignals(True)
        t.setRowCount(0)
        rows = sorted(files.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
        if "" in groups:
            rows.append(("", groups[""]))
        for folder, recs in rows:
            row = t.rowCount()
            t.insertRow(row)
            ok = sum(1 for r in recs if r.status == STATUS_VALID)
            bad = sum(1 for r in recs if r.status in (STATUS_BROKEN, "Not checked"))
            size = sum({r.path: r.size_bytes for r in recs}.values())
            if folder:
                parent = os.path.dirname(folder)
                label = f"{os.path.basename(folder) or folder}   \u2014   {parent}"
            else:
                label = "(database, web and memory layers - no folder)"
            vals = (label, str(len(recs)), str(ok) if folder else "-",
                    str(bad) if bad else "-", human_size(size) if size else "-")
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.ItemDataRole.UserRole, folder)
                tip = folder or "Layers without a local folder"
                names = ", ".join(r.name for r in recs[:12]) + (" ..." if len(recs) > 12 else "")
                it.setToolTip(f"{tip}\n{len(recs)} layer(s): {names}")
                if c == 3 and bad:
                    it.setForeground(QColor("#cf222e"))
                if c == 0 and not folder:
                    it.setForeground(QColor("#808080"))
                t.setItem(row, c, it)
            if current is not None and folder == current:
                t.selectRow(row)
        t.blockSignals(False)
        n_layers = sum(len(v) for v in files.values())
        if files:
            biggest = rows[0]
            self.folderSummary.setText(
                f"<b>{n_layers}</b> file layer(s) in <b>{len(files)}</b> folder(s) &middot; most in "
                f"<i>{html.escape(os.path.basename(biggest[0]) or biggest[0])}</i> "
                f"({len(biggest[1])})")
        else:
            self.folderSummary.setText("No file-based layers.")
        self.btnFolders.setText(f"Folders {len(files)}")

    def _folder_selected(self):
        items = self.folderTable.selectedItems()
        folder = items[0].data(Qt.ItemDataRole.UserRole) if items else None
        self._filter(folder=folder)
        if folder is not None:
            self.statusLabel.setText(f"Showing layers in {folder or 'no folder'} "
                                     "(click the folder again or press All to show every layer)")

    def _check_folder(self, row):
        folder = self.folderTable.item(row, 0).data(Qt.ItemDataRole.UserRole)
        ids = [r.layer_id for r in self.records if r.folder == folder]
        self.model.set_checked([r.layer_id for r in self.model.records], False)
        self.model.set_checked(ids, True)

    def _folder_context_menu(self, pos):
        item = self.folderTable.itemAt(pos)
        if item is None:
            return
        folder = item.data(Qt.ItemDataRole.UserRole)
        recs = [r for r in self.records if r.folder == folder and r.is_file_based]
        menu = QMenu(self)
        ticked = [r for r in self.model.checked_records() if r.is_file_based and r.folder != folder]
        if folder and ticked:
            menu.addAction(theme_icon("mActionFileSaveAs.svg"),
                           f"Put the {len(ticked)} ticked layer(s) into this folder...",
                           lambda: self.copy_data(target=folder, records=ticked))
            menu.addSeparator()
        if recs:
            menu.addAction(theme_icon("mActionFileSaveAs.svg"),
                           f"Copy / Move these {len(recs)} layer(s) to...",
                           lambda: self.copy_data(records=recs))
            menu.addAction(theme_icon("mActionSelectAll.svg"), "Tick these layers",
                           lambda: self._check_folder(self.folderTable.row(item)))
            act = menu.addAction(theme_icon("mIconFolderOpen.svg"), "Open folder",
                                 lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            act.setEnabled(os.path.isdir(folder))
            menu.addAction(theme_icon("mActionEditCopy.svg"), "Copy folder path",
                           lambda: QGuiApplication.clipboard().setText(folder))
        if not menu.isEmpty():
            menu.exec(self.folderTable.viewport().mapToGlobal(pos))

    def _update_dashboard(self):
        s = summarize(self.records)
        counts = self.audit.counts()
        problems = s["broken"] + sum(1 for r in self.records if r.status == "Not checked")
        self.btnShowAll.setText(f"All {s['total']}")
        self.btnShowProblems.setText(f"Problems {problems}")
        self.btnRepair.setText(f"Find Missing Data ({s['broken_file']})")
        self.btnRepair.setVisible(bool(s["broken_file"]))     # only when there is something to find
        self.btnRepair.setEnabled(not self._busy)
        dash = build_dashboard(s, counts)
        rows = "".join(f"<tr><td>{k.replace('_', ' ').capitalize()}</td><td align='right'>"
                       f"<b>{v}</b></td></tr>" for k, v in dash.items())
        self.summaryText.setHtml(
            f"<p><b>Session summary</b> &mdash; updated {datetime.now():%H:%M:%S}</p>"
            f"<table cellpadding='3'>{rows}</table>"
            f"<p>Paths: {s['relative']} relative / {s['absolute']} absolute. "
            f"Layers that can be undone: <b>{len(self.history.changed_layer_ids())}</b></p>")
        current = self.providerFilter.currentData()
        providers = sorted({r.provider for r in self.records if r.provider})
        self.providerFilter.blockSignals(True)
        self.providerFilter.clear()
        self.providerFilter.addItem("Any provider", "")
        for pr in providers:
            self.providerFilter.addItem(pr, pr)
        idx = self.providerFilter.findData(current)
        self.providerFilter.setCurrentIndex(max(0, idx))
        self.providerFilter.blockSignals(False)
        self._update_folder_panel()
        self._update_selection_label()
        self._update_banner()

    def _update_selection_label(self):
        checked = len(self.model.checked)
        selected = len(self.layerTable.selectionModel().selectedRows()) \
            if self.layerTable.selectionModel() else 0
        bits = []
        if checked:
            bits.append(f"{checked} checked")
        if selected > 1:
            bits.append(f"{selected} selected")
        shown = self.proxy.rowCount()
        if shown != len(self.model.records):
            bits.append(f"showing {shown} of {len(self.model.records)}")
        self.selectionLabel.setText("  ·  ".join(bits))

    def _update_history_label(self):
        n = len(self.history.changed_layer_ids())
        settings = sum(1 for s in self.history.settings if not s.restored)
        self.btnUndo.setVisible(bool(n or settings))
        self.btnUndo.setText(f"Undo ({n})" if n else "Undo")
        self.actUndoChecked.setEnabled(bool(n))
        self.actUndoInfo.setText(
            f"{n} layer(s) can be undone until QGIS closes" +
            (", plus the path-format change" if settings else ""))

    def _filter(self, **kw):
        self.proxy.set_filters(**kw)
        self._update_selection_label()

    # ==================================================================
    # Activity / audit panels
    # ==================================================================
    def _append_activity(self, msg):
        color = LEVEL_COLORS.get(msg.level)
        text = msg.text if color is None else f"<span style='color:{color}'>{msg.text}</span>"
        details = (f"<br><span style='color:palette(mid)'>&nbsp;&nbsp;&nbsp;"
                   f"{html.escape(msg.details)}</span>" if msg.details else "")
        self.activityText.append(f"<span style='color:palette(mid)'>{msg.timestamp:%H:%M:%S}</span>"
                                 f"&nbsp; {text}{details}")
        if not self._busy:
            import re
            self.statusLabel.setText(re.sub(r"<[^>]+>", "", msg.text))

    def _append_audit_row(self, e):
        from qgis.PyQt.QtGui import QColor
        t = self.auditTable
        row = t.rowCount()
        t.insertRow(row)
        vals = (e.timestamp[11:], e.operation, e.layer_name, e.result, e.previous_status,
                e.updated_status, e.original_source, e.new_source, e.message)
        for col, v in enumerate(vals):
            it = QTableWidgetItem(v)
            it.setToolTip(v)
            if col == 3:
                it.setForeground(QColor({"Success": "#2ea043", "Failed": "#cf222e"}.get(v, "#d48c0a")))
            t.setItem(row, col, it)
        t.scrollToBottom()

    def clear_log(self):
        self.activityText.clear()
        self.audit.clear_messages()

    # ==================================================================
    # Busy state / background jobs
    # ==================================================================
    def _set_busy(self, busy, text=""):
        self._busy = busy
        for b in (self.btnScan, self.btnRepair, self.btnRelocate, self.btnCopy, self.btnEdit,
                  self.btnUndo, self.btnMore, self.bannerAction):
            b.setEnabled(not busy)
        self.progressBar.setVisible(busy)
        self.btnCancel.setVisible(busy)
        self.progressBar.setValue(0)
        self.statusLabel.setText(text or ("Working..." if busy else "Ready"))
        if not busy:
            self._update_history_label()
            self._update_dashboard()
        else:
            self._update_banner()

    def _run_job(self, description, func, on_done, *args):
        """Run ``func(feedback, *args)`` in the QGIS task manager."""
        job = BackgroundJob(description, func, *args)
        self.job = job
        job.progressChanged.connect(lambda v: self.progressBar.setValue(int(v)))
        job.jobFinished.connect(lambda ok, res, err: self._job_done(job, ok, res, err, on_done))
        self._set_busy(True, description + "...")
        QgsApplication.taskManager().addTask(job)

    def _job_done(self, job, ok, result, err, on_done):
        if self.job is job:
            self.job = None
        self._set_busy(False)
        try:
            on_done(ok, result, err)
        except Exception as exc:
            logger.exception("Job completion handler failed")
            self.audit.error("Unexpected error while finishing the operation", str(exc))

    def cancel_job(self):
        if self.job is not None:
            try:
                self.job.cancel()
            except RuntimeError:
                pass
            self.audit.warning("Cancelling...")

    # ==================================================================
    # Scan
    # ==================================================================
    def _layers_for_scope(self):
        if self.actSelectedOnly.isChecked() and self.iface is not None:
            view = self.iface.layerTreeView()
            return view.selectedLayersRecursive() if hasattr(view, "selectedLayersRecursive") \
                else view.selectedLayers()
        return None

    def start_scan(self, auto=False, on_done=None):
        if on_done is not None:
            self._scan_callbacks.append(on_done)
        if self.job is not None:
            self._rescan_pending = True
            return
        layers = self._layers_for_scope()
        if layers is not None and not layers:
            if not auto:
                QMessageBox.information(self, PLUGIN_NAME,
                                        "No layers are selected in the Layers panel.\n\n"
                                        "(More › Settings › Scan only selected layers is on.)")
            return
        records = self.inspector.collect_records(layers)
        # Network shares can block on existence checks: always scan those in the background.
        on_network = any(r.path.startswith(("//", "\\\\")) for r in records if r.is_file_based)
        if len(records) <= SYNC_SCAN_LIMIT and not on_network:
            LayerInspector.check_filesystem(None, records)
            self._scan_finished(records, True, "", auto)
        else:
            self._run_job("Checking layer sources", LayerInspector.check_filesystem,
                          lambda ok, res, err: self._scan_finished(records, ok, err, auto), records)

    def _scan_finished(self, records, ok, err, auto):
        if not ok:
            for r in records:
                if r.is_file_based and r.exists is None:
                    r.status = "Not checked"
                    r.error = "Scan cancelled before this layer was checked"
            self.audit.warning(f"Check stopped: {err}")
        repaired = self.audit.repaired_layer_ids()
        for r in records:
            r.repaired = r.layer_id in repaired and r.status == STATUS_VALID
        self._scanned_once = True
        self.records = records
        self.model.set_records(records)
        self.proxy.sort(self.proxy.sortColumn(), self.layerTable.horizontalHeader().sortIndicatorOrder())
        s = summarize(records)
        self.audit.scan_stats = s
        self._update_dashboard()
        self._update_header()
        if ok:
            msg = (f"Checked {s['total']} layer(s): {s['valid']} OK, {s['broken']} broken, "
                   f"{s['remote']} web, {s['unsupported']} other")
            if auto:
                self.statusLabel.setText(msg)
            else:
                (self.audit.warning if s["broken"] else self.audit.success)(msg)
        callbacks, self._scan_callbacks = self._scan_callbacks, []
        for cb in callbacks:
            cb()
        if self._rescan_pending:
            self._rescan_pending = False
            QTimer.singleShot(0, lambda: self.start_scan(auto=True))

    # ==================================================================
    # Selection helpers
    # ==================================================================
    def check_all_visible(self):
        ids = [self.proxy.data(self.proxy.index(r, 0), Qt.ItemDataRole.UserRole + 2).layer_id
               for r in range(self.proxy.rowCount())]
        self.model.set_checked(ids, True)

    def _current_record(self) -> Optional[LayerRecord]:
        idx = self.layerTable.currentIndex()
        if not idx.isValid():
            return None
        return self.model.record(self.proxy.mapToSource(idx).row())

    def _selected_records(self) -> List[LayerRecord]:
        rows = {self.proxy.mapToSource(i).row()
                for i in self.layerTable.selectionModel().selectedRows()}
        return [self.model.record(r) for r in sorted(rows)]

    def _checked_file_records(self) -> List[LayerRecord]:
        return [r for r in self.model.checked_records() if r.is_file_based]

    def _targets(self, require_file=True) -> List[LayerRecord]:
        """Checked layers, else selected rows, else the current row."""
        recs = self.model.checked_records() or self._selected_records()
        if not recs and self._current_record():
            recs = [self._current_record()]
        return [r for r in recs if r.is_file_based] if require_file else recs

    def _ensure_scanned(self) -> bool:
        if not self.records:
            self.start_scan()
        if not self.records:
            QMessageBox.information(self, PLUGIN_NAME, "The project has no layers to process.")
            return False
        return True

    # ==================================================================
    # Repair & relocate
    # ==================================================================
    def relocate(self, default_scope):
        if not self._ensure_scanned():
            return
        if default_scope == SCOPE_BROKEN and not any(
                r.is_file_based and r.status == STATUS_BROKEN for r in self.records):
            others = sum(1 for r in self.records if r.status == STATUS_BROKEN)
            QMessageBox.information(
                self, PLUGIN_NAME,
                "There are no broken file-based layers." +
                (f"\n\n{others} broken database/remote layer(s) must be fixed through their "
                 "connection settings." if others else ""))
            return
        title = "Find Missing Data" if default_scope == SCOPE_BROKEN else "Reconnect to Another Folder"
        dlg = RelocationDialog(self.records, self.model.checked, title, default_scope, self)
        if not dlg.exec():
            return
        req = dlg.request()
        recs = dlg.selected_records()
        operation = "Repair broken path" if req.scope == SCOPE_BROKEN else "Relocate data source"
        proposals = [make_proposal(r, operation) for r in recs]
        if req.mode == "prefix":
            for p in proposals:
                PathMatcher.match_prefix(p, req.old_root, req.search_root)
            self._show_preview(proposals, title, operation,
                               f"Root folder <code>{html.escape(req.old_root)}</code> &rarr; "
                               f"<code>{html.escape(req.search_root)}</code>")
            return
        self.audit.info(f"Indexing {req.search_root} ...")

        def done(ok, index, err):
            if not ok or index is None:
                self.audit.warning(f"Folder search stopped: {err or 'cancelled'}")
                return
            for e in index.errors[:10]:
                self.audit.warning("Search warning", e)
            self.audit.info(f"Indexed {index.entry_count} file(s)/folder(s) in {req.search_root}")
            matcher = PathMatcher(index, req.options)
            for p in proposals:
                matcher.match(p)
            self._report_match_stats(proposals)
            if self._nothing_found(proposals, recs, req.search_root):
                return
            self._show_preview(proposals, title, operation,
                               f"Found in <code>{html.escape(req.search_root)}</code>. "
                               "Untick anything you don't want to change, then Apply.")

        self._run_job("Indexing search folder",
                      lambda fb, root, rec: FileIndex().build(fb, [root], rec), done,
                      req.search_root, req.recursive)

    def _nothing_found(self, proposals, recs, folder) -> bool:
        """Handle 'the chosen folder doesn't contain the data' with a helpful next step."""
        same = [p for p in proposals if p.match_type == "Same file as now"]
        found = [p for p in proposals
                 if (p.new_path and p.match_type != "Same file as now") or
                 (p.candidates and not p.new_path)]
        if found:
            return False
        if same and len(same) == len(proposals):
            QMessageBox.information(
                self, PLUGIN_NAME,
                f"The layers already use the files in\n{folder}\n\nNothing needs to change.")
            return True
        copyable = [r for r in recs if r.exists]
        if copyable:
            res = QMessageBox.question(
                self, PLUGIN_NAME,
                f"Your data isn't in this folder yet:\n{folder}\n\n"
                f"Do you want to COPY the data of {len(copyable)} layer(s) into this folder and "
                "switch the layers to the copies?\n\nThe original files stay where they are.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if res == QMessageBox.StandardButton.Yes:
                self.copy_data(target=folder, records=copyable)
            else:
                self.audit.info("No matching files in the chosen folder - nothing changed")
            return True
        QMessageBox.information(
            self, PLUGIN_NAME,
            f"None of the missing files were found in\n{folder}\nor its sub-folders.\n\n"
            "Choose the folder that contains them.")
        self.audit.warning(f"No matching files found in {html.escape(folder)}")
        return True

    # ==================================================================
    # Copy data to a new folder
    # ==================================================================
    def copy_data(self, target=None, records=None):
        """Copy or move the selected layers' data into one folder and switch the layers."""
        from .dialogs import CopyDataDialog
        from .data_copier import ST_CANCELLED, ST_COPIED, ST_FAILED, delete_originals, execute_plan
        if not self._ensure_scanned():
            return
        if not any(r.is_file_based for r in self.records):
            QMessageBox.information(self, PLUGIN_NAME, "The project has no file-based layers to copy.")
            return
        if records is None:
            # ticked layers, else rows the user selected; nothing -> all file layers
            picked = self.model.checked_records() or self._selected_records()
            records = [r for r in picked if r.is_file_based] or None
        dlg = CopyDataDialog(self.records, self.model.checked, self, target=target,
                             preset_records=records)
        if not dlg.exec():
            return
        plan, scope_recs = dlg.plan, dlg.selected_records()
        move = dlg.is_move()
        switch = move or dlg.chk_switch.isChecked()
        verb = "Moving" if move else "Copying"
        self.audit.info(f"{verb} {len(plan.to_copy)} dataset(s) ({human_size(plan.copy_bytes)}) to "
                        f"{html.escape(plan.target)} ...")

        for i in plan.items:
            if i.notice:
                (self.audit.warning if i.level == "warn" else self.audit.info)(
                    f"{html.escape(os.path.basename(i.src_path))}: {html.escape(i.notice)}")

        def done(ok, result, err):
            plan2 = result or plan
            cancelled = not ok or any(i.status == ST_CANCELLED for i in plan2.items)
            for i in plan2.items:
                res = {ST_COPIED: "Success", ST_FAILED: "Failed"}.get(i.status, "Skipped")
                self.audit.record("Copy dataset", ", ".join(i.layer_names), ",".join(i.layer_ids),
                                  i.src_path, i.dest_path, "-", "-", res,
                                  f"{i.status}: {i.message}")
                if i.status == ST_FAILED:
                    self.audit.error(f"Copy failed: {html.escape(os.path.basename(i.src_path))}",
                                     i.message)
            copied = sum(1 for i in plan2.items if i.status == ST_COPIED)
            if cancelled:
                self.audit.warning(f"Stopped. {copied} dataset(s) were copied completely; the layers "
                                   "were not switched and no original was deleted.")
                return
            self.audit.success(f"Copied {copied} dataset(s) to {html.escape(plan2.target)}.")
            if not switch:
                return
            norm = (lambda p: p.lower()) if os.name == "nt" else (lambda p: p)
            pmap = {norm(k): v for k, v in plan2.path_map().items()}
            props = []
            for r in scope_recs:
                if r.is_file_based and norm(r.path) in pmap:
                    p = make_proposal(r, "Move data" if move else "Switch to copied data")
                    set_proposal_path(p, pmap[norm(r.path)], "Copied data", 100)
                    if p.match_type == "Same file as now":
                        continue
                    self.validator.validate_proposal(p, self.project.mapLayer(r.layer_id))
                    p.accepted = p.validation_state in ("OK", "Warning")
                    props.append(p)
            results = self.apply_proposals(props, "Move data" if move else "Switch to copied data")
            if move:
                self._finish_move(plan2, results, delete_originals)

        self._run_job(f"{verb} data", execute_plan, done, plan)

    def _finish_move(self, plan, results, delete_originals):
        """Delete originals only for datasets whose every layer now opens from the copy."""
        from .data_copier import ST_COPIED, ST_REUSE
        ok_ids = {r.layer_id for r in results or [] if r.result == "Success" and r.new_valid}
        done = (ST_COPIED, ST_REUSE)      # REUSE = byte-identical copy was already there
        eligible = [i for i in plan.items if i.status in done and not i.keep_original
                    and all(lid in ok_ids for lid in i.layer_ids)]
        kept = [i for i in plan.items if i.status in done and i not in eligible]
        deleted, errors = delete_originals(eligible)
        moved_ids = {lid for i in eligible for lid in i.layer_ids
                     if not any(os.path.basename(i.src_path) in e for e in errors)}
        for i in eligible:
            self.audit.record("Delete original (move)", ", ".join(i.layer_names), ",".join(i.layer_ids),
                              i.src_path, "", "-", "-",
                              "Failed" if any(os.path.basename(i.src_path) in e for e in errors)
                              else "Success", "Original removed after verified copy")
        for e in errors:
            self.audit.error("Original not deleted", e)
        for i in kept:
            self.audit.warning(f"Original kept: {html.escape(os.path.basename(i.src_path))}",
                               i.message or "a layer did not switch to the copy")
        # a move can't be undone - the originals are gone
        self.history.entries = [e for e in self.history.entries if e.layer_id not in moved_ids]
        self._update_history_label()
        n_moved = len({lid for lid in moved_ids})
        msg = f"Moved {n_moved} layer(s) to {plan.target}"
        if kept or errors:
            msg += f"; {len(kept) + len(errors)} original(s) kept (see Details)"
        (self.audit.success if not errors else self.audit.warning)(html.escape(msg))
        if self.iface is not None:
            (self.iface.messageBar().pushSuccess if not errors else
             self.iface.messageBar().pushWarning)(PLUGIN_NAME, msg)
        self.start_scan(auto=True)

    def _report_match_stats(self, proposals):
        auto = sum(1 for p in proposals if p.accepted)
        amb = sum(1 for p in proposals if "Ambiguous" in p.match_type)
        none = sum(1 for p in proposals if not p.new_path and not p.candidates)
        review = sum(1 for p in proposals if p.new_path and not p.accepted)
        self.audit.info(f"Matching: {auto} confident, {review} to review, {amb} ambiguous, "
                        f"{none} without match (of {len(proposals)})")

    def _show_preview(self, proposals, title, operation, intro, auto_validate=True):
        if not proposals:
            QMessageBox.information(self, PLUGIN_NAME, "Nothing to process.")
            return
        dlg = ChangePreviewDialog(proposals, self.project, title, intro, self,
                                  auto_validate=auto_validate)
        if dlg.exec():
            self.apply_proposals(dlg.accepted_proposals(), operation)
        else:
            self.audit.info(f"{title}: cancelled - no changes made")

    # ==================================================================
    # 4. Edit selected paths
    # ==================================================================
    def edit_selected_paths(self, records=None):
        if not self._ensure_scanned():
            return
        recs = records if records is not None else self._targets()
        if not recs:
            QMessageBox.information(
                self, PLUGIN_NAME,
                "Check (or select) one or more file-based layers in the table first.\n\n"
                "Database, web-service and memory layers have no file path to edit.")
            return
        proposals = [make_proposal(r, "Edit data source") for r in recs]
        if len(proposals) == 1:
            from .dialogs import SingleChangeDialog, browse_for_proposal
            p = proposals[0]
            path = browse_for_proposal(self, p)
            if not path:
                return
            set_proposal_path(p, path)
            if p.match_type == "Same file as now":
                QMessageBox.information(
                    self, PLUGIN_NAME,
                    f"\u201c{p.layer_name}\u201d already uses this file, so nothing needs to change."
                    "\n\nTo use a different file, choose another one. If the whole data folder "
                    "is somewhere else, use Find Missing Data or Copy / Move To... instead.")
                return
            dlg = SingleChangeDialog(p, self.project, self)
            if dlg.exec() and p.accepted:
                self.apply_proposals([p], "Change file")
            return
        intro = (f"Choose new files for these {len(proposals)} layers: select row(s), then use "
                 "<b>Change</b> \u203a <i>Browse for a file</i>, or <i>Use one file for all selected "
                 "rows</i> (e.g. several layers of one GeoPackage).")
        self._show_preview(proposals, "Change Files", "Change file", intro)

    def _table_double_clicked(self, index):
        rec = self.model.record(self.proxy.mapToSource(index).row())
        if index.column() == 0:
            return
        if rec and rec.is_file_based:
            self.edit_selected_paths([rec])
        elif rec:
            self.audit.info(f"'{rec.name}' is a {rec.source_kind.lower()} layer - use Layer "
                            "Properties > Source to change it")

    # ==================================================================
    # Apply / restore
    # ==================================================================
    def apply_proposals(self, proposals: List[ChangeProposal], operation: str):
        if not proposals:
            return
        prog = QProgressDialog("Updating layer sources...", "Stop", 0, len(proposals), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(300)

        def progress(i, n):
            prog.setValue(i)
            prog.setLabelText(f"Updating {proposals[i].layer_name} ({i + 1}/{n})")
            QApplication.processEvents()
            return not prog.wasCanceled()

        self._busy = True
        try:
            results = self.updater.apply(proposals, operation, progress)
        except Exception as exc:
            logger.exception("Apply failed")
            self.audit.error("Unexpected error while updating sources", str(exc))
            results = []
        finally:
            prog.setValue(len(proposals))
            self._busy = False
        self._report_results(results, operation)
        return results

    def _report_results(self, results, operation):
        self.audit.record_results(results)
        ok = [r for r in results if r.result == "Success"]
        self._last_repaired = 0 if operation == "Restore" else \
            sum(1 for r in ok if r.new_valid)
        self._last_operation = operation
        failed = [r for r in results if r.result == "Failed"]
        skipped = [r for r in results if r.result == "Skipped"]
        for r in failed:
            self.audit.error(f"Failed: {html.escape(r.layer_name)}", r.message)
        for r in skipped:
            self.audit.warning(f"Skipped: {html.escape(r.layer_name)}", r.message)
        summary = f"{operation}: {len(ok)} updated, {len(failed)} failed, {len(skipped)} skipped"
        (self.audit.success if not failed else self.audit.warning)(summary)
        self._refresh_qgis(ok)
        if self.iface is not None:
            bar = self.iface.messageBar()
            if failed:
                bar.pushWarning(PLUGIN_NAME, summary + " - see the Activity panel")
            elif ok:
                bar.pushSuccess(PLUGIN_NAME, summary)
        self._update_history_label()
        self.start_scan(auto=True)

    def _refresh_qgis(self, results):
        if self.iface is None:
            return
        try:
            view = self.iface.layerTreeView()
            for r in results:
                if hasattr(view, "refreshLayerSymbology"):
                    view.refreshLayerSymbology(r.layer_id)
            self.iface.mapCanvas().refreshAllLayers()
        except Exception as exc:  # never fail an update because of a refresh
            logger.warning("Canvas refresh failed: %s", exc)

    def restore_checked(self):
        ids = [r.layer_id for r in self.model.checked_records()
               if self.history.first_for(r.layer_id) is not None]
        if not ids:
            QMessageBox.information(self, PLUGIN_NAME,
                                    "None of the checked layers has a change recorded this session.")
            return
        self._restore(ids, include_settings=False)

    def restore_all(self):
        self._restore(self.history.changed_layer_ids(), include_settings=True)

    def _restore(self, ids, include_settings):
        settings = [s for s in self.history.settings if not s.restored] if include_settings else []
        msg = f"Restore {len(ids)} layer(s) to the data source they had before this session?"
        if settings:
            msg += "\n\nThe project's path storage setting will also be restored."
        msg += "\n\nLayers that were removed from the project cannot be restored."
        if QMessageBox.question(self, "Restore", msg) != QMessageBox.StandardButton.Yes:
            return
        results = self.updater.restore(ids, to_original=True)
        for s in reversed(settings):
            self.normalizer.restore_storage_mode(s)
            self.audit.record("Restore path storage", "(project)", "", "", s.description,
                              "-", "-", "Success", "Restored to " +
                              ("relative" if s.old_relative else "absolute"), s.op_id)
        self._report_results(results, "Restore")
        self._update_header()

    # ==================================================================
    # 5. Normalize
    # ==================================================================
    def normalize_paths(self):
        if not self._ensure_scanned():
            return
        if not any(r.is_file_based for r in self.records):
            QMessageBox.information(self, PLUGIN_NAME, "The project has no file-based layers.")
            return
        dlg = NormalizeDialog(self.normalizer, self.records, self)
        if not dlg.exec():
            return
        mode = dlg.mode()
        if mode == MODE_CLEAN:
            results = self.normalizer.apply_clean(dlg.items, self.updater)
            self._report_results(results, "Normalize path")
        else:
            relative = mode == MODE_TO_RELATIVE
            entry = self.normalizer.apply_storage_mode(relative)
            for it in dlg.items:
                st = "Valid"
                rec = next((r for r in self.records if r.layer_id == it.layer_id), None)
                if rec is not None and rec.status == STATUS_BROKEN:
                    st = "Broken"
                self.audit.record(
                    "Convert to " + ("relative" if relative else "absolute"), it.layer_name,
                    it.layer_id, it.current, it.proposed if it.convertible else it.current, st, st,
                    "Success" if it.convertible else "Skipped", it.message, entry.op_id)
            self.audit.success(entry.description +
                               f" ({sum(1 for i in dlg.items if i.changed and i.convertible)} path(s) change)")
            if dlg.chk_save.isChecked():
                if self.project.write():
                    self.audit.success(f"Project saved: {self.project.fileName()}")
                else:
                    self.audit.error("Project could not be saved", self.project.error())
            else:
                self.audit.info("Save the project to write the new path format to disk.")
            self._update_history_label()
            self.start_scan(auto=True)

    # ==================================================================
    # 6. Validate
    # ==================================================================
    def validate_sources(self):
        if not self._ensure_scanned():
            return
        recs = [r for r in (self.model.checked_records() or self.records) if r.is_file_based]
        if not recs:
            QMessageBox.information(self, PLUGIN_NAME, "No file-based layers to validate.")
            return
        prog = QProgressDialog("Validating sources...", "Cancel", 0, len(recs), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(300)
        ok_n = fail_n = 0
        reconnectable = []
        for i, r in enumerate(recs):
            prog.setValue(i)
            prog.setLabelText(f"Validating {r.name} ({i + 1}/{len(recs)})")
            QApplication.processEvents()
            if prog.wasCanceled():
                self.audit.warning("Validation cancelled")
                break
            layer = self.project.mapLayer(r.layer_id)
            state, msg = self.validator.check_layer(layer)
            if state == VALID_FAIL:
                fail_n += 1
                self.audit.error(f"Not readable: {html.escape(r.name)}", msg)
            else:
                ok_n += 1
                if layer is not None and not layer.isValid():
                    reconnectable.append(r)
        prog.setValue(len(recs))
        self.audit.record("Validate sources", f"{len(recs)} layer(s)", "", "", "", "-", "-",
                          "Success" if not fail_n else "Failed",
                          f"{ok_n} readable, {fail_n} not readable")
        (self.audit.success if not fail_n else self.audit.warning)(
            f"Validation: {ok_n} readable, {fail_n} not readable")
        if reconnectable:
            names = "\n".join(f"  - {r.name}" for r in reconnectable[:15])
            if QMessageBox.question(
                    self, PLUGIN_NAME,
                    f"{len(reconnectable)} broken layer(s) can be opened again at their original "
                    f"location (the data is back):\n\n{names}\n\nReconnect them now?") == \
                    QMessageBox.StandardButton.Yes:
                props = []
                for r in reconnectable:
                    p = make_proposal(r, "Reconnect")
                    p.new_path, p.new_source = r.path, r.source
                    p.accepted, p.confidence, p.match_type = True, 100, "Original location"
                    props.append(p)
                self.apply_proposals(props, "Reconnect original source")
                return
        self.start_scan(auto=True)

    # ==================================================================
    # 7. Reports
    # ==================================================================
    def export_report(self):
        if not self.records:
            self.start_scan()
        path, flt = QFileDialog.getSaveFileName(
            self, "Export audit report",
            os.path.join(last_dir(), f"layer_path_audit_{datetime.now():%Y%m%d_%H%M}.csv"),
            "CSV - operations table (*.csv);;TXT - readable report (*.txt);;"
            "JSON - full report incl. layer inventory (*.json)")
        if not path:
            return
        ext = {"CSV": ".csv", "TXT": ".txt", "JSO": ".json"}.get(flt[:3], ".csv")
        if not os.path.splitext(path)[1]:
            path += ext
        try:
            dash = build_dashboard(summarize(self.records), self.audit.counts())
            export_audit(path, self.audit.entries, dash, self.records, self.project)
            remember_dir(path)
            self.audit.success(f"Audit report exported: {html.escape(path)}")
            if self.iface is not None:
                self.iface.messageBar().pushSuccess(PLUGIN_NAME, f"Report saved to {path}")
        except OSError as exc:
            self.audit.error("Export failed", str(exc))
            QMessageBox.critical(self, PLUGIN_NAME, f"Could not write the report:\n{exc}")

    def show_health_report(self):
        if not self._ensure_scanned():
            return
        rep = build_health_report(self.project, self.records)
        self.audit.info(f"Health report: score {rep.score}/100 ({rep.grade})")
        ReportDialog("Project Path Health Report", rep.to_html(), rep.to_text(), self,
                     base_name="path_health_report").exec()

    def show_migration_report(self):
        if not self._ensure_scanned():
            return
        rep = build_migration_report(self.project, self.records)
        self.audit.info(f"Migration readiness: {rep.readiness} - {len(rep.datasets)} file dataset(s), "
                        f"{human_size(rep.total_size)}")
        ReportDialog("Migration Assistant", rep.to_html(), rep.to_text(), self,
                     csv_rows_provider=rep.dependency_rows, base_name="migration_report").exec()

    def show_help(self):
        help_file = os.path.join(PLUGIN_DIR, "help", "index.html")
        try:
            with open(help_file, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = "<p>Help file not found.</p>"
        dlg = ReportDialog("Help", body, "", self, base_name="layer_path_manager_help")
        dlg.exec()

    # ==================================================================
    # Context menu / columns
    # ==================================================================
    def _table_context_menu(self, pos):
        idx = self.layerTable.indexAt(pos)
        if not idx.isValid():
            return
        rec = self.model.record(self.proxy.mapToSource(idx).row())
        layer = self.project.mapLayer(rec.layer_id)
        menu = QMenu(self)
        sel = self._selected_records()
        if rec.is_file_based:
            menu.addAction(theme_icon("mActionFileOpen.svg"), "Replace File...",
                           lambda: self.edit_selected_paths([rec]))
            files_sel = [r for r in sel if r.is_file_based] or [rec]
            menu.addAction(theme_icon("mActionFileSaveAs.svg"),
                           f"Copy / Move {len(files_sel)} layer(s) to...",
                           lambda: self.copy_data(records=files_sel))
            if len(sel) > 1:
                menu.addAction(f"Edit Paths of {len(sel)} Selected Rows...",
                               lambda: self.edit_selected_paths([r for r in sel if r.is_file_based]))
            menu.addAction(theme_icon("mIconSuccess.svg"), "Validate Source",
                           lambda: self._validate_one(rec))
        if self.history.first_for(rec.layer_id) is not None:
            menu.addAction(theme_icon("mActionUndo.svg"), "Restore Original Source",
                           lambda: self._restore([rec.layer_id], False))
        menu.addSeparator()
        menu.addAction("Check Selected Rows",
                       lambda: self.model.set_checked([r.layer_id for r in sel] or [rec.layer_id], True))
        menu.addAction("Uncheck Selected Rows",
                       lambda: self.model.set_checked([r.layer_id for r in sel] or [rec.layer_id], False))
        menu.addSeparator()
        if rec.path:
            menu.addAction(theme_icon("mActionEditCopy.svg"), "Copy File Path",
                           lambda: QGuiApplication.clipboard().setText(rec.path))
        menu.addAction(theme_icon("mActionEditCopy.svg"), "Copy Source URI",
                       lambda: QGuiApplication.clipboard().setText(rec.source))
        if rec.is_file_based:
            folder = rec.path if rec.is_directory else os.path.dirname(rec.path)
            act = menu.addAction(theme_icon("mIconFolderOpen.svg"), "Open Containing Folder",
                                 lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            act.setEnabled(os.path.isdir(folder))
        if layer is not None and self.iface is not None:
            menu.addSeparator()
            menu.addAction(theme_icon("mIconLayerTree.svg"), "Show in Layers Panel",
                           lambda: self.iface.layerTreeView().setCurrentLayer(layer))
            menu.addAction(theme_icon("mActionOptions.svg"), "Layer Properties...",
                           lambda: self.iface.showLayerProperties(layer))
        menu.exec(self.layerTable.viewport().mapToGlobal(pos))

    def _validate_one(self, rec):
        state, msg = self.validator.check_layer(self.project.mapLayer(rec.layer_id))
        (self.audit.error if state == VALID_FAIL else self.audit.success)(
            f"{html.escape(rec.name)}: {state}", msg)

    def _columns_menu(self, anchor):
        menu = QMenu(self)
        for i, (key, label) in enumerate(LayerTableModel.COLUMNS):
            if i == 0:
                continue
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(not self.layerTable.isColumnHidden(i))
            act.toggled.connect(lambda v, col=i: self._toggle_column(col, v))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _toggle_column(self, col, visible):
        self.layerTable.setColumnHidden(col, not visible)
        hidden = [k for i, (k, _l) in enumerate(LayerTableModel.COLUMNS)
                  if self.layerTable.isColumnHidden(i)]
        QgsSettings().setValue(SETTINGS + "hiddenColumnsV3", hidden)

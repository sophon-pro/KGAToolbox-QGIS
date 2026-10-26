"""The Data Explorer dock - QGIS's answer to Premiere Pro's
Media Browser panel: browse, preview, inspect, interpret and ingest."""

import datetime
import os
import time

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsMimeDataUtils,
    QgsProject,
    QgsRectangle,
    QgsTask,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDir, QEvent, QFileSystemWatcher, QMimeData, QSize, QStringListModel, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QKeySequence, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import output_defaults as od
from . import create_data as CD
from . import type_icons as ICONS
from . import inspector
from .folder_tree import FolderTreeModel
from . import tasks
from . import loader
from . import presets as P
from . import search_index as SI
from . import search_query as SQ
from .compat import ignore, QAction, QActionGroup, QShortcut
from .ingest_task import IngestTask
from .interpret_dialog import InterpretDialog
from .settings_dialog import IngestSettingsDialog

ROLE = Qt.ItemDataRole.UserRole
MAX_LISTING = 5000

KIND_ICONS = {
    inspector.KIND_VECTOR: "/mIconVector.svg",
    inspector.KIND_RASTER: "/mIconRaster.svg",
    inspector.KIND_TABLE: "/mIconTableLayer.svg",
    inspector.KIND_POINTCLOUD: "/mIconPointCloudLayer.svg",
    inspector.KIND_MESH: "/mIconMeshLayer.svg",
    inspector.KIND_UNKNOWN: "/mIconFile.svg",
}

SCOPES = [
    ("folder", "This folder"),
    ("tree", "This folder + subfolders"),
    ("favorites", "Favourite folders"),
    ("all", "All indexed locations"),
]
INDEX_STALE_SECONDS = 600
THUMBNAIL_TIMEOUT_MS = 6000

FILTERS = [
    ("All geodata", None),
    ("Vectors", {inspector.KIND_VECTOR}),
    ("Rasters / imagery", {inspector.KIND_RASTER}),
    ("Tables / CSV / Excel", {inspector.KIND_TABLE}),
    ("Point clouds", {inspector.KIND_POINTCLOUD}),
    ("Meshes", {inspector.KIND_MESH}),
]


def icon(kind):
    return QgsApplication.getThemeIcon(KIND_ICONS.get(kind, KIND_ICONS[inspector.KIND_UNKNOWN]))


def _fmt_size(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return ""


def add_action(menu, text, slot=None, icon_path=None):
    """QMenu.addAction(text, slot) differs between PyQt5 and PyQt6."""
    act = QAction(text, menu)
    if icon_path:
        act.setIcon(QgsApplication.getThemeIcon(icon_path))
    if slot is not None:
        act.triggered.connect(lambda _checked=False: slot())
    menu.addAction(act)
    return act


class SearchTask(QgsTask):
    """Runs a query off the GUI thread so typing never blocks.
    Prepared records are cached per scope until the index changes."""

    def __init__(self, params, cache, gen, on_done):
        flags = QgsTask.Flag.CanCancel
        hidden = getattr(QgsTask.Flag, "Hidden", None)
        if hidden is not None:
            flags = flags | hidden
        super().__init__("Data Explorer search", flags)
        self.params, self.cache, self.gen, self.on_done = params, cache, gen, on_done
        self.results, self.info, self.error = [], {}, ""

    def _records(self):
        p = self.params
        key = (tuple(p["roots"]) if p["roots"] is not None else None, p["scope"])
        hit = self.cache.get(key)
        if hit is not None and hit[0] == p["stamp"]:
            return hit[1]
        records = SI.load_records(p["roots"], recursive=(p["scope"] != "folder"))
        if p["scope"] == "folder" and p["roots"]:
            nf = SI.norm_path(p["roots"][0])
            records = [r for r in records if r["folder"] == nf]
        for r in records:
            r["container"] = inspector.container_label(r["path"])
            r["folder_display"] = os.path.dirname(r["path"].rstrip("/\\"))
        if len(self.cache) > 8:
            self.cache.clear()
        self.cache[key] = (p["stamp"], records)
        return records

    def run(self):
        try:
            records = self._records()
            if self.isCanceled():
                return False
            p = self.params
            self.results, self.info = SQ.search(records, p["query"], kinds=p["kinds"], bbox=p["bbox"])
        except Exception as exc:
            self.error = str(exc)
        return True

    def finished(self, result):
        self.on_done(self)


class IndexManagerDialog(QDialog):
    """Lists indexed folders (like ArcGIS Pro's indexing options)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Indexed locations")
        self.resize(640, 380)
        self.reindex = []
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Folders in the search index. Favourite folders and folders you search "
                             "are indexed automatically; add others (e.g. your data drive) here."))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Folder", "Datasets", "Last indexed"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, slot in (("Add folder…", self._add), ("Re-index selected", self._reindex),
                           ("Remove selected", self._remove), ("Clear whole index", self._clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load()

    def _load(self):
        status = SI.root_status()
        roots = dict(status)
        for r in P.extra_index_roots():
            roots.setdefault(SI.norm_path(r), (None, None))
        self.table.setRowCount(len(roots))
        for i, (path, (last, files)) in enumerate(sorted(roots.items())):
            self.table.setItem(i, 0, QTableWidgetItem(path))
            self.table.setItem(i, 1, QTableWidgetItem("" if files is None else "{:,}".format(files)))
            when = datetime.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else "pending"
            self.table.setItem(i, 2, QTableWidgetItem(when))

    def _selected_paths(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).text() for r in rows]

    def _add(self):
        path = QFileDialog.getExistingDirectory(self, "Add folder to search index")
        if path:
            roots = P.extra_index_roots()
            if path not in roots:
                roots.append(path)
                P.set_extra_index_roots(roots)
            self.reindex.append(path)
            self._load()

    def _reindex(self):
        self.reindex += self._selected_paths()
        self.accept()

    def _remove(self):
        paths = self._selected_paths()
        extra = [r for r in P.extra_index_roots() if SI.norm_path(r) not in {SI.norm_path(p) for p in paths}]
        P.set_extra_index_roots(extra)
        for p in paths:
            SI.forget_root(p)
        self._load()

    def _clear(self):
        if QMessageBox.question(self, "Clear index", "Remove all indexed information? "
                                "(Your data is not touched.)") == QMessageBox.StandardButton.Yes:
            SI.clear_index()
            self._load()


class FileRow(QTreeWidgetItem):
    """List row that keeps sub-folders above the data, whichever way the column is sorted."""

    def __lt__(self, other):
        a = (self.data(0, ROLE) or {}).get("is_folder", False)
        b = (other.data(0, ROLE) or {}).get("is_folder", False)
        if a != b:
            tree = self.treeWidget()
            ascending = tree is None or tree.header().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
            return a if ascending else not a
        col = self.treeWidget().sortColumn() if self.treeWidget() is not None else 0
        return self.text(col).casefold() < other.text(col).casefold()


class FileTree(QTreeWidget):
    """File list that drags as file URLs and accepts OS drops."""

    def __init__(self, browser):
        super().__init__()
        self.browser = browser
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

    def mimeData(self, items):  # noqa: N802 (Qt API)
        datas = [d for d in (it.data(0, ROLE) or {} for it in items) if not d.get("is_folder")]
        # Hand QGIS ready-made layer URIs so a drop on the map or Layers panel adds the
        # layers straight away. Plain file URLs make QGIS open its own "Select Items to
        # Add" window for GeoPackages, geodatabases and other multi-layer files.
        # The file URLs must NOT ride along with the URIs: QGIS handles both, so the
        # layers would be added and the file would then open the picker as well.
        try:
            uris = self.browser.layer_uris(datas)
            if uris:
                return QgsMimeDataUtils.encodeUriList(uris)
        except Exception as exc:
            ignore(exc)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(d["path"]) for d in datas if d.get("path")])
        return mime

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls() and event.source() is not self:
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls() and event.source() is not self:
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        if event.source() is self or not event.mimeData().hasUrls():
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        self.browser.handle_dropped_paths(paths)


class SublayerPicker(QDialog):
    def __init__(self, path, details, parent=None):
        super().__init__(parent)
        self.setWindowTitle("%s contains %d layers" % (os.path.basename(path), len(details)))
        self.setMinimumSize(460, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("This file contains %d layers.\nAdd all of them, or tick the ones you want "
                             "(double-click a layer to add only that one):" % len(details)))
        self.list = QListWidget()
        for d in details:
            kind = inspector.kind_of_details(d)
            count = d.featureCount()
            label = d.name() or os.path.basename(path)
            if count is not None and count >= 0 and kind in (inspector.KIND_VECTOR, inspector.KIND_TABLE):
                label += "   (%s features)" % "{:,}".format(count)
            it = QListWidgetItem(ICONS.file_icon(kind, category=ICONS.details_category(d)), label)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            it.setData(ROLE, d)
            self.list.addItem(it)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        for text, state in (("Select all", Qt.CheckState.Checked), ("Select none", Qt.CheckState.Unchecked)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, s=state: self._set_all(s))
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.list.itemDoubleClicked.connect(self._only_this)
        bb = QDialogButtonBox()
        self.all_btn = bb.addButton("All %d layers" % len(details), QDialogButtonBox.ButtonRole.AcceptRole)
        self.sel_btn = bb.addButton("Selected layers", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.all_btn.setDefault(True)
        self.all_btn.clicked.connect(lambda: (self._set_all(Qt.CheckState.Checked), self.accept()))
        self.sel_btn.clicked.connect(self._accept_selected)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _only_this(self, item):
        self._set_all(Qt.CheckState.Unchecked)
        item.setCheckState(Qt.CheckState.Checked)
        self.accept()

    def _accept_selected(self):
        if not self.selected():
            QMessageBox.information(self, "Layers", "Tick at least one layer, or choose All.")
            return
        self.accept()

    def _set_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def selected(self):
        return [self.list.item(i).data(ROLE) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class MediaBrowserDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Data Explorer", parent)
        self.setObjectName("KgaDataIngestBrowser")
        self.iface = iface
        self.catalog_getter = None  # set by the plugin: the KGA Catalog
        self.settings = P.current_settings()
        self._tasks = []
        self._preview_layer = None
        self._render_job = None
        self._jobs_alive = []
        self._current_folder = ""
        self._history = []
        self._index_task = None
        self._index_roots = set()
        self._index_stamp = 0
        self._records_cache = {}
        self._search_gen = 0
        self._search_task = None
        self._search_active = False
        self._preview_task = None
        self._preview_gen = 0
        self._preview_data = None
        self._pending_preview = None
        self._mini_windows = []
        self._graveyard = []
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._preview_pending)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.run_search)
        # Watch the open folder: new / removed / changed files refresh the list by themselves.
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _p: self._auto_refresh_timer.start(700))
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setSingleShot(True)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh)
        self._folder_signature = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1200)
        self._poll_timer.timeout.connect(self._poll_index)
        self._build_ui()
        for keys, slot in ((QKeySequence(QKeySequence.StandardKey.Find), self._focus_search),
                           (QKeySequence("F1"), self.show_search_help)):
            sc = QShortcut(keys, self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        try:
            self.iface.mapCanvas().extentsChanged.connect(self._extent_changed)
        except Exception as exc:
            ignore(exc)
        start = (P.recent_folders() or [QgsProject.instance().homePath() or QDir.homePath()])[0]
        if not os.path.isdir(start):
            start = QDir.homePath()
        self.navigate(start)

    # ================================================================ UI
    def _build_ui(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(4, 4, 4, 4)

        # --- navigation row
        nav = QHBoxLayout()
        self.back_btn = self._tool("/mActionArrowLeft.svg", "Back", self.go_back)
        self.up_btn = self._tool("/mActionArrowUp.svg", "Parent folder", self.go_up)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.up_btn)
        self.path_combo = QComboBox()
        self.path_combo.setEditable(True)
        self.path_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.path_combo.lineEdit().returnPressed.connect(
            lambda: self.navigate(self.path_combo.currentText()))
        self.path_combo.activated.connect(lambda i: self.navigate(self.path_combo.itemData(i) or self.path_combo.itemText(i)))
        self.path_combo.setMinimumWidth(160)
        nav.addWidget(self.path_combo, 6)
        nav.addWidget(self._tool("/mActionFileOpen.svg", "Browse for folder", self.browse_folder))
        self.fav_btn = self._tool("/mActionFavourites.svg", "Add / remove this folder as a favourite", self.toggle_favorite)
        self.fav_btn.setIcon(ICONS.star_icon(False))
        nav.addWidget(self.fav_btn)
        nav.addWidget(self._tool("/mActionRefresh.svg", "Refresh (re-index when searching)", self.refresh_hard))

        # --- search (Premiere-style Search Bins + ArcGIS-style filters) on the same row
        srow = nav
        srow.addSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Search…  (F1 for syntax)")
        self.search.setToolTip("Search names, layers, fields and CRS, e.g.  roads crs:32648 size<50mb   (F1 for syntax)")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(QgsApplication.getThemeIcon("/search.svg"),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda _t: self._schedule_search())
        self.search.returnPressed.connect(lambda: self._schedule_search(0, remember=True))
        self._completer_model = QStringListModel(P.recent_searches(), self)
        completer = QCompleter(self._completer_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.search.setCompleter(completer)
        self.search.setMinimumWidth(150)
        srow.addWidget(self.search, 2)
        # The scope is chosen from the filter menu; this hidden combo holds the state.
        self.scope_combo = QComboBox(self)
        self.scope_combo.hide()
        for key, label in SCOPES:
            self.scope_combo.addItem(label, key)
        self.scope_combo.setCurrentIndex(1)
        self.scope_combo.setToolTip("Where to search")
        self.scope_combo.currentIndexChanged.connect(lambda _i: self._schedule_search(0))
        self.scope_combo.currentIndexChanged.connect(lambda _i: self._update_filter_button())
        # "In map extent" lives in the filter menu (one compact row); this hidden checkbox
        # holds its state for the search code and the Search Bins.
        self.extent_check = QCheckBox("In map extent", self)
        self.extent_check.setToolTip("Only datasets that overlap the current map view")
        self.extent_check.hide()
        self.extent_check.toggled.connect(lambda _c: self._schedule_search(0))
        self.extent_check.toggled.connect(lambda _c: self._update_filter_button())
        self.search_menu_btn = QToolButton()
        self.search_menu_btn.setIcon(QgsApplication.getThemeIcon("/mActionFilter2.svg"))
        self.search_menu_btn.setToolTip("Where to search, map-extent filter, Search Bins, syntax help and index")
        self.search_menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.search_menu = QMenu(self)
        self.search_menu.aboutToShow.connect(self._fill_search_menu)
        self.search_menu_btn.setMenu(self.search_menu)
        self._update_filter_button()
        srow.addWidget(self.search_menu_btn)
        lay.addLayout(nav)

        # --- filter + ingest row
        row = QHBoxLayout()
        self.filter_combo = QComboBox()
        for label, _kinds in FILTERS:
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.filter_combo)
        self.recursive = QCheckBox("Subfolders")
        self.recursive.setToolTip("Show geodata from all subfolders (like Premiere's flattened view)")
        self.recursive.toggled.connect(lambda _c: self.refresh())
        row.addWidget(self.recursive)
        row.addSpacing(12)
        row.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(180)
        self._fill_presets()
        self.preset_combo.activated.connect(self._preset_chosen)
        row.addWidget(self.preset_combo)
        row.addSpacing(10)
        # Create: appears only when the selected folder / database can take new data
        self.create_btn = QToolButton()
        self.create_btn.setIcon(QgsApplication.getThemeIcon("/symbologyAdd.svg"))
        self.create_btn.setText("Create")
        self.create_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.create_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        create_menu = QMenu(self.create_btn)
        create_menu.aboutToShow.connect(lambda: self._fill_create_menu(create_menu, self._create_target()))
        self.create_btn.setMenu(create_menu)
        self.create_btn.setVisible(False)
        row.addWidget(self.create_btn)
        row.addStretch(1)
        self.ingest_check = QCheckBox("Ingest")
        self.ingest_check.setToolTip("When checked, imports are copied / converted / proxied in the "
                                     "background using the Ingest Settings (wrench).")
        self.ingest_check.setChecked(P.ingest_enabled())
        self.ingest_check.toggled.connect(self._ingest_toggled)
        row.addWidget(self.ingest_check)
        settings_btn = self._tool("/mActionOptions.svg", "Ingest Settings…", self.open_settings)
        settings_btn.setText("Settings")
        settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        row.addWidget(settings_btn)
        self.proxy_btn = self._tool("/mActionToggleEditing.svg",
                                    "Toggle Proxies (switch proxy-enabled layers between "
                                    "low-res proxy and full resolution)", self.toggle_proxies)
        self.proxy_btn.setText("Proxies")
        self.proxy_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        row.addWidget(self.proxy_btn)
        lay.addLayout(row)

        # --- three panes
        split = QSplitter(Qt.Orientation.Horizontal)
        self.fs_model = FolderTreeModel(self)
        self.folder_view = QTreeView()
        self.folder_view.setModel(self.fs_model)
        self.folder_view.setHeaderHidden(True)
        self.folder_view.expanded.connect(
            lambda idx: self.fs_model.load_children(self.fs_model.itemFromIndex(idx)))
        self.folder_view.expand(self.fs_model.index(0, 0))  # Home
        self.folder_view.clicked.connect(self._folder_clicked)
        self.folder_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_view.customContextMenuRequested.connect(self._tree_context_menu)
        split.addWidget(self.folder_view)

        self.files = FileTree(self)
        self.files.setHeaderLabels(["Name", "Type", "Size", "Modified", "Location", "Why it matched"])
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.setRootIsDecorated(True)
        self.files.setSortingEnabled(True)
        # Every column, Name included, can be dragged to resize; the last one fills the rest.
        self.files.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.files.header().setStretchLastSection(True)
        self.files.setColumnWidth(0, 300)
        self.files.setColumnWidth(1, 130)
        self.files.itemExpanded.connect(self._expand_item)
        self.files.itemSelectionChanged.connect(self._selection_changed)
        self.files.itemDoubleClicked.connect(lambda *_: self._open_item())
        self.files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._context_menu)
        split.addWidget(self.files)

        self.tabs = QTabWidget()
        self.thumb = QLabel("Select a dataset to preview it")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumSize(QSize(220, 220))
        self.thumb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.timeout.connect(self._rerender_thumbnail)
        self.thumb.installEventFilter(self)
        self.tabs.addTab(self.thumb, "Preview")
        self.info = QTableWidget(0, 2)
        self.info.setHorizontalHeaderLabels(["Property", "Value"])
        self.info.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.info.verticalHeader().setVisible(False)
        self.info.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.info.setWordWrap(True)
        self.tabs.addTab(self.info, "Metadata")
        self.attrs = QTableWidget(0, 0)
        self.attrs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabs.addTab(self.attrs, "Attributes")
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addWidget(self.tabs, 1)
        self.mini_btn = QPushButton(QgsApplication.getThemeIcon("/mActionZoomIn.svg"), "Open Mini Preview…")
        self.mini_btn.setToolTip("Zoom, select, identify and export features in a read-only preview window")
        self.mini_btn.setEnabled(False)
        self.mini_btn.clicked.connect(self.open_mini_preview)
        rlay.addWidget(self.mini_btn)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        lay.addWidget(split, 1)

        # --- bottom row
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.status = QLabel("")
        bottom.addWidget(self.status)
        interp = QPushButton("Interpret…")
        interp.setToolTip("Override CRS, encoding, NoData, CSV columns, name and bin before importing")
        interp.clicked.connect(self.interpret_selected)
        bottom.addWidget(interp)
        folder_btn = QPushButton("Import Folder as Bin")
        folder_btn.setToolTip("Import every dataset in the current folder (recursively), "
                              "mirroring sub-folders as groups")
        folder_btn.clicked.connect(self.import_folder)
        bottom.addWidget(folder_btn)
        self.import_btn = QPushButton("Import")
        self.import_btn.setDefault(True)
        self.import_btn.clicked.connect(self.import_selected)
        bottom.addWidget(self.import_btn)
        lay.addLayout(bottom)

        self.setWidget(root)
        self._refresh_ingest_label()

    def _tool(self, icon_path, tip, slot):
        b = QToolButton()
        b.setIcon(QgsApplication.getThemeIcon(icon_path))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        return b

    def _fill_presets(self):
        self.preset_combo.clear()
        current = self.settings.get("name")
        for p in P.all_presets():
            self.preset_combo.addItem(p["name"], p)
        idx = self.preset_combo.findText(current)
        if idx < 0:
            self.preset_combo.addItem(current or "Custom", self.settings)
            idx = self.preset_combo.count() - 1
        self.preset_combo.setCurrentIndex(idx)

    # ========================================================= navigation
    def navigate(self, path, sync_tree=True):
        path = os.path.expanduser((path or "").strip())
        if not os.path.isdir(path):
            if os.path.isfile(path):
                path = os.path.dirname(path)
            else:
                self.iface.messageBar().pushWarning("Data Explorer", "Folder not found: %s" % path)
                return
        if self._current_folder and self._current_folder != path:
            self._history.append(self._current_folder)
        self._current_folder = path
        self._watch_folder(path)
        P.push_recent_folder(path)
        self._fill_path_combo()
        if sync_tree:
            idx = self.fs_model.index_for_path(path)
            if idx.isValid():
                parent = idx.parent()
                while parent.isValid():
                    self.folder_view.expand(parent)
                    parent = parent.parent()
                self.folder_view.setCurrentIndex(idx)
                self.folder_view.scrollTo(idx)
        self.refresh()

    # ------------------------------------------------------------ add to project
    @staticmethod
    def _can_add_to_project(path):
        """Folders and geodatabases join the project's Folders, GeoPackages its Databases."""
        if not path:
            return False
        if os.path.isdir(path):
            return True
        return os.path.isfile(path) and path.lower().endswith(".gpkg")

    def _add_project_action(self, menu, path):
        if self._can_add_to_project(path) and self.catalog_getter is not None:
            add_action(menu, "Add to Project", lambda: self.add_to_project(path))

    def add_to_project(self, path):
        """Attach a folder / geodatabase / GeoPackage to the project (the Catalog's Project tab)."""
        catalog = self.catalog_getter() if self.catalog_getter else None
        workspace = getattr(catalog, "workspace", None)
        if catalog is None or workspace is None:
            self.iface.messageBar().pushWarning(
                "Data Explorer", "Add to Project needs the KGA Catalog, which is not available.")
            return
        known = list(workspace.folders) + list(workspace.databases)
        key = os.path.normcase(os.path.abspath(path))
        if any(os.path.normcase(os.path.abspath(k)) == key for k in known):
            self.iface.messageBar().pushInfo("Data Explorer", "%s is already in the project." % os.path.basename(path.rstrip("/\\")))
            return
        catalog.add_to_project(path)
        now = list(workspace.folders) + list(workspace.databases)
        if any(os.path.normcase(os.path.abspath(k)) == key for k in now):
            self.iface.messageBar().pushSuccess(
                "Data Explorer", "Added %s to the project." % os.path.basename(path.rstrip("/\\")))

    def _tree_context_menu(self, pos):
        idx = self.folder_view.indexAt(pos)
        path = self.fs_model.path_of(idx)
        if not path or not os.path.isdir(path):
            return
        menu = QMenu(self)
        add_action(menu, "Open", lambda: self.navigate(path))
        self._add_project_action(menu, path)
        target = (CD.target_for_path(path) or CD.T_FOLDER, path)
        if target[0] == CD.T_GDB and not CD.gdb_writable():
            target = None
        elif target[0] == CD.T_FOLDER and not os.access(path, os.W_OK):
            target = None
        if target is not None:
            self._fill_create_menu(menu.addMenu("Create"), target)
        menu.addSeparator()
        add_action(menu, "Refresh", lambda: self._refresh_tree_node(path))
        menu.exec(self.folder_view.viewport().mapToGlobal(pos))

    def _refresh_tree_node(self, path):
        """Tree right-click Refresh: re-read that folder's sub-folders (and the list, if it is open)."""
        self.fs_model.refresh_path(path)
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self._current_folder)):
            self.refresh_keep_state()
        self.status.setText("Refreshed %s" % (os.path.basename(path.rstrip("/\\")) or path))

    def _folder_clicked(self, idx):
        path = self.fs_model.path_of(idx)
        if path:
            self.navigate(path, sync_tree=False)

    def _fill_path_combo(self):
        self.path_combo.blockSignals(True)
        self.path_combo.clear()
        self.path_combo.addItem(self._current_folder, self._current_folder)
        favs = P.favorite_folders()
        for f in favs:
            self.path_combo.addItem(ICONS.star_icon(True), f, f)
        for r in P.recent_folders():
            if r != self._current_folder and r not in favs:
                self.path_combo.addItem(r, r)
        self.path_combo.setCurrentIndex(0)
        self.path_combo.blockSignals(False)
        is_fav = self._current_folder in favs
        self.fav_btn.setIcon(ICONS.star_icon(is_fav))
        self.fav_btn.setToolTip("Remove this folder from favourites" if is_fav else "Add this folder to favourites")

    def go_up(self):
        parent = os.path.dirname(self._current_folder.rstrip("/\\"))
        if parent and parent != self._current_folder:
            self.navigate(parent)

    def go_back(self):
        if self._history:
            target = self._history.pop()
            self._current_folder = ""
            self.navigate(target)

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self._current_folder)
        if path:
            self.navigate(path)

    def toggle_favorite(self):
        favs = P.favorite_folders()
        if self._current_folder in favs:
            favs.remove(self._current_folder)
            msg = "Removed from favourites"
        else:
            favs.insert(0, self._current_folder)
            msg = "Added to favourites"
        P.set_favorite_folders(favs)
        self._fill_path_combo()
        self.status.setText(msg)

    # ============================================================ listing
    def _iter_paths(self):
        if self.recursive.isChecked():
            count = 0
            for root, dirs, files in os.walk(self._current_folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for d in list(dirs):
                    if os.path.splitext(d)[1].lower() in inspector.DIR_DATASETS:
                        dirs.remove(d)
                        yield os.path.join(root, d)
                for f in files:
                    yield os.path.join(root, f)
                    count += 1
                    if count > MAX_LISTING:
                        return
        else:
            try:
                for entry in os.scandir(self._current_folder):
                    yield entry.path
            except OSError:
                return

    def populate(self):
        self._search_active = False
        self._set_columns(search=False)
        self.files.setSortingEnabled(False)
        self.files.clear()
        _label, kinds = FILTERS[self.filter_combo.currentIndex()]
        needle = ""  # text search is handled by run_search()
        if inspector.is_dataset_folder(self._current_folder):
            # The user opened a File Geodatabase (or other folder dataset)
            # in the folder tree: show its feature classes / tables / rasters.
            self._populate_container(self._current_folder, kinds, needle)
            return
        n = 0
        if not self.recursive.isChecked():
            self._add_folder_rows()
        for path in self._iter_paths():
            name = os.path.basename(path)
            if name.startswith(".") or not inspector.is_geodata_candidate(path):
                continue
            if self._is_shapefile_sidecar(path):
                continue
            kind = inspector.guess_kind_from_extension(path)
            # containers (GDB, GeoPackage, zip...) may hold any data type
            if kinds is not None and kind not in kinds and not (
                    inspector.is_container(path) and kinds & {inspector.KIND_VECTOR, inspector.KIND_RASTER,
                                                              inspector.KIND_TABLE}):
                continue
            if needle and needle not in name.lower():
                continue
            self._add_file_item(path, kind)
            n += 1
        self.files.setSortingEnabled(True)
        self.files.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        folders = sum(1 for i in range(self.files.topLevelItemCount())
                      if (self.files.topLevelItem(i).data(0, ROLE) or {}).get("is_folder"))
        self._set_base_status(("%d folder(s), " % folders if folders else "") + "%d dataset(s)" % n)

    def _add_folder_rows(self):
        """Sub-folders of the current folder, so you can step into them from the list."""
        icons = QFileIconProvider()
        folder_icon = icons.icon(QFileIconProvider.IconType.Folder)
        try:
            entries = [e for e in os.scandir(self._current_folder) if e.is_dir()]
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or os.path.splitext(e.name)[1].lower() in inspector.DIR_DATASETS:
                continue  # geodatabases are datasets, listed with the data
            try:
                mtime = datetime.datetime.fromtimestamp(e.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                mtime = ""
            it = FileRow([e.name, "Folder", "", mtime])
            it.setIcon(0, folder_icon)
            it.setToolTip(0, e.path)
            it.setData(0, ROLE, {"path": e.path, "is_folder": True, "name": e.name})
            self.files.addTopLevelItem(it)

    def _populate_container(self, path, kinds, needle):
        cached = inspector.cached_sublayers(path)
        if cached is not None:
            self._fill_container(path, cached, kinds, needle)
            return
        self.files.setSortingEnabled(True)
        self.status.setText("Reading %s…" % os.path.basename(path.rstrip("/\\")))
        task = tasks.SublayerTask(path, self._container_ready, payload=(kinds, needle))
        self._keep(task)
        QgsApplication.taskManager().addTask(task)

    def _container_ready(self, task):
        self._release(task)
        if SI.norm_path(self._current_folder) != SI.norm_path(task.path) or self._search_active:
            return  # user moved on
        if task.error:
            self.iface.messageBar().pushWarning("Data Explorer", "Cannot read %s: %s" % (task.path, task.error))
        kinds, needle = task.payload
        self._fill_container(task.path, task.details, kinds, needle)

    def _fill_container(self, path, details, kinds, needle):
        name = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
        self.files.setSortingEnabled(False)
        self.files.clear()
        n = 0
        for d in details:
            kind = inspector.kind_of_details(d)
            if kinds is not None and kind not in kinds:
                continue
            if needle and needle not in (d.name() or "").lower():
                continue
            self.files.addTopLevelItem(self._sublayer_item(path, name, d, kind))
            n += 1
        self.files.setSortingEnabled(True)
        self.files.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        if not details:
            self._set_base_status("QGIS found no readable layers in this geodatabase")
        else:
            self._set_base_status("%d layer(s) in %s" % (n, os.path.basename(path.rstrip("/\\"))))

    # keep Python references to running tasks (QGIS owns them, but the
    # wrappers must survive until finished() has called back)
    def _keep(self, task):
        self._tasks.append(task)

    def _release(self, task):
        if task in self._tasks:
            self._tasks.remove(task)

    def _sublayer_item(self, path, container_name, d, kind):
        count = d.featureCount()
        size = "{:,} feat.".format(count) if count is not None and count >= 0 and \
            kind in (inspector.KIND_VECTOR, inspector.KIND_TABLE) else ""
        label = d.name() or container_name
        cat = ICONS.details_category(d)
        item = QTreeWidgetItem([label, ICONS.type_label(kind, cat), size, ""])
        item.setIcon(0, ICONS.file_icon(kind, category=cat))
        item.setToolTip(0, "%s\nlayer: %s" % (path, label))
        item.setData(0, ROLE, {"path": path, "kind": kind, "name": label,
                               "details": d, "overrides": {}})
        return item

    @staticmethod
    def _is_shapefile_sidecar(path):
        stem, ext = os.path.splitext(path)
        return ext.lower() in (".dbf", ".prj", ".shx", ".cpg") and (
            os.path.exists(stem + ".shp") or os.path.exists(stem + ".SHP"))

    def _add_file_item(self, path, kind):
        name = os.path.basename(path.rstrip("/\\"))
        if self.recursive.isChecked():
            rel = os.path.relpath(os.path.dirname(path), self._current_folder)
            if rel != ".":
                name = os.path.join(rel, name)
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime = ""
        cat = None if inspector.container_label(path) or self._may_have_sublayers(path) else ICONS.path_category(path)
        type_label = inspector.container_label(path) or ICONS.type_label(kind, cat, path)
        # folder datasets (GDB) would need a recursive walk: sized lazily in the preview
        size = "" if os.path.isdir(path) else inspector.human_size(path)
        it = FileRow([name, type_label, size, mtime])
        it.setIcon(0, ICONS.file_icon(kind, path, cat, top=True))
        it.setToolTip(0, path)
        it.setData(0, ROLE, {"path": path, "kind": kind, "name": os.path.splitext(os.path.basename(path))[0],
                             "details": None, "overrides": {}})
        if self._may_have_sublayers(path):
            it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self.files.addTopLevelItem(it)

    @staticmethod
    def _may_have_sublayers(path):
        ext = os.path.splitext(path)[1].lower()
        return ext in {".gpkg", ".kml", ".kmz", ".gdb", ".dxf", ".gml", ".sqlite", ".zip",
                       ".nc", ".hdf", ".h5", ".he5", ".xlsx", ".xls", ".ods", ".osm", ".pbf",
                       ".grib", ".grb", ".grb2", ".gpx", ".tab", ".mbtiles"}

    def _expand_item(self, item):
        data = item.data(0, ROLE)
        if not data or item.childCount() or item.parent() is not None or data.get("details") is not None:
            return
        cached = inspector.cached_sublayers(data["path"])
        if cached is not None:
            self._fill_children(item, data, cached)
            return
        placeholder = QTreeWidgetItem(["Reading layers…"])
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        item.addChild(placeholder)
        task = tasks.SublayerTask(data["path"], self._children_ready, payload=item)
        self._keep(task)
        QgsApplication.taskManager().addTask(task)

    def _children_ready(self, task):
        self._release(task)
        item = task.payload
        try:
            data = item.data(0, ROLE)
            item.takeChildren()
        except RuntimeError:
            return  # list was refreshed meanwhile
        if task.error:
            self.iface.messageBar().pushWarning("Data Explorer", "Cannot read %s: %s" % (task.path, task.error))
        self._fill_children(item, data, task.details)

    def _fill_children(self, item, data, details):
        if len(details) <= 1:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
            return
        for d in details:
            item.addChild(self._sublayer_item(data["path"], data["name"], d, inspector.kind_of_details(d)))
        item.setExpanded(True)

    def _context_menu(self, pos):
        menu = QMenu(self)
        items = self._selected_items()
        if len(items) == 1 and (items[0].data(0, ROLE) or {}).get("is_folder"):
            folder = items[0].data(0, ROLE)["path"]
            add_action(menu, "Open folder", lambda: self.navigate(folder))
            add_action(menu, "Refresh", lambda: self._refresh_item(items[0]))
            self._add_project_action(menu, folder)
            if os.access(folder, os.W_OK):
                self._fill_create_menu(menu.addMenu("Create"), (CD.T_FOLDER, folder))
            add_action(menu, "Show in file explorer",
                       lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            menu.exec(self.files.viewport().mapToGlobal(pos))
            return
        add_action(menu, "Add to Map", self.add_to_map)
        add_action(menu, "Import (with Ingest settings)", self.import_selected)
        add_action(menu, "Interpret…", self.interpret_selected)
        add_action(menu, "Open in Mini Preview…", self.open_mini_preview)
        menu.addSeparator()
        add_action(menu, "Show in folder", self._show_in_folder)
        add_action(menu, "Copy path", self._copy_paths)
        menu.addSeparator()
        if len(items) == 1 and CD.target_for_path((items[0].data(0, ROLE) or {}).get("path")) or (
                len(items) == 1 and inspector.is_container((items[0].data(0, ROLE) or {}).get("path", ""))):
            add_action(menu, "Refresh", lambda: self._refresh_item(items[0]))
        add_action(menu, "Refresh folder", self.refresh_keep_state)
        if len(items) == 1:
            self._add_project_action(menu, (items[0].data(0, ROLE) or {}).get("path"))
        target = self._create_target()
        if target is not None:
            self._fill_create_menu(menu.addMenu("Create"), target)
        menu.exec(self.files.viewport().mapToGlobal(pos))

    def _copy_paths(self):
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.clipboard().setText("\n".join(d["path"] for d in self._selected_data()))

    # ============================================================ preview
    def _selected_items(self):
        return self.files.selectedItems()

    def _selected_data(self):
        return [it.data(0, ROLE) for it in self._selected_items()
                if it.data(0, ROLE) and not it.data(0, ROLE).get("is_folder")]

    # ------------------------------------------------------------ create data
    def _create_target(self):
        """(type, path) of the database or folder that new data would go into, or None."""
        items = self._selected_items()
        if len(items) == 1:
            data = items[0].data(0, ROLE) or {}
            path = data.get("path") or ""
            if data.get("is_folder"):
                return (CD.T_FOLDER, path) if os.access(path, os.W_OK) else None
            tt = CD.target_for_path(path)
            if tt == CD.T_GDB and not CD.gdb_writable():
                return None
            if tt:
                return tt, path
            return None
        if len(items) > 1 or self._search_active:
            return None
        folder = self._current_folder
        if not folder or not os.path.isdir(folder):
            return None
        if inspector.is_dataset_folder(folder):
            return (CD.T_GDB, folder) if folder.lower().endswith(".gdb") and CD.gdb_writable() else None
        return (CD.T_FOLDER, folder) if os.access(folder, os.W_OK) else None

    def _update_create_button(self):
        target = self._create_target()
        self.create_btn.setVisible(target is not None)
        if target:
            self.create_btn.setToolTip("Create a new layer or table in\n%s" % target[1])

    def _fill_create_menu(self, menu, target):
        """Create entries for `target` = (type, path): layers and tables, plus databases in a folder."""
        menu.clear()
        if target is None:
            return
        tt, path = target
        for geom, label in ((CD.POINT, "Create Point layer"), (CD.LINE, "Create Line layer"),
                            (CD.POLYGON, "Create Polygon layer"), (CD.TABLE, "Create Table")):
            act = QAction(ICONS.file_icon(inspector.KIND_TABLE if geom == CD.TABLE else inspector.KIND_VECTOR,
                                          category=None if geom == CD.TABLE else geom), label, menu)
            act.triggered.connect(lambda _c=False, g=geom, t=target: self.create_data(g, t))
            menu.addAction(act)
        if tt == CD.T_FOLDER:
            menu.addSeparator()
            folder_act = QAction(QFileIconProvider().icon(QFileIconProvider.IconType.Folder),
                                 "Create Folder", menu)
            folder_act.triggered.connect(lambda _c=False, f=path: self.create_folder(f))
            menu.addAction(folder_act)
            for key, (label, _ext, _drv, _opt) in CD.DATABASES.items():
                if CD.database_supported(key):
                    act = QAction(ICONS.file_icon(inspector.KIND_UNKNOWN, "x." + key, top=True), label, menu)
                    act.triggered.connect(lambda _c=False, k=key, f=path: self.create_database(k, f))
                    menu.addAction(act)

    def create_folder(self, parent):
        from qgis.PyQt.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Create Folder", "Name of the new folder:")
        if not ok:
            return
        try:
            path = CD.create_folder(parent, name)
        except ValueError as exc:
            QMessageBox.warning(self, "Create Folder", str(exc))
            return
        self.iface.messageBar().pushSuccess("Data Explorer", "Created folder %s" % os.path.basename(path))
        self._after_create(parent, path)

    def create_database(self, key, folder):
        from qgis.PyQt.QtWidgets import QInputDialog
        label = CD.DATABASES[key][0]
        name, ok = QInputDialog.getText(self, label, "Name of the new database:")
        if not ok:
            return
        try:
            path = CD.create_database(key, folder, name.strip())
        except ValueError as exc:
            QMessageBox.warning(self, label, str(exc))
            return
        self.iface.messageBar().pushSuccess("Data Explorer", "Created %s" % os.path.basename(path))
        self._after_create(folder, path)

    def _after_create(self, folder, select_path):
        """Show a new file: refresh the list when it is the open folder, and the tree always."""
        if os.path.normcase(os.path.abspath(folder)) == os.path.normcase(os.path.abspath(self._current_folder)):
            self._folder_signature = self._signature_of(self._current_folder)
            self.refresh_keep_state(select_path=select_path)
        else:
            self.fs_model.refresh_path(folder)

    def create_data(self, geom, target=None):
        target = target or self._create_target()
        if target is None:
            return
        tt, path = target
        existing = CD.existing_names(path) if tt in (CD.T_GPKG, CD.T_GDB) else []
        dlg = CD.CreateDataDialog(geom, tt, path, existing, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = CD.create_data(dlg.spec())
        except ValueError as exc:
            QMessageBox.warning(self, "Create " + CD.TITLES[geom], str(exc))
            return
        msg = "Created '%s' in %s" % (result["layer"], os.path.basename(result["path"].rstrip("/\\")))
        if dlg.add_to_map.isChecked():
            if CD.add_to_project(result) is None:
                msg += " (could not add it to the map)"
        self.iface.messageBar().pushSuccess("Data Explorer", msg)
        self._after_create(path if tt == CD.T_FOLDER else os.path.dirname(path.rstrip("/\\")), result["path"])

    def _set_base_status(self, text):
        """The folder / search summary; the selection count is appended to it."""
        self._status_base = text
        self._update_create_button()
        self._show_selection_status(len(self._selected_items()))

    def _show_selection_status(self, n_selected):
        base = getattr(self, "_status_base", "")
        parts = [base] if base else []
        if n_selected:
            parts.append("%d selected" % n_selected)
        self.status.setText(" • ".join(parts))

    def _selection_changed(self):
        items = self._selected_items()
        self.mini_btn.setEnabled(len(items) == 1)
        self._show_selection_status(len(items))
        self._update_create_button()
        if len(items) == 1 and (items[0].data(0, ROLE) or {}).get("is_folder"):
            self.mini_btn.setEnabled(False)
            self._preview_timer.stop()
            self._pending_preview = None
            return
        if len(items) != 1:
            self._preview_timer.stop()
            self._pending_preview = None
            return
        # debounce: arrowing through a list only previews where you stop
        self._pending_preview = items[0].data(0, ROLE)
        self._preview_timer.start(180)

    def _preview_pending(self):
        if self._pending_preview is not None:
            self._preview(self._pending_preview)

    @staticmethod
    def _resolve_details(data):
        """Search results point at a layer by name; find its sublayer details."""
        if data.get("details") is not None or not data.get("layer_name"):
            return
        try:
            details = inspector.query_sublayers(data["path"])
        except Exception:
            return
        wanted = data["layer_name"].casefold()
        for d in details:
            if (d.name() or "").casefold() == wanted:
                data["details"] = d
                return

    def _task_data(self, data):
        """Copy of an item's data with what a background task needs."""
        d = dict(data)
        if d.get("details") is None and inspector.is_delimited_text(d["path"]):
            c = self._csv_options(d)
            mode = c.get("geom_mode", "xy")
            d["preview_uri"] = inspector.csv_preview_uri(
                d["path"], c.get("delimiter", ","),
                c.get("x_field", "") if mode == "xy" else "", c.get("y_field", "") if mode == "xy" else "",
                c.get("wkt_field", "") if mode == "wkt" else "")
            d["preview_provider"] = "ogr"
            d["preview_crs"] = (d.get("overrides") or {}).get("crs") or \
                self.settings.get("default_csv_crs", "EPSG:4326")
        return d

    def _preview(self, data):
        """Start an asynchronous preview; the GUI thread never opens files."""
        self._cancel_render()
        self._preview_gen += 1
        if self._preview_task is not None:
            try:
                self._preview_task.cancel()
            except RuntimeError:
                pass
        self._retire(self._preview_layer)
        self._preview_layer = None
        self._preview_data = data
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText("Loading preview…")
        self.info.setRowCount(0)
        self.attrs.clear()
        self.attrs.setRowCount(0)
        self.attrs.setColumnCount(0)
        task = tasks.PreviewTask(self._task_data(data), QgsProject.instance().transformContext(),
                                 self._preview_done, gen=self._preview_gen)
        self._preview_task = task
        QgsApplication.taskManager().addTask(task)

    def _retire(self, layer):
        """Drop our reference to a layer without destroying it on this thread."""
        if layer is not None:
            self._graveyard.append(layer)
        self._collect_graveyard()

    def _collect_graveyard(self):
        live = {id(l) for _j, l in self._jobs_alive}
        if self._preview_layer is not None:
            live.add(id(self._preview_layer))
        keep = []
        for layer in self._graveyard:
            if id(layer) in live:
                keep.append(layer)
            else:
                tasks.dispose(layer)
        self._graveyard = keep

    def _preview_done(self, task):
        if task.gen != self._preview_gen:
            tasks.dispose(task.layer)  # superseded by a newer selection
            task.layer = None
            return
        self._preview_task = None
        data = self._preview_data or task.data
        rows = list(task.rows)
        if data.get("overrides"):
            rows.insert(2, ("Interpretation", ", ".join(
                "%s=%s" % kv for kv in data["overrides"].items() if kv[0] != "csv")))
        if task.error:
            rows.append(("Error", task.error))
        self._fill_info(rows)
        layer = task.layer
        if layer is None:
            if task.note == "pointcloud":
                self.thumb.setText("Point clouds are indexed on import;\npreview is skipped to keep browsing fast.")
            else:
                self.thumb.setText("Preview failed: %s" % task.error if task.error else "No preview available")
            return
        if not layer.isValid():
            self.thumb.setText("QGIS cannot read this dataset")
            return
        self._preview_layer = layer
        task.layer = None
        if task.attr_fields:
            self.attrs.setColumnCount(len(task.attr_fields))
            self.attrs.setHorizontalHeaderLabels(task.attr_fields)
            self.attrs.setRowCount(len(task.attr_rows))
            for r, row in enumerate(task.attr_rows):
                for c, val in enumerate(row):
                    self.attrs.setItem(r, c, QTableWidgetItem(val))
        self._render_thumbnail(layer)

    def _fill_info(self, rows):
        self.info.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.info.setItem(r, 0, QTableWidgetItem(k))
            item = QTableWidgetItem(str(v))
            item.setToolTip(str(v))
            self.info.setItem(r, 1, item)
        self.info.resizeRowsToContents()

    def _render_thumbnail(self, layer):
        extent = layer.extent()  # already computed (cached) by the preview task
        if extent.isEmpty() or not extent.isFinite():
            if isinstance(layer, QgsVectorLayer) and not layer.isSpatial():
                self.thumb.setText("Attribute table (no geometry)\nsee the Attributes tab")
            else:
                self.thumb.setText("No spatial extent")
            return
        if extent.width() == 0 or extent.height() == 0:
            extent = QgsRectangle(extent)
            extent.grow(max(extent.width(), extent.height(), 1.0) * 0.1 or 1.0)
        ms = QgsMapSettings()
        ms.setLayers([layer])
        ms.setDestinationCrs(layer.crs())
        # Render at the pane's real pixel size (and the screen's pixel ratio) so the
        # picture is drawn sharp instead of a small image stretched to fit.
        dpr = max(1.0, float(self.thumb.devicePixelRatioF()))
        side = self.thumb.contentsRect().size()
        w = int(min(max(side.width(), 220), 1400) * dpr)
        h = int(min(max(side.height(), 220), 1400) * dpr)
        ms.setOutputSize(QSize(w, h))
        ms.setDevicePixelRatio(dpr)
        ms.setOutputDpi(96 * dpr)
        ms.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
        ms.setBackgroundColor(QColor(255, 255, 255, 0))
        pad = extent.width(), extent.height()
        extent = QgsRectangle(extent)
        extent.grow(max(pad) * 0.06)  # a little margin so edge lines are not clipped
        ms.setExtent(extent)
        self._thumb_dpr = dpr
        job = QgsMapRendererParallelJob(ms)
        job.finished.connect(lambda j=job: self._thumbnail_ready(j))
        self._render_job = job
        self._jobs_alive.append((job, layer))  # keep alive until finished
        job.start()
        # huge layers: show what is drawn after a few seconds instead of waiting
        QTimer.singleShot(THUMBNAIL_TIMEOUT_MS, lambda j=job: self._thumbnail_timeout(j))

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.thumb and event.type() == QEvent.Type.Resize:
            self._thumb_timer.start(250)
        return super().eventFilter(obj, event)

    def _rerender_thumbnail(self):
        """Redraw the preview sharp for the new pane size."""
        layer = self._preview_layer
        if layer is not None and self._render_job is None and self.thumb.pixmap() is not None                 and not self.thumb.pixmap().isNull():
            self._render_thumbnail(layer)

    def _thumbnail_timeout(self, job):
        if job is self._render_job:
            try:
                job.cancelWithoutBlocking()
            except RuntimeError:
                pass

    def _thumbnail_ready(self, job):
        self._jobs_alive = [(j, l) for j, l in self._jobs_alive if j is not job]
        self._collect_graveyard()
        if job is not self._render_job:
            return
        self._render_job = None
        try:
            img = job.renderedImage()
        except RuntimeError:
            return
        if img.isNull():
            self.thumb.setText("No preview")
            return
        dpr = getattr(self, "_thumb_dpr", 1.0)
        pix = QPixmap.fromImage(img)
        box = self.thumb.contentsRect().size()
        if box.width() > 1 and box.height() > 1 and (
                abs(pix.width() / dpr - box.width()) > 2 or abs(pix.height() / dpr - box.height()) > 2):
            # The pane changed size while drawing: fit the whole picture now (never crop),
            # then redraw sharp for the new size.
            pix = pix.scaled(box * dpr, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            self._thumb_timer.start(150)
        pix.setDevicePixelRatio(dpr)
        self.thumb.setPixmap(pix)

    def _cancel_render(self):
        if self._render_job is not None:
            try:
                self._render_job.cancelWithoutBlocking()
            except Exception as exc:
                ignore(exc)
            self._render_job = None

    # ======================================================= mini preview
    def open_mini_preview(self):
        datas = self._selected_data()
        if len(datas) != 1:
            self.iface.messageBar().pushInfo("Data Explorer", "Select one dataset or layer to preview.")
            return
        data = datas[0]
        if data["kind"] == inspector.KIND_POINTCLOUD:
            self.iface.messageBar().pushInfo("Data Explorer", "Mini Preview supports vector and raster data.")
            return
        layer = self._preview_layer
        if layer is not None and self._preview_data is data and layer.isValid():
            self._preview_layer = None  # hand the already-open layer over: no reload
            self._show_mini(layer, data)
            return
        self.status.setText("Opening Mini Preview…")
        task = tasks.PreviewTask(self._task_data(data), QgsProject.instance().transformContext(),
                                 lambda t, d=data: self._mini_ready(t, d), attr_limit=0)
        self._keep(task)
        QgsApplication.taskManager().addTask(task)

    def _mini_ready(self, task, data):
        self._release(task)
        layer, task.layer = task.layer, None
        if layer is None or not layer.isValid():
            tasks.dispose(layer)
            self.iface.messageBar().pushWarning("Data Explorer", "Cannot open %s for preview. %s"
                                                % (data["name"], task.error))
            return
        self._show_mini(layer, data)

    def _show_mini(self, layer, data):
        from .mini_preview import MiniPreviewDialog
        dlg = MiniPreviewDialog(layer, data, self.iface, self.iface.mainWindow())
        self._mini_windows.append(dlg)
        dlg.finished.connect(lambda _r, d=dlg: self._mini_windows.remove(d) if d in self._mini_windows else None)
        dlg.show()
        dlg.raise_()

    # ===================================================== interpretation
    def interpret_selected(self):
        items = self._selected_items()
        if not items:
            QMessageBox.information(self, "Interpret", "Select one or more datasets first.")
            return
        datas = [it.data(0, ROLE) for it in items]
        dlg = InterpretDialog(datas, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ov = dlg.overrides()
        for it, data in zip(items, datas):
            new = dict(data.get("overrides") or {})
            new.update(ov)
            if not dlg.single:
                new.pop("name", None)
            data["overrides"] = new
            it.setData(0, ROLE, data)
            f = it.font(0)
            f.setItalic(True)
            it.setFont(0, f)
            it.setToolTip(0, "%s\nInterpreted: %s" % (data["path"], ", ".join(sorted(new.keys()))))
        if len(items) == 1:
            self._preview(datas[0])

    # ============================================================ import
    def _csv_options(self, data):
        c = (data.get("overrides") or {}).get("csv")
        if c is None:
            sniff = inspector.sniff_csv(data["path"]) if self.settings.get("auto_xy", True) else \
                {"delimiter": ",", "x_field": "", "y_field": "", "wkt_field": ""}
            if not self.settings.get("auto_xy", True):
                sniff["delimiter"] = inspector.sniff_csv(data["path"])["delimiter"]
            c = {"delimiter": sniff["delimiter"], "x_field": sniff["x_field"],
                 "y_field": sniff["y_field"], "wkt_field": sniff["wkt_field"],
                 "geom_mode": "wkt" if sniff["wkt_field"] else "xy" if sniff["x_field"] else "none",
                 "detect_types": True}
        return c

    def _csv_job(self, data, spatial_index=True):
        ov = data.get("overrides") or {}
        c = self._csv_options(data)
        mode = c.get("geom_mode", "xy")
        crs = ov.get("crs") or self.settings.get("default_csv_crs", "EPSG:4326")
        uri = inspector.delimited_text_uri(
            data["path"], delimiter=c.get("delimiter", ","),
            x_field=c.get("x_field", "") if mode == "xy" else "",
            y_field=c.get("y_field", "") if mode == "xy" else "",
            wkt_field=c.get("wkt_field", "") if mode == "wkt" else "",
            crs=crs, detect_types=c.get("detect_types", True),
            encoding=ov.get("encoding") or "UTF-8", spatial_index=spatial_index)
        has_geom = (mode == "xy" and c.get("x_field") and c.get("y_field")) or \
                   (mode == "wkt" and c.get("wkt_field"))
        return {"source_path": data["path"], "uri": uri, "provider": "delimitedtext",
                "kind": inspector.KIND_VECTOR if has_geom else inspector.KIND_TABLE,
                "name": data["name"], "sublayer_name": data["name"],
                "overrides": {k: v for k, v in ov.items() if k not in ("csv", "crs", "encoding")}}

    def _job_from_details(self, data, d):
        name = d.name() or data["name"]
        if data.get("details") is None and len(name) == 0:
            name = data["name"]
        return {"source_path": data["path"], "uri": d.uri(), "provider": d.providerKey(),
                "kind": inspector.kind_of_details(d), "name": name,
                "sublayer_name": d.name() or data["name"],
                "overrides": dict(data.get("overrides") or {})}

    def _jobs_for(self, data, sublayer_policy):
        """Synchronous variant (uses the sublayer cache when possible)."""
        self._resolve_details(data)
        details = None
        if data.get("details") is None and not inspector.is_delimited_text(data["path"]):
            details = inspector.query_sublayers(data["path"])
        return self._jobs_from(data, details, sublayer_policy)

    def _jobs_from(self, data, details, sublayer_policy):
        if data.get("details") is not None:
            return [self._job_from_details(data, data["details"])]
        if details == "direct":
            provider, uri, kind = inspector.direct_source(data["path"])
            return [{"source_path": data["path"], "uri": uri, "provider": provider, "kind": kind,
                     "name": data["name"], "sublayer_name": data["name"],
                     "overrides": dict(data.get("overrides") or {})}]
        if inspector.is_delimited_text(data["path"]):
            return [self._csv_job(data)]
        if not details:
            return []
        if len(details) == 1:
            job = self._job_from_details(data, details[0])
            job["name"] = data["name"]
            return [job]
        if sublayer_policy == "first":
            details = details[:1]
        elif sublayer_policy == "ask":
            picker = SublayerPicker(data["path"], details, self)
            if picker.exec() != QDialog.DialogCode.Accepted:
                return None  # the user cancelled: not an error
            details = picker.selected()
        jobs = [self._job_from_details(data, d) for d in details]
        if len(jobs) > 1 and self.settings.get("bin_mode") != P.BIN_NONE:
            for j in jobs:  # a container file becomes its own sub-bin
                j["container"] = data["name"]
        return jobs

    def _prepare_import(self, datas, policy, bin_fn=None, link_only=False):
        """Resolve sublayers in the background, then build jobs and run."""
        datas = [dict(d) for d in datas]
        self.status.setText("Preparing import of %d item(s)…" % len(datas))
        task = tasks.PrepareImportTask(datas, lambda t: self._import_prepared(t, policy, bin_fn, link_only))
        self._keep(task)
        QgsApplication.taskManager().addTask(task)

    def _import_prepared(self, task, policy, bin_fn, link_only=False):
        self._release(task)
        jobs, skipped = [], []
        for i, data in enumerate(task.datas):
            js = self._jobs_from(data, task.details.get(i), policy)
            if js is None:  # cancelled in the layer picker: stop quietly
                self.status.setText("")
                return
            if not js:
                skipped.append(os.path.basename(data["path"].rstrip("/\\")))
            if bin_fn is not None:
                for j in js:
                    j["bin_path"] = bin_fn(j)
            jobs.extend(js)
        self.status.setText("")
        self._run(jobs, skipped, link_only)

    @staticmethod
    def _data_for_path(p):
        return {"path": p, "kind": inspector.guess_kind_from_extension(p),
                "name": os.path.splitext(os.path.basename(p.rstrip("/\\")))[0],
                "details": None, "overrides": {}}

    _LAYER_TYPE_NAMES = {"Vector": "vector", "Raster": "raster", "Mesh": "mesh", "PointCloud": "pointcloud",
                         "VectorTile": "vector-tile", "Plugin": "plugin", "Annotation": "annotation",
                         "Group": "group"}

    def layer_uris(self, datas):
        """QGIS drag URIs for the given items; every layer of a multi-layer file."""
        uris, seen = [], set()
        for data in datas:
            if not data.get("path"):
                continue
            details = [data["details"]] if data.get("details") is not None else []
            if not details:
                self._resolve_details(data)
                if data.get("details") is not None:
                    details = [data["details"]]
                elif not data.get("layer_name") and not inspector.is_delimited_text(data["path"]):
                    try:
                        details = list(inspector.query_sublayers(data["path"]))
                    except Exception as exc:
                        ignore(exc)
            for d in details:
                key = (d.providerKey(), d.uri())
                if key in seen:
                    continue
                seen.add(key)
                lt = getattr(d.type(), "name", str(d.type()))
                u = QgsMimeDataUtils.Uri()  # field-by-field: the constructors differ between QGIS versions
                u.layerType = self._LAYER_TYPE_NAMES.get(lt, "vector")
                u.providerKey = d.providerKey()
                u.name = d.name() or data.get("name", "")
                u.uri = d.uri()
                uris.append(u)
        return uris

    def _open_item(self):
        """Double-click: open a folder; otherwise ingest (Ingest on) or add straight to the map."""
        items = self._selected_items()
        if len(items) == 1 and (items[0].data(0, ROLE) or {}).get("is_folder"):
            self.navigate(items[0].data(0, ROLE)["path"])
            return
        if self.ingest_check.isChecked() and self.settings["mode"] != P.MODE_LINK:
            self.import_selected()
        else:
            self.add_to_map()

    def add_to_map(self):
        """Add the selection to the map right now (no ingest). A file with several layers asks:
        all of them, or only some."""
        datas = self._selected_data()
        if not datas:
            self.iface.messageBar().pushInfo("Data Explorer", "Select one or more datasets to add.")
            return
        parents = {d["path"] for d in datas if d.get("details") is None and not d.get("layer_name")}
        datas = [d for d in datas if (d.get("details") is None and not d.get("layer_name"))
                 or d["path"] not in parents]
        self._prepare_import(datas, "ask", link_only=True)

    def import_selected(self):
        datas = self._selected_data()
        if not datas:
            self.iface.messageBar().pushInfo("Data Explorer", "Select one or more datasets to import.")
            return
        # avoid importing a parent file and its own sublayers twice
        parents = {d["path"] for d in datas if d.get("details") is None and not d.get("layer_name")}
        datas = [d for d in datas if (d.get("details") is None and not d.get("layer_name"))
                 or d["path"] not in parents]
        self._prepare_import(datas, self.settings.get("load_sublayers", "ask"))

    def import_folder(self):
        folder = self._current_folder
        if inspector.is_dataset_folder(folder):
            # Inside a File Geodatabase: import all of its layers as one bin
            name = os.path.splitext(os.path.basename(folder.rstrip("/\\")))[0]
            self._prepare_import([dict(self._data_for_path(folder), name=name)], "all",
                                 bin_fn=lambda j, n=name: [n])
            return
        self.status.setText("Scanning %s…" % folder)
        task = tasks.FolderScanTask(folder, self._folder_scanned)
        self._keep(task)
        QgsApplication.taskManager().addTask(task)

    def _folder_scanned(self, task):
        self._release(task)
        self.status.setText("")
        folder, paths = task.root, task.paths
        if not paths:
            self.iface.messageBar().pushInfo("Data Explorer", "No geodata found in %s" % folder)
            return
        if QMessageBox.question(
                self, "Import Folder as Bin",
                "Import %d dataset(s) from\n%s\n\ninto a group named '%s' (sub-folders become sub-groups)?"
                % (len(paths), folder, os.path.basename(folder))) != QMessageBox.StandardButton.Yes:
            return
        self._prepare_import([self._data_for_path(p) for p in paths], "all",
                             bin_fn=lambda j, f=folder: loader.bin_path_for(
                                 j, {"bin_mode": P.BIN_FOLDER}, root_folder=f))

    def handle_dropped_paths(self, paths):
        if len(paths) == 1 and os.path.isdir(paths[0]) and \
                os.path.splitext(paths[0])[1].lower() not in inspector.DIR_DATASETS:
            self.navigate(paths[0])
            return
        good = [p for p in paths if inspector.is_geodata_candidate(p)]
        bad = [os.path.basename(p) for p in paths if p not in good]
        if bad:
            self.iface.messageBar().pushWarning("Data Explorer", "Not geodata: %s" % ", ".join(bad[:8]))
        if good:
            self._prepare_import([self._data_for_path(p) for p in good],
                                 self.settings.get("load_sublayers", "ask"))

    def _finalize_bins(self, jobs):
        for j in jobs:
            if j.get("bin_path") is not None:
                continue
            if j.get("overrides", {}).get("bin"):
                j["bin_path"] = list(j["overrides"]["bin"])
            else:
                j["bin_path"] = loader.bin_path_for(j, self.settings)
            if j.get("container") and self.settings.get("bin_mode") != P.BIN_NONE:
                j["bin_path"] = j["bin_path"] + [j["container"]]

    def _destination(self):
        dest = self.settings.get("destination")
        if dest:
            return dest
        default_folder = od.project_folder()
        if default_folder:
            return os.path.join(default_folder, "ingest")
        home = QgsProject.instance().homePath()
        if home:
            return os.path.join(home, "ingest")
        return os.path.join(QDir.homePath(), "KgaDataIngest")

    def _run(self, jobs, skipped=None, link_only=False):
        if skipped:
            self.iface.messageBar().pushWarning(
                "Data Explorer", "Not readable by QGIS: %s" % ", ".join(skipped[:8]) +
                (" …" if len(skipped) > 8 else ""))
        if not jobs:
            return
        ingest = (not link_only) and self.ingest_check.isChecked() and self.settings["mode"] != P.MODE_LINK
        if not ingest:
            # Adding to the map, like a drag and drop, puts layers straight in the Layers
            # panel; groups only come from Interpret's bin, Import Folder as Bin or ingest.
            for j in jobs:
                if j.get("bin_path") is None and not j.get("overrides", {}).get("bin"):
                    j["bin_path"] = []
                    j.pop("container", None)
        self._finalize_bins(jobs)
        if not ingest:
            settings = dict(self.settings, mode=P.MODE_LINK)
            results = [{"job": j, "uri": j["uri"], "provider": j["provider"],
                        "notes": ["Linked in place"], "proxy_uri": None, "full_uri": None}
                       for j in jobs]
            # open the layers in the background (big CSV/GeoJSON take seconds)
            task = tasks.LoadLayersTask(results, settings,
                                        lambda t: (self._release(t), self._add_results(t.results, t.settings, [])))
            self._keep(task)
            QgsApplication.taskManager().addTask(task)
            if len(results) > 2:
                self.status.setText("Loading %d layer(s)…" % len(results))
            return
        dest = self._destination()
        if not QgsProject.instance().homePath() and not self.settings.get("destination"):
            self.iface.messageBar().pushInfo(
                "Data Explorer", "Project not saved yet - ingesting to %s" % dest)
        settings = dict(self.settings)
        task = IngestTask(jobs, settings, dest, QgsProject.instance().transformContext(),
                          QgsProject.instance().crs().toWkt(), self._task_done)
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)
        self.iface.messageBar().pushInfo(
            "Data Explorer", "Ingesting %d item(s) in the background (%s). Keep working - "
            "layers appear when ready." % (len(jobs), P.MODE_LABELS[settings["mode"]]))

    def _task_done(self, task, ok):
        if task in self._tasks:
            self._tasks.remove(task)
        if not ok and not task.results:
            self.iface.messageBar().pushWarning("Data Explorer", "Ingest cancelled or failed. "
                                                "See Log Messages > Data Explorer.")
            return
        self._add_results(task.results, task.settings, task.errors)

    def _add_results(self, results, settings, errors):
        added, problems = [], list(errors)
        for res in results:
            layer, err = loader.add_result(res, settings, self.iface,
                                           layer=res.get("layer"), error=res.get("error"))
            if err:
                problems.append(err)
            elif layer is not None:
                added.append(layer)
        if added and settings.get("zoom_to"):
            self._zoom_to(added)
        level = Qgis.MessageLevel.Success if not problems else Qgis.MessageLevel.Warning
        msg = "Imported %d layer(s)." % len(added)
        if problems:
            msg += " %d problem(s): %s" % (len(problems), "; ".join(problems[:3]))
        self.iface.messageBar().pushMessage("Data Explorer", msg, level, 8)

    def _zoom_to(self, layers):
        canvas = self.iface.mapCanvas()
        dest = canvas.mapSettings().destinationCrs()
        from qgis.core import QgsCoordinateTransform
        full = QgsRectangle()
        full.setNull()
        for lyr in layers:
            ext = lyr.extent()
            if ext.isEmpty():
                continue
            try:
                ext = QgsCoordinateTransform(lyr.crs(), dest, QgsProject.instance()).transformBoundingBox(ext)
            except Exception as exc:
                ignore(exc)
                continue
            full.combineExtentWith(ext)
        if not full.isNull() and not full.isEmpty():
            canvas.setExtent(full)
            canvas.refresh()

    # ============================================== settings & proxies
    def _ingest_toggled(self, flag):
        P.set_ingest_enabled(flag)
        self._refresh_ingest_label()

    def _refresh_ingest_label(self):
        on = self.ingest_check.isChecked() and self.settings["mode"] != P.MODE_LINK
        self.import_btn.setText("Ingest" if on else "Import")
        self.import_btn.setToolTip(P.MODE_LABELS[self.settings["mode"]] if on else
                                   "Add selected datasets to the project, reading them in place")

    def open_settings(self):
        dlg = IngestSettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings = dlg.settings()
            P.save_current_settings(self.settings)
            if self.settings["mode"] != P.MODE_LINK and not self.ingest_check.isChecked():
                self.ingest_check.setChecked(True)
            self._fill_presets()
            self._refresh_ingest_label()

    def _preset_chosen(self, idx):
        preset = self.preset_combo.itemData(idx)
        if preset:
            self.settings = P.normalize(preset)
            P.save_current_settings(self.settings)
            self.ingest_check.setChecked(self.settings["mode"] != P.MODE_LINK)
            self._refresh_ingest_label()

    def toggle_proxies(self):
        layers = loader.proxy_layers()
        if not layers:
            self.iface.messageBar().pushInfo(
                "Data Explorer", "No proxy-enabled layers. Ingest rasters with a proxy preset first.")
            return
        n = loader.toggle_proxies(layers)
        using = any(str(lyr.customProperty(loader.PROP_USING_PROXY)).lower() == "true" for lyr in layers)
        self.proxy_btn.setDown(False)
        self.iface.messageBar().pushInfo(
            "Data Explorer", "%d layer(s) now showing %s." % (n, "low-res proxies" if using else "full resolution"))

    # ============================================================ search
    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _set_columns(self, search):
        self.files.setColumnHidden(4, not search)
        self.files.setColumnHidden(5, not search)

    def _search_requested(self):
        return bool(self.search.text().strip()) or self.extent_check.isChecked()

    def refresh(self):
        if self._search_requested():
            self._schedule_search(0)
        else:
            self.populate()

    # ------------------------------------------------------------ auto refresh
    _TRANSIENT = ("-wal", "-shm", "-journal", ".lock", ".tmp")

    def _watch_folder(self, path):
        old = self._watcher.directories()
        if old:
            self._watcher.removePaths(old)
        if path and os.path.isdir(path):
            self._watcher.addPath(path)
        self._folder_signature = self._signature_of(path)

    def _signature_of(self, folder):
        """What the list would show, so a change we cannot see (a lock file) does not refresh it."""
        try:
            return tuple(sorted(
                (e.name, e.is_dir(), e.stat().st_size, int(e.stat().st_mtime))
                for e in os.scandir(folder) if not e.name.lower().endswith(self._TRANSIENT)))
        except OSError:
            return None

    def _auto_refresh(self):
        from qgis.PyQt.QtWidgets import QApplication
        if not self.isVisible() or self._search_active:
            return
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self._auto_refresh_timer.start(700)  # not while the user is clicking or dragging
            return
        sig = self._signature_of(self._current_folder)
        if sig is None or sig == self._folder_signature:
            return
        self._folder_signature = sig
        self.refresh_keep_state()

    def refresh_keep_state(self, select_path=None):
        """Re-read the open folder, keeping what is selected and which files are expanded."""
        inspector.forget_sublayers()
        expanded, selected = set(), set()
        for i in range(self.files.topLevelItemCount()):
            it = self.files.topLevelItem(i)
            data = it.data(0, ROLE) or {}
            if it.isExpanded() and data.get("path"):
                expanded.add(data["path"])
            if it.isSelected() and data.get("path"):
                selected.add(data["path"])
        scroll = self.files.verticalScrollBar().value()
        self.fs_model.refresh_path(self._current_folder)
        self.refresh()
        if self._search_active:
            return
        for i in range(self.files.topLevelItemCount()):
            it = self.files.topLevelItem(i)
            path = (it.data(0, ROLE) or {}).get("path")
            if path in expanded:
                it.setExpanded(True)
            if path in selected or (select_path and path == select_path):
                it.setSelected(True)
                if select_path and path == select_path:
                    self.files.scrollToItem(it)
        if not select_path:
            self.files.verticalScrollBar().setValue(scroll)

    def _refresh_item(self, item):
        """Right-click Refresh on a folder or a database."""
        data = item.data(0, ROLE) or {}
        path = data.get("path")
        if not path:
            return
        if data.get("is_folder"):
            self.fs_model.refresh_path(path)
            self.status.setText("Refreshed %s" % os.path.basename(path))
            return
        inspector.forget_sublayers(path)
        top = item
        while top.parent() is not None:
            top = top.parent()
        was_open = top.isExpanded()
        top.takeChildren()
        if was_open or self._may_have_sublayers(path):
            top.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if was_open:
            self._expand_item(top)
        self.status.setText("Refreshed %s" % os.path.basename(path.rstrip("/\\")))

    def refresh_hard(self):
        """Refresh button: re-scan the folder, and re-index when searching."""
        if self._search_requested():
            self._index_stamp += 1
            self._start_index(self._scope_roots() or [self._current_folder], force=True)
        self.refresh()

    def _schedule_search(self, delay=250, remember=False):
        if remember:
            self._completer_model.setStringList(P.push_recent_search(self.search.text()))
        self._search_timer.start(delay)

    def _scope(self):
        return self.scope_combo.currentData()

    def _scope_roots(self):
        scope = self._scope()
        if scope in ("folder", "tree"):
            return [self._current_folder]
        if scope == "favorites":
            return [f for f in P.favorite_folders() if os.path.isdir(f)]
        return None  # all indexed locations

    def _roots_to_keep_fresh(self):
        roots = self._scope_roots()
        if roots is None:
            status = SI.root_status()
            roots = [r for r in list(status.keys()) + P.extra_index_roots() + P.favorite_folders()
                     if os.path.isdir(r)]
        return roots

    def _ensure_index(self):
        """Start background indexing for stale / never-indexed roots."""
        roots = self._roots_to_keep_fresh()
        if not roots:
            return
        status = SI.root_status()
        now = time.time()
        stale = []
        for r in roots:
            if inspector.is_dataset_folder(r):
                continue
            st = status.get(SI.norm_path(r))
            if st is None or now - (st[0] or 0) > INDEX_STALE_SECONDS:
                stale.append(r)
        if stale:
            self._start_index(stale)

    def _start_index(self, roots, force=False):
        roots = [r for r in roots if r and os.path.isdir(r) and not inspector.is_dataset_folder(r)]
        if not roots:
            return
        if self._index_task is not None:
            if set(SI.norm_path(r) for r in roots) <= self._index_roots and not force:
                return
            try:
                self._index_task.cancel()
            except RuntimeError:
                pass
        task = SI.IndexTask(roots, deep=True, on_done=self._index_done)
        self._index_task = task
        self._index_roots = {SI.norm_path(r) for r in roots}
        QgsApplication.taskManager().addTask(task)
        self._poll_timer.start()

    def _index_done(self, task, ok):
        if task is not self._index_task:
            return
        self._index_task = None
        self._index_roots = set()
        self._poll_timer.stop()
        if self._search_active or self._search_requested():
            self.run_search(from_index=True)

    def _poll_index(self):
        if self._index_task is None:
            self._poll_timer.stop()
            return
        if self._search_requested():
            self.run_search(from_index=True)

    def _extent_changed(self):
        if self.extent_check.isChecked():
            self._schedule_search(400)

    def _canvas_bbox_wgs84(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform
        canvas = self.iface.mapCanvas()
        ext = canvas.extent()
        if ext.isEmpty():
            return None
        try:
            ct = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(),
                                        QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            b = ct.transformBoundingBox(ext)
            return (b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum())
        except Exception:
            return None

    def run_search(self, from_index=False):
        if not self._search_requested():
            if self._search_active:
                self.populate()
            return
        if not from_index:
            self._ensure_index()
        else:
            self._index_stamp += 1  # index content changed: reload records
        scope = self._scope()
        roots = self._scope_roots()
        if scope == "folder" and roots and inspector.is_dataset_folder(roots[0]):
            roots = [os.path.dirname(roots[0].rstrip("/\\"))]
        _label, kinds = FILTERS[self.filter_combo.currentIndex()]
        params = {
            "query": self.search.text().strip(),
            "scope": scope,
            "roots": roots,
            "kinds": kinds,
            "bbox": self._canvas_bbox_wgs84() if self.extent_check.isChecked() else None,
            "stamp": self._index_stamp,
        }
        self._search_gen += 1
        task = SearchTask(params, self._records_cache, self._search_gen, self._search_done)
        self._search_task = task
        if not self._search_active:
            self.status.setText("Searching…")
        QgsApplication.taskManager().addTask(task)

    def _search_done(self, task):
        if task.gen != self._search_gen:
            return  # a newer search superseded this one
        self._search_task = None
        if task.error:
            self.status.setText("Search index unavailable: %s" % task.error)
            return
        if not self._search_requested():
            return
        self._show_results(task.results, task.info, task.params["roots"])

    def _show_results(self, results, info, roots):
        selected = {(d["path"], d.get("layer_name")) for d in self._selected_data()}
        self._search_active = True
        self._set_columns(search=True)
        self.files.setSortingEnabled(False)
        self.files.clear()
        base = roots[0] if roots and len(roots) == 1 else None
        for rec, _score, reasons, layers in results:
            path = rec["path"]
            kind = rec.get("kind") or inspector.guess_kind_from_extension(path)
            folder = os.path.dirname(path.rstrip("/\\"))
            if base:
                rel = os.path.relpath(folder, base)
                location = "." if rel == "." else rel
            else:
                location = folder
            mtime = datetime.datetime.fromtimestamp(rec["mtime"]).strftime("%Y-%m-%d %H:%M") \
                if rec.get("mtime") else ""
            why = "; ".join(dict.fromkeys(SQ.describe_reason(r) for r in reasons if r))
            rec_layers = rec.get("layers", [])
            top_cat = None
            if len(rec_layers) == 1 and not inspector.is_container(path):
                top_cat = ICONS.geometry_category(rec_layers[0].get("geom"))
            type_label = rec.get("container") or ICONS.type_label(kind, top_cat, path)
            name = os.path.basename(path.rstrip("/\\"))
            it = QTreeWidgetItem([name, type_label, _fmt_size(rec.get("size")), mtime, location, why])
            it.setIcon(0, ICONS.file_icon(kind, path, top_cat, top=True))
            it.setToolTip(0, path)
            it.setToolTip(5, why)
            data = {"path": path, "kind": kind, "name": os.path.splitext(name)[0],
                    "details": None, "overrides": {}}
            it.setData(0, ROLE, data)
            show_children = len(rec_layers) > 1 or inspector.is_container(path)
            if show_children:
                for lyr in rec_layers:
                    if layers and lyr.get("name") not in layers:
                        continue
                    lkind = lyr.get("kind") or kind
                    count = lyr.get("count", -1)
                    lcat = ICONS.geometry_category(lyr.get("geom"))
                    child = QTreeWidgetItem([
                        lyr.get("name", ""), ICONS.type_label(lkind, lcat),
                        "{:,} feat.".format(count) if count is not None and count >= 0 else "",
                        "", lyr.get("crs", ""), lyr.get("geom", "")])
                    child.setIcon(0, ICONS.file_icon(lkind, category=lcat))
                    child.setToolTip(0, "%s\nlayer: %s\nfields: %s" % (
                        path, lyr.get("name"), ", ".join(lyr.get("fields", [])[:30])))
                    child.setData(0, ROLE, {"path": path, "kind": lkind, "name": lyr.get("name", ""),
                                            "layer_name": lyr.get("name", ""), "details": None,
                                            "overrides": {}})
                    it.addChild(child)
                    if (path, lyr.get("name")) in selected:
                        child.setSelected(True)
                if layers:
                    it.setExpanded(True)
            elif not rec.get("deep") and self._may_have_sublayers(path):
                it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            self.files.addTopLevelItem(it)
            if (path, None) in selected:
                it.setSelected(True)
        for col in (1, 2, 3):
            self.files.resizeColumnToContents(col)
        msg = []
        if info.get("error"):
            msg.append("⚠ " + info["error"])
        else:
            msg.append("%d result(s)" % info.get("count", len(results)))
            if info.get("count", 0) > len(results):
                msg[-1] += " (showing %d)" % len(results)
            if info.get("fuzzy"):
                msg.append("no exact match – showing similar names")
            msg.append("%d datasets searched" % info.get("total", 0))
        if self._index_task is not None:
            msg.append("indexing… %d%%" % int(self._index_task.progress()))
        self._set_base_status(" • ".join(msg))

    def _show_in_folder(self):
        data = self._selected_data()
        if not data:
            return
        path = data[0]["path"]
        folder = os.path.dirname(path.rstrip("/\\"))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.extent_check.blockSignals(True)
        self.extent_check.setChecked(False)
        self.extent_check.blockSignals(False)
        self._update_filter_button()
        self.navigate(folder)
        for i in range(self.files.topLevelItemCount()):
            it = self.files.topLevelItem(i)
            if (it.data(0, ROLE) or {}).get("path") == path:
                self.files.setCurrentItem(it)
                self.files.scrollToItem(it)
                break

    # ---------------------------------------------------- search menu
    def _fill_search_menu(self):
        m = self.search_menu
        m.clear()
        m.addSection("Search in")
        group = QActionGroup(m)
        for i, (_key, label) in enumerate(SCOPES):
            act = QAction(label, m)
            act.setCheckable(True)
            act.setChecked(i == self.scope_combo.currentIndex())
            act.triggered.connect(lambda _c=False, i=i: self.scope_combo.setCurrentIndex(i))
            group.addAction(act)
            m.addAction(act)
        m.addSeparator()
        extent_act = QAction("Only datasets in the map extent", m)
        extent_act.setCheckable(True)
        extent_act.setChecked(self.extent_check.isChecked())
        extent_act.setToolTip(self.extent_check.toolTip())
        extent_act.toggled.connect(self.extent_check.setChecked)
        m.addAction(extent_act)
        m.addSeparator()
        add_action(m, "Save search as Search Bin…", self.save_search_bin,
                   "/mActionFileSave.svg").setEnabled(self._search_requested())
        bins = P.search_bins()
        if bins:
            m.addSection("Search Bins")
            for b in bins:
                act = add_action(m, b["name"], lambda b=b: self.apply_search_bin(b), "/mActionFolder.svg")
                act.setToolTip(b["query"])
            delm = m.addMenu("Delete Search Bin")
            for b in bins:
                add_action(delm, b["name"], lambda b=b: P.delete_search_bin(b["name"]))
        recent = P.recent_searches()[:8]
        if recent:
            m.addSection("Recent searches")
            for q in recent:
                add_action(m, q, lambda q=q: self.search.setText(q))
        m.addSection("Index")
        total, deep = SI.index_counts()
        info = add_action(m, "%s datasets indexed (%s fully)" % ("{:,}".format(total), "{:,}".format(deep)))
        info.setEnabled(False)
        add_action(m, "Index this folder now", lambda: self._start_index([self._current_folder], force=True))
        add_action(m, "Manage indexed locations…", self.manage_index)
        m.addSeparator()
        add_action(m, "Search syntax (F1)", self.show_search_help, "/mActionHelpContents.svg")

    def _update_filter_button(self):
        """Icon-only button; the current scope and map-extent filter are in its tooltip."""
        tip = "Searching in: %s" % self.scope_combo.currentText()
        if self.extent_check.isChecked():
            tip += "\nOnly datasets in the map extent"
        self.search_menu_btn.setToolTip(tip + "\n\nClick for scope, filters, Search Bins and index")

    def save_search_bin(self):
        from qgis.PyQt.QtWidgets import QInputDialog
        default = self.search.text().strip() or "Map extent"
        name, ok = QInputDialog.getText(self, "Save Search Bin", "Name:", text=default)
        if not ok or not name.strip():
            return
        P.save_search_bin(name.strip(), self.search.text().strip(), self._scope(),
                          self.extent_check.isChecked(), self.filter_combo.currentIndex(),
                          self._current_folder)
        self.iface.messageBar().pushInfo("Data Explorer", "Search Bin '%s' saved." % name.strip())

    def apply_search_bin(self, b):
        widgets = (self.search, self.scope_combo, self.extent_check, self.filter_combo)
        for w in widgets:
            w.blockSignals(True)
        if b.get("folder") and os.path.isdir(b["folder"]) and b.get("scope") in ("folder", "tree"):
            self._current_folder = b["folder"]
            self._fill_path_combo()
        self.search.setText(b.get("query", ""))
        idx = self.scope_combo.findData(b.get("scope", "tree"))
        self.scope_combo.setCurrentIndex(max(idx, 0))
        self.extent_check.setChecked(bool(b.get("extent")))
        self.filter_combo.setCurrentIndex(int(b.get("kind_index", 0)))
        for w in widgets:
            w.blockSignals(False)
        self._update_filter_button()
        self._schedule_search(0)

    def show_search_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Search syntax")
        dlg.resize(620, 560)
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setHtml(SQ.HELP_HTML)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def manage_index(self):
        dlg = IndexManagerDialog(self)
        dlg.exec()
        self._index_stamp += 1
        if dlg.reindex:
            self._start_index(dlg.reindex, force=True)
        self.refresh()

    def closeEvent(self, event):  # noqa: N802
        self._cancel_render()
        super().closeEvent(event)

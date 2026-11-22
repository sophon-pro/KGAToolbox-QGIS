# -*- coding: utf-8 -*-
"""Attributes section: Table (main view), Fields and Statistics.

Table – lazy, provider-sorted attribute table of the layer (All or Selected);
        row selection and map selection follow each other.
Fields – names, aliases, types and visibility from the field definitions.
Statistics – background summary of one field (see stats.py).
"""

from contextlib import suppress

from qgis.core import QgsApplication, QgsExpression, QgsFields, QgsVectorLayer
from qgis.PyQt.QtCore import (QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex,
                              QSortFilterProxyModel, Qt, QTimer)
from qgis.PyQt.QtGui import QGuiApplication, QPalette
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                                 QMenu, QStackedWidget, QTableView, QToolButton,
                                 QVBoxLayout, QWidget)

from . import compat as C
from .new_field import NewFieldForm, can_add_fields
from .stats import StatsView
from .table_model import FeatureTableModel, SortTask
from .ui_common import icon, small_button

SUB_VIEWS = (("table", "Table"), ("fields", "Fields"), ("stats", "Statistics"))
ORIGIN_LABELS = {}


def _origin_label(fields, i):
    try:
        origin = fields.fieldOrigin(i)
    except Exception:
        return ""
    if not ORIGIN_LABELS:
        for name, label in (("OriginProvider", "Provider"), ("OriginJoin", "Joined"),
                            ("OriginEdit", "Edit buffer"), ("OriginExpression", "Virtual")):
            val = C.enum_value(QgsFields, "FieldOrigin", name)
            if val is not None:
                ORIGIN_LABELS[C.as_int(val)] = label
    return ORIGIN_LABELS.get(C.as_int(origin), "")


def hidden_field_names(layer):
    hidden = set()
    with suppress(Exception):
        for col in layer.attributeTableConfig().columns():
            if col.hidden and col.name:
                hidden.add(col.name)
    with suppress(Exception):
        fields = layer.fields()
        for i in range(fields.count()):
            if layer.editorWidgetSetup(i).type() == "Hidden":
                hidden.add(fields.at(i).name())
    return hidden


def _pill(text, tip=""):
    b = QToolButton()
    b.setObjectName("pill")
    b.setText(text)
    b.setCheckable(True)
    b.setToolTip(tip)
    return b


# ====================================================================== fields
class FieldsModel(QAbstractTableModel):
    COL_NAME, COL_ALIAS, COL_TYPE = range(3)
    HEADERS = ("Field", "Alias", "Type")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self._muted = None

    def set_layer(self, layer):
        self.beginResetModel()
        self.rows = []
        if isinstance(layer, QgsVectorLayer):
            fields = layer.fields()
            hidden = hidden_field_names(layer)
            for i in range(fields.count()):
                f = fields.at(i)
                tname = f.typeName()
                if not tname and hasattr(f, "friendlyTypeString"):
                    tname = f.friendlyTypeString()
                length, prec = f.length(), f.precision()
                if length > 0:
                    tname = "{}({}{})".format(tname, length, ",{}".format(prec) if prec > 0 else "")
                self.rows.append(dict(name=f.name(), alias=f.alias(), type=tname or "?",
                                      origin=_origin_label(fields, i), hidden=f.name() in hidden,
                                      comment=f.comment()))
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=C.DisplayRole):  # noqa: N802
        if orientation == C.Horizontal and role == C.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=C.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        col = index.column()
        if role == C.DisplayRole:
            if col == self.COL_NAME:
                return r["name"] + ("  (hidden)" if r["hidden"] else "")
            return r["alias"] if col == self.COL_ALIAS else r["type"]
        if role == C.ToolTipRole:
            tip = ["<b>{}</b>".format(r["name"])]
            if r["alias"]:
                tip.append("Alias: {}".format(r["alias"]))
            tip.append("Type: {}".format(r["type"]))
            if r["origin"]:
                tip.append("Source: {}".format(r["origin"]))
            tip.append("Visible in attribute table: {}".format("no" if r["hidden"] else "yes"))
            if r["comment"]:
                tip.append("Comment: {}".format(r["comment"]))
            return "<br>".join(tip)
        if role == C.ForegroundRole and (col == self.COL_TYPE or (col == self.COL_NAME and r["hidden"])):
            if self._muted is None:
                self._muted = QGuiApplication.palette().brush(C.enum_value(QPalette, "ColorGroup", "Disabled"),
                                                              C.enum_value(QPalette, "ColorRole", "Text"))
            return self._muted
        if role == C.UserRole:
            return r["name"]
        return None


class FieldFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""

    def set_text(self, text):
        self.text = (text or "").strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        if not self.text:
            return True
        r = self.sourceModel().rows[row]
        hay = "{} {} {}".format(r["name"], r["alias"], r["type"]).lower()
        return all(tok in hay for tok in self.text.split())


def _table_view(name):
    v = QTableView()
    v.setObjectName(name)
    v.verticalHeader().setVisible(False)
    v.verticalHeader().setDefaultSectionSize(24)
    v.setEditTriggers(C.enum_value(QAbstractItemView, "EditTrigger", "NoEditTriggers"))
    v.setSelectionBehavior(C.enum_value(QAbstractItemView, "SelectionBehavior", "SelectRows"))
    v.setWordWrap(False)
    v.setTextElideMode(C.ElideRight)
    v.setAlternatingRowColors(True)
    v.setShowGrid(False)
    v.horizontalHeader().setHighlightSections(False)
    v.horizontalHeader().setDefaultAlignment(C.AlignLeft | C.AlignVCenter)
    v.setContextMenuPolicy(C.CustomContextMenu)
    # the Layer Bag has a fixed maximum width: wide tables scroll sideways, smoothly
    v.setHorizontalScrollMode(C.enum_value(QAbstractItemView, "ScrollMode", "ScrollPerPixel"))
    v.setHorizontalScrollBarPolicy(C.enum_value(Qt, "ScrollBarPolicy", "ScrollBarAsNeeded"))
    return v


# ====================================================================== section
class AttributesSection(QWidget):
    def __init__(self, iface, watcher, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.watcher = watcher
        self.layer = None
        self._counts = None
        self._syncing = False
        self._dirty = dict(table=True, fields=True, stats=True)
        self.stats_info = dict(selection_syncs=0)
        self.on_view_changed = None      # set by the dock (smart size)
        self.natural_widths = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # --- sub navigation + native table
        top = QHBoxLayout()
        top.setSpacing(5)
        self.pill_group = QButtonGroup(self)
        self.pills = {}
        for i, (key, label) in enumerate(SUB_VIEWS):
            b = _pill(label)
            self.pill_group.addButton(b, i)
            self.pills[key] = b
            top.addWidget(b)
        top.addStretch(1)
        self.table_btn = small_button(icon("table"), "Open the native QGIS attribute table (editing, forms, "
                                      "field calculator)", self.open_table)
        top.addWidget(self.table_btn)
        lay.addLayout(top)

        # --- counts
        counts = QHBoxLayout()
        counts.setSpacing(5)
        self.count_lbl = QLabel()
        self.count_lbl.setObjectName("chip")
        self.sel_lbl = QLabel()
        self.sel_lbl.setObjectName("chip")
        self.fields_lbl = QLabel()
        self.fields_lbl.setObjectName("chip")
        for w in (self.count_lbl, self.sel_lbl, self.fields_lbl):
            counts.addWidget(w)
            if w is self.sel_lbl:
                self.clear_sel_btn = QToolButton()
                self.clear_sel_btn.setObjectName("textButton")
                self.clear_sel_btn.setText("✕ Clear")
                self.clear_sel_btn.setToolTip("Clear the selection of this layer")
                self.clear_sel_btn.clicked.connect(self.clear_selection)
                self.clear_sel_btn.hide()
                counts.addWidget(self.clear_sel_btn)
        counts.addStretch(1)
        lay.addLayout(counts)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self._build_table()
        self._build_fields()
        self.stats_view = StatsView(iface)
        self.stack.addWidget(self.stats_view)

        if hasattr(self.pill_group, "idClicked"):
            self.pill_group.idClicked.connect(self.show_view)
        else:
            self.pill_group.buttonClicked[int].connect(self.show_view)
        self.pills["table"].setChecked(True)

    # ------------------------------------------------------------------ table view
    def _build_table(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        bar.setSpacing(5)
        self.mode_group = QButtonGroup(self)
        self.all_pill = _pill("All", "Show all features")
        self.sel_pill = _pill("Selected", "Show only the selected features")
        self.mode_group.addButton(self.all_pill)
        self.mode_group.addButton(self.sel_pill)
        self.all_pill.setChecked(True)
        bar.addWidget(self.all_pill)
        bar.addWidget(self.sel_pill)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter rows…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setToolTip("Shows rows whose text fields contain this (or numeric fields equal it).\n"
                               "Filtering is done by the data provider.")
        bar.addWidget(self.filter, 1)
        self.zoom_btn = small_button(icon("zoom"), "Zoom to the selected features", self.zoom_selected)
        bar.addWidget(self.zoom_btn)
        lay.addLayout(bar)

        self.tmodel = FeatureTableModel(self)
        self.tview = _table_view("featureTable")
        self.tview.setModel(self.tmodel)
        self.tview.setSortingEnabled(False)
        self.tview.horizontalHeader().setSectionsClickable(True)
        self.tview.horizontalHeader().sectionClicked.connect(self._header_clicked)
        self.tview.horizontalHeader().setStretchLastSection(True)
        self.tview.selectionModel().selectionChanged.connect(self._rows_selected)
        self.tview.doubleClicked.connect(lambda idx: self.zoom_fids([self.tmodel.fid_at(idx.row())]))
        self.tview.customContextMenuRequested.connect(self._table_menu)
        self.tmodel.rowsInserted.connect(lambda *_: (self._update_status(), self._mark_selected_rows()))
        self.tmodel.modelReset.connect(lambda *_: (self._update_status(), self._fit_columns(), self._notify_view()))
        lay.addWidget(self.tview, 1)
        self.table_status = QLabel()
        self.table_status.setObjectName("muted")
        self.table_status.linkActivated.connect(self._status_link)
        self._sort_task = None
        lay.addWidget(self.table_status)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(350)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.filter.textChanged.connect(lambda *_: self._filter_timer.start())
        self.all_pill.toggled.connect(lambda on: on and self._set_mode(FeatureTableModel.MODE_ALL))
        self.sel_pill.toggled.connect(lambda on: on and self._set_mode(FeatureTableModel.MODE_SELECTED))
        self._sort = (None, True)
        self.stack.addWidget(w)

    def _fit_columns(self):
        """Column widths from the header and the first rows only (cheap with 100+ fields)."""
        hh = self.tview.horizontalHeader()
        rm = C.enum_value(QHeaderView, "ResizeMode", "Interactive")
        fm = self.tview.fontMetrics()
        hfm = hh.fontMetrics()
        sample = self.tmodel.rows[:40]
        self.natural_widths = []
        for c in range(self.tmodel.columnCount()):
            hh.setSectionResizeMode(c, rm)
            title = self.tmodel.headerData(c, C.Horizontal) or ""
            w = hfm.horizontalAdvance(title) + 22
            for _fid, vals in sample:
                w = max(w, fm.horizontalAdvance(vals[c][0][:40]) + 14)
            w = max(56, min(220, w))
            self.natural_widths.append(w)
            hh.resizeSection(c, w)

    def _set_mode(self, mode):
        self.cancel_sort()
        self.tmodel.set_mode(mode)
        self._sort = (None, True)
        self.tview.horizontalHeader().setSortIndicatorShown(False)
        self._mark_selected_rows()

    def _header_clicked(self, col):
        strategy = self.tmodel.sort_strategy()
        if strategy is None:
            self.iface.messageBar().pushMessage(
                "Attributes", "This selection is too large to sort here – the native attribute table can.",
                C.MSG_INFO, 3)
            return
        last, asc = self._sort
        asc = not asc if last == col else True
        self._sort = (col, asc)
        order = C.enum_value(Qt, "SortOrder", "AscendingOrder" if asc else "DescendingOrder")
        hh = self.tview.horizontalHeader()
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(col, order)
        if strategy == "provider":
            self.cancel_sort()
            self.tmodel.sort(col, order)
            return
        self._start_background_sort(col, asc)

    # --- background sort (providers that cannot sort large tables quickly)
    def _start_background_sort(self, col, asc):
        self.cancel_sort()
        lyr = self.layer
        fids = lyr.selectedFeatureIds() if self.tmodel.mode == self.tmodel.MODE_SELECTED else None
        idx = self.tmodel.columns[col][0]
        task = SortTask(lyr, idx, asc, fids=fids, filter_expression=self.tmodel.filter_expression,
                        on_done=self._sort_done)
        task.progressChanged.connect(self._sort_progress)
        self._sort_task = (task, col, asc)
        self.table_status.setText("Sorting {:,} rows… <a href='cancel'>Cancel</a>".format(task.total))
        QgsApplication.taskManager().addTask(task)

    def _sort_progress(self, v):
        if self._sort_task is not None:
            self.table_status.setText("Sorting {:,} rows… {:.0f}%  <a href='cancel'>Cancel</a>".format(
                self._sort_task[0].total, v))

    def _sort_done(self, task, ok):
        if self._sort_task is None or self._sort_task[0] is not task:
            return
        _t, col, asc = self._sort_task
        self._sort_task = None
        if ok and task.order is not None and self.layer is not None:
            self.tmodel.set_order_ids(col, asc, task.order)
        else:
            self.tview.horizontalHeader().setSortIndicatorShown(False)
            self._sort = (None, True)
            self._update_status()

    def cancel_sort(self):
        st = getattr(self, "_sort_task", None)
        self._sort_task = None
        if st is not None:
            with suppress(RuntimeError):
                st[0].on_done = None
                st[0].cancel()

    def _status_link(self, link):
        if link == "cancel":
            self.cancel_sort()
            self.tview.horizontalHeader().setSortIndicatorShown(False)
            self._sort = (None, True)
            self._update_status()

    def filter_expression_for(self, text):
        lyr = self.layer
        if lyr is None or not text:
            return None
        parts = []
        esc = text.replace("'", "''")
        for i in range(lyr.fields().count()):
            f = lyr.fields().at(i)
            q = QgsExpression.quotedColumnRef(f.name())
            tname = f.typeName().lower()
            if f.isNumeric():
                with suppress(ValueError):
                    float(text)
                    parts.append("{} = {}".format(q, text))
            elif any(k in tname for k in ("char", "string", "text")):
                parts.append("{} ILIKE '%{}%'".format(q, esc))
        return " OR ".join(parts) if parts else "FALSE"

    def _apply_filter(self):
        self.cancel_sort()
        self._sort = (None, True)
        self.tview.horizontalHeader().setSortIndicatorShown(False)
        self.tmodel.order = None
        self.tmodel.filter_expression = self.filter_expression_for(self.filter.text().strip())
        self.tmodel.reload()

    def _update_status(self):
        m = self.tmodel
        if self.layer is None:
            self.table_status.setText("")
            return
        loaded = m.rowCount()
        more = m.canFetchMore()
        if m.mode == m.MODE_SELECTED and loaded == 0 and not more:
            self.table_status.setText("No features selected – select some on the map.")
            return
        if m.filter_expression:
            self.table_status.setText("{:,}{} matching row{}".format(loaded, "+" if more else "",
                                                                      "" if loaded == 1 else "s"))
            return
        total = m.expected_rows()
        if more and total > 0:
            self.table_status.setText("Showing {:,} of {:,}".format(loaded, total))
            self.table_status.setToolTip("More rows load as you scroll")
        else:
            self.table_status.setText("{:,} row{}".format(loaded, "" if loaded == 1 else "s"))

    def _rows_selected(self, *_):
        """Rows picked in the table select those features on the map (All mode)."""
        if self._syncing or self.layer is None:
            return
        fids = [self.tmodel.fid_at(i.row()) for i in self.tview.selectionModel().selectedRows()]
        fids = [f for f in fids if f is not None]
        if self.tmodel.mode == self.tmodel.MODE_SELECTED:
            if fids:
                self.flash_fids(fids[:50])
            return
        self._syncing = True
        try:
            self.layer.selectByIds(fids)
            self.stats_info["selection_syncs"] += 1
        finally:
            self._syncing = False

    def _mark_selected_rows(self):
        """Highlight the loaded rows whose features are selected on the map."""
        if self.layer is None or self.tmodel.mode != self.tmodel.MODE_ALL:
            return
        sm = self.tview.selectionModel()
        self._syncing = True
        try:
            n = self.layer.selectedFeatureCount()
            if n == 0:
                sm.clearSelection()
                return
            ids = self.layer.selectedFeatureIds()
            if len(self.tmodel.rows) < n:
                hits = [r for r, (fid, _v) in enumerate(self.tmodel.rows) if fid in ids]
            else:
                hits = [self.tmodel.row_of[f] for f in ids if f in self.tmodel.row_of]
            sel = QItemSelection()
            last_col = max(0, self.tmodel.columnCount() - 1)
            for r in hits:
                sel.select(self.tmodel.index(r, 0), self.tmodel.index(r, last_col))
            sm.select(sel, C.enum_value(QItemSelectionModel, "SelectionFlag", "ClearAndSelect"))
        finally:
            self._syncing = False

    def _table_menu(self, pos):
        idx = self.tview.indexAt(pos)
        if not idx.isValid():
            return
        rows = sorted({i.row() for i in self.tview.selectionModel().selectedRows()} | {idx.row()})
        fids = [self.tmodel.fid_at(r) for r in rows]
        m = QMenu(self)
        m.addAction(icon("copy"), "Copy value",
                    lambda: self._copy(self.tmodel.rows[idx.row()][1][idx.column()][1]))
        m.addAction(icon("copy"), "Copy row{}".format("s" if len(rows) > 1 else ""),
                    lambda: self._copy(self._rows_text(rows)))
        m.addAction(icon("copy"), "Copy column name", lambda: self._copy(self.tmodel.columns[idx.column()][1]))
        m.addSeparator()
        m.addAction(icon("zoom"), "Zoom to feature{}".format("s" if len(fids) > 1 else ""),
                    lambda: self.zoom_fids(fids))
        m.addAction(icon("flash"), "Flash on the map", lambda: self.flash_fids(fids))
        m.addSeparator()
        name = self.tmodel.columns[idx.column()][1]
        m.addAction("Use “{}” as label field".format(name), lambda: self._use_label(name))
        m.addAction("Statistics of “{}”".format(name), lambda: self._stats_for(name))
        exec_ = getattr(m, "exec", None) or m.exec_
        exec_(self.tview.viewport().mapToGlobal(pos))

    def _rows_text(self, rows):
        header = "\t".join(c[1] for c in self.tmodel.columns)
        lines = ["\t".join(v[1].replace("\t", " ").replace("\n", " ") for v in self.tmodel.rows[r][1])
                 for r in rows]
        return header + "\n" + "\n".join(lines)

    # ------------------------------------------------------------------ fields view
    def _build_fields(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search fields…")
        self.search.setClearButtonEnabled(True)
        top.addWidget(self.search, 1)
        self.new_field_btn = QToolButton()
        self.new_field_btn.setObjectName("textButton")
        self.new_field_btn.setText("+ New field")
        self.new_field_btn.setToolTip("Add a new field to this layer")
        self.new_field_btn.clicked.connect(self.open_new_field)
        top.addWidget(self.new_field_btn)
        lay.addLayout(top)
        self.new_field = NewFieldForm(on_added=self._field_added)
        lay.addWidget(self.new_field)
        self.model = FieldsModel(self)
        self.proxy = FieldFilter(self)
        self.proxy.setSourceModel(self.model)
        self.view = _table_view("fieldTable")
        self.view.setModel(self.proxy)
        hh = self.view.horizontalHeader()
        hh.setStretchLastSection(True)
        rm = C.enum_value(QHeaderView, "ResizeMode", "Interactive")
        for c, wdt in ((0, 140), (1, 100)):
            hh.setSectionResizeMode(c, rm)
            hh.resizeSection(c, wdt)
        self.view.customContextMenuRequested.connect(self._fields_menu)
        lay.addWidget(self.view, 1)
        self.search.textChanged.connect(self.proxy.set_text)
        self.stack.addWidget(w)

    # --- new field
    def open_new_field(self):
        if self.layer is None:
            return
        self.new_field.open_for(self.layer)
        self._notify_view()

    def _field_added(self, name, msg):
        self.iface.messageBar().pushMessage("Fields", msg, C.MSG_SUCCESS, 4)
        self.refresh_fields()
        for r, row in enumerate(self.model.rows):
            if row["name"] == name:
                idx = self.proxy.mapFromSource(self.model.index(r, 0))
                self.view.selectRow(idx.row())
                self.view.scrollTo(idx)
                break
        self._notify_view()

    def editing_changed(self):
        """Editing mode toggled: refresh what the new-field form says."""
        self._update_new_field_btn()
        if self.new_field.isVisible() and self.layer is not None:
            name, alias, key = self.new_field.name.text(), self.new_field.alias.text(), self.new_field.type.currentData()
            self.new_field.open_for(self.layer)
            self.new_field.name.setText(name)
            self.new_field.alias.setText(alias)
            i = self.new_field.type.findData(key)
            if i >= 0:
                self.new_field.type.setCurrentIndex(i)

    def _update_new_field_btn(self):
        ok, why = can_add_fields(self.layer) if self.layer is not None else (False, "")
        self.new_field_btn.setEnabled(ok)
        self.new_field_btn.setToolTip("Add a new field to this layer" if ok else why)

    def _fields_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.view.selectionModel().selectedRows()} |
                      {self.proxy.mapToSource(idx).row()})
        names = [self.model.rows[r]["name"] for r in rows]
        m = QMenu(self)
        m.addAction(icon("copy"), "Copy field name" + ("s" if len(names) > 1 else ""),
                    lambda: self._copy("\n".join(names)))
        if len(names) == 1:
            m.addAction("Use as label field", lambda: self._use_label(names[0]))
            m.addAction("Statistics", lambda: self._stats_for(names[0]))
        exec_ = getattr(m, "exec", None) or m.exec_
        exec_(self.view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ navigation
    def current_view(self):
        return SUB_VIEWS[self.stack.currentIndex()][0]

    def show_view(self, index):
        self.stack.setCurrentIndex(index)
        self.pills[SUB_VIEWS[index][0]].setChecked(True)
        self.table_btn.setVisible(index == 0)       # the native-table shortcut belongs to the Table view
        self.ensure_current()
        self._notify_view()

    def _notify_view(self):
        if self.on_view_changed is not None:
            self.on_view_changed()

    def ensure_current(self):
        key = self.current_view()
        if not self._dirty.get(key):
            return
        self._dirty[key] = False
        if key == "table":
            self.tmodel.filter_expression = None
            self.filter.blockSignals(True)
            self.filter.clear()
            self.filter.blockSignals(False)
            self.tmodel.set_layer(self.layer)
            self._fit_columns()
            self._mark_selected_rows()
        elif key == "fields":
            self.model.set_layer(self.layer)
            self._update_new_field_btn()
        elif key == "stats":
            self.stats_view.set_layer(self.layer)

    # ------------------------------------------------------------------ layer
    def set_layer(self, layer):
        self.new_field.close_form()
        self.cancel_sort()
        self._sort = (None, True)
        self.tview.horizontalHeader().setSortIndicatorShown(False)
        self.layer = layer if isinstance(layer, QgsVectorLayer) else None
        self._dirty = dict(table=True, fields=True, stats=True)
        if self.layer is None:
            self.tmodel.set_layer(None)
            self.model.set_layer(None)
            self.stats_view.set_layer(None)
        self.refresh_counts()
        if self.isVisible() or self.layer is None:
            self.ensure_current()
        for w in (self.table_btn, self.filter, self.search, self.zoom_btn):
            w.setEnabled(self.layer is not None)

    def refresh_fields(self):
        self._dirty = dict(table=True, fields=True, stats=True)
        self.refresh_counts()
        if self.isVisible():
            self.ensure_current()

    def refresh_counts(self):
        lyr = self.layer
        if lyr is None:
            self._counts = None
        else:
            n = lyr.featureCount()
            self._counts = (n if n is not None else -1, lyr.selectedFeatureCount(), lyr.fields().count(),
                            lyr.providerType())
        self._render_counts()

    def _render_counts(self):
        c = self._counts
        if c is None:
            for w in (self.count_lbl, self.sel_lbl, self.fields_lbl):
                w.setText("")
                w.hide()
            self.clear_sel_btn.hide()
            return
        n, sel, nfields, provider = c
        narrow = self.width() < 330
        self.count_lbl.setText("? features" if n < 0 else "{:,}{}".format(n, "" if narrow else " features"))
        self.count_lbl.setToolTip("Feature count reported by the {} provider".format(provider))
        self.sel_lbl.setText("{:,} sel.".format(sel) if narrow else "{:,} selected".format(sel))
        name = "chipAccent" if sel else "chip"
        if self.sel_lbl.objectName() != name:
            self.sel_lbl.setObjectName(name)
            self.sel_lbl.style().unpolish(self.sel_lbl)
            self.sel_lbl.style().polish(self.sel_lbl)
        self.fields_lbl.setText("{} fields".format(nfields))
        self.count_lbl.show()
        self.sel_lbl.show()
        self.clear_sel_btn.setVisible(sel > 0)
        self.clear_sel_btn.setText("✕" if narrow else "✕ Clear")
        self.fields_lbl.setVisible(not narrow)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._render_counts()   # layout only: never touches the layer
        narrow = self.width() < 330
        self.pills["stats"].setText("Stats" if narrow else "Statistics")
        self.pills["stats"].setToolTip("Statistics")

    def refresh_selection(self):
        """Map selection changed."""
        self.refresh_counts()
        if self.layer is not None:
            self.stats_view.refresh_selection()
        if self._syncing or self.layer is None:
            return
        if self.tmodel.mode == self.tmodel.MODE_SELECTED:
            if self.isVisible() and self.current_view() == "table" and not self._dirty["table"]:
                self.tmodel.reload()
            else:
                self._dirty["table"] = True
        elif not self._dirty["table"]:
            self._mark_selected_rows()

    def refresh_data(self):
        """Features added / deleted / edits committed."""
        self.refresh_counts()
        self._dirty["table"] = self._dirty["stats"] = True
        if self.isVisible():
            self.ensure_current()

    # ------------------------------------------------------------------ actions
    def open_table(self):
        if self.layer is not None:
            self.iface.showAttributeTable(self.layer)

    def clear_selection(self):
        if self.layer is not None and self.layer.selectedFeatureCount():
            self.layer.removeSelection()

    def zoom_selected(self):
        if self.layer is not None and self.layer.selectedFeatureCount():
            self.iface.mapCanvas().zoomToSelected(self.layer)

    def zoom_fids(self, fids):
        fids = [f for f in fids if f is not None]
        if self.layer is not None and fids and self.layer.isSpatial():
            self.iface.mapCanvas().zoomToFeatureIds(self.layer, fids)
            self.iface.mapCanvas().flashFeatureIds(self.layer, fids)

    def flash_fids(self, fids):
        fids = [f for f in fids if f is not None]
        if self.layer is not None and fids and self.layer.isSpatial():
            self.iface.mapCanvas().flashFeatureIds(self.layer, fids)

    def _use_label(self, name):
        w = self.parentWidget()
        while w is not None and not hasattr(w, "use_label_field"):
            w = w.parentWidget()
        hook = w.use_label_field if w is not None else getattr(self, "use_label_field", None)
        if hook is not None:
            hook(name)

    def _stats_for(self, name):
        self.show_view(2)
        self.stats_view.field.setField(name)
        self.stats_view.calculate()

    def _copy(self, text):
        QGuiApplication.clipboard().setText(text)
        self.iface.statusBarIface().showMessage("Copied to clipboard", 2000)

    def set_palette(self, p):
        self.stats_view.set_palette(p)

    def cleanup(self):
        self.cancel_sort()
        self.stats_view.cleanup()
        self.tmodel.cleanup()

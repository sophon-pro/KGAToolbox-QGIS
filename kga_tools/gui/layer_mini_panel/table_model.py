# -*- coding: utf-8 -*-
"""Lazy attribute table model.

Rows are read in chunks only as the user scrolls (Qt's canFetchMore/fetchMore),
without geometry and with only the shown columns, so opening the table on a
million-feature layer reads 200 features, not a million.

Sorting is passed to the data provider (ORDER BY). Providers that cannot sort
themselves would make QGIS read and sort the whole layer in memory, so for those
sorting is offered only up to SORT_LIMIT features.
"""

from contextlib import suppress

from array import array

from qgis.core import (NULL, QgsExpression, QgsFeature, QgsFeatureRequest, QgsTask, QgsVectorLayer,
                       QgsVectorLayerFeatureSource)
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QFont, QGuiApplication, QPalette

from ...core.compat import scoped
from . import compat as C

CHUNK = 200
SORT_LIMIT = 100000           # client-side sorting limit for providers that cannot ORDER BY
FIDS_FILTER_LIMIT = 2000      # above this, a selection is read in id chunks (no giant IN-lists)
MAX_TEXT = 200
BACKGROUND_SORT_LIMIT = 3000000   # background sort keeps one value per feature in memory
FAST_SORT_PROVIDERS = ("spatialite",)


def provider_can_sort(layer):
    """Providers measured to sort large tables quickly themselves (SQLite-based)."""
    p = layer.providerType()
    if p in FAST_SORT_PROVIDERS:
        return True
    if p == "ogr":
        try:
            st = (layer.dataProvider().storageType() or "").lower()
        except Exception:
            st = ""
        return any(k in st for k in ("gpkg", "geopackage", "sqlite"))
    return False


def _sort_key(v):
    """(group, value): NULLs last, numbers before text, text case-insensitive."""
    if v is None or v == NULL:
        return (2, 0)
    with suppress(Exception):
        if hasattr(v, "isNull") and v.isNull():
            return (2, 0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, v)
    if hasattr(v, "toString") and not isinstance(v, str):
        try:
            return (1, v.toString(Qt.DateFormat.ISODate))
        except Exception:
            return (1, str(v))
    return (1, str(v).lower())


class SortTask(QgsTask):
    """Reads one column (no geometry) through a thread-safe feature source,
    sorts the feature ids by it and returns them in that order."""

    def __init__(self, layer, field_index, ascending, fids=None, filter_expression=None, on_done=None):
        super().__init__("Sorting {}".format(layer.name()), scoped(QgsTask, 'Flag', 'CanCancel'))
        self.source = QgsVectorLayerFeatureSource(layer)
        self.idx = field_index
        self.ascending = ascending
        self.fids = fids
        self.filter_expression = filter_expression
        n = len(fids) if fids is not None else layer.featureCount()
        self.total = max(1, n if n and n > 0 else 1)
        self.order = None
        self.on_done = on_done

    def run(self):
        req = QgsFeatureRequest()
        req.setFlags(C.no_geometry_flag())
        req.setSubsetOfAttributes([self.idx])
        if self.fids is not None:
            req.setFilterFids(self.fids)
        if self.filter_expression:
            req.setFilterExpression(self.filter_expression)
        pairs = []
        f = QgsFeature()
        it = self.source.getFeatures(req)
        k = 0
        while it.nextFeature(f):
            k += 1
            if k % 5000 == 0:
                if self.isCanceled():
                    return False
                self.setProgress(min(90.0, 90.0 * k / self.total))
            pairs.append((_sort_key(f.attributes()[self.idx]), f.id()))
        try:
            nulls = [p for p in pairs if p[0][0] == 2]
            vals = [p for p in pairs if p[0][0] != 2]
            vals.sort(key=lambda p: p[0], reverse=not self.ascending)
        except TypeError:          # mixed, incomparable values: compare as text
            vals = [((1, str(p[0][1])), p[1]) for p in pairs if p[0][0] != 2]
            nulls = [p for p in pairs if p[0][0] == 2]
            vals.sort(key=lambda p: p[0], reverse=not self.ascending)
        self.order = array("q", [p[1] for p in vals] + [p[1] for p in nulls])   # NULLs always last
        self.setProgress(100.0)
        return True

    def finished(self, ok):
        if self.on_done is not None:
            self.on_done(self, ok)


def format_value(value):
    """(display text, full text, is_null)."""
    if value is None or value == NULL:
        return "NULL", "NULL", True
    with suppress(Exception):
        if hasattr(value, "isNull") and value.isNull():
            return "NULL", "NULL", True
    if hasattr(value, "toString") and not isinstance(value, str):
        try:
            text = value.toString(C.enum_value(Qt, "DateFormat", "ISODate"))
        except Exception:
            text = value.toString()
    elif isinstance(value, (bytes, bytearray)):
        text = "<binary, {} bytes>".format(len(value))
    elif isinstance(value, float):
        text = repr(value) if abs(value) >= 1e15 or (value and abs(value) < 1e-6) else "{:.10g}".format(value)
    else:
        text = str(value)
    short = text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"
    return short.replace("\n", " ⏎ "), text, False


class FeatureTableModel(QAbstractTableModel):
    MODE_ALL, MODE_SELECTED = "all", "selected"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer = None
        self.mode = self.MODE_ALL
        self.columns = []          # [(field index, name, alias)]
        self.rows = []             # [(fid, [(display, full, is_null), ...])]
        self.row_of = {}           # fid -> row
        self.order = None          # (column index, ascending)
        self.filter_expression = None
        self._it = None
        self._ids = None           # id list read in chunks (big selection or background sort)
        self._ids_pos = 0
        self._done = True
        self.reads = 0             # features read (for tests / benchmarks)
        self._muted = None

    # ------------------------------------------------------------------ setup
    def set_layer(self, layer, mode=None):
        self.layer = layer if isinstance(layer, QgsVectorLayer) else None
        if mode is not None:
            self.mode = mode
        self.order = None
        self.reload()

    def set_mode(self, mode):
        if mode != self.mode:
            self.mode = mode
            self.order = None
            self.reload()

    def visible_columns(self, layer):
        hidden = set()
        with suppress(Exception):
            for col in layer.attributeTableConfig().columns():
                if col.hidden and col.name:
                    hidden.add(col.name)
        fields = layer.fields()
        out = []
        for i in range(fields.count()):
            f = fields.at(i)
            if f.name() in hidden:
                continue
            with suppress(Exception):
                if layer.editorWidgetSetup(i).type() == "Hidden":
                    continue
            out.append((i, f.name(), f.alias()))
        return out

    def sort_strategy(self):
        """'provider' (fast, here and now), 'background' (task) or None."""
        lyr = self.layer
        if lyr is None or not self.columns:
            return None
        n = self.expected_rows() if not self.filter_expression else lyr.featureCount()
        if self.mode == self.MODE_SELECTED:
            n = lyr.selectedFeatureCount()
        if 0 <= n <= SORT_LIMIT and not (self.mode == self.MODE_SELECTED and n > FIDS_FILTER_LIMIT):
            return "provider"
        if provider_can_sort(lyr) and self.mode == self.MODE_ALL:
            return "provider"
        if n > BACKGROUND_SORT_LIMIT:
            return None
        return "background"

    def set_order_ids(self, column, ascending, ids):
        """Show rows in a precomputed order (from SortTask)."""
        self.order = (column, ascending)
        self.beginResetModel()
        self._close()
        self.rows = []
        self.row_of = {}
        self._done = len(ids) == 0
        self._ids = ids
        self._ids_pos = 0
        self._ordered_ids = True
        self.endResetModel()
        self.fetchMore(QModelIndex())

    def can_sort(self):
        return self.sort_strategy() is not None

    def expected_rows(self):
        lyr = self.layer
        if lyr is None:
            return 0
        if self.filter_expression:
            return -1
        if self.mode == self.MODE_SELECTED:
            return lyr.selectedFeatureCount()
        n = lyr.featureCount()
        return n if n is not None and n >= 0 else -1

    def reload(self):
        if getattr(self, "_ordered_ids", False):
            self.order = None       # a precomputed order is stale after a reload
        self.beginResetModel()
        self._close()
        self.rows = []
        self.row_of = {}
        lyr = self.layer
        self.columns = self.visible_columns(lyr) if lyr is not None else []
        self._done = lyr is None
        if lyr is not None:
            self._open()
        self.endResetModel()
        if lyr is not None:
            self.fetchMore(QModelIndex())

    def _request(self, ordered=True):
        req = QgsFeatureRequest()
        req.setFlags(C.no_geometry_flag())
        req.setSubsetOfAttributes([c[0] for c in self.columns])
        if self.filter_expression:
            req.setFilterExpression(self.filter_expression)
        if ordered and self.order is not None:
            col, asc = self.order
            name = self.columns[col][1]
            req.addOrderBy(QgsExpression.quotedColumnRef(name), asc)
        return req

    def _open(self):
        lyr = self.layer
        self._ids = None
        self._ids_pos = 0
        self._ordered_ids = False
        if self.mode == self.MODE_SELECTED:
            n = lyr.selectedFeatureCount()
            if n == 0:
                self._done = True
                return
            ids = lyr.selectedFeatureIds()
            if n <= FIDS_FILTER_LIMIT:
                req = self._request()
                req.setFilterFids(ids)
                self._it = lyr.getFeatures(req)
            else:  # big selection: read it id-chunk by id-chunk
                self._ids = sorted(ids)
            return
        self._it = lyr.getFeatures(self._request())

    def _close(self):
        if self._it is not None:
            with suppress(Exception):
                self._it.close()
        self._it = None
        self._ids = None

    def cleanup(self):
        self._close()
        self.layer = None

    # ------------------------------------------------------------------ lazy loading
    def canFetchMore(self, parent=QModelIndex()):  # noqa: N802
        return not parent.isValid() and not self._done

    def fetchMore(self, parent=QModelIndex()):  # noqa: N802
        if parent.isValid() or self._done or self.layer is None:
            return
        new = []
        f = QgsFeature()
        if self._ids is not None:
            chunk = self._ids[self._ids_pos:self._ids_pos + CHUNK]
            self._ids_pos += len(chunk)
            if chunk:
                req = self._request(ordered=False)
                req.setFilterFids(list(chunk))
                it = self.layer.getFeatures(req)
                got = {}
                while it.nextFeature(f):
                    got[f.id()] = self._row(f)
                it.close()
                new.extend(got[fid] for fid in chunk if fid in got)   # keep the chunk's order
            if self._ids_pos >= len(self._ids):
                self._done = True
        elif self._it is not None:
            while len(new) < CHUNK and self._it.nextFeature(f):
                new.append(self._row(f))
            if len(new) < CHUNK:
                self._done = True
                self._close()
        else:
            self._done = True
        if new:
            start = len(self.rows)
            self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
            for i, r in enumerate(new):
                self.row_of[r[0]] = start + i
            self.rows.extend(new)
            self.endInsertRows()

    def fetch_all_for_test(self):
        while self.canFetchMore():
            self.fetchMore()

    def _row(self, f):
        self.reads += 1
        attrs = f.attributes()
        return (f.id(), [format_value(attrs[c[0]] if c[0] < len(attrs) else None) for c in self.columns])

    # ------------------------------------------------------------------ Qt model API
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=C.DisplayRole):  # noqa: N802
        if orientation == C.Horizontal and 0 <= section < len(self.columns):
            _i, name, alias = self.columns[section]
            if role == C.DisplayRole:
                return alias or name
            if role == C.ToolTipRole:
                tip = name + (" (alias: {})".format(alias) if alias else "")
                if not self.can_sort():
                    tip += "\nSorting this large layer: use the native attribute table"
                return tip
        return None

    def data(self, index, role=C.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        fid, vals = self.rows[index.row()]
        disp, full, is_null = vals[index.column()]
        if role == C.DisplayRole:
            return disp
        if role == C.ToolTipRole:
            return full if len(full) < 2000 else full[:2000] + "…"
        if role == C.ForegroundRole and is_null:
            if self._muted is None:
                self._muted = QGuiApplication.palette().brush(C.enum_value(QPalette, "ColorGroup", "Disabled"),
                                                              C.enum_value(QPalette, "ColorRole", "Text"))
            return self._muted
        if role == C.FontRole and is_null:
            ft = QFont()
            ft.setItalic(True)
            return ft
        if role == C.UserRole:
            return fid
        return None

    def sort(self, column, order=None):
        if self.layer is None or not (0 <= column < len(self.columns)) or not self.can_sort():
            return
        asc = order != C.enum_value(Qt, "SortOrder", "DescendingOrder")
        self.order = (column, asc)
        self.beginResetModel()
        self._close()
        self.rows = []
        self.row_of = {}
        self._done = False
        self._open()
        self.endResetModel()
        self.fetchMore(QModelIndex())

    def fid_at(self, row):
        return self.rows[row][0] if 0 <= row < len(self.rows) else None

    def full_row(self, row):
        return [(c[1], self.rows[row][1][k][1]) for k, c in enumerate(self.columns)]

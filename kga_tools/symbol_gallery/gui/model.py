# -*- coding: utf-8 -*-
"""Lightweight list model: only thumbnails the view actually paints are rendered."""
from qgis.PyQt.QtCore import Qt, QAbstractListModel, QModelIndex, QSize
from qgis.PyQt.QtGui import QPixmap, QColor, QIcon

from ..core.catalog import KIND_LABELS

ROLE_ID = Qt.ItemDataRole.UserRole + 1


class GalleryModel(QAbstractListModel):
    def __init__(self, thumbs, parent=None):
        super().__init__(parent)
        self._thumbs = thumbs
        self._rows = []           # list of Entry
        self._row_of = {}         # entry id -> row
        self.icon_size = QSize(96, 65)
        self.background = "light"
        self.dpr = 1.0
        self._placeholder = None
        self._icons = {}          # entry id -> QIcon (for current size/bg)
        thumbs.ready.connect(self._on_ready)

    # ----------------------------------------------------------- content
    def set_entries(self, entries):
        self.beginResetModel()
        self._rows = list(entries)
        self._row_of = {e.id: i for i, e in enumerate(self._rows)}
        self.endResetModel()

    def entries(self):
        return list(self._rows)

    def entry(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_of(self, entry_id):
        return self._row_of.get(entry_id, -1)

    def set_appearance(self, size, background, dpr):
        changed = (size != self.icon_size or background != self.background or dpr != self.dpr)
        self.icon_size = QSize(size)
        self.background = background
        self.dpr = dpr
        if changed:
            self._icons.clear()
            self._placeholder = None
            self._thumbs.clear_queue()
            if self._rows:
                self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1),
                                      [Qt.ItemDataRole.DecorationRole])

    # -------------------------------------------------------------- Qt API
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return e.name
        if role == Qt.ItemDataRole.DecorationRole:
            icon = self._icons.get(e.id)
            if icon is not None:
                return icon
            pm = self._thumbs.get(e, self.icon_size, self.background, self.dpr)
            if pm is None:
                return self._placeholder_icon()
            icon = QIcon(pm)
            self._icons[e.id] = icon
            return icon
        if role == Qt.ItemDataRole.ToolTipRole:
            return ("<b>%s</b><br>%s · %s<br>%s<br><i>Double-click to apply</i>"
                    % (_esc(e.name), _esc(e.category), KIND_LABELS[e.kind],
                       _esc(e.description)))
        if role == ROLE_ID:
            return e.id
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------ internals
    def _placeholder_icon(self):
        if self._placeholder is None:
            pm = QPixmap(max(1, int(self.icon_size.width() * self.dpr)),
                         max(1, int(self.icon_size.height() * self.dpr)))
            pm.fill(QColor(128, 128, 128, 18))
            pm.setDevicePixelRatio(self.dpr)
            self._placeholder = QIcon(pm)
        return self._placeholder

    def _on_ready(self, entry_id):
        row = self._row_of.get(entry_id, -1)
        if row >= 0:
            self._icons.pop(entry_id, None)
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

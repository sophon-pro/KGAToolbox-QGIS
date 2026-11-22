# -*- coding: utf-8 -*-
"""
Layer Path Manager - Qt item models and delegates.

LayerTableModel     inspection table (checkable, colour-coded, sortable)
LayerFilterProxy    search text + status / type / provider filters
ProposalTableModel  preview of proposed source changes (editable path)
PathDiffDelegate    highlights the part of a path that changes
"""

from typing import List, Optional

from qgis.PyQt.QtCore import (
    QAbstractTableModel, QModelIndex, QRectF, QSize, QSortFilterProxyModel, Qt, pyqtSignal,
)
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap, QTextDocument
from qgis.PyQt.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .utils import (
    STATUS_BROKEN, STATUS_REMOTE, STATUS_UNSUPPORTED, STATUS_VALID, ChangeProposal,
    LayerRecord, theme_icon,
)

STATUS_COLORS = {
    STATUS_VALID: QColor(46, 160, 67),
    STATUS_BROKEN: QColor(207, 34, 46),
    STATUS_REMOTE: QColor(31, 111, 235),
    STATUS_UNSUPPORTED: QColor(130, 130, 130),
}
VALIDATION_COLORS = {
    "OK": QColor(46, 160, 67), "Warning": QColor(212, 140, 10), "Failed": QColor(207, 34, 46),
}

SortRole = Qt.ItemDataRole.UserRole + 1
RecordRole = Qt.ItemDataRole.UserRole + 2
ComparePathRole = Qt.ItemDataRole.UserRole + 3


def status_icon(color: QColor, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(color.darker(130))
    p.setBrush(color)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


_ICON_CACHE = {}


def _cached_icon(key, factory):
    if key not in _ICON_CACHE:
        _ICON_CACHE[key] = factory()
    return _ICON_CACHE[key]


def layer_icon(rec: LayerRecord) -> QIcon:
    g = rec.geometry.lower()
    if rec.layer_type == "Raster":
        name = "mIconRasterLayer.svg"
        if rec.source_kind == "Remote service":
            name = "mIconXyz.svg" if "xyz" in rec.source.lower() else "mIconWms.svg"
    elif rec.layer_type == "Vector":
        if rec.provider == "postgres":
            name = "mIconPostgis.svg"
        elif g.startswith("point"):
            name = "mIconPointLayer.svg"
        elif g.startswith("line"):
            name = "mIconLineLayer.svg"
        elif g.startswith("polygon"):
            name = "mIconPolygonLayer.svg"
        elif g.startswith("no geometry"):
            name = "mIconTableLayer.svg"
        else:
            name = "mIconGeometryCollectionLayer.svg"
    elif rec.layer_type == "Mesh":
        name = "mIconMeshLayer.svg"
    elif rec.layer_type == "Point cloud":
        name = "mIconPointCloudLayer.svg"
    else:
        name = "mIconLayerTree.svg"
    return _cached_icon(name, lambda: theme_icon(name))


# ======================================================================
# Inspection table
# ======================================================================


class LayerTableModel(QAbstractTableModel):
    COLUMNS = [
        ("name", "Layer"), ("status", "Status"), ("file_name", "File"), ("folder", "Folder"),
        ("layer_type", "Layer Type"),
        ("provider", "Provider"), ("geometry", "Geometry"), ("file_format", "Format"),
        ("path_storage", "Path Mode"), ("path", "File Path"),
        ("error", "Problem"), ("source", "Data Source URI"), ("notes", "Notes"),
        ("source_kind", "Source Kind"), ("layer_id", "Layer ID"),
    ]
    COL = {key: i for i, (key, _l) in enumerate(COLUMNS)}

    checkedChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: List[LayerRecord] = []
        self.checked = set()

    # -------------------------------------------------------------
    def set_records(self, records: List[LayerRecord], keep_checks=True):
        self.beginResetModel()
        ids = {r.layer_id for r in records}
        self.checked = (self.checked & ids) if keep_checks else set()
        self.records = records
        self.endResetModel()
        self.checkedChanged.emit()

    def record(self, row) -> Optional[LayerRecord]:
        return self.records[row] if 0 <= row < len(self.records) else None

    def checked_records(self) -> List[LayerRecord]:
        return [r for r in self.records if r.layer_id in self.checked]

    def set_checked(self, ids, state=True):
        ids = set(ids)
        if state:
            self.checked |= ids
        else:
            self.checked -= ids
        if self.records:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.records) - 1, 0),
                                  [Qt.ItemDataRole.CheckStateRole])
        self.checkedChanged.emit()

    # -------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section][1]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        rec = self.records[index.row()]
        key = self.COLUMNS[index.column()][0]
        R = Qt.ItemDataRole
        if role in (R.DisplayRole, R.EditRole):
            val = getattr(rec, key)
            if key == "status" and rec.repaired and rec.status == STATUS_VALID:
                return "Repaired"
            if key == "path_storage" and val == "N/A":
                return "-"
            return val
        if role == SortRole:
            if key == "status":
                order = {STATUS_BROKEN: 0, STATUS_REMOTE: 2, STATUS_UNSUPPORTED: 3, STATUS_VALID: 1}
                return order.get(rec.status, 9)
            return str(getattr(rec, key)).lower()
        if role == R.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if rec.layer_id in self.checked else Qt.CheckState.Unchecked
        if role == R.DecorationRole:
            if key == "name":
                return layer_icon(rec)
            if key == "status":
                return _cached_icon("st_" + rec.status,
                                    lambda: status_icon(STATUS_COLORS.get(rec.status, QColor("gray"))))
        if role == R.ForegroundRole:
            if key in ("status", "error") and rec.status == STATUS_BROKEN:
                return QBrush(STATUS_COLORS[STATUS_BROKEN])
            if key == "status":
                return QBrush(STATUS_COLORS.get(rec.status, QColor("gray")).darker(110))
        if role == R.BackgroundRole and rec.status == STATUS_BROKEN:
            c = QColor(STATUS_COLORS[STATUS_BROKEN])
            c.setAlpha(28)
            return QBrush(c)
        if role == R.FontRole:
            if key == "status" or (key == "name" and rec.repaired):
                f = QFont()
                f.setBold(True)
                return f
        if role == R.ToolTipRole:
            lines = [f"<b>{rec.name}</b> &mdash; {rec.status}",
                     f"Provider: {rec.provider} &nbsp; Type: {rec.layer_type}",
                     f"Source: {rec.source}"]
            if rec.path:
                lines.append(f"Path: {rec.path}")
                if rec.stored_path and rec.stored_path != rec.path:
                    lines.append(f"Stored in project as: {rec.stored_path}")
            if rec.sublayer:
                lines.append(f"Sublayer: {rec.sublayer}")
            if rec.group_path:
                lines.append(f"Group: {rec.group_path}")
            if rec.error:
                lines.append(f"<span style='color:#cf222e'>Error: {rec.error}</span>")
            if rec.notes:
                lines.append(f"Notes: {rec.notes}")
            return "<br>".join(lines)
        if role == RecordRole:
            return rec
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            rec = self.records[index.row()]
            checked = value in (Qt.CheckState.Checked, 2, Qt.CheckState.Checked.value
                                if hasattr(Qt.CheckState.Checked, "value") else 2)
            if checked:
                self.checked.add(rec.layer_id)
            else:
                self.checked.discard(rec.layer_id)
            self.dataChanged.emit(index, index, [role])
            self.checkedChanged.emit()
            return True
        return False


class LayerFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.status = ""
        self.layer_type = ""
        self.provider = ""
        self.problems = False
        self.folder = None          # None = any folder; "" = non-file layers
        self.setSortRole(SortRole)
        self.setDynamicSortFilter(False)

    def set_filters(self, text=None, status=None, layer_type=None, provider=None, problems=None,
                    folder=False):
        if folder is not False:
            self.folder = folder
        if problems is not None:
            self.problems = problems
        if text is not None:
            self.text = text.strip().lower()
        if status is not None:
            self.status = status
        if layer_type is not None:
            self.layer_type = layer_type
        if provider is not None:
            self.provider = provider
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        rec = self.sourceModel().record(row)
        if rec is None:
            return False
        if self.problems and rec.status not in (STATUS_BROKEN, "Not checked"):
            return False
        if self.folder is not None and rec.folder != self.folder:
            return False
        if self.status and rec.status != self.status:
            return False
        if self.layer_type:
            if self.layer_type == "Other":
                if rec.layer_type in ("Vector", "Raster"):
                    return False
            elif rec.layer_type != self.layer_type:
                return False
        if self.provider and rec.provider != self.provider:
            return False
        if self.text:
            hay = " ".join((rec.name, rec.path, rec.source, rec.provider, rec.error,
                            rec.file_format, rec.group_path, rec.layer_id)).lower()
            return all(tok in hay for tok in self.text.split())
        return True


# ======================================================================
# Preview table
# ======================================================================


def friendly_match(match_type: str) -> str:
    """Short, plain-language label for a matcher result."""
    mt = match_type or ""
    if mt.startswith("Ambiguous"):
        n = "".join(ch for ch in mt.split("-")[-1] if ch.isdigit())
        return f"Choose one of {n} files" if n else "Choose a file"
    table = [
        ("Filename + folder structure (", "Same name, folders decide"),
        ("Filename + folder structure", "Same name & folders"),
        ("Exact filename", "Same name"),
        ("Filename (case differs)", "Same name (case differs)"),
        ("Extension variant", "Same name, other extension"),
        ("Fuzzy filename", "Similar name"),
        ("Root folder substitution - target missing", "Not found in new folder"),
        ("Root folder substitution", "Same sub-folders"),
        ("Manual selection", "Chosen by you"),
        ("Candidate chosen by user", "Chosen by you"),
        ("Folder assigned by user", "Chosen by you"),
        ("No match", "Not found"),
        ("Not found in chosen folder", "Not found in that folder"),
        ("Not under the old root folder", "Not under the old folder"),
        ("Cleared by user", "Won't change"),
        ("Same file as now", "Same file as now - no change"),
    ]
    for key, label in table:
        if mt.startswith(key):
            return label
    return mt


class ProposalTableModel(QAbstractTableModel):
    COLUMNS = ["", "Layer", "Current Path", "New Path", "Match", "Confidence", "Check"]
    C_APPLY, C_LAYER, C_ORIG, C_NEW, C_MATCH, C_CONF, C_VALID = range(7)

    proposalEdited = pyqtSignal(int)   # row whose path was typed by the user
    acceptedChanged = pyqtSignal()

    def __init__(self, proposals: List[ChangeProposal], editor_callback=None, parent=None):
        super().__init__(parent)
        self.proposals = proposals
        self.editor_callback = editor_callback

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.proposals)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == self.C_APPLY:
            p = self.proposals[index.row()]
            if p.new_source:
                f |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() == self.C_NEW:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def refresh_row(self, row):
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def refresh_all(self):
        if self.proposals:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.proposals) - 1, self.columnCount() - 1))
        self.acceptedChanged.emit()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        p = self.proposals[index.row()]
        c = index.column()
        R = Qt.ItemDataRole
        if role == R.CheckStateRole and c == self.C_APPLY:
            return Qt.CheckState.Checked if p.accepted else Qt.CheckState.Unchecked
        if role in (R.DisplayRole, R.EditRole):
            if c == self.C_LAYER:
                return p.layer_name
            if c == self.C_ORIG:
                return p.original_path
            if c == self.C_NEW:
                if not p.new_path and role == R.DisplayRole:
                    if p.candidates:
                        return f"<choose one of {len(p.candidates)} candidates>"
                    return "<no replacement>"
                return p.new_path
            if c == self.C_MATCH:
                label = friendly_match(p.match_type)
                if p.new_path and p.match_type and p.confidence < 100:
                    return f"{label} \u00b7 {p.confidence}%"
                return label
            if c == self.C_CONF:
                return f"{p.confidence}%" if p.new_path else "-"
            if c == self.C_VALID:
                if not p.new_source:
                    return ""
                if not p.validation_state:
                    return "Not checked yet"
                if p.validation_state == "OK":
                    return "Opens fine"
                msg = p.validation_message.replace("Readable; ", "")
                return msg
        if role == ComparePathRole:
            if c == self.C_NEW:
                return p.original_path
            if c == self.C_ORIG:
                return p.new_path
        if role == R.ForegroundRole:
            if c == self.C_VALID and p.validation_state:
                return QBrush(VALIDATION_COLORS.get(p.validation_state, QColor("gray")))
            if c == self.C_NEW and not p.new_path:
                return QBrush(QColor(207, 34, 46) if not p.candidates else QColor(212, 140, 10))
            if c == self.C_CONF and p.new_path:
                col = QColor(46, 160, 67) if p.confidence >= 80 else \
                    QColor(212, 140, 10) if p.confidence >= 50 else QColor(207, 34, 46)
                return QBrush(col)
        if role == R.BackgroundRole and p.needs_review and p.new_source != p.original_source:
            c2 = QColor(212, 140, 10)
            c2.setAlpha(26)
            return QBrush(c2)
        if role == R.DecorationRole and c == self.C_VALID and p.validation_state:
            return _cached_icon("val_" + p.validation_state, lambda: status_icon(
                VALIDATION_COLORS.get(p.validation_state, QColor("gray"))))
        if role == R.ToolTipRole:
            tip = [f"<b>{p.layer_name}</b>", f"Original source: {p.original_source}",
                   f"Proposed source: {p.new_source or '-'}"]
            if p.candidates:
                tip.append(f"Candidates ({len(p.candidates)}):<br>&nbsp;" +
                           "<br>&nbsp;".join(p.candidates[:12]))
            if p.validation_message:
                tip.append(f"Validation: {p.validation_message}")
            return "<br>".join(tip)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        p = self.proposals[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == self.C_APPLY:
            p.accepted = value in (Qt.CheckState.Checked, 2) and bool(p.new_source)
            self.refresh_row(index.row())
            self.acceptedChanged.emit()
            return True
        if role == Qt.ItemDataRole.EditRole and index.column() == self.C_NEW:
            text = str(value).strip().strip('"')
            if text == p.new_path:
                return False
            if self.editor_callback:
                self.editor_callback(p, text)
            self.refresh_row(index.row())
            self.proposalEdited.emit(index.row())
            self.acceptedChanged.emit()
            return True
        return False


# ======================================================================
# Delegate highlighting changed part of a path
# ======================================================================


def _split_diff(a: str, b: str):
    """Split ``a`` into (common prefix, changed middle, common suffix) versus ``b``.

    Prefix and suffix are cut on '/' boundaries so whole folder names are
    highlighted, e.g. '/old/root/' + 'data_2020' + '/roads.shp'.
    """
    if not a or not b:
        return "", a, ""
    pa, pb = a.split("/"), b.split("/")
    i = 0
    while i < min(len(pa), len(pb)) - 1 and pa[i].lower() == pb[i].lower():
        i += 1
    j = 0
    while j < min(len(pa), len(pb)) - i and pa[-1 - j].lower() == pb[-1 - j].lower():
        j += 1
    if i + j >= len(pa):          # identical (or a is contained in b)
        return a, "", ""
    prefix = "/".join(pa[:i]) + ("/" if i else "")
    middle = "/".join(pa[i:len(pa) - j])
    suffix = ("/" if j else "") + "/".join(pa[len(pa) - j:])
    return prefix, middle, suffix


class MiddleElideDelegate(QStyledItemDelegate):
    """Elides long paths in the middle so the file name stays visible."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideMiddle


class PathDiffDelegate(QStyledItemDelegate):
    """Draws the unchanged prefix muted and the changed tail emphasised."""

    def __init__(self, emphasis: QColor, strike=False, parent=None):
        super().__init__(parent)
        self.emphasis = emphasis
        self.strike = strike

    def paint(self, painter, option, index):
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        other = index.data(ComparePathRole) or ""
        if not other or not text or text.startswith("<"):
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        common, rest, suffix = _split_diff(text, other)
        import html
        from qgis.PyQt.QtGui import QFontMetrics
        # Fit into the cell: collapse the *unchanged* prefix first, then the
        # unchanged suffix, so the changed part always stays visible.
        avail = opt.rect.width() - 12
        bold = QFont(opt.font)
        bold.setWeight(QFont.Weight.DemiBold)
        fm, fmb = QFontMetrics(opt.font), QFontMetrics(bold)
        ell = "…"

        def width():
            return (fm.horizontalAdvance(common) + fmb.horizontalAdvance(rest)
                    + fm.horizontalAdvance(suffix))

        if width() > avail and common:
            room = avail - fmb.horizontalAdvance(rest) - fm.horizontalAdvance(suffix) \
                - fm.horizontalAdvance(ell)
            tail = common
            while tail and fm.horizontalAdvance(tail) > max(0, room):
                tail = tail[1:]
            common = ell + tail
        if width() > avail and suffix:
            suffix = fm.elidedText(suffix, Qt.TextElideMode.ElideLeft,
                                   max(fm.horizontalAdvance(ell * 2),
                                       avail - fm.horizontalAdvance(common)
                                       - fmb.horizontalAdvance(rest)))
        if width() > avail:
            rest = fmb.elidedText(rest, Qt.TextElideMode.ElideMiddle,
                                  max(10, avail - fm.horizontalAdvance(common)
                                      - fm.horizontalAdvance(suffix)))
        muted = opt.palette.color(opt.palette.ColorGroup.Normal, opt.palette.ColorRole.Text)
        if opt.state & QStyle.StateFlag.State_Selected:
            muted = opt.palette.color(opt.palette.ColorRole.HighlightedText)
            emph = muted
        else:
            emph = self.emphasis
        deco = "line-through" if self.strike else "none"
        html_text = (f"<span style='color:{muted.name()}'>{html.escape(common)}</span>"
                     f"<span style='color:{emph.name()};font-weight:600;text-decoration:{deco}'>"
                     f"{html.escape(rest)}</span>"
                     f"<span style='color:{muted.name()}'>{html.escape(suffix)}</span>")
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(2)
        doc.setHtml(html_text)
        painter.save()
        rect = opt.rect.adjusted(3, 0, -3, 0)
        painter.translate(rect.left(), rect.top() + max(0, (rect.height() - doc.size().height()) / 2))
        doc.drawContents(painter, QRectF(0, 0, rect.width(), rect.height()))
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 22))

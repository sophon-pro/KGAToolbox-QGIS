"""
The field list of the Attributes pane: model, in-place editors and the table view.

One model serves one *target*: a layer plus the feature ids being edited. One feature = its values.
Several features (a layer node in the pane) = "multi-edit": a value shared by all is shown, a
differing value is blank, and typing a value sets it on every feature.

Values are written to the layer's normal edit buffer (undo with Ctrl+Z, Save Edits / Discard Edits).
With Auto Apply off, edits wait in the model until `apply_pending()` (Apply) or `cancel_pending()`.
"""

from contextlib import suppress

from qgis.core import (
    NULL,
    QgsApplication,
    QgsFeatureRequest,
    QgsFieldConstraints,
    QgsVectorLayer,
    QgsVectorLayerUtils,
)
from qgis.gui import QgsDateTimeEdit
from qgis.PyQt.QtCore import (
    QAbstractTableModel,
    QDate,
    QDateTime,
    QModelIndex,
    Qt,
    QTime,
    QTimer,
    pyqtSignal,
)
from qgis.PyQt.QtGui import QBrush, QColor, QPainter
from qgis.PyQt.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QComboBox,
    QLineEdit,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
)

from ...core.compat import (INTEGER_TYPES, T_BOOL, T_DATE, T_DATETIME, T_DOUBLE, T_LONGLONG, T_STRING, T_TIME,
                            enum_value)
from .style import ERROR_BG, ERROR_TEXT, NULL_TEXT, PENDING_TEXT, READONLY_TEXT

ROLE_EDITABLE = Qt.ItemDataRole.UserRole + 10
ROLE_PENDING = Qt.ItemDataRole.UserRole + 11
ROLE_ERROR = Qt.ItemDataRole.UserRole + 12

MULTI = object()                 # "the selected features differ here"
VALUEMAP_NULL = "{2839923C-8B7D-417C-9F29-4BA4AD1D4B50}"     # QGIS's marker for a NULL entry in a value map
MAX_FEATURES = 100000            # most features one multi-edit will read
READONLY_NAMES = frozenset(("objectid", "shape_length", "shape_area", "shape_leng", "shape_len", "globalid"))
INT32 = (-2 ** 31, 2 ** 31 - 1)


def is_null(value):
    if value is None or value is MULTI:
        return True
    try:
        return bool(value.isNull())              # QVariant
    except AttributeError:
        return False


def _widget_type(setup):
    return setup.type() if setup is not None else ""


class FieldInfo:
    """Everything the pane needs to know about one field of the target layer."""

    def __init__(self, layer, idx, pk):
        field = layer.fields().field(idx)
        setup = layer.editorWidgetSetup(idx)
        self.idx = idx
        self.field = field
        self.name = field.name()
        self.label = field.displayName() or field.name()
        self.setup_type = _widget_type(setup)
        self.config = dict(setup.config()) if setup is not None else {}
        self.type_id = int(field.type())
        self.hidden = self.setup_type == "Hidden"
        self.not_null = bool(field.constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull)
        origin_ok = True
        with suppress(Exception):
            origin_ok = layer.fields().fieldOrigin(idx) in _editable_origins()
        self.editable = (
            origin_ok
            and idx not in pk
            and self.name.lower() not in READONLY_NAMES
            and not layer.editFormConfig().readOnly(idx)
        )
        self.formatter = QgsApplication.fieldFormatterRegistry().fieldFormatter(self.setup_type)
        self.cache = None
        with suppress(Exception):
            self.cache = self.formatter.createCache(layer, idx, self.config)
        self.kind = self._kind()

    def _kind(self):
        t = self.type_id
        if self.setup_type == "ValueMap":
            return "valuemap"
        if self.setup_type == "ValueRelation":
            return "valuerelation"
        if self.setup_type == "CheckBox":
            return "checkbox"
        if t == T_BOOL:
            return "bool"
        if t in (T_DATE, T_TIME, T_DATETIME):
            return "temporal"
        if t in INTEGER_TYPES:
            return "int"
        if t == T_DOUBLE or self.field.isNumeric():
            return "double"
        return "text"

    # -- choices for drop-down editors: [(text, value)] ------------------------------------------
    def choices(self, layer):
        out = []
        if self.kind == "valuemap":
            for entry in self.config.get("map", []) or []:
                if isinstance(entry, dict):
                    for text, value in entry.items():
                        out.append((str(text), None if value == VALUEMAP_NULL else value))
        elif self.kind == "valuerelation":
            with suppress(Exception):
                from qgis.core import QgsValueRelationFieldFormatter
                items = QgsValueRelationFieldFormatter.createCache(self.config)
                out = [(str(i.value), i.key) for i in items]
        elif self.kind == "checkbox":
            checked = self.config.get("CheckedState") or "1"
            unchecked = self.config.get("UncheckedState") or "0"
            out = [(str(checked), checked), (str(unchecked), unchecked)]
        elif self.kind == "bool":
            out = [("True", True), ("False", False)]
        if not self.not_null and not any(v is None for _t, v in out):
            out.insert(0, ("<Null>", None))
        return out

    def display(self, layer, value):
        if is_null(value):
            return "<Null>"
        with suppress(Exception):
            return self.formatter.representValue(layer, self.idx, self.config, self.cache, value)
        return str(value)

    def range_limits(self):
        if self.setup_type == "Range":
            with suppress(Exception):
                return float(self.config["Min"]), float(self.config["Max"])
        return None


def _editable_origins():
    from qgis.core import Qgis, QgsFields
    return tuple(enum_value((Qgis, 'FieldOrigin.' + name), (QgsFields, 'FieldOrigin.Origin' + name),
                            (QgsFields, 'Origin' + name)) for name in ('Provider', 'Edit'))


def layer_pk_indexes(layer):
    pk = set()
    with suppress(Exception):
        pk.update(layer.primaryKeyAttributes())
    with suppress(Exception):
        pk.update(layer.dataProvider().pkAttributeIndexes())
    return pk


def ensure_editing(layer):
    """Put a layer in edit mode if it is not. Returns True when it can take edits now."""
    if layer.isEditable():
        return True
    if not layer.supportsEditing():
        return False
    return bool(layer.startEditing())


# ----------------------------------------------------------------------------------------- model
class AttributeModel(QAbstractTableModel):
    """Two columns: field name | value. Rows are the visible fields of the target layer."""

    pendingChanged = pyqtSignal(bool)
    editingStarted = pyqtSignal(object)        # a layer was put into edit mode by an edit here
    valuesWritten = pyqtSignal()
    problem = pyqtSignal(str)
    before_edit = None             # callable(layer), run just before an edit is written (may turn Edit Session on)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer = None
        self.fids = []
        self.rows = []                 # [FieldInfo]
        self.auto_apply = True
        self._values = []              # per row: shared value, None (NULL) or MULTI
        self._pending = {}             # field idx -> value
        self._invalid = {}             # field idx -> (message, text typed)

    # ------------------------------------------------------------------ target
    def set_target(self, layer, fids):
        self.beginResetModel()
        self.layer = layer
        self.fids = list(fids)[:MAX_FEATURES]
        self._pending.clear()
        self._invalid.clear()
        self.rows = []
        if isinstance(layer, QgsVectorLayer):
            pk = layer_pk_indexes(layer)
            self.rows = [r for r in (FieldInfo(layer, i, pk) for i in range(layer.fields().count()))
                         if not r.hidden]
        self._read_values()
        self.endResetModel()
        self.pendingChanged.emit(False)

    def clear(self):
        self.set_target(None, [])

    def refresh(self):
        """Re-read the values (the layer changed underneath us) without losing pending edits."""
        if self.layer is None:
            return
        self._read_values()
        if self.rows:
            self.dataChanged.emit(self.index(0, 1), self.index(len(self.rows) - 1, 1))

    def _read_values(self):
        self._values = [None] * len(self.rows)
        if not self.rows or not self.fids:
            return
        request = QgsFeatureRequest().setFilterFids(self.fids).setFlags(QgsFeatureRequest.Flag.NoGeometry)
        first = True
        shared = [None] * len(self.rows)
        for feature in self.layer.getFeatures(request):
            attrs = feature.attributes()
            if first:
                shared = [None if is_null(attrs[r.idx]) else attrs[r.idx] for r in self.rows]
                first = False
                continue
            for n, r in enumerate(self.rows):
                if shared[n] is MULTI:
                    continue
                v = None if is_null(attrs[r.idx]) else attrs[r.idx]
                if v != shared[n]:
                    shared[n] = MULTI
        self._values = shared

    def info(self, row):
        return self.rows[row]

    def layer_is_writable(self):
        return self.layer is not None and (self.layer.isEditable() or self.layer.supportsEditing())

    def is_multi(self):
        return len(self.fids) > 1

    # ------------------------------------------------------------------ Qt model
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 2

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 1 and self.rows[index.row()].editable and self.layer_is_writable():
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def current_value(self, row):
        info = self.rows[row]
        if info.idx in self._pending:
            return self._pending[info.idx]
        return self._values[row]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        info = self.rows[row]
        editable = info.editable and self.layer_is_writable()
        invalid = self._invalid.get(info.idx)
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return info.label
            if invalid:
                return invalid[1]
            value = self.current_value(row)
            if value is MULTI:
                return ""
            return info.display(self.layer, value)
        if role == Qt.ItemDataRole.EditRole and col == 1:
            value = self.current_value(row)
            return None if value is MULTI else value
        if role == Qt.ItemDataRole.ForegroundRole:
            if invalid and col == 1:
                return QBrush(QColor(ERROR_TEXT))
            if not editable:
                return QBrush(QColor(READONLY_TEXT))
            if col == 1:
                value = self.current_value(row)
                if info.idx in self._pending:
                    return QBrush(QColor(PENDING_TEXT))
                if is_null(value) and value is not MULTI:
                    return QBrush(QColor(NULL_TEXT))
            return None
        if role == Qt.ItemDataRole.BackgroundRole:
            if invalid and col == 1:
                return QBrush(QColor(ERROR_BG))
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            if invalid:
                return invalid[0]
            if col == 1 and self.current_value(row) is MULTI:
                return "The selected features have different values here. Type a value to set it on all of them."
            return "{} ({})".format(info.name, info.field.typeName() or "field")
        if role == ROLE_EDITABLE:
            return editable
        if role == ROLE_PENDING:
            return info.idx in self._pending
        if role == ROLE_ERROR:
            return invalid[0] if invalid else ""
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or index.column() != 1:
            return False
        ok, _message = self.commit(index.row(), value)
        return ok

    # ------------------------------------------------------------------ commit
    def normalize(self, info, value):
        """(value ready for the layer, error text). Empty text means NULL, as in Pro."""
        text = None
        if isinstance(value, str):
            text = value.strip() if info.kind != "text" else value
            if text == "" or (info.kind != "text" and text.lower() in ("<null>", "null")):
                value = None
        if is_null(value):
            if info.not_null:
                return None, "A value is required for this field."
            return None, ""
        kind = info.kind
        try:
            if kind == "int" or (kind in ("valuemap", "valuerelation", "checkbox") and info.type_id in INTEGER_TYPES):
                raw = text if text is not None else value
                number = _to_int(raw)
                if number is None:
                    return None, "Enter a whole number."
                if info.type_id != T_LONGLONG and not INT32[0] <= number <= INT32[1]:
                    return None, "That number is too large for this field."
                value = number
            elif kind == "double" or (kind in ("valuemap", "valuerelation") and info.field.isNumeric()):
                value = float(text if text is not None else value)
            elif kind == "bool":
                if isinstance(value, str):
                    low = value.strip().lower()
                    if low not in ("true", "false", "1", "0", "yes", "no"):
                        return None, "Enter True or False."
                    value = low in ("true", "1", "yes")
            elif kind == "temporal":
                value = self._temporal(info, value)
            elif info.type_id == T_STRING or kind in ("text", "valuemap", "valuerelation", "checkbox"):
                value = str(value)
        except ValueError:
            return None, ("Enter a number." if kind == "double" else
                          "Enter a whole number." if kind == "int" else "That value is not valid for this field.")
        if kind == "text" or info.type_id == T_STRING:
            limit = info.field.length()
            if limit and limit > 0 and len(value) > limit:
                return None, "Text is longer than the {} characters this field allows.".format(limit)
        limits = info.range_limits()
        if limits and isinstance(value, (int, float)) and not limits[0] <= value <= limits[1]:
            return None, "Enter a value from {:g} to {:g}.".format(*limits)
        if isinstance(value, str) is False and kind == "temporal" and value is None:
            return None, "That date or time is not valid."
        return value, ""

    @staticmethod
    def _temporal(info, value):
        if isinstance(value, (QDate, QDateTime, QTime)):
            return value
        text = str(value).strip()
        formats = {T_DATE: ("yyyy-MM-dd", "M/d/yyyy", "d/M/yyyy"), T_TIME: ("HH:mm:ss", "HH:mm"),
                   T_DATETIME: ("yyyy-MM-dd HH:mm:ss", "yyyy-MM-ddTHH:mm:ss", "yyyy-MM-dd HH:mm", "yyyy-MM-dd")}
        for fmt in formats.get(info.type_id, ()):
            if info.type_id == T_DATE:
                parsed = QDate.fromString(text, fmt)
            elif info.type_id == T_TIME:
                parsed = QTime.fromString(text, fmt)
            else:
                parsed = QDateTime.fromString(text, fmt)
            if parsed.isValid():
                return parsed
        raise ValueError(text)

    def _validate(self, info, value):
        """Layer constraints (not null, unique, expression) for a single feature."""
        if self.is_multi() or not self.fids or is_null(value) and not info.not_null:
            return ""
        feature = next(self.layer.getFeatures(QgsFeatureRequest(self.fids[0])), None)
        if feature is None:
            return ""
        feature.setAttribute(info.idx, NULL if value is None else value)
        with suppress(Exception):
            result = QgsVectorLayerUtils.validateAttribute(self.layer, feature, info.idx)
            ok, errors = (result[0], result[1]) if isinstance(result, tuple) else (result, [])
            if not ok:
                return "; ".join(str(e) for e in errors) or "That value breaks a rule set on this field."
        return ""

    def commit(self, row, value):
        """Take a value typed (or chosen) in row `row`. Returns (ok, message)."""
        info = self.rows[row]
        typed = value if isinstance(value, str) else None
        new, error = self.normalize(info, value)
        if not error:
            old = self.current_value(row)
            same = (not is_null(new) and old is not MULTI and not is_null(old) and new == old) or \
                   (is_null(new) and old is not MULTI and is_null(old))
            if same:
                if info.idx in self._invalid:
                    self._invalid.pop(info.idx)
                    self._changed(row)
                return True, ""
            error = self._validate(info, new)
        if error:
            self._invalid[info.idx] = (error, typed if typed is not None else str(value))
            self._changed(row)
            self.problem.emit(error)
            return False, error
        self._invalid.pop(info.idx, None)
        if self.auto_apply:
            ok, message = self._write({info.idx: new})
            if not ok:
                self._invalid[info.idx] = (message, typed if typed is not None else str(value))
                self.problem.emit(message)
            self._changed(row)
            return ok, message
        self._pending[info.idx] = new
        self._changed(row)
        self.pendingChanged.emit(True)
        return True, ""

    def _changed(self, row):
        self.dataChanged.emit(self.index(row, 1), self.index(row, 1))

    def _write(self, changes):
        layer = self.layer
        if layer is None or not self.fids or not changes:
            return True, ""
        if self.before_edit is not None:
            self.before_edit(layer)
        was_editable = layer.isEditable()
        if not ensure_editing(layer):
            return False, "This layer cannot be edited."
        if not was_editable:
            self.editingStarted.emit(layer)
        layer.beginEditCommand("Edit attributes")
        ok = True
        for fid in self.fids:
            for idx, value in changes.items():
                ok = layer.changeAttributeValue(fid, idx, NULL if value is None else value) and ok
        if ok:
            layer.endEditCommand()
        else:
            layer.destroyEditCommand()
            self._read_values()
            return False, "The layer did not accept that value."
        self._read_values()
        self.valuesWritten.emit()
        return True, ""

    # ------------------------------------------------------------------ Apply / Cancel
    def has_pending(self):
        return bool(self._pending)

    def apply_pending(self):
        if not self._pending:
            return True
        changes = dict(self._pending)
        ok, message = self._write(changes)
        if ok:
            self._pending.clear()
            self.pendingChanged.emit(False)
        else:
            self.problem.emit(message)
        if self.rows:
            self.dataChanged.emit(self.index(0, 1), self.index(len(self.rows) - 1, 1))
        return ok

    def cancel_pending(self):
        had = bool(self._pending) or bool(self._invalid)
        self._pending.clear()
        self._invalid.clear()
        if had and self.rows:
            self.dataChanged.emit(self.index(0, 1), self.index(len(self.rows) - 1, 1))
        self.pendingChanged.emit(False)

    def set_auto_apply(self, on):
        self.auto_apply = bool(on)
        if on and self._pending:
            self.apply_pending()


def _to_int(value):
    """A whole number from text like '12' / '12.0' or an int/float; None if it is not one."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    with suppress(TypeError, ValueError):
        return int(str(value).strip())
    return int(float(value)) if _integral(value) else None


def _integral(value):
    """True when `value` is a whole number (text like '12' or '12.0', or an int/float)."""
    try:
        return float(value) == int(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


# --------------------------------------------------------------------------------------- delegate
class ValueDelegate(QStyledItemDelegate):
    """Editors per field type: text box, drop-down for domains and booleans, date picker."""

    BAR = QColor("#4d5156")
    BAR_WIDTH = 2
    TEXT_OFFSET = 9

    def createEditor(self, parent, _option, index):
        model = index.model()
        info = model.info(index.row())
        if info.kind in ("valuemap", "valuerelation", "checkbox", "bool"):
            combo = QComboBox(parent)
            for text, value in info.choices(model.layer):
                combo.addItem(text, value)
            combo.activated.connect(lambda _i, c=combo: self._finish(c))
            QTimer.singleShot(0, combo.showPopup)           # Pro opens the list on the first click
            return combo
        if info.kind == "temporal":
            return self._temporal_editor(parent, info)
        return QLineEdit(parent)

    @staticmethod
    def _temporal_editor(parent, info):
        edit = QgsDateTimeEdit(parent)
        edit.setAllowNull(True)
        fmt = {T_DATE: "yyyy-MM-dd", T_TIME: "HH:mm:ss"}.get(info.type_id, "yyyy-MM-dd HH:mm:ss")
        edit.setDisplayFormat(str(info.config.get("display_format") or fmt))
        if info.type_id != T_TIME:
            edit.setCalendarPopup(True)
        return edit

    def _finish(self, editor):
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def setEditorData(self, editor, index):
        value = index.data(Qt.ItemDataRole.EditRole)
        info = index.model().info(index.row())
        if isinstance(editor, QComboBox):
            target = 0
            for n in range(editor.count()):
                data = editor.itemData(n)
                if (is_null(data) and is_null(value)) or (not is_null(data) and not is_null(value)
                                                          and str(data) == str(value)):
                    target = n
                    break
            editor.setCurrentIndex(target)
        elif isinstance(editor, QgsDateTimeEdit):
            if is_null(value):
                editor.clear()
            elif isinstance(value, QDate):
                editor.setDateTime(QDateTime(value))
            elif isinstance(value, QTime):
                editor.setDateTime(QDateTime(QDate.currentDate(), value))
            elif isinstance(value, QDateTime):
                editor.setDateTime(value)
        elif isinstance(editor, QLineEdit):
            invalid = index.data(Qt.ItemDataRole.DisplayRole) if index.data(ROLE_ERROR) else None
            if invalid is not None:
                editor.setText(invalid)
            elif is_null(value):
                editor.setText("")
            else:
                editor.setText(_plain_text(info, value))
            editor.selectAll()

    def setModelData(self, editor, model, index):
        info = model.info(index.row())
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentData())
        elif isinstance(editor, QgsDateTimeEdit):
            if editor.isNull():
                model.setData(index, None)
            elif info.type_id == T_DATE:
                model.setData(index, editor.dateTime().date())
            elif info.type_id == T_TIME:
                model.setData(index, editor.dateTime().time())
            else:
                model.setData(index, editor.dateTime())
        elif isinstance(editor, QLineEdit):
            model.setData(index, editor.text())

    def paint(self, painter, option, index):
        if index.column() == 0:
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(self.TEXT_OFFSET, 0, 0, 0)    # field names sit clear of the bar
        super().paint(painter, option, index)
        if index.column() == 0 and index.data(ROLE_EDITABLE):
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.BAR)
            rect = option.rect
            painter.drawRect(rect.left() - self.TEXT_OFFSET, rect.top() + 1, self.BAR_WIDTH, rect.height() - 2)
            painter.restore()


def _plain_text(info, value):
    if info.kind == "double" and isinstance(value, float):
        text = repr(value)
        return text[:-2] if text.endswith(".0") else text
    return str(value)


# ----------------------------------------------------------------------------------------- view
class FieldView(QTableView):
    """Click a value to edit it; Tab / Shift+Tab / Enter move to the next editable value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(ValueDelegate(self))
        self.horizontalHeader().hide()
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(34)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 130)
        self.setShowGrid(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setWordWrap(False)

    def paintEvent(self, event):
        super().paintEvent(event)
        # a thin rule under every field row, like Pro's list
        model = self.model()
        if model is None:
            return
        painter = QPainter(self.viewport())
        painter.setPen(QColor("#d4d8da"))
        for row in range(model.rowCount()):
            rect = self.visualRect(model.index(row, 0))
            painter.drawLine(0, rect.bottom(), self.viewport().width(), rect.bottom())
        painter.end()

    def closeEditor(self, editor, hint):
        move = hint in (QAbstractItemDelegate.EndEditHint.EditNextItem, QAbstractItemDelegate.EndEditHint.EditPreviousItem)
        step = 1 if hint == QAbstractItemDelegate.EndEditHint.EditNextItem else -1
        current = self.currentIndex().row()
        super().closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)
        if not move:
            return
        model = self.model()
        row = current + step
        while 0 <= row < model.rowCount():
            index = model.index(row, 1)
            if model.flags(index) & Qt.ItemFlag.ItemIsEditable:
                self.setCurrentIndex(index)
                self.edit(index)
                return
            row += step

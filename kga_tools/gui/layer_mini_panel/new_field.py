# -*- coding: utf-8 -*-
"""Add a new field to a vector layer – logic and the inline form.

* Layer in editing mode: the field goes into the edit buffer (like QGIS's own
  "New Field"); it is kept when edits are saved, and Undo works.
* Not in editing mode: the field is written to the data source immediately –
  the form says so before you press Add.
Only types the data provider supports are offered.
"""

from contextlib import suppress

from qgis.core import Qgis, QgsField, QgsVectorDataProvider, QgsVectorLayer
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import (QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                 QSpinBox, QVBoxLayout, QWidget)

from ...core.compat import scoped
from . import compat as C

try:
    from qgis.PyQt.QtCore import QMetaType
except ImportError:  # very old bindings
    QMetaType = None

# key, label, QVariant name, QMetaType name, default length, default precision, uses length, uses precision
TYPES = [
    ("text", "Text (string)", "String", "QString", 80, 0, True, False),
    ("int", "Whole number (integer)", "Int", "Int", 0, 0, False, False),
    ("int64", "Whole number (64-bit)", "LongLong", "LongLong", 0, 0, False, False),
    ("double", "Decimal number (real)", "Double", "Double", 18, 3, True, True),
    ("date", "Date", "Date", "QDate", 0, 0, False, False),
    ("datetime", "Date & time", "DateTime", "QDateTime", 0, 0, False, False),
    ("bool", "Boolean", "Bool", "Bool", 0, 0, False, False),
]
TYPE = {t[0]: t for t in TYPES}
SHAPEFILE_NAME_LIMIT = 10


def _use_metatype():
    return QMetaType is not None and hasattr(QMetaType, "Type") and Qgis.QGIS_VERSION_INT >= 33800


def variant_type(key):
    t = TYPE[key]
    if _use_metatype():
        return getattr(QMetaType.Type, t[3])
    return getattr(QVariant, t[2])


def _type_int(v):
    try:
        return int(v)
    except TypeError:
        return int(getattr(v, "value", -1))


def _both_ints(key):
    """Integer codes of the type in both enums (QVariant / QMetaType use the same numbers)."""
    t = TYPE[key]
    out = set()
    if hasattr(QVariant, t[2]):
        out.add(_type_int(getattr(QVariant, t[2])))
    if QMetaType is not None and hasattr(QMetaType, "Type") and hasattr(QMetaType.Type, t[3]):
        out.add(_type_int(getattr(QMetaType.Type, t[3])))
    return out


def can_add_fields(layer):
    """(True, '') or (False, reason)."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return False, "No valid vector layer."
    if layer.readOnly():
        return False, "This layer is read-only."
    caps = layer.dataProvider().capabilities()
    flag = C.enum_value(Qgis, "VectorProviderCapability", "AddAttributes") if \
        hasattr(Qgis, "VectorProviderCapability") else None
    if flag is None:
        flag = scoped(QgsVectorDataProvider, 'Capability', 'AddAttributes')
    try:
        ok = bool(caps & flag)
    except TypeError:
        ok = bool(int(caps) & int(flag))
    if not ok:
        return False, "The {} data source does not allow adding fields.".format(layer.providerType())
    return True, ""


def supported_types(layer):
    """Type keys this provider can store (falls back to all when it does not say)."""
    try:
        natives = layer.dataProvider().nativeTypes()
    except Exception:
        natives = []
    if not natives:
        return [t[0] for t in TYPES]
    have = {_type_int(n.mType) for n in natives}
    return [t[0] for t in TYPES if _both_ints(t[0]) & have]


def native_type_name(layer, key):
    with suppress(Exception):
        for n in layer.dataProvider().nativeTypes():
            if _type_int(n.mType) in _both_ints(key):
                return n.mTypeName
    return ""


def is_shapefile(layer):
    try:
        return "shapefile" in (layer.dataProvider().storageType() or "").lower()
    except Exception:
        return False


def validate_name(layer, name):
    name = (name or "").strip()
    if not name:
        return "Enter a field name."
    existing = {n.lower() for n in layer.fields().names()}
    if name.lower() in existing:
        return "A field named “{}” already exists.".format(name)
    if is_shapefile(layer) and len(name) > SHAPEFILE_NAME_LIMIT:
        return "Shapefile field names can have at most {} characters.".format(SHAPEFILE_NAME_LIMIT)
    if any(ch in name for ch in "\"'`;\n\t"):
        return "Field names cannot contain quotes, semicolons or line breaks."
    return ""


def add_field(layer, name, key, length=0, precision=0, alias=""):
    """Add the field. Returns (ok, message)."""
    ok, why = can_add_fields(layer)
    if not ok:
        return False, why
    name = (name or "").strip()
    err = validate_name(layer, name)
    if err:
        return False, err
    t = TYPE[key]
    fld = QgsField(name, variant_type(key), native_type_name(layer, key),
                   int(length) if t[6] else 0, int(precision) if t[7] else 0)
    if layer.isEditable():
        if not layer.addAttribute(fld):
            return False, "QGIS could not add the field to the edit buffer."
        where = "added to the edit buffer – save your edits to keep it"
    else:
        if not layer.dataProvider().addAttributes([fld]):
            return False, "The data source refused the new field ({}).".format(
                "; ".join(layer.dataProvider().errors()[-1:]) or "no reason given")
        layer.updateFields()
        where = "written to the data source"
    idx = layer.fields().indexOf(name)
    if idx < 0:
        return False, "The field was not created (the data source may have renamed or refused it)."
    if alias.strip():
        layer.setFieldAlias(idx, alias.strip())
    return True, "Field “{}” {}.".format(name, where)


# ====================================================================== form
class NewFieldForm(QFrame):
    """Inline card in the Fields view."""

    def __init__(self, on_added=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.layer = None
        self.on_added = on_added
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        title = QLabel("NEW FIELD")
        title.setObjectName("sectionTitle")
        lay.addWidget(title)
        form = QFormLayout()
        form.setRowWrapPolicy(C.enum_value(QFormLayout, "RowWrapPolicy", "WrapLongRows"))
        form.setFieldGrowthPolicy(C.enum_value(QFormLayout, "FieldGrowthPolicy", "AllNonFixedFieldsGrow"))
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. crop_type")
        form.addRow("Name", self.name)
        self.type = QComboBox()
        form.addRow("Type", self.type)
        self.size_row = QWidget()
        sl = QHBoxLayout(self.size_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        self.length = QSpinBox()
        self.length.setRange(1, 10000)
        self.length.setToolTip("Maximum characters (text) or total digits (decimal)")
        self.prec_lbl = QLabel("Decimals")
        self.precision = QSpinBox()
        self.precision.setRange(0, 15)
        sl.addWidget(self.length, 1)
        sl.addWidget(self.prec_lbl)
        sl.addWidget(self.precision, 1)
        self.size_lbl = QLabel("Length")
        form.addRow(self.size_lbl, self.size_row)
        self.alias = QLineEdit()
        self.alias.setPlaceholderText("optional display name")
        form.addRow("Alias", self.alias)
        lay.addLayout(form)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("notice")
        lay.addWidget(self.note)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c0392b;")
        self.error.hide()
        lay.addWidget(self.error)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("secondary")
        self.add_btn = QPushButton("Add field")
        self.add_btn.setObjectName("primary")
        self.add_btn.setDefault(True)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.add_btn)
        lay.addLayout(btns)

        self.type.currentIndexChanged.connect(self._type_changed)
        self.name.textChanged.connect(self._validate)
        self.name.returnPressed.connect(self.submit)
        self.alias.returnPressed.connect(self.submit)
        self.add_btn.clicked.connect(self.submit)
        self.cancel_btn.clicked.connect(self.close_form)
        self.hide()

    def open_for(self, layer):
        self.layer = layer
        ok, why = can_add_fields(layer)
        self.type.blockSignals(True)
        self.type.clear()
        for key in supported_types(layer) if ok else []:
            self.type.addItem(TYPE[key][1], key)
        self.type.blockSignals(False)
        self.name.clear()
        self.alias.clear()
        self._type_changed()
        if ok and layer.isEditable():
            self.note.setText("The layer is in <b>editing mode</b>: the field is added to the edit buffer. "
                              "Save your edits to keep it (Undo works until then).")
        elif ok:
            limit = " Shapefile names: up to 10 characters." if is_shapefile(layer) else ""
            self.note.setText("The layer is <b>not</b> in editing mode: the field is written to the data "
                              "source right away.{}".format(limit))
        else:
            self.note.setText(why)
        for w in (self.name, self.type, self.size_row, self.alias, self.add_btn):
            w.setEnabled(ok)
        self.error.hide()
        self.show()
        self.name.setFocus()
        self._validate()

    def close_form(self):
        self.hide()
        self.layer = None

    def _type_changed(self, *_):
        key = self.type.currentData()
        if key is None:
            self.size_lbl.hide()
            self.size_row.hide()
            return
        t = TYPE[key]
        self.size_lbl.setVisible(t[6])
        self.size_row.setVisible(t[6])
        self.prec_lbl.setVisible(t[7])
        self.precision.setVisible(t[7])
        self.size_lbl.setText("Length" if key == "text" else "Digits")
        self.length.setValue(254 if (key == "text" and self.layer is not None and is_shapefile(self.layer))
                             else max(1, t[4]))
        self.precision.setValue(t[5])

    def _validate(self, *_):
        if self.layer is None:
            return False
        err = validate_name(self.layer, self.name.text()) if self.name.text().strip() else ""
        self.error.setText(err)
        self.error.setVisible(bool(err))
        self.add_btn.setEnabled(bool(self.name.text().strip()) and not err and self.type.count() > 0)
        return not err

    def submit(self):
        if self.layer is None or not self.add_btn.isEnabled():
            return
        ok, msg = add_field(self.layer, self.name.text(), self.type.currentData(), self.length.value(),
                            self.precision.value(), self.alias.text())
        if not ok:
            self.error.setText(msg)
            self.error.show()
            return
        name = self.name.text().strip()
        self.close_form()
        if self.on_added is not None:
            self.on_added(name, msg)

"""
Template properties page of the Create Features pane (the blue arrow on a picked template).

Name, description, tags, default tool and default attribute values. Saved in the layer, so they
travel with the project. Values that come from the category (the field the symbology is classified
on) are filled in and can be overridden.
"""

import os
from contextlib import suppress

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .templates import default_problems, save_overrides
from .tools import tools_for

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
ROLE_FIELD = Qt.ItemDataRole.UserRole


def _value_map(layer, index):
    """[(text shown, value)] for a field edited with a Value Map widget, else None."""
    try:
        setup = layer.editorWidgetSetup(index)
        if setup.type() != "ValueMap":
            return None
        pairs = []
        for entry in setup.config().get("map", []):
            if isinstance(entry, dict):
                pairs.extend((str(k), v) for k, v in entry.items())
        return pairs or None
    except Exception:
        return None


def editable_fields(layer):
    """Indexes of the fields a default value makes sense for."""
    skip = set()
    with suppress(Exception):
        skip.update(layer.dataProvider().pkAttributeIndexes())
    out = []
    for i in range(layer.fields().count()):
        if i in skip:
            continue
        with suppress(Exception):
            if layer.editorWidgetSetup(i).type() == "Hidden":
                continue
        out.append(i)
    return out


class TemplateProperties(QWidget):

    finished = pyqtSignal(bool)                 # True = saved

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer = None
        self.template = None
        self.editing = False
        self._cells = {}                         # field name -> QLineEdit / QComboBox

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        head = QHBoxLayout()
        back = QToolButton(self)
        back.setObjectName("KgaCreateGo")
        back.setIcon(QIcon(os.path.join(ICON_DIR, "back.svg")))
        back.setIconSize(QSize(20, 20))
        back.setToolTip("Back to the templates (discards unsaved changes)")
        back.clicked.connect(lambda: self._close(False))
        self.title = QLabel(self)
        self.title.setStyleSheet("font-size: 11pt; font-weight: 600;")
        head.addWidget(back)
        head.addWidget(self.title, 1)
        outer.addLayout(head)

        form = QFormLayout()
        form.setContentsMargins(4, 0, 4, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.name = QLineEdit(self)
        self.name.setPlaceholderText("Name shown in the pane")
        self.description = QPlainTextEdit(self)
        self.description.setFixedHeight(52)
        self.tags = QLineEdit(self)
        self.tags.setPlaceholderText("tag1, tag2")
        self.tool = QComboBox(self)
        self.source = QLabel(self)
        self.source.setWordWrap(True)
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("Tags", self.tags)
        form.addRow("Default tool", self.tool)
        form.addRow("Source", self.source)
        outer.addLayout(form)

        outer.addWidget(QLabel("Default attribute values", self))
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Field", "Value"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        outer.addWidget(self.table, 1)
        self.message = QLabel(self)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color: #b3261e;")
        self.message.hide()
        outer.addWidget(self.message)

        buttons = QHBoxLayout()
        reset = QPushButton("Reset", self)
        reset.setToolTip("Forget the name, description, tags, tool and default values of this template")
        reset.clicked.connect(self._reset)
        save = QPushButton("Save", self)
        save.setDefault(True)
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(lambda: self._close(False))
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        outer.addLayout(buttons)

    # ------------------------------------------------------------------ show
    def edit(self, layer, template):
        self.layer, self.template, self.editing = layer, template, True
        self.title.setText(template.label)
        self.name.setText(template.name)
        self.description.setPlainText(template.description)
        self.tags.setText(", ".join(template.tags))
        self.tool.clear()
        for tool_id, label, icon, _tip in tools_for(layer.geometryType()):
            self.tool.addItem(icon, label, tool_id)
        index = self.tool.findData(template.tool)
        self.tool.setCurrentIndex(max(0, index))
        note = f" Values not set: {template.note}." if template.note else ""
        self.source.setText(f"{layer.name()} ({layer.featureCount()} features).{note}")
        self.message.hide()
        self._fill_table()

    def _fill_table(self, use_defaults=True):
        layer, tmpl = self.layer, self.template
        fields = layer.fields()
        from_category = {fields.at(i).name() for i in tmpl.attrs}
        shown = editable_fields(layer)
        self.table.setRowCount(len(shown))
        self._cells = {}
        for row, index in enumerate(shown):
            fld = fields.at(index)
            item = QTableWidgetItem(fld.name())
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if fld.name() in from_category:
                item.setToolTip("Filled in by the category; type another value to replace it.")
            self.table.setItem(row, 0, item)
            text = tmpl.defaults.get(fld.name()) if use_defaults else None
            if text is None and index in tmpl.attrs:
                text = tmpl.attrs[index]
            text = "" if text is None else str(text)
            choices = _value_map(layer, index)
            if choices:
                cell = QComboBox(self.table)
                cell.addItem("(layer default)", "")
                for shown_text, value in choices:
                    cell.addItem(shown_text, str(value))
                at = cell.findData(text)
                cell.setCurrentIndex(max(0, at))
            else:
                cell = QLineEdit(text, self.table)
                cell.setPlaceholderText("(layer default)")
            self.table.setCellWidget(row, 1, cell)
            self._cells[fld.name()] = cell

    # ------------------------------------------------------------------ actions
    def _cell_text(self, cell):
        return cell.currentData() or "" if isinstance(cell, QComboBox) else cell.text().strip()

    def _reset(self):
        self.name.clear()
        self.description.clear()
        self.tags.clear()
        self.tool.setCurrentIndex(0)
        self._fill_table(use_defaults=False)

    def _save(self):
        tmpl, layer = self.template, self.layer
        defaults = {}
        category = {layer.fields().at(i).name(): v for i, v in tmpl.attrs.items()}
        for name, cell in self._cells.items():
            text = self._cell_text(cell)
            if text == "" or (name in category and text == str(category[name])):
                continue                          # empty or just the category's own value: nothing to store
            defaults[name] = text
        probe_defaults, tmpl.defaults = tmpl.defaults, defaults
        bad = default_problems(layer, tmpl)
        if bad:
            tmpl.defaults = probe_defaults
            self.message.setText("These values do not fit their field's type: " + ", ".join(bad))
            self.message.show()
            return
        tmpl.name = self.name.text().strip()
        tmpl.description = self.description.toPlainText().strip()
        tmpl.tags = [t.strip() for t in self.tags.text().split(",") if t.strip()]
        tmpl.tool = self.tool.currentData() if self.tool.currentIndex() > 0 else ""
        save_overrides(layer, tmpl)
        self._close(True)

    def _close(self, saved):
        self.editing = False
        self.finished.emit(saved)

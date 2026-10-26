"""
Merge, laid out like the Merge pane of ArcGIS Pro: tabs Existing Feature / New Feature, "Change the selection.", the
Layer, the "Features to merge" (one row per feature with its flash and unselect buttons; on the Existing Feature tab
the preserved one is marked "(preserve)" - click a row to preserve another), and the Merged Feature Attributes (type
a value to change it).

    Existing Feature   the features become the preserved one: it gets the combined shape and the attribute values
                       shown, the others are deleted
    New Feature        a new feature with the combined shape and those values is added (to the template layer if
                       "Use Template" is on); "Keep original features" leaves the originals in place

The shape is the union of the features (touching parts dissolve, others stay as parts of a multi-part feature). One
undoable edit command per layer.
"""

from contextlib import suppress

from qgis.core import QgsGeometry, QgsSettings, QgsVectorLayerUtils
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import schema
from ...core.modify_features import fit_to_layer, merge_geometries
from ..selection import _transform, all_vector_layers, map_layers
from .page import DoneStrip, button, check, project_layer, selection_banner
from .reshape import PickTool
from .split import TABS_QSS

SETTINGS = "kga_editor/modify_"
NULL_TEXT = "<Null>"
LIST_QSS = "QListWidget { background: #f1f2f3; border: 1px solid #c8cacc; } QListWidget::item { padding: 2px; }"


def feature_name(layer, feature):
    from ..attributes.layer_browser import feature_label
    return feature_label(layer, feature)


class MergeTool(PickTool):
    KEY = "merge"
    LABEL = "Merge"
    HINT = "select the features to merge, then press Merge."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False                # clicking selects until you press Merge

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.keep_original = settings.value(SETTINGS + "merge_keep", True, type=bool)
        self.use_template = settings.value(SETTINGS + "merge_template", True, type=bool)
        self.tab = 0                       # 0 Existing Feature, 1 New Feature
        self.layer = None                  # the layer whose selected features are listed
        self.preserve = None               # fid of the feature the Existing Feature tab keeps
        self.fids = []                     # the listed features
        self._values = {}                  # field index -> text the user typed (reset when the list changes)
        self._signature = None
        self.info = self.tab_bar = self.layer_combo = self.list = self.table = None
        self.keep_check = self.template_check = self.template_combo = self.merge_button = None
        self.new_box = None
        self._busy = False
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def start_select_mode(self):
        self.set_selecting(True, auto=True)

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        self.tab_bar = QTabBar()
        self.tab_bar.setStyleSheet(TABS_QSS)
        self.tab_bar.addTab("Existing Feature")
        self.tab_bar.addTab("New Feature")
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setCurrentIndex(self.tab)
        self.tab_bar.currentChanged.connect(self.set_tab)
        box.addWidget(self.tab_bar)
        self.banner = selection_banner(lambda: self.set_selecting(not self._selecting))
        self._select_button = self.banner
        self.banner.setChecked(self._selecting)
        box.addWidget(self.banner)

    def build_page(self, box):
        box.addWidget(QLabel("Layer"))
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self._layer_picked)
        box.addWidget(self.layer_combo)
        box.addWidget(QLabel("Features to merge"))
        self.list = QListWidget()
        self.list.setStyleSheet(LIST_QSS)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setMinimumHeight(110)
        self.list.setMaximumHeight(170)
        self.list.itemClicked.connect(self._row_clicked)
        box.addWidget(self.list)
        self.new_box = QWidget()
        nb = QVBoxLayout(self.new_box)
        nb.setContentsMargins(0, 0, 0, 0)
        self.keep_check = check("Keep original features", self.keep_original, self.set_keep)
        self.template_check = check("Use Template", self.use_template, self.set_template)
        self.template_combo = QComboBox()
        self.template_combo.currentIndexChanged.connect(lambda _i: self._rebuild_table())
        nb.addWidget(self.keep_check)
        nb.addWidget(self.template_check)
        nb.addWidget(self.template_combo)
        box.addWidget(self.new_box)
        box.addWidget(QLabel("Merged Feature Attributes"))
        self.table = QTableWidget(0, 2)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(120)
        self.table.itemChanged.connect(self._value_typed)
        box.addWidget(self.table, 1)
        self.new_box.setVisible(self.tab == 1)

    def build_footer(self, box):
        row = QHBoxLayout()
        row.addStretch(1)
        self.merge_button = button("Merge", self.merge, "Merge the features (Ctrl+Z undoes it)")
        row.addWidget(self.merge_button)
        box.addLayout(row)

    # ------------------------------------------------------------------ what is listed
    def _candidates(self):
        """[(layer, [selected fids])] of the editable layers with a selection, shown on the map."""
        shown = map_layers(self.canvas())
        return [(layer, sorted(layer.selectedFeatureIds())) for layer in all_vector_layers()
                if layer.isEditable() and layer.isSpatial() and layer.selectedFeatureCount()
                and (shown is None or layer.id() in shown)]

    def update_page(self):
        if self.merge_button is None or sip.isdeleted(self.merge_button) or self._busy:
            return
        candidates = self._candidates()
        signature = tuple((layer.id(), tuple(ids)) for layer, ids in candidates)
        if signature != self._signature:
            self._signature = signature
            self._load(candidates)
        self._refresh_state()

    def _load(self, candidates):
        self._busy = True
        try:
            names = [layer.id() for layer, _ids in candidates]
            current = self.layer.id() if self.layer is not None and not sip.isdeleted(self.layer) else None
            self.layer_combo.blockSignals(True)
            self.layer_combo.clear()
            for layer, _ids in candidates:
                self.layer_combo.addItem(layer.name(), layer.id())
            index = names.index(current) if current in names else 0
            if candidates:
                self.layer_combo.setCurrentIndex(index)
            self.layer_combo.blockSignals(False)
            self.layer = candidates[index][0] if candidates else None
            self.fids = list(candidates[index][1]) if candidates else []
            if self.preserve not in self.fids:
                self.preserve = self.fids[0] if self.fids else None
            self._values = {}
            self._rebuild_list()
            self._rebuild_templates()
            self._rebuild_table()
        finally:
            self._busy = False

    def _layer_picked(self, index):
        if self._busy or index < 0:
            return
        layer = project_layer(self.layer_combo.itemData(index))
        if layer is None:
            return
        self.layer = layer
        self.fids = sorted(layer.selectedFeatureIds())
        self.preserve = self.fids[0] if self.fids else None
        self._values = {}
        self._rebuild_list()
        self._rebuild_templates()
        self._rebuild_table()
        self._refresh_state()

    def _rebuild_list(self):
        self.list.clear()
        layer = self.layer
        if layer is None:
            return
        for fid in self.fids:
            item = QListWidgetItem(self.list)
            item.setData(Qt.ItemDataRole.UserRole, fid)
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(4, 0, 4, 0)
            lay.addWidget(QLabel(feature_name(layer, layer.getFeature(fid))), 1)
            tag = QLabel("(preserve)")
            tag.setVisible(self.tab == 0 and fid == self.preserve)
            tag.setObjectName("KgaMergePreserve")
            lay.addWidget(tag)
            flash = QToolButton()
            flash.setText("◎")
            flash.setAutoRaise(True)
            flash.setToolTip("Flash this feature on the map")
            flash.clicked.connect(lambda _c=False, f=fid: self.flash(f))
            lay.addWidget(flash)
            drop = QToolButton()
            drop.setText("✕")
            drop.setAutoRaise(True)
            drop.setToolTip("Unselect this feature")
            drop.clicked.connect(lambda _c=False, f=fid: self.unselect(f))
            lay.addWidget(drop)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
            if fid == self.preserve and self.tab == 0:
                item.setBackground(QColor(225, 228, 231))

    def _tags(self):
        for i in range(self.list.count()):
            item = self.list.item(i)
            row = self.list.itemWidget(item)
            tag = row.findChild(QLabel, "KgaMergePreserve") if row is not None else None
            kept = self.tab == 0 and item.data(Qt.ItemDataRole.UserRole) == self.preserve
            if tag is not None:
                tag.setVisible(kept)
            item.setBackground(QColor(225, 228, 231) if kept else QColor(0, 0, 0, 0))

    def _row_clicked(self, item):
        if self.tab != 0:
            return
        self.preserve = item.data(Qt.ItemDataRole.UserRole)
        self._values = {}
        self._tags()
        self._rebuild_table()

    def flash(self, fid):
        if self.layer is None:
            return
        geom = self.layer.getFeature(fid).geometry()
        if not geom.isNull():
            with suppress(Exception):
                self.canvas().flashGeometries([geom], self.layer.crs())

    def unselect(self, fid):
        if self.layer is not None:
            self.layer.deselect(fid)

    # ------------------------------------------------------------------ New Feature options
    def _rebuild_templates(self):
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        layer = self.layer
        if layer is not None:
            for other in all_vector_layers():
                if other.isEditable() and other.isSpatial() and other.geometryType() == layer.geometryType():
                    self.template_combo.addItem(other.name(), other.id())
            index = self.template_combo.findData(layer.id())
            self.template_combo.setCurrentIndex(max(0, index))
        self.template_combo.blockSignals(False)
        self.template_combo.setEnabled(self.use_template)

    def target_layer(self):
        """The layer the merged feature goes to: the template layer on the New Feature tab, else the source layer."""
        if self.tab == 1 and self.use_template:
            chosen = project_layer(self.template_combo.currentData())
            if chosen is not None:
                return chosen
        return self.layer

    def set_tab(self, index):
        self.tab = int(index)
        self.new_box.setVisible(self.tab == 1)
        self._values = {}
        self._tags()
        self._rebuild_table()
        self._refresh_state()

    def set_keep(self, on):
        self.keep_original = bool(on)
        QgsSettings().setValue(SETTINGS + "merge_keep", self.keep_original)

    def set_template(self, on):
        self.use_template = bool(on)
        QgsSettings().setValue(SETTINGS + "merge_template", self.use_template)
        self.template_combo.setEnabled(self.use_template)
        self._rebuild_table()

    # ------------------------------------------------------------------ the attributes
    def _table_layer(self):
        return self.target_layer()

    def _defaults(self, layer):
        """{field index: value} the merged feature starts with."""
        if self.tab == 0:
            feature = self.layer.getFeature(self.preserve) if self.layer is not None and self.preserve is not None else None
            return {i: feature.attribute(i) for i in range(layer.fields().count())} if feature is not None else {}
        fresh = QgsVectorLayerUtils.createFeature(layer, QgsGeometry())
        return {i: fresh.attribute(i) for i in range(layer.fields().count())}

    def _rebuild_table(self):
        if self.table is None:
            return
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        layer = self._table_layer()
        if layer is not None and not sip.isdeleted(layer) and self.fids:
            defaults = self._defaults(layer)
            owned = set(layer.primaryKeyAttributes() or [])
            rows = [i for i in range(layer.fields().count()) if i not in owned]
            self.table.setRowCount(len(rows))
            for row, index in enumerate(rows):
                field = layer.fields().at(index)
                name = QTableWidgetItem(field.name())
                name.setFlags(Qt.ItemFlag.ItemIsEnabled)
                name.setToolTip(f"{field.name()} ({field.typeName()})")
                value = self._values.get(index, defaults.get(index))
                text = value if isinstance(value, str) and index in self._values else (
                    NULL_TEXT if schema.is_null(value) else str(value))
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, index)
                self.table.setItem(row, 0, name)
                self.table.setItem(row, 1, cell)
        self.table.blockSignals(False)

    def _value_typed(self, item):
        if item.column() == 1:
            self._values[item.data(Qt.ItemDataRole.UserRole)] = item.text()

    def _typed_attributes(self, layer):
        """{field index: value} to write: what the table shows, converted to the field's type."""
        values = {}
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 1)
            index = cell.data(Qt.ItemDataRole.UserRole)
            text = cell.text()
            field = layer.fields().at(index)
            if text.strip() in ("", NULL_TEXT):
                values[index] = None
                continue
            if index in self._values:
                try:
                    values[index] = field.convertCompatible(text)
                except (ValueError, TypeError):
                    raise ValueError(f"'{text}' does not fit the field {field.name()} ({field.typeName()}).")
            else:
                values[index] = self._defaults(layer).get(index)      # untouched: the value as it is
        return values

    # ------------------------------------------------------------------ state
    def _refresh_state(self):
        ready = self.layer is not None and len(self.fids) >= 2
        if self.merge_button is not None and not sip.isdeleted(self.merge_button):
            self.merge_button.setEnabled(ready)

    # ------------------------------------------------------------------ the run
    def merge(self):
        layer = self.layer
        if layer is None or len(self.fids) < 2:
            self.tell("Select two or more features to merge.", ok=False)
            return 0
        target = self.target_layer()
        features = [layer.getFeature(fid) for fid in self.fids]
        merged = merge_geometries([f.geometry() for f in features], layer.crs())
        if merged is None:
            self.tell("Those features could not be combined.", ok=False)
            return 0
        if target.crs() != layer.crs():
            merged.transform(_transform(layer.crs(), target.crs()))
        parts = fit_to_layer(merged, target)
        if not parts:
            self.tell(f"The merged shape does not fit \"{target.name()}\".", ok=False)
            return 0
        if len(parts) > 1:
            self.tell(f"They do not touch, and \"{target.name()}\" is a single-part layer, so they cannot become one "
                      "feature.", ok=False)
            return 0
        try:
            values = self._typed_attributes(target)
        except ValueError as error:
            self.tell(str(error), ok=False)
            return 0
        count = len(features)
        if self.tab == 0:
            keep = self.preserve
            layer.beginEditCommand("Merge")
            ok = layer.changeGeometry(keep, parts[0])
            for index, value in values.items():
                ok = ok and layer.changeAttributeValue(keep, index, value)
            for fid in self.fids:
                if ok and fid != keep:
                    ok = layer.deleteFeature(fid)
            if ok:
                layer.endEditCommand()
            else:
                layer.destroyEditCommand()
            result_ids = [keep]
            text = f"Merge completed: {count} features became feature {keep}."
        else:
            target.beginEditCommand("Merge")
            feature = QgsVectorLayerUtils.createFeature(target, parts[0], values)
            ok = target.addFeature(feature)
            if ok:
                target.endEditCommand()
            else:
                target.destroyEditCommand()
            if ok and not self.keep_original:
                layer.beginEditCommand("Merge: remove the originals")
                ok = all(layer.deleteFeature(fid) for fid in self.fids)
                if ok:
                    layer.endEditCommand()
                else:
                    layer.destroyEditCommand()
            result_ids = [feature.id()]
            text = f"Merge completed: {count} features merged into a new feature."
        if not ok:
            self.tell(f"Could not edit {target.name()} - is it in edit mode?", ok=False)
            return 0
        layer.triggerRepaint()
        target.triggerRepaint()
        for each in {layer, target}:
            each.removeSelection()
        target.selectByIds(result_ids)
        self._signature = None
        self.tell(text)
        self.refresh_page()
        return count

    def tell(self, text, ok=True):
        if self.info is not None and not sip.isdeleted(self.info):
            self.info.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))

    def on_click(self, point):
        pass


TOOL_CLASSES = {MergeTool.KEY: MergeTool}

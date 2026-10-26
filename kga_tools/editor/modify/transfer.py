"""
Transfer Attributes, laid out like the Transfer Attributes pane of ArcGIS Pro: the tool tile and the green "Attribute
transfer completed." strip. On the map, click a source feature, then click a target feature (the tip beside the
cursor names the step and the source); the values go across and the tool is ready for the next source. Every field
with the same name in both layers is copied (values are converted to the target field's type); the primary key,
unique and calculated fields are never touched. The page lists the fields; untick the ones to leave out. One
undoable edit command per transfer. Right-click or Esc goes back to choosing a source.
"""

import os
from contextlib import suppress

from qgis.core import NULL, Qgis, QgsFeatureRequest, QgsFieldConstraints, QgsFields, QgsSettings

from ...core.compat import enum_value
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QCursor, QIcon
from qgis.PyQt.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QSizePolicy, QToolButton, QToolTip

from ..selection import all_vector_layers, map_layers
from .page import DoneStrip, button, hint_label, section_label
from .pick import pick_features
from .reshape import PickTool

SETTINGS = "kga_editor/modify_transfer_skip"
ICON = os.path.join(os.path.dirname(__file__), "icons", "transfer_attributes.svg")


def matching_fields(source_layer, target_layer, skip_names=()):
    """[(source field index, target field index)] of the fields that can be copied, matched by name."""
    skip_names = {n.lower() for n in skip_names}
    protected = set(target_layer.primaryKeyAttributes())
    pairs = []
    for i, field in enumerate(target_layer.fields()):
        if (i in protected or field.name().lower() in skip_names
                or target_layer.fields().fieldOrigin(i) == enum_value((Qgis, 'FieldOrigin.Expression'), (QgsFields, 'FieldOrigin.OriginExpression'),
                                                                  (QgsFields, 'OriginExpression'))
                or target_layer.fieldConstraints(i) & enum_value((QgsFieldConstraints, 'Constraint.ConstraintUnique'),
                                                                      (QgsFieldConstraints, 'ConstraintUnique'))):
            continue
        j = source_layer.fields().lookupField(field.name())
        if j >= 0:
            pairs.append((j, i))
    return pairs


def _converted(field, value):
    """The value in the target field's type, or the string 'skip' if it cannot be converted."""
    if value is None or value == NULL:
        return NULL
    try:
        return field.convertCompatible(value)
    except (ValueError, TypeError):
        return "skip"


def transfer_attributes(source_layer, source_feature, targets, skip_names=()):
    """
    Copy values from one feature onto many. targets: [(layer, ids)]. Returns (features changed, values written,
    layers failed, values that could not be converted).
    """
    features = values = bad = 0
    failed = []
    for layer, ids in targets:
        pairs = matching_fields(source_layer, layer, skip_names)
        if not pairs:
            continue
        layer.beginEditCommand("Transfer attributes")
        ok = True
        for fid in ids:
            if layer is source_layer and fid == source_feature.id():
                continue
            wrote = 0
            for j, i in pairs:
                value = _converted(layer.fields()[i], source_feature.attribute(j))
                if isinstance(value, str) and value == "skip":
                    bad += 1
                    continue
                ok = layer.changeAttributeValue(fid, i, value) and ok
                wrote += 1
            if wrote:
                features += 1
                values += wrote
        if ok:
            layer.endEditCommand()
        else:
            layer.destroyEditCommand()
            failed.append(layer.name())
        layer.triggerRepaint()
    return features, values, failed, bad


def layer_color(layer):
    """The colour of a layer's symbol, for the swatch in the cursor tip."""
    with suppress(Exception):
        symbol = layer.renderer().symbol()
        if symbol is not None:
            return symbol.color()
    return QColor(200, 200, 200)


class TransferAttributesTool(PickTool):
    KEY = "transfer_attributes"
    LABEL = "Transfer Attributes"
    HINT = "click a source feature, then click a target feature to copy the values onto."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = AUTO_SELECT = False
    source = None                                   # (layer, feature id) once a source feature is picked

    def __init__(self, canvas, iface, select_tool=None):
        saved = QgsSettings().value(SETTINGS, [])
        self.skip = set(saved if isinstance(saved, list) else [saved] if saved else [])
        self.info = self.tool_tile = self.field_list = None
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def reset(self):
        self.source = None
        super().reset()

    def deactivate(self):
        QToolTip.hideText()
        super().deactivate()

    def on_right_click(self, _e):
        self.start_over()

    def start_over(self):
        self.reset()
        self.update_page()

    # ------------------------------------------------------------------ the map
    def _sources(self, point):
        return pick_features(self.canvas(), point)

    def _targets(self, point):
        own = (self.source[0].id(), self.source[1]) if self.source else None
        return [h for h in pick_features(self.canvas(), point, editable_only=True) if (h[0].id(), h[1]) != own]

    def tip(self):
        """The text beside the cursor: the step, and below it the source feature once there is one."""
        if self.source is None:
            return "Click a source feature"
        layer, fid = self.source
        color = layer_color(layer).name()
        return ("Click a target feature<hr>Source feature:<br>"
                f"<table><tr><td bgcolor='{color}' width='22'>&nbsp;</td><td>&nbsp;{fid if fid >= 0 else 'new'}"
                "</td></tr></table>")

    def on_move(self, _point):
        QToolTip.showText(QCursor.pos(), self.tip(), self.canvas())

    def on_click(self, point):
        if self.source is None:
            hits = self._sources(point)
            if not hits:
                self.info.show_text("No feature there to copy the values from.", ok=False)
                return
            layer, fid, geom = hits[0]
            self.source = (layer, fid)
            self.highlight(geom)
            self.info.hide()
            self.update_page()
            self.on_move(point)
            return
        hits = self._targets(point)
        if not hits:
            self.info.show_text("No editable target feature there.", ok=False)
            return
        layer, fid, _g = hits[0]
        source_layer, source_fid = self.source
        source = next(source_layer.getFeatures(QgsFeatureRequest(source_fid)), None)
        if source is None:
            self.start_over()
            return
        self.transfer(source_layer, source, [(layer, [fid])])
        self.start_over()                                               # ready for the next source
        self.on_move(point)

    def transfer(self, layer, source, targets):
        features, values, failed, bad = transfer_attributes(layer, source, targets, self.skip)
        ok = bool(features) and not failed
        if features:
            text = "Attribute transfer completed."
        else:
            text = "Nothing transferred: no field names in common with the target feature."
        if bad:
            text += f" {bad} value(s) did not fit the target field and were left out."
        if failed:
            text += f" Failed in: {', '.join(failed)}."
        self.info.show_text(text, ok=ok)
        return features, values

    def set_skip(self, names):
        self.skip = {n.lower() for n in names}
        with suppress(Exception):
            QgsSettings().setValue(SETTINGS, sorted(self.skip))

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)

    def build_page(self, box):
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(QIcon(ICON))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Transfer Attributes")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT)
        tile.clicked.connect(lambda _c=False: tile.setChecked(True))
        self.tool_tile = tile
        box.addWidget(tile)
        box.addWidget(section_label("Fields to copy"))
        box.addWidget(hint_label("Matched by name between the source and the target feature. "
                                 "Untick a field to leave it alone."))
        self.field_list = QListWidget()
        self.field_list.setMinimumHeight(140)
        self.field_list.itemChanged.connect(self._field_toggled)
        box.addWidget(self.field_list)
        row = QHBoxLayout()
        row.addWidget(button("Tick all", lambda: self._tick_all(True)))
        row.addWidget(button("Untick all", lambda: self._tick_all(False)))
        box.addLayout(row)

    def field_names(self):
        """The field names of the editable layers (and the source's layer), in order, each once."""
        shown = map_layers(self.canvas())
        layers = [layer for layer in all_vector_layers()
                  if layer.isEditable() and layer.isSpatial() and (shown is None or layer.id() in shown)]
        if self.source is not None and self.source[0] not in layers:
            layers.insert(0, self.source[0])
        names = []
        for layer in layers:
            for field in layer.fields():
                if field.name().lower() not in [n.lower() for n in names]:
                    names.append(field.name())
        return names

    def update_page(self):
        view = self.field_list
        if view is None:
            return
        view.blockSignals(True)
        view.clear()
        for name in self.field_names():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if name.lower() in self.skip else Qt.CheckState.Checked)
            view.addItem(item)
        view.blockSignals(False)

    def _field_toggled(self, _item):
        view = self.field_list
        skipped = {view.item(i).text() for i in range(view.count())
                   if view.item(i).checkState() != Qt.CheckState.Checked}
        shown = {view.item(i).text().lower() for i in range(view.count())}
        self.set_skip(skipped | {n for n in self.skip if n not in shown})      # keep choices for fields not shown now

    def _tick_all(self, on):
        view = self.field_list
        view.blockSignals(True)
        for i in range(view.count()):
            view.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        view.blockSignals(False)
        self._field_toggled(None)


TOOL_CLASSES = {TransferAttributesTool.KEY: TransferAttributesTool}

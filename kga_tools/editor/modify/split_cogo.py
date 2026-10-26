"""
Split Into COGO Lines, laid out like the Split Into COGO Lines pane of ArcGIS Pro: "Change the selection." and the
selected features, the Template section (Use Template and the line layer the COGO lines are made in), "Split lines
with existing COGO attributes" and the Split button. Every selected line, or polygon boundary, is split at its
vertices: each straight run and each arc becomes a line of its own carrying its Direction, Distance, Radius and
ArcLength (the direction convention and units are the shared COGO settings of the Update COGO / Traverse tools).

    Use Template    on: the new lines go into the template layer (an editable line layer), which a polygon
                    boundary needs. Off: lines are split in their own layer
    Existing COGO   lines that already have a Direction and a Distance are left alone unless this is ticked
    The originals   a line that was split is replaced by its courses; a polygon is kept

New lines copy the attributes of the feature they come from. One undoable edit command per layer.
"""

from qgis.core import NULL, Qgis, QgsFeatureRequest, QgsGeometry, QgsSettings
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout

from ...core.modify_features import (
    COGO_DIRECTION,
    COGO_DISTANCE,
    Workspace,
    cogo_of,
    curves_of,
    find_cogo_field,
    fit_to_layer,
    segment_curves,
)
from ...gui.modify_base import carry_attributes, make_feature
from ..selection import _transform, all_vector_layers, map_layers
from .buffer import layer_icon
from .cogo import CogoSettings, _unit_values, cogo_attributes, ensure_cogo_fields
from .page import DoneStrip, SelectedTree, button, check, project_layer, section_label, selection_banner
from .reshape import PickTool

SETTINGS = "kga_editor/modify_"
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
NEED_SELECTION = "Select one or more lines or polygons"


def selected_sources(canvas):
    """[(layer, ids)] of the selected line and polygon features of the layers shown on the map."""
    shown = map_layers(canvas)
    return [(layer, list(layer.selectedFeatureIds())) for layer in all_vector_layers()
            if layer.isSpatial() and layer.geometryType() in (LINE, POLYGON) and layer.selectedFeatureCount()
            and (shown is None or layer.id() in shown)]


def template_layers(canvas):
    """The editable line layers shown on the map: where COGO lines can be made."""
    shown = map_layers(canvas)
    return [layer for layer in all_vector_layers()
            if layer.isSpatial() and layer.geometryType() == LINE and layer.isEditable()
            and (shown is None or layer.id() in shown)]


def has_cogo(layer, feature):
    """True when the feature already has a Direction and a Distance (so it is a COGO line already)."""
    for name in (COGO_DIRECTION, COGO_DISTANCE):
        index = find_cogo_field(layer.fields(), name)
        if index < 0:
            return False
        value = feature.attribute(index)
        if value is None or value == NULL or value == "":
            return False
    return True


def courses(layer, feature, settings):
    """[(geometry in the layer's CRS, COGO values)] for every course of one feature."""
    geometry = feature.geometry()
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []
    workspace = Workspace(layer, settings.distance_unit, extent_hint=geometry.boundingBox().center())
    if workspace.fell_back:
        raise ValueError("the layer is in degrees and no projected CRS fits it - pick 'Map units' or reproject")
    out = []
    for curve in curves_of(workspace.to_work(geometry)):
        for piece in segment_curves(curve, True):
            out.append((workspace.from_work(QgsGeometry(piece.clone())), _unit_values(workspace, cogo_of(piece))))
    return out


def split_into_cogo(sources, template, settings, split_existing=False):
    """
    Split the selected features into COGO lines (a line with several courses is always split; one that is already a
    single COGO course is left alone unless split_existing). sources: [(layer, ids)]; template: the layer the lines go in, or
    None to split each line in its own layer. Returns a dict: features (split), created (lines), removed
    (originals), existing (skipped for having COGO values), dropped, added (COGO fields created), missing
    (layers without COGO fields), failed (layer names), new ([(layer, fid)] of the new lines).
    """
    result = {"features": 0, "created": 0, "removed": 0, "existing": 0, "dropped": 0, "added": [], "missing": [],
              "failed": [], "new": []}
    deletions = {}
    for layer, ids in sources:
        target = template or layer
        if target.geometryType() != LINE or not target.isEditable():
            result["failed"].append(target.name())
            continue
        indices, added = ensure_cogo_fields(target, settings.create_fields)
        result["added"] += [n for n in added if n not in result["added"]]
        if all(i < 0 for i in indices.values()) and target.name() not in result["missing"]:
            result["missing"].append(target.name())
        transform = _transform(layer.crs(), target.crs()) if layer.crs() != target.crs() else None
        target.beginEditCommand("Split into COGO lines")
        try:
            for feature in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
                pieces = courses(layer, feature, settings)
                if (not split_existing and len(pieces) == 1 and layer.geometryType() == LINE
                        and has_cogo(layer, feature)):
                    result["existing"] += 1                    # already a COGO line: nothing to split
                    continue
                made = 0
                for shape, values in pieces:
                    if transform is not None:
                        shape.transform(transform)
                    attrs = carry_attributes(feature, layer, target)
                    attrs.update(cogo_attributes(target, values, settings, indices))
                    for part in fit_to_layer(shape, target):
                        new = make_feature(target, part, attrs)
                        if target.addFeature(new):
                            made += 1
                            result["new"].append((target, new.id()))
                        else:
                            result["dropped"] += 1
                if made:
                    result["features"] += 1
                    result["created"] += made
                    if layer.geometryType() == LINE and layer.isEditable():
                        deletions.setdefault(layer, []).append(feature.id())
        except Exception:
            target.destroyEditCommand()
            raise
        target.endEditCommand()
        target.triggerRepaint()
    for layer, ids in deletions.items():
        layer.beginEditCommand("Split into COGO lines (remove originals)")
        ok = all(layer.deleteFeature(fid) for fid in ids)
        if ok:
            layer.endEditCommand()
            result["removed"] += len(ids)
        else:
            layer.destroyEditCommand()
            result["failed"].append(layer.name())
        layer.triggerRepaint()
    return result


class SplitCogoTool(PickTool):
    KEY = "split_cogo_lines"
    LABEL = "Split Into COGO Lines"
    HINT = "select lines or polygons, choose the template, then press Split."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.use_template = settings.value(SETTINGS + "splitcogo_template", True, type=bool)
        self.split_existing = settings.value(SETTINGS + "splitcogo_existing", False, type=bool)
        self.info = self.tree = self.template_combo = self.split_button = self.banner = None
        self._result = None
        self._result_sig = frozenset()
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)

    def build_page(self, box):
        self.banner = selection_banner(self._banner_clicked)
        self.banner.setChecked(self._selecting)
        self._select_button = self.banner
        box.addWidget(self.banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)
        box.addWidget(section_label("Template"))
        self.use_check = check("Use Template", self.use_template, self.set_use_template)
        self.use_check.setToolTip("Make the COGO lines in the template layer. A polygon boundary needs one")
        box.addWidget(self.use_check)
        self.template_combo = QComboBox()
        self.template_combo.setToolTip("The editable line layer the COGO lines are created in")
        self.template_combo.currentIndexChanged.connect(self._template_changed)
        box.addWidget(self.template_combo)
        self.existing_check = check("Split lines with existing COGO attributes", self.split_existing,
                                    self.set_split_existing)
        self.existing_check.setToolTip("Also split lines that already have a Direction and a Distance")
        box.addWidget(self.existing_check)

    def build_footer(self, box):
        row = QHBoxLayout()
        row.addStretch(1)
        self.split_button = button("Split", self.split, "Split the selected features into COGO lines")
        row.addWidget(self.split_button)
        box.addLayout(row)

    def _banner_clicked(self):
        self.set_selecting(not self._selecting)

    def set_use_template(self, on):
        self.use_template = bool(on)
        QgsSettings().setValue(SETTINGS + "splitcogo_template", self.use_template)
        self.refresh_page()

    def set_split_existing(self, on):
        self.split_existing = bool(on)
        QgsSettings().setValue(SETTINGS + "splitcogo_existing", self.split_existing)

    def _template_changed(self, _index):
        if self.template_combo is not None and self.template_combo.currentData():
            QgsSettings().setValue(SETTINGS + "splitcogo_layer", self.template_combo.currentData())

    def template(self):
        """The template layer, or None when it is not used (or there is none)."""
        if not self.use_template or self.template_combo is None:
            return None
        return project_layer(self.template_combo.currentData())

    def _rebuild_templates(self):
        combo = self.template_combo
        keep = combo.currentData() or QgsSettings().value(SETTINGS + "splitcogo_layer", "")
        layers = template_layers(self.canvas())
        combo.blockSignals(True)
        combo.clear()
        for layer in layers:
            combo.addItem(layer_icon(layer), layer.name(), layer.id())
        combo.setCurrentIndex(max(0, combo.findData(keep)))
        combo.blockSignals(False)

    def status(self, sources):
        """(can split, blue message): what is wrong with the selection or the template, if anything."""
        if not sources:
            return False, ""
        if not self.use_template:
            if any(layer.geometryType() == POLYGON for layer, _ids in sources):
                return False, "Polygons need a template: tick Use Template and choose a line layer."
            if any(not layer.isEditable() for layer, _ids in sources):
                return False, "Start editing the line layer first, or use a template."
        elif self.template() is None:
            return False, "There is no editable line layer to use as a template."
        return True, ""

    def update_page(self):
        if self.split_button is None or sip.isdeleted(self.split_button):
            return
        self._rebuild_templates()
        self.template_combo.setEnabled(self.use_template)
        self.tree.refresh(self.canvas())
        sources = selected_sources(self.canvas())
        ok, message = self.status(sources)
        self.split_button.setEnabled(ok)
        self.banner.setText("Change the selection." if sources else NEED_SELECTION)
        signature = frozenset((layer.id(), fid) for layer, ids in sources for fid in ids)
        if self._result and signature != self._result_sig:
            self._result = None                         # the selection moved on: the result no longer applies
        if self._result:
            self.info.show_text(*self._result)
        elif message:
            self.info.show_text(message, ok="info")
        else:
            self.info.hide()

    def _select_new(self, sources, new):
        """Like Pro, the new lines are the selection afterwards."""
        for layer, _ids in sources:
            layer.removeSelection()
        by_layer = {}
        for layer, fid in new:
            by_layer.setdefault(layer, []).append(fid)
        for layer, ids in by_layer.items():
            layer.selectByIds(ids)

    # ------------------------------------------------------------------ the run
    def split(self):
        sources = selected_sources(self.canvas())
        ok, message = self.status(sources)
        if not ok:
            self.info.show_text(message or NEED_SELECTION, ok="info")
            return None
        try:
            result = split_into_cogo(sources, self.template(), CogoSettings.load(), self.split_existing)
        except ValueError as exc:
            self._result = (f"Split failed: {exc}.", False)
            self.refresh_page()
            return None
        created = result["created"]
        if created:
            text = "Split Into COGO Lines completed."
        else:
            text = "Nothing was split."
        notes = []
        if result["existing"]:
            notes.append(f"{result['existing']} with existing COGO attributes left alone")
        if result["added"]:
            notes.append("added " + ", ".join(result["added"]))
        if result["missing"]:
            notes.append(f"no COGO fields in {', '.join(result['missing'])}")
        if result["dropped"]:
            notes.append(f"{result['dropped']} course(s) could not be created")
        if notes:
            text += f" ({'; '.join(notes)})"
        if result["failed"]:
            text += f" Failed in: {', '.join(result['failed'])}."
        if created:
            self._select_new(sources, result["new"])
        self._result = (text, bool(created) and not result["failed"])
        self._result_sig = frozenset((layer.id(), fid) for layer, ids in selected_sources(self.canvas())
                                     for fid in ids)
        self.say(text + (" (Ctrl+Z undoes it)" if created else ""))
        self.refresh_page()
        return result


TOOL_CLASSES = {SplitCogoTool.KEY: SplitCogoTool}

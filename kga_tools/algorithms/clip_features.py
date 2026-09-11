# -*- coding: utf-8 -*-
"""
Clip (interactive)
==================

Replica of the ArcGIS Pro *Modify Features > Clip* pane.

Select the features that define the cut - a road corridor, a reservoir
footprint, a proposed canal - give them a buffer distance if the cut is wider
than they are, and then either take that area out of everything around them or
keep only that area.

Pro's two words for it are the ones on the pane:

* **Discard the area that intersects** - the clip is a hole. Parcels lose the
  strip the road takes; a parcel entirely inside the corridor is removed.
* **Preserve the area that intersects** - the clip is a cookie cutter. Each
  feature is reduced to the part inside it, and anything outside disappears.

Two things this pane insists on, because getting either wrong is expensive:

* **You choose what gets clipped.** Pro clips every editable layer, which is
  fine when one layer is open for editing and alarming when six are. The list
  is explicit, it starts on the layers already in edit mode, and every one of
  them is opened for editing before a single feature is touched - half a clip
  is worse than none of one.
* **The clipping features are never clipped.** When the corridor and the
  parcels are in the same layer, the selected features are skipped, so the
  corridor does not eat itself.
"""

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProject,
    QgsVectorLayer,
)

from ..core.compat import no_threading
from ..core.modify_features import (
    CLIP_DISCARD,
    CLIP_LABELS,
    buffer_geometry,
    clip_geometry,
    dissolve,
    fit_to_layer,
    layer_filter,
)
from ..gui.modify_base import carry_attributes, is_deleted, make_feature
from ..gui.modify_dialog import (
    ModifyFeaturesDialog,
    close_dialog,
    show_dialog,
)

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None


class ClipDialog(ModifyFeaturesDialog):
    """The Clip window."""

    TITLE = 'Clip'
    ACTION_LABEL = 'Clip'
    HINT = ('Tick the layers to clip and choose whether the overlap is '
            'discarded or preserved, press Clip, then click the feature that '
            'defines the cut. Drag a path across several to cut with all of '
            'them at once.')

    LAYER_FILTER = layer_filter('VectorLayer')
    EDITS_SOURCE = False                    # the ticked layers are what is written

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        self.distance_spin, self.unit_combo = self.add_distance_row(
            'Buffer distance', value=0.0, minimum=0.0)
        self.distance_spin.setToolTip(
            'How far beyond the selected features the cut reaches. Leave it at '
            'zero to clip with the features themselves.')

        self.mode_combo = self.add_choice('Clip', CLIP_LABELS, CLIP_DISCARD)

        self.target_list = QListWidget()
        self.target_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self.target_list.setMaximumHeight(180)
        self.target_list.setToolTip(
            'The layers whose features are clipped. Each one is opened for '
            'editing before anything is written.')
        form.addRow('Layers to clip', self.target_list)

        self.target_list.itemChanged.connect(self._targets_changed)
        QgsProject.instance().layersAdded.connect(self._rebuild_targets)
        QgsProject.instance().layersRemoved.connect(self._rebuild_targets)
        self._rebuild_targets()

    def _rebuild_targets(self, *args):
        """List every vector layer, ticking the ones already being edited.

        Starting on the edit-mode layers is the closest honest reading of Pro's
        "clips the editable layers", without silently reaching into a layer the
        user never opened.
        """
        ticked = self.target_names()
        first_run = not ticked and self.target_list.count() == 0

        self.target_list.blockSignals(True)
        self.target_list.clear()
        source = self.current_layer()
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or is_deleted(layer):
                continue
            if not layer.isSpatial():
                continue
            item = QListWidgetItem(layer.name())
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = layer.name() in ticked or (first_run and layer.isEditable())
            item.setCheckState(Qt.CheckState.Checked if on
                               else Qt.CheckState.Unchecked)
            if source is not None and layer.id() == source.id():
                item.setToolTip(
                    'The selected features are skipped, so the clip boundary '
                    'does not clip itself.')
            self.target_list.addItem(item)
        self.target_list.blockSignals(False)
        self._targets_changed()

    def _targets_changed(self, *args):
        """Nothing to recompute: what gets clipped is decided on the click."""

    def target_names(self):
        return {self.target_list.item(row).text()
                for row in range(self.target_list.count())
                if self.target_list.item(row).checkState() == Qt.CheckState.Checked}

    def target_layers(self):
        layers = []
        project = QgsProject.instance()
        for row in range(self.target_list.count()):
            item = self.target_list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            layer = project.mapLayer(item.data(Qt.ItemDataRole.UserRole))
            if isinstance(layer, QgsVectorLayer) and not is_deleted(layer):
                layers.append(layer)
        return layers

    def edit_targets(self):
        return self.target_layers()

    def blocker(self):
        """Clip needs something to cut.

        "Layers to clip" starts with only the layers already in edit mode
        ticked, so on a project where nothing is being edited yet the list
        opens empty. Saying so is the difference between that and a button
        that looks broken.
        """
        if not self.target_layers():
            return 'Tick at least one layer to clip in the list above.'
        return ''

    def on_layer_changed(self, layer):
        if hasattr(self, 'target_list'):
            self._rebuild_targets()

    # ------------------------------------------------------ clip boundary --

    def clip_boundary(self, layer, features):
        """The cut, in the source layer CRS: the features, buffered if asked."""
        if not features:
            return None

        distance = self.distance_spin.value()
        workspace = self.make_workspace(layer, self.unit_combo.currentData())
        offset = workspace.distance(distance) if distance > 0 else 0.0

        shapes = []
        for feature in features:
            geometry = feature.geometry()
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                continue
            if offset > 0:
                try:
                    working = workspace.to_work(geometry)
                except ValueError:
                    continue
                buffered = buffer_geometry(working, offset, 8, 'Round', 'Round')
                if buffered is None:
                    continue
                try:
                    shapes.append(workspace.from_work(buffered))
                except ValueError:          # pragma: no cover
                    continue
            else:
                shapes.append(QgsGeometry(geometry))

        return dissolve(shapes)

    def result_for(self, feature):
        """Hovering shows the cut this feature would make."""
        layer = self.current_layer()
        if layer is None:
            return []
        boundary = self.clip_boundary(layer, [feature])
        return [] if boundary is None else [boundary]

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        boundary = self.clip_boundary(layer, features)
        if boundary is None:
            return 'That feature has no geometry to cut with.'

        targets = self.target_layers()
        mode = self.mode_combo.currentIndex()
        # Never clip the boundary with itself.
        protected = {feature.id() for feature in features}

        changed = 0
        added = 0
        removed = 0
        touched = []

        for target in targets:
            shape = self._boundary_in(boundary, layer, target)
            if shape is None:
                continue

            edits = []
            deletes = []
            request = QgsFeatureRequest().setFilterRect(shape.boundingBox())
            for feature in target.getFeatures(request):
                if target.id() == layer.id() and feature.id() in protected:
                    continue
                geometry = feature.geometry()
                if geometry is None or geometry.isNull() or geometry.isEmpty():
                    continue
                if not geometry.intersects(shape):
                    continue

                clipped = clip_geometry(geometry, shape, mode)
                if clipped is None:
                    deletes.append(feature.id())
                    continue
                if clipped.equals(geometry):
                    continue                # nothing to write, so no edit
                parts = fit_to_layer(clipped, target)
                if not parts:
                    deletes.append(feature.id())
                    continue
                # A cut straight through a feature leaves two pieces. In a
                # multipart layer that is still one feature; in a single-part
                # one it has to become two, and dropping the second would take
                # half a parcel off the map without saying so.
                edits.append((feature, parts))

            if not edits and not deletes:
                continue

            target.beginEditCommand('KGA Clip')
            try:
                for feature, parts in edits:
                    if target.changeGeometry(feature.id(), parts[0]):
                        changed += 1
                    # `carry_attributes` rather than the raw attribute list:
                    # position 0 of that list is the primary key, and handing a
                    # provider the id of the feature being split asks it to
                    # write a duplicate.
                    values = (carry_attributes(feature, target, target)
                              if parts[1:] else {})
                    for extra in parts[1:]:
                        if target.addFeature(
                                make_feature(target, extra, values)):
                            added += 1
                for fid in deletes:
                    if target.deleteFeature(fid):
                        removed += 1
            except Exception:
                target.destroyEditCommand()
                raise
            target.endEditCommand()
            target.triggerRepaint()
            touched.append(target.name())

        if not touched:
            return 'Nothing intersected the clip boundary.'
        self.count(changed + added + removed)
        split = '' if not added else ', {} split off as new features'.format(added)
        return '{} feature{} clipped, {} removed{}, in {}.'.format(
            changed, '' if changed == 1 else 's', removed, split,
            ', '.join(touched))

    @staticmethod
    def _boundary_in(boundary, source, target):
        """The clip boundary in `target`'s CRS."""
        if not target.crs().isValid() or target.crs() == source.crs():
            return boundary
        shape = QgsGeometry(boundary)
        transform = QgsCoordinateTransform(source.crs(), target.crs(),
                                           QgsProject.instance())
        try:
            if shape.transform(transform) != 0:
                return None
        except Exception:                   # pragma: no cover
            return None
        return shape

    # -------------------------------------------------------------- close --

    def cleanup(self):
        for signal, slot in ((QgsProject.instance().layersAdded, self._rebuild_targets),
                             (QgsProject.instance().layersRemoved, self._rebuild_targets)):
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        super().cleanup()


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_clip_features():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(ClipDialog)


class ClipFeaturesAlgorithm(QgsProcessingAlgorithm):
    """Show the Clip pane."""

    def tr(self, string):
        return QCoreApplication.translate('KgaClipFeatures', string)

    def createInstance(self):
        return ClipFeaturesAlgorithm()

    def name(self):
        return 'clip_features'

    def displayName(self):
        return self.tr('Clip')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/clip_features'

    def shortHelpString(self):
        return self.tr(
            'Clip features with the current selection, the way ArcGIS Pro\'s '
            '<i>Modify Features > Clip</i> does.\n\n'
            'Select the features that define the cut, give them a <b>Buffer '
            'distance</b> if the cut is wider than they are, tick the '
            '<b>Layers to clip</b>, and choose what happens to the overlap:\n\n'
            '• <b>Discard the area that intersects</b> — the clip is a hole. '
            'Features lose the part inside it, and a feature entirely inside '
            'is removed.\n'
            '• <b>Preserve the area that intersects</b> — the clip is a cookie '
            'cutter. Each feature is reduced to the part inside it.\n\n'
            'The list starts on the layers already in edit mode, and every '
            'ticked layer is opened for editing before a single feature is '
            'touched. The selected features themselves are never clipped, so a '
            'boundary in the same layer as its neighbours does not eat '
            'itself.\n\n'
            'The boundary is drawn in cyan before anything is written, and '
            'each layer\'s changes are one undo step. The pane stays open '
            'while you work — close this Processing window once it appears.'
        )

    def flags(self):
        # Opens a dock and writes into project layers: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Clip is an interactive tool and needs the QGIS map canvas, so '
                'it cannot run from a head-less Processing session.'))
        show_dialog(iface, ClipDialog)
        feedback.pushInfo(self.tr(
            'Clip opened. You can close this Processing dialog and keep '
            'working on the map.'))
        return {}

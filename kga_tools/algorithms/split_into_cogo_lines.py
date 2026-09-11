# -*- coding: utf-8 -*-
"""
Split into COGO Lines (interactive)
===================================

Replica of the ArcGIS Pro *Modify Features > Split into COGO Lines* pane.

Take a boundary - a parcel polygon, a right of way, a surveyed traverse - and
break it into one line feature per course, each carrying the four COGO values
a deed is written in: **Direction**, **Distance**, **Radius** and **ArcLength**.

The parts that decide whether the result matches the survey it came from:

* **Arcs stay arcs.** A File Geodatabase parcel boundary holds true circular
  arcs. Segmenting one into chords would lengthen the boundary and lose the
  curve entirely, so an arc comes through as a single COGO line whose radius
  and arc length describe it, with Direction and Distance giving the chord -
  which is what chord bearing and chord distance mean on a plat.
* **The radius is signed.** Positive turns clockwise from the start of the
  course, negative counter-clockwise. Reading a description back without the
  sign gives a mirror image of the curve.
* **Directions say which convention they are in.** North azimuth, south
  azimuth, polar and quadrant bearing, written as decimal degrees, packed
  degrees-minutes-seconds, gradians or radians - the same four by four ArcGIS
  offers. A number with no convention beside it is not a direction.
* **Bearings are grid bearings.** They are measured in the projected CRS the
  lengths are measured in, so a layer stored in degrees is taken through its
  UTM zone rather than having angles read off a plate carree.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QCheckBox

from qgis.core import (
    QgsGeometry,
    QgsProcessingAlgorithm,
    QgsProcessingException,
)

from ..core import compat
from ..core.compat import no_threading
from ..core.modify_features import (
    COGO_ARCLENGTH,
    COGO_DIRECTION,
    COGO_DISTANCE,
    COGO_FIELDS,
    COGO_RADIUS,
    DIRECTION_TYPES,
    DIRECTION_UNITS,
    DIR_DMS,
    DIR_NORTH_AZIMUTH,
    DIR_QUADRANT,
    DISTANCE_UNITS,
    cogo_of,
    curves_of,
    direction_number,
    direction_text,
    fit_to_layer,
    geometry_type,
    layer_filter,
    segment_curves,
)
from ..gui.modify_base import carry_attributes, make_feature
from ..gui.modify_dialog import (
    ModifyFeaturesDialog,
    close_dialog,
    show_dialog,
)

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None


def find_cogo_field(fields, wanted):
    """The index of a COGO field, matched the way ArcGIS matches it.

    Case-insensitively, and ignoring an underscore, so `ARCLENGTH`, `ArcLength`
    and `arc_length` all count as the arc length field. A geodatabase that has
    been through three agencies rarely spells them the same way twice.
    """
    target = wanted.lower().replace('_', '')
    for index, field in enumerate(fields):
        if field.name().lower().replace('_', '') == target:
            return index
    return -1


class SplitIntoCogoLinesDialog(ModifyFeaturesDialog):
    """The Split into COGO Lines window."""

    TITLE = 'Split into COGO Lines'
    ACTION_LABEL = 'Split into COGO Lines'
    HINT = ('Press Split into COGO Lines, then hover a boundary to see its '
            'courses and click to write them. Each straight run and each arc '
            'becomes one line with its own COGO values.')

    LAYER_FILTER = layer_filter('LineLayer', 'PolygonLayer')
    ACCEPTS = ('Line', 'Polygon')
    EDITS_SOURCE = False                    # decided by the template and the delete box

    # ---------------------------------------------------------- parameters --

    def build_parameters(self, form):
        self.template_combo = self.add_template_combo(
            'Template', layer_filter('LineLayer'),
            'The line layer the COGO lines are created in. A polygon boundary '
            'has to go somewhere else; a line layer can split into itself.')

        self.direction_combo = self.add_choice(
            'Direction type', DIRECTION_TYPES, DIR_NORTH_AZIMUTH)
        self.direction_combo.setToolTip(
            'Where zero points and which way the angle runs. North azimuth is '
            'clockwise from north; polar is counter-clockwise from east.')

        self.direction_unit_combo = self.add_choice(
            'Direction units', DIRECTION_UNITS, DIR_DMS)
        self.direction_unit_combo.setToolTip(
            'Degrees minutes seconds is stored packed in a numeric field - '
            '45 degrees 30 minutes 15 seconds is the number 45.3015 - and '
            'written out in full in a text field.')

        self.distance_unit_combo = self.add_unit_combo(
            'Distance units', DISTANCE_UNITS, 'Meters')
        self.distance_unit_combo.setToolTip(
            'The unit Distance, Radius and ArcLength are written in.')

        self.arcs_check = QCheckBox('Keep circular arcs as single courses')
        self.arcs_check.setChecked(True)
        self.arcs_check.setToolTip(
            'On, an arc is one COGO line with a radius and an arc length. Off, '
            'it is broken into straight chords, which lengthens the boundary.')
        form.addRow('', self.arcs_check)

        self.create_check = QCheckBox('Create the COGO fields if they are missing')
        self.create_check.setChecked(True)
        self.create_check.setToolTip(
            'Adds Direction, Distance, Radius and ArcLength to the template '
            'layer. Existing fields are matched by name and left alone.')
        form.addRow('', self.create_check)

        self.attributes_check = QCheckBox('Copy the source attributes')
        self.attributes_check.setChecked(True)
        form.addRow('', self.attributes_check)

        self.delete_check = QCheckBox('Delete the original feature')
        self.delete_check.setChecked(True)
        self.delete_check.setToolTip(
            'On - the ArcGIS default - the boundary is replaced by its '
            'courses. Off, the original is kept alongside them.')
        form.addRow('', self.delete_check)

    def on_layer_changed(self, layer):
        # A line layer splits into itself; a polygon boundary has to go
        # somewhere else, and there the combo's own first choice stands.
        if layer is not None and layer.geometryType() == geometry_type('Line'):
            self.follow_source(layer)

    def target_layer(self):
        # No falling back to the source: a polygon boundary cannot hold its
        # own courses.
        return self.template_combo.currentLayer()

    def edit_targets(self):
        targets = []
        target = self.target_layer()
        if target is not None:
            targets.append(target)
        # Deleting the original touches the source layer too, and it may not be
        # the template. Compared by id: two handles on one layer are not always
        # the same Python object.
        layer = self.current_layer()
        if (self.delete_check.isChecked() and layer is not None
                and not any(t.id() == layer.id() for t in targets)):
            targets.append(layer)
        return targets

    def blocker(self):
        if self.target_layer() is None:
            return 'Choose a line layer for the COGO lines to go in.'
        return ''

    # --------------------------------------------------------------- split --

    def courses_of(self, feature, layer):
        """`(geometry, cogo values)` for every course of one feature.

        Geometries come back in the layer CRS ready to write; the COGO values
        are already converted into the direction convention and the distance
        unit the dialog is set to.
        """
        geometry = feature.geometry()
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return []

        unit_name = self.distance_unit_combo.currentData()
        workspace = self.make_workspace(layer, unit_name)
        try:
            working = workspace.to_work(geometry)
        except ValueError:
            return []

        keep_arcs = self.arcs_check.isChecked()
        courses = []
        for curve in curves_of(working):
            for piece in segment_curves(curve, keep_arcs):
                values = cogo_of(piece)
                for key in ('distance', 'radius', 'arclength'):
                    values[key] = workspace.to_distance_unit(values[key],
                                                             unit_name)
                try:
                    shape = workspace.from_work(QgsGeometry(piece.clone()))
                except ValueError:          # pragma: no cover
                    continue
                courses.append((shape, values))
        return courses

    def result_for(self, feature):
        layer = self.current_layer()
        if layer is None:
            return []
        return [geometry for geometry, _values in self.courses_of(feature,
                                                                  layer)]

    # ---------------------------------------------------- the COGO columns --

    def _ensure_cogo_fields(self, target):
        """Field indices for the four COGO columns, adding any that are missing.

        Returns `(indices, added names)`. A provider that will not take a new
        field - a read-only join, a view - leaves the index at -1 and the value
        simply is not written, rather than failing the whole split.
        """
        added = []
        if self.create_check.isChecked():
            missing = [name for name in COGO_FIELDS
                       if find_cogo_field(target.fields(), name) < 0]
            if missing:
                fields = [compat.make_field(name, compat.T_DOUBLE, 0, 8)
                          for name in missing]
                try:
                    if target.isEditable():
                        # Through the edit buffer, so the new columns are part
                        # of the same undo step as the lines written into them.
                        # Going straight to the provider while an edit session
                        # is open leaves the buffer holding stale field indices.
                        if all(target.addAttribute(field) for field in fields):
                            added = list(missing)
                    elif target.dataProvider().addAttributes(fields):
                        target.updateFields()
                        added = list(missing)
                except Exception:           # pragma: no cover - provider said no
                    added = []

        indices = {name: find_cogo_field(target.fields(), name)
                   for name in COGO_FIELDS}
        return indices, added

    def _cogo_values(self, values, target, indices):
        """The four COGO values as a {field index: value} map for `target`.

        A text Direction column gets the written form - `N45-30-15.00E` - and a
        numeric one gets the number. Quadrant bearing is the reason both exist:
        the two letters are part of the value, so a quadrant layer with a
        numeric Direction column can only store the angle, and the pane says so.
        """
        direction_type = self.direction_combo.currentIndex()
        direction_unit = self.direction_unit_combo.currentIndex()

        out = {}
        index = indices.get(COGO_DIRECTION, -1)
        if index >= 0:
            field = target.fields().at(index)
            if field.type() == compat.T_STRING:
                out[index] = direction_text(values['direction'], direction_type,
                                            direction_unit)
            else:
                out[index] = direction_number(values['direction'],
                                              direction_type, direction_unit)

        for name, key in ((COGO_DISTANCE, 'distance'),
                          (COGO_RADIUS, 'radius'),
                          (COGO_ARCLENGTH, 'arclength')):
            index = indices.get(name, -1)
            if index < 0:
                continue
            number = values[key]
            field = target.fields().at(index)
            if field.type() == compat.T_STRING:
                out[index] = '{:.4f}'.format(number)
            else:
                out[index] = round(number, 6)
        return out

    # ---------------------------------------------------------------- run --

    def apply_to(self, features):
        layer = self.current_layer()
        target = self.target_layer()

        courses = []
        for feature in features:
            for geometry, values in self.courses_of(feature, layer):
                courses.append((feature, geometry, values))
        if not courses:
            return 'No courses could be read from {} feature{}.'.format(
                len(features), '' if len(features) == 1 else 's')

        indices, added = self._ensure_cogo_fields(target)
        missing = [name for name in COGO_FIELDS if indices.get(name, -1) < 0]
        carry = self.attributes_check.isChecked()
        drop_source = self.delete_check.isChecked()

        added_count = 0
        dropped = 0
        source_ids = set()

        target.beginEditCommand('KGA Split into COGO Lines')
        try:
            for feature, geometry, values in courses:
                attributes = (carry_attributes(feature, layer, target)
                              if carry else {})
                attributes.update(self._cogo_values(values, target, indices))
                parts = fit_to_layer(geometry, target)
                if not parts:
                    dropped += 1
                    continue
                for part in parts:
                    if target.addFeature(
                            make_feature(target, part, attributes)):
                        added_count += 1
                        source_ids.add(feature.id())
                    else:
                        dropped += 1
        except Exception:
            target.destroyEditCommand()
            raise
        target.endEditCommand()

        removed = 0
        if drop_source and source_ids:
            # A second command, because the originals live in the source layer
            # and an undo has to be able to take back each layer's own change.
            layer.beginEditCommand('KGA Split into COGO Lines (remove originals)')
            try:
                for fid in source_ids:
                    if layer.deleteFeature(fid):
                        removed += 1
            except Exception:
                layer.destroyEditCommand()
                raise
            layer.endEditCommand()

        target.triggerRepaint()
        if removed:
            layer.triggerRepaint()

        notes = []
        if added:
            notes.append('added ' + ', '.join(added))
        if missing:
            notes.append('no {} field'.format(' or '.join(missing)))
        if removed:
            notes.append('{} original{} removed'.format(
                removed, '' if removed == 1 else 's'))
        if dropped:
            notes.append('{} course{} could not be created'.format(
                dropped, '' if dropped == 1 else 's'))
        if (self.direction_combo.currentIndex() == DIR_QUADRANT
                and indices.get(COGO_DIRECTION, -1) >= 0
                and target.fields().at(indices[COGO_DIRECTION]).type()
                != compat.T_STRING):
            notes.append('the quadrant letters need a text Direction field')

        self.count(added_count)
        tail = ' ({}).'.format('; '.join(notes)) if notes else '.'
        return '{} COGO line{} from {} feature{} written to "{}"{}'.format(
            added_count, '' if added_count == 1 else 's',
            len(features), '' if len(features) == 1 else 's',
            target.name(), tail)


# --------------------------------------------------------------------------- #
#  launcher
# --------------------------------------------------------------------------- #

def close_split_into_cogo_lines():
    """Close the dialog. Called by `KgaToolsPlugin.unload`."""
    close_dialog(SplitIntoCogoLinesDialog)


class SplitIntoCogoLinesAlgorithm(QgsProcessingAlgorithm):
    """Show the Split into COGO Lines pane."""

    def tr(self, string):
        return QCoreApplication.translate('KgaSplitIntoCogoLines', string)

    def createInstance(self):
        return SplitIntoCogoLinesAlgorithm()

    def name(self):
        return 'split_into_cogo_lines'

    def displayName(self):
        return self.tr('Split into COGO Lines')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/split_into_cogo_lines'

    def shortHelpString(self):
        return self.tr(
            'Break the selected boundaries into one line per course, each with '
            'its COGO values, the way ArcGIS Pro\'s <i>Modify Features > Split '
            'into COGO Lines</i> does.\n\n'
            'Select lines or polygons, choose the <b>Template</b> line layer '
            'the courses are written to, and set the direction convention:\n\n'
            '• <b>Direction type</b> — north azimuth, south azimuth, polar or '
            'quadrant bearing\n'
            '• <b>Direction units</b> — decimal degrees, degrees minutes '
            'seconds, gradians or radians\n'
            '• <b>Distance units</b> — what Distance, Radius and ArcLength are '
            'written in\n\n'
            'A circular arc stays one course: Direction and Distance give its '
            'chord, and Radius and ArcLength describe the curve. The radius is '
            'signed — positive turns clockwise from the start of the course.\n\n'
            'The four COGO fields are matched by name, ignoring case and '
            'underscores, and created when they are missing. A text Direction '
            'field is written in full (<i>N45-30-15.00E</i>); a numeric one '
            'takes the packed number.\n\n'
            'Bearings are grid bearings, measured in the same projected CRS as '
            'the lengths.\n\n'
            'One press is one undo step per layer. The pane stays open while '
            'you work — close this Processing window once it appears.'
        )

    def flags(self):
        # Opens a dock and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        if iface is None:
            raise QgsProcessingException(self.tr(
                'Split into COGO Lines is an interactive tool and needs the '
                'QGIS map canvas, so it cannot run from a head-less Processing '
                'session.'))
        show_dialog(iface, SplitIntoCogoLinesDialog)
        feedback.pushInfo(self.tr(
            'Split into COGO Lines opened. You can close this Processing '
            'dialog and keep working on the map.'))
        return {}

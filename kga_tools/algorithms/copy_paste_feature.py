# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsMessageLog,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProject,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand

from ..branding import LOG_TAG, docs_url, help_button
from ..core import schema
from ..core.compat import no_threading, type_name
# `qgis.PyQt.sip` is the name that works both where sip is a
# top-level module and where it is only PyQt5.sip; see modify_base.
from ..gui.modify_base import is_deleted as _is_deleted

try:
    from qgis.utils import iface
except ImportError:  # pragma: no cover - head-less
    iface = None

#: The one open dialog, if any. Module-level for the same reason the Sequential
#: Numbering dialog is: it is parented to the QGIS main window, not to anything
#: the run that opened it owns, so `unload` needs a way to reach it.
DIALOG_INSTANCE = None

#: How a target field is filled. The first half of a mapping rule.
SOURCE_NONE = '__none__'        # leave it to the layer's own default
SOURCE_FIXED = '__fixed__'      # a literal typed into the table
SOURCE_FIELD = '__field__'      # a field of the copied feature

#: What to do with a multipart geometry going into a single-part target.
MULTI_EXPLODE, MULTI_SKIP = range(2)

#: Attribute handling modes, in the order the combo shows them.
ATTR_MATCH, ATTR_MANUAL, ATTR_NONE = range(3)

ATTR_MODES = (
    'Match target fields by name',
    'Map fields manually',
    'Do not copy attributes (use the target defaults)',
)

MATCH_STRATEGIES = (
    ('Names must match exactly', schema.STRATEGY_EXACT),
    ('Ignore upper/lower case', schema.STRATEGY_CASE_INSENSITIVE),
    ('Ignore case, spaces and underscores', schema.STRATEGY_FUZZY),
)

MULTIPART_OPTIONS = (
    'Explode it into one feature per part',
    'Skip the feature',
)

#: Copying more than this many features at once is usually a slip, and the
#: clipboard holds them in memory until it is cleared.
LARGE_SELECTION = 2000


# --------------------------------------------------------------------------- #
#  enum shims
# --------------------------------------------------------------------------- #

def geometry_type(name):
    """`Qgis.GeometryType.Polygon` etc., falling back to `QgsWkbTypes`.

    The scoped enum spells these `Null` / `Unknown`; the old one spells them
    `NullGeometry` / `UnknownGeometry`. The underlying values are the same, so
    either can be compared against `QgsWkbTypes.geometryType()`.
    """
    try:
        return getattr(Qgis.GeometryType, name)
    except AttributeError:                  # pragma: no cover - QGIS < 3.30
        return getattr(QgsWkbTypes, name + 'Geometry')


def flat_wkb_type(name):
    """`Qgis.WkbType.Unknown` etc., falling back to `QgsWkbTypes`."""
    try:
        return getattr(Qgis.WkbType, name)
    except AttributeError:                  # pragma: no cover - QGIS < 3.30
        return getattr(QgsWkbTypes, name)


def wkb_display(value):
    """'MultiPolygonZ' for a layer's `wkbType()`.

    From 3.30 the argument is the scoped `Qgis.WkbType` and an `int` is
    rejected outright; before that it was the int-based `QgsWkbTypes.Type`,
    which takes the value either way. Passing it through untouched first
    covers both.
    """
    if value is None:
        return 'no geometry'
    try:
        return QgsWkbTypes.displayString(value)
    except TypeError:                       # pragma: no cover - QGIS < 3.30
        try:
            return QgsWkbTypes.displayString(int(value))
        except Exception:
            return str(value)


# --------------------------------------------------------------------------- #
#  the clipboard
# --------------------------------------------------------------------------- #

class _Clipboard(object):
    """Copies of features, held apart from the layer they came from.

    Geometries and attribute values are copied out at the moment of the copy.
    The source layer is remembered by name and id only, for the status line -
    nothing here keeps it alive or reads from it again.
    """

    def __init__(self):
        self.clear()

    def clear(self):
        self.source_name = ''
        self.source_id = ''
        self.crs = None
        self.fields = None                  # QgsFields, copied from the source
        self.wkb_type = None
        self.rows = []                      # [(QgsGeometry|None, {name: value})]

    def is_empty(self):
        return not self.rows

    def count(self):
        return len(self.rows)

    def field_names(self):
        return [field.name() for field in self.fields] if self.fields else []

    def take(self, layer, features, append=False):
        """Copy `features` of `layer` onto the clipboard; return how many.

        `append` only holds when the features come from the layer already on
        the clipboard: attribute names and CRS have to mean one thing for the
        whole clipboard, or the paste mapping would be a lie.
        """
        keep = append and self.source_id == layer.id() and not self.is_empty()
        rows = list(self.rows) if keep else []

        fields = layer.fields()
        names = [field.name() for field in fields]
        added = 0
        for feature in features:
            geometry = feature.geometry()
            if geometry is None or geometry.isNull():
                geometry = None
            else:
                geometry = QgsGeometry(geometry)
            rows.append((geometry, dict(zip(names, feature.attributes()))))
            added += 1

        self.source_name = layer.name()
        self.source_id = layer.id()
        self.crs = layer.crs()
        self.fields = fields
        self.wkb_type = layer.wkbType()
        self.rows = rows
        return added

    def describe(self):
        if self.is_empty():
            return 'Clipboard is empty.'
        crs = (self.crs.authid()
               if self.crs is not None and self.crs.isValid() else 'no CRS')
        return '{} feature(s) from "{}" - {}, {}'.format(
            self.count(), self.source_name, wkb_display(self.wkb_type), crs)


#: One clipboard for the whole plugin, so a copy survives closing the dialog.
CLIPBOARD = _Clipboard()


# --------------------------------------------------------------------------- #
#  canvas housekeeping
# --------------------------------------------------------------------------- #

#: Stamped on our rubber band so a later run can recognise one of ours among
#: everything else the canvas scene holds - other plugins' bands, the measure
#: tool's, the snapping markers - and take only those back.
BAND_TAG_KEY = 0
BAND_TAG = 'kga-copy-paste-clipboard'


def sweep_stale_bands(canvas, keep=None):
    """Drop clipboard outlines an earlier dialog left behind.

    `_teardown` is the belt; this is the braces. A plugin reload, a crash in a
    slot, or anything else that kills the dialog without running its clean-up
    would otherwise strand an orange outline on the canvas that no layer owns
    and nothing can clear short of restarting QGIS.
    """
    try:
        scene = canvas.scene()
        items = list(scene.items()) if scene is not None else []
    except Exception:                       # pragma: no cover
        return
    for item in items:
        if item is keep:
            continue
        try:
            if item.data(BAND_TAG_KEY) == BAND_TAG:
                scene.removeItem(item)
        except Exception:                   # pragma: no cover
            continue


# --------------------------------------------------------------------------- #
#  map tool
# --------------------------------------------------------------------------- #

class CopyPickMapTool(QgsMapTool):
    """Click a feature on the canvas to put it straight on the clipboard."""

    message = pyqtSignal(str)

    PICK_RADIUS_PX = 6

    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller        # the dialog, asked for the layer
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        layer = self.controller.current_layer()
        if layer is None:
            self.message.emit('Choose a source layer first.')
            return

        # Ctrl or Shift adds to the clipboard instead of replacing it, the way
        # they add to a selection everywhere else in QGIS.
        adding = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                           | Qt.KeyboardModifier.ShiftModifier))

        features = self._features_under(layer, event.pos())
        if not features:
            self.message.emit('No feature at that point.')
            return
        self.controller.take(layer, features, append=adding)

    def _features_under(self, layer, pos):
        """The one feature under the cursor, smallest hit first.

        Smallest rather than first: where a small polygon sits inside a large
        one, the small one is the one being aimed at.
        """
        point = self.toMapCoordinates(pos)
        tolerance = self.canvas.mapUnitsPerPixel() * self.PICK_RADIUS_PX
        search = QgsGeometry.fromPointXY(point).buffer(tolerance, 8)

        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if canvas_crs != layer.crs():
            transform = QgsCoordinateTransform(
                canvas_crs, layer.crs(), QgsProject.instance())
            try:
                search.transform(transform)
            except Exception as exc:        # pragma: no cover
                self.message.emit('Reprojection failed: {}'.format(exc))
                return []

        engine = QgsGeometry.createGeometryEngine(search.constGet())
        engine.prepareGeometry()

        hits = []
        for feature in layer.getFeatures(
                QgsFeatureRequest().setFilterRect(search.boundingBox())):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            if engine.intersects(geometry.constGet()):
                hits.append(feature)

        if not hits:
            return []
        hits.sort(key=_pick_weight)
        return hits[:1]


def _pick_weight(feature):
    """Area, then length: the yardstick behind "smallest hit wins"."""
    geometry = feature.geometry()
    try:
        return (geometry.area(), geometry.length())
    except Exception:                       # pragma: no cover
        return (0.0, 0.0)


# --------------------------------------------------------------------------- #
#  the paste itself - no Qt, so it can be reasoned about (and tested) alone
# --------------------------------------------------------------------------- #

class PasteReport(object):
    """What one paste actually did, down to each value that had to change."""

    def __init__(self):
        self.added = 0
        self.exploded = 0               # extra features made by splitting
        self.reprojected = False
        self.new_ids = []
        self.skipped = []               # [(row number, why)]
        self.coercions = []             # [(field, old, new, why)]
        self.violations = []            # [(field, value, why)] - left NULL
        self.unmapped = []              # target fields left at their default

    def summary(self):
        parts = ['{} feature(s) pasted'.format(self.added)]
        if self.exploded:
            parts.append('{} extra from exploding multiparts'.format(self.exploded))
        if self.reprojected:
            parts.append('reprojected')
        if self.coercions:
            parts.append('{} value(s) converted'.format(len(self.coercions)))
        if self.violations:
            parts.append('{} value(s) left NULL'.format(len(self.violations)))
        if self.skipped:
            parts.append('{} skipped'.format(len(self.skipped)))
        return ', '.join(parts) + '.'

    def details(self):
        """The long form, for the message box and the log."""
        lines = []
        if self.unmapped:
            lines.append('Target fields left at their own default:')
            lines += ['  - {}'.format(name) for name in self.unmapped]
        if self.coercions:
            lines.append('Values converted to fit the target field:')
            for field, old, new, why in self.coercions[:200]:
                lines.append('  - {}: {!r} -> {!r} ({})'.format(field, old, new, why))
        if self.violations:
            lines.append('Values that would not fit and were left NULL:')
            for field, value, why in self.violations[:200]:
                lines.append('  - {}: {!r} ({})'.format(field, value, why))
        if self.skipped:
            lines.append('Features not pasted:')
            for row, why in self.skipped[:200]:
                lines.append('  - feature {}: {}'.format(row, why))
        return '\n'.join(lines)

    def has_details(self):
        return bool(self.unmapped or self.coercions or self.violations
                    or self.skipped)


def fit_dimensions(geometry, target_wkb):
    """Add or drop Z and M so `geometry` matches what the target column holds.

    A Z geometry written into a 2D column loses its Z anyway; doing it here
    means the value is dropped once, deliberately, rather than by the provider
    halfway through the write.
    """
    abstract = geometry.get()
    if abstract is None:                    # pragma: no cover
        return
    source_wkb = geometry.wkbType()

    if QgsWkbTypes.hasZ(target_wkb) and not QgsWkbTypes.hasZ(source_wkb):
        abstract.addZValue(0.0)
    elif not QgsWkbTypes.hasZ(target_wkb) and QgsWkbTypes.hasZ(source_wkb):
        abstract.dropZValue()

    source_wkb = geometry.wkbType()
    if QgsWkbTypes.hasM(target_wkb) and not QgsWkbTypes.hasM(source_wkb):
        abstract.addMValue(0.0)
    elif not QgsWkbTypes.hasM(target_wkb) and QgsWkbTypes.hasM(source_wkb):
        abstract.dropMValue()


def fit_geometry(geometry, target_wkb, transform, multipart_mode, report, row):
    """The geometries to write for one copied feature, or None to skip it.

    Returns a list because a multipart geometry going into a single-part target
    becomes several features - the same thing ArcGIS does when you paste a
    multipart into a single-part feature class.
    """
    target_geom_type = QgsWkbTypes.geometryType(target_wkb)

    # A table with no geometry column takes the attributes and nothing else.
    if target_geom_type == geometry_type('Null'):
        return [None]
    if geometry is None or geometry.isEmpty():
        return [None]

    geometry = QgsGeometry(geometry)
    if transform is not None:
        try:
            geometry.transform(transform)
        except Exception as exc:            # pragma: no cover
            report.skipped.append((row, 'reprojection failed: {}'.format(exc)))
            return None

    # A generic or collection column takes whatever it is given; converting
    # towards it would only throw parts away.
    if QgsWkbTypes.flatType(target_wkb) in (flat_wkb_type('Unknown'),
                                            flat_wkb_type('GeometryCollection')):
        return [geometry]

    # Curves have to be segmented before they go into a straight-line column,
    # or the provider stores a straightened version without saying so.
    if (QgsWkbTypes.isCurvedType(geometry.wkbType())
            and not QgsWkbTypes.isCurvedType(target_wkb)):
        segmented = geometry.constGet().segmentize()
        if segmented is not None:
            geometry = QgsGeometry(segmented)

    parts = [geometry]
    if QgsWkbTypes.isMultiType(target_wkb):
        if not QgsWkbTypes.isMultiType(geometry.wkbType()):
            geometry.convertToMultiType()
    elif QgsWkbTypes.isMultiType(geometry.wkbType()):
        pieces = geometry.asGeometryCollection()
        if len(pieces) <= 1:
            # convertToSingleType keeps the first part only, which is exactly
            # right when there is only one part and wrong when there are more.
            geometry.convertToSingleType()
        elif multipart_mode == MULTI_EXPLODE:
            parts = pieces
            report.exploded += len(pieces) - 1
        else:
            report.skipped.append(
                (row, 'multipart geometry, and the target holds single parts'))
            return None

    for part in parts:
        fit_dimensions(part, target_wkb)
    return parts


def map_attributes(attributes, mapping, target_fields, report):
    """Turn one copied feature's values into ``{field index: value}``.

    Fields with no rule are simply absent from the result, which is what makes
    `QgsVectorLayerUtils.createFeature` fall back to the layer's own default
    value expression for them.
    """
    values = {}
    for name, rule in (mapping or {}).items():
        index = target_fields.lookupField(name)
        if index < 0:
            # The layer changed under the dialog. Reported, never guessed at.
            report.violations.append(
                (name, None, 'the target no longer has this field'))
            continue

        kind, payload = rule
        if kind == SOURCE_NONE:
            continue
        if kind == SOURCE_FIXED:
            raw = payload
        else:
            if payload not in attributes:
                report.violations.append(
                    (name, None,
                     'the copied features have no field "{}"'.format(payload)))
                continue
            raw = attributes.get(payload)

        field = target_fields.at(index)
        try:
            value, why = schema.coerce(raw, field)
        except schema.CoercionError as exc:
            report.violations.append((name, raw, str(exc)))
            continue
        if why:
            report.coercions.append((name, raw, value, why))
        values[index] = value
    return values


def paste_features(clipboard, layer, mapping, reproject=True,
                   multipart_mode=MULTI_EXPLODE):
    """Write the clipboard into `layer`. The layer must already be editable.

    One `beginEditCommand` wraps the whole run, so a paste is one undo step and
    a failure part-way through takes the whole thing back rather than leaving
    half the features behind.
    """
    report = PasteReport()
    target_fields = layer.fields()
    target_wkb = layer.wkbType()

    transform = None
    if (reproject and clipboard.crs is not None and clipboard.crs.isValid()
            and layer.crs().isValid() and clipboard.crs != layer.crs()):
        transform = QgsCoordinateTransform(
            clipboard.crs, layer.crs(), QgsProject.instance())
        report.reprojected = True

    mapped = {name for name, rule in (mapping or {}).items()
              if rule[0] != SOURCE_NONE}
    try:
        # The provider fills its own key; naming it here would be noise in
        # every single report.
        owned = {target_fields.at(i).name()
                 for i in (layer.primaryKeyAttributes() or [])
                 if 0 <= i < target_fields.count()}
    except Exception:                       # pragma: no cover
        owned = set()
    report.unmapped = [field.name() for field in target_fields
                       if field.name() not in mapped
                       and field.name() not in owned]

    context = layer.createExpressionContext()

    layer.beginEditCommand('KGA Paste Feature')
    try:
        for row, (geometry, attributes) in enumerate(clipboard.rows, start=1):
            values = map_attributes(attributes, mapping, target_fields, report)
            parts = fit_geometry(geometry, target_wkb, transform,
                                 multipart_mode, report, row)
            if parts is None:
                continue
            for part in parts:
                feature = QgsVectorLayerUtils.createFeature(
                    layer, part if part is not None else QgsGeometry(),
                    values, context)
                if layer.addFeature(feature):
                    report.added += 1
                    report.new_ids.append(feature.id())
                else:
                    report.skipped.append((row, 'the layer refused the feature'))
    except Exception:
        layer.destroyEditCommand()
        raise
    layer.endEditCommand()
    return report


# --------------------------------------------------------------------------- #
#  Paste Special popup
# --------------------------------------------------------------------------- #

class PasteSpecialDialog(QDialog):
    """Where the copied features go, and what happens to their attributes."""

    COL_TARGET, COL_TYPE, COL_SOURCE, COL_VALUE = range(4)

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle('Paste Special')
        self.setMinimumWidth(620)
        self.setMinimumHeight(520)

        self._loading = False               # guards the "edited by hand" flip
        self.last_report = None

        self._build_ui()
        self._connect()
        self._target_changed()

    # ----------------------------------------------------------------- ui --

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.source_label = QLabel(CLIPBOARD.describe())
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet('color: #666;')
        layout.addWidget(self.source_label)

        form = QFormLayout()
        self.target_combo = QgsMapLayerComboBox()
        self.target_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        form.addRow('Paste into', self.target_combo)
        layout.addLayout(form)

        self.target_label = QLabel('')
        self.target_label.setWordWrap(True)
        layout.addWidget(self.target_label)

        # --- attributes ----------------------------------------------------
        attributes = QGroupBox('Attributes')
        attr_layout = QVBoxLayout(attributes)

        attr_form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(ATTR_MODES)
        attr_form.addRow('Handling', self.mode_combo)

        self.strategy_combo = QComboBox()
        for label, _key in MATCH_STRATEGIES:
            self.strategy_combo.addItem(label)
        attr_form.addRow('Name matching', self.strategy_combo)
        attr_layout.addLayout(attr_form)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ['Target field', 'Type', 'Take value from', 'Fixed value'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_TARGET,
                                    QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_TYPE,
                                    QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SOURCE,
                                    QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_VALUE,
                                    QHeaderView.ResizeMode.Stretch)
        attr_layout.addWidget(self.table)

        self.mapping_note = QLabel('')
        self.mapping_note.setWordWrap(True)
        self.mapping_note.setStyleSheet('color: #666;')
        attr_layout.addWidget(self.mapping_note)
        layout.addWidget(attributes, 1)

        # --- geometry ------------------------------------------------------
        geometry = QGroupBox('Geometry')
        geometry_form = QFormLayout(geometry)
        self.reproject_check = QCheckBox(
            'Reproject to the target layer CRS')
        self.reproject_check.setChecked(True)
        geometry_form.addRow(self.reproject_check)

        self.multipart_combo = QComboBox()
        self.multipart_combo.addItems(MULTIPART_OPTIONS)
        self.multipart_combo.setToolTip(
            'Only used when a copied feature is multipart and the target holds '
            'single-part geometry.')
        geometry_form.addRow('Multipart into single', self.multipart_combo)
        layout.addWidget(geometry)

        # --- after the paste -----------------------------------------------
        after = QGroupBox('After pasting')
        after_layout = QVBoxLayout(after)
        self.select_check = QCheckBox('Select the pasted features')
        self.select_check.setChecked(True)
        after_layout.addWidget(self.select_check)
        self.zoom_check = QCheckBox('Zoom the map to them')
        after_layout.addWidget(self.zoom_check)
        self.keep_open_check = QCheckBox('Keep this window open after pasting')
        after_layout.addWidget(self.keep_open_check)
        layout.addWidget(after)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.paste_button = QPushButton('Paste')
        self.paste_button.setDefault(True)
        buttons.addWidget(self.paste_button)
        self.close_button = QPushButton('Close')
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('color: #666;')
        layout.addWidget(self.status_label)

    def _connect(self):
        self.target_combo.layerChanged.connect(self._target_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.strategy_combo.currentIndexChanged.connect(self._fill_from_mode)
        self.paste_button.clicked.connect(self._paste)
        self.close_button.clicked.connect(self.reject)

    # -------------------------------------------------------------- state --

    def _strategy(self):
        return MATCH_STRATEGIES[max(0, self.strategy_combo.currentIndex())][1]

    def _mode(self):
        return self.mode_combo.currentIndex()

    def _mode_changed(self):
        self.strategy_combo.setEnabled(self._mode() == ATTR_MATCH)
        if self._mode() != ATTR_MANUAL:
            self._fill_from_mode()

    def _target_changed(self):
        layer = self.target_combo.currentLayer()
        ok, note = self._compatibility(layer)
        self.target_label.setText(note)
        self.target_label.setStyleSheet(
            'color: #666;' if ok else 'color: #a40000; font-weight: bold;')
        self.paste_button.setEnabled(ok and not CLIPBOARD.is_empty())

        if layer is not None and layer.crs().isValid() and CLIPBOARD.crs is not None:
            same = layer.crs() == CLIPBOARD.crs
            self.reproject_check.setEnabled(not same)
            if same:
                self.reproject_check.setChecked(False)
            else:
                self.reproject_check.setChecked(True)

        self._build_table(layer)
        self._fill_from_mode()

    def _compatibility(self, layer):
        """(can we paste, what to tell the user) for this target layer."""
        if CLIPBOARD.is_empty():
            return False, 'Nothing on the clipboard - copy some features first.'
        if layer is None:
            return False, 'Choose a target layer.'

        target_wkb = layer.wkbType()
        target_geom = QgsWkbTypes.geometryType(target_wkb)
        source_geom = QgsWkbTypes.geometryType(CLIPBOARD.wkb_type)

        crs = layer.crs().authid() if layer.crs().isValid() else 'no CRS'
        head = 'Target: {}, {}'.format(wkb_display(target_wkb), crs)

        if target_geom == geometry_type('Null'):
            return True, head + '. The target has no geometry column, so only ' \
                                'attributes are pasted.'
        if target_geom == geometry_type('Unknown'):
            return True, head + '. The target takes any geometry.'
        if source_geom == geometry_type('Null'):
            return True, head + '. The copied rows have no geometry; they are ' \
                                'pasted as features with an empty geometry.'
        if source_geom != target_geom:
            return False, (
                'Cannot paste {} features into a {} layer. Pick a target of '
                'the same geometry type.'.format(
                    QgsWkbTypes.geometryDisplayString(source_geom),
                    QgsWkbTypes.geometryDisplayString(target_geom)))

        notes = [head]
        if (not QgsWkbTypes.isMultiType(target_wkb)
                and QgsWkbTypes.isMultiType(CLIPBOARD.wkb_type)):
            notes.append('multipart copies are handled by the setting below')
        if QgsWkbTypes.hasZ(CLIPBOARD.wkb_type) and not QgsWkbTypes.hasZ(target_wkb):
            notes.append('Z values are dropped')
        if QgsWkbTypes.hasM(CLIPBOARD.wkb_type) and not QgsWkbTypes.hasM(target_wkb):
            notes.append('M values are dropped')
        return True, '. '.join(notes) + '.'

    # -------------------------------------------------------------- table --

    def _build_table(self, layer):
        """One row per writable target field. Rebuilt whenever the target changes."""
        self._loading = True
        self.table.setRowCount(0)
        self.mapping_note.setText('')
        if layer is None:
            self._loading = False
            return

        fields = layer.fields()
        # The provider owns its key: writing a copied fid is how a paste turns
        # into "could not commit, duplicate key" three edits later.
        try:
            skip = set(layer.primaryKeyAttributes() or [])
        except Exception:                   # pragma: no cover
            skip = set()

        source_names = CLIPBOARD.field_names()
        rows = [index for index in range(fields.count()) if index not in skip]
        self.table.setRowCount(len(rows))

        for row, index in enumerate(rows):
            field = fields.at(index)

            name_item = QTableWidgetItem(field.name())
            alias = field.alias()
            if alias and alias != field.name():
                name_item.setText('{}  ({})'.format(field.name(), alias))
            name_item.setData(Qt.ItemDataRole.UserRole, field.name())
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, self.COL_TARGET, name_item)

            type_item = QTableWidgetItem(self._type_label(field))
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, self.COL_TYPE, type_item)

            combo = QComboBox()
            combo.addItem('(leave to the target default)', SOURCE_NONE)
            combo.addItem('(fixed value)', SOURCE_FIXED)
            for source_name in source_names:
                combo.addItem(source_name, source_name)
            combo.currentIndexChanged.connect(
                lambda _index, r=row: self._source_changed(r))
            self.table.setCellWidget(row, self.COL_SOURCE, combo)

            editor = QLineEdit()
            editor.setPlaceholderText('typed into every pasted feature')
            editor.setEnabled(False)
            editor.textEdited.connect(self._touched)
            self.table.setCellWidget(row, self.COL_VALUE, editor)

        if skip:
            names = ', '.join(fields.at(i).name() for i in sorted(skip)
                              if 0 <= i < fields.count())
            if names:
                self.mapping_note.setText(
                    'The target primary key ({}) is left to the provider.'.format(names))
        self._loading = False

    @staticmethod
    def _type_label(field):
        label = type_name(field.type())
        if field.length() and field.length() > 0:
            if field.precision() and field.precision() > 0:
                return '{} ({},{})'.format(label, field.length(), field.precision())
            return '{} ({})'.format(label, field.length())
        return label

    def _source_changed(self, row):
        combo = self.table.cellWidget(row, self.COL_SOURCE)
        editor = self.table.cellWidget(row, self.COL_VALUE)
        if combo is None or editor is None:
            return
        fixed = combo.currentData() == SOURCE_FIXED
        editor.setEnabled(fixed)
        if not fixed:
            editor.clear()
        self._touched()

    def _touched(self):
        """A hand edit means the mapping is no longer whatever the mode built."""
        if self._loading:
            return
        if self._mode() != ATTR_MANUAL:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(ATTR_MANUAL)
            self.mode_combo.blockSignals(False)
            self.strategy_combo.setEnabled(False)

    def _fill_from_mode(self):
        """Fill every row's source combo from the current handling mode."""
        layer = self.target_combo.currentLayer()
        if layer is None or self._mode() == ATTR_MANUAL:
            return

        pairs = {}
        if self._mode() == ATTR_MATCH and CLIPBOARD.fields is not None:
            matched, _unmatched_source, _unmatched_target = schema.match_fields(
                CLIPBOARD.fields, layer.fields(), self._strategy())
            pairs = {target: source for source, target in matched}

        self._loading = True
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, self.COL_TARGET)
            combo = self.table.cellWidget(row, self.COL_SOURCE)
            editor = self.table.cellWidget(row, self.COL_VALUE)
            if name_item is None or combo is None:
                continue
            target_name = name_item.data(Qt.ItemDataRole.UserRole)
            wanted = pairs.get(target_name, SOURCE_NONE)
            index = combo.findData(wanted)
            combo.setCurrentIndex(index if index >= 0 else 0)
            if editor is not None:
                editor.setEnabled(False)
                editor.clear()
        self._loading = False

        if self._mode() == ATTR_MATCH:
            unmatched = [name for name in CLIPBOARD.field_names()
                         if name not in pairs.values()]
            if unmatched:
                self.mapping_note.setText(
                    'Not carried across ({} source field(s)): {}'.format(
                        len(unmatched), ', '.join(unmatched[:12])
                        + (' ...' if len(unmatched) > 12 else '')))
            else:
                self.mapping_note.setText(
                    'Every copied field has a home in the target.')
        elif self._mode() == ATTR_NONE:
            self.mapping_note.setText(
                'No attributes are copied; every field takes the target '
                'layer default.')

    def current_mapping(self):
        """``{target field name: (kind, payload)}`` for `paste_features`."""
        mapping = {}
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, self.COL_TARGET)
            combo = self.table.cellWidget(row, self.COL_SOURCE)
            editor = self.table.cellWidget(row, self.COL_VALUE)
            if name_item is None or combo is None:
                continue
            target_name = name_item.data(Qt.ItemDataRole.UserRole)
            data = combo.currentData()
            if data == SOURCE_NONE:
                mapping[target_name] = (SOURCE_NONE, None)
            elif data == SOURCE_FIXED:
                text = editor.text() if editor is not None else ''
                # An empty box is not a fixed empty string: it means the row was
                # never filled in, so treat it as untouched.
                if text == '':
                    mapping[target_name] = (SOURCE_NONE, None)
                else:
                    mapping[target_name] = (SOURCE_FIXED, text)
            else:
                mapping[target_name] = (SOURCE_FIELD, data)
        return mapping

    # -------------------------------------------------------------- paste --

    def _paste(self):
        layer = self.target_combo.currentLayer()
        ok, note = self._compatibility(layer)
        if not ok:
            self.status_label.setText(note)
            return

        if not layer.isEditable():
            answer = QMessageBox.question(
                self, 'Paste Special',
                'The layer "{}" is not in edit mode.\n\nStart editing now?'.format(
                    layer.name()),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes or not layer.startEditing():
                self.status_label.setText(
                    'The target has to be in edit mode to receive features.')
                return

        try:
            report = paste_features(
                CLIPBOARD, layer, self.current_mapping(),
                reproject=self.reproject_check.isChecked(),
                multipart_mode=self.multipart_combo.currentIndex())
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Paste into {} failed: {}'.format(layer.name(), exc),
                LOG_TAG, Qgis.MessageLevel.Critical)
            QMessageBox.critical(
                self, 'Paste Special',
                'Nothing was pasted. The edit was rolled back.\n\n{}'.format(exc))
            return

        self.last_report = report
        self._after_paste(layer, report)

    def _after_paste(self, layer, report):
        if report.new_ids:
            if self.select_check.isChecked():
                layer.selectByIds(report.new_ids)
            if self.zoom_check.isChecked() and self.iface is not None:
                self.iface.mapCanvas().zoomToSelected(layer)

        if self.iface is not None:
            self.iface.mapCanvas().refresh()

        details = report.details()
        if details:
            QgsMessageLog.logMessage(
                'Paste into {}: {}\n{}'.format(
                    layer.name(), report.summary(), details),
                LOG_TAG, Qgis.MessageLevel.Info)

        self.status_label.setText(report.summary())

        # A clean paste says so in the message bar and gets out of the way. One
        # with anything to report puts the report in front of the user, because
        # a converted or dropped value found three weeks later is the failure
        # this tool exists to avoid.
        needs_attention = bool(report.violations or report.skipped)
        if needs_attention or (report.has_details() and report.added == 0):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle('Paste Special')
            box.setText(report.summary())
            box.setDetailedText(details)
            box.exec()
        elif self.iface is not None:
            self.iface.messageBar().pushSuccess('KGA Copy-Paste', report.summary())

        if report.added and not self.keep_open_check.isChecked():
            self.accept()


# --------------------------------------------------------------------------- #
#  main dialog
# --------------------------------------------------------------------------- #

class CopyPasteDialog(QDialog):
    """Copy features onto the KGA clipboard, then paste them somewhere else."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setWindowTitle('Copy-Paste Feature')
        self.setMinimumWidth(420)

        self._tool = None
        #: Set by the first Paste Special, reused by Paste Again:
        #: (layer id, mapping, reproject, multipart mode).
        self._last_paste = None
        #: The layer whose selectionChanged we are currently listening to.
        self._watched = None

        #: True once `_teardown` has handed the canvas back.
        self._torn_down = False

        sweep_stale_bands(self.canvas)
        self._band = QgsRubberBand(self.canvas, geometry_type('Line'))
        try:
            self._band.setData(BAND_TAG_KEY, BAND_TAG)
        except Exception:                   # pragma: no cover
            pass
        self._band.setColor(QColor(255, 120, 0, 190))
        self._band.setWidth(2)
        try:
            # setColor fills a polygon band with the same solid colour, which
            # blankets the very features being copied. Outline them instead.
            self._band.setFillColor(QColor(255, 120, 0, 40))
            self._band.setStrokeColor(QColor(255, 120, 0, 220))
        except AttributeError:              # pragma: no cover - older QGIS
            pass
        try:
            self._band.setIcon(QgsRubberBand.ICON_CIRCLE)
            self._band.setIconSize(9)
        except AttributeError:              # pragma: no cover
            pass

        self._build_ui()
        self._connect()
        self._preselect_active_layer()
        self._layer_changed()

    # ----------------------------------------------------------------- ui --

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        form.addRow('Copy from', self.layer_combo)
        layout.addLayout(form)

        copy_buttons = QHBoxLayout()
        self.copy_button = QPushButton('Copy Selected')
        self.copy_button.setToolTip(
            'Put the layer\'s selected features on the KGA clipboard.')
        copy_buttons.addWidget(self.copy_button)

        self.pick_button = QPushButton('Copy by Clicking')
        self.pick_button.setCheckable(True)
        self.pick_button.setToolTip(
            'Click a feature on the map to copy it. Hold Ctrl or Shift to add '
            'to what is already on the clipboard.')
        copy_buttons.addWidget(self.pick_button)

        self.clear_button = QPushButton('Clear')
        self.clear_button.setToolTip('Empty the clipboard.')
        copy_buttons.addWidget(self.clear_button)
        layout.addLayout(copy_buttons)

        self.clipboard_label = QLabel('')
        self.clipboard_label.setWordWrap(True)
        self.clipboard_label.setStyleSheet(
            'padding: 6px; border: 1px solid #c8c8c8; border-radius: 3px;')
        layout.addWidget(self.clipboard_label)

        paste_buttons = QHBoxLayout()
        self.paste_button = QPushButton('Paste Special...')
        self.paste_button.setToolTip(
            'Choose the target layer and how the attributes are carried over.')
        paste_buttons.addWidget(self.paste_button)

        self.repeat_button = QPushButton('Paste Again')
        self.repeat_button.setToolTip(
            'Repeat the last paste: same target layer, same field mapping.')
        paste_buttons.addWidget(self.repeat_button)
        layout.addLayout(paste_buttons)

        close_row = QHBoxLayout()
        close_row.addWidget(help_button('copy_paste_feature', self))
        close_row.addStretch(1)
        self.close_button = QPushButton('Close')
        close_row.addWidget(self.close_button)
        layout.addLayout(close_row)

        self.status_label = QLabel(
            'Select features and press Copy Selected, or press Copy by '
            'Clicking and pick them on the map.')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('color: #666;')
        layout.addWidget(self.status_label)

    def _connect(self):
        self.layer_combo.layerChanged.connect(self._layer_changed)
        self.copy_button.clicked.connect(self._copy_selected)
        self.pick_button.toggled.connect(self._toggle_tool)
        self.clear_button.clicked.connect(self._clear)
        self.paste_button.clicked.connect(self._paste_special)
        self.repeat_button.clicked.connect(self._paste_again)
        self.close_button.clicked.connect(self.close)
        self.canvas.mapToolSet.connect(self._map_tool_set)
        self.canvas.destinationCrsChanged.connect(self._refresh_band)

    def _preselect_active_layer(self):
        layer = self.iface.activeLayer()
        if layer is not None and hasattr(layer, 'selectedFeatureCount'):
            self.layer_combo.setLayer(layer)

    # -------------------------------------------------------------- state --

    def current_layer(self):
        return self.layer_combo.currentLayer()

    def _layer_changed(self):
        """Follow the new layer's selection so Copy Selected stays honest.

        Without this the button keeps the count it had when the layer was
        chosen, and a user who selects three more features sees "Copy Selected
        (1)" and copies one.
        """
        layer = self.current_layer()
        if self._watched is layer:
            self._refresh()
            return
        if self._watched is not None:
            try:
                self._watched.selectionChanged.disconnect(self._refresh)
            except Exception:
                pass
        self._watched = layer
        if layer is not None:
            layer.selectionChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        layer = self.current_layer()
        selected = layer.selectedFeatureCount() if layer is not None else 0
        self.copy_button.setEnabled(selected > 0)
        self.copy_button.setText(
            'Copy Selected ({})'.format(selected) if selected else 'Copy Selected')

        empty = CLIPBOARD.is_empty()
        self.clipboard_label.setText(CLIPBOARD.describe())
        self.paste_button.setEnabled(not empty)
        self.repeat_button.setEnabled(not empty and self._last_paste is not None)
        self.clear_button.setEnabled(not empty)
        self._refresh_band()

    def _refresh_band(self):
        """Draw what is on the clipboard, in canvas coordinates."""
        if self._band is None:              # torn down; nothing to draw on
            return
        band_type = (QgsWkbTypes.geometryType(CLIPBOARD.wkb_type)
                     if not CLIPBOARD.is_empty() else geometry_type('Line'))
        self._band.reset(band_type)
        if CLIPBOARD.is_empty():
            return

        canvas_crs = self.canvas.mapSettings().destinationCrs()
        transform = None
        if (CLIPBOARD.crs is not None and CLIPBOARD.crs.isValid()
                and canvas_crs != CLIPBOARD.crs):
            transform = QgsCoordinateTransform(
                CLIPBOARD.crs, canvas_crs, QgsProject.instance())

        for geometry, _attributes in CLIPBOARD.rows:
            if geometry is None or geometry.isEmpty():
                continue
            drawn = QgsGeometry(geometry)
            if transform is not None:
                try:
                    drawn.transform(transform)
                except Exception:           # pragma: no cover
                    continue
            # No layer argument: the geometry is already in the canvas CRS.
            self._band.addGeometry(drawn, None)

    # --------------------------------------------------------------- copy --

    def _copy_selected(self):
        layer = self.current_layer()
        if layer is None:
            self.status_label.setText('Choose a layer first.')
            return
        count = layer.selectedFeatureCount()
        if not count:
            self.status_label.setText('Nothing is selected in that layer.')
            return
        if count > LARGE_SELECTION:
            answer = QMessageBox.question(
                self, 'Copy-Paste Feature',
                '{} features are selected. They are all held in memory until '
                'the clipboard is cleared.\n\nCopy them anyway?'.format(count),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.take(layer, layer.getSelectedFeatures())

    def take(self, layer, features, append=False):
        """Put features on the clipboard. Also the hook the map tool calls."""
        try:
            added = CLIPBOARD.take(layer, features, append=append)
        except Exception as exc:            # pragma: no cover
            QgsMessageLog.logMessage(
                'Copy from {} failed: {}'.format(layer.name(), exc),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self.status_label.setText('Could not copy: {}'.format(exc))
            return

        # The mapping in a Paste Special built against the old clipboard no
        # longer describes what is on it, so the repeat has to be re-set up.
        if not append:
            self._last_paste = None

        self._refresh()
        self.status_label.setText(
            '{} feature(s) copied. Press Paste Special to choose where they '
            'go.'.format(added))

    def _clear(self):
        CLIPBOARD.clear()
        self._last_paste = None
        self._refresh()
        self.status_label.setText('Clipboard emptied.')

    # --------------------------------------------------------------- tool --

    def _toggle_tool(self, checked):
        if not checked:
            if self._tool is not None and self.canvas.mapTool() is self._tool:
                self.canvas.unsetMapTool(self._tool)
            return

        layer = self.current_layer()
        if layer is None:
            self.status_label.setText('Choose a layer first.')
            self._untoggle()
            return

        self.iface.setActiveLayer(layer)
        if self._tool is None:
            self._tool = CopyPickMapTool(self.canvas, self)
            self._tool.message.connect(self.status_label.setText)
        self.canvas.setMapTool(self._tool)
        self.status_label.setText(
            'Click a feature to copy it. Ctrl or Shift adds to the clipboard.')

    def _untoggle(self):
        self.pick_button.blockSignals(True)
        self.pick_button.setChecked(False)
        self.pick_button.blockSignals(False)

    def _map_tool_set(self, new_tool, old_tool=None):
        if self._tool is not None and new_tool is not self._tool:
            self._untoggle()

    # -------------------------------------------------------------- paste --

    def _paste_special(self):
        if CLIPBOARD.is_empty():
            self.status_label.setText('Copy some features first.')
            return

        dialog = PasteSpecialDialog(self.iface, self)
        target = self._last_paste[0] if self._last_paste else None
        if target is not None:
            layer = QgsProject.instance().mapLayer(target)
            if layer is not None:
                dialog.target_combo.setLayer(layer)

        dialog.exec()
        if dialog.last_report is not None:
            layer = dialog.target_combo.currentLayer()
            if layer is not None:
                self._last_paste = (layer.id(), dialog.current_mapping(),
                                    dialog.reproject_check.isChecked(),
                                    dialog.multipart_combo.currentIndex())
            self.status_label.setText(dialog.last_report.summary())
        self._refresh()

    def _paste_again(self):
        """Repeat the last paste without reopening the popup."""
        if self._last_paste is None or CLIPBOARD.is_empty():
            return
        layer_id, mapping, reproject, multipart = self._last_paste

        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            self._last_paste = None
            self._refresh()
            self.status_label.setText(
                'That target layer is no longer in the project. Use Paste '
                'Special to pick a new one.')
            return

        if not layer.isEditable() and not layer.startEditing():
            self.status_label.setText(
                '"{}" could not be put into edit mode.'.format(layer.name()))
            return

        try:
            report = paste_features(CLIPBOARD, layer, mapping,
                                    reproject=reproject,
                                    multipart_mode=multipart)
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Repeat paste into {} failed: {}'.format(layer.name(), exc),
                LOG_TAG, Qgis.MessageLevel.Critical)
            self.status_label.setText(
                'Nothing was pasted; the edit was rolled back. {}'.format(exc))
            return

        if report.new_ids:
            layer.selectByIds(report.new_ids)
        self.canvas.refresh()
        if report.has_details():
            QgsMessageLog.logMessage(
                'Repeat paste into {}: {}\n{}'.format(
                    layer.name(), report.summary(), report.details()),
                LOG_TAG, Qgis.MessageLevel.Info)
        self.status_label.setText(
            'Into "{}": {}'.format(layer.name(), report.summary()))

    # -------------------------------------------------------------- close --

    def _teardown(self):
        """Hand back everything this dialog put on the canvas.

        Closing is not the only way out. Escape, `reject()` and a plain
        `hide()` all leave a modeless dialog invisible without ever sending a
        QCloseEvent, so hanging the clean-up off `closeEvent` alone missed
        them. That mattered because a `QgsRubberBand` is owned by the canvas
        scene, not by the widget that made it: a dialog that went away by one
        of those routes left its orange outline painted on the map for the
        rest of the session. It belongs to no layer, so switching layers off
        did not clear it, and the next run of the tool drew a second one
        beside the first.

        Idempotent - `closeEvent`, `done` and the plugin unload all call it.
        """
        if self._torn_down:
            return
        self._torn_down = True

        if self._tool is not None:
            if self.canvas.mapTool() is self._tool:
                self.canvas.unsetMapTool(self._tool)
            self._tool.deleteLater()
            self._tool = None

        band, self._band = self._band, None
        if band is not None:
            band.reset(geometry_type('Line'))
            # reset() empties the band but leaves the item in the scene; take
            # it out so the band dies with the dialog instead of outliving it.
            try:
                scene = self.canvas.scene()
                if scene is not None:
                    scene.removeItem(band)
            except Exception:               # pragma: no cover
                pass

        signals = [(self.canvas.mapToolSet, self._map_tool_set),
                   (self.canvas.destinationCrsChanged, self._refresh_band)]
        if self._watched is not None:
            signals.append((self._watched.selectionChanged, self._refresh))
            self._watched = None
        for signal, slot in signals:
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        self.canvas.refresh()

    def done(self, result):
        # Escape and reject() land here and never raise a close event.
        self._teardown()
        super().done(result)

    def closeEvent(self, event):
        self._teardown()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
#  launcher: the entry the toolbox, toolbar and Geoprocessing panel see
# --------------------------------------------------------------------------- #


def close_copy_paste_feature():
    """Close the dialog if one is open. Safe to call when none is.

    Called by `KgaToolsPlugin.unload`; without it the dialog - and the map tool
    and rubber band it may have on the canvas - survives the plugin being
    disabled. The clipboard is emptied too, so a reload starts clean.
    """
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    CLIPBOARD.clear()
    if dialog is None or _is_deleted(dialog):
        return
    dialog._teardown()
    dialog.close()
    dialog.deleteLater()
    if iface is not None:
        sweep_stale_bands(iface.mapCanvas())


class CopyPasteFeatureAlgorithm(QgsProcessingAlgorithm):
    """Show the Copy-Paste Feature dialog."""

    def tr(self, string):
        return QCoreApplication.translate('KgaCopyPasteFeature', string)

    def createInstance(self):
        return CopyPasteFeatureAlgorithm()

    def name(self):
        return 'copy_paste_feature'

    def displayName(self):
        return self.tr('Copy-Paste Feature')

    def group(self):
        return self.tr('KGA Editing Tools')

    def groupId(self):
        return 'kgaeditingtools'

    def helpUrl(self):
        return docs_url('copy_paste_feature')

    def shortHelpString(self):
        return self.tr(
            'Copy features from one layer and paste them into another, the way '
            'ArcGIS Pro\'s <i>Copy</i> and <i>Paste Special</i> do.\n\n'
            '<b>Copy.</b> Pick the source layer, then either select features '
            'and press <b>Copy Selected</b>, or press <b>Copy by Clicking</b> '
            'and pick them straight off the map (Ctrl or Shift adds to the '
            'clipboard). What is on the clipboard is outlined on the canvas.\n\n'
            '<b>Paste Special.</b> Choose the target layer and decide what '
            'happens to the attributes:\n\n'
            '• <i>Match target fields by name</i> — exactly, ignoring case, or '
            'ignoring case, spaces and underscores\n'
            '• <i>Map fields manually</i> — pick a source field, or type a '
            'fixed value, for each target field\n'
            '• <i>Do not copy attributes</i> — every field takes the target '
            'layer default\n\n'
            'Geometry is fitted to the target on the way in: reprojected to '
            'its CRS, promoted to multipart or exploded into single parts, '
            'curves segmented, Z and M added or dropped. A target with no '
            'geometry column takes the attributes alone.\n\n'
            'Values that will not fit the target field are converted where '
            'that is lossless and reported where it is not, so a paste never '
            'quietly leaves a NULL behind. Every paste is one undo step.\n\n'
            'The target layer has to be in edit mode; the tool offers to start '
            'editing if it is not.\n\n'
            'The dialog stays open while you work — close this Processing '
            'window once it appears.'
        )

    def flags(self):
        # Opens a dialog and writes into a project layer: GUI thread only.
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE

        if iface is None:
            raise QgsProcessingException(self.tr(
                'Copy-Paste Feature is an interactive tool and needs the QGIS '
                'map canvas, so it cannot run from a head-less Processing '
                'session.'))

        if DIALOG_INSTANCE is not None and _is_deleted(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        # _teardown drops the map tool, takes the rubber band off the canvas
        # and disconnects the canvas signals, so a closed dialog is spent:
        # build a fresh one rather than re-showing that. The clipboard is
        # module-level, so a copy made before the close is still there to
        # paste. Tearing down here too covers a dialog that was hidden
        # without a close event ever reaching it.
        if DIALOG_INSTANCE is not None and not DIALOG_INSTANCE.isVisible():
            DIALOG_INSTANCE._teardown()
            DIALOG_INSTANCE.deleteLater()
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = CopyPasteDialog(iface)

        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo(self.tr(
            'Copy-Paste Feature opened. You can close this Processing dialog '
            'and keep working on the map.'))
        return {}

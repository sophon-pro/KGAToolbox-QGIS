# -*- coding: utf-8 -*-
"""Shape_Length / Shape_Area on line and polygon layers, like a File Geodatabase.

Lines get `Shape_Length`; polygons get `Shape_Length` (the perimeter) and
`Shape_Area`; points get nothing. The values are planar, in the units of the
layer's CRS, which is what ArcGIS Pro stores.

Three things keep them current:

* a default-value expression with "apply on update", so the attribute form and
  table show the new value as soon as a geometry is drawn or reshaped;
* a read-only form setting, so nobody types over them;
* a `beforeCommitChanges` handler, for features that never went through the
  form (Processing, Python, pasted features), whose defaults QGIS does not run.

Nothing here touches a widget.
"""

import os
from contextlib import suppress

from qgis.core import (
    NULL, Qgis, QgsDefaultValue, QgsEditorWidgetSetup, QgsFeatureRequest,
    QgsMessageLog, QgsProject, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QObject, QTimer

from . import compat, kga_settings
from .gpkg_layers import split_uri

LENGTH = 'Shape_Length'
AREA = 'Shape_Area'

LINE_FIELDS = ((LENGTH, 'length($geometry)'),)
POLYGON_FIELDS = ((LENGTH, 'perimeter($geometry)'), (AREA, 'area($geometry)'))

#: Fields written per batch while filling an existing layer.
CHUNK = 5000
LOG_TAG = 'KGA Tools'


def field_specs(layer):
    """((name, expression), ...) this layer should carry, empty for others."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return ()
    kind = layer.geometryType()
    if kind == Qgis.GeometryType.Line:
        return LINE_FIELDS
    if kind == Qgis.GeometryType.Polygon:
        return POLYGON_FIELDS
    return ()


def _index(layer, name):
    """Field index by case-insensitive name, -1 when missing."""
    return layer.fields().lookupField(name)


def _value(geometry, name):
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    if name == AREA:
        return float(geometry.area())
    if geometry.type() == Qgis.GeometryType.Polygon:
        return float(geometry.constGet().perimeter())
    return float(geometry.length())


def can_add(layer):
    """None when the fields can be added, otherwise the reason they cannot."""
    if not field_specs(layer):
        return 'The layer has no line or polygon geometry.'
    provider = layer.dataProvider()
    if provider is None or not (provider.capabilities()
                                & provider.AddAttributes):
        return 'The data source cannot take new fields.'
    return None


def _missing(layer):
    return [s for s in field_specs(layer) if _index(layer, s[0]) < 0]


def add_fields(layer):
    """Add the missing Shape_* fields to the data source; the names added."""
    missing = _missing(layer)
    if not missing:
        return []
    why = can_add(layer)
    if why:
        raise ValueError(why)
    if layer.isEditable():
        raise ValueError('Save and stop editing the layer first.')
    fields = [compat.make_field(name, compat.T_DOUBLE) for name, _e in missing]
    if not layer.dataProvider().addAttributes(fields):
        raise ValueError('The data source refused the new fields.')
    layer.updateFields()
    return [name for name, _e in missing]


def apply_defaults(layer):
    """Default expression (applied on update) and read-only form for each field."""
    config = layer.editFormConfig()
    for name, expression in field_specs(layer):
        index = _index(layer, name)
        if index < 0:
            continue
        layer.setDefaultValueDefinition(index, QgsDefaultValue(expression, True))
        config.setReadOnly(index, True)
        layer.setEditorWidgetSetup(
            index, QgsEditorWidgetSetup('TextEdit', {'IsMultiline': False}))
    layer.setEditFormConfig(config)


def fill_values(layer):
    """Write the right value into every feature that has a wrong or empty one.

    Goes straight to the data source (no edit session), so it works on a layer
    nobody is editing and leaves the undo stack alone. The count of features
    changed.
    """
    provider = layer.dataProvider()
    wanted = [(name, _index(layer, name)) for name, _e in field_specs(layer)]
    wanted = [(n, i) for n, i in wanted if i >= 0]
    if not wanted or not (provider.capabilities() & provider.ChangeAttributeValues):
        return 0
    request = QgsFeatureRequest()
    request.setSubsetOfAttributes([i for _n, i in wanted])
    changed, batch = 0, {}
    for feature in layer.getFeatures(request):
        change = {}
        for name, index in wanted:
            value = _value(feature.geometry(), name)
            current = feature.attribute(index)
            empty = current is None or current == NULL
            if value is None:
                if not empty:
                    change[index] = None
            elif empty or abs(float(current) - value) > 1e-9 * max(1.0, abs(value)):
                change[index] = value
        if change:
            batch[feature.id()] = change
        if len(batch) >= CHUNK:
            provider.changeAttributeValues(batch)
            changed += len(batch)
            batch = {}
    if batch:
        provider.changeAttributeValues(batch)
        changed += len(batch)
    layer.dataProvider().reloadData()
    layer.triggerRepaint()
    return changed


def prepare(layer, fill=True):
    """Add the fields, set up defaults and the safety net. (added, filled)."""
    added = add_fields(layer)
    apply_defaults(layer)
    filled = fill_values(layer) if fill else 0
    return added, filled


def stamp_pending_edits(layer):
    """Put the right values on every added or reshaped feature in the edit buffer."""
    buffer = layer.editBuffer()
    if buffer is None:
        return
    names = [(n, _index(layer, n)) for n, _e in field_specs(layer)]
    names = [(n, i) for n, i in names if i >= 0]
    if not names:
        return
    ids = set(buffer.addedFeatures().keys()) | set(buffer.changedGeometries().keys())
    for fid in ids:
        feature = buffer.addedFeatures().get(fid)
        geometry = feature.geometry() if feature is not None \
            else buffer.changedGeometries().get(fid)
        for name, index in names:
            layer.changeAttributeValue(fid, index, _value(geometry, name))


class GeometryFieldsHook(QObject):
    """Prepares workspace GeoPackage layers as they are added to the project."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layers = {}       # layer id -> handler connected to it
        self._busy = False

    def install(self):
        QgsProject.instance().layersAdded.connect(self._layers_added)
        # The workspace is only known once the project has been read.
        QgsProject.instance().readProject.connect(self.rescan)
        self.scan()

    def uninstall(self):
        for signal, slot in ((QgsProject.instance().layersAdded, self._layers_added),
                             (QgsProject.instance().readProject, self.rescan)):
            with suppress(Exception):
                signal.disconnect(slot)
        for layer_id in list(self._layers):
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                with suppress(Exception):
                    layer.beforeCommitChanges.disconnect(self._before_commit)
        self._layers.clear()

    def rescan(self, *args):
        """`scan` once the event loop is free (the project is still loading)."""
        QTimer.singleShot(0, self.scan)

    def scan(self):
        """Handle every layer already in the project (workspace just changed)."""
        self._layers_added(list(QgsProject.instance().mapLayers().values()))

    def _layers_added(self, layers):
        if self._busy or not kga_settings.geometry_fields():
            return
        self._busy = True
        try:
            for layer in layers:
                if self._eligible(layer):
                    self._handle(layer)
        finally:
            self._busy = False

    @staticmethod
    def _eligible(layer):
        if not field_specs(layer) or layer.providerType() != 'ogr':
            return False
        parts = split_uri(layer.source())
        if parts is None:
            return False
        from . import output_defaults
        workspace = output_defaults.workspace()
        if workspace is None:
            return False
        path = os.path.normcase(os.path.normpath(parts[0]))
        return any(os.path.normcase(os.path.normpath(d)) == path
                   for d in workspace.all_databases())

    def prepare(self, layer):
        """Fields, defaults, fill and the commit safety net; (added, filled).

        Raises ValueError with a message for the user when it cannot.
        """
        result = prepare(layer)
        if layer.id() not in self._layers:
            layer.beforeCommitChanges.connect(self._before_commit)
            self._layers[layer.id()] = True
        return result

    def _handle(self, layer):
        try:
            if layer.isEditable():
                return
            self.prepare(layer)
        except Exception as error:
            QgsMessageLog.logMessage(
                'Could not set up Shape_Length / Shape_Area on "{}": {}'.format(
                    layer.name(), error), LOG_TAG, Qgis.MessageLevel.Warning)

    def _before_commit(self, *args):
        layer = self.sender()
        if layer is not None:
            stamp_pending_edits(layer)


_hook = None


def install():
    """Start preparing workspace layers; the hook, so callers can reach it."""
    global _hook
    if _hook is None:
        _hook = GeometryFieldsHook()
        _hook.install()
    return _hook


def uninstall():
    global _hook
    if _hook is not None:
        _hook.uninstall()
        _hook.deleteLater()
        _hook = None


def rescan():
    """Look at the project's layers again (the workspace changed)."""
    if _hook is not None:
        _hook.scan()

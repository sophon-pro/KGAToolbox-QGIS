# -*- coding: utf-8 -*-
"""Point every Processing tool's output at the project, the way Pro does.

When a Processing dialog opens, each output that would otherwise be a temporary
layer, or empty, is filled in:

    layer outputs   -> a new table in the project's default database
    file outputs    -> a new file in the project's default folder
    folder outputs  -> the project's default folder

The name is `<Tool>_<InputLayer>` and follows the input while the user picks a
different one. Anything the user types, browses to or clears is left alone, and
optional outputs are never switched on. This covers QGIS's own tools too: the
hook sits on the stock dialog class rather than on the KGA algorithms.
"""

import os

from qgis.core import (
    Qgis, QgsMapLayer, QgsMessageLog, QgsProcessingContext,
    QgsProcessingFeatureSourceDefinition, QgsProcessingParameterDefinition,
    QgsProcessing, QgsProject, QgsProperty,
)

from ..core import output_defaults as od

TAG = 'KGA Toolbox'
_INPUT_TYPES = ('source', 'vector', 'raster', 'layer', 'mesh')
_TEMPORARY = 'TEMPORARY_OUTPUT'

_installed = {'cls': None, 'original': None}


def _log(message):
    QgsMessageLog.logMessage(message, TAG, Qgis.MessageLevel.Warning)


# ------------------------------------------------------------------ values

def _text(value):
    """A parameter value as the string the destination field holds."""
    if value is None:
        return ''
    sink = getattr(value, 'sink', None)
    if sink is not None:
        value = sink
    if isinstance(value, QgsProperty):
        value = value.staticValue() if value.propertyType() == \
            QgsProperty.Type.StaticProperty else ''
    return str(value or '')


def _is_unset(value):
    text = _text(value).strip()
    return not text or text == _TEMPORARY


def _wants_default(param, value):
    """Should this output get the project default when the dialog opens?

    A required output that is empty or temporary does. An optional one is off
    on purpose when it is empty ("skip output") and stays off; only one the
    dialog would otherwise send to a temporary layer is redirected.
    """
    if _optional(param):
        return _text(value).strip() == _TEMPORARY
    return _is_unset(value)


def _source_name(value):
    """The layer name behind an input wrapper's value, or ''."""
    if isinstance(value, QgsProcessingFeatureSourceDefinition):
        value = value.source
    if isinstance(value, QgsProperty):
        value = value.staticValue() \
            if value.propertyType() == QgsProperty.Type.StaticProperty else ''
    if isinstance(value, QgsMapLayer):
        return value.name()
    if isinstance(value, str) and value:
        layer = QgsProject.instance().mapLayer(value)
        if layer is not None:
            return layer.name()
        found = QgsProject.instance().mapLayersByName(value)
        if found:
            return found[0].name()
        return os.path.splitext(os.path.basename(value.split('|')[0]))[0]
    return ''


def _optional(param):
    return bool(param.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional)


def _geometry(param):
    """False for a table output, which has no `(geom)` column."""
    data_type = getattr(param, 'dataType', None)
    if data_type is None:
        return True
    try:
        return data_type() != QgsProcessing.SourceType.TypeVector
    except Exception:       # noqa: BLE001
        return True


def _extension(param):
    finder = getattr(param, 'defaultFileExtension', None)
    return finder() if finder else ''


# --------------------------------------------------------------- the filler

class OutputFiller(object):
    """Fills one dialog's outputs and keeps the names in step with the input."""

    def __init__(self, dialog):
        self.dialog = dialog
        self.context = QgsProcessingContext()
        panel = dialog.mainWidget()
        self.wrappers = getattr(panel, 'wrappers', None) or {}
        self.algorithm = dialog.algorithm()
        self.outputs = []       # wrappers of output parameters we may fill
        self.inputs = []        # wrappers of layer inputs the name follows
        self.auto = {}          # output name -> the value we put there
        for wrapper in self.wrappers.values():
            param = wrapper.parameterDefinition()
            if param.type() in od.OUTPUT_TYPES:
                self.outputs.append(wrapper)
            elif param.type() in _INPUT_TYPES:
                self.inputs.append(wrapper)

    def source_name(self):
        for wrapper in self.inputs:
            name = _source_name(wrapper.parameterValue())
            if name:
                return name
        return ''

    def refresh(self, first=False):
        """Set every output we own; leave the ones the user has changed."""
        source = self.source_name()
        tool = self.algorithm.displayName()
        many = len(self.outputs) > 1
        claimed = []
        for wrapper in self.outputs:
            param = wrapper.parameterDefinition()
            name = param.name()
            current = _text(wrapper.parameterValue())
            ours = name in self.auto and current == self.auto[name]
            if not (ours or (first and _wants_default(param, current))):
                if current:
                    claimed.append(current)
                continue
            label = '{} {}'.format(tool, param.description()) if many else tool
            value = od.processing_default(
                param.type(), od.make_stem(label, source), _extension(param),
                _geometry(param), claimed)
            if value is None:
                continue
            wrapper.setParameterValue(value, self.context)
            self.auto[name] = _text(wrapper.parameterValue())
            claimed.append(self.auto[name])

    def connect(self):
        for wrapper in self.inputs:
            wrapper.widgetValueHasChanged.connect(self._input_changed)

    def _input_changed(self, *_args):
        try:
            self.refresh()
        except Exception as error:      # noqa: BLE001
            _log('Could not update the output name: {}'.format(error))


def fill(dialog):
    """Fill the outputs of a freshly built Processing dialog."""
    if od.workspace() is None or getattr(dialog, 'in_place', False):
        return None
    filler = OutputFiller(dialog)
    if not filler.outputs:
        return None
    filler.refresh(first=True)
    filler.connect()
    dialog._kga_output_filler = filler      # lives as long as the dialog
    return filler


# -------------------------------------------------------------- the hook

def install():
    """Wrap the stock Processing dialog so it fills its outputs when built."""
    try:
        from processing.gui.AlgorithmDialog import AlgorithmDialog
    except Exception as error:      # noqa: BLE001 - no Processing, nothing to do
        _log('Output defaults are off, Processing is unavailable: {}'.format(error))
        return False
    if _installed['cls'] is not None:
        return True
    original = AlgorithmDialog.__init__

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            fill(self)
        except Exception as error:      # noqa: BLE001 - never block a tool
            _log('Could not set the default outputs: {}'.format(error))

    AlgorithmDialog.__init__ = __init__
    _installed['cls'], _installed['original'] = AlgorithmDialog, original
    return True


def uninstall():
    cls, original = _installed['cls'], _installed['original']
    if cls is not None and original is not None:
        cls.__init__ = original
    _installed['cls'] = _installed['original'] = None

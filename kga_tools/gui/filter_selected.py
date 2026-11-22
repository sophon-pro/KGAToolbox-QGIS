# -*- coding: utf-8 -*-
"""Filter Selected Features: show only the selected features of a layer, no expression to write.

Offered on the Layers panel right-click menu (below QGIS's own Filter...; greyed
out until the layer has a selection) and on the map's right-click menu.
It sets the layer's provider filter (the same thing the Query Builder does), so
the filter is saved in the project and can be changed or removed from Layer >
Filter... like any other. Run it again to narrow to a new selection: the filter
the layer had before the first run is kept underneath and nothing stacks up.
"""

from contextlib import suppress

from qgis.core import QgsApplication, QgsFeatureRequest, QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QMenu
try:                                    # Qt 6: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:                     # Qt 5
    from qgis.PyQt.QtWidgets import QAction

TEXT = 'Filter Selected Features'
CLEAR_TEXT = 'Clear Filter'
BASE_PROP = 'kga/filter_selected_base'       # the layer's own filter before ours
APPLIED_PROP = 'kga/filter_selected_applied'  # the filter string we set (to spot a later manual change)


def _plain(text):
    return text.replace('&', '').replace('…', '').replace('...', '').strip().lower()


def _icon():
    return QgsApplication.getThemeIcon('/mActionFilter2.svg')


def _quote_value(value, numeric):
    if numeric:
        return str(int(value)) if float(value) == int(value) else repr(float(value))
    return "'{}'".format(str(value).replace("'", "''"))


def _field_ident(name):
    return '"{}"'.format(name.replace('"', '""'))


def build_expression(layer):
    """The provider filter that keeps exactly the selected features, or None if it cannot be keyed."""
    ids = layer.selectedFeatureIds()
    if not ids:
        return None
    provider = layer.providerType()
    pk = list(layer.primaryKeyAttributes())
    if len(pk) == 1:
        field = layer.fields().at(pk[0])
        request = QgsFeatureRequest().setFilterFids(ids).setFlags(QgsFeatureRequest.Flag.NoGeometry)
        request.setSubsetOfAttributes([pk[0]])
        numeric = field.isNumeric()
        values = [f.attribute(pk[0]) for f in layer.getFeatures(request)]
        values = [v for v in values if v is not None and not _is_null(v)]
        if not values:
            return None
        return '{} IN ({})'.format(_field_ident(field.name()),
                                   ', '.join(_quote_value(v, numeric) for v in values))
    # no single key: the feature id itself is the key for these providers
    ids = sorted(ids)
    if provider == 'ogr':
        return 'FID IN ({})'.format(', '.join(str(i) for i in ids))
    if provider == 'memory':
        return '$id IN ({})'.format(', '.join(str(i) for i in ids))
    return None


def _is_null(value):
    try:
        from qgis.core import NULL
        return value == NULL
    except Exception:
        return False


def can_filter(layer):
    return isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.selectedFeatureCount() > 0


def filter_selected(layer, iface=None):
    """Apply the filter. Returns (ok, message)."""
    if not can_filter(layer):
        return False, 'Select at least one feature in "{}" first.'.format(layer.name() if layer else '')
    if layer.isEditable():
        return False, ('"{}" is being edited. Save or stop editing it first - '
                       'a filter cannot be set while editing.'.format(layer.name()))
    expression = build_expression(layer)
    if expression is None:
        return False, ('"{}" has no unique key to filter by '
                       '(its data source type is not supported by this tool).'.format(layer.name()))
    current = layer.subsetString()
    base = current
    if layer.customProperty(APPLIED_PROP) == current and current:
        base = layer.customProperty(BASE_PROP) or ''      # replace our own earlier filter, never stack
    new = '({}) AND ({})'.format(base, expression) if base else expression
    count = layer.selectedFeatureCount()
    if not layer.setSubsetString(new):
        layer.setSubsetString(current)
        return False, 'The data source did not accept the filter for "{}".'.format(layer.name())
    layer.setCustomProperty(BASE_PROP, base)
    layer.setCustomProperty(APPLIED_PROP, new)
    layer.triggerRepaint()
    if iface is not None:
        with suppress(Exception):
            iface.mapCanvas().refresh()
    return True, '"{}" now shows only the {} selected feature{}. Remove it from Layer > Filter...'.format(
        layer.name(), count, '' if count == 1 else 's')


def is_filtered(layer):
    return isinstance(layer, QgsVectorLayer) and layer.isValid() and bool(layer.subsetString())


def has_our_filter(layer):
    return is_filtered(layer) and layer.customProperty(APPLIED_PROP) == layer.subsetString()


def clear_filter(layer, iface=None):
    """Remove the layer's filter entirely. Returns (ok, message)."""
    if not is_filtered(layer):
        return False, '"{}" has no filter.'.format(layer.name() if layer else '')
    if layer.isEditable():
        return False, ('"{}" is being edited. Save or stop editing it first - '
                       'a filter cannot be changed while editing.'.format(layer.name()))
    current = layer.subsetString()
    if not layer.setSubsetString(''):
        layer.setSubsetString(current)
        return False, 'The data source did not let the filter of "{}" be removed.'.format(layer.name())
    layer.removeCustomProperty(BASE_PROP)
    layer.removeCustomProperty(APPLIED_PROP)
    layer.triggerRepaint()
    if iface is not None:
        with suppress(Exception):
            iface.mapCanvas().refresh()
    return True, 'Filter removed: "{}" shows all its features again.'.format(layer.name())


class FilterSelectedController:
    """Wires Filter Selected Features into the layer right-click menu and the map right-click menu."""

    def __init__(self, iface):
        self.iface = iface
        self._connected = False

    def install(self):
        view = self.iface.layerTreeView()
        view.contextMenuAboutToShow.connect(self._layer_menu)
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self._connected = True

    def uninstall(self):
        if not self._connected:
            return
        with suppress(TypeError, RuntimeError):
            self.iface.layerTreeView().contextMenuAboutToShow.disconnect(self._layer_menu)
        with suppress(TypeError, RuntimeError):
            self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._canvas_menu)
        self._connected = False

    # ------------------------------------------------------------------ actions
    def _run(self, layer):
        ok, message = filter_selected(layer, self.iface)
        bar = self.iface.messageBar()
        if ok:
            bar.pushSuccess(TEXT, message)
        else:
            bar.pushWarning(TEXT, message)

    def _run_clear(self, layer):
        ok, message = clear_filter(layer, self.iface)
        bar = self.iface.messageBar()
        if ok:
            bar.pushSuccess(CLEAR_TEXT, message)
        else:
            bar.pushWarning(CLEAR_TEXT, message)

    def _make_clear_action(self, menu, layer):
        action = QAction(_icon(), CLEAR_TEXT, menu)
        enabled = is_filtered(layer)
        action.setEnabled(enabled)
        action.setToolTip('Remove the filter so every feature shows again' if enabled
                          else 'This layer has no filter')
        action.triggered.connect(lambda _c=False, lyr=layer: self._run_clear(lyr))
        return action

    def _make_action(self, menu, layer):
        action = QAction(_icon(), TEXT, menu)
        action.setToolTip('Show only the selected features of this layer')
        enabled = can_filter(layer)
        action.setEnabled(enabled)
        if not enabled:
            action.setToolTip('Select features in this layer to use this')
        action.triggered.connect(lambda _c=False, lyr=layer: self._run(lyr))
        return action

    # ------------------------------------------------------------------ layer menu
    def _layer_menu(self, menu):
        with suppress(Exception):       # never break a menu over this
            layer = self.iface.activeLayer()
            if not isinstance(layer, QgsVectorLayer):
                return
            actions = menu.actions()
            if any(a.text() == TEXT for a in actions):
                return
            new = [self._make_action(menu, layer), self._make_clear_action(menu, layer)]
            before = None
            for index, existing in enumerate(actions):
                if not existing.isSeparator() and _plain(existing.text()) == 'filter':
                    before = actions[index + 1] if index + 1 < len(actions) else None
                    break
            for action in new:
                if before is None:
                    menu.addAction(action)
                else:
                    menu.insertAction(before, action)

    # ------------------------------------------------------------------ map menu
    def _canvas_menu(self, menu, _event):
        with suppress(Exception):
            all_layers = list(QgsProject.instance().mapLayers().values())
            active = self.iface.activeLayer()

            def ordered(layers):
                return sorted(layers, key=lambda lyr: (lyr is not active, lyr.name().lower()))

            first = menu.actions()[0] if menu.actions() else None
            self._insert(menu, first, TEXT, ordered(lyr for lyr in all_layers if can_filter(lyr)),
                         self._make_action, self._run,
                         lambda lyr: '{} ({})'.format(lyr.name(), lyr.selectedFeatureCount()))
            # Clear Filter: the layers this tool filtered (a filter set elsewhere is cleared from the Layers panel)
            self._insert(menu, first, CLEAR_TEXT, ordered(lyr for lyr in all_layers if has_our_filter(lyr)),
                         self._make_clear_action, self._run_clear, lambda lyr: lyr.name())

    def _insert(self, menu, before, text, layers, make_action, run, label):
        if not layers:
            return
        if len(layers) == 1:
            menu.insertAction(before, make_action(menu, layers[0]))
            return
        sub = QMenu(text, menu)
        sub.setIcon(_icon())
        for layer in layers:
            sub.addAction(label(layer)).triggered.connect(lambda _c=False, lyr=layer: run(lyr))
        menu.insertMenu(before, sub)

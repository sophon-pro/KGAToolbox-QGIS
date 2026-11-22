"""
The Quick Labeling toolbar button.

* The button is a switch for the active layer's labels. It is pressed whenever that layer shows
  labels, whoever set them: this button, Layer Properties > Labels or Callout Label Studio.
* Click while it is not pressed: label the layer with a random field (a text field when there is
  one). Click while it is pressed: switch the labels off - a quick label gives back whatever the layer
  had before; labels from Layer Properties or Callout Label Studio are switched off and their
  settings stay on the layer.
* Right-click (only while a quick label is on): a small window to pick the field or an expression,
  the font, style, size, colour and halo. Changes apply to the layer as you make them.

The button follows the active layer, and follows labels that are switched on or off elsewhere.
"""

from contextlib import suppress

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint, Qt

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

from ...branding import icon
from ...gui import window_reveal
from . import engine
from .dialog import QuickLabelDialog

TITLE = "Quick Labeling"


def tell_callout_studio(layer):
    """An open Callout Label Studio panel must know the labels are being removed on purpose."""
    with suppress(Exception):
        from ...gui.callout_label_tab import labels_removed_elsewhere
        labels_removed_elsewhere(layer)


class QuickLabelController:

    def __init__(self, iface):
        self.iface = iface
        self.labels = engine.QuickLabels()
        self.action = None
        self.toolbar = None
        self.dialog = None
        self._button = None
        self._watched = None                     # the layer whose label changes the button listens to
        self._bound = None                       # what the window was last bound to
        self._applying = False                   # the window's own change is being written: do not echo it back

    # ------------------------------------------------------------------ build / remove
    def build(self, toolbar, before=None):
        win = self.iface.mainWindow()
        label_icon = icon("mapping/quickLabel.png")              # the KGA icon; QGIS's own label icon if the file is missing
        if label_icon.isNull():
            label_icon = QgsApplication.getThemeIcon("/mActionLabeling.svg")
        self.action = QAction(label_icon, "Quick Labeling", win)
        self.action.setObjectName("mActionKgaQuickLabel")
        self.action.setCheckable(True)
        self.action.setToolTip("Quick Labeling (single label) - click to label the active layer with a field "
                               "(a random one); click again to turn the layer's labels off, whether they came "
                               "from here, Layer Properties or Callout Label Studio. Right-click while a quick "
                               "label is on to choose the field, font, size, colour and halo")
        self.action.triggered.connect(self._clicked)

        self.toolbar = toolbar
        if before is not None:
            toolbar.insertAction(before, self.action)
        else:
            toolbar.addAction(self.action)
        self._button = toolbar.widgetForAction(self.action)
        if self._button is not None:
            self._button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._button.customContextMenuRequested.connect(self._right_click)

        self.dialog = QuickLabelDialog(win)
        self.dialog.changed.connect(self._dialog_changed)
        self.dialog.layer_chosen.connect(self._layer_chosen)
        self.iface.currentLayerChanged.connect(self._layer_changed)
        QgsProject.instance().layersWillBeRemoved.connect(self.labels.forget)
        self._layer_changed(self.iface.activeLayer())

    def remove(self):
        with suppress(TypeError, RuntimeError):
            self.iface.currentLayerChanged.disconnect(self._layer_changed)
        with suppress(TypeError, RuntimeError):
            QgsProject.instance().layersWillBeRemoved.disconnect(self.labels.forget)
        self._unwatch()
        if self.dialog is not None:
            self.dialog.disconnect_all()
            self.dialog.hide()
            self.dialog.deleteLater()
            self.dialog = None
        if self.action is not None:
            if self.toolbar is not None:
                self.toolbar.removeAction(self.action)
            self.action.deleteLater()
            self.action = None
        self._button = None

    # ------------------------------------------------------------------ state
    def _layer(self):
        layer = self.iface.activeLayer()
        return layer if engine.can_label(layer) else None

    def _watch(self, layer):
        """Listen to the active layer: labels switched on or off in Layer Properties, the Layer Styling
        panel or Callout Label Studio repaint the layer, and the button follows."""
        if layer is self._watched:
            return
        self._unwatch()
        if layer is not None and not sip.isdeleted(layer):
            layer.repaintRequested.connect(self._refresh)
            layer.styleChanged.connect(self._refresh)
            self._watched = layer

    def _unwatch(self):
        layer, self._watched = self._watched, None
        if layer is not None and not sip.isdeleted(layer):
            with suppress(TypeError, RuntimeError):
                layer.repaintRequested.disconnect(self._refresh)
            with suppress(TypeError, RuntimeError):
                layer.styleChanged.disconnect(self._refresh)

    def _layer_changed(self, layer):
        """Available for a layer that can be labelled; pressed while that layer shows labels."""
        if self.action is None:
            return
        usable = engine.can_label(layer)
        self._watch(layer if usable else None)
        self.action.setEnabled(usable)
        self._sync(layer)
        if self.dialog is not None and self.dialog.isVisible() and usable:
            self._show_layer(layer)                    # the window follows the layer panel

    def _refresh(self, *_args):
        """The active layer's labels changed (here or elsewhere): follow."""
        if self.action is None:
            return
        layer = self.iface.activeLayer()
        if layer is not self._watched:
            return
        self._sync(layer)
        if (self.dialog is not None and self.dialog.isVisible() and self.dialog.layer is layer
                and not self._applying and not self._shows(layer)):
            self._show_layer(layer)                    # changed elsewhere (e.g. the Label Bag): show what is there now

    def _shows(self, layer):
        """The window already shows exactly the quick label `layer` has (nothing to rebind)."""
        return engine.is_quick(layer) and self._bound is not None and self._bound == engine.current(layer)

    def _sync(self, layer):
        self._set_checked(bool(engine.can_label(layer) and engine.has_labels(layer)))

    def _status(self, text, ms=6000):
        self.iface.mainWindow().statusBar().showMessage(text, ms)

    # ------------------------------------------------------------------ click
    def _clicked(self, _checked):
        layer = self._layer()
        if layer is None:
            self._set_checked(False)
            self._status("Quick Labeling: choose a vector layer with attributes first.")
            return
        if engine.has_labels(layer):                    # whatever the button said: look at the layer itself
            self._turn_off(layer)
        else:
            self._turn_on(layer)

    def _turn_on(self, layer):
        field = self.labels.enable(layer)
        if field is None:
            self._set_checked(False)
            self._status("Quick Labeling: '{}' has no field to label with.".format(layer.name()))
            return
        self._set_checked(True)
        self._status("Quick Labeling: '{}' is labelled with '{}'. Right-click the button to change the "
                     "field, font, size or colour.".format(layer.name(), field), 8000)
        if self.dialog is not None and self.dialog.isVisible() and self.dialog.layer is layer:
            self._show_layer(layer)                    # an open (greyed-out) window comes to life

    def _turn_off(self, layer):
        if not engine.is_quick(layer):
            tell_callout_studio(layer)                  # before the labels go: an open studio must not restore them
        kind = self.labels.turn_off(layer)
        self._set_checked(False)
        if self.dialog is not None:
            self.dialog.hide()
        if kind == "quick":
            self._status("Quick Labeling off for '{}'.".format(layer.name()))
        else:
            self._status("Labels off for '{}'. Their settings are kept: turn them on again in Layer "
                         "Properties > Labels, or click the button for a quick label.".format(layer.name()), 8000)

    def _set_checked(self, on):
        if self.action is None:
            return
        self.action.blockSignals(True)
        self.action.setChecked(on)
        self.action.blockSignals(False)

    # ------------------------------------------------------------------ right-click window
    def _right_click(self, pos):
        layer = self._layer()
        if layer is None or not engine.is_quick(layer):
            if layer is not None and engine.has_labels(layer):
                self._status("Quick Labeling: these labels are not a quick label (they come from Layer "
                             "Properties or Callout Label Studio). Click the button to turn them off, then "
                             "click again for a quick label.", 8000)
            else:
                self._status("Quick Labeling: click the button to turn it on, then right-click it to "
                             "choose the field and font.")
            return                                      # the window only exists while a quick label is on
        self._bind_dialog(layer)
        anchor = self._button.mapToGlobal(QPoint(0, self._button.height())) if self._button is not None else None
        if anchor is not None and not self.dialog.isVisible():
            self.dialog.move(anchor)
        if not self.dialog.isVisible():
            window_reveal.set_origin(window_reveal.button_rect(self._button))   # opens out of the button
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _show_layer(self, layer):
        """The window shows `layer`: its quick label, or a greyed-out window that says what to do."""
        if layer is None or sip.isdeleted(layer):
            return
        if engine.is_quick(layer):
            self._bind_dialog(layer)
        elif engine.has_labels(layer):
            self.dialog.bind_empty(layer, "These labels come from Layer Properties or Callout Label Studio. Click "
                                          "the Quick Labeling button to turn them off, then click it again for a "
                                          "quick label.")
        else:
            self.dialog.bind_empty(layer, "Not labelled yet. Click the Quick Labeling button to label this layer, "
                                          "or pick another layer.")

    def _layer_chosen(self, layer):
        """The user picked a layer in the window: quick-label it (without selecting it in the layer panel)."""
        if not engine.can_label(layer):
            self._status("Quick Labeling: '{}' has no field to label with.".format(layer.name()))
            self._show_layer(self.dialog.layer)           # back to the layer it was showing
            return
        if not engine.is_quick(layer) and not engine.has_labels(layer):
            field = self.labels.enable(layer)
            if field is None:
                self._status("Quick Labeling: '{}' has no field to label with.".format(layer.name()))
            else:
                self._status("Quick Labeling: '{}' is labelled with '{}'.".format(layer.name(), field), 8000)
            if layer is self.iface.activeLayer():
                self._sync(layer)
        self._show_layer(layer)

    def _bind_dialog(self, layer):
        info = engine.current(layer)
        if info is None:
            return
        field, is_expression, style = info
        self._bound = info
        self.dialog.bind(layer, field, is_expression, style)

    def _dialog_changed(self, field, is_expression, style):
        layer = self.dialog.layer
        if layer is None or sip.isdeleted(layer) or not engine.is_quick(layer):
            return
        self._applying = True
        try:
            engine.apply(layer, field, is_expression, style)
        finally:
            self._applying = False
        self._bound = engine.current(layer)

"""
The Quick Symbology toolbar button.

Select a layer in the Layers panel and click the button: a small window opens to restyle it - one
symbol (fill, outline, width) or unique values (field, colour scheme, ordered classes). If the layer is
already drawn with unique values and you have selected one of its classes in the Layers panel, the
window opens straight on that class's symbol instead.

The button is pressed while the window is open; click it again to close the window. The window follows
the active layer.
"""

from contextlib import suppress

from qgis.core import QgsApplication
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

from ...branding import icon
from ...gui import window_reveal
from . import engine
from .dialog import QuickSymbolDialog

TITLE = "Quick Symbology"


class QuickSymbolController:

    def __init__(self, iface, bag_provider=None):
        self.iface = iface
        self.bag_provider = bag_provider        # callable -> the Symbol Bag plugin, or None
        self.action = None
        self.toolbar = None
        self.dialog = None
        self._button = None

    # ------------------------------------------------------------------ build / remove
    def build(self, toolbar, before=None):
        win = self.iface.mainWindow()
        symbol_icon = icon("mapping/quickSymbol.png")            # the KGA icon; QGIS's own symbology icon if the file is missing
        if symbol_icon.isNull():
            symbol_icon = QgsApplication.getThemeIcon("/propertyicons/symbology.svg")
        self.action = QAction(symbol_icon, "Quick Symbology", win)
        self.action.setObjectName("mActionKgaQuickSymbol")
        self.action.setCheckable(True)
        self.action.setToolTip("Quick Symbology - select a layer and click to restyle it: one symbol, or unique "
                               "values with a colour scheme and ordered classes. Select one class of a "
                               "unique-value layer in the Layers panel first to edit just that class")
        self.action.triggered.connect(self._clicked)

        self.toolbar = toolbar
        if before is not None:
            toolbar.insertAction(before, self.action)
        else:
            toolbar.addAction(self.action)
        self._button = toolbar.widgetForAction(self.action)

        self.dialog = QuickSymbolDialog(win)
        self.dialog.layerChanged.connect(self._layer_restyled)
        self.dialog.layer_chosen.connect(self._layer_chosen)
        self.dialog.message.connect(self._status)
        self.dialog.bag_requested.connect(self._open_bag)
        self.dialog.visibilityChanged.connect(self._window_visibility)
        self.iface.currentLayerChanged.connect(self._layer_changed)
        self._layer_changed(self.iface.activeLayer())

    def remove(self):
        self._release_bag()
        with suppress(TypeError, RuntimeError):
            self.iface.currentLayerChanged.disconnect(self._layer_changed)
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
        return layer if engine.can_symbolize(layer) else None

    def _status(self, text, ms=8000):
        self.iface.mainWindow().statusBar().showMessage(text, ms)

    def _set_checked(self, on):
        if self.action is None:
            return
        self.action.blockSignals(True)
        self.action.setChecked(on)
        self.action.blockSignals(False)

    def _window_visibility(self, visible):
        self._set_checked(bool(visible))
        if not visible:
            self._release_bag()

    # ------------------------------------------------------------------ Symbol Bag
    def _bag(self):
        return self.bag_provider() if self.bag_provider is not None else None

    def _bag_target(self):
        """Where the Symbol Bag applies while this window is open: its layer, or the class it is editing."""
        d = self.dialog
        if d is None or not d.isVisible() or d.layer is None or sip.isdeleted(d.layer):
            return None
        return d.layer, d.class_key

    def _open_bag(self):
        bag = self._bag()
        if bag is None:
            self._status("Quick Symbology: the Symbol Bag is not available.")
            return
        bag.start_picking(self._bag_target, self._bag_applied)
        self._status("Symbol Bag: double-click a symbol to apply it to '{}'.".format(
            self.dialog.layer.name() if self.dialog.layer is not None else ""))

    def _release_bag(self):
        with suppress(Exception):
            bag = self._bag()
            if bag is not None:
                bag.stop_picking()

    def _bag_applied(self, layers):
        """A symbol came from the bag: show it in this window."""
        d = self.dialog
        if d is None or d.layer is None or sip.isdeleted(d.layer):
            return
        if any(lyr is d.layer or lyr.id() == d.layer.id() for lyr in layers):
            d.refresh()

    def _layer_changed(self, layer):
        """Available for a layer that has symbols; an open window moves to the new layer."""
        if self.action is None:
            return
        usable = engine.can_symbolize(layer)
        self.action.setEnabled(usable)
        if self.dialog is not None and self.dialog.isVisible():
            if usable:
                self.dialog.open_for(layer, self._selected_class(layer))
            else:
                self.dialog.hide()

    def _layer_chosen(self, layer):
        """The user picked a layer in the window: restyle it (without selecting it in the Layers panel)."""
        if not engine.can_symbolize(layer):
            self._status("Quick Symbology: '{}' has no symbols to restyle.".format(layer.name()))
            self.dialog.open_for(self.dialog.layer, self.dialog.class_key)       # back to what it was showing
            return
        self.dialog.open_for(layer, None)

    def _selected_class(self, layer):
        view = None
        with suppress(Exception):
            view = self.iface.layerTreeView()
        return engine.selected_class_key(view, layer)

    def _layer_restyled(self, layer):
        """The Layers panel shows the symbols: tell it they changed."""
        with suppress(Exception):
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())

    # ------------------------------------------------------------------ click
    def _clicked(self, _checked):
        layer = self._layer()
        if layer is None:
            self._set_checked(False)
            self._status("Quick Symbology: select a vector layer in the Layers panel first.")
            return
        if self.dialog.isVisible() and self.dialog.layer is layer and not self._wants_class(layer):
            self.dialog.hide()                          # the button is a switch for the window
            return
        self.dialog.open_for(layer, self._selected_class(layer))
        if not self.dialog.isVisible():
            anchor = self._button.mapToGlobal(QPoint(0, self._button.height())) if self._button is not None else None
            if anchor is not None:
                self.dialog.move(anchor)
            window_reveal.set_origin(window_reveal.button_rect(self._button))   # opens out of the button
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        self._set_checked(True)

    def _wants_class(self, layer):
        """True when a different class is selected than the one the open window shows."""
        key = self._selected_class(layer)
        return key is not None and key != self.dialog.class_key

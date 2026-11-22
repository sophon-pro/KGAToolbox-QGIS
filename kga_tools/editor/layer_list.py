"""
The Layer list shared by the Quick Labeling and Quick Symbology windows.

It lists the vector layers with geometry of the project. While Map Tabs is on, only the layers of the map you
are on are listed, not those of the other maps; with Map Tabs off every layer is listed. The scope is worked out
again each time the list opens, so a map switched to, or a layer added to it, shows up at once.
"""

from contextlib import suppress

from qgis.core import QgsMapLayerProxyModel, QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt import sip

from ..core.compat import scoped


def current_map_layer_ids():
    """The ids of the layers of the map being shown, or None when every layer counts (Map Tabs is off)."""
    with suppress(Exception):
        from ..gui import map_tabs                         # not at import time: it pulls in a lot of the GUI
        return map_tabs.active_map_layer_ids()
    return None


class CurrentMapLayerComboBox(QgsMapLayerComboBox):
    """A layer list of the vector layers with geometry of the current map."""

    def __init__(self, parent=None):
        super().__init__(parent)
        try:
            self.setFilters(QgsMapLayerProxyModel.Filter.HasGeometry)
        except AttributeError:                           # older PyQt5 enum spelling
            self.setFilters(scoped(QgsMapLayerProxyModel, 'Filter', 'HasGeometry'))

    def refresh_scope(self):
        """List only the layers of the current map. The layer being shown always stays listed."""
        ids = current_map_layer_ids()
        keep = self.currentLayer()
        outside = []
        if ids is not None:
            outside = [layer for layer in QgsProject.instance().mapLayers().values()
                       if layer.id() not in ids and layer is not keep and not sip.isdeleted(layer)]
        self.setExceptedLayerList(outside)

    def show_layer(self, layer):
        """Make `layer` the one shown, without that counting as the user picking it."""
        self.blockSignals(True)
        try:
            self.setExceptedLayerList([])                # a layer of another map must still be showable
            self.setLayer(layer)
            self.refresh_scope()
        finally:
            self.blockSignals(False)

    def showPopup(self):
        self.refresh_scope()
        super().showPopup()

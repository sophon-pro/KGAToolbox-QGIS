# -*- coding: utf-8 -*-
"""Label Bag, built into KGA Toolbox.

Same hosting as the Layer Bag (floating dock over the map, minimizes into a bubble in the bags' column);
KgaToolsPlugin adds ``action`` to the Bags drop-down on the KGA Layer toolbar and to the KGA menu's Bags
submenu. It starts closed.
"""

from ..layer_mini_panel.controller import LayerMiniPanelController
from .dock import BUBBLE_KEY, NAME

SETTINGS_KEY = "LabelBag/"


class LabelBagController(LayerMiniPanelController):
    NAME = NAME
    ACTION_NAME = "mActionKgaLabelBag"
    TIP = ("Label Bag - label the layer selected in the Layers panel: field, font, size, colour, "
           "buffer and placement, in one small floating bag")
    BUBBLE_TIP = "Label Bag – click to open"
    GLYPH = "bubble/label_bag.png"
    VISIBLE_KEY = SETTINGS_KEY + "visible"
    BUBBLE_KEY = BUBBLE_KEY

    def _new_dock(self):
        from .dock import LabelBagDock
        return LabelBagDock(self.iface, self.watcher, self.iface.mainWindow(), bag_provider=self.bag_provider)

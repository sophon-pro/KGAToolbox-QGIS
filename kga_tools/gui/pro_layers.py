# -*- coding: utf-8 -*-
"""Dress the QGIS Layers panel like ArcGIS Pro's Contents pane.

QGIS's own panel is kept (drag and drop, context menus, indicators, editing
toggles all stay), only its look changes, through a style sheet on the view:
filled-triangle expanders on the left, roomier rows, plain layer names, a flat
light-blue selection and hover, and larger legend swatches. The previous style sheet,
indentation and icon size are put back on uninstall.

The switch lives in Settings > Appearance (`core/kga_settings.py`).
"""

import os
from contextlib import suppress

from qgis.core import QgsLayerTree, QgsLayerTreeModel, QgsLayerTreeNode

from ..core.compat import enum_value

NODE_LAYER = enum_value((QgsLayerTreeNode, 'NodeType.NodeLayer'), (QgsLayerTreeNode, 'NodeLayer'))
ALLOW_LEGEND_CHANGE = enum_value((QgsLayerTreeModel, 'Flag.AllowLegendChangeState'),
                                 (QgsLayerTreeModel, 'AllowLegendChangeState'))
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import QStyledItemDelegate

from ..branding import ICON_DIR
from ..core import kga_settings as settings

INDENT = 20
#: Legend swatches (the coloured squares under a layer) are drawn at this size.
ICON_SIZE = 18
#: The marker for a view that is already ours, so install() is idempotent.
_PROP = 'kga_pro_layers'


def _url(name):
    return os.path.join(ICON_DIR, name).replace('\\', '/')


def is_dark(widget):
    return widget.palette().window().color().lightness() < 128


def build_style(dark):
    """The style sheet for the Layers view, light or dark."""
    if dark:
        selected, hover, text = '#264f78', '#2c3033', '#ffffff'
    else:
        selected, hover, text = '#cfe3f5', '#e8f1f9', '#000000'
    return """
QgsLayerTreeView {{ show-decoration-selected: 1; outline: 0; }}
QgsLayerTreeView::item {{ padding: 2px 0px; border: 0px; }}
QgsLayerTreeView::item:hover {{ background: {hover}; }}
QgsLayerTreeView::item:selected,
QgsLayerTreeView::item:selected:active,
QgsLayerTreeView::item:selected:!active {{ background: {selected}; color: {text}; }}
QgsLayerTreeView::branch {{ background: transparent; }}
QgsLayerTreeView::branch:has-children:closed {{
    border-image: none; image: url({closed}); }}
QgsLayerTreeView::branch:has-children:open {{
    border-image: none; image: url({opened}); }}
""".format(hover=hover, selected=selected, text=text,
           closed=_url('interface/pro_branch_closed.svg'),
           opened=_url('interface/pro_branch_open.svg'))


class _Delegate(QStyledItemDelegate):
    """Wraps the panel's own delegate (which paints the indicators) and leaves
    out the geometry-type icon in front of a layer name, as Pro does."""

    def __init__(self, view, inner):
        super().__init__(view)
        self.view = view
        self.inner = inner

    def _bare_layer(self, index):
        # a layer whose one symbol is embedded in its row has no children and
        # needs the icon: it is the symbol
        node = self.view.index2node(index)
        return (node is not None and QgsLayerTree.isLayer(node)
                and index.model().rowCount(index) > 0)

    def paint(self, painter, option, index):
        if self.inner is None:
            return super().paint(painter, option, index)
        if self._bare_layer(index):
            option.decorationSize = QSize(0, 0)
        self.inner.paint(painter, option, index)

    def sizeHint(self, option, index):
        if self.inner is None:
            return super().sizeHint(option, index)
        return self.inner.sizeHint(option, index)


class ProLayers:
    """Switch the Layers panel between the QGIS and the Pro look."""

    def __init__(self, iface):
        self.iface = iface
        self._saved = None
        self._delegate = None

    @property
    def installed(self):
        return self._saved is not None

    def _view(self):
        return self.iface.layerTreeView()

    def install(self):
        if self.installed:
            return
        view = self._view()
        model = view.layerTreeModel()
        self._saved = (view.styleSheet(), view.indentation(), view.iconSize(),
                       QFont(model.layerTreeNodeFont(NODE_LAYER)),
                       model.testFlag(ALLOW_LEGEND_CHANGE),
                       view.itemDelegate())
        font = QFont(self._saved[3])
        font.setBold(False)
        model.setLayerTreeNodeFont(NODE_LAYER, font)
        # no tick boxes on the legend rows
        model.setFlag(ALLOW_LEGEND_CHANGE, False)
        inner = view.itemDelegate()
        self._delegate = _Delegate(view, inner)
        view.setItemDelegate(self._delegate)
        view.setStyleSheet(build_style(is_dark(view)))
        view.setIndentation(INDENT)
        view.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        view.setProperty(_PROP, True)

    def uninstall(self):
        if not self.installed:
            return
        sheet, indent, icon, font, can_check, inner = self._saved
        self._saved = None
        with suppress(RuntimeError):    # the view may already be gone
            view = self._view()
            view.setStyleSheet(sheet)
            view.setIndentation(indent)
            view.setIconSize(icon)
            model = view.layerTreeModel()
            model.setLayerTreeNodeFont(NODE_LAYER, font)
            model.setFlag(ALLOW_LEGEND_CHANGE, can_check)
            view.setItemDelegate(inner)
            self._delegate = None
            view.setProperty(_PROP, None)

    def refresh(self):
        """Follow the Settings switch."""
        if settings.pro_layers():
            self.install()
        else:
            self.uninstall()

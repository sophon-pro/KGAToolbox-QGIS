# -*- coding: utf-8 -*-
"""Clip Basemap by Boundary: the window.

A small modeless window: pick a boundary (a polygon layer, a layer or map
extent, or a rectangle or polygon drawn on the map), pick a basemap (a preset
such as Google Satellite or OpenStreetMap, or a raster layer already in the
project) and press Clip. The basemap then shows only inside the boundary.
"""

import traceback

from qgis.core import (Qgis, QgsCoordinateTransform, QgsMessageLog, QgsProject,
                       QgsRasterLayer)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout)

from ..branding import LOG_TAG
from ..core import clip_basemap as CB
from ..core import data_sources as DS
from ..core import dem_sources as DEM
from .roi_picker import RoiPicker

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

TITLE = 'Clip Basemap by Boundary'
BLUE = '#174592'

STYLE = """
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 22px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaHint { color: palette(mid); }
""" % {'blue': BLUE}

_INSTANCE = None


def tr(text):
    return text


class ClipBasemapDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaClipBasemapDialog')
        self.setMinimumWidth(440)
        self.setStyleSheet(STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.basemap_combo = QComboBox(self)
        self.basemap_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Fixed)
        form.addRow(tr('Basemap'), self.basemap_combo)
        outer.addLayout(form)

        self.area_picker = RoiPicker(
            iface.mapCanvas() if iface is not None else None, self,
            title='Boundary')
        self.area_picker.message.connect(self._say)
        self.area_picker.changed.connect(self._refresh)
        index = self.area_picker.mode_combo.findData(DEM.ROI_LAYER_CLIP)
        self.area_picker.mode_combo.setCurrentIndex(max(index, 0))
        outer.insertWidget(0, self.area_picker)

        self.status = QLabel('', self)
        self.status.setObjectName('kgaHint')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.clip_button = QPushButton(tr('Clip'), self)
        self.clip_button.setObjectName('kgaPrimary')
        self.clip_button.setDefault(True)
        self.clip_button.clicked.connect(self.clip)
        buttons.addWidget(self.clip_button)
        self.unclip_button = QPushButton(tr('Remove Clip'), self)
        self.unclip_button.setToolTip(tr(
            'Delete the clip mask so the basemap shows everywhere again.'))
        self.unclip_button.clicked.connect(self.unclip)
        buttons.addWidget(self.unclip_button)
        close = QPushButton(tr('Close'), self)
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        outer.addLayout(buttons)

        self._fill_basemaps()
        self._refresh()

    # ------------------------------------------------------------------ state

    def _fill_basemaps(self):
        """Basemaps already in the layer panel (top one first), then presets.

        The top-most tile layer is the default, so with one basemap in the
        map it is picked, and with several the upper one is.
        """
        combo = self.basemap_combo
        combo.clear()
        project = QgsProject.instance()
        for node in project.layerTreeRoot().findLayers():
            layer = node.layer()
            if (isinstance(layer, QgsRasterLayer)
                    and layer.providerType() == 'wms'
                    and 'type=xyz' in layer.source()
                    and not layer.customProperty(CB.MASK_PROPERTY)):
                combo.addItem(tr('{} (in project)').format(layer.name()),
                              ('layer', layer.id()))
        for basemap in DS.BASEMAPS:
            combo.addItem(basemap.label, ('preset', basemap.id))
        combo.setCurrentIndex(0)

    def _say(self, text, error=False):
        self.status.setStyleSheet('color: #B3261E;' if error else '')
        self.status.setText(text)

    def _refresh(self, *_args):
        picker = self.area_picker
        layer = picker.layer_combo.currentLayer()
        if picker.mode() == DEM.ROI_LAYER_CLIP and layer is not None:
            has = layer.selectedFeatureCount() > 0
            picker.selected_check.setEnabled(has)
            if not has:
                picker.selected_check.setChecked(False)

    def _basemap_layer(self):
        kind, key = self.basemap_combo.currentData()
        if kind == 'layer':
            layer = QgsProject.instance().mapLayer(key)
            if layer is None:
                raise CB.ClipError('That basemap layer is no longer in the '
                                   'project.')
            return layer
        return CB.find_or_add_basemap(key)

    # ------------------------------------------------------------------- work

    def clip(self):
        picker = self.area_picker
        mode = picker.mode()
        layer = picker.layer_combo.currentLayer()
        try:
            region = picker.region()    # raises when the area is not set
            basemap = self._basemap_layer()
            if mode == DEM.ROI_LAYER_CLIP:
                CB.clip(basemap, layer, picker.selected_check.isChecked())
                label = layer.name()
            else:
                CB.clip(basemap, geometry=region.geometry, crs=region.crs)
                label = region.label
        except (CB.ClipError, DEM.DemError) as exc:
            self._say(str(exc), error=True)
            return
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Clip Basemap failed:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self._say(str(exc), error=True)
            return
        if iface is not None and mode != DEM.ROI_CANVAS:
            canvas = iface.mapCanvas()
            transform = QgsCoordinateTransform(
                region.crs, canvas.mapSettings().destinationCrs(),
                QgsProject.instance())
            canvas.setExtent(transform.transformBoundingBox(
                region.geometry.boundingBox()))
            canvas.refresh()
        if mode == DEM.ROI_DRAW:
            picker.clear_drawn()
        elif mode == DEM.ROI_LAYER_CLIP:
            # Hide the boundary so its own fill does not cover the basemap.
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(False)
        self._say(tr('"{}" now shows only inside: {}.').format(
            basemap.name(), label))

    def unclip(self):
        try:
            basemap = self._basemap_layer()
        except CB.ClipError as exc:
            self._say(str(exc), error=True)
            return
        removed = CB.remove_masks(basemap)
        self._say(tr('Clip removed.') if removed
                  else tr('This basemap has no clip to remove.'))

    def closeEvent(self, event):
        self.area_picker.disarm()
        self.area_picker.clear_drawn()
        super().closeEvent(event)

    def shutdown(self):
        self.area_picker.teardown()
        self.close()
        self.deleteLater()


def _alive(dialog):
    if dialog is None:
        return False
    try:
        dialog.objectName()
    except RuntimeError:                        # deleted by Qt
        return False
    return True


def show_clip_basemap():
    """Open the window, or bring the one that is already open forward."""
    global _INSTANCE
    if not _alive(_INSTANCE):
        _INSTANCE = ClipBasemapDialog(
            iface.mainWindow() if iface is not None else None)
    else:
        _INSTANCE._fill_basemaps()
    _INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
    _INSTANCE.show()
    _INSTANCE.raise_()
    _INSTANCE.activateWindow()
    return _INSTANCE


def close_clip_basemap():
    """Close the window on plugin unload."""
    global _INSTANCE
    dialog, _INSTANCE = _INSTANCE, None
    if _alive(dialog):
        dialog.shutdown()

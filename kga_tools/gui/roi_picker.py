# -*- coding: utf-8 -*-
"""The "Region of interest" group: map extent, layer extent, layer boundary, or
a shape drawn on the map.

One widget for every tool that starts from an area on the map. It owns the
combo, the draw buttons, the layer combo and the map tool that draws, and
hands back a ``dem_sources.Roi`` on request. The drawing uses ``RoiMapTool``, a
map tool of its own rather than an iface action.

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

from qgis.core import QgsGeometry
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                                 QHBoxLayout, QPushButton, QWidget)

from ..core import dem_sources as DEM
from ..core import duplicate_layer as DUP
from ..core.modify_features import layer_filter
from .roi_tools import (SHAPE_POLYGON, SHAPE_RECTANGLE, RoiMapTool, drop_band,
                        make_band)

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: Optional first mode for tools whose default is "no area at all".
ROI_WHOLE = 'whole'

ROI_MODES = (
    (DEM.ROI_CANVAS, 'Current map extent'),
    (DEM.ROI_DRAW, 'Draw a rectangle or polygon on the map'),
    (DEM.ROI_LAYER_CLIP, 'Boundary of a polygon layer'),
    (DEM.ROI_LAYER_EXTENT, 'Extent of a layer'),
)


def tr(text):
    return text


class RoiPicker(QGroupBox):
    """Choose an area. Emits `changed` whenever the answer may have changed."""

    changed = pyqtSignal()
    #: A hint for the status line, e.g. how to finish drawing.
    message = pyqtSignal(str)

    def __init__(self, canvas, parent=None, allow_whole=False,
                 title='Region of interest'):
        super().__init__(tr(title), parent)
        self.canvas = canvas
        self._drawn = None                  # (geometry, canvas crs, shape)
        self._tool = None
        self._band = None
        self._previous_tool = None
        self._layer_cache = {}
        self._locked = False
        self._torn_down = False

        self.form = form = QFormLayout(self)

        self.mode_combo = QComboBox(self)
        if allow_whole:
            self.mode_combo.addItem(tr('Whole raster'), ROI_WHOLE)
        for mode, label in ROI_MODES:
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setToolTip(tr(
            'A map or layer extent gives its bounding box. A drawn shape or a '
            'layer boundary cuts the raster to the shape.'))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow(tr('Area'), self.mode_combo)

        self.draw_widget = QWidget(self)
        row = QHBoxLayout(self.draw_widget)
        row.setContentsMargins(0, 0, 0, 0)
        self.rect_button = QPushButton(tr('Draw rectangle'), self.draw_widget)
        self.rect_button.setCheckable(True)
        self.rect_button.setToolTip(tr(
            'Press and drag on the map to draw a rectangle.'))
        self.rect_button.clicked.connect(
            lambda checked: self._arm(SHAPE_RECTANGLE, checked))
        row.addWidget(self.rect_button)
        self.poly_button = QPushButton(tr('Draw polygon'), self.draw_widget)
        self.poly_button.setCheckable(True)
        self.poly_button.setToolTip(tr(
            'Click each corner on the map, then double-click, right-click or '
            'press Enter to close the polygon. Backspace removes the last '
            'corner, Escape starts again.'))
        self.poly_button.clicked.connect(
            lambda checked: self._arm(SHAPE_POLYGON, checked))
        row.addWidget(self.poly_button)
        self.clear_button = QPushButton(tr('Clear'), self.draw_widget)
        self.clear_button.clicked.connect(self.clear_drawn)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        form.addRow('', self.draw_widget)

        self.layer_combo = QgsMapLayerComboBox(self)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.layerChanged.connect(self._emit_changed)
        form.addRow(tr('Layer'), self.layer_combo)

        self.selected_check = QCheckBox(tr('Selected features only'), self)
        self.selected_check.toggled.connect(self._emit_changed)
        form.addRow('', self.selected_check)

        if canvas is not None:
            canvas.extentsChanged.connect(self._on_canvas_extent)
            canvas.mapToolSet.connect(self._sync_buttons)
        self._on_mode_changed()

    # ---------------------------------------------------------------- state

    def mode(self):
        return self.mode_combo.currentData()

    def set_locked(self, locked):
        """Disable the controls while a job runs."""
        self._locked = locked
        for widget in (self.mode_combo, self.draw_widget, self.layer_combo,
                       self.selected_check):
            widget.setEnabled(not locked)
        if locked:
            self.disarm()

    def _emit_changed(self, *_args):
        if not self._torn_down:
            self.changed.emit()

    def _row_visible(self, widget, visible):
        label = self.form.labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    def _on_mode_changed(self, *_args):
        mode = self.mode()
        layer_mode = mode in (DEM.ROI_LAYER_CLIP, DEM.ROI_LAYER_EXTENT)
        self._row_visible(self.draw_widget, mode == DEM.ROI_DRAW)
        self._row_visible(self.layer_combo, layer_mode)
        self._row_visible(self.selected_check, mode == DEM.ROI_LAYER_CLIP)
        if layer_mode:
            self._apply_layer_filter()
        if self._band is not None:
            self._band.setVisible(mode == DEM.ROI_DRAW)
        if mode != DEM.ROI_DRAW:
            self.disarm()
        self._emit_changed()

    def _apply_layer_filter(self):
        """Polygon layers to take a boundary from; any layer for an extent."""
        if self.mode() == DEM.ROI_LAYER_CLIP:
            self.layer_combo.setFilters(layer_filter('PolygonLayer'))
        else:
            self.layer_combo.setFilters(layer_filter('HasGeometry',
                                                     'RasterLayer'))
        if self.layer_combo.currentLayer() is None:
            active = iface.activeLayer() if iface is not None else None
            index = self.layer_combo.model().indexFromLayer(active) \
                if active is not None else None
            if index is not None and index.isValid():
                self.layer_combo.setLayer(active)
            elif self.layer_combo.count() > 1:
                self.layer_combo.setCurrentIndex(1)

    def _on_canvas_extent(self, *_args):
        if self.isVisible() and not self._locked \
                and self.mode() == DEM.ROI_CANVAS:
            self._emit_changed()

    # -------------------------------------------------------------- drawing

    def _arm(self, shape, checked):
        if self.canvas is None:
            self._sync_buttons()
            return
        if not checked:
            self.disarm()
            return
        if self._tool is None:
            self._tool = RoiMapTool(self.canvas, shape)
            self._tool.drawn.connect(self._on_drawn)
            self._tool.message.connect(self.message)
        else:
            self._tool.set_shape(shape)
        current = self.canvas.mapTool()
        if current is not None and current is not self._tool:
            self._previous_tool = current
        self.canvas.setMapTool(self._tool)
        self._sync_buttons()
        self.message.emit(
            tr('Press and drag on the map to draw the rectangle.')
            if shape == SHAPE_RECTANGLE else
            tr('Click each corner on the map; double-click, right-click or '
               'press Enter to finish.'))

    def disarm(self):
        """Hand the canvas back to the tool it had before drawing."""
        if self.canvas is not None and self._drawing():
            previous, self._previous_tool = self._previous_tool, None
            if previous is not None:
                try:
                    self.canvas.setMapTool(previous)
                except RuntimeError:        # the old tool was deleted
                    self.canvas.unsetMapTool(self._tool)
            else:
                self.canvas.unsetMapTool(self._tool)
        self._sync_buttons()

    def _drawing(self):
        return (self._tool is not None and self.canvas is not None
                and self.canvas.mapTool() is self._tool)

    def _sync_buttons(self, *_args):
        active = self._drawing()
        shape = self._tool.shape if self._tool is not None else None
        for button, own in ((self.rect_button, SHAPE_RECTANGLE),
                            (self.poly_button, SHAPE_POLYGON)):
            button.blockSignals(True)
            button.setChecked(active and shape == own)
            button.blockSignals(False)

    def _on_drawn(self, geometry):
        crs = self.canvas.mapSettings().destinationCrs()
        self._drawn = (geometry, crs, self._tool.shape)
        if self._band is None:
            self._band = make_band(self.canvas, fill_alpha=25)
        self._band.setToGeometry(geometry, crs)
        self._band.setVisible(True)
        self.disarm()
        self.message.emit(tr('Area drawn. Draw again to replace it.'))
        self._emit_changed()

    def clear_drawn(self):
        self._drawn = None
        if self._band is not None:
            drop_band(self.canvas, self._band)
            self._band = None
        self._emit_changed()

    # ------------------------------------------------------------------ roi

    def forget_cached_boundary(self):
        """Re-read the layer on the next call; its features may have changed."""
        self._layer_cache.clear()

    def roi(self):
        """The ROI for the current mode. Raises DemError when it is unset."""
        mode = self.mode()
        if mode == DEM.ROI_CANVAS:
            if self.canvas is None:
                raise DEM.DemError(tr('The map canvas is not available.'))
            return DEM.roi_from_extent(
                self.canvas.extent(),
                self.canvas.mapSettings().destinationCrs(),
                tr('Current map extent'))
        if mode == DEM.ROI_DRAW:
            if self._drawn is None:
                raise DEM.DemError(tr(
                    'Click <b>Draw rectangle</b> or <b>Draw polygon</b>, then '
                    'draw the area on the map.'))
            geometry, crs, shape = self._drawn
            return DEM.roi_from_geometry(
                geometry, crs, tr('Drawn rectangle') if shape ==
                SHAPE_RECTANGLE else tr('Drawn polygon'))
        layer = self.layer_combo.currentLayer()
        if layer is None:
            raise DEM.DemError(tr('Choose a layer.') if mode ==
                               DEM.ROI_LAYER_EXTENT else
                               tr('Choose a polygon layer.'))
        if mode == DEM.ROI_LAYER_CLIP:
            selected = self.selected_check.isChecked()
            key = (layer.id(), selected,
                   tuple(sorted(layer.selectedFeatureIds())) if selected
                   else ())
            if key not in self._layer_cache:
                # A boundary is a union of every feature; once is enough.
                self._layer_cache = {key: DEM.roi_from_layer(layer, selected)}
            return self._layer_cache[key]
        return DEM.roi_from_extent(layer.extent(), layer.crs(),
                                   tr('Extent of {0}').format(layer.name()))

    def region(self):
        """The area as a `duplicate_layer.Region` in its own CRS, or None.

        None means the whole raster. Unlike `roi()` nothing is converted to
        latitude and longitude, so a map or layer extent stays an exact box in
        its own CRS. Raises DemError when the area is not set.
        """
        mode = self.mode()
        if mode == ROI_WHOLE:
            return None
        if mode == DEM.ROI_CANVAS:
            if self.canvas is None:
                raise DEM.DemError(tr('The map canvas is not available.'))
            return DUP.Region(QgsGeometry.fromRect(self.canvas.extent()),
                              self.canvas.mapSettings().destinationCrs(),
                              False, tr('Current map extent'))
        if mode == DEM.ROI_DRAW:
            if self._drawn is None:
                raise DEM.DemError(tr(
                    'Click <b>Draw rectangle</b> or <b>Draw polygon</b>, then '
                    'draw the area on the map.'))
            geometry, crs, _shape = self._drawn
            return DUP.Region(geometry, crs, True, tr('Drawn area'))
        layer = self.layer_combo.currentLayer()
        if layer is None:
            raise DEM.DemError(tr('Choose a layer.') if mode ==
                               DEM.ROI_LAYER_EXTENT else
                               tr('Choose a polygon layer.'))
        if mode == DEM.ROI_LAYER_EXTENT:
            return DUP.Region(QgsGeometry.fromRect(layer.extent()),
                              layer.crs(), False,
                              tr('Extent of {0}').format(layer.name()))
        selected = self.selected_check.isChecked()
        if selected and layer.selectedFeatureCount() == 0:
            raise DEM.DemError(tr(
                '"{0}" has no selected features. Select the ones to cut to, '
                'or untick "Selected features only".').format(layer.name()))
        features = layer.getSelectedFeatures() if selected \
            else layer.getFeatures()
        parts = [f.geometry() for f in features
                 if f.hasGeometry() and not f.geometry().isEmpty()]
        if not parts:
            raise DEM.DemError(tr('"{0}" has no polygons to cut to.').format(
                layer.name()))
        merged = QgsGeometry.unaryUnion(parts)
        if merged is None or merged.isEmpty():
            raise DEM.DemError(tr(
                'The polygons of "{0}" could not be merged into one boundary.'
            ).format(layer.name()))
        return DUP.Region(merged, layer.crs(), True,
                          tr('Boundary of {0}').format(layer.name()))

    # ------------------------------------------------------------- teardown

    def teardown(self):
        """Give the canvas back and remove the drawn outline."""
        if self._torn_down:
            return
        self._torn_down = True
        if self.canvas is not None:
            for signal, slot in ((self.canvas.extentsChanged,
                                  self._on_canvas_extent),
                                 (self.canvas.mapToolSet,
                                  self._sync_buttons)):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            if self._tool is not None and self.canvas.mapTool() is self._tool:
                try:
                    if self._previous_tool is not None:
                        self.canvas.setMapTool(self._previous_tool)
                    else:
                        self.canvas.unsetMapTool(self._tool)
                except RuntimeError:
                    pass
            if self._tool is not None:
                self._tool.remove()
                self._tool = None
            drop_band(self.canvas, self._band)
            self._band = None

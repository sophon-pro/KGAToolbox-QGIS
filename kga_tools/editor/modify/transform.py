"""
Alignment tools of the Modify Features pane: Move, Rotate, Scale and Move To.

All four act on the selected features of every editable layer that is shown on the map (select with the
Select tool, or just click a feature while one of these tools is active and nothing is selected). They
draw a live preview and apply as one undoable edit command per layer. Each has a page in the pane with
fields for typed values (offset, angle, factor, coordinates); Rotate, Scale and Move To work about an
anchor: the centre of the selection unless the page's anchor buttons set it elsewhere.

    Move     press, drag, release - or type dX, dY on the page
    Rotate   press, drag around the anchor, release - or type an angle
    Scale    press, drag away from / towards the anchor, release - or type a factor
    Move To  choose a method (Absolute / Delta / Direction/Distance), type or click the values, press Move To

Everything works in the map's CRS and is converted back to each layer's CRS, so layers in other CRSs
follow along.
"""

import math
import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSnappingConfig,
    QgsUnitTypes,
)
from qgis.gui import QgsMapCanvasSnappingUtils, QgsMapTool, QgsRubberBand, QgsSnapIndicator, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor, QTransform
from qgis.PyQt.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QWidget,
)

from ..selection import _transform, all_vector_layers, map_layers
from ..snapping import all_types
from .page import PageMixin, SelectModeMixin, add_form, button, combo, section_label, separator, spin
from ...core.compat import enum_value

MARKER_CIRCLE = enum_value((Qgis, 'VertexMarkerType.Circle'), (QgsVertexMarker, 'IconType.ICON_CIRCLE'))  # QGIS >= 3.40 / 4

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
SNAP_TOLERANCE_PX = 12     # snapping when the project's own snapping is switched off
CLICK_PIXELS = 3           # a press-release that moved less than this is a click, not a drag
PREVIEW_MAX = 500          # features drawn while previewing a rotation or scale
MIN_FACTOR = 1e-9
ORANGE = QColor(255, 140, 0, 230)
ORANGE_FILL = QColor(255, 140, 0, 70)


# ---------------------------------------------------------------------------------------- geometry
def translated(geom, dx, dy):
    g = QgsGeometry(geom)
    g.translate(dx, dy)
    return g


def rotated(geom, ccw_degrees, center):
    """Rotate counter-clockwise (QGIS itself rotates clockwise, hence the sign)."""
    g = QgsGeometry(geom)
    g.rotate(-ccw_degrees, QgsPointXY(center))
    return g


def scaled(geom, factor, center):
    g = QgsGeometry(geom)
    t = QTransform()
    t.translate(center.x(), center.y())
    t.scale(factor, factor)
    t.translate(-center.x(), -center.y())
    g.transform(t)
    return g


# ---------------------------------------------------------------------------------------- selection
def editable_selection(canvas):
    """[(layer, selected ids)] of the layers that are shown on the map and in edit mode."""
    shown = map_layers(canvas)
    return [(layer, layer.selectedFeatureIds()) for layer in all_vector_layers()
            if layer.isEditable() and layer.selectedFeatureCount() and (shown is None or layer.id() in shown)]


def apply_to_selection(canvas, fn, text):
    """
    Replace the geometry of every selected feature (shown, editable layers) by fn(geometry in the map's
    CRS); fn may return None to leave a feature alone. One edit command per layer, so Ctrl+Z / Discard Edits
    undo it. Returns (changed, layers, skipped).
    """
    canvas_crs = canvas.mapSettings().destinationCrs()
    shown = map_layers(canvas)
    changed, layers, skipped = 0, 0, []
    for layer in all_vector_layers():
        ids = layer.selectedFeatureIds()
        if not ids or (shown is not None and layer.id() not in shown):
            continue                                    # never change what you cannot see
        if not layer.isEditable():
            skipped.append(layer.name())
            continue
        same = layer.crs() == canvas_crs
        to_map = None if same else _transform(layer.crs(), canvas_crs)
        to_layer = None if same else _transform(canvas_crs, layer.crs())
        layer.beginEditCommand(text)
        ok, count = True, 0
        for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids).setNoAttributes()):
            g = QgsGeometry(f.geometry())
            if g.isEmpty():
                continue
            if to_map is not None:
                g.transform(to_map)
            g = fn(g)
            if g is None:
                continue                                # fn declined this feature: leave it as it is
            if to_layer is not None:
                g.transform(to_layer)
            if not layer.changeGeometry(f.id(), g):
                ok = False
                break
            count += 1
        if ok:
            layer.endEditCommand()
            changed += count
            layers += 1
        else:
            layer.destroyEditCommand()
            skipped.append(layer.name())
        layer.triggerRepaint()
    return changed, layers, skipped


# ---------------------------------------------------------------------------------------- tools
class TransformTool(SelectModeMixin, PageMixin, QgsMapTool):
    """Shared behaviour: capture the selection, preview on rubber bands, anchor marker, apply."""

    KEY = ""
    LABEL = ""
    CURSOR = Qt.CursorShape.CrossCursor
    SHOW_ANCHOR = False         # draw the anchor marker while the tool is idle
    HOVER = False               # preview follows the mouse without a button held (Move To)
    SELECT_ONE_SHOT = True      # select, then move: a selection made with the Select button ends Select mode
    # Rotate / Scale: their drag works anywhere, so a plain click (no drag) is free to select or clear, and a press
    # on a feature while nothing is selected selects it first. Move To / Offset need the click for themselves.
    CLICK_SELECTS = False
    SHOWS_SELECTION = False     # the selected-features list on the page says it (no "n features selected" line)
    HINT = ""
    _gesture = None             # "select": the current press-drag-release belongs to the Edit Session select tool
    _press_px = None            # where on the screen the current drag began

    def __init__(self, canvas, iface, select_tool=None):
        super().__init__(canvas)
        self.iface = iface
        self.select_tool = select_tool      # the Edit Session select tool: a click selects when nothing is
        self._items = None
        self._sig = None
        self._geoms = []                    # [(geometry type, geometry in the map's CRS)] for the preview
        self._bbox = None
        self._anchor = None                 # set from the page; None = centre of the selection
        self._picking_anchor = False        # the next map click places the anchor
        self._start = None
        self._bands = {}
        self.preview_offset = None          # (dx, dy) the preview is shifted by, if it is a shift
        self.snap_indicator = QgsSnapIndicator(canvas)
        self._own_snapping = None           # snapping used when the project's is off (every type, all layers)
        self.marker = QgsVertexMarker(canvas)
        self.marker.setIconType(MARKER_CIRCLE)
        self.marker.setColor(QColor(220, 0, 0))
        self.marker.setIconSize(14)
        self.marker.setPenWidth(2)
        self.marker.hide()
        self.setCursor(QCursor(self.CURSOR))

    # ------------------------------------------------------------------ lifecycle
    def _selection_changed(self, *_args):
        """Whoever changed the selection (the select tool, another tool): what was captured is stale."""
        self._items = None
        self.refresh_page()

    def activate(self):
        super().activate()
        self.canvas().selectionChanged.connect(self._selection_changed)
        self._anchor = None
        self._picking_anchor = False
        self.start_select_mode()                        # nothing to work on yet: start by selecting
        self._refresh(quiet=True)
        self.refresh_page()
        self.say(self.HINT, 10000)

    def deactivate(self):
        with suppress(TypeError, RuntimeError):
            self.canvas().selectionChanged.disconnect(self._selection_changed)
        self._clear_preview()
        self._items = None
        self._gesture = None
        self._picking_anchor = False
        self._cancel_selecting()
        self.marker.hide()
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()

    def dispose(self):
        self._clear_preview()
        for band in self._bands.values():
            self.canvas().scene().removeItem(band)
        self._bands = {}
        self.canvas().scene().removeItem(self.marker)
        self.snap_indicator.setMatch(QgsPointLocator.Match())

    # ------------------------------------------------------------------ selection
    def _signature(self):
        return tuple((layer.id(), layer.selectedFeatureCount(), layer.isEditable()) for layer in all_vector_layers()
                     if layer.selectedFeatureCount())

    def _capture(self):
        """Read the selection (geometries in the map's CRS) for previewing and for the anchor."""
        self._clear_preview()
        self._items = editable_selection(self.canvas())
        self._sig = self._signature()
        self._geoms, self._bbox = [], None
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        for layer, ids in self._items:
            to_map = None if layer.crs() == canvas_crs else _transform(layer.crs(), canvas_crs)
            for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids).setNoAttributes()):
                g = QgsGeometry(f.geometry())
                if g.isEmpty():
                    continue
                if to_map is not None:
                    g.transform(to_map)
                box = g.boundingBox()
                if self._bbox is None:
                    self._bbox = QgsRectangle(box)
                else:
                    self._bbox.combineExtentWith(box)
                if len(self._geoms) < PREVIEW_MAX:
                    self._geoms.append((layer.geometryType(), g))
        return bool(self._items) and self._bbox is not None

    def _refresh(self, quiet=False):
        """Re-read the selection if it changed; keeps the anchor marker right. True if features are ready."""
        if self._items is None or self._sig != self._signature():
            self._capture()
        ready = bool(self._items) and self._bbox is not None
        if not ready and not quiet:
            n = sum(layer.selectedFeatureCount() for layer in all_vector_layers())
            self.say("select features first - click one on the map." if not n else
                     "the selected features are not in an editable layer shown on the map.")
        self._show_marker(ready)
        return ready

    def anchor(self):
        if self._anchor is not None:
            return QgsPointXY(self._anchor)
        return QgsPointXY(self._bbox.center())

    def _show_marker(self, ready):
        if ready and (self.SHOW_ANCHOR or self._anchor is not None):
            self.marker.setCenter(self.anchor())
            self.marker.show()
        else:
            self.marker.hide()

    # ------------------------------------------------------------------ preview
    def _band(self, gtype):
        band = self._bands.get(gtype)
        if band is None:
            band = QgsRubberBand(self.canvas(), gtype)
            band.setColor(ORANGE)
            band.setFillColor(ORANGE_FILL)
            band.setWidth(2)
            self._bands[gtype] = band
        return band

    def _draw(self, fn=None, offset=None):
        """Draw the selection through fn(geometry), or shifted by offset (dx, dy)."""
        self.preview_offset = offset
        for gtype, band in self._bands.items():
            band.reset(gtype)
            band.setTranslationOffset(0, 0)
        for gtype, g in self._geoms:
            shown = g if fn is None else fn(g)
            if shown is not None and not shown.isNull():            # fn may decline a feature
                self._band(gtype).addGeometry(shown, QgsCoordinateReferenceSystem(), False)
        for band in self._bands.values():
            if offset is not None:
                band.setTranslationOffset(*offset)
            band.updatePosition()
            band.update()

    def _clear_preview(self):
        self._start = None
        self.preview_offset = None
        for gtype, band in self._bands.items():
            band.reset(gtype)
            band.setTranslationOffset(0, 0)

    # ------------------------------------------------------------------ snapping
    def _snap(self, e):
        """
        (point, match) for a mouse event: snapped to vertices, edges, midpoints ... of the features on the map.
        Follows the project's snapping settings. While the anchor is being placed the tool snaps by itself to
        every type when the project's snapping is off, so the anchor can always be put exactly on a feature.
        """
        if QgsProject.instance().snappingConfig().enabled() or not self._picking_anchor:
            point = QgsPointXY(e.snapPoint())             # must run BEFORE mapPointMatch()
            return point, e.mapPointMatch()
        if self._own_snapping is None:
            config = QgsSnappingConfig(QgsProject.instance())
            config.setEnabled(True)
            config.setMode(Qgis.SnappingMode.AllLayers)
            config.setTypeFlag(all_types())
            config.setTolerance(SNAP_TOLERANCE_PX)
            config.setUnits(Qgis.MapToolUnit.Pixels)
            self._own_snapping = QgsMapCanvasSnappingUtils(self.canvas())
            self._own_snapping.setConfig(config)
        self._own_snapping.setMapSettings(self.canvas().mapSettings())      # tolerance is in pixels of the current view
        match = self._own_snapping.snapToMap(e.originalMapPoint())
        return (QgsPointXY(match.point()) if match.isValid() else QgsPointXY(e.mapPoint())), match

    def _show_snap(self, match):
        self.snap_indicator.setMatch(match)

    # ------------------------------------------------------------------ mouse
    def canvasPressEvent(self, e):
        if self.forward_select("canvasPressEvent", e):
            return
        if e.button() != Qt.MouseButton.LeftButton or self._picking_anchor:
            return
        if not self._has_selection() and not self._press_with_nothing_selected(e):
            return
        if not self._capture() or not self._refresh():
            return
        self._start, _match = self._snap(e)
        self._press_px = e.pixelPoint() if hasattr(e, "pixelPoint") else None
        self.on_press(self._start)

    def _press_with_nothing_selected(self, e):
        """Nothing is selected. True when the tool's own gesture should carry on (a feature was picked)."""
        if self.CLICK_SELECTS and self.select_tool is not None and hasattr(self.select_tool, "hits_at"):
            if self._under_mouse(e):
                self._select_click(e)                            # press on a feature: select it, then go on
                return True
            self._start_select_gesture(e)                        # empty map: the select tool (click or marquee)
            return False
        self._select_under(e)                                    # a click picks the feature to work on
        return False

    def canvasMoveEvent(self, e):
        if self._gesture == "select":
            self.select_tool.canvasMoveEvent(e)
            return
        if self.forward_select("canvasMoveEvent", e):
            return
        point, match = self._snap(e)
        self._show_snap(match)
        if self._start is not None:
            self.on_move(point)
        elif self.HOVER and not self._picking_anchor:
            if self._refresh(quiet=True):
                self.on_move(point)
        else:
            self._refresh(quiet=True)        # keeps the anchor marker with the selection

    def canvasReleaseEvent(self, e):
        if self._gesture == "select":
            self._gesture = None
            self.select_tool.canvasReleaseEvent(e)
            self._items = None
            self.refresh_page()
            return
        if self.forward_select("canvasReleaseEvent", e):
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._picking_anchor:
            point = self._snap(e)[0]
            self._picking_anchor = False
            self.snap_indicator.setMatch(QgsPointLocator.Match())
            if self._refresh():
                self.set_anchor(point)
            return
        if self._start is None and self.HOVER and self._refresh():
            self._start = self._snap(e)[0]               # Move To: the click itself is the whole gesture
        if self._start is None:
            return
        point = self._snap(e)[0]
        start = self._start
        self._clear_preview()
        if self.CLICK_SELECTS and self._is_click(start, point, e):
            self._select_click(e)                        # no drag: a click, so it selects (or clears on empty map)
            return
        self.on_release(start, point)

    # ------------------------------------------------------------------ selecting with the mouse
    def _under_mouse(self, e):
        """[(layer, fid)] of the features under the mouse, top layer / nearest first; None without a hit test."""
        hits_at = getattr(self.select_tool, "hits_at", None)
        if hits_at is None:
            return None
        try:
            return list(hits_at(e.mapPoint()))
        except Exception:
            return None

    @staticmethod
    def _any_selected(hits):
        return any(layer.selectedFeatureCount() and fid in layer.selectedFeatureIds() for layer, fid in hits)

    def _start_select_gesture(self, e):
        """The whole press-drag-release is the select tool's (click selects / clears, drag is a marquee)."""
        self._gesture = "select"
        self.select_tool.canvasPressEvent(e)

    def _select_click(self, e):
        """A click on the map as the select tool takes it: select what is there, or clear on empty map; Shift / Ctrl
        add or remove. Whatever anchor there was belonged to the old selection."""
        from ..selection import behavior_from_modifiers
        modifiers = e.modifiers() if hasattr(e, "modifiers") else Qt.KeyboardModifier.NoModifier
        self.select_tool.select_click(e.mapPoint(), behavior_from_modifiers(modifiers))
        self._anchor = None
        self._items = None
        self._refresh(quiet=True)
        self.refresh_page()

    def _is_click(self, start, end, e):
        """Press and release within a few pixels of each other (measured on the screen when the event says where)."""
        if self._press_px is not None and hasattr(e, "pixelPoint"):
            return (e.pixelPoint() - self._press_px).manhattanLength() < CLICK_PIXELS
        return math.hypot(end.x() - start.x(), end.y() - start.y()) < CLICK_PIXELS * self.canvas().mapUnitsPerPixel()

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):          # N = next overlapping feature, Delete; Esc leaves
            return
        if e.key() == Qt.Key.Key_Escape and (self._start is not None or self._anchor is not None
                                             or self._picking_anchor):
            self._clear_preview()
            self._anchor = None
            self._picking_anchor = False
            self._refresh(quiet=True)
            self.refresh_page()
            e.ignore()
        elif e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            from ..selection import delete_selected
            delete_selected(self.iface)
            e.ignore()

    def _select_under(self, e):
        if self.select_tool is not None:
            from ..selection import SELECT_SET
            self.select_tool.select_click(e.mapPoint(), SELECT_SET)
            self._items = None
            self._refresh(quiet=True)

    # ------------------------------------------------------------------ anchor
    def set_anchor(self, point):
        self._anchor = None if point is None else QgsPointXY(point)
        self._show_marker(self._bbox is not None)
        self.refresh_page()

    def pick_anchor(self):
        """Page button: the next click on the map places the anchor instead of acting on the features."""
        self.set_selecting(False)
        self._picking_anchor = True
        self.say("click the map to place the anchor (it snaps to vertices, edges and midpoints).")

    def add_anchor_group(self, box):
        box.addWidget(separator())
        box.addWidget(section_label("Anchor"))
        self._anchor_label = QLabel()
        self._anchor_label.setObjectName("KgaModifyHint")
        self._anchor_label.setWordWrap(True)
        box.addWidget(self._anchor_label)
        row = QHBoxLayout()
        row.addWidget(button("Set on the map", self.pick_anchor, "Then click the map where the anchor goes"))
        row.addWidget(button("Reset", lambda: self.set_anchor(None), "Back to the centre of the selection"))
        box.addLayout(row)

    def update_page(self):
        label = getattr(self, "_anchor_label", None)
        if label is None:
            return
        if self._anchor is not None:
            label.setText(f"At {self._anchor.x():.3f}, {self._anchor.y():.3f} (set by you).")
        else:
            label.setText("The centre of the selection.")

    # ------------------------------------------------------------------ applying
    def apply(self, fn, text, done):
        changed, layers, skipped = apply_to_selection(self.canvas(), fn, text)
        message = f"{done} {changed} feature(s) in {layers} layer(s)."
        if skipped:
            message += f" Not editable, left alone: {', '.join(skipped)}."
        self.say(f"{message} (Ctrl+Z or Discard Edits undoes it)")
        self._items = None
        self._refresh(quiet=True)
        self.refresh_page()
        return changed, layers, skipped

    # ------------------------------------------------------------------ per tool
    def on_press(self, point):
        pass

    def on_move(self, point):
        pass

    def on_release(self, start, end):
        pass


class MoveTool(TransformTool):
    KEY = "move"
    LABEL = "Move"
    SHOW_HINT = False           # no text above or below: the page is the banner, the list and the options
    SHOW_STATUS = False
    CURSOR = Qt.CursorShape.SizeAllCursor
    AUTO_SELECT = False         # no Select mode to start in: pressing on a feature selects it
    HINT = ("press on a feature and drag to move it (it is selected for you). Click empty map to clear the "
            "selection, drag there to select several; Shift or Ctrl adds or removes. Or type an offset below. "
            "With an anchor set, click anywhere to drop the anchor there.")

    # ------------------------------------------------------------------ select and drag in one gesture
    def canvasPressEvent(self, e):
        direct = (not self._selecting and not self._picking_anchor and self._anchor is None
                  and e.button() == Qt.MouseButton.LeftButton and self.select_tool is not None)
        hits = self._under_mouse(e) if direct else None
        if hits is None:
            return super().canvasPressEvent(e)                  # Select mode, an anchor, or no select tool: as before
        if not self._adding(e):
            if self._any_selected(hits):
                return super().canvasPressEvent(e)              # grabbing a selected feature: move it
            if hits:                                            # an unselected feature: select it, then move it
                self._select_click(e)
                return super().canvasPressEvent(e)
        self._start_select_gesture(e)                           # empty map, or Shift / Ctrl: the select tool's job

    def build_page(self, box):
        box.addWidget(section_label("Offset"))
        form = add_form(box)
        self.dx_spin, self.dy_spin = spin(0.0, decimals=4), spin(0.0, decimals=4)
        form.addRow("dX (map units)", self.dx_spin)
        form.addRow("dY (map units)", self.dy_spin)
        box.addWidget(button("Move by this offset", self.apply_offset))
        self.add_anchor_group(box)

    def _base(self, start):
        """Where the move is measured from: the anchor when one is set, else where the mouse went down."""
        return self._anchor if self._anchor is not None else start

    def apply_offset(self):
        dx, dy = self.dx_spin.value(), self.dy_spin.value()
        if (dx or dy) and self._refresh():
            self.move_by(dx, dy)

    def on_press(self, point):
        self._draw()

    def on_move(self, point):
        base = self._base(self._start)
        self._draw(offset=(point.x() - base.x(), point.y() - base.y()))

    def on_release(self, start, end):
        base = self._base(start)
        dx, dy = end.x() - base.x(), end.y() - base.y()
        if math.hypot(dx, dy) > 1e-12:
            self.move_by(dx, dy)

    def move_by(self, dx, dy):
        result = self.apply(lambda g: translated(g, dx, dy), "Move selected features", "Moved")
        if self._anchor is not None and result[0]:             # the anchor travels with the features
            self.set_anchor(QgsPointXY(self._anchor.x() + dx, self._anchor.y() + dy))
        return result


class RotateTool(TransformTool):
    KEY = "rotate"
    LABEL = "Rotate"
    SHOW_HINT = False           # no text above or below: the page is the banner, the list and the options
    SHOW_STATUS = False
    SHOW_ANCHOR = True
    CLICK_SELECTS = True
    AUTO_SELECT = False
    HINT = ("drag around the red anchor on the map to rotate the selected features, or type an angle below. "
            "Click a feature to select it, click empty map to clear; Shift or Ctrl adds or removes.")

    def build_page(self, box):
        box.addWidget(section_label("Angle"))
        form = add_form(box)
        self.angle_spin = spin(0.0, -360.0, 360.0, 3, 5.0, " °")
        form.addRow("Counter-clockwise", self.angle_spin)
        box.addWidget(button("Rotate by this angle", self.apply_angle))
        self.add_anchor_group(box)

    def apply_angle(self):
        angle = self.angle_spin.value()
        if angle and self._refresh():
            self.rotate_by(angle)

    def _angle(self, point):
        a = self.anchor()
        return math.degrees(math.atan2(point.y() - a.y(), point.x() - a.x()))

    def on_press(self, point):
        self._from = self._angle(point)
        self._draw()

    def on_move(self, point):
        a, turn = self.anchor(), self._angle(point) - self._from
        self._draw(lambda g: rotated(g, turn, a))

    def on_release(self, start, end):
        turn = self._angle(end) - self._angle(start)
        if abs(turn) > 1e-9:
            self.rotate_by(turn)

    def rotate_by(self, ccw_degrees):
        a = self.anchor()
        return self.apply(lambda g: rotated(g, ccw_degrees, a), "Rotate selected features", "Rotated")


class ScaleTool(TransformTool):
    KEY = "scale"
    LABEL = "Scale"
    SHOW_HINT = False           # no text above or below: the page is the banner, the list and the options
    SHOW_STATUS = False
    SHOW_ANCHOR = True
    CLICK_SELECTS = True
    AUTO_SELECT = False
    HINT = ("drag away from or towards the red anchor on the map to scale the selected features, or type a factor "
            "below. Click a feature to select it, click empty map to clear; Shift or Ctrl adds or removes.")

    def build_page(self, box):
        box.addWidget(section_label("Scale factor"))
        form = add_form(box)
        self.factor_spin = spin(1.0, 1e-6, 1e6, 4, 0.1)
        form.addRow("Factor (2 = twice as big)", self.factor_spin)
        box.addWidget(button("Scale by this factor", self.apply_factor))
        self.add_anchor_group(box)

    def apply_factor(self):
        factor = self.factor_spin.value()
        if abs(factor - 1.0) > 1e-9 and self._refresh():
            self.scale_by(factor)

    def _factor(self, start, end):
        a = self.anchor()
        d0 = math.hypot(start.x() - a.x(), start.y() - a.y())
        d1 = math.hypot(end.x() - a.x(), end.y() - a.y())
        if d0 < 1e-12 or d1 < 1e-12:
            return 1.0
        return max(d1 / d0, MIN_FACTOR)

    def on_press(self, point):
        self._draw()

    def on_move(self, point):
        a, f = self.anchor(), self._factor(self._start, point)
        self._draw(lambda g: scaled(g, f, a))

    def on_release(self, start, end):
        factor = self._factor(start, end)
        if abs(factor - 1.0) > 1e-9:
            self.scale_by(factor)

    def scale_by(self, factor):
        a = self.anchor()
        return self.apply(lambda g: scaled(g, factor, a), "Scale selected features", "Scaled")


class MoveToTool(TransformTool):
    """
    Laid out like the Move To pane of ArcGIS Pro: "Change the selection." and the selected features, a Method
    (Absolute / Delta / Direction/Distance), its Values, and a Move To button. Clicking the map fills the values in
    for the chosen method and previews the move; the button carries it out. Absolute puts the anchor (the centre of
    the selection unless set below) at X, Y; Delta moves by dX, dY; Direction/Distance by a distance along a
    direction (degrees clockwise from north).
    """

    KEY = "move_to"
    LABEL = "Move To"
    SHOW_ANCHOR = True
    HOVER = True                # the click itself is the gesture: it sets the target
    SHOW_HINT = False
    METHODS = ("Absolute", "Delta", "Direction/Distance")
    ABSOLUTE, DELTA, DIRECTION = range(3)
    HINT = "choose a method, type the values or click the map, then press Move To."

    _defaults_key = None        # (selection, anchor) the Absolute X, Y were last filled in for

    # ------------------------------------------------------------------ page
    def build_page(self, box):
        box.addWidget(section_label("Method"))
        self.method_combo = combo(self.METHODS)
        self.method_combo.currentIndexChanged.connect(self._method_changed)
        box.addWidget(self.method_combo)
        box.addWidget(section_label("Values"))
        self.value_stack = QStackedWidget()
        self.x_spin, self.y_spin = spin(0.0, decimals=3), spin(0.0, decimals=3)
        self.dx_spin, self.dy_spin = spin(0.0, decimals=3), spin(0.0, decimals=3)
        self.direction_spin = spin(0.0, 0.0, 360.0, 3, 5.0, " °")
        self.distance_spin = spin(0.0, 0.0, 1e12, 3)
        for page_rows in ((("X", self.x_spin), ("Y", self.y_spin)),
                          (("dX", self.dx_spin), ("dY", self.dy_spin)),
                          (("Direction", self.direction_spin), ("Distance", self.distance_spin))):
            holder = QWidget()
            form = QFormLayout(holder)
            form.setContentsMargins(0, 0, 0, 0)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            for label, box_ in page_rows:
                form.addRow(label, box_)
                box_.valueChanged.connect(self._preview)
            self.value_stack.addWidget(holder)
        box.addWidget(self.value_stack)
        self.add_anchor_group(box)

    def build_footer(self, box):
        row = QHBoxLayout()
        row.addStretch(1)
        self.move_button = button("Move To", self.apply_location, "Move the selected features to the location above")
        self.move_button.setEnabled(False)
        row.addWidget(self.move_button)
        box.addLayout(row)

    def _method_changed(self, index):
        self.value_stack.setCurrentIndex(index)
        self._preview()

    def _units(self):
        crs = self.canvas().mapSettings().destinationCrs()
        return QgsUnitTypes.toAbbreviatedString(crs.mapUnits()) if crs.isValid() else ""

    def update_page(self):
        super().update_page()                                    # the selected features and the anchor line
        if getattr(self, "method_combo", None) is None:
            return
        units = f" {self._units()}".rstrip()
        for box_ in (self.x_spin, self.y_spin, self.dx_spin, self.dy_spin, self.distance_spin):
            box_.setSuffix(units)
        if self._refresh(quiet=True):
            a = self.anchor()
            key = (self._sig, round(a.x(), 6), round(a.y(), 6))
            if key != self._defaults_key:                        # a new selection (or the features moved):
                self._defaults_key = key                         # start from where they are, nothing to do yet
                for box_, value in ((self.x_spin, a.x()), (self.y_spin, a.y()), (self.dx_spin, 0.0),
                                    (self.dy_spin, 0.0), (self.direction_spin, 0.0), (self.distance_spin, 0.0)):
                    box_.blockSignals(True)
                    box_.setValue(value)
                    box_.blockSignals(False)
        self._preview()

    # ------------------------------------------------------------------ the pending move
    def pending_offset(self):
        """(dx, dy) in map units the current method and values describe, measured from the anchor."""
        method = self.method_combo.currentIndex()
        if method == self.ABSOLUTE:
            a = self.anchor()
            return self.x_spin.value() - a.x(), self.y_spin.value() - a.y()
        if method == self.DELTA:
            return self.dx_spin.value(), self.dy_spin.value()
        azimuth = math.radians(self.direction_spin.value())
        distance = self.distance_spin.value()
        return distance * math.sin(azimuth), distance * math.cos(azimuth)

    def _preview(self, *_args):
        """Draw the selection where it would go, and enable Move To when there is somewhere to go."""
        if getattr(self, "method_combo", None) is None:
            return
        ready = self._refresh(quiet=True)
        dx, dy = self.pending_offset() if ready else (0.0, 0.0)
        moves = ready and math.hypot(dx, dy) > 1e-12
        if moves:
            self._draw(offset=(dx, dy))
        else:
            self._clear_preview()
        self.move_button.setEnabled(bool(moves))

    def set_target(self, point):
        """A click on the map: fill the values in for the chosen method."""
        if not self._refresh():
            return
        a = self.anchor()
        dx, dy = point.x() - a.x(), point.y() - a.y()
        method = self.method_combo.currentIndex()
        if method == self.ABSOLUTE:
            self.x_spin.setValue(point.x())
            self.y_spin.setValue(point.y())
        elif method == self.DELTA:
            self.dx_spin.setValue(dx)
            self.dy_spin.setValue(dy)
        else:
            self.direction_spin.setValue(math.degrees(math.atan2(dx, dy)) % 360.0)
            self.distance_spin.setValue(math.hypot(dx, dy))
        self._preview()
        self.say("location set - press Move To.")

    def on_move(self, point):
        pass                                                     # the preview follows the values, not the mouse

    def on_release(self, start, end):
        self.set_target(end)

    # ------------------------------------------------------------------ doing it
    def apply_location(self):
        """The Move To button."""
        if not self._refresh():
            return 0, 0, []
        dx, dy = self.pending_offset()
        if math.hypot(dx, dy) < 1e-12:
            self.say("nothing to move yet - type the values or click the map.")
            return 0, 0, []
        result = self.apply(lambda g: translated(g, dx, dy), "Move selected features to a location", "Moved")
        if self._anchor is not None and result[0]:               # a set anchor travels with the features
            self.set_anchor(QgsPointXY(self._anchor.x() + dx, self._anchor.y() + dy))
        self._clear_preview()
        return result

    def fill_from_anchor(self):
        """Absolute: start from where the anchor is now."""
        if self._refresh():
            a = self.anchor()
            self.method_combo.setCurrentIndex(self.ABSOLUTE)
            self.x_spin.setValue(a.x())
            self.y_spin.setValue(a.y())

    def move_anchor_to(self, point):
        a = self.anchor()
        dx, dy = point.x() - a.x(), point.y() - a.y()
        if math.hypot(dx, dy) < 1e-12:
            return 0, 0, []
        result = self.apply(lambda g: translated(g, dx, dy), "Move selected features to a location", "Moved")
        self._anchor = None if self._anchor is None else QgsPointXY(point)
        self._show_marker(self._bbox is not None)
        self.refresh_page()
        return result


TOOL_CLASSES = {cls.KEY: cls for cls in (MoveTool, RotateTool, ScaleTool, MoveToTool)}

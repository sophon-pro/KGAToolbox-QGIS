# -*- coding: utf-8 -*-
"""
The dialog every ArcGIS Pro *Modify Features* replica is built on.

In Pro these tools are pages of one pane, and they all behave the same way:
pick the tool, point at a feature, watch the result appear, click to commit.
`ModifyFeaturesDialog` is that behaviour, once.

It replaces an earlier docked-pane version of the first six, and the
reason is worth keeping. That version asked the user to select features with
the QGIS select tool and then press a button: it handed the canvas to something
it did not own, and learned what had happened through a signal it did not
control. When any link in that chain broke - and one did, on a perfectly
ordinary QGIS 3.44 - nothing said so. The pane sat there looking correct, the
count frozen at whatever it read when it opened, and the button wrote nothing.

`sequential_numbering_dialog` never had that problem, because it owns its map
tool. So does this. Press the action button and there is a cross-hair on the
canvas; hover a feature and the result is drawn before it is written; click and
it is written. There is nothing in between to go wrong.

A subclass sets four strings, says which geometry types it accepts, builds its
own parameters into the form it is handed, and answers two questions: what the
result looks like, and what committing it does.

    class BufferDialog(ModifyFeaturesDialog):
        TITLE = 'Buffer'
        ACTION_LABEL = 'Buffer'
        def build_parameters(self, form): ...
        def result_for(self, feature): ...      # geometries, in layer CRS
        def apply_to(self, features): ...       # -> a line for the status label

Tools that need more than one feature at a time - Merge - set `MIN_FEATURES`
and get click-to-collect: each click adds a feature to the set in hand, Enter
commits it, Esc drops it. A tool that collects but will settle for one feature
- Construct Polygon, where a single closed line is already a boundary - says so
with `COLLECTS = True`. Everything else commits on the click.

The preview is drawn with rubber bands, and a rubber band belongs to the canvas
scene rather than to the tool or to any layer. Nothing takes one down on its
own: not switching the layer off, not removing it, not closing the dialog. So
everything that ends a preview is wired up by hand - the pointer leaving the
map, the canvas being handed a new list of layers, the source combo moving, and
every way out of the dialog - and `sweep_stale_bands` picks up after anything
that got past all of them.

What a click picks matters for the same reason. A tool that writes into the
layer it reads puts its own result under the cursor that made it, and a feature
added to a layer being edited is numbered downwards from -2 - so taking the
hits in id order handed back the newest first, and Buffer spent a click
buffering the buffer from the click before. Saved features come first now
(`pick_order`), and a tool whose result covers its own source says
`SKIPS_OWN_OUTPUT` and is not offered its own work back at all.
"""

import traceback

from qgis.PyQt.QtCore import QEvent, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand

from ..core.modify_features import (
    AREA_UNITS,
    DISTANCE_UNITS,
    geometry_type,
    layer_filter,
    nearest_segment,
)
from .modify_base import (
    LOG_TAG,
    PREVIEW_COLOUR,
    PREVIEW_FILL,
    canvas_to_layer,
    default_unit_index,
    features_touching,
    is_deleted,
    layer_is_shown,
    layer_to_canvas,
    object_address,
    offer_to_edit,
    workspace_for,
)

#: The feature under the cursor, while it is under the cursor.
HOVER_COLOUR = QColor(255, 160, 0)

#: Features collected so far, for the tools that need more than one.
HELD_COLOUR = QColor(255, 90, 0)

#: The edge or the line an angle is being taken off, while it is being taken.
ANGLE_COLOUR = QColor(230, 0, 130)

#: Stamped on every band these tools put on the canvas, so a later run can
#: recognise one of ours among everything else the scene holds - other plugins'
#: bands, the measure tool's, the snapping markers - and take only those back.
BAND_TAG_KEY = 0
BAND_TAG = 'kga-modify-features-band'


def drop_band(canvas, band):
    """Take one rubber band off the canvas for good.

    `reset()` empties a band but leaves the item in the scene, and a
    `QgsRubberBand` belongs to that scene rather than to whatever made it - so
    a band that is only reset outlives the dialog that drew it, invisible but
    still there, and the next run draws a second one beside it.
    """
    if band is None:
        return
    try:
        # `reset()` and not `reset(band.geometryType())`: a QgsRubberBand has
        # no `geometryType` at all, so the call it looks like raised
        # AttributeError, took the `removeItem` beside it down with it, and
        # left every preview this tool ever drew in the scene. That is why the
        # cyan buffers stayed on the map after the layer they came from was
        # switched off, removed, and added back: they were not the layer's to
        # take away. The default is a line band, and the type does not matter
        # to an item that is about to leave the scene.
        band.reset()
    except Exception:                       # pragma: no cover - already gone
        pass
    try:
        scene = canvas.scene()
        if scene is not None:
            scene.removeItem(band)
    except Exception:                       # pragma: no cover
        pass


def sweep_stale_bands(canvas, keep=()):
    """Drop bands an earlier dialog left behind; `keep` is what is still in use.

    `cleanup` is the belt and this is the braces. A plugin reload, a crash in a
    slot, or anything else that kills a dialog without running its clean-up
    would otherwise strand a cyan preview on the canvas that belongs to no
    layer, so switching layers off does not clear it and nothing short of
    restarting QGIS takes it away.
    """
    spared = {object_address(band) for band in keep}
    try:
        scene = canvas.scene()
        items = list(scene.items()) if scene is not None else []
    except Exception:                       # pragma: no cover
        return
    for item in items:
        if object_address(item) in spared:
            continue
        try:
            if item.data(BAND_TAG_KEY) == BAND_TAG:
                scene.removeItem(item)
        except Exception:                   # pragma: no cover
            continue


#: Every tool that still has bands on a canvas. A tool adds itself when it is
#: built and takes itself out when it is disposed of, so the sweep below can
#: tell a band that is still in use from one that was left behind.
_LIVE_TOOLS = []


def live_bands():
    """Every band a tool still in use has on the canvas.

    So that closing one tool does not sweep away the preview another one is in
    the middle of drawing - Buffer and Copy Parallel can both be open.
    """
    bands = []
    for tool in list(_LIVE_TOOLS):
        if is_deleted(tool):
            try:
                _LIVE_TOOLS.remove(tool)
            except ValueError:              # pragma: no cover
                pass
            continue
        try:
            bands.extend(tool.bands())
        except Exception:                   # pragma: no cover
            continue
    return bands


class InteractiveMapTool(QgsMapTool):
    """Hover to see the result, click to write it, drag a path to do several.

    The same two gestures as `SequentialNumberingMapTool`, because they are the
    two gestures this plugin's interactive tools have: a click for one feature,
    a dragged path for a run of them. The hover is the addition - most of these
    tools take a distance or a count, and a number is worth seeing on the map
    before it is committed to.
    """

    message = pyqtSignal(str)

    DRAG_THRESHOLD_PX = 4
    #: How close a click has to be, in pixels. A line one pixel wide is not
    #: something anyone can hit dead on.
    PICK_RADIUS_PX = 6

    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller        # the dialog, asked for the settings
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._path_band = self._band(QColor(0, 0, 0), 2, 'Line')
        self._hover_band = self._band(HOVER_COLOUR, 3, 'Line')
        self._held_band = self._band(HELD_COLOUR, 3, 'Line')
        self._preview_bands = []

        self._points = []
        self._press_pos = None
        self._dragging = False
        self._hovered = None                # feature id the preview is for

        self._watch_viewport(True)
        _LIVE_TOOLS.append(self)

    def _band(self, colour, width, kind):
        band = QgsRubberBand(self.canvas, geometry_type(kind))
        band.setColor(colour)
        band.setFillColor(QColor(colour.red(), colour.green(), colour.blue(), 40))
        band.setWidth(width)
        band.setData(BAND_TAG_KEY, BAND_TAG)
        try:
            band.setLineStyle(Qt.PenStyle.DashLine)
        except AttributeError:              # pragma: no cover - older Qt
            pass
        return band

    def bands(self):
        """Every band this tool has on the canvas, live ones included."""
        candidates = [self._path_band, self._hover_band, self._held_band]
        candidates.extend(self._preview_bands)
        return [band for band in candidates if band is not None]

    # ----------------------------------------------------------- viewport --

    def _watch_viewport(self, watch):
        """Watch the map viewport so the preview can be dropped on the way out.

        `canvasMoveEvent` is the only thing that ever cleared a hover preview,
        and it stops arriving the moment the pointer leaves the map - which is
        exactly what the pointer does on its way to the layer tree to switch
        the layer off. The cyan copies were left painted over a canvas that no
        longer drew the line they came from, and because a rubber band belongs
        to no layer, unticking or removing that layer did not take them with
        it.
        """
        try:
            viewport = self.canvas.viewport()
        except Exception:                   # pragma: no cover - no canvas
            return
        if viewport is None:                # pragma: no cover
            return
        try:
            if watch:
                viewport.installEventFilter(self)
            else:
                viewport.removeEventFilter(self)
        except Exception:                   # pragma: no cover
            pass

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Leave:
            self.clear_hover()
        return False

    # ------------------------------------------------------------- events --

    def canvasPressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            self.reset()
            return
        self._press_pos = event.pos()
        self._dragging = False
        self._points = [self.toMapCoordinates(event.pos())]

    def canvasMoveEvent(self, event):
        if self._press_pos is None:
            self.hover(event.pos())
            return
        if not self._dragging:
            delta = event.pos() - self._press_pos
            if max(abs(delta.x()), abs(delta.y())) < self.DRAG_THRESHOLD_PX:
                return
            self._dragging = True
            self.clear_hover()
        point = self.toMapCoordinates(event.pos())
        self._points.append(point)
        self._path_band.addPoint(point, True)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        dragging = self._dragging
        points = list(self._points)
        position = event.pos()
        self.reset()

        if dragging and len(points) > 1:
            search = QgsGeometry.fromPolylineXY(
                [QgsPointXY(point) for point in points]).buffer(
                    self.tolerance(), 8)
        else:
            search = self.click_geometry(position)
        self.controller.act_on(search, dragged=dragging)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.reset()
            self.controller.drop_held()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.controller.commit_held()

    def deactivate(self):
        self.reset()
        super().deactivate()

    # -------------------------------------------------------------- hover --

    def hover(self, position):
        """Draw what clicking here would write, before it is written."""
        if self._hover_band is None:        # disposed of; nothing to draw on
            return
        try:
            found = self.controller.features_under(self.click_geometry(position))
        except Exception:                   # pragma: no cover - defensive
            found = []
        if not found:
            self.clear_hover()
            return
        feature = found[0]
        if feature.id() == self._hovered:
            return                          # already showing this one
        self._hovered = feature.id()

        self._hover_band.reset(geometry_type('Line'))
        outline = self.controller.to_canvas(feature.geometry())
        if outline is not None:
            self._hover_band.setToGeometry(outline, None)

        self.clear_previews()
        for geometry in self.controller.preview_under(feature):
            band = QgsRubberBand(self.canvas, geometry.type())
            band.setColor(PREVIEW_COLOUR)
            band.setFillColor(PREVIEW_FILL)
            band.setWidth(2)
            band.setData(BAND_TAG_KEY, BAND_TAG)
            try:
                band.setLineStyle(Qt.PenStyle.DashLine)
            except AttributeError:          # pragma: no cover
                pass
            band.setToGeometry(geometry, None)
            self._preview_bands.append(band)

    def clear_hover(self):
        self._hovered = None
        if self._hover_band is not None:
            self._hover_band.reset(geometry_type('Line'))
        self.clear_previews()

    def clear_previews(self):
        bands, self._preview_bands = self._preview_bands, []
        for band in bands:
            drop_band(self.canvas, band)

    # --------------------------------------------------------------- held --

    def show_held(self, geometries):
        """Outline the features collected so far, for the tools that collect."""
        if self._held_band is None:
            return
        self._held_band.reset(geometry_type('Line'))
        for geometry in geometries:
            self._held_band.addGeometry(geometry, None)

    # ------------------------------------------------------------- shapes --

    def tolerance(self):
        return self.canvas.mapUnitsPerPixel() * self.PICK_RADIUS_PX

    def click_geometry(self, position):
        """A click, grown to something a one-pixel line can be hit with."""
        point = self.toMapCoordinates(position)
        return QgsGeometry.fromPointXY(QgsPointXY(point)).buffer(
            self.tolerance(), 8)

    def reset(self):
        if self._path_band is not None:
            self._path_band.reset(geometry_type('Line'))
        self.clear_hover()
        self._points = []
        self._press_pos = None
        self._dragging = False

    def dispose(self):
        """Hand the canvas back everything this tool drew on it.

        Resetting a band is not enough: the scene keeps the item. The dialog
        calls this on the way out so nothing of the tool's is left over the
        map once it is gone.
        """
        self._watch_viewport(False)
        try:
            _LIVE_TOOLS.remove(self)
        except ValueError:                  # pragma: no cover - disposed twice
            pass
        self.clear_previews()
        for name in ('_path_band', '_hover_band', '_held_band'):
            band = getattr(self, name, None)
            setattr(self, name, None)
            drop_band(self.canvas, band)
        self._hovered = None
        self._points = []
        self._press_pos = None
        self._dragging = False


#: The two ways of taking a direction off the map, for `AngleMapTool`.
ANGLE_EDGE, ANGLE_DRAW = range(2)


class AngleMapTool(QgsMapTool):
    """Take a direction off the map: click an edge, or draw a line yourself.

    ArcGIS Pro puts two small buttons beside the division angle in its Divide
    pane, and this is what is behind both of them. Typing an angle is the
    third-best way of saying which way a parcel should be cut; the two better
    ones are "parallel to that boundary" and "along this line", and both come
    down to two points on the map.

    In `ANGLE_EDGE` the cursor finds the nearest segment of anything drawn -
    the parcel's own boundary, the road it fronts, the neighbour it abuts - and
    clicking takes that segment's direction. In `ANGLE_DRAW` two clicks give
    the line, with the project's snapping applied, so it can be drawn exactly
    between two corners.

    Nothing here knows what an angle *is*: it hands back the two points, in
    canvas CRS, and leaves the arithmetic to the dialog - which is the only one
    that knows the CRS the cut is actually made in. The controller answers:

        angle_layers()             -> layers whose edges may be clicked
        angle_hovered(start, end)  -> the pair under the cursor, or (None, None)
        angle_chosen(start, end)   -> that is the one
        angle_cancelled()          -> nothing was picked; put the canvas back
    """

    message = pyqtSignal(str)

    #: How close a click has to land. Wider than the feature tools' radius
    #: because it is aimed at a one-pixel boundary rather than at an area.
    PICK_RADIUS_PX = 8

    def __init__(self, canvas, controller, mode=ANGLE_EDGE):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller
        self.mode = mode
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._band = QgsRubberBand(canvas, geometry_type('Line'))
        self._band.setColor(ANGLE_COLOUR)
        self._band.setWidth(3)
        self._band.setData(BAND_TAG_KEY, BAND_TAG)

        self._anchor = None                 # draw mode: the first click
        _LIVE_TOOLS.append(self)

    def bands(self):
        return [] if self._band is None else [self._band]

    # ------------------------------------------------------------- events --

    def canvasMoveEvent(self, event):
        if self.mode == ANGLE_DRAW:
            if self._anchor is not None:
                self._show(self._anchor, self._snapped(event.pos()))
            return
        found = self.edge_under(event.pos())
        if found is None:
            self._clear()
        else:
            self._show(*found)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            # Right-click is "never mind" everywhere else in QGIS digitizing.
            self.controller.angle_cancelled()
            return

        if self.mode == ANGLE_DRAW:
            point = self._snapped(event.pos())
            if self._anchor is None:
                self._anchor = point
                self.message.emit('Now click the other end of the line. Esc '
                                  'takes the first point back.')
                return
            if self._anchor.distance(point) <= 0.0:
                self.message.emit('That is the point you started at. Click '
                                  'somewhere along the direction you want.')
                return
            self._show(self._anchor, point)
            self.controller.angle_chosen(self._anchor, point)
            return

        found = self.edge_under(event.pos())
        if found is None:
            self.message.emit('No edge under there. Hover a boundary until it '
                              'lights up, then click it.')
            return
        self.controller.angle_chosen(*found)

    def keyPressEvent(self, event):
        if event.key() != Qt.Key.Key_Escape:
            return
        if self.mode == ANGLE_DRAW and self._anchor is not None:
            self._anchor = None             # one Esc drops a misplaced start
            self._clear()
            self.message.emit('Start again: click one end of the line.')
            return
        self.controller.angle_cancelled()

    def deactivate(self):
        self._clear()
        super().deactivate()

    # -------------------------------------------------------------- edges --

    def tolerance(self):
        return self.canvas.mapUnitsPerPixel() * self.PICK_RADIUS_PX

    def edge_under(self, position):
        """The segment nearest `position`, as two canvas-CRS points, or None.

        Every candidate is brought back into canvas CRS before the distances
        are compared, because the layers offered are not all in one CRS and a
        metre in one of them is not a metre in the next.
        """
        point = QgsPointXY(self.toMapCoordinates(position))
        cursor = QgsGeometry.fromPointXY(point)
        search = cursor.buffer(self.tolerance(), 8)

        try:
            layers = list(self.controller.angle_layers())
        except Exception:                   # pragma: no cover - defensive
            layers = []

        best = None
        best_distance = None
        for layer in layers:
            try:
                found = features_touching(self.canvas, layer, search)
            except ValueError:              # out of the layer CRS domain
                continue
            if not found:
                continue

            forward = canvas_to_layer(self.canvas, layer)
            back = layer_to_canvas(self.canvas, layer)
            local = QgsPointXY(point)
            if forward is not None:
                try:
                    local = forward.transform(local)
                except Exception:           # pragma: no cover - out of domain
                    continue

            for feature in found:
                segment = nearest_segment(feature.geometry(), local)
                if segment is None:
                    continue
                ends = self._to_canvas(segment, back)
                if ends is None:
                    continue
                distance = QgsGeometry.fromPolylineXY(ends).distance(cursor)
                if best_distance is None or distance < best_distance:
                    best, best_distance = ends, distance
        return best

    @staticmethod
    def _to_canvas(segment, transform):
        ends = []
        for end in segment:
            if transform is not None:
                try:
                    end = transform.transform(end)
                except Exception:           # pragma: no cover - out of domain
                    return None
            ends.append(QgsPointXY(end))
        return ends

    def _snapped(self, position):
        """The cursor, pulled onto a vertex or an edge if the project snaps.

        Drawing the angle by eye is not what anybody wants when the direction
        is meant to run corner to corner, and the project already says what to
        snap to - so that is used rather than a rule of this tool's own.
        """
        try:
            match = self.canvas.snappingUtils().snapToMap(position)
            if match is not None and match.isValid():
                return QgsPointXY(match.point())
        except Exception:                   # pragma: no cover - no snapping
            pass
        return QgsPointXY(self.toMapCoordinates(position))

    # --------------------------------------------------------------- band --

    def _show(self, start, end):
        if self._band is None:              # disposed of; nothing to draw on
            return
        self._band.reset(geometry_type('Line'))
        self._band.addPoint(start, False)
        self._band.addPoint(end, True)
        self.controller.angle_hovered(start, end)

    def _clear(self):
        if self._band is not None:
            self._band.reset(geometry_type('Line'))
        self.controller.angle_hovered(None, None)

    def dispose(self):
        """Hand the canvas back the band, as `InteractiveMapTool.dispose` does."""
        try:
            _LIVE_TOOLS.remove(self)
        except ValueError:                  # pragma: no cover - disposed twice
            pass
        band, self._band = self._band, None
        drop_band(self.canvas, band)
        self._anchor = None


class ModifyFeaturesDialog(QDialog):
    """Shared scaffolding for the Modify Features tools."""

    #: Window title and the label on the action button.
    TITLE = 'Modify Features'
    ACTION_LABEL = 'Apply'
    #: One line under the title saying what to do, the way a Pro pane does.
    HINT = ''

    #: Which layers the source combo offers, and which geometry types the tool
    #: can actually work on. Empty ACCEPTS means "whatever the filter let in".
    LAYER_FILTER = layer_filter('VectorLayer')
    ACCEPTS = ()

    #: How many features the tool needs at once. Above one it collects: each
    #: click adds to the set in hand and Enter commits it.
    MIN_FEATURES = 1

    #: Whether a click adds to a set in hand rather than running there and
    #: then. `None` means "whenever more than one feature is needed", which is
    #: what Merge wants. Construct Polygon sets it True with a MIN_FEATURES of
    #: one: a single closed line is a boundary on its own, but the courses of
    #: a parcel usually arrive several at a time and have to be gathered
    #: before anything can be built from them.
    COLLECTS = None

    #: Set False for the tools that read the source layer but write somewhere
    #: else, so the dialog does not demand an edit session it will not use.
    EDITS_SOURCE = True

    #: Set True for a tool whose result covers the feature it came from, so
    #: that hovering the result would work on it again. Buffer is the one:
    #: every buffer contains its own source, so reading its own output back
    #: made each click a ring wider than the last. Divide and Merge write into
    #: the source layer too, but a piece of a divided polygon is an ordinary
    #: feature somebody may well want to divide again, so they leave this off.
    SKIPS_OWN_OUTPUT = False

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setWindowTitle(self.TITLE)
        self.setMinimumWidth(380)

        self._tool = None
        self._previous_tool = None
        self._held = []                     # features collected, when collecting
        self._written = 0                   # results since the last reset
        self._torn_down = False
        self._own = {}                      # {layer id: ids this tool wrote}
        self._watched = []                  # layers whose commit we listen for

        # Bands a dialog that died without its clean-up left over the map -
        # a plugin reload, a crash in a slot - belong to nobody and nothing
        # short of this takes them off. Opening the tool again is when the
        # user is looking at them and is as good a moment as any to clear up.
        sweep_stale_bands(self.canvas, live_bands())

        self._build_ui()
        self._connect()
        self._preselect_active_layer()
        self.on_layer_changed(self.current_layer())
        self.reset_units()

    # ------------------------------------------------------------------ ui --

    def _build_ui(self):
        outer = QVBoxLayout(self)

        hint = QLabel(self.HINT or self.default_hint())
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #666;')
        outer.addWidget(hint)
        self.hint_label = hint

        top = QFormLayout()
        top.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(self.LAYER_FILTER)
        top.addRow('Layer', self.layer_combo)
        outer.addLayout(top)

        body = QWidget()
        self.parameters = QFormLayout(body)
        self.parameters.setContentsMargins(0, 0, 0, 0)
        self.parameters.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.build_parameters(self.parameters)
        body.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Expanding)
        outer.addWidget(body, 1)

        buttons = QHBoxLayout()
        self.activate_button = QPushButton(self.ACTION_LABEL)
        # Checkable, because it puts a tool in hand rather than doing something
        # once - and because a button that stays down is the only thing on
        # screen saying the map is in this tool's mode.
        self.activate_button.setCheckable(True)
        self.activate_button.setToolTip(self.button_tip())
        buttons.addWidget(self.activate_button)

        self.reset_button = QPushButton('Reset')
        self.reset_button.setToolTip(
            'Set the running count back to zero.' + (
                ' What has been written so far stops being off limits, so a '
                'result can be worked on in turn.'
                if self.SKIPS_OWN_OUTPUT else ''))
        buttons.addWidget(self.reset_button)

        self.close_button = QPushButton('Close')
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('color: #666;')
        outer.addWidget(self.status_label)

    def default_hint(self):
        if self.collects():
            return ('Press {0}, then click the features to {1} - Enter runs it, '
                    'Esc starts over. Dragging a path across them does it in '
                    'one go.'.format(self.ACTION_LABEL,
                                     self.ACTION_LABEL.lower()))
        return ('Press {0}, then hover a feature on the map to see the result '
                'and click to write it. Drag a path across several to do them '
                'all.'.format(self.ACTION_LABEL))

    def button_tip(self):
        if self.collects():
            return ('Click the features on the map, then press Enter. Esc '
                    'clears what is in hand.')
        return ('Click a feature on the map, or drag a path across several.')

    def _connect(self):
        self.layer_combo.layerChanged.connect(self._layer_changed)
        self.activate_button.toggled.connect(self._toggle_tool)
        self.reset_button.clicked.connect(self._reset_counter)
        self.close_button.clicked.connect(self.close)
        self.canvas.mapToolSet.connect(self._map_tool_set)
        self.iface.currentLayerChanged.connect(self._active_layer_changed)
        # Unticking a layer and removing one both end up here, because either
        # way the layer tree hands the canvas a new list of layers to draw.
        self.canvas.layersChanged.connect(self._map_changed)
        QgsProject.instance().layersWillBeRemoved.connect(self._map_changed)

    # ------------------------------------------- helpers for subclasses --

    def add_template_combo(self, label, filters, tip=''):
        """A second layer combo for "where the result goes".

        It follows the source layer until the user picks something themselves.
        Not "only when it is empty": a `QgsMapLayerComboBox` starts on the first
        layer its filter lets through, so it was never empty, and the results
        went to whichever layer sat at the top of the tree.
        """
        self._template_pinned = False
        self._setting_template = False
        combo = QgsMapLayerComboBox()
        combo.setFilters(filters)
        if tip:
            combo.setToolTip(tip)
        self.parameters.addRow(label, combo)
        combo.layerChanged.connect(self._template_changed)
        self.template_combo = combo
        return combo

    def _template_changed(self, _layer=None):
        if not getattr(self, '_setting_template', False):
            self._template_pinned = True

    def follow_source(self, layer):
        """Point the template at `layer`, unless the user has pinned it."""
        combo = getattr(self, 'template_combo', None)
        if combo is None or layer is None or self._template_pinned:
            return
        if combo.currentLayer() is not layer:
            self._setting_template = True
            try:
                combo.setLayer(layer)
            finally:
                self._setting_template = False

    def add_distance_row(self, label, value=10.0, minimum=0.0, maximum=1e9,
                         decimals=3, units=DISTANCE_UNITS):
        """A distance spin box with the unit combo beside it, as Pro has.

        Returns `(spin, unit_combo)`; the unit combo's data is the name
        `Workspace` wants, or None for map units.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSingleStep(1.0)
        row.addWidget(spin, 1)

        combo = QComboBox()
        for text, name in units:
            combo.addItem(text, name)
        row.addWidget(combo)

        holder = QWidget()
        holder.setLayout(row)
        self.parameters.addRow(label, holder)
        self._unit_combos = getattr(self, '_unit_combos', [])
        self._unit_combos.append((combo, units))
        return spin, combo

    def add_area_row(self, label, value=1.0, minimum=0.0, maximum=1e12,
                     decimals=4):
        return self.add_distance_row(label, value, minimum, maximum, decimals,
                                     units=AREA_UNITS)

    def add_unit_combo(self, label, choices, default_name=None):
        """A combo over `(text, data)` pairs."""
        combo = QComboBox()
        for text, name in choices:
            combo.addItem(text, name)
        if default_name is not None:
            index = combo.findData(default_name)
            if index >= 0:
                combo.setCurrentIndex(index)
        self.parameters.addRow(label, combo)
        return combo

    def add_choice(self, label, labels, current=0):
        """A combo over plain labels whose index is the value."""
        combo = QComboBox()
        for text in labels:
            combo.addItem(text)
        combo.setCurrentIndex(current)
        self.parameters.addRow(label, combo)
        return combo

    def set_row_visible(self, widget, visible):
        """Show or hide a form row, its label with it.

        `QFormLayout.setRowVisible` only arrived in Qt 6.4 and the plugin has to
        run on Qt 5 builds, so the label is looked up and hidden by hand.
        """
        widget.setVisible(visible)
        label = self.parameters.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def reset_units(self):
        """Meters for a layer in degrees, map units for anything projected.

        A distance typed against a layer stored in degrees is almost never
        meant to be degrees, and starting on "Map units" would put the first
        result clean off the map.
        """
        layer = self.current_layer()
        for combo, _units in getattr(self, '_unit_combos', []):
            combo.setCurrentIndex(default_unit_index(combo, layer))

    # --------------------------------------------------------------- state --

    def collects(self):
        """Whether clicks gather features for Enter rather than running one by one."""
        return self.MIN_FEATURES > 1 if self.COLLECTS is None else self.COLLECTS

    def current_layer(self):
        layer = self.layer_combo.currentLayer()
        if layer is None or is_deleted(layer):
            return None
        return layer if isinstance(layer, QgsVectorLayer) else None

    def accepts(self, layer):
        """Whether this tool can work on `layer`'s geometry type."""
        if layer is None or not isinstance(layer, QgsVectorLayer):
            return False
        if not self.ACCEPTS:
            return True
        return any(layer.geometryType() == geometry_type(name)
                   for name in self.ACCEPTS)

    def target_layer(self):
        """Where results are written. Defaults to the template, then the source."""
        combo = getattr(self, 'template_combo', None)
        if combo is not None and combo.currentLayer() is not None:
            return combo.currentLayer()
        return self.current_layer()

    def edit_targets(self):
        """Every layer a run will write to, so all of them can be opened before
        any of them is touched. Half a Clip is worse than none of one."""
        if self.EDITS_SOURCE:
            layers = [self.current_layer()]
        else:
            layers = [self.target_layer()]
        return [layer for layer in layers if layer is not None]

    def _preselect_active_layer(self):
        active = self.iface.activeLayer()
        if self.accepts(active):
            self.layer_combo.setLayer(active)

    def _active_layer_changed(self, layer):
        """Follow the active layer, but never while the tool is in hand.

        These tools write into a second layer, so the user clicks that one in
        the tree all the time - to switch it on, or to start editing it. That
        used to drag the source combo onto it, and the next click on the map
        read the layer being written to.
        """
        if self.activate_button.isChecked() or not self.accepts(layer):
            return
        if layer is not self.current_layer():
            self.layer_combo.setLayer(layer)

    def _layer_changed(self, _layer=None):
        self.clear_preview()
        self.drop_held()
        self.on_layer_changed(self.current_layer())
        self.reset_units()

    def _map_changed(self, *_args):
        """The layers under the preview changed, so the preview is stale.

        A hover preview is a scene item and belongs to no layer: switching the
        source layer off, or removing it, took the feature off the map and left
        the cyan copies of it floating over an empty canvas.
        """
        self.clear_preview()

    def clear_preview(self):
        """Take the hover preview off the canvas, if there is one."""
        if self._tool is not None and not is_deleted(self._tool):
            self._tool.clear_hover()

    def _reset_counter(self):
        self._written = 0
        self.drop_held()
        self._unwatch_layers()              # results become ordinary features
        self.report('Count reset.')

    # ----------------------------------------------------------- geometry --

    def make_workspace(self, layer, unit_name):
        """The `Workspace` this dialog's numbers are measured in."""
        workspace = workspace_for(self.canvas, layer, unit_name)
        if workspace.fell_back:             # pragma: no cover - no zone found
            self.report(
                'No projected CRS could be found for "{}", so the number is '
                "being read in the layer's own units.".format(layer.name()))
        return workspace

    def to_canvas(self, geometry):
        """`geometry`, which is in the source layer CRS, in canvas CRS."""
        layer = self.current_layer()
        if layer is None or geometry is None or geometry.isNull():
            return None
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if not canvas_crs.isValid() or canvas_crs == layer.crs():
            return QgsGeometry(geometry)
        clone = QgsGeometry(geometry)
        try:
            clone.transform(QgsCoordinateTransform(
                layer.crs(), canvas_crs, QgsProject.instance()))
        except Exception:                   # pragma: no cover - out of domain
            return None
        return clone

    def features_under(self, search):
        """Source features the drawn `search` shape touches, canvas CRS in."""
        layer = self.current_layer()
        if layer is None:
            return []
        return features_touching(self.canvas, layer, search,
                                 with_attributes=True,
                                 skip=self._own_ids(layer))

    # ------------------------------------------- what this tool has written --

    def _own_ids(self, layer):
        """Ids this tool has written into `layer` and must not read back.

        Buffer writes into a polygon layer, and when the source is a polygon
        layer that is the same layer by default - so its own buffer lands under
        the cursor that made it. Hovering there buffered the buffer, and the
        click after that buffered *that*, one ring wider every time. Ordering
        saved features first stops it happening over the feature itself; this
        stops it happening over the ring, which is the part of the result the
        feature is not under.

        The record is per edit session: ids change when the layer is saved, and
        a saved buffer is an ordinary feature that may be buffered again.
        """
        if layer is None or not self.SKIPS_OWN_OUTPUT:
            return ()
        return self._own.get(layer.id(), ())

    def _added_ids(self, layer):
        """Ids of the features waiting unsaved in `layer`'s edit buffer."""
        try:
            buffer = layer.editBuffer()
            return set(buffer.addedFeatures().keys()) if buffer else set()
        except Exception:                   # pragma: no cover - not editing
            return set()

    def _remember_own(self, layer, ids):
        if not ids or not self.SKIPS_OWN_OUTPUT:
            return
        self._own.setdefault(layer.id(), set()).update(ids)
        if layer not in self._watched:
            # Saving renumbers everything in the edit buffer and rolling back
            # throws it away, so the ids held here mean nothing afterwards -
            # and negative ids start again from the top, so keeping them would
            # hide somebody else's feature.
            for signal in (layer.afterCommitChanges, layer.afterRollBack):
                try:
                    signal.connect(self._forget_own)
                except Exception:           # pragma: no cover
                    pass
            self._watched.append(layer)

    def _forget_own(self, *_args):
        self._own.clear()

    def _unwatch_layers(self):
        for layer in self._watched:
            if is_deleted(layer):
                continue
            for signal in (layer.afterCommitChanges, layer.afterRollBack):
                try:
                    signal.disconnect(self._forget_own)
                except Exception:
                    pass
        self._watched = []
        self._own.clear()

    def preview_under(self, feature):
        """What hovering `feature` should draw, in canvas CRS."""
        drawn = []
        try:
            geometries = self.result_for(feature)
        except Exception:                   # pragma: no cover - defensive
            self._log_failure('preview')
            return []
        for geometry in geometries:
            canvas_geometry = self.to_canvas(geometry)
            if canvas_geometry is not None and not canvas_geometry.isEmpty():
                drawn.append(canvas_geometry)
        return drawn

    # ---------------------------------------------------------------- run --

    def act_on(self, search, dragged=False):
        """A click or a drag landed. Collect or commit, as the tool wants."""
        layer = self.current_layer()
        if layer is None:
            self.warn('Choose a layer first.')
            return
        reason = self.blocker()
        if reason:
            self.warn(reason)
            return

        try:
            found = self.features_under(search)
        except ValueError as exc:           # pragma: no cover - out of domain
            self.warn(str(exc))
            return
        if not found:
            if self.collects() and not dragged:
                return                      # a stray click while collecting
            self.report('Nothing under there. Click a feature in "{}".'.format(
                layer.name()))
            return

        if self.collects() and not dragged:
            self.hold(found[:1])
            return
        self.run_on(found)

    def hold(self, features):
        """Add to the set in hand, or take back out what is already in it."""
        held = {feature.id(): feature for feature in self._held}
        for feature in features:
            if feature.id() in held:
                del held[feature.id()]      # clicking it again takes it back
            else:
                held[feature.id()] = feature
        self._held = [held[key] for key in sorted(held)]
        self._show_held()
        self.on_held_changed(self._held)

        count = len(self._held)
        if count < self.MIN_FEATURES:
            self.report('{} feature{} in hand. Click at least {} of them, then '
                        'press Enter.'.format(
                            count, '' if count == 1 else 's',
                            self.MIN_FEATURES))
        else:
            self.report('{} feature{} in hand. Press Enter to {}, Esc to start '
                        'over.'.format(count, '' if count == 1 else 's',
                                       self.ACTION_LABEL.lower()))

    def commit_held(self):
        if len(self._held) < self.MIN_FEATURES:
            if self._held:
                self.report('{} needs at least {} features; {} in hand.'.format(
                    self.ACTION_LABEL, self.MIN_FEATURES, len(self._held)))
            return
        features, self._held = self._held, []
        self._show_held()
        self.on_held_changed(self._held)
        self.run_on(features)

    def drop_held(self):
        if not self._held:
            return
        self._held = []
        self._show_held()
        self.on_held_changed(self._held)
        self.report('Nothing in hand.')

    def _show_held(self):
        if self._tool is None:
            return
        outlines = []
        for feature in self._held:
            geometry = self.to_canvas(feature.geometry())
            if geometry is not None:
                outlines.append(geometry)
        self._tool.show_held(outlines)

    def run_on(self, features):
        """Write the result for `features`, inside one undo step."""
        if len(features) < self.MIN_FEATURES:
            self.report('{} needs at least {} features.'.format(
                self.ACTION_LABEL, self.MIN_FEATURES))
            return
        for target in self.edit_targets():
            if not target.isEditable():
                started, message = offer_to_edit(self, self.TITLE, target)
                if not started:
                    self.warn(message)
                    return
        targets = self.edit_targets()
        before = {layer.id(): self._added_ids(layer) for layer in targets}
        try:
            message = self.apply_to(features)
        except Exception as exc:
            self.warn('Failed: {} (the details are in the Log Messages panel, '
                      'under "{}").'.format(exc, LOG_TAG))
            self._log_failure('apply')
            return
        finally:
            for layer in targets:
                self._remember_own(
                    layer,
                    self._added_ids(layer) - before.get(layer.id(), set()))
        self.canvas.refresh()
        if message:
            self.report('{} {} so far.'.format(message, self._written))

    def count(self, written):
        """Add `written` to the running total. Subclasses call this from apply."""
        self._written += written

    def _log_failure(self, what):
        """Put the traceback somewhere it can be read.

        The status line only has room for the message, and a bare message is
        not enough to fix anything by - so the stack goes to the Log Messages
        panel under the same tag as the rest of the plugin.
        """
        QgsMessageLog.logMessage(
            '{} - {} failed:\n{}'.format(self.TITLE, what,
                                         traceback.format_exc()),
            LOG_TAG, Qgis.MessageLevel.Critical)

    # --------------------------------------------------------------- tool --

    def _toggle_tool(self, checked):
        if not checked:
            self.drop_held()
            if self._tool is not None and self.canvas.mapTool() is self._tool:
                self.canvas.unsetMapTool(self._tool)
                if self._previous_tool is not None \
                        and not is_deleted(self._previous_tool):
                    self.canvas.setMapTool(self._previous_tool)
            self._previous_tool = None
            return

        layer = self.current_layer()
        if layer is None:
            self._refuse('Choose a layer first.')
            return
        reason = self.blocker()
        if reason:
            self._refuse(reason)
            return

        for target in self.edit_targets():
            started, message = offer_to_edit(self, self.TITLE, target)
            if not started:
                self._refuse(message)
                return

        # The rest of QGIS - the attribute table, Zoom to Selection, the other
        # editing tools - works off the active layer, so keep it honest about
        # which layer is being read.
        self.iface.setActiveLayer(layer)
        if self._tool is None:
            self._tool = InteractiveMapTool(self.canvas, self)
            self._tool.message.connect(self.report)
        self._previous_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self._tool)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

        self.on_activated()
        if not layer_is_shown(layer):
            self.report(
                '"{}" is switched off in the layer tree, so you will not see '
                'what you are clicking. Tick it first.'.format(layer.name()))
            return
        self.report(self.ready_message())

    def ready_message(self):
        layer = self.current_layer()
        target = self.target_layer()
        where = ('' if target is None or target is layer
                 else ' into "{}"'.format(target.name()))
        if self.collects():
            return ('Click the features in "{}" to {}{}, then press '
                    'Enter.'.format(layer.name(), self.ACTION_LABEL.lower(),
                                    where))
        return ('Hover a feature in "{}" to see the result, click to write '
                'it{}. Drag a path to do several.'.format(layer.name(), where))

    def _map_tool_set(self, new_tool, _old_tool=None):
        """Pop the button back up when something else takes the canvas."""
        if self._tool is not None and new_tool is not self._tool:
            self._uncheck()

    def _refuse(self, text):
        self.warn(text)
        self._uncheck()

    def _uncheck(self):
        self.activate_button.blockSignals(True)
        self.activate_button.setChecked(False)
        self.activate_button.blockSignals(False)

    # ------------------------------------------------------------ messages --

    def warn(self, text):
        self.status_label.setText(text)
        self.iface.messageBar().pushWarning(self.TITLE, text)

    def report(self, text):
        self.status_label.setText(text)

    # -------------------------------------------------------------- hooks --

    def build_parameters(self, form):
        """Add the tool's own rows to `form`. Subclasses override."""

    def on_layer_changed(self, layer):
        """React to a new source layer. Subclasses override."""

    def on_held_changed(self, features):
        """React to the set in hand changing. Subclasses override."""

    def on_activated(self):
        """The tool has just been put in hand. Subclasses override.

        Construct Polygon uses it to take the layer's existing selection into
        hand, so lines selected with QGIS's own select tool - or by an
        attribute query - are a starting point rather than something to click
        all over again.
        """

    def blocker(self):
        """Why the tool cannot run yet, or '' when it can. Subclasses override."""
        return ''

    def result_for(self, feature):
        """Geometries to draw for `feature`, in layer CRS. Subclasses override."""
        return []

    def apply_to(self, features):
        """Write the result. Return a line for the status label."""
        raise NotImplementedError

    # -------------------------------------------------------------- close --

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def done(self, result):
        # Escape and `reject()` land here and never raise a close event, so
        # hanging the clean-up off `closeEvent` alone left the map tool in hand
        # and the preview on the canvas for the rest of the session.
        self.cleanup()
        super().done(result)

    def cleanup(self):
        """Hand back everything this dialog put on the canvas. Idempotent."""
        if self._torn_down:
            return
        self._torn_down = True

        self._held = []
        self._unwatch_layers()
        tool, self._tool = self._tool, None
        if tool is not None and not is_deleted(tool):
            if self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)
            # `reset()` empties the bands; the scene still holds them.
            tool.dispose()
            tool.deleteLater()
        self._previous_tool = None

        for signal, slot in ((self.canvas.mapToolSet, self._map_tool_set),
                             (self.iface.currentLayerChanged,
                              self._active_layer_changed),
                             (self.canvas.layersChanged, self._map_changed),
                             (QgsProject.instance().layersWillBeRemoved,
                              self._map_changed)):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

        # Anything of ours a dialog that died without its clean-up left behind.
        sweep_stale_bands(self.canvas, live_bands())
        try:
            self.canvas.refresh()
        except Exception:                   # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
#  the lifecycle every dialog shares
#
#  A dialog is parented to the QGIS main window, not to the Processing run that
#  opened it, so `KgaToolsPlugin.unload` has to be able to take it down. Same
#  shape as `sequential_numbering_dialog`, which these all follow.
# --------------------------------------------------------------------------- #

#: {dialog class: the one open instance}, so each module's close helper and its
#: algorithm can find it without a global of its own.
_OPEN = {}


def show_dialog(iface, dialog_class):
    """Open one dialog of this class, reusing the one already up."""
    dialog = _OPEN.get(dialog_class)
    if dialog is not None and is_deleted(dialog):
        dialog = None
    # closeEvent drops the map tool and disconnects its signals, so a closed
    # dialog is spent: build a fresh one rather than re-showing it.
    if dialog is not None and not dialog.isVisible():
        dialog.deleteLater()
        dialog = None
    if dialog is None:
        dialog = dialog_class(iface)
        _OPEN[dialog_class] = dialog

    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def close_dialog(dialog_class):
    """Close the dialog of this class, if one is open. Safe when none is."""
    dialog = _OPEN.pop(dialog_class, None)
    if dialog is None or is_deleted(dialog):
        return
    dialog.close()
    dialog.deleteLater()

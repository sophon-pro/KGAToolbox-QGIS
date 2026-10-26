"""
Reshape tools of the Modify Features pane, and the commands of the Reshape and Divide groups.

Map tools (click on the map; each has a page in the pane):
    Reshape            draw a line over features to reshape them (right-click, Enter or the Finish button)
    Replace Geometry   with features selected, draw the new shape they should take
    Extend or Trim     hover a line: grey preview; click to extend it to / trim it back to the nearest feature
    Fillet             click two lines, they are joined by an arc of the radius on the page
    Line Intersection  click two lines, they are trimmed or extended to meet at their corner
    Change Line Length click a line near the end to move, type its new length on the page

Commands (their page has the options and a button that acts on the selection):
    Generalize         remove vertices within a tolerance
    Reverse Direction  turn the selected lines around
    Explode            break multi-part features into single parts
    Edit Vertices      QGIS's Vertex tool in all-layers mode
    Split              the KGA Editor's Split tool

Features are picked from the layers shown on the map; only editable layers are changed. Every change is one
undoable edit command per layer. The maths is in geometry_ops.py.
"""

import os
import time
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsFieldConstraints,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointLocator,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsVectorLayerUtils,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator, QgsVertexMarker
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QCursor, QIcon
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMenu,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
)

from ...core.compat import enum_value
from ..selection import _status, _transform, all_vector_layers, map_layers
from . import geometry_ops as G
from .page import (
    DoneStrip,
    PageMixin,
    SelectedTree,
    SelectModeMixin,
    add_form,
    button,
    check,
    hint_label,
    section_label,
    spin,
)
from .pick import commit, pick_features
from .transform import apply_to_selection, editable_selection
from .generalize import GeneralizePage
from .sketch_constraints import SketchConstraints
from .line_length import ChangeLineLengthPage
from .vertices import EditVerticesTool

MARKER_BOX = enum_value((Qgis, 'VertexMarkerType.Box'), (QgsVertexMarker, 'IconType.ICON_BOX'))  # QGIS >= 3.40 / 4
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
BLUE = QColor(0, 121, 193, 220)
ORANGE = QColor(255, 140, 0, 230)
SETTINGS = "kga_editor/modify_"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")


# ---------------------------------------------------------------------------------------- base
class PickTool(SelectModeMixin, PageMixin, QgsMapTool):
    """Click-driven tool: snapping indicator, a highlight, status messages, Esc to start over."""

    KEY = ""
    LABEL = ""
    HINT = ""
    CURSOR = Qt.CursorShape.CrossCursor
    SHOWS_SELECTION = False
    SELECTS = False             # tools that work on the selection switch this on (and AUTO_SELECT)
    AUTO_SELECT = False
    SELECT_ONE_SHOT = True      # select, then work: a plain selection made with the Select button ends Select mode

    def __init__(self, canvas, iface, select_tool=None):
        super().__init__(canvas)
        self.iface = iface
        self.select_tool = select_tool
        self.snap_indicator = QgsSnapIndicator(canvas)
        self.band = QgsRubberBand(canvas, LINE)
        self.band.setColor(BLUE)
        self.band.setWidth(3)
        self.setCursor(QCursor(self.CURSOR))
        self.reset()

    # ------------------------------------------------------------------ lifecycle
    def activate(self):
        super().activate()
        self.reset()
        self.start_select_mode()
        self.say(self.HINT, 10000)

    def deactivate(self):
        self.reset()
        self._cancel_selecting()
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        super().deactivate()

    def dispose(self):
        self.reset()
        self.canvas().scene().removeItem(self.band)
        self.snap_indicator.setMatch(QgsPointLocator.Match())

    def reset(self):
        """Forget what has been picked so far."""
        self.band.reset(LINE)
        self.refresh_page()

    def highlight(self, geom, color=BLUE):
        self.band.reset(LINE)
        self.band.setColor(color)
        if geom.type() == POLYGON:                                    # a polygon is shown by its outline
            geom = geom.convertToType(LINE, False)
        self.band.addGeometry(geom, QgsCoordinateReferenceSystem(), True)

    # ------------------------------------------------------------------ mouse
    def canvasPressEvent(self, e):
        self.forward_select("canvasPressEvent", e)

    def canvasMoveEvent(self, e):
        if self.forward_select("canvasMoveEvent", e):
            return
        point = QgsPointXY(e.snapPoint())             # must run BEFORE mapPointMatch()
        self.snap_indicator.setMatch(e.mapPointMatch())
        self.on_move(point)

    def canvasReleaseEvent(self, e):
        if self.forward_select("canvasReleaseEvent", e):
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.on_click(QgsPointXY(e.snapPoint()))
        elif e.button() == Qt.MouseButton.RightButton:
            self.on_right_click(e)

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):
            return
        if e.key() == Qt.Key.Key_Escape:
            self.reset()
            self.say(self.HINT)
            e.ignore()

    def on_move(self, point):
        pass

    def on_click(self, point):
        pass

    def on_right_click(self, e):
        self.reset()
        self.say(self.HINT)

    def start_over(self):
        self.reset()
        self.say(self.HINT)


def _line_layers_hit(canvas, point, skip=None):
    """Editable line features under a click, nearest first, leaving out `skip` = (layer id, fid)."""
    return [h for h in pick_features(canvas, point, (LINE,), editable_only=True)
            if skip is None or (h[0].id(), h[1]) != skip]


class ProPage:
    """What the Reshape and Replace Geometry pages share, laid out like ArcGIS Pro's: the completed strip, "Change
    the selection.", the selected features, and the tool tile that leaves Select mode."""

    BANNER_TIP = ("Click or drag on the map to select the features to change. "
                  "Shift adds, Ctrl removes, N picks the next overlapping feature.")
    done_strip = None
    tree = None
    tool_tile = None

    def build_selection_row(self, box):
        self.done_strip = DoneStrip()
        box.addWidget(self.done_strip)
        banner = QToolButton()
        banner.setObjectName("KgaModifyBanner")
        banner.setCheckable(True)
        banner.setIcon(QIcon(os.path.join(ICON_DIR, "select_change.svg")))
        banner.setIconSize(QSize(26, 26))
        banner.setText("Change the selection.")
        banner.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        banner.setToolTip(self.BANNER_TIP)
        banner.clicked.connect(lambda _c=False: self.set_selecting(not self._selecting))
        self._select_button = banner                   # the Select mode keeps its checked state in step
        banner.setChecked(self._selecting)
        self.banner = banner
        box.addWidget(banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)

    def draw_mode(self):
        """The Reshape tile: leave Select mode and draw."""
        if self._selecting:
            self.set_selecting(False)
        elif self.tool_tile is not None:
            self.tool_tile.setChecked(True)


# ---------------------------------------------------------------------------------------- reshape
def reshape_single_intersection(g, points):
    """
    A line crossed once by the sketch: the shorter side is dropped and the line continues along the sketch, on the
    sketch's side of the crossing that ends nearer the dropped part. None when the sketch does not cross exactly once.
    """
    if g.type() != LINE or g.isMultipart():
        return None
    sketch = QgsGeometry.fromPolylineXY(points)
    hit = g.intersection(sketch)
    if hit.isEmpty() or hit.type() != Qgis.GeometryType.Point:
        return None
    crossings = hit.asMultiPoint() if hit.isMultipart() else [hit.asPoint()]
    if len(crossings) != 1:
        return None
    p = crossings[0]
    here = QgsGeometry.fromPointXY(p)
    along, total = g.lineLocatePoint(here), g.length()
    curve, ends = g.constGet(), g.asPolyline()
    keep_start = along >= total / 2
    kept = QgsGeometry(curve.curveSubstring(0, along) if keep_start else curve.curveSubstring(along, total)).asPolyline()
    dropped_end = QgsGeometry.fromPointXY(ends[-1] if keep_start else ends[0])
    s_along = sketch.lineLocatePoint(here)
    sk = sketch.constGet()
    forward = QgsGeometry(sk.curveSubstring(s_along, sketch.length())).asPolyline()         # crossing -> sketch end
    backward = QgsGeometry(sk.curveSubstring(0, s_along)).asPolyline()[::-1]                # crossing -> sketch start
    far = [part for part in (forward, backward) if len(part) > 1]
    if not far:
        return None
    outward = min(far, key=lambda part: QgsGeometry.fromPointXY(part[-1]).distance(dropped_end))
    new = kept + outward[1:] if keep_start else outward[::-1][:-1] + kept
    out = QgsGeometry.fromPolylineXY([QgsPointXY(q) for q in new])
    return out if not out.isNull() and out.isGeosValid() else None


def _split_pieces(g, points):
    """The pieces a polygon falls into when the sketch cuts clean through it (the sketch is their new edge)."""
    cut = QgsGeometry(g)
    result, others, _tests = cut.splitGeometry(points, False)
    if result != Qgis.GeometryOperationResult.Success or not others:
        return []
    pieces = [cut] + [QgsGeometry(o) for o in others]
    return sorted(pieces, key=lambda piece: -piece.constGet().perimeter()) if g.type() == POLYGON else pieces


def reshape_options(canvas, points, allow_no_selection=True, single_intersection=False):
    """
    [(layer, fid, [candidate geometries in the map's CRS])] for reshaping the selected features with the line
    `points` (or, with no selection and `allow_no_selection`, every editable feature the line crosses). A polygon
    the sketch cuts in two or more has one candidate per piece, the longest-perimeter piece first (Pro's default);
    everything else has one. Nothing is changed.
    """
    canvas_crs = canvas.mapSettings().destinationCrs()
    line = QgsLineString([QgsPoint(p) for p in points])
    box = QgsGeometry(line.clone()).boundingBox()
    selected = editable_selection(canvas)
    if selected:
        targets = selected
    elif allow_no_selection:
        shown = map_layers(canvas)
        targets = [(layer, None) for layer in all_vector_layers()
                   if layer.isEditable() and layer.geometryType() in (LINE, POLYGON)
                   and (shown is None or layer.id() in shown)]
    else:
        targets = []
    options = []
    for layer, ids in targets:
        if layer.geometryType() not in (LINE, POLYGON):
            continue
        same = layer.crs() == canvas_crs
        to_map = None if same else _transform(layer.crs(), canvas_crs)
        request = QgsFeatureRequest().setNoAttributes()
        if ids is not None:
            request.setFilterFids(ids)
        else:
            request.setFilterRect(box if same else _transform(canvas_crs, layer.crs()).transformBoundingBox(box))
        for f in layer.getFeatures(request):
            g = QgsGeometry(f.geometry())
            if g.isEmpty():
                continue
            if to_map is not None:
                g.transform(to_map)
            candidates = _split_pieces(g, points) if layer.geometryType() == POLYGON else []
            if not candidates:
                done = QgsGeometry(g)
                if done.reshapeGeometry(line) == Qgis.GeometryOperationResult.Success:
                    candidates = [done]
                elif single_intersection and ids is not None and layer.geometryType() == LINE:
                    one = reshape_single_intersection(g, points)
                    candidates = [one] if one is not None else []
            if candidates:
                options.append((layer, f.id(), candidates))
    return options


def reshape_changes(canvas, points, allow_no_selection=True, single_intersection=False):
    """[(layer, fid, new geometry in the map's CRS)]: the default candidate of `reshape_options`."""
    return [(layer, fid, candidates[0])
            for layer, fid, candidates in reshape_options(canvas, points, allow_no_selection, single_intersection)]


def reshape_with(canvas, points, allow_no_selection=True, single_intersection=False, changes=None):
    """Reshape the selected features (or, with no selection and allowed, every editable feature the line crosses)."""
    if changes is None:
        changes = reshape_changes(canvas, points, allow_no_selection, single_intersection)
    failed = commit(canvas, changes, [], "Reshape features") if changes else []
    return len(changes), failed


class ReshapeTool(SketchConstraints, ProPage, PickTool):
    """Laid out like the Reshape pane of ArcGIS Pro: completed strip, "Change the selection.", the selected
    features, the Reshape tool, and at the bottom Show Preview and Allow reshaping without a selection."""

    KEY = "reshape"
    LABEL = "Reshape"
    HINT = "click points on the map to draw the new edge over the features, then Finish (or right-click or Enter)."
    SHOWS_SELECTION = False
    SHOW_HINT = False
    SHOW_STATUS = False
    SELECTS = AUTO_SELECT = True
    PREVIEW_MAX = 40            # features previewed while drawing

    def __init__(self, canvas, iface, select_tool=None):
        self.allow_no_selection = QgsSettings().value(SETTINGS + "reshape_no_selection", False, type=bool)
        self.show_preview = QgsSettings().value(SETTINGS + "reshape_preview", False, type=bool)
        self.single_intersection = QgsSettings().value(SETTINGS + "reshape_single", False, type=bool)
        self.choose_result = QgsSettings().value(SETTINGS + "reshape_choose", False, type=bool)
        self._preview_bands = {}
        self._choosing = None               # {"fixed": [...], "pending": [(layer, fid, candidates)], "picked": [...]}
        self._choice_bands = []
        self._markers = None
        self.banner = None
        self.done_strip = None
        self.tree = None
        self.tool_tile = None
        super().__init__(canvas, iface, select_tool)

    def start_select_mode(self):
        """Select first when there is nothing to reshape (unless reshaping without a selection is allowed)."""
        self.set_selecting(not self.allow_no_selection and not self._has_selection(), auto=True)

    def reset(self):
        self.points = []
        self.clear_constraint()
        self._clear_preview()
        self._end_choosing()
        self._place_markers([])
        super().reset()

    def dispose(self):
        self.dispose_constraint_bands()
        self._clear_preview()
        self._end_choosing()
        for marker in self._markers or ():
            with suppress(Exception):
                self.canvas().scene().removeItem(marker)
        self._markers = None
        for band in self._preview_bands.values():
            self.canvas().scene().removeItem(band)
        self._preview_bands = {}
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_page(self, box):
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "reshape.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Reshape")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT + " Esc cancels, Backspace removes the last point.")
        tile.clicked.connect(lambda _c=False: self.draw_mode())
        self.tool_tile = tile
        box.addWidget(tile)
        self.single_check = check("Reshape with single intersection", self.single_intersection, self.set_single)
        self.single_check.setToolTip("Let one crossing reshape a line: the shorter side is trimmed and the line "
                                     "continues along the sketch. Needs a selected line.")
        self.choose_check = check("Choose result on finish", self.choose_result, self.set_choose)
        self.choose_check.setToolTip("When the sketch cuts a polygon in pieces, click the piece to keep. "
                                     "Unchecked, the piece with the longest perimeter is kept.")
        box.addWidget(self.single_check)
        box.addWidget(self.choose_check)

    def build_footer(self, box):
        self.preview_check = check("Show Preview", self.show_preview, self.set_show_preview)
        self.preview_check.setToolTip("Draw the reshaped features while you move the mouse")
        self.nosel_check = check("Allow reshaping without a selection", self.allow_no_selection,
                                 self.set_allow_no_selection)
        self.nosel_check.setToolTip("With nothing selected, reshape every editable feature the line crosses")
        box.addWidget(self.preview_check)
        box.addWidget(self.nosel_check)

    def update_page(self):
        if self.tree is not None:
            self.tree.refresh(self.canvas())
        if self.tool_tile is not None:
            self.tool_tile.setChecked(not self._selecting)
        selecting_first = not self.allow_no_selection             # without a selection there is nothing to list
        for widget in (self.banner, self.tree):
            if widget is not None:
                widget.setVisible(selecting_first)
        if getattr(self, "choose_check", None) is not None:
            self.choose_check.setEnabled(selecting_first)
            lines = any(layer.geometryType() == LINE for layer, _ids in editable_selection(self.canvas()))
            self.single_check.setEnabled(selecting_first and lines)

    def set_single(self, on):
        self.single_intersection = bool(on)
        QgsSettings().setValue(SETTINGS + "reshape_single", self.single_intersection)

    def set_choose(self, on):
        self.choose_result = bool(on)
        QgsSettings().setValue(SETTINGS + "reshape_choose", self.choose_result)

    def set_show_preview(self, on):
        self.show_preview = bool(on)
        QgsSettings().setValue(SETTINGS + "reshape_preview", self.show_preview)
        if not on:
            self._clear_preview()

    def set_allow_no_selection(self, on):
        self.allow_no_selection = bool(on)
        QgsSettings().setValue(SETTINGS + "reshape_no_selection", self.allow_no_selection)
        if on and self._selecting:
            self.set_selecting(False)
        self.refresh_page()

    # ------------------------------------------------------------------ drawing
    def _draw(self, tail=None):
        self.band.reset(LINE)
        self.band.setColor(QColor(0, 0, 0))
        self.band.setWidth(2)
        self.band.setLineStyle(Qt.PenStyle.DashLine)
        shown = self.points + ([tail] if tail is not None else [])
        for i, p in enumerate(shown):
            self.band.addPoint(p, i == len(shown) - 1)        # the last one refreshes the band's extent
        self._place_markers(shown)
        self._draw_preview(tail)

    def _place_markers(self, shown):
        """Pro's sketch: a green square at the first point, a red one at the last."""
        if self._markers is None:
            if not shown:
                return
            self._markers = []
            for colour in (QColor(0, 140, 60), QColor(220, 30, 30)):
                marker = QgsVertexMarker(self.canvas())
                marker.setIconType(MARKER_BOX)
                marker.setColor(colour)
                marker.setIconSize(8)
                marker.setPenWidth(2)
                self._markers.append(marker)
        start, end = self._markers
        if len(shown) < 1:
            start.hide()
            end.hide()
            return
        start.setCenter(shown[0])
        start.show()
        end.setCenter(shown[-1])
        end.setVisible(len(shown) > 1)

    def _draw_preview(self, tail):
        self._clear_preview()
        pts = self.points + ([tail] if tail is not None else [])
        if not self.show_preview or len(pts) < 2:
            return
        changes = reshape_changes(self.canvas(), pts, self.allow_no_selection, self.single_intersection)[:self.PREVIEW_MAX]
        for _layer, _fid, geom in changes:
            gtype = geom.type()
            band = self._preview_bands.get(gtype)
            if band is None:
                band = QgsRubberBand(self.canvas(), gtype)
                band.setColor(BLUE)
                band.setFillColor(QColor(0, 121, 193, 50))
                band.setWidth(2)
                self._preview_bands[gtype] = band
            band.addGeometry(geom, QgsCoordinateReferenceSystem(), False)
        for band in self._preview_bands.values():
            band.updatePosition()
            band.update()

    def _clear_preview(self):
        for gtype, band in self._preview_bands.items():
            band.reset(gtype)

    def on_click(self, point):
        if self._choosing is not None:
            self._pick_result(point)
            return
        if not self.points and not self.allow_no_selection and not self._has_selection():
            self.set_selecting(True, auto=True)        # nothing to reshape yet: select first
            self.say("select the features to reshape first.")
            return
        if self.constraint_click(point):
            return
        self.points.append(self.constrained(point))
        self.clear_constraint()                        # a lock is used up by the point it placed
        self._draw()

    def on_move(self, point):
        if self._choosing is not None:
            self._hover_result(point)
        elif self.constraint_move(point):
            return
        elif self.points:
            self._draw(self.constrained(point))

    def on_right_click(self, e):
        if self._choosing is not None:
            self.start_over()
            return
        if self._picking is not None:
            self.clear_constraint()
            self._draw(None)
            return
        if self.points:
            self.sketch_menu(QgsPointXY(e.snapPoint()))          # Finish Sketch, Parallel, Perpendicular ...
            return
        self.finish()

    def remove_last_point(self):
        if self.points:
            self.points.pop()
            self._draw()

    def finish(self):
        if self._choosing is not None:
            return 0
        points, self.points = self.points, []
        self.reset()
        if len(points) < 2:
            self.say("draw at least two points.")
            return 0
        options = reshape_options(self.canvas(), points, self.allow_no_selection, self.single_intersection)
        ambiguous = [o for o in options if len(o[2]) > 1]
        if self.choose_result and ambiguous and not self.allow_no_selection:
            self._choosing = {"fixed": [(layer, fid, c[0]) for layer, fid, c in options if len(c) == 1],
                              "pending": ambiguous, "picked": [], "hover": None}
            self._show_choices()
            return 0
        return self._complete([(layer, fid, c[0]) for layer, fid, c in options])

    def _complete(self, changes):
        failed = commit(self.canvas(), changes, [], "Reshape features") if changes else []
        n = len(changes)
        if n:
            text = "Reshape completed."
        elif not self.allow_no_selection and not self._has_selection():
            text = "Nothing was reshaped: select the features first, or allow reshaping without a selection."
        else:
            text = "Nothing was reshaped: the line did not cross any editable feature it could reshape."
        if failed:
            text += f" Failed in: {', '.join(failed)}."
        if self.done_strip is not None:
            self.done_strip.show_text(text, ok=bool(n) and not failed)
        self.say(text + (" (Ctrl+Z undoes it)" if n else ""))
        self.refresh_page()
        return n

    # ------------------------------------------------------------------ choosing the result
    def _show_choices(self):
        """The pieces of the current feature: dashed outlines, the one under the mouse filled."""
        self._clear_choice_bands()
        layer, _fid, candidates = self._choosing["pending"][0]
        hover = self._choosing["hover"]
        for i, geom in enumerate(candidates):
            band = QgsRubberBand(self.canvas(), geom.type())
            on = i == hover
            band.setColor(ORANGE if on else BLUE)
            band.setFillColor(QColor(255, 140, 0, 70) if on else QColor(0, 121, 193, 20))
            band.setWidth(3 if on else 2)
            band.setLineStyle(Qt.PenStyle.SolidLine if on else Qt.PenStyle.DashLine)
            band.addGeometry(geom, QgsCoordinateReferenceSystem(), True)
            self._choice_bands.append(band)
        self.say("click the piece to keep (Esc cancels).")

    def _clear_choice_bands(self):
        for band in self._choice_bands:
            with suppress(Exception):
                self.canvas().scene().removeItem(band)
        self._choice_bands = []

    def _end_choosing(self):
        self._choosing = None
        self._clear_choice_bands()

    def _hover_result(self, point):
        here = QgsGeometry.fromPointXY(point)
        candidates = self._choosing["pending"][0][2]
        hit = next((i for i, g in enumerate(candidates) if g.contains(here) or g.distance(here) < 1e-9), None)
        if hit != self._choosing["hover"]:
            self._choosing["hover"] = hit
            self._show_choices()

    def _pick_result(self, point):
        self._hover_result(point)
        hit = self._choosing["hover"]
        if hit is None:
            return
        state = self._choosing
        layer, fid, candidates = state["pending"].pop(0)
        state["picked"].append((layer, fid, candidates[hit]))
        state["hover"] = None
        if state["pending"]:
            self._show_choices()
            return
        changes = state["fixed"] + state["picked"]
        self._end_choosing()
        self._complete(changes)

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):
            return
        if self.constraint_key(e):
            e.ignore()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finish()
            e.ignore()
        elif e.key() == Qt.Key.Key_Backspace and self.points:
            self.remove_last_point()
            e.ignore()
        else:
            super().keyPressEvent(e)


# ---------------------------------------------------------------------------------------- replace geometry
def _shaped_like(source):
    """fn(target geometry) -> the source geometry in the target's single / multi form, or None if the kinds differ."""
    def fn(target):
        if source.type() != target.type():
            return None
        out = QgsGeometry(source)
        if target.isMultipart() and not out.isMultipart():
            out.convertToMultiType()
        elif not target.isMultipart() and out.isMultipart():
            if out.constGet().partCount() != 1:
                return None
            out.convertToSingleType()
        return out
    return fn


class ReplaceGeometryTool(SketchConstraints, ProPage, PickTool):
    """Laid out like the Replace Geometry pane of ArcGIS Pro: "Change the selection.", the selected features and the
    Replace Polygon / Line / Point tile. With the tile active, draw the new shape on the map (dashed outline, green
    vertices, snapping): click the points, then right-click or Enter. The selected features take that shape."""

    KEY = "replace_geometry"
    LABEL = "Replace Geometry"
    HINT = ("select the features to change, then draw the new shape on the map: click its points, then right-click or "
            "Enter to finish.")
    SHOWS_SELECTION = False
    SHOW_HINT = False
    SHOW_STATUS = False
    SELECTS = AUTO_SELECT = True
    KIND_NAMES = {Qgis.GeometryType.Point: "Point", LINE: "Line", POLYGON: "Polygon"}

    def __init__(self, canvas, iface, select_tool=None):
        self._vertex_band = None
        super().__init__(canvas, iface, select_tool)

    def reset(self):
        self.points = []
        self.clear_constraint()
        self._clear_vertices()
        super().reset()

    def _clear_vertices(self):
        if getattr(self, "_vertex_band", None) is not None:
            self._vertex_band.reset(Qgis.GeometryType.Point)

    def dispose(self):
        self.dispose_constraint_bands()
        if self._vertex_band is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(self._vertex_band)
            self._vertex_band = None
        super().dispose()

    def build_page(self, box):
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "replace_geometry.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT + " Esc cancels, Backspace removes the last point.")
        tile.clicked.connect(lambda _c=False: self.draw_mode())
        self.tool_tile = tile
        box.addWidget(tile)
        self.update_page()

    def selected_kind(self):
        """The kind of geometry to draw: that of the selected features (polygon first when they are mixed)."""
        kinds = {layer.geometryType() for layer, _ids in editable_selection(self.canvas())}
        for kind in (POLYGON, LINE, Qgis.GeometryType.Point):
            if kind in kinds:
                return kind
        return None

    def update_page(self):
        if self.tree is not None:
            self.tree.refresh(self.canvas())
        tile = self.tool_tile
        if tile is not None:
            kind = self.KIND_NAMES.get(self.selected_kind())
            tile.setText(f"Replace {kind}" if kind else "Replace Geometry")
            tile.setChecked(not self._selecting)

    # ------------------------------------------------------------------ drawing
    def _draw(self, tail=None):
        kind = self.selected_kind()
        shown = self.points + ([tail] if tail is not None else [])
        self.band.reset(LINE)
        self.band.setColor(QColor(0, 0, 0))
        self.band.setWidth(2)
        self.band.setLineStyle(Qt.PenStyle.DashLine)
        outline = shown + ([shown[0]] if kind == POLYGON and len(shown) > 2 else [])
        for i, p in enumerate(outline):
            self.band.addPoint(p, i == len(outline) - 1)
        if self._vertex_band is None:
            self._vertex_band = QgsRubberBand(self.canvas(), Qgis.GeometryType.Point)
            self._vertex_band.setColor(QColor(0, 120, 40))
            self._vertex_band.setFillColor(QColor(0, 120, 40))
            self._vertex_band.setIconSize(7)
            with suppress(AttributeError):
                self._vertex_band.setIcon(QgsRubberBand.IconType.ICON_BOX)
        self._vertex_band.reset(Qgis.GeometryType.Point)
        for i, p in enumerate(self.points):
            self._vertex_band.addPoint(p, i == len(self.points) - 1)

    def on_move(self, point):
        if self.constraint_move(point):
            return
        if self.points:
            self._draw(self.constrained(point))

    def on_click(self, point):
        kind = self.selected_kind()
        if kind is None:
            self.tell("Select the features to change first, then draw the new shape.", ok=False)
            return
        if self._selecting:
            return
        if self.constraint_click(point):
            return
        self.points.append(self.constrained(point))
        self.clear_constraint()                        # a lock is used up by the point it placed
        if kind == Qgis.GeometryType.Point:
            self.finish()
            return
        self._draw()

    def on_right_click(self, e):
        if self._picking is not None:
            self.clear_constraint()
            self._draw(None)
        elif self.points:
            self.sketch_menu(QgsPointXY(e.snapPoint()))          # Finish Sketch, Parallel, Perpendicular ...
        else:
            self.finish()

    def remove_last_point(self):
        if self.points:
            self.points.pop()
            self._draw()

    def sketch_geometry(self):
        kind = self.selected_kind()
        pts = self.points
        if kind == Qgis.GeometryType.Point and pts:
            return QgsGeometry.fromPointXY(pts[0])
        if kind == LINE and len(pts) >= 2:
            return QgsGeometry.fromPolylineXY(pts)
        if kind == POLYGON and len(pts) >= 3:
            return QgsGeometry.fromPolygonXY([pts + [pts[0]]])
        return None

    def finish(self):
        geom = self.sketch_geometry()
        if geom is None:
            if self.points:
                self.say("draw at least 3 points for a polygon, 2 for a line.")
            return 0
        if not geom.isGeosValid():
            self.tell("The new shape crosses itself - draw it again.", ok=False)
            self.reset()
            return 0
        changed, _layers, skipped = apply_to_selection(self.canvas(), _shaped_like(geom), "Replace geometry")
        text = "Replace Geometry completed." if changed else "Nothing was replaced."
        if skipped:
            text += f" Not editable, left alone: {', '.join(skipped)}."
        self.reset()
        self.tell(text, ok=bool(changed) and not skipped)
        return changed

    def keyPressEvent(self, e):
        if self.forward_select("keyPressEvent", e):
            return
        if self.constraint_key(e):
            e.ignore()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finish()
            e.ignore()
        elif e.key() == Qt.Key.Key_Backspace and self.points:
            self.remove_last_point()
            e.ignore()
        else:
            super().keyPressEvent(e)

    def tell(self, text, ok=True):
        if self.done_strip is not None:
            self.done_strip.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))


# ---------------------------------------------------------------------------------------- extend / trim
class ExtendTrimTool(ProPage, PickTool):
    """
    Laid out like the Extend or Trim pane of ArcGIS Pro: three tiles (Extend or Trim, Extend, Trim) and at the
    bottom "Recalculate COGO attributes" and "Allow Extend or Trim without a selection". Hover a line: a grey
    preview shows the part that would be removed (thick) or the extension (dashed); click to do it. Lines change
    to the nearest other feature shown on the map; only selected lines change unless no selection is allowed.
    """

    KEY = "extend_trim"
    LABEL = "Extend or Trim"
    HINT = ("point at the end of a line to extend it to the nearest feature, or at the part beyond a feature to trim "
            "it away, then click.")
    SHOWS_SELECTION = False
    SHOW_HINT = False
    SHOW_STATUS = False
    SELECTS = AUTO_SELECT = True
    TOUCH_PX = 4
    MODES = (
        ("both", "Extend or Trim", "extend_trim.svg",
         "Extend a line to the nearest feature, or trim the part beyond one - whichever the pointed-at part needs."),
        ("extend", "Extend", "extend.svg", "Extend the end of a line nearer to the click to the nearest feature ahead."),
        ("trim", "Trim", "trim.svg", "Remove the clicked part of a line, back to the nearest feature."),
    )

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.allow_no_selection = settings.value(SETTINGS + "extrim_no_selection", False, type=bool)
        self.recalc_cogo = settings.value(SETTINGS + "extrim_cogo", False, type=bool)
        self.mode = settings.value(SETTINGS + "extrim_mode", "both")
        if self.mode not in ("both", "extend", "trim"):
            self.mode = "both"
        self.tiles = {}
        self.banner = None
        self._boundary_cache = None
        super().__init__(canvas, iface, select_tool)

    def start_select_mode(self):
        self.set_selecting(not self.allow_no_selection and not self._has_selection(), auto=True)

    def reset(self):
        self._boundary_cache = None
        self.band.reset(LINE)
        super().reset()

    # ------------------------------------------------------------------ page
    @property
    def tool_tile(self):
        return self.tiles.get(self.mode)

    @tool_tile.setter
    def tool_tile(self, _value):
        pass

    def build_page(self, box):
        for key, text, icon, tip in self.MODES:
            tile = QToolButton()
            tile.setObjectName("KgaModifyToolTile")
            tile.setCheckable(True)
            tile.setIcon(QIcon(os.path.join(ICON_DIR, icon)))
            tile.setIconSize(QSize(26, 26))
            tile.setText(text)
            tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            tile.setToolTip(tip)
            tile.clicked.connect(lambda _c=False, k=key: self.set_mode(k))
            self.tiles[key] = tile
            box.addWidget(tile)

    def build_footer(self, box):
        self.cogo_check = check("Recalculate COGO attributes", self.recalc_cogo, self.set_recalc)
        self.cogo_check.setToolTip("After a change, rewrite the line's COGO fields (direction, distance...) "
                                   "from its geometry")
        self.nosel_check = check("Allow Extend or Trim without a selection", self.allow_no_selection,
                                 self.set_allow_no_selection)
        self.nosel_check.setToolTip("With nothing selected, change any editable line you click")
        box.addWidget(self.cogo_check)
        box.addWidget(self.nosel_check)

    def update_page(self):
        if self.tree is not None:
            self.tree.refresh(self.canvas())
        for key, tile in self.tiles.items():
            tile.setChecked(key == self.mode and not self._selecting)
        for widget in (self.banner, self.tree):
            if widget is not None:
                widget.setVisible(not self.allow_no_selection)

    def draw_mode(self):
        if self._selecting:
            self.set_selecting(False)
        else:
            self.update_page()

    def set_mode(self, key):
        self.mode = key
        QgsSettings().setValue(SETTINGS + "extrim_mode", key)
        self.band.reset(LINE)
        self.draw_mode()

    def set_recalc(self, on):
        self.recalc_cogo = bool(on)
        QgsSettings().setValue(SETTINGS + "extrim_cogo", self.recalc_cogo)

    def set_allow_no_selection(self, on):
        self.allow_no_selection = bool(on)
        QgsSettings().setValue(SETTINGS + "extrim_no_selection", self.allow_no_selection)
        if on and self._selecting:
            self.set_selecting(False)
        self.refresh_page()

    # ------------------------------------------------------------------ what a click would do
    def _boundary_for(self, layer, fid):
        """Every other line and polygon outline shown on the map, as one geometry in the map's CRS."""
        extent = self.canvas().extent()
        key = (layer.id(), fid, extent.toString(1))
        cached = self._boundary_cache
        if cached is not None and cached[0] == key and time.monotonic() - cached[2] < 2:
            return cached[1]
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        shown = map_layers(self.canvas())
        parts = []
        for other in all_vector_layers():
            if (shown is not None and other.id() not in shown) or other.geometryType() not in (LINE, POLYGON):
                continue
            same = other.crs() == canvas_crs
            rect = extent if same else _transform(canvas_crs, other.crs()).transformBoundingBox(extent)
            to_map = None if same else _transform(other.crs(), canvas_crs)
            for f in other.getFeatures(QgsFeatureRequest().setFilterRect(rect).setNoAttributes()):
                if other is layer and f.id() == fid:
                    continue
                geom = QgsGeometry(f.geometry())
                if geom.isEmpty():
                    continue
                if to_map is not None:
                    geom.transform(to_map)
                parts.append(geom if geom.type() == LINE else geom.convertToType(LINE, False))
        boundary = QgsGeometry.collectGeometry(parts) if parts else QgsGeometry()
        self._boundary_cache = (key, boundary, time.monotonic())
        return boundary

    def _target_at(self, point):
        selected = None
        if not self.allow_no_selection:
            selected = {(layer.id(), fid) for layer, ids in editable_selection(self.canvas()) for fid in ids}
            if not selected:
                return None
        for layer, fid, geom in pick_features(self.canvas(), point, (LINE,), editable_only=True):
            if selected is None or (layer.id(), fid) in selected:
                return layer, fid, geom
        return None

    def _plan(self, point):
        """(layer, fid, geometry, new points, what, preview points) or (None, message)."""
        target = self._target_at(point)
        if target is None:
            return None, ("no selected line there." if not self.allow_no_selection
                          else "no editable line there.")
        layer, fid, geom = target
        pts = G.single_line(geom)
        if pts is None:
            return None, "only single-part lines can be extended or trimmed."
        boundary = self._boundary_for(layer, fid)
        if boundary.isEmpty():
            return None, "no other feature on the map to extend or trim to."
        tol = self.TOUCH_PX * self.canvas().mapUnitsPerPixel()      # a feature ending this close to the line touches it
        new, what, preview = G.extend_trim_plan(pts, boundary, point, self.mode, tol)
        if new is None:
            return None, what + "."
        return layer, fid, geom, new, what, preview

    # ------------------------------------------------------------------ mouse
    def on_move(self, point):
        self.band.reset(LINE)
        if self._selecting:
            return
        plan = self._plan(point)
        if plan[0] is None:
            return
        preview = plan[5]
        self.band.setColor(QColor(110, 110, 110))
        self.band.setWidth(4)
        self.band.setLineStyle(Qt.PenStyle.DashLine)                # Pro draws both the trim and the extension dashed
        for i, p in enumerate(preview):
            self.band.addPoint(QgsPointXY(p), i == len(preview) - 1)

    def on_click(self, point):
        if not self.allow_no_selection and not self._has_selection():
            self.set_selecting(True, auto=True)
            self.say("select the lines to extend or trim first.")
            return
        plan = self._plan(point)
        if plan[0] is None:
            self.say(plan[1])
            return
        layer, fid, geom, new, what, _preview = plan
        self.band.reset(LINE)
        failed = commit(self.canvas(), [(layer, fid, G.line_like(geom, new))], [], f"{what.capitalize()} line")
        self._boundary_cache = None
        if failed:
            self.tell(f"Could not edit {', '.join(failed)} - is it in edit mode?", ok=False)
            return
        if self.recalc_cogo:
            self._recalculate_cogo(layer, fid)
        self.tell(f"Line {what}.")

    def _recalculate_cogo(self, layer, fid):
        recalculate_cogo(layer, fid)

    def tell(self, text, ok=True):
        if self.done_strip is not None:
            self.done_strip.show_text(text, ok=ok)
        self.say(text + (" (Ctrl+Z undoes it)" if ok else ""))


# ---------------------------------------------------------------------------------------- fillet / line intersection
class CornerTool(PickTool):
    """Two lines meet at a corner: joined by an arc with a radius (Fillet), else trimmed or extended to it."""

    ROUNDED = False

    def __init__(self, canvas, iface, select_tool=None):
        super().__init__(canvas, iface, select_tool)
        self.radius = 5.0
        self.trim = True                # cut or extend the lines back to where the arc touches them
        self.make_arc = True            # add the arc as a new feature in the first line's layer

    def reset(self):
        self.first = None
        super().reset()

    def build_page(self, box):
        if self.ROUNDED:
            box.addWidget(section_label("Fillet"))
            form = add_form(box)
            self.radius_spin = spin(self.radius, 1e-6, 1e9, 3, 1.0)
            self.radius_spin.valueChanged.connect(lambda v: setattr(self, "radius", v))
            form.addRow("Radius (map units)", self.radius_spin)
            box.addWidget(check("Trim or extend the lines to the arc", self.trim, lambda on: setattr(self, "trim", on)))
            box.addWidget(check("Add the arc as a new feature", self.make_arc,
                                lambda on: setattr(self, "make_arc", on)))
        self._first_label = hint_label("")
        box.addWidget(self._first_label)
        box.addWidget(button("Start over", self.start_over, "Esc or right-click does the same"))

    def update_page(self):
        label = getattr(self, "_first_label", None)
        if label is not None:
            label.setText("First line picked - now click the second line." if self.first is not None
                          else "Click the first line.")
        spin_box = getattr(self, "radius_spin", None)
        if spin_box is not None and abs(spin_box.value() - self.radius) > 1e-12:
            spin_box.blockSignals(True)
            spin_box.setValue(self.radius)
            spin_box.blockSignals(False)

    def on_click(self, point):
        skip = None if self.first is None else (self.first[0].id(), self.first[1])
        hits = _line_layers_hit(self.canvas(), point, skip)
        if not hits:
            self.say("no editable line there." if self.first is None else "no other editable line there.")
            return
        layer, fid, g = hits[0]
        if G.single_line(g) is None:
            self.say("only single-part lines can be joined.")
            return
        if self.first is None:
            self.first = (layer, fid, g, point)
            self.highlight(g)
            self.refresh_page()
            self.say("first line picked - now click the second line (click the part of each line to keep).")
            return
        self.join(self.first, (layer, fid, g, point))
        self.reset()

    def join(self, a, b):
        (l1, f1, g1, c1), (l2, f2, g2, c2) = a, b
        new1, new2, arc = G.corner(G.single_line(g1), c1, G.single_line(g2), c2, self.radius if self.ROUNDED else 0.0)
        if new1 is None:
            self.say(new2 + ".")
            return False
        changes, adds = [], []
        if self.trim or not self.ROUNDED:
            changes = [(l1, f1, G.line_like(g1, new1)), (l2, f2, G.line_like(g2, new2))]
        if arc is not None and self.make_arc:
            adds = [(l1, QgsGeometry.fromPolylineXY(arc))]
        if not changes and not adds:
            self.say("nothing to do - both 'trim or extend the lines' and 'add the arc' are off.")
            return False
        failed = commit(self.canvas(), changes, adds, self.LABEL)
        self.say("done. (Ctrl+Z undoes it)" if not failed else f"could not edit {', '.join(failed)}.")
        return not failed


class FilletTool(CornerTool):
    """
    Laid out like the Fillet pane of ArcGIS Pro: the completed strip, the Fillet tile and "Fixed Radius" with its
    value in the project's distance unit. Click the first line, then move over the second: both lines and the arc
    are previewed (the arc dashed). Without a fixed radius the mouse sets it: the arc touches the second line where
    the mouse is. Click to finish.
    """

    KEY = "fillet"
    LABEL = "Fillet"
    ROUNDED = True
    HINT = ("click the first line, then the second: they are joined by an arc. With Fixed Radius off, the arc "
            "touches the second line where you click.")
    SHOW_HINT = False
    SHOW_STATUS = False
    GREY = QColor(128, 128, 128)

    def __init__(self, canvas, iface, select_tool=None):
        self._arc_band = None
        self.done_strip = None
        self.tool_tile = None
        self.fixed = QgsSettings().value(SETTINGS + "fillet_fixed", False, type=bool)
        super().__init__(canvas, iface, select_tool)
        self.radius = QgsSettings().value(SETTINGS + "fillet_radius", 5.0, type=float)      # in map units

    def reset(self):
        if getattr(self, "_arc_band", None) is not None:          # not there yet / any more while Qt tears the tool down
            self._arc_band.reset(LINE)
        super().reset()

    def dispose(self):
        if self._arc_band is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(self._arc_band)
            self._arc_band = None
        super().dispose()

    # ------------------------------------------------------------------ the project's unit
    def unit(self):
        return QgsProject.instance().distanceUnits()

    def unit_label(self):
        return QgsUnitTypes.toAbbreviatedString(self.unit())

    def unit_factor(self):
        """Map units in one project distance unit."""
        map_units = self.canvas().mapSettings().destinationCrs().mapUnits()
        return QgsUnitTypes.fromUnitToUnitFactor(self.unit(), map_units)

    def radius_in_unit(self):
        return self.radius / self.unit_factor()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.done_strip = DoneStrip()
        box.addWidget(self.done_strip)

    def build_page(self, box):
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "fillet.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Fillet")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT)
        tile.clicked.connect(lambda _c=False: tile.setChecked(True))
        self.tool_tile = tile
        box.addWidget(tile)
        row = QHBoxLayout()
        self.fixed_check = check("Fixed Radius", self.fixed, self.set_fixed)
        self.fixed_check.setToolTip("Round every corner with the same radius. Unchecked, the mouse sets the radius.")
        self.radius_spin = spin(self.radius_in_unit(), 1e-6, 1e9, 3, 1.0)
        self.radius_spin.setEnabled(self.fixed)
        self.radius_spin.valueChanged.connect(self.set_radius_in_unit)
        self.unit_text = QLabel(self.unit_label())
        row.addWidget(self.fixed_check, 1)
        row.addWidget(self.radius_spin)
        row.addWidget(self.unit_text)
        box.addLayout(row)

    def update_page(self):
        spin_box = getattr(self, "radius_spin", None)
        if spin_box is None:
            return
        self.unit_text.setText(self.unit_label())
        self.fixed_check.blockSignals(True)
        self.fixed_check.setChecked(self.fixed)
        self.fixed_check.blockSignals(False)
        spin_box.setEnabled(self.fixed)
        shown = self.radius_in_unit()
        if abs(spin_box.value() - shown) > 1e-9:
            spin_box.blockSignals(True)
            spin_box.setValue(shown)
            spin_box.blockSignals(False)

    def set_fixed(self, on):
        self.fixed = bool(on)
        QgsSettings().setValue(SETTINGS + "fillet_fixed", self.fixed)
        self.update_page()

    def set_radius_in_unit(self, value):
        self.radius = value * self.unit_factor()
        QgsSettings().setValue(SETTINGS + "fillet_radius", self.radius)

    # ------------------------------------------------------------------ what a click would do
    def _corner_hits(self, point):
        """Editable lines under the mouse and editable polygons whose OUTLINE is under it, nearest first."""
        tolerance = 8 * self.canvas().mapUnitsPerPixel()
        out = []
        for hit in pick_features(self.canvas(), point, (LINE, POLYGON), editable_only=True):
            if hit[2].type() == POLYGON:
                edge = G.nearest_ring_edge(hit[2], point)
                if edge is None or edge[4] > tolerance:
                    continue
            out.append(hit)
        return out

    def _first_ring(self):
        """(part, ring, closed points, segment) of the polygon edge the first click was on, or None for a line."""
        if self.first is None or self.first[2].type() != POLYGON:
            return None
        edge = G.nearest_ring_edge(self.first[2], self.first[3])
        return None if edge is None else (edge[0], edge[1], edge[3], edge[2])

    def _second_at(self, point):
        """(layer, fid, geometry) of what the mouse is over as the second line, or None. The first line itself counts
        when the mouse is over another segment of it (a single line with a corner); a polygon is its own second pick:
        another edge of the same ring."""
        if self.first is None:
            return None
        own = (self.first[0].id(), self.first[1])
        ring = self._first_ring()
        if ring is not None:
            for hit in self._corner_hits(point):
                if (hit[0].id(), hit[1]) == own:
                    edge = G.nearest_ring_edge(hit[2], point)
                    if edge is not None and edge[:2] == ring[:2] and edge[2] != ring[3]:
                        return hit
            return None
        first_pts = G.single_line(self.first[2])
        first_segment = G.nearest_segment(first_pts, self.first[3])[0]
        for hit in _line_layers_hit(self.canvas(), point):
            pts = G.single_line(hit[2])
            if pts is None:
                continue
            if (hit[0].id(), hit[1]) == own:
                if G.nearest_segment(pts, point)[0] == first_segment:
                    continue
            return hit
        return None

    def same_line(self, second):
        return (second[0].id(), second[1]) == (self.first[0].id(), self.first[1])

    def radius_for(self, first, second_geom, click):
        """The fixed radius, or the one the mouse position sets; None when there is no corner."""
        if self.fixed:
            return self.radius
        return G.corner_radius_at(G.single_line(first[2]), first[3], G.single_line(second_geom), click, click)

    def _plan(self, second, point):
        """((first line's new points, second line's new points or None, arc, radius, ring), None) or (None, message).
        With one line (or a polygon) cornering on itself the second is None: the arc is part of the first line / ring;
        `ring` is (part, ring index) for a polygon, else None."""
        ring = self._first_ring()
        if ring is not None:                                                     # a corner of a polygon
            radius = self.radius
            if not self.fixed:
                radius = G.ring_radius_at(ring[2], self.first[3], point, point)
                if radius is None:
                    return None, "the two edges have no corner to round"
            new, arc = G.ring_fillet(ring[2], self.first[3], point, radius)
            if new is None:
                return None, arc
            return (new, None, arc, radius, ring[:2]), None
        pts1 = G.single_line(self.first[2])
        if self.same_line(second):
            radius = self.radius
            if not self.fixed:
                radius = G.vertex_radius_at(pts1, self.first[3], point, point)
                if radius is None:
                    return None, "the two segments have no corner to round"
            new, arc = G.vertex_fillet(pts1, self.first[3], point, radius)
            if new is None:
                return None, arc
            return (new, None, arc, radius, None), None
        radius = self.radius_for(self.first, second[2], point)
        if radius is None:
            return None, "the two lines have no corner to round"
        new1, new2, arc = G.corner(pts1, self.first[3], G.single_line(second[2]), point, radius)
        if new1 is None:
            return None, new2
        return (new1, new2, arc, radius, None), None

    def on_move(self, point):
        if self._arc_band is not None:
            self._arc_band.reset(LINE)
        second = self._second_at(point)
        if second is None:
            if self.first is not None:
                self.highlight(self.first[2], self.GREY)
            return
        plan, _message = self._plan(second, point)
        if plan is None:
            return
        new1, new2, arc, _radius, _ring = plan
        self.band.reset(LINE)
        self.band.setColor(self.GREY)
        self.band.setWidth(3)
        for pts in (new1, new2):
            if pts is not None:
                self.band.addGeometry(QgsGeometry.fromPolylineXY(pts), QgsCoordinateReferenceSystem(), False)
        self.band.updatePosition()
        self.band.update()
        if self._arc_band is None:
            self._arc_band = QgsRubberBand(self.canvas(), LINE)
            self._arc_band.setColor(QColor(0, 0, 0))
            self._arc_band.setWidth(3)
            self._arc_band.setLineStyle(Qt.PenStyle.DashLine)
        self._arc_band.addGeometry(QgsGeometry.fromPolylineXY(arc), QgsCoordinateReferenceSystem(), True)

    def on_click(self, point):
        if self.first is None:
            hits = [h for h in self._corner_hits(point)
                    if h[2].type() == POLYGON or G.single_line(h[2]) is not None]
            if not hits:
                self.say("no editable single-part line or polygon edge there.")
                return
            layer, fid, geom = hits[0]
            self.first = (layer, fid, geom, point)
            self.highlight(geom, self.GREY)
            self.say("now click the other edge of the corner." if geom.type() == POLYGON else "now click the second line.")
            return
        second = self._second_at(point)
        if second is None:
            self.say("no other editable line there.")
            return
        plan, message = self._plan(second, point)
        if plan is None:
            self.say(message + ".")
            return
        new1, new2, arc, _radius, ring = plan
        (l1, f1, g1), (l2, f2, g2) = self.first[:3], second
        if ring is not None:                              # a polygon corner: the ring is rebuilt with the arc in it
            changes, adds = [(l1, f1, G.polygon_with_ring(g1, ring[0], ring[1], new1))], []
        elif new2 is None:                                # one line rounding its own corner: the arc is in the line
            changes, adds = [(l1, f1, G.line_like(g1, new1))], []
        else:
            changes = [(l1, f1, G.line_like(g1, new1)), (l2, f2, G.line_like(g2, new2))]
            adds = [(l1, QgsGeometry.fromPolylineXY(arc))]
        failed = commit(self.canvas(), changes, adds, self.LABEL)
        self.reset()
        text = "Fillet completed." if not failed else f"Could not edit {', '.join(failed)} - is it in edit mode?"
        if self.done_strip is not None:
            self.done_strip.show_text(text, ok=not failed)
        self.say(text + (" (Ctrl+Z undoes it)" if not failed else ""))


def recalculate_cogo(layer, fid):
    """Rewrite one line's COGO fields from its geometry (nothing happens if the layer has none)."""
    from .cogo import CogoSettings, update_cogo_values          # cogo builds on this module
    settings = CogoSettings.load()
    settings.create_fields = False
    with suppress(ValueError):
        update_cogo_values(layer, [fid], settings)


class LineIntersectionTool(CornerTool):
    """
    Laid out like the Line Intersection pane of ArcGIS Pro: a blue instruction bar, the Line Intersection tile,
    "Recalculate COGO attributes" and "Create new lines when extending". Click the first line (grey), then hover
    the second: the result is previewed (blue dot at the intersection). Lines that cross are cut in two there, so
    two crossing lines become four; lines that do not meet are extended to meet. Tab tries another crossing when
    there are several, a click or Enter makes it.
    """

    KEY = "line_intersection"
    LABEL = "Line Intersection"
    HINT = ("click two lines on the map: where they cross both are cut in two; lines that do not meet are extended "
            "until they do.")
    SHOW_HINT = False
    SHOW_STATUS = False
    GREY = QColor(128, 128, 128)
    FIRST_TEXT = "Click on the first line on the map"
    SECOND_TEXT = "Click on the second line on the map"
    PREVIEW_TEXT = ("Hover or press Tab to preview the possible intersections. Click the line or press Enter to "
                    "create the intersection.")

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.recalc_cogo = settings.value(SETTINGS + "lineint_cogo", False, type=bool)
        self.create_new = settings.value(SETTINGS + "lineint_new", False, type=bool)
        self.variant = 0
        self._count = 1
        self.done_strip = None
        self.tool_tile = None
        self._message = None                  # the result of the last run, shown until the next click
        self._last = None                     # (second line hit, mouse point) of the last hover
        self._dot = None
        super().__init__(canvas, iface, select_tool)

    def reset(self):
        self.variant = 0
        self._count = 1
        self._last = None
        self._message = None
        if self._dot is not None:
            self._dot.reset(Qgis.GeometryType.Point)
        super().reset()

    def dispose(self):
        if self._dot is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(self._dot)
            self._dot = None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.done_strip = DoneStrip()
        box.addWidget(self.done_strip)

    def build_page(self, box):
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "line_intersection.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Line Intersection")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT)
        tile.clicked.connect(lambda _c=False: tile.setChecked(True))
        self.tool_tile = tile
        box.addWidget(tile)
        self.cogo_check = check("Recalculate COGO attributes", self.recalc_cogo, self.set_recalc)
        self.cogo_check.setToolTip("After a change, rewrite the lines' COGO fields (direction, distance...) "
                                   "from their geometry")
        self.new_check = check("Create new lines when extending", self.create_new, self.set_create_new)
        self.new_check.setToolTip("An extended line keeps its shape; the extension is added as a new line instead")
        box.addWidget(self.cogo_check)
        box.addWidget(self.new_check)

    def update_page(self):
        strip = self.done_strip
        if strip is None or self._message is not None:
            return
        strip.show_text(self.FIRST_TEXT if self.first is None else self.SECOND_TEXT, ok="info")

    def set_recalc(self, on):
        self.recalc_cogo = bool(on)
        QgsSettings().setValue(SETTINGS + "lineint_cogo", self.recalc_cogo)

    def set_create_new(self, on):
        self.create_new = bool(on)
        QgsSettings().setValue(SETTINGS + "lineint_new", self.create_new)

    def _info(self, text, ok="info"):
        if self.done_strip is not None:
            self.done_strip.show_text(text, ok=ok)

    # ------------------------------------------------------------------ what a click would do
    def _second_at(self, point):
        if self.first is None:
            return None
        hits = _line_layers_hit(self.canvas(), point, (self.first[0].id(), self.first[1]))
        return next((h for h in hits if G.single_line(h[2]) is not None), None)

    def _plan(self, second, point):
        """(ops, intersection point, number of intersections) for the current variant, or (None, message, 0)."""
        return G.line_intersection_plan(G.single_line(self.first[2]), self.first[3], G.single_line(second[2]), point,
                                        self.variant)

    def _draw(self):
        """Preview the plan: the lines as they would end up, grey, and a blue dot where they meet."""
        self.band.reset(LINE)
        if self._dot is not None:
            self._dot.reset(Qgis.GeometryType.Point)
        if self._last is None:
            return
        second, point = self._last
        ops, where, count = self._plan(second, point)
        self._count = count
        if ops is None:
            self.highlight(self.first[2], self.GREY)
            return
        self.band.setColor(self.GREY)
        self.band.setWidth(4)
        for op, (_l, _f, geom) in zip(ops, (self.first[:3], second[:3])):
            shown = [G.single_line(geom)] if op[0] == "none" else [op[1], op[2]] if op[0] == "split" else [op[1]]
            for pts in shown:
                self.band.addGeometry(QgsGeometry.fromPolylineXY(pts), QgsCoordinateReferenceSystem(), False)
        self.band.updatePosition()
        self.band.update()
        if self._dot is None:
            self._dot = QgsRubberBand(self.canvas(), Qgis.GeometryType.Point)
            self._dot.setColor(QColor(0, 0, 0))
            self._dot.setFillColor(QColor(51, 153, 255))
            self._dot.setIconSize(10)
            with suppress(AttributeError):
                self._dot.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
        if where is not None:
            self._dot.addPoint(QgsPointXY(where.x(), where.y()), True)
        if self._message is None:
            self._info(self.PREVIEW_TEXT)

    def on_move(self, point):
        second = self._second_at(point)
        if second is None:
            self._last = None
            if self.first is not None:
                self.highlight(self.first[2], self.GREY)
                if self._dot is not None:
                    self._dot.reset(Qgis.GeometryType.Point)
                if self._message is None:
                    self._info(self.SECOND_TEXT)
            return
        if self._last is None or self._last[0][:2] != second[:2]:
            self.variant = 0
        self._last = (second, point)
        self._draw()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and self._last is not None:
            step = -1 if e.key() == Qt.Key.Key_Backtab else 1
            self.variant = (self.variant + step) % max(1, self._count)
            self._draw()
            e.ignore()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._last is not None:
            self.complete(*self._last)
            e.ignore()
        else:
            super().keyPressEvent(e)

    def on_click(self, point):
        if self.first is None:
            hits = [h for h in _line_layers_hit(self.canvas(), point) if G.single_line(h[2]) is not None]
            if not hits:
                self.say("no editable single-part line there.")
                return
            layer, fid, geom = hits[0]
            self._message = None
            self.first = (layer, fid, geom, point)
            self.highlight(geom, self.GREY)
            self.refresh_page()
            return
        second = self._second_at(point)
        if second is None:
            self.say("no other editable line there.")
            return
        self.complete(second, point)

    def complete(self, second, point):
        ops, _where, _count = self._plan(second, point)
        if ops is None:
            self.say(_where + ".")
            return False
        changes, adds = [], []
        for op, (layer, fid, geom) in zip(ops, (self.first[:3], second[:3])):
            if op[0] == "none":
                continue
            attrs = _copyable(layer, layer.getFeature(fid).attributes())
            if op[0] == "split":
                changes.append((layer, fid, G.line_like(geom, op[1])))
                adds.append((layer, G.line_like(geom, op[2]), attrs))
            elif self.create_new and op[2] is not None:
                adds.append((layer, QgsGeometry.fromPolylineXY(op[2]), attrs))      # the line itself stays as it was
            else:
                changes.append((layer, fid, G.line_like(geom, op[1])))
        created = []
        failed = commit(self.canvas(), changes, adds, self.LABEL, created)
        touched = [(layer, fid) for layer, fid, _g in changes] + created
        self.reset()
        if failed:
            self._message = f"Could not edit {', '.join(failed)} - is it in edit mode?"
            self._info(self._message, ok=False)
            return False
        if self.recalc_cogo:
            for layer, fid in touched:
                recalculate_cogo(layer, fid)
        self._message = "Line Intersection completed."
        self._info(self._message, ok=True)
        self.say(self._message + " (Ctrl+Z undoes it)")
        return True


# ---------------------------------------------------------------------------------------- reverse direction
class ReverseDirectionTool(PickTool):
    """
    Laid out like the Reverse Direction pane of ArcGIS Pro: the tile, "Reverse topmost line" (with "Choose component
    part of a multipart line"), "Choose from overlapping lines" and "Recalculate COGO Direction attribute".
    Hover an editable line: arrows show the way it runs; click to turn it around.
    """

    KEY = "reverse_direction"
    LABEL = "Reverse Direction"
    HINT = "hover a line to see which way it runs, click it to reverse it."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    ARROW_SPACING_PX = 90
    ARROW_SIZE_PX = 16

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.overlap = settings.value(SETTINGS + "reverse_overlap", False, type=bool)
        self.component = settings.value(SETTINGS + "reverse_component", False, type=bool)
        self.recalc_cogo = settings.value(SETTINGS + "reverse_cogo", True, type=bool)
        self._arrows = None
        self.tool_tile = None
        super().__init__(canvas, iface, select_tool)

    def reset(self):
        self.clear_arrows()
        super().reset()

    def dispose(self):
        if self._arrows is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(self._arrows)
            self._arrows = None
        super().dispose()

    def deactivate(self):
        self.clear_arrows()
        super().deactivate()

    # ------------------------------------------------------------------ page
    def build_page(self, box):
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "reverse_direction.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Reverse Direction")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip(self.HINT)
        tile.clicked.connect(lambda _c=False: tile.setChecked(True))
        self.tool_tile = tile
        box.addWidget(tile)
        group = QButtonGroup(tile)
        self.topmost_radio = QRadioButton("Reverse topmost line")
        self.topmost_radio.setToolTip("Reverse the line on top where lines overlap")
        self.overlap_radio = QRadioButton("Choose from overlapping lines")
        self.overlap_radio.setToolTip("Where lines overlap, pick which one to reverse from a list")
        for radio in (self.topmost_radio, self.overlap_radio):
            group.addButton(radio)
        self.overlap_radio.setChecked(self.overlap)
        self.topmost_radio.setChecked(not self.overlap)
        self.topmost_radio.toggled.connect(lambda on: self.set_overlap(not on))
        self.component_check = check("Choose component part of a multipart line", self.component, self.set_component)
        self.component_check.setToolTip("Reverse only the part of a multipart line you click, not all of its parts")
        indent = QHBoxLayout()
        indent.setContentsMargins(24, 0, 0, 0)
        indent.addWidget(self.component_check)
        self.cogo_check = check("Recalculate COGO Direction attribute", self.recalc_cogo, self.set_recalc)
        self.cogo_check.setToolTip("After reversing, rewrite the line's COGO fields from its new direction")
        box.addWidget(self.topmost_radio)
        box.addLayout(indent)
        box.addWidget(self.overlap_radio)
        box.addWidget(self.cogo_check)
        self.component_check.setEnabled(not self.overlap)

    def set_overlap(self, on):
        self.overlap = bool(on)
        QgsSettings().setValue(SETTINGS + "reverse_overlap", self.overlap)
        if getattr(self, "component_check", None) is not None:
            self.component_check.setEnabled(not self.overlap)

    def set_component(self, on):
        self.component = bool(on)
        QgsSettings().setValue(SETTINGS + "reverse_component", self.component)

    def set_recalc(self, on):
        self.recalc_cogo = bool(on)
        QgsSettings().setValue(SETTINGS + "reverse_cogo", self.recalc_cogo)

    # ------------------------------------------------------------------ arrows
    def clear_arrows(self):
        if self._arrows is not None:
            self._arrows.reset(Qgis.GeometryType.Polygon)
        QToolTip.hideText()

    def _hits(self, point):
        return pick_features(self.canvas(), point, (LINE,), editable_only=True)

    def _near(self, geom, point):
        """The point that picks one part of a multi-part line, when that is asked for."""
        return point if self.component and not self.overlap and geom.isMultipart() else None

    def show_arrows(self, hit, point):
        layer, fid, geom = hit
        if self._arrows is None:
            self._arrows = QgsRubberBand(self.canvas(), Qgis.GeometryType.Polygon)
            self._arrows.setColor(QColor(0, 0, 0))
            self._arrows.setFillColor(QColor(0, 0, 0))
            self._arrows.setWidth(1)
        self._arrows.reset(Qgis.GeometryType.Polygon)
        mupp = self.canvas().mapUnitsPerPixel()
        for tri in G.direction_arrows(geom, self.ARROW_SPACING_PX * mupp, self.ARROW_SIZE_PX * mupp,
                                      self._near(geom, point)):
            self._arrows.addGeometry(QgsGeometry.fromPolygonXY([tri + [tri[0]]]), QgsCoordinateReferenceSystem(), False)
        self._arrows.updatePosition()
        self._arrows.update()
        QToolTip.showText(QCursor.pos(), f"Reverse direction of {fid if fid >= 0 else 'new feature'}", self.canvas())

    def on_move(self, point):
        hits = self._hits(point)
        if not hits:
            self.clear_arrows()
            return
        self.show_arrows(hits[0], point)

    # ------------------------------------------------------------------ the click
    def on_click(self, point):
        hits = self._hits(point)
        if not hits:
            self.say("no editable line there.")
            return
        chosen = hits[0]
        if self.overlap and len(hits) > 1:
            chosen = self._choose(hits)
            if chosen is None:
                return
        return self.reverse(chosen, point)

    def _choose(self, hits):
        menu = QMenu(self.canvas())
        actions = {}
        for hit in hits:
            action = menu.addAction(f"{hit[0].name()} : {hit[1] if hit[1] >= 0 else 'new feature'}")
            actions[action] = hit
        picked = (getattr(menu, "exec", None) or getattr(menu, "exec_"))(QCursor.pos())
        return actions.get(picked)

    def reverse(self, hit, point):
        layer, fid, geom = hit
        new = G.reversed_geometry(geom, self._near(geom, point))
        if new is None:
            return False
        failed = commit(self.canvas(), [(layer, fid, new)], [], "Reverse line direction")
        if failed:
            self.say(f"could not edit {', '.join(failed)} - is it in edit mode?")
            return False
        if self.recalc_cogo:
            recalculate_cogo(layer, fid)
        self.say(f"Reversed the direction of {fid if fid >= 0 else 'the new feature'}. (Ctrl+Z undoes it)")
        again = [h for h in self._hits(point) if (h[0].id(), h[1]) == (layer.id(), fid)]
        if again:
            self.show_arrows(again[0], point)                   # the arrows now point the other way
        return True


TOOL_CLASSES = {cls.KEY: cls for cls in (ReshapeTool, ReplaceGeometryTool, ExtendTrimTool, FilletTool,
                                         LineIntersectionTool, ReverseDirectionTool, EditVerticesTool)}


# ---------------------------------------------------------------------------------------- commands
def _need_selection(ctl, label):
    if editable_selection(ctl.iface.mapCanvas()):
        return True
    ctl.iface.messageBar().pushInfo(label, "Select features first (KGA Editor Select tool), then click this again.")
    return False


def generalize(ctl, tolerance=None):
    """Remove vertices from the selected lines and polygons, keeping every point within a tolerance of the original."""
    canvas = ctl.iface.mapCanvas()
    if not _need_selection(ctl, "Generalize"):
        return None
    settings = QgsSettings()
    if tolerance is None:
        tolerance = float(settings.value(SETTINGS + "generalize_tolerance", 1.0))
    settings.setValue(SETTINGS + "generalize_tolerance", tolerance)
    counts = [0, 0]

    def fn(g):
        if g.type() == Qgis.GeometryType.Point:
            return None
        out = g.simplify(tolerance)
        if out is None or out.isNull() or out.isEmpty():
            return None
        counts[0] += sum(1 for _ in g.vertices())
        counts[1] += sum(1 for _ in out.vertices())
        return out

    changed, layers, skipped = apply_to_selection(canvas, fn, "Generalize features")
    text = (f"Generalized {changed} feature(s): {counts[0]} -> {counts[1]} vertices." if changed else
            "Nothing to remove at that tolerance (points are left alone).")
    if skipped:
        text += f" Not editable, left alone: {', '.join(skipped)}."
    _status(ctl.iface, "Generalize: " + text, 8000)
    return changed, counts


def _copyable(layer, attributes):
    """The attribute values a new part may copy: not the primary key or unique fields, which get fresh values."""
    skip = set(layer.primaryKeyAttributes())
    skip.update(i for i in range(len(attributes))
                if layer.fieldConstraints(i) & enum_value((QgsFieldConstraints, 'Constraint.ConstraintUnique'),
                                                          (QgsFieldConstraints, 'ConstraintUnique')))
    return {i: v for i, v in enumerate(attributes) if i not in skip}


def explode_selection(canvas, keep_original=False):
    """
    Break every selected multi-part feature of the editable layers shown on the map into single parts: the
    first part stays in the original feature, each further part becomes a new feature with the same attributes
    (primary key and unique fields get fresh values). With `keep_original` the original features stay as they are
    and every part becomes a new feature. Returns (features exploded, new features, skipped).
    """
    exploded, created, skipped = 0, 0, []
    for layer, ids in editable_selection(canvas):
        if not QgsWkbTypes.isMultiType(layer.wkbType()):
            continue                                    # a single-part layer cannot hold multi-part features
        layer.beginEditCommand("Explode multi-part features")
        ok, new_ids, count, layer_created = True, [], 0, 0
        for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(ids)):
            g = f.geometry()
            if g.isEmpty() or not g.isMultipart() or g.constGet().partCount() < 2:
                continue
            parts = g.asGeometryCollection()
            for part in parts:
                part.convertToMultiType()               # the layer is multi-part: one part per feature
            if keep_original:
                new_parts = parts
            else:
                ok = layer.changeGeometry(f.id(), parts[0])
                new_parts = parts[1:]
            attrs = _copyable(layer, f.attributes())
            for part in new_parts:
                if not ok:
                    break
                new = QgsVectorLayerUtils.createFeature(layer, part, attrs)
                ok = layer.addFeature(new)
                new_ids.append(new.id())
                layer_created += 1
            if not ok:
                break
            count += 1
        if ok:
            layer.endEditCommand()
            exploded += count
            created += layer_created
            if new_ids:
                layer.selectByIds(list(ids) + new_ids)
        else:
            layer.destroyEditCommand()
            skipped.append(layer.name())
        layer.triggerRepaint()
    return exploded, created, skipped


COMMAND_PAGES = {"generalize": GeneralizePage, "change_line_length": ChangeLineLengthPage}

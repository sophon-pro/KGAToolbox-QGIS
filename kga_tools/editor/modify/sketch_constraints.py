"""
Parallel and perpendicular sketching for the tools that draw a line on the map (Reshape, Replace Geometry), like Pro's
sketch context menu. While a sketch has a point, right-click opens a menu: Finish Sketch, Parallel, Perpendicular, Clear
Constraint and Delete Sketch.

    Parallel / Perpendicular   the NEXT segment is locked to run parallel (or at right angles) to an edge. With the mouse
                               over an edge of any feature - or of the sketch itself - when you right-click, that edge is
                               used at once; otherwise hover and click the edge to follow (Esc cancels).
                               The sketch then follows the mouse along the locked direction (a thin cyan guide shows
                               it) and the next click places the point on it. The lock is used up by that click.

Keys while sketching: P = Parallel, R = Perpendicular (the same pick rules).
"""

import math
from contextlib import suppress

from qgis.core import Qgis, QgsPointXY
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QCursor
from qgis.PyQt.QtWidgets import QMenu

from . import geometry_ops as G
from .pick import pick_features

LINE = Qgis.GeometryType.Line
GUIDE = QColor(0, 200, 220, 200)
REFERENCE = QColor(0, 121, 193, 230)
KINDS = {"parallel": "Parallel", "perpendicular": "Perpendicular"}


def _outlines(geometry):
    """The point lists of a line's parts or of a polygon's rings."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []
    if geometry.type() == LINE:
        parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
        return [list(p) for p in parts if len(p) > 1]
    if geometry.type() == Qgis.GeometryType.Polygon:
        polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
        return [list(ring) for polygon in polygons for ring in polygon if len(ring) > 1]
    return []


def edge_near(canvas, point, sketch=()):
    """(start, end) of the edge of a feature (or of the sketch) nearest `point` within the click tolerance, or None."""
    best = None
    for _layer, _fid, geometry in pick_features(canvas, point):
        for pts in _outlines(geometry):
            index, near = G.nearest_segment(pts, point)
            distance = math.hypot(near.x() - point.x(), near.y() - point.y())
            if best is None or distance < best[0]:
                best = (distance, pts[index], pts[index + 1])
    tolerance = 8 * canvas.mapUnitsPerPixel()
    pts = list(sketch)
    if len(pts) > 1:
        index, near = G.nearest_segment(pts, point)
        distance = math.hypot(near.x() - point.x(), near.y() - point.y())
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, pts[index], pts[index + 1])
    return None if best is None else (QgsPointXY(best[1]), QgsPointXY(best[2]))


def locked_direction(kind, edge):
    """The unit direction the next segment must follow: along the edge, or at right angles to it."""
    d = G._unit(G._sub(edge[1], edge[0]))
    if d is None:
        return None
    return d if kind == "parallel" else (-d[1], d[0])


def project_on_line(origin, direction, point):
    """The point of the line through `origin` along `direction` nearest `point`."""
    k = (point.x() - origin.x()) * direction[0] + (point.y() - origin.y()) * direction[1]
    return QgsPointXY(origin.x() + direction[0] * k, origin.y() + direction[1] * k)


class SketchConstraints:
    """Mixin for a PickTool that keeps its sketch in `self.points` and redraws it with `_draw(tail)`."""

    _lock = None                # (kind, unit direction) once an edge has been chosen
    _picking = None             # "parallel" | "perpendicular" while the edge to follow is still to be clicked
    _ref_band = None
    _guide_band = None

    # ------------------------------------------------------------------ state
    def constraint_active(self):
        return self._lock is not None or self._picking is not None

    def clear_constraint(self):
        self._lock = None
        self._picking = None
        for band in (self._ref_band, self._guide_band):
            if band is not None:
                band.reset(LINE)

    def dispose_constraint_bands(self):
        for name in ("_ref_band", "_guide_band"):
            band = getattr(self, name)
            if band is not None:
                with suppress(Exception):
                    self.canvas().scene().removeItem(band)
                setattr(self, name, None)

    def constrained(self, point):
        """`point`, moved onto the locked direction from the last sketch point when a lock is on."""
        if self._lock is None or not self.points:
            return point
        return project_on_line(self.points[-1], self._lock[1], point)

    def _band(self, name, colour, width, dashed=False):
        band = getattr(self, name)
        if band is None:
            band = QgsRubberBand(self.canvas(), LINE)
            band.setColor(colour)
            band.setWidth(width)
            if dashed:
                band.setLineStyle(Qt.PenStyle.DashLine)
            setattr(self, name, band)
        band.reset(LINE)
        return band

    def _show_edge(self, edge):
        band = self._band("_ref_band", REFERENCE, 4)
        if edge is not None:
            for i, p in enumerate(edge):
                band.addPoint(p, i == len(edge) - 1)

    def _show_guide(self):
        band = self._band("_guide_band", GUIDE, 1, dashed=True)
        if self._lock is None or not self.points:
            return
        origin, d = self.points[-1], self._lock[1]
        reach = 4000 * self.canvas().mapUnitsPerPixel()
        for i, p in enumerate((QgsPointXY(origin.x() - d[0] * reach, origin.y() - d[1] * reach),
                               QgsPointXY(origin.x() + d[0] * reach, origin.y() + d[1] * reach))):
            band.addPoint(p, i == 1)

    # ------------------------------------------------------------------ choosing the edge
    def begin_constraint(self, kind, edge=None):
        """Lock the next segment parallel / perpendicular to `edge`, or wait for the edge to be clicked."""
        if not self.points:
            self.say("place the first point before using Parallel or Perpendicular.")
            return False
        if edge is None:
            self._lock = None
            self._picking = kind
            self._show_guide()
            self.say(f"click the edge the next segment should be {KINDS[kind].lower()} to (Esc cancels).")
            return True
        return self._lock_to(kind, edge)

    def _lock_to(self, kind, edge):
        direction = locked_direction(kind, edge)
        if direction is None:
            self.say("that edge has no direction.")
            return False
        self._picking = None
        self._lock = (kind, direction)
        self._show_edge(edge)
        self._show_guide()
        self.say(f"{KINDS[kind]} locked - click to place the next point (Esc clears it).")
        return True

    def constraint_move(self, point):
        """While the edge is being picked: highlight the edge under the mouse. True when the move was used."""
        if self._picking is None:
            return False
        self._show_edge(edge_near(self.canvas(), point, self.points))
        return True

    def constraint_click(self, point):
        """While the edge is being picked: take the edge under the click. True when the click was used."""
        if self._picking is None:
            return False
        edge = edge_near(self.canvas(), point, self.points)
        if edge is None:
            self.say("no edge there - click an edge of a feature or of the sketch.")
        else:
            self._lock_to(self._picking, edge)
        return True

    # ------------------------------------------------------------------ the menu and keys
    def exec_menu(self, menu):
        return (getattr(menu, "exec", None) or getattr(menu, "exec_"))(QCursor.pos())

    def sketch_menu(self, point):
        """The right-click menu of a sketch in progress; `point` is where the mouse is on the map."""
        menu = QMenu(self.canvas())
        actions = {}
        actions["finish"] = menu.addAction("Finish Sketch\tEnter")
        menu.addSeparator()
        actions["parallel"] = menu.addAction("Parallel\tP")
        actions["perpendicular"] = menu.addAction("Perpendicular\tR")
        actions["clear"] = menu.addAction("Clear Constraint")
        actions["clear"].setEnabled(self.constraint_active())
        menu.addSeparator()
        actions["delete"] = menu.addAction("Delete Sketch\tEsc")
        picked = self.exec_menu(menu)
        menu.deleteLater()
        for key, action in actions.items():
            if action is picked:
                self._menu_choice(key, point)
                return key
        return None

    def _menu_choice(self, key, point):
        if key == "finish":
            self.clear_constraint()
            self.finish()
        elif key in KINDS:
            self.begin_constraint(key, edge_near(self.canvas(), point, self.points))
        elif key == "clear":
            self.clear_constraint()
            self._draw(None)
        elif key == "delete":
            self.clear_constraint()
            self.reset()

    def constraint_key(self, e):
        """P / R while sketching and Esc to drop a lock. True when the key was used."""
        if e.key() == Qt.Key.Key_Escape and self.constraint_active():
            self.clear_constraint()
            self._draw(None)
            return True
        if self.points and e.key() in (Qt.Key.Key_P, Qt.Key.Key_R) and not e.modifiers():
            kind = "parallel" if e.key() == Qt.Key.Key_P else "perpendicular"
            pixel = self.canvas().mouseLastXY()
            point = QgsPointXY(self.canvas().getCoordinateTransform().toMapCoordinates(pixel.x(), pixel.y()))
            edge = edge_near(self.canvas(), point, self.points)
            self.begin_constraint(kind, edge)
            return True
        return False

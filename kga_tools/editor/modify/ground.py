"""
Lengths and areas on the ground, for the tools that balance or target a measured amount (Divide, Change Line Length).

What the attribute table and $area / $length report is measured on the project's ellipsoid (Project Properties >
General). In a projected CRS that differs from the flat figure by the scale distortion of the projection (about 0.1 %
in a UTM zone away from its central meridian), so parts that are equal FLAT are not equal as reported. With an
ellipsoid set these measures give the ground figure; with none they give the flat one in the map's units.

    AreaMeasure     area(geometry), .unit (the area unit the numbers are in)
    LengthMeasure   length(geometry), .unit, and the mapping between a distance along a line on the ground and the flat
                    distance along it that the geometry functions (curveSubstring, interpolate) use
"""

import math
from bisect import bisect_right

from qgis.core import QgsDistanceArea, QgsProject, QgsUnitTypes, QgsWkbTypes


def _distance_area(canvas):
    """(QgsDistanceArea, ellipsoid name) when the project has an ellipsoid and the map a valid CRS, else (None, "")."""
    if canvas is None:
        return None, ""
    project = QgsProject.instance()
    crs = canvas.mapSettings().destinationCrs()
    ellipsoid = project.ellipsoid()
    if not crs.isValid() or not ellipsoid or ellipsoid in ("NONE", ""):
        return None, ""
    da = QgsDistanceArea()
    da.setSourceCrs(crs, project.transformContext())
    return (da, ellipsoid) if da.setEllipsoid(ellipsoid) else (None, "")


class AreaMeasure:
    """How the areas of the parts are measured: on the ground when the project has an ellipsoid, else flat."""

    def __init__(self, canvas=None):
        self._da, self.name = _distance_area(canvas)
        self.ellipsoidal = self._da is not None
        self.unit = None                                      # the area unit the numbers come in
        if self.ellipsoidal:
            self.unit = self._da.areaUnits()
        elif canvas is not None and canvas.mapSettings().destinationCrs().isValid():
            self.unit = QgsUnitTypes.distanceToAreaUnit(canvas.mapSettings().destinationCrs().mapUnits())

    def __call__(self, geom):
        if geom is None or geom.isNull() or geom.isEmpty():
            return 0.0
        return self._da.measureArea(geom) if self._da is not None else geom.area()


class LengthMeasure:
    """How lengths are measured: on the ground when the project has an ellipsoid, else flat in the map's units."""

    def __init__(self, canvas=None):
        self._da, self.name = _distance_area(canvas)
        self.ellipsoidal = self._da is not None
        self.unit = None
        if self.ellipsoidal:
            self.unit = self._da.lengthUnits()
        elif canvas is not None and canvas.mapSettings().destinationCrs().isValid():
            self.unit = canvas.mapSettings().destinationCrs().mapUnits()

    def usable_for(self, geom):
        """True when ground distances can be mapped onto this line (an ellipsoid, and no true curves)."""
        return (self.ellipsoidal and geom is not None and not geom.isNull()
                and not QgsWkbTypes.isCurvedType(geom.wkbType()))

    def length(self, geom):
        if geom is None or geom.isNull() or geom.isEmpty():
            return 0.0
        return self._da.measureLength(geom) if self.usable_for(geom) else geom.length()

    def points_length(self, points):
        """The length of a polyline given as QgsPointXY."""
        if len(points) < 2:
            return 0.0
        if self._da is not None:
            return sum(self._da.measureLine(a, b) for a, b in zip(points, points[1:]))
        return sum(math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(points, points[1:]))

    def profile(self, points):
        """([flat distance at each vertex], [ground distance at each vertex]) of a polyline."""
        flat, ground = [0.0], [0.0]
        for a, b in zip(points, points[1:]):
            flat.append(flat[-1] + math.hypot(b.x() - a.x(), b.y() - a.y()))
            ground.append(ground[-1] + (self._da.measureLine(a, b) if self._da is not None else flat[-1] - flat[-2]))
        return flat, ground

    @staticmethod
    def _interpolate(xs, ys, x):
        """y at x along the piecewise linear line through (xs, ys); beyond the ends the end segment carries on."""
        if len(xs) < 2 or xs[-1] - xs[0] <= 0:
            return x
        i = min(max(bisect_right(xs, x) - 1, 0), len(xs) - 2)
        span = xs[i + 1] - xs[i]
        if span <= 0:
            return ys[i]
        return ys[i] + (x - xs[i]) * (ys[i + 1] - ys[i]) / span

    def flat_at(self, points, ground_distance):
        """The flat distance along the polyline that is `ground_distance` along it on the ground."""
        if self._da is None:
            return ground_distance
        flat, ground = self.profile(points)
        return self._interpolate(ground, flat, ground_distance)

    def flats_at(self, points, ground_distances, profile=None):
        """flat_at for many distances at once: the profile of the line is worked out once (or passed in)."""
        if self._da is None:
            return list(ground_distances)
        flat, ground = profile or self.profile(points)
        return [self._interpolate(ground, flat, g) for g in ground_distances]

    def ground_at(self, points, flat_distance):
        if self._da is None:
            return flat_distance
        flat, ground = self.profile(points)
        return self._interpolate(flat, ground, flat_distance)

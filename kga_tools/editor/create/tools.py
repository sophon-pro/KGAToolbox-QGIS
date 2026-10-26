"""The construction tools offered for each geometry type in the Create Features pane."""

import os

from qgis.PyQt.QtGui import QIcon

from .templates import GEOM_LINE, GEOM_POINT, GEOM_POLYGON

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

# id, label, icon, tooltip
_POINT = ("point", "Point", "point.svg",
          "Point: click to place a point. Right-click for Absolute X,Y, Direction/Distance and Delta X,Y.")
_LINE = ("line", "Line", "line.svg",
         "Line: click the vertices. Right-click for parallel, perpendicular, direction, distance... "
         "Double-click, Enter or F2 finishes.")
_RIGHT_ANGLE_LINE = ("right_angle", "Right Angle Line", "right_angle_line.svg",
                     "Right Angle Line: every segment after the first turns exactly 90 degrees.")
_FREEHAND_LINE = ("freehand", "Freehand Line", "freehand.svg",
                  "Freehand Line: press and drag, release to finish.")
_POLYGON = ("polygon", "Polygon", "polygon.svg",
            "Polygon: click the vertices. Right-click for parallel, perpendicular, direction, distance... "
            "Double-click, Enter or F2 finishes.")
_RECTANGLE = ("rectangle", "Rectangle", "rectangle.svg",
              "Rectangle: click one corner, then the opposite corner. Right-click for Delta X,Y.")
_RIGHT_ANGLE_POLYGON = ("right_angle", "Right Angle Polygon", "right_angle_polygon.svg",
                        "Right Angle Polygon: every segment after the first turns exactly 90 degrees. "
                        "Right-click > Square and Finish closes it with a right angle.")
_CIRCLE = ("circle", "Circle", "circle.svg",
           "Circle: click the centre, then a point on the edge. Right-click > Distance sets the radius.")
_ELLIPSE = ("ellipse", "Ellipse", "ellipse.svg",
            "Ellipse: click the centre, the end of the major axis, then the minor axis.")
_FREEHAND_POLYGON = ("freehand", "Freehand Polygon", "freehand.svg",
                     "Freehand Polygon: press and drag, release to close the shape.")

TOOLS = {
    GEOM_POINT: (_POINT,),
    GEOM_LINE: (_LINE, _RIGHT_ANGLE_LINE, _FREEHAND_LINE),
    GEOM_POLYGON: (_POLYGON, _RECTANGLE, _RIGHT_ANGLE_POLYGON, _CIRCLE, _ELLIPSE, _FREEHAND_POLYGON),
}


def tools_for(geometry_type):
    """[(id, label, icon, tooltip)] for the geometry type."""
    return [(i, label, QIcon(os.path.join(ICON_DIR, icon)), tip) for i, label, icon, tip in TOOLS[geometry_type]]


def tool_ids(geometry_type):
    return [t[0] for t in TOOLS[geometry_type]]


def default_tool(geometry_type, wanted=""):
    ids = tool_ids(geometry_type)
    return wanted if wanted in ids else ids[0]

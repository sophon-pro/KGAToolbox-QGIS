# -*- coding: utf-8 -*-

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt

from ..branding import docs_url
from ..core.compat import no_threading

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: Kept alive between runs so the window is not garbage collected the moment
#: `processAlgorithm` returns. Re-running while it is open raises it.
DIALOG_INSTANCE = None


def _alive(dialog):
    try:
        return dialog is not None and not dialog._torn_down
    except RuntimeError:                    # the Qt object is already gone
        return False


class StripPointGridAlgorithm(QgsProcessingAlgorithm):
    """Open the Strip Lines and Point Grid window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaStripPointGrid', string)

    def createInstance(self):
        return StripPointGridAlgorithm()

    def name(self):
        return 'strip_point_grid'

    def displayName(self):
        return self.tr('Strip Lines and Point Grid')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def tags(self):
        return self.tr('strip,lines,transect,grid,points,sampling,spacing,'
                       'survey,plot,parallel,regular').split(',')

    def helpUrl(self):
        return docs_url('strip_point_grid')

    def shortHelpString(self):
        return self.tr(
            'Lay <b>parallel strip lines</b> or a <b>regular point grid</b> '
            'over an area, with a few settings.\n\n'
            'Choose the area as the current map extent, a rectangle or '
            'polygon drawn on the map, a layer\'s extent, or a polygon '
            'layer\'s boundary. Lines and points are cut to the area, so a '
            'polygon gives lines and points that stop at its edge.\n\n'
            '<b>Strip lines</b> can be equally spaced (every 50 m) or follow '
            'a pattern of gaps that repeats: 10, 20, 30 gives gaps of 10, '
            '20, 30, 10, 20, 30 and so on. <b>Point grid</b> takes the '
            'distance between points, and optionally a different distance '
            'between rows. <b>Shape grid</b> fills the area with squares, '
            'rectangles, hexagons or triangles that fit edge to edge, or '
            'regular polygons of any number of sides, and can add a centre '
            'point for every cell.\n\n'
            '<b>Direction</b> is vertical, horizontal or any angle (0 is '
            'north-south, 90 is east-west). <b>First one</b> sets whether '
            'the first line or point sits one spacing in, half a spacing in, '
            'or on the edge.\n\n'
            'Distances are metres. The result is made in the UTM zone under '
            'the area when the map is in degrees or Web Mercator, so 50 m '
            'really is 50 m. The result is a temporary layer, or a layer in '
            'a GeoPackage.\n\n'
            '<b>Watch out for:</b> a very small spacing over a large area '
            'makes a huge layer; the tool stops at one million features.\n\n'
            'The window opens modelessly: you can close this Processing '
            'dialog and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.strip_grid_dialog import StripGridDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = StripGridDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_strip_point_grid():
    """Close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
        dialog.deleteLater()
    except RuntimeError:                    # already destroyed by Qt
        pass

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


class PointToGeometryAlgorithm(QgsProcessingAlgorithm):
    """Open the Point to Geometry Converter window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaPointToGeometry', string)

    def createInstance(self):
        return PointToGeometryAlgorithm()

    def name(self):
        return 'point_to_geometry'

    def displayName(self):
        return self.tr('Point to Geometry Converter')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def tags(self):
        return self.tr('points,line,polygon,polyline,segments,boundary,gps,'
                       'track,survey,traverse,order,connect,join').split(',')

    def helpUrl(self):
        return docs_url('point_to_geometry')

    def shortHelpString(self):
        return self.tr(
            'Build <b>lines</b> or <b>polygons</b> from an ordered point '
            'layer, without digitizing.\n\n'
            '<b>Order by</b> feature ID, a field or any QGIS expression '
            '(optionally descending). Ties are broken by feature ID; points '
            'with a NULL order value are left out and listed.\n'
            '<b>Group by</b> (optional): each value becomes its own feature.\n\n'
            '<b>Line</b>: one continuous polyline per group, or individual '
            'segments (one line per consecutive pair, with seg_seq, from_fid '
            'and to_fid).\n'
            '<b>Polygon</b>: the ring is closed for you. Write the polygon, '
            'the polygon plus a matching boundary line layer, or the closed '
            'boundary line only.\n\n'
            'Geometries are validated and self-intersecting polygons can be '
            'repaired; nothing is dropped silently - every skipped, excluded '
            'or repaired feature is in the Report tab. The run is a background '
            'task you can stop. Output to GeoPackage (the project database by '
            'default), Shapefile or a temporary layer; Z and M are kept.\n\n'
            '<b>Watch out for:</b> text order fields sort alphabetically '
            "('10' before '2'); use Expression with to_real(\"field\"). "
            'Polygons are single rings: holes are not made from points.\n\n'
            'The window opens modelessly: you can close this Processing '
            'dialog and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.point_geometry_dialog import PointToGeometryDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = PointToGeometryDialog(
                iface.mainWindow() if iface else None)
        else:
            DIALOG_INSTANCE.refresh_layers()
        if iface is not None:
            DIALOG_INSTANCE.set_layer(iface.activeLayer())
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_point_to_geometry():
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

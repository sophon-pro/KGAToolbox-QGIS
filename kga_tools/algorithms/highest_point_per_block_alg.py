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


class HighestPointPerBlockAlgorithm(QgsProcessingAlgorithm):
    """Open the Paddy Irrigation Tools window (step 1: high points, step 2: gravity command)."""

    def tr(self, string):
        return QCoreApplication.translate('KgaHighestPointPerBlock', string)

    def createInstance(self):
        return HighestPointPerBlockAlgorithm()

    def name(self):
        return 'highest_point_per_block'

    def displayName(self):
        return self.tr('Paddy Irrigation Tools')

    def group(self):
        return 'KGA Irrigation Tools'

    def groupId(self):
        return 'kgairrigationtools'

    def helpUrl(self):
        return docs_url('highest_point_per_block')

    def shortHelpString(self):
        return self.tr(
            'Find the <b>highest point</b> (and optionally the lowest) of '
            'every block - a polygon or a line - from a DEM, and put it on '
            'the map as a point with its elevation, X/Y, longitude/latitude '
            'and the block attributes you choose.\n\n'
            'For polygons the default method finds the <b>top of the general '
            'slope</b>: trees, dikes and huts are filtered out of the DEM and '
            'the surface is smoothed, so the point marks the high side of the '
            'field, and each block also gets <i>Slope_pct</i>, <i>Fall_m</i> '
            'and <i>Flow_dir</i>. Choose <i>Highest DEM cell</i> for the raw '
            'peak.\n\n'
            'Tick any number of polygon and line layers; each feature gets '
            'its own point, or features sharing a field value (for example a '
            'BM name) get one point together. Polygons count the DEM cells '
            'whose centre is inside; lines count every cell they cross and '
            'the point is placed on the line. <i>Top_cells</i> above 1 means '
            'a flat top.\n\n'
            'You can resample the DEM first (0.5 m, 1 m, 30 m or custom, '
            'using Maximum so peaks survive), ignore dikes and roads with an '
            'inner buffer, clean the blocks on a working copy (snap, '
            'overlaps, gaps, reported in a <i>Topology_issues</i> layer), '
            'write the results back into the block layers, and save to '
            'GeoPackage, Shapefile or Excel.\n\n'
            '<b>Watch out for:</b> a DEM in degrees (reproject it to a metre '
            'CRS first) and a very fine DEM over many blocks, which is slow. '
            'Your block layers are not changed unless you choose to write the '
            'results into them.\n\n'
            '<b>Step 2 - Gravity command</b> (second tab) then checks whether '
            'every block can be irrigated by gravity - Field, tertiary, '
            'secondary and main canal, reservoir: plot-to-plot supply, '
            'turnouts, the water level each canal needs (DESIGN) or a check '
            'of your canal levels (CHECK).\n\n'
            'The window opens modelessly: you can close this Processing '
            'dialog and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.block_elevation_dialog import BlockElevationDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = BlockElevationDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_highest_point_per_block():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

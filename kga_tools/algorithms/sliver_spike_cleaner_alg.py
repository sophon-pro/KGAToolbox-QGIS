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


class SliverSpikeCleanerAlgorithm(QgsProcessingAlgorithm):
    """Open the Sliver and Spike Cleaner window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaSliverSpikeCleaner', string)

    def createInstance(self):
        return SliverSpikeCleanerAlgorithm()

    def name(self):
        return 'sliver_spike_cleaner'

    def displayName(self):
        return self.tr('Sliver and Spike Cleaner')

    def group(self):
        return self.tr('KGA Geometry Utilities')

    def groupId(self):
        return 'kgageometryutilities'

    def helpUrl(self):
        return docs_url('sliver_spike_cleaner')

    def shortHelpString(self):
        return self.tr(
            'Clean the <b>spikes, wedges, narrow strips and slivers</b> left '
            'after polygons are split with lines.\n\n'
            'Choose a polygon layer and a strength (Light 1 m, Standard 2 m, '
            'Strong 3 m), then <i>Clean polygons</i>. In order, the tool '
            'repairs invalid geometry, trims sharp spikes square along the '
            'corner edge, removes any part narrower than the chosen width '
            '(however long it is), removes tiny parts and holes, and snaps '
            'the result back to the original vertices so shared edges stay '
            'identical.\n\n'
            'The input layer is never changed. The result is a new layer '
            '(temporary or saved to a GeoPackage / Shapefile) with check '
            'fields (cleaned, area_old, area_new, area_rem) and an optional '
            '"_removed" layer showing exactly what was cut.\n\n'
            '<b>Watch out for:</b> widths and areas are in layer units, so '
            'use a projected CRS (metres), not degrees. Real narrow features '
            '(bunds, canals, roads) are narrow too: run once with "Keep and '
            'flag", check them, then use "Delete".\n\n'
            'The window opens modelessly: you can close this Processing '
            'dialog and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.sliver_spike_dialog import CleanSpikesDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = CleanSpikesDialog(
                iface.mainWindow() if iface else None)
        else:
            DIALOG_INSTANCE.refresh_layers()
            if iface and iface.activeLayer() is not None:
                DIALOG_INSTANCE.cmb_layer.setLayer(iface.activeLayer())
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_sliver_spike_cleaner():
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

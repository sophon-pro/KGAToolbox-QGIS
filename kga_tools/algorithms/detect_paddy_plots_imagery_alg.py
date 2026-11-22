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


class DetectPaddyPlotsImageryAlgorithm(QgsProcessingAlgorithm):
    """Open the Detect Paddy Field Plots from Imagery window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaDetectPaddyPlots', string)

    def createInstance(self):
        return DetectPaddyPlotsImageryAlgorithm()

    def name(self):
        return 'detect_paddy_plots_imagery'

    def displayName(self):
        return self.tr('Detect Paddy Field Plots from Imagery')

    def group(self):
        return 'KGA GeoAI'

    def groupId(self):
        return 'kgageoai'

    def helpUrl(self):
        return docs_url('detect_paddy_plots_imagery')

    def shortHelpString(self):
        return self.tr(
            'Outline <b>paddy field plots</b> in satellite, aerial or drone '
            'imagery with an open deep-learning model, and get one '
            '<b>polygon per plot</b> (area in m² and ha) and the <b>plot '
            'boundaries</b> (bund lines) between them.\n\n'
            'The imagery can be any raster layer: an XYZ, WMS, WMTS or ArcGIS '
            'tile service, a satellite GeoTIFF or a drone orthomosaic. It is '
            'read as it shows on the map, at 0.5, 1 or 2 m per pixel. Choose '
            'the area as the map extent, a layer\'s extent, a polygon '
            'layer\'s boundary, or a rectangle or polygon drawn on the '
            'map.\n\n'
            'Two models: Delineate Anything S (12 MB, fast) and Delineate '
            'Anything v2 (248 MB, slower); neither is best everywhere, so try '
            'both on a small area first. The first time, the window '
            'installs the AI runtime (onnxruntime, about 40 MB, shared with '
            'Detect Roads) and downloads the chosen model into your QGIS '
            'profile, on request. Plots can be kept only where ESA WorldCover '
            'shows farmland, which drops most ponds, yards and woodland.\n\n'
            '<b>Watch out for:</b> the model outlines every kind of field, '
            'not only rice; neighbouring plots that look alike can come out '
            'as one; ponds and village yards are sometimes taken for plots. '
            'Treat the result as a draft to check and edit. Check the imagery '
            'provider\'s terms before you share plots traced from it.\n\n'
            'Model: Delineate Anything by Lavreniuk et al. (AGPL-3.0), '
            'converted to ONNX.\n\n'
            'The window opens modelessly: you can close this Processing '
            'dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.paddy_detect_dialog import PaddyDetectDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = PaddyDetectDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_detect_paddy_plots_imagery():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

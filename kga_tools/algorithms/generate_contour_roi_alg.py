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


class GenerateContourRoiAlgorithm(QgsProcessingAlgorithm):
    """Open the Generate Contour for ROI window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaGenerateContourRoi', string)

    def createInstance(self):
        return GenerateContourRoiAlgorithm()

    def name(self):
        return 'generate_contour_roi'

    def displayName(self):
        return self.tr('Generate Contour for ROI')

    def group(self):
        return 'KGA Irrigation Tools'

    def groupId(self):
        return 'kgairrigationtools'

    def helpUrl(self):
        return docs_url('generate_contour_roi')

    def shortHelpString(self):
        return self.tr(
            'Make <b>contour lines</b> for any area of interest from the free '
            'Copernicus DEM (30 m or 90 m) - no account or API key.\n\n'
            'Choose the area as the current map extent, a layer\'s extent, a '
            'layer\'s boundary, or a rectangle or polygon drawn on the map. '
            'Set the contour interval and which contours are <i>main</i> '
            '(every Nth), and optionally smooth the lines. Main contours are '
            'drawn dark and thick, sub contours grey and thin, and labels go '
            'on the main contours only or on all of them.\n\n'
            'The DEM is downloaded and cut to the area, then the contours are '
            'traced from it. One progress bar covers all of it, and the job '
            'runs in the background so QGIS stays usable. The DEM (GeoTIFF) '
            'and contours (GeoPackage) are saved to the output folder; leave '
            'it empty for a temporary folder, which QGIS deletes when it '
            'closes.\n\n'
            'Tick <b>Terrain surfaces</b> to also derive a hillshade, slope '
            'and aspect from the same DEM.\n\n'
            '<b>Watch out for:</b> large areas. A big DEM is read and '
            'contoured in memory and can make a modest PC freeze, so the tool '
            'warns you first. The Copernicus DEM is a surface model: it '
            'follows tree tops and buildings, not bare ground.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.contour_roi_dialog import ContourRoiDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = ContourRoiDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_generate_contour_roi():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

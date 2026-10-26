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


class GenerateStreamRoiAlgorithm(QgsProcessingAlgorithm):
    """Open the Generate River or Stream for ROI window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaGenerateStreamRoi', string)

    def createInstance(self):
        return GenerateStreamRoiAlgorithm()

    def name(self):
        return 'generate_stream_roi'

    def displayName(self):
        return self.tr('Generate River or Stream for ROI')

    def group(self):
        return 'KGA Irrigation Tools'

    def groupId(self):
        return 'kgairrigationtools'

    def helpUrl(self):
        return docs_url('generate_stream_roi')

    def shortHelpString(self):
        return self.tr(
            'Extract the <b>river and stream network</b> for any area of '
            'interest from the free Copernicus DEM (30 m or 90 m) - no '
            'account or API key - or from a DEM you already have, as a '
            'project layer or a file.\n\n'
            'Choose the area as the current map extent, a layer\'s extent, a '
            'layer\'s boundary, or a rectangle or polygon drawn on the map. '
            'Choose how much stream you want: major rivers only, main '
            'streams, a detailed network, headwater gullies, a custom '
            'contributing area, or a target drainage density. Streams are '
            'numbered by Strahler order and drawn wider as they grow.\n\n'
            'The DEM is downloaded and cut to the area, depressions are '
            'filled, the water is routed downhill and the streams are traced '
            'from the flow accumulation. One progress bar covers all of it, '
            'and the job runs in the background so QGIS stays usable. The '
            'DEM (GeoTIFF), streams (GeoPackage) and a report are saved to '
            'the output folder; leave it empty for a temporary folder, which '
            'QGIS deletes when it closes.\n\n'
            '<b>Checking the result.</b> Every segment is graded High, Medium '
            'or Low confidence from the DEM alone (flat ground, filled '
            'depressions, no valley). You can also compare with '
            'OpenStreetMap rivers and streams or a line layer of your own: '
            'streams that only one network has are drawn on a separate '
            'check layer, and the report gives the agreement.\n\n'
            '<b>Watch out for:</b> large areas. Routing a big DEM needs a '
            'lot of memory and can make a modest PC freeze, so the tool '
            'warns you first. A DEM trace is a model of where water runs, '
            'not a survey: it is least reliable on flat ground, and it '
            'includes gullies that are dry most of the year.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.stream_roi_dialog import StreamRoiDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = StreamRoiDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_generate_stream_roi():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

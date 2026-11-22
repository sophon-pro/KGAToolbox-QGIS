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


class DetectRoadsImageryAlgorithm(QgsProcessingAlgorithm):
    """Open the Detect Roads from Imagery window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaDetectRoadsImagery', string)

    def createInstance(self):
        return DetectRoadsImageryAlgorithm()

    def name(self):
        return 'detect_roads_imagery'

    def displayName(self):
        return self.tr('Detect Roads from Imagery')

    def group(self):
        return 'KGA GeoAI'

    def groupId(self):
        return 'kgageoai'

    def helpUrl(self):
        return docs_url('detect_roads_imagery')

    def shortHelpString(self):
        return self.tr(
            'Detect <b>roads</b> in satellite, aerial or drone imagery with an '
            'open deep-learning model, and get them as <b>centerlines</b>, '
            '<b>road edges</b> and <b>road surface</b> polygons.\n\n'
            'The imagery can be any raster layer: an XYZ, WMS, WMTS or ArcGIS '
            'tile service, a satellite GeoTIFF or a drone orthomosaic. It is '
            'read as it shows on the map, at 0.3 m per pixel. Choose the area '
            'as the map extent, a layer\'s extent, a polygon layer\'s boundary, '
            'or a rectangle or polygon drawn on the map.\n\n'
            'The first time, the window installs the AI runtime (onnxruntime, '
            'about 40 MB) and downloads the model (about 98 MB) into your QGIS '
            'profile, on request. The job runs in the background; one '
            'progress bar covers reading the imagery, detecting, tracing and '
            'writing. The centerlines can be compared with OpenStreetMap or a '
            'line layer of your own.\n\n'
            '<b>Watch out for:</b> roads under trees, in shadow or narrower '
            'than about 3 m are often missed, and railways, bare plots and '
            'parking lots are sometimes taken for roads - treat the result as '
            'a draft to edit. Check the imagery provider\'s terms before you '
            'share roads traced from it.\n\n'
            'Model: "unet-roads", a fAIr base model by Rafael990 (CC BY-SA '
            '4.0), trained on SpaceNet 3, OpenAerialMap and OpenStreetMap.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.road_detect_dialog import RoadDetectDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = RoadDetectDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_detect_roads_imagery():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

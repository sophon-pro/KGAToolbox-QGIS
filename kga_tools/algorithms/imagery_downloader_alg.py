# -*- coding: utf-8 -*-

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt

from ..core.compat import no_threading
from ..branding import docs_url

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


class ImageryDownloaderAlgorithm(QgsProcessingAlgorithm):
    """Open the Imagery Downloader window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaImageryDownloader', string)

    def createInstance(self):
        return ImageryDownloaderAlgorithm()

    def name(self):
        return 'imagery_downloader'

    def displayName(self):
        return self.tr('Imagery Downloader')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('imagery_downloader')

    def shortHelpString(self):
        return self.tr(
            'Download web-map <b>imagery tiles</b> for an area, stitch them '
            'into one georeferenced image and add it to the map.\n\n'
            'Choose the area as the current map extent, a layer\'s extent, a '
            'box drawn on the map, or a polygon layer to clip to. Pick the '
            'zoom level, the output CRS (the project CRS by default) and the '
            'format (GeoTIFF, JPEG, PNG, JPEG 2000, or ECW where your GDAL '
            'build can write it).\n\n'
            'The details panel shows the tile count, image size, resolution '
            'and estimated download and file size before anything is '
            'downloaded. Large jobs run in the background and can be stopped '
            'and <b>resumed</b> later from the same folder: finished tiles '
            'are kept in a work file and never downloaded twice.\n\n'
            'You are responsible for having the right to download and use '
            'imagery from the source you choose, and for keeping its '
            'attribution. Google\'s terms restrict bulk downloading of map '
            'tiles.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.imagery_downloader_dialog import ImageryDownloaderDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = ImageryDownloaderDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_imagery_downloader():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

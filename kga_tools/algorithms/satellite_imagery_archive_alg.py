# -*- coding: utf-8 -*-

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QDockWidget

from ..core.compat import no_threading
from ..branding import docs_url

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: The panel is kept between runs: running the tool again raises it instead of
#: building a second one, and a search in progress is not lost.
DOCK_INSTANCE = None


def _alive(dock):
    try:
        dock.objectName()
        return True
    except (RuntimeError, AttributeError):  # the Qt object is already gone
        return False


CATALOG_DOCK = 'KgaCatalogPanel'


def _dock_with_catalog(dock):
    """Add `dock` as a tab beside the Catalog panel; on the right side if there is none."""
    window = iface.mainWindow()
    catalog = window.findChild(QDockWidget, CATALOG_DOCK)
    if catalog is None or catalog.isFloating():
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        return
    area = window.dockWidgetArea(catalog)
    iface.addDockWidget(area, dock)
    window.tabifyDockWidget(catalog, dock)


class SatelliteImageryArchiveAlgorithm(QgsProcessingAlgorithm):
    """Open the Satellite Imagery Archive panel."""

    def tr(self, string):
        return QCoreApplication.translate('KgaSatelliteImageryArchive', string)

    def createInstance(self):
        return SatelliteImageryArchiveAlgorithm()

    def name(self):
        return 'satellite_imagery_archive'

    def displayName(self):
        return self.tr('Satellite Imagery Archive')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('satellite_imagery_archive')

    def shortHelpString(self):
        return self.tr(
            'Search, preview and download <b>free historical satellite and '
            'aerial imagery</b> for an area, clipped to it and saved as '
            'GeoTIFF or Cloud-Optimized GeoTIFF.\n\n'
            'Sources: Sentinel-2 L2A (2015 on), Landsat 4-9 Collection 2 '
            '(1982 on), OpenAerialMap, Google Earth Engine (needs your own '
            'account and the earthengine-api package) and any STAC catalog.\n\n'
            'Pick the area (map view, a box, or a polygon layer), a year or '
            'date range and the maximum cloud cover, then search. Scenes '
            'appear with thumbnails; select the ones you want, choose the band '
            'combination and resolution, and download. Only the area you '
            'chose is transferred. Each file gets a .json metadata file and a '
            'quality check.\n\n'
            'The panel docks beside the map: you can close this Processing '
            'dialog and keep working. Use Imagery Downloader instead for '
            'web-map tiles (Google Satellite).'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DOCK_INSTANCE
        from ..gui.historical_imagery.dock import HistoricalImageryDock
        from ..gui.panel_bubble import attach_reveal, file_glyph

        if DOCK_INSTANCE is not None and not _alive(DOCK_INSTANCE):
            DOCK_INSTANCE = None
        if DOCK_INSTANCE is None:
            DOCK_INSTANCE = HistoricalImageryDock(iface, iface.mainWindow())
            _dock_with_catalog(DOCK_INSTANCE)
            DOCK_INSTANCE.hide()            # placed hidden, so a toolbar press can open it out of its button
            attach_reveal(DOCK_INSTANCE, None, 'satellite_imagery_archive',
                          'Satellite Imagery Archive – click to open',
                          file_glyph('bubble/satellite_imagery_archive.png'))
        DOCK_INSTANCE.show()
        DOCK_INSTANCE.raise_()
        return {}


def close_satellite_imagery_archive():
    """Stop any running search or download and remove the panel on unload."""
    global DOCK_INSTANCE
    dock, DOCK_INSTANCE = DOCK_INSTANCE, None
    if dock is None:
        return
    try:
        dock.cleanup()
        if iface is not None:
            iface.removeDockWidget(dock)
        dock.deleteLater()
    except RuntimeError:                    # already destroyed by Qt
        pass

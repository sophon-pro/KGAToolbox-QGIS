# -*- coding: utf-8 -*-

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QDockWidget

from ..branding import docs_url
from ..core.compat import no_threading

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: Kept between runs: running the tool again raises the panel instead of
#: building a second one, and a result on screen is not lost.
DOCK_INSTANCE = None

CATALOG_DOCK = 'KgaCatalogPanel'


def _alive(dock):
    try:
        dock.objectName()
        return True
    except (RuntimeError, AttributeError):  # the Qt object is already gone
        return False


def _dock_with_catalog(dock):
    """Add `dock` as a tab beside the Catalog panel; on the right side if there is none."""
    window = iface.mainWindow()
    catalog = window.findChild(QDockWidget, CATALOG_DOCK)
    if catalog is None or catalog.isFloating():
        iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        return
    iface.addDockWidget(window.dockWidgetArea(catalog), dock)
    window.tabifyDockWidget(catalog, dock)


class DemDsmInspectorAlgorithm(QgsProcessingAlgorithm):
    """Open the DEM / DSM Inspector panel."""

    def tr(self, string):
        return QCoreApplication.translate('KgaDemDsmInspector', string)

    def createInstance(self):
        return DemDsmInspectorAlgorithm()

    def name(self):
        return 'dem_dsm_inspector'

    def displayName(self):
        return self.tr('DEM / DSM Inspector')

    def group(self):
        return 'KGA Irrigation Tools'

    def groupId(self):
        return 'kgairrigationtools'

    def helpUrl(self):
        return docs_url('dem_dsm_inspector')

    def shortHelpString(self):
        return self.tr(
            'Find out whether an elevation raster is a <b>bare-earth DEM/DTM</b> '
            'or a <b>DSM</b> (a surface that includes buildings and trees), '
            'with a confidence score and a report.\n\n'
            'Pick the raster and run. Eight independent checks vote: product '
            'name and metadata, height above a robust local ground, steep '
            'edges, roughness asymmetry, profile spikes, sharp-edged '
            'footprints, and two optional decisive checks - a comparison '
            'with a second layer of the same area, and an orthophoto test '
            '(do trees and roofs seen in the image stand above the open ground '
            'beside them?). The orthophoto test also detects a <i>partially '
            'filtered</i> DTM. A block grid shows where on the map the '
            'objects are; click a block to inspect its profile.\n\n'
            'Reports export to HTML, PDF, JSON and CSV. Object-height, '
            'filtered-surface and highest-cell layers can be added to the '
            'map.\n\n'
            '<b>Watch out for:</b> the confidence is capped by what the data '
            'can prove (a 30 m raster never scores above 78%), and the tool '
            'says INCONCLUSIVE rather than guessing. A DSM over bare land is '
            'physically the same as a DEM. Only file-based rasters are '
            'supported, not WMS/WCS.\n\n'
            'The panel docks beside the map: you can close this Processing '
            'dialog and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DOCK_INSTANCE
        from ..gui.dem_dsm_inspector.dock import InspectorDock
        from ..gui.panel_bubble import attach_reveal, file_glyph

        if DOCK_INSTANCE is not None and not _alive(DOCK_INSTANCE):
            DOCK_INSTANCE = None
        if DOCK_INSTANCE is None:
            DOCK_INSTANCE = InspectorDock(iface, iface.mainWindow())
            _dock_with_catalog(DOCK_INSTANCE)
            DOCK_INSTANCE.hide()            # placed hidden, so a toolbar press can open it out of its button
            attach_reveal(DOCK_INSTANCE, None, 'dem_dsm_inspector', 'DEM / DSM Inspector – click to open',
                          file_glyph('bubble/dem_dsm_inspector.png'))
        DOCK_INSTANCE.show()
        DOCK_INSTANCE.raise_()
        return {}


def close_dem_dsm_inspector():
    """Stop any running analysis and remove the panel on unload."""
    global DOCK_INSTANCE
    dock, DOCK_INSTANCE = DOCK_INSTANCE, None
    if dock is None:
        return
    try:
        dock.cancel()
        if iface is not None:
            iface.removeDockWidget(dock)
        dock.deleteLater()
    except RuntimeError:                    # already destroyed by Qt
        pass

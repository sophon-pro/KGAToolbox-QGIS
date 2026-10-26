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


class ReservoirStorageAnalyzerAlgorithm(QgsProcessingAlgorithm):
    """Open the Reservoir Storage Analyzer window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaReservoirStorage', string)

    def createInstance(self):
        return ReservoirStorageAnalyzerAlgorithm()

    def name(self):
        return 'reservoir_storage_analyzer'

    def displayName(self):
        return self.tr('Reservoir Storage Analyzer')

    def group(self):
        return 'KGA Irrigation Tools'

    def groupId(self):
        return 'kgairrigationtools'

    def tags(self):
        return ['reservoir', 'storage', 'capacity', 'volume', 'area',
                'elevation', 'curve', 'dem', 'inundation', 'excavation',
                'dam', 'irrigation', 'cut', 'fill', 'depth']

    def shortDescription(self):
        return self.tr('Storage capacity of a reservoir from a DEM: '
                       'elevation-area-volume table and curves, inundation '
                       'maps, volume-to-level lookup and depth rasters.')

    def helpUrl(self):
        return docs_url('reservoir_storage_analyzer')

    def shortHelpString(self):
        return self.tr(
            '<p>Works out how much water a reservoir holds at each water '
            'level, from its DEM. Opens a window with six tabs.</p>'
            '<ul>'
            '<li><b>Curves / Table</b> - the elevation-area-volume table and '
            'the elevation-volume and elevation-area curves, saved as CSV '
            '(m&sup3;, 1,000 m&sup3; and million m&sup3;) and PNG.</li>'
            '<li><b>Inundation Maps</b> - one panel per water level on a '
            'hillshade, with a depth legend, scale bar and summary table, on '
            'a paper size of your choice (PNG, PDF, TIFF, JPEG, SVG).</li>'
            '<li><b>Volume Lookup</b> - the water level for a volume you '
            'give, read from the table.</li>'
            '<li><b>Export Rasters</b> - a depth or bottom-elevation GeoTIFF '
            'per level, for checking in other software.</li>'
            '<li><b>Contours to DEM</b> - builds a DEM from contour lines '
            'when there is no DEM yet.</li>'
            '</ul>'
            '<p><b>Surface:</b> <i>Natural ground</i> fills the DEM as it '
            'is. <i>Excavated to base level</i> lowers the ground inside the '
            'flooded area to a base level first and reports the soil removed '
            'separately.</p>'
            '<p><b>Watch out for:</b></p>'
            '<ul>'
            '<li>The DEM must be in a projected CRS in metres.</li>'
            '<li>Water is counted only in cells below the level that connect '
            'to the <i>lowest cell</i> of the DEM, so isolated pits are left '
            'out. Use a boundary polygon (or split line) so the lowest cell '
            'is inside the reservoir, not a stray low pixel elsewhere.</li>'
            '<li>A split line (for example a dam axis) must cross the whole '
            'DEM.</li>'
            '<li>Smoothing changes volumes slightly; keep it off for '
            'reported figures.</li>'
            '</ul>'
            '<p>The window opens modelessly: you can close this Processing '
            'dialog and keep working.</p>'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.reservoir_storage_dialog import ReservoirStorageDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = ReservoirStorageDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_reservoir_storage():
    """Stop any running job and close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
    except RuntimeError:                    # already destroyed by Qt
        pass

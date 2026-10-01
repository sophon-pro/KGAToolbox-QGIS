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
#: `processAlgorithm` returns. Re-running while it is open raises it; re-running
#: after it was closed builds a clean one. Closing deletes the window, so this
#: can hold a wrapper whose Qt object is already gone.
DIALOG_INSTANCE = None


class AddDataAlgorithm(QgsProcessingAlgorithm):
    """Open the Add Open Data & Basemaps window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaAddData', string)

    def createInstance(self):
        return AddDataAlgorithm()

    def name(self):
        return 'add_data'

    def displayName(self):
        return self.tr('Add Open Data & Basemaps')

    def group(self):
        return self.tr('KGA Data Management')

    def groupId(self):
        return 'kgadatamanagement'

    def helpUrl(self):
        return docs_url('add_data')

    def shortHelpString(self):
        return self.tr(
            'Pull <b>administrative boundaries</b>, <b>basemaps</b>, '
            '<b>digital elevation models</b> and <b>land cover</b> straight '
            'into the project, '
            'without the browser trip.\n\n'
            'Pick a country and an admin level and the tool fetches the '
            'boundary from an open data source, shows you who published it, '
            'when it represents, under what licence and how many features it '
            'holds, and adds it to the map.\n\n'
            '<b>Sources</b>\n'
            'geoBoundaries - global, ADM0 to ADM5 where it exists, with each '
            'national source and licence carried through.\n'
            'HDX / OCHA COD-AB - the p-coded boundaries the humanitarian '
            'community agrees on. One download carries every level.\n'
            'Natural Earth - country and state outlines, public domain. The '
            'files are worldwide and are cut down to the country you pick, or '
            'taken whole.\n'
            'Custom URL - any GeoJSON file, ArcGIS REST layer or WFS endpoint. '
            'An ArcGIS service that caps each reply at 1000 records is paged '
            'through, so the layer arrives complete.\n\n'
            '<b>Basemaps</b> are added as XYZ tile layers, optionally saved '
            'into the Browser panel so they are there in every project.\n\n'
            '<b>Digital elevation</b> comes from the Copernicus DEM on AWS '
            '(GLO-30 / GLO-90, no account) or the OpenTopography API (SRTM, '
            'NASADEM, ALOS, GEBCO and more, free API key). Choose the area as '
            'the current map extent, a rectangle or polygon drawn on the map, '
            'a polygon layer\'s boundary to clip to, or any layer\'s extent. '
            'The result is a GeoTIFF, optionally reprojected to the project '
            'CRS.\n\n'
            '<b>Land cover</b> comes from Esri Land Cover 10 m (Sentinel-2, '
            'Impact Observatory, every year since 2017) or ESA WorldCover 10 m '
            '(2020, 2021), no account needed. Pick the year and the area - the '
            'same four area choices as a DEM. The GeoTIFF arrives already '
            'classified: each pixel holds its land-cover code, and the file '
            'carries the class names, the publisher\'s colours and an '
            'attribute table with <b>lc_code</b> and <b>lc_class</b>. Tick '
            '<b>Also add as polygons</b> for a GeoPackage of the classes as '
            'features with the same two fields.\n\n'
            'Vector data arrives as a <b>temporary scratch layer</b>: it lives '
            'in memory and is lost when the project closes, so export it to '
            'keep it. Downloads are cached, so adding the same boundary twice '
            'costs one download and the second one works offline.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.add_data_dialog import AddDataDialog

        parent = iface.mainWindow() if iface else None

        if DIALOG_INSTANCE is not None:
            try:
                still_open = DIALOG_INSTANCE.isVisible()
            except RuntimeError:            # closed: the window deletes itself
                still_open = False
            if not still_open:
                try:
                    DIALOG_INSTANCE.deleteLater()
                except RuntimeError:        # already gone, nothing to delete
                    pass
                DIALOG_INSTANCE = None

        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = AddDataDialog(parent)

        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo(self.tr(
            'Add Open Data & Basemaps opened. You can close this Processing '
            'dialog and keep working.'))
        return {}


def close_add_data():
    """Shut the window on plugin unload.

    The dialog is parented to the QGIS main window rather than to anything the
    plugin owns, so without this it would survive an unload as a dead widget.
    """
    global DIALOG_INSTANCE
    if DIALOG_INSTANCE is None:
        return
    try:
        DIALOG_INSTANCE.close()
        DIALOG_INSTANCE.deleteLater()
    except RuntimeError:                    # already destroyed by Qt
        pass
    DIALOG_INSTANCE = None

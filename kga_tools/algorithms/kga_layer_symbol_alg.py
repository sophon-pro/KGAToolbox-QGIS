# -*- coding: utf-8 -*-
"""KGA Layer Symbol.

Takes no Processing parameters: running it opens the KGA Layer Symbol window,
which is where the work happens. `kga_plugin.run_algorithm` spots the empty
parameter list and runs it straight away. The same gallery is also the
"KGA Layer Symbol" tab of Layer Properties (see `symbol_gallery.gui.layer_symbol_tab`).
"""

import traceback

from qgis.core import Qgis, QgsMessageLog, QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt

from ..branding import LOG_TAG, docs_url
from ..core.compat import no_threading

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: Kept alive between runs. Re-running while it is open raises it; the window
#: deletes itself on close, so a later run builds a clean one.
DIALOG_INSTANCE = None


class KgaLayerSymbolAlgorithm(QgsProcessingAlgorithm):
    """Open the KGA Layer Symbol window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaLayerSymbol', string)

    def createInstance(self):
        return KgaLayerSymbolAlgorithm()

    def name(self):
        return 'kga_layer_symbol'

    def displayName(self):
        return self.tr('KGA Layer Symbol')

    def group(self):
        return self.tr('KGA Mapping')

    def groupId(self):
        return 'kgamapping'

    def helpUrl(self):
        return docs_url('kga_layer_symbol')

    def shortHelpString(self):
        return self.tr(
            'Browse a gallery of ready-made <b>symbols, icons, textures and '
            'colour ramps</b> organised by field - hydraulics, irrigation, '
            'agriculture, land cover, cadastre, transport, geology, remote '
            'sensing and more - and apply one to a layer with a click.\n\n'
            'The window follows the layer selected in the Layers panel: a layer '
            'styles the whole layer, legend classes selected in the Layers panel '
            'style just those classes, and a raster gets colour ramps (a DEM '
            'can get a hillshade too). After applying, a small pop-up fine-tunes '
            'colours, sizes, spacing and angles live on the map; <b>Undo</b> '
            'puts the previous style back.\n\n'
            'The same gallery is the <b>KGA Layer Symbol</b> tab of Layer '
            'Properties, right under Symbology.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE

        if iface is None:
            feedback.pushInfo(self.tr(
                'KGA Layer Symbol is a window and needs the QGIS desktop; '
                'there is nothing to run head-lessly.'))
            return {}

        win = DIALOG_INSTANCE
        if win is not None:
            try:
                win.isVisible()
            except RuntimeError:            # closed: Qt deleted the window
                win = DIALOG_INSTANCE = None

        try:
            if win is None:
                from ..symbol_gallery.gui.layer_symbol_window import LayerSymbolWindow
                win = DIALOG_INSTANCE = LayerSymbolWindow(iface)
            win.setWindowModality(Qt.WindowModality.NonModal)
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception as exc:
            DIALOG_INSTANCE = None
            QgsMessageLog.logMessage(
                'KGA Layer Symbol could not open:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Critical)
            feedback.reportError('KGA Layer Symbol could not open: {}'.format(exc))
            return {}

        feedback.pushInfo(self.tr(
            'KGA Layer Symbol opened. You can close this Processing dialog '
            'and keep working.'))
        return {}


def close_kga_layer_symbol():
    """Shut the window on plugin unload (it is parented to the QGIS main window)."""
    global DIALOG_INSTANCE
    if DIALOG_INSTANCE is None:
        return
    try:
        DIALOG_INSTANCE.close()
    except RuntimeError:                    # already destroyed by Qt
        pass
    DIALOG_INSTANCE = None

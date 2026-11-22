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


class MultipartLineRepairAlgorithm(QgsProcessingAlgorithm):
    """Open the Multipart Line Checker & Repair window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaMultipartLineRepair', string)

    def createInstance(self):
        return MultipartLineRepairAlgorithm()

    def name(self):
        return 'multipart_line_repair'

    def displayName(self):
        return self.tr('Multipart Line Repair')

    def group(self):
        return 'KGA Irrigation Tools'

    def groupId(self):
        return 'kgairrigationtools'

    def helpUrl(self):
        return docs_url('multipart_line_repair')

    def shortHelpString(self):
        return self.tr(
            'Find, explain and repair <b>multipart line features</b> - canals '
            'and drains that were cut with Split or Trim/Extend and no longer '
            'merge because the parts do not touch exactly.\n\n'
            'Tick any number of line layers and press <i>Check</i>: every true '
            'multipart feature is listed with the reason (small gap, overlap, '
            'tiny part, separate parts) and whether repair will make it one '
            'line. Then <i>Repair</i>: duplicate vertices are removed, slivers '
            'shorter than the minimum are dropped, part ends closer than the '
            'snap tolerance are snapped together, overlaps are dissolved and '
            'the lines merged. A feature that ends up as one part keeps its '
            'attributes; parts that are really apart (branches, distant '
            'pieces) become separate features with the attributes copied.\n\n'
            'Write the result to a new GeoPackage or Shapefile, to temporary '
            'layers, or fix the layers in place in an edit session you can '
            'undo or roll back. New layers can keep the original symbology '
            'and labels.\n\n'
            '<b>Watch out for:</b> tolerance and minimum length are in layer '
            'units, so use a projected CRS (metres), not degrees. A tolerance '
            'larger than the real gaps can snap parts that should stay apart.\n\n'
            'The window opens modelessly: you can close this Processing '
            'dialog and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.multipart_repair_dialog import MultipartToolDialog

        if not _alive(DIALOG_INSTANCE):
            DIALOG_INSTANCE = None
        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = MultipartToolDialog(
                iface.mainWindow() if iface else None)
        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()
        return {}


def close_multipart_line_repair():
    """Close the window on plugin unload."""
    global DIALOG_INSTANCE
    dialog, DIALOG_INSTANCE = DIALOG_INSTANCE, None
    if dialog is None:
        return
    try:
        dialog.shutdown()
        dialog.deleteLater()
    except RuntimeError:                    # already destroyed by Qt
        pass

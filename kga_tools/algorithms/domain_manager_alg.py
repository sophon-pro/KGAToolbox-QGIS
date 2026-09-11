# -*- coding: utf-8 -*-
"""Domain & Schema Manager - the dialog launcher.

A zero-parameter algorithm whose `processAlgorithm` opens the modeless dialog,
following the pattern `GeoPackage_Data_Manager.GpkgManagerAlgorithm` set. The
plugin's `run_algorithm` notices an algorithm with no parameters and runs it
straight away instead of showing an empty Processing dialog in front of it.

The module is deliberately thin: `provider.py` imports every module in this
folder at start-up, so the dialog itself lives in `gui/` and is imported only
when the tool is actually run.
"""

from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt

from ..core.compat import no_threading

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: Kept alive between runs so the window is not garbage collected the moment
#: `processAlgorithm` returns. Re-running while it is open raises it; re-running
#: after it was closed builds a clean one, because closing a QDialog only hides
#: it and a cached instance would come back with the previous container loaded.
DIALOG_INSTANCE = None


class DomainManagerAlgorithm(QgsProcessingAlgorithm):
    """Open the Domain & Schema Manager."""

    def tr(self, string):
        return QCoreApplication.translate('KgaDomainManager', string)

    def createInstance(self):
        return DomainManagerAlgorithm()

    def name(self):
        return 'domain_manager'

    def displayName(self):
        return self.tr('Domain & Schema Manager')

    def group(self):
        return self.tr('KGA Schema Tools')

    def groupId(self):
        return 'kgaschematools'

    def helpUrl(self):
        return 'https://khmergrs.com/docs/qgis/domain_manager'

    def shortHelpString(self):
        return self.tr(
            'Create, edit and reuse <b>field domains</b> — the database-level '
            'lists of allowed values that drive the value-map widget in the '
            'attribute form.\n\n'
            'QGIS can create a domain from the Browser panel but not edit one '
            'afterwards, attach one to many fields at once, or move a set of '
            'domains between containers. This tool does all three:\n\n'
            '<b>Domains</b> — add, rename, retype and delete, with a live count '
            'of the fields using each one.\n'
            '<b>Assignment</b> — every layer and field in one grid, so one '
            'domain can be attached to twenty fields in a single pass.\n'
            '<b>Library</b> — export the domains to JSON and import them into '
            'another GeoPackage, so a domain set travels with a project.\n\n'
            'Editing needs a GeoPackage. A File Geodatabase can be read and '
            'exported to a library, but not written to.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working, and you can drag a GeoPackage onto it from the '
            'Browser panel.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..gui.domain_manager import DomainManagerDialog

        parent = iface.mainWindow() if iface else None

        if DIALOG_INSTANCE is not None:
            try:
                still_open = DIALOG_INSTANCE.isVisible()
            except RuntimeError:            # Qt already destroyed the widget
                DIALOG_INSTANCE = None
                still_open = False
            if not still_open:
                try:
                    DIALOG_INSTANCE.deleteLater()
                except RuntimeError:
                    pass
                DIALOG_INSTANCE = None

        if DIALOG_INSTANCE is None:
            DIALOG_INSTANCE = DomainManagerDialog(parent)
            _preload_from_project(DIALOG_INSTANCE)

        DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
        DIALOG_INSTANCE.show()
        DIALOG_INSTANCE.raise_()
        DIALOG_INSTANCE.activateWindow()

        feedback.pushInfo(self.tr(
            'Domain & Schema Manager opened. You can close this Processing '
            'dialog and keep working.'))
        return {}


def _preload_from_project(dialog):
    """Open the GeoPackage behind the active layer, if there is one.

    Saves the usual first step. Silent when there is nothing obvious to open.
    """
    if iface is None:
        return
    layer = iface.activeLayer()
    if layer is None:
        return
    try:
        source = layer.source().split('|')[0]
    except Exception:                       # pragma: no cover - defensive
        return
    if source.lower().endswith('.gpkg'):
        dialog.open_container(source)

# -*- coding: utf-8 -*-
"""Add Grid to Layout.

Takes no Processing parameters: running it opens the Add Grid window, which is
where the work happens. `kga_plugin.run_algorithm` spots the empty parameter
list and runs it straight away, so no empty Processing dialog lands in front.
"""

import traceback

from qgis.core import Qgis, QgsMessageLog, QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QMessageBox

from ..branding import LOG_TAG, docs_url
from ..core.compat import no_threading

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

#: Kept alive between runs so the window is not garbage collected the moment
#: `processAlgorithm` returns. Re-running while it is open raises it; re-running
#: after it was closed builds a clean one.
DIALOG_INSTANCE = None


class AddGridAlgorithm(QgsProcessingAlgorithm):
    """Open the Add Grid to Layout window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaAddGrid', string)

    def createInstance(self):
        return AddGridAlgorithm()

    def name(self):
        return 'add_grid'

    def displayName(self):
        return self.tr('Add Grid to Layout')

    def group(self):
        return self.tr('KGA Mapping')

    def groupId(self):
        return 'kgamapping'

    def helpUrl(self):
        return docs_url('add_grid')

    def shortHelpString(self):
        return self.tr(
            'Put a <b>coordinate grid</b> on a print layout map, from one '
            'window instead of five panels.\n\n'
            'Select the map in the layout designer, run this, and the tool '
            'starts on that map. Choose the grid type, the interval, the '
            'frame, and what each of the four sides shows, where the labels '
            'sit and which way up they read. <b>Suggest</b> works out a round '
            'interval from what the map is showing, so a 1:25,000 sheet does '
            'not need the arithmetic done by hand.\n\n'
            '<b>Coordinate system</b> left unset grids the map in its own CRS. '
            'Set it to EPSG:4326 for a latitude/longitude graticule over a '
            'projected map, with degree/minute labels - the case QGIS makes '
            'you dig for.\n\n'
            '<b>Presets</b> fill the whole form in one pick: a thin black '
            'graticule, exterior ticks, a zebra frame, a lat/long graticule, '
            'or crosses with the labels inside.\n\n'
            '<b>MGRS</b> is the odd one out, because a grid reference such as '
            '48P VT 92 77 is read off several grids rather than one. Picking '
            'it lays down four at once - a zone-and-band graticule, a 100 km '
            'grid labelled with the square letters, a grid that writes one '
            'full coordinate at the start of each margin, and a fine grid '
            'labelled with the principal digits - as a single undo step, in '
            'the UTM zone under the middle of the map. The 100 km letters go '
            'in the margin or inside the map, whichever you pick, and the '
            '1 km mesh of a 1:50,000 sheet and a repeat of the numbers inside '
            'the frame can be ticked on, and the grid interval can be left '
            'automatic or named for a sheet drawn to a stated scale. The '
            'corner label reads the map extent as it draws, so it follows the '
            'map when it is panned. '
            'Each is an ordinary grid afterwards, editable on its own. A map '
            'spanning more than one zone is gridded for the centre zone and '
            'says so.\n\n'
            'The grid combo lists the grids the map already has, so the tool '
            'edits an existing one rather than only adding new ones, and '
            '<b>Apply</b> keeps working on the grid it just wrote instead of '
            'stacking a second one beside it. Every change is one undo step in '
            'the layout designer.\n\n'
            'The window opens modelessly: you can close this Processing dialog '
            'and keep working.'
        )

    def flags(self):
        return no_threading(super().flags())

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        global DIALOG_INSTANCE
        from ..core import layout_grid as LG

        # Nothing to grid: say which of the two things is missing and stop,
        # rather than opening a window whose every combo box is empty.
        problem = LG.missing_target()
        if problem is not None:
            title, message = problem
            feedback.pushInfo('{}. {}'.format(
                title, ' '.join(message.split())))
            self._tell(title, message)
            return {}

        # The sheet being gridded is in the layout designer, which is a window
        # of its own rather than a panel of the main window. Parenting there
        # rather than on the main window is what puts the form in front of the
        # sheet instead of behind it. The dialog keeps itself on the right
        # window from then on; this is only the first placement.
        # Guessing the target is a courtesy: if it fails the window still
        # opens, and its own Layout and Map combos are there to choose from.
        try:
            layout, map_item = LG.active_selection(iface)
            parent = LG.designer_window(iface, layout)
        except Exception:
            QgsMessageLog.logMessage(
                'Could not work out the target layout:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)
            layout, map_item, parent = None, None, None
        if parent is None:
            parent = iface.mainWindow() if iface else None

        # Said here as well as in the window, so it also reaches somebody
        # running kga:add_grid from the Processing dialog rather than the
        # toolbar.
        try:
            _zone, _band, _north, lowest, highest = LG.mgrs_zone_span(map_item)
        except Exception:                   # a warning only; never a reason to fail
            lowest = highest = 0
        if lowest and highest and lowest != highest:
            feedback.pushWarning(self.tr(
                'This map spans UTM zones {} to {}. An MGRS grid on it is '
                'only right inside one of them - see the warning the window '
                'shows when the MGRS preset is picked.').format(lowest, highest))

        if DIALOG_INSTANCE is not None:
            old = DIALOG_INSTANCE
            try:
                still_open = old.isVisible()
            except RuntimeError:            # Qt already destroyed the widget
                still_open = False
                old = None
            if not still_open:
                DIALOG_INSTANCE = None
                if old is not None:
                    try:
                        old.deleteLater()
                    except RuntimeError:
                        pass

        # Anything that goes wrong from here is reported where the user is
        # looking, with the traceback in the log. Letting it out instead gets
        # it caught by the toolbar's handler, which can only say "Could not
        # open kga:add_grid" - true, and of no use to anybody.
        try:
            from ..gui.add_grid_dialog import AddGridDialog

            if DIALOG_INSTANCE is None:
                DIALOG_INSTANCE = AddGridDialog(parent)
            else:
                # Already open: the user has very likely selected a different
                # map, possibly in a different designer, which is the route
                # into this tool.
                DIALOG_INSTANCE.reload_targets()
                DIALOG_INSTANCE.follow_target_window()

            DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
            DIALOG_INSTANCE.show()
            DIALOG_INSTANCE.raise_()
            DIALOG_INSTANCE.activateWindow()
        except Exception as exc:
            DIALOG_INSTANCE = None
            QgsMessageLog.logMessage(
                'Add Grid to Layout could not open:\n{}'.format(
                    traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Critical)
            feedback.reportError('Add Grid to Layout could not open: {}'.format(exc))
            self._tell(
                self.tr('Add Grid to Layout could not open'),
                self.tr('The window could not be built:\n\n{}\n\nThe full '
                        'details are in View > Panels > Log Messages, under '
                        'the "{}" tab.').format(exc, LOG_TAG))
            return {}

        feedback.pushInfo(self.tr(
            'Add Grid to Layout opened. You can close this Processing dialog '
            'and keep working.'))
        return {}

    def _tell(self, title, message):
        """Put `message` in front of the user, when there is a user to tell.

        Head-lessly - a model, a script, a test - there is no window to parent
        a message box to and nobody to close it, so the feedback object is the
        only channel and the caller has already used it.
        """
        if iface is None:
            return
        try:
            QMessageBox.information(iface.mainWindow(), title, message)
        except Exception as exc:            # pragma: no cover - defensive
            QgsMessageLog.logMessage(
                'Could not show the Add Grid message: {}'.format(exc),
                LOG_TAG, Qgis.MessageLevel.Warning)


def close_add_grid():
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

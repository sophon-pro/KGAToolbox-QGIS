# -*- coding: utf-8 -*-
"""Callout Label Studio.

Takes no Processing parameters: running it opens the Callout Label Studio
window, which is where the work happens. `kga_plugin.run_algorithm` spots the
empty parameter list and runs it straight away, so no empty Processing dialog
lands in front.
"""

from contextlib import suppress
import traceback

from qgis.core import Qgis, QgsMessageLog, QgsProcessingAlgorithm
from qgis.PyQt.QtCore import QCoreApplication, Qt

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


class CalloutLabelStudioAlgorithm(QgsProcessingAlgorithm):
    """Open the Callout Label Studio window."""

    def tr(self, string):
        return QCoreApplication.translate('KgaCalloutLabelStudio', string)

    def createInstance(self):
        return CalloutLabelStudioAlgorithm()

    def name(self):
        return 'callout_label_studio'

    def displayName(self):
        return self.tr('Callout Label Studio')

    def group(self):
        return self.tr('KGA Mapping')

    def groupId(self):
        return 'kgamapping'

    def helpUrl(self):
        return docs_url('callout_label_studio')

    def shortHelpString(self):
        return self.tr(
            'Style the labels of a layer as <b>callouts</b> - leader lines, '
            'Manhattan elbows, curved arrows and speech balloons - from one '
            'window, with the map updating as you go.\n\n'
            'Pick a layer, a field or expression to label with and, if you '
            'want, an <i>Only where</i> filter. Click a <b>quick style</b> '
            '(Classic Leader, Manhattan Elbow, Curved Arrow, Speech Balloon, '
            'Dark Badge, Imagery Overlay, Survey Dashed, Map Inset Box) and '
            'fine-tune it in the Callout, Text and Placement tabs.\n\n'
            '<b>Organized layout</b> stacks the labels in tidy columns or rows '
            'beside the data, sorted so the leader lines never cross. '
            '<b>Number key</b> puts a number or letter on each small feature '
            'and lists the names in key boxes outside the data; it can send '
            'the key to a print layout as editable text boxes.\n\n'
            '<b>Apply</b> keeps the result; <b>Cancel</b> puts the original '
            'labels of the layer back. Save your own presets, export the style '
            'as a QML or copy the equivalent PyQGIS code from the More menu.\n\n'
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
                'Callout Label Studio is a window and needs the QGIS '
                'desktop; there is nothing to run head-lessly.'))
            return {}

        old = DIALOG_INSTANCE
        if old is not None:
            try:
                still_open = old.isVisible()
            except RuntimeError:            # Qt already destroyed the widget
                still_open = False
            if not still_open:
                DIALOG_INSTANCE = None
                try:
                    old.deleteLater()
                except RuntimeError:
                    pass

        try:
            from ..gui.callout_label_dialog import CalloutStudioDialog

            if DIALOG_INSTANCE is None:
                DIALOG_INSTANCE = CalloutStudioDialog(iface)
            active = iface.activeLayer()
            if active is not None:
                with suppress(Exception):  # a courtesy; the combo still works
                    DIALOG_INSTANCE.layer_cb.setLayer(active)

            DIALOG_INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
            DIALOG_INSTANCE.show()
            DIALOG_INSTANCE.raise_()
            DIALOG_INSTANCE.activateWindow()
        except Exception as exc:
            DIALOG_INSTANCE = None
            QgsMessageLog.logMessage(
                'Callout Label Studio could not open:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Critical)
            feedback.reportError(
                'Callout Label Studio could not open: {}'.format(exc))
            return {}

        feedback.pushInfo(self.tr(
            'Callout Label Studio opened. You can close this Processing dialog '
            'and keep working.'))
        return {}


def close_callout_label_studio():
    """Shut the window on plugin unload.

    It is parented to the QGIS main window rather than to anything the plugin
    owns, so without this it would survive an unload as a dead widget.
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

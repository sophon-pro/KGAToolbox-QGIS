# -*- coding: utf-8 -*-
"""Add Layers to Layer Package: the window.

Three inputs - the layers to add (ticked from the project or browsed to on
disk), whether to take only selected features, and the existing package to add
them to. It stays open so several batches can be added in a row.
"""

import os
import traceback

from qgis.core import Qgis, QgsMessageLog, QgsVectorLayer
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import QApplication, QCheckBox, QVBoxLayout

from ..branding import LOG_TAG
from ..core import packaging as P
from .project_layer_checklist import ProjectLayerChecklist
from .package_window import PackageWindow, tr

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None


def selection_count(layers):
    """How many of `layers` are vector layers with a selection."""
    return len([layer for layer in layers if isinstance(layer, QgsVectorLayer)
                and layer.selectedFeatureCount() > 0])


class AddToPackageDialog(PackageWindow):
    TITLE = 'Add Layers to Layer Package'
    ALG_NAME = 'add_to_layer_package'
    RUN_TEXT = 'Add'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 460)
        outer = QVBoxLayout(self)

        self.checklist = ProjectLayerChecklist(tr('Input layers'), self)
        self.checklist.changed.connect(self.refresh_state)
        self.checklist.message.connect(self.say)
        outer.addWidget(self.checklist, 1)

        self.selected_check = QCheckBox(tr('Use only the selected features'),
                                        self)
        self.selected_check.setToolTip(tr(
            'For every ticked vector layer that has a selection, add only the '
            'selected features. Layers without a selection are added whole.'))
        outer.addWidget(self.selected_check)

        self.path_edit = self.path_row(
            outer, 'Layer package (.kgalp)', 'The existing package to add to',
            self.choose_package, self.refresh_state)
        self.add_footer(outer)
        self.refresh_state()

    def refresh_state(self, *_args):
        layers = self.checklist.layers()
        text = tr('Use only the selected features')
        count = selection_count(layers)
        if count:
            text += tr(' ({} ticked layer(s) have a selection)').format(count)
        self.selected_check.setText(text)
        self.run_button.setEnabled(bool(layers)
                                   and bool(self.text_of(self.path_edit)))

    def set_package(self, path):
        self.path_edit.setText(path)

    def changeEvent(self, event):
        # Selections change in the map, not here; re-read them on return.
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.refresh_state()

    def run(self):
        layers = self.checklist.layers()
        path = self.text_of(self.path_edit)
        if not layers:
            self.say(tr('Tick at least one layer.'), error=True)
            return
        if not path.lower().endswith(P.EXTENSION) or not os.path.isfile(path):
            self.say(tr('Choose an existing {} package.').format(P.EXTENSION),
                     error=True)
            return

        self.run_button.setEnabled(False)
        self.say(tr('Adding...'))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = P.append_layers(path, layers,
                                     self.selected_check.isChecked())
        except P.PackageError as exc:
            self.say(str(exc), error=True)
            return
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Add Layers to Layer Package failed:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)
            self.say(str(exc), error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_state()

        added = result['vectors'] + result['rasters']
        message = tr('Added {} layer(s) to {}.').format(
            added, os.path.basename(path))
        if result['skipped']:
            message += tr(' {} skipped.').format(result['skipped'])
        for warning in result['warnings']:
            message += '\n' + warning
            QgsMessageLog.logMessage(warning, LOG_TAG,
                                     Qgis.MessageLevel.Warning)
        self.say(message, error=bool(added == 0))
        if iface is not None and added:
            iface.messageBar().pushSuccess(self.TITLE, message.split('\n')[0])

    def shutdown(self):
        self.checklist.disconnect_project()
        super().shutdown()


def show_add_to_package(package=None):
    """Open the window (or raise the open one), optionally on `package`."""
    window = AddToPackageDialog.show_window()
    if package:
        window.set_package(package)
    return window


def close_add_to_package():
    AddToPackageDialog.close_window()

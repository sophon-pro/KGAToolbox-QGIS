# -*- coding: utf-8 -*-
"""Create Layer Package: the window.

The layers to package (ticked from the project or browsed to on disk), the
options, and where to write the `.kgalp`.
"""

import os
import traceback

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QMessageBox,
                                 QVBoxLayout)

from ..branding import LOG_TAG
from ..core import output_defaults as od
from ..core import packaging as P
from ..core.changelog import ChangeReport
from .add_to_package_dialog import selection_count
from .project_layer_checklist import ProjectLayerChecklist
from .output_fields import FollowingField
from .package_window import PackageWindow, tr

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None


class CreatePackageDialog(PackageWindow):
    TITLE = 'Create Layer Package'
    ALG_NAME = 'create_layer_package'
    RUN_TEXT = 'Create'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 520)
        outer = QVBoxLayout(self)

        self.checklist = ProjectLayerChecklist(tr('Layers to package'), self)
        self.checklist.changed.connect(self.refresh_state)
        self.checklist.message.connect(self.say)
        outer.addWidget(self.checklist, 1)

        self.selected_check = QCheckBox(tr('Package only the selected features'),
                                        self)
        self.selected_check.setToolTip(tr(
            'For every ticked vector layer that has a selection, package only '
            'the selected features. Layers without a selection go in whole.'))
        outer.addWidget(self.selected_check)

        self.resources_check = QCheckBox(
            tr('Collect SVG symbols and marker images'), self)
        self.resources_check.setChecked(True)
        self.resources_check.setToolTip(tr(
            "Copy every SVG and image the layers' symbology uses into the "
            'package, so the symbols render the same on another machine.'))
        outer.addWidget(self.resources_check)

        self.metadata_check = QCheckBox(tr('Include layer metadata'), self)
        self.metadata_check.setChecked(True)
        outer.addWidget(self.metadata_check)

        self.report_check = QCheckBox(
            tr('Write an HTML report beside the package'), self)
        outer.addWidget(self.report_check)

        self.path_edit = self.path_row(
            outer, 'Layer package (.kgalp)', 'Where to save the package',
            lambda edit: self.choose_package(edit, save=True),
            self.refresh_state)
        self._path_follow = FollowingField(self.path_edit, P.EXTENSION)
        self.add_footer(outer)
        self.follow_project()       # after the footer: it fires refresh_state
        self.refresh_state()

    def follow_project(self):
        """Offer a new package in the project folder, named for the project."""
        workspace = od.workspace()
        stem = workspace.name if workspace is not None else 'Layer_Package'
        self._path_follow.retarget(stem)

    def refresh_state(self, *_args):
        layers = self.checklist.layers()
        text = tr('Package only the selected features')
        count = selection_count(layers)
        if count:
            text += tr(' ({} ticked layer(s) have a selection)').format(count)
        self.selected_check.setText(text)
        self.run_button.setEnabled(bool(layers)
                                   and bool(self.text_of(self.path_edit)))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.refresh_state()

    def run(self):
        layers = self.checklist.layers()
        path = self.text_of(self.path_edit)
        if not layers:
            self.say(tr('Tick at least one layer.'), error=True)
            return
        if not path:
            self.say(tr('Choose where to save the package.'), error=True)
            return
        if not path.lower().endswith(P.EXTENSION):
            path += P.EXTENSION
        if os.path.exists(path):
            answer = QMessageBox.question(
                self, self.TITLE,
                tr('{} already exists. Replace it?').format(
                    os.path.basename(path)))
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.run_button.setEnabled(False)
        self.say(tr('Packaging...'))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = P.create_package(
                path, layers, self.selected_check.isChecked(),
                self.resources_check.isChecked(),
                self.metadata_check.isChecked())
            report_path = (self._write_report(result)
                           if self.report_check.isChecked() else '')
        except P.PackageError as exc:
            self.say(str(exc), error=True)
            return
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Create Layer Package failed:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)
            self.say(str(exc), error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_state()

        count = result['vectors'] + result['rasters']
        message = tr('Packaged {} layer(s) into {} ({} KB).').format(
            count, os.path.basename(result['path']), result['size_kb'])
        if result['skipped']:
            message += tr(' {} skipped.').format(result['skipped'])
        if report_path:
            message += tr('\nReport: {}').format(report_path)
        for warning in result['warnings']:
            message += '\n' + warning
            QgsMessageLog.logMessage(warning, LOG_TAG,
                                     Qgis.MessageLevel.Warning)
        self.say(message, error=(count == 0))
        if iface is not None and count:
            iface.messageBar().pushSuccess(self.TITLE, message.split('\n')[0])

    @staticmethod
    def _write_report(result):
        report = ChangeReport('Create Layer Package',
                              os.path.basename(result['path']), dry_run=False)
        report.set_count('vector layers', result['vectors'])
        report.set_count('raster layers', result['rasters'])
        report.set_count('layers skipped', result['skipped'])
        report.set_count('resources collected', result['resources'])
        report.set_count('package size (KB)', result['size_kb'])
        report.add_table('Layers', ['Layer', 'Kind', 'Stored as', 'Style'],
                         result['rows'])
        for warning in result['warnings']:
            report.warn(warning)
        return report.write_html(
            os.path.splitext(result['path'])[0] + '_report.html')

    def shutdown(self):
        self.checklist.disconnect_project()
        super().shutdown()


def show_create_package():
    return CreatePackageDialog.show_window()


def close_create_package():
    CreatePackageDialog.close_window()

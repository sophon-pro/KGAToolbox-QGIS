# -*- coding: utf-8 -*-
"""Open Layer Package: the window.

The package, where to unpack it, whether to add its layers to the project and
what to call the group they go in.
"""

import os
import traceback

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QLabel, QLineEdit,
                                 QVBoxLayout)

from ..branding import LOG_TAG
from ..core import packaging as P
from .package_window import PackageWindow, tr

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None


class OpenPackageDialog(PackageWindow):
    TITLE = 'Open Layer Package'
    ALG_NAME = 'open_layer_package'
    RUN_TEXT = 'Open'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(520)
        outer = QVBoxLayout(self)

        self.path_edit = self.path_row(
            outer, 'Layer package (.kgalp)', 'The package to open',
            self.choose_package, self.refresh_state)
        self.folder_edit = self.path_row(
            outer, 'Extract to', '', self.choose_folder, self.refresh_state)
        self.folder_edit.setToolTip(tr(
            'A permanent folder to unpack into - the layers point at the '
            'unpacked files. Empty uses a "KGA Packages" folder beside the '
            'project file.'))

        self.add_check = QCheckBox(tr('Add the layers to the project'), self)
        self.add_check.setChecked(True)
        self.add_check.toggled.connect(self.refresh_state)
        outer.addWidget(self.add_check)

        self.group_label = QLabel(tr('Group name'), self)
        outer.addWidget(self.group_label)
        self.group_edit = QLineEdit(self)
        self.group_edit.setPlaceholderText(tr('The package name'))
        outer.addWidget(self.group_edit)

        self.add_footer(outer)
        self.refresh_state()

    def refresh_state(self, *_args):
        path = self.text_of(self.path_edit)
        self.folder_edit.setPlaceholderText(
            P.default_unpack_folder(path) if path else '')
        add = self.add_check.isChecked()
        self.group_label.setEnabled(add)
        self.group_edit.setEnabled(add)
        self.run_button.setEnabled(bool(path))

    def set_package(self, path):
        self.path_edit.setText(path)

    def run(self):
        path = self.text_of(self.path_edit)
        if not path.lower().endswith(P.EXTENSION) or not os.path.isfile(path):
            self.say(tr('Choose an existing {} package.').format(P.EXTENSION),
                     error=True)
            return

        adding = self.add_check.isChecked()
        self.run_button.setEnabled(False)
        self.say(tr('Opening...'))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = P.open_package(
                path, self.text_of(self.folder_edit), adding,
                self.group_edit.text().strip())
        except P.PackageError as exc:
            self.say(str(exc), error=True)
            return
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Open Layer Package failed:\n{}'.format(
                    traceback.format_exc()), LOG_TAG, Qgis.MessageLevel.Warning)
            self.say(str(exc), error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_state()

        if adding:
            message = tr('Added {} layer(s) from {}.').format(
                result['added'], os.path.basename(path))
        else:
            message = tr('Unpacked {}.').format(os.path.basename(path))
        message += tr('\nExtracted to {}').format(result['folder'])
        for line in result['notes']:
            QgsMessageLog.logMessage(line, LOG_TAG, Qgis.MessageLevel.Info)
        for line in result['warnings']:
            message += '\n' + line
            QgsMessageLog.logMessage(line, LOG_TAG, Qgis.MessageLevel.Warning)
        self.say(message, error=(adding and result['added'] == 0))
        if iface is not None and (result['added'] or not adding):
            iface.messageBar().pushSuccess(self.TITLE, message.split('\n')[0])


def show_open_package(package=None):
    window = OpenPackageDialog.show_window()
    if package:
        window.set_package(package)
    return window


def close_open_package():
    OpenPackageDialog.close_window()

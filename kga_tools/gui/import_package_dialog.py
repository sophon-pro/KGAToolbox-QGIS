# -*- coding: utf-8 -*-
"""Project > Import Project Package: unpack a `.kgapp` into a workspace.

Shows what the package says about itself (name, summary, author, when and with
which QGIS it was made, how many layers, and every warning the sender's analysis
raised) before anything is written, asks where to store the project, and refuses
a location that already holds something. The work is `core/projectpkg.py`.
"""

import os
import traceback
from html import escape

from qgis.core import Qgis, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .window_reveal import revealed
from ..branding import LOG_TAG
from ..core import data_paste as dp
from ..core import projectpkg as pp
from ..core import workspace as ws
from .share_workspace_dialog import human_size

SETTINGS_FOLDER = 'KGA/projectpackage/import_folder'
SETTINGS_PACKAGE = 'KGA/projectpackage/last_package'

BAD = QColor(253, 232, 232)


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def describe(manifest, path):
    """Rich text summary of a package for the dialog."""
    info = manifest.info
    lines = ['<b style="font-size:13pt">{}</b>'.format(
        escape(info.name or os.path.basename(path)))]
    if info.summary:
        lines.append(escape(info.summary))
    facts = []
    if info.author:
        facts.append('Author: {}'.format(escape(info.author)))
    if manifest.created:
        facts.append('Created: {}'.format(escape(manifest.created.replace('T', ' '))))
    if manifest.qgis:
        facts.append('QGIS {}'.format(escape(manifest.qgis)))
    facts.append('Shared: {}'.format(
        'everything' if manifest.mode == pp.MODE_EVERYTHING else 'map data only'))
    try:
        facts.append('File size: {}'.format(human_size(os.path.getsize(path))))
    except OSError:
        pass
    lines.append(' &middot; '.join(facts))
    layers = manifest.layers
    if layers:
        parts = ['{} layers'.format(len(layers))]
        for status, label in ((pp.STATUS_SERVICE, 'web services'),
                              (pp.STATUS_NOT_SHARABLE, 'not shareable'),
                              (pp.STATUS_EXCLUDED, 'left out')):
            number = manifest.count(status)
            if number:
                parts.append('{} {}'.format(number, label))
        lines.append(', '.join(parts))
    layouts = manifest.project.get('layouts') or []
    if layouts:
        lines.append('Layouts: {}'.format(escape(', '.join(layouts))))
    if info.tags:
        lines.append('Tags: {}'.format(escape(', '.join(info.tags))))
    if info.description:
        lines.append('<br>' + escape(info.description))
    if info.credits:
        lines.append('Credits: {}'.format(escape(info.credits)))
    if info.use_limitations:
        lines.append('Use limitations: {}'.format(escape(info.use_limitations)))
    return '<br>'.join(lines)


@revealed
class ImportPackageDialog(QDialog):

    def __init__(self, parent=None, package=''):
        super().__init__(parent)
        self.setWindowTitle('Import Project Package')
        self.setMinimumWidth(640)
        self.manifest = None

        self.package = QLineEdit(package)
        self.package.setPlaceholderText('A .kgapp file')
        browse_package = QPushButton('Browse...')
        browse_package.clicked.connect(self.browse_package)
        package_row = QHBoxLayout()
        package_row.addWidget(self.package, 1)
        package_row.addWidget(browse_package)

        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(150, 100)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setStyleSheet('border: 1px solid #c8c8c8;')
        self.details = QLabel('Choose a package to see what is in it.')
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        info_row = QHBoxLayout()
        info_row.addWidget(self.thumbnail)
        info_row.addWidget(self.details, 1)

        self.warnings = QPlainTextEdit()
        self.warnings.setReadOnly(True)
        self.warnings.setMaximumHeight(90)
        self.warnings_label = QLabel('Warnings from the sender')

        self.location = QLineEdit(self._default_location())
        browse_location = QPushButton('Browse...')
        browse_location.clicked.connect(self.browse_location)
        location_row = QHBoxLayout()
        location_row.addWidget(self.location, 1)
        location_row.addWidget(browse_location)
        self.name = QLineEdit()
        self.preview = QLabel('')
        self.preview.setWordWrap(True)
        self.problem = QLabel('')
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet('color: #b00020;')

        self.verify = QCheckBox('Check the package for damage while unpacking')
        self.verify.setChecked(True)
        self.open_after = QCheckBox('Open the workspace when finished')
        self.open_after.setChecked(True)

        form = QFormLayout()
        form.addRow('Package', package_row)
        form.addRow('Store the project in', location_row)
        form.addRow('Project name', self.name)
        form.addRow('Workspace', self.preview)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = self.buttons.addButton(
            'Import', QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(info_row)
        layout.addWidget(self.warnings_label)
        layout.addWidget(self.warnings)
        layout.addWidget(self.problem)
        layout.addWidget(self.verify)
        layout.addWidget(self.open_after)
        layout.addWidget(self.buttons)

        self.package.editingFinished.connect(self.load_package)
        self.location.textChanged.connect(self.validate)
        self.name.textChanged.connect(self.validate)
        self.load_package()

    # ----------------------------------------------------------------- inputs

    @staticmethod
    def _default_location():
        saved = QgsSettings().value(SETTINGS_FOLDER, '')
        return os.path.normpath(str(saved) if saved else ws.default_location())

    def browse_package(self):
        start = self.package.text().strip() or str(
            QgsSettings().value(SETTINGS_PACKAGE, '') or ws.documents_dir())
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Open project package', start,
            'KGA project package (*{})'.format(pp.EXTENSION))
        if path:
            self.package.setText(os.path.normpath(path))
            self.load_package()

    def browse_location(self):
        start = self.location.text().strip()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            if parent == start:
                break
            start = parent
        folder = QFileDialog.getExistingDirectory(
            self, 'Store the project in', start or ws.documents_dir())
        if folder:
            self.location.setText(os.path.normpath(folder))

    def load_package(self):
        path = self.package.text().strip()
        self.manifest = None
        self.thumbnail.clear()
        self.warnings.clear()
        self.warnings_label.setVisible(False)
        self.warnings.setVisible(False)
        if not path:
            self.details.setText('Choose a package to see what is in it.')
            self.validate()
            return
        try:
            self.manifest = pp.read_manifest(path)
        except pp.ProjectPackageError as error:
            self.details.setText('<span style="color:#b00020">{}</span>'.format(
                escape(str(error))))
            self.validate()
            return
        self.details.setText(describe(self.manifest, path))
        data = pp.read_thumbnail(path)
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self.thumbnail.setPixmap(pixmap.scaled(
                    self.thumbnail.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
        else:
            self.thumbnail.setText('No preview')
        warnings = self.manifest.warnings
        if warnings:
            self.warnings.setPlainText('\n'.join(warnings))
            self.warnings_label.setVisible(True)
            self.warnings.setVisible(True)
        suggestion = ws.name_problem(self.manifest.info.name or '') == '' and \
            self.manifest.info.name or os.path.splitext(os.path.basename(path))[0]
        folder = self.location.text().strip()
        self.name.setText(pp.unique_folder_name(folder, suggestion)
                          if folder else suggestion)
        self.validate()

    # ------------------------------------------------------------- validation

    def target(self):
        return os.path.normpath(os.path.join(self.location.text().strip(),
                                             self.name.text().strip()))

    def why_not(self):
        if self.manifest is None:
            return ''
        return pp.target_problem(self.location.text(), self.name.text())

    def validate(self):
        self.preview.setText(self.target() if self.name.text().strip() else '')
        problem = self.why_not()
        self.problem.setText(problem)
        self.import_button.setEnabled(self.manifest is not None and not problem)

    def values(self):
        """(package, location, name, verify, open after)."""
        return (self.package.text().strip(), self.location.text().strip(),
                self.name.text().strip(), self.verify.isChecked(),
                self.open_after.isChecked())


# ------------------------------------------------------------------ running it

def run_import(parent, package, location, name, verify=True):
    """Import under a progress dialog. An `ImportResult`, or None on failure."""
    progress = QProgressDialog('Unpacking...', 'Cancel', 0, 100, parent)
    progress.setWindowTitle('Import Project Package')
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.setMinimumWidth(420)

    def on_progress(percent, text):
        progress.setValue(int(percent))
        if text:
            progress.setLabelText(text)
        QApplication.processEvents()

    feedback = dp.Feedback(on_progress=on_progress, on_tick=QApplication.processEvents)
    progress.canceled.connect(feedback.cancel)
    progress.show()
    try:
        return pp.import_package(package, location, name, verify, feedback)
    except pp.ProjectPackageError as error:
        if str(error) != 'Canceled.':
            QMessageBox.warning(parent, 'Import Project Package', str(error))
        return None
    except Exception as error:
        _log('Import Project Package failed:\n{}'.format(traceback.format_exc()),
             Qgis.MessageLevel.Critical)
        QMessageBox.critical(
            parent, 'Import Project Package',
            'The import failed: {}\n\nSee the KGA Toolbox log for details.'.format(error))
        return None
    finally:
        progress.close()
        progress.deleteLater()


class ImportResultDialog(QDialog):
    """Where the project went, and which layers still do not load."""

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Project imported')
        self.setMinimumSize(560, 320)
        workspace = result.workspace
        text = ('<b>{}</b><br>Stored in {}<br>{} files unpacked.'.format(
            escape(workspace.name), escape(workspace.root), result.files))
        head = QLabel(text)
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        if result.invalid_layers:
            layout.addWidget(QLabel(
                '<b style="color:#b00020">{} layer{} could not be loaded:</b>'.format(
                    len(result.invalid_layers),
                    '' if len(result.invalid_layers) == 1 else 's')))
            table = QTreeWidget()
            table.setRootIsDecorated(False)
            table.setHeaderLabels(['Layer', 'Problem'])
            table.setColumnWidth(0, 200)
            for name, why in result.invalid_layers:
                item = QTreeWidgetItem(table, [name, why])
                for column in range(2):
                    item.setBackground(column, QBrush(BAD))
            layout.addWidget(table, 1)
        else:
            layout.addWidget(QLabel('Every layer loads.'))
        if result.warnings:
            layout.addWidget(QLabel('Warnings from the sender'))
            box = QPlainTextEdit('\n'.join(result.warnings))
            box.setReadOnly(True)
            box.setMaximumHeight(100)
            layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

# -*- coding: utf-8 -*-
"""Project > Set Workspace: pick the folder and the name of the project."""

import os

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core import workspace as ws


def _native(path):
    """`path` with the separators of this OS (Qt hands out forward slashes)."""
    return os.path.normpath(path) if path else path


class SetWorkspaceDialog(QDialog):
    """Asks for a folder (required) and a project name.

    The workspace is `<folder>/<name>`. Everything the project owns - the
    `.qgz`, `Default.gpkg`, the Toolbox and Layouts folders - is created there,
    which is why the folder cannot be left empty.
    """

    def __init__(self, parent=None, name='Project', location=None):
        super().__init__(parent)
        self.setWindowTitle('Set Workspace')
        self.setMinimumWidth(520)

        self.location = QLineEdit(_native(location or ws.default_location()))
        browse = QPushButton('Browse...')
        browse.clicked.connect(self.browse)
        row = QHBoxLayout()
        row.addWidget(self.location, 1)
        row.addWidget(browse)

        self.name = QLineEdit(name)
        self.name.setPlaceholderText('Project name')

        self.preview = QLabel('')
        self.preview.setWordWrap(True)
        self.problem = QLabel('')
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet('color: #b00020;')

        form = QFormLayout()
        form.addRow('Folder', row)
        form.addRow('Project name', self.name)
        form.addRow('Workspace', self.preview)

        note = QLabel(
            'The project (.qgz), the default GeoPackage (Default.gpkg), the '
            'Toolbox folder and the Layouts folder are created in this '
            'workspace.')
        note.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

        self.location.textChanged.connect(self.validate)
        self.location.editingFinished.connect(self._tidy_location)
        self.name.textChanged.connect(self.validate)
        self.validate()

    def browse(self):
        start = self.location.text().strip()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            if parent == start:
                break
            start = parent
        folder = QFileDialog.getExistingDirectory(
            self, 'Folder for the workspace', start or ws.documents_dir())
        if folder:
            self.location.setText(_native(folder))

    def _tidy_location(self):
        """Show the folder with the OS's own separators once typing is done."""
        text = self.location.text().strip()
        if text and _native(text) != self.location.text():
            self.location.setText(_native(text))

    def target(self):
        return _native(os.path.join(self.location.text().strip(),
                                    self.name.text().strip()))

    def why_not(self):
        """What stops this being created, or ''."""
        if not self.location.text().strip():
            return 'Choose a folder for the workspace.'
        problem = ws.name_problem(self.name.text())
        if problem:
            return problem
        target = self.target()
        if os.path.exists(target) and (
                not os.path.isdir(target) or os.listdir(target)):
            return '"{}" already exists and is not empty.'.format(target)
        return ''

    def validate(self):
        self.preview.setText(self.target() if self.name.text().strip() else '')
        problem = self.why_not()
        self.problem.setText(problem)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            not problem)

    def values(self):
        return _native(self.location.text().strip()), self.name.text().strip()

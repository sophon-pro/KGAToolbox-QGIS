# -*- coding: utf-8 -*-
"""Project > Convert Project to Workspace: make a traditional `.qgz` a KGA workspace.

A plain QGIS project is a file with absolute paths to data scattered across the
disk. A KGA workspace keeps everything in one folder: the project, a default
GeoPackage, a Toolbox and Layouts folder. This window converts the open project:
it shows every layer and what will happen to its data (kept beside the project,
copied into `Default.gpkg`, copied into `commondata/`, kept as a web service, or
flagged because its source is lost), then builds the workspace with the same
engine Share Workspace uses. The original project and data are not touched.

It reuses the Share Workspace window - same analysis table, same tick boxes -
with the package fields replaced by a name and a location.
"""

import os
import traceback

from qgis.core import Qgis, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QBrush, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
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
    QWidget,
)

from ..branding import LOG_TAG
from ..core import data_paste as dp
from ..core import project_share as ps
from ..core import projectpkg as pp
from ..core import workspace as ws
from .share_workspace_dialog import BAD, ShareWorkspaceDialog, human_size

SETTINGS_FOLDER = 'KGA/convertproject/folder'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


class ConvertProjectDialog(ShareWorkspaceDialog):

    def __init__(self, project, project_file, parent=None, extent=None, iface=None):
        self.open_after = None
        super().__init__(project, project_file, None, parent, extent, iface)
        self.setWindowTitle('Convert Project to Workspace')
        self.tabs.setTabText(0, 'Workspace')
        self.share_button.setText('Convert')
        # Fixed choices the package options would otherwise offer.
        for widget in (self.mode_everything, self.mode_map, self.include_toolbox,
                       self.include_layouts, self.make_thumbnail, self.save_report):
            widget.setVisible(False)
        self.mode_everything.parentWidget().setVisible(False)     # "What to share"
        self.copy_remote.parentWidget().setTitle('Options')
        self.copy_remote.setText(
            'Copy data from databases and web feature services into the workspace')
        self.update_preview()

    def update_summary(self):
        analysis = self.analysis
        included = analysis.included()
        bad = [i for i in included if not i.sharable]
        text = ('{} of {} layers will be converted; about {} of data is copied '
                'into the workspace.'.format(
                    len(included), len(analysis.layers),
                    human_size(analysis.estimated_size())))
        if bad:
            text += ('<br><b style="color:#b00020">{} layer{} cannot be converted '
                     '</b>(data source not found or not supported); {} kept in the '
                     'project, flagged.'.format(
                         len(bad), '' if len(bad) == 1 else 's',
                         'it is' if len(bad) == 1 else 'they are'))
        self.summary_label.setText(text)

    @property
    def mode(self):
        # Every layer starts ticked; what is copied is decided by the engine.
        return pp.MODE_EVERYTHING

    # ------------------------------------------------------------ workspace tab

    def _build_package_tab(self):
        page = QWidget()
        stem = os.path.splitext(os.path.basename(self.project_file))[0]
        self.name = QLineEdit(stem)
        saved = QgsSettings().value(SETTINGS_FOLDER, '')
        self.output = QLineEdit(os.path.normpath(
            str(saved) if saved else ws.default_location()))
        self.output.textChanged.connect(self._location_changed)
        browse = QPushButton('Browse...')
        browse.clicked.connect(self.browse_output)
        row = QHBoxLayout()
        row.addWidget(self.output, 1)
        row.addWidget(browse)
        self.preview = QLabel('')
        self.preview.setWordWrap(True)
        self.problem = QLabel('')
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet('color: #b00020;')
        self.open_after = QCheckBox('Open the new workspace when finished')
        self.open_after.setChecked(True)

        # The share dialog's build_options reads these; they stay empty here.
        self.summary = self.tags = self.author = self.credits = self.limitations = \
            QLineEdit()
        self.description = QPlainTextEdit()

        form = QFormLayout()
        form.addRow('Workspace name', self.name)
        form.addRow('Store it in', row)
        form.addRow('Workspace', self.preview)
        note = QLabel(
            'The project is copied into a new workspace folder. Data beside the '
            'project keeps its place; data from anywhere else is copied into '
            'Default.gpkg (vector) or commondata (rasters and other files). '
            'The original project and its data are not changed.')
        note.setWordWrap(True)
        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.problem)
        layout.addWidget(self.open_after)
        layout.addStretch(1)
        self.location_edit = self.output
        return page

    def _name_changed(self, _text):
        self.update_preview()

    def update_preview(self):
        if self.open_after is None or getattr(self, 'share_button', None) is None:
            return                      # the window is still being built
        name = self.name.text().strip()
        location = self.output.text().strip()
        self.preview.setText(os.path.normpath(os.path.join(location, name))
                             if name and location else '')
        why = ps.convert_problem(self.project_file, location, name)
        self.problem.setText(why)
        self.share_button.setEnabled(not why)

    def _location_changed(self, _text):
        self.update_preview()

    def browse_output(self):
        start = self.output.text().strip()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            if parent == start:
                break
            start = parent
        folder = QFileDialog.getExistingDirectory(
            self, 'Store the workspace in', start or ws.documents_dir())
        if folder:
            self.output.setText(os.path.normpath(folder))

    # ------------------------------------------------------------------- convert

    def build_options(self):
        return None

    def share_clicked(self):
        location = self.output.text().strip()
        name = self.name.text().strip()
        why = ps.convert_problem(self.project_file, location, name)
        if why:
            QMessageBox.warning(self, 'Convert Project', why)
            return
        bad = [i for i in self.analysis.included() if not i.sharable]
        if bad:
            names = ', '.join(i.name for i in bad[:5]) + (' ...' if len(bad) > 5 else '')
            answer = QMessageBox.question(
                self, 'Convert Project',
                'These layers cannot be converted and will stay in the project '
                'flagged as broken: {}.\n\nConvert anyway?'.format(names),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        QgsSettings().setValue(SETTINGS_FOLDER, location)
        result = self.perform_convert(location, name)
        if result is not None:
            self.result = result
            ConvertResultDialog(result, self).exec()
            self.accept()

    def perform_convert(self, location, name):
        """Convert under a progress dialog; None if it failed or was canceled."""
        return run_convert(self, self.project, self.project_file, self.analysis,
                           location, name, self.copy_remote.isChecked())


def run_convert(parent, project, project_file, analysis, location, name,
                copy_remote=False):
    """Convert `project` under a progress dialog; a `ShareResult`, or None on failure."""
    progress = QProgressDialog('Converting...', 'Cancel', 0, 100, parent)
    progress.setWindowTitle('Convert Project to Workspace')
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

    feedback = dp.Feedback(on_progress=on_progress,
                           on_tick=QApplication.processEvents)
    progress.canceled.connect(feedback.cancel)
    progress.show()
    try:
        return ps.convert(project, project_file, analysis, location, name,
                          copy_remote, feedback)
    except pp.ProjectPackageError as error:
        if str(error) != 'Canceled.':
            QMessageBox.warning(parent, 'Convert Project', str(error))
        return None
    except Exception as error:
        _log('Convert Project failed:\n{}'.format(traceback.format_exc()),
             Qgis.MessageLevel.Critical)
        QMessageBox.critical(
            parent, 'Convert Project',
            'The conversion failed: {}\n\nSee the KGA Toolbox log for '
            'details.'.format(error))
        return None
    finally:
        progress.close()
        progress.deleteLater()


class ConvertResultDialog(QDialog):
    """Where the workspace went, and which layers could not come along."""

    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle('Project converted')
        self.setMinimumSize(640, 420)
        packaged = result.count(pp.STATUS_PACKAGED)
        services = result.count(pp.STATUS_SERVICE)
        bad = result.count(pp.STATUS_NOT_SHARABLE)
        head = QLabel('<b>{}</b><br>{}<br>{} layers moved into the workspace, {} kept as '
                      'web services, {} could not be converted.'.format(
                          os.path.basename(result.path), result.path, packaged,
                          services, bad))
        head.setWordWrap(True)
        head.setTextFormat(Qt.TextFormat.RichText)
        table = QTreeWidget()
        table.setRootIsDecorated(False)
        table.setHeaderLabels(['Layer', 'Result', 'Now stored as'])
        for entry in result.layers:
            item = QTreeWidgetItem(table, [
                entry['name'], entry['status'].replace('_', ' '),
                entry.get('packaged_as', '') or entry.get('source', '')])
            if entry['status'] == pp.STATUS_NOT_SHARABLE:
                for column in range(3):
                    item.setBackground(column, QBrush(BAD))
            item.setToolTip(0, '\n'.join(entry.get('warnings', [])))
        for column, width in enumerate((200, 110, 280)):
            table.setColumnWidth(column, width)
        warnings = QPlainTextEdit('\n'.join(result.warnings) or 'No warnings.')
        warnings.setReadOnly(True)
        warnings.setMaximumHeight(90)
        folder = QPushButton('Open folder')
        folder.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(result.path)))
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(folder)
        row.addStretch(1)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(table, 1)
        layout.addWidget(QLabel('Warnings'))
        layout.addWidget(warnings)
        layout.addLayout(row)
        self.size_text = human_size(result.size)

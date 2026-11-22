# -*- coding: utf-8 -*-
"""Project > Share Workspace: package the open project as a `.kgapp`.

Modelled on ArcGIS Pro's Share > Project Package pane. Two tabs:

* **Package** - what the package is called and says about itself (name, summary,
  tags, description, credits, use limitations), and where it is saved.
* **Contents** - Everything or Map data only, the options that go with each, and
  the layer table the analysis fills in: every layer, where its data lives, what
  will happen to it, how big it is and why it is included. A layer that cannot be
  shared is marked here, before anything is written.

The work is `core/project_share.py`; this window only gathers choices, runs it
under a progress dialog, and shows the result.
"""

import os
import traceback
from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .window_reveal import revealed_preview
from ..branding import LOG_TAG
from ..core import data_paste as dp
from ..core import project_analysis as pa
from ..core import project_share as ps
from ..core import projectpkg as pp

SETTINGS_AUTHOR = 'KGA/projectpackage/author'
SETTINGS_FOLDER = 'KGA/projectpackage/folder'

COL_INCLUDE, COL_LAYER, COL_SOURCE, COL_ACTION, COL_SIZE, COL_STATUS = range(6)

BAD = QColor(253, 232, 232)
SERVICE = QColor(238, 244, 251)
DIM = QColor(130, 130, 130)


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def human_size(size):
    size = float(size or 0)
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return '{:.0f} {}'.format(size, unit) if unit == 'bytes' \
                else '{:.1f} {}'.format(size, unit)
        size /= 1024.0


def default_author():
    saved = QgsSettings().value(SETTINGS_AUTHOR, '')
    if saved:
        return str(saved)
    return os.environ.get('USERNAME') or os.environ.get('USER') or ''


@revealed_preview
class ShareWorkspaceDialog(QDialog):

    def __init__(self, project, project_file, workspace, parent=None, extent=None,
                 iface=None):
        super().__init__(parent)
        self.project = project
        self.project_file = project_file
        self.workspace = workspace
        self.home = workspace.root if workspace is not None \
            else os.path.dirname(project_file)
        self.extent = extent
        self.iface = iface
        self.analysis = None
        self.result = None
        self.setWindowTitle('Share Workspace')
        self.setMinimumSize(780, 640)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_package_tab(), 'Package')
        self.tabs.addTab(self._build_contents_tab(), 'Contents')

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.share_button = self.buttons.addButton(
            'Share', QDialogButtonBox.ButtonRole.AcceptRole)
        self.share_button.clicked.connect(self.share_clicked)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.buttons)

        self.name.textChanged.connect(self._name_changed)
        self.reanalyze()

    # ------------------------------------------------------------ package tab

    def _build_package_tab(self):
        page = QWidget()
        name = self.workspace.name if self.workspace is not None else os.path.basename(self.home)
        stem = os.path.splitext(os.path.basename(self.project_file))[0]
        self.name = QLineEdit(stem or name)
        self.summary = QLineEdit()
        self.summary.setPlaceholderText('One line about what this project is')
        self.tags = QLineEdit()
        self.tags.setPlaceholderText('canal, irrigation, survey')
        self.author = QLineEdit(default_author())
        self.description = QPlainTextEdit()
        self.description.setFixedHeight(90)
        self.credits = QLineEdit()
        self.limitations = QLineEdit()
        self.limitations.setPlaceholderText('Terms of use, licence, confidentiality')

        self.output = QLineEdit(self._default_output(self.name.text()))
        browse = QPushButton('Browse...')
        browse.clicked.connect(self.browse_output)
        row = QHBoxLayout()
        row.addWidget(self.output, 1)
        row.addWidget(browse)

        form = QFormLayout()
        form.addRow('Name', self.name)
        form.addRow('Summary', self.summary)
        form.addRow('Tags', self.tags)
        form.addRow('Author', self.author)
        form.addRow('Description', self.description)
        form.addRow('Credits', self.credits)
        form.addRow('Use limitations', self.limitations)
        form.addRow('Save package as', row)

        note = QLabel(
            'The package is one .kgapp file holding the project, its layers\' '
            'data, layouts and styles. Whoever opens it with Project > Import '
            'Project Package gets a complete workspace.')
        note.setWordWrap(True)
        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _default_output(self, name):
        folder = QgsSettings().value(SETTINGS_FOLDER, '')
        if folder and os.path.isdir(str(folder)):
            return os.path.join(str(folder), ps.safe_name(name) + pp.EXTENSION)
        return ps.default_output_path(name)

    def _name_changed(self, text):
        # Keep the file name following the package name until the user picks one.
        current = self.output.text().strip()
        folder = os.path.dirname(current) if current else ''
        if folder:
            self.output.setText(os.path.join(
                folder, ps.safe_name(text) + pp.EXTENSION))

    def browse_output(self):
        start = self.output.text().strip() or ps.default_output_path(self.name.text())
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Save project package', start,
            'KGA project package (*{})'.format(pp.EXTENSION))
        if path:
            if not path.lower().endswith(pp.EXTENSION):
                path += pp.EXTENSION
            self.output.setText(os.path.normpath(path))

    # ----------------------------------------------------------- contents tab

    def _build_contents_tab(self):
        page = QWidget()
        self.mode_everything = QRadioButton(
            'Everything - the map, layouts and every file and table in the '
            'project folder and databases')
        self.mode_map = QRadioButton(
            'Map data only - the layers in the map and the data they need '
            '(joined and related tables, lookups ...)')
        self.mode_everything.setChecked(True)
        self.mode_everything.toggled.connect(self.mode_changed)

        self.include_toolbox = QCheckBox('Include the Toolbox scripts')
        self.include_toolbox.setChecked(True)
        self.include_layouts = QCheckBox('Include the Layouts folder')
        self.include_layouts.setChecked(True)
        self.copy_remote = QCheckBox(
            'Copy data from databases and web feature services into the package')
        self.copy_remote.setToolTip(
            'Without this, PostGIS, WFS and ArcGIS feature layers stay live '
            'connections that the recipient needs access to.')
        self.copy_remote.toggled.connect(self.reanalyze)
        self.make_thumbnail = QCheckBox('Include a thumbnail of the map')
        self.make_thumbnail.setChecked(True)
        self.save_report = QCheckBox('Save a report next to the package')
        self.save_report.setChecked(True)

        mode_box = QGroupBox('What to share')
        mode_layout = QVBoxLayout(mode_box)
        mode_layout.addWidget(self.mode_everything)
        mode_layout.addWidget(self.mode_map)

        options_box = QGroupBox('Options')
        options = QVBoxLayout(options_box)
        for widget in (self.include_toolbox, self.include_layouts,
                       self.copy_remote, self.make_thumbnail, self.save_report):
            options.addWidget(widget)

        top = QHBoxLayout()
        top.addWidget(mode_box, 3)
        top.addWidget(options_box, 2)

        self.table = QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(False)
        self.table.setHeaderLabels(['', 'Layer', 'Data source', 'What happens',
                                    'Size', 'Status'])
        self.table.setColumnWidth(COL_INCLUDE, 28)
        self.table.setColumnWidth(COL_LAYER, 150)
        self.table.setColumnWidth(COL_SOURCE, 150)
        self.table.setColumnWidth(COL_ACTION, 170)
        self.table.setColumnWidth(COL_SIZE, 70)
        self.table.itemChanged.connect(self.item_changed)

        tick = QPushButton('Tick all')
        tick.clicked.connect(lambda: self.set_all(True))
        untick = QPushButton('Untick all')
        untick.clicked.connect(lambda: self.set_all(False))
        reanalyze = QPushButton('Analyze again')
        reanalyze.clicked.connect(lambda _c=False: self.reanalyze())
        tools = QHBoxLayout()
        tools.addWidget(tick)
        tools.addWidget(untick)
        tools.addStretch(1)
        tools.addWidget(reanalyze)

        self.summary_label = QLabel('')
        self.summary_label.setWordWrap(True)

        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(tools)
        layout.addWidget(self.summary_label)
        return page

    # -------------------------------------------------------------- analysis

    @property
    def mode(self):
        return pp.MODE_EVERYTHING if self.mode_everything.isChecked() \
            else pp.MODE_MAP

    def reanalyze(self, *_args):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.analysis = pa.analyze(
                self.project, self.home, self.project_file,
                copy_remote=self.copy_remote.isChecked()).apply_mode(self.mode)
        except Exception:
            _log('Share Workspace analysis failed:\n{}'.format(
                traceback.format_exc()))
            self.analysis = pa.Analysis(self.home)
        finally:
            QApplication.restoreOverrideCursor()
        self.fill_table()

    def mode_changed(self, _checked=False):
        everything = self.mode == pp.MODE_EVERYTHING
        self.include_toolbox.setChecked(everything)
        self.include_layouts.setChecked(True)
        if self.analysis is not None:
            self.analysis.apply_mode(self.mode)
            self.fill_table()

    def fill_table(self):
        self.table.blockSignals(True)
        self.table.clear()
        for info in self.analysis.layers:
            item = QTreeWidgetItem(self.table)
            item.setData(0, Qt.ItemDataRole.UserRole, info.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self.paint_row(item, info)
        self.table.blockSignals(False)
        self.update_summary()

    def paint_row(self, item, info):
        item.setCheckState(COL_INCLUDE, Qt.CheckState.Checked if info.include
                           else Qt.CheckState.Unchecked)
        item.setText(COL_LAYER, info.name)
        source = info.category_label
        if info.path:
            source += ' - ' + os.path.basename(info.path)
        item.setText(COL_SOURCE, source)
        item.setText(COL_ACTION, info.action_label)
        item.setText(COL_SIZE, human_size(info.size) if info.size else '')
        if not info.sharable:
            first = info.warnings[0] if info.warnings else ''
            item.setText(COL_STATUS, 'Not shared: ' + first)
        elif info.warnings:
            item.setText(COL_STATUS, 'Warning: ' + info.warnings[0])
        else:
            item.setText(COL_STATUS, info.reason)
        tooltip = ['{} ({})'.format(info.name, info.reason)]
        tooltip += ['Warning: ' + w for w in info.warnings]
        tooltip += info.notes
        for column in range(6):
            item.setToolTip(column, '\n'.join(tooltip))
            if not info.sharable:
                item.setBackground(column, QBrush(BAD))
            elif info.action == pa.ACTION_SERVICE:
                item.setBackground(column, QBrush(SERVICE))
            if not info.include:
                item.setForeground(column, QBrush(DIM))

    def item_changed(self, item, column):
        if column != COL_INCLUDE:
            return
        info = self.analysis.by_id().get(item.data(0, Qt.ItemDataRole.UserRole))
        if info is None:
            return
        info.include = item.checkState(COL_INCLUDE) == Qt.CheckState.Checked
        self.table.blockSignals(True)
        self.paint_row(item, info)
        self.table.blockSignals(False)
        self.update_summary()

    def set_all(self, ticked):
        for info in self.analysis.layers:
            info.include = ticked
        self.fill_table()

    def update_summary(self):
        analysis = self.analysis
        included = analysis.included()
        bad = [i for i in included if not i.sharable]
        text = '{} of {} layers will be shared; about {} of layer data.'.format(
            len(included), len(analysis.layers), human_size(analysis.estimated_size()))
        if self.mode == pp.MODE_EVERYTHING:
            text += ' Files and tables in the project folder come too.'
        if bad:
            text += ('<br><b style="color:#b00020">{} layer{} cannot be shared '
                     '</b>(data source not found or not supported); {} kept in '
                     'the project, flagged.'.format(
                         len(bad), '' if len(bad) == 1 else 's',
                         'it is' if len(bad) == 1 else 'they are'))
        self.summary_label.setText(text)

    # ----------------------------------------------------------------- sharing

    def build_options(self):
        info = pp.PackageInfo(
            name=self.name.text().strip(), summary=self.summary.text().strip(),
            description=self.description.toPlainText().strip(),
            tags=self.tags.text(), author=self.author.text().strip(),
            credits=self.credits.text().strip(),
            use_limitations=self.limitations.text().strip())
        workspace = self.workspace
        return ps.ShareOptions(
            self.output.text().strip(), mode=self.mode, info=info,
            include_toolbox=self.include_toolbox.isChecked(),
            include_layouts_folder=self.include_layouts.isChecked(),
            copy_remote=self.copy_remote.isChecked(),
            attached_databases=list(workspace.databases) if workspace else [],
            default_database=workspace.default_database if workspace else '',
            default_folder=workspace.default_folder if workspace else '')

    def share_clicked(self):
        options = self.build_options()
        why = ps.problem(self.project, self.project_file, options)
        if why:
            QMessageBox.warning(self, 'Share Workspace', why)
            return
        if os.path.exists(options.output):
            answer = QMessageBox.question(
                self, 'Share Workspace',
                '"{}" already exists. Replace it?'.format(options.output),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        bad = [i for i in self.analysis.included() if not i.sharable]
        if bad:
            names = ', '.join(i.name for i in bad[:5]) + (' ...' if len(bad) > 5 else '')
            answer = QMessageBox.question(
                self, 'Share Workspace',
                'These layers cannot be shared and will stay in the project '
                'flagged as broken: {}.\n\nShare anyway?'.format(names),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        QgsSettings().setValue(SETTINGS_AUTHOR, options.info.author)
        QgsSettings().setValue(SETTINGS_FOLDER,
                               os.path.dirname(os.path.abspath(options.output)))
        result = self.perform(options)
        if result is not None:
            self.result = result
            ShareResultDialog(result, self.save_report.isChecked(), self).exec()
            self.accept()

    def perform(self, options):
        """Run the share under a progress dialog; None if it failed or was canceled."""
        if self.make_thumbnail.isChecked():
            options.thumbnail = ps.render_thumbnail(self.project, extent=self.extent)
        progress = QProgressDialog('Sharing...', 'Cancel', 0, 100, self)
        progress.setWindowTitle('Share Workspace')
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
            return ps.share(self.project, self.project_file, self.home,
                            self.analysis, options, feedback)
        except pp.ProjectPackageError as error:
            if str(error) != 'Canceled.':
                QMessageBox.warning(self, 'Share Workspace', str(error))
            return None
        except Exception as error:
            _log('Share Workspace failed:\n{}'.format(traceback.format_exc()),
                 Qgis.MessageLevel.Critical)
            QMessageBox.critical(
                self, 'Share Workspace',
                'Sharing failed: {}\n\nSee the KGA Toolbox log for details.'.format(
                    error))
            return None
        finally:
            progress.close()
            progress.deleteLater()


class ShareResultDialog(QDialog):
    """What was shared, what was not, and every warning."""

    def __init__(self, result, save_report=True, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle('Project package created')
        self.setMinimumSize(700, 480)
        packaged = result.count(pp.STATUS_PACKAGED)
        services = result.count(pp.STATUS_SERVICE)
        bad = result.count(pp.STATUS_NOT_SHARABLE)
        skipped = result.count(pp.STATUS_EXCLUDED)
        head = QLabel(
            '<b>{}</b><br>{}<br>{} layers packaged, {} kept as web services, '
            '{} not shareable, {} left out.'.format(
                os.path.basename(result.path), human_size(result.size),
                packaged, services, bad, skipped))
        head.setTextFormat(Qt.TextFormat.RichText)

        table = QTreeWidget()
        table.setRootIsDecorated(False)
        table.setHeaderLabels(['Layer', 'Result', 'Why', 'Packaged as'])
        for entry in result.layers:
            item = QTreeWidgetItem(table, [
                entry['name'], entry['status'].replace('_', ' '),
                entry.get('reason', ''), entry.get('packaged_as', '')])
            if entry['status'] == pp.STATUS_NOT_SHARABLE:
                for column in range(4):
                    item.setBackground(column, QBrush(BAD))
            item.setToolTip(0, '\n'.join(entry.get('warnings', [])))
        for column, width in enumerate((170, 100, 220, 180)):
            table.setColumnWidth(column, width)

        warnings = QPlainTextEdit('\n'.join(result.warnings) or 'No warnings.')
        warnings.setReadOnly(True)
        warnings.setMaximumHeight(120)

        folder = QPushButton('Open folder')
        folder.clicked.connect(self.open_folder)
        report = QPushButton('Save report...')
        report.clicked.connect(self.save_report_clicked)
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(folder)
        row.addWidget(report)
        row.addStretch(1)
        row.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(head)
        layout.addWidget(table, 1)
        layout.addWidget(QLabel('Warnings'))
        layout.addWidget(warnings)
        layout.addLayout(row)

        self.report_path = ''
        if save_report:
            self.report_path = self.write_report(
                os.path.splitext(result.path)[0] + '.report.html')

    def write_report(self, path):
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(ps.report_html(self.result))
        except OSError as error:
            _log('Could not write the report {}: {}'.format(path, error))
            return ''
        return path

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(
            os.path.dirname(self.result.path)))

    def save_report_clicked(self):
        start = self.report_path or os.path.splitext(self.result.path)[0] + '.report.html'
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Save report', start, 'HTML (*.html)')
        if path and self.write_report(path):
            with suppress(Exception):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

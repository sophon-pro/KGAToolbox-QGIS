# -*- coding: utf-8 -*-
"""Do not let scratch (memory) layers vanish when the project is closed.

A scratch layer keeps its features only in QGIS's memory, so closing QGIS,
opening another project or starting a new one throws them away. This guard
asks first: save them to the project GeoPackage, to another GeoPackage, or let
them go. *Settings > Projects > Closing projects* can answer for the user
(always add them to the project GeoPackage, or never ask).

Two places are watched:

* the main window's close event, where **Cancel** can still keep QGIS open;
* `QgsProject.aboutToBeCleared`, which covers Project > New / Open / Close and
  opening a recent project. QGIS has no way to cancel there, so the question
  has no Cancel button; the layers are still alive when the signal fires.

A window close is handled first and remembered, so the clear that follows does
not ask a second time.
"""

import os
import traceback
from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog, QgsProject
from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..branding import LOG_TAG
from ..core import kga_settings as ks
from ..core import scratch_layers as sl

CHOICE_PROJECT = 'project'
CHOICE_OTHER = 'other'
CHOICE_DISCARD = 'discard'
CHOICE_CANCEL = 'cancel'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def project_database(workspace):
    """The GeoPackage that belongs to the project, or '' when there is none.

    The scratch space of an unsaved project is not one: it is deleted with the
    session, so writing there would only move the loss.
    """
    if workspace is None or workspace.is_untitled:
        return ''
    return workspace.default_database or workspace.database


class ScratchDialog(QDialog):
    """Which scratch layers to keep, and where."""

    def __init__(self, layers, database, cancelable, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Scratch Layers')
        self.choice = CHOICE_CANCEL
        lay = QVBoxLayout(self)
        head = QLabel(
            '<b>These scratch layers exist only in memory.</b><br>Their '
            'features will be lost when the project is closed.')
        head.setWordWrap(True)
        lay.addWidget(head)
        self.listing = QListWidget()
        for layer in layers:
            count = layer.featureCount()
            item = QListWidgetItem('{}  ({} feature{})'.format(
                layer.name(), count, '' if count == 1 else 's'))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if sl.has_content(layer)
                               else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            self.listing.addItem(item)
        lay.addWidget(self.listing)
        if database:
            where = QLabel('Project GeoPackage:<br>{}'.format(
                os.path.normpath(database)))
        else:
            where = QLabel(
                'This project is not in a workspace yet, so it has no project '
                'GeoPackage. Choose a GeoPackage to save them in.')
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(where)
        self.always = QCheckBox(
            'Add them to the project GeoPackage automatically from now on')
        self.always.setVisible(bool(database))
        lay.addWidget(self.always)
        # A plain row, not a QDialogButtonBox: the box reorders buttons by
        # role, and Don't Save has to sit right next to Cancel.
        buttons = QHBoxLayout()
        if database:
            self._add(buttons, 'Save to Project GeoPackage', CHOICE_PROJECT,
                      default=True)
        self._add(buttons, 'Save to Another GeoPackage...', CHOICE_OTHER,
                  default=not database)
        buttons.addStretch(1)
        self._add(buttons, "Don't Save", CHOICE_DISCARD)
        if cancelable:
            self._add(buttons, 'Cancel', CHOICE_CANCEL)
        lay.addLayout(buttons)
        self.resize(480, 360)

    def _add(self, row, text, choice, default=False):
        button = QPushButton(text)
        row.addWidget(button)
        button.clicked.connect(lambda _checked=False, c=choice: self._pick(c))
        if default:
            button.setDefault(True)

    def _pick(self, choice):
        self.choice = choice
        if choice == CHOICE_CANCEL:
            self.reject()
        else:
            self.accept()

    def reject(self):
        self.choice = CHOICE_CANCEL if self.choice not in (
            CHOICE_PROJECT, CHOICE_OTHER, CHOICE_DISCARD) else self.choice
        super().reject()

    def checked_ids(self):
        return {self.listing.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.listing.count())
                if self.listing.item(i).checkState() == Qt.CheckState.Checked}


class ScratchCloseGuard(QObject):

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._window = None
        self._busy = False
        self._decided = {}          # layer id -> feature count when the user decided
        self._connected = False

    # ------------------------------------------------------------- lifecycle

    def install(self):
        self._window = self.controller.iface.mainWindow()
        self._window.installEventFilter(self)
        QgsProject.instance().aboutToBeCleared.connect(self._about_to_clear)
        QgsProject.instance().cleared.connect(self._decided.clear)
        self._connected = True

    def uninstall(self):
        if self._window is not None:
            with suppress(RuntimeError):
                self._window.removeEventFilter(self)
            self._window = None
        if self._connected:
            project = QgsProject.instance()
            with suppress(RuntimeError, TypeError):
                project.aboutToBeCleared.disconnect(self._about_to_clear)
            with suppress(RuntimeError, TypeError):
                project.cleared.disconnect(self._decided.clear)
            self._connected = False

    # ----------------------------------------------------------------- hooks

    def eventFilter(self, obj, event):           # noqa: N802 - Qt naming
        if obj is self._window and event.type() == QEvent.Type.Close:
            try:
                if not self.handle(cancelable=True):
                    event.ignore()
                    return True
            except Exception:
                _log('Scratch layer check failed:\n{}'.format(traceback.format_exc()))
        return False

    def _about_to_clear(self):
        try:
            self.handle(cancelable=False)
        except Exception:
            _log('Scratch layer check failed:\n{}'.format(traceback.format_exc()))

    # ------------------------------------------------------------- decisions

    def pending(self):
        """Scratch layers not yet answered for (or changed since the answer)."""
        return [layer for layer in sl.scratch_layers()
                if self._decided.get(layer.id()) != layer.featureCount()]

    def handle(self, cancelable):
        """Deal with the scratch layers. False only when the user cancelled the close."""
        if self._busy or ks.scratch_on_close() == ks.SCRATCH_OFF:
            return True
        layers = self.pending()
        if not any(sl.has_content(layer) for layer in layers):
            return True
        self._busy = True
        try:
            return self._handle(layers, cancelable)
        finally:
            self._busy = False

    def _handle(self, layers, cancelable):
        database = project_database(self.controller.workspace)
        if database and ks.scratch_on_close() == ks.SCRATCH_PROJECT:
            chosen = [layer for layer in layers if sl.has_content(layer)]
            return self._save(chosen, database, layers, cancelable)
        window = self.controller.iface.mainWindow()
        dialog = ScratchDialog(layers, database, cancelable, window)
        dialog.exec()
        choice = dialog.choice
        if choice == CHOICE_CANCEL:
            return not cancelable
        ids = dialog.checked_ids()
        chosen = [layer for layer in layers if layer.id() in ids]
        if choice == CHOICE_DISCARD:
            self._let_go(layers)
            return True
        if choice == CHOICE_PROJECT:
            if dialog.always.isChecked():
                ks.set_value(ks.KEY_SCRATCH_ON_CLOSE, ks.SCRATCH_PROJECT)
            return self._save(chosen, database, layers, cancelable)
        path = self._ask_path()
        if not path:
            return not cancelable           # backed out of the file dialog
        return self._save(chosen, path, layers, cancelable)

    def _ask_path(self):
        workspace = self.controller.workspace
        start = ''
        if workspace is not None and not workspace.is_untitled:
            start = workspace.root
        path, _filter = QFileDialog.getSaveFileName(
            self.controller.iface.mainWindow(), 'Save Scratch Layers To',
            start, 'GeoPackage (*.gpkg)',
            options=QFileDialog.Option.DontConfirmOverwrite)
        if path and not path.lower().endswith('.gpkg'):
            path += '.gpkg'
        return os.path.normpath(path) if path else ''

    def _let_go(self, layers):
        sl.mark_discarded(layers)
        self._decided.update({layer.id(): layer.featureCount() for layer in layers})

    def _save(self, chosen, path, all_layers, cancelable):
        """Write the chosen layers; the rest are let go. False when the close should stop."""
        result = sl.save_layers(chosen, path)
        self._let_go([layer for layer in all_layers
                      if layer not in chosen])
        if result.failed:
            detail = '\n'.join('  - {}: {}'.format(name, why)
                               for name, why in result.failed.items())
            box = QMessageBox(self.controller.iface.mainWindow())
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle('Scratch Layers')
            box.setText('Some scratch layers could not be saved.')
            box.setInformativeText(
                '{}\n\nThey are still in memory.{}'.format(
                    detail, ' The project was not closed.' if cancelable else ''))
            box.exec()
            return not cancelable
        with suppress(Exception):
            self.controller.iface.messageBar().pushSuccess(
                'KGA Toolbox', '{} scratch layer{} saved to {}'.format(
                    len(result.saved), '' if len(result.saved) == 1 else 's',
                    os.path.basename(path)))
        return True

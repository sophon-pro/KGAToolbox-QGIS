# -*- coding: utf-8 -*-
"""Offer to convert a traditional project to a KGA workspace as it is opened.

*Project > Open Recent* is watched: before QGIS opens a project that is not
already in a KGA workspace, the user is asked whether to convert it. **Yes** asks
where to put the workspace (alongside the original project, or in a folder of
their choice), converts, and opens the workspace instead; **No** lets QGIS open
the project as a traditional one, exactly as it would have.

QGIS gives a plugin no "about to open project" signal, and its Open Recent slot
cannot be put in front of. So an event filter on the menu catches the click (or
Enter) *before* QGIS sees it, swallows it, closes the menus, and decides in the
next turn of the event loop; "No" simply triggers the same menu action again, so
QGIS's own logic (save-changes prompt, recent list, messages) is untouched.

Other ways of opening a project (File > Open, drag and drop, the welcome page, a
.qgz double-clicked in Explorer) have no such hook. For those the same question is
asked just after the project has loaded; "Yes" then converts and reopens it. Both
paths share one switch, the *Ask to convert* item in the Catalog's menu.
"""

import os
import re
import traceback

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QMenu,
    QMessageBox,
)

from ..branding import LOG_TAG
from ..core import projectpkg as pp
from ..core import workspace as ws

SETTING_ASK = 'KGA/catalog/ask_convert'
PROJECT_SUFFIXES = ('.qgz', '.qgs')

ANSWER_YES = 'yes'
ANSWER_NO = 'no'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def asking_enabled():
    return QgsSettings().value(SETTING_ASK, True, type=bool)


def set_asking(enabled):
    QgsSettings().setValue(SETTING_ASK, bool(enabled))


def _key(path):
    return os.path.normcase(os.path.normpath(path))


def recent_path(action):
    """The project path an Open Recent action stands for, or ''.

    The action's text is "<name> (<path>)"; its data is only an index. The
    path itself may hold parentheses, so each " (" is tried as the start of it.
    """
    if action is None or action.isSeparator() or not action.isEnabled():
        return ''
    text = action.text().rstrip()
    if not text.endswith(')'):
        return ''
    text = text.replace('&&', '&')              # a menu spells a literal & as &&
    for match in re.finditer(r' \(', text):
        candidate = os.path.normpath(text[match.end():-1].strip())
        if candidate.lower().endswith(PROJECT_SUFFIXES) and os.path.isfile(candidate):
            return candidate
    return ''


def default_location(path):
    """Alongside the original project: its own folder."""
    return os.path.dirname(os.path.abspath(path))


def suggested_name(path, location):
    stem = os.path.splitext(os.path.basename(path))[0]
    if ws.name_problem(stem):
        stem = 'Project'
    return pp.unique_folder_name(location, stem)


class ProjectOpenGuard(QObject):

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.menu = None
        self._busy = False          # a question is up; the open it leads to must not ask again
        self._connected = False

    # ------------------------------------------------------------- lifecycle

    def install(self):
        window = self.controller.iface.mainWindow()
        self.menu = window.findChild(QMenu, 'mRecentProjectsMenu')
        if self.menu is not None:
            self.menu.installEventFilter(self)
        QgsProject.instance().readProject.connect(self._project_read)
        self._connected = True

    def uninstall(self):
        if self.menu is not None:
            try:
                self.menu.removeEventFilter(self)
            except RuntimeError:            # the menu is already gone
                pass
            self.menu = None
        if self._connected:
            try:
                QgsProject.instance().readProject.disconnect(self._project_read)
            except (TypeError, RuntimeError):
                pass
            self._connected = False

    # -------------------------------------------------------------- questions

    def eligible(self, path):
        """True when `path` is a project file that is not in a KGA workspace."""
        return bool(path) and asking_enabled() \
            and path.lower().endswith(PROJECT_SUFFIXES) \
            and os.path.isfile(path) and ws.open_workspace(path) is None

    def ask_convert(self, path):
        """Yes / No to "convert this project?". Also stores "don't ask again"."""
        box = QMessageBox(self.controller.iface.mainWindow())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle('Open Project')
        box.setText('"{}" is a traditional QGIS project, not a KGA workspace.'
                    '\n\nConvert it to a workspace?'.format(os.path.basename(path)))
        box.setInformativeText(
            'A workspace keeps the project, its data, a default GeoPackage, a '
            'Toolbox and your layouts together in one folder.\n\nYes converts it '
            'and opens the workspace. No opens it as a traditional QGIS '
            'project.')
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        box.setEscapeButton(QMessageBox.StandardButton.No)
        again = QCheckBox("Don't ask me again")
        box.setCheckBox(again)
        answer = box.exec()
        if again.isChecked():
            set_asking(False)
        return ANSWER_YES if answer == QMessageBox.StandardButton.Yes else ANSWER_NO

    def ask_location(self, path):
        """(location, name) for the new workspace, or None if the user backs out."""
        alongside = default_location(path)
        name = suggested_name(path, alongside)
        box = QMessageBox(self.controller.iface.mainWindow())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle('Convert to Workspace')
        box.setText('Where should the workspace be created?')
        box.setInformativeText(
            'Alongside the original project:\n{}'.format(
                os.path.join(alongside, name)))
        here = box.addButton('Alongside the project',
                             QMessageBox.ButtonRole.AcceptRole)
        choose = box.addButton('Choose a folder...',
                               QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(here)
        box.exec()
        clicked = box.clickedButton()
        if clicked is here:
            return alongside, name
        if clicked is choose:
            folder = QFileDialog.getExistingDirectory(
                self.controller.iface.mainWindow(),
                'Folder for the new workspace', alongside)
            if not folder:
                return None
            folder = os.path.normpath(folder)
            return folder, suggested_name(path, folder)
        return None

    # --------------------------------------------------------------- the menu

    def eventFilter(self, obj, event):           # noqa: N802 - Qt naming
        if obj is not self.menu or self._busy:
            return False
        kind = event.type()
        try:
            if kind == QEvent.Type.MouseButtonRelease \
                    and event.button() == Qt.MouseButton.LeftButton:
                action = self.menu.actionAt(event.pos())
            elif kind == QEvent.Type.KeyPress and event.key() in (
                    Qt.Key.Key_Return, Qt.Key.Key_Enter):
                action = self.menu.activeAction()
            else:
                return False
            path = recent_path(action)
            if not path or not self.eligible(path):
                return False
        except Exception:
            _log('Open Recent check failed:\n{}'.format(traceback.format_exc()))
            return False
        # Take the click: QGIS must not open the project yet. Menus are closed
        # now and the question is asked once the click has been fully handled.
        self._busy = True
        self._close_popups()
        QTimer.singleShot(0, lambda: self._handle_menu(action, path))
        return True

    @staticmethod
    def _close_popups():
        popup = QApplication.activePopupWidget()
        while popup is not None:
            popup.close()
            following = QApplication.activePopupWidget()
            popup = following if following is not popup else None

    def _handle_menu(self, action, path):
        try:
            if self.ask_convert(path) == ANSWER_YES:
                target = self.ask_location(path)
                if target is None:
                    return                      # backed out: nothing opens
                if self.controller.convert_and_open(path, *target):
                    return
                # The conversion failed (the user has been told); fall through
                # and open the project the traditional way.
            self._open_traditionally(action)
        except Exception:
            _log('Opening a recent project failed:\n{}'.format(
                traceback.format_exc()))
            self._open_traditionally(action)
        finally:
            self._busy = False

    @staticmethod
    def _open_traditionally(action):
        """Let QGIS do what the click would have done."""
        try:
            action.trigger()
        except RuntimeError:                    # the menu was rebuilt meanwhile
            pass

    # ------------------------------------------------------- everything else

    def _project_read(self, _document=None):
        path = QgsProject.instance().fileName()
        if self._busy:
            return                  # this is the open that follows a question
        if self.eligible(path):
            path = os.path.normpath(path)
            QTimer.singleShot(0, lambda: self._offer_after_open(path))

    def _offer_after_open(self, path):
        if self._busy or _key(QgsProject.instance().fileName() or '') != _key(path) \
                or not self.eligible(path):
            return
        self._busy = True
        try:
            if self.ask_convert(path) != ANSWER_YES:
                return
            target = self.ask_location(path)
            if target is not None:
                self.controller.convert_and_open(path, *target)
        except Exception:
            _log('Convert after opening failed:\n{}'.format(traceback.format_exc()))
        finally:
            self._busy = False

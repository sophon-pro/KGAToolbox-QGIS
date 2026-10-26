# -*- coding: utf-8 -*-
"""Save and Save As offer to save as a KGA workspace, not only a plain `.qgz`.

QGIS gives a plugin no "about to save" hook and its Save dialog is a native one
that cannot be put in front of. So the two actions are stood in for: a proxy
`Save` and `Save As...` take the originals' place in the Project menu and in
any toolbar holding them, and take over their shortcuts (Ctrl+S, Ctrl+Shift+S).
The originals are hidden, never replaced, so QGIS's own save logic (backup,
messages, the dirty flag, the recent list) still does the saving.

    Save        project already has a file  -> QGIS saves it, no question
                project is new              -> workspace or traditional?
    Save As     project is in a workspace   -> QGIS's own Save As
                anything else               -> workspace or traditional?

"Workspace" is Project > Set Workspace (which also moves the Untitled work in);
"traditional" hands over to QGIS's own Save As dialog.

Saving that QGIS starts itself (the prompt on closing QGIS) does not pass
through here and stays a traditional save.
"""

import traceback
from contextlib import suppress

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolBar

from ..branding import LOG_TAG
from ..core import kga_settings as ks
from ..core import workspace as ws

CHOICE_WORKSPACE = 'workspace'
CHOICE_TRADITIONAL = 'traditional'


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


class _Stand(object):
    """One original action and the proxy that stands in for it."""

    def __init__(self, original, proxy, shortcuts, places, mirror):
        self.original = original
        self.mirror = mirror
        self.proxy = proxy
        self.shortcuts = shortcuts
        self.places = places        # menus / toolbars the proxy was put into


class SaveGuard(QObject):

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._stands = []
        self._save = None
        self._save_as = None

    # ------------------------------------------------------------- lifecycle

    def install(self):
        iface = self.controller.iface
        window = iface.mainWindow()
        save = iface.actionSaveProject()
        save_as = getattr(iface, 'actionSaveProjectAs', lambda: None)()
        for original, handler in ((save, self.save), (save_as, self.save_as)):
            if original is None:
                continue
            self._stands.append(self._stand_in(
                window, original, handler, always_when_unsaved=original is save))
        self._save, self._save_as = save, save_as
        project = QgsProject.instance()
        project.isDirtyChanged.connect(self._refresh)
        project.fileNameChanged.connect(self._refresh)
        self._refresh()

    def _stand_in(self, window, original, handler, always_when_unsaved=False):
        proxy = QAction(original.icon(), original.text(), window)
        proxy.setToolTip(original.toolTip())
        proxy.setStatusTip(original.statusTip())
        proxy.setEnabled(original.isEnabled())
        proxy.setProperty('kga_always_when_unsaved', always_when_unsaved)
        shortcuts = original.shortcuts()
        proxy.setShortcuts(shortcuts)
        proxy.setShortcutContext(original.shortcutContext())
        original.setShortcuts([])
        places = []
        menu = self.controller.iface.projectMenu()
        holders = [menu] + window.findChildren(QToolBar)
        for holder in holders:
            if original in holder.actions():
                holder.insertAction(original, proxy)
                places.append(holder)
        original.setVisible(False)

        def mirror():
            self._mirror(original, proxy)

        original.changed.connect(mirror)
        proxy.triggered.connect(lambda _checked=False: handler())
        return _Stand(original, proxy, shortcuts, places, mirror)

    @staticmethod
    def _mirror(original, proxy):
        proxy.setEnabled(SaveGuard._wanted(original, proxy))
        proxy.setText(original.text())
        proxy.setIcon(original.icon())

    @staticmethod
    def _wanted(original, proxy):
        """Whether the stand-in is usable.

        QGIS's own Save can sit disabled while the project has changes (and
        for a project that was never saved), and the stand-in used to copy
        that, leaving Save greyed out until QGIS was closed. Save is usable
        whenever there is something to save.
        """
        if original.isEnabled():
            return True
        if not proxy.property('kga_always_when_unsaved'):
            return False
        project = QgsProject.instance()
        return project.isDirty() or not project.fileName()

    def _refresh(self, *_args):
        for stand in self._stands:
            with suppress(RuntimeError):
                stand.proxy.setEnabled(self._wanted(stand.original, stand.proxy))

    @staticmethod
    def _fire(action):
        """Trigger `action` even when QGIS has it disabled (a disabled one ignores it)."""
        if action.isEnabled():
            action.trigger()
            return
        action.setEnabled(True)
        try:
            action.trigger()
        finally:
            with suppress(RuntimeError):
                action.setEnabled(False)

    def uninstall(self):
        project = QgsProject.instance()
        for signal in (project.isDirtyChanged, project.fileNameChanged):
            with suppress(RuntimeError, TypeError):
                signal.disconnect(self._refresh)
        for stand in self._stands:
            with suppress(RuntimeError, TypeError):
                stand.original.changed.disconnect(stand.mirror)
            with suppress(RuntimeError):
                for holder in stand.places:
                    holder.removeAction(stand.proxy)
                stand.proxy.deleteLater()
                stand.original.setShortcuts(stand.shortcuts)
                stand.original.setVisible(True)
        self._stands = []
        self._save = self._save_as = None

    # --------------------------------------------------------------- actions

    @staticmethod
    def pending_layers():
        """Vector layers being edited that hold edits not yet written."""
        return [layer for layer in QgsProject.instance().mapLayers().values()
                if isinstance(layer, QgsVectorLayer)
                and layer.isEditable() and layer.isModified()]

    def _edits_saved(self):
        """True when no layer has unsaved edits, or the user saved them now.

        The project must not be saved while features are mid-edit: the project
        file would point at data that does not yet hold those edits. The user
        is asked to save the edits first; cancelling stops the project save.
        """
        pending = self.pending_layers()
        if not pending:
            return True
        window = self.controller.iface.mainWindow()
        names = '\n'.join('  - {}'.format(layer.name()) for layer in pending)
        box = QMessageBox(window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('Unsaved Edits')
        box.setText('Save your edits before saving the project.')
        box.setInformativeText(
            'These layers have edits that are not saved yet:\n{}\n\n'
            'Save the edits first, then the project will be saved.'.format(names))
        save = box.addButton('Save Edits', QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save)
        box.exec()
        if box.clickedButton() is not save:
            return False
        failed = {}
        for layer in pending:
            if not layer.commitChanges(False):          # stay in edit mode
                failed[layer.name()] = list(layer.commitErrors())
            layer.triggerRepaint()
        if failed:
            detail = '\n\n'.join('{}:\n{}'.format(name, '\n'.join(errors))
                                 for name, errors in failed.items())
            QMessageBox.warning(
                window, 'Unsaved Edits',
                'Some edits could not be saved, so the project was not saved.\n\n'
                + detail)
            return False
        return True

    def save(self):
        if not self._edits_saved():
            return
        if QgsProject.instance().fileName():
            self._fire(self._save)
        else:
            self._ask_and_save()

    def save_as(self):
        if not self._edits_saved():
            return
        path = QgsProject.instance().fileName()
        if path and ws.open_workspace(path) is not None:
            self._traditional()
        else:
            self._ask_and_save()

    def _ask_and_save(self):
        choice = self.ask_kind()
        if choice == CHOICE_WORKSPACE:
            self.controller.set_workspace_dialog()
        elif choice == CHOICE_TRADITIONAL:
            self._traditional()

    def _traditional(self):
        """QGIS's own Save As (Save, for a project with no file, opens the same)."""
        try:
            self._fire(self._save_as or self._save)
        except Exception:
            _log('Could not start the QGIS save:\n{}'.format(traceback.format_exc()))

    def ask_kind(self):
        """'workspace', 'traditional' or None (cancelled).

        Settings > Projects can answer for the user; otherwise they are asked.
        """
        mode = ks.save_mode()
        if mode == ks.SAVE_WORKSPACE:
            return CHOICE_WORKSPACE
        if mode == ks.SAVE_TRADITIONAL:
            return CHOICE_TRADITIONAL
        box = QMessageBox(self.controller.iface.mainWindow())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle('Save Project')
        box.setText('How do you want to save this project?')
        box.setInformativeText(
            'A workspace is a folder that keeps the project, a default '
            'GeoPackage, the data and output made so far, a Toolbox and your '
            'layouts together.\n\nA traditional project is a single .qgz file.')
        workspace = box.addButton('Save as Workspace...',
                                  QMessageBox.ButtonRole.AcceptRole)
        traditional = box.addButton('Save as QGIS Project (.qgz)...',
                                    QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(workspace)
        box.exec()
        clicked = box.clickedButton()
        if clicked is workspace:
            return CHOICE_WORKSPACE
        if clicked is traditional:
            return CHOICE_TRADITIONAL
        return None

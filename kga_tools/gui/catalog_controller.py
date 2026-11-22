# -*- coding: utf-8 -*-
"""Owns the Catalog: the panel, the workspace, and Project > Set / Open Workspace.

`KgaToolsPlugin` creates one `CatalogController`, asks for its toggle action to
put on the toolbar, and calls `unload()` on the way out. Everything else - which
workspace is open, when it changes, what the right-click entries do - lives here.

A workspace follows the project: a saved `.qgz` that sits in a workspace folder
makes that workspace current; a new or unrelated project falls back to the
scratch `Untitled` workspace (see core/workspace.py) that is made fresh each
session, emptied into a workspace by Set Workspace, and offered back if it
was left holding work.
"""

import os
import traceback
from contextlib import suppress
from datetime import datetime

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsProject, QgsSettings
from qgis.gui import QgsGui
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QFileSystemWatcher, QObject, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAction,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QListWidget,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..branding import LOG_TAG
from ..branding import icon as _icon
from ..core import geometry_fields
from ..core import output_defaults as od
from ..core import toolbox_scripts as ts
from ..core import workspace as ws
from . import catalog_items as ci
from . import map_tabs
from . import processing_defaults
from ..core import layout_import, layout_shortcuts, map_shortcuts
from . import new_layout_dialog, new_project_basemap
from .browser_layouts import LayoutShortcutInstaller
from .browser_textfiles import TextFileInstaller
from .browser_project_package import ProjectPackageInstaller
from .catalog_menu import CatalogMenu
from .project_open_guard import ProjectOpenGuard
from .save_guard import SaveGuard
from .scratch_close_guard import ScratchCloseGuard
from .catalog_panel import (
    TAB_COMPUTER,
    TAB_PROJECT,
    CatalogPanel,
    build_model,
    tab_of_root,
)
from .workspace_dialog import SetWorkspaceDialog

#: Recovery banner: after QGIS reports start-up done, and the latest it waits.
RECOVERY_SETTLE_MS = 3000
RECOVERY_FALLBACK_MS = 12000

SETTINGS_FIRST_RUN = 'KGA/catalog/first_run_done'

# QGIS remembers the last folder each kind of file dialog used; these are the
# ones that open a tool's output chooser, a Save As, or a file field.
LAST_FOLDER_KEYS = (
    'Processing/LastOutputPath',
    'UI/lastVectorFileFilterDir',
    'UI/lastRasterFileFilterDir',
    'UI/lastFileNameWidgetDir',
)


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


class CatalogController(QObject):

    def __init__(self, iface, parent=None, group=None):
        super().__init__(parent)
        self.iface = iface
        self.group = group            # BubbleGroup shared with the bags (None: no bubble)
        self.bubble = None            # CatalogBubbleManager
        self.workspace = None
        self.model = None
        self.panel = None
        self.toggle_action = None
        self.set_action = None
        self.open_action = None
        self.share_action = None
        self.import_action = None
        self.convert_action = None
        self._separator = None
        self._providers = []
        self._menu = None
        self._package_items = None
        self._layout_items = None
        self._text_items = None
        self._layout_timer = None
        self._open_guard = None
        self._save_guard = None
        self._scratch_guard = None
        self._untitled = None
        self._recoverable = []
        self._offered = False
        self._offer_widget = None
        self._nudged = None
        self._listed = None
        self._connected = False
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._toolbox_changed)
        self._toolbox_timer = QTimer(self)
        self._toolbox_timer.setSingleShot(True)
        self._toolbox_timer.setInterval(400)
        self._toolbox_timer.timeout.connect(self._refresh_toolbox_section)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self.sync_with_project)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(300)
        self._layout_timer.timeout.connect(self.sync_layouts)
        self._maps_timer = QTimer(self)
        self._maps_timer.setSingleShot(True)
        self._maps_timer.setInterval(200)
        self._maps_timer.timeout.connect(self._refresh_maps_section)

    # ------------------------------------------------------------------ setup

    def initGui(self):
        self._providers = [ci.ProjectProvider(), ci.ComputerProvider()]
        for provider in self._providers:
            QgsApplication.dataItemProviderRegistry().addProvider(provider)

        self._untitled = self._make_untitled()
        self.workspace = self._workspace_for_project() or self._untitled
        self._register_toolbox()
        self._apply_output_defaults()

        self.model = build_model(self.iface, self.workspace)
        self.panel = CatalogPanel(self.iface, self.model,
                                  self.iface.mainWindow())
        self.panel.setWorkspaceRequested.connect(self.set_workspace_dialog)
        self.panel.openWorkspaceRequested.connect(self.open_workspace_dialog)
        self.panel.convertProjectRequested.connect(self.convert_project)
        self.panel.shareWorkspaceRequested.connect(self.share_workspace)
        self.panel.importPackageRequested.connect(self.import_project_package)
        self.panel.refreshRequested.connect(self.refresh)
        self.panel.set_workspace_text(self.workspace)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                 self.panel)
        self._install_bubble()

        map_tabs.add_listener(self._maps_changed)
        self._menu = CatalogMenu(self)
        QgsGui.dataItemGuiProviderRegistry().addProvider(self._menu)

        # .kgapp files appear in the Browser and the Catalog, with an Import entry.
        self._package_items = ProjectPackageInstaller(self)
        try:
            self._package_items.install()
        except Exception:
            self._package_items = None
            _log('Could not add project packages to the Browser:\n{}'.format(
                traceback.format_exc()))

        # Layouts live inside the .qgz; shortcuts in Layouts/ stand for them.
        self._layout_items = LayoutShortcutInstaller(self.iface)
        try:
            self._layout_items.install()
        except Exception:
            self._layout_items = None
            _log('Could not add layout shortcuts to the Browser:\n{}'.format(
                traceback.format_exc()))

        # .txt tables, which QGIS does not list on its own
        self._text_items = TextFileInstaller()
        try:
            self._text_items.install()
        except Exception:
            self._text_items = None
            _log('Could not add text files to the Browser:\n{}'.format(
                traceback.format_exc()))

        self.toggle_action = self.panel.toggleViewAction()
        self.toggle_action.setIcon(_icon('workspace/catalog.png'))
        self.toggle_action.setText('Catalog')
        self.toggle_action.setToolTip(
            'Show or hide the Catalog panel (project, connections, computer, '
            'favorites)')

        # Docked beside the Processing Toolbox, QGIS tabs the two together and
        # showing the dock would leave it behind the Toolbox's tab; bring it
        # to the front whenever the button switches it on.
        self.toggle_action.triggered.connect(self._raise_panel)
        if self.bubble is not None:
            self.bubble.toggle_action = self.toggle_action

        self._add_project_menu_actions()
        self._watch_toolbox()

        # Ask to convert a traditional project to a workspace as it is opened.
        self._open_guard = ProjectOpenGuard(self)
        try:
            self._open_guard.install()
        except Exception:
            self._open_guard = None
            _log('Could not watch Open Recent:' + chr(10) + traceback.format_exc())

        # Save / Save As offer a workspace as well as a plain .qgz.
        self._save_guard = SaveGuard(self)
        try:
            self._save_guard.install()
        except Exception:
            with suppress(Exception):
                self._save_guard.uninstall()
            self._save_guard = None
            _log('Could not watch Save:' + chr(10) + traceback.format_exc())

        # Scratch layers are saved (or let go on purpose) before a close.
        self._scratch_guard = ScratchCloseGuard(self)
        try:
            self._scratch_guard.install()
        except Exception:
            with suppress(Exception):
                self._scratch_guard.uninstall()
            self._scratch_guard = None
            _log('Could not watch scratch layers:' + chr(10) + traceback.format_exc())

        project = QgsProject.instance()
        project.readProject.connect(self._project_changed)
        project.cleared.connect(self._project_changed)
        project.fileNameChanged.connect(self._project_changed)
        project.projectSaved.connect(self._layouts_changed)
        manager = project.layoutManager()
        manager.layoutAdded.connect(self._layouts_changed)
        manager.layoutRemoved.connect(self._layouts_changed)
        manager.layoutRenamed.connect(self._layouts_changed)
        for signal in (manager.layoutAdded, manager.layoutRemoved,
                       manager.layoutRenamed):
            signal.connect(self._maps_changed)
        self._connected = True
        self._layouts_changed()

        # Deferred: Processing may not have built its dock yet, and QGIS
        # restores the saved window layout after plugins load, which would
        # undo a tabify done here.
        QTimer.singleShot(0, self._dock_with_toolbox)
        QTimer.singleShot(0, self._first_run)
        # The start page is only in front once QGIS has finished starting, so
        # the offer waits for that (or, loaded late, for a fixed time).
        self._offered = False
        with suppress(Exception):
            self.iface.initializationCompleted.connect(self._start_up_done)
        QTimer.singleShot(RECOVERY_FALLBACK_MS, self._offer_recovery)

    def _install_bubble(self):
        """Minimize-to-bubble, shared with the Tool Bag's bubbles. Never blocks the Catalog."""
        if self.group is None:
            return
        try:
            from .panel_bubble import PanelBubbleManager, file_glyph, install_title_bar
            self.bubble = PanelBubbleManager(
                self.panel, self.group, self.iface.mapCanvas(), self._raise_panel,
                'catalog', 'Catalog – click to open', file_glyph('bubble/catalog.png'))
            self.panel.minimizeRequested.connect(self.bubble.minimize)
            install_title_bar(self.panel, self.bubble)      # Minimize beside Float and Close
        except Exception:
            self.bubble = None
            _log('Could not add the Catalog bubble:\n{}'.format(traceback.format_exc()))

    def _make_untitled(self):
        """A fresh scratch workspace for this session's unsaved project.

        Spent scratch folders of earlier sessions are cleared first; the ones
        that still hold work are kept in `_recoverable` for the start-up offer.
        """
        try:
            self._recoverable = ws.cleanup_scratches()
            return ws.new_scratch_workspace()
        except ws.WorkspaceError as error:
            _log('Could not create the default workspace: {}'.format(error))
            return None

    # ------------------------------------------------------ scratch: recover

    def _start_up_done(self, *_args):
        QTimer.singleShot(RECOVERY_SETTLE_MS, self._offer_recovery)

    def _offer_recovery(self):
        """Tell the user about unsaved work left by an earlier session."""
        if self._offered:
            return
        if not self._recoverable or self.panel is None \
                or self.workspace is not self._untitled:
            return
        # Not over the start page: wait until a project is on screen.
        self._offered = True
        stack = self._start_page_stack()
        if stack is not None:
            stack.currentChanged.connect(self._start_page_left)
            return
        found = self._recoverable[0]
        stamp = datetime.fromtimestamp(found.modified()).strftime('%d %b %Y %H:%M')
        bar = self.iface.messageBar()
        widget = bar.createMessage(
            'KGA Toolbox', 'Unsaved work from {} was found (data made while no '
            'workspace was set).'.format(stamp))
        recover = QPushButton('Recover', widget)
        discard = QPushButton('Discard', widget)
        recover.setToolTip('Show it in the Catalog as the Untitled workspace')
        discard.setToolTip('Delete it')
        # Bound slots, no closures over Qt objects; the work is deferred because
        # it removes the bar item that owns the clicked button.
        self._offer_widget = widget
        recover.clicked.connect(self._recover_clicked)
        discard.clicked.connect(self._discard_clicked)
        widget.layout().addWidget(recover)
        widget.layout().addWidget(discard)
        bar.pushWidget(widget, Qgis.MessageLevel.Warning, 0)

    def _recover_clicked(self, _checked=False):
        QTimer.singleShot(0, self._recover_deferred)

    def _discard_clicked(self, _checked=False):
        QTimer.singleShot(0, self._discard_deferred)

    def _recover_deferred(self):
        widget, self._offer_widget = self._offer_widget, None
        if self._recoverable:
            self._recover(self._recoverable[0], widget)

    def _discard_deferred(self):
        widget, self._offer_widget = self._offer_widget, None
        self._discard_recoverable(widget)

    def _start_page_stack(self):
        """The central stack while QGIS's welcome (start) page is in front, else None."""
        for widget in self.iface.mainWindow().findChildren(QWidget):
            if widget.metaObject().className() == 'QgsWelcomePage':
                stack = widget.parent()
                if isinstance(stack, QStackedWidget) \
                        and stack.currentWidget() is widget:
                    return stack
                return None
        return None

    def _start_page_left(self, _index=0):
        stack = self.sender()
        if stack is not None:
            with suppress(Exception):
                stack.currentChanged.disconnect(self._start_page_left)
        self._offered = False
        QTimer.singleShot(500, self._offer_recovery)

    def _recover(self, found, widget=None):
        """Make an earlier session's scratch the current Untitled workspace."""
        previous = self._untitled
        self._untitled = found
        self._recoverable = [w for w in self._recoverable if w != found]
        self.set_workspace(found)
        if previous is not None and previous != found:
            ws.discard_if_empty(previous)
        self._dismiss(widget)

    def _discard_recoverable(self, widget=None):
        for found in self._recoverable:
            ws.remove_tree(found.root)
        self._recoverable = []
        self._dismiss(widget)

    def _dismiss(self, widget):
        if widget is None or sip.isdeleted(widget):
            return
        with suppress(Exception):
            self.iface.messageBar().popWidget(widget)

    # ----------------------------------------- scratch: saved outside a workspace

    def _nudge_outside_workspace(self):
        """A project saved outside any workspace leaves its data in the scratch."""
        path = QgsProject.instance().fileName()
        scratch = self._untitled
        if not path or scratch is None or self.workspace is not scratch \
                or path == self._nudged or not scratch.has_content():
            return
        self._nudged = path
        bar = self.iface.messageBar()
        widget = bar.createMessage(
            'KGA Toolbox', 'This project is saved outside a workspace, so the '
            'data made in Untitled stays in a temporary folder (cleared after '
            '{} days).'.format(ws.SCRATCH_KEEP_DAYS))
        button = QPushButton('Set Workspace...', widget)
        button.clicked.connect(
            lambda: QTimer.singleShot(0, lambda: (self._dismiss(widget),
                                                  self.set_workspace_dialog())))
        widget.layout().addWidget(button)
        bar.pushWidget(widget, Qgis.MessageLevel.Warning, 0)

    def _ask_move_scratch(self):
        """Ask whether the Untitled work comes along: 'move', 'leave' or None (cancel)."""
        scratch = self.workspace
        if scratch is None or not scratch.is_untitled or not scratch.has_content():
            return 'leave'
        box = QMessageBox(
            QMessageBox.Icon.Question, 'Set Workspace',
            'Untitled holds work that is not in any workspace yet (Default.gpkg '
            'and tool output).\n\nMove it into the new workspace? Layers in the '
            'project are pointed at the moved data.',
            QMessageBox.StandardButton.NoButton, self.iface.mainWindow())
        move = box.addButton('Move it', QMessageBox.ButtonRole.AcceptRole)
        leave = box.addButton('Leave it behind', QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(move)
        box.exec()
        if box.clickedButton() is move:
            return 'move'
        if box.clickedButton() is leave:
            return 'leave'
        return None

    def _dock_with_toolbox(self):
        """Put the Catalog in the Processing Toolbox's tab group.

        The same arrangement as the KGA Geoprocessing panel: one tab group on
        the right instead of panels stacked on top of each other.
        """
        if self.panel is None or self.panel.isFloating():
            return
        window = self.iface.mainWindow()
        toolbox = window.findChild(QDockWidget, 'ProcessingToolbox')
        if toolbox is None or toolbox.isFloating() or toolbox is self.panel:
            return
        if self.panel in window.tabifiedDockWidgets(toolbox):
            return
        window.tabifyDockWidget(toolbox, self.panel)

    def _raise_panel(self, checked=True):
        """Show the Catalog as the front tab when the button switches it on."""
        if not checked or self.panel is None:
            return
        self._dock_with_toolbox()
        self.panel.show()
        self.panel.raise_()

    def _first_run(self):
        """The first time: show the Catalog and put the stock Browser away.

        Run once only, so a user who closes the Catalog, or switches the
        Browser back on, is never overridden again. QGIS restores the saved
        window layout after plugins load, so this waits for that.
        """
        if self.panel is None:
            return
        settings = QgsSettings()
        if settings.value(SETTINGS_FIRST_RUN, False, type=bool):
            return
        settings.setValue(SETTINGS_FIRST_RUN, True)
        self._raise_panel()
        self.hide_stock_browser()

    def hide_stock_browser(self):
        window = self.iface.mainWindow()
        for dock in window.findChildren(QDockWidget):
            if dock.objectName().startswith('Browser'):
                dock.hide()

    # ----------------------------------------------------------- menu actions

    def _add_project_menu_actions(self):
        menu = self.iface.projectMenu()
        window = self.iface.mainWindow()
        self.set_action = QAction(_icon('workspace/setWorkspace.png'), 'Set Workspace...',
                                  window)
        self.set_action.setToolTip(
            'Choose the folder and name of the project; everything the '
            'project owns is kept there')
        self.set_action.triggered.connect(
            lambda _checked=False: self.set_workspace_dialog())
        self.open_action = QAction(_icon('workspace/openWorkspace.png'), 'Open Workspace...', window)
        self.open_action.setToolTip('Open a KGA workspace folder')
        self.open_action.triggered.connect(
            lambda _checked=False: self.open_workspace_dialog())
        self.convert_action = QAction(_icon('workspace/convertProject.png'),
                                      'Convert Project to Workspace...', window)
        self.convert_action.setToolTip(
            'Turn the open QGIS project (.qgz) into a KGA workspace: its data '
            'is gathered into one folder with a default GeoPackage')
        self.convert_action.triggered.connect(
            lambda _checked=False: self.convert_project())
        self.share_action = QAction(_icon('workspace/shareWorkspace.png'),
                                    'Share Workspace...', window)
        self.share_action.setToolTip(
            'Package the project, its data and layouts into one .kgapp file '
            'to send to someone')
        self.share_action.triggered.connect(
            lambda _checked=False: self.share_workspace())
        self.import_action = QAction(_icon('workspace/importProjectPackage.png'),
                                     'Import Project Package...', window)
        self.import_action.setToolTip(
            'Unpack a .kgapp project package into a workspace and open it')
        self.import_action.triggered.connect(
            lambda _checked=False: self.import_project_package())
        before = self.iface.actionSaveProject()
        for action in (self.set_action, self.open_action, self.convert_action,
                       self.share_action, self.import_action):
            menu.insertAction(before, action)
        self._separator = QAction(window)
        self._separator.setSeparator(True)
        menu.insertAction(before, self._separator)
        # QGIS and other plugins keep editing this menu while they start up;
        # look again once that has settled and put the divider back if it is
        # no longer between Open Workspace and Save.
        QTimer.singleShot(0, self._ensure_separator)
        QTimer.singleShot(2000, self._ensure_separator)

    def _ensure_separator(self):
        """A divider directly after the workspace actions, whatever else changed."""
        last = self.import_action or self.open_action
        if last is None or self._separator is None:
            return
        menu = self.iface.projectMenu()
        actions = menu.actions()
        if last not in actions:
            return
        position = actions.index(last) + 1
        if position < len(actions) and actions[position].isSeparator():
            return
        if self._separator in actions:
            menu.removeAction(self._separator)
        if position < len(menu.actions()):
            menu.insertAction(menu.actions()[position], self._separator)
        else:
            menu.addAction(self._separator)

    def _remove_project_menu_actions(self):
        menu = self.iface.projectMenu()
        for action in (self.set_action, self.open_action, self.convert_action,
                       self.share_action, self.import_action, self._separator):
            if action is not None:
                with suppress(Exception):
                    menu.removeAction(action)
                    action.deleteLater()
        self.set_action = self.open_action = self._separator = None
        self.share_action = self.import_action = self.convert_action = None

    # ------------------------------------------------------- following projects

    def _workspace_for_project(self):
        path = QgsProject.instance().fileName()
        return ws.open_workspace(path) if path else None

    def _project_changed(self, *_args):
        # A project load fires several of these in a row, with the file name
        # only settled at the end; look once, afterwards.
        self._sync_timer.start(0)

    def sync_with_project(self):
        target = self._workspace_for_project() or self._untitled
        if target != self.workspace:
            self.set_workspace(target)
        self._nudge_outside_workspace()
        self._layouts_changed()

    def _layouts_changed(self, *_args):
        if self._layout_timer is not None:
            self._layout_timer.start()

    def sync_maps(self):
        """Keep one .kgamap shortcut per map in the workspace's Maps folder."""
        project = QgsProject.instance()
        path = project.fileName()
        current = self._workspace_for_project()
        if not path or self.workspace is None or current != self.workspace:
            return
        try:
            os.makedirs(self.workspace.maps, exist_ok=True)
            layers = map_tabs.map_layer_names(project)
            map_shortcuts.sync(self.workspace.maps, path,
                               [(mid, name, layers.get(mid, [])) for mid, name, _open
                                in map_tabs.list_maps(project)])
        except Exception:
            _log('Could not update the map shortcuts:\n{}'.format(
                traceback.format_exc()))

    def sync_layouts(self):
        """Keep one shortcut per print layout in the workspace's Layouts folder."""
        self.sync_maps()
        project = QgsProject.instance()
        path = project.fileName()
        current = self._workspace_for_project()
        if not path or self.workspace is None or current != self.workspace:
            return
        names = [layout.name() for layout in project.layoutManager().layouts()]
        try:
            layout_shortcuts.sync(self.workspace.layouts, path, names)
        except Exception:
            _log('Could not update the layout shortcuts:\n{}'.format(
                traceback.format_exc()))

    def set_workspace(self, workspace):
        """Make `workspace` current: Project tab, toolbox, script folders."""
        self.workspace = workspace
        self._register_toolbox()
        self._apply_output_defaults()
        self._watch_toolbox()
        if self.model is not None:
            ci.rebuild_project(self.model, workspace)
            ci.rebuild_computer(self.model, workspace)    # Home lists its folders
        if self.panel is not None:
            self.panel.set_workspace_text(workspace)

    def _apply_output_defaults(self):
        """Send tool output to this project's default database and folder."""
        od.set_workspace(self.workspace)
        with suppress(Exception):
            geometry_fields.rescan()
        with suppress(Exception):
            processing_defaults.install()
        self._point_file_dialogs_at_project()

    def _point_file_dialogs_at_project(self):
        """Make the stock "last folder" settings open in the project folder."""
        folder = od.project_folder()
        if not folder:
            return
        settings = QgsSettings()
        for key in LAST_FOLDER_KEYS:
            settings.setValue(key, folder)

    def _register_toolbox(self):
        folder = self.workspace.toolbox if self.workspace is not None else ''
        with suppress(Exception):
            ts.register(folder)

    # ------------------------------------------------------- Set / Open dialog

    def set_workspace_dialog(self):
        window = self.iface.mainWindow()
        project = QgsProject.instance()
        name = os.path.splitext(os.path.basename(project.fileName()))[0] \
            or project.title() or 'Project'
        if ws.name_problem(name):
            name = 'Project'
        dialog = SetWorkspaceDialog(window, name)
        if dialog.exec() != SetWorkspaceDialog.DialogCode.Accepted:
            return None
        location, chosen = dialog.values()
        choice = self._ask_move_scratch()
        if choice is None:
            return None
        try:
            workspace = ws.create_workspace(location, chosen)
        except ws.WorkspaceError as error:
            QMessageBox.warning(window, 'Set Workspace', str(error))
            return None
        self.adopt_current_project(workspace, move_scratch=choice == 'move')
        return workspace

    def adopt_current_project(self, workspace, move_scratch=False):
        """Save the open project into `workspace` and make it current.

        With `move_scratch`, the work in the Untitled scratch comes along and
        the project's layers are pointed at the moved data.
        """
        project = QgsProject.instance()
        scratch = None
        if move_scratch and self.workspace is not None \
                and self.workspace.is_untitled:
            scratch = self.workspace
        if scratch is not None:
            try:
                ws.promote_files(scratch, workspace)
            except ws.WorkspaceError as error:
                QMessageBox.warning(
                    self.iface.mainWindow(), 'Set Workspace',
                    '{}\n\nThe workspace is set without it; the work stays '
                    'in Untitled.'.format(error))
                scratch = None
        if not project.title():
            project.setTitle(workspace.name)
        # A project that is still empty gets the new project basemap, like any
        # other new project; one with layers is left as it is.
        new_project_basemap.apply(
                project, force=True, canvas=self.iface.mapCanvas())
        # Switch first: the save below changes the project's file name, and
        # the follow-the-project check should find the workspace already set.
        self.set_workspace(workspace)
        failed = []
        if scratch is not None:
            _moved, failed = ws.repoint_layers(project, scratch.root,
                                               workspace.root)
        if not project.write(workspace.project_file):
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'The workspace was created but the project '
                'could not be saved to {}.'.format(workspace.project_file))
            return False
        if scratch is not None:
            self._finish_scratch_move(scratch, failed)
        self.iface.messageBar().pushSuccess(
            'KGA Toolbox', 'Workspace set: {}'.format(workspace.root))
        return True

    def _finish_scratch_move(self, scratch, failed):
        """Clear the moved scratch and give the session a new, empty Untitled."""
        if failed:
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Moved the work, but these layers still point at '
                'Untitled: {}.'.format('; '.join(
                    '{} ({})'.format(n, why) for n, why in failed)))
        else:
            ws.mark_promoted(scratch)
            ws.remove_tree(scratch.root)
        if scratch == self._untitled:
            self._untitled = self._make_untitled()
            self._nudged = None

    def open_workspace_dialog(self):
        window = self.iface.mainWindow()
        start = ws.default_location()
        if not os.path.isdir(start):
            start = ws.documents_dir()
        folder = QFileDialog.getExistingDirectory(
            window, 'Open Workspace', start)
        if not folder:
            return None
        workspace = ws.open_workspace(folder)
        if workspace is None:
            workspace = self._offer_to_adopt(folder)
        if workspace is None:
            return None
        self.open_workspace(workspace)
        return workspace

    def _offer_to_adopt(self, folder):
        """A folder with a single .qgz but no marker can become a workspace."""
        window = self.iface.mainWindow()
        try:
            projects = [n for n in os.listdir(folder)
                        if n.lower().endswith(ws.PROJECT_EXTENSION)]
        except OSError:
            projects = []
        if len(projects) != 1:
            QMessageBox.information(
                window, 'Open Workspace',
                '"{}" is not a KGA workspace. A workspace is a folder made by '
                'Project > Set Workspace.'.format(folder))
            return None
        answer = QMessageBox.question(
            window, 'Open Workspace',
            '"{}" is not a KGA workspace yet but holds the project {}.\n\n'
            'Make it one? Default.gpkg, Toolbox and Layouts are added next to '
            'the project; nothing already there is changed.'.format(
                folder, projects[0]))
        if answer != QMessageBox.StandardButton.Yes:
            return None
        workspace = ws.Workspace(
            folder, os.path.splitext(projects[0])[0])
        try:
            return workspace.scaffold()
        except ws.WorkspaceError as error:
            QMessageBox.warning(window, 'Open Workspace', str(error))
            return None

    def open_workspace(self, workspace):
        """Open the workspace's project (or start one) and make it current."""
        window = self.iface.mainWindow()
        project = QgsProject.instance()
        if project.isDirty():
            box = QMessageBox(
                QMessageBox.Icon.Question, 'Open Workspace',
                'Save changes to the current project first?',
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel, window)
            answer = box.exec()
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save:
                self.iface.actionSaveProject().trigger()
                if project.isDirty():
                    return False        # the save was cancelled
        self.set_workspace(workspace)
        path = ws.project_file_for(workspace)
        if path:
            self.iface.addProject(path)
        else:
            self.iface.newProject()
            # The basemap hook runs after this method, by when the project has
            # a file name and no longer counts as new; add it here.
            new_project_basemap.apply(
                project, force=True, canvas=self.iface.mapCanvas())
            project.write(workspace.project_file)
        return True

    # ------------------------------------------------ plain project -> workspace

    def convert_project(self):
        """Project > Convert Project to Workspace: gather a plain .qgz into a workspace."""
        window = self.iface.mainWindow()
        project = QgsProject.instance()
        path = project.fileName()
        if not path:
            QMessageBox.information(
                window, 'Convert Project',
                'Save the project first (Project > Save), then convert it.')
            return None
        if ws.open_workspace(path) is not None:
            QMessageBox.information(
                window, 'Convert Project',
                'This project is already in a KGA workspace.')
            return None
        if project.isDirty():
            box = QMessageBox(
                QMessageBox.Icon.Question, 'Convert Project',
                'The project has unsaved changes. The conversion is made from '
                'the saved project, so save it first.',
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                window)
            if box.exec() != QMessageBox.StandardButton.Save:
                return None
            self.iface.actionSaveProject().trigger()
            if project.isDirty():
                return None
            path = project.fileName()
        extent = None
        with suppress(Exception):
            extent = self.iface.mapCanvas().extent()
        from .convert_project_dialog import ConvertProjectDialog
        dialog = ConvertProjectDialog(project, path, window, extent=extent,
                                      iface=self.iface)
        dialog.exec()
        result = dialog.result
        if result is not None and dialog.open_after.isChecked():
            workspace = ws.open_workspace(result.path)
            if workspace is not None:
                self.open_workspace(workspace)
        return result

    def convert_and_open(self, project_path, location, name):
        """Convert the project file `project_path` into `location/name`, then open it.

        Used when a traditional project is opened and the user says yes. The
        conversion works on a separate copy read from the file, so it does not
        matter whether QGIS has the project open yet. True when the workspace
        was created (and opened); False when it failed, so the caller can fall
        back to opening the project as it was.
        """
        window = self.iface.mainWindow()
        from ..core import project_analysis as pa
        from ..core import projectpkg as pp
        from .convert_project_dialog import ConvertResultDialog, run_convert
        source = QgsProject()
        if not source.read(project_path):
            QMessageBox.warning(
                window, 'Convert Project',
                'The project could not be read:\n{}'.format(project_path))
            return False
        analysis = pa.analyze(source, os.path.dirname(os.path.abspath(project_path)),
                              project_path).apply_mode(pp.MODE_EVERYTHING)
        result = run_convert(window, source, project_path, analysis, location, name)
        source.clear()
        if result is None:
            return False
        workspace = ws.open_workspace(result.path)
        if workspace is None:
            return False
        if result.warnings or result.count(pp.STATUS_NOT_SHARABLE):
            ConvertResultDialog(result, window).exec()      # say what could not come
        self.open_workspace(workspace)
        self.iface.messageBar().pushSuccess(
            'KGA Toolbox', 'Converted to a workspace: {}'.format(result.path))
        return True

    # ---------------------------------------------------- project packages

    def share_workspace(self):
        """Project > Share Workspace: open the sharing window for the open project."""
        window = self.iface.mainWindow()
        project = QgsProject.instance()
        path = project.fileName()
        if not path:
            answer = QMessageBox.question(
                window, 'Share Workspace',
                'The project has not been saved yet. Save it into a workspace '
                'first (Project > Set Workspace).\n\nSet the workspace now?')
            if answer == QMessageBox.StandardButton.Yes:
                self.set_workspace_dialog()
            path = project.fileName()
            if not path:
                return None
        if project.isDirty():
            box = QMessageBox(
                QMessageBox.Icon.Question, 'Share Workspace',
                'The project has unsaved changes. A package is made from the '
                'saved project, so save it first.',
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                window)
            if box.exec() != QMessageBox.StandardButton.Save:
                return None
            self.iface.actionSaveProject().trigger()
            if project.isDirty():
                return None             # the save was cancelled
            path = project.fileName()
        workspace = ws.open_workspace(path)
        extent = None
        with suppress(Exception):
            extent = self.iface.mapCanvas().extent()
        from .share_workspace_dialog import ShareWorkspaceDialog
        dialog = ShareWorkspaceDialog(project, path, workspace, window,
                                      extent=extent, iface=self.iface)
        dialog.exec()
        return dialog.result

    def import_project_package(self, package=''):
        """Project > Import Project Package: unpack a `.kgapp` and open it."""
        window = self.iface.mainWindow()
        from .import_package_dialog import (
            SETTINGS_FOLDER,
            SETTINGS_PACKAGE,
            ImportPackageDialog,
            ImportResultDialog,
            run_import,
        )
        dialog = ImportPackageDialog(window, package)
        if dialog.exec() != ImportPackageDialog.DialogCode.Accepted:
            return None
        path, location, name, verify, open_after = dialog.values()
        result = run_import(window, path, location, name, verify)
        if result is None:
            return None
        QgsSettings().setValue(SETTINGS_FOLDER, location)
        QgsSettings().setValue(SETTINGS_PACKAGE, path)
        ImportResultDialog(result, window).exec()
        if open_after:
            self.open_workspace(result.workspace)
        return result

    # ------------------------------------------------- right-click operations

    def attach_folder(self, path=None):
        if self.workspace is None:
            return
        if not path:
            path = QFileDialog.getExistingDirectory(
                self.iface.mainWindow(), 'Attach Folder',
                QgsSettings().value('KGA/catalog/last_folder', ws.documents_dir()))
            if not path:
                return
            QgsSettings().setValue('KGA/catalog/last_folder', path)
        if self.workspace.attach_folder(path):
            self.refresh_section(ci.PATH_FOLDERS)
        else:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox', '"{}" is already in the project.'.format(path))

    def detach_folder(self, path):
        if self.workspace is not None and self.workspace.detach_folder(path):
            self.refresh_section(ci.PATH_FOLDERS)

    def attach_database(self, path=None):
        if self.workspace is None:
            return
        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self.iface.mainWindow(), 'Attach Database',
                QgsSettings().value('KGA/catalog/last_folder',
                                    ws.documents_dir()),
                'GeoPackage (*.gpkg);;All files (*.*)')
            if not path:
                return
            QgsSettings().setValue('KGA/catalog/last_folder',
                                   os.path.dirname(path))
        if self.workspace.attach_database(path):
            self.refresh_section(ci.PATH_DATABASES)
        else:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox', '"{}" is already in the project.'.format(path))

    def add_to_project(self, path):
        """Right-click > Add to Project: a folder joins Folders, a database Databases."""
        if os.path.isdir(path):
            self.attach_folder(path)
        else:
            self.attach_database(path)

    def make_default_folder(self, path):
        if self.workspace is not None and self.workspace.set_default_folder(path):
            self._point_file_dialogs_at_project()
            self.refresh_section(ci.PATH_FOLDERS)

    def make_default_database(self, path):
        if self.workspace is not None and self.workspace.set_default_database(path):
            self.refresh_section(ci.PATH_DATABASES)

    def detach_database(self, path):
        if self.workspace is not None and self.workspace.detach_database(path):
            self.refresh_section(ci.PATH_DATABASES)

    def open_toolbox_folder(self):
        if self.workspace is not None:
            os.makedirs(self.workspace.toolbox, exist_ok=True)
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(self.workspace.toolbox))

    def new_script(self):
        if self.workspace is None:
            return
        window = self.iface.mainWindow()
        title, accepted = QInputDialog.getText(
            window, 'New Script', 'Name of the script tool:')
        if not accepted or not title.strip():
            return
        os.makedirs(self.workspace.toolbox, exist_ok=True)
        try:
            path = ts.write_new_script(self.workspace.toolbox, title)
        except OSError as error:
            QMessageBox.warning(window, 'New Script', str(error))
            return
        self.refresh_section(ci.PATH_TOOLBOX)
        ts.refresh()
        self.edit_script(path)

    def edit_script(self, path):
        try:
            from processing.script.ScriptEditorDialog import ScriptEditorDialog
            dialog = ScriptEditorDialog(path, self.iface.mainWindow())
            dialog.show()
        except Exception:
            _log('Could not open the script editor:\n{}'.format(
                traceback.format_exc()))
            self.show_in_folder(path)

    def reveal_path(self, path):
        """Show the Catalog and select `path` in it (Reveal in Catalog)."""
        if self.panel is None:
            return False
        self._dock_with_toolbox()
        self.panel.show()
        self.panel.raise_()
        return self.panel.reveal_path(path)

    def show_in_folder(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def run_script(self, path):
        """Open the tool dialog of the script at `path`."""
        bar = self.iface.messageBar()
        try:
            import processing
            ts.refresh()
            alg_id = ts.algorithm_id(path)
            if not alg_id or QgsApplication.processingRegistry() \
                    .algorithmById(alg_id) is None:
                bar.pushWarning(
                    'KGA Toolbox', 'Could not load the script tool "{}". '
                    'See the KGA Toolbox log for details.'.format(
                        os.path.basename(path)))
                return
            processing.execAlgorithmDialog(alg_id, {})
        except Exception as error:
            _log('Could not run {}:\n{}'.format(path, traceback.format_exc()))
            bar.pushWarning('KGA Toolbox',
                            'Could not run the script: {}'.format(error))

    # ----------------------------------------------------------------- refresh

    def refresh_project(self):
        self.refresh(TAB_PROJECT)

    def refresh(self, tab):
        if self.model is None:
            return
        if tab == TAB_PROJECT:
            ci.rebuild_project(self.model, self.workspace)
            return
        if tab == TAB_COMPUTER:
            ci.rebuild_computer(self.model)
            return
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            item = self.model.dataItem(index)
            if item is not None and tab_of_root(item) == tab:
                self.model.refresh(index)

    # ------------------------------------------------------------------- maps

    def _maps_changed(self):
        self._maps_timer.start()

    def _refresh_maps_section(self):
        """Refill Maps and Layouts; the Layouts node comes and goes whole."""
        if bool(ci.project_layouts()) != ci.STATE.layouts_shown:
            self.refresh_project()
            return
        self.sync_maps()
        self.refresh_section(ci.PATH_MAPS)
        if ci.STATE.layouts_shown:
            self.refresh_section(ci.PATH_LAYOUTS)

    def _layout_for(self, path):
        if not path.startswith(ci.PATH_LAYOUT_PREFIX):
            return None
        name = path[len(ci.PATH_LAYOUT_PREFIX):]
        return QgsProject.instance().layoutManager().layoutByName(name)

    def is_layouts_folder(self, path):
        """True for the workspace's Layouts folder as listed under Folders."""
        return self.workspace is not None and bool(path) and \
            ci._norm_key(path) == ci._norm_key(self.workspace.layouts)

    def new_layout(self):
        window = self.iface.mainWindow()
        project = QgsProject.instance()
        default = project.layoutManager().generateUniqueTitle()
        answer = new_layout_dialog.ask(window, default)
        if answer is None:
            return
        name, options = answer
        layout = layout_import.new_layout(project, name, **options)
        if layout is None:
            QMessageBox.warning(window, 'New Layout',
                                'QGIS could not add the layout.')
            return
        self.iface.openLayoutDesigner(layout)

    def import_layout(self, path=None, pick=None):
        """Add layouts from a template (.qpt) or from another project.

        `path` and `pick` stand in for the file and selection dialogs.
        """
        window = self.iface.mainWindow()
        project = QgsProject.instance()
        if path is None:
            path, _filter = QFileDialog.getOpenFileName(
                window, 'Import Layout', os.path.dirname(project.fileName()),
                'Layouts (*.qpt *.qgz *.qgs);;Layout templates (*.qpt);;'
                'QGIS projects (*.qgz *.qgs)')
        if not path:
            return []
        added = []
        if path.lower().endswith(layout_import.TEMPLATE_EXT):
            layout = layout_import.from_template(project, path)
            if layout is not None:
                added.append(layout)
        else:
            other = layout_import.read_project(path)
            names = layout_import.print_layouts(other) if other else []
            if other is None or not names:
                QMessageBox.information(
                    window, 'Import Layout',
                    'That file has no print layouts to import.')
                return []
            chosen = (pick or self._pick_names)('Import Layout', names)
            database = self.workspace.default_database if self.workspace else ''
            cache, notes = {}, []
            tabs = map_tabs.active()
            for name in chosen:
                # the layout's data gets a map of its own, never an existing one
                group = tabs.new_import_group(name) if tabs is not None else None
                layout, layer_notes = layout_import.import_with_data(
                    project, other, name, database, cache, group)
                notes.extend(layer_notes)
                if group is not None:
                    tabs.add_import_group(group)
                if layout is not None:
                    added.append(layout)
            self._report_import(notes, database)
        if not added:
            if path.lower().endswith(layout_import.TEMPLATE_EXT) or pick is None:
                QMessageBox.warning(window, 'Import Layout',
                                    'The layout could not be imported.')
            return []
        self.iface.openLayoutDesigner(added[-1])
        return [layout.name() for layout in added]

    def _report_import(self, notes, database):
        """Tell the user what became of the layers the layouts draw."""
        if not notes:
            return
        copied = sum(1 for _name, note in notes if note.startswith('copied'))
        text = ('{} layer(s) copied to {}'.format(copied, os.path.basename(database))
                if copied else 'No layers were copied')
        placed = len(notes) - copied
        if placed:
            text += '; {} used in place'.format(placed)
        self.iface.messageBar().pushInfo(
            'KGA Toolbox', text + '. Layer styles were kept.')

    def _pick_names(self, title, names):
        """Ask which of `names` to import; [] when cancelled."""
        if len(names) == 1:
            return names
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        listing.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        listing.addItems(names)
        listing.selectAll()
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if not dialog.exec():
            return []
        return [item.text() for item in listing.selectedItems()]

    def open_layout(self, path):
        layout = self._layout_for(path)
        if layout is not None:
            self.iface.openLayoutDesigner(layout)

    def rename_layout(self, path):
        layout = self._layout_for(path)
        if layout is None:
            return
        window = self.iface.mainWindow()
        name, accepted = QInputDialog.getText(
            window, 'Rename Layout', 'Layout name:', text=layout.name())
        name = name.strip()
        if not accepted or not name or name == layout.name():
            return
        if QgsProject.instance().layoutManager().layoutByName(name) is not None:
            QMessageBox.warning(window, 'Rename Layout',
                                'A layout called "{}" already exists.'.format(name))
            return
        layout.setName(name)

    def duplicate_layout(self, path):
        layout = self._layout_for(path)
        if layout is None:
            return
        manager = QgsProject.instance().layoutManager()
        manager.duplicateLayout(layout, manager.generateUniqueTitle())

    def delete_layout(self, path):
        self.delete_layouts([path])

    def delete_layouts(self, paths):
        """Delete the layouts at `paths`, after one question."""
        layouts = [layout for layout in map(self._layout_for, paths)
                   if layout is not None]
        if not layouts:
            return
        if len(layouts) == 1:
            text = 'Delete the layout "{}"?'.format(layouts[0].name())
        else:
            text = 'Delete these {} layouts?\n\n{}'.format(
                len(layouts), '\n'.join(layout.name() for layout in layouts))
        answer = QMessageBox.question(
            self.iface.mainWindow(),
            'Delete Layout' if len(layouts) == 1 else 'Delete Layouts', text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            manager = QgsProject.instance().layoutManager()
            for layout in layouts:
                manager.removeLayout(layout)

    def _map_for(self, path):
        tabs = map_tabs.active()
        if tabs is None or not path.startswith(ci.PATH_MAP_PREFIX):
            return None, None
        return tabs, tabs.group_by_id(path[len(ci.PATH_MAP_PREFIX):])

    def is_maps_folder(self, path):
        """True for the workspace's Maps folder as listed under Folders."""
        return self.workspace is not None and bool(path) and \
            ci._norm_key(path) == ci._norm_key(self.workspace.maps)

    def _tabs_or_warn(self):
        tabs = map_tabs.active()
        if tabs is None:
            self.iface.messageBar().pushInfo(
                'KGA Toolbox', 'Turn on Map tabs in KGA Toolbox > Settings '
                'to create and import maps.')
        return tabs

    def new_map(self):
        tabs = self._tabs_or_warn()
        if tabs is not None:
            tabs.new_map()

    def import_map(self):
        tabs = self._tabs_or_warn()
        if tabs is None:
            return
        database = self.workspace.default_database if self.workspace else ''
        if tabs.import_map(database=database):
            self._report_import(tabs.last_notes, database)

    def open_map(self, path):
        tabs, group = self._map_for(path)
        if group is not None:
            tabs.open_map(group)

    def rename_map(self, path):
        tabs, group = self._map_for(path)
        if group is not None:
            tabs.rename_group(group)

    def duplicate_map(self, path):
        tabs, group = self._map_for(path)
        if group is not None:
            tabs.duplicate_group(group)

    def delete_map(self, path):
        self.delete_maps([path])

    def delete_maps(self, paths):
        """Delete the maps at `paths`, after one question."""
        tabs = map_tabs.active()
        groups = [self._map_for(path)[1] for path in paths]
        groups = [group for group in groups if group is not None]
        if tabs is not None and groups:
            tabs.delete_groups(groups)

    def refresh_section(self, path):
        if self.model is None:
            return
        ci.rebuild_section(self.model, path, self.workspace)
        if path == ci.PATH_FOLDERS:
            ci.rebuild_computer(self.model)     # Home lists the same folders

    # ------------------------------------------------------------ toolbox watch

    def _watch_toolbox(self):
        """Watch the Toolbox and the project folder for files coming and going."""
        paths = self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)
        self._listed = self._listing()
        if self.workspace is None:
            return
        for folder in (self.workspace.toolbox, self.workspace.root):
            if os.path.isdir(folder):
                self._watcher.addPath(folder)

    def _listing(self):
        """What the Toolbox and Databases sections would list right now."""
        if self.workspace is None:
            return None
        return (ts.list_scripts(self.workspace.toolbox),
                self.workspace.all_databases())

    def _toolbox_changed(self, _path):
        self._toolbox_timer.start()

    def _refresh_toolbox_section(self):
        """Refill Toolbox / Databases, but only when their contents changed.

        Writing to a GeoPackage also stirs the folder watcher; rebuilding the
        tree for each of those would collapse what the user has open.
        """
        listing = self._listing()
        previous = self._listed
        if listing is None or previous is None:
            self._listed = listing
            return
        if listing[0] != previous[0]:
            self.refresh_section(ci.PATH_TOOLBOX)
        if listing[1] != previous[1]:
            self.refresh_section(ci.PATH_DATABASES)
        self._listed = listing

    # ------------------------------------------------------------------ unload

    def unload(self):
        with suppress(Exception):
            self.iface.initializationCompleted.disconnect(self._start_up_done)
        self._sync_timer.stop()
        self._toolbox_timer.stop()
        self._maps_timer.stop()
        map_tabs.remove_listener(self._maps_changed)
        if self._layout_timer is not None:
            self._layout_timer.stop()
        if self._connected:
            project = QgsProject.instance()
            manager = project.layoutManager()
            for signal in (manager.layoutAdded, manager.layoutRemoved,
                           manager.layoutRenamed, project.projectSaved):
                with suppress(Exception):
                    signal.disconnect(self._layouts_changed)
            for signal in (manager.layoutAdded, manager.layoutRemoved,
                           manager.layoutRenamed):
                with suppress(Exception):
                    signal.disconnect(self._maps_changed)
            for signal in (project.readProject, project.cleared,
                           project.fileNameChanged):
                with suppress(Exception):
                    signal.disconnect(self._project_changed)
            self._connected = False
        if self._save_guard is not None:
            with suppress(Exception):
                self._save_guard.uninstall()
            self._save_guard = None
        if self._scratch_guard is not None:
            with suppress(Exception):
                self._scratch_guard.uninstall()
            self._scratch_guard.deleteLater()
            self._scratch_guard = None
        self._remove_project_menu_actions()
        if self._open_guard is not None:
            with suppress(Exception):
                self._open_guard.uninstall()
            self._open_guard.deleteLater()
            self._open_guard = None
        if self._package_items is not None:
            with suppress(Exception):
                self._package_items.uninstall()
            self._package_items = None
        if self._layout_items is not None:
            with suppress(Exception):
                self._layout_items.uninstall()
            self._layout_items = None
        if self._text_items is not None:
            with suppress(Exception):
                self._text_items.uninstall()
            self._text_items = None
        if self._menu is not None:
            with suppress(Exception):
                QgsGui.dataItemGuiProviderRegistry().removeProvider(self._menu)
            self._menu = None
        with suppress(Exception):
            ts.unregister()
        if self.bubble is not None:
            with suppress(Exception):
                self.bubble.unload()
            self.bubble = None
        if self.panel is not None:
            with suppress(Exception):
                self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
            self.toggle_action = None
        if self.model is not None:
            self.model.deleteLater()
            self.model = None
        for provider in self._providers:
            with suppress(Exception):
                QgsApplication.dataItemProviderRegistry().removeProvider(
                    provider)
        self._providers = []
        od.set_workspace(None)
        with suppress(Exception):
            ws.discard_if_empty(self._untitled)
        with suppress(Exception):
            processing_defaults.uninstall()
        ci.ICONS.clear()
        ci.STATE.workspace = None

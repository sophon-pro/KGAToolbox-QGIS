"""KGA Toolbox plugin.

Registers the KGA processing provider, builds a toolbar whose buttons mirror the
algorithm groups, and hosts the KGA Geoprocessing dock panel.

Icon and group-ordering configuration now lives in branding.py so the toolbar
and the panel stay in sync.
"""

from functools import partial

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QMenu, QToolButton

from .branding import (
    GROUP_ICONS,
    KGA_WEBSITE,
    LOG_TAG,
    STANDALONE_ALGS,
    algorithm_icon,
    button_label,
    icon as _icon,
    ordered_algorithms,
    ordered_groups,
)
from .gui import geoprocessing_panel as gp
from .gui.about_dialog import AboutDialog
from .gui.geoprocessing_panel import KgaGeoprocessingPanel
from .provider import KgaProvider

MENU_TITLE = '&KGA Toolbox'


class KgaToolsPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.toolbar = None
        self.menu = None
        self.panel = None
        self.panel_action = None
        self._owned_widgets = []

    # ------------------------------------------------------------------ setup

    def initGui(self):
        self.init_processing()

        self.toolbar = self.iface.addToolBar('KGA Toolbox')
        self.toolbar.setObjectName('KGAToolsToolbar')
        self.toolbar.setToolTip('KGA Toolbox')

        self.menu = QMenu(MENU_TITLE, self.iface.mainWindow())
        self.iface.mainWindow().menuBar().insertMenu(
            self.iface.firstRightStandardMenu().menuAction(), self.menu)

        self.init_panel()
        self.build_toolbar()

    def init_processing(self):
        self.provider = KgaProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def init_panel(self):
        """The Geoprocessing pane. Hidden by default; toggled from the toolbar."""
        self.panel = KgaGeoprocessingPanel(
            self.provider.algorithms, self.iface.mainWindow())
        self.panel.algorithmTriggered.connect(self.run_algorithm)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel.hide()
        # It lists the same algorithms the Processing Toolbox does, so it
        # belongs in the Toolbox's tab group rather than as a second panel
        # stacked under it. Deferred to the event loop for two reasons: the
        # Processing plugin may not have built its dock yet when this one is
        # loaded at start-up, and QGIS restores the saved window layout after
        # plugins load, which would undo a tabify done here.
        QTimer.singleShot(0, self.tabify_panel_with_toolbox)

        # toggleViewAction gives a checkable action wired to the dock for free,
        # so the toolbar button reflects the panel's visibility.
        self.panel_action = self.panel.toggleViewAction()
        self.panel_action.setIcon(_icon('kga.png'))
        self.panel_action.setText('KGA Geoprocessing')
        self.panel_action.setToolTip('Show the KGA Geoprocessing panel')
        # toggleViewAction only shows the dock. Tabbed onto the Processing
        # Toolbox that leaves it behind whichever tab is in front, so the
        # button reads as doing nothing; raise it to the front tab as well.
        self.panel_action.triggered.connect(self.raise_panel)

    def raise_panel(self, checked):
        """Make the Geoprocessing panel the current tab when it is switched on."""
        if checked and self.panel is not None:
            self.panel.raise_()

    def tabify_panel_with_toolbox(self):
        """Put the Geoprocessing panel in the Processing Toolbox's tab group."""
        if self.panel is None:
            return
        window = self.iface.mainWindow()
        toolbox = window.findChild(QDockWidget, 'ProcessingToolbox')
        # No Toolbox (Processing switched off) or the user has pulled it out
        # into its own window: leave the panel where QGIS put it.
        if toolbox is None or toolbox.isFloating() or self.panel.isFloating():
            return
        window.tabifyDockWidget(toolbox, self.panel)

    # ---------------------------------------------------------------- toolbar

    def build_toolbar(self):
        """Group the registered algorithms and make one dropdown per group."""
        standalone_icons = dict(STANDALONE_ALGS)

        groups = {}
        promoted = {}
        for alg in self.provider.algorithms():
            if alg.name() in standalone_icons:
                promoted[alg.name()] = alg
            else:
                groups.setdefault(alg.group() or 'General', []).append(alg)

        # Promoted algorithms lead the toolbar as plain single-click buttons.
        for alg_name, icon_file in STANDALONE_ALGS:
            alg = promoted.get(alg_name)
            if alg is None:
                continue
            action = QAction(_icon(icon_file), alg.displayName(),
                             self.iface.mainWindow())
            action.setToolTip(alg.displayName())
            action.triggered.connect(partial(self.run_algorithm, alg.id()))
            self.toolbar.addAction(action)
            self.menu.addAction(action)
            self._owned_widgets.append(action)
        if promoted:
            self.toolbar.addSeparator()
            self.menu.addSeparator()

        for group_name in ordered_groups(groups.keys()):
            group_icon = _icon(GROUP_ICONS.get(group_name))

            button = QToolButton(self.toolbar)
            button.setText(button_label(group_name))
            button.setToolTip(group_name)
            button.setIcon(group_icon)
            # Split button: clicking the icon runs the selected algorithm,
            # clicking the arrow beside it opens the group menu.
            button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            # Icon-only keeps the toolbar compact; the tooltip carries the name.
            # Without an icon the button would be blank, so keep text in that case.
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly if not group_icon.isNull()
                else Qt.ToolButtonStyle.ToolButtonTextOnly)

            menu = QMenu(button)
            submenu = self.menu.addMenu(group_icon, button_label(group_name))
            first_action = None
            for alg in ordered_algorithms(group_name, groups[group_name]):
                action = QAction(algorithm_icon(alg, group_icon),
                                 alg.displayName(), menu)
                action.setToolTip('{}: {}'.format(group_name, alg.displayName()))
                # Bind the id, not the instance: instances are replaced on refresh.
                action.triggered.connect(partial(self.run_algorithm, alg.id()))
                menu.addAction(action)
                submenu.addAction(action)
                first_action = first_action or action

            button.setMenu(menu)
            # Picking from the menu makes that entry the button's default, so the
            # icon half re-runs it. Keep the group icon so the toolbar stays
            # readable whichever algorithm was used last.
            menu.triggered.connect(partial(self.set_default_action, button,
                                           group_icon))
            if first_action is not None:
                self.set_default_action(button, group_icon, first_action)

            self.toolbar.addWidget(button)
            self._owned_widgets.append(button)

        # --- Non-algorithm tools go here, as plain actions --------------------
        self.toolbar.addSeparator()
        self.menu.addSeparator()

        # Only the Geoprocessing panel's toggle goes here.
        if self.panel_action is not None:
            self.toolbar.addAction(self.panel_action)
            self.menu.addAction(self.panel_action)
            self.toolbar.addSeparator()
            self.menu.addSeparator()

        about = QAction(_icon('kga_logo.png'), 'About Us',
                        self.iface.mainWindow())
        about.setToolTip('About Us - KGA Toolbox')
        about.triggered.connect(self.show_about)
        self.toolbar.addAction(about)
        self.menu.addAction(about)
        self._owned_widgets.append(about)

    def rebuild_toolbar(self):
        """Call after adding/removing algorithms during development."""
        if self.toolbar is None:
            return
        self.toolbar.clear()
        self.menu.clear()
        self._owned_widgets = []
        self.provider.refreshAlgorithms()
        self.build_toolbar()
        if self.panel is not None:
            self.panel.refresh()

    # ----------------------------------------------------------------- actions

    def show_about(self):
        try:
            AboutDialog(self.iface.mainWindow()).exec()
        except Exception as exc:
            # Never let the About box be the thing that breaks the toolbar:
            # fall back to the behaviour it replaced.
            QgsMessageLog.logMessage(
                'Could not open the About dialog: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.open_website()

    def open_website(self):
        if not QDesktopServices.openUrl(QUrl(KGA_WEBSITE)):
            QgsMessageLog.logMessage(
                'Could not open {}'.format(KGA_WEBSITE), LOG_TAG,
                Qgis.MessageLevel.Warning)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open {}'.format(KGA_WEBSITE))

    def set_default_action(self, button, group_icon, action):
        """Make `action` what the icon half of `button` runs."""
        button.setDefaultAction(action)
        # setDefaultAction copies the action's icon; put the group icon back.
        if not group_icon.isNull():
            button.setIcon(group_icon)

    def run_algorithm(self, alg_id):
        try:
            import processing
            alg = QgsApplication.processingRegistry().algorithmById(alg_id)

            # Record before running: "recent" should mean recently opened, not
            # recently completed, and the dialog may sit open for a long time.
            gp.push_recent(alg_id)
            if self.panel is not None:
                self.panel.refresh()

            # Algorithms that declare no parameters build their own dialog in
            # processAlgorithm(). Showing the Processing parameter dialog for
            # those puts an empty window with a Run button in front of it, so
            # execute them straight away and let their dialog come up.
            if alg is not None and not alg.parameterDefinitions():
                processing.run(alg_id, {})
            else:
                processing.execAlgorithmDialog(alg_id, {})
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open {}: {}'.format(alg_id, exc), LOG_TAG,
                Qgis.MessageLevel.Critical)
            self.iface.messageBar().pushWarning(
                'KGA Toolbox', 'Could not open {}'.format(alg_id))

    # ----------------------------------------------------------------- cleanup

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

        # Some algorithms put a window on the QGIS main window instead of
        # returning a layer: the Error Inspector window, the Duplicate Checker
        # dialog, the Sequential Numbering dialog, the Copy-Paste Feature
        # dialog and the Modify Features dialogs. None is parented to
        # anything the plugin owns, so without this they survive the unload as
        # dead widgets — and the interactive ones would leave a map tool or a
        # rubber band on the canvas.
        from .algorithms import (
            buffer_features,
            clip_features,
            construct_polygon,
            copy_parallel,
            copy_paste_feature,
            divide_features,
            duplicate_checker,
            error_inspector,
            merge_features,
            sequential_numbering_dialog,
            split_into_cogo_lines,
        )
        for close in (error_inspector.close_error_inspector,
                      duplicate_checker.close_duplicate_checker,
                      sequential_numbering_dialog.close_sequential_numbering,
                      copy_paste_feature.close_copy_paste_feature,
                      copy_parallel.close_copy_parallel,
                      buffer_features.close_buffer_features,
                      split_into_cogo_lines.close_split_into_cogo_lines,
                      merge_features.close_merge_features,
                      divide_features.close_divide_features,
                      clip_features.close_clip_features,
                      construct_polygon.close_construct_polygon):
            try:
                close()
            except Exception as exc:
                QgsMessageLog.logMessage(
                    'Could not close a KGA window: {}'.format(exc), LOG_TAG,
                    Qgis.MessageLevel.Warning)

        if self.panel is not None:
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
            self.panel_action = None

        if self.toolbar is not None:
            self.toolbar.clear()
            # Removes the toolbar itself from the QGIS main window.
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

        if self.menu is not None:
            self.menu.clear()
            self.iface.mainWindow().menuBar().removeAction(self.menu.menuAction())
            self.menu.deleteLater()
            self.menu = None

        self._owned_widgets = []

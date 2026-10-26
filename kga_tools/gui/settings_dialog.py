# -*- coding: utf-8 -*-
"""The KGA Toolbox Settings window (KGA Toolbox menu > Settings).

Every KGA setting lives here, one page per area. Adding a setting means adding
its key to `core/kga_settings.py` and a control to a page below; `apply()`
writes the controls back and `restore_defaults()` resets them.
"""

from contextlib import suppress
import os

from qgis.core import QgsCoordinateReferenceSystem
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QCompleter
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import basemaps, country_extents
from ..core import kga_settings as ks
from ..core import system_theme, ui_layout
from ..core import workspace as ws


def _native(path):
    return os.path.normpath(path) if path else path


def _note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet('color: palette(dark);')
    return label


def background_basemaps():
    """Names of the basemaps that make sense as a base (labels / overlays out)."""
    return [name for name, _url, _zmax in basemaps.BASEMAPS
            if name not in basemaps.OVERLAYS]


class SettingsDialog(QDialog):

    def __init__(self, parent=None, layout_hooks=None):
        super().__init__(parent)
        # (remember, reset) for the toolbar layout. The plugin passes ones that
        # also know about the start page; these work on the main window alone.
        self.layout_hooks = layout_hooks or (self._remember_window,
                                             self._reset_window)
        self._was_remembering = False
        self.setWindowTitle('KGA Toolbox - Settings')
        self.setMinimumSize(680, 560)

        self.pages = QListWidget()
        self.pages.setFixedWidth(150)
        self.stack = QStackedWidget()
        self.pages.currentRowChanged.connect(self.stack.setCurrentIndex)
        self._add_page('Projects', self._projects_page())
        self._add_page('Catalog', self._catalog_page())
        self._add_page('Appearance', self._appearance_page())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.defaults = buttons.addButton(
            'Restore Defaults', QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.defaults.clicked.connect(self.restore_defaults)

        body = QHBoxLayout()
        body.addWidget(self.pages)
        body.addWidget(self.stack, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(buttons)

        self.load()
        self.pages.setCurrentRow(0)

    # ------------------------------------------------------------------ pages

    def _add_page(self, title, widget):
        self.pages.addItem(title)
        self.stack.addWidget(widget)

    def _projects_page(self):
        """Everything about projects on one page: where, CRS, basemap, extent, output."""
        page = QWidget()
        box = QGroupBox('Where new projects go')
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(_native(ws.factory_location()))
        browse = QPushButton('Browse...')
        browse.clicked.connect(self.browse_folder)
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form = QFormLayout(box)
        form.addRow('Default location', row)
        form.addRow(_note('Where new workspaces are created. Empty uses the standard folder.'))

        crs_box = QGroupBox('Project coordinate reference system')
        self.crs = QgsProjectionSelectionWidget()
        self.crs.setOptionVisible(
            QgsProjectionSelectionWidget.CrsOption.CrsNotSet, False)
        crs_form = QFormLayout(crs_box)
        crs_form.addRow('Default CRS', self.crs)
        crs_form.addRow(_note('CRS of new projects. Default: WGS 84 / UTM zone 48N (EPSG:32648).'))

        out = QGroupBox('Tool output')
        self.output_to_project = QCheckBox(
            'Save tool output in the project default database and folder')
        lay = QVBoxLayout(out)
        lay.addWidget(self.output_to_project)
        lay.addWidget(_note('New outputs start in the default database or folder of the project.'))
        self.geometry_fields = QCheckBox(
            'Keep Shape_Length and Shape_Area up to date on project database layers')
        lay.addWidget(self.geometry_fields)
        lay.addWidget(_note('Adds the fields to line and polygon layers in the project databases, as a File Geodatabase does, and recalculates them whenever a feature is created or edited.'))
        basemap_box, extent_box = self._new_project_boxes()
        return self._page(page, box, crs_box, basemap_box, extent_box, out)

    def _new_project_boxes(self):
        box = QGroupBox('Basemap for new projects')
        self.basemap_on = QCheckBox('Add a basemap to every new project')
        self.basemap = QComboBox()
        for name in background_basemaps():
            self.basemap.addItem(name, name)
        self.basemap_on.toggled.connect(self.basemap.setEnabled)
        form = QFormLayout(box)
        form.addRow(self.basemap_on)
        form.addRow('Basemap', self.basemap)
        form.addRow(_note('Added to new projects only. Requires an internet connection.'))

        extent_box = QGroupBox('Start extent of new projects')
        self.zoom_on = QCheckBox('Zoom new projects to a country')
        self.country = QComboBox()
        self.country.setEditable(True)
        self.country.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for iso3, name in country_extents.available():
            self.country.addItem(name, iso3)
        completer = QCompleter(self.country.model(), self.country)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.country.setCompleter(completer)
        self.country.lineEdit().setPlaceholderText('Type to search')
        self.country.activated.connect(self._country_picked)
        self.country.lineEdit().editingFinished.connect(self._country_typed)
        self._last_country = 0
        self.zoom_on.toggled.connect(self.country.setEnabled)
        extent_form = QFormLayout(extent_box)
        extent_form.addRow(self.zoom_on)
        extent_form.addRow('Country', self.country)
        return box, extent_box

    def _catalog_page(self):
        page = QWidget()
        box = QGroupBox('Opening projects')
        self.ask_convert = QCheckBox(
            'Ask to convert a traditional QGIS project to a workspace when '
            'it is opened')
        lay = QVBoxLayout(box)
        lay.addWidget(self.ask_convert)
        lay.addWidget(_note('Projects already in a workspace are not asked about.'))
        save_box = QGroupBox('Saving projects')
        self.save_mode = QComboBox()
        self.save_mode.addItem('Ask me each time', ks.SAVE_ASK)
        self.save_mode.addItem('Always save as a workspace', ks.SAVE_WORKSPACE)
        self.save_mode.addItem('Always save as a QGIS project (.qgz)',
                               ks.SAVE_TRADITIONAL)
        save_form = QFormLayout(save_box)
        save_form.addRow('Save / Save As', self.save_mode)
        save_form.addRow(_note(
            'Applies to a new project, or one that is not in a workspace yet. '
            'A project that already has a file is simply saved.'))
        return self._page(page, box, save_box)

    def _appearance_page(self):
        page = QWidget()
        box = QGroupBox('QGIS theme')
        self.follow_theme = QCheckBox(
            'Match QGIS to the computer\'s light or dark mode')
        lay = QVBoxLayout(box)
        lay.addWidget(self.follow_theme)
        lay.addWidget(_note(
            'QGIS opens in the dark theme when Windows is in dark mode and in '
            'the light theme when it is in light mode. Turning this on also '
            'switches the current session now. Some parts of the window may '
            'only change after QGIS is restarted.'))
        sel_box = QGroupBox('Selection')
        self.pro_selection = QCheckBox(
            'Show selected features in ArcGIS Pro style (cyan outline)')
        sel_lay = QVBoxLayout(sel_box)
        sel_lay.addWidget(self.pro_selection)
        sel_lay.addWidget(_note(
            'Selected features keep their symbol and get a bright cyan outline '
            '(a ring on a point), as in ArcGIS Pro. Turn it off for the '
            'standard QGIS yellow.'))
        layers_box = QGroupBox('Layers panel')
        self.pro_layers = QCheckBox(
            'Show the Layers panel in ArcGIS Pro style')
        layers_lay = QVBoxLayout(layers_box)
        layers_lay.addWidget(self.pro_layers)
        layers_lay.addWidget(_note(
            'Filled-triangle expanders, roomier rows, a light-blue selection '
            'and larger legend swatches, as in the Pro Contents pane. Nothing '
            'else about the panel changes. Turn it off for the standard QGIS '
            'look.'))
        tabs_box = QGroupBox('Map tabs')
        self.map_tabs = QCheckBox(
            'Show maps and print layouts as tabs above the map canvas')
        tabs_lay = QVBoxLayout(tabs_box)
        tabs_lay.addWidget(self.map_tabs)
        self.tabs_dock_layouts = QCheckBox(
            'Open print layouts as tabs in the main window')
        self.tabs_sync_themes = QCheckBox(
            'Keep a map theme for each map (for print layouts)')
        self.tabs_auto = QCheckBox('Automatically use map tabs in new projects')
        for box_ in (self.tabs_dock_layouts, self.tabs_sync_themes, self.tabs_auto):
            tabs_lay.addWidget(box_)
            self.map_tabs.toggled.connect(box_.setEnabled)
        tabs_lay.addWidget(_note(
            'As in ArcGIS Pro, one project can hold several maps, each with '
            'its own layers, extent and CRS, and print layouts open as tabs '
            'too. Each map is a top-level group in the Layers panel, and the '
            'tab bar offers Organize this project into Map tabs. Turn it off '
            'to go back to the standard QGIS window.'))
        start_box = QGroupBox('Start page')
        self.start_page = QCheckBox(
            'Show the KGA start page instead of the QGIS welcome page')
        start_lay = QVBoxLayout(start_box)
        start_lay.addWidget(self.start_page)
        self.start_menu = QCheckBox('Also hide the menu bar while the start page is showing')
        start_lay.addWidget(self.start_menu)
        start_lay.addWidget(_note(
            'The page QGIS shows when no project is open: recent projects as '
            'cards, new projects and templates. Turn it off to get the '
            'standard QGIS welcome page back.'))
        layout_box = QGroupBox('Toolbars and panels')
        self.remember_ui = QCheckBox(
            'Remember the toolbars and panels that are enabled')
        self.remember_ui.toggled.connect(self._remember_toggled)
        self.remember_now = QPushButton('Remember current layout')
        self.reset_ui = QPushButton('Reset toolbars and panels')
        self.remember_now.clicked.connect(self._remember_now)
        self.reset_ui.clicked.connect(self._reset_now)
        self.layout_status = _note('')
        layout_buttons = QHBoxLayout()
        layout_buttons.addWidget(self.remember_now)
        layout_buttons.addWidget(self.reset_ui)
        layout_buttons.addStretch(1)
        layout_lay = QVBoxLayout(layout_box)
        layout_lay.addWidget(self.remember_ui)
        layout_lay.addLayout(layout_buttons)
        layout_lay.addWidget(self.layout_status)
        layout_lay.addWidget(_note(
            'Tick the box and press OK to remember what is on now. If a toolbar '
            'or panel is later turned off by accident, Reset switches every '
            'remembered one back on, in its old place, and leaves the rest as '
            'it is. "Remember current layout" replaces the memory with what is '
            'on now.'))
        return self._page(page, box, sel_box, layers_box, tabs_box, start_box, layout_box)

    @staticmethod
    def _page(page, *boxes):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 0, 8, 0)
        for box in boxes:
            layout.addWidget(box)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------- behaviour

    @staticmethod
    def _remember_window():
        from qgis.utils import iface
        return ui_layout.remember(iface.mainWindow())

    @staticmethod
    def _reset_window():
        from qgis.utils import iface
        return ui_layout.reset(iface.mainWindow())

    def _remember_toggled(self, _checked=False):
        on = self.remember_ui.isChecked()
        self.remember_now.setEnabled(on)
        self.reset_ui.setEnabled(on and ui_layout.has_snapshot())

    def _show_layout_status(self):
        text = ui_layout.describe()
        self.layout_status.setText(
            'Remembered: {}.'.format(text) if text else 'Nothing remembered yet.')
        self._remember_toggled()

    def _remember_now(self):
        self.layout_hooks[0]()
        self._show_layout_status()

    def _reset_now(self):
        switched = self.layout_hooks[1]()
        QMessageBox.information(
            self, 'Toolbars and panels',
            '{} toolbar(s) and panel(s) switched back on.'.format(switched)
            if switched else 'Everything remembered is already on.')

    def _country_picked(self, index):
        self._last_country = index

    def _country_typed(self):
        """Typed text that names a country selects it; anything else is undone."""
        index = self.country.findText(self.country.currentText().strip(),
                                      Qt.MatchFlag.MatchFixedString)
        if index < 0:
            index = self._last_country
        self.country.setCurrentIndex(index)
        self._last_country = index

    def _show_country(self, iso3):
        index = max(self.country.findData(iso3), 0)
        self.country.setCurrentIndex(index)
        self._last_country = index

    def browse_folder(self):
        start = self.folder.text().strip() or ws.factory_location()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            if parent == start:
                start = ws.documents_dir()
                break
            start = parent
        chosen = QFileDialog.getExistingDirectory(
            self, 'Default location for new projects', start)
        if chosen:
            self.folder.setText(_native(chosen))

    def load(self):
        """Show the saved values."""
        self.folder.setText(_native(ks.projects_folder()))
        self.output_to_project.setChecked(ks.output_to_project())
        self.geometry_fields.setChecked(ks.geometry_fields())
        self.basemap_on.setChecked(ks.basemap_enabled())
        index = self.basemap.findData(ks.basemap_name())
        self.basemap.setCurrentIndex(max(index, 0))
        self.basemap.setEnabled(self.basemap_on.isChecked())
        self.ask_convert.setChecked(ks.ask_convert())
        self.save_mode.setCurrentIndex(max(self.save_mode.findData(ks.save_mode()), 0))
        self.crs.setCrs(QgsCoordinateReferenceSystem(ks.project_crs()))
        self.zoom_on.setChecked(ks.zoom_enabled())
        self._show_country(ks.zoom_country())
        self.country.setEnabled(self.zoom_on.isChecked())
        self.follow_theme.setChecked(ks.follow_system_theme())
        self.pro_selection.setChecked(ks.pro_selection())
        self.pro_layers.setChecked(ks.pro_layers())
        self.map_tabs.setChecked(ks.map_tabs())
        self.tabs_dock_layouts.setChecked(ks.map_tabs_dock_layouts())
        self.tabs_sync_themes.setChecked(ks.map_tabs_sync_themes())
        self.tabs_auto.setChecked(ks.map_tabs_auto_organize())
        for box_ in (self.tabs_dock_layouts, self.tabs_sync_themes, self.tabs_auto):
            box_.setEnabled(self.map_tabs.isChecked())
        self.start_page.setChecked(ks.start_page_enabled())
        self.start_menu.setChecked(ks.start_page_hide_menu())
        self._was_remembering = ks.remember_layout()
        self.remember_ui.setChecked(self._was_remembering)
        self._show_layout_status()

    def restore_defaults(self):
        """Show the factory values; nothing is saved until OK."""
        d = ks.DEFAULTS
        self.folder.setText(d[ks.KEY_PROJECTS_FOLDER])
        self.output_to_project.setChecked(d[ks.KEY_OUTPUT_TO_PROJECT])
        self.geometry_fields.setChecked(d[ks.KEY_GEOMETRY_FIELDS])
        self.basemap_on.setChecked(d[ks.KEY_BASEMAP_ENABLED])
        self.basemap.setCurrentIndex(
            max(self.basemap.findData(d[ks.KEY_BASEMAP_NAME]), 0))
        self.ask_convert.setChecked(d[ks.KEY_ASK_CONVERT])
        self.save_mode.setCurrentIndex(
            max(self.save_mode.findData(d[ks.KEY_SAVE_MODE]), 0))
        self.crs.setCrs(QgsCoordinateReferenceSystem(d[ks.KEY_PROJECT_CRS]))
        self.zoom_on.setChecked(d[ks.KEY_ZOOM_ENABLED])
        self._show_country(d[ks.KEY_ZOOM_COUNTRY])
        self.follow_theme.setChecked(d[ks.KEY_FOLLOW_SYSTEM_THEME])
        self.pro_selection.setChecked(d[ks.KEY_PRO_SELECTION])
        self.pro_layers.setChecked(d[ks.KEY_PRO_LAYERS])
        self.map_tabs.setChecked(d[ks.KEY_MAP_TABS])
        self.tabs_dock_layouts.setChecked(d[ks.KEY_MAP_TABS_DOCK_LAYOUTS])
        self.tabs_sync_themes.setChecked(d[ks.KEY_MAP_TABS_SYNC_THEMES])
        self.tabs_auto.setChecked(d[ks.KEY_MAP_TABS_AUTO_ORGANIZE])
        self.start_page.setChecked(d[ks.KEY_START_PAGE])
        self.start_menu.setChecked(d[ks.KEY_START_HIDE_MENU])
        self.remember_ui.setChecked(d[ks.KEY_REMEMBER_LAYOUT])

    def problem(self):
        """Why the settings cannot be saved, or ''."""
        text = self.folder.text().strip()
        if text and not os.path.isabs(text):
            return 'Enter the full path of the folder.'
        if text and os.path.exists(text) and not os.path.isdir(text):
            return '"{}" is a file, not a folder.'.format(text)
        return ''

    def apply(self):
        ks.set_value(ks.KEY_PROJECTS_FOLDER, self.folder.text().strip())
        ks.set_value(ks.KEY_OUTPUT_TO_PROJECT, self.output_to_project.isChecked())
        ks.set_value(ks.KEY_GEOMETRY_FIELDS, self.geometry_fields.isChecked())
        ks.set_value(ks.KEY_BASEMAP_ENABLED, self.basemap_on.isChecked())
        ks.set_value(ks.KEY_BASEMAP_NAME,
                     self.basemap.currentData() or ks.DEFAULT_BASEMAP)
        ks.set_value(ks.KEY_ASK_CONVERT, self.ask_convert.isChecked())
        ks.set_value(ks.KEY_SAVE_MODE, self.save_mode.currentData() or ks.SAVE_ASK)
        ks.set_value(ks.KEY_PROJECT_CRS,
                     self.crs.crs().authid() or ks.DEFAULT_CRS)
        ks.set_value(ks.KEY_ZOOM_ENABLED, self.zoom_on.isChecked())
        ks.set_value(ks.KEY_ZOOM_COUNTRY,
                     self.country.currentData() or ks.DEFAULT_COUNTRY)
        ks.set_value(ks.KEY_FOLLOW_SYSTEM_THEME, self.follow_theme.isChecked())
        ks.set_value(ks.KEY_PRO_SELECTION, self.pro_selection.isChecked())
        ks.set_value(ks.KEY_PRO_LAYERS, self.pro_layers.isChecked())
        ks.set_value(ks.KEY_MAP_TABS, self.map_tabs.isChecked())
        ks.set_value(ks.KEY_MAP_TABS_DOCK_LAYOUTS, self.tabs_dock_layouts.isChecked())
        ks.set_value(ks.KEY_MAP_TABS_SYNC_THEMES, self.tabs_sync_themes.isChecked())
        ks.set_value(ks.KEY_MAP_TABS_AUTO_ORGANIZE, self.tabs_auto.isChecked())
        ks.set_value(ks.KEY_START_PAGE, self.start_page.isChecked())
        ks.set_value(ks.KEY_START_HIDE_MENU, self.start_menu.isChecked())
        remembering = self.remember_ui.isChecked()
        ks.set_value(ks.KEY_REMEMBER_LAYOUT, remembering)
        # Ticked just now (or nothing stored yet): remember what is on. Already
        # ticked: leave the memory alone, or OK would overwrite it with a layout
        # that may be the very thing that went wrong.
        if remembering and (not self._was_remembering
                            or not ui_layout.has_snapshot()):
            self.layout_hooks[0]()

    def accept(self):
        why = self.problem()
        if why:
            self.pages.setCurrentRow(0)
            self.folder.setFocus(Qt.FocusReason.OtherFocusReason)
            QMessageBox.warning(self, 'Settings', why)
            return
        self.apply()
        if self.follow_theme.isChecked():
            with suppress(Exception):
                system_theme.follow_system()
        super().accept()

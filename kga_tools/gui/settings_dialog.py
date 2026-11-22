# -*- coding: utf-8 -*-
"""The KGA Toolbox Settings window (KGA Toolbox menu > Settings).

Every KGA setting lives here, one page per area. Adding a setting means adding
its key to `core/kga_settings.py` and a control to a page below; `apply()`
writes the controls back and `restore_defaults()` resets them.
"""

from contextlib import suppress
import os

from qgis.core import QgsCoordinateReferenceSystem
from qgis.gui import QgsColorButton, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QCompleter
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import basemaps, country_extents
from ..core import kga_settings as ks
from ..core import system_theme, ui_layout
from .bubble_settings import BubbleControls
from ..core import workspace as ws
from .temporary_pan.keys import DEFAULT_KEY, KeyCaptureButton, check_key, key_name


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

    def __init__(self, parent=None, layout_hooks=None, page=None):
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
        self._add_page('Editor', self._editor_page())
        self._add_page('Bubbles', self._bubbles_page())

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
        rows = [self.pages.item(r).text() for r in range(self.pages.count())]
        self.pages.setCurrentRow(rows.index(page) if page in rows else 0)

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
        scratch_box = QGroupBox('Closing projects')
        self.scratch_mode = QComboBox()
        self.scratch_mode.addItem('Ask me what to do with them', ks.SCRATCH_ASK)
        self.scratch_mode.addItem('Add them to the project GeoPackage automatically',
                                  ks.SCRATCH_PROJECT)
        self.scratch_mode.addItem('Do nothing (they are lost)', ks.SCRATCH_OFF)
        scratch_form = QFormLayout(scratch_box)
        scratch_form.addRow('Scratch layers', self.scratch_mode)
        scratch_form.addRow(_note(
            'Scratch (memory) layers exist only until the project is closed, '
            'another one is opened, or QGIS quits. With the first choice you '
            'can save them to the project GeoPackage or to another one, or '
            'let them go. A project that is not in a workspace has no project '
            'GeoPackage, so you are asked where to save instead.'))
        return self._page(page, box, save_box, scratch_box)

    def _bubbles_page(self):
        """Every bubble on the map (bags and panels) in one place; see bubble_settings.py."""
        page = QWidget()
        self.bubbles = BubbleControls()
        self.bag_animation = self.bubbles.animation
        self.window_reveal = self.bubbles.window_reveal
        return self._page(page, *self.bubbles.boxes)

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
        self.tabs_layer_lists = QCheckBox(
            'Layer drop-down lists show only the layers of the current map')
        for box_ in (self.tabs_dock_layouts, self.tabs_sync_themes, self.tabs_auto,
                     self.tabs_layer_lists):
            tabs_lay.addWidget(box_)
            self.map_tabs.toggled.connect(box_.setEnabled)
        tabs_lay.addWidget(_note(
            'As in ArcGIS Pro, one project can hold several maps, each with '
            'its own layers, extent and CRS, and print layouts open as tabs '
            'too. Each map is a top-level group in the Layers panel, and the '
            'tab bar offers Organize this project into Map tabs. Turn it off '
            'to go back to the standard QGIS window.'))
        nav_box = self._navigation_box()
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
        return self._page(page, box, sel_box, layers_box, tabs_box, nav_box, start_box, layout_box)

    def _navigation_box(self):
        box = QGroupBox('Map navigation')
        self.smooth_nav = QCheckBox(
            'Smooth navigation: animated zoom and kinetic panning, as in ArcGIS Pro')
        self.nav_zoom = QCheckBox('Animated mouse-wheel zoom, anchored under the cursor')
        self.nav_zoom_ms = QSpinBox()
        self.nav_zoom_ms.setRange(30, 500)
        self.nav_zoom_ms.setSuffix(' ms')
        self.nav_zoom_ms.setToolTip('Lower is snappier, higher is floatier.')
        self.nav_inertia = QCheckBox('Keep gliding after a pan is released')
        self.nav_glide_ms = QSpinBox()
        self.nav_glide_ms.setRange(80, 2000)
        self.nav_glide_ms.setSuffix(' ms')
        self.nav_glide_ms.setToolTip('How long a flick keeps gliding.')
        self.nav_keyboard = QCheckBox(
            'Arrow keys pan continuously, Page Up / Page Down zoom smoothly')
        self.nav_right_zoom = QCheckBox(
            'Zoom by holding the right button and dragging, with the Pan tool')
        self.nav_right_dir = QComboBox()
        self.nav_right_dir.addItem('Drag up to zoom in, down to zoom out', ks.RIGHT_UP_IN)
        self.nav_right_dir.addItem('Drag up to zoom out, down to zoom in', ks.RIGHT_UP_OUT)
        self.nav_underlay = QCheckBox('Fill the edges while zooming out or panning')
        self.nav_underlay.setToolTip(
            'Keeps a low-resolution picture of a larger area behind the map, so '
            'zooming out never shows blank space. It costs some background rendering.')
        self.temp_pan = QCheckBox(
            'Temporary pan: hold a key and drag with the left button while digitizing')
        self.temp_pan_key = KeyCaptureButton(ks.temp_pan_key())
        self.temp_pan_key.keyCaptured.connect(self._temp_pan_key_captured)
        self.temp_pan_msg = QLabel()
        self.temp_pan_msg.setWordWrap(True)
        form = QFormLayout(box)
        form.addRow(self.smooth_nav)
        form.addRow(self.nav_zoom)
        form.addRow('Zoom smoothness', self.nav_zoom_ms)
        form.addRow(self.nav_inertia)
        form.addRow('Glide length', self.nav_glide_ms)
        form.addRow(self.nav_keyboard)
        form.addRow(self.nav_right_zoom)
        form.addRow('Right-drag direction', self.nav_right_dir)
        form.addRow(self.nav_underlay)
        form.addRow(_note(
            'Drag with the Pan tool, or with the middle button in any tool. '
            'Ctrl, Shift and Alt with the wheel, and the keys of editing tools, '
            'keep their QGIS behaviour. Turn it off to go back to the standard '
            'QGIS navigation straight away.'))
        form.addRow(self.temp_pan)
        form.addRow('Pan key (hold)', self.temp_pan_key)
        form.addRow(self.temp_pan_msg)
        form.addRow(_note(
            'Start a line or polygon, hold the key and drag with the left button to '
            'move the map, then let go and carry on clicking: no vertex is added and '
            'the drawing is not interrupted. The map needs the keyboard focus (click '
            'it once). Right-clicks are ignored while the key is held. The middle '
            'button and Space already pan in QGIS.'))
        self.temp_pan.toggled.connect(self._nav_toggled)
        self.smooth_nav.toggled.connect(self._nav_toggled)
        self.nav_zoom.toggled.connect(self._nav_toggled)
        self.nav_inertia.toggled.connect(self._nav_toggled)
        self.nav_right_zoom.toggled.connect(self._nav_toggled)
        return box

    def _temp_pan_key_captured(self, key):
        ok, msg = check_key(key)
        if not ok:
            self.temp_pan_msg.setText(
                '<span style="color:#cf222e">{} cannot be the pan key: {}. Still using {}.</span>'
                .format(key_name(key), msg, key_name(self.temp_pan_key.key)))
            return
        self.temp_pan_key.set_key(key)
        self._show_temp_pan_note()

    def _show_temp_pan_note(self):
        note = check_key(self.temp_pan_key.key)[1]
        self.temp_pan_msg.setText(
            '<span style="color:#b26b00">{}</span>'.format(note) if note else '')

    def _nav_toggled(self, _checked=False):
        self.temp_pan_key.setEnabled(self.temp_pan.isChecked())
        on = self.smooth_nav.isChecked()
        for widget in (self.nav_zoom, self.nav_inertia, self.nav_keyboard,
                       self.nav_right_zoom, self.nav_underlay):
            widget.setEnabled(on)
        self.nav_zoom_ms.setEnabled(on and self.nav_zoom.isChecked())
        self.nav_glide_ms.setEnabled(on and self.nav_inertia.isChecked())
        self.nav_right_dir.setEnabled(on and self.nav_right_zoom.isChecked())

    def _editor_page(self):
        """Editing aids: the vertices and nodes view (ArcGIS Pro's "show vertices and nodes")."""
        page = QWidget()
        bar_box = self._toolbar_box()
        box = QGroupBox('Vertices and nodes view')
        self.node_view = QCheckBox('Show vertices and nodes on the map')
        self.node_vertices = QCheckBox('Vertices (squares)')
        self.node_nodes = QCheckBox('Nodes: where features connect or cross (circles)')
        self.node_midpoints = QCheckBox('Midpoints of segments (diamonds)')
        self.node_scope = QComboBox()
        self.node_scope.addItem('All visible line and polygon layers', ks.NODE_SCOPE_VISIBLE)
        self.node_scope.addItem('Only the layer selected in the Layers panel', ks.NODE_SCOPE_ACTIVE)
        self.node_scope.addItem('Only layers in edit mode', ks.NODE_SCOPE_EDITABLE)
        self.node_vertex_size = QSpinBox()
        self.node_vertex_size.setRange(4, 20)
        self.node_vertex_size.setSuffix(' px')
        self.node_node_size = QSpinBox()
        self.node_node_size.setRange(5, 28)
        self.node_node_size.setSuffix(' px')
        self.node_vertex_color = QgsColorButton()
        self.node_node_color = QgsColorButton()
        self.node_mid_color = QgsColorButton()
        for button in (self.node_vertex_color, self.node_node_color, self.node_mid_color):
            button.setAllowOpacity(False)
        self.node_max = QSpinBox()
        self.node_max.setRange(2000, 1000000)
        self.node_max.setSingleStep(5000)
        form = QFormLayout(box)
        form.addRow(self.node_view)
        form.addRow(_note(
            'Switch it on and off here, with the Vertices and Nodes button in the status bar beside the '
            "coordinates, or from the map's right-click menu."))
        form.addRow(self.node_vertices)
        form.addRow(self.node_nodes)
        form.addRow(self.node_midpoints)
        form.addRow('Layers', self.node_scope)
        form.addRow('Vertex size', self.node_vertex_size)
        form.addRow('Node size', self.node_node_size)
        form.addRow('Vertex colour', self.node_vertex_color)
        form.addRow('Node colour', self.node_node_color)
        form.addRow('Midpoint colour', self.node_mid_color)
        form.addRow('Stop above', self.node_max)
        form.addRow(_note(
            'A node is a vertex shared by two features, a line that ends on another feature, a polygon '
            "corner on a neighbour's edge, or the point where two features cross. When the view holds "
            'more vertices than the limit above, nothing is drawn until you zoom in, so a zoomed-out '
            'map never slows down.'))
        grid_box = QGroupBox('Grid')
        self.grid_on = QCheckBox('Show a grid on the map')
        self.grid_spacing = QDoubleSpinBox()
        self.grid_spacing.setDecimals(4)
        self.grid_spacing.setRange(0.0001, 10000000.0)
        self.grid_unit = QComboBox()
        self.grid_unit.addItem('m', ks.GRID_UNIT_M)
        self.grid_unit.addItem('ft', ks.GRID_UNIT_FT)
        self.grid_unit.addItem('map units', ks.GRID_UNIT_MAP)
        spacing_row = QHBoxLayout()
        spacing_row.addWidget(self.grid_spacing, 1)
        spacing_row.addWidget(self.grid_unit)
        self.grid_rotation = QDoubleSpinBox()
        self.grid_rotation.setDecimals(2)
        self.grid_rotation.setRange(-360.0, 360.0)
        self.grid_rotation.setSuffix(' °')
        grid_form = QFormLayout(grid_box)
        grid_form.addRow(self.grid_on)
        grid_form.addRow('Spacing', spacing_row)
        grid_form.addRow('Rotation', self.grid_rotation)
        grid_form.addRow(_note(
            'Switch the grid on and off with the Grid button in the status bar beside the coordinates or '
            "from the map's right-click menu; the button's arrow sets the same spacing and rotation. When the lines would be "
            'closer than a few pixels the spacing is multiplied by 10 until they are not (dynamic scaling). '
            'Rotation turns the grid counter-clockwise about the map origin.'))
        return self._page(page, bar_box, box, grid_box)

    def _toolbar_box(self):
        """The floating editing toolbar over the map (Create Features), as in ArcGIS Pro's Editing options."""
        box = QGroupBox('Toolbar')
        self.bar_show = QCheckBox('Show the editing toolbar on the map')
        self.bar_position = {}
        self.bar_size = {}
        position_row, size_row = QHBoxLayout(), QHBoxLayout()
        for group, store, row, choices in (
                (QButtonGroup(box), self.bar_position, position_row,
                 (('left', 'Left'), ('top', 'Top'), ('bottom', 'Bottom'), ('right', 'Right'))),
                (QButtonGroup(box), self.bar_size, size_row,
                 (('small', 'Small'), ('medium', 'Medium'), ('large', 'Large')))):
            for ident, label in choices:
                radio = QRadioButton(label)
                group.addButton(radio)
                store[ident] = radio
                row.addWidget(radio)
            row.addStretch(1)
        self.bar_magnify = QCheckBox('Magnify Toolbar')
        self.magnify_toolbars = QCheckBox('Magnify the KGA toolbars in the QGIS window '
                                          '(KGA Toolbox, Editor, Layer and GeoAI)')
        self.bar_magnify.setToolTip('The button under the cursor grows, and its neighbours a little, like the macOS Dock.')
        form = QFormLayout(box)
        form.addRow(self.bar_show)
        form.addRow('Position', position_row)
        form.addRow('Size', size_row)
        form.addRow(self.bar_magnify)
        form.addRow(self.magnify_toolbars)
        form.addRow(_note(
            'The toolbar that floats over the map while a Create Features template is active: segment types, '
            'sketch tools, Finish and Cancel. Magnify Toolbar makes the button under the cursor swell, and the '
            'row open up around it, so small buttons are easy to hit. "Magnify Toolbar" is this floating toolbar; '
            'the last checkbox is the same effect on the KGA toolbars at the top of the QGIS window. Each can be turned off '
            'on its own.'))
        self.bar_show.toggled.connect(self._bar_toggled)
        return box

    def _bar_toggled(self, _checked=False):
        on = self.bar_show.isChecked()
        for radio in list(self.bar_position.values()) + list(self.bar_size.values()):
            radio.setEnabled(on)
        self.bar_magnify.setEnabled(on)

    def _show_bar(self, shown, position, size, magnify):
        self.bar_show.setChecked(shown)
        self.bar_position.get(position, self.bar_position['bottom']).setChecked(True)
        self.bar_size.get(size, self.bar_size['medium']).setChecked(True)
        self.bar_magnify.setChecked(magnify)
        self._bar_toggled()

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
        self.scratch_mode.setCurrentIndex(
            max(self.scratch_mode.findData(ks.scratch_on_close()), 0))
        self.crs.setCrs(QgsCoordinateReferenceSystem(ks.project_crs()))
        self.zoom_on.setChecked(ks.zoom_enabled())
        self._show_country(ks.zoom_country())
        self.country.setEnabled(self.zoom_on.isChecked())
        self.follow_theme.setChecked(ks.follow_system_theme())
        self.bag_animation.setCurrentIndex(
            max(self.bag_animation.findData(ks.bag_animation()), 0))
        self.window_reveal.setChecked(ks.window_reveal())
        self.bubbles.load()
        self.pro_selection.setChecked(ks.pro_selection())
        self.pro_layers.setChecked(ks.pro_layers())
        self.magnify_toolbars.setChecked(ks.magnify_toolbars())
        self._show_bar(ks.sketch_bar_enabled(), ks.sketch_bar_position(), ks.sketch_bar_size(),
                       ks.sketch_bar_magnify())
        self.node_view.setChecked(ks.node_view_enabled())
        self.node_vertices.setChecked(ks.node_view_vertices())
        self.node_nodes.setChecked(ks.node_view_nodes())
        self.node_midpoints.setChecked(ks.node_view_midpoints())
        self.node_scope.setCurrentIndex(max(self.node_scope.findData(ks.node_view_scope()), 0))
        self.node_vertex_size.setValue(ks.node_view_vertex_size())
        self.node_node_size.setValue(ks.node_view_node_size())
        self.node_vertex_color.setColor(QColor(ks.node_view_vertex_color()))
        self.node_node_color.setColor(QColor(ks.node_view_node_color()))
        self.node_mid_color.setColor(QColor(ks.node_view_midpoint_color()))
        self.node_max.setValue(ks.node_view_max_vertices())
        self.grid_on.setChecked(ks.grid_enabled())
        self.grid_spacing.setValue(ks.grid_spacing())
        self.grid_unit.setCurrentIndex(max(self.grid_unit.findData(ks.grid_unit()), 0))
        self.grid_rotation.setValue(ks.grid_rotation())
        self.map_tabs.setChecked(ks.map_tabs())
        self.tabs_dock_layouts.setChecked(ks.map_tabs_dock_layouts())
        self.tabs_sync_themes.setChecked(ks.map_tabs_sync_themes())
        self.tabs_auto.setChecked(ks.map_tabs_auto_organize())
        self.tabs_layer_lists.setChecked(ks.layer_lists_current_map())
        for box_ in (self.tabs_dock_layouts, self.tabs_sync_themes, self.tabs_auto, self.tabs_layer_lists):
            box_.setEnabled(self.map_tabs.isChecked())
        self.smooth_nav.setChecked(ks.smooth_nav_enabled())
        self.nav_zoom.setChecked(ks.smooth_nav_zoom())
        self.nav_zoom_ms.setValue(ks.smooth_nav_zoom_ms())
        self.nav_inertia.setChecked(ks.smooth_nav_inertia())
        self.nav_glide_ms.setValue(ks.smooth_nav_glide_ms())
        self.nav_keyboard.setChecked(ks.smooth_nav_keyboard())
        self.nav_right_zoom.setChecked(ks.smooth_nav_right_zoom())
        self.nav_right_dir.setCurrentIndex(
            max(self.nav_right_dir.findData(ks.smooth_nav_right_dir()), 0))
        self.nav_underlay.setChecked(ks.smooth_nav_underlay())
        self.temp_pan.setChecked(ks.temp_pan_enabled())
        key = ks.temp_pan_key()
        self.temp_pan_key.set_key(key if check_key(key)[0] else DEFAULT_KEY)
        self._show_temp_pan_note()
        self._nav_toggled()
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
        self.scratch_mode.setCurrentIndex(
            max(self.scratch_mode.findData(d[ks.KEY_SCRATCH_ON_CLOSE]), 0))
        self.crs.setCrs(QgsCoordinateReferenceSystem(d[ks.KEY_PROJECT_CRS]))
        self.zoom_on.setChecked(d[ks.KEY_ZOOM_ENABLED])
        self._show_country(d[ks.KEY_ZOOM_COUNTRY])
        self.follow_theme.setChecked(d[ks.KEY_FOLLOW_SYSTEM_THEME])
        self.bag_animation.setCurrentIndex(
            max(self.bag_animation.findData(d[ks.KEY_BAG_ANIMATION]), 0))
        self.window_reveal.setChecked(d[ks.KEY_WINDOW_REVEAL])
        self.bubbles.defaults()
        self.pro_selection.setChecked(d[ks.KEY_PRO_SELECTION])
        self.pro_layers.setChecked(d[ks.KEY_PRO_LAYERS])
        self.magnify_toolbars.setChecked(d[ks.KEY_MAGNIFY_TOOLBARS])
        self._show_bar(d[ks.KEY_SKETCH_BAR], d[ks.KEY_SKETCH_BAR_POSITION], d[ks.KEY_SKETCH_BAR_SIZE],
                       d[ks.KEY_SKETCH_BAR_MAGNIFY])
        self.node_view.setChecked(d[ks.KEY_NODE_VIEW])
        self.node_vertices.setChecked(d[ks.KEY_NODE_VERTICES])
        self.node_nodes.setChecked(d[ks.KEY_NODE_NODES])
        self.node_midpoints.setChecked(d[ks.KEY_NODE_MIDPOINTS])
        self.node_scope.setCurrentIndex(max(self.node_scope.findData(d[ks.KEY_NODE_SCOPE]), 0))
        self.node_vertex_size.setValue(d[ks.KEY_NODE_VERTEX_SIZE])
        self.node_node_size.setValue(d[ks.KEY_NODE_NODE_SIZE])
        self.node_vertex_color.setColor(QColor(d[ks.KEY_NODE_VERTEX_COLOR]))
        self.node_node_color.setColor(QColor(d[ks.KEY_NODE_NODE_COLOR]))
        self.node_mid_color.setColor(QColor(d[ks.KEY_NODE_MID_COLOR]))
        self.node_max.setValue(d[ks.KEY_NODE_MAX])
        self.grid_on.setChecked(d[ks.KEY_GRID])
        self.grid_spacing.setValue(d[ks.KEY_GRID_SPACING])
        self.grid_unit.setCurrentIndex(max(self.grid_unit.findData(d[ks.KEY_GRID_UNIT]), 0))
        self.grid_rotation.setValue(d[ks.KEY_GRID_ROTATION])
        self.map_tabs.setChecked(d[ks.KEY_MAP_TABS])
        self.tabs_dock_layouts.setChecked(d[ks.KEY_MAP_TABS_DOCK_LAYOUTS])
        self.tabs_sync_themes.setChecked(d[ks.KEY_MAP_TABS_SYNC_THEMES])
        self.tabs_auto.setChecked(d[ks.KEY_MAP_TABS_AUTO_ORGANIZE])
        self.tabs_layer_lists.setChecked(d[ks.KEY_LAYER_LISTS_CURRENT_MAP])
        self.smooth_nav.setChecked(d[ks.KEY_SMOOTH_NAV])
        self.nav_zoom.setChecked(d[ks.KEY_SMOOTH_NAV_ZOOM])
        self.nav_zoom_ms.setValue(d[ks.KEY_SMOOTH_NAV_ZOOM_MS])
        self.nav_inertia.setChecked(d[ks.KEY_SMOOTH_NAV_INERTIA])
        self.nav_glide_ms.setValue(d[ks.KEY_SMOOTH_NAV_GLIDE_MS])
        self.nav_keyboard.setChecked(d[ks.KEY_SMOOTH_NAV_KEYBOARD])
        self.nav_right_zoom.setChecked(d[ks.KEY_SMOOTH_NAV_RIGHT_ZOOM])
        self.nav_right_dir.setCurrentIndex(
            max(self.nav_right_dir.findData(d[ks.KEY_SMOOTH_NAV_RIGHT_DIR]), 0))
        self.nav_underlay.setChecked(d[ks.KEY_SMOOTH_NAV_UNDERLAY])
        self.temp_pan.setChecked(d[ks.KEY_TEMP_PAN])
        self.temp_pan_key.set_key(d[ks.KEY_TEMP_PAN_KEY])
        self._show_temp_pan_note()
        self._nav_toggled()
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
        ks.set_value(ks.KEY_SCRATCH_ON_CLOSE,
                     self.scratch_mode.currentData() or ks.SCRATCH_ASK)
        ks.set_value(ks.KEY_PROJECT_CRS,
                     self.crs.crs().authid() or ks.DEFAULT_CRS)
        ks.set_value(ks.KEY_ZOOM_ENABLED, self.zoom_on.isChecked())
        ks.set_value(ks.KEY_ZOOM_COUNTRY,
                     self.country.currentData() or ks.DEFAULT_COUNTRY)
        ks.set_value(ks.KEY_FOLLOW_SYSTEM_THEME, self.follow_theme.isChecked())
        ks.set_value(ks.KEY_BAG_ANIMATION, self.bag_animation.currentData())
        self.bubbles.apply()
        ks.set_value(ks.KEY_WINDOW_REVEAL, self.window_reveal.isChecked())
        ks.set_value(ks.KEY_PRO_SELECTION, self.pro_selection.isChecked())
        ks.set_value(ks.KEY_PRO_LAYERS, self.pro_layers.isChecked())
        ks.set_value(ks.KEY_MAGNIFY_TOOLBARS, self.magnify_toolbars.isChecked())
        ks.set_value(ks.KEY_SKETCH_BAR, self.bar_show.isChecked())
        ks.set_value(ks.KEY_SKETCH_BAR_POSITION,
                     next((k for k, r in self.bar_position.items() if r.isChecked()), 'bottom'))
        ks.set_value(ks.KEY_SKETCH_BAR_SIZE, next((k for k, r in self.bar_size.items() if r.isChecked()), 'medium'))
        ks.set_value(ks.KEY_SKETCH_BAR_MAGNIFY, self.bar_magnify.isChecked())
        ks.set_value(ks.KEY_NODE_VIEW, self.node_view.isChecked())
        ks.set_value(ks.KEY_NODE_VERTICES, self.node_vertices.isChecked())
        ks.set_value(ks.KEY_NODE_NODES, self.node_nodes.isChecked())
        ks.set_value(ks.KEY_NODE_MIDPOINTS, self.node_midpoints.isChecked())
        ks.set_value(ks.KEY_NODE_SCOPE, self.node_scope.currentData() or ks.NODE_SCOPE_VISIBLE)
        ks.set_value(ks.KEY_NODE_VERTEX_SIZE, self.node_vertex_size.value())
        ks.set_value(ks.KEY_NODE_NODE_SIZE, self.node_node_size.value())
        ks.set_value(ks.KEY_NODE_VERTEX_COLOR, self.node_vertex_color.color().name())
        ks.set_value(ks.KEY_NODE_NODE_COLOR, self.node_node_color.color().name())
        ks.set_value(ks.KEY_NODE_MID_COLOR, self.node_mid_color.color().name())
        ks.set_value(ks.KEY_NODE_MAX, self.node_max.value())
        ks.set_value(ks.KEY_GRID, self.grid_on.isChecked())
        ks.set_value(ks.KEY_GRID_SPACING, self.grid_spacing.value())
        ks.set_value(ks.KEY_GRID_UNIT, self.grid_unit.currentData() or ks.GRID_UNIT_M)
        ks.set_value(ks.KEY_GRID_ROTATION, self.grid_rotation.value())
        ks.set_value(ks.KEY_MAP_TABS, self.map_tabs.isChecked())
        ks.set_value(ks.KEY_MAP_TABS_DOCK_LAYOUTS, self.tabs_dock_layouts.isChecked())
        ks.set_value(ks.KEY_MAP_TABS_SYNC_THEMES, self.tabs_sync_themes.isChecked())
        ks.set_value(ks.KEY_MAP_TABS_AUTO_ORGANIZE, self.tabs_auto.isChecked())
        ks.set_value(ks.KEY_LAYER_LISTS_CURRENT_MAP, self.tabs_layer_lists.isChecked())
        ks.set_value(ks.KEY_SMOOTH_NAV, self.smooth_nav.isChecked())
        ks.set_value(ks.KEY_SMOOTH_NAV_ZOOM, self.nav_zoom.isChecked())
        ks.set_value(ks.KEY_SMOOTH_NAV_ZOOM_MS, self.nav_zoom_ms.value())
        ks.set_value(ks.KEY_SMOOTH_NAV_INERTIA, self.nav_inertia.isChecked())
        ks.set_value(ks.KEY_SMOOTH_NAV_GLIDE_MS, self.nav_glide_ms.value())
        ks.set_value(ks.KEY_SMOOTH_NAV_KEYBOARD, self.nav_keyboard.isChecked())
        ks.set_value(ks.KEY_SMOOTH_NAV_RIGHT_ZOOM, self.nav_right_zoom.isChecked())
        ks.set_value(ks.KEY_SMOOTH_NAV_RIGHT_DIR,
                     self.nav_right_dir.currentData() or ks.RIGHT_UP_IN)
        ks.set_value(ks.KEY_SMOOTH_NAV_UNDERLAY, self.nav_underlay.isChecked())
        ks.set_value(ks.KEY_TEMP_PAN, self.temp_pan.isChecked())
        ks.set_value(ks.KEY_TEMP_PAN_KEY, int(self.temp_pan_key.key))
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

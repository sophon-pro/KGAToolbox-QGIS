# -*- coding: utf-8 -*-
"""Add Grid to Layout dialog.

One window over `core.layout_grid`. The user picks a layout, a map item and
either a new grid or one the map already has, sets it up across three tabs -
the grid itself, its frame, its labels - and presses Apply.

Four decisions shape it:

* **The map comes to the tool.** The documented route is to select the map in
  the layout designer and then run this, so the target row starts on whatever
  is selected there. It is still three combos rather than a fixed target,
  because a project with four sheets is the case that makes a grid tool worth
  having.
* **Apply keeps editing the same grid.** Pressing it on a new grid adds one and
  then selects it, so the second press changes that grid instead of stacking a
  second one on the map. Tuning an interval is the loop this tool exists to
  shorten.
* **Every setting lives in a `GridSpec`.** The widgets are filled from one and
  read back into one, so "load the grid that is already there", "apply a
  preset" and "start from the defaults" are all the same operation. Nothing is
  remembered between sessions: every window opens on the defaults.
* **The window follows its layout.** The layout designer is a top-level window,
  not a panel, so a dialog parented to the QGIS main window vanishes behind the
  sheet it is editing. This one re-parents onto the designer showing the target
  layout - see `follow_target_window`.

Enum members are written scoped (`Qt.Orientation.Horizontal`, not
`Qt.Horizontal`) because the unscoped spelling is gone in PyQt6, which QGIS 4
moves to.
"""

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsColorButton, QgsFontButton, QgsProjectionSelectionWidget

from ..branding import LOG_TAG, open_docs
from ..core import interior_labels as IL
from ..core import layout_grid as LG

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

ALG_NAME = 'add_grid'

#: What the grid combo shows for "make a new one".
NEW_GRID = '__new__'

#: Marks a preset combo entry as a grid *set* rather than a single-grid preset.
SET_PREFIX = 'set:'


def tr(text):
    return text


def _enum(holder, member, owner):
    """A Qt/QGIS enum member, scoped spelling first."""
    scoped = getattr(owner, holder, None)
    value = getattr(scoped, member, None) if scoped is not None else None
    if value is None:
        value = getattr(owner, member, None)
    return value


def _alive(obj):
    """True when `obj` is a Qt object C++ has not deleted under us.

    Layouts and map items are held in combo boxes between one press of Apply
    and the next, and the user can close a layout designer or delete a map in
    that window. Touching the wrapper afterwards raises RuntimeError.
    """
    if obj is None:
        return False
    try:
        obj.objectName()
    except RuntimeError:
        return False
    except AttributeError:                  # not a QObject; assume it is fine
        return True
    return True


def _same_object(left, right):
    """True when two wrappers point at one and the same C++ object.

    `is` alone is not enough: a layout reached through `designer.layout()` and
    the same layout reached through the layout manager can arrive as two
    different Python wrappers around one pointer.
    """
    if left is right:
        return left is not None
    if left is None or right is None:
        return False
    try:
        return sip.unwrapinstance(left) == sip.unwrapinstance(right)
    except (TypeError, ValueError, RuntimeError):
        return False


def _index_in(items, obj):
    """The position of `obj` in `items`, or -1."""
    for index, item in enumerate(items):
        if _same_object(item, obj):
            return index
    return -1


def _positioning_icon(key):
    """A small picture of where a positioning option puts its strips."""
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        color = QColor(70, 70, 70)
        painter.setPen(QPen(color, 1.6))
        painter.drawRect(3, 3, size - 6, size - 6)
        painter.setPen(QPen(color, 1.0))
        inner = size - 6
        for fraction in IL.POSITION_FRACTIONS[key]:
            pos = 3 + inner * fraction
            painter.drawLine(int(pos), 3, int(pos), size - 3)
            painter.drawLine(3, int(pos), size - 3, int(pos))
    finally:
        painter.end()
    return QIcon(pixmap)


class AddGridDialog(QDialog):
    """The whole tool."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('KGA Add Grid to Layout'))
        self.setObjectName('KgaAddGridDialog')
        self.resize(620, 640)

        #: Guards the handlers while the form is being filled from a spec.
        self._loading = False
        #: The grid set Apply will lay down, or None for the ordinary
        #: one-grid-at-a-time behaviour. Armed by picking a set from the preset
        #: combo and disarmed by anything that changes what is being edited.
        self._pending_set = None
        #: Caveats about that set, shown in the status line until it is applied.
        self._set_warnings = []
        #: The layouts and map items the two combos list, in the same order.
        #: They are held here rather than as the combos' user data because Qt
        #: stores that as a QVariant: a QgsPrintLayout put into one comes back
        #: out of `itemData` downcast to a plain QGraphicsScene, and a map item
        #: fares no better. Only the combo's index crosses that boundary.
        self._layouts = []
        self._maps = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(self._build_target())

        tabs = QTabWidget(self)
        tabs.addTab(self._build_grid_tab(), tr('Grid'))
        tabs.addTab(self._build_frame_tab(), tr('Frame'))
        tabs.addTab(self._build_labels_tab(), tr('Labels'))
        layout.addWidget(tabs, 1)

        self.status = QLabel('', self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color: palette(mid);')
        layout.addWidget(self.status)

        layout.addWidget(self._build_buttons())

        # Nothing is carried over from an earlier session: every window opens
        # on the same defaults, so a grid made today does not inherit the font,
        # size or placement of one tuned last week.
        self._loading = True
        self._select_key(self.letters_combo, 'margin')
        self._select_key(self.mgrs_step_combo, 'auto')
        self.one_km_check.setChecked(False)
        self.interior_check.setChecked(False)
        self.positioning_buttons[IL.DEFAULT_POSITION].setChecked(True)
        self._loading = False

        self.reload_targets()
        self._apply_visibility()

    # ----------------------------------------------------------------- target

    def _build_target(self):
        box = QGroupBox(tr('Which map'), self)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.layout_combo = QComboBox(box)
        self.layout_combo.currentIndexChanged.connect(self._on_layout_changed)
        layout_row = QWidget(box)
        layout_line = QHBoxLayout(layout_row)
        layout_line.setContentsMargins(0, 0, 0, 0)
        self.layout_combo.setParent(layout_row)
        layout_line.addWidget(self.layout_combo, 1)
        self.open_button = QPushButton(tr('Open'), layout_row)
        self.open_button.setToolTip(tr(
            'Open this layout in the layout designer, or bring it to the '
            'front when it is already open.'))
        self.open_button.clicked.connect(self._on_open_clicked)
        layout_line.addWidget(self.open_button)
        form.addRow(tr('Layout'), layout_row)

        self.map_combo = QComboBox(box)
        self.map_combo.currentIndexChanged.connect(self._on_map_changed)
        form.addRow(tr('Map item'), self.map_combo)

        row = QWidget(box)
        grid_row = QHBoxLayout(row)
        grid_row.setContentsMargins(0, 0, 0, 0)
        self.grid_combo = QComboBox(row)
        self.grid_combo.currentIndexChanged.connect(self._on_grid_changed)
        grid_row.addWidget(self.grid_combo, 1)
        self.reload_button = QPushButton(tr('Reload'), row)
        self.reload_button.setToolTip(tr(
            'Read the layouts, maps and grids again, after adding or removing '
            'one in the layout designer.'))
        self.reload_button.clicked.connect(self._on_reload_clicked)
        grid_row.addWidget(self.reload_button)
        form.addRow(tr('Grid'), row)

        self.name_edit = QLineEdit(box)
        self.name_edit.setPlaceholderText(tr('Grid'))
        form.addRow(tr('Grid name'), self.name_edit)

        self.preset_combo = QComboBox(box)
        self.preset_combo.addItem(tr('Start from...'), '')
        for key, label, _changes in LG.PRESETS:
            self.preset_combo.addItem(label, key)
        # Below the line, the presets that are not one grid but several. They
        # carry a 'set:' prefix so the one handler can tell them apart.
        self.preset_combo.insertSeparator(self.preset_combo.count())
        for key, label in LG.GRID_SETS:
            self.preset_combo.addItem(label, SET_PREFIX + key)
        self.preset_combo.setToolTip(tr(
            'Fill the whole form with a ready-made look, then adjust it. The '
            'MGRS entry is different: it lays down three grids at once, '
            'because a grid reference is read off three of them.'))
        self.preset_combo.currentIndexChanged.connect(self._on_preset_chosen)
        form.addRow(tr('Preset'), self.preset_combo)

        # Only shown while an MGRS set is armed - it decides how that set is
        # built, and means nothing the rest of the time.
        self.mgrs_step_combo = self._vocabulary_combo(
            box, [(key, label, key) for key, label, _step in LG.MGRS_INTERVALS])
        self.mgrs_step_combo.setToolTip(tr(
            'The spacing of the labelled grid. Automatic works it out from '
            'what the map is showing, which suits most sheets; name one '
            'instead when the sheet is drawn to a stated scale, such as the '
            '1 km grid of a 1:50,000 map.'))
        self.mgrs_step_combo.currentIndexChanged.connect(
            self._on_letters_changed)
        form.addRow(tr('Grid interval'), self.mgrs_step_combo)

        self.letters_combo = self._vocabulary_combo(
            box, [(key, label, key) for key, label in LG.LETTER_PLACEMENTS])
        self.letters_combo.setToolTip(tr(
            'Where the 100 km square letters go. Inside the map keeps the '
            'margin to the numbers alone, which is how a printed sheet does '
            'it.'))
        self.letters_combo.currentIndexChanged.connect(
            self._on_letters_changed)
        form.addRow(tr('100 km letters'), self.letters_combo)

        self.extras_widget = QWidget(box)
        extras = QHBoxLayout(self.extras_widget)
        extras.setContentsMargins(0, 0, 0, 0)
        self.one_km_check = QCheckBox(tr('1 km mesh'), self.extras_widget)
        self.one_km_check.setToolTip(tr(
            'Adds the 1 km mesh a 1:50,000 sheet carries, under whatever the '
            'automatic interval turned out to be. Lines only - a second set '
            'of numbers would land in the same margin row as the first.'))
        self.one_km_check.toggled.connect(self._on_extras_changed)
        extras.addWidget(self.one_km_check)
        self.interior_check = QCheckBox(tr('Labels inside too'),
                                        self.extras_widget)
        self.interior_check.setToolTip(tr(
            'Draws strips of numbers across the inside of the map, with the '
            'grid line broken behind each one, so a reader working in the '
            'middle of a large sheet does not have to trace a line out to '
            'the margin. Positioning below decides where the strips go.'))
        self.interior_check.toggled.connect(self._on_extras_changed)
        self.interior_check.toggled.connect(self._apply_visibility)
        extras.addWidget(self.interior_check)
        extras.addStretch(1)
        form.addRow(tr('Also add'), self.extras_widget)

        # The strips' positioning. Only meaningful with "Labels inside too",
        # so it is disabled without it; it never touches the margin labels.
        self.positioning_widget = QWidget(box)
        positioning = QHBoxLayout(self.positioning_widget)
        positioning.setContentsMargins(0, 0, 0, 0)
        self.positioning_group = QButtonGroup(self.positioning_widget)
        self.positioning_group.setExclusive(True)
        self.positioning_buttons = {}
        for key, label in IL.POSITION_OPTIONS:
            button = QToolButton(self.positioning_widget)
            button.setCheckable(True)
            button.setAutoRaise(False)
            button.setIcon(_positioning_icon(key))
            button.setIconSize(QSize(32, 32))
            button.setToolTip(tr(label))
            self.positioning_group.addButton(button)
            self.positioning_buttons[key] = button
            positioning.addWidget(button)
        positioning.addStretch(1)
        self.positioning_buttons[IL.DEFAULT_POSITION].setChecked(True)
        self.positioning_group.buttonClicked.connect(self._on_positioning_changed)
        form.addRow(tr('Positioning'), self.positioning_widget)

        self.target_form = form
        return box

    def interior_positioning(self):
        for key, button in self.positioning_buttons.items():
            if button.isChecked():
                return key
        return IL.DEFAULT_POSITION

    def _on_positioning_changed(self, _button=None):
        """Re-lay the strips already on the map."""
        if self._loading:
            return
        map_item = self.current_map()
        if map_item is not None:
            LG.update_interior_position(map_item, self.interior_positioning())

    def _on_extras_changed(self, _checked=None):
        if self._loading or not self._pending_set:
            return
        self._choose_grid_set(self._pending_set)

    def set_options(self):
        """The four MGRS set choices, as `grid_set_specs` wants them."""
        key = self.mgrs_step_combo.currentData()
        step = 0.0
        for entry_key, _label, metres in LG.MGRS_INTERVALS:
            if entry_key == key:
                step = metres
                break
        return {'letters': self.letters_combo.currentData() or 'margin',
                'one_km': self.one_km_check.isChecked(),
                'interior': self.interior_check.isChecked(),
                'positioning': self.interior_positioning(),
                'step': step}

    def _on_letters_changed(self, _index=None):
        if self._loading or not self._pending_set:
            return
        # Rebuild the armed set so the form and the status line follow.
        self._choose_grid_set(self._pending_set)

    # -------------------------------------------------------------- grid tab

    def _build_grid_tab(self):
        page = QWidget(self)
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.style_combo = self._vocabulary_combo(page, LG.GRID_STYLES)
        self.style_combo.currentIndexChanged.connect(self._apply_visibility)
        form.addRow(tr('Grid type'), self.style_combo)

        self.crs_widget = QgsProjectionSelectionWidget(page)
        not_set = _enum('CrsOption', 'CrsNotSet', QgsProjectionSelectionWidget)
        if not_set is not None:
            # "Not set" is how a grid says "use the map's own CRS", which is
            # what all but a lat/long graticule over a projected map wants.
            self.crs_widget.setOptionVisible(not_set, True)
        self.crs_widget.setToolTip(tr(
            'Leave this unset to grid the map in its own coordinate system. '
            'Pick EPSG:4326 to lay a latitude/longitude graticule over a '
            'projected map.'))
        self.crs_widget.crsChanged.connect(self._on_crs_changed)
        form.addRow(tr('Coordinate system'), self.crs_widget)

        self.unit_combo = self._vocabulary_combo(page, LG.GRID_UNITS)
        self.unit_combo.currentIndexChanged.connect(self._apply_visibility)
        form.addRow(tr('Interval units'), self.unit_combo)

        self.interval_widget = QWidget(page)
        interval_row = QHBoxLayout(self.interval_widget)
        interval_row.setContentsMargins(0, 0, 0, 0)
        self.interval_x = self._number(self.interval_widget, maximum=1e9)
        self.interval_y = self._number(self.interval_widget, maximum=1e9)
        interval_row.addWidget(QLabel(tr('X'), self.interval_widget))
        interval_row.addWidget(self.interval_x, 1)
        interval_row.addWidget(QLabel(tr('Y'), self.interval_widget))
        interval_row.addWidget(self.interval_y, 1)
        self.suggest_button = QPushButton(tr('Suggest'), self.interval_widget)
        self.suggest_button.setToolTip(tr(
            'A round interval that cuts the map into about five columns and '
            'rows, worked out from what the map is showing now.'))
        self.suggest_button.clicked.connect(self.suggest_interval)
        # The MGRS digit count is derived from the interval, so the preview
        # has to be told when it moves.
        self.interval_x.valueChanged.connect(self._refresh_expression_preview)
        self.interval_y.valueChanged.connect(self._refresh_expression_preview)
        interval_row.addWidget(self.suggest_button)
        form.addRow(tr('Interval'), self.interval_widget)

        self.offset_widget = QWidget(page)
        offset_row = QHBoxLayout(self.offset_widget)
        offset_row.setContentsMargins(0, 0, 0, 0)
        self.offset_x = self._number(self.offset_widget, minimum=-1e9, maximum=1e9)
        self.offset_y = self._number(self.offset_widget, minimum=-1e9, maximum=1e9)
        offset_row.addWidget(QLabel(tr('X'), self.offset_widget))
        offset_row.addWidget(self.offset_x, 1)
        offset_row.addWidget(QLabel(tr('Y'), self.offset_widget))
        offset_row.addWidget(self.offset_y, 1)
        form.addRow(tr('Offset'), self.offset_widget)

        self.fit_widget = QWidget(page)
        fit_row = QHBoxLayout(self.fit_widget)
        fit_row.setContentsMargins(0, 0, 0, 0)
        self.min_width = self._number(self.fit_widget, maximum=1000.0, step=5.0)
        self.max_width = self._number(self.fit_widget, maximum=1000.0, step=5.0)
        fit_row.addWidget(QLabel(tr('min'), self.fit_widget))
        fit_row.addWidget(self.min_width, 1)
        fit_row.addWidget(QLabel(tr('max'), self.fit_widget))
        fit_row.addWidget(self.max_width, 1)
        fit_row.addWidget(QLabel(tr('mm'), self.fit_widget))
        form.addRow(tr('Spacing on the page'), self.fit_widget)

        self.line_color = self._color_button(page, tr('Grid line colour'))
        form.addRow(tr('Line colour'), self.line_color)

        self.line_width = self._number(page, maximum=20.0, step=0.1, decimals=2)
        self.line_width.setSuffix(tr(' mm'))
        form.addRow(tr('Line width'), self.line_width)

        self.cross_size = self._number(page, maximum=100.0, step=0.5, decimals=2)
        self.cross_size.setSuffix(tr(' mm'))
        form.addRow(tr('Cross size'), self.cross_size)

        self.marker_size = self._number(page, maximum=100.0, step=0.5, decimals=2)
        self.marker_size.setSuffix(tr(' mm'))
        form.addRow(tr('Marker size'), self.marker_size)

        self.grid_form = form
        return page

    # ------------------------------------------------------------- frame tab

    def _build_frame_tab(self):
        page = QWidget(self)
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.frame_combo = self._vocabulary_combo(page, LG.FRAME_STYLES,
                                                  current='exterior')
        self.frame_combo.currentIndexChanged.connect(self._apply_visibility)
        form.addRow(tr('Frame style'), self.frame_combo)

        self.frame_size = self._number(page, maximum=100.0, step=0.5, decimals=2)
        self.frame_size.setSuffix(tr(' mm'))
        self.frame_size.setToolTip(tr(
            'How far the frame or the ticks reach out from the map.'))
        form.addRow(tr('Frame size'), self.frame_size)

        self.frame_line_width = self._number(page, maximum=20.0, step=0.1,
                                             decimals=2)
        self.frame_line_width.setSuffix(tr(' mm'))
        form.addRow(tr('Frame line thickness'), self.frame_line_width)

        self.frame_pen_color = self._color_button(page, tr('Frame line colour'))
        form.addRow(tr('Frame line colour'), self.frame_pen_color)

        self.frame_fill_1 = self._color_button(page, tr('First fill colour'))
        form.addRow(tr('Fill colour 1'), self.frame_fill_1)

        self.frame_fill_2 = self._color_button(page, tr('Second fill colour'))
        form.addRow(tr('Fill colour 2'), self.frame_fill_2)

        self.frame_sides = {}
        sides_widget = QWidget(page)
        sides_row = QHBoxLayout(sides_widget)
        sides_row.setContentsMargins(0, 0, 0, 0)
        for side_key, side_label, _border, _flag in LG.SIDES:
            check = QCheckBox(tr(side_label), sides_widget)
            check.setChecked(True)
            self.frame_sides[side_key] = check
            sides_row.addWidget(check)
        sides_row.addStretch(1)
        form.addRow(tr('Draw on'), sides_widget)
        self.frame_sides_widget = sides_widget

        self.frame_form = form
        return page

    # ------------------------------------------------------------ labels tab

    def _build_labels_tab(self):
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)

        box = QGroupBox(tr('Show coordinate labels'), page)
        box.setCheckable(True)
        box.setChecked(True)
        box.toggled.connect(self._apply_visibility)
        self.annotations_box = box
        column.addWidget(box)

        outer = QVBoxLayout(box)

        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.format_combo = self._vocabulary_combo(box, LG.ANNOTATION_FORMATS)
        self.format_combo.currentIndexChanged.connect(self._apply_visibility)
        form.addRow(tr('Number format'), self.format_combo)

        self.zone_spin = QSpinBox(box)
        self.zone_spin.setRange(0, 60)
        self.zone_spin.setSpecialValueText(tr('from the coordinate system'))
        self.zone_spin.setToolTip(tr(
            'Which UTM zone the 100 km letters belong to. Left at nought it '
            'is read from the grid\'s own coordinate system, which is what '
            'the MGRS preset sets.'))
        self.zone_spin.valueChanged.connect(self._refresh_expression_preview)
        form.addRow(tr('UTM zone'), self.zone_spin)

        self.expression_edit = QLineEdit(box)
        self.expression_edit.setPlaceholderText(
            tr("An expression using @grid_number and @grid_axis"))
        self.expression_edit.setToolTip(tr(
            '@grid_number is the value of the line being labelled and '
            "@grid_axis is 'x' or 'y'."))
        self.expression_edit.textEdited.connect(self._refresh_expression_preview)
        form.addRow(tr('Expression'), self.expression_edit)

        # What will actually be written. It is here so the rule that the MGRS
        # digits follow the interval is visible rather than folklore: change
        # the interval from 1 km to 100 m and the divisor in this line changes
        # with it.
        self.expression_preview = QLabel('', box)
        self.expression_preview.setWordWrap(True)
        self.expression_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.expression_preview.setStyleSheet('color: palette(mid);')
        form.addRow(tr('Label rule'), self.expression_preview)

        self.precision_spin = QSpinBox(box)
        self.precision_spin.setRange(0, 12)
        self.precision_spin.setToolTip(tr(
            'Decimal places. On a degree/minute/second format this is the '
            'decimals on the last part shown.'))
        form.addRow(tr('Decimals'), self.precision_spin)

        self.distance_spin = self._number(box, maximum=100.0, step=0.5,
                                          decimals=2)
        self.distance_spin.setSuffix(tr(' mm'))
        form.addRow(tr('Distance from the frame'), self.distance_spin)

        self.font_button = QgsFontButton(box)
        mode = _enum('Mode', 'ModeQFont', QgsFontButton)
        if mode is not None:
            self.font_button.setMode(mode)
        form.addRow(tr('Font'), self.font_button)

        self.font_color = self._color_button(box, tr('Label colour'))
        form.addRow(tr('Label colour'), self.font_color)

        outer.addLayout(form)
        outer.addWidget(self._build_sides_table(box))
        column.addStretch(1)
        self.labels_form = form
        return page

    def _build_sides_table(self, parent):
        """Four rows - one per side - of what to show, where, and which way up."""
        box = QGroupBox(tr('Each side'), parent)
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)

        for column, heading in enumerate(
                (tr('Side'), tr('Show'), tr('Placement'), tr('Orientation'))):
            label = QLabel('<b>{}</b>'.format(heading), box)
            label.setTextFormat(Qt.TextFormat.RichText)
            grid.addWidget(label, 0, column)

        self.side_widgets = {}
        for row, (side_key, side_label, _border, _flag) in enumerate(LG.SIDES, 1):
            grid.addWidget(QLabel(tr(side_label), box), row, 0)
            display = self._vocabulary_combo(box, LG.ANNOTATION_DISPLAY)
            position = self._vocabulary_combo(box, LG.ANNOTATION_POSITIONS)
            direction = self._vocabulary_combo(box, LG.ANNOTATION_DIRECTIONS)
            grid.addWidget(display, row, 1)
            grid.addWidget(position, row, 2)
            grid.addWidget(direction, row, 3)
            self.side_widgets[side_key] = {
                'display': display,
                'position': position,
                'direction': direction,
            }

        copy_button = QPushButton(tr('Copy the Left row to every side'), box)
        copy_button.clicked.connect(self._copy_first_side)
        grid.addWidget(copy_button, len(LG.SIDES) + 1, 0, 1, 4)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        return box

    # ---------------------------------------------------------------- buttons

    def _build_buttons(self):
        widget = QWidget(self)
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        self.apply_button = QPushButton(tr('Add Grid'), widget)
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.apply_grid)
        row.addWidget(self.apply_button)

        self.remove_button = QPushButton(tr('Remove Grid'), widget)
        self.remove_button.setToolTip(tr('Delete the selected grid from the map.'))
        self.remove_button.clicked.connect(self.remove_grid)
        row.addWidget(self.remove_button)

        self.reset_button = QPushButton(tr('Reset'), widget)
        self.reset_button.setToolTip(tr('Put every setting back to its default.'))
        self.reset_button.clicked.connect(self.reset_form)
        row.addWidget(self.reset_button)

        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
            | QDialogButtonBox.StandardButton.Help, widget)
        buttons.rejected.connect(self.close)
        buttons.helpRequested.connect(lambda: open_docs(ALG_NAME))
        row.addWidget(buttons)
        return widget

    # ------------------------------------------------------ widget shorthands

    def _vocabulary_combo(self, parent, vocabulary, current=None):
        """A combo filled from one of `layout_grid`'s (key, label, value) lists."""
        combo = QComboBox(parent)
        for key, label, _value in vocabulary:
            combo.addItem(tr(label), key)
        if current is not None:
            self._select_key(combo, current)
        return combo

    @staticmethod
    def _select_key(combo, key):
        index = combo.findData(key)
        combo.setCurrentIndex(index if index >= 0 else 0)

    @staticmethod
    def _number(parent, minimum=0.0, maximum=1e6, step=1.0, decimals=6):
        spin = QDoubleSpinBox(parent)
        spin.setDecimals(decimals)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        # Degrees need six decimals and metres need none; a fixed width keeps
        # the two from resizing the form as the units combo changes.
        spin.setMinimumWidth(110)
        return spin

    @staticmethod
    def _color_button(parent, title):
        button = QgsColorButton(parent)
        button.setAllowOpacity(True)
        button.setColorDialogTitle(title)
        button.setMinimumWidth(110)
        return button

    def _set_row_visible(self, form, widget, visible):
        """Show or hide a form row, its label included."""
        label = form.labelForField(widget)
        widget.setVisible(visible)
        if label is not None:
            label.setVisible(visible)

    # ------------------------------------------------------------- the target

    def target_window(self):
        """The window this one should sit in front of.

        The designer showing the layout being gridded, or the QGIS main window
        when that layout is not open in one.
        """
        window = LG.designer_window(iface, self.current_layout())
        if window is not None:
            return window
        return iface.mainWindow() if iface is not None else None

    def follow_target_window(self):
        """Re-parent onto the window holding the layout being gridded.

        The tool is reached from the main window's toolbar, but the sheet it
        works on is in the layout designer - a top-level window of its own, not
        a panel. A dialog parented to the main window therefore disappears
        behind the designer the moment the designer comes back to the front,
        which is exactly when the form is wanted. Parenting onto the designer
        instead is what keeps the form and the sheet on screen together.

        Closing that designer destroys this window with it; the algorithm
        notices the dead wrapper on the next run and builds a fresh one.
        """
        window = self.target_window()
        if window is self.parent():
            return

        visible = self.isVisible()
        geometry = self.geometry()
        # setParent() on a top-level widget hides it and drops its window
        # flags, so both are handed back.
        self.setParent(window, self.windowFlags())
        if visible:
            self.setGeometry(geometry)
            self.show()

    def schedule_follow(self):
        """Re-parent once the signal being handled has been delivered.

        `follow_target_window` hides and re-shows this window, which is not
        something to do in the middle of delivering a signal from one of its
        own child widgets. Everything driven by a widget goes through here;
        only the algorithm, which is outside the widget's event delivery,
        calls `follow_target_window` directly.
        """
        QTimer.singleShot(0, self._deferred_follow)

    def _deferred_follow(self):
        try:
            self.follow_target_window()
        except RuntimeError:                # went with its designer meanwhile
            pass

    def _on_reload_clicked(self):
        self.reload_targets()
        # The user may have pressed this because they moved to another
        # designer, so follow them there.
        self.schedule_follow()

    def _on_open_clicked(self):
        layout = self.current_layout()
        if layout is None:
            self.set_status(tr('Pick a layout to open first.'))
            return
        try:
            designer = LG.open_designer(iface, layout)
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not open the layout: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Warning)
            designer = None
        if designer is None:
            self.set_status(tr('Could not open the layout "{}".').format(
                layout.name()))
            return
        # Sit in front of the designer just opened, so the form is not lost
        # behind it.
        self.schedule_follow()
        self.set_status(tr('Opened "{}" in the layout designer.').format(
            layout.name()))

    def reload_targets(self):
        """Fill the layout combo, preferring whatever is selected in a designer."""
        wanted_layout, wanted_map = LG.active_selection(iface)

        self._loading = True
        self.layout_combo.clear()
        self._layouts = [layout for layout in LG.print_layouts()
                         if _alive(layout)]
        for layout in self._layouts:
            self.layout_combo.addItem(layout.name() or tr('Layout'))
        self._loading = False

        if wanted_layout is not None:
            index = _index_in(self._layouts, wanted_layout)
            if index >= 0:
                self.layout_combo.setCurrentIndex(index)
        self._on_layout_changed(preferred_map=wanted_map)

    @staticmethod
    def _at(items, index):
        """`items[index]`, or None when the index or the object is no good."""
        if index < 0 or index >= len(items):
            return None
        item = items[index]
        return item if _alive(item) else None

    def current_layout(self):
        return self._at(self._layouts, self.layout_combo.currentIndex())

    def current_map(self):
        return self._at(self._maps, self.map_combo.currentIndex())

    def current_grid(self):
        """The selected existing grid, or None when a new one is to be added."""
        map_item = self.current_map()
        grid_id = self.grid_combo.currentData()
        if map_item is None or not grid_id or grid_id == NEW_GRID:
            return None
        grid = map_item.grids().grid(grid_id)
        return grid if grid is not None else None

    def _on_layout_changed(self, _index=None, preferred_map=None):
        if self._loading:
            return
        layout = self.current_layout()

        self._loading = True
        self.map_combo.clear()
        self._maps = LG.map_items(layout)
        for map_item in self._maps:
            self.map_combo.addItem(map_item.displayName())
        self._loading = False

        if preferred_map is None:
            preferred_map = LG.selected_map(layout)
        if preferred_map is not None:
            index = _index_in(self._maps, preferred_map)
            if index >= 0:
                self.map_combo.setCurrentIndex(index)
        self._on_map_changed()
        # The chosen layout may live in a different designer window than the
        # one this dialog is currently sitting in front of.
        self.schedule_follow()

    def _on_map_changed(self, _index=None, preferred_grid=None):
        if self._loading:
            return
        map_item = self.current_map()

        self._loading = True
        self.grid_combo.clear()
        self.grid_combo.addItem(tr('<Add a new grid>'), NEW_GRID)
        if map_item is not None:
            for grid in map_item.grids().asList():
                self.grid_combo.addItem(
                    tr('Edit: {}').format(grid.name() or tr('Grid')), grid.id())
        self._loading = False

        index = self.grid_combo.findData(preferred_grid) if preferred_grid else -1
        self.grid_combo.setCurrentIndex(index if index >= 0 else 0)
        self._on_grid_changed()

    def _on_grid_changed(self, _index=None):
        if self._loading:
            return
        # Whatever set was armed was armed for a target the user has now moved
        # away from, so it must not survive the move.
        self._pending_set = None
        self._set_warnings = []
        grid = self.current_grid()
        if grid is not None:
            self.load_spec(LG.read_spec(grid))
        else:
            self.load_spec(LG.GridSpec())
        self._update_state()

    def _update_state(self):
        """Button labels, enabled states and the status line, for the target."""
        map_item = self.current_map()
        grid = self.current_grid()
        editing = grid is not None

        armed = bool(self._pending_set) and map_item is not None
        if armed:
            count = len(LG.grid_set_specs(self._pending_set, map_item,
                                          self.read_form(),
                                          **self.set_options())[0])
            self.apply_button.setText(
                tr('Add MGRS Grid ({} grids)').format(count)
                if self._pending_set == 'mgrs' else tr('Add Grid Set'))
        else:
            self.apply_button.setText(tr('Apply') if editing
                                      else tr('Add Grid'))
        self.apply_button.setEnabled(map_item is not None)
        self.remove_button.setEnabled(editing and not armed)

        if map_item is None:
            if not LG.print_layouts():
                self.set_status(tr(
                    'This project has no print layout yet. Make one with '
                    'Project > New Print Layout, put a map on it, then press '
                    'Reload.'))
            else:
                self.set_status(tr(
                    'That layout has no map item. Add one in the layout '
                    'designer, then press Reload.'))
            return

        layout = self.current_layout()
        where = '{} - {}'.format(layout.name() if layout is not None else '?',
                                 map_item.displayName())
        if armed:
            specs, _warnings = LG.grid_set_specs(
                self._pending_set, map_item, self.read_form(),
                **self.set_options())
            names = ', '.join(spec.name for spec in specs)
            message = tr('Add puts {} grids on {}: {}. The form is showing '
                         'the last of them; the others become grids of their '
                         'own once they are added.').format(
                len(specs), where, names)
            self.set_status(' '.join([message] + self._set_warnings))
        elif editing:
            self.set_status(tr('Editing "{}" on {}.').format(grid.name(), where))
        else:
            self.set_status(tr('A new grid will be added to {}.').format(where))

    def set_status(self, text):
        self.status.setText(text)

    # -------------------------------------------------------- form <-> spec

    def load_spec(self, spec):
        """Fill every widget from `spec`."""
        self._loading = True
        try:
            self.name_edit.setText(spec.name)
            self._select_key(self.style_combo, spec.style)
            self.crs_widget.setCrs(spec.crs())
            self._select_key(self.unit_combo, spec.unit)
            self.interval_x.setValue(float(spec.interval_x))
            self.interval_y.setValue(float(spec.interval_y))
            self.offset_x.setValue(float(spec.offset_x))
            self.offset_y.setValue(float(spec.offset_y))
            self.min_width.setValue(float(spec.min_width))
            self.max_width.setValue(float(spec.max_width))
            self.line_color.setColor(LG.color_from_text(spec.line_color))
            self.line_width.setValue(float(spec.line_width))
            self.cross_size.setValue(float(spec.cross_size))
            self.marker_size.setValue(float(spec.marker_size))

            self._select_key(self.frame_combo, spec.frame)
            self.frame_size.setValue(float(spec.frame_size))
            self.frame_line_width.setValue(float(spec.frame_line_width))
            self.frame_pen_color.setColor(LG.color_from_text(spec.frame_pen_color))
            self.frame_fill_1.setColor(LG.color_from_text(spec.frame_fill_1))
            self.frame_fill_2.setColor(LG.color_from_text(spec.frame_fill_2))

            self.annotations_box.setChecked(bool(spec.annotations))
            self._select_key(self.format_combo, spec.annotation_format)
            self.zone_spin.setValue(int(spec.mgrs_zone))
            # No widgets: set by the MGRS set, carried through so an edit of
            # its grids does not give back the corner slot, or turn an
            # interior strip back into an ordinary labelled grid.
            self._skip_corner = bool(spec.skip_corner)
            self._interior_role = spec.interior_role
            self._halo = bool(spec.halo)
            self.expression_edit.setText(spec.annotation_expression)
            self.precision_spin.setValue(int(spec.annotation_precision))
            self.distance_spin.setValue(float(spec.annotation_distance))
            self.font_button.setCurrentFont(spec.label_font())
            self.font_color.setColor(LG.color_from_text(spec.font_color))

            for side_key, row in spec.sides.items():
                widgets = self.side_widgets.get(side_key)
                if widgets is None:
                    continue
                self._select_key(widgets['display'], row['display'])
                self._select_key(widgets['position'], row['position'])
                self._select_key(widgets['direction'], row['direction'])
                check = self.frame_sides.get(side_key)
                if check is not None:
                    check.setChecked(bool(row['frame_enabled']))
        finally:
            self._loading = False
        self._apply_visibility()

    def read_form(self):
        """Every widget's value as a `GridSpec`."""
        font = self.font_button.currentFont() or QFont()
        size = font.pointSizeF()
        crs = self.crs_widget.crs()

        spec = LG.GridSpec(
            name=self.name_edit.text().strip() or tr('Grid'),
            style=self.style_combo.currentData(),
            crs_authid=crs.authid() if crs.isValid() else '',
            unit=self.unit_combo.currentData(),
            interval_x=self.interval_x.value(),
            interval_y=self.interval_y.value(),
            offset_x=self.offset_x.value(),
            offset_y=self.offset_y.value(),
            min_width=self.min_width.value(),
            max_width=self.max_width.value(),
            line_color=LG.color_to_text(self.line_color.color()),
            line_width=self.line_width.value(),
            cross_size=self.cross_size.value(),
            marker_size=self.marker_size.value(),
            frame=self.frame_combo.currentData(),
            frame_size=self.frame_size.value(),
            frame_line_width=self.frame_line_width.value(),
            frame_pen_color=LG.color_to_text(self.frame_pen_color.color()),
            frame_fill_1=LG.color_to_text(self.frame_fill_1.color()),
            frame_fill_2=LG.color_to_text(self.frame_fill_2.color()),
            annotations=self.annotations_box.isChecked(),
            annotation_format=self.format_combo.currentData(),
            annotation_expression=self.expression_edit.text().strip(),
            mgrs_zone=self.zone_spin.value(),
            skip_corner=getattr(self, '_skip_corner', False),
            interior_role=getattr(self, '_interior_role', ''),
            halo=getattr(self, '_halo', False),
            annotation_precision=self.precision_spin.value(),
            annotation_distance=self.distance_spin.value(),
            # The button's font carries the size; keep the two in step so the
            # spec can be applied without the button being present.
            font=LG.font_to_text(font),
            font_size=size if size > 0 else 9.0,
            font_color=LG.color_to_text(self.font_color.color()),
        )
        for side_key, widgets in self.side_widgets.items():
            check = self.frame_sides.get(side_key)
            spec.sides[side_key] = {
                'display': widgets['display'].currentData(),
                'position': widgets['position'].currentData(),
                'direction': widgets['direction'].currentData(),
                'frame_enabled': check.isChecked() if check is not None else True,
            }
        return spec

    # -------------------------------------------------------- form behaviour

    def _apply_visibility(self, *_args):
        """Show only the rows the current grid type, unit and frame style use."""
        style = self.style_combo.currentData()
        unit = self.unit_combo.currentData()
        frame = self.frame_combo.currentData()

        fitted = unit == 'fit'
        self._set_row_visible(self.grid_form, self.interval_widget, not fitted)
        self._set_row_visible(self.grid_form, self.fit_widget, fitted)

        drawn = style in ('solid', 'cross', 'markers')
        self._set_row_visible(self.grid_form, self.line_color, drawn)
        self._set_row_visible(self.grid_form, self.line_width,
                              style in ('solid', 'cross'))
        self._set_row_visible(self.grid_form, self.cross_size, style == 'cross')
        self._set_row_visible(self.grid_form, self.marker_size, style == 'markers')

        framed = frame != 'none'
        zebra = frame in ('zebra', 'zebra_nautical')
        for widget in (self.frame_size, self.frame_line_width,
                       self.frame_pen_color, self.frame_sides_widget):
            self._set_row_visible(self.frame_form, widget, framed)
        self._set_row_visible(self.frame_form, self.frame_fill_1, zebra)
        self._set_row_visible(self.frame_form, self.frame_fill_2, zebra)

        armed_set = bool(self._pending_set)
        self._set_row_visible(self.target_form, self.mgrs_step_combo, armed_set)
        self._set_row_visible(self.target_form, self.letters_combo, armed_set)
        self._set_row_visible(self.target_form, self.extras_widget, armed_set)
        map_item = self.current_map() if hasattr(self, 'map_combo') else None
        has_strips = map_item is not None and any(
            grid.customProperty(LG.INTERIOR_PROPERTY, '')
            for grid in map_item.grids().asList())
        self._set_row_visible(self.target_form, self.positioning_widget,
                              armed_set or has_strips)
        self.positioning_widget.setEnabled(
            has_strips or self.interior_check.isChecked())

        fmt = self.format_combo.currentData()
        by_expression = fmt in LG.EXPRESSION_FORMATS
        # QGIS does not consult annotationPrecision under a custom format, so
        # leaving Decimals on screen would be a control that silently does
        # nothing.
        self._set_row_visible(self.labels_form, self.precision_spin,
                              not by_expression)
        self._set_row_visible(self.labels_form, self.zone_spin,
                              fmt == 'mgrs_square')
        self._set_row_visible(self.labels_form, self.expression_edit,
                              fmt == 'custom')
        self._set_row_visible(self.labels_form, self.expression_preview,
                              by_expression)
        self._refresh_expression_preview()

    def _refresh_expression_preview(self, *_args):
        """Show the expression the current form would actually write."""
        if self._loading or not hasattr(self, 'expression_preview'):
            return
        if self.format_combo.currentData() not in LG.EXPRESSION_FORMATS:
            self.expression_preview.setText('')
            return
        try:
            text = LG.annotation_expression(self.read_form(),
                                            self.current_map())
        except Exception:                   # pragma: no cover - defensive
            text = ''
        self.expression_preview.setText(text or tr(
            'Nothing - this format needs a UTM zone, and there is none to '
            'read from the coordinate system. The labels would fall back to '
            'plain numbers.'))

    def _on_crs_changed(self, *_args):
        """Re-suggest the interval when the grid moves to another CRS.

        Switching a metre grid to EPSG:4326 leaves an interval of 1000, which
        is 1000 degrees and draws nothing at all - the one wrong number that
        looks like the tool is broken. Only offered while the form is not being
        loaded from an existing grid, whose interval is the user's own.
        """
        if self._loading:
            return
        self.suggest_interval(quiet=True)

    def _on_preset_chosen(self, _index=None):
        if self._loading:
            return
        key = self.preset_combo.currentData()
        if not key:
            return
        if key.startswith(SET_PREFIX):
            self._choose_grid_set(key[len(SET_PREFIX):])
        else:
            self.load_spec(LG.preset_spec(key, self.read_form()))
            self.suggest_interval(quiet=True)
        self._loading = True
        self.preset_combo.setCurrentIndex(0)
        self._loading = False

    def _choose_grid_set(self, key):
        """Arm a set of grids, and show the finest one in the form.

        The form cannot display three grids, so it shows the one the user is
        most likely to go on tuning - the fine numeric grid - while the other
        two ride along until Apply. They become ordinary grids in the Grid
        combo the moment they are written.
        """
        map_item = self.current_map()
        specs, warnings = LG.grid_set_specs(key, map_item, LG.GridSpec(),
                                            **self.set_options())
        if not specs:
            self._pending_set = None
            self._set_warnings = []
            self.set_status(' '.join(warnings))
            return

        # load_spec leaves the grid combo alone, so arming afterwards is safe;
        # arming first would be undone by _on_grid_changed.
        self.load_spec(specs[-1])
        # Not suggest_interval: the interval is already snapped to an MGRS
        # step, and re-suggesting would unsnap it.
        self._pending_set = key
        self._set_warnings = list(warnings)
        # After arming, not before: `load_spec` runs `_apply_visibility` while
        # the set is still disarmed, so the placement row would stay hidden.
        self._apply_visibility()
        self._update_state()

    def _copy_first_side(self):
        """Give every side what the Left row has: the common case in one press."""
        first = self.side_widgets.get(LG.SIDE_KEYS[0])
        if first is None:
            return
        for side_key, widgets in self.side_widgets.items():
            if side_key == LG.SIDE_KEYS[0]:
                continue
            for field, combo in widgets.items():
                self._select_key(combo, first[field].currentData())

    def suggest_interval(self, _checked=False, quiet=False):
        """Fill the interval boxes from what the map is showing."""
        map_item = self.current_map()
        if map_item is None:
            if not quiet:
                self.set_status(tr('Pick a map item first.'))
            return
        crs = self.crs_widget.crs()
        x, y = LG.suggest_interval(map_item,
                                   crs.authid() if crs.isValid() else '')
        if x <= 0 or y <= 0:
            if not quiet:
                self.set_status(tr(
                    'The map has no extent to work an interval out from yet.'))
            return
        self.interval_x.setValue(x)
        self.interval_y.setValue(y)
        if not quiet:
            self.set_status(tr('Interval set to {} by {}.').format(x, y))

    def reset_form(self):
        self.load_spec(LG.GridSpec())
        self.suggest_interval(quiet=True)

    # ------------------------------------------------------------- the action

    def apply_grid(self):
        map_item = self.current_map()
        if map_item is None:
            self._update_state()
            return

        if self._pending_set:
            return self._apply_grid_set()

        spec = self.read_form()
        if spec.unit != 'fit' and (spec.interval_x <= 0 or spec.interval_y <= 0):
            QMessageBox.warning(
                self, tr('KGA Add Grid'),
                tr('The interval has to be greater than zero, or the grid '
                   'draws nothing. Press Suggest for a round one that suits '
                   'the map.'))
            return

        if spec.unit == 'fit' and spec.annotation_format == 'mgrs_digits':
            # The digit count is worked out from the interval when the grid is
            # written. "Fit the page" lets QGIS recompute that interval at
            # every render, so the labels would stop matching the lines the
            # second time the layout was drawn.
            QMessageBox.warning(
                self, tr('KGA Add Grid'),
                tr('MGRS principal digits need a fixed interval. "Fit the '
                   'page automatically" changes the interval at every render, '
                   'so the digits would stop matching the lines. Pick map '
                   'units, or a different number format.'))
            return

        try:
            grid = self.current_grid()
            if grid is None:
                grid = LG.add_grid(map_item, spec)
                message = tr('Added "{}" to {}.')
            else:
                LG.update_grid(map_item, grid, spec)
                message = tr('Updated "{}" on {}.')
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not apply the grid: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Critical)
            QMessageBox.critical(
                self, tr('KGA Add Grid'),
                tr('Could not apply the grid:\n{}').format(exc))
            return

        # Re-selecting the grid that was just written means the next press of
        # Apply changes it rather than adding a second one beside it.
        self._on_map_changed(preferred_grid=grid.id())
        status = message.format(grid.name(), map_item.displayName())
        if (spec.annotation_format in ('mgrs_square', 'mgrs_digits')
                and not LG.zone_for_spec(spec, map_item)):
            status += tr(
                ' The labels fell back to plain numbers: MGRS needs a UTM '
                'zone, and neither the grid nor the map is in one. Set the '
                'coordinate system to a UTM zone, or give the zone below the '
                'number format.')
        self.set_status(status)

    def _apply_grid_set(self):
        """Write the armed grid set: several grids, one undo step."""
        map_item = self.current_map()
        if map_item is None:
            self._update_state()
            return

        specs, warnings = LG.grid_set_specs(
            self._pending_set, map_item, LG.GridSpec(), **self.set_options())
        if not specs:
            self.set_status(' '.join(warnings))
            return

        try:
            grids = LG.add_grids(map_item, specs, label=tr('Add MGRS grid'))
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not apply the grid set: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Critical)
            QMessageBox.critical(
                self, tr('KGA Add Grid'),
                tr('Could not apply the grid set:\n{}').format(exc))
            return

        for warning in warnings:
            QgsMessageLog.logMessage(warning, LOG_TAG,
                                     Qgis.MessageLevel.Warning)

        self._pending_set = None
        self._set_warnings = []
        # The finest grid is the one worth tuning, so leave the tool on it.
        self._on_map_changed(preferred_grid=grids[-1].id())
        added = ', '.join(grid.name() for grid in grids)
        self.set_status(' '.join([tr(
            'Added {} grids to {}: {}. Now editing "{}" - pick the others '
            'from the Grid list to change them.').format(
                len(grids), map_item.displayName(), added, grids[-1].name())]
            + warnings))

    def remove_grid(self):
        map_item = self.current_map()
        grid = self.current_grid()
        if map_item is None or grid is None:
            return
        name = grid.name()
        answer = QMessageBox.question(
            self, tr('KGA Add Grid'),
            tr('Remove the grid "{}" from {}?').format(
                name, map_item.displayName()))
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            LG.remove_grid(map_item, grid)
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Could not remove the grid: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Critical)
            QMessageBox.critical(
                self, tr('KGA Add Grid'),
                tr('Could not remove the grid:\n{}').format(exc))
            return
        self._on_map_changed()
        self.set_status(tr('Removed "{}".').format(name))


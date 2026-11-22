# -*- coding: utf-8 -*-
"""Settings > Bubbles: one place for every bubble the plugin puts on the map.

The Tool Bag, Symbol Bag, Layer Colors, Catalog, Data Explorer, attribute tables and the panels with a
Minimize button all draw the same bubble and share these settings (`bag_group.read / apply_settings`):
whether panels minimize to a bubble, where the column sits, how big the bubbles are, the space between
them, their colour, the animation and its speed, the sound, and which panels get a bubble at all.

`BubbleControls` builds the group boxes; the Settings window shows them and calls `load`, `apply` and
`defaults`. A small preview shows the column the way the map will.
"""

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QLinearGradient, QPainter, QPen
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                                 QHeaderView, QLabel, QPushButton, QSlider, QTableWidget, QTableWidgetItem,
                                 QToolButton, QVBoxLayout, QWidget)

from ..core import kga_settings as ks
from . import bag_group as bg
from .bag_common import MOTION_NAMES, POSITION_NAMES, SIZES, _swatch
from .symbol_bag.ui.sound import STYLES as SOUND_STYLES
from .symbol_bag.ui.theme import BAG_STYLES, STYLE_ORDER
from .tool_bag.themes import theme


def _note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet('color: palette(dark);')
    return label


class _Preview(QWidget):
    """The map in miniature with the bubbles where the settings put them."""

    SCALE = 0.42

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 150)
        self.values = {}

    def set_values(self, **values):
        self.values.update(values)
        self.update()

    def paintEvent(self, _event):
        v = self.values
        if not v:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(QColor('#b9c4d3'), 1))
        p.setBrush(QColor('#eef3f8'))
        p.drawRoundedRect(area, 4, 4)
        t = theme('classic' if v['style'] == 'white' else v['style'])
        k = self.SCALE
        d, gap, edge = v['size'] * k, v['gap'] * k, v['edge'] * k
        count = 3                                   # a Tool Bag, a Catalog and a Data Explorer, say
        h, vert = bg.POSITIONS[v['position']]
        height = count * d + (count - 1) * gap
        left = (area.left() + edge, area.center().x() - d / 2, area.right() - edge - d)[h]
        top = (area.top() + edge, area.center().y() - height / 2, area.bottom() - edge - height)[vert]
        for i in range(count):
            c = QPointF(left + d / 2, top + d / 2 + i * (d + gap))
            r = d / 2
            shade = QLinearGradient(c.x(), c.y() - r, c.x(), c.y() + r)
            shade.setColorAt(0, QColor(t['top']))
            shade.setColorAt(1, QColor(t['bottom']))
            p.setPen(QPen(QColor(255, 255, 255, 170), 1.2))
            p.setBrush(shade)
            p.setOpacity(1.0 if v['enabled'] else 0.25)
            p.drawEllipse(c, r, r)
        p.end()


BAG_BUBBLES = (('tool', 'Tool Bag'), ('symbol', 'Symbol Bag'), ('colors', 'Layer Colors'))


class BubbleControls:
    """The widgets of the Bubbles page (group boxes in `boxes`)."""

    def __init__(self):
        self.preview = _Preview()

        # ---- bubbles on / off + preview
        self.enabled = QCheckBox('Minimize panels and bags to a bubble on the map')
        main = QGroupBox('Bubbles')
        lay = QVBoxLayout(main)
        lay.addWidget(self.enabled)
        lay.addWidget(_note(
            'When on, the Tool Bag, Symbol Bag, Layer Colors, Catalog, Data Explorer, attribute tables and '
            'the KGA panels can fold into a round bubble on the map and open again with a click. When off, '
            'minimizing simply closes the panel.'))
        lay.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)

        # ---- position and size
        self.position_buttons = {}
        grid = QGridLayout()
        grid.setSpacing(3)
        pad = QWidget()
        pad.setLayout(grid)
        for key, (h, v) in bg.POSITIONS.items():
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFixedSize(26, 18)
            b.setToolTip(POSITION_NAMES[key])
            b.clicked.connect(lambda _c=False, k=key: self._changed(position=k))
            grid.addWidget(b, v, h)
            self.position_buttons[key] = b
        self.size = self._slider(*bg.LIMITS['size'], ' px')
        self.gap = self._slider(*bg.LIMITS['gap'], ' px')
        self.edge = self._slider(*bg.LIMITS['edge'], ' px')
        place = QGroupBox('Position and size')
        form = QFormLayout(place)
        form.addRow('Position on the map', pad)
        form.addRow('Size', self.size[0])
        form.addRow('Space between bubbles', self.gap[0])
        form.addRow('Distance from the map edge', self.edge[0])
        form.addRow(_note('The bubbles stack in one column at this place. Every bag and panel uses it, so '
                          'changing it here moves them all.'))
        self.place_box = place

        # ---- colour
        self.style = QComboBox()
        for key in STYLE_ORDER:
            self.style.addItem(_swatch(key), BAG_STYLES[key]['label'], key)
        self.style.currentIndexChanged.connect(lambda _i: self._changed())
        colour = QGroupBox('Colour')
        cform = QFormLayout(colour)
        cform.addRow('Bubble colour', self.style)
        cform.addRow(_note('Also recolours the Tool Bag, Symbol Bag and Layer Colors so a bag and its '
                           'bubble always match. Each bag can still be recoloured from its own menu.'))

        # ---- animation
        self.animation = QComboBox()
        self.animation.addItem('Genie: the window pours into its bubble and flows back out', ks.BAG_GENIE)
        self.animation.addItem('Classic: the window shrinks into its bubble and grows back', ks.BAG_CLASSIC)
        self.motion = QComboBox()
        for key in bg.MOTIONS:
            self.motion.addItem(MOTION_NAMES[key], key)
        self.window_reveal = QCheckBox('Open tool windows and panels out of their toolbar button')
        anim = QGroupBox('Animation')
        aform = QFormLayout(anim)
        aform.addRow('Open / minimize animation', self.animation)
        aform.addRow('Speed', self.motion)
        aform.addRow(self.window_reveal)
        aform.addRow(_note(
            'A tool window or panel grows out of the KGA toolbar button you clicked and shrinks back into '
            'it when closed. It uses the animation and speed above; with speed Off, or when started from a '
            'menu or shortcut, it just appears.'))

        # ---- sound
        self.sound = QCheckBox('Play a sound when a window folds into a bubble or opens')
        self.sound_style = QComboBox()
        for key, label, desc in SOUND_STYLES:
            self.sound_style.addItem('{}  -  {}'.format(label, desc), key)
        self.volume = self._slider(0, 100, ' %')
        test = QPushButton('Play')
        test.clicked.connect(self._play)
        vrow = QHBoxLayout()
        vrow.addWidget(self.volume[0], 1)
        vrow.addWidget(test)
        sound = QGroupBox('Sound effects')
        sform = QFormLayout(sound)
        sform.addRow(self.sound)
        sform.addRow('Style', self.sound_style)
        sform.addRow('Volume', vrow)
        self.sound_box = sound

        # ---- each bubble on its own
        self.rows = [(k, n, False) for k, n in BAG_BUBBLES] + [(k, n, True) for k, n in bg.PANEL_BUBBLES]
        self.table = QTableWidget(len(self.rows), 4)
        self.table.setHorizontalHeaderLabels(['Bubble', 'Colour', 'Position', 'Size'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(34 * (len(self.rows) + 1))
        self.combos = {}                            # (key, 'style' | 'position' | 'size') -> QComboBox
        self._bag_styles = {}                       # bag key -> its colour when the page was loaded
        for row, (key, label, is_panel) in enumerate(self.rows):
            item = QTableWidgetItem(label)
            if is_panel:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(row, 0, item)
            for col, what in ((1, 'style'), (2, 'position'), (3, 'size')):
                combo = QComboBox()
                combo.currentIndexChanged.connect(lambda _i: self._changed())
                self.table.setCellWidget(row, col, combo)
                self.combos[(key, what)] = combo
        which = QGroupBox('Each bubble on its own')
        wlay = QVBoxLayout(which)
        wlay.addWidget(self.table)
        wlay.addWidget(_note(
            'Every bubble follows the settings above unless you give it its own colour, position or size '
            '(or right-click the bubble on the map for the same choices). Bubbles that share a position '
            'stack in one column; a bubble with a position of its own starts another. Untick a panel to '
            'take its Minimize button and bubble away; the Tool Bag, Symbol Bag and Layer Colors follow '
            'the main switch and keep their own colour.'))
        self.which_box = which

        self.boxes = [main, place, colour, anim, sound, which]
        self.enabled.toggled.connect(self._enable_changed)
        self.sound.toggled.connect(lambda on: self.sound_style.setEnabled(on))
        self._position = bg.DEFAULT_POSITION

    # ------------------------------------------------------------ helpers
    def _slider(self, lo, hi, unit):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        label = QLabel()
        label.setMinimumWidth(46)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(slider, 1)
        lay.addWidget(label)

        def moved(value):
            label.setText('{}{}'.format(value, unit))
            self._changed()
        slider.valueChanged.connect(moved)
        return row, slider, label

    def _enable_changed(self, on):
        for box in (self.place_box, self.which_box, self.sound_box):
            box.setEnabled(on)
        self._changed()

    def _changed(self, position=None):
        if position is not None:
            self._position = position
        self.preview.set_values(style=self.style.currentData() or bg.COMMON_DEFAULTS['style'],
                                size=self.size[1].value(), gap=self.gap[1].value(),
                                edge=self.edge[1].value(), position=self._position,
                                enabled=self.enabled.isChecked())

    def _play(self):
        from .symbol_bag.ui import sound
        sound.play_event('in', self.sound_style.currentData(), self.volume[1].value() / 100.0)

    # ------------------------------------------------------------ load / apply
    def _show(self, values, animation, motion, window_reveal, reset=False):
        self.enabled.setChecked(values['bubble'])
        self.size[1].setValue(values['size'])
        self.gap[1].setValue(values['gap'])
        self.edge[1].setValue(values['edge'])
        self._position = values['position']
        for key, button in self.position_buttons.items():
            button.setChecked(key == self._position)
        self.style.setCurrentIndex(max(self.style.findData(values['style']), 0))
        self.animation.setCurrentIndex(max(self.animation.findData(animation), 0))
        self.motion.setCurrentIndex(max(self.motion.findData(motion), 0))
        self.window_reveal.setChecked(window_reveal)
        self.sound.setChecked(values['sound'])
        self.sound_style.setEnabled(values['sound'])
        self.sound_style.setCurrentIndex(max(self.sound_style.findData(values['sound_style']), 0))
        self.volume[1].setValue(values['sound_volume'])
        off = {k for k in str(values['disabled']).split(',') if k}
        self._fill_table(off, reset)
        self._enable_changed(values['bubble'])

    def load(self):
        """Show what is saved."""
        self._show({k: bg.read(k) for k in bg.COMMON_DEFAULTS}, ks.bag_animation(), bg.read('motion'),
                   ks.window_reveal())

    def defaults(self):
        """Show the defaults (nothing is saved until OK)."""
        self._show(dict(bg.COMMON_DEFAULTS), ks.DEFAULTS[ks.KEY_BAG_ANIMATION], bg.COMMON_DEFAULTS['motion'],
                   ks.DEFAULTS[ks.KEY_WINDOW_REVEAL], reset=True)

    def _fill_table(self, off, reset):
        group = bg.BubbleGroup.instance
        sizes = [px for _l, px in SIZES]
        for row, (key, _label, is_panel) in enumerate(self.rows):
            if is_panel:
                self.table.item(row, 0).setCheckState(
                    Qt.CheckState.Unchecked if key in off else Qt.CheckState.Checked)
            own = {what: None if reset else bg.override(key, what) for what in bg.OVERRIDES}
            # colour: a panel bubble can follow the shared colour; a bag keeps its own
            combo = self.combos[(key, 'style')]
            combo.blockSignals(True)
            combo.clear()
            if is_panel:
                combo.addItem('Same as others', None)
            for k in STYLE_ORDER:
                combo.addItem(_swatch(k), BAG_STYLES[k]['label'], k)
            if is_panel:
                combo.setCurrentIndex(max(combo.findData(own['style']), 0))
            else:
                setter = group.style_setters.get(key) if group is not None else None
                current = setter[0]() if setter else None
                self._bag_styles[key] = current
                combo.setCurrentIndex(max(combo.findData(current), 0))
                combo.setEnabled(setter is not None)
            combo.blockSignals(False)
            combo = self.combos[(key, 'position')]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('Same as others', None)
            for k in bg.POSITIONS:
                combo.addItem(POSITION_NAMES[k], k)
            combo.setCurrentIndex(max(combo.findData(own['position']), 0))
            combo.blockSignals(False)
            combo = self.combos[(key, 'size')]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('Same as others', None)
            for px in sorted(set(sizes + ([own['size']] if own['size'] else []))):
                combo.addItem('{} px'.format(px), px)
            combo.setCurrentIndex(max(combo.findData(own['size']), 0))
            combo.blockSignals(False)

    def apply(self):
        """Save the choices and tell every bubble. (The animation and the toolbar reveal are KGA
        settings: the Settings window saves them with the rest.)"""
        off = [key for row, (key, _label, is_panel) in enumerate(self.rows)
               if is_panel and self.table.item(row, 0).checkState() != Qt.CheckState.Checked]
        group = bg.BubbleGroup.instance
        for key, _label, is_panel in self.rows:
            for what in bg.OVERRIDES:
                value = self.combos[(key, what)].currentData()
                if what == 'style' and not is_panel:           # a bag's colour is the bag's own
                    setter = group.style_setters.get(key) if group is not None else None
                    if setter and value != self._bag_styles.get(key):
                        setter[1](value)
                    continue
                bg.set_override(key, what, value)
        return bg.apply_settings({
            'bubble': self.enabled.isChecked(), 'size': self.size[1].value(), 'gap': self.gap[1].value(),
            'edge': self.edge[1].value(), 'position': self._position,
            'style': self.style.currentData(), 'motion': self.motion.currentData(),
            'sound': self.sound.isChecked(), 'sound_style': self.sound_style.currentData(),
            'sound_volume': self.volume[1].value(), 'disabled': ','.join(off)})

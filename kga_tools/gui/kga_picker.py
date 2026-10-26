# -*- coding: utf-8 -*-
"""The unit picker for the KGA Cambodia database.

Sits inside the Add Open Data window when the source is the KGA database. The
shape follows how people ask for Cambodian places: narrow by province, then
district, then commune with three drop-downs, and tick the units of the chosen
level in a searchable list. Ticking nothing means "everything inside the
filters above" - so a whole province's communes is two clicks, and one named
district is a filter and a tick.

Everything it lists comes from the manifest, so the drop-downs fill without
downloading any geometry. Only the village list needs a province pack (14,000
villages would triple the manifest), which the window supplies through
`villages`; until a province is chosen the list says so rather than guessing.

Enum members are scoped (`Qt.CheckState.Checked`) for PyQt6.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QComboBox, QHBoxLayout, QLabel,
                                 QLineEdit, QListWidget, QListWidgetItem,
                                 QPushButton, QVBoxLayout, QWidget)

from ..core import kga_release as KR
from ..core.net import NetError


def tr(text):
    return text


class KgaPicker(QWidget):
    """Province / district / commune filters and a tickable list of units."""

    #: The selection changed; the window should describe it again.
    changed = pyqtSignal()
    #: A new access key was saved; the window should reload the release.
    keySaved = pyqtSignal()

    def __init__(self, parent=None, villages=None):
        super().__init__(parent)
        #: callable(pro_code) -> [(vil_code, name_en, name_kh, com_code)]
        self._villages = villages
        self._manifest = None
        self._level = 2
        self._geometry = KR.BOUNDARY
        self._building = False
        self._ticked = {}                   # code -> display name

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        # One row of three drop-downs, no labels: each starts with its own
        # "(All ...)" entry, which names it, and a row of three costs a third
        # of the height of three form rows in a window that is already tall.
        self.scope = QHBoxLayout()
        self.scope.setContentsMargins(0, 0, 0, 0)
        self.province_combo = QComboBox(self)
        self.district_combo = QComboBox(self)
        self.commune_combo = QComboBox(self)
        for combo, tip in ((self.province_combo, tr('Province')),
                           (self.district_combo, tr('District')),
                           (self.commune_combo, tr('Commune'))):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
            combo.setToolTip(tip)
            self.scope.addWidget(combo, 1)
        column.addLayout(self.scope)
        self.province_combo.currentIndexChanged.connect(self._on_province)
        self.district_combo.currentIndexChanged.connect(self._on_district)
        self.commune_combo.currentIndexChanged.connect(self._on_commune)

        self.heading = QLabel('', self)
        column.addWidget(self.heading)

        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText(tr('Type to filter the list...'))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        column.addWidget(self.filter_edit)

        self.list = QListWidget(self)
        self.list.setMinimumHeight(110)
        self.list.itemChanged.connect(self._on_item_changed)
        column.addWidget(self.list)

        row = QHBoxLayout()
        self.tick_button = QPushButton(tr('Tick shown'), self)
        self.tick_button.setToolTip(tr(
            'Tick every unit the filter box leaves in the list.'))
        self.tick_button.clicked.connect(self._tick_shown)
        self.clear_button = QPushButton(tr('Clear ticks'), self)
        self.clear_button.clicked.connect(self._clear_ticks)
        self.count_label = QLabel('', self)
        self.count_label.setStyleSheet('color: palette(mid);')
        row.addWidget(self.tick_button)
        row.addWidget(self.clear_button)
        row.addWidget(self.count_label, 1)
        column.addLayout(row)

        column.addWidget(self._build_key_row())

    # ------------------------------------------------------------ access key

    def _build_key_row(self):
        widget = QWidget(self)
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        self.key_label = QLabel('', widget)
        self.key_edit = QLineEdit(widget)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(tr('Paste the KGA access key'))
        self.key_save = QPushButton(tr('Save key'), widget)
        self.key_save.setToolTip(tr(
            'Stored encrypted in the QGIS password store, never in plain '
            'settings. QGIS may ask for its master password.'))
        self.key_change = QPushButton(tr('Change key'), widget)
        self.key_save.clicked.connect(self._save_key)
        self.key_edit.returnPressed.connect(self._save_key)
        self.key_change.clicked.connect(self._start_change)
        row.addWidget(self.key_label)
        row.addWidget(self.key_edit, 10)
        row.addWidget(self.key_save)
        row.addWidget(self.key_change)
        row.addStretch(1)
        self._sync_key_row(editing=not KR.has_token())
        return widget

    def _sync_key_row(self, editing, message=''):
        saved = KR.has_token()
        self.key_edit.setVisible(editing)
        self.key_save.setVisible(editing)
        self.key_change.setVisible(not editing)
        if message:
            self.key_label.setText(message)
        elif saved:
            self.key_label.setText(tr('Access key: saved'))
        else:
            self.key_label.setText(tr('Access key:'))

    def _start_change(self):
        self.key_edit.clear()
        self._sync_key_row(editing=True)
        self.key_edit.setFocus()

    def _save_key(self):
        key = self.key_edit.text().strip()
        if not key:
            return
        where = KR.save_token(key)
        self.key_edit.clear()
        if where == 'profile settings':
            message = tr('Access key saved in your QGIS profile settings (the '
                         'QGIS password store is not available here).')
        else:
            message = tr('Access key: saved')
        self._sync_key_row(editing=False, message=message)
        self.keySaved.emit()

    # ------------------------------------------------------------- configure

    def configure(self, manifest, level, geometry):
        """Point the picker at a manifest, a level and a feature type.

        Called by the window whenever any of those change; it never emits
        `changed`, because the window is already describing the new state.
        """
        self._building = True
        try:
            first = self._manifest is not manifest
            if first:
                self._sync_key_row(editing=not KR.has_token())
            self._manifest = manifest
            self._geometry = geometry
            if first:
                self._fill_provinces()
            if level != self._level:
                self._ticked.clear()
            self._level = level
            self._show_scope_rows()
            self._rebuild_list()
        finally:
            self._building = False
        self._relayout()

    def _relayout(self):
        """Make every layout above this one forget its cached size.

        Showing or hiding the scope rows changes how tall the picker must be,
        but the group box and window around it keep the height they computed
        for the old row count and squeeze the list under its buttons. The row
        of scope drop-downs is a sub-layout with a size cache of its own, which
        a plain invalidate() of its parent does not reach; activate() does.
        """
        self.layout().activate()
        widget = self.parentWidget()
        while widget is not None:
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
            widget.updateGeometry()
            widget = widget.parentWidget()

    def clear_manifest(self):
        """The release could not be read: show an empty picker."""
        self._building = True
        try:
            self._manifest = None
            self._ticked.clear()
            # Whatever went wrong, the key is the likeliest thing to fix, and a
            # saved one may be the expired one: put the box where it is needed.
            self._sync_key_row(editing=True)
            for combo in (self.province_combo, self.district_combo,
                          self.commune_combo):
                combo.clear()
            self.list.clear()
            self.count_label.setText('')
        finally:
            self._building = False

    def _show_scope_rows(self):
        for combo, minimum in ((self.province_combo, 2),
                               (self.district_combo, 3),
                               (self.commune_combo, 4)):
            combo.setVisible(self._level >= minimum)
        self.district_combo.setEnabled(bool(self._province()))
        self.commune_combo.setEnabled(bool(self._district()))

    # --------------------------------------------------------------- filling

    def _fill_provinces(self):
        previous = self._province()
        self.province_combo.clear()
        self.province_combo.addItem(tr('(All provinces)'), None)
        for row in self._manifest.provinces:
            self.province_combo.addItem(KR.label(row), row['code'])
        self.province_combo.setCurrentIndex(
            max(0, self.province_combo.findData(previous)))
        self._fill_districts()

    def _fill_districts(self):
        pro = self._province()
        self.district_combo.clear()
        self.district_combo.addItem(tr('(All districts)'), None)
        if pro:
            for row in self._manifest.districts_of(pro):
                self.district_combo.addItem(KR.label(row), row['code'])
        self.district_combo.setEnabled(bool(pro))
        self._fill_communes()

    def _fill_communes(self):
        dist = self._district()
        self.commune_combo.clear()
        self.commune_combo.addItem(tr('(All communes)'), None)
        if dist:
            for row in self._manifest.communes_of(dist):
                self.commune_combo.addItem(KR.label(row), row['code'])
        self.commune_combo.setEnabled(bool(dist))

    def _province(self):
        return self.province_combo.currentData()

    def _district(self):
        return self.district_combo.currentData()

    def _commune(self):
        return self.commune_combo.currentData()

    def _on_province(self):
        if self._building or self._manifest is None:
            return
        self._building = True
        try:
            self._fill_districts()
            self._ticked.clear()
            self._rebuild_list()
        finally:
            self._building = False
        self.changed.emit()

    def _on_district(self):
        if self._building or self._manifest is None:
            return
        self._building = True
        try:
            self._fill_communes()
            self._ticked.clear()
            self._rebuild_list()
        finally:
            self._building = False
        self.changed.emit()

    def _on_commune(self):
        if self._building or self._manifest is None:
            return
        self._building = True
        try:
            self._ticked.clear()
            self._rebuild_list()
        finally:
            self._building = False
        self.changed.emit()

    # ------------------------------------------------------------------ list

    def _candidates(self):
        """[(code, label, enabled, tooltip)] for the current level and scope."""
        manifest = self._manifest
        pro, dist = self._province(), self._district()
        boundary = self._geometry == KR.BOUNDARY
        out = []

        def add(row, has_geometry):
            tip = row['code']
            if boundary and not has_geometry:
                tip += tr(' - centre point only, no boundary in the database yet')
            out.append((row['code'], KR.label(row),
                        has_geometry or not boundary, tip))

        if self._level == 1:
            for row in manifest.provinces:
                add(row, row.get('boundary', True))
        elif self._level == 2:
            for row in (manifest.districts_of(pro) if pro
                        else manifest.districts):
                add(row, row.get('boundary', True))
        elif self._level == 3:
            if dist:
                rows = manifest.communes_of(dist)
            elif pro:
                rows = [c for c in manifest.communes if c['code'][:2] == pro]
            else:
                rows = manifest.communes
            for row in rows:
                add(row, row.get('boundary', False))
        return out

    def _village_candidates(self):
        pro, dist, com = self._province(), self._district(), self._commune()
        if not pro:
            return None
        rows = self._villages(pro) if self._villages else []
        out = []
        for vil, en, kh, com_code in rows:
            if dist and com_code[:4] != dist:
                continue
            if com and com_code != com:
                continue
            if not vil:
                continue
            name = '%s (%s)' % (en, kh) if en and kh else (en or kh or vil)
            commune = self._manifest.name(com_code)
            out.append((vil, '%s - %s' % (name, commune), True, vil))
        return out

    def _rebuild_list(self):
        self.list.blockSignals(True)
        try:
            self.list.clear()
            label = KR.LEVELS[self._level][2]
            self.heading.setText(tr('{0}s to add').format(label))
            self.filter_edit.clear()

            if self._level == 4:
                hint = ''
                try:
                    rows = self._village_candidates()
                except NetError as exc:
                    rows, hint = None, str(exc)
                if rows is None:
                    self.heading.setText(hint or tr(
                        'Choose a province to list its villages, or leave '
                        'everything unticked to add every village in the '
                        'filters above.'))
                    rows = []
            else:
                rows = self._candidates()

            for code, text, enabled, tip in rows:
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, code)
                flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                if not enabled:
                    flags &= ~Qt.ItemFlag.ItemIsEnabled
                item.setFlags(flags)
                item.setToolTip(tip)
                item.setCheckState(Qt.CheckState.Checked
                                   if code in self._ticked
                                   else Qt.CheckState.Unchecked)
                self.list.addItem(item)
            # Drop ticks for units no longer listed.
            shown = {self.list.item(i).data(Qt.ItemDataRole.UserRole)
                     for i in range(self.list.count())}
            self._ticked = {c: n for c, n in self._ticked.items() if c in shown}
        finally:
            self.list.blockSignals(False)
        self._update_count()

    def _apply_filter(self, text):
        needle = (text or '').strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            code = item.data(Qt.ItemDataRole.UserRole)
            item.setHidden(bool(needle) and needle not in item.text().lower()
                           and needle not in code)

    def _on_item_changed(self, item):
        code = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._ticked[code] = item.text()
        else:
            self._ticked.pop(code, None)
        self._update_count()
        if not self._building:
            self.changed.emit()

    def _tick_shown(self):
        self._set_all(lambda item: not item.isHidden()
                      and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled))

    def _clear_ticks(self):
        self._set_all(lambda item: False)

    def _set_all(self, wanted):
        self._building = True
        try:
            for i in range(self.list.count()):
                item = self.list.item(i)
                item.setCheckState(Qt.CheckState.Checked if wanted(item)
                                   else Qt.CheckState.Unchecked)
        finally:
            self._building = False
        self._update_count()
        self.changed.emit()

    def _update_count(self):
        n = len(self._ticked)
        total = self.list.count()
        if n:
            self.count_label.setText(tr('{0} ticked of {1}').format(n, total))
        else:
            self.count_label.setText(tr(
                'Nothing ticked: everything in the filters above is added.'))

    # ------------------------------------------------------------- selection

    def selection(self):
        """The current choice, in the shape ``KgaCambodiaSource.plan`` reads."""
        names = list(self._ticked.values())
        return {
            'province': self._province() if self._level >= 2 else None,
            'district': self._district() if self._level >= 3 else None,
            'commune': self._commune() if self._level >= 4 else None,
            'units': list(self._ticked.keys()),
            'names': names,
        }

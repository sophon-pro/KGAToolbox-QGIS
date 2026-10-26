# -*- coding: utf-8 -*-
"""The window shown when data is pasted from the Browser clipboard."""

from contextlib import suppress

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
)

from ..core import data_paste as dp

_SETTINGS = 'KGA/browserPaste/'

_CLASH_CHOICES = (
    (dp.CLASH_RENAME, 'Keep both (add _2 to the new name)'),
    (dp.CLASH_REPLACE, 'Replace the existing one'),
    (dp.CLASH_SKIP, 'Skip it'),
)


def _read(key, default):
    try:
        value = QgsSettings().value(_SETTINGS + key, default)
    except Exception:
        return default
    return value if value is not None else default


def _write(key, value):
    with suppress(Exception):
        QgsSettings().setValue(_SETTINGS + key, value)


def needs_dialog(sources, target, options, direct=False):
    """False when a paste can go straight ahead without asking anything.

    `direct` means the format was already chosen from the menu, so a folder
    only needs the window when a name clashes.
    """
    if target.kind == dp.TARGET_FOLDER and not direct:
        return True
    return bool(dp.clashes(sources, target, options))


def remembered_options():
    """Options as the user last left them."""
    return dp.Options(
        format=_read('format', dp.FMT_SHAPEFILE),
        on_clash=_read('onClash', dp.CLASH_RENAME),
    )


class PasteDialog(QDialog):

    def __init__(self, sources, target, parent=None, fmt=None):
        super().__init__(parent)
        self.sources = list(sources)
        self.target = target
        self.setWindowTitle('Paste')
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        count = len(self.sources)
        title = QLabel('<b>Paste {} item{} into {}</b>'.format(
            count, '' if count == 1 else 's', _escape(target.label)))
        title.setWordWrap(True)
        title.setToolTip(target.path)
        layout.addWidget(title)

        self.items = QListWidget()
        self.items.setMaximumHeight(110)
        self.items.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        for source in self.sources:
            self.items.addItem(source.name + (
                '  (raster)' if source.kind == dp.KIND_RASTER else ''))
        layout.addWidget(self.items)

        form = QFormLayout()
        remembered = remembered_options()

        self.format_combo = QComboBox()
        self.container_edit = QLineEdit()
        if target.kind == dp.TARGET_FOLDER:
            for code, label, _ext in dp.FORMATS:
                if code == dp.FMT_GDB and not dp.can_write_gdb():
                    continue
                self.format_combo.addItem(label, code)
            index = self.format_combo.findData(fmt or remembered.format)
            self.format_combo.setCurrentIndex(max(index, 0))
            form.addRow('Paste as', self.format_combo)

            self.container_label = QLabel('Name')
            self.container_edit.setText(dp.default_container_name(self.sources))
            self.container_edit.setToolTip(
                'The GeoPackage or geodatabase every pasted layer is put into.')
            form.addRow(self.container_label, self.container_edit)
            self.format_combo.currentIndexChanged.connect(self._refresh)
            self.container_edit.textChanged.connect(self._refresh)
        else:
            self.container_label = None
            self.format_combo.hide()
            self.container_edit.hide()

        self.clash_label = QLabel()
        self.clash_label.setWordWrap(True)
        self.clash_combo = QComboBox()
        for code, label in _CLASH_CHOICES:
            self.clash_combo.addItem(label, code)
        self.clash_combo.setCurrentIndex(
            max(self.clash_combo.findData(remembered.on_clash), 0))
        clash_prompt = QLabel('If the name exists')
        form.addRow(self.clash_label)
        form.addRow(clash_prompt, self.clash_combo)
        self._clash_rows = (self.clash_label, clash_prompt, self.clash_combo)

        self.styles_check = QCheckBox('Keep QGIS layer styles')
        self.styles_check.setChecked(True)
        self.styles_check.setToolTip(
            'Symbology and labels saved in a GeoPackage or geodatabase come '
            'along with the layer.')
        form.addRow(self.styles_check)
        layout.addLayout(form)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Paste')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)

        self._refresh()

    # -------------------------------------------------------------- state

    def options(self):
        fmt = self.format_combo.currentData() \
            if self.target.kind == dp.TARGET_FOLDER else dp.FMT_SHAPEFILE
        return dp.Options(
            format=fmt or dp.FMT_SHAPEFILE,
            container_name=self.container_edit.text().strip(),
            on_clash=self.clash_combo.currentData(),
            carry_styles=self.styles_check.isChecked(),
        )

    def _refresh(self, *_args):
        options = self.options()
        is_folder = self.target.kind == dp.TARGET_FOLDER
        is_container = (not is_folder) or options.format in (dp.FMT_GPKG,
                                                             dp.FMT_GDB)
        if is_folder:
            self.container_edit.setVisible(is_container)
            self.container_label.setVisible(is_container)
        self.styles_check.setVisible(is_container)

        found = dp.clashes(self.sources, self.target, options)
        for widget in self._clash_rows:
            widget.setVisible(bool(found))
        if found:
            shown = ', '.join(found[:5]) + (' ...' if len(found) > 5 else '')
            self.clash_label.setText(
                '<b>Already there:</b> {}'.format(_escape(shown)))

        rasters = [s for s in self.sources if s.kind == dp.KIND_RASTER]
        notes = []
        if rasters and (not is_folder):
            notes.append('Rasters can only be pasted into a folder, so '
                         '{} will be skipped.'.format(
                             'it' if len(rasters) == 1 else 'they'))
        elif rasters:
            notes.append('Rasters are saved as GeoTIFF.')
        self.note.setText(' '.join(notes))

        why = dp.problem(self.sources, self.target, options)
        if why:
            self.note.setText(why)
        name_ok = not (is_folder and is_container
                       and not (options.container_name
                                or dp.default_container_name(self.sources)))
        self._ok.setEnabled(not why and name_ok)

    def accept(self):
        options = self.options()
        _write('format', options.format)
        _write('onClash', options.on_clash)
        super().accept()


def _escape(text):
    return (text or '').replace('&', '&amp;').replace('<', '&lt;') \
        .replace('>', '&gt;')

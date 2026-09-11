# -*- coding: utf-8 -*-
"""Domain & Schema Manager dialog.

Three tabs over one open container:

* **Domains** - the editor QGIS does not have. Create, duplicate, rename,
  retype and delete, with a live usage count so a domain in use cannot be
  deleted by accident.
* **Assignment** - every table x field in one grid, so attaching one domain to
  twenty fields is one pass rather than twenty trips through the Browser panel.
* **Library** - export the container's domains to JSON and import them into
  another, which is what makes a domain set portable between a training
  dataset and a client deliverable.

The dialog is modeless so it can sit open beside the Browser panel; the caller
keeps the reference alive. It talks to `core.domains` and knows nothing about
the plugin object, matching the house rule the geoprocessing panel follows.

Enum members are written in their scoped form (`Qt.ItemDataRole.UserRole`, not
`Qt.UserRole`) because the unscoped spelling is gone in PyQt6, which QGIS 4
moves to.
"""

import os

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import (
    QCoreApplication,
    QElapsedTimer,
    QEvent,
    QEventLoop,
    Qt,
)
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPainter
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..branding import LOG_TAG
from ..core import domains as D
from ..core.compat import (
    NAME_BY_TYPE,
    T_DATE,
    T_DOUBLE,
    T_INT,
    T_LONGLONG,
    T_STRING,
    type_name,
)

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - outside QGIS
    iface = None

ROLE_NAME = Qt.ItemDataRole.UserRole

#: Field types a domain can be declared against, in the order they are offered.
DOMAIN_FIELD_TYPES = (T_STRING, T_INT, T_LONGLONG, T_DOUBLE, T_DATE)

DOMAIN_TYPES = ((D.CODED, 'Coded values'),
                (D.RANGE, 'Range'),
                (D.GLOB, 'Glob pattern'))

NO_DOMAIN = '(none)'


def tr(text):
    return QCoreApplication.translate('KgaDomainManager', text)


class _BusyOverlay(QWidget):
    """A translucent "working" panel laid over the widget it is given.

    Reading a container and building the Assignment grid both run on the GUI
    thread. A worker thread would not help: the time goes into creating one
    combo box per field, and widgets can only be made on the GUI thread. So
    the panel is pumped by hand instead — `step` and `message` run the event
    loop just long enough to repaint, with user input excluded so a second
    click on Reload cannot re-enter a rebuild that is still running.

    Calls nest. A grid rebuild started from inside a reload leaves the panel
    up until the outermost caller finishes, so the dialog never flickers
    between phases of one operation.
    """

    #: Milliseconds between repaints. Pumping the event loop more often than
    #: this costs more than the progress it reports.
    _REPAINT_MS = 60

    def __init__(self, parent):
        super().__init__(parent)
        self._depth = 0
        self._clock = QElapsedTimer()
        self._clock.start()
        parent.installEventFilter(self)

        outer = QVBoxLayout(self)

        panel = QFrame(self)
        # An object-name selector, not `QFrame` - QLabel is a QFrame too, and
        # a plain type selector would draw a border around the caption.
        panel.setObjectName('kgaBusyPanel')
        panel.setStyleSheet(
            '#kgaBusyPanel { background: palette(window); '
            'border: 1px solid palette(mid); border-radius: 4px; }')
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(10)

        self._label = QLabel('', panel)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(self._label)

        self._bar = QProgressBar(panel)
        self._bar.setFixedWidth(280)
        self._bar.setTextVisible(False)
        inner.addWidget(self._bar)

        # Aligned on the *widget*, not the layout: a layout alignment still
        # lets the panel stretch to the full width of the dialog.
        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignCenter)
        self.hide()

    # -- lifecycle ---------------------------------------------------------

    def begin(self, text):
        """Raise the panel. Always pair with `end()` in a `finally`."""
        self._depth += 1
        if self._depth == 1:
            self.setGeometry(self.parentWidget().rect())
            # 0..0 is Qt's marching indeterminate bar, for the reads whose
            # length is not known until they finish.
            self._bar.setRange(0, 0)
            self.show()
            self.raise_()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.message(text, force=True)

    def end(self):
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            self.hide()
            QApplication.restoreOverrideCursor()

    # -- progress ----------------------------------------------------------

    def message(self, text, force=False):
        """Name the phase, leaving the bar as it is."""
        self._label.setText(text)
        self._pump(force)

    def step(self, done, total, text=None):
        """Report measured progress, turning the bar determinate."""
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
        if text is not None:
            self._label.setText(text)
        self._pump(False)

    def _pump(self, force):
        if not force and self._clock.elapsed() < self._REPAINT_MS:
            return
        self._clock.restart()
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    # -- painting ----------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

    def paintEvent(self, event):
        # A wash of black rather than of palette(window): the window colour
        # over a window-coloured tab page is no dimming at all, and black at
        # this alpha reads as "behind glass" in a light theme and a dark one
        # alike.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 70))


class DomainManagerDialog(QDialog):
    """The whole tool. Owns one open container at a time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('KGA Domain & Schema Manager'))
        self.setObjectName('KgaDomainManagerDialog')
        self.resize(940, 620)

        self.path = ''
        self.conn = None
        self._specs = {}            # name -> DomainSpec, as last read
        self._editing = None        # name of the domain in the editor
        self._loading = False       # guards signal handlers during a rebuild
        self._assignments_stale = True   # does the grid need rebuilding?

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        layout.addWidget(self._build_source_row())
        self.warning_strip = self._build_warning_strip()
        layout.addWidget(self.warning_strip)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_domains_tab(), tr('Domains'))
        self.tabs.addTab(self._build_assignment_tab(), tr('Assignment'))
        self.tabs.addTab(self._build_library_tab(), tr('Library'))
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        # Parented to the tabs so it covers the tab bar too: a rebuild the
        # user cannot interrupt should not look like a tab they can leave.
        self.busy = _BusyOverlay(self.tabs)

        self.status = QLabel('', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._set_enabled(False)

    # ------------------------------------------------------------- source row

    def _build_source_row(self):
        widget = QWidget(self)
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel(tr('GeoPackage / Geodatabase:'), widget))
        self.path_edit = QLineEdit(widget)
        self.path_edit.setPlaceholderText(
            tr('Choose a .gpkg, or drag one here from the Browser panel'))
        self.path_edit.returnPressed.connect(
            lambda: self.open_container(self.path_edit.text().strip()))
        row.addWidget(self.path_edit, 1)

        browse = QPushButton(tr('Browse…'), widget)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)

        reload_button = QPushButton(tr('Reload'), widget)
        reload_button.setToolTip(tr('Re-read the container from disk'))
        reload_button.clicked.connect(lambda: self.open_container(self.path))
        row.addWidget(reload_button)

        widget.setAcceptDrops(True)
        widget.dragEnterEvent = self._drag_enter
        widget.dropEvent = self._drop
        self.setAcceptDrops(True)
        return widget

    def _build_warning_strip(self):
        """The "Save Features As" reminder, shown only when it can bite.

        Whether an export keeps domains depends on the GDAL build underneath.
        Rather than name GDAL at someone who only wants to hand a file to a
        colleague, the strip says where domains live, what loses them, and
        which button keeps them.
        """
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet(
            'QFrame { background: #fdf3d8; border: 1px solid #e3c96b; }')
        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 4, 8, 4)

        label = QLabel(tr(
            '<b>Domains belong to this whole GeoPackage, not to one layer.</b>'
            '<br>Exporting a single layer with <b>Save Features As…</b> can '
            'leave its domains behind. To share or back up the data with the '
            'domains intact, use <b>Copy container…</b> instead.'), frame)
        label.setWordWrap(True)
        row.addWidget(label, 1)

        self.copy_button = QPushButton(tr('Copy container…'), frame)
        self.copy_button.setToolTip(tr(
            'Save a copy of the whole GeoPackage — every layer and every '
            'domain together'))
        self.copy_button.clicked.connect(self._copy_container)
        row.addWidget(self.copy_button)

        frame.setVisible(False)
        return frame

    # ----------------------------------------------------------- domains tab

    def _build_domains_tab(self):
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Orientation.Horizontal, page)

        # --- left: the list ------------------------------------------------
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.domain_list = QListWidget(left)
        self.domain_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.domain_list.currentItemChanged.connect(self._on_domain_selected)
        left_layout.addWidget(self.domain_list, 1)

        list_buttons = QHBoxLayout()
        for label, slot, tip in (
                (tr('New'), self._new_domain, tr('Define a new domain')),
                (tr('Duplicate'), self._duplicate_domain,
                 tr('Copy the selected domain under a new name')),
                (tr('Delete'), self._delete_domain,
                 tr('Remove the selected domain'))):
            button = QPushButton(label, left)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            list_buttons.addWidget(button)
        left_layout.addLayout(list_buttons)
        splitter.addWidget(left)

        # --- right: the editor ---------------------------------------------
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        form_box = QGroupBox(tr('Definition'), right)
        form = QFormLayout(form_box)

        self.name_edit = QLineEdit(form_box)
        form.addRow(tr('Name'), self.name_edit)

        self.description_edit = QLineEdit(form_box)
        form.addRow(tr('Description'), self.description_edit)

        self.type_combo = QComboBox(form_box)
        for key, label in DOMAIN_TYPES:
            self.type_combo.addItem(tr(label), key)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(tr('Domain type'), self.type_combo)

        self.field_type_combo = QComboBox(form_box)
        for type_id in DOMAIN_FIELD_TYPES:
            self.field_type_combo.addItem(type_name(type_id), type_id)
        form.addRow(tr('Field type'), self.field_type_combo)

        self.usage_label = QLabel('', form_box)
        form.addRow(tr('Used by'), self.usage_label)
        right_layout.addWidget(form_box)

        # coded editor
        self.coded_box = QGroupBox(tr('Coded values'), right)
        coded_layout = QVBoxLayout(self.coded_box)
        self.values_table = QTableWidget(0, 2, self.coded_box)
        self.values_table.setHorizontalHeaderLabels([tr('Code'), tr('Label')])
        self.values_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.values_table.verticalHeader().setVisible(False)
        coded_layout.addWidget(self.values_table, 1)

        value_buttons = QHBoxLayout()
        for label, slot in ((tr('Add'), self._add_value),
                            (tr('Remove'), self._remove_value),
                            (tr('Move up'), lambda: self._move_value(-1)),
                            (tr('Move down'), lambda: self._move_value(1)),
                            (tr('Paste list…'), self._paste_values)):
            button = QPushButton(label, self.coded_box)
            button.clicked.connect(slot)
            value_buttons.addWidget(button)
        value_buttons.addStretch(1)
        coded_layout.addLayout(value_buttons)
        right_layout.addWidget(self.coded_box, 1)

        # range editor
        self.range_box = QGroupBox(tr('Range'), right)
        range_form = QFormLayout(self.range_box)
        self.min_spin = _wide_spin(self.range_box)
        self.min_inclusive = QCheckBox(tr('Minimum is included'), self.range_box)
        self.min_inclusive.setChecked(True)
        self.max_spin = _wide_spin(self.range_box)
        self.max_inclusive = QCheckBox(tr('Maximum is included'), self.range_box)
        self.max_inclusive.setChecked(True)
        self.use_min = QCheckBox(tr('Has a minimum'), self.range_box)
        self.use_min.setChecked(True)
        self.use_max = QCheckBox(tr('Has a maximum'), self.range_box)
        self.use_max.setChecked(True)
        range_form.addRow(self.use_min, self.min_spin)
        range_form.addRow('', self.min_inclusive)
        range_form.addRow(self.use_max, self.max_spin)
        range_form.addRow('', self.max_inclusive)
        right_layout.addWidget(self.range_box)

        # glob editor
        self.glob_box = QGroupBox(tr('Glob pattern'), right)
        glob_form = QFormLayout(self.glob_box)
        self.glob_edit = QLineEdit(self.glob_box)
        self.glob_edit.setPlaceholderText('K-*')
        glob_form.addRow(tr('Pattern'), self.glob_edit)
        right_layout.addWidget(self.glob_box)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.revert_button = QPushButton(tr('Revert'), right)
        self.revert_button.clicked.connect(self._revert_domain)
        save_row.addWidget(self.revert_button)
        self.save_button = QPushButton(tr('Save domain'), right)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save_domain)
        save_row.addWidget(self.save_button)
        right_layout.addLayout(save_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        outer.addWidget(splitter)
        return page

    # -------------------------------------------------------- assignment tab

    def _build_assignment_tab(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel(tr('Filter:'), page))
        self.assignment_filter = QLineEdit(page)
        self.assignment_filter.setPlaceholderText(tr('layer or field name'))
        self.assignment_filter.textChanged.connect(self._filter_assignments)
        top.addWidget(self.assignment_filter, 1)

        apply_all = QPushButton(tr('Apply to all matching field names'), page)
        apply_all.setToolTip(tr(
            'Give every field with the same name as the selected row the same '
            'domain. The same column name recurs across layers — status, '
            'owner, material — so this is usually what you want.'))
        apply_all.clicked.connect(self._apply_to_matching_names)
        top.addWidget(apply_all)
        layout.addLayout(top)

        self.assignment_table = QTableWidget(0, 3, page)
        self.assignment_table.setHorizontalHeaderLabels(
            [tr('Layer'), tr('Field'), tr('Domain')])
        header = self.assignment_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.assignment_table.verticalHeader().setVisible(False)
        self.assignment_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # The Layer column is merged per layer (see _update_layer_spans);
        # a merged cell is one row tall in every other respect, so wrapping
        # would only stretch the first row of each group.
        self.assignment_table.setWordWrap(False)
        layout.addWidget(self.assignment_table, 1)

        bottom = QHBoxLayout()
        self.assignment_hint = QLabel('', page)
        bottom.addWidget(self.assignment_hint, 1)
        revert = QPushButton(tr('Revert'), page)
        revert.clicked.connect(self._reload_assignments)
        bottom.addWidget(revert)
        apply_button = QPushButton(tr('Apply assignments'), page)
        apply_button.clicked.connect(self._apply_assignments)
        bottom.addWidget(apply_button)
        layout.addLayout(bottom)
        return page

    # ----------------------------------------------------------- library tab

    def _build_library_tab(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)

        export_box = QGroupBox(tr('Export'), page)
        export_layout = QVBoxLayout(export_box)
        export_layout.addWidget(QLabel(tr(
            'Write this container\'s domains, and which fields use them, to a '
            'JSON file you can version and reuse.'), export_box))
        self.export_assignments = QCheckBox(
            tr('Include field assignments'), export_box)
        self.export_assignments.setChecked(True)
        export_layout.addWidget(self.export_assignments)
        export_button = QPushButton(tr('Export library…'), export_box)
        export_button.clicked.connect(self._export_library)
        export_layout.addWidget(export_button)
        layout.addWidget(export_box)

        import_box = QGroupBox(tr('Import'), page)
        import_layout = QVBoxLayout(import_box)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(tr('Mode:'), import_box))
        self.import_mode = QComboBox(import_box)
        self.import_mode.addItem(tr('Merge — add and update, keep the rest'),
                                 D.MODE_MERGE)
        self.import_mode.addItem(tr('Replace — make this container match exactly'),
                                 D.MODE_REPLACE)
        mode_row.addWidget(self.import_mode, 1)
        import_layout.addLayout(mode_row)

        self.import_attach = QCheckBox(
            tr('Attach each domain to every field with a matching name'),
            import_box)
        self.import_attach.setChecked(True)
        import_layout.addWidget(self.import_attach)

        button_row = QHBoxLayout()
        preview_button = QPushButton(tr('Preview a library…'), import_box)
        preview_button.clicked.connect(lambda: self._import_library(True))
        button_row.addWidget(preview_button)
        import_button = QPushButton(tr('Import a library…'), import_box)
        import_button.clicked.connect(lambda: self._import_library(False))
        button_row.addWidget(import_button)
        button_row.addStretch(1)
        import_layout.addLayout(button_row)

        self.library_log = QPlainTextEdit(import_box)
        self.library_log.setReadOnly(True)
        self.library_log.setPlaceholderText(
            tr('The result of a preview or an import appears here.'))
        import_layout.addWidget(self.library_log, 1)
        layout.addWidget(import_box, 1)
        return page

    # ------------------------------------------------------------ drag & drop

    def _drag_enter(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def _drop(self, event):
        path = ''
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if local:
                    path = local
                    break
        if not path and event.mimeData().hasText():
            path = event.mimeData().text().split('|')[0].strip()
        if path:
            self.open_container(path)
            event.acceptProposedAction()

    def dragEnterEvent(self, event):         # noqa: N802 - Qt naming
        self._drag_enter(event)

    def dropEvent(self, event):              # noqa: N802 - Qt naming
        self._drop(event)

    # -------------------------------------------------------------- container

    def _browse(self):
        start = os.path.dirname(self.path) if self.path else ''
        path, _ = QFileDialog.getOpenFileName(
            self, tr('Open a container'), start,
            tr('GeoPackage (*.gpkg);;File Geodatabase (*.gdb);;All files (*)'))
        if path:
            self.open_container(path)

    def open_container(self, path):
        """Point the whole dialog at `path`. The one entry point for loading."""
        path = (path or '').strip().strip('"')
        if not path:
            return
        # A drop from the Browser panel carries a full layer URI.
        path = path.split('|')[0]

        # The panel stays up across the open *and* the reload it ends
        # with, so one user action reads as one wait, not three.
        self.busy.begin(tr('Opening {name}…').format(
            name=os.path.basename(path) or path))
        try:
            try:
                conn = D.connection_for(path)
            except D.DomainError as exc:
                self._warn(str(exc))
                return

            caps = D.capability_report(conn)
            if not caps['list']:
                self._warn(tr(
                    'This build cannot list field domains. Field '
                    'domain support needs GDAL 3.3 or newer.'))
                return

            self.path = path
            self.conn = conn
            self.path_edit.setText(path)
            self._set_enabled(True)

            writable = D.is_geopackage(path)
            for widget in (self.save_button, self.revert_button):
                widget.setEnabled(writable)
            if not writable:
                self._say(tr(
                    'Read-only: a File Geodatabase\'s domains can be read and '
                    'exported to a library, but only a GeoPackage can '
                    'be edited.'))

            self.reload()
        finally:
            self.busy.end()

    def reload(self):
        """Re-read everything from disk and rebuild every tab."""
        if self.conn is None:
            return
        # Marked before the read, not after: if listing the domains throws,
        # the grid still holds the old container's combo boxes and the next
        # visit to the tab must rebuild it.
        self._assignments_stale = True
        self.busy.begin(tr('Reading domains…'))
        try:
            try:
                specs = D.list_domains(self.conn)
            except D.DomainError as exc:
                self._warn(str(exc))
                return

            self._specs = {spec.name: spec for spec in specs}
            self.busy.message(tr('Building the domain list…'))
            self._reload_domain_list()
            self._reload_assignments()
            self.warning_strip.setVisible(bool(self._specs))
            self._say(tr('{count} domain(s) in {name}.').format(
                count=len(self._specs), name=os.path.basename(self.path)))
        finally:
            self.busy.end()

    def _set_enabled(self, enabled):
        self.tabs.setEnabled(enabled)

    # ------------------------------------------------------- domains: listing

    def _reload_domain_list(self):
        self._loading = True
        try:
            previous = self._editing
            self.domain_list.clear()
            counts = D.usage_counts(self.path)
            for name in sorted(self._specs):
                spec = self._specs[name]
                used = counts.get(name, 0)
                item = QListWidgetItem('{}  —  {}'.format(name, spec.summary()))
                item.setData(ROLE_NAME, name)
                item.setToolTip(tr('{type} domain, used by {n} field(s)').format(
                    type=spec.domain_type, n=used))
                if not used:
                    item.setForeground(QColor('#7a8b90'))
                self.domain_list.addItem(item)
        finally:
            self._loading = False

        if previous and previous in self._specs:
            self._select_domain(previous)
        elif self.domain_list.count():
            self.domain_list.setCurrentRow(0)
        else:
            self._clear_editor()

    def _select_domain(self, name):
        for row in range(self.domain_list.count()):
            if self.domain_list.item(row).data(ROLE_NAME) == name:
                self.domain_list.setCurrentRow(row)
                return

    def _current_domain_name(self):
        item = self.domain_list.currentItem()
        return item.data(ROLE_NAME) if item else None

    def _on_domain_selected(self, current, _previous):
        if self._loading or current is None:
            return
        name = current.data(ROLE_NAME)
        spec = self._specs.get(name)
        if spec is not None:
            self._load_editor(spec, name)

    # -------------------------------------------------------- domains: editor

    def _load_editor(self, spec, original_name):
        self._loading = True
        try:
            self._editing = original_name
            self.name_edit.setText(spec.name)
            self.description_edit.setText(spec.description)
            self.type_combo.setCurrentIndex(
                max(0, self.type_combo.findData(spec.domain_type)))
            index = self.field_type_combo.findData(spec.field_type)
            self.field_type_combo.setCurrentIndex(index if index >= 0 else 0)

            self.values_table.setRowCount(0)
            for code, label in spec.values:
                self._append_value_row(code, label)

            self.use_min.setChecked(spec.min_value is not None)
            self.use_max.setChecked(spec.max_value is not None)
            self.min_spin.setValue(spec.min_value or 0.0)
            self.max_spin.setValue(spec.max_value or 0.0)
            self.min_inclusive.setChecked(spec.min_inclusive)
            self.max_inclusive.setChecked(spec.max_inclusive)
            self.glob_edit.setText(spec.glob_pattern)

            used = D.usage_counts(self.path).get(original_name, 0)
            self.usage_label.setText(
                tr('{n} field(s)').format(n=used) if used else tr('nothing yet'))
        finally:
            self._loading = False
        self._on_type_changed()

    def _clear_editor(self):
        self._loading = True
        try:
            self._editing = None
            self.name_edit.clear()
            self.description_edit.clear()
            self.values_table.setRowCount(0)
            self.glob_edit.clear()
            self.usage_label.setText('')
        finally:
            self._loading = False
        self._on_type_changed()

    def _on_type_changed(self):
        kind = self.type_combo.currentData()
        self.coded_box.setVisible(kind == D.CODED)
        self.range_box.setVisible(kind == D.RANGE)
        self.glob_box.setVisible(kind == D.GLOB)

    def _append_value_row(self, code='', label=''):
        row = self.values_table.rowCount()
        self.values_table.insertRow(row)
        self.values_table.setItem(row, 0, QTableWidgetItem(_text(code)))
        self.values_table.setItem(row, 1, QTableWidgetItem(_text(label)))
        return row

    def _add_value(self):
        row = self._append_value_row()
        self.values_table.setCurrentCell(row, 0)
        self.values_table.editItem(self.values_table.item(row, 0))

    def _remove_value(self):
        rows = sorted({i.row() for i in self.values_table.selectedIndexes()},
                      reverse=True)
        for row in rows:
            self.values_table.removeRow(row)

    def _move_value(self, delta):
        row = self.values_table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.values_table.rowCount():
            return
        for column in range(2):
            here = self.values_table.takeItem(row, column)
            there = self.values_table.takeItem(target, column)
            self.values_table.setItem(row, column, there)
            self.values_table.setItem(target, column, here)
        self.values_table.setCurrentCell(target, 0)

    def _paste_values(self):
        """Bulk entry: one value per line, optionally `code=label` or `code,label`."""
        text, ok = _multiline_prompt(
            self, tr('Paste values'),
            tr('One value per line. Use "code = label" or "code, label" to give '
               'a code a different label.'))
        if not ok or not text.strip():
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for separator in ('=', ',', '\t'):
                if separator in line:
                    code, label = line.split(separator, 1)
                    self._append_value_row(code.strip(), label.strip())
                    break
            else:
                self._append_value_row(line, line)

    def _spec_from_editor(self):
        kind = self.type_combo.currentData()
        spec = D.DomainSpec(
            name=self.name_edit.text().strip(),
            description=self.description_edit.text().strip(),
            domain_type=kind,
            field_type=self.field_type_combo.currentData(),
        )
        if kind == D.CODED:
            values = []
            for row in range(self.values_table.rowCount()):
                code_item = self.values_table.item(row, 0)
                label_item = self.values_table.item(row, 1)
                code = code_item.text().strip() if code_item else ''
                if not code:
                    continue
                label = label_item.text().strip() if label_item else ''
                values.append((code, label or code))
            spec.values = values
        elif kind == D.RANGE:
            spec.min_value = self.min_spin.value() if self.use_min.isChecked() else None
            spec.max_value = self.max_spin.value() if self.use_max.isChecked() else None
            spec.min_inclusive = self.min_inclusive.isChecked()
            spec.max_inclusive = self.max_inclusive.isChecked()
        else:
            spec.glob_pattern = self.glob_edit.text().strip()
        return spec

    def _new_domain(self):
        self.domain_list.setCurrentItem(None)
        self._clear_editor()
        self.type_combo.setCurrentIndex(0)
        self.name_edit.setFocus()
        self._say(tr('Give the new domain a name and its values, then Save.'))

    def _duplicate_domain(self):
        name = self._current_domain_name()
        if not name:
            return
        spec = self._specs[name]
        copy = D.DomainSpec.from_dict(spec.to_dict())
        copy.name = _unique_name(name, self._specs)
        self.domain_list.setCurrentItem(None)
        self._load_editor(copy, None)
        self._say(tr('Duplicated. Save to write "{name}".').format(name=copy.name))

    def _delete_domain(self):
        name = self._current_domain_name()
        if not name:
            return
        used = D.usage_counts(self.path).get(name, 0)
        if used:
            answer = QMessageBox.question(
                self, tr('Delete domain'),
                tr('"{name}" is used by {n} field(s). Deleting it detaches it '
                   'from all of them. Continue?').format(name=name, n=used),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif QMessageBox.question(
                self, tr('Delete domain'),
                tr('Delete the domain "{name}"?').format(name=name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return

        if self._run(lambda: D.delete_domain(self.conn, name, self.path),
                     tr('Deleted "{name}".').format(name=name)):
            self._editing = None
            self.reload()

    def _revert_domain(self):
        name = self._editing
        if name and name in self._specs:
            self._load_editor(self._specs[name], name)
        else:
            self._clear_editor()

    def _save_domain(self):
        spec = self._spec_from_editor()
        original = self._editing
        message = tr('Saved "{name}".').format(name=spec.name)
        if self._run(lambda: D.write_domain(self.conn, spec, self.path, original),
                     message):
            self._editing = spec.name
            self.reload()
            self._refresh_open_layers()

    # ------------------------------------------------------------ assignment

    def _on_tab_changed(self, index):
        """Rebuild the Assignment grid on show, but only if it is out of date.

        A domain created on the Domains tab has to appear in the combo boxes
        here, so the grid is rebuilt on show rather than kept in sync from
        every edit path. But every path that can change what the grid shows -
        saving, deleting, applying, importing a library - goes through
        `reload()`, which rebuilds it already. Without the flag this rebuilt
        the whole grid on every visit to the tab, which on a container with a
        few hundred fields is seconds of waiting for an identical result.

        Nothing here refuses a rebuild the user asked for: Reload and Revert
        both go straight to `_reload_assignments`, which always re-reads.
        """
        if index == 1 and self.conn is not None and self._assignments_stale:
            self._reload_assignments()

    def _reload_assignments(self):
        """Rebuild the whole grid: one row, and one combo box, per field.

        A container with a few hundred fields takes long enough that a frozen
        dialog looks broken, so the work is reported layer by layer through
        the busy panel. Table repaints are switched off for the duration -
        without that, every pump of the event loop would redraw a grid that
        is still growing.
        """
        if self.conn is None:
            return
        self.busy.begin(tr('Reading layers and fields…'))
        self._loading = True
        table_view = self.assignment_table
        try:
            def reading(done, total, layer):
                # By far the longest phase on a big container, and it is one
                # blocking call from here - without this the bar would sit
                # frozen for the whole read.
                self.busy.step(done, total, tr(
                    'Reading fields — {layer} ({done} of {total} '
                    'layers)').format(layer=layer, done=done, total=total))

            try:
                listing = D.tables_and_fields(self.conn, progress=reading)
            except D.DomainError as exc:
                self._warn(str(exc))
                return
            self.busy.message(tr('Reading current assignments…'))
            current = D.field_domain_map(self.path)
            names = [NO_DOMAIN] + sorted(self._specs)
            editable = D.is_geopackage(self.path)
            total = sum(len(fields) for _, fields in listing)

            table_view.setRowCount(0)
            table_view.setUpdatesEnabled(False)
            done = 0
            for table, fields in listing:
                self.busy.step(done, total, tr(
                    'Building the grid — {layer} ({done} of {total} '
                    'fields)').format(layer=table, done=done, total=total))
                for field in fields:
                    row = table_view.rowCount()
                    table_view.insertRow(row)
                    layer_item = _readonly_item(table)
                    layer_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignTop
                        | Qt.AlignmentFlag.AlignLeft)
                    layer_item.setFont(_group_font())
                    layer_item.setToolTip(tr('{layer} — {n} field(s)').format(
                        layer=table, n=len(fields)))
                    table_view.setItem(row, 0, layer_item)
                    table_view.setItem(row, 1, _readonly_item(field))
                    combo = QComboBox(table_view)
                    combo.addItems(names)
                    assigned = current.get((table, field))
                    combo.setCurrentText(
                        assigned if assigned in names else NO_DOMAIN)
                    combo.setEnabled(editable)
                    # Remembered so Apply only touches rows that really moved.
                    combo.setProperty('kga_original', combo.currentText())
                    table_view.setCellWidget(row, 2, combo)
                    done += 1

            self.busy.step(total, total, tr('Grouping rows by layer…'))
            self._filter_assignments(self.assignment_filter.text())
            # Only here, at the end: every early return above leaves the grid
            # stale so the next visit tries again.
            self._assignments_stale = False
            self.assignment_hint.setText(tr(
                '{n} field(s) in {layers} layer(s). Changes are written when '
                'you press Apply.').format(n=table_view.rowCount(),
                                           layers=len(listing)))
        finally:
            table_view.setUpdatesEnabled(True)
            self._loading = False
            self.busy.end()

    def _filter_assignments(self, text):
        needle = (text or '').strip().lower()
        for row in range(self.assignment_table.rowCount()):
            table_item = self.assignment_table.item(row, 0)
            field_item = self.assignment_table.item(row, 1)
            haystack = '{} {}'.format(table_item.text(), field_item.text()).lower()
            self.assignment_table.setRowHidden(row, bool(needle) and
                                               needle not in haystack)
        self._update_layer_spans()

    def _update_layer_spans(self):
        """Merge the Layer cell down each run of rows belonging to one layer.

        A layer contributes a dozen rows or more, and repeating its name on
        every one of them buries the field names the eye is actually hunting
        for. One merged cell per layer reads as a group heading instead, with
        alternating shading so where one layer ends and the next begins is
        visible without reading.

        Spans are rebuilt from scratch on every filter change rather than
        adjusted: hiding rows can leave a group with no visible rows at all,
        and a stale span drawn over that gap survives the filter it was built
        for.
        """
        table = self.assignment_table
        table.clearSpans()
        shades = (QBrush(), table.palette().alternateBase())
        total = table.rowCount()
        row = 0
        group = 0
        while row < total:
            name = table.item(row, 0).text()
            end = row
            while end + 1 < total and table.item(end + 1, 0).text() == name:
                end += 1
            shown = [r for r in range(row, end + 1)
                     if not table.isRowHidden(r)]
            if shown:
                # Rows of one layer are contiguous, so any hidden row inside
                # this range is the same layer and spanning over it is safe:
                # a hidden row has no height, so the span draws only over what
                # is on screen.
                if shown[-1] > shown[0]:
                    table.setSpan(shown[0], 0,
                                  shown[-1] - shown[0] + 1, 1)
                for r in range(row, end + 1):
                    table.item(r, 0).setBackground(shades[group % 2])
                group += 1
            row = end + 1

    def _apply_to_matching_names(self):
        row = self.assignment_table.currentRow()
        if row < 0:
            self._say(tr('Select a row first.'))
            return
        field = self.assignment_table.item(row, 1).text()
        combo = self.assignment_table.cellWidget(row, 2)
        if combo is None:
            return
        wanted = combo.currentText()
        touched = 0
        for other in range(self.assignment_table.rowCount()):
            if self.assignment_table.item(other, 1).text() != field:
                continue
            other_combo = self.assignment_table.cellWidget(other, 2)
            if other_combo is not None and other_combo.isEnabled():
                other_combo.setCurrentText(wanted)
                touched += 1
        self._say(tr('Set {n} field(s) named "{field}" to {domain}. Press Apply '
                     'to write.').format(n=touched, field=field, domain=wanted))

    def _apply_assignments(self):
        if self.conn is None:
            return
        pending = []
        for row in range(self.assignment_table.rowCount()):
            combo = self.assignment_table.cellWidget(row, 2)
            if combo is None:
                continue
            now = combo.currentText()
            if now == combo.property('kga_original'):
                continue
            pending.append((self.assignment_table.item(row, 0).text(),
                            self.assignment_table.item(row, 1).text(), now))

        if not pending:
            self._say(tr('Nothing to apply.'))
            return

        def apply_all():
            for index, (table, field, domain) in enumerate(pending, 1):
                if domain == NO_DOMAIN:
                    D.detach_domain(self.conn, table, field)
                else:
                    D.attach_domain(self.conn, table, field, domain)
                self.busy.step(index, len(pending), tr(
                    'Writing change {n} of {total}…').format(
                        n=index, total=len(pending)))

        # One panel over the write and the reread it triggers.
        self.busy.begin(tr('Writing assignments…'))
        try:
            if self._run(apply_all, tr('Applied {n} assignment change(s).')
                         .format(n=len(pending))):
                self.reload()
                self.busy.message(tr('Refreshing open layers…'))
                self._refresh_open_layers()
        finally:
            self.busy.end()

    # -------------------------------------------------------------- library

    def _export_library(self):
        if self.conn is None:
            return
        start = os.path.splitext(self.path)[0] + '_domains.json'
        path, _ = QFileDialog.getSaveFileName(
            self, tr('Export domain library'), start, tr('JSON (*.json)'))
        if not path:
            return

        def do_export():
            data = D.export_library(self.conn, self.path,
                                    self.export_assignments.isChecked())
            D.save_library(data, path)

        self._run(do_export, tr('Exported to {path}.').format(path=path))

    def _import_library(self, preview_only):
        if self.conn is None:
            return
        if not preview_only and not D.is_geopackage(self.path):
            self._warn(tr('Only a GeoPackage can be written to.'))
            return

        path, _ = QFileDialog.getOpenFileName(
            self, tr('Open domain library'), os.path.dirname(self.path or ''),
            tr('JSON (*.json);;All files (*)'))
        if not path:
            return

        mode = self.import_mode.currentData()
        attach = self.import_attach.isChecked()
        try:
            data = D.load_library(path)
            report = D.preview_library(self.conn, data, self.path, mode, attach)
        except D.DomainError as exc:
            self._warn(str(exc))
            return

        lines = report.lines() or [tr('Nothing would change.')]
        self.library_log.setPlainText('\n'.join(lines))
        self.tabs.setCurrentIndex(2)

        if preview_only:
            self._say(tr('Preview only — nothing was written.'))
            return

        if report.total_changes:
            answer = QMessageBox.question(
                self, tr('Import library'),
                tr('{n} change(s) will be written to {name}. Continue?').format(
                    n=report.total_changes, name=os.path.basename(self.path)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self._say(tr('Import cancelled.'))
                return

        def do_import():
            result = D.import_library(self.conn, data, self.path, mode, attach)
            self.library_log.setPlainText(
                '\n'.join(result.lines() or [tr('Nothing changed.')]))
            if result.errors:
                raise D.DomainError('; '.join(result.errors))

        if self._run(do_import, tr('Library imported.')):
            self.reload()
            self._refresh_open_layers()

    def _copy_container(self):
        """Copy the whole GeoPackage, which keeps domains whatever GDAL does
        on a per-layer export."""
        if not self.path:
            return
        import shutil
        start = os.path.splitext(self.path)[0] + '_copy.gpkg'
        target, _ = QFileDialog.getSaveFileName(
            self, tr('Copy container to'), start, tr('GeoPackage (*.gpkg)'))
        if not target:
            return
        self._run(lambda: shutil.copyfile(self.path, target),
                  tr('Copied to {path}.').format(path=target))

    # --------------------------------------------------------------- plumbing

    def _refresh_open_layers(self):
        """Push edited domains into layers already loaded in the project."""
        try:
            touched = D.refresh_project_layers(self.path)
        except Exception as exc:            # pragma: no cover - defensive
            QgsMessageLog.logMessage(
                'Could not refresh layers: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Warning)
            return
        if touched and iface is not None:
            iface.mapCanvas().refresh()

    def _run(self, action, success_message):
        """Run one write, turning a `DomainError` into a message rather than a
        traceback. Returns True when it worked."""
        try:
            action()
        except D.DomainError as exc:
            self._warn(str(exc))
            return False
        except Exception as exc:            # pragma: no cover - defensive
            QgsMessageLog.logMessage(
                'Domain manager: {}'.format(exc), LOG_TAG,
                Qgis.MessageLevel.Critical)
            self._warn(tr('Unexpected error: {err}').format(err=exc))
            return False
        self._say(success_message)
        return True

    def _say(self, message):
        self.status.setText(message)
        self.status.setStyleSheet('color: #10333B;')

    def _warn(self, message):
        self.status.setText(message)
        self.status.setStyleSheet('color: #a3251b; font-weight: bold;')
        QgsMessageLog.logMessage(message, LOG_TAG, Qgis.MessageLevel.Warning)


# --------------------------------------------------------------------- helpers

def _wide_spin(parent):
    spin = QDoubleSpinBox(parent)
    spin.setDecimals(6)
    spin.setRange(-1e12, 1e12)
    return spin


def _group_font():
    font = QFont()
    font.setBold(True)
    return font


def _readonly_item(text):
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _text(value):
    return '' if value is None else str(value)


def _unique_name(base, taken):
    candidate = '{}_copy'.format(base)
    index = 2
    while candidate in taken:
        candidate = '{}_copy{}'.format(base, index)
        index += 1
    return candidate


def _multiline_prompt(parent, title, label):
    """A multi-line input box; QInputDialog's is single-line only."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    prompt = QLabel(label, dialog)
    prompt.setWordWrap(True)
    layout.addWidget(prompt)
    editor = QPlainTextEdit(dialog)
    editor.setFont(QFont('Consolas'))
    layout.addWidget(editor, 1)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        dialog)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.resize(420, 320)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return editor.toPlainText(), accepted

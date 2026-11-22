# -*- coding: utf-8 -*-
"""Zoom to Location: the window.

A small modeless window. Pick a continent, then a country, province, district,
commune and village - each list is filled from the one above it - or type a
latitude and longitude, and press Zoom To. The map moves there; the basemap can
be clipped to the place and the place can be added to the map as a layer.

Province, district, commune and village are check lists: tick several and the
list below holds the places of all of them, and Zoom To frames every place
ticked in the deepest list that has any.

Boundary lists fill on the GUI thread, like Add Open Data, with a wait cursor;
the village lookup runs in the background because OpenStreetMap can be slow.
"""

import secrets
import traceback
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsCoordinateTransform,
                       QgsGeometry, QgsMessageLog, QgsProject, QgsRasterLayer,
                       QgsTask)
from qgis.PyQt.QtCore import QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from ..branding import LOG_TAG, open_docs
from ..core import clip_basemap as CB
from ..core import data_sources as DS
from ..core import zoom_location as Z
from .check_combo import CheckCombo
from .window_reveal import revealed

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

TITLE = 'Zoom to Location'
ALG_NAME = 'zoom_to_location'
BLUE = '#174592'
DEFAULT_CONTINENT = 'AS'
DEFAULT_BASEMAP = 'google_satellite'
NO_BASEMAP = 'none'

STYLE = """
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 22px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QLabel#kgaHeading { font-weight: bold; }
QLabel#kgaHint { color: palette(mid); }
""" % {'blue': BLUE}

#: Shown in the coordinate box as a hint; one is picked each time the window
#: opens.
EXAMPLE_COORDINATES = ('11.5972, 104.8887', '13.4141, 103.8661',
                       '12.8716, 105.0357', '13.0973, 103.2027')

#: How long to wait after a tick before refilling the lists below, so ticking
#: three provinces in a row refills once. A test sets this to 0.
DEBOUNCE_MS = 350

#: Overridable so a test can serve places without the network.
make_tree = Z.AdminTree
find_villages = Z.villages_in

_INSTANCE = None


def tr(text):
    return text


class Spinner(QWidget):
    """A small turning arc, shown while something loads."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(QSize(18, 18))
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self):
        self.show()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(BLUE), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(2, 2, 14, 14), -self._angle * 16, 270 * 16)


def run_in_background(work, done):
    """Run `work()` off the GUI thread; `done(result, error)` runs on it.

    Returns the task, which the caller keeps alive. A test replaces this with a
    function that calls both at once.
    """
    def job(_task):
        # Wrapped: QGIS hands an empty result back as None, which would read
        # as a failure when the answer is simply "no villages".
        return [work()]

    def finished(exception, result=None):
        done(result[0] if result else None, exception)

    task = QgsTask.fromFunction(tr('Zoom to Location'), job,
                                on_finished=finished)
    QgsApplication.taskManager().addTask(task)
    return task


@revealed
class ZoomLocationDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(TITLE))
        self.setObjectName('KgaZoomLocationDialog')
        self.setMinimumWidth(460)
        self.setStyleSheet(STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._tree = None
        self._tree_iso3 = None
        #: Every unit shown in a combo, by combo, so a pick finds its Unit.
        self._units = {}

        outer = QVBoxLayout(self)
        heading = QLabel(tr('Zoom to'), self)
        heading.setObjectName('kgaHeading')
        outer.addWidget(heading)

        form = QFormLayout()
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.continent_combo = self._combo()
        self.country_combo = self._combo()
        self.province_combo = CheckCombo(self)
        self.district_combo = CheckCombo(self)
        self.commune_combo = CheckCombo(self)
        self.village_combo = CheckCombo(self)
        self.basemap_combo = self._combo()
        #: (admin level, combo) down the ladder, parents first.
        self._ladder = [(1, self.province_combo), (2, self.district_combo),
                        (3, self.commune_combo),
                        (Z.LEVEL_VILLAGE, self.village_combo)]
        form.addRow(tr('Continent'), self.continent_combo)
        form.addRow(tr('Country'), self.country_combo)
        form.addRow(tr('Province'), self.province_combo)
        form.addRow(tr('District'), self.district_combo)
        form.addRow(tr('Commune'), self.commune_combo)
        form.addRow(tr('Village'), self.village_combo)
        form.addRow(tr('Basemap'), self.basemap_combo)
        outer.addLayout(form)

        checks = QHBoxLayout()
        self.clip_check = QCheckBox(tr('Clip basemap for the area? (by extent)'),
                                    self)
        self.clip_check.setToolTip(tr(
            'Show the basemap only inside the box around the place.'))
        self.add_check = QCheckBox(tr('Add the location data to map?'), self)
        self.add_check.setToolTip(tr(
            'Add the place as a layer: its boundary, or a point for a '
            'village or a coordinate.'))
        checks.addWidget(self.clip_check)
        checks.addWidget(self.add_check)
        outer.addLayout(checks)

        coordinate = QHBoxLayout()
        coordinate.addWidget(QLabel(tr('Or type coordinate'), self))
        self.coordinate_edit = QLineEdit(self)
        self.new_example()
        self.coordinate_edit.setToolTip(tr(
            'Latitude, longitude in decimal degrees. When this is filled in '
            'it is used instead of the lists.'))
        self.coordinate_edit.setClearButtonEnabled(True)
        self.coordinate_edit.returnPressed.connect(self.zoom)
        coordinate.addWidget(self.coordinate_edit, 1)
        outer.addLayout(coordinate)

        status_row = QHBoxLayout()
        self.spinner = Spinner(self)
        status_row.addWidget(self.spinner)
        self.status = QLabel('', self)
        self.status.setObjectName('kgaHint')
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        outer.addLayout(status_row)
        self._village_job = 0
        self._task = None
        #: Units ticked in the lists below a changed row, to tick again when
        #: those lists are refilled, so adding a province keeps the districts.
        self._previous = {}
        self._pending_row = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._flush_pending)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.help_button = QPushButton(tr('Help'), self)
        self.help_button.setAutoDefault(False)
        self.help_button.clicked.connect(lambda: open_docs(ALG_NAME))
        buttons.addWidget(self.help_button)
        self.clear_button = QPushButton(tr('Clear Search'), self)
        self.clear_button.setAutoDefault(False)
        self.clear_button.setToolTip(tr(
            'Empty every list and the coordinate box and start again.'))
        self.clear_button.clicked.connect(self.clear_search)
        buttons.addWidget(self.clear_button)
        self.zoom_button = QPushButton(tr('Zoom To'), self)
        self.zoom_button.setObjectName('kgaPrimary')
        self.zoom_button.setDefault(True)
        self.zoom_button.clicked.connect(self.zoom)
        buttons.addWidget(self.zoom_button)
        outer.addLayout(buttons)

        self._fill_continents()
        self._fill_basemaps()
        self.continent_combo.currentIndexChanged.connect(self._continent_changed)
        self.country_combo.currentIndexChanged.connect(self._country_changed)
        for number, (_level, combo) in enumerate(self._ladder):
            combo.selectionChanged.connect(
                lambda n=number: self._schedule_ladder(n))
        self._continent_changed()

    def clear_search(self):
        """Back to the opening state: no place picked, no coordinate typed."""
        self._debounce.stop()
        self._pending_row = None
        self.coordinate_edit.clear()
        self.new_example()
        self.continent_combo.blockSignals(True)
        self.continent_combo.setCurrentIndex(
            max(self.continent_combo.findData(DEFAULT_CONTINENT), 0))
        self.continent_combo.blockSignals(False)
        self._continent_changed()           # empties the country and below
        self._say('')

    def new_example(self):
        """Pick a fresh example coordinate for the hint."""
        self.coordinate_edit.setPlaceholderText(
            tr('e.g. {}').format(secrets.choice(EXAMPLE_COORDINATES)))

    # ------------------------------------------------------------------ lists

    def _combo(self):
        combo = QComboBox(self)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Fixed)
        combo.setMaxVisibleItems(18)
        return combo

    def _fill(self, combo, entries, keep_blank=True):
        """Fill `combo` with (label, data) entries after a blank first one."""
        combo.blockSignals(True)
        combo.clear()
        if keep_blank:
            combo.addItem('', None)
        for label, data in entries:
            combo.addItem(label, data)
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _fill_continents(self):
        self._fill(self.continent_combo,
                   [(label, cid) for cid, label, _box in Z.CONTINENTS])
        index = self.continent_combo.findData(DEFAULT_CONTINENT)
        self.continent_combo.setCurrentIndex(max(index, 0))

    @staticmethod
    def _map_layer_ids():
        """Ids of the layers in the map being shown, or None for "all of them".

        With Map Tabs on, the project holds several maps; only the open one
        counts. Without it the whole project is the map.
        """
        try:
            from ..gui import map_tabs
            return map_tabs.active_map_layer_ids()
        except Exception:                   # noqa: BLE001 - Map Tabs is optional
            return None

    def _fill_basemaps(self):
        """Basemaps in the open map (top one first), then presets."""
        combo = self.basemap_combo
        combo.blockSignals(True)
        combo.clear()
        project = QgsProject.instance()
        in_map = self._map_layer_ids()
        for node in project.layerTreeRoot().findLayers():
            layer = node.layer()
            if layer is None or (in_map is not None
                                 and layer.id() not in in_map):
                continue
            if (isinstance(layer, QgsRasterLayer)
                    and layer.providerType() == 'wms'
                    and 'type=xyz' in layer.source()
                    and not layer.customProperty(CB.MASK_PROPERTY)):
                combo.addItem(tr('{} (in project)').format(layer.name()),
                              'layer:' + layer.id())
        in_project = combo.count()
        for basemap in DS.BASEMAPS:
            combo.addItem(basemap.label, 'preset:' + basemap.id)
        combo.addItem(tr('No basemap'), NO_BASEMAP)
        if in_project:
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(
                max(combo.findData('preset:' + DEFAULT_BASEMAP), 0))
        combo.blockSignals(False)

    def _clear_ladder(self, start=0):
        self._village_job += 1          # drop any village lookup in flight
        self.spinner.stop()
        for _level, combo in self._ladder[start:]:
            combo.clear_items()
            combo.setEnabled(False)
            combo.setToolTip('')

    def _continent_changed(self, *_args):
        cid = self.continent_combo.currentData()
        entries = ([(name, iso3) for iso3, name in Z.countries_in(cid)]
                   if cid else [])
        self._fill(self.country_combo, entries)
        self.country_combo.setEnabled(bool(cid))
        self._country_changed()

    def _country_changed(self, *_args):
        self._clear_ladder()
        self._previous = {}
        self._pending_row = None
        self._tree = None
        self._tree_iso3 = None
        iso3 = self.country_combo.currentData()
        if not iso3:
            self._say('')
            return
        self._tree = make_tree(iso3)
        self._tree_iso3 = iso3
        try:
            self._busy(tr('Looking up the places in {}...').format(
                self.country_combo.currentText()))
            levels = self._tree.levels()
        except Z.ZoomError as exc:
            self._done()
            self._say(str(exc), error=True)
            return
        self._done()
        for level, combo in self._ladder[:3]:
            if level not in levels:
                combo.setToolTip(tr('{} publishes no boundaries at this '
                                    'level.').format(
                    self.country_combo.currentText()))
        self._fill_next(-1, [None])

    def _schedule_ladder(self, number):
        """A tick in ladder row `number`; refill below once the ticking stops."""
        self._pending_row = number
        if DEBOUNCE_MS <= 0:
            self._flush_pending()
        else:
            self._debounce.start(DEBOUNCE_MS)

    def _flush_pending(self):
        number, self._pending_row = self._pending_row, None
        self._debounce.stop()
        if number is not None:
            self._ladder_changed(number)

    def _ladder_changed(self, number):
        """Ticks changed in ladder row `number`: refill the row below."""
        self._previous = {combo: self._units_in(combo)
                          for _level, combo in self._ladder[number + 1:]}
        self._clear_ladder(number + 1)
        parents = self._units_in(self._ladder[number][1])
        if parents:
            self._fill_next(number, parents)
        else:
            self._say('')

    def _label(self, unit, parents):
        """What a list shows for `unit`: its name, and where it is when the
        list holds the places of more than one parent."""
        if len([p for p in parents if p is not None]) > 1 and unit.parent:
            return '{0} ({1})'.format(unit.label, unit.parent.name)
        return unit.label

    def _fill_next(self, number, parents):
        """Fill the next row that can be filled, from `parents`' children.

        `parents` is a list of units; [None] means the country itself.
        """
        tree = self._tree
        if tree is None:
            return
        levels = tree.levels()
        for row in range(number + 1, len(self._ladder)):
            level, combo = self._ladder[row]
            if level != Z.LEVEL_VILLAGE and level not in levels:
                combo.setEnabled(False)
                continue
            if level == Z.LEVEL_VILLAGE:
                if parents == [None]:
                    break
                self._start_villages(parents)
                return
            try:
                self._busy(tr('Loading the {}...').format(
                    self._row_name(level)))
                units = [child for parent in parents
                         for child in tree.children(level, parent)]
            except Z.ZoomError as exc:
                self._done()
                self._say(str(exc), error=True)
                return
            self._done()
            self._units[combo] = units
            combo.set_items([(self._label(u, parents), i)
                             for i, u in enumerate(units)])
            combo.setEnabled(bool(units))
            kept = [u for u in self._previous.get(combo, []) if u in units]
            if units:
                self._say(tr('{} {} found.').format(len(units),
                                                    self._row_name(level)))
            else:
                self._say(tr('No {} found there.').format(
                    self._row_name(level)))
            if kept:
                combo.set_checked_data([units.index(u) for u in kept],
                                       notify=False)
                self._fill_next(row, kept)
            return
        if parents == [None]:
            self._say(tr('No boundaries below the country are published for '
                         '{}.').format(self.country_combo.currentText()))

    def _start_villages(self, parents):
        """Look for villages in the background; enable the list only if some
        turn up, so a slow or empty answer never blocks the window."""
        combo = self.village_combo
        self._village_job += 1
        job = self._village_job
        combo.setEnabled(False)
        self._say(tr('Looking for villages...'))
        self.spinner.start()

        def work():
            found, error = [], None
            for parent in parents:
                try:
                    found.extend(find_villages(parent))
                except Z.ZoomError as exc:
                    error = error or exc
            if error is not None and not found:
                raise error
            return found

        def done(units, error):
            if job != self._village_job or not _alive(self):
                return              # the pick moved on; this answer is stale
            self.spinner.stop()
            if error is not None or units is None:
                message = (str(error) if isinstance(error, Z.ZoomError)
                           else tr('Could not look up villages.'))
                self._say(message, error=True)
                combo.setToolTip(message)
                return
            self._units[combo] = units
            combo.set_items([(self._label(u, parents), i)
                             for i, u in enumerate(units)])
            combo.setEnabled(bool(units))
            if units:
                combo.setToolTip('')
                self._say(tr('{} villages found.').format(len(units)))
            else:
                combo.setToolTip(tr('No villages are recorded for this '
                                    'area.'))
                self._say(tr('No villages are recorded for this area.'))

        self._task = run_in_background(work, done)

    def _row_name(self, level):
        names = dict(Z.ADMIN_ROWS)
        names[Z.LEVEL_VILLAGE] = 'villages'
        name = names[level]
        return name.lower() + ('s' if level != Z.LEVEL_VILLAGE else '')

    def _units_in(self, combo):
        """The units ticked in a ladder list, in list order."""
        units = self._units.get(combo) or []
        return [units[i] for i in combo.checked_data()
                if 0 <= i < len(units)]

    # ----------------------------------------------------------------- status

    def _say(self, text, error=False):
        self.status.setStyleSheet('color: #B3261E;' if error else '')
        self.status.setText(text)

    def _busy(self, text):
        self._say(text)
        self.spinner.start()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

    def _done(self):
        QApplication.restoreOverrideCursor()
        self.spinner.stop()

    # ----------------------------------------------------------------- target

    def selected_target(self):
        """The place the window points at, a :class:`Z.Target`.

        A typed coordinate wins over the lists. Otherwise the deepest list
        with a pick decides. Raises :class:`Z.ZoomError`.
        """
        text = self.coordinate_edit.text().strip()
        if text:
            lat, lon = Z.parse_coordinate(text)
            return Z.coordinate_target(lat, lon)

        for level, combo in reversed(self._ladder):
            units = self._units_in(combo)
            if units:
                kind = ('Village' if level == Z.LEVEL_VILLAGE
                        else dict(Z.ADMIN_ROWS)[level])
                return Z.units_target(units, kind, self._tree)

        iso3 = self.country_combo.currentData()
        if iso3:
            name = self.country_combo.currentText()
            box = Z.country_extent(iso3)
            geometry = None
            if box is None or self.add_check.isChecked():
                units = self._tree.loader.load(0) if self._tree else []
                if units:
                    geometry = units[0].geometry
                    box = box or units[0].extent()
            if box is None:
                raise Z.ZoomError(tr('{} cannot be framed: no extent is '
                                     'known for it.').format(name))
            item = getattr(self._tree.loader, 'item', {}).get(0) \
                if self._tree else None
            return Z.Target(
                name, 'Country', box, geometry, [name],
                item.attribution if item else
                DS.GeoBoundariesSource.attribution,
                item.licence if item else '')

        cid = self.continent_combo.currentData()
        if cid:
            return Z.Target(self.continent_combo.currentText(), 'Continent',
                            Z.continent_extent(cid))
        raise Z.ZoomError(tr('Pick a place from the lists, or type a '
                             'coordinate.'))

    # ------------------------------------------------------------------- work

    def _basemap_layer(self):
        data = self.basemap_combo.currentData()
        if not data or data == NO_BASEMAP:
            return None
        kind, _sep, key = data.partition(':')
        if kind == 'layer':
            layer = QgsProject.instance().mapLayer(key)
            if layer is None:
                raise Z.ZoomError(tr('That basemap layer is no longer in the '
                                     'project.'))
            return layer
        try:
            return CB.find_or_add_basemap(key, only_ids=self._map_layer_ids())
        except CB.ClipError as exc:
            raise Z.ZoomError(str(exc))

    def zoom(self):
        self._flush_pending()
        try:
            self._busy(tr('Finding the place...'))
            try:
                target = self.selected_target()
            finally:
                self._done()
            notes = self._apply(target)
        except Z.ZoomError as exc:
            self._say(str(exc), error=True)
            return
        except Exception as exc:
            QgsMessageLog.logMessage(
                'Zoom to Location failed:\n{}'.format(traceback.format_exc()),
                LOG_TAG, Qgis.MessageLevel.Warning)
            self._say(str(exc), error=True)
            return
        self._say(' '.join([tr('Zoomed to {}.').format(
            ' > '.join(target.path))] + notes))

    def _apply(self, target):
        """Basemap, clip, layer, then the canvas. Returns notes to show."""
        notes = []
        # The map moves last: a failed transform must not leave half the work
        # done, so the box is checked first.
        canvas_box = self._canvas_extent(target.extent)

        basemap = self._basemap_layer()
        if basemap is not None and self.clip_check.isChecked():
            CB.clip(basemap, geometry=QgsGeometry.fromRect(target.extent),
                    crs=Z.wgs84())
            notes.append(tr('Basemap clipped to the area.'))

        if self.add_check.isChecked():
            if not target.geometries():
                notes.append(tr('A {} has no boundary to add.').format(
                    target.kind.lower()))
            else:
                layer = Z.target_layer(target)
                QgsProject.instance().addMapLayer(layer)
                notes.append(tr('Added "{}".').format(layer.name()))

        if iface is not None:
            canvas = iface.mapCanvas()
            canvas.setExtent(canvas_box)
            canvas.refresh()
            if target.geometries() and target.kind != 'Continent':
                with suppress(Exception):       # flashing is decoration
                    canvas.flashGeometries(target.geometries(), Z.wgs84())
        return notes

    def _canvas_extent(self, extent):
        """`extent` (EPSG:4326) in the map's CRS, a little wider."""
        crs = (iface.mapCanvas().mapSettings().destinationCrs()
               if iface is not None else QgsProject.instance().crs())
        transform = QgsCoordinateTransform(Z.wgs84(), crs,
                                           QgsProject.instance())
        try:
            box = transform.transformBoundingBox(extent)
        except Exception:               # noqa: BLE001 - QgsCsException
            box = None
        if box is None or box.isNull() or not box.isFinite() \
                or box.width() <= 0 or box.height() <= 0:
            raise Z.ZoomError(tr(
                'That place cannot be drawn in the map\'s coordinate system '
                '({}). Change the project CRS, for example to EPSG:4326 or '
                'EPSG:3857, and try again.').format(crs.authid()))
        box.scale(1.05)
        return box

    def shutdown(self):
        self.close()
        self.deleteLater()


def _alive(dialog):
    if dialog is None:
        return False
    try:
        dialog.objectName()
    except RuntimeError:                        # deleted by Qt
        return False
    return True


def show_zoom_location():
    """Open the window, or bring the one that is already open forward."""
    global _INSTANCE
    if not _alive(_INSTANCE):
        _INSTANCE = ZoomLocationDialog(
            iface.mainWindow() if iface is not None else None)
    else:
        _INSTANCE._fill_basemaps()
        _INSTANCE.new_example()
    _INSTANCE.setWindowModality(Qt.WindowModality.NonModal)
    _INSTANCE.show()
    _INSTANCE.raise_()
    _INSTANCE.activateWindow()
    return _INSTANCE


def close_zoom_location():
    """Close the window on plugin unload."""
    global _INSTANCE
    dialog, _INSTANCE = _INSTANCE, None
    if _alive(dialog):
        dialog.shutdown()

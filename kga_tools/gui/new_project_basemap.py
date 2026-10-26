# -*- coding: utf-8 -*-
"""Put the chosen basemap into every new project, as ArcGIS Pro does.

A new project is one QGIS has just created empty: File > New, the blank
project QGIS starts with, or a workspace with no project file yet. The basemap
is added only to a project that has no layers and no file, so opening a
project, or a project the user already filled, is never touched. The choice
lives in Settings (`core/kga_settings.py`).
"""

import time
import traceback
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
)
from qgis.PyQt.QtCore import QObject, QTimer

from ..branding import LOG_TAG
from ..core import basemaps, country_extents
from ..core import kga_settings as settings


def is_blank(project):
    """True for a project with no file and no layers."""
    return not project.fileName() and not project.mapLayers()


def zoom_to_country(project, canvas, iso3=None):
    """Frame a country (the chosen one) on `canvas`; False if it could not."""
    if canvas is None:
        return False
    extent = country_extents.extent_for(iso3 or settings.zoom_country())
    if extent is None:
        return False
    try:
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        transform = QgsCoordinateTransform(wgs84, project.crs(), project)
        rect = transform.transformBoundingBox(extent)
        if rect.isEmpty() or not rect.isFinite():
            return False
        canvas.setExtent(rect)
        canvas.refresh()
        return True
    except Exception:
        return False


#: After QGIS reports start-up done, and the latest a late-loaded plugin waits.
START_UP_SETTLE_MS = 2500
START_UP_WAIT_MS = 8000
#: How long a brand-new project is kept "unmodified" while QGIS settles it.
KEEP_CLEAN_SECONDS = 4.0
_GUARDS = []


class _KeepClean(QObject):
    """Undo QGIS's own "project changed" marks on a project nobody has touched.

    Adding the basemap makes QGIS zoom to it and mark the project changed a
    moment later, so the next File > New (or the one QGIS does at start-up)
    asked to save an empty project. For a few seconds, while the project is
    still just the basemap, every such mark is cleared again.
    """

    def __init__(self, project, layer):
        super().__init__()
        self.project = project
        self.layer = layer
        self.until = time.monotonic() + KEEP_CLEAN_SECONDS
        project.isDirtyChanged.connect(self._changed)
        _GUARDS.append(self)

    def _changed(self, dirty):
        if not dirty:
            return
        try:
            untouched = (time.monotonic() < self.until
                         and not self.project.fileName()
                         and list(self.project.mapLayers().values()) == [self.layer])
        except RuntimeError:
            untouched = False
        if untouched:
            self.project.setDirty(False)
        else:
            self.release()

    def release(self):
        with suppress(Exception):
            self.project.isDirtyChanged.disconnect(self._changed)
        if self in _GUARDS:
            _GUARDS.remove(self)


def _settle(project, layer, canvas):
    """After QGIS has zoomed to the new layer: set the CRS and extent again, stay clean."""
    with suppress(RuntimeError):
        if list(project.mapLayers().values()) != ([layer] if layer else []):
            return                          # the user has started working
        _set_crs(project)                   # QGIS may take the basemap's CRS again
        if settings.zoom_enabled():
            zoom_to_country(project, canvas)
        if not project.fileName():
            project.setDirty(False)


def _set_crs(project):
    crs = QgsCoordinateReferenceSystem(settings.project_crs())
    if not crs.isValid():
        crs = QgsCoordinateReferenceSystem(settings.DEFAULT_CRS)
    project.setCrs(crs)


def apply(project=None, force=False, canvas=None):
    """Set up a new project: its CRS, the chosen basemap, the start extent.

    Without `force`, only a blank project (no file, no layers) is touched. The
    basemap is added only when the setting is on, and the CRS is applied either
    way. Returns the basemap layer, or None. An unsaved blank project stays
    unmodified, so closing QGIS does not ask to save a project nobody touched.
    """
    project = project or QgsProject.instance()
    if not force and not is_blank(project):
        return None
    if project.mapLayers():
        return None
    untitled = not project.fileName()
    _set_crs(project)
    layer = None
    if settings.basemap_enabled():
        name = settings.basemap_name()
        try:
            layer = basemaps.add_basemap_layer(name, project)
        except Exception:
            QgsMessageLog.logMessage(
                'Could not add the basemap "{}" to the new project:{}{}'.format(
                    name, chr(10), traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)
        else:
            _set_crs(project)               # in case QGIS took the basemap's CRS
    if settings.zoom_enabled():
        zoom_to_country(project, canvas)
    if untitled:                # nothing of the user's yet: do not ask to save it
        project.setDirty(False)
        if layer is not None:
            _KeepClean(project, layer)
    # QGIS sets its own CRS and zooms to the new layer a moment later; put
    # both right again.
    QTimer.singleShot(400, lambda: _settle(project, layer, canvas))
    return layer


class NewProjectBasemap(QObject):
    """Adds the basemap when QGIS makes a new project."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._connected = False
        self._ready = False

    def install(self):
        if self._connected:
            return
        self.iface.newProjectCreated.connect(self._later)
        # QGIS announces a new project of its own while it starts, and a basemap
        # added then would take the window off the start page. Only projects the
        # user makes after start-up get one: ready a little after QGIS says it
        # has finished, or after a fixed wait when the plugin is loaded late.
        self._ready = False
        with suppress(Exception):
            self.iface.initializationCompleted.connect(self._start_up_done)
        QTimer.singleShot(START_UP_WAIT_MS, self._set_ready)
        # Not on start-up: the blank project QGIS opens with is left alone, so
        # the start page stays up until the user chooses or opens a project.
        self._connected = True

    def uninstall(self):
        if not self._connected:
            return
        with suppress(Exception):
            self.iface.newProjectCreated.disconnect(self._later)
        with suppress(Exception):
            self.iface.initializationCompleted.disconnect(self._start_up_done)
        self._connected = False

    def _start_up_done(self, *_args):
        QTimer.singleShot(START_UP_SETTLE_MS, self._set_ready)

    def _set_ready(self):
        self._ready = True

    def _later(self, *_args):
        if not self._ready:
            return
        # After QGIS has finished setting the new project up.
        QTimer.singleShot(0, self._apply)

    def _apply(self):
        try:
            apply(canvas=self.iface.mapCanvas())
        except RuntimeError:            # the plugin was unloaded meanwhile
            pass
        except Exception:
            QgsMessageLog.logMessage(
                'Could not set up the new project:{}{}'.format(
                    chr(10), traceback.format_exc()), LOG_TAG,
                Qgis.MessageLevel.Warning)

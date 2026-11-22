# -*- coding: utf-8 -*-
"""Drag and drop in the KGA Catalog: the compact drag badge, and drop-to-copy.

* Dragging items shows the same small badge as the Layers panel and the Data
  Explorer ('bridges  +7 more') instead of a picture of every selected row.
  The drag still carries the Browser's own mime data, so a drop on the map or
  the Layers panel adds the layers exactly as before.
* Dropping layers (or a whole GeoPackage / File Geodatabase) on a GeoPackage,
  a File Geodatabase or a folder copies the data there, like Pro's Catalog.
  Dropping on a layer copies into the database that holds it. The copy is the
  same as right-click Copy then Paste (`browser_paste`), with the same format
  question for folders and the same report; the Copy clipboard is not touched.

QgsBrowserTreeView is a C++ widget, so its startDrag() and drop handlers cannot
be overridden from Python: an event filter on its viewport takes the mouse move
that would start Qt's drag (see `drag_chip`) and the drag/drop events.
Drops from the QGIS Browser panel or the Data Explorer (layer URI lists) are
taken too.
"""

import os
import traceback
import uuid
from functools import partial

from qgis.core import Qgis, QgsLayerItem, QgsMessageLog, QgsMimeDataUtils
from qgis.PyQt.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from qgis.PyQt.QtGui import QCursor, QDrag
from qgis.PyQt.QtWidgets import QApplication, QToolTip

from ..branding import LOG_TAG
from ..core import data_paste as dp
from . import browser_paste as bp
from .drag_chip import drag_pixmap, reset_drag_state

#: Marks a drag that started in a Catalog tab; the data is a one-off token.
CATALOG_MIME = 'application/x-kga-catalog-drag'

#: (token, [Source]) of the Catalog drag in progress. Containers cannot travel
#: as layer URIs, so the sources are kept here rather than in the mime data.
_DRAGGED = ('', [])


def _log(message, level=Qgis.MessageLevel.Warning):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def _pos(event):
    if hasattr(event, 'position'):
        return event.position().toPoint()
    return event.pos()


def sources_from_mime(mime):
    """The copyable `Source`s a drop brings, or []."""
    if mime is None:
        return []
    if mime.hasFormat(CATALOG_MIME):
        token = bytes(mime.data(CATALOG_MIME)).decode('ascii', 'ignore')
        if token and token == _DRAGGED[0]:
            return list(_DRAGGED[1])
    if not QgsMimeDataUtils.isUriList(mime):
        return []
    sources = []
    for uri in QgsMimeDataUtils.decodeUriList(mime):
        if uri.layerType == 'vector':
            kind = dp.KIND_VECTOR
        elif uri.layerType == 'raster' and uri.providerKey == 'gdal':
            kind = dp.KIND_RASTER
        else:
            continue
        sources.append(dp.Source(uri.name, uri.uri, uri.providerKey, kind))
    return sources


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ''


def _source_container(source):
    return _norm(dp.container_path(source.uri.split('|', 1)[0]))


def drop_target(item):
    """The `Target` a drop on this Catalog item would copy into, or None.

    A layer inside a GeoPackage / File Geodatabase stands for its database."""
    if item is None:
        return None
    if isinstance(item, QgsLayerItem):
        path = dp.container_path(item.path())
        return dp.target_for_path(path) if path else None
    return bp.BrowserCopyPaste.paste_target([item])


class CatalogDragDrop(QObject):
    """Event filter on one Catalog tab's tree viewport."""

    def __init__(self, page, iface):
        super().__init__(page)
        self.page = page
        self.iface = iface
        self._press = None
        view = page.view
        view.setAcceptDrops(True)
        view.viewport().setAcceptDrops(True)
        view.viewport().installEventFilter(self)

    # ------------------------------------------------------------- events
    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        kind = event.type()
        try:
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseMove):
                return self._mouse(kind, event)
            if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
                return self._drag_event(kind, event)
            if kind == QEvent.Type.DragLeave:
                QToolTip.hideText()
        except Exception:
            _log('Catalog drag and drop failed:\n{}'.format(traceback.format_exc()))
        return False

    # --------------------------------------------------------- dragging out
    def _mouse(self, kind, event):
        if kind == QEvent.Type.MouseButtonPress:
            self._press = _pos(event) if event.button() == Qt.MouseButton.LeftButton else None
            return False
        if kind == QEvent.Type.MouseButtonRelease or self._press is None:
            self._press = None
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            self._press = None
            return False
        if (_pos(event) - self._press).manhattanLength() < QApplication.startDragDistance():
            return False
        press, self._press = self._press, None
        return self._start(press)

    def _start(self, press):
        global _DRAGGED
        view = self.page.view
        index = view.indexAt(press)
        selection = view.selectionModel()
        if not view.dragEnabled() or not index.isValid() or selection is None \
                or not selection.isSelected(index):
            return False            # not a drag Qt would start: leave it to the view
        proxy = view.model()
        rows = [i for i in selection.selectedRows()
                if proxy.flags(i) & Qt.ItemFlag.ItemIsDragEnabled]
        if not rows:
            return False
        mime = proxy.mimeData(rows)
        if mime is None:
            return False
        items = [i for i in (self.page.item_at(r) for r in rows) if i is not None]
        token = uuid.uuid4().hex
        try:
            sources = bp.sources_from_items(items)
        except Exception:
            sources = []
        _DRAGGED = (token, sources)
        mime.setData(CATALOG_MIME, token.encode('ascii'))

        names = [r.data() or '' for r in rows]
        icon = rows[0].data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not hasattr(icon, 'pixmap'):
            icon = None
        drag = QDrag(view)
        drag.setMimeData(mime)
        drag.setPixmap(drag_pixmap(names, icon))
        drag.setHotSpot(QPoint(12, 14))
        try:
            drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            reset_drag_state(view)
            QToolTip.hideText()
        return True

    # ----------------------------------------------------------- dropping in
    def _target_at(self, pos, sources):
        """(target, item) for a drop at `pos`, or (None, None) when refused."""
        view = self.page.view
        index = view.indexAt(pos)
        if not index.isValid():
            return None, None
        item = self.page.item_at(index)
        target = drop_target(item)
        if target is None or not sources:
            return None, None
        if target.kind != dp.TARGET_FOLDER and not any(
                s.kind == dp.KIND_VECTOR for s in sources):
            return None, None       # rasters go into folders only
        # dropped back on the database it came from: nothing to copy
        here = _norm(target.path)
        if target.kind != dp.TARGET_FOLDER and all(
                _source_container(s) == here for s in sources):
            return None, None
        return target, item

    def _drag_event(self, kind, event):
        mime = event.mimeData()
        sources = sources_from_mime(mime)
        if not sources:
            return False            # not data: the view's own handling (none)
        target, item = self._target_at(_pos(event), sources)
        if target is None:
            QToolTip.hideText()
            if kind == QEvent.Type.DragEnter:
                event.accept()      # keep the moves coming: another row may take it
            else:
                event.ignore()
            return True
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if kind != QEvent.Type.Drop:
            QToolTip.showText(QCursor.pos() + QPoint(18, 6),
                              '+ Copy to {}'.format(target.label), self.page.view)
            return True
        QToolTip.hideText()
        browser_path = item.path() if item is not None else target.path
        QTimer.singleShot(0, partial(self._paste, sources, target.path, browser_path))
        return True

    def _paste(self, sources, target_path, browser_path):
        provider = bp.BrowserCopyPaste(self.iface)
        provider.paste_sources(sources, target_path, browser_path)
        # the Catalog has its own browser model: show the new layers there too
        model = getattr(self.page.panel, 'model', None)
        if model is not None:
            for path in (browser_path, target_path):
                try:
                    model.refresh(path)
                except Exception:   # nosec B112 - a refresh is best effort
                    continue

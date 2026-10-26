# -*- coding: utf-8 -*-
"""
Thumbnail service.

* Memory LRU cache of QPixmaps.
* Persistent PNG cache in the QGIS profile, so thumbnails are rendered once
  and load instantly on every later start.
* Rendering happens in short, time-boxed slices on the GUI thread (QGIS
  symbol previews must be painted there), newest requests first, so the
  thumbnails the user is looking at appear first and QGIS never freezes.
"""
from contextlib import suppress
import hashlib
import os
import time
from collections import OrderedDict

from qgis.PyQt.QtCore import QObject, QTimer, QSize, pyqtSignal
from qgis.PyQt.QtGui import QPixmap
from qgis.core import QgsApplication

from . import previews

CACHE_VERSION = "2.4.3-1"     # bump when built-in symbol designs change
MEM_LIMIT = 4000
SLICE_MS = 12                 # max rendering time per event-loop slice


def cache_dir():
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "cache", "symbol_gallery",
                     CACHE_VERSION)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:  # pragma: no cover - read-only profile
        return None
    return d


class ThumbnailService(QObject):
    ready = pyqtSignal(str)          # entry id whose thumbnail just became available

    def __init__(self, lookup, parent=None):
        """*lookup(entry_id)* returns the catalog Entry (or None)."""
        super().__init__(parent)
        self._lookup = lookup
        self._mem = OrderedDict()
        self._queue = OrderedDict()   # key -> (entry_id, size, bg, dpr), newest last
        self._dir = cache_dir()
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)
        self.rendered = 0             # statistics (used by tests / benchmarks)
        self.from_disk = 0

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _key(entry, size, bg, dpr):
        return "%s|%s|%dx%d|%.2f|%s" % (entry.id, bg, size.width(), size.height(), dpr,
                                         getattr(entry, "version", ""))

    def _path(self, key):
        if not self._dir:
            return None
        return os.path.join(self._dir, hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest() + ".png")

    # ------------------------------------------------------------------ api
    def get(self, entry, size, bg, dpr=1.0, request=True):
        """Return a QPixmap if available now, else None (and queue it)."""
        key = self._key(entry, size, bg, dpr)
        pm = self._mem.get(key)
        if pm is not None:
            self._mem.move_to_end(key)
            return pm
        path = self._path(key)
        if path and os.path.exists(path):
            pm = QPixmap(path)
            if not pm.isNull():
                pm.setDevicePixelRatio(dpr)
                self._remember(key, pm)
                self.from_disk += 1
                return pm
        if request:
            self._queue.pop(key, None)
            self._queue[key] = (entry.id, QSize(size), bg, dpr)
            while len(self._queue) > 400:          # forget stale off-screen requests
                self._queue.popitem(last=False)
            if not self._timer.isActive():
                self._timer.start()
        return None

    def get_now(self, entry, size, bg, dpr=1.0):
        """Synchronous variant (detail preview)."""
        pm = self.get(entry, size, bg, dpr, request=False)
        if pm is None:
            key = self._key(entry, size, bg, dpr)
            pm = self._render(entry, size, bg, dpr)
            self._store(key, pm)
        return pm

    def clear_queue(self):
        self._queue.clear()
        self._timer.stop()

    def pending(self):
        return len(self._queue)

    def clear_memory(self):
        self._mem.clear()

    # ------------------------------------------------------------- internals
    def _remember(self, key, pm):
        self._mem[key] = pm
        if len(self._mem) > MEM_LIMIT:
            self._mem.popitem(last=False)

    def _store(self, key, pm):
        self._remember(key, pm)
        path = self._path(key)
        if path:
            with suppress(Exception):  # pragma: no cover - disk full etc.
                pm.save(path, "PNG")

    @staticmethod
    def _render(entry, size, bg, dpr):
        real = QSize(max(8, int(size.width() * dpr)), max(8, int(size.height() * dpr)))
        zoom = 1.6 * dpr * (size.width() / 96.0) ** 0.5
        try:
            pm = previews.entry_pixmap(entry, real, bg, zoom=zoom)
        except Exception:  # one broken symbol must never stop the gallery
            pm = previews.error_pixmap(real, bg)
        pm.setDevicePixelRatio(dpr)
        return pm

    def _work(self):
        deadline = time.perf_counter() + SLICE_MS / 1000.0
        while self._queue and time.perf_counter() < deadline:
            key, (eid, size, bg, dpr) = self._queue.popitem(last=True)   # newest first
            if key in self._mem:
                continue
            entry = self._lookup(eid)
            if entry is None:
                continue
            self._store(key, self._render(entry, size, bg, dpr))
            self.rendered += 1
            self.ready.emit(eid)
        if not self._queue:
            self._timer.stop()

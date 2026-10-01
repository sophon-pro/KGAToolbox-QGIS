# -*- coding: utf-8 -*-
"""The tile-download engine: fetch, retry, throttle, stitch, checkpoint.

No QgsTask in here. ``downloader.ImageryTask`` wraps this in a task; the tests
drive it directly. Network access is injected as ``fetch(url) -> (status, body,
error)`` so the engine can be tested against a local server, and the QGIS proxy
aware fetcher lives in ``downloader``.

Threads: workers only fetch and decode. The one thread that calls ``run`` is the
only one that touches GDAL (datasets are not thread-safe), and it is also the
one that writes the checkpoint.
"""

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import suppress
from dataclasses import dataclass

import numpy as np

from . import checkpoint as ck
from . import tile_math as tm
from .mosaic_writer import Mosaic

CHECKPOINT_SECONDS = 30
CHECKPOINT_TILES = 500
PROGRESS_INTERVAL = 0.2
MAX_RETRIES = 4

#: Spreads the retries of many workers apart. Not a security use of chance, but
#: the system generator costs nothing here and keeps the scanners quiet.
_JITTER = random.SystemRandom()


@dataclass
class JobSpec:
    source: object
    zoom: int
    tile_range: tuple
    bbox: tuple
    output_crs: str              # authid, e.g. EPSG:32648, or WKT for custom
    fmt: str                     # tif / jpg / png / jp2 / ecw
    folder: str
    name: str
    mask_wkt: str = None         # clip polygon in EPSG:4326
    checkpoint_enabled: bool = True
    workers: int = 8
    pyramids: bool = True
    keep_intermediate: bool = False
    plugin_version: str = ''
    export_only: bool = False    # skip downloading, build from the work file
    layer_name: str = ''
    transparent: bool = True     # clip mode: transparent outside, else white

    @property
    def total(self):
        return tm.tile_count(self.tile_range)

    @property
    def work_dir(self):
        """Where the work file lives."""
        if self.checkpoint_enabled:
            return self.folder
        return os.path.join(self.folder, '.' + self.name + '_kga_tmp')

    @property
    def work_file(self):
        return ck.work_path(self.work_dir, self.name)

    @property
    def checkpoint_file(self):
        # Without a user-visible checkpoint the same file still lives beside
        # the scratch work file, so 'retry failed tiles' can pick up.
        return ck.checkpoint_path(self.work_dir, self.name)

    @property
    def mask_hash(self):
        return ck.hash_text(self.mask_wkt) if self.mask_wkt else ''

    @property
    def signature(self):
        return ck.signature(self.source.id, self.zoom, self.tile_range,
                            self.output_crs, self.mask_hash, self.fmt)


@dataclass
class Outcome:
    state: str = 'error'   # complete / incomplete / stopped / throttled / error
    done: int = 0
    failed: int = 0
    message: str = ''


class Throttle:
    """Caps concurrency and reacts to a server that starts refusing us."""

    def __init__(self, limit, on_status=None):
        self._cond = threading.Condition()
        self.limit = max(1, limit)
        self._active = 0
        self._streak = 0
        self.aborted = False
        self._on_status = on_status
        self.slowed = False

    def acquire(self, should_stop):
        with self._cond:
            while self._active >= self.limit and not self.aborted \
                    and not should_stop():
                self._cond.wait(0.2)
            self._active += 1

    def release(self):
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def success(self):
        with self._cond:
            self._streak = 0

    def refused(self):
        """A 429 or 403 came back."""
        with self._cond:
            self._streak += 1
            if self._streak < 5:
                return
            self._streak = 0
            if self.limit > 1:
                self.limit = max(1, self.limit // 2)
                self.slowed = True
                if self._on_status:
                    self._on_status('Server is throttling, slowing down '
                                    '(%d connections)' % self.limit)
            else:
                self._fails_at_one = getattr(self, '_fails_at_one', 0) + 1
                if self._fails_at_one >= 3:
                    self.aborted = True
            self._cond.notify_all()


def _sleep(seconds, should_stop):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not should_stop():
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))


def fetch_with_retries(fetch, url, throttle, should_stop, backoff=1.0):
    """Return ('ok', bytes) or ('fail', reason)."""
    limited = 0
    reason = ''
    for attempt in range(MAX_RETRIES + 1):
        if should_stop():
            return 'fail', 'stopped'
        status, body, error = fetch(url)
        if status == 200 and body:
            throttle.success()
            return 'ok', body
        if status in (429, 403):
            throttle.refused()
        reason = error or ('HTTP %s' % status if status else 'network error')
        if status in (403, 404):
            limited += 1
            if limited >= 2:
                return 'fail', reason
        elif status is not None and 400 <= status < 500 and status != 429:
            return 'fail', reason
        if attempt < MAX_RETRIES:
            delay = min(20.0, backoff * (2 ** attempt)) * (0.5 + _JITTER.random())
            _sleep(delay, should_stop)
        if throttle.aborted:
            break
    return 'fail', reason


def decode_tile(data, tile_size=tm.TILE_SIZE):
    """Image bytes -> (4, h, w) uint8 RGBA array. Raises ValueError."""
    from qgis.PyQt.QtGui import QImage

    img = QImage()
    if not img.loadFromData(data):
        raise ValueError('The tile is not a readable image.')
    if img.width() != tile_size or img.height() != tile_size:
        raise ValueError('The tile is %dx%d, expected %d.' % (
            img.width(), img.height(), tile_size))
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    rows = np.frombuffer(ptr, dtype=np.uint8).reshape(
        tile_size, img.bytesPerLine())[:, :tile_size * 4]
    arr = rows.reshape(tile_size, tile_size, 4).transpose(2, 0, 1)
    return np.ascontiguousarray(arr)


class TileJob:
    def __init__(self, spec, fetch, decode=decode_tile, should_stop=None,
                 on_progress=None, on_status=None, backoff=1.0):
        self.spec = spec
        self.fetch = fetch
        self.decode = decode
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress
        self.on_status = on_status
        self.backoff = backoff
        self.bitmap = None
        self.failed = 0
        self._started_iso = None
        self._last_progress = 0.0
        self._session_done = 0
        self._t0 = None

    # ---- checkpoint plumbing -------------------------------------------

    def _save_checkpoint(self, mosaic):
        mosaic.commit()
        s = self.spec
        data = ck.build(s.signature, s.source.id, s.zoom, s.tile_range, s.bbox,
                        s.mask_hash, s.output_crs, s.fmt, s.total, self.bitmap,
                        self.failed, self._started_iso, s.plugin_version)
        ck.write(s.checkpoint_file, data)

    def _prepare(self):
        """Open or create the work file and bitmap. Returns the Mosaic."""
        s = self.spec
        os.makedirs(s.work_dir, exist_ok=True)
        data = ck.read(s.checkpoint_file)
        if data is not None and data.get('signature') == s.signature \
                and os.path.exists(s.work_file):
            self.bitmap = ck.load_bitmap(data)
            self._started_iso = data.get('started')
            return Mosaic.open(s.work_file, s.tile_range, s.source.tile_size)
        self.bitmap = ck.TileBitmap(s.total)
        return Mosaic.create(s.work_file, s.tile_range, s.zoom,
                             s.source.tile_size)

    # ---- progress ------------------------------------------------------

    def _progress(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_INTERVAL:
            return
        self._last_progress = now
        if self.on_progress is None:
            return
        done = self.bitmap.count()
        elapsed = max(1e-6, now - self._t0)
        rate = self._session_done / elapsed
        remaining = self.spec.total - done - 0
        eta = remaining / rate if rate > 0 else -1
        self.on_progress(done, self.spec.total, self.failed, rate, eta)

    # ---- the run -------------------------------------------------------

    def run(self):
        s = self.spec
        out = Outcome()
        self._t0 = time.monotonic()
        try:
            mosaic = self._prepare()
        except (OSError, RuntimeError, ValueError) as exc:
            out.message = 'Could not prepare the work file: %s' % exc
            return out

        try:
            if s.export_only:
                out.done = self.bitmap.count()
                out.failed = s.total - out.done
                out.state = 'complete' if out.failed == 0 else 'incomplete'
                mosaic.close()
                return out
            outcome = self._download(mosaic)
        except OSError as exc:
            outcome = Outcome('error', message='Could not write to the output '
                              'folder: %s' % exc)
            with suppress(Exception):
                self._save_checkpoint(mosaic)
        finally:
            with suppress(Exception):
                mosaic.close()
        return outcome

    def _download(self, mosaic):
        s = self.spec
        total = s.total
        tiles = list(tm.iter_tiles(s.tile_range))
        x_min, _, y_min, _ = s.tile_range
        todo = [i for i in range(total) if not self.bitmap.get(i)]
        self.failed = 0
        throttle = Throttle(s.workers, self.on_status)

        def stopped():
            return self.should_stop() or throttle.aborted

        def work(index):
            x, y = tiles[index]
            throttle.acquire(stopped)
            try:
                if stopped():
                    return index, 'skip', None
                status, payload = fetch_with_retries(
                    self.fetch, s.source.tile_url(x, y, s.zoom), throttle,
                    stopped, self.backoff)
                if status != 'ok':
                    return index, 'fail', payload
                try:
                    return index, 'ok', self.decode(payload, s.source.tile_size)
                except ValueError as exc:
                    return index, 'fail', str(exc)
            except Exception as exc:
                # A tile that blows up for an unforeseen reason is a failed
                # tile, not a reason to abandon thousands of good ones.
                return index, 'fail', str(exc)
            finally:
                throttle.release()

        def handle(fut):
            index, kind, payload = fut.result()
            if kind == 'ok':
                x, y = tiles[index]
                mosaic.write_tile(x - x_min, y - y_min, payload)
                self.bitmap.set(index)
                self._session_done += 1
                return 1
            if kind == 'fail':
                self.failed += 1
            return 0

        window = max(4, s.workers * 4)
        last_ckpt = time.monotonic()
        since_ckpt = 0
        pending = set()
        cursor = 0
        pool = ThreadPoolExecutor(max_workers=max(1, s.workers))
        try:
            while True:
                while cursor < len(todo) and len(pending) < window \
                        and not stopped():
                    pending.add(pool.submit(work, todo[cursor]))
                    cursor += 1
                if not pending:
                    break
                done, pending = wait(pending, timeout=0.25,
                                     return_when=FIRST_COMPLETED)
                for fut in done:
                    since_ckpt += handle(fut)
                self._progress()
                if since_ckpt and (since_ckpt >= CHECKPOINT_TILES or
                                   time.monotonic() - last_ckpt >=
                                   CHECKPOINT_SECONDS):
                    self._save_checkpoint(mosaic)
                    since_ckpt = 0
                    last_ckpt = time.monotonic()
                if stopped():
                    for fut in list(pending):
                        if fut.cancel():
                            pending.discard(fut)
            # Keep what the in-flight requests brought back.
            for fut in wait(pending)[0]:
                handle(fut)
        finally:
            pool.shutdown(wait=True)
        self._progress(force=True)
        self._save_checkpoint(mosaic)

        done_count = self.bitmap.count()
        out = Outcome(done=done_count, failed=total - done_count)
        if throttle.aborted:
            out.state = 'throttled'
            out.message = ('The server keeps refusing requests, so the '
                           'download was stopped. Progress is saved; try again '
                           'later.')
        elif self.should_stop():
            out.state = 'stopped'
        elif done_count == total:
            out.state = 'complete'
        else:
            out.state = 'incomplete'
            out.message = '%d tiles could not be downloaded.' % out.failed
        return out

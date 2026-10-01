# -*- coding: utf-8 -*-
"""QGIS side of the download: proxy-aware fetcher and the background task."""

import shutil
import traceback
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsBlockingNetworkRequest,
                       QgsMessageLog, QgsTask)
from qgis.PyQt.QtCore import QUrl, pyqtSignal

from ..net import user_agent
from . import checkpoint as ck
from . import exporter
from .job import TileJob, decode_tile

LOG_TAG = 'KGA Toolbox'
TIMEOUT_MS = 20000


def qgis_fetch(url):
    """GET one tile through QGIS, so the user's proxy and auth settings apply.

    Returns (status, body, error). status is None when nothing came back.
    """
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b'User-Agent', user_agent().encode('utf-8'))
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    # Tens of thousands of tiles must not fill the QGIS network cache.
    request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.AlwaysNetwork)
    request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute,
                         False)
    try:
        request.setTransferTimeout(TIMEOUT_MS)
    except AttributeError:                  # pragma: no cover - Qt < 5.15
        pass

    fetcher = QgsBlockingNetworkRequest()
    try:
        fetcher.setTimeout(TIMEOUT_MS)
    except AttributeError:                  # pragma: no cover - older QGIS
        pass
    fetcher.get(request, False)
    reply = fetcher.reply()
    status = None
    try:
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    content = reply.content()
    body = bytes(content) if content is not None else b''
    error = ''
    with suppress(Exception):               # pragma: no cover - defensive
        error = reply.errorString() or ''
    return status, body, error


def free_bytes(folder):
    """Free space at `folder`, or on the nearest existing parent."""
    import os
    path = folder
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return shutil.disk_usage(path or '.').free
    except OSError:
        return None


class ImageryTask(QgsTask):
    """Download every tile, then build the image. One job at a time."""

    progressInfo = pyqtSignal(int, int, int, float, float)
    statusText = pyqtSignal(str)
    phase = pyqtSignal(str, float)      # step text, progress of that step 0..1
    jobFinished = pyqtSignal(dict)

    def __init__(self, spec, fetch=qgis_fetch, decode=decode_tile, backoff=1.0):
        flag = getattr(QgsTask, 'Flag', QgsTask)     # scoped enum, QGIS 3.36+
        super().__init__('KGA imagery download: %s' % spec.name,
                         flag.CanCancel)
        self.spec = spec
        self._fetch = fetch
        self._decode = decode
        self._backoff = backoff
        self._stop = False
        self.result = {}
        self._last = None

    def request_stop(self):
        self._stop = True

    def should_stop(self):
        return self._stop or self.isCanceled()

    def _emit_progress(self, done, total, failed, rate, eta):
        self.setProgress(100.0 * done / max(1, total))
        self.progressInfo.emit(done, total, failed, rate, eta)

    def _export_progress(self, fraction, text):
        self.setProgress(100.0 * fraction)
        self.phase.emit(text, float(fraction))

    def run(self):
        spec = self.spec
        result = {'state': 'error', 'message': '', 'path': '', 'files': [],
                  'done': 0, 'failed': 0}
        try:
            job = TileJob(spec, self._fetch, self._decode, self.should_stop,
                          self._emit_progress, self.statusText.emit, self._backoff)
            outcome = job.run()
            result.update(state=outcome.state, message=outcome.message,
                          done=outcome.done, failed=outcome.failed)
            if outcome.state in ('complete', 'incomplete') and \
                    (outcome.state == 'complete' or spec.export_only):
                # export_only + incomplete means "finish anyway"
                self._build(result, has_failed=outcome.failed > 0)
            elif outcome.state == 'incomplete':
                pass                        # the dialog asks: retry / finish
        except Exception as exc:            # never let a task die silently
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            result.update(state='error', message=str(exc))
        finally:
            # 'throttled' tells the user their progress is saved, so the scratch
            # folder has to survive it just as it survives 'incomplete'.
            if not spec.checkpoint_enabled and \
                    result['state'] not in ('incomplete', 'throttled'):
                shutil.rmtree(spec.work_dir, ignore_errors=True)
        self.result = result
        return result['state'] != 'error'

    def _build(self, result, has_failed):
        spec = self.spec
        self.phase.emit('Building image...', 0.0)
        try:
            exported = exporter.export(spec, has_failed, self._export_progress,
                                       self.should_stop)
        except exporter.ExportStopped:
            result.update(state='stopped', message='Stopped while building the '
                          'image. Start again to resume from the saved tiles.')
            return
        except exporter.ExportError as exc:
            result.update(state='error', message=str(exc) + ' The downloaded '
                          'tiles are kept so the export can be retried.')
            return
        result.update(state='exported', path=exported.path,
                      files=exported.files, width=exported.width,
                      height=exported.height, pixel_size=exported.pixel_size)
        if not spec.keep_intermediate:
            ck.remove_job_files(spec.folder, spec.name)
            if not spec.checkpoint_enabled:
                shutil.rmtree(spec.work_dir, ignore_errors=True)

    def finished(self, _ok):
        self.jobFinished.emit(dict(self.result))

    def cancel(self):
        self._stop = True
        super().cancel()


def start(task):
    QgsApplication.taskManager().addTask(task)
    return task

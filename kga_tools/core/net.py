# -*- coding: utf-8 -*-
"""HTTP and the on-disk download cache.

This is the first networking code in the plugin, so it sets the conventions the
rest of the online tools should follow:

* **Everything goes through QGIS, not through urllib.** ``QgsBlockingNetworkRequest``
  and ``QgsFileDownloader`` pick up the proxy, SSL exception and authentication
  settings the user already configured in QGIS Options. A raw ``urlopen`` would
  work on a developer's laptop and fail behind every corporate proxy in the
  region.
* **A User-Agent is mandatory.** data.humdata.org answers 403 to a request with
  no recognisable agent string and 200 to the same request with one. Any source
  can decide to do this, so every request carries one.
* **Errors arrive as a sentence.** :class:`NetError` carries text a dialog can
  show as-is. Nothing here raises a traceback at the GUI.

Downloads are cached on disk under the QGIS profile directory. A boundary file
is a fixed published artefact, not a live feed: fetching Cambodia ADM2 twice
should cost one download, and the second one should work with the network
unplugged.
"""

import json
import os
import shutil
import time
from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsBlockingNetworkRequest,
                       QgsFileDownloader, QgsMessageLog)
from qgis.PyQt.QtCore import QEventLoop, QUrl

LOG_TAG = 'KGA Toolbox'

#: Seconds before a metadata request gives up. The API calls this module makes
#: are all small JSON documents; anything slower than this is a dead endpoint.
TIMEOUT = 30

#: Metadata is re-read after a week. Published boundaries change far more slowly
#: than that, but a stale download URL is worse than a slow one: geoBoundaries
#: pins a git commit hash into its file URLs, and those do move between releases.
METADATA_TTL = 7 * 24 * 3600

#: Stop the cache growing without limit. Natural Earth alone is 40 MB a file.
CACHE_CAP_BYTES = 500 * 1024 * 1024


class NetError(Exception):
    """A network failure with a message meant for a human.

    ``str(exc)`` is shown directly in the dialog, so it is written as a full
    sentence and never contains a URL fragment or a Qt enum name.
    """


#: Read once, from metadata.txt, on the first request.
_VERSION = None


def plugin_version():
    """The plugin version out of metadata.txt, so it cannot drift from it."""
    global _VERSION
    if _VERSION is not None:
        return _VERSION

    _VERSION = 'unknown'
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), 'metadata.txt')
    with suppress(Exception):               # pragma: no cover - defensive
        import configparser

        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding='utf-8')
        _VERSION = parser.get('general', 'version', fallback='unknown')
    return _VERSION


def user_agent():
    """The agent string every request sends.

    HDX rejects requests without one - verified, it is a 403, not a timeout -
    and identifying the plugin is the polite thing to do when hitting somebody
    else's free API.
    """
    try:
        qgis_version = Qgis.QGIS_VERSION.split('-')[0]
    except Exception:                       # pragma: no cover - defensive
        qgis_version = 'unknown'
    return 'KGAToolbox/{0} (QGIS/{1}; +https://khmergrs.com)'.format(
        plugin_version(), qgis_version)


def _request(url):
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b'User-Agent', user_agent().encode('utf-8'))
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
    return request


def _describe_error(reply, fallback):
    """Turn a QgsNetworkReplyContent into one sentence."""
    from qgis.PyQt.QtNetwork import QNetworkRequest

    status = None
    with suppress(Exception):               # pragma: no cover - defensive
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute)

    if status == 403:
        return ('The data source refused the request (HTTP 403). Some sources '
                'block automated requests; try again later or use a different '
                'source.')
    if status == 404:
        return ('The data source has nothing published at that address '
                '(HTTP 404).')
    if status and int(status) >= 500:
        return ('The data source is having trouble of its own '
                '(HTTP {0}). Try again in a few minutes.'.format(status))
    if status and int(status) >= 400:
        return 'The data source rejected the request (HTTP {0}).'.format(status)

    text = ''
    with suppress(Exception):               # pragma: no cover - defensive
        text = reply.errorString() or ''
    if 'Host' in text or 'host' in text or 'resolve' in text:
        return ('Could not reach the data source. Check the internet '
                'connection, or the proxy settings in QGIS Options.')
    return fallback


def get(url):
    """GET one URL and return the body as bytes.

    Blocking, but on a local event loop, so it is safe on the GUI thread - the
    same thing ``QgsBlockingNetworkRequest`` is built for. Everything this
    fetches is a small JSON document; large files go through :func:`download_to`.
    """
    fetcher = QgsBlockingNetworkRequest()
    try:
        fetcher.setTimeout(TIMEOUT * 1000)
    except AttributeError:                  # pragma: no cover - older QGIS
        pass

    code = fetcher.get(_request(url), False)
    reply = fetcher.reply()

    if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetError(_describe_error(
            reply, 'The request to the data source failed.'))

    content = reply.content()
    if content is None:
        raise NetError('The data source returned an empty response.')
    return bytes(content)


def fetch_body(url):
    """GET one URL and return ``(status, bytes)`` whatever the status was.

    For the APIs that explain a refusal in the body of the error reply -
    OpenTopography answers a bad key with HTTP 400 and an XML sentence - where
    :func:`get` would only have the status code to go on. Raises
    :class:`NetError` only when nothing came back at all.
    """
    from qgis.PyQt.QtNetwork import QNetworkRequest

    fetcher = QgsBlockingNetworkRequest()
    try:
        fetcher.setTimeout(TIMEOUT * 1000)
    except AttributeError:                  # pragma: no cover - older QGIS
        pass
    fetcher.get(_request(url), False)
    reply = fetcher.reply()
    content = reply.content()
    status = None
    with suppress(Exception):               # pragma: no cover - defensive
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if status is None and not content:
        raise NetError(_describe_error(
            reply, 'The request to the data source failed.'))
    return status, bytes(content or b'')


def get_json(url):
    """GET one URL and parse it as JSON."""
    raw = get(url)
    try:
        return json.loads(raw.decode('utf-8'))
    except ValueError:
        raise NetError('The data source answered with something that is not '
                       'valid JSON. It may be down for maintenance.')
    except UnicodeDecodeError:              # pragma: no cover - defensive
        raise NetError('The data source answered with text this tool cannot '
                       'read.')


def download_to(url, dest, on_progress=None, parent=None):
    """Download `url` to `dest`, driving `on_progress(received, total)`.

    ``QgsFileDownloader`` is used rather than a plain GET because it is the one
    QGIS class that already handles the three things that break downloads here:
    it follows redirects (geoBoundaries hands out URLs that redirect to GitHub),
    it honours the proxy, and it can be cancelled mid-flight.

    `on_progress` may return ``False`` to cancel. The partial file is removed on
    both failure and cancellation, so a cancelled download never poisons the
    cache with a truncated GeoJSON that OGR would later half-open.

    Returns ``True`` when the file arrived, ``False`` when the user cancelled.
    """
    part = dest + '.part'
    _ensure_dir(os.path.dirname(dest))
    _remove(part)

    state = {'error': None, 'done': False, 'cancelled': False}
    loop = QEventLoop()
    downloader = QgsFileDownloader(QUrl(url), part, '', True)

    def finished():
        state['done'] = True
        loop.quit()

    def failed(messages):
        state['error'] = ' '.join(messages) if messages else ''
        loop.quit()

    def cancelled():
        state['cancelled'] = True
        loop.quit()

    def progress(received, total):
        if on_progress is not None and on_progress(received, total) is False:
            downloader.cancelDownload()

    downloader.downloadCompleted.connect(finished)
    downloader.downloadError.connect(failed)
    downloader.downloadCanceled.connect(cancelled)
    downloader.downloadProgress.connect(progress)
    downloader.startDownload()
    loop.exec()

    if state['cancelled']:
        _remove(part)
        return False
    if state['error'] is not None or not os.path.exists(part):
        _remove(part)
        raise NetError(_download_message(state['error']))
    if os.path.getsize(part) == 0:
        _remove(part)
        raise NetError('The data source returned an empty file.')

    _remove(dest)
    os.rename(part, dest)
    return True


def _download_message(detail):
    detail = (detail or '').strip()
    lowered = detail.lower()
    if '403' in lowered or 'forbidden' in lowered:
        return ('The data source refused the download (HTTP 403). Some sources '
                'block automated requests; try again later or use a different '
                'source.')
    if '404' in lowered or 'not found' in lowered:
        return 'The file is no longer published at that address (HTTP 404).'
    if 'host' in lowered or 'resolve' in lowered or 'unreachable' in lowered:
        return ('Could not reach the data source. Check the internet '
                'connection, or the proxy settings in QGIS Options.')
    if detail:
        return 'The download failed: {0}'.format(detail)
    return 'The download failed.'


# --------------------------------------------------------------------- cache

def cache_dir(*parts):
    """A directory inside the QGIS profile, created on demand.

    Under the profile rather than the system temp folder so that a boundary
    downloaded today is still there next week - which is the whole point - and
    so that removing the profile removes the cache with it.
    """
    root = os.path.join(QgsApplication.qgisSettingsDirPath(),
                        'kga_tools', 'data_cache')
    path = os.path.join(root, *parts) if parts else root
    _ensure_dir(path)
    return path


def cached_path(source_id, key, extension):
    """Where one downloaded artefact lives. Does not fetch anything."""
    return os.path.join(cache_dir(source_id), '{0}{1}'.format(
        _safe(key), extension))


def is_fresh(path, ttl=None):
    """True when `path` exists and, if `ttl` is given, is younger than it."""
    if not path or not os.path.exists(path):
        return False
    if os.path.getsize(path) == 0:
        return False
    if ttl is None:
        return True
    return (time.time() - os.path.getmtime(path)) < ttl


def read_cached_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, payload):
    """Write `payload` as JSON so that `path` is whole or not there at all.

    The file is written beside the target and moved into place. ``is_fresh``
    takes any non-empty file for a good one, so a write cut short by a crash or
    a full disk must never be left where the cache would trust it.
    """
    _ensure_dir(os.path.dirname(path))
    part = path + '.part'
    try:
        with open(part, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle)
        os.replace(part, path)
    except OSError:
        _remove(part)
        raise


def write_cached_json(path, payload):
    try:
        write_json_atomic(path, payload)
    except OSError as exc:                  # pragma: no cover - disk full etc
        QgsMessageLog.logMessage(
            'Could not write the data cache: {0}'.format(exc), LOG_TAG,
            Qgis.MessageLevel.Warning)


def cache_size():
    """Total bytes held in the cache."""
    total = 0
    for folder, _dirs, files in os.walk(cache_dir()):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:                 # pragma: no cover - vanished file
                pass
    return total


def clear_cache():
    """Empty the cache. Returns the number of bytes reclaimed."""
    freed = cache_size()
    root = cache_dir()
    for entry in os.listdir(root):
        path = os.path.join(root, entry)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:                     # pragma: no cover - locked file
            pass
    return freed


def trim_cache(cap=CACHE_CAP_BYTES):
    """Drop the least recently used files until the cache fits under `cap`.

    Called after every successful download. Without it one afternoon of pulling
    Natural Earth at 10m would quietly eat a gigabyte of the user's profile.
    """
    entries = []
    for folder, _dirs, files in os.walk(cache_dir()):
        for name in files:
            path = os.path.join(folder, name)
            try:
                entries.append((os.path.getatime(path),
                                os.path.getsize(path), path))
            except OSError:                 # pragma: no cover - vanished file
                pass

    total = sum(size for _atime, size, _path in entries)
    if total <= cap:
        return 0

    freed = 0
    for _atime, size, path in sorted(entries):
        if total - freed <= cap:
            break
        try:
            os.remove(path)
            freed += size
        except OSError:                     # pragma: no cover - locked file
            pass
    return freed


def human_size(num_bytes):
    """1383743 -> '1.3 MB'. Used in the details panel before a download."""
    if not num_bytes:
        return 'unknown size'
    value = float(num_bytes)
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            if unit == 'bytes':
                return '{0:.0f} bytes'.format(value)
            return '{0:.1f} {1}'.format(value, unit)
        value /= 1024
    return '{0:.1f} GB'.format(value)       # pragma: no cover - unreachable


# ------------------------------------------------------------------- helpers

def _safe(text):
    """A filename that survives every platform, from arbitrary key text."""
    keep = []
    for char in str(text):
        keep.append(char if (char.isalnum() or char in '-_.') else '_')
    return ''.join(keep)[:120] or 'item'


def _ensure_dir(path):
    if path and not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:                     # pragma: no cover - race
            pass


def _remove(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:                     # pragma: no cover - locked file
            pass

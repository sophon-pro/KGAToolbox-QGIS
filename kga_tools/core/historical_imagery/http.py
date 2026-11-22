# -*- coding: utf-8 -*-
"""
Small blocking HTTP client meant to be called from background QgsTasks.

Inside QGIS it uses QgsBlockingNetworkRequest, which honours the user's proxy,
SSL and authentication settings from Settings ▸ Options ▸ Network.
"""

import json
import urllib.parse

USER_AGENT = "KGAToolbox-HistoricalImagery/1.1"
DEFAULT_TIMEOUT = 60  # seconds


class HttpError(Exception):
    """HTTP or network failure with a status code (0 = network/transport error)."""

    def __init__(self, status, message, url="", bad_response=False):
        super().__init__(message)
        self.status = int(status or 0)
        self.message = message
        self.url = url
        self.bad_response = bad_response  # server answered, but not with what we expected

    @property
    def is_rate_limited(self):
        return self.status == 429

    @property
    def is_auth_error(self):
        return self.status in (401, 403)

    def __str__(self):
        if self.status:
            return "HTTP {} – {}".format(self.status, self.message)
        return self.message


def _error_text(body, fallback):
    """Extract a readable message from an API error body."""
    if not body:
        return fallback
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, dict):
            for key in ("description", "detail", "message", "error"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, dict) and val.get("message"):
                    return str(val["message"])
    except (ValueError, UnicodeDecodeError):
        pass
    text = body.decode("utf-8", "replace").strip()
    return text[:300] if text else fallback


def _qgis_request(method, url, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QByteArray, QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"User-Agent", USER_AGENT.encode())
    for key, value in (headers or {}).items():
        request.setRawHeader(key.encode(), value.encode())
    try:
        request.setTransferTimeout(int(timeout * 1000))  # Qt >= 5.15
    except AttributeError:
        pass

    blocking = QgsBlockingNetworkRequest()
    if method == "POST":
        err = blocking.post(request, QByteArray(data or b""), True)
    else:
        err = blocking.get(request, True)

    reply = blocking.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    try:
        status = int(status) if status is not None else 0
    except (TypeError, ValueError):
        status = 0
    body = bytes(reply.content())

    if status >= 400:
        raise HttpError(status, _error_text(body, blocking.errorMessage()), url)
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise HttpError(status, blocking.errorMessage() or "Network error", url)
    return body


def request(method, url, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    return _qgis_request(method, url, data, headers, timeout)


def get_bytes(url, headers=None, timeout=DEFAULT_TIMEOUT):
    return request("GET", url, headers=headers, timeout=timeout)


def get_json(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    body = request("GET", url, headers=hdrs, timeout=timeout)
    return _parse_json(body, url)


def post_json(url, payload, headers=None, timeout=DEFAULT_TIMEOUT):
    hdrs = {"Content-Type": "application/json", "Accept": "application/geo+json, application/json"}
    hdrs.update(headers or {})
    body = request("POST", url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, timeout=timeout)
    return _parse_json(body, url)


def _parse_json(body, url):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        snippet = body[:120].decode("utf-8", "replace").strip().replace("\n", " ")
        raise HttpError(200, "The server did not return valid JSON (got: {}).".format(snippet), url,
                        bad_response=True)


def s3_to_https(href):
    """Convert s3://bucket/key to a public HTTPS URL (works for open-data buckets)."""
    if href and href.startswith("s3://"):
        bucket, _, key = href[5:].partition("/")
        return "https://{}.s3.amazonaws.com/{}".format(bucket, key)
    return href


def strip_query(url):
    """Remove the query string (e.g. SAS tokens) before writing URLs to metadata."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

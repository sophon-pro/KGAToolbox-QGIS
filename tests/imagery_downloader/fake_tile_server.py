# -*- coding: utf-8 -*-
"""A local XYZ tile server for tests. No third-party imports.

Tiles are 256x256 PNGs whose pixels are a pure function of (z, x, y), so any
two runs against it must produce identical mosaics. Fault switches:

    server.delay      seconds to sleep before answering
    server.fail_429   answer 429 to the next N requests
    server.fail_500   answer 500 to every Nth request (0 = never)
    server.drop_every drop the connection on every Nth request (0 = never)
    server.always     force one status for every request (e.g. 403)
"""

import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


def tile_array(z, x, y, size=256):
    """(size, size, 3) uint8, deterministic and unique per tile."""
    yy, xx = np.mgrid[0:size, 0:size]
    r = (x * 37 + xx // 2) % 256
    g = (y * 53 + yy // 2) % 256
    b = (z * 11 + (xx + yy) // 4) % 256
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def png_bytes(arr):
    h, w, _ = arr.shape
    raw = b''.join(b'\x00' + arr[row].tobytes() for row in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack(
            '>I', zlib.crc32(body) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 1)) + chunk(b'IEND', b''))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server
        with srv.lock:
            srv.requests += 1
            n = srv.requests
            fail429 = srv.fail_429 > 0
            if fail429:
                srv.fail_429 -= 1
        if srv.delay:
            time.sleep(srv.delay)
        if srv.drop_every and n % srv.drop_every == 0:
            self.connection.close()
            return
        status = srv.always
        if status is None and fail429:
            status = 429
        if status is None and srv.fail_500 and n % srv.fail_500 == 0:
            status = 500
        if status is not None:
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        try:
            z, x, y = [int(p) for p in
                       self.path.split('?')[0].strip('/').split('.')[0]
                       .split('/')]
        except ValueError:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = png_bytes(tile_array(z, x, y))
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeTileServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.httpd.lock = threading.Lock()
        self.httpd.requests = 0
        self.httpd.delay = 0
        self.httpd.fail_429 = 0
        self.httpd.fail_500 = 0
        self.httpd.drop_every = 0
        self.httpd.always = None
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __getattr__(self, name):
        if name in ('requests', 'delay', 'fail_429', 'fail_500', 'drop_every',
                    'always'):
            return getattr(self.httpd, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ('requests', 'delay', 'fail_429', 'fail_500', 'drop_every',
                    'always'):
            setattr(self.httpd, name, value)
        else:
            object.__setattr__(self, name, value)

    @property
    def url_template(self):
        return 'http://127.0.0.1:%d/{z}/{x}/{y}.png' % \
            self.httpd.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

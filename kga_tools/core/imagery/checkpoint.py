# -*- coding: utf-8 -*-
"""Job signature, checkpoint file and the done-tile bitmap. Pure Python + numpy."""

import base64
import hashlib
import json
import os
from datetime import datetime

import numpy as np

VERSION = 1
SUFFIX = '.kga_checkpoint.json'


def checkpoint_path(folder, name):
    return os.path.join(folder, name + SUFFIX)


def work_path(folder, name):
    return os.path.join(folder, name + '_work.tif')


def hash_text(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]


def signature(source_id, zoom, tile_range, output_crs, mask_hash, fmt):
    raw = '|'.join([source_id, str(zoom),
                    ','.join(str(v) for v in tile_range),
                    output_crs, mask_hash or '', fmt])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class TileBitmap:
    """One bit per tile, row-major."""

    def __init__(self, total, bits=None):
        self.total = total
        self._a = np.zeros(total, dtype=bool) if bits is None else bits

    def set(self, index):
        self._a[index] = True

    def get(self, index):
        return bool(self._a[index])

    def count(self):
        return int(self._a.sum())

    def encode(self):
        return base64.b64encode(np.packbits(self._a).tobytes()).decode('ascii')

    @classmethod
    def decode(cls, text, total):
        raw = np.frombuffer(base64.b64decode(text), dtype=np.uint8)
        bits = np.unpackbits(raw)[:total].astype(bool)
        if bits.size != total:
            raise ValueError('bitmap size mismatch')
        return cls(total, bits)


def _now():
    return datetime.now().isoformat(timespec='seconds')


def build(sig, source_id, zoom, tile_range, bbox, mask_hash, output_crs, fmt,
          total, bitmap, failed, started, plugin_version):
    return {
        'version': VERSION,
        'signature': sig,
        'source_id': source_id,
        'zoom': zoom,
        'tile_range': list(tile_range),
        'bbox_4326': list(bbox),
        'mask_wkt_hash': mask_hash,
        'output_crs': output_crs,
        'format': fmt,
        'total_tiles': total,
        'done_bitmap_b64': bitmap.encode(),
        'failed_count': failed,
        'started': started or _now(),
        'updated': _now(),
        'plugin_version': plugin_version,
    }


def write(path, data):
    """Atomically replace the checkpoint file."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read(path):
    """Return the checkpoint dict, or None when absent. Raises ValueError if bad."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if data.get('version') != VERSION:
            raise ValueError('unsupported checkpoint version')
        for key in ('signature', 'total_tiles', 'done_bitmap_b64',
                    'tile_range'):
            if key not in data:
                raise ValueError('checkpoint is missing ' + key)
        return data
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError('The checkpoint file is unreadable: {}'.format(exc))


def load_bitmap(data):
    return TileBitmap.decode(data['done_bitmap_b64'], int(data['total_tiles']))


def describe_differences(data, source_id, zoom, tile_range, output_crs,
                         mask_hash, fmt):
    diffs = []
    if data.get('source_id') != source_id:
        diffs.append('imagery source')
    if data.get('zoom') != zoom:
        diffs.append('zoom level')
    if list(data.get('tile_range', [])) != list(tile_range):
        diffs.append('extent')
    if data.get('mask_wkt_hash') != mask_hash:
        diffs.append('clip polygon')
    if data.get('output_crs') != output_crs:
        diffs.append('output CRS')
    if data.get('format') != fmt:
        diffs.append('format')
    return diffs


def remove_job_files(folder, name):
    ckpt = checkpoint_path(folder, name)
    for path in (ckpt, ckpt + '.tmp', work_path(folder, name)):
        try:
            os.remove(path)
        except OSError:
            pass

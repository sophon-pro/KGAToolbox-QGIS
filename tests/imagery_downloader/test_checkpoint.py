# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..')))

from kga_tools.core.imagery import checkpoint as ck  # noqa: E402


class CheckpointTests(unittest.TestCase):
    def test_bitmap_round_trip(self):
        for total in (1, 7, 8, 9, 1000):
            b = ck.TileBitmap(total)
            for i in range(0, total, 3):
                b.set(i)
            back = ck.TileBitmap.decode(b.encode(), total)
            self.assertEqual(back.count(), b.count())
            self.assertTrue(all(back.get(i) == b.get(i) for i in range(total)))

    def test_signature_changes(self):
        base = ck.signature('s', 10, (1, 2, 3, 4), 'EPSG:3857', 'h', 'tif')
        self.assertEqual(base, ck.signature('s', 10, (1, 2, 3, 4),
                                            'EPSG:3857', 'h', 'tif'))
        for other in (ck.signature('t', 10, (1, 2, 3, 4), 'EPSG:3857', 'h', 'tif'),
                      ck.signature('s', 11, (1, 2, 3, 4), 'EPSG:3857', 'h', 'tif'),
                      ck.signature('s', 10, (1, 2, 3, 5), 'EPSG:3857', 'h', 'tif'),
                      ck.signature('s', 10, (1, 2, 3, 4), 'EPSG:4326', 'h', 'tif'),
                      ck.signature('s', 10, (1, 2, 3, 4), 'EPSG:3857', 'x', 'tif'),
                      ck.signature('s', 10, (1, 2, 3, 4), 'EPSG:3857', 'h', 'png')):
            self.assertNotEqual(base, other)

    def test_atomic_write_and_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = ck.checkpoint_path(d, 'ខ្មែរ name')
            b = ck.TileBitmap(20)
            b.set(3)
            data = ck.build('sig', 'src', 5, (0, 1, 0, 1), (0, 0, 1, 1), '',
                            'EPSG:3857', 'tif', 20, b, 0, None, '0.2.0')
            ck.write(path, data)
            got = ck.read(path)
            self.assertEqual(got['signature'], 'sig')
            self.assertEqual(ck.load_bitmap(got).count(), 1)
            self.assertFalse(os.path.exists(path + '.tmp'))

    def test_corrupt_checkpoint_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = ck.checkpoint_path(d, 'x')
            with open(path, 'w') as fh:
                fh.write('{not json')
            with self.assertRaises(ValueError):
                ck.read(path)
            self.assertIsNone(ck.read(ck.checkpoint_path(d, 'absent')))

    def test_differences(self):
        data = {'source_id': 'a', 'zoom': 5, 'tile_range': [0, 1, 0, 1],
                'mask_wkt_hash': '', 'output_crs': 'EPSG:3857', 'format': 'tif'}
        self.assertEqual(ck.describe_differences(
            data, 'a', 6, (0, 1, 0, 1), 'EPSG:4326', '', 'tif'),
            ['zoom level', 'output CRS'])


if __name__ == '__main__':
    unittest.main()

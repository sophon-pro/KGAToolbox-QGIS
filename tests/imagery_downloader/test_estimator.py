# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..')))

from kga_tools.core.imagery import estimator as est  # noqa: E402
from kga_tools.core.imagery import tile_math as tm  # noqa: E402
from kga_tools.core.imagery.sources import SOURCES  # noqa: E402

SRC = SOURCES[0]


class EstimatorTests(unittest.TestCase):
    def test_counts_match_independent_calc(self):
        bbox = (104.90, 11.54, 104.95, 11.58)
        e = est.estimate(bbox, 17, SRC)
        # independent: fractional tile coordinates
        import math
        n = 2 ** 17
        fx0 = (bbox[0] + 180) / 360 * n
        fx1 = (bbox[2] + 180) / 360 * n

        def fy(lat):
            r = math.radians(lat)
            return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n
        cols = math.floor(fx1) - math.floor(fx0) + 1
        rows = math.floor(fy(bbox[1])) - math.floor(fy(bbox[3])) + 1
        self.assertEqual((e.cols, e.rows, e.tiles), (cols, rows, cols * rows))
        self.assertTrue(e.valid)
        self.assertAlmostEqual(e.native_w, (fx1 - fx0) * 256, delta=2)

    def test_invalid_extents(self):
        for bad in (None, (1, 1, 1, 1), (10, 5, 5, 6), (170, 0, 190, 5),
                    (float('nan'), 0, 1, 1)):
            e = est.estimate(bad, 10, SRC)
            self.assertFalse(e.valid, bad)
            self.assertTrue(e.blockers)

    def test_tile_ceiling_blocks(self):
        e = est.estimate((100, 5, 110, 20), 18, SRC)
        self.assertFalse(e.valid)

    def test_jpg_dimension_limit(self):
        e = est.estimate((104.0, 11.0, 105.5, 12.5), 19, SRC, fmt='jpg')
        self.assertFalse(e.valid)

    def test_reprojected_grid(self):
        bbox = (104.90, 11.54, 104.95, 11.58)

        def tb(b):
            return (b[0] * 111320, b[1] * 110574, b[2] * 111320, b[3] * 110574)
        e = est.estimate(bbox, 17, SRC, transform_bounds=tb)
        self.assertTrue(any('reproject' in w for w in e.warnings))
        self.assertGreater(e.out_w, 0)

    def test_area(self):
        a = est.spherical_area_km2((0, 0, 1, 1))
        self.assertAlmostEqual(a, 12364, delta=20)

    def test_clamped_latitude(self):
        e = est.estimate((0, 80, 10, 90), 6, SRC)
        self.assertTrue(e.valid)
        self.assertAlmostEqual(e.bbox[3], tm.MAX_LAT)


if __name__ == '__main__':
    unittest.main()

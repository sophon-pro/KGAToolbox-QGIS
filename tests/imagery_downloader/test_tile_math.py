# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..')))

from kga_tools.core.imagery import tile_math as tm  # noqa: E402


class TileMathTests(unittest.TestCase):
    def test_zoom0_is_one_tile(self):
        self.assertEqual(tm.tile_range_for_bbox((-180, -85, 180, 85), 0),
                         (0, 0, 0, 0))

    def test_origin_at_z1(self):
        self.assertEqual(tm.lonlat_to_tile(0.001, -0.001, 1), (1, 1))
        self.assertEqual(tm.lonlat_to_tile(-0.001, 0.001, 1), (0, 0))

    def test_known_tile(self):
        # Phnom Penh at z12: x = (104.9282+180)/360*4096 = 3241.9
        self.assertEqual(tm.lonlat_to_tile(104.9282, 11.5564, 12), (3241, 1915))

    def test_round_trip(self):
        for lon, lat in [(0, 0), (104.9, 11.55), (-73.9, 40.7), (179, -60)]:
            x, y = tm.lonlat_to_mercator(lon, lat)
            lo, la = tm.mercator_to_lonlat(x, y)
            self.assertAlmostEqual(lon, lo, 9)
            self.assertAlmostEqual(lat, la, 9)

    def test_clamp_lat(self):
        self.assertAlmostEqual(tm.clamp_lat(90), tm.MAX_LAT)
        self.assertEqual(tm.tile_range_for_bbox((0, 80, 10, 90), 3)[2], 0)

    def test_edges_of_world(self):
        n = 2 ** 5
        r = tm.tile_range_for_bbox((-180, -90, 180, 90), 5)
        self.assertEqual(r, (0, n - 1, 0, n - 1))

    def test_boundary_does_not_pull_neighbour(self):
        b = tm.tile_bounds_3857(5, 5, 4)
        w, s = tm.mercator_to_lonlat(b[0], b[1])
        e, n = tm.mercator_to_lonlat(b[2], b[3])
        self.assertEqual(tm.tile_range_for_bbox((w, s, e, n), 4), (5, 5, 5, 5))

    def test_geotransform_and_size(self):
        r = (3220, 3222, 1948, 1949)
        self.assertEqual(tm.mosaic_size(r), (768, 512))
        gt = tm.mosaic_geotransform(r, 12)
        b = tm.tile_bounds_3857(3220, 1948, 12)
        self.assertAlmostEqual(gt[0], b[0], 6)
        self.assertAlmostEqual(gt[3], b[3], 6)
        self.assertAlmostEqual(gt[1], tm.resolution(12), 9)

    def test_crop_window_inside_mosaic(self):
        bbox = (104.90, 11.54, 104.95, 11.58)
        r = tm.tile_range_for_bbox(bbox, 14)
        xo, yo, w, h = tm.crop_window(bbox, r, 14)
        mw, mh = tm.mosaic_size(r)
        self.assertTrue(0 <= xo and xo + w <= mw and 0 <= yo and yo + h <= mh)
        self.assertTrue(w > 0 and h > 0)

    def test_ground_resolution(self):
        self.assertAlmostEqual(tm.ground_resolution(18, 0), 0.5971642834779395,
                               6)

    def test_count_matches_iter(self):
        r = (3, 6, 2, 4)
        self.assertEqual(tm.tile_count(r), len(list(tm.iter_tiles(r))))


if __name__ == '__main__':
    unittest.main()

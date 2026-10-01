# -*- coding: utf-8 -*-
"""Raster Format Converter: blocks, resume, cancel and format limits.

Run with the QGIS interpreter, e.g. from PowerShell:

    $env:QT_QPA_PLATFORM='offscreen'
    & "C:\\Program Files\\QGIS 3.44.12\\bin\\python-qgis-ltr.bat" tests\\raster_format_converter\\test_converter.py
"""

import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsApplication, QgsProcessingContext,  # noqa: E402
                       QgsProcessingException, QgsProcessingFeedback,
                       QgsProject)

_app = QgsApplication([], True)
_app.initQgis()

from kga_tools.algorithms import raster_format_converter as R  # noqa: E402


class Feedback(QgsProcessingFeedback):
    """Cancels itself when a log line it is watching for goes past."""

    def __init__(self, cancel_on=None):
        super().__init__()
        self.cancel_on = cancel_on
        self.cancelled = False
        self.log = []

    def pushInfo(self, text):
        self.log.append(text)
        if self.cancel_on and self.cancel_on in text:
            self.cancelled = True

    def pushWarning(self, text):
        self.log.append('WARN ' + text)

    def isCanceled(self):
        return self.cancelled


def make_source(path, width=2600, height=2100, bands=3, georef=True):
    ds = gdal.GetDriverByName('GTiff').Create(
        path, width, height, bands, gdal.GDT_Byte,
        ['TILED=YES', 'COMPRESS=DEFLATE'])
    if georef:
        ds.SetGeoTransform((500000, 0.5, 0, 1300000, 0, -0.5))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32648)
        ds.SetProjection(srs.ExportToWkt())
    rng = np.random.default_rng(3)
    for band in range(bands):
        ds.GetRasterBand(band + 1).WriteArray(
            rng.integers(0, 255, (height, width)).astype(np.uint8))
    ds.FlushCache()
    ds = None


def same_pixels(left, right):
    a, b = gdal.Open(left), gdal.Open(right)
    if (a.RasterXSize, a.RasterYSize, a.RasterCount) != \
            (b.RasterXSize, b.RasterYSize, b.RasterCount):
        return False
    if a.GetGeoTransform() != b.GetGeoTransform():
        return False
    return all(np.array_equal(a.GetRasterBand(i + 1).ReadAsArray(),
                              b.GetRasterBand(i + 1).ReadAsArray())
               for i in range(a.RasterCount))


class ConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gdal.UseExceptions()
        cls.folder = tempfile.mkdtemp(prefix='kga_rfc_')
        cls.source = os.path.join(cls.folder, 'src.tif')
        make_source(cls.source)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.folder, name)

    def run_alg(self, source, output, fmt=0, block=512, resume=True,
                feedback=None):
        alg = R.RasterFormatConverter()
        alg.initAlgorithm({})
        params = {'INPUT': source, 'FORMAT': fmt, 'OUTPUT': output,
                  'COMPRESSION': 0, 'QUALITY': 75, 'OVERVIEWS': True,
                  'LOAD': False, 'BLOCK_SIZE': block, 'THREADS': 1,
                  'RESUME': resume}
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = feedback or Feedback()
        ok, message = alg.checkParameterValues(params, context)
        if not ok:
            return None, feedback, message
        return alg.processAlgorithm(params, context, feedback), feedback, ''

    def test_tiled_conversion_is_pixel_exact_and_cleans_up(self):
        out = self.path('exact.tif')
        result, _fb, message = self.run_alg(self.source, out)
        self.assertIsNotNone(result, message)
        self.assertTrue(same_pixels(self.source, out))
        self.assertFalse(os.path.exists(out + '_blocks'))
        self.assertFalse(os.path.exists(out + '.checkpoint.json'))
        ds = gdal.Open(out)
        self.assertGreater(ds.GetRasterBand(1).GetOverviewCount(), 0)
        ds = None

    def test_cancel_between_blocks_then_resume(self):
        out = self.path('resume.tif')
        result, _fb, _m = self.run_alg(
            self.source, out, feedback=Feedback(cancel_on='  block 6/'))
        self.assertEqual(result['BLOCKS_DONE'], 0)
        self.assertFalse(os.path.exists(out))
        self.assertTrue(os.path.exists(out + '.checkpoint.json'))
        _r, fb, _m = self.run_alg(self.source, out)
        self.assertTrue(any('Resuming: 6 of' in line for line in fb.log))
        self.assertTrue(same_pixels(self.source, out))

    def test_cancel_during_merge_is_graceful_and_resumable(self):
        # With exceptions on, GDAL raises when the progress callback aborts;
        # that used to escape as a RuntimeError instead of the documented
        # "blocks kept" message, leaving a half-written output behind.
        out = self.path('merge.tif')
        result, fb, _m = self.run_alg(
            self.source, out, feedback=Feedback(cancel_on='Merging'))
        self.assertIsNotNone(result)
        self.assertTrue(any('Cancelled while merging' in line
                            for line in fb.log))
        self.assertFalse(os.path.exists(out), 'half-written output kept')
        self.assertTrue(os.path.isdir(out + '_blocks'), 'blocks were deleted')
        self.assertTrue(os.path.exists(out + '.checkpoint.json'))
        self.run_alg(self.source, out)
        self.assertTrue(same_pixels(self.source, out))

    def test_cancel_in_one_pass_removes_the_unfinished_file(self):
        small = self.path('small.tif')
        make_source(small, 300, 300)
        out = self.path('onepass.tif')
        with self.assertRaises(QgsProcessingException):
            self.run_alg(small, out, block=4096,
                         feedback=Feedback(cancel_on='fits in one block'))
        self.assertFalse(os.path.exists(out))

    def test_block_size_change_starts_over(self):
        out = self.path('restart.tif')
        self.run_alg(self.source, out,
                     feedback=Feedback(cancel_on='  block 4/'))
        _r, fb, _m = self.run_alg(self.source, out, block=1024)
        self.assertTrue(any('starts over' in line for line in fb.log))
        self.assertTrue(same_pixels(self.source, out))

    def test_format_limits_and_extension(self):
        two_band = self.path('two.tif')
        make_source(two_band, 700, 600, bands=2)
        result, _fb, message = self.run_alg(two_band, self.path('a.jpg'),
                                            fmt=5)
        self.assertIsNone(result)
        self.assertIn('2 bands', message)
        result, _fb, message = self.run_alg(self.source, self.source)
        self.assertIsNone(result)
        small = self.path('small2.tif')
        make_source(small, 300, 300)
        result, _fb, _m = self.run_alg(small, self.path('b.dat'), block=4096)
        self.assertTrue(result['OUTPUT'].endswith('.tif'))


if __name__ == '__main__':
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""Engine + exporter tests against the local fake tile server.

Run with the QGIS Python:  python-qgis-ltr.bat tests\\imagery_downloader\\test_job.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))
sys.path.insert(0, HERE)

from qgis.core import QgsApplication, QgsRasterLayer  # noqa: E402

_app = QgsApplication([], False)
_app.initQgis()

from osgeo import gdal  # noqa: E402

from fake_tile_server import FakeTileServer, tile_array  # noqa: E402
from kga_tools.core.imagery import checkpoint as ck  # noqa: E402
from kga_tools.core.imagery import exporter  # noqa: E402
from kga_tools.core.imagery import tile_math as tm  # noqa: E402
from kga_tools.core.imagery.downloader import qgis_fetch  # noqa: E402
from kga_tools.core.imagery.job import JobSpec, TileJob  # noqa: E402
from kga_tools.core.imagery.sources import ImagerySource  # noqa: E402

BBOX = (104.9100, 11.5500, 104.9300, 11.5700)
ZOOM = 15


def urllib_fetch(url):
    try:
        with urlopen(url, timeout=5) as r:
            return r.status, r.read(), ''
    except HTTPError as exc:
        return exc.code, b'', str(exc)
    except Exception as exc:
        return None, b'', str(exc)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeTileServer().start()
        cls.source = ImagerySource(
            id='fake', name='Fake', url_template=cls.server.url_template,
            max_zoom=22)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kga_img_')
        s = self.server
        s.delay, s.fail_429, s.fail_500, s.drop_every, s.always = 0, 0, 0, 0, None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spec(self, name='job', **kw):
        params = dict(source=self.source, zoom=ZOOM,
                      tile_range=tm.tile_range_for_bbox(BBOX, ZOOM), bbox=BBOX,
                      output_crs='EPSG:3857', fmt='tif', folder=self.tmp,
                      name=name, workers=4, pyramids=False)
        params.update(kw)
        return JobSpec(**params)

    def run_job(self, spec, fetch=urllib_fetch, stop_after=None):
        state = {'stop': False}

        def progress(done, total, failed, rate, eta):
            if stop_after is not None and done >= stop_after:
                state['stop'] = True

        job = TileJob(spec, fetch, should_stop=lambda: state['stop'],
                      on_progress=progress, backoff=0.01)
        return job.run()

    def export(self, spec, failed=False):
        return exporter.export(spec, failed, lambda f, t: None, lambda: False)

    @staticmethod
    def read_all(path):
        ds = gdal.Open(path)
        arr = ds.ReadAsArray()
        ds = None
        return arr


class EngineTests(Base):
    def test_straight_run_pixels_and_georef(self):
        spec = self.spec()
        out = self.run_job(spec)
        self.assertEqual(out.state, 'complete')
        res = self.export(spec)
        ds = gdal.Open(res.path)
        self.assertIn('3857', ds.GetProjection())
        xo, yo, w, h = tm.crop_window(BBOX, spec.tile_range, ZOOM)
        self.assertEqual((ds.RasterXSize, ds.RasterYSize), (w, h))
        # pixel (10, 10) of the output lies in a known tile
        x_min, _, y_min, _ = spec.tile_range
        px, py = xo + 10, yo + 10
        tx, ty = x_min + px // 256, y_min + py // 256
        expect = tile_array(ZOOM, tx, ty)[py % 256, px % 256]
        got = ds.ReadAsArray(10, 10, 1, 1)[:3, 0, 0]
        self.assertEqual(list(got), list(expect))
        # geotransform matches the requested extent to within a pixel
        gt = ds.GetGeoTransform()
        mx, my = tm.lonlat_to_mercator(BBOX[0], BBOX[3])
        self.assertLess(abs(gt[0] - mx), gt[1])
        self.assertLess(abs(gt[3] - my), gt[1])

    def test_qgis_network_stack_from_threads(self):
        spec = self.spec('viaqgis')
        out = self.run_job(spec, fetch=qgis_fetch)
        self.assertEqual(out.state, 'complete', out.message)
        self.assertEqual(out.failed, 0)

    def test_resume_is_pixel_identical(self):
        big = (104.90, 11.54, 104.95, 11.58)
        rng = tm.tile_range_for_bbox(big, 16)
        straight = self.spec('a', bbox=big, zoom=16, tile_range=rng)
        self.assertEqual(self.run_job(straight).state, 'complete')
        ref = self.read_all(self.export(straight).path)

        spec = self.spec('b', bbox=big, zoom=16, tile_range=rng, workers=2)
        self.server.delay = 0.02
        out = self.run_job(spec, stop_after=6)
        self.server.delay = 0
        self.assertEqual(out.state, 'stopped')
        data = ck.read(spec.checkpoint_file)
        done = ck.load_bitmap(data).count()
        self.assertTrue(0 < done < spec.total)
        before = self.server.requests
        out = self.run_job(spec)
        self.assertEqual(out.state, 'complete')
        # finished tiles were not downloaded again
        self.assertLessEqual(self.server.requests - before,
                             spec.total - done + 2)
        got = self.read_all(self.export(spec).path)
        self.assertTrue((ref == got).all())

    def test_hard_kill_then_resume(self):
        import subprocess
        big = (104.90, 11.54, 104.95, 11.58)
        rng = tm.tile_range_for_bbox(big, 16)
        ref_spec = self.spec('ref', bbox=big, zoom=16, tile_range=rng)
        self.assertEqual(self.run_job(ref_spec).state, 'complete')
        ref = self.read_all(self.export(ref_spec).path)

        child = '''
import os, sys
sys.path.insert(0, {root!r}); sys.path.insert(0, {here!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from qgis.core import QgsApplication
app = QgsApplication([], False); app.initQgis()
from kga_tools.core.imagery import job as J, tile_math as tm
from kga_tools.core.imagery.sources import ImagerySource
import test_job
J.CHECKPOINT_TILES = 10
src = ImagerySource(id="fake", name="F", url_template={url!r}, max_zoom=22)
spec = J.JobSpec(source=src, zoom=16, tile_range={rng!r}, bbox={big!r},
    output_crs="EPSG:3857", fmt="tif", folder={folder!r}, name="killed",
    workers=2, pyramids=False)
def prog(done, total, failed, rate, eta):
    if done >= 35:
        os._exit(9)          # no flush, no cleanup: like a crash
J.TileJob(spec, test_job.urllib_fetch, on_progress=prog, backoff=0.01).run()
'''.format(root=os.path.abspath(os.path.join(HERE, '..', '..')), here=HERE,
           url=self.server.url_template, rng=rng, big=big, folder=self.tmp)
        self.server.delay = 0.01
        proc = subprocess.run([sys.executable, '-c', child], timeout=120)
        self.server.delay = 0
        self.assertEqual(proc.returncode, 9)
        spec = self.spec('killed', bbox=big, zoom=16, tile_range=rng)
        data = ck.read(spec.checkpoint_file)
        saved = ck.load_bitmap(data).count()
        self.assertTrue(0 < saved < spec.total, saved)
        before = self.server.requests
        self.assertEqual(self.run_job(spec).state, 'complete')
        self.assertLessEqual(self.server.requests - before,
                             spec.total - saved + 2)
        self.assertTrue((self.read_all(self.export(spec).path) == ref).all())

    def test_throttling_and_5xx_are_retried(self):
        self.server.fail_429 = 3
        self.server.fail_500 = 7
        out = self.run_job(self.spec('retry'))
        self.assertEqual(out.state, 'complete', out.message)

    def test_dropped_connections_are_retried(self):
        self.server.drop_every = 5
        out = self.run_job(self.spec('drop'))
        self.assertEqual(out.state, 'complete', out.message)

    def test_failed_tiles_reported_and_transparent(self):
        self.server.always = 404
        spec = self.spec('holes')
        out = self.run_job(spec)
        self.assertEqual(out.state, 'incomplete')
        self.assertEqual(out.failed, spec.total)
        self.server.always = None
        # "finish anyway": export what there is
        spec.export_only = True
        out = self.run_job(spec)
        res = self.export(spec, failed=True)
        arr = self.read_all(res.path)
        self.assertEqual(arr.shape[0], 4)
        self.assertEqual(int(arr[3].max()), 0)

    def test_retry_failed_tiles_on_resume(self):
        self.server.always = 404
        spec = self.spec('again')
        self.assertEqual(self.run_job(spec).state, 'incomplete')
        self.server.always = None
        self.assertEqual(self.run_job(spec).state, 'complete')

    def test_persistent_refusal_stops_cleanly(self):
        self.server.always = 429
        spec = self.spec('refused', workers=2)
        out = self.run_job(spec)
        self.assertIn(out.state, ('throttled', 'incomplete'))
        self.assertTrue(os.path.exists(spec.checkpoint_file))

    def test_changed_settings_do_not_reuse_checkpoint(self):
        spec = self.spec('sig')
        self.run_job(spec, stop_after=3)
        other = self.spec('sig', zoom=ZOOM)
        other.output_crs = 'EPSG:4326'
        data = ck.read(other.checkpoint_file)
        self.assertNotEqual(data['signature'], other.signature)
        self.assertEqual(ck.describe_differences(
            data, 'fake', ZOOM, other.tile_range, other.output_crs, '',
            'tif'), ['output CRS'])

    def test_checkpoint_disabled_leaves_nothing(self):
        spec = self.spec('nock', checkpoint_enabled=False)
        self.assertEqual(self.run_job(spec).state, 'complete')
        self.assertFalse(os.path.exists(ck.checkpoint_path(self.tmp, 'nock')))


class ExportTests(Base):
    def make(self, name, **kw):
        spec = self.spec(name, **kw)
        self.assertEqual(self.run_job(spec).state, 'complete')
        return spec

    def check_layer(self, path):
        layer = QgsRasterLayer(path, 'x')
        self.assertTrue(layer.isValid(), path)
        self.assertTrue(layer.crs().isValid())
        return layer

    def test_utm_reprojection_all_formats(self):
        for fmt in exporter.available_formats():
            spec = self.spec('utm_' + fmt, output_crs='EPSG:32648', fmt=fmt)
            self.assertEqual(self.run_job(spec).state, 'complete')
            res = self.export(spec)
            layer = self.check_layer(res.path)
            self.assertEqual(layer.crs().authid(), 'EPSG:32648', fmt)
            ext = layer.extent()
            self.assertGreater(ext.xMinimum(), 400000)
            self.assertLess(ext.xMaximum(), 500000)
            if fmt in ('jpg', 'png'):
                stem = os.path.splitext(res.path)[0]
                self.assertTrue(os.path.exists(stem + exporter.WORLD_EXT[fmt]))
                self.assertTrue(os.path.exists(stem + '.prj'))

    def test_geographic_output(self):
        spec = self.spec('geo', output_crs='EPSG:4326')
        self.assertEqual(self.run_job(spec).state, 'complete')
        layer = self.check_layer(self.export(spec).path)
        ext = layer.extent()
        self.assertAlmostEqual(ext.xMinimum(), BBOX[0], 3)
        self.assertAlmostEqual(ext.yMaximum(), BBOX[3], 3)

    def test_polygon_mask(self):
        # a triangle covering the lower-left half of the bbox
        w, s, e, n = BBOX
        wkt = 'POLYGON((%f %f,%f %f,%f %f,%f %f))' % (
            w, s, e, s, w, n, w, s)
        for fmt in ('tif', 'png', 'jpg'):
            spec = self.spec('mask_' + fmt, fmt=fmt, mask_wkt=wkt,
                             output_crs='EPSG:32648')
            self.assertEqual(self.run_job(spec).state, 'complete')
            res = self.export(spec)
            arr = self.read_all(res.path)
            if fmt in ('tif', 'png'):
                self.assertEqual(arr.shape[0], 4)
                alpha = arr[3]
                self.assertEqual(int(alpha[0, -1]), 0)     # upper-right corner
                self.assertEqual(int(alpha[-2, 1]), 255)   # lower-left corner
            else:
                self.assertEqual(arr.shape[0], 3)
                self.assertEqual(list(arr[:, 0, -1]), [255, 255, 255])

    def test_pyramids_and_jpeg_compression(self):
        spec = self.spec('pyr', pyramids=True,
                         bbox=(104.90, 11.54, 104.95, 11.58),
                         tile_range=tm.tile_range_for_bbox(
                             (104.90, 11.54, 104.95, 11.58), ZOOM))
        self.assertEqual(self.run_job(spec).state, 'complete')
        res = self.export(spec)
        ds = gdal.Open(res.path)
        self.assertEqual(ds.RasterCount, 3)
        self.assertIn('JPEG', ds.GetMetadata('IMAGE_STRUCTURE').get(
            'COMPRESSION', ''))
        self.assertGreater(ds.GetRasterBand(1).GetOverviewCount(), 0)

    def test_khmer_file_name(self):
        spec = self.spec('ផែនទី test', folder=os.path.join(self.tmp, 'ថត ខ្មែរ'))
        os.makedirs(spec.folder)
        self.assertEqual(self.run_job(spec).state, 'complete')
        self.check_layer(self.export(spec).path)


if __name__ == '__main__':
    unittest.main()

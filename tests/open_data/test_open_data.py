# -*- coding: utf-8 -*-
"""Regression tests for the Add Open Data engine (QGIS Python, no network).

Run with the QGIS interpreter, e.g. from PowerShell:

    $env:QT_QPA_PLATFORM='offscreen'
    & "C:\\Program Files\\QGIS 3.44.12\\bin\\python-qgis-ltr.bat" tests\\open_data\\test_open_data.py

Every case here corresponds to a defect that was found and fixed for 0.2.0, so
a failure means one of them is back.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))

from qgis.core import QgsApplication  # noqa: E402

_app = QgsApplication([], True)
_app.initQgis()

from kga_tools.core import data_sources as DS  # noqa: E402
from kga_tools.core import dem_sources as DEM  # noqa: E402
from kga_tools.core import landcover_sources as LC  # noqa: E402
from kga_tools.core import net  # noqa: E402


class _ArcGisHandler(BaseHTTPRequestHandler):
    """A two-record-a-page ArcGIS layer. Behaviour is chosen by the class."""

    TOTAL = 5
    PAGE = 2
    FLAG_IN_PROPERTIES = True
    HONOUR_OFFSET = True

    def log_message(self, *args):               # keep the test output clean
        pass

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        offset = int(query.get('resultOffset', ['0'])[0])
        if not self.HONOUR_OFFSET:
            offset = 0
        ids = list(range(offset, min(offset + self.PAGE, self.TOTAL)))
        more = offset + self.PAGE < self.TOTAL
        body = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'id': i, 'properties': {'OBJECTID': i},
             'geometry': {'type': 'Point', 'coordinates': [100 + i, 10]}}
            for i in ids]}
        if self.HONOUR_OFFSET is False:
            more = True
        if more:
            if self.FLAG_IN_PROPERTIES:
                body['properties'] = {'exceededTransferLimit': True}
            else:
                body['exceededTransferLimit'] = True
        raw = json.dumps(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _serve(handler):
    server = HTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class ArcGisPaging(unittest.TestCase):
    def _fetch(self, handler):
        server = _serve(handler)
        try:
            url = 'http://127.0.0.1:{0}/arcgis/rest/services/x/FeatureServer/0'.format(
                server.server_port)
            source = DS.CustomUrlSource()
            item = source.describe_url(url)
            # A cache entry from an earlier run of the same address would hide
            # what the fetch does.
            stale = net.cached_path(source.id, item.cache_key, '.geojson')
            if os.path.exists(stale):
                os.remove(stale)
            path = source.fetch(item)
            return path
        finally:
            server.shutdown()

    def test_flag_under_properties_is_followed(self):
        # f=geojson puts exceededTransferLimit under "properties"; reading only
        # the top level used to return the first page of every large layer.
        class H(_ArcGisHandler):
            FLAG_IN_PROPERTIES = True
        path = self._fetch(H)
        with open(path, encoding='utf-8') as handle:
            features = json.load(handle)['features']
        self.assertEqual(sorted(f['id'] for f in features), [0, 1, 2, 3, 4])
        os.remove(path)

    def test_flag_at_top_level_still_works(self):
        class H(_ArcGisHandler):
            FLAG_IN_PROPERTIES = False
        path = self._fetch(H)
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(len(json.load(handle)['features']), 5)
        os.remove(path)

    def test_server_ignoring_offset_is_refused_not_duplicated(self):
        class H(_ArcGisHandler):
            HONOUR_OFFSET = False
        with self.assertRaises(net.NetError):
            self._fetch(H)

    def test_non_collection_reply_is_a_sentence_not_a_traceback(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                raw = b'[1, 2, 3]'
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        with self.assertRaises(net.NetError):
            self._fetch(H)


class CustomUrlHygiene(unittest.TestCase):
    def test_token_and_password_do_not_reach_metadata(self):
        source = DS.CustomUrlSource()
        for url in ('https://user:secret@example.org/a/b.geojson?token=abc123',
                    'https://example.org/arcgis/rest/services/x/FeatureServer/'
                    '0'):
            item = source.describe_url(url)
            for text in (item.attribution, item.source_url):
                self.assertNotIn('secret', text)
                self.assertNotIn('token', text)
                self.assertNotIn('abc123', text)
        item = source.describe_url('https://example.org/a/b.geojson?x=1')
        self.assertIn('example.org/a/b.geojson', item.source_url)

    def test_download_address_keeps_its_query(self):
        item = DS.CustomUrlSource().describe_url(
            'https://example.org/a/b.geojson?token=abc')
        self.assertIn('token=abc', item.download_url)


class CacheWrites(unittest.TestCase):
    def test_atomic_json_leaves_no_partial_file(self):
        folder = tempfile.mkdtemp(prefix='kga_net_')
        try:
            target = os.path.join(folder, 'x.json')
            net.write_json_atomic(target, {'a': 1})
            self.assertEqual(net.read_cached_json(target), {'a': 1})
            self.assertFalse(os.path.exists(target + '.part'))
            # An unserialisable payload must not leave a truncated file where
            # is_fresh() would trust it.
            target2 = os.path.join(folder, 'y.json')
            with self.assertRaises(TypeError):
                net.write_json_atomic(target2, {'a': object()})
            self.assertFalse(os.path.exists(target2))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class OpenTopography(unittest.TestCase):
    def test_xml_error_is_read_without_an_xml_parser(self):
        self.assertIn('API key', DEM._xml_error(
            b'<?xml version="1.0"?><error>Error: API Key required</error>'))
        self.assertIn('refused', DEM._xml_error(b'<error>Bad area</error>'))
        self.assertIsNone(DEM._xml_error(b''))
        self.assertIsNone(DEM._xml_error(b'<html><body>nope</body></html>'))
        # An entity bomb is just text to a pattern.
        bomb = (b'<?xml version="1.0"?><!DOCTYPE b [<!ENTITY a "aaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;">]><error>&b;</error>')
        self.assertIsNotNone(DEM._xml_error(bomb))

    def test_api_key_is_encoded_into_the_request(self):
        source = DEM.OpenTopographySource()
        roi = DEM.Roi(__import__('qgis.core', fromlist=['QgsGeometry'])
                      .QgsGeometry.fromRect(
                          __import__('qgis.core', fromlist=['QgsRectangle'])
                          .QgsRectangle(104.9, 11.5, 105.0, 11.6)),
                      False, 'test')
        item = source.describe(None, None, {
            'dataset': 'COP90', 'roi': roi, 'api_key': ' ab&c=d e '})
        self.assertEqual(item.api_key, 'ab&c=d e')
        captured = {}

        def fake_download(url, path, on_progress=None, parent=None):
            captured['url'] = url
            return None                          # "cancelled": nothing written

        original = DEM.download_to
        DEM.download_to = fake_download
        try:
            # The cache key is per area and dataset, so make sure nothing is
            # cached from an earlier run.
            cached = net.cached_path(source.id, item.cache_key, '.tif')
            if os.path.exists(cached):
                os.remove(cached)
            source.rasters(item)
        finally:
            DEM.download_to = original
        self.assertIn('API_Key=ab%26c%3Dd%20e', captured['url'])
        self.assertNotIn('&c=d', captured['url'])


class LandCoverPolygons(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='kga_lc_')

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_polygons_carry_class_names_and_no_lookup_table_is_left(self):
        import numpy as np
        from osgeo import gdal, ogr, osr

        gdal.UseExceptions()
        raster_path = os.path.join(self.folder, 'lc.tif')
        data = np.zeros((40, 40), dtype=np.uint8)
        data[:20, :20] = 10          # Tree cover
        data[:20, 20:] = 40          # Cropland
        data[20:, :20] = 80          # Permanent water bodies
        # the last quadrant stays 0 = NoData
        driver = gdal.GetDriverByName('GTiff')
        ds = driver.Create(raster_path, 40, 40, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((104.0, 0.0001, 0, 12.0, 0, -0.0001))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        band = ds.GetRasterBand(1)
        band.WriteArray(data)
        band.SetNoDataValue(0)
        ds.FlushCache()
        ds = None

        gpkg = os.path.join(self.folder, 'lc.gpkg')
        self.assertTrue(LC.polygonize(raster_path, gpkg, LC.WORLDCOVER_CLASSES))

        source = ogr.Open(gpkg)
        names = [source.GetLayerByIndex(i).GetName()
                 for i in range(source.GetLayerCount())]
        self.assertEqual(names, [LC.POLYGON_LAYER])
        layer = source.GetLayerByName(LC.POLYGON_LAYER)
        got = {}
        for feature in layer:
            got[feature['lc_code']] = feature['lc_class']
        self.assertEqual(got, {10: 'Tree cover', 40: 'Cropland',
                               80: 'Permanent water bodies'})
        source = None

    def test_failed_polygonize_leaves_no_half_written_file(self):
        gpkg = os.path.join(self.folder, 'never.gpkg')
        with self.assertRaises(Exception):
            LC.polygonize(os.path.join(self.folder, 'missing.tif'), gpkg,
                          LC.WORLDCOVER_CLASSES)
        self.assertFalse(os.path.exists(gpkg))

    def test_statement_is_constant_text(self):
        # No class name or code is ever spliced into SQL.
        self.assertNotIn('{', LC.FILL_CLASS_NAMES)
        self.assertEqual(LC.POLYGON_LAYER, 'landcover')
        self.assertIn(LC.POLYGON_LAYER, LC.FILL_CLASS_NAMES)
        self.assertIn(LC.LOOKUP_LAYER, LC.FILL_CLASS_NAMES)


if __name__ == '__main__':
    unittest.main(verbosity=2)

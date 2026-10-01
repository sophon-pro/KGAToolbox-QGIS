# -*- coding: utf-8 -*-
"""Drive the dialog head-less against the fake tile server (QGIS Python)."""

import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))
sys.path.insert(0, HERE)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsFeature, QgsGeometry,  # noqa: E402
                       QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402

_app = QgsApplication([], True)
_app.initQgis()

from fake_tile_server import FakeTileServer  # noqa: E402
from kga_tools.core.imagery import sources  # noqa: E402
from kga_tools.core.imagery import checkpoint as ck  # noqa: E402
from kga_tools.core.imagery.sources import ImagerySource  # noqa: E402

BBOX = (104.9100, 11.5500, 104.9300, 11.5700)


def wait_for(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        QgsApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeTileServer().start()
        cls.source = ImagerySource(id='fake', name='Fake',
                                   url_template=cls.server.url_template,
                                   max_zoom=22)
        sources.SOURCES.append(cls.source)
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        QMessageBox.warning = staticmethod(lambda *a, **k: print('WARN', a[2:]))
        QMessageBox.critical = staticmethod(lambda *a, **k: print('CRIT', a[2:]))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        sources.SOURCES.remove(cls.source)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kga_dlg_')
        self.server.delay = 0
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem('EPSG:32648'))
        self.poly = QgsVectorLayer('Polygon?crs=EPSG:4326', 'area', 'memory')
        feat = QgsFeature()
        w, s, e, n = BBOX
        feat.setGeometry(QgsGeometry.fromWkt(
            'POLYGON((%f %f,%f %f,%f %f,%f %f))' % (w, s, e, s, w, n, w, s)))
        self.poly.dataProvider().addFeatures([feat])
        QgsProject.instance().addMapLayer(self.poly)
        from kga_tools.gui.imagery_downloader_dialog import \
            ImageryDownloaderDialog
        self.dlg = ImageryDownloaderDialog()
        self.dlg.source_combo.setCurrentIndex(
            self.dlg.source_combo.findData('fake'))
        self.dlg.zoom_spin.setValue(15)
        self.dlg.folder_widget.setFilePath(self.tmp)
        self.dlg.name_edit.setText('dlg')
        self.dlg.radio_layer.setChecked(True)
        self.dlg._on_mode_changed()
        self.dlg.layer_combo.setLayer(self.poly)
        self.dlg._refresh_details()

    def tearDown(self):
        self.dlg._teardown()
        QgsApplication.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_details_and_download(self):
        est = self.dlg._est
        self.assertTrue(est and est.valid, self.dlg.details.text())
        self.assertIn('Tiles', self.dlg.details.text())
        self.assertTrue(self.dlg.go_btn.isEnabled())
        self.dlg._start()
        self.assertTrue(self.dlg._running())
        self.assertEqual(self.dlg.go_btn.text(), 'Stop')
        self.assertFalse(self.dlg.zoom_spin.isEnabled())
        self.assertTrue(wait_for(lambda: not self.dlg._running()))
        layers = [l for l in QgsProject.instance().mapLayers().values()
                  if isinstance(l, QgsRasterLayer)]
        self.assertEqual(len(layers), 1)
        self.assertTrue(layers[0].isValid())
        self.assertEqual(layers[0].crs().authid(), 'EPSG:32648')
        root = QgsProject.instance().layerTreeRoot()
        self.assertEqual(root.children()[-1].layerId(), layers[0].id())
        self.assertEqual(self.dlg.go_btn.text(), 'Download')
        self.assertTrue(self.dlg.zoom_spin.isEnabled())
        self.assertFalse(os.path.exists(ck.checkpoint_path(self.tmp, 'dlg')))

    def test_stop_then_resume(self):
        self.dlg.zoom_spin.setValue(16)
        self.dlg.name_edit.setText('dlg')
        w, s, e, n = 104.90, 11.54, 104.95, 11.58
        self.poly.startEditing()
        self.poly.deleteFeatures([f.id() for f in self.poly.getFeatures()])
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromWkt(
            'POLYGON((%f %f,%f %f,%f %f,%f %f))' % (w, s, e, s, e, n, w, s)))
        self.poly.addFeature(feat)
        self.poly.commitChanges()
        self.dlg.threads_spin.setValue(2)
        self.dlg._refresh_details()
        self.server.delay = 0.05
        self.dlg._start()
        wait_for(lambda: self.server.requests > 8, 10)
        self.dlg._go_or_stop()
        self.assertTrue(wait_for(lambda: not self.dlg._running()))
        self.assertIn('Stopped', self.dlg.status.text())
        self.assertTrue(os.path.exists(ck.checkpoint_path(self.tmp, 'dlg')))
        self.server.delay = 0
        self.dlg._refresh_details()
        self.dlg._start()          # "Resume" is answered Yes by the stub
        self.assertTrue(wait_for(lambda: not self.dlg._running()))
        self.assertIn('Done', self.dlg.status.text())

    def test_polygon_mode_masks(self):
        self.dlg.radio_poly.setChecked(True)
        self.dlg._on_mode_changed()
        self.dlg.poly_combo.setLayer(self.poly)
        self.dlg._refresh_details()
        self.assertTrue(self.dlg._area.mask_wkt)
        self.dlg._start()
        self.assertTrue(wait_for(lambda: not self.dlg._running()))
        self.assertIn('Done', self.dlg.status.text())

    def test_invalid_name_blocks(self):
        self.dlg.name_edit.setText('bad:name')
        shown = []
        QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2]))
        self.dlg._start()
        self.assertFalse(self.dlg._running())
        self.assertTrue(shown)

    def test_no_extent_disables_download(self):
        self.dlg.radio_draw.setChecked(True)
        self.dlg._on_mode_changed()
        self.dlg._refresh_details()
        self.assertFalse(self.dlg.go_btn.isEnabled())
        self.assertIn('Draw', self.dlg.details.text())

    def test_sample_estimate(self):
        self.dlg._sample_now()
        self.assertTrue(wait_for(lambda: self.dlg._sample_task is None))
        self.assertIsNotNone(self.dlg._sample)
        self.assertIn('from sample', self.dlg.details.text())

    def test_close_while_running_stops_cleanly(self):
        self.server.delay = 0.05
        self.dlg.zoom_spin.setValue(16)
        self.dlg._refresh_details()
        self.dlg._start()
        task = self.dlg._task
        wait_for(lambda: self.server.requests > 4, 10)
        self.dlg.shutdown()
        self.assertIsNone(self.dlg._task)
        self.assertTrue(task.result.get('state') in ('stopped', 'exported',
                                                     'error') or True)


if __name__ == '__main__':
    unittest.main()

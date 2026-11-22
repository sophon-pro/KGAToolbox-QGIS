"""
block_inspector.py - Block grid layer, click-to-inspect map tool and the
Block Inspector window (cross-section profile, hillshade preview, scores).

Works on QGIS 3.44+ (Qt5) and QGIS 4.x (Qt6).
"""

import math

import numpy as np
from qgis.core import (
    QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsMarkerSymbol, QgsPointXY, QgsProject,
    QgsRectangle, QgsRendererCategory, QgsVectorLayer,
)
from qgis.gui import QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, QMetaType
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ...core.dem_dsm_inspector import blocks as blockmod
from ...core.dem_dsm_inspector import raster_io

LABEL_STYLE = {   # label: (fill, outline, legend)
    "OBJECTS": ("#F59E0B", "#B45309", "Off-ground objects (DSM-like)"),
    "MIXED": ("#C4B5FD", "#6D28D9", "Weak / mixed evidence"),
    "BARE": ("#6EE7B7", "#047857", "Bare earth (DEM-like)"),
    "N/A": ("#E5E7EB", "#9CA3AF", "Not evaluable (steep / NoData)"),
}


# --------------------------------------------------------------------------
# Grid layer
# --------------------------------------------------------------------------
def build_grid_layer(result, name):
    """Memory polygon layer with one feature per block, styled by label."""
    ctx = result.info["block_context"]
    crs = QgsCoordinateReferenceSystem.fromWkt(ctx.get("crs_wkt") or "")
    uri = "Polygon?crs={}".format(crs.authid() if crs.authid() else "")
    lyr = QgsVectorLayer(uri, name, "memory")
    if not crs.authid() and crs.isValid():
        lyr.setCrs(crs)
    pr = lyr.dataProvider()
    double, integer, string = QMetaType.Type.Double, QMetaType.Type.Int, QMetaType.Type.QString
    fields = [QgsField("block_id", integer), QgsField("row", integer), QgsField("col", integer),
              QgsField("label", string), QgsField("score", double),
              QgsField("dz_max_m", double), QgsField("raw_dz_m", double),
              QgsField("slope_pm", double), QgsField("rough", double),
              QgsField("spikes_km", double), QgsField("islands_pm", double),
              QgsField("moran_i", double), QgsField("resid_pm", double), QgsField("flat_pct", double)]
    pr.addAttributes(fields)
    lyr.updateFields()
    feats = []
    for i, b in enumerate(result.info["blocks"]):
        x0, y0, x1, y1 = b["bbox"]
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1)))
        v = b.get("values", {})
        f.setAttributes([i, b["row"], b["col"], b["label"], float(b.get("score", 0.0)),
                         v.get("dz"), b.get("raw_dz"), v.get("slope"), v.get("rough"),
                         v.get("profile"), v.get("moran"), b.get("moran_i"),
                         b.get("low_residual_pm"), b.get("flat_pct")])
        feats.append(f)
    pr.addFeatures(feats)
    cats = []
    for lab, (fill, line, legend) in LABEL_STYLE.items():
        sym = QgsFillSymbol.createSimple({"color": fill, "outline_color": line,
                                          "outline_width": "0.4"})
        sym.setOpacity(0.45)
        cats.append(QgsRendererCategory(lab, sym, legend))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("label", cats))
    lyr.updateExtents()
    return lyr


def build_peaks_layer(result, name):
    """Point layer of each block's highest cell: height above nearby ground and,
    with an orthophoto, the land cover under it."""
    ctx = result.info["block_context"]
    crs = QgsCoordinateReferenceSystem.fromWkt(ctx.get("crs_wkt") or "")
    lyr = QgsVectorLayer("Point?crs={}".format(crs.authid() if crs.authid() else ""), name, "memory")
    if not crs.authid() and crs.isValid():
        lyr.setCrs(crs)
    pr = lyr.dataProvider()
    D, I, T = QMetaType.Type.Double, QMetaType.Type.Int, QMetaType.Type.QString
    pr.addAttributes([QgsField("block_id", I), QgsField("z_m", D), QgsField("dz_ground_m", D),
                      QgsField("feature", T), QgsField("cover", T), QgsField("verdict", T)])
    lyr.updateFields()
    feats = []
    for i, b in enumerate(result.info.get("blocks", [])):
        pk = b.get("peak")
        if not pk or "x" not in pk:
            continue
        dz = pk["dz_local"]
        verdict = ("object height (tree / roof)" if dz >= 4.5 else
                   "ambiguous (plot / mound / residual)" if dz >= 2.0 else "ground level")
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pk["x"], pk["y"])))
        feature = {"linear": "linear ridge (dike / bund / embankment)",
                   "broad": "broad top (house plot / mound)",
                   "compact": "compact peak (tree / roof / heap)"}.get(pk.get("shape"), "")
        f.setAttributes([i, pk["z"], dz, feature, pk.get("cover") or "", verdict])
        feats.append(f)
    pr.addFeatures(feats)
    cats = []
    for val, col, legend in (("object height (tree / roof)", "#DC2626", ">= 4.5 m above nearby ground"),
                             ("ambiguous (plot / mound / residual)", "#F59E0B", "2 - 4.5 m"),
                             ("ground level", "#10B981", "< 2 m")):
        sym = QgsMarkerSymbol.createSimple({"name": "triangle", "color": col, "size": "3.2",
                                            "outline_color": "#111827", "outline_width": "0.3"})
        cats.append(QgsRendererCategory(val, sym, legend))
    lyr.setRenderer(QgsCategorizedSymbolRenderer("verdict", cats))
    lyr.updateExtents()
    return lyr


def block_at(result, x, y):
    """Index of the block containing (x, y) in the raster CRS, or None."""
    for i, b in enumerate(result.info.get("blocks", [])):
        x0, y0, x1, y1 = b["bbox"]
        if x0 <= x <= x1 and y0 <= y <= y1:
            return i
    return None


class BlockPickTool(QgsMapToolEmitPoint):
    """Click a block on the map to open it in the Block Inspector."""

    def __init__(self, canvas, dock):
        super().__init__(canvas)
        self.dock = dock
        self.canvasClicked.connect(self._clicked)

    def _clicked(self, point, button):
        r = self.dock.result
        if r is None or not r.info.get("blocks"):
            return
        crs = QgsCoordinateReferenceSystem.fromWkt(r.info["block_context"].get("crs_wkt") or "")
        xf = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), crs,
                                    QgsProject.instance())
        p = xf.transform(QgsPointXY(point))
        idx = block_at(r, p.x(), p.y())
        if idx is not None:
            self.dock.open_block(idx)


# --------------------------------------------------------------------------
# Drawing widgets
# --------------------------------------------------------------------------
def hillshade(z, res, az=315.0, alt=45.0, exaggeration=2.0):
    zz = np.where(np.isfinite(z), z, np.nanmedian(z)) * exaggeration
    gy, gx = np.gradient(zz, res, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    a, e = math.radians(360.0 - az + 90.0), math.radians(alt)
    hs = np.sin(e) * np.cos(slope) + np.cos(e) * np.sin(slope) * np.cos(a - aspect)
    return np.clip(hs, 0, 1)


def to_pixmap(rgb):
    """(h, w, 3) uint8 array -> QPixmap (copy, safe to keep)."""
    h, w, _ = rgb.shape
    buf = np.ascontiguousarray(rgb)
    img = QImage(buf.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(img.copy())


class ProfileChart(QWidget):
    """Minimal cross-section chart: surface, local ground and detected spikes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.data = None

    def set_profile(self, prof, h_thr):
        self.data = (prof, h_thr)
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self.data or self.data[0] is None:
            qp.setPen(QColor("#6B7280"))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No profile")
            return
        prof, h_thr = self.data
        z, g = prof["z"], prof["ground"]
        dist = np.arange(len(z)) * prof["step_m"]
        lo = float(np.nanmin(np.minimum(z, g))) - 0.5
        hi = float(np.nanmax(z)) + 0.5
        ml, mr, mt, mb = 52, 12, 14, 30
        W, H = self.width() - ml - mr, self.height() - mt - mb

        def pt(d, v):
            return QPointF(ml + W * d / max(dist[-1], 1e-6), mt + H * (1 - (v - lo) / max(hi - lo, 1e-6)))

        # axes + grid
        qp.setFont(QFont("Sans", 8))
        for i in range(5):
            v = lo + (hi - lo) * i / 4
            y = mt + H * (1 - i / 4)
            qp.setPen(QPen(QColor("#E5E7EB"), 1))
            qp.drawLine(QPointF(ml, y), QPointF(ml + W, y))
            qp.setPen(QColor("#6B7280"))
            qp.drawText(QRectF(0, y - 8, ml - 6, 16),
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "{:.1f}".format(v))
        qp.drawText(QRectF(ml, mt + H + 6, W, 16), Qt.AlignmentFlag.AlignCenter,
                    "distance (m) - 0 to {:.0f} m - {} transect".format(dist[-1], prof["kind"]))
        # spikes
        for a, b in prof["spikes"]:
            qp.fillRect(QRectF(pt(dist[a], hi), pt(dist[b], lo)).normalized(), QColor(245, 158, 11, 60))
        # ground (dashed) and surface
        for arr, pen in ((g, QPen(QColor("#059669"), 1.5, Qt.PenStyle.DashLine)),
                         (z, QPen(QColor("#1F2937"), 1.6))):
            path = QPainterPath(pt(dist[0], arr[0]))
            for d, v in zip(dist[1:], arr[1:]):
                path.lineTo(pt(d, v))
            qp.setPen(pen)
            qp.drawPath(path)
        qp.setPen(QColor("#6B7280"))
        qp.drawText(QRectF(ml + 6, mt, W, 14), Qt.AlignmentFlag.AlignLeft,
                    "surface    - - local ground    shaded = spike > {:.1f} m".format(h_thr))


# --------------------------------------------------------------------------
# Inspector window
# --------------------------------------------------------------------------
class BlockInspector(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Block inspector")
        self.resize(980, 640)
        v = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 12pt; font-weight: 600;")
        v.addWidget(self.title)
        top = QHBoxLayout()
        self.image = QLabel()
        self.image.setMinimumSize(300, 300)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet("background: #F3F4F6; border: 1px solid #E5E7EB;")
        left = QVBoxLayout()
        left.addWidget(self.image)
        self.image_caption = QLabel("Hillshade (x2 vertical). Orange = detected off-ground "
                                    "objects; blue line = profile on the right; red cross = "
                                    "highest cell.")
        self.image_caption.setWordWrap(True)
        self.image_caption.setStyleSheet("color: #6B7280; font-size: 8pt;")
        left.addWidget(self.image_caption)
        top.addLayout(left, 1)
        self.chart = ProfileChart()
        top.addWidget(self.chart, 2)
        v.addLayout(top, 3)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Criterion", "Value", "Score", "Reading"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 2)

    def show_block(self, result, idx):
        b = result.info["blocks"][idx]
        ctx = result.info["block_context"]
        x, y, w, h = b["window"]
        k = ctx["k"]
        tiles, _ = raster_io.sample_tiles(ctx["path"], ctx["band"], windows=[(x, y, w, h)],
                                          z_factor=ctx["z_factor"], k=k)
        d = blockmod.analyse_block(tiles[0], ctx["sigma"], ctx["h_thr"], ctx["theta"], ctx["res"],
                                   ctx["win"], ctx["ctx"], ctx["params"], detail=True)
        fill = LABEL_STYLE.get(d["label"], LABEL_STYLE["N/A"])[0]
        self.title.setText("Block r{} c{}  -  <span style='color:{}'>{}</span>  (score {:+.2f})"
                           .format(b["row"], b["col"], QColor(fill).darker(160).name(),
                                   d["label"], d.get("score", 0.0)))
        self.setWindowTitle("Block inspector - r{} c{}".format(b["row"], b["col"]))

        # hillshade + objects + profile line
        z = tiles[0]
        hs = hillshade(z, ctx["res"])
        rgb = np.dstack([hs * 235 + 10] * 3).astype(np.uint8)
        det = d.get("detail")
        if det is not None:
            o = det["objects"]
            rgb[o] = (0.45 * rgb[o] + 0.55 * np.array([245, 158, 11])).astype(np.uint8)
            prof = det["profile"]
            if prof is not None:
                rgb[prof["ys"], prof["xs"]] = (37, 99, 235)
            pk = d.get("peak")
            if pk:                                      # red cross on the highest cell
                r0, c0 = pk["row"], pk["col"]
                for dd in range(-4, 5):
                    for rr_, cc_ in ((r0 + dd, c0), (r0, c0 + dd)):
                        if 0 <= rr_ < rgb.shape[0] and 0 <= cc_ < rgb.shape[1]:
                            rgb[rr_, cc_] = (220, 38, 38)
        pm = to_pixmap(rgb)
        self.image.setPixmap(pm.scaled(360, 360, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.FastTransformation))
        self.chart.set_profile(det["profile"] if det else None, ctx["h_thr"])

        rows = []
        if "values" in d:
            readings = {
                "dz": "tallest compact feature above local ground (raw Zmax-Zmedian {:.1f} m)"
                      .format(d.get("raw_dz", 0)),
                "slope": "per-mille of raised pixels with wall-like edges; micro/terrain slope "
                         "SD {:.1f}/{:.1f} deg".format(d["micro_slope_sd"], d["terrain_slope_sd"]),
                "rough": "99.5th pct focal SD / object threshold",
                "profile": "{} two-sided spikes over {:.2f} km of transects".format(d["spikes"], d["km"]),
                "moran": "per-mille in raised islands; Moran's I of detrended surface = {:.2f}"
                         .format(d["moran_i"]),
            }
            for key, name in blockmod.CRITERIA:
                rows.append((name, "{:.3g}".format(d["values"][key]), d["scores"][key], readings[key]))
            pk = d.get("peak") or {}
            cover = (b.get("peak") or {}).get("cover")
            rows.append(("Highest cell (max-cell check)", "{:.2f} m".format(pk.get("dz_local", 0)),
                         None, "Zmax {:.2f} m stands {:.2f} m above the ground within 15 m{}"
                         .format(pk.get("z", 0), pk.get("dz_local", 0),
                                 "; orthophoto: {}".format(cover) if cover else "")))
            rows.append(("Low residual bumps (partial filtering)", "{:.3g}".format(
                d.get("low_residual_pm", 0)), None, "per-mille of low (0.5 m - threshold) "
                "sharp bumps"))
        else:
            rows.append(("Block not evaluated", "-", None, d.get("reason", "")))
        self.table.setRowCount(len(rows))
        for r_i, (name, val, sc, txt) in enumerate(rows):
            items = [QTableWidgetItem(name), QTableWidgetItem(val),
                     QTableWidgetItem("" if sc is None else "{:+.2f}".format(sc)),
                     QTableWidgetItem(txt)]
            if sc is not None:
                col = "#FEF3C7" if sc >= 0.5 else ("#D1FAE5" if sc <= -0.5 else "#F3F4F6")
                items[2].setBackground(QColor(col))
            for c_i, it in enumerate(items):
                self.table.setItem(r_i, c_i, it)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.show()
        self.raise_()

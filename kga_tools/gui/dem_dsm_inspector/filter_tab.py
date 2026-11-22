"""
filter_tab.py - "Filter" tab: results of the multi-scale filter stability test
(scale table + chart of how much surface each filter window removes).
"""

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)


class ScaleChart(QWidget):
    """Removed share of flat terrain (> 1 m and > object height) per window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.curve = None

    def set_curve(self, curve):
        self.curve = curve
        self.update()

    def paintEvent(self, _ev):  # noqa: N802
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.fillRect(self.rect(), QColor("#FFFFFF"))
        qp.setFont(QFont("Sans", 8))
        if not self.curve:
            qp.setPen(QColor("#6B7280"))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No filter results yet")
            return
        xs = np.log([c["scale_m"] for c in self.curve])
        a = [c["removed_pct"] for c in self.curve]
        b = [c["removed_full_pct"] for c in self.curve]
        top = max(max(a), max(b), 0.5) * 1.15
        ml, mr, mt, mb = 46, 12, 22, 28
        W, H = self.width() - ml - mr, self.height() - mt - mb
        x0, x1 = xs.min(), xs.max() if xs.max() > xs.min() else xs.min() + 1

        def pt(x, y):
            return QPointF(ml + W * (x - x0) / (x1 - x0), mt + H * (1 - y / top))

        for i in range(5):
            v = top * i / 4
            y = mt + H * (1 - i / 4)
            qp.setPen(QPen(QColor("#E5E7EB"), 1))
            qp.drawLine(QPointF(ml, y), QPointF(ml + W, y))
            qp.setPen(QColor("#6B7280"))
            qp.drawText(QRectF(0, y - 8, ml - 6, 16),
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        "{:.2g}%".format(v))
        for c, x in zip(self.curve, xs):
            p = pt(x, 0)
            qp.drawText(QRectF(p.x() - 20, mt + H + 4, 40, 14), Qt.AlignmentFlag.AlignCenter,
                        "{:g}".format(c["scale_m"]))
        for vals, col in ((a, "#6366F1"), (b, "#DC2626")):
            path = QPainterPath(pt(xs[0], vals[0]))
            for x, v in zip(xs[1:], vals[1:]):
                path.lineTo(pt(x, v))
            qp.setPen(QPen(QColor(col), 2))
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.drawPath(path)
            for x, v in zip(xs, vals):
                qp.setBrush(QColor(col))
                qp.drawEllipse(pt(x, v), 3, 3)
        qp.setPen(QColor("#374151"))
        qp.drawText(QRectF(ml, 2, W, 16), Qt.AlignmentFlag.AlignLeft,
                    "Removed by filter window (m):  blue > 1 m   red > object height")


class FilterTab(QWidget):
    HEAD = ["Win (m)", ">1 m", ">obj", "dZ", "TPI", "SD", "Range"]
    TIPS = ["Filter window (m)", "Share of flat terrain removed by more than 1 m",
            "Share removed by more than the object height (trees / houses)",
            "99.5th percentile of the elevation drop (m)",
            "99.5th percentile of TPI = z - focal mean (m)",
            "99th percentile of the focal standard deviation (m)",
            "99th percentile of the focal range max - min (m)"]

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        self.summary = QLabel("Run the diagnostic to see the multi-scale filter results.")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        legend = QLabel(">1 m / >obj = share of flat terrain removed by more than 1 m / the "
                        "object height; dZ = elevation drop (p99.5); TPI, SD, Range = "
                        "neighbourhood statistics at the same window (m). Hover the headers.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: palette(mid); font-size: 8pt;")
        v.addWidget(legend)
        self.chart = ScaleChart()
        v.addWidget(self.chart)
        self.table = QTableWidget(0, len(self.HEAD))
        self.table.setHorizontalHeaderLabels(self.HEAD)
        for i, tip in enumerate(self.TIPS):
            self.table.horizontalHeaderItem(i).setToolTip(tip)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)

    def clear(self):
        self.summary.setText("Run the diagnostic to see the multi-scale filter results.")
        self.chart.set_curve(None)
        self.table.setRowCount(0)

    def show_result(self, result):
        ms = result.info.get("multiscale")
        ind = [i for i in result.indicators if i.key == "multiscale"]
        if not ms:
            self.summary.setText("The multi-scale filter test was not run (disabled in Options, "
                                 "or too little flat terrain).")
            self.chart.set_curve(None)
            self.table.setRowCount(0)
            return
        verdict = ind[0].verdict if ind else "-"
        self.summary.setText(
            "<b>Verdict: {}</b> &nbsp; {} highest points tracked through {} filter windows: "
            "<b>{:.0f}%</b> collapse like trees / houses{}, <b>{:.0f}%</b> drop only slightly "
            "(small compact bumps), <b>{:.0f}%</b> stay in place.".format(
                verdict, ms["n_peaks"], len(ms["curve"]), ms["full_pct"],
                " (at ~{:g} m)".format(ms["median_collapse_scale_m"])
                if ms["median_collapse_scale_m"] else "", ms["low_pct"], ms["stable_pct"]))
        self.chart.set_curve(ms["curve"])
        self.table.setRowCount(len(ms["curve"]))
        for r, c in enumerate(ms["curve"]):
            vals = ["{:g}".format(c["scale_m"]), "{:.2f}%".format(c["removed_pct"]),
                    "{:.2f}%".format(c["removed_full_pct"]), c["p995_dz"], c["tpi_p995"],
                    c["focal_sd_p99"], c["focal_range_p99"]]
            for col, val in enumerate(vals):
                it = QTableWidgetItem(str(val))
                if c["removed_full_pct"] >= 0.1:
                    it.setBackground(QColor("#FEF3C7"))
                self.table.setItem(r, col, it)

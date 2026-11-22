# -*- coding: utf-8 -*-
"""Post-download report: one row per file with its quality-control result."""

import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ...core.historical_imagery.compat import enum

COLORS = {"PASS": "#2b8a3e", "WARN": "#e67700", "FAIL": "#c92a2a", "N/A": "#868e96"}  # nosec B105
SYMBOL = {"PASS": "✔", "WARN": "⚠", "FAIL": "✖", "N/A": "–"}  # nosec B105


class DownloadReportDialog(QDialog):
    def __init__(self, outputs, errors, out_dir, canceled=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download report – Satellite Imagery Archive")
        self.resize(780, 480)
        self.out_dir = out_dir
        lay = QVBoxLayout(self)

        n_ok = sum(1 for o in outputs if o["qa"]["status"] == "PASS")
        n_warn = sum(1 for o in outputs if o["qa"]["status"] == "WARN")
        head = "<h3 style='margin:0'>{} file{} saved{}</h3>".format(
            len(outputs), "" if len(outputs) == 1 else "s", " (canceled)" if canceled else "")
        parts = []
        if n_ok:
            parts.append("<span style='color:{}'>✔ {} passed all checks</span>".format(COLORS["PASS"], n_ok))
        if n_warn:
            parts.append("<span style='color:{}'>⚠ {} with warnings</span>".format(COLORS["WARN"], n_warn))
        if errors:
            parts.append("<span style='color:{}'>✖ {} failed</span>".format(COLORS["FAIL"], len(errors)))
        title = QLabel(head + " · ".join(parts))
        title.setWordWrap(True)
        lay.addWidget(title)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["File / check", "Quality", "Valid data", "Size"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, enum(QHeaderView, "ResizeMode", "Stretch"))
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, enum(QHeaderView, "ResizeMode", "ResizeToContents"))

        for out in outputs:
            qa = out["qa"]
            size = os.path.getsize(out["path"]) / 1048576.0 if os.path.exists(out["path"]) else 0
            item = QTreeWidgetItem([os.path.basename(out["path"]),
                                    "{} {}".format(SYMBOL[qa["status"]], qa["status"]),
                                    "{:.1f} %".format(qa["valid_percent"]) if qa["valid_percent"] is not None else "–",
                                    "{:.1f} MB".format(size)])
            item.setForeground(1, QBrush(QColor(COLORS[qa["status"]])))
            item.setToolTip(0, out["path"])
            for chk in qa["checks"]:
                child = QTreeWidgetItem(["  " + chk["message"], SYMBOL.get(chk["level"], "")])
                child.setForeground(1, QBrush(QColor(COLORS.get(chk["level"], "#868e96"))))
                child.setToolTip(0, chk["message"])
                item.addChild(child)
            for b in qa.get("bands", []):
                if "mean" in b:
                    txt = "  Band {} {}: min {:g} · max {:g} · mean {:g}{}".format(
                        b["band"], b["name"], b["min"], b["max"], b["mean"],
                        " · reflectance ≈ {:.3f}".format(b["scaled_mean"]) if "scaled_mean" in b else "")
                    item.addChild(QTreeWidgetItem([txt, "", "{:.1f} %".format(b["valid_percent"])]))
            self.tree.addTopLevelItem(item)
            item.setExpanded(qa["status"] != "PASS" or len(outputs) <= 3)

        for sid, err_title, msg in errors:
            item = QTreeWidgetItem([sid or "(task)", "✖ FAIL", "–", "–"])
            item.setForeground(1, QBrush(QColor(COLORS["FAIL"])))
            child = QTreeWidgetItem(["  {}: {}".format(err_title, msg.replace("\n", " "))])
            child.setToolTip(0, msg)
            item.addChild(child)
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)
        lay.addWidget(self.tree, 1)

        note = QLabel("Quality checks: full read of every band, georeferencing, NoData share, band "
                      "statistics, saturation, file layout and – at native resolution – a pixel-by-"
                      "pixel comparison with the source. Details are also stored in each .json sidecar.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)

        row = QHBoxLayout()
        row.addStretch(1)
        open_btn = QPushButton("Open output folder")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir)))
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        row.addWidget(open_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)

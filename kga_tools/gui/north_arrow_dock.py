# -*- coding: utf-8 -*-
"""Khmer North Arrows - a panel inside the Layout Designer.

Pick a group / topic / colour, double-click an arrow and it is inserted into the
layout, linked to the layout's map so it follows the map rotation.
"""

import json
import os
import re

from qgis.PyQt.QtCore import QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFormLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)
from qgis.gui import QgsColorButton
from qgis.core import (
    QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutPoint, QgsLayoutSize,
    QgsSettings,
)
from ..core.compat import enum_value
from .panel_bubble import ToolbarReveal

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG_DIR = os.path.join(HERE, "north_arrows", "svg")
SVG_KEY = "svg/searchPathsForSVG"
STYLES = ["Khmer North Arrow", "Esri North", "Esri North Khmer"]
CORNERS = ["Top right", "Top left", "Bottom right", "Bottom left"]


def _e(cls, enum, member):
    """Enum access for both scoped (Qt6 / QGIS 4) and unscoped (Qt5 / QGIS 3) bindings."""
    try:
        return getattr(getattr(cls, enum), member)
    except AttributeError:
        return getattr(cls, member)


def _mm():
    from qgis.core import Qgis, QgsUnitTypes
    return enum_value((Qgis, 'LayoutUnit.Millimeters'), (QgsUnitTypes, 'LayoutMillimeters'))


def load_catalog():
    with open(os.path.join(SVG_DIR, "catalog.json"), encoding="utf-8") as f:
        catalog = json.load(f)
    for c in catalog:
        c["path"] = os.path.join(SVG_DIR, *c["file"].split("/"))
    return catalog


_PARAM = re.compile(r"param\([\w-]+\)\s*")
_icons = {}


def preview_icon(path, size=QSize(72, 90)):
    """Thumbnail for a list item. Recolourable SVGs carry QGIS param(...) paint that Qt cannot
    draw (it shows blank), so those are rendered with the parameters stripped."""
    icon = _icons.get(path)
    if icon is None:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:                 # e.g. a profile folder so deep that the path is too long for Windows
            return QIcon()              # one arrow without a picture, not a layout without its docks
        if "param(" not in text:
            icon = QIcon(path)
        else:
            renderer = QSvgRenderer(_PARAM.sub("", text).encode("utf-8"))
            image = QImage(size, QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            view = renderer.viewBoxF()
            scale = min(size.width() / view.width(), size.height() / view.height())
            w, h = view.width() * scale, view.height() * scale
            renderer.render(painter, QRectF((size.width() - w) / 2, (size.height() - h) / 2, w, h))
            painter.end()
            icon = QIcon(QPixmap.fromImage(image))
        _icons[path] = icon
    return icon


def _svg_paths():
    v = QgsSettings().value(SVG_KEY, [])
    if isinstance(v, str):
        v = [v] if v else []
    return [p for p in (v or []) if p]


def register_svg_path():
    """Let QGIS store the arrows as relative SVG paths and show them in its SVG browser."""
    paths = _svg_paths()
    if SVG_DIR not in paths:
        QgsSettings().setValue(SVG_KEY, paths + [SVG_DIR])


def unregister_svg_path():
    paths = _svg_paths()
    if SVG_DIR in paths:
        QgsSettings().setValue(SVG_KEY, [p for p in paths if p != SVG_DIR])


class NorthArrowDock(ToolbarReveal, QDockWidget):
    def __init__(self, designer, catalog, parent=None):
        super().__init__("Khmer North Arrows", parent)
        self.setObjectName("KgaNorthArrowsDock")
        self.designer = designer
        self.catalog = catalog
        self.role = _e(Qt, "ItemDataRole", "UserRole")

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QFormLayout()
        self.style_box = QComboBox()
        self.style_box.addItems(STYLES)
        top.addRow("Style:", self.style_box)
        self.group_box = QComboBox()
        self.topic_box = QComboBox()
        self.palette_box = QComboBox()
        self.palette_box.addItems(["Colour", "Mono"])
        top.addRow("Group:", self.group_box)
        top.addRow("Topic:", self.topic_box)
        top.addRow("Colour:", self.palette_box)
        self.fill_btn = QgsColorButton()
        self.fill_btn.setColor(QColor("#000000"))
        self.line_btn = QgsColorButton()
        self.line_btn.setColor(QColor("#000000"))
        top.addRow("Fill:", self.fill_btn)
        top.addRow("Outline:", self.line_btn)
        self._top = top
        lay.addLayout(top)
        self._style_changed()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search (e.g. health, angkor, lotus, needle)...")
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.setViewMode(_e(QListView, "ViewMode", "IconMode"))
        self.list.setResizeMode(_e(QListView, "ResizeMode", "Adjust"))
        self.list.setMovement(_e(QListView, "Movement", "Static"))
        self.list.setIconSize(QSize(72, 90))
        self.list.setGridSize(QSize(100, 128))
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        lay.addWidget(self.list, 1)

        form = QFormLayout()
        self.width_box = QDoubleSpinBox()
        self.width_box.setRange(5, 300)
        self.width_box.setValue(25)
        self.width_box.setSuffix(" mm")
        form.addRow("Width:", self.width_box)
        self.corner_box = QComboBox()
        self.corner_box.addItems(CORNERS)
        form.addRow("Position:", self.corner_box)
        self.margin_box = QDoubleSpinBox()
        self.margin_box.setRange(0, 100)
        self.margin_box.setValue(10)
        self.margin_box.setSuffix(" mm")
        form.addRow("Margin:", self.margin_box)
        self.link_box = QCheckBox("Link to map (rotates with map)")
        self.link_box.setChecked(True)
        form.addRow(self.link_box)
        lay.addLayout(form)

        self.insert_btn = QPushButton("Insert into layout")
        lay.addWidget(self.insert_btn)
        hint = QLabel("Tip: double-click an arrow to insert it.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.setWidget(body)

        self.style_box.currentIndexChanged.connect(self._style_changed)
        self.group_box.currentIndexChanged.connect(self._fill_topics)
        self.topic_box.currentIndexChanged.connect(self.refresh)
        self.palette_box.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self.insert_btn.clicked.connect(self.insert)
        self.list.itemDoubleClicked.connect(lambda *_: self.insert())
        self.refresh()

    def _rows(self):
        style = self.style_box.currentText()
        return [c for c in self.catalog if c["style"] == style]

    def _style_changed(self, *_):
        khmer = self.style_box.currentText() == STYLES[0]
        for w in (self.group_box, self.topic_box, self.palette_box):
            w.setVisible(khmer)
            self._top.labelForField(w).setVisible(khmer)
        for w in (self.fill_btn, self.line_btn):
            w.setVisible(not khmer)
            self._top.labelForField(w).setVisible(not khmer)
        if hasattr(self, "search"):
            self.search.setPlaceholderText(
                "Search (e.g. health, angkor, lotus, needle)..." if khmer
                else "Search (e.g. N, arrow, U+0041)...")
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItems(["All groups"] + self._unique("group", self._rows()))
        self.group_box.blockSignals(False)
        self._fill_topics()

    def _unique(self, field, rows=None):
        out = []
        for c in rows if rows is not None else self.catalog:
            if c[field] not in out:
                out.append(c[field])
        return out

    def _fill_topics(self, *_):
        grp = self.group_box.currentText()
        rows = [c for c in self._rows() if grp == "All groups" or c["group"] == grp]
        self.topic_box.blockSignals(True)
        self.topic_box.clear()
        self.topic_box.addItems(["All topics"] + self._unique("topic", rows))
        self.topic_box.blockSignals(False)
        if hasattr(self, "list"):
            self.refresh()

    def refresh(self, *_):
        grp = self.group_box.currentText()
        top = self.topic_box.currentText()
        pal = self.palette_box.currentText()
        khmer = self.style_box.currentText() == STYLES[0]
        q = self.search.text().strip().lower()
        self.list.clear()
        self.list.setGridSize(QSize(100, 128) if khmer else QSize(84, 104))
        for c in self._rows():
            if khmer and c["palette"] != pal:
                continue
            if grp != "All groups" and c["group"] != grp:
                continue
            if top not in ("", "All topics") and c["topic"] != top:
                continue
            if q and q not in "{} {} {} {}".format(
                    c["title"], c["topic"], c["group"], c.get("search", "")).lower():
                continue
            if not khmer:
                label = ""      # the glyph names are code points, not readable
            else:
                label = c["variant"] if top not in ("", "All topics") else c["title"]
            item = QListWidgetItem(preview_icon(c["path"]), label)
            item.setToolTip("{} - {} ({})".format(c["group"], c["title"], c["palette"]))
            item.setData(self.role, c)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def insert(self):
        item = self.list.currentItem()
        layout = self.designer.layout()
        if item is None or layout is None:
            return
        c = item.data(self.role)
        w = self.width_box.value()
        h = w * float(c.get("aspect", 1.25))
        m = self.margin_box.value()
        page = layout.pageCollection().page(0).rect()
        corner = self.corner_box.currentText()
        x = page.width() - w - m if "right" in corner else m
        y = m if corner.startswith("Top") else page.height() - h - m

        stack = layout.undoStack()
        stack.beginMacro("Insert Khmer north arrow")
        pic = QgsLayoutItemPicture(layout)
        pic.setPicturePath(c["path"])
        if c["style"] != STYLES[0]:
            pic.setSvgFillColor(self.fill_btn.color())
            pic.setSvgStrokeColor(self.line_btn.color())
        pic.setResizeMode(_e(QgsLayoutItemPicture, "ResizeMode", "Zoom"))
        pic.setId("North Arrow - {}".format(c["title"]))
        layout.addLayoutItem(pic)
        pic.attemptMove(QgsLayoutPoint(x, y, _mm()))
        pic.attemptResize(QgsLayoutSize(w, h, _mm()))
        if self.link_box.isChecked():
            maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
            ref = layout.referenceMap() or (maps[0] if maps else None)
            if ref is not None:
                pic.setLinkedMap(ref)
                pic.setNorthMode(_e(QgsLayoutItemPicture, "NorthMode", "GridNorth"))
        stack.endMacro()
        layout.setSelectedItem(pic)
        layout.refresh()
        try:
            self.designer.messageBar().pushSuccess(
                "Khmer North Arrows", "Added {}.".format(c["title"]))
        except AttributeError:
            pass

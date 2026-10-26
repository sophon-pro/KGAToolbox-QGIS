# -*- coding: utf-8 -*-
"""
Live property editor for a symbol: one compact group per symbol layer with the
settings that matter for that layer type (line spacing and angle for hatches,
X/Y spacing for dot patterns, density for scattered patterns, widths, offsets,
intervals, sizes, colours …).
"""
from contextlib import suppress
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLabel, QDoubleSpinBox, QCheckBox, QFrame,
    QTabWidget, QScrollArea,
)
from qgis.gui import QgsColorButton

# layer type -> (title, [(label, getter, setter, kind, min, max, step)])
#   kind: "mm" | "deg" | "num" | "pct" | "color"
#   getter/setter names prefixed with "sub." act on the layer's sub-symbol
SPECS = {
    "SimpleFill": ("Fill", [
        ("Fill colour", "color", "setColor", "color"),
        ("Outline colour", "strokeColor", "setStrokeColor", "color"),
        ("Outline width", "strokeWidth", "setStrokeWidth", "mm", 0, 20, 0.05),
    ]),
    "LinePatternFill": ("Line hatch", [
        ("Line spacing", "distance", "setDistance", "mm", 0.1, 100, 0.1),
        ("Line angle", "lineAngle", "setLineAngle", "deg", -360, 360, 5),
        ("Line width", "sub.width", "sub.setWidth", "mm", 0.01, 20, 0.05),
        ("Line colour", "sub.color", "sub.setColor", "color"),
        ("Offset", "offset", "setOffset", "mm", -100, 100, 0.1),
    ]),
    "PointPatternFill": ("Dot / marker pattern", [
        ("Horizontal spacing", "distanceX", "setDistanceX", "mm", 0.1, 100, 0.2),
        ("Vertical spacing", "distanceY", "setDistanceY", "mm", 0.1, 100, 0.2),
        ("Row stagger", "displacementX", "setDisplacementX", "mm", -100, 100, 0.2),
        ("Marker size", "sub.size", "sub.setSize", "mm", 0.05, 50, 0.1),
        ("Marker colour", "sub.color", "sub.setColor", "color"),
    ]),
    "RandomMarkerFill": ("Scattered markers", [
        ("Area per marker", "densityArea", "setDensityArea", "num", 0.1, 10000, 1),
        ("Marker size", "sub.size", "sub.setSize", "mm", 0.05, 50, 0.1),
        ("Marker colour", "sub.color", "sub.setColor", "color"),
    ]),
    "ShapeburstFill": ("Shapeburst (soft edge)", [
        ("Colour", "color", "setColor", "color"),
        ("Width", "maxDistance", "setMaxDistance", "mm", 0.1, 100, 0.2),
        ("Blur", "blurRadius", "setBlurRadius", "int", 0, 17, 1),
    ]),
    "GradientFill": ("Gradient", [
        ("Colour 1", "color", "setColor", "color"),
        ("Colour 2", "color2", "setColor2", "color"),
        ("Angle", "angle", "setAngle", "deg", -360, 360, 5),
    ]),
    "SVGFill": ("SVG pattern", [
        ("Tile width", "patternWidth", "setPatternWidth", "mm", 0.5, 200, 0.5),
        ("Angle", "angle", "setAngle", "deg", -360, 360, 5),
    ]),
    "RasterFill": ("Texture", [
        ("Tile width", "width", "setWidth", "mm", 0.5, 500, 1),
        ("Opacity", "opacity", "setOpacity", "pct", 0, 1, 0.05),
    ]),
    "SimpleLine": ("Line", [
        ("Colour", "color", "setColor", "color"),
        ("Width", "width", "setWidth", "mm", 0.01, 50, 0.05),
        ("Offset", "offset", "setOffset", "mm", -100, 100, 0.1),
    ]),
    "MarkerLine": ("Markers along line", [
        ("Interval", "interval", "setInterval", "mm", 0.1, 500, 0.5),
        ("Offset", "offset", "setOffset", "mm", -100, 100, 0.1),
        ("Marker size", "sub.size", "sub.setSize", "mm", 0.05, 50, 0.1),
        ("Marker colour", "sub.color", "sub.setColor", "color"),
    ]),
    "HashLine": ("Ticks along line", [
        ("Interval", "interval", "setInterval", "mm", 0.1, 500, 0.2),
        ("Tick length", "hashLength", "setHashLength", "mm", 0.05, 50, 0.1),
        ("Tick angle", "hashAngle", "setHashAngle", "deg", -360, 360, 5),
        ("Offset", "offset", "setOffset", "mm", -100, 100, 0.1),
        ("Tick width", "sub.width", "sub.setWidth", "mm", 0.01, 20, 0.05),
        ("Tick colour", "sub.color", "sub.setColor", "color"),
    ]),
    "ArrowLine": ("Arrow", [
        ("Arrow width", "arrowWidth", "setArrowWidth", "mm", 0.05, 50, 0.1),
        ("Head length", "headLength", "setHeadLength", "mm", 0.1, 50, 0.2),
        ("Colour", "sub.color", "sub.setColor", "color"),
    ]),
    "SimpleMarker": ("Marker", [
        ("Size", "size", "setSize", "mm", 0.05, 100, 0.1),
        ("Fill colour", "color", "setColor", "color"),
        ("Outline colour", "strokeColor", "setStrokeColor", "color"),
        ("Outline width", "strokeWidth", "setStrokeWidth", "mm", 0, 10, 0.05),
        ("Rotation", "angle", "setAngle", "deg", -360, 360, 5),
    ]),
    "SvgMarker": ("Icon (SVG)", [
        ("Size", "size", "setSize", "mm", 0.1, 200, 0.2),
        ("Colour", "color", "setColor", "color"),
        ("Outline colour", "strokeColor", "setStrokeColor", "color"),
        ("Outline width", "strokeWidth", "setStrokeWidth", "mm", 0, 10, 0.05),
        ("Rotation", "angle", "setAngle", "deg", -360, 360, 5),
    ]),
    "FontMarker": ("Character marker", [
        ("Size", "size", "setSize", "mm", 0.1, 100, 0.2),
        ("Colour", "color", "setColor", "color"),
        ("Rotation", "angle", "setAngle", "deg", -360, 360, 5),
    ]),
}


def _target(layer, name):
    if name.startswith("sub."):
        sub = layer.subSymbol()
        return (sub, name[4:]) if sub is not None else (None, None)
    return layer, name


def _get(layer, name):
    obj, attr = _target(layer, name)
    if obj is None or not hasattr(obj, attr):
        return None
    try:
        return getattr(obj, attr)()
    except Exception:
        return None


def _set(layer, name, value):
    obj, attr = _target(layer, name)
    if obj is None or not hasattr(obj, attr):
        return False
    getattr(obj, attr)(value)
    return True


def _layer_titles(symbol):
    """A tab name per symbol layer; layers of one kind get told apart by what
    sets them apart (hatch angle, background vs top fill), never by a number."""
    n = symbol.symbolLayerCount()
    base = []
    for i in range(n):
        lyr = symbol.symbolLayer(i)
        spec = SPECS.get(lyr.layerType())
        base.append(spec[0] if spec else lyr.layerType())
    titles = list(base)
    for name in set(base):
        idx = [i for i in range(n) if base[i] == name]
        if len(idx) < 2:
            continue
        for i in idx:
            lyr = symbol.symbolLayer(i)
            t = lyr.layerType()
            if t == "LinePatternFill":
                titles[i] = "Hatch %d°" % round(lyr.lineAngle())
            elif t == "SimpleFill":
                titles[i] = "Background fill" if i == idx[0] else (
                    "Top fill" if i == idx[-1] else "Fill")
            elif t == "SimpleLine":
                titles[i] = "Base line" if i == idx[0] else (
                    "Top line" if i == idx[-1] else "Line")
            elif t in ("SimpleMarker", "SvgMarker", "FontMarker"):
                titles[i] = "Base marker" if i == idx[0] else (
                    "Top marker" if i == idx[-1] else "Marker")
    # anything still alike (same hatch angle, other layer types): add which one it is
    seen = {}
    for i in reversed(range(n)):
        seen[titles[i]] = seen.get(titles[i], 0) + 1
    for name, count in seen.items():
        if count > 1:
            same = [i for i in reversed(range(n)) if titles[i] == name]
            for k, i in enumerate(same):
                titles[i] = "%s (%s)" % (name, ("top", "next", "lower")[min(k, 2)])
    return titles


class SymbolPropertiesWidget(QWidget):
    """Shows editable properties of every layer of a QgsSymbol."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._symbol = None
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(6)
        self.empty = QLabel("Select a symbol to see its properties.")
        self.empty.setObjectName("muted")
        self._lay.addWidget(self.empty)
        self._lay.addStretch(1)
        self.tabs = None
        self._tab_index = 0

    def _clear(self):
        if self.tabs is not None:
            self._tab_index = max(0, self.tabs.currentIndex())
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None and w is not self.empty:
                w.deleteLater()
        self.tabs = None
        self.empty.setParent(None)

    def set_symbol(self, symbol, message=None, class_layer=None):
        """*symbol* is edited in place; emits changed() after every edit.

        *class_layer*: index of the layer that takes each class's colour – its
        colour editors are shown locked with a note."""
        self._symbol = symbol
        self._clear()
        if symbol is None:
            self.empty.setText(message or "Select a symbol to see its properties.")
            self._lay.addWidget(self.empty)
            self._lay.addStretch(1)
            return
        # one tab per symbol layer (top layer first, like the QGIS symbol editor)
        self.tabs = QTabWidget()
        self._lay.addWidget(self.tabs, 1)
        n = symbol.symbolLayerCount()
        titles = _layer_titles(symbol)
        shown = 0
        # top layer first, like the QGIS symbol editor
        for pos, i in enumerate(reversed(range(n))):
            lyr = symbol.symbolLayer(i)
            spec = SPECS.get(lyr.layerType())
            title = titles[i]
            box = QFrame()
            box.setObjectName("propBox")
            form = QFormLayout(box)
            form.setContentsMargins(6, 4, 6, 4)
            form.setHorizontalSpacing(8)
            form.setVerticalSpacing(3)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            head = QWidget()
            hl = QVBoxLayout(head)
            hl.setContentsMargins(0, 0, 0, 0)
            is_class = class_layer is not None and i == class_layer
            enabled = QCheckBox("Show this part%s" % (
                "  – colour from each class" if is_class else ""))
            enabled.setChecked(lyr.enabled())
            enabled.setToolTip("Show / hide this part of the symbol")
            enabled.toggled.connect(lambda v, lyr_=lyr: (lyr_.setEnabled(v), self.changed.emit()))
            f = enabled.font()
            f.setBold(True)
            enabled.setFont(f)
            hl.addWidget(enabled)
            form.addRow(head)
            rows = 0
            for row in (spec[1] if spec else []):
                label, getter, setter, kind = row[:4]
                value = _get(lyr, getter)
                if value is None:
                    continue
                w = self._editor(lyr, setter, kind, value, row[4:])
                if w is None:
                    continue
                if is_class and kind == "color" and setter in ("setColor", "sub.setColor"):
                    w.setEnabled(False)
                    w.setToolTip("Each class keeps its own colour here")
                form.addRow(label, w)
                rows += 1
            if not rows:
                form.addRow(QLabel("No quick settings – use Edit… for this layer."))
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.Shape.NoFrame)
            page.setWidget(box)
            self.tabs.addTab(page, title)
            shown += 1
        self.tabs.setCurrentIndex(min(self._tab_index, self.tabs.count() - 1))

    # ---------------------------------------------------------------- editors
    def _editor(self, lyr, setter, kind, value, rng):
        if kind == "color":
            b = QgsColorButton()
            b.setAllowOpacity(True)
            b.setColor(QColor(value))
            b.setMinimumWidth(60)
            b.colorChanged.connect(lambda c, lyr_=lyr, s=setter: self._apply(lyr_, s, QColor(c)))
            return b
        lo, hi, step = (rng + (0, 100, 1))[:3] if len(rng) < 3 else rng
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setKeyboardTracking(False)
        if kind == "mm":
            spin.setDecimals(2)
            spin.setSuffix(" mm")
        elif kind == "deg":
            spin.setDecimals(0)
            spin.setSuffix(" °")
        elif kind == "pct":
            spin.setDecimals(2)
        elif kind == "int":
            spin.setDecimals(0)
        else:
            spin.setDecimals(1)
        try:
            spin.setValue(float(value))
        except (TypeError, ValueError):
            return None
        if kind == "int":
            spin.valueChanged.connect(lambda v, lyr_=lyr, s=setter: self._apply(lyr_, s, int(v)))
        else:
            spin.valueChanged.connect(lambda v, lyr_=lyr, s=setter: self._apply(lyr_, s, float(v)))
        return spin

    def _apply(self, lyr, setter, value):
        with suppress(Exception):  # pragma: no cover - never break the panel
            if _set(lyr, setter, value):
                self.changed.emit()

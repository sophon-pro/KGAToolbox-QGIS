# -*- coding: utf-8 -*-
"""Basic statistics for one field – computed in a background QgsTask.

The task reads through a QgsVectorLayerFeatureSource (the thread-safe way to
read a layer outside the main thread), only the chosen attribute and no
geometry. The UI is updated in finished(), which QGIS runs on the main thread.
"""

from contextlib import suppress

import math
from array import array
from collections import Counter

from qgis.core import (NULL, QgsApplication, QgsFeature, QgsFeatureRequest, QgsTask, QgsVectorLayer,
                       QgsVectorLayerFeatureSource)
from qgis.gui import QgsFieldComboBox
from qgis.PyQt.QtCore import QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from qgis.PyQt.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
                                 QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ...core.compat import scoped
from . import compat as C
from .ui_common import icon, section_label

AUTO_LIMIT = 100000      # compute automatically up to this many features
BINS = 24
TOP_N = 6
PROGRESS_EVERY = 5000


def _is_null(v):
    if v is None or v == NULL:
        return True
    try:
        return hasattr(v, "isNull") and v.isNull()
    except Exception:
        return False


def fmt_num(v, integer=False):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "–"
    if integer and float(v).is_integer():
        return "{:,}".format(int(v))
    a = abs(v)
    if a >= 1e9 or (a and a < 1e-4):
        return "{:.4g}".format(v)
    if a >= 1000:
        return "{:,.2f}".format(v)
    return "{:,.4f}".format(v).rstrip("0").rstrip(".")


def _quantile(sorted_vals, q):
    n = len(sorted_vals)
    if n == 0:
        return None
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# ====================================================================== task
class StatsTask(QgsTask):
    def __init__(self, layer, field_name, fids=None, on_done=None):
        super().__init__("Statistics: {}".format(field_name), scoped(QgsTask, 'Flag', 'CanCancel'))
        self.source = QgsVectorLayerFeatureSource(layer)   # created on the main thread
        fields = layer.fields()
        self.idx = fields.indexOf(field_name)
        fld = fields.at(self.idx)
        self.field_name = field_name
        self.numeric = fld.isNumeric()
        tname = (fld.typeName() or "").lower()
        self.kind = "number" if self.numeric else ("date" if "date" in tname or "time" in tname else "text")
        self.integer = self.numeric and fld.precision() == 0 and "int" in tname
        self.fids = fids
        n = len(fids) if fids is not None else layer.featureCount()
        self.total = max(1, n if n and n > 0 else 1)
        self.result = None
        self.error = None
        self.on_done = on_done

    def run(self):
        try:
            req = QgsFeatureRequest()
            req.setFlags(C.no_geometry_flag())
            req.setSubsetOfAttributes([self.idx])
            if self.fids is not None:
                req.setFilterFids(self.fids)
            it = self.source.getFeatures(req)
            f = QgsFeature()
            count = nulls = 0
            nums = array("d")
            texts = Counter()
            dates = []
            seen = 0
            while it.nextFeature(f):
                seen += 1
                if seen % PROGRESS_EVERY == 0:
                    if self.isCanceled():
                        return False
                    self.setProgress(min(99.0, 100.0 * seen / self.total))
                v = f.attributes()[self.idx]
                if _is_null(v):
                    nulls += 1
                    continue
                count += 1
                if self.kind == "number":
                    try:
                        nums.append(float(v))
                    except (TypeError, ValueError):
                        nulls += 1
                        count -= 1
                elif self.kind == "date":
                    dates.append(v)
                else:
                    texts[str(v)] += 1
            self.result = self._summarise(count, nulls, nums, texts, dates)
            return True
        except Exception as e:  # reported in finished()
            self.error = str(e)
            return False

    def _summarise(self, count, nulls, nums, texts, dates):
        r = dict(field=self.field_name, kind=self.kind, count=count, nulls=nulls, integer=self.integer)
        if self.kind == "number":
            vals = sorted(nums)
            n = len(vals)
            if n:
                s = math.fsum(vals)
                mean = s / n
                var = math.fsum((x - mean) ** 2 for x in vals) / (n - 1) if n > 1 else 0.0
                r.update(sum=s, mean=mean, min=vals[0], max=vals[-1], range=vals[-1] - vals[0],
                         std=math.sqrt(var), median=_quantile(vals, 0.5), q1=_quantile(vals, 0.25),
                         q3=_quantile(vals, 0.75), unique=len(set(vals)))
                lo, hi = vals[0], vals[-1]
                bins = [0] * BINS
                if hi > lo:
                    w = (hi - lo) / BINS
                    for x in vals:
                        bins[min(BINS - 1, int((x - lo) / w))] += 1
                else:
                    bins[BINS // 2] = n
                r["hist"] = bins
        elif self.kind == "date":
            if dates:
                with suppress(TypeError):
                    ds = sorted(dates)
                    r.update(min=ds[0], max=ds[-1], unique=len(set(ds)))
        else:
            r["unique"] = len(texts)
            r["top"] = texts.most_common(TOP_N)
            if texts:
                lengths = [len(t) for t in texts]
                r.update(min_len=min(lengths), max_len=max(lengths))
        return r

    def finished(self, ok):
        if self.on_done is not None:
            self.on_done(self, ok)


# ====================================================================== widgets
class Histogram(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bins = []
        self.lo = self.hi = None
        self.accent = QColor("#2f74d8")
        self.text = QColor("#647487")
        self.setMinimumHeight(86)
        self.setSizePolicy(C.enum_value(QSizePolicy, "Policy", "Expanding"),
                           C.enum_value(QSizePolicy, "Policy", "Fixed"))

    def sizeHint(self):  # noqa: N802
        return QSize(200, 92)

    def set_data(self, bins, lo, hi, integer=False):
        self.bins, self.lo, self.hi, self.integer = list(bins or []), lo, hi, integer
        self.update()

    def paintEvent(self, e):  # noqa: N802
        if not self.bins:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -18)
        top = max(self.bins) or 1
        n = len(self.bins)
        gap = 2.0
        bw = (r.width() - gap * (n - 1)) / n
        for i, v in enumerate(self.bins):
            h = r.height() * v / top
            if v and h < 2:
                h = 2
            x = r.left() + i * (bw + gap)
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, r.bottom() - h, bw, h), min(3, bw / 2), min(3, bw / 2))
            c = QColor(self.accent)
            c.setAlphaF(0.35 + 0.65 * (v / top))
            p.fillPath(path, c)
        p.setPen(self.text)
        f = p.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        p.setFont(f)
        p.drawText(QRectF(self.rect()).adjusted(4, 0, -4, 0),
                   C.enum_value(Qt, "AlignmentFlag", "AlignBottom") | C.AlignLeft, fmt_num(self.lo, self.integer))
        p.drawText(QRectF(self.rect()).adjusted(4, 0, -4, 0),
                   C.enum_value(Qt, "AlignmentFlag", "AlignBottom") | C.enum_value(Qt, "AlignmentFlag", "AlignRight"),
                   fmt_num(self.hi, self.integer))
        p.end()


class StatTile(QFrame):
    def __init__(self, label, parent=None):
        super().__init__(parent)
        self.setObjectName("statTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 6, 9, 6)
        lay.setSpacing(0)
        self.value = QLabel("–")
        self.value.setObjectName("bigValue")
        self.value.setTextInteractionFlags(C.enum_value(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
        self.label = QLabel(label)
        self.label.setObjectName("tileLabel")
        lay.addWidget(self.value)
        lay.addWidget(self.label)

    def set(self, label, value, tip=None):
        self.label.setText(label)
        self.value.setText(value)
        self.setToolTip(tip or "{}: {}".format(label, value))


class StatsView(QWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.layer = None
        self.task = None
        self.result = None
        self._accent = QColor("#2f74d8")
        self.stats = dict(runs=0)
        self.on_result = None            # set by the dock (smart size)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.field = QgsFieldComboBox()
        self.field.setToolTip("Field to summarise")
        row.addWidget(self.field, 2)
        self.scope = QComboBox()
        self.scope.addItem("All features", "all")
        self.scope.addItem("Selected", "selected")
        self.scope.setToolTip("Summarise all features or only the selected ones")
        row.addWidget(self.scope, 1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.run_btn = QPushButton("Calculate")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.calculate)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.hide()
        self.cancel_btn = QToolButton()
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.setObjectName("textButton")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        self.copy_btn = QToolButton()
        self.copy_btn.setIcon(icon("copy"))
        self.copy_btn.setToolTip("Copy the statistics")
        self.copy_btn.clicked.connect(self.copy)
        self.copy_btn.setEnabled(False)
        self.state = QLabel()
        self.state.setObjectName("muted")
        row2.addWidget(self.run_btn)
        row2.addWidget(self.progress, 1)
        row2.addWidget(self.cancel_btn)
        row2.addWidget(self.state, 1)
        row2.addWidget(self.copy_btn)
        lay.addLayout(row2)

        self.tiles_box = QWidget()
        self.grid = QGridLayout(self.tiles_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.tiles = [StatTile("") for _ in range(12)]
        lay.addWidget(self.tiles_box)

        self.extra_title = section_label("DISTRIBUTION")
        lay.addWidget(self.extra_title)
        self.hist = Histogram()
        lay.addWidget(self.hist)
        self.top_box = QWidget()
        self.top_lay = QVBoxLayout(self.top_box)
        self.top_lay.setContentsMargins(0, 0, 0, 0)
        self.top_lay.setSpacing(3)
        lay.addWidget(self.top_box)
        lay.addStretch(1)

        self.field.fieldChanged.connect(lambda *_: self._auto())
        self.scope.activated.connect(lambda *_: self._auto())
        self._clear_results("Choose a field.")

    # ------------------------------------------------------------------ appearance
    def set_palette(self, p):
        self._accent = QColor(p["accent"])
        self.hist.accent = self._accent
        self.hist.text = QColor(p["sub"])
        self.hist.update()
        if self.result:
            self._show(self.result)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.result:
            self._layout_tiles(self._shown_tiles)

    # ------------------------------------------------------------------ layer
    def set_layer(self, layer):
        self.cancel()
        self.layer = layer if isinstance(layer, QgsVectorLayer) else None
        self.field.blockSignals(True)
        self.field.setLayer(self.layer)
        if self.layer is not None and self.layer.fields().count():
            # first numeric field is the most useful default
            pick = None
            for i in range(self.layer.fields().count()):
                if self.layer.fields().at(i).isNumeric():
                    pick = self.layer.fields().at(i).name()
                    break
            self.field.setField(pick or self.layer.fields().at(0).name())
        self.field.blockSignals(False)
        self.refresh_selection()
        self._clear_results("" if self.layer is not None else "Select a vector layer.")
        self._auto()

    def refresh_selection(self):
        n = self.layer.selectedFeatureCount() if self.layer is not None else 0
        self.scope.setItemText(1, "Selected ({:,})".format(n) if n else "Selected (none)")
        if n == 0 and self.scope.currentData() == "selected":
            self.scope.setCurrentIndex(0)

    def _auto(self):
        if self.layer is None or not self.isVisible():
            return
        n = self._target_count()
        if 0 <= n <= AUTO_LIMIT:
            QTimer.singleShot(0, self.calculate)
        else:
            self._clear_results("{:,} features – press Calculate (runs in the background).".format(n))

    def _target_count(self):
        if self.scope.currentData() == "selected":
            return self.layer.selectedFeatureCount()
        n = self.layer.featureCount()
        return n if n is not None else -1

    # ------------------------------------------------------------------ run
    def calculate(self):
        lyr = self.layer
        name = self.field.currentField()
        if lyr is None or not name or lyr.fields().indexOf(name) < 0:
            return
        self.cancel()
        fids = None
        if self.scope.currentData() == "selected":
            if lyr.selectedFeatureCount() == 0:
                self._clear_results("No features selected.")
                return
            fids = lyr.selectedFeatureIds()
        self.task = StatsTask(lyr, name, fids, on_done=self._done)
        self.task.progressChanged.connect(lambda v: self.progress.setValue(int(v)))
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_btn.show()
        self.run_btn.hide()
        self.state.setText("")
        self.stats["runs"] += 1
        QgsApplication.taskManager().addTask(self.task)

    def cancel(self):
        t = self.task
        self.task = None
        if t is not None:
            with suppress(RuntimeError):
                t.on_done = None
                t.cancel()
        self._idle()

    def _idle(self):
        self.progress.hide()
        self.cancel_btn.hide()
        self.run_btn.show()

    def _done(self, task, ok):
        if task is not self.task:
            return            # an older run that was replaced or cancelled
        self.task = None
        self._idle()
        if not ok:
            self._clear_results(task.error or "Cancelled.")
            return
        self.result = task.result
        self._show(self.result)

    # ------------------------------------------------------------------ results
    def _clear_results(self, msg):
        self.result = None
        self._shown_tiles = []
        self.state.setText(msg)
        self.copy_btn.setEnabled(False)
        for t in self.tiles:
            t.hide()
        self.extra_title.hide()
        self.hist.hide()
        self._clear_top()

    def _clear_top(self):
        while self.top_lay.count():
            w = self.top_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.top_box.hide()

    def _items(self, r):
        integer = r.get("integer")
        items = [("Count", "{:,}".format(r["count"])), ("NULL", "{:,}".format(r["nulls"]))]
        if r["kind"] == "number" and r["count"]:
            items += [("Sum", fmt_num(r["sum"], integer)), ("Mean", fmt_num(r["mean"])),
                      ("Median", fmt_num(r["median"], integer)), ("Std dev", fmt_num(r["std"])),
                      ("Min", fmt_num(r["min"], integer)), ("Max", fmt_num(r["max"], integer)),
                      ("Range", fmt_num(r["range"], integer)), ("Q1", fmt_num(r["q1"], integer)),
                      ("Q3", fmt_num(r["q3"], integer)), ("Unique", "{:,}".format(r["unique"]))]
        elif r["kind"] == "date" and r.get("min") is not None:
            def d(v):
                return v.toString(C.enum_value(Qt, "DateFormat", "ISODate")) if hasattr(v, "toString") else str(v)
            items += [("Earliest", d(r["min"])), ("Latest", d(r["max"])), ("Unique", "{:,}".format(r["unique"]))]
        elif r["kind"] == "text":
            items += [("Unique", "{:,}".format(r["unique"]))]
            if r.get("min_len") is not None:
                items += [("Shortest", "{} ch".format(r["min_len"])), ("Longest", "{} ch".format(r["max_len"]))]
        return items

    def _layout_tiles(self, items):
        cols = 3 if self.width() >= 330 else 2
        for t in self.tiles:
            self.grid.removeWidget(t)
            t.hide()
        for i, (label, value) in enumerate(items[:len(self.tiles)]):
            t = self.tiles[i]
            t.set(label, value)
            self.grid.addWidget(t, i // cols, i % cols)
            t.show()

    def _show(self, r):
        items = self._items(r)
        self._shown_tiles = items
        self._layout_tiles(items)
        scope = "selected features" if self.scope.currentData() == "selected" else "all features"
        self.state.setText("{} · {}".format(r["field"], scope))
        self.copy_btn.setEnabled(True)
        self._clear_top()
        if r["kind"] == "number" and r.get("hist"):
            self.extra_title.setText("DISTRIBUTION")
            self.extra_title.show()
            self.hist.set_data(r["hist"], r["min"], r["max"], r.get("integer"))
            self.hist.show()
        elif r["kind"] == "text" and r.get("top"):
            self.hist.hide()
            self.extra_title.setText("MOST COMMON")
            self.extra_title.show()
            top = r["top"][0][1] or 1
            for value, n in r["top"]:
                self.top_lay.addWidget(_TopRow(value, n, top, r["count"], self._accent))
            self.top_box.show()
        else:
            self.hist.hide()
            self.extra_title.hide()
        if self.on_result is not None:
            self.on_result()

    def copy(self):
        if not self.result:
            return
        lines = ["Field\t{}".format(self.result["field"])]
        lines += ["{}\t{}".format(k, v) for k, v in self._items(self.result)]
        if self.result.get("top"):
            lines += ["{}\t{}".format(v, n) for v, n in self.result["top"]]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.iface.statusBarIface().showMessage("Statistics copied", 2000)

    def cleanup(self):
        self.cancel()


class _TopRow(QWidget):
    """'value  ███████░░  1,234 (12%)' row for the most common values."""

    def __init__(self, value, n, top, total, accent, parent=None):
        super().__init__(parent)
        self.value, self.n, self.top, self.total, self.accent = value, n, top, total, QColor(accent)
        self.setMinimumHeight(20)
        self.setToolTip("{}: {:,} ({:.1f}%)".format(value, n, 100.0 * n / max(1, total)))

    def sizeHint(self):  # noqa: N802
        return QSize(200, 20)

    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        r = QRectF(self.rect())
        label_w = r.width() * 0.36
        count_txt = "{:,} ({:.0f}%)".format(self.n, 100.0 * self.n / max(1, self.total))
        count_w = p.fontMetrics().horizontalAdvance(count_txt) + 8
        bar = QRectF(r.left() + label_w + 6, r.top() + 5, max(10, r.width() - label_w - count_w - 12), r.height() - 10)
        bg = QColor(self.accent)
        bg.setAlphaF(0.12)
        path = QPainterPath()
        path.addRoundedRect(bar, 4, 4)
        p.fillPath(path, bg)
        fill = QRectF(bar.left(), bar.top(), bar.width() * self.n / max(1, self.top), bar.height())
        path2 = QPainterPath()
        path2.addRoundedRect(fill, 4, 4)
        p.fillPath(path2, self.accent)
        p.setPen(self.palette().color(self.foregroundRole()))
        fm = p.fontMetrics()
        p.drawText(QRectF(r.left(), r.top(), label_w, r.height()), C.AlignLeft | C.AlignVCenter,
                   fm.elidedText(self.value, C.ElideRight, int(label_w)))
        p.drawText(QRectF(r.right() - count_w, r.top(), count_w, r.height()),
                   C.enum_value(Qt, "AlignmentFlag", "AlignRight") | C.AlignVCenter, count_txt)
        p.end()

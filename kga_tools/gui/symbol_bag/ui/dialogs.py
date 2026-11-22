# -*- coding: utf-8 -*-
"""Symbol Bag – dialogs."""

from contextlib import suppress
import os

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
                                 QDialogButtonBox, QFormLayout, QFrame, QGroupBox,
                                 QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QMessageBox, QPlainTextEdit, QPushButton, QSlider,
                                 QSpinBox, QGridLayout, QTextBrowser, QVBoxLayout, QWidget)
from qgis.core import (QgsColorRampShader, QgsFillSymbol, QgsLineSymbol,
                       QgsMarkerSymbol, QgsRasterLayer, QgsRasterShader,
                       QgsSingleBandPseudoColorRenderer, QgsStyle)
from qgis.gui import (QgsBlendModeComboBox, QgsColorButton, QgsColorRampButton, QgsDoubleSpinBox,
                      QgsRendererRasterPropertiesWidget, QgsSymbolSelectorDialog, QgsSymbolWidgetContext)

from ..core.compat import shader_type
from ..core.storage import KIND_FILL, KIND_LABELS, KIND_LINE, KIND_MARKER, KIND_RASTER, LibraryError
from ..core.symbols import (raster_preview_image, raster_style_from_layer, raster_style_info,
                            sample_raster_layer, set_raster_stretch_flag, symbol_from_xml, symbol_kind,
                            symbol_to_xml, vector_preview, apply_raster_style, broken_preview,
                            RENDERER_LABELS)


# ----------------------------------------------------------------------
def fill_collection_combo(combo, store, first=("Unsorted", None), select=None):
    combo.clear()
    if first is not None:
        combo.addItem(first[0], first[1])
    cols = store.collections()
    children = {}
    for c in cols:
        children.setdefault(c["parent_id"], []).append(c)

    def add(parent, depth):
        for c in children.get(parent, []):
            combo.addItem(("    " * depth) + c["name"], c["id"])
            add(c["id"], depth + 1)
    add(None, 0)
    if select is not None:
        i = combo.findData(select)
        if i >= 0:
            combo.setCurrentIndex(i)


def candidate_pixmap(kind, xml, size=48):
    try:
        if kind == KIND_RASTER:
            return QPixmap.fromImage(raster_preview_image(xml, size))
        return vector_preview(symbol_from_xml(xml), size)
    except Exception:
        return broken_preview(size)


# ----------------------------------------------------------------------
class ImportPickerDialog(QDialog):
    """Pick which candidates to import, with previews and a target collection."""

    def __init__(self, candidates, store, title="Import Symbols", parent=None, default_collection=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 560)
        self.store = store
        self.candidates = candidates
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter…")
        self.filter_edit.setClearButtonEnabled(True)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("All types", "")
        kinds = sorted({c.kind for c in candidates})
        for k in kinds:
            self.kind_combo.addItem(KIND_LABELS.get(k, k), k)
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.kind_combo)
        lay.addLayout(top)

        self.list = QListWidget()
        self.list.setIconSize(QSize(40, 40))
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.list, 1)

        sel = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_none = QPushButton("Select none")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none.clicked.connect(lambda: self._set_all(False))
        self.summary = QLabel()
        sel.addWidget(b_all)
        sel.addWidget(b_none)
        sel.addStretch(1)
        sel.addWidget(self.summary)
        lay.addLayout(sel)

        form = QFormLayout()
        self.coll_combo = QComboBox()
        fill_collection_combo(self.coll_combo, store, select=default_collection)
        form.addRow("Add to collection:", self.coll_combo)
        self.keep_paths = QCheckBox("Recreate collections stored in the file")
        self.keep_paths.setChecked(True)
        self.keep_paths.setVisible(any(c.collection_path for c in candidates))
        form.addRow("", self.keep_paths)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("optional, comma separated – added to every imported symbol")
        form.addRow("Extra tags:", self.tags_edit)
        lay.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Import")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._populate()
        self.filter_edit.textChanged.connect(self._filter)
        self.kind_combo.currentIndexChanged.connect(self._filter)
        self.list.itemChanged.connect(self._update_summary)

    def _populate(self):
        self.list.blockSignals(True)
        for i, c in enumerate(self.candidates):
            dup = self.store.find_duplicate(c.kind, c.xml) is not None
            label = c.name + ("   (already in bag)" if dup else "")
            item = QListWidgetItem(QIcon(candidate_pixmap(c.kind, c.xml, 40)), label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setToolTip("{} · from {}".format(KIND_LABELS.get(c.kind, c.kind), c.source))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if dup else Qt.CheckState.Checked)
            if dup:
                item.setForeground(QColor("#888888"))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_summary()

    def _filter(self):
        text = self.filter_edit.text().lower()
        kind = self.kind_combo.currentData()
        for row in range(self.list.count()):
            item = self.list.item(row)
            c = self.candidates[item.data(Qt.ItemDataRole.UserRole)]
            hide = (text and text not in c.name.lower()) or (kind and c.kind != kind)
            item.setHidden(bool(hide))

    def _set_all(self, value):
        self.list.blockSignals(True)
        for row in range(self.list.count()):
            item = self.list.item(row)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._update_summary()

    def _update_summary(self, *_):
        n = sum(1 for r in range(self.list.count())
                if self.list.item(r).checkState() == Qt.CheckState.Checked)
        self.summary.setText("{} of {} selected".format(n, self.list.count()))
        self.ok_btn.setEnabled(n > 0)

    def selected_candidates(self):
        extra = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        out = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            c = self.candidates[item.data(Qt.ItemDataRole.UserRole)]
            c.selected = item.checkState() == Qt.CheckState.Checked
            if extra:
                tags = [t.strip() for t in c.tags.split(",") if t.strip()]
                c.tags = ", ".join(tags + [t for t in extra if t not in tags])
            if c.selected:
                out.append(c)
        return out

    def target_collection(self):
        return self.coll_combo.currentData()


# ----------------------------------------------------------------------
class RasterRendererDialog(QDialog):
    """Full native QGIS raster renderer editor on a scratch copy of a layer."""

    def __init__(self, xml, canvas, base_layer=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Raster Style")
        self.resize(520, 600)
        info = raster_style_info(xml)
        self.stretch = info["stretch"]
        if base_layer is not None and isinstance(base_layer, QgsRasterLayer) and base_layer.isValid():
            self.layer = QgsRasterLayer(base_layer.source(), base_layer.name(), base_layer.providerType())
            source_note = "Editing on a copy of '{}' – the layer itself is not changed.".format(base_layer.name())
        else:
            kind = "image" if info["renderer"] == "multibandcolor" else "dem"
            self.layer = sample_raster_layer(kind=kind)
            source_note = "Editing on Symbol Bag's sample terrain (select a raster layer to edit with real data)."
        with suppress(Exception):
            apply_raster_style(self.layer, xml, stretch=False)
        lay = QVBoxLayout(self)
        note = QLabel(source_note)
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)
        self.widget = QgsRendererRasterPropertiesWidget(self.layer, canvas, self)
        lay.addWidget(self.widget, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def result_xml(self):
        self.widget.apply()
        return raster_style_from_layer(self.layer, stretch=self.stretch)


# ----------------------------------------------------------------------
def default_symbol(kind):
    if kind == KIND_MARKER:
        return QgsMarkerSymbol.createSimple({"name": "circle", "color": "#2f80ed", "outline_color": "#1b2b3a",
                                             "size": "4", "outline_width": "0.4"})
    if kind == KIND_LINE:
        return QgsLineSymbol.createSimple({"line_color": "#2f80ed", "line_width": "0.8",
                                           "capstyle": "round", "joinstyle": "round"})
    return QgsFillSymbol.createSimple({"color": "#9cc3f5", "outline_color": "#1f5fbf", "outline_width": "0.4"})


def build_pseudocolor_xml(ramp, classes=6, discrete=False, stretch=True, blend=None):
    """Create a pseudocolor raster style from a QGIS colour ramp (0…1, stretched on apply)."""
    layer = sample_raster_layer(kind="dem")
    classes = max(2, classes)
    fn = QgsColorRampShader(0.0, 1.0)
    fn.setColorRampType(shader_type("Discrete" if discrete else "Interpolated"))
    items = []
    for i in range(classes):
        v = i / float(classes - 1)
        items.append(QgsColorRampShader.ColorRampItem(v, ramp.color(v), "{:g}".format(v)))
    fn.setColorRampItemList(items)
    fn.setSourceColorRamp(ramp.clone())
    shader = QgsRasterShader(0.0, 1.0)
    shader.setRasterShaderFunction(fn)
    r = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    r.setClassificationMin(0.0)
    r.setClassificationMax(1.0)
    layer.setRenderer(r)
    if blend is not None:
        layer.setBlendMode(blend)
    return raster_style_from_layer(layer, stretch=stretch)


class SymbolEditorDialog(QDialog):
    """Create a new symbol or edit an existing one (definition + properties)."""

    def __init__(self, store, iface, uid=None, kind=None, symbol=None, collection_id=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.iface = iface
        self.uid = uid
        rec = store.record(uid) if uid else None
        self.kind = rec.kind if rec else (kind or (symbol_kind(symbol) if symbol else KIND_MARKER))
        self.xml = store.symbol_xml(uid) if uid else None
        self.symbol = None
        self.raster_xml = None
        self.setWindowTitle("Edit Symbol" if rec else "Create Symbol")
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        body = QHBoxLayout()
        lay.addLayout(body)

        # --- left: preview
        left = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(150, 150)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.preview.setStyleSheet("background: white; border-radius: 8px;")
        left.addWidget(self.preview)
        self.type_combo = QComboBox()
        for k in (KIND_MARKER, KIND_LINE, KIND_FILL, KIND_RASTER):
            self.type_combo.addItem(KIND_LABELS[k], k)
        self.type_combo.setCurrentIndex(self.type_combo.findData(self.kind))
        self.type_combo.setEnabled(rec is None and symbol is None)
        left.addWidget(self.type_combo)
        left.addStretch(1)
        body.addLayout(left)

        # --- right: properties
        right = QVBoxLayout()
        form = QFormLayout()
        self.name_edit = QLineEdit(rec.name if rec else "")
        self.name_edit.setPlaceholderText("Symbol name")
        form.addRow("Name:", self.name_edit)
        self.coll_combo = QComboBox()
        fill_collection_combo(self.coll_combo, store, select=rec.collection_id if rec else collection_id)
        form.addRow("Collection:", self.coll_combo)
        self.tags_edit = QLineEdit(rec.tags if rec else "")
        self.tags_edit.setPlaceholderText("e.g. road, transport")
        form.addRow("Tags:", self.tags_edit)
        self.desc_edit = QPlainTextEdit(rec.description if rec else "")
        self.desc_edit.setFixedHeight(54)
        form.addRow("Description:", self.desc_edit)
        self.fav_check = QCheckBox("Favorite")
        self.fav_check.setChecked(bool(rec.favorite) if rec else False)
        form.addRow("", self.fav_check)
        right.addLayout(form)

        # vector quick properties
        self.vec_box = QGroupBox("Quick style")
        vf = QFormLayout(self.vec_box)
        self.fill_btn = QgsColorButton()
        self.fill_btn.setAllowOpacity(True)
        self.stroke_btn = QgsColorButton()
        self.stroke_btn.setAllowOpacity(True)
        self.size_spin = QgsDoubleSpinBox()
        self.size_spin.setRange(0.0, 200.0)
        self.size_spin.setDecimals(2)
        self.size_spin.setSingleStep(0.2)
        self.size_spin.setSuffix(" mm")
        self.stroke_spin = QgsDoubleSpinBox()
        self.stroke_spin.setRange(0.0, 50.0)
        self.stroke_spin.setDecimals(2)
        self.stroke_spin.setSingleStep(0.1)
        self.stroke_spin.setSuffix(" mm")
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.size_label = QLabel("Size:")
        self.color_label = QLabel("Color:")
        self.stroke_label = QLabel("Outline color:")
        self.stroke_w_label = QLabel("Outline width:")
        vf.addRow(self.color_label, self.fill_btn)
        vf.addRow(self.stroke_label, self.stroke_btn)
        vf.addRow(self.size_label, self.size_spin)
        vf.addRow(self.stroke_w_label, self.stroke_spin)
        vf.addRow("Opacity:", self.opacity_slider)
        self.layers_label = QLabel()
        self.layers_label.setStyleSheet("color: gray;")
        vf.addRow("", self.layers_label)
        self.adv_btn = QPushButton("Advanced Editor (QGIS Symbol Selector)…")
        self.adv_btn.setToolTip("Edit every symbol layer: add layers, SVG / font markers, patterns, "
                                "data-defined properties, effects…")
        vf.addRow(self.adv_btn)
        right.addWidget(self.vec_box)

        # raster properties
        self.ras_box = QGroupBox("Raster style")
        rf = QFormLayout(self.ras_box)
        self.renderer_label = QLabel()
        rf.addRow("Renderer:", self.renderer_label)
        self.ramp_btn = QgsColorRampButton()
        self.ramp_btn.setShowGradientOnly(False)
        self.ramp_btn.setColorRampFromName("Spectral")
        self.classes_spin = QSpinBox()
        self.classes_spin.setRange(2, 32)
        self.classes_spin.setValue(7)
        self.discrete_check = QCheckBox("Discrete classes")
        self.rebuild_btn = QPushButton("Build pseudocolor style from ramp")
        rf.addRow("Color ramp:", self.ramp_btn)
        rf.addRow("Classes:", self.classes_spin)
        rf.addRow("", self.discrete_check)
        rf.addRow(self.rebuild_btn)
        self.stretch_check = QCheckBox("Stretch to each target layer's min/max when applied")
        rf.addRow(self.stretch_check)
        self.blend_combo = QgsBlendModeComboBox()
        rf.addRow("Blending:", self.blend_combo)
        self.edit_ras_btn = QPushButton("Edit Renderer (QGIS raster properties)…")
        rf.addRow(self.edit_ras_btn)
        right.addWidget(self.ras_box)
        right.addStretch(1)
        body.addLayout(right, 1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        # state
        if self.kind == KIND_RASTER:
            self.raster_xml = self.xml or build_pseudocolor_xml(self.ramp_btn.colorRamp(), 7)
        else:
            if symbol is not None:
                self.symbol = symbol.clone()
            elif self.xml:
                self.symbol = symbol_from_xml(self.xml) or default_symbol(self.kind)
            else:
                self.symbol = default_symbol(self.kind)
        self._load_controls()

        self.type_combo.currentIndexChanged.connect(self._type_changed)
        for w in (self.fill_btn, self.stroke_btn):
            w.colorChanged.connect(self._quick_changed)
        self.size_spin.valueChanged.connect(self._quick_changed)
        self.stroke_spin.valueChanged.connect(self._quick_changed)
        self.opacity_slider.valueChanged.connect(self._quick_changed)
        self.adv_btn.clicked.connect(self._advanced)
        self.rebuild_btn.clicked.connect(self._rebuild_raster)
        self.edit_ras_btn.clicked.connect(self._edit_raster)
        self.stretch_check.toggled.connect(self._stretch_changed)
        self.blend_combo.currentIndexChanged.connect(self._blend_changed)
        if not rec:
            self.name_edit.setFocus()

    # ------------------------------------------------------------------
    def _type_changed(self):
        self.kind = self.type_combo.currentData()
        if self.kind == KIND_RASTER:
            self.symbol = None
            if self.raster_xml is None:
                self.raster_xml = build_pseudocolor_xml(self.ramp_btn.colorRamp(), self.classes_spin.value())
        else:
            self.symbol = default_symbol(self.kind)
        self._load_controls()

    def _load_controls(self):
        is_r = self.kind == KIND_RASTER
        self.vec_box.setVisible(not is_r)
        self.ras_box.setVisible(is_r)
        widgets = (self.fill_btn, self.stroke_btn, self.size_spin, self.stroke_spin, self.opacity_slider,
                   self.stretch_check, self.blend_combo)
        for w in widgets:
            w.blockSignals(True)
        if is_r:
            info = raster_style_info(self.raster_xml)
            self.renderer_label.setText(RENDERER_LABELS.get(info["renderer"], info["renderer"] or "?"))
            self.stretch_check.setChecked(info["stretch"])
            layer = sample_raster_layer()
            with suppress(Exception):
                apply_raster_style(layer, self.raster_xml, stretch=False)
                self.blend_combo.setBlendMode(layer.blendMode())
        else:
            s = self.symbol
            self.fill_btn.setColor(s.color())
            sl = s.symbolLayer(0) if s.symbolLayerCount() else None
            stroke_color = sl.strokeColor() if sl is not None and hasattr(sl, "strokeColor") else QColor("black")
            self.stroke_btn.setColor(stroke_color)
            if self.kind == KIND_MARKER:
                self.size_label.setText("Size:")
                self.size_spin.setValue(s.size())
            elif self.kind == KIND_LINE:
                self.size_label.setText("Line width:")
                self.size_spin.setValue(s.width())
            self.size_spin.setVisible(self.kind != KIND_FILL)
            self.size_label.setVisible(self.kind != KIND_FILL)
            is_line = self.kind == KIND_LINE
            self.color_label.setText("Line color:" if is_line else ("Fill color:" if self.kind == KIND_FILL else "Color:"))
            for w in (self.stroke_label, self.stroke_btn, self.stroke_w_label, self.stroke_spin):
                w.setVisible(not is_line)
            sw = 0.0
            if sl is not None and hasattr(sl, "strokeWidth"):
                try:
                    sw = sl.strokeWidth()
                except Exception:
                    sw = 0.0
            self.stroke_spin.setValue(sw)
            self.opacity_slider.setValue(int(round(s.opacity() * 100)))
            self.layers_label.setText("{} symbol layer(s)".format(s.symbolLayerCount()))
        for w in widgets:
            w.blockSignals(False)
        self._update_preview()

    def _quick_changed(self, *_):
        s = self.symbol
        if s is None:
            return
        s.setColor(self.fill_btn.color())
        for i in range(s.symbolLayerCount()):
            sl = s.symbolLayer(i)
            if hasattr(sl, "setStrokeColor"):
                sl.setStrokeColor(self.stroke_btn.color())
            if hasattr(sl, "setStrokeWidth") and self.kind != KIND_LINE:
                sl.setStrokeWidth(self.stroke_spin.value())
        if self.kind == KIND_MARKER:
            s.setSize(self.size_spin.value())
        elif self.kind == KIND_LINE:
            s.setWidth(self.size_spin.value())
        s.setOpacity(self.opacity_slider.value() / 100.0)
        self._update_preview()

    def _advanced(self):
        sym = self.symbol.clone()
        layer = self.iface.activeLayer() if self.iface else None
        from qgis.core import QgsVectorLayer
        if not isinstance(layer, QgsVectorLayer):
            layer = None
        dlg = QgsSymbolSelectorDialog(sym, QgsStyle.defaultStyle(), layer, self)
        if self.iface is not None:
            ctx = QgsSymbolWidgetContext()
            ctx.setMapCanvas(self.iface.mapCanvas())
            ctx.setMessageBar(self.iface.messageBar())
            dlg.setContext(ctx)
        if dlg.exec():
            self.symbol = sym
            self._load_controls()

    def _rebuild_raster(self):
        ramp = self.ramp_btn.colorRamp()
        if ramp is None:
            return
        self.raster_xml = build_pseudocolor_xml(ramp, self.classes_spin.value(), self.discrete_check.isChecked(),
                                                stretch=True)
        self._load_controls()

    def _edit_raster(self):
        base = self.iface.activeLayer() if self.iface else None
        dlg = RasterRendererDialog(self.raster_xml, self.iface.mapCanvas() if self.iface else None,
                                   base if isinstance(base, QgsRasterLayer) else None, self)
        if dlg.exec():
            try:
                self.raster_xml = dlg.result_xml()
            except Exception as e:
                QMessageBox.warning(self, "Symbol Bag", "Could not read the edited style:\n{}".format(e))
            self._load_controls()

    def _stretch_changed(self, value):
        self.raster_xml = set_raster_stretch_flag(self.raster_xml, value)

    def _blend_changed(self, *_):
        layer = sample_raster_layer()
        with suppress(Exception):
            apply_raster_style(layer, self.raster_xml, stretch=False)
            layer.setBlendMode(self.blend_combo.blendMode())
            self.raster_xml = raster_style_from_layer(layer, stretch=self.stretch_check.isChecked())

    def _update_preview(self):
        if self.kind == KIND_RASTER:
            try:
                pm = QPixmap.fromImage(raster_preview_image(self.raster_xml, 140))
            except Exception:
                pm = broken_preview(140)
        else:
            pm = vector_preview(self.symbol, 140)
        self.preview.setPixmap(pm)

    # ------------------------------------------------------------------
    def _save(self):
        name = self.name_edit.text().strip()
        if not name:
            self.name_edit.setFocus()
            self.name_edit.setPlaceholderText("Please enter a name")
            return
        if self.kind == KIND_RASTER:
            xml = self.raster_xml
            subtype = raster_style_info(xml)["renderer"]
        else:
            xml = symbol_to_xml(self.symbol)
            subtype = ""
        fields = dict(name=name, tags=self.tags_edit.text(), description=self.desc_edit.toPlainText().strip(),
                      collection_id=self.coll_combo.currentData(), favorite=self.fav_check.isChecked())
        try:
            if self.uid:
                if xml != self.xml:
                    fields.update(xml=xml, subtype=subtype, kind=self.kind)
                self.store.update_symbol(self.uid, **fields)
            else:
                self.uid, _ = self.store.add_symbol(name, self.kind, xml, subtype, fields["description"],
                                                    fields["tags"], fields["collection_id"], fields["favorite"],
                                                    allow_duplicate=True)
        except LibraryError as e:
            QMessageBox.warning(self, "Symbol Bag", str(e))
            return
        self.accept()


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "sound": True,           # sound effects with the Genie motion
    "sound_style": "whoosh", # whoosh | bubble | chime | magic
    "sound_volume": 70,      # %
    # look (Bag Style dialog)
    "style": "mint",
    "background": 86,        # % opacity of the bag body (map shows through)
    "fade": 100,             # % opacity of the whole bag
    "top": "ring",           # ring | handle | none
    "compact": False,
    "show_as": "grid_names",  # grid_names | grid_icons | list
    "columns": 0,            # 0 = automatic
    "preview_size": 44,
    "animations": True,
    "motion": "normal",      # off | snappy | normal | relaxed
    "bubble": True,          # minimize to a bubble instead of folding up
    "bubble_size": 44,
    "bubble_pos": 2,         # 0..8 = 3x3 grid over the map canvas (2 = top right)
    # behaviour (Preferences)
    "smart": True,           # filter by the selected layer's geometry type
    "double_click": "apply",
    "auto_backups": 5,
    "recent_limit": 24,
    "sort": "manual",
}

SHOW_AS = [("grid_names", "Grid – icons with names"), ("grid_icons", "Grid – icons only"), ("list", "List")]
ICON_SIZES = [(32, "Small"), (44, "Medium"), (60, "Large"), (80, "Extra large")]
TOPS = [("ring", "Ring (key ring)"), ("handle", "Handle"), ("none", "None")]


class BagThumb(QWidget):
    """Clickable bag illustration in the style picker."""

    def __init__(self, key, on_pick, parent=None):
        super().__init__(parent)
        from .theme import BAG_STYLES
        self.key = key
        self.label = BAG_STYLES[key]["label"]
        self.on_pick = on_pick
        self.selected = False
        self.setFixedSize(112, 112)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self.label)

    def paintEvent(self, event):
        from qgis.PyQt.QtCore import QRectF
        from qgis.PyQt.QtGui import QPainter, QPen
        from .theme import paint_bag_thumbnail
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.selected:
            p.setPen(QPen(QColor("#4c8be0"), 2))
            p.setBrush(QColor(76, 139, 224, 30))
            p.drawRoundedRect(r, 10, 10)
        paint_bag_thumbnail(p, QRectF(r.left(), r.top() + 2, r.width(), r.height() - 22), self.key)
        p.setPen(QColor("#333333"))
        p.drawText(QRectF(r.left(), r.bottom() - 20, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.label)
        p.end()

    def mousePressEvent(self, event):
        self.on_pick(self.key)


class StyleOnlyDialog(QDialog):
    """Just the colour: the bag illustrations, applied live to one window (Layer Colors)."""

    def __init__(self, current, on_change, parent=None, title="Bag Style"):
        super().__init__(parent)
        from .theme import STYLE_ORDER
        self.setWindowTitle(title)
        self.original = current
        self.value = current
        self.on_change = on_change
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Choose the colour of Layer Colors</b>"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.thumbs = {}
        for i, key in enumerate(STYLE_ORDER):
            t = BagThumb(key, self._pick, self)
            t.selected = key == current
            self.thumbs[key] = t
            grid.addWidget(t, i // 3, i % 3, Qt.AlignmentFlag.AlignCenter)
        lay.addLayout(grid)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _pick(self, key):
        self.value = key
        for k, t in self.thumbs.items():
            t.selected = k == key
            t.update()
        self.on_change(key)

    def reject(self):
        self.on_change(self.original)
        super().reject()


class BagStyleDialog(QDialog):
    """'Choose your bag' – colourway, transparency, top decoration, layout and bubble. Live preview."""

    def __init__(self, settings, on_change, parent=None):
        super().__init__(parent)
        from .theme import STYLE_ORDER
        self.setWindowTitle("Bag Style")
        self.original = dict(settings)
        self.values = dict(settings)
        self.on_change = on_change
        lay = QVBoxLayout(self)
        title = QLabel("<b>Choose your bag</b>")
        lay.addWidget(title)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.thumbs = {}
        for i, key in enumerate(STYLE_ORDER):
            t = BagThumb(key, self._pick, self)
            self.thumbs[key] = t
            grid.addWidget(t, i // 3, i % 3, Qt.AlignmentFlag.AlignCenter)
        lay.addLayout(grid)
        self._mark(settings["style"])

        form = QGridLayout()
        form.setColumnStretch(1, 1)
        row = 0

        def slider_row(label, lo, hi, value):
            nonlocal row
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(value)
            val = QLabel("{}%".format(value))
            val.setMinimumWidth(36)
            sl.valueChanged.connect(lambda v: val.setText("{}%".format(v)))
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(sl, row, 1, 1, 2)
            form.addWidget(val, row, 3)
            row += 1
            return sl

        self.background = slider_row("Background", 25, 100, settings["background"])
        self.fade = slider_row("Fade all", 35, 100, settings["fade"])

        self.top = QComboBox()
        for k, lbl in TOPS:
            self.top.addItem(lbl, k)
        self.top.setCurrentIndex(max(0, self.top.findData(settings["top"])))
        self.compact = QCheckBox("Compact")
        self.compact.setChecked(settings["compact"])
        form.addWidget(QLabel("Top"), row, 0)
        form.addWidget(self.top, row, 1)
        form.addWidget(self.compact, row, 2, 1, 2)
        row += 1

        self.show_as = QComboBox()
        for k, lbl in SHOW_AS:
            self.show_as.addItem(lbl, k)
        self.show_as.setCurrentIndex(max(0, self.show_as.findData(settings["show_as"])))
        self.columns = QComboBox()
        self.columns.addItem("Auto", 0)
        for n in range(1, 7):
            self.columns.addItem(str(n), n)
        self.columns.setCurrentIndex(max(0, self.columns.findData(settings["columns"])))
        form.addWidget(QLabel("Symbols"), row, 0)
        form.addWidget(self.show_as, row, 1)
        cl = QHBoxLayout()
        cl.addWidget(QLabel("Columns"))
        cl.addWidget(self.columns)
        form.addLayout(cl, row, 2, 1, 2)
        row += 1

        self.icon_size = QComboBox()
        for px, lbl in ICON_SIZES:
            self.icon_size.addItem("{} ({} px)".format(lbl, px), px)
        idx = self.icon_size.findData(settings["preview_size"])
        if idx < 0:
            self.icon_size.addItem("{} px".format(settings["preview_size"]), settings["preview_size"])
            idx = self.icon_size.count() - 1
        self.icon_size.setCurrentIndex(idx)
        from .motion import SPEEDS
        motion_box = QHBoxLayout()
        motion_box.addWidget(QLabel("Motion"))
        self.animations = QComboBox()
        for key, label, _f in SPEEDS:
            self.animations.addItem(label, key)
        current = settings.get("motion", "normal") if settings.get("animations", True) else "off"
        self.animations.setCurrentIndex(max(0, self.animations.findData(current)))
        self.animations.setToolTip("Spring motion for opening, the bubble, folding, the category\n"
                                   "switch and the cards. 'Off' turns all animation off.")
        motion_box.addWidget(self.animations, 1)
        form.addWidget(QLabel("Icon size"), row, 0)
        form.addWidget(self.icon_size, row, 1)
        form.addLayout(motion_box, row, 2, 1, 2)
        row += 1
        lay.addLayout(form)

        # bubble
        bub = QHBoxLayout()
        left = QVBoxLayout()
        self.bubble = QCheckBox("Minimize to a bubble")
        self.bubble.setChecked(settings["bubble"])
        left.addWidget(self.bubble)
        srow = QHBoxLayout()
        self.size_caption = QLabel("Size")
        srow.addWidget(self.size_caption)
        self.bubble_size = QSlider(Qt.Orientation.Horizontal)
        self.bubble_size.setRange(32, 72)
        self.bubble_size.setValue(settings["bubble_size"])
        self.bubble_lbl = QLabel("{} px".format(settings["bubble_size"]))
        self.bubble_size.valueChanged.connect(lambda v: self.bubble_lbl.setText("{} px".format(v)))
        srow.addWidget(self.bubble_size, 1)
        srow.addWidget(self.bubble_lbl)
        left.addLayout(srow)
        self.bubble_hint = QLabel("The bag hides in the bubble.\nPlace it on the map:")
        self.bubble_hint.setStyleSheet("color: gray;")
        left.addWidget(self.bubble_hint)
        bub.addLayout(left, 1)
        self.pos_group = QButtonGroup(self)
        pos_grid = QGridLayout()
        pos_grid.setSpacing(4)
        frame = QFrame()
        frame.setStyleSheet("QFrame { background: #eef3fa; border-radius: 8px; }"
                            "QToolButton { background: white; border: 1px solid #c9d4e3; border-radius: 9px; }"
                            "QToolButton:checked { background: #5b8def; border-color: #3d6fd1; }")
        frame.setLayout(pos_grid)
        self.pos_frame = frame
        from qgis.PyQt.QtWidgets import QToolButton
        for i in range(9):
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(26, 18)
            self.pos_group.addButton(b, i)
            pos_grid.addWidget(b, i // 3, i % 3)
        self.pos_group.button(settings["bubble_pos"] if 0 <= settings["bubble_pos"] <= 8 else 2).setChecked(True)
        bub.addWidget(frame)
        lay.addLayout(bub)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                              | QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._defaults)
        lay.addWidget(bb)

        for sig in (self.background.valueChanged, self.fade.valueChanged, self.top.currentIndexChanged,
                    self.compact.toggled, self.show_as.currentIndexChanged, self.columns.currentIndexChanged,
                    self.icon_size.currentIndexChanged, self.animations.currentIndexChanged, self.bubble.toggled,
                    self.bubble_size.valueChanged, self.pos_group.buttonClicked):
            sig.connect(self._changed)

    def _mark(self, key):
        for k, t in self.thumbs.items():
            t.selected = k == key
            t.update()

    def _pick(self, key):
        self.values["style"] = key
        self._mark(key)
        self._changed()

    def _collect(self):
        v = self.values
        v["background"] = self.background.value()
        v["fade"] = self.fade.value()
        v["top"] = self.top.currentData()
        v["compact"] = self.compact.isChecked()
        v["show_as"] = self.show_as.currentData()
        v["columns"] = self.columns.currentData()
        v["preview_size"] = self.icon_size.currentData()
        v["motion"] = self.animations.currentData()
        v["animations"] = v["motion"] != "off"
        v["bubble"] = self.bubble.isChecked()
        v["bubble_size"] = self.bubble_size.value()
        v["bubble_pos"] = max(0, self.pos_group.checkedId())
        return v

    def _changed(self, *_):
        self.on_change(dict(self._collect()))

    def _defaults(self):
        d = DEFAULT_SETTINGS
        self._pick(d["style"])
        self.background.setValue(d["background"])
        self.fade.setValue(d["fade"])
        self.top.setCurrentIndex(self.top.findData(d["top"]))
        self.compact.setChecked(d["compact"])
        self.show_as.setCurrentIndex(self.show_as.findData(d["show_as"]))
        self.columns.setCurrentIndex(self.columns.findData(d["columns"]))
        self.icon_size.setCurrentIndex(self.icon_size.findData(d["preview_size"]))
        self.animations.setCurrentIndex(self.animations.findData(d["motion"]))
        self.bubble.setChecked(d["bubble"])
        self.bubble_size.setValue(d["bubble_size"])
        self.pos_group.button(d["bubble_pos"]).setChecked(True)
        self._changed()

    def reject(self):
        self.on_change(dict(self.original))
        super().reject()


class SettingsDialog(QDialog):
    """Behaviour preferences."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Symbol Bag Preferences")
        self.values = dict(settings)
        lay = QVBoxLayout(self)
        h = QFormLayout()
        self.smart = QCheckBox("Show only symbols that fit the selected layer")
        self.smart.setChecked(settings["smart"])
        h.addRow("Smart filter:", self.smart)
        self.dbl = QComboBox()
        self.dbl.addItem("Apply to selected layer(s)", "apply")
        self.dbl.addItem("Edit symbol", "edit")
        self.dbl.setCurrentIndex(max(0, self.dbl.findData(settings["double_click"])))
        h.addRow("Double-click:", self.dbl)
        self.recent = QSpinBox()
        self.recent.setRange(5, 200)
        self.recent.setValue(settings["recent_limit"])
        h.addRow("Recently used list:", self.recent)
        self.backups = QSpinBox()
        self.backups.setRange(0, 50)
        self.backups.setSpecialValueText("Off")
        self.backups.setValue(settings["auto_backups"])
        h.addRow("Automatic backups kept:", self.backups)
        lay.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def accept(self):
        v = self.values
        v["smart"] = self.smart.isChecked()
        v["double_click"] = self.dbl.currentData()
        v["recent_limit"] = self.recent.value()
        v["auto_backups"] = self.backups.value()
        super().accept()


# ----------------------------------------------------------------------
class AboutDialog(QDialog):
    def __init__(self, version, store_path, help_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Symbol Bag")
        self.resize(500, 520)
        lay = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        html = ""
        if os.path.exists(help_path):
            with open(help_path, "r", encoding="utf-8") as fh:
                html = fh.read()
        html = html.replace("{{VERSION}}", version).replace("{{LIBRARY}}", store_path)
        tb.setHtml(html)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)

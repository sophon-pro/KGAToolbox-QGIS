# -*- coding: utf-8 -*-
"""
The "Current style" page for point / line / polygon symbols: main colour, outline,
size or width, and every part of the symbol (line spacing and angle of hatches, dot
spacing, tick interval…). It is a page of the gallery panel, reached by applying a
symbol and left with "Back to styles".

* "after"  – the symbol is already applied; changes show on the map live.
* "before" – nothing changes on the layer until commit() (the Apply / OK buttons of
  Layer Properties).

The page edits its OWN copy of the symbol: it changes only the layer it was
applied to, never the symbol stored in the gallery.
"""
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox, QPushButton,
    QFrame, QScrollArea, QCheckBox,
)
from qgis.gui import QgsColorButton

from ..core import apply as applier
from ..core import previews
from ..core.compat import SYMBOL_MARKER, SYMBOL_LINE
from .props import SymbolPropertiesWidget

CURRENT_ID = "__current__"     # the pseudo entry for the layer's own symbol


class SymbolQuickPopup(QWidget):
    def __init__(self, dock):
        super().__init__(dock)
        self.dock = dock
        self.dirty = False
        self.entry = None
        self.layer = None
        self.mode = "after"
        self.symbol = None
        self._loading = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)          # live map update, debounced
        self._timer.timeout.connect(self._live)
        self._build()
        self._loading = False

    # ------------------------------------------------------------------ UI
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QHBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(84, 56)
        self.preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(220)
        head.addWidget(self.preview)
        head.addWidget(self.title, 1)
        lay.addLayout(head)

        quick = QHBoxLayout()
        quick.addWidget(QLabel("Colour"))
        self.col_btn = QgsColorButton()
        self.col_btn.setAllowOpacity(True)
        self.col_btn.setMinimumWidth(48)
        quick.addWidget(self.col_btn)
        quick.addWidget(QLabel("Outline"))
        self.out_btn = QgsColorButton()
        self.out_btn.setAllowOpacity(True)
        self.out_btn.setMinimumWidth(48)
        quick.addWidget(self.out_btn)
        self.size_lbl = QLabel("Size")
        quick.addWidget(self.size_lbl)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setDecimals(2)
        self.size_spin.setRange(0.05, 100)
        self.size_spin.setSingleStep(0.1)
        self.size_spin.setSuffix(" mm")
        self.size_spin.setKeyboardTracking(False)
        quick.addWidget(self.size_spin)
        quick.addStretch(1)
        lay.addLayout(quick)

        self.class_cb = QCheckBox("Keep each class's colour")
        self.class_cb.setToolTip(
            "On: every Categorized / Graduated / Rule class keeps its own colour and takes "
            "only the pattern, widths and other parts from this symbol.\n"
            "Off: all classes get exactly this symbol, including its colour.")
        lay.addWidget(self.class_cb)

        self.props = SymbolPropertiesWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.props)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(self.scroll, 1)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self.show_next = QCheckBox("Open this page after applying")
        self.show_next.setToolTip("Untick to apply symbols and colour ramps and stay on the "
                                  "gallery (turn it back on in ⋯ ▸ Open style settings "
                                  "after applying).")
        lay.addWidget(self.show_next)

        buttons = QHBoxLayout()
        self.edit_btn = QPushButton("Edit…")
        self.edit_btn.setToolTip("Open the full QGIS symbol editor")
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Back to the symbol as designed")
        self.cancel_btn = QPushButton("Undo")
        self.cancel_btn.setToolTip("Remove this symbol again")
        self.back_btn = QPushButton("‹  Change style" if self.dock._deferred
                                    else "‹  Back to styles")
        self.back_btn.setToolTip("Back to the style gallery")
        self.back_btn.clicked.connect(self.dock.show_gallery)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.edit_btn)
        buttons.addWidget(self.reset_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        lay.addLayout(buttons)

        self.col_btn.colorChanged.connect(self._on_colour)
        self.out_btn.colorChanged.connect(self._on_outline)
        self.size_spin.valueChanged.connect(self._on_size)
        self.props.changed.connect(self._changed)
        self.class_cb.toggled.connect(self._on_class_colours)
        self.show_next.toggled.connect(
            lambda on: self.dock.set_dem_mode("after" if on else "direct"))
        self.cancel_btn.clicked.connect(self._cancel)
        self.edit_btn.clicked.connect(self._edit)
        self.reset_btn.clicked.connect(self._reset)

    # --------------------------------------------------------------- public
    def open_for(self, entry, layer, mode, symbol=None, dirty=False):
        """*symbol*: keep editing this copy. *dirty*: in "before" mode, the layer still
        has to be given this symbol (commit())."""
        self.entry, self.layer, self.mode = entry, layer, mode
        self.dirty = dirty
        # a private copy – the gallery's own symbol is never modified from here
        self.symbol = symbol if symbol is not None else self.dock.gallery_symbol(entry).clone()
        self._loading = True
        self.show_next.blockSignals(True)
        self.show_next.setChecked(self.dock.dem_mode() != "direct")
        self.show_next.blockSignals(False)
        classified = self.dock.is_classified(layer)
        self._classified = classified
        self.class_cb.setVisible(classified)
        self.class_cb.setChecked(self.dock.class_colours_default())
        self.props._tab_index = 0
        self.props.set_symbol(self.symbol, class_layer=self._class_layer())
        self._fit_height()
        self._loading = False
        self._sync_quick()
        target = self.dock.target_text()
        if entry.id == CURRENT_ID:
            self.title.setText("<b>Current style</b><br>of %s" % target)
        else:
            verb = "applied to" if mode == "after" else "for"
            self.title.setText("<b>%s</b><br>%s %s" % (entry.name, verb, target))
        self.cancel_btn.setVisible(mode == "after")
        self._preview()
        self.dock.show_style_page(self)

    # -------------------------------------------------------------- helpers
    def _preview(self):
        if self.symbol is None:
            return
        try:
            dpr = self.devicePixelRatioF()
        except AttributeError:
            dpr = 1.0
        from qgis.PyQt.QtCore import QSize
        pm = previews.symbol_pixmap(self.symbol, QSize(int(84 * dpr), int(56 * dpr)),
                                    self.dock.bg_combo.currentData(), zoom=1.8 * dpr)
        pm.setDevicePixelRatio(dpr)
        self.preview.setPixmap(pm)

    def _class_layer(self):
        if self.symbol is None or not self._keeps_class_colour():
            return None
        return applier._color_layer_index(self.entry, self.symbol)

    def _fit_height(self):
        """The parts list fills the page and scrolls; nothing to measure."""

    def _keeps_class_colour(self):
        return getattr(self, "_classified", False) and self.class_cb.isChecked()

    def _sync_quick(self):
        obj, e = self.symbol, self.entry
        if obj is None:
            return
        keeps = self._keeps_class_colour()
        self.col_btn.setEnabled(not keeps)
        self.col_btn.setToolTip("Each class keeps its own colour – untick 'Keep each "
                                "class's colour' to use one colour for all classes"
                                if keeps else "Main colour")
        for w in (self.col_btn, self.out_btn, self.size_spin):
            w.blockSignals(True)
        try:
            self.col_btn.setColor(obj.symbolLayer(applier._color_layer_index(e, obj)).color())
            stroke = None
            for i in range(obj.symbolLayerCount()):
                sl = obj.symbolLayer(i)
                if hasattr(sl, "strokeColor"):
                    stroke = sl.strokeColor()
                    break
            self.out_btn.setEnabled(stroke is not None)
            if stroke is not None:
                self.out_btn.setColor(stroke)
            if obj.type() == SYMBOL_MARKER:
                self.size_lbl.setText("Size")
                self.size_spin.setValue(obj.size())
                vis = True
            elif obj.type() == SYMBOL_LINE:
                self.size_lbl.setText("Width")
                self.size_spin.setValue(obj.width())
                vis = True
            else:
                vis = False
            self.size_lbl.setVisible(vis)
            self.size_spin.setVisible(vis)
        finally:
            for w in (self.col_btn, self.out_btn, self.size_spin):
                w.blockSignals(False)

    # -------------------------------------------------------------- editing
    def _on_colour(self, c):
        if self.symbol is None:
            return
        self.symbol.symbolLayer(applier._color_layer_index(self.entry, self.symbol)) \
            .setColor(QColor(c))
        self._rebind()

    def _on_outline(self, c):
        if self.symbol is None:
            return
        for i in range(self.symbol.symbolLayerCount()):
            sl = self.symbol.symbolLayer(i)
            if hasattr(sl, "setStrokeColor"):
                sl.setStrokeColor(QColor(c))
        self._rebind()

    def _on_size(self, v):
        if self.symbol is None:
            return
        if self.symbol.type() == SYMBOL_MARKER:
            self.symbol.setSize(v)
        elif self.symbol.type() == SYMBOL_LINE:
            self.symbol.setWidth(v)
        self._rebind()

    def _rebind(self):
        """Quick controls changed values the property list shows: refresh it."""
        self._loading = True
        self.props.set_symbol(self.symbol, class_layer=self._class_layer())
        self._fit_height()
        self._loading = False
        self._changed()

    def _on_class_colours(self, on):
        if self._loading:
            return
        self.dock.set_class_colours_default(on)
        self._loading = True
        self.props.set_symbol(self.symbol, class_layer=self._class_layer())
        self._fit_height()
        self._loading = False
        self._sync_quick()
        self._changed()

    def _changed(self, *_):
        if self._loading:
            return
        self._preview()
        self._sync_quick()
        self.dirty = True
        if self.mode == "after":
            self._timer.start()

    def _live(self):
        self.dock.reapply(self.entry, self.layer, self.symbol, self._keeps_class_colour())

    def commit(self):
        """Give the layer this symbol ("before" mode; the Apply / OK of Layer Properties)."""
        if self.mode != "before" or not self.dirty or self.layer is None:
            return False
        if self.dock.apply_entry_now(self.entry, self.layer, self.symbol,
                                     self._keeps_class_colour()):
            self.dirty = False
            return True
        return False

    def flush(self):
        """Leaving the page: show a change still waiting for the live timer."""
        if self._timer.isActive():
            self._timer.stop()
            self._live()

    def _cancel(self):
        self._timer.stop()
        if self.mode == "after":
            self.dock.undo()
        self.dock.show_gallery()

    def _edit(self):
        """Full QGIS symbol editor on the pop-up's copy."""
        if self.symbol is None:
            return
        from qgis.core import QgsStyle
        from qgis.gui import QgsSymbolSelectorDialog
        obj = self.symbol.clone()
        dlg = QgsSymbolSelectorDialog(obj, QgsStyle.defaultStyle(), self.layer, self)
        dlg.setWindowTitle("Edit Symbol – %s" % self.entry.name)
        try:
            accepted = dlg.exec()
        finally:
            dlg.setParent(None)        # the dialog points at *obj*: delete it first
            dlg.deleteLater()
        if accepted:
            self.symbol = obj.clone()
            self.dirty = True
            self._quiet_refresh()
            if self.mode == "after":
                self._live()
        self._keep_alive = obj

    def _reset(self):
        """Back to the symbol as it is in the gallery."""
        if self.entry is None:
            return
        self.symbol = self.dock.gallery_symbol(self.entry).clone()
        self.dirty = True
        self._quiet_refresh()
        if self.mode == "after":
            self._live()

    def _quiet_refresh(self):
        self._loading = True
        self.props.set_symbol(self.symbol, class_layer=self._class_layer())
        self._fit_height()
        self._loading = False
        self._sync_quick()
        self._preview()

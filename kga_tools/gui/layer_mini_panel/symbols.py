# -*- coding: utf-8 -*-
"""Symbols section: current symbology + Symbol Bag's Layer Colors, embedded.

The colour wheel is Symbol Bag's own Layer Colors tool placed inside this
panel (KGA's built-in Symbol Bag). It recolours the selected layer – or the classes
selected in the Layers panel – live on the map, keeps categorized / graduated
/ rule-based classes, and shares Symbol Bag's style and Ctrl+Z.
"""

from contextlib import suppress

from qgis.core import QgsSymbolLayerUtils, QgsVectorLayer
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QToolButton, QVBoxLayout,
                                 QWidget)

from . import compat as C
from .symbolbag_bridge import SymbolBagBridge
from .ui_common import icon, small_button


class SymbolsSection(QWidget):
    def __init__(self, iface, watcher, parent=None, bag_provider=None):
        super().__init__(parent)
        self.iface = iface
        self.watcher = watcher
        self.bridge = SymbolBagBridge(iface, bag_provider)
        self.layer = None
        self.colors = None           # embedded Layer Colors widget (Symbol Bag)
        self._keys = None            # renderer legend keys, to notice class changes
        self.on_colors_created = None    # set by the dock (theme + smart size)
        self.stats = dict(embeds=0)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # --- current symbology card
        card = QFrame()
        card.setObjectName("card")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(10, 8, 8, 8)
        cl.setSpacing(10)
        self.current_icon = QLabel()
        self.current_icon.setFixedSize(40, 40)
        cl.addWidget(self.current_icon)
        info = QVBoxLayout()
        info.setSpacing(0)
        self.renderer_lbl = QLabel()
        self.renderer_lbl.setObjectName("layerNameDark")
        self.renderer_lbl.setStyleSheet("font-weight: 700;")
        self.renderer_sub = QLabel()
        self.renderer_sub.setObjectName("muted")
        self.renderer_sub.setWordWrap(True)
        info.addWidget(self.renderer_lbl)
        info.addWidget(self.renderer_sub)
        cl.addLayout(info, 1)
        self.undo_btn = small_button(icon("undo"), "Undo the last Symbol Bag change", self.undo)
        self.native_btn = small_button(icon("settings"), "Open the layer's symbology in Layer Properties",
                                       self.open_native)
        cl.addWidget(self.undo_btn)
        cl.addWidget(self.native_btn)
        lay.addWidget(card)

        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.linkActivated.connect(self._link)
        self.notice.hide()
        lay.addWidget(self.notice)

        # --- Layer Colors lives here
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(C.enum_value(QFrame, "Shape", "NoFrame"))
        self.scroll.setHorizontalScrollBarPolicy(C.enum_value(Qt, "ScrollBarPolicy", "ScrollBarAsNeeded"))
        self.host = QWidget()
        self.host_lay = QVBoxLayout(self.host)
        self.host_lay.setContentsMargins(0, 0, 0, 0)
        self.host_lay.setSpacing(0)
        self.host_lay.addStretch(1)
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll, 1)

        # --- footer
        foot = QHBoxLayout()
        self.bag_btn = QToolButton()
        self.bag_btn.setObjectName("textButton")
        self.bag_btn.setIcon(icon("bag"))
        self.bag_btn.setText("Symbol library")
        self.bag_btn.setToolButtonStyle(C.ToolButtonTextBesideIcon)
        self.bag_btn.setToolTip("Open Symbol Bag to apply, create or edit symbols")
        self.bag_btn.clicked.connect(self.open_bag)
        foot.addWidget(self.bag_btn)
        foot.addStretch(1)
        self.float_btn = QPushButton("Floating Layer Colors")
        self.float_btn.setObjectName("secondary")
        self.float_btn.setToolTip("Open Layer Colors as Symbol Bag's floating tool")
        self.float_btn.clicked.connect(self.open_layer_colors)
        self.float_btn.hide()
        foot.addWidget(self.float_btn)
        lay.addLayout(foot)

    # ================================================================== lifecycle
    def cleanup(self):
        if self.colors is not None:
            with suppress(RuntimeError):
                self.colors.cleanup()
        self.colors = None

    def _colors_alive(self):
        c = self.colors
        if c is None:
            return False
        with suppress(ImportError):
            from qgis.PyQt import sip
            if sip.isdeleted(c):
                return False
        try:
            return not getattr(c, "_dead", False) and c.parentWidget() is self.host
        except RuntimeError:
            return False

    def _colors_destroyed(self, *_):
        """Symbol Bag was unloaded / reloaded: its widget is gone."""
        self.colors = None
        QTimer.singleShot(0, self.ensure_current)

    # ================================================================== layer
    def set_layer(self, layer):
        self.layer = layer if isinstance(layer, QgsVectorLayer) and layer.isSpatial() else None
        self._keys = None
        self.refresh_renderer()
        if self.isVisible():
            self.ensure_current()

    def ensure_current(self):
        """Make sure Layer Colors is embedded and pointing at the current layer."""
        if not self.bridge.resolve():
            self._show_unavailable()
            return
        self.notice.hide()
        if self.colors is not None and not self._colors_alive():
            self.colors = None           # Symbol Bag was unloaded / reloaded meanwhile
        if self.colors is None and self.bridge.can_embed_colors():
            try:
                self.colors = self.bridge.create_layer_colors_widget(self.host)
                self.colors.destroyed.connect(self._colors_destroyed)
                self.host_lay.insertWidget(0, self.colors)
                self.stats["embeds"] += 1
                if self.on_colors_created is not None:
                    self.on_colors_created(self.colors)
            except Exception as e:  # never break the panel because of the bag
                self.colors = None
                self.notice.setText("Layer Colors could not be shown here ({}).".format(e))
                self.notice.show()
        self.float_btn.setVisible(self.colors is None)
        if self.colors is None and not self.bridge.can_embed_colors():
            self.notice.setText("The installed Symbol Bag cannot show Layer Colors inside this panel. "
                                "Use <a href='float'>floating Layer Colors</a> instead.")
            self.notice.show()
        if self.colors is not None and self.layer is not None and C.geometry_family(self.layer) == C.GEOM_POINT \
                and not self.bridge.supports_point_size():
            self.notice.setText("Point size and outline need <b>Symbol Bag 1.16.0</b> or newer (connected: {}). Install it and "
                                "restart QGIS.".format(self.bridge.version()))
            self.notice.show()
        if self.colors is not None:
            self.colors.setVisible(self.layer is not None)
            if self.layer is not None:
                QTimer.singleShot(0, self._follow)
        self.refresh_renderer()

    def _follow(self):
        c = self.colors
        if c is None or self.layer is None or not self._colors_alive():
            return
        try:
            if c.layer_id != self.layer.id():
                c.follow_layer_selection()
                if c.layer_id != self.layer.id():      # nothing selected in Layers panel: use ours
                    c.follow_layer_selection(self.layer, None)
        except RuntimeError:
            self.colors = None

    def refresh_renderer(self):
        """Describe the layer's current symbology (renderer metadata only)."""
        lyr = self.layer
        if lyr is None:
            self.current_icon.clear()
            self.renderer_lbl.setText("No vector layer")
            self.renderer_sub.setText("Select a point, line or polygon layer.")
            self.native_btn.setEnabled(False)
            self.undo_btn.setEnabled(False)
            return
        self.native_btn.setEnabled(True)
        if self.bridge.available:
            t, label, n, classified = self.bridge.renderer_info(lyr)
        else:
            r = lyr.renderer()
            label, n, classified = (r.type() if r else "None"), 0, False
        self.renderer_lbl.setText(label)
        self.renderer_sub.setText("{} class{}".format(n, "" if n == 1 else "es") if classified
                                  else C.wkb_label(lyr))
        self.current_icon.setPixmap(self._current_symbol_pixmap(lyr))
        self.undo_btn.setEnabled(self.bridge.available and self.bridge.can_undo())
        # classes added / removed elsewhere: restart the colour session on the new classes
        try:
            keys = tuple(sorted(it.ruleKey() for it in lyr.renderer().legendSymbolItems()))
        except Exception:
            keys = None
        if self._keys is not None and keys != self._keys and self.colors is not None:
            try:
                self.colors._end_session()
                self.colors.layer_id = None
                QTimer.singleShot(0, self._follow)
            except RuntimeError:
                self.colors = None
        self._keys = keys

    def _current_symbol_pixmap(self, lyr):
        try:
            items = lyr.renderer().legendSymbolItems()
        except Exception:
            items = []
        syms = []
        for it in items:
            s = it.symbol()
            if s is not None:
                syms.append(s.clone())
            if len(syms) == 4:
                break
        if not syms:
            return icon("symbols").pixmap(24, 24)
        if len(syms) == 1:
            return QgsSymbolLayerUtils.symbolPreviewPixmap(syms[0], QSize(40, 40), 3)
        pm = QPixmap(40, 40)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        for i, s in enumerate(syms):
            p.drawPixmap((i % 2) * 21, (i // 2) * 21, QgsSymbolLayerUtils.symbolPreviewPixmap(s, QSize(19, 19), 1))
        p.end()
        return pm

    def _show_unavailable(self):
        self.cleanup()
        self.float_btn.hide()
        self.bag_btn.setEnabled(False)
        self.notice.setText("<b>Symbol Bag is not available.</b> {}<br>It could not be loaded; see the "
                            "KGA Tools log, then <a href='retry'>try again</a>. "
                            "Symbology can still be edited in <a href='native'>Layer Properties</a>."
                            .format(self.bridge.error or ""))
        self.notice.show()
        self.refresh_renderer()

    # ================================================================== actions
    def _link(self, link):
        if link == "native":
            self.open_native()
        elif link == "retry":
            self.bag_btn.setEnabled(True)
            self.ensure_current()
        elif link == "float":
            self.open_layer_colors()

    def open_bag(self):
        if self.bridge.available or self.bridge.resolve():
            kind = self.bridge.layer_kind(self.layer) if self.layer is not None else None
            self.bridge.open_bag(kind)

    def open_layer_colors(self):
        if self.layer is not None and (self.bridge.available or self.bridge.resolve()):
            self.bridge.open_layer_colors()

    def undo(self):
        if not self.bridge.available:
            return
        with self.watcher.own_change():
            n = self.bridge.undo()
        self.iface.messageBar().pushMessage(
            "Symbols", "Restored the previous style." if n else "Nothing to undo.", C.MSG_INFO, 2)
        if self.colors is not None:
            try:
                self.colors._end_session()
                self.colors.layer_id = None
            except RuntimeError:
                self.colors = None
        self.refresh_renderer()
        QTimer.singleShot(0, self._follow)

    def open_native(self):
        if self.layer is None:
            return
        try:
            self.iface.showLayerProperties(self.layer, "mOptsPage_Style")
        except TypeError:
            self.iface.showLayerProperties(self.layer)

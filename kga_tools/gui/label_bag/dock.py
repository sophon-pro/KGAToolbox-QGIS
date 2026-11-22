# -*- coding: utf-8 -*-
"""The Label Bag window: the Layer Bag's dock showing only its Labels section.

Its height is always the height of its content: no scroll bar. Rows that come and go (the number options of
a numeric field, X / Y of a point) grow or shrink the bag, which keeps its top edge where it is.
"""

from contextlib import suppress

from qgis.PyQt.QtCore import QEasingCurve, QPropertyAnimation, QRect
from qgis.PyQt.QtGui import QGuiApplication

from ..layer_mini_panel.dock import MiniPanelDock

NAME = "Label Bag"
BUBBLE_KEY = "label_bag"
LABELS_INDEX = 1                 # position of "labels" in the Layer Bag's sections
MIN_WIDTH = 300
FIT_MS = 140


class LabelBagDock(MiniPanelDock):
    TITLE = NAME
    OBJECT_NAME = "LabelBagDock"
    BUBBLE_KEY = BUBBLE_KEY
    HELP_PAGE = "label_bag"
    GEOMETRY_KEY = "label_bag_float_geometry"
    EMPTY_TEXT = "Select a layer in the Layers panel to label it."
    EXTENDED_LABELS = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # no Attributes / Labels / Colors switch: this bag is the Labels section only
        nav = self.root_layout.itemAt(0).widget()
        if nav is not None and nav.objectName() == "segment":
            nav.hide()
        self.labels.on_layout_changed = self.request_fit
        super().show_section(LABELS_INDEX)

    def show_section(self, index):
        super().show_section(LABELS_INDEX)

    def _fill_menu(self):
        super()._fill_menu()
        for a in list(self.menu.actions()):      # the height always follows the content: no such switch
            if a.text().startswith(("Auto-fit", "Fit to content")):
                self.menu.removeAction(a)

    # ------------------------------------------------------------------ height follows the content
    def request_fit(self):
        if self.isVisible():
            self._fit_timer.start()

    def fit_to_content(self, force=False):
        if not self.isVisible():
            return
        self.root_layout.activate()
        want = self.root.sizeHint().height() + (self.height() - self.root.height())
        screen = self.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        h = max(80, min(int(want), int(avail.height() * 0.9)))
        w = max(self.width(), MIN_WIDTH, self.minimumSizeHint().width())
        if self.isFloating():
            w = min(w, self.width_cap())
        g = self.geometry()
        target = QRect(g.left(), g.top(), w, h)
        if target.bottom() > avail.bottom():
            target.moveBottom(avail.bottom())
        if abs(target.height() - g.height()) < 2 and abs(target.width() - g.width()) < 2:
            return
        self.metrics["fits"] += 1
        if not self.isFloating():
            return                                  # docked: QGIS arranges the height
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.setDuration(FIT_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(g)
        self._anim.setEndValue(target)
        with suppress(RuntimeError):
            self._anim.start()

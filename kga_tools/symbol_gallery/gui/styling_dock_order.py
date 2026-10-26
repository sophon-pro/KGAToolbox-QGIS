# -*- coding: utf-8 -*-
"""Show the KGA Layer Symbol button right after Symbology in the Layer Styling panel.

QGIS lists plugin pages after its own and picks the page to show from the button's
ROW number, so moving the button really would open the wrong page. Instead the list
keeps its order and only the *drawing* and the *clicks* are re-mapped: the slot at
position 1 draws (and selects) our row, the slots after it draw the rows that were
there before. Nothing but the list widget's look is touched.
"""
from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import (
    QListWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget,
)

HOOKED = "kga_layer_symbol_order"
AFTER = 1                    # the slot right after Symbology


def _class_name(w):
    return w.metaObject().className()


def logical_order(lw, title):
    """order[slot] = row drawn in that slot; the row titled *title* moved to slot 1."""
    n = lw.count()
    order = list(range(n))
    ours = next((i for i in range(n) if lw.item(i).toolTip() == title), -1)
    if ours > AFTER:
        order.remove(ours)
        order.insert(AFTER, ours)
    return order


class _Delegate(QStyledItemDelegate):
    def __init__(self, lw, title):
        super().__init__(lw)
        self.lw = lw
        self.title = title

    def _row(self, slot):
        order = logical_order(self.lw, self.title)
        return order[slot] if 0 <= slot < len(order) else slot

    def paint(self, painter, option, index):
        row = self._row(index.row())
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, self.lw.model().index(row, 0))
        if row == self.lw.currentRow():
            opt.state |= QStyle.StateFlag.State_Selected
        else:
            opt.state &= ~QStyle.StateFlag.State_Selected
        style = self.lw.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, self.lw)

    def helpEvent(self, event, view, option, index):
        return super().helpEvent(event, view, option,
                                 self.lw.model().index(self._row(index.row()), 0))


class _Clicks(QObject):
    """A click on a slot selects the row that slot is drawing."""

    def __init__(self, lw, title):
        super().__init__(lw)
        self.lw = lw
        self.title = title

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseButtonDblClick):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        index = self.lw.indexAt(event.pos())
        if not index.isValid():
            return False
        order = logical_order(self.lw, self.title)
        if order == list(range(len(order))):
            return False
        if kind == QEvent.Type.MouseButtonPress:
            self.lw.setCurrentRow(order[index.row()])
        return True


class StylingDockOrder:
    """Installs the re-mapping on the Layer Styling panel's button list."""

    def __init__(self, iface, title):
        self.iface = iface
        self.title = title
        self._lists = []

    def attach(self):
        try:
            for sw in self.iface.mainWindow().findChildren(QWidget):
                if _class_name(sw) != "QgsLayerStylingWidget" or sw.property(HOOKED):
                    continue
                lw = sw.findChild(QListWidget, "mOptionsListWidget")
                if lw is None:
                    continue
                if _class_name(lw.itemDelegate()) not in ("QStyledItemDelegate",
                                                          "QItemDelegate"):
                    continue          # someone else's delegate: leave the list alone
                sw.setProperty(HOOKED, True)
                delegate = _Delegate(lw, self.title)
                clicks = _Clicks(lw, self.title)
                self._lists.append((sw, lw, lw.itemDelegate(), delegate, clicks))
                lw.setItemDelegate(delegate)
                lw.viewport().installEventFilter(clicks)
                lw.viewport().update()
        except RuntimeError:
            pass

    def detach(self):
        for sw, lw, old, delegate, clicks in self._lists:
            try:
                lw.viewport().removeEventFilter(clicks)
                lw.setItemDelegate(old)
                sw.setProperty(HOOKED, False)
                lw.viewport().update()
            except RuntimeError:
                pass
        self._lists = []

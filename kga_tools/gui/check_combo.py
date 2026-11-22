# -*- coding: utf-8 -*-
"""A drop-down list whose items carry check boxes, so several can be picked.

The popup stays open while boxes are ticked. The box itself shows the ticked
names (or a count when they do not fit). Items hold one piece of user data
each, like a QComboBox, and the signal fires once per tick.
"""

from qgis.PyQt.QtCore import QEvent, QRect, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QPalette, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (QApplication, QComboBox, QSizePolicy, QStyle,
                                 QStyledItemDelegate, QStyleOptionButton,
                                 QStyleOptionComboBox, QStylePainter)

_CHECKED = Qt.CheckState.Checked
_UNCHECKED = Qt.CheckState.Unchecked


class _CheckDelegate(QStyledItemDelegate):
    """Draws each row with an ordinary check box (the size and look of a
    QCheckBox) instead of the list view's own, larger, indicator."""

    GAP = 6

    @staticmethod
    def _box_size(option):
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        probe = QStyleOptionButton()
        return (style, style.pixelMetric(
            QStyle.PixelMetric.PM_IndicatorWidth, probe, widget),
            style.pixelMetric(QStyle.PixelMetric.PM_IndicatorHeight, probe,
                              widget))

    def sizeHint(self, option, index):      # noqa: N802 (Qt API name)
        _style, width, height = self._box_size(option)
        base = super().sizeHint(option, index)
        return QSize(base.width() + width + 2 * self.GAP,
                     max(base.height(), height + 8))

    def paint(self, painter, option, index):
        style, width, height = self._box_size(option)
        widget = option.widget
        rect = option.rect
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem,
                            option, painter, widget)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == _CHECKED

        box = QStyleOptionButton()
        box.rect = QRect(rect.left() + self.GAP,
                         rect.top() + (rect.height() - height) // 2,
                         width, height)
        box.state = (QStyle.StateFlag.State_Enabled if enabled
                     else QStyle.StateFlag.State_None)
        box.state |= (QStyle.StateFlag.State_On if checked
                      else QStyle.StateFlag.State_Off)
        box.palette = option.palette
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox,
                            box, painter, widget)

        role = (QPalette.ColorRole.HighlightedText if selected
                else QPalette.ColorRole.Text)
        text_rect = rect.adjusted(width + 2 * self.GAP, 0, -self.GAP, 0)
        style.drawItemText(
            painter, text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            option.palette, enabled,
            str(index.data(Qt.ItemDataRole.DisplayRole) or ''), role)


class CheckCombo(QComboBox):

    #: Emitted after the set of ticked items changed.
    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self._silent = False
        #: True between a press inside the popup and its release, so the
        #: release of the click that *opened* the popup ticks nothing.
        self._pressed = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMaxVisibleItems(18)
        self._model.itemChanged.connect(self._item_changed)
        # A click on an item must tick it without closing the popup, so the
        # view's own mouse handling is replaced by this filter.
        self.view().setItemDelegate(_CheckDelegate(self.view()))
        self.view().viewport().installEventFilter(self)
        self.view().installEventFilter(self)

    # ---------------------------------------------------------------- content

    def set_items(self, entries):
        """Replace the items with [(label, data)], none ticked."""
        self._silent = True
        self._model.clear()
        for label, data in entries:
            item = QStandardItem(label)
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                          | Qt.ItemFlag.ItemIsEnabled)
            item.setData(_UNCHECKED, Qt.ItemDataRole.CheckStateRole)
            item.setData(data, Qt.ItemDataRole.UserRole)
            self._model.appendRow(item)
        self._silent = False
        self.update()

    def clear_items(self):
        self.set_items([])

    def _items(self):
        return [self._model.item(row) for row in range(self._model.rowCount())]

    def checked_data(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self._items()
                if item.checkState() == _CHECKED]

    def checked_texts(self):
        return [item.text() for item in self._items()
                if item.checkState() == _CHECKED]

    def set_checked_data(self, wanted, notify=True):
        """Tick exactly the items whose data is in `wanted`."""
        wanted = list(wanted)
        self._silent = True
        for item in self._items():
            item.setCheckState(
                _CHECKED if item.data(Qt.ItemDataRole.UserRole) in wanted
                else _UNCHECKED)
        self._silent = False
        self.update()
        if notify:
            self.selectionChanged.emit()

    def check_texts(self, texts):
        """Tick exactly the items with these labels (for tests and scripts)."""
        texts = list(texts)
        missing = [t for t in texts if self.findText(t) < 0]
        if missing:
            raise ValueError('not in the list: {}'.format(missing))
        self._silent = True
        for item in self._items():
            item.setCheckState(_CHECKED if item.text() in texts
                               else _UNCHECKED)
        self._silent = False
        self.update()
        self.selectionChanged.emit()

    def _item_changed(self, _item):
        if not self._silent:
            self.update()
            self.selectionChanged.emit()

    # ---------------------------------------------------------------- display

    def summary(self):
        """What the closed box shows."""
        names = self.checked_texts()
        text = ', '.join(names)
        room = max(self.width() - 40, 40)
        if len(names) > 1 and self.fontMetrics().horizontalAdvance(text) > room:
            return '{} selected'.format(len(names))
        return text

    def paintEvent(self, _event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self.summary()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)

    # -------------------------------------------------------------- the popup

    def _toggle(self, index):
        if not index.isValid():
            return
        item = self._model.itemFromIndex(index)
        if item is not None and item.isEnabled():
            item.setCheckState(_UNCHECKED if item.checkState() == _CHECKED
                               else _CHECKED)

    def showPopup(self):                    # noqa: N802 (Qt API name)
        self._pressed = False
        super().showPopup()

    def eventFilter(self, obj, event):      # noqa: N802 (Qt API name)
        view = self.view()
        kind = event.type()
        if obj is view.viewport() and kind in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick):
            if kind == QEvent.Type.MouseButtonPress:
                self._pressed = True
            elif kind == QEvent.Type.MouseButtonRelease and self._pressed:
                self._pressed = False
                point = (event.position().toPoint()
                         if hasattr(event, 'position') else event.pos())
                self._toggle(view.indexAt(point))
            return True
        if obj is view and kind == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Space:
            self._toggle(view.currentIndex())
            return True
        return super().eventFilter(obj, event)

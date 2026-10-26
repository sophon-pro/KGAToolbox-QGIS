"""Small widgets for the Attributes pane: the Auto Apply switch and the flat tab strip."""

from qgis.PyQt.QtCore import QEvent, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtWidgets import QAbstractButton, QTabBar


class ToggleSwitch(QAbstractButton):
    """Pill switch like Pro's Auto Apply: blue with the knob on the right when on."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(34, 18)

    def sizeHint(self):
        return QSize(34, 18)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0a74c9") if on else QColor("#8d9296"))
        p.drawRoundedRect(QRectF(self.rect()), 9, 9)
        knob = 12
        x = self.width() - knob - 3 if on else 3
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, (self.height() - knob) / 2.0, knob, knob))
        p.end()


class FlatTabBar(QTabBar):
    """Tab strip whose tabs are sized from the font the style sheet really gives them.

    With a style sheet that sets font-size and padding, Qt can size the tabs with the application
    font and then draw the text in the larger one, so the labels come out clipped ("electio",
    "ttribute") on some machines. Measuring with the polished font, plus a little slack, avoids it.
    """

    PADDING = 8           # the style sheet's left + right padding
    MARGIN = 10           # its margin-right between tabs
    SLACK = 8             # room for font rendering differences between machines

    def tabSizeHint(self, index):
        self.ensurePolished()
        fm = self.fontMetrics()
        width = fm.horizontalAdvance(self.tabText(index)) + self.PADDING + self.MARGIN + self.SLACK
        height = fm.height() + 3 + 4 + 2 + 4          # padding top / bottom, underline, breathing room
        return QSize(width, height)

    def minimumTabSizeHint(self, index):
        return self.tabSizeHint(index)

    def _relayout(self):
        # QTabBar caches its layout; re-setting a text makes it measure the tabs again
        for i in range(self.count()):
            self.setTabText(i, self.tabText(i))
        self.updateGeometry()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.FontChange, QEvent.Type.Polish):
            self._relayout()
        return result

    def showEvent(self, event):
        super().showEvent(event)
        self._relayout()


def make_tab_bar(names, parent=None):
    """Flat tab strip (underline on the active tab); the look comes from the pane's style sheet."""
    bar = FlatTabBar(parent)
    bar.setDrawBase(False)
    bar.setExpanding(False)
    bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    for name in names:
        bar.addTab(name)
    return bar

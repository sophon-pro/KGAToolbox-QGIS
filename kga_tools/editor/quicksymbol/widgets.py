"""Small widgets for the Quick Symbology window: the on/off switch and the symbol preview."""

from qgis.PyQt.QtCore import QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPixmap
from qgis.PyQt.QtWidgets import QAbstractButton, QLabel, QSizePolicy, QWidget

from . import engine

ON_COLOR = QColor("#4cd964")
OFF_COLOR = QColor("#d9d9d9")


class ToggleSwitch(QAbstractButton):
    """The iOS-style switch: green with the knob on the right when on, grey with the knob on the left when off."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)           # no focus ring: it is a plain switch

    def sizeHint(self):
        return QSize(34, 18)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = rect.height() / 2
        enabled = self.isEnabled()
        track = QColor(ON_COLOR if self.isChecked() else OFF_COLOR)
        if not enabled:
            track.setAlpha(110)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(rect, radius, radius)
        knob = rect.height() - 4
        x = rect.right() - knob - 2 if self.isChecked() else rect.left() + 2
        p.setBrush(QColor(0, 0, 0, 40))                       # soft shadow
        p.drawEllipse(QRectF(x, rect.top() + 2.8, knob, knob))
        p.setBrush(QColor("#ffffff") if enabled else QColor("#f2f2f2"))
        p.drawEllipse(QRectF(x, rect.top() + 2, knob, knob))
        p.end()


def switch_label(text, switch, parent):
    """The text beside a ToggleSwitch; clicking it flips the switch too."""
    label = QLabel(text, parent)
    label.setToolTip(switch.toolTip())
    label.mouseReleaseEvent = lambda _e: switch.click() if switch.isEnabled() else None
    return label


class SymbolPreview(QWidget):
    """Draws a symbol the way the map will, centred, on a plain background."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._symbol = None
        self.setMinimumSize(150, 62)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_symbol(self, symbol):
        self._symbol = symbol.clone() if symbol is not None else None
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().base())
        if self._symbol is not None:
            w, h = max(8, self.width() - 24), max(8, self.height() - 16)
            pixmap = engine.preview_pixmap(self._symbol, w, h)
            p.drawPixmap((self.width() - pixmap.width()) // 2, (self.height() - pixmap.height()) // 2, pixmap)
        p.end()


def color_pixmap(color, width=48, height=16):
    """A solid swatch with a thin border, for lists."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(color))
    p = QPainter(pixmap)
    p.setPen(QColor(0, 0, 0, 90))
    p.drawRect(0, 0, width - 1, height - 1)
    p.end()
    return pixmap

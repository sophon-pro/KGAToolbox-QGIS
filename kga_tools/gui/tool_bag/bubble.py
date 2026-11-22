"""The floating bubble that lives on the map, and the animation that pours the bag out of it."""

from qgis.PyQt.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QVariantAnimation
from qgis.PyQt.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
                             QRadialGradient)
from qgis.PyQt.QtWidgets import QWidget

from .compat import OWN_PREFIX, alive
from .themes import theme, tinted_icon

# Where the bubble sits on the map: key -> (label, horizontal 0/1/2, vertical 0/1/2)
BUBBLE_POSITIONS = {
    "top_left": ("Top left", 0, 0),
    "top_center": ("Top center", 1, 0),
    "top_right": ("Top right", 2, 0),
    "middle_left": ("Middle left", 0, 1),
    "center": ("Center", 1, 1),
    "middle_right": ("Middle right", 2, 1),
    "bottom_left": ("Bottom left", 0, 2),
    "bottom_center": ("Bottom center", 1, 2),
    "bottom_right": ("Bottom right", 2, 2),
}
DEFAULT_BUBBLE_POSITION = "bottom_right"

DIAMETER = 56               # design size; the bubble is scaled from this
PAD = 9                      # room around the circle for its soft shadow (at design size)
MIN_BUBBLE, MAX_BUBBLE, DEFAULT_BUBBLE = 28, 64, 40   # circle diameter in px the user can choose
EDGE = 14                    # gap between the bubble and the edge of the map


class BubbleButton(QWidget):
    """A round, glossy bag button that sits on top of the map canvas."""

    def __init__(self, controller, canvas):
        super().__init__(canvas)
        self.ctrl = controller
        self.canvas = canvas
        self.setObjectName(OWN_PREFIX + "_bubble")
        self.apply_size()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self._scale = 1.0
        self._pressed = False
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_scale)
        canvas.installEventFilter(self)
        self.setToolTip("Tool Bag – click to open or close")
        self.reposition()

    # ------------------------------------------------------------ placement
    def _factor(self):
        return max(MIN_BUBBLE, min(MAX_BUBBLE, int(self.ctrl.bubble_size))) / float(DIAMETER)

    def pad(self):
        # room for the shadow, the count badge and the hover grow (all draw outside the circle)
        return int(round(PAD * self._factor())) + 22

    def apply_size(self):
        side = int(round(DIAMETER * self._factor())) + 2 * self.pad()
        self.setFixedSize(side, side)

    def group_center(self):
        return QPointF(self.mapToGlobal(self.rect().center()))

    def group_move(self, center):
        """Put the circle's centre at *center* (screen coordinates); used by the bubble group."""
        if not alive(self.canvas):
            return
        p = self.canvas.mapFromGlobal(QPoint(int(round(center.x())), int(round(center.y()))))
        self.move(p.x() - self.width() // 2, p.y() - self.height() // 2)

    def reposition(self):
        if not alive(self.canvas):
            return
        group = getattr(self.ctrl, "group", None)
        if group is not None:            # the bubbles of all the bags sit together in one column
            group.relayout(False)
            self.raise_()
            return
        _label, h, v = BUBBLE_POSITIONS.get(self.ctrl.bubble_position,
                                            BUBBLE_POSITIONS[DEFAULT_BUBBLE_POSITION])
        cw, ch = self.canvas.width(), self.canvas.height()
        side, pad = self.width(), self.pad()
        xs = (EDGE - pad, (cw - side) // 2, cw - side - EDGE + pad)
        ys = (EDGE - pad, (ch - side) // 2, ch - side - EDGE + pad)
        self.move(max(0, xs[h]), max(0, ys[v]))
        self.raise_()

    def global_circle(self):
        """The bubble's circle in screen coordinates (where the bag flies from / to)."""
        pad = self.pad()
        return QRect(self.mapToGlobal(self.rect().topLeft()), self.size()).adjusted(pad, pad, -pad, -pad)

    def eventFilter(self, obj, event):
        if obj is self.canvas and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.reposition()
        return False

    # ------------------------------------------------------------ interaction
    def _animate_to(self, value, ms=140, curve=QEasingCurve.Type.OutCubic):
        self._anim.stop()
        self._anim.setStartValue(self._scale)
        self._anim.setEndValue(value)
        self._anim.setDuration(ms)
        self._anim.setEasingCurve(curve)
        self._anim.start()

    def _on_scale(self, v):
        self._scale = float(v)
        self.update()

    def pop_in(self, start=0.2):
        """Little bounce when the bubble appears."""
        self._scale = start
        self._animate_to(1.0, 380, QEasingCurve.Type.OutBack)

    def enterEvent(self, event):
        if not self._pressed:
            self._animate_to(1.1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._pressed = False
        self._animate_to(1.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._animate_to(0.92, 80)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            inside = self.rect().contains(event.pos())
            self._animate_to(1.1 if inside else 1.0, 160, QEasingCurve.Type.OutBack)
            if inside:
                self.ctrl.toggle_bag()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.ctrl.show_bubble_menu(event.globalPos())
        event.accept()

    # ------------------------------------------------------------ painting
    def paintEvent(self, _event):
        t = theme(self.ctrl.theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        p.translate(c)
        f = self._factor()
        p.save()
        p.scale(self._scale * f, self._scale * f)
        r = DIAMETER / 2.0

        # soft shadow: a few translucent discs growing outwards
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(6):
            grow = i * 1.5
            p.setBrush(QColor(30, 45, 70, 20 - i * 3))
            p.drawEllipse(QPointF(0, 3), r + grow - 2, r + grow - 2)

        # body
        body = QLinearGradient(0, -r, 0, r)
        body.setColorAt(0.0, QColor(t["top"]))
        body.setColorAt(1.0, QColor(t["bottom"]))
        p.setBrush(QBrush(body))
        p.setPen(QPen(QColor(255, 255, 255, 150), 1.6))
        p.drawEllipse(QPointF(0, 0), r - 0.8, r - 0.8)

        # gloss
        gloss = QRadialGradient(QPointF(-r * 0.25, -r * 0.55), r * 0.95)
        gloss.setColorAt(0.0, QColor(255, 255, 255, 150))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        clip = QPainterPath()
        clip.addEllipse(QPointF(0, 0), r - 2, r - 2)
        p.save()
        p.setClipPath(clip)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(gloss))
        p.drawEllipse(QRectF(-r, -r, 2 * r, r * 1.15))
        p.restore()

        # bag glyph
        pix = tinted_icon("bag_white.svg", "#ffffff").pixmap(QSize(28, 28))
        p.drawPixmap(int(-14), int(-14), pix)
        p.restore()

        # how many tools are in the bag
        n = len(self.ctrl.tools)
        if n:
            text = str(n) if n < 100 else "99+"
            font = QFont(self.font())
            font.setBold(True)
            font.setPixelSize(10)
            p.setFont(font)
            w = max(17, p.fontMetrics().horizontalAdvance(text) + 9)
            # drawn outside the size scaling so the number stays readable on a small bubble
            p.scale(self._scale, self._scale)
            rr = r * f
            badge = QRectF(rr * 0.62 - w / 2.0 + 3, -rr * 0.85 - 4, w, 17)
            p.setPen(QPen(QColor(t["bottom"]), 1.4))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(badge, 8.5, 8.5)
            p.setPen(QColor(t["handle"]).darker(115))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


class FlyOverlay(QWidget):
    """Click-through window that draws a picture of the bag growing out of (or shrinking
    into) the bubble. The real bag is only shown once the animation has finished, so
    nothing has to be re-laid-out while it moves."""

    def __init__(self, main_window):
        super().__init__(main_window, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName(OWN_PREFIX + "_fly")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_value)
        self._anim.finished.connect(self._on_finished)
        self._pix = None
        self._from = QRectF()
        self._to = QRectF()
        self._t = 0.0
        self._opacity = 1.0
        self._opening = True
        self._done = None
        self._origin = QPointF()

    @property
    def running(self):
        return self._anim.state() == QVariantAnimation.State.Running

    def play(self, pixmap, bubble_rect, bag_rect, opening, opacity, done):
        """Animate *pixmap* (the bag) between the bubble circle and the bag's own rectangle."""
        self.finish()
        self._pix = pixmap
        self._opening = opening
        self._opacity = opacity
        self._done = done
        # the bag grows from the bubble's centre; while shrinking it lands on it
        self._from = QRectF(bubble_rect)
        self._to = QRectF(bag_rect)
        area = self._from.united(self._to).adjusted(-60, -60, 60, 60).toAlignedRect()
        self._origin = QPointF(area.topLeft())
        self.setGeometry(area)
        self.show()
        self.raise_()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(360 if opening else 240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack if opening else QEasingCurve.Type.InCubic)
        self._t = 0.0
        self._anim.start()

    def finish(self):
        """Jump to the end of a running animation (used when the user clicks again mid-way)."""
        if self.running:
            self._anim.stop()
            self._on_finished()

    def _on_value(self, v):
        self._t = float(v)
        self.update()

    def _on_finished(self):
        done, self._done = self._done, None
        if done is not None:
            done()
        self.hide()
        self._pix = None

    def paintEvent(self, _event):
        if self._pix is None:
            return
        # progress 0 -> 1 means "bubble -> bag" when opening and "bag -> bubble" when closing
        k = self._t if self._opening else 1.0 - self._t
        k = max(-0.2, min(1.25, k))
        a, b = self._from, self._to
        w = a.width() + (b.width() - a.width()) * k
        h = a.height() + (b.height() - a.height()) * k
        # the bag stays anchored to the bubble's centre at k=0 and drifts to its own centre
        cx = a.center().x() + (b.center().x() - a.center().x()) * k
        cy = a.center().y() + (b.center().y() - a.center().y()) * k
        w, h = max(2.0, w), max(2.0, h)
        rect = QRectF(cx - w / 2.0 - self._origin.x(), cy - h / 2.0 - self._origin.y(), w, h)
        fade = max(0.0, min(1.0, (k if self._opening else k * 1.0) * 1.8))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._opacity * fade)
        p.drawPixmap(rect, self._pix, QRectF(self._pix.rect()))
        p.end()

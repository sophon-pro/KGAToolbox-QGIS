# -*- coding: utf-8 -*-
"""Symbol Bag – shared motion widgets used by the bag and the colour picker."""

import math

from qgis.PyQt.QtCore import QPointF, QRectF, QSizeF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QWidget

from .motion import Tween, lerp, spring


# ----------------------------------------------------------------------
class GhostWidget(QWidget):
    """A snapshot of the bag that does the moving.

    Moving, resizing and repainting the real translucent window every frame
    stutters (especially on Windows). Instead one small transparent window
    carries a cached image of the bag and only re-positions and repaints that
    image: it can travel along a curve, scale, squash, tilt and fade at 60 fps.
    """

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.pixmap = None
        self._token = 0
        self.reset()

    # --- pixel-exact placement --------------------------------------------
    # The ghost must land exactly where the real window will appear: the
    # image is drawn at whole pixels and its size is the window's own size,
    # so swapping ghost and window at the end shows no 1-pixel jump or blur.
    def image_size(self):
        if self.pixmap is None:
            return 0, 0
        s = QSizeF(self.pixmap.size()) / self.pixmap.devicePixelRatio()
        return int(round(s.width())), int(round(s.height()))

    def set_box(self, w, h):
        pw, ph = self.image_size()
        w, h = int(math.ceil(w)), int(math.ceil(h))
        if (w - pw) % 2:
            w += 1
        if (h - ph) % 2:
            h += 1
        self.setFixedSize(w, h)

    def show_now(self):
        """Show and paint at once, before the real window is hidden."""
        self._token += 1
        self.show()
        self.raise_()
        self.repaint()

    def hide_soon(self, ms=60):
        """Hide a moment after the real window was shown, so there is never a
        frame with neither of them on screen (that blank frame is a blink)."""
        token = self._token

        def go():
            if token == self._token:
                self.hide()
                self.pixmap = None
        QTimer.singleShot(ms, go)

    def reset(self):
        self.opacity = 1.0
        self.sx = self.sy = 1.0
        self.rotation = 0.0
        self.drawer = None        # (header_h, visible_h, radius) for fold/unfold
        self.ring = None          # (radius, alpha, width, QColor) ripple
        self.image_opacity = 1.0

    def place(self, center_global):
        """Keep the window centred on the moving bag (moves are cheap)."""
        pw, ph = self.image_size()
        # top-left of the image on whole pixels, then the margin around it
        self.move(int(round(center_global.x() - pw / 2.0)) - (self.width() - pw) // 2,
                  int(round(center_global.y() - ph / 2.0)) - (self.height() - ph) // 2)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        if self.pixmap is not None and self.image_opacity > 0.003:
            size = QSizeF(*self.image_size())
            p.save()
            p.setOpacity(max(0.0, min(1.0, self.opacity * self.image_opacity)))
            if self.drawer is not None:
                header_h, visible_h, radius = self.drawer
                top = QPointF((self.width() - size.width()) // 2, (self.height() - size.height()) // 2)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(top, QSizeF(size.width(), max(header_h, visible_h))), radius, radius)
                p.setClipPath(clip)
                # body slides out from under the header like a drawer
                slide = size.height() - max(header_h, visible_h)
                body_src = QRectF(0, header_h, size.width(), size.height() - header_h)
                dpr = self.pixmap.devicePixelRatio()
                p.drawPixmap(QRectF(top.x(), top.y() + header_h - slide, body_src.width(), body_src.height()),
                             self.pixmap, QRectF(body_src.x() * dpr, body_src.y() * dpr,
                                                 body_src.width() * dpr, body_src.height() * dpr))
                p.drawPixmap(QRectF(top.x(), top.y(), size.width(), header_h), self.pixmap,
                             QRectF(0, 0, size.width() * dpr, header_h * dpr))
            else:
                if abs(self.rotation) < 1e-6 and abs(self.sx - 1.0) < 1e-6 and abs(self.sy - 1.0) < 1e-6:
                    # resting frame: blit 1:1 at whole pixels (identical to the window)
                    p.drawPixmap(QRectF((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                                        size.width(), size.height()), self.pixmap, QRectF(self.pixmap.rect()))
                else:
                    p.translate(c)
                    p.rotate(self.rotation)
                    p.scale(self.sx, self.sy)
                    p.drawPixmap(QRectF(-size.width() / 2.0, -size.height() / 2.0, size.width(), size.height()),
                                 self.pixmap, QRectF(self.pixmap.rect()))
            p.restore()
        if self.ring is not None:
            radius, alpha, width, color = self.ring
            col = QColor(color)
            col.setAlphaF(max(0.0, min(1.0, alpha)))
            p.setPen(QPen(col, width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, radius, radius)
        p.end()


# ----------------------------------------------------------------------
class ChipBar(QWidget):
    """Segmented category control. The accent pill slides between the
    buttons on a spring, stretching ahead and catching up like a gel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = None
        self.buttons = []
        self.active = None
        self.motion_enabled = True
        self.speed = 1.0
        self._from = None
        self._to = None
        self._t = 1.0
        self._tween = None

    def add(self, button):
        self.buttons.append(button)

    def _rect_of(self, b):
        return QRectF(b.geometry()).adjusted(1, 1, -1, -1)

    def set_active(self, button, animate=True):
        old = self.active
        self.active = button
        if not (animate and self.motion_enabled and old is not None and old is not button and self.isVisible()):
            if self._tween is not None:
                self._tween.stop()
            self._from = self._to = None
            self._t = 1.0
            self.update()
            return
        if self._tween is not None and self._tween.running:
            self._from = self._current_rect()
            self._tween.stop()
        else:
            self._from = self._rect_of(old)
        self._to = button

        def frame(t):
            self._t = t
            self.update()
        self._tween = Tween(self, 380 * self.speed, frame).start()

    def _current_rect(self):
        if self.active is None:
            return None
        to = self._rect_of(self.active)
        if self._from is None or self._t >= 1.0:
            return to
        f = self._from
        moving_right = to.center().x() >= f.center().x()
        lead = spring(min(1.0, self._t / 0.7), 0.7)   # leading edge: fast
        trail = spring(self._t, 0.62)                   # trailing edge: catches up
        if moving_right:
            left, right = lerp(f.left(), to.left(), trail), lerp(f.right(), to.right(), lead)
        else:
            left, right = lerp(f.left(), to.left(), lead), lerp(f.right(), to.right(), trail)
        if right - left < 8:
            mid = (left + right) / 2.0
            left, right = mid - 4, mid + 4
        return QRectF(left, to.top(), right - left, to.height())

    def paintEvent(self, event):
        if self.theme is None or not self.buttons:
            return
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        first, last = self.buttons[0].geometry(), self.buttons[-1].geometry()
        track = QRectF(first.left() - 2, first.top() - 2, last.right() - first.left() + 5, first.height() + 4)
        card = QColor(t.p["card"])
        card.setAlpha(235)
        border = QColor(0, 0, 0, 30) if not t.dark else QColor(255, 255, 255, 30)
        p.setPen(QPen(border, 1))
        p.setBrush(card)
        p.drawRoundedRect(track, track.height() / 2.0, track.height() / 2.0)
        pill = self._current_rect()
        if pill is not None:
            shadow = QColor(t.accent).darker(140)
            shadow.setAlpha(60)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shadow)
            p.drawRoundedRect(pill.translated(0, 1.2), pill.height() / 2.0, pill.height() / 2.0)
            p.setBrush(t.accent)
            p.drawRoundedRect(pill, pill.height() / 2.0, pill.height() / 2.0)
        p.end()


# ----------------------------------------------------------------------
def fly(owner, ghost, pixmap, duration, frame, finished=None, enabled=True, max_scale=1.12):
    """Run a ghost animation (see GhostWidget). frame(t) returns a dict with
    center (global QPointF) and optionally sx, sy, rotation, opacity,
    image_opacity, ring. Returns the Tween (or None when motion is off)."""
    import math
    if not enabled or pixmap is None or pixmap.isNull():
        if finished:
            finished()
        return None
    ghost.reset()
    ghost.pixmap = pixmap
    size = QSizeF(pixmap.size()) / pixmap.devicePixelRatio()
    tilt = math.radians(10)
    w = (size.width() * math.cos(tilt) + size.height() * math.sin(tilt)) * max_scale + 48
    h = (size.width() * math.sin(tilt) + size.height() * math.cos(tilt)) * max_scale + 48
    ghost.set_box(w, h)

    def apply(t):
        st = frame(t)
        ghost.sx = st.get("sx", 1.0)
        ghost.sy = st.get("sy", 1.0)
        ghost.rotation = st.get("rotation", 0.0)
        ghost.opacity = st.get("opacity", 1.0)
        ghost.image_opacity = st.get("image_opacity", 1.0)
        ghost.ring = st.get("ring")
        ghost.place(st["center"])
        ghost.update()

    apply(0.0)
    ghost.show_now()

    def done():
        if finished:
            finished()           # the real window appears first …
        ghost.hide_soon()        # … then the ghost leaves (no blank frame)
    return Tween(owner, duration, apply, done).start()


# ----------------------------------------------------------------------
def _smooth(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


class GenieWidget(QWidget):
    """The macOS-style "Genie" minimize.

    The window's picture is cut into thin slices; each slice slides toward the
    target (the bubble) while the sides bend into a funnel, so the window
    pours itself into the bubble like liquid – and flows back out on restore.

    The overlay is placed so the picture sits on exactly the same device
    pixels as the real window (its origin is the window's origin minus a
    multiple of 4 logical px, which is a whole number of device pixels at
    100/125/150/175/200 % scaling). The first and last frames are therefore
    identical to the window: swapping them is invisible – no blink, no shake.
    """

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.pixmap = None
        self.t = 0.0
        self._token = 0
        self.win = QRectF()
        self.tgt = QRectF()
        self.vertical = True
        self.toward_end = True     # target lies after the window along the axis

    def setup(self, pixmap, win_rect, target_rect):
        """win_rect: QRect of the window (global, logical); target_rect: QRectF of the bubble."""
        self.pixmap = pixmap
        tr = target_rect.toAlignedRect()
        union = win_rect.united(tr).adjusted(-8, -8, 8, 8)

        def snap(d):
            return (max(0, d) + 3) // 4 * 4
        ox = win_rect.left() - snap(win_rect.left() - union.left())
        oy = win_rect.top() - snap(win_rect.top() - union.top())
        w = max(union.right(), win_rect.right()) - ox + 2
        h = max(union.bottom(), win_rect.bottom()) - oy + 2
        self.setGeometry(ox, oy, w, h)
        self.win = QRectF(win_rect.translated(-ox, -oy))
        self.tgt = QRectF(target_rect).translated(-ox, -oy)
        c, tc = self.win.center(), self.tgt.center()
        # funnel along the axis on which the bubble lies outside the window
        outside_v = tc.y() < self.win.top() or tc.y() > self.win.bottom()
        outside_h = tc.x() < self.win.left() or tc.x() > self.win.right()
        if outside_v and outside_h:
            self.vertical = abs(tc.y() - c.y()) / max(1.0, self.win.height()) >= \
                abs(tc.x() - c.x()) / max(1.0, self.win.width())
        else:
            self.vertical = outside_v or not outside_h and abs(tc.y() - c.y()) >= abs(tc.x() - c.x())
        self.toward_end = (tc.y() > c.y()) if self.vertical else (tc.x() > c.x())

    def set_t(self, t):
        self.t = max(0.0, min(1.0, t))
        self.update()

    def show_now(self):
        self._token += 1
        self.show()
        self.raise_()
        self.repaint()

    def hide_soon(self, ms=60):
        token = self._token

        def go():
            if token == self._token:
                self.hide()
                self.pixmap = None
        QTimer.singleShot(ms, go)

    # --- the effect --------------------------------------------------------------
    def paintEvent(self, e):
        if self.pixmap is None:
            return
        p = QPainter(self)
        t = self.t
        if t <= 0.0005:                       # resting: exactly the window, pixel for pixel
            p.drawPixmap(self.win.topLeft().toPoint(), self.pixmap)
            p.end()
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        V = self.vertical
        win, tgt = self.win, self.tgt
        # along-axis span of the window, cross-axis span, target position
        a0, a1 = (win.top(), win.bottom() + 1) if V else (win.left(), win.right() + 1)
        b0, b1 = (win.left(), win.right() + 1) if V else (win.top(), win.bottom() + 1)
        tc = tgt.center()
        at = tc.y() if V else tc.x()
        half = (tgt.width() if V else tgt.height()) * 0.42
        t0, t1 = ((tc.x() if V else tc.y()) - half, (tc.x() if V else tc.y()) + half)
        far = a0 if self.toward_end else a1     # edge furthest from the target
        span = (at - far) or 1.0
        funnel = _smooth(min(1.0, t / 0.5))                      # sides bend first …
        slide = _smooth(max(0.0, (t - 0.22) / 0.78))             # … then it pours in
        src_len = (self.pixmap.height() if V else self.pixmap.width())   # device px
        step = 3
        fade = 1.0 - _smooth(max(0.0, (t - 0.86) / 0.14))
        p.setOpacity(fade)

        shift = slide * (at - far)          # the window slides as one piece …

        def dest_a(v):
            # v: 0 at the far edge … 1 at the edge nearest the target
            a_src = far + (a1 - a0) * v * (1 if self.toward_end else -1)
            d = a_src + shift
            # … and whatever reaches the neck disappears into the bubble
            return min(d, at) if self.toward_end else max(d, at)

        def cross(a):
            u = _smooth((a - far) / span)
            k = u * funnel
            return b0 + (t0 - b0) * k, b1 + (t1 - b1) * k

        L = float(src_len)
        i = 0
        while i < src_len:
            j = min(src_len, i + step)
            # device rows/cols [i, j) of the picture, counted from its start (a0 side)
            v_i = (i / L) if self.toward_end else 1.0 - (i / L)
            v_j = (j / L) if self.toward_end else 1.0 - (j / L)
            d_i, d_j = dest_a(v_i), dest_a(v_j)
            lo, hi = min(d_i, d_j), max(d_i, d_j)
            if hi - lo < 0.35:
                hi = lo + 0.35
            c0, c1 = cross((lo + hi) / 2.0)
            if V:
                src = QRectF(0, i, self.pixmap.width(), j - i)
                dst = QRectF(c0, lo, c1 - c0, hi - lo + 0.6)
            else:
                src = QRectF(i, 0, j - i, self.pixmap.height())
                dst = QRectF(lo, c0, hi - lo + 0.6, c1 - c0)
            p.drawPixmap(dst, self.pixmap, src)
            i = j
        p.end()

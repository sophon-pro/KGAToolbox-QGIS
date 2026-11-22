# -*- coding: utf-8 -*-
"""Panels that fold into a bubble on the map: Catalog, Data Explorer, Attribute Table.

The bubbles are the same kind the Tool Bag, Symbol Bag and Layer Colors use. Each one joins their
`BubbleGroup`, so it sits in the same column, at the position and in the size the Tool Bag's bubble
settings name, and takes its colour from the shared bag style. The dock / window folds into the
bubble and flows back out with the same animation as the bags (Genie or Classic).

* `PanelBubble`         - the round button on the map.
* `Motion`              - the fold / unfold animation of any widget (dock or window).
* `PanelBubbleManager`  - one dock (Catalog, Data Explorer): Minimize shows the bubble and hides the dock.
* `AttributeBubbleManager` - attribute tables: many windows, one bubble; each table gets a Minimize
  button, the bubble counts the tables waiting in it and lists them.
"""

import os
from contextlib import suppress

from qgis.PyQt import sip
from qgis.PyQt.QtCore import (QCoreApplication, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer,
                              QVariantAnimation, pyqtSignal)
from qgis.PyQt.QtGui import (QBrush, QColor, QCursor, QIcon, QLinearGradient, QPainter, QPainterPath,
                             QPen, QPixmap, QRadialGradient)
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QDockWidget, QGraphicsOpacityEffect, QHBoxLayout,
                                 QLabel, QMenu, QSizePolicy, QStyle, QToolBar, QToolButton, QWidget)
try:                                    # Qt 6: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:                     # Qt 5
    from qgis.PyQt.QtWidgets import QAction

from .bag_group import animation_style
from .tool_bag.bubble import FlyOverlay
from .tool_bag.themes import theme

PAD = 12            # room around the circle for the shadow, the hover swell and the badge
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icons')


def _alive(widget):
    return widget is not None and not sip.isdeleted(widget)


def minimize_icon(color):
    """A panel outline shrinking into a bubble: an open rounded square whose bottom-right corner
    holds a filled dot. Drawn large so it stays crisp."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(11, 11, 42, 42, 8, 8)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(31, 31, 17, 17)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------- glyphs
_GLYPHS = {}


def file_glyph(name):
    """A glyph from icons/<name>; the bubbles' white pictures live in icons/bubble/."""
    def load():
        return QPixmap(os.path.join(ICON_DIR, name))
    return load


def icon_glyph(icon_source):
    """A glyph painted white from a QIcon, or from a QIcon-returning callable (the toolbar button's icon)."""
    def load():
        icon = icon_source() if callable(icon_source) else icon_source
        pix = icon.pixmap(128, 128) if isinstance(icon, QIcon) else QPixmap()
        if pix.isNull():
            return pix
        p = QPainter(pix)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(pix.rect(), QColor('#ffffff'))
        p.end()
        return pix
    return load


def _glyph(loader):
    if loader not in _GLYPHS:
        _GLYPHS[loader] = loader()
    return _GLYPHS[loader]


# ---------------------------------------------------------------- the bubble
class PanelBubble(QWidget):
    """A round, glossy button on the map canvas."""

    clicked = pyqtSignal()
    hideRequested = pyqtSignal()
    holdRequested = pyqtSignal()       # left button held down (only when `hold_ms` is set)

    def __init__(self, group, canvas, key, tip, glyph):
        super().__init__(canvas)
        self.group = group
        self.canvas = canvas
        self.key = key
        self.glyph = glyph
        self.count = 0                # more than one: shown as a badge
        self.menu_fn = None           # callable(QMenu) that fills the right-click menu
        self.hold_ms = 0              # > 0: holding the left button this long emits holdRequested
        self._held = False
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._hold_fired)
        self.size_px = group.size_of(key)
        self._scale = 1.0
        self._pressed = False
        self.setObjectName('kga_bubble_' + key)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(tip)
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_scale)
        self._apply_size()
        group.register(key, self)
        group.settingsChanged.connect(self._group_changed)
        self.hide()

    def _group_changed(self, key):
        if key == 'style':
            self.update()

    # ---------------------------------------------------------- group protocol
    def _apply_size(self):
        side = self.size_px + 2 * PAD
        self.setFixedSize(side, side)

    def group_set_size(self, px):
        if px != self.size_px:
            self.size_px = px
            self._apply_size()
            self.update()

    def group_center(self):
        return QPointF(self.mapToGlobal(self.rect().center()))

    def group_move(self, center):
        if not _alive(self.canvas):
            return
        p = self.canvas.mapFromGlobal(QPoint(int(round(center.x())), int(round(center.y()))))
        self.move(p.x() - self.width() // 2, p.y() - self.height() // 2)

    def circle_global(self):
        """The circle in screen coordinates (where a panel flies from / to)."""
        d = float(self.size_px)
        c = self.group_center()
        return QRectF(c.x() - d / 2.0, c.y() - d / 2.0, d, d)

    # ---------------------------------------------------------- motion
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

    def pop_in(self):
        self._scale = 0.2
        self._animate_to(1.0, 380, QEasingCurve.Type.OutBack)

    def enterEvent(self, event):
        if not self._pressed:
            self._animate_to(1.1)
        super().enterEvent(event)

    def _hold_fired(self):
        if not self._pressed:
            return
        self._held = True                 # the release that follows must not count as a click
        self._pressed = False
        self._animate_to(1.0, 120)
        self.holdRequested.emit()

    def leaveEvent(self, event):
        self._hold_timer.stop()
        self._pressed = False
        self._animate_to(1.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._held = False
            self._animate_to(0.92, 80)
            if self.hold_ms > 0:
                self._hold_timer.start(self.hold_ms)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._hold_timer.stop()
        if event.button() == Qt.MouseButton.LeftButton and self._held:
            self._held = False
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            inside = self.rect().contains(event.pos())
            self._animate_to(1.1 if inside else 1.0, 160, QEasingCurve.Type.OutBack)
            if inside:
                self.clicked.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        if self.menu_fn is not None:
            self.menu_fn(menu)
        else:
            menu.addAction('Open', self.clicked.emit)
            menu.addAction('Hide Bubble', self.hideRequested.emit)
        menu.addSeparator()
        from .bag_common import fill_override_menus
        fill_override_menus(menu, self.group, self.key, with_style=True)
        menu.exec(event.globalPos())
        event.accept()

    # ---------------------------------------------------------- painting
    def _theme(self):
        key = self.group.style_of(self.key)
        return theme('classic' if key == 'white' else key)

    def paintEvent(self, _event):
        t = self._theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width() / 2.0, self.height() / 2.0)
        r = self.size_px / 2.0
        p.save()
        p.scale(self._scale, self._scale)

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(6):                       # soft shadow
            grow = i * 1.2
            p.setBrush(QColor(30, 45, 70, 20 - i * 3))
            p.drawEllipse(QPointF(0, 2), r + grow - 1, r + grow - 1)

        body = QLinearGradient(0, -r, 0, r)
        body.setColorAt(0.0, QColor(t['top']))
        body.setColorAt(1.0, QColor(t['bottom']))
        p.setBrush(QBrush(body))
        p.setPen(QPen(QColor(255, 255, 255, 150), 1.6))
        p.drawEllipse(QPointF(0, 0), r - 0.8, r - 0.8)

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

        pix = _glyph(self.glyph)
        if pix is not None and not pix.isNull():
            side = r * 1.2
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(QRectF(-side / 2.0, -side / 2.0, side, side), pix, QRectF(pix.rect()))
        p.restore()

        if self.count > 1:                       # how many windows wait in the bubble
            text = str(self.count) if self.count < 100 else '99+'
            font = p.font()
            font.setBold(True)
            font.setPixelSize(10)
            p.setFont(font)
            w = max(17, p.fontMetrics().horizontalAdvance(text) + 9)
            p.scale(self._scale, self._scale)
            badge = QRectF(r * 0.62 - w / 2.0 + 3, -r * 0.85 - 4, w, 17)
            p.setPen(QPen(QColor(t['bottom']), 1.4))
            p.setBrush(QColor('#ffffff'))
            p.drawRoundedRect(badge, 8.5, 8.5)
            p.setPen(QColor(t['handle']).darker(115))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


# ---------------------------------------------------------------- toolbar open / close
class ToolbarReveal:
    """Mixin (put it first) for a dock that opens out of / closes into its toolbar button.

    `PanelBubbleManager` fills `reveal_hook`; it returns True when it took over the show / hide.
    The hook only acts when a KGA toolbar button was just pressed, so the shortcut, the bubble and
    code paths keep their plain behaviour.
    """

    reveal_hook = None
    reveal_plain = False        # set while the animation itself shows / hides the dock
    reveal_by_close = False     # hook is being asked from closeEvent (the X button, not the toolbar)
    reveal_closing = False      # the fold in progress began as a close (so it ends as one)

    def setVisible(self, visible):  # noqa: N802 - Qt virtual
        hook = self.reveal_hook
        if hook is not None and not self.reveal_plain and hook(bool(visible)):
            return
        super().setVisible(visible)

    def closeEvent(self, event):  # noqa: N802 - Qt virtual
        hook = self.reveal_hook
        if hook is None or self.reveal_plain or not self.isVisible():
            super().closeEvent(event)
            return
        self.reveal_by_close = True             # the title bar's X: fold into the toolbar button too
        try:
            took = hook(False)
        finally:
            self.reveal_by_close = False
        if took:
            self.reveal_closing = True
            event.ignore()                      # the animation hides the dock when it has landed
            return
        super().closeEvent(event)


# ---------------------------------------------------------------- docked room
class DockRoom:
    """Opens / closes the room a *docked* panel takes, in step with its picture.

    Without this the dock area snaps open (or shut) the moment the panel is shown (hidden) and the
    picture then pours into an already-empty hole. Here the panel's content is hidden and the dock
    area is resized every frame, so the room grows (shrinks) while the picture flows in (out).
    Floating, tabbed and unplaced docks have no room to animate: `make` returns None for them.
    """

    FLOOR = 16

    @classmethod
    def make(cls, window, widget):
        try:
            if not isinstance(widget, QDockWidget) or widget.isFloating() or widget.widget() is None:
                return None
            area = window.dockWidgetArea(widget)
            if area == Qt.DockWidgetArea.NoDockWidgetArea:
                return None
            # tabbed with a dock that is showing: the area stays open either way. Tabbed only with
            # hidden docks (the Catalog next to a closed Toolbox): the panel alone owns the room.
            if any(d.isVisible() and not d.isFloating() for d in window.tabifiedDockWidgets(widget)):
                return None
            return cls(window, widget, area)
        except Exception:
            return None

    def __init__(self, window, dock, area):
        self.window = window
        self.dock = dock
        self.orient = Qt.Orientation.Horizontal \
            if area in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea) \
            else Qt.Orientation.Vertical
        self.full = 0
        self.began = False
        self.anim = None
        self.policy = None
        self.hidden = []
        self.last = None

    def _size(self):
        return self.dock.width() if self.orient == Qt.Orientation.Horizontal else self.dock.height()

    def begin(self):
        """Make the dock invisible (the picture stands in for it) and remember how big the room is.

        The content must stay *shown*: a dock whose widget is hidden collapses to its minimum width
        and Qt then ignores every `resizeDocks`, so the room would only snap at the end."""
        if self.began or not _alive(self.dock):
            return
        self.began = True
        self.full = self._size()
        inner = self.dock.widget()
        if _alive(inner):
            # an ignored size policy drops the content's own minimum size (the Data Explorer wants
            # 437 px): without it the room cannot shrink below that and only snaps at the end
            self.policy = inner.sizePolicy()
            inner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            # the effect alone left the content's rows painted at the dock's bottom while it shrank: the
            # content's own children are hidden (the content itself stays shown, or the dock collapses)
            self.hidden = [c for c in inner.children() if isinstance(c, QWidget) and c.isVisibleTo(inner)]
            for c in self.hidden:
                c.setVisible(False)
        if self.dock.graphicsEffect() is None:
            effect = QGraphicsOpacityEffect(self.dock)
            effect.setOpacity(0.0)
            self.dock.setGraphicsEffect(effect)
        self.dock.update()
        self.window.update()                    # the content's old pixels are repainted over, not left behind

    def set(self, openness):
        if not self.began or not _alive(self.dock):
            return
        size = max(1, int(round(self.FLOOR + (self.full - self.FLOOR) * max(0.0, min(1.0, openness)))))
        if size == self.last:
            return                              # nothing to re-lay-out: the main window is the costly part
        self.last = size
        # No window.update() here: Qt repaints what the resize moved (the dock, the map, the splitter).
        # A full update every frame repainted every toolbar and panel of QGIS 60 times a second
        # (~6000 widget paints per fold instead of ~500), which is what made dock animations lag.
        with suppress(Exception):
            self.window.resizeDocks([self.dock], [size], self.orient)

    def run(self, ms, opening):
        """Drive the room on its own clock (when the picture is not a genie)."""
        self.stop_anim()
        self.anim = QVariantAnimation(self.dock)
        self.anim.setStartValue(0.0 if opening else 1.0)
        self.anim.setEndValue(1.0 if opening else 0.0)
        self.anim.setDuration(max(1, int(ms)))
        self.anim.valueChanged.connect(lambda v: self.set(float(v)))
        self.set(0.0 if opening else 1.0)
        self.anim.start()

    def stop_anim(self):
        if self.anim is not None:
            anim, self.anim = self.anim, None
            anim.stop()
            anim.deleteLater()

    def finish(self):
        """Give the panel its full room and its content back. Safe to call twice."""
        if not self.began:
            return
        self.began = False
        self.stop_anim()
        self.last = None
        if not _alive(self.dock):
            return
        inner = self.dock.widget()
        if _alive(inner) and self.policy is not None:
            inner.setSizePolicy(self.policy)
        for c in self.hidden:
            if _alive(c):
                c.setVisible(True)
        self.hidden = []
        with suppress(Exception):
            self.window.resizeDocks([self.dock], [max(1, self.full)], self.orient)


# ---------------------------------------------------------------- the animation
class Motion:
    """Folds a widget into a bubble and unfolds it again, in the bags' animation style.

    Works for docks and for top-level windows: the widget is photographed, the picture pours into
    (or out of) the bubble's circle, and the real widget is hidden / shown exactly under the first /
    last frame so nothing is laid out while it moves.
    """

    def __init__(self, group, main_window):
        self.group = group
        self.window = main_window
        self.flyer = FlyOverlay(main_window)
        self.genie_w = None
        self.tween = None
        self.room = None

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _rect(widget):
        return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())

    def _enabled(self):
        return self.group.speed() > 0 and self.window.isVisible()

    def settle(self):
        """Finish an animation that is still running."""
        self.flyer.finish()
        if self.tween is not None:
            tween, self.tween = self.tween, None
            tween.stop(finish=True)
        self._end_room()

    def _end_room(self):
        room, self.room = self.room, None
        if room is not None:
            room.finish()

    def unload(self):
        self.settle()
        if _alive(self.genie_w):
            self.genie_w.hide()
            self.genie_w.deleteLater()
        if _alive(self.flyer):
            self.flyer.hide()
            self.flyer.deleteLater()

    # ------------------------------------------------------------ fold
    def fold(self, widget, circle, done, source=None):
        """Pour *widget* (visible) into *circle*; hide it; call *done* when it has landed.
        With *source* (a screen point, e.g. where the user clicked) the window starts as a small picture
        there and flies to the circle, instead of pouring out of its own place.
        False when nothing was animated (the caller hides the widget and carries on)."""
        self.settle()
        if circle is None or not self._enabled() or not widget.isVisible():
            return False
        try:
            if source is not None:              # a table that was only just made: lay it out before it is photographed
                widget.ensurePolished()
                layout = widget.window().layout()
                if layout is not None:
                    layout.activate()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            pix = widget.grab()
        except Exception:
            return False
        if pix.isNull():
            return False
        rect = self._rect(widget)
        if source is not None:
            small = pix.scaled(int(180 * pix.devicePixelRatio()), int(120 * pix.devicePixelRatio()),
                               Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            ratio = small.devicePixelRatio() or 1.0
            w, h = int(small.width() / ratio), int(small.height() / ratio)
            rect = QRect(source.x() - w // 2, source.y() - h // 2, w, h)
            pix = small
        room = None if source is not None else DockRoom.make(self.window, widget)
        self.room = room

        def landed():
            if room is None:
                if done is not None:
                    done()
                return
            # the room is back to full size before the dock is hidden, so it reopens the right size
            self._end_room()
            if _alive(widget):
                widget.setGraphicsEffect(None)
                widget.reveal_plain = True      # this hide is the end of the animation, not a new click
                closing = getattr(widget, 'reveal_closing', False)
                widget.reveal_closing = False
                try:
                    if closing:
                        widget.close()          # a real close: the dock announces `closed` as usual
                    else:
                        widget.hide()
                finally:
                    widget.reveal_plain = False
            if done is not None:
                done()
        hide = room.begin if room is not None else widget.hide
        if animation_style() == 'genie' and self._genie(False, pix, rect, circle, landed, hide, room,
                                                        fade=source is not None):
            return True
        hide()
        if room is not None:
            room.run(240, False)
        self.flyer.play(pix, QRectF(circle), QRectF(rect), False, 0.95, landed)
        return True

    # ------------------------------------------------------------ unfold
    def unfold(self, widget, circle, show):
        """Call *show* (which shows *widget*) and flow its picture out of *circle* over it."""
        self.settle()
        if circle is None or not self._enabled():
            show()
            return
        top = widget.isWindow()
        old = widget.windowOpacity() if top else 1.0
        veil = None
        if top:
            widget.setWindowOpacity(0.0)        # invisible until the last frame
        elif widget.graphicsEffect() is None:
            veil = QGraphicsOpacityEffect(widget)   # in place before the show: no unpainted flash
            veil.setOpacity(0.0)
            widget.setGraphicsEffect(veil)
        # a docked panel opens its dock area (the map shrinks, its unpainted children show) the moment it is
        # shown: the main window stays frozen until the area is back to its closed state under the picture
        frozen = not top and self.window.updatesEnabled()
        if frozen:
            self.window.setUpdatesEnabled(False)

        def thaw():
            if frozen and _alive(self.window):
                self.window.setUpdatesEnabled(True)
                # A repaint Qt tried while the window was frozen leaves the whole window marked dirty;
                # update() then counts it as "already dirty" and never asks again, so nothing on screen
                # changed (no dock, the map not resized) until the window was dragged or another panel
                # opened. repaint() paints now and clears that state.
                self.window.repaint()
        show()
        if isinstance(widget, QDockWidget) and not widget.isFloating():
            widget.raise_()                     # a tab behind another dock only gets its place once it is in front

        def unveil():
            self._end_room()
            if not _alive(widget):
                return
            if top:
                widget.setWindowOpacity(old)
                # A separate window (an undocked attribute table) was shown while the picture covered it;
                # the picture's own window and the main window took the focus meanwhile and left it
                # behind QGIS. Bring it to the front once the picture is gone, and again after the
                # picture's window has finished hiding.
                def front():
                    if _alive(widget) and widget.isVisible():
                        widget.raise_()
                        widget.activateWindow()
                front()
                QTimer.singleShot(0, front)
                QTimer.singleShot(150, front)
            else:
                widget.setGraphicsEffect(None)

        try:
            widget.ensurePolished()
            layout = widget.window().layout()
            if layout is not None:
                layout.activate()               # it has its final place before it is photographed
            if veil is not None:
                veil.setOpacity(1.0)            # the photo needs the real picture; nothing paints meanwhile
            if frozen:
                self.window.setUpdatesEnabled(True)     # a frozen window renders its children blank
            try:
                pix = widget.grab()
            finally:
                if frozen:
                    self.window.setUpdatesEnabled(False)    # no event loop ran in between: nothing was painted
            if pix.isNull() or not widget.isVisible():
                raise ValueError('nothing to animate')
            rect = self._rect(widget)
            if veil is not None:
                veil.setOpacity(0.0)
        except Exception:
            if top:
                widget.setWindowOpacity(old)
            elif veil is not None:
                widget.setGraphicsEffect(None)
            thaw()
            return
        room = None if top else DockRoom.make(self.window, widget)
        self.room = room
        if room is not None:
            room.begin()                        # content away, room closed: it opens with the picture
            room.set(0.0)
        # resizeDocks applies on the next pass of the event loop: thaw only after it, or one frame shows
        # the dock area still open
        QTimer.singleShot(40, thaw)
        if animation_style() == 'genie' and self._genie(True, pix, rect, circle, unveil, None, room):
            return
        if room is not None:
            room.run(360, True)
        self.flyer.play(pix, QRectF(circle), QRectF(rect), True, 1.0, unveil)

    def fly(self, pix, rect, circle, done):
        """Pour a picture (not a widget) from *rect* into *circle*; *done* when it has landed. False if not animated."""
        self.settle()
        if circle is None or not self._enabled() or pix.isNull():
            return False
        if animation_style() == 'genie' and self._genie(False, pix, rect, circle, done, None, None, fade=True):
            return True
        self.flyer.play(pix, QRectF(circle), QRectF(rect), False, 0.95, done)
        return True

    # ------------------------------------------------------------ the genie
    def _genie(self, opening, pix, rect, circle, done, hide, room=None, fade=False):
        from .symbol_bag.ui.motion import Tween
        from .symbol_bag.ui.widgets import GenieWidget
        try:
            if self.genie_w is None or not _alive(self.genie_w):
                self.genie_w = GenieWidget(self.window)
            g = self.genie_w
            g.setup(pix, rect, QRectF(circle))
        except Exception:
            return False
        self.group.play_sound('out' if opening else 'in')
        g.set_t(1.0 if opening else 0.0)
        g.setWindowOpacity(0.0 if fade else 1.0)        # a small picture made at the click grows out of nothing
        g.show_now()                            # exactly over the widget ...
        if hide is not None:
            hide()                              # ... so hiding it changes nothing on screen

        def frame(t):
            g.set_t(1.0 - t if opening else t)
            if fade:
                g.setWindowOpacity(min(1.0, t / 0.35))
            if room is not None:
                room.set(t if opening else 1.0 - t)

        def end():
            self.tween = None
            g.setWindowOpacity(1.0)
            if opening:
                g.hide_soon()
            else:
                g.hide()
                g.pixmap = None
            if done is not None:
                done()
        speed = self.group.speed()
        self.tween = Tween(self.window, (460 if opening else 520) * speed, frame, end).start()
        return True


# ---------------------------------------------------------------- dock reveal (no bubble)
class DockReveal:
    """Opens a dock out of its toolbar button and folds it back in (no bubble).

    The dock's class carries `ToolbarReveal`; this fills its `reveal_hook`. `toggle_action` is the
    toolbar action that shows the dock: when its checked state agrees with the change being made
    the click came from the button (or its shortcut), so the dock flies from / to that button even
    if the press tracker saw nothing (the button is not on the KGA toolbar, say).
    """

    def __init__(self, panel, motion, key='dock'):
        self.panel = panel
        self.motion = motion
        self.key = key
        self._busy = 0                          # our own show / hide: not a toolbar press
        self.toggle_action = None
        if isinstance(panel, ToolbarReveal):
            panel.reveal_hook = self._toolbar_hook

    def _before_open(self):
        """Hook for subclasses: runs when the dock is about to open out of the button."""

    def _toolbar_hook(self, visible):
        """The toolbar button shows / hides the dock: open it out of the button, fold it back in."""
        panel = self.panel
        if self._busy or not _alive(panel) or visible == panel.isVisible():
            return False
        from ..core import kga_settings as ks
        from . import window_reveal
        if not ks.window_reveal():
            return False
        origin = window_reveal.take_origin()
        action = self.toggle_action
        if origin is None and action is not None and action.isChecked() == visible:
            origin = self._button_rect()        # the dock's own toolbar button
        if origin is None and not visible and getattr(panel, 'reveal_by_close', False):
            origin = self._button_rect() or self._fallback_rect()   # closed with its X: folds into its button
        if origin is None:
            return False
        self.motion.settle()
        self._busy += 1
        try:
            if visible:
                self._before_open()
                self.motion.unfold(panel, origin, lambda: QDockWidget.setVisible(panel, True))
                return True
            return bool(self.motion.fold(panel, origin, None))
        except Exception:
            from qgis.core import Qgis, QgsMessageLog
            import traceback
            QgsMessageLog.logMessage('Toolbar animation skipped for {}: {}'.format(
                self.key, traceback.format_exc()), 'KGA Toolbox', Qgis.MessageLevel.Warning)
            return False
        finally:
            self._busy -= 1

    def _fallback_rect(self):
        """Where a dock folds to when its toolbar button is not on screen (None: no animation)."""
        return None

    def _button_rect(self):
        """Screen rectangle of the toolbar button of `toggle_action`, or None."""
        action = self.toggle_action
        if action is None:
            return None
        from . import window_reveal
        from ..core import compat
        for widget in compat.action_widgets(action):
            rect = window_reveal.button_rect(widget) if isinstance(widget, QToolButton) else None
            if rect is not None:
                return rect
        return None


_REVEAL = {'group': None, 'all': []}


class DockTitleBar(QWidget):
    """A dock's title bar with a Minimize-to-Bubble button beside Float and Close."""

    def __init__(self, dock, on_minimize):
        super().__init__(dock)
        self.dock = dock
        self.setObjectName('kga_dock_title')
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 1, 2, 1)
        row.setSpacing(0)
        self.label = QLabel(dock.windowTitle(), self)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.label, 1)
        style = self.style()
        self.minimize = self._button(minimize_icon(self.palette().color(self.palette().ColorRole.WindowText)),
                                     'Minimize to a bubble on the map', on_minimize)
        self.float_btn = self._button(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton),
                                      'Float', lambda: dock.setFloating(not dock.isFloating()))
        self.close_btn = self._button(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton),
                                      'Close', dock.close)
        for b in (self.minimize, self.float_btn, self.close_btn):
            row.addWidget(b)
        self.setStyleSheet(
            '#kga_dock_title QToolButton { border: none; background: transparent; padding: 2px;'
            ' border-radius: 3px; }'
            '#kga_dock_title QToolButton:hover { background: rgba(0, 121, 193, 45); }'
            '#kga_dock_title QToolButton:pressed { background: rgba(0, 121, 193, 90); }')
        dock.windowTitleChanged.connect(self.label.setText)
        dock.featuresChanged.connect(self._features)
        self._features(dock.features())

    def _button(self, icon, tip, slot):
        b = QToolButton(self)
        b.setAutoRaise(True)
        b.setIcon(icon)
        b.setIconSize(QSize(14, 14))
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(lambda _c=False: slot())
        return b

    def _features(self, features):
        self.float_btn.setVisible(bool(features & QDockWidget.DockWidgetFeature.DockWidgetFloatable))
        self.close_btn.setVisible(bool(features & QDockWidget.DockWidgetFeature.DockWidgetClosable))

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt virtual
        if self.dock.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable:
            self.dock.setFloating(not self.dock.isFloating())
        event.accept()


def install_title_bar(panel, manager):
    """Give *panel* the title bar with Minimize beside Float and Close, wired to its bubble *manager*."""
    bar = DockTitleBar(panel, manager.minimize)
    panel.setTitleBarWidget(bar)
    bar.minimize.setVisible(manager.bubbles_enabled())
    return bar


def set_reveal_group(group):
    """The BubbleGroup whose speed / sound / style setting the dock animations follow."""
    _REVEAL['group'] = group


def attach_reveal(panel, action=None, key='dock', tip=None, glyph=None, window=None):
    """Let *panel* (its class carries `ToolbarReveal`) open out of / fold into its toolbar button.

    With *tip* and *glyph* the panel also gets a Minimize button in its title bar and a bubble on the map
    (`PanelBubbleManager`); without them it only animates. *window* is the main window the dock lives
    in when that is not QGIS's own (a layout designer); only the bubble-less form supports it.
    """
    group = _REVEAL['group']
    with_bubble = tip is not None and glyph is not None
    # A dock of QGIS itself (the Layers panel) cannot carry the mixin: it only gets the bubble, whose
    # fold / unfold animation needs no hook. The toolbar reveal is for our own docks.
    if group is None or not isinstance(panel, QDockWidget) or not (
            with_bubble or isinstance(panel, ToolbarReveal)):
        return None
    try:
        from qgis.utils import iface
        if with_bubble:
            def restore():
                panel.show()
                panel.raise_()
            reveal = PanelBubbleManager(panel, group, iface.mapCanvas(), restore, key, tip, glyph)
            install_title_bar(panel, reveal)
        else:
            reveal = DockReveal(panel, Motion(group, window or iface.mainWindow()), key)
    except Exception:
        return None
    reveal.toggle_action = action
    _REVEAL['all'].append(reveal)
    return reveal


def detach_reveal(panel):
    """Forget the reveal of *panel* (a dock that is about to be destroyed, e.g. with its layout designer)."""
    for reveal in list(_REVEAL['all']):
        if reveal.panel is panel:
            _REVEAL['all'].remove(reveal)
            with suppress(Exception):
                reveal.motion.unload()
    if _alive(panel):
        panel.reveal_hook = None


def unload_reveals():
    for reveal in _REVEAL['all']:
        with suppress(Exception):
            if hasattr(reveal, 'bubble'):
                reveal.unload()
            else:
                reveal.motion.unload()
        if _alive(reveal.panel):
            reveal.panel.reveal_hook = None
            bar = reveal.panel.titleBarWidget()
            if isinstance(bar, DockTitleBar):          # give a dock of QGIS its own title bar back
                reveal.panel.setTitleBarWidget(None)
                bar.deleteLater()
    _REVEAL['all'] = []
    _REVEAL['group'] = None


# ---------------------------------------------------------------- one dock
class PanelBubbleManager(DockReveal):
    """Folds one dock (Catalog, Data Explorer) into its bubble and back.

    The bubble shows only when the user minimized the dock (its Minimize button or menu entry).
    Anything that shows the dock again - the bubble, the toolbar button, Reveal in Catalog -
    takes the bubble away; closing the dock with the toolbar button hides both.
    """

    def __init__(self, panel, group, canvas, on_restore, key, tip, glyph):
        self.group = group
        self._on_restore = on_restore           # callable: show the dock again, in front
        self._minimizing = False
        self.bubble = PanelBubble(group, canvas, key, tip, glyph)
        DockReveal.__init__(self, panel, Motion(group, canvas.window()), key)
        self.bubble.clicked.connect(self.restore)
        self.bubble.hideRequested.connect(self.dismiss)
        panel.visibilityChanged.connect(self._visibility_changed)
        group.settingsChanged.connect(self._group_changed)

    def _before_open(self):
        self._hide_bubble()

    def _fallback_rect(self):
        with suppress(Exception):
            return self.group.slot_rect(self.key)       # its bubble's spot on the map
        return None

    def bubbles_enabled(self):
        return self.group.bubble_enabled(self.key)

    def _group_changed(self, key):
        """Settings > Bubbles: a bubble switched off brings its dock back; the title bar button follows."""
        if key not in ('bubble', 'disabled'):
            return
        enabled = self.bubbles_enabled()
        bar = self.panel.titleBarWidget() if _alive(self.panel) else None
        if isinstance(bar, DockTitleBar):
            bar.minimize.setVisible(enabled)
        if not enabled and _alive(self.bubble) and self.bubble.isVisible():
            self.restore()

    def minimize(self):
        """Dock -> bubble. With bubbles switched off in the shared settings it just hides the dock."""
        if not _alive(self.panel):
            return
        self.motion.settle()
        self._busy += 1
        try:
            self._minimize()
        finally:
            self._busy -= 1

    def _minimize(self):
        self._minimizing = self.bubbles_enabled()
        circle = None
        if self._minimizing and self.panel.isVisible():
            self.group.set_present(self.key, True, animate=False)    # the others make room first
            circle = self.group.slot_rect(self.key)
        if not (circle is not None and self.motion.fold(self.panel, circle, self._landed)):
            self.panel.hide()
            if self._minimizing:
                self._show_bubble()
            # visibilityChanged can arrive a beat after hide(); keep the flag until then
            QTimer.singleShot(0, self._end_minimize)

    def _landed(self):
        """The fold finished (a docked panel is hidden only now): show the bubble."""
        if self._minimizing:
            self._show_bubble()
        QTimer.singleShot(0, self._end_minimize)

    def _end_minimize(self):
        self._minimizing = False

    def restore(self):
        """Bubble -> dock."""
        self.motion.settle()
        circle = self.bubble.circle_global() if _alive(self.bubble) and self.bubble.isVisible() else None
        self._hide_bubble()
        if _alive(self.panel):
            self._busy += 1
            try:
                self.motion.unfold(self.panel, circle, self._on_restore)
            finally:
                self._busy -= 1

    def dismiss(self):
        """Take the bubble away without opening the dock."""
        self._hide_bubble()

    def unload(self):
        with suppress(Exception):
            self.panel.visibilityChanged.disconnect(self._visibility_changed)
        with suppress(Exception):
            self.group.settingsChanged.disconnect(self._group_changed)
        self.motion.unload()
        self.group.unregister(self.key)
        if _alive(self.bubble):
            self.bubble.hide()
            self.bubble.deleteLater()
        self.bubble = None

    def _show_bubble(self):
        if not _alive(self.bubble):
            return
        self.group.set_present(self.key, True, animate=False)
        self.bubble.show()
        self.bubble.raise_()
        self.bubble.pop_in()
        self.group.relayout(False)

    def _hide_bubble(self):
        if not _alive(self.bubble):
            return
        self.bubble.hide()
        self.group.set_present(self.key, False)

    def _visibility_changed(self, visible):
        if visible:
            self._hide_bubble()                 # the dock is back: the bubble has done its job
        elif not self._minimizing:
            self._hide_bubble()                 # closed for good (toolbar button / dock close)


# ---------------------------------------------------------------- attribute tables
ATTRIBUTE_CLASS = 'QgsAttributeTableDialog'
ATTRIBUTE_KEY = 'attributes'
HOLD_MS = 350                 # how long the bubble is held before the layer list appears


class AttributeBubbleManager(QObject):
    """Attribute tables fold into one bubble.

    QGIS makes the attribute table in C++, so this watches for them: a docked table
    gets a Minimize button in its dock's title bar, beside Float and Close, like every KGA panel; a
    floating table (a window with the system's title bar) gets one in its toolbar. A minimized table is hidden and
    waits in the bubble, which shows how many there are. A click opens it (a menu when several wait);
    the right-click menu lists them all.

    The search for new tables walks every window and panel of QGIS, which costs several milliseconds -
    a dropped frame when it lands in the middle of a zoom. So the once-a-second tick only searches when
    something may have appeared: the main window got a new child (a docked table) or another window
    took the focus (a table in its own window). Every FULL_SCAN_TICKS ticks it searches regardless.
    """

    SCAN_MS = 1000
    FULL_SCAN_TICKS = 10

    def __init__(self, iface, group, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.group = group
        self.window = iface.mainWindow()
        self.items = []                         # [(target widget, title)], oldest first
        self._pending = 0                       # tables being made for the bubble (it must stay up meanwhile)
        self._actions = []                      # [(table dialog, its toolbar Minimize action)]
        self.bubble = PanelBubble(group, iface.mapCanvas(), ATTRIBUTE_KEY,
                                  'Attribute tables – click to open, hold for the layers of this map',
                                  file_glyph('bubble/attribute_tables.png'))
        self.bubble.menu_fn = self._fill_menu
        self.bubble.clicked.connect(self._clicked)
        self.bubble.hold_ms = HOLD_MS
        self.bubble.holdRequested.connect(self._show_layer_list)
        self.motion = Motion(group, self.window)
        self._dirty = True                      # a table may have appeared since the last search
        self._ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.SCAN_MS)
        self._timer.timeout.connect(self._scan)
        self._timer.start()
        group.settingsChanged.connect(self._group_changed)
        self.window.installEventFilter(self)
        with suppress(Exception):
            QApplication.instance().focusWindowChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args):
        self._dirty = True

    def eventFilter(self, obj, event):  # noqa: N802 - Qt virtual
        if event.type() == QEvent.Type.ChildAdded:
            self._dirty = True
        return False

    def _group_changed(self, key):
        if key not in ('bubble', 'disabled'):
            return
        self._sync_buttons()
        if not self.group.bubble_enabled(ATTRIBUTE_KEY):
            self.restore_all()                  # a switched-off bubble gives its tables back

    def _sync_buttons(self):
        """Docked tables: the title-bar button; floating ones: the toolbar button."""
        enabled = self.group.bubble_enabled(ATTRIBUTE_KEY)
        self._actions = [(d, a) for d, a in self._actions if _alive(d) and _alive(a)]
        for dialog, action in self._actions:
            target = self._target(dialog)
            docked = isinstance(target, QDockWidget)
            if docked and not isinstance(target.titleBarWidget(), DockTitleBar):
                target.setTitleBarWidget(DockTitleBar(target, lambda d=dialog: self.minimize(d)))
            bar = target.titleBarWidget() if docked else None
            if isinstance(bar, DockTitleBar):
                bar.minimize.setVisible(enabled)
            action.setVisible(enabled and not docked)

    # ------------------------------------------------------------ finding the tables
    def _scan(self):
        if not _alive(self.window):
            return
        if not self.group.bubble_enabled(ATTRIBUTE_KEY):
            self._sync_buttons()                # cheap: only the tables already seen
            self._refresh()                     # a restore may still be landing
            self._dirty = True                  # search as soon as the bubble is switched on again
            return                              # no search for tables nobody can minimize
        self._ticks += 1
        if self._dirty or self._ticks >= self.FULL_SCAN_TICKS:
            self._dirty = False
            self._ticks = 0
            for dialog in self._tables():
                try:
                    if not dialog.property('kgaBubble'):
                        self._inject(dialog)
                        if not dialog.property('kgaBubble'):
                            self._dirty = True  # its toolbar is not built yet: look again next tick
                except RuntimeError:            # deleted while we looked
                    continue
        self._sync_buttons()                    # a table may have been docked or floated since
        self._refresh()

    def _tables(self):
        """Every attribute table dialog there is, floating or docked.

        Docks (floating or not) are direct children of the main window, so only those are looked at:
        a recursive findChildren() wraps every object in QGIS, and once a second that froze it."""
        found = list(QApplication.topLevelWidgets()) + self.window.findChildren(
            QDockWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)   # (a name of '' would match unnamed docks only)
        tables, seen = [], set()

        def add(table):
            if id(table) not in seen:           # a floating dock is both a window and a dock
                seen.add(id(table))
                tables.append(table)
        for w in found:
            try:
                if w.metaObject().className() == ATTRIBUTE_CLASS:
                    add(w)
                elif isinstance(w, QDockWidget):
                    if w.widget() is not None and w.widget().metaObject().className() == ATTRIBUTE_CLASS:
                        add(w.widget())
                elif isinstance(w, QDialog):
                    # QGIS 3.44 opens an undocked table inside a plain QDialog window. children(), not
                    # findChildren(QWidget): the table's class is not in the Python API, and PyQt's type
                    # filter drops it (found live; a Python stand-in passes either way)
                    for child in w.children():
                        if child.metaObject().className() == ATTRIBUTE_CLASS:
                            add(child)
            except RuntimeError:                # deleted while we looked
                continue
        return tables

    def _inject(self, dialog):
        bar = dialog.findChild(QToolBar)
        if bar is None:
            return                              # not built yet: the next scan tries again
        dialog.setProperty('kgaBubble', True)
        color = bar.palette().color(bar.palette().ColorRole.WindowText)
        action = QAction(minimize_icon(color), 'Minimize to Bubble', bar)
        action.setToolTip('Minimize this attribute table to a bubble on the map')
        self._actions.append((dialog, action))
        action.triggered.connect(lambda _c=False, d=dialog: self.minimize(d))
        bar.addSeparator()
        bar.addAction(action)

    @staticmethod
    def _target(dialog):
        """The dock the table is docked in, else the window it is in (the table itself, or the QDialog
        QGIS 3.44 wraps an undocked table in): the thing to hide, show and title."""
        p = dialog.parent()
        while p is not None:
            if isinstance(p, QDockWidget):
                return p
            if isinstance(p, QDialog) and p.isWindow():
                return p
            p = p.parent()
        return dialog

    # ------------------------------------------------------------ minimize / restore
    def _live(self):
        return [(w, t) for w, t in self.items if _alive(w)]

    def open_in_bubble(self, layer, origin):
        """Send the attribute table of *layer* to the bubble, flying from *origin* (the screen point of
        the click). A small card flies at once; QGIS builds the table (and shows its busy cursor) only
        after the card has landed, hidden, so nothing of the table or its dock area ever shows."""
        if not self.group.bubble_enabled(ATTRIBUTE_KEY):
            self.iface.showAttributeTable(layer)    # bubbles are off: the table simply opens
            return
        self.motion.settle()
        self.group.set_present(ATTRIBUTE_KEY, True, animate=False)     # the others make room first
        circle = self.group.slot_rect(ATTRIBUTE_KEY) if not self.bubble.isVisible()             else self.bubble.circle_global()
        card = self._card()
        rect = QRect(int(origin.x() - card.width() / card.devicePixelRatio() / 2),
                     int(origin.y() - card.height() / card.devicePixelRatio() / 2),
                     int(card.width() / card.devicePixelRatio()), int(card.height() / card.devicePixelRatio()))
        self._pending += 1

        def landed():
            if _alive(self.bubble) and not self.bubble.isVisible():
                self.group.set_present(ATTRIBUTE_KEY, True, animate=False)
                self.bubble.count = len(self._live()) + 1
                self.bubble.show()
                self.bubble.raise_()
                self.bubble.pop_in()
                self.group.relayout(False)
            QTimer.singleShot(40, lambda: self._build_hidden(layer))    # let the bubble paint first
        if circle is None or not self.motion.fly(card, rect, circle, landed):
            self._build_hidden(layer)

    def _card(self):
        """The small dot (the bubble's colours) that flies from the click to the bubble: a window-like
        picture would read as a flash of a window."""
        ratio = self.window.devicePixelRatioF()
        d = 30
        pix = QPixmap(int(d * ratio), int(d * ratio))
        pix.setDevicePixelRatio(ratio)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.bubble._theme() if _alive(self.bubble) else {}
        p.setPen(QPen(QColor('#ffffff'), 2))
        p.setBrush(QColor(theme.get('handle', '#0079c1')))
        p.drawEllipse(QRectF(2, 2, d - 4, d - 4))
        p.end()
        return pix

    def _build_hidden(self, layer):
        """Make the table and put it away in the bubble without a frame of it reaching the screen: the main
        window is frozen meanwhile, and a floating table (its own window) is made see-through."""
        known = {id(d) for d in self._tables()}
        self.window.setUpdatesEnabled(False)
        hidden = []
        try:
            self.iface.showAttributeTable(layer)
            tables = self._tables()
            fresh = [d for d in tables if id(d) not in known]
            # a table that was already open for the layer: QGIS just raises it, send that one
            pool = fresh or [d for d in tables
                             if layer.name() in (self._target(d).windowTitle() or d.windowTitle())]
            for dialog in pool[-1:]:
                if not dialog.property('kgaBubble'):
                    self._inject(dialog)
                target = self._target(dialog)
                if not _alive(target):
                    continue
                if target.isWindow():
                    target.setWindowOpacity(0.0)
                    hidden.append(target)
                title = target.windowTitle() or dialog.windowTitle() or 'Attribute table'
                self.items = [i for i in self._live() if i[0] is not target]
                self.items.append((target, title))
                target.hide()
        finally:
            for target in hidden:
                if _alive(target):
                    target.setWindowOpacity(1.0)
            self.window.setUpdatesEnabled(True)
            self.window.repaint()           # a repaint dropped while frozen would otherwise stick (see Motion.unfold)
            self._pending = max(0, self._pending - 1)
            self._refresh()

    def minimize(self, dialog, source=None):
        target = self._target(dialog)
        if not _alive(target) or not target.isVisible():
            return
        self.motion.settle()
        title = target.windowTitle() or dialog.windowTitle() or 'Attribute table'
        self.items = [i for i in self._live() if i[0] is not target]
        self.items.append((target, title))
        self.group.set_present(ATTRIBUTE_KEY, True, animate=False)   # the others make room first
        circle = self.group.slot_rect(ATTRIBUTE_KEY) if not self.bubble.isVisible() \
            else self.bubble.circle_global()
        if not self.motion.fold(target, circle, self._refresh, source):
            target.hide()
            self._refresh()

    def _refresh(self):
        """Bring the bubble in line with the tables waiting in it."""
        if not _alive(self.bubble):
            return
        self.items = self._live()
        if self.motion.tween is not None or self.motion.flyer.running or self._pending:
            return                              # the landing refreshes once it is down
        self.bubble.count = len(self.items)
        if self.items:
            if not self.bubble.isVisible():
                self.group.set_present(ATTRIBUTE_KEY, True, animate=False)
                self.bubble.show()
                self.bubble.raise_()
                self.bubble.pop_in()
                self.group.relayout(False)
            self.bubble.update()
        elif self.bubble.isVisible() or ATTRIBUTE_KEY in self.group.present:
            self.bubble.hide()
            self.group.set_present(ATTRIBUTE_KEY, False)

    def restore(self, target, animate=True):
        self.motion.settle()
        circle = self.bubble.circle_global() if self.bubble.isVisible() else None
        self.items = [i for i in self._live() if i[0] is not target]
        self._refresh()
        if not _alive(target):
            return

        def show():
            target.show()
            target.raise_()
            if target.isWindow():
                target.activateWindow()
        self.motion.unfold(target, circle if animate else None, show)

    def close_table(self, target):
        """Close a table that waits in the bubble (it is not shown first); the bubble follows."""
        self.items = [i for i in self._live() if i[0] is not target]
        if _alive(target):
            target.close()
        self._refresh()

    def close_all(self):
        for target, _title in self._live():
            self.close_table(target)

    def restore_all(self):
        first = True
        for target, _title in self._live():
            self.restore(target, animate=first)
            first = False

    # ------------------------------------------------------------ hold: the layers of this map
    def map_layers(self):
        """The vector layers of the current map (the active map tab; the whole project without Map Tabs)."""
        from qgis.core import QgsProject, QgsVectorLayer
        try:
            from .map_tabs import active_map_layer_ids
            ids = active_map_layer_ids()
        except Exception:
            ids = None
        layers = []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            layer = node.layer()
            if isinstance(layer, QgsVectorLayer) and (ids is None or layer.id() in ids):
                layers.append(layer)
        return layers

    def _show_layer_list(self):
        """Holding the bubble lists the layers of the map; one click opens that layer's attribute table."""
        from qgis.core import QgsIconUtils
        menu = QMenu(self.window)
        menu.setObjectName('kga_attribute_layers')
        menu.addSection('Open attribute table')
        layers = self.map_layers()
        for layer in layers:
            action = menu.addAction(QgsIconUtils.iconForLayer(layer), layer.name())
            action.triggered.connect(lambda _c=False, lyr=layer: self._open_table(lyr))
        if not layers:
            menu.addAction('No vector layers in this map').setEnabled(False)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self._menu_origin(menu.sizeHint()))

    def _open_table(self, layer):
        """Open *layer*'s table out of the bubble: a table waiting in it flows back out; a new one is
        opened and its picture flows from the bubble to its place."""
        name = layer.name()
        for target, title in self._live():
            if name in title:
                self.restore(target)
                return
        known = {id(d) for d in self._tables()}
        with suppress(RuntimeError):
            self.iface.showAttributeTable(layer)
        fresh = [d for d in self._tables() if id(d) not in known]
        if not fresh or not self.bubble.isVisible():
            return                              # already open: QGIS raised it
        target = self._target(fresh[-1])

        def show():
            target.show()
            target.raise_()
            if target.isWindow():
                target.activateWindow()
        self.motion.unfold(target, self.bubble.circle_global(), show)

    def _menu_origin(self, size):
        """Where the list goes: beside the bubble, towards the middle of the map, kept on the screen."""
        circle = self.bubble.circle_global()
        canvas = self.iface.mapCanvas()
        area = QRect(canvas.mapToGlobal(QPoint(0, 0)), canvas.size())
        x = circle.left() - size.width() - 8 if circle.center().x() > area.center().x() else circle.right() + 8
        y = circle.bottom() - size.height() if circle.center().y() > area.center().y() else circle.top()
        screen = QApplication.screenAt(QPoint(int(circle.center().x()), int(circle.center().y())))
        if screen is not None:
            bounds = screen.availableGeometry()
            x = max(bounds.left(), min(int(x), bounds.right() - size.width()))
            y = max(bounds.top(), min(int(y), bounds.bottom() - size.height()))
        return QPoint(int(x), int(y))

    def _clicked(self):
        live = self._live()
        if len(live) == 1:
            self.restore(live[0][0])
        elif live:
            menu = QMenu(self.window)
            self._fill_menu(menu)
            menu.exec(QCursor.pos())

    def _fill_menu(self, menu):
        live = self._live()
        for target, title in live:
            menu.addAction(title, lambda _c=False, t=target: self.restore(t))
        if len(live) > 1:
            menu.addSeparator()
            menu.addAction('Open All', self.restore_all)
        if live:
            menu.addSeparator()
            if len(live) == 1:
                menu.addAction('Close', lambda _c=False, t=live[0][0]: self.close_table(t))
            else:
                close = menu.addMenu('Close')
                for target, title in live:
                    close.addAction(title, lambda _c=False, t=target: self.close_table(t))
                menu.addAction('Close All', self.close_all)
        else:
            menu.addAction('No minimized attribute tables').setEnabled(False)

    # ------------------------------------------------------------ unload
    def unload(self):
        self._timer.stop()
        with suppress(Exception):
            self.group.settingsChanged.disconnect(self._group_changed)
        with suppress(Exception):
            self.window.removeEventFilter(self)
        with suppress(Exception):
            QApplication.instance().focusWindowChanged.disconnect(self._mark_dirty)
        self.motion.settle()
        for target, _title in self._live():     # tables still in the bubble come back
            with suppress(Exception):
                target.show()
        self.items = []
        self.motion.unload()
        self.group.unregister(ATTRIBUTE_KEY)
        if _alive(self.bubble):
            self.bubble.hide()
            self.bubble.deleteLater()
        self.bubble = None

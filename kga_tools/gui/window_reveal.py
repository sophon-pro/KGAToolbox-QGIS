# -*- coding: utf-8 -*-
"""Open a tool window out of the toolbar button that started it.

The same idea as a minimized window coming back on macOS, and the same two
animations as the floating bags: the window is photographed and the picture
either pours out of the button (Genie) or grows out of it (Classic); the real
window is shown under the last frame, so nothing is laid out while it moves.
The Settings choice "Open / minimize animation" picks between the two and the
bags' motion setting scales the speed. Closing plays it back.

How a window joins in: its class is decorated with `@revealed` (or `patch(cls)`
is called for a class that is not ours, such as the Processing dialog). That
wraps `setVisible` and `hideEvent`: when the window is about to be shown and a
KGA toolbar button was pressed a moment ago, the window is photographed while
still hidden and the animation plays from that button. With no recent toolbar
press (menu, panel, shortcut) nothing changes.
Windows written in C++ (Attribute Table, Layer Properties) cannot be wrapped
this way and keep QGIS's own behaviour.
"""

import sys
import time
from contextlib import suppress

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSizeF, Qt
from qgis.PyQt.QtWidgets import QToolButton, QWidget

from ..core import kga_settings as ks

try:
    from qgis.utils import iface
except ImportError:                         # pragma: no cover - head-less
    iface = None

_pending = None              # (QRectF in screen coordinates, time it was set)
_tracker = None              # the OriginTracker of the toolbar
_ghosts = {}                 # main window id -> {'genie': GenieWidget, 'fly': FlyOverlay}
_running = []                # live tweens, so they are not collected mid-flight
_patched = []                # (class, setVisible, hideEvent) as they were, for unload
_ORIGIN_TTL = 5.0            # seconds an explicitly set origin stays valid
_REVEAL_MAX = 2.0            # longest an opening animation can be pending (safety net)


def button_rect(widget):
    """Screen rectangle of a visible widget, or None."""
    try:
        if widget is None or not widget.isVisible():
            return None
        return QRectF(QPointF(widget.mapToGlobal(QPoint(0, 0))), QSizeF(widget.size()))
    except RuntimeError:                    # the widget was deleted
        return None


class OriginTracker(QObject):
    """Tells which toolbar button the user has just used.

    Tools are started through actions shared with the KGA menu, so the action
    cannot say where a click came from. This remembers the last mouse press on
    a watched toolbar button (and the closing of its drop-down) and when the KGA
    menu last closed: a window shown within `MAX_AGE` seconds of a press that
    came after the menu closed belongs to that button.
    """

    MAX_AGE = 3.0

    def __init__(self):
        super().__init__()
        # Marks the buttons this tracker follows. It is kept on the button
        # itself: an id() of the Python wrapper is not stable - findChildren
        # hands out temporary wrappers, their ids are reused once they are
        # freed, and a new button then looked "already watched" and was
        # skipped (8 of 20 KGA Toolbox buttons, none on the GeoAI toolbar).
        self._token = '{0:x}-{1}'.format(id(self), time.monotonic())
        self._press = (None, 0.0)
        self._menu_hidden = 0.0

    def watch(self, button):
        """Follow one toolbar button (and its drop-down, if it has one)."""
        if button.property('kgaRevealTracker') == self._token:
            return
        button.setProperty('kgaRevealTracker', self._token)
        button.installEventFilter(self)
        menu = button.menu()
        if menu is not None:
            menu.aboutToHide.connect(lambda b=button: self._touch(b))

    def watch_toolbar(self, toolbar):
        for button in toolbar.findChildren(QToolButton):
            self.watch(button)

    def _touch(self, button):
        self._press = (button, time.monotonic())

    def menu_hidden(self, *_args):
        self._menu_hidden = time.monotonic()

    def clear(self):
        self._press = (None, 0.0)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self._touch(obj)
        return False

    def current(self):
        """Screen rectangle of the toolbar button just used, or None."""
        button, when = self._press
        if button is None or when <= self._menu_hidden or time.monotonic() - when > self.MAX_AGE:
            return None
        return button_rect(button)


def set_tracker(tracker):
    global _tracker
    _tracker = tracker


def no_system_transition(window):
    """Stop Windows from adding its own zoom/fade when the window is shown or hidden.

    Without this the real window pops in with the system animation on top of
    the ghost's, which looks like a second window appearing.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(window.winId())), 3,          # DWMWA_TRANSITIONS_FORCEDISABLED
            ctypes.byref(value), ctypes.sizeof(value))
    except Exception:                       # cosmetic only
        return


def set_origin(rect):
    global _pending
    _pending = (QRectF(rect), time.monotonic()) if rect is not None else None


def clear_origin():
    set_origin(None)


def take_origin():
    """The origin for the window being shown (once), or None."""
    global _pending
    pending, _pending = _pending, None
    if pending is not None and time.monotonic() - pending[1] <= _ORIGIN_TTL:
        if _tracker is not None:
            _tracker.clear()
        return pending[0]
    if _tracker is None:
        return None
    rect = _tracker.current()
    _tracker.clear()
    return rect


def _speed():
    motion = str(QgsSettings().value('KGA/bags/motion', 'normal') or 'normal')
    from .bag_group import SPEED
    return SPEED.get(motion, 1.0)


def _main():
    return iface.mainWindow() if iface is not None else None


def _wants_reveal(window):
    main = _main()
    return (ks.window_reveal() and _speed() > 0 and main is not None
            and main.isVisible() and not main.isMinimized() and window.isWindow()
            and not window.isVisible() and window is not main)


# --------------------------------------------------------------------- patching

def _base(original, fallback, window, *args):
    """Call the class's own setVisible/hideEvent; the plain QWidget one if that refuses."""
    try:
        original(window, *args)
    except TypeError:                       # sip's "must have type QDialog" on an odd wrapper
        _log_problem(window)
        fallback(window, *args)


def _log_problem(window):
    import traceback
    from qgis.core import Qgis, QgsMessageLog
    QgsMessageLog.logMessage(
        'Window animation skipped for {}.{}: {}'.format(
            type(window).__module__, type(window).__name__, traceback.format_exc()),
        'KGA Toolbox', Qgis.MessageLevel.Warning)


def patch(cls, preview=False):
    """Make windows of *cls* open out of the toolbar button and shrink back.

    preview=True is for windows whose layout is only right once they have been
    shown (the Processing dialog): they are first shown invisibly
    (WA_DontShowOnScreen) to settle, photographed, and hidden again.
    """
    if cls.__dict__.get('_kga_reveal_patched'):
        return cls
    orig_visible = cls.setVisible
    orig_hide = cls.hideEvent
    # Which of the two the class defined itself. A method that was only
    # inherited must be deleted again on unload, never assigned back: sip takes
    # a method put in a subclass for a Python override and calls it without
    # `self` ("first argument of unbound method must have type 'QDialog'").
    own = ('setVisible' in cls.__dict__, 'hideEvent' in cls.__dict__)

    def setVisible(self, visible):
        if visible:
            if getattr(self, '_kga_landing', False):
                _base(orig_visible, QWidget.setVisible, self, True)
                return
            # Processing opens a custom dialog with show() and then exec(), and
            # exec() calls show() again. While the opening animation is still
            # running that second call must not put the real window on screen
            # (it would pop up before the ghost lands) nor forget the origin
            # the window shrinks back to on close.
            if time.monotonic() - getattr(self, '_kga_revealing', 0.0) < _REVEAL_MAX:
                return
            self._kga_reveal_origin = None
            if _wants_reveal(self):
                origin = take_origin()
                try:
                    started = (origin is not None
                               and _reveal(self, _main(), origin, _speed(), preview))
                except Exception:                    # an animation problem never blocks a window
                    _log_problem(self)
                    started = False
                if started:
                    return                     # the animation shows the window when it ends
        _base(orig_visible, QWidget.setVisible, self, visible)

    def hideEvent(self, event):
        _base(orig_hide, QWidget.hideEvent, self, event)
        if not event.spontaneous() and getattr(self, '_kga_reveal_origin', None) is not None:
            try:
                play_close(self)
            except Exception:
                _log_problem(self)

    cls.setVisible = setVisible
    cls.hideEvent = hideEvent
    cls._kga_reveal_patched = True
    _patched.append((cls, orig_visible, orig_hide, own))
    return cls


def revealed(cls):
    """Class decorator for the plugin's own tool windows."""
    return patch(cls)


def revealed_preview(cls):
    """Like `revealed`, for windows that settle their size and layout only once shown."""
    return patch(cls, preview=True)


def patch_processing():
    """The Processing parameters dialog, which every KGA tool with parameters opens."""
    try:
        from processing.gui.AlgorithmDialog import AlgorithmDialog
    except Exception:                       # processing not available yet
        return
    patch(AlgorithmDialog, preview=True)


def _prepare(window, main):
    """Lay the hidden window out, place it and photograph it."""
    window.ensurePolished()
    if QWidget.layout(window) is not None:
        QWidget.layout(window).activate()
    size = QWidget.size(window).expandedTo(QWidget.minimumSizeHint(window)).expandedTo(QWidget.minimumSize(window))
    if not window.property('kgaRevealPlaced'):
        size = size.expandedTo(QWidget.sizeHint(window))
        frame = main.frameGeometry()
        top_left = QPoint(frame.center().x() - size.width() // 2,
                          frame.center().y() - size.height() // 2)
        window.setProperty('kgaRevealPlaced', True)
    else:
        top_left = QWidget.geometry(window).topLeft()
    rect = QRect(top_left, size)
    QWidget.setGeometry(window, rect)
    return rect, QWidget.grab(window)


def _preview(window):
    """Show the window without putting it on screen, photograph it, hide it again."""
    window._kga_landing = True
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        window.setVisible(True)
        QCoreApplication.sendPostedEvents(None, 0)
        QCoreApplication.processEvents()
        rect = QRect(QWidget.geometry(window))
        pix = QWidget.grab(window)
        window._kga_reveal_origin = None        # this hide is not a close
        window.setVisible(False)
    finally:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        window._kga_landing = False
    return rect, pix


def _reveal(window, main, origin, speed, preview=False):
    """Run the opening animation. False = nothing was started (the caller shows the window)."""
    try:
        # The window stays hidden while it is photographed: showing it, even
        # fully transparent, makes Windows paint a frame of it (the flash).
        # Its place is worked out here and set as its client geometry, so it
        # appears exactly there when the animation ends.
        no_system_transition(window)
        rect, pix = _preview(window) if preview else _prepare(window, main)
    except RuntimeError:
        return False
    if pix.isNull():
        return False

    _settle()
    window._kga_reveal_origin = QRectF(origin)      # where it shrinks back to on close
    window._kga_revealing = time.monotonic()        # repeated show() calls wait for the landing

    def land():
        with suppress(RuntimeError):
            window._kga_revealing = 0.0
        try:
            window._kga_landing = True
            QWidget.setGeometry(window, rect)
            window.setVisible(True)         # the window appears exactly under the last frame
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            return
        finally:
            with suppress(RuntimeError):
                window._kga_landing = False

    _play_open(main, pix, rect, origin, speed, land)
    return True


def _play_open(main, pix, rect, origin, speed, land):
    """Grow the picture *pix* from *origin* to *rect* (global), then call land()."""
    ghosts = _ghosts.setdefault(id(main), {})
    if ks.bag_animation() == ks.BAG_GENIE:
        from .symbol_bag.ui.motion import Tween
        from .symbol_bag.ui.widgets import GenieWidget
        g = ghosts.get('genie')
        if g is None:
            g = ghosts['genie'] = GenieWidget(main)
        g.setup(pix, rect, QRectF(origin))
        g.set_t(1.0)
        g.show_now()

        def end():
            land()
            g.hide_soon()
            _running[:] = [t for t in _running if t.running]

        _running.append(Tween(main, 460 * speed, lambda t: g.set_t(1.0 - t), end).start())
        return

    from .tool_bag.bubble import FlyOverlay
    fly = ghosts.get('fly')
    if fly is None:
        fly = ghosts['fly'] = FlyOverlay(main)
    fly.play(pix, origin, QRectF(rect), True, 1.0, land)
    fly._anim.setDuration(int(360 * speed))


def _settle():
    for t in list(_running):
        t.stop(finish=True)
    _running.clear()
    for pair in _ghosts.values():
        fly = pair.get('fly')
        if fly is not None:
            fly.finish()


def play_close(window):
    """Shrink the picture of a window that has just been hidden into its button.

    Called from the window's hideEvent, so the window is already gone and the
    ghost (placed exactly over it) takes over without a visible change.
    """
    origin = getattr(window, '_kga_reveal_origin', None)
    main = _main()
    speed = _speed()
    if (origin is None or main is None or speed <= 0 or not ks.window_reveal()
            or not main.isVisible() or main.isMinimized()):
        return
    try:
        pix = QWidget.grab(window)
        rect = QRect(QWidget.geometry(window))
    except RuntimeError:
        return
    if pix.isNull():
        return
    _settle()
    _play_close(main, pix, rect, origin, speed)


def _play_close(main, pix, rect, origin, speed):
    """Shrink the picture *pix* (global *rect*) into *origin*."""
    ghosts = _ghosts.setdefault(id(main), {})
    if ks.bag_animation() == ks.BAG_GENIE:
        from .symbol_bag.ui.motion import Tween
        from .symbol_bag.ui.widgets import GenieWidget
        g = ghosts.get('genie')
        if g is None:
            g = ghosts['genie'] = GenieWidget(main)
        g.setup(pix, rect, QRectF(origin))
        g.set_t(0.0)
        g.show_now()

        def end():
            g.hide()
            g.pixmap = None
            _running[:] = [t for t in _running if t.running]

        _running.append(Tween(main, 520 * speed, g.set_t, end).start())
        return
    from .tool_bag.bubble import FlyOverlay
    fly = ghosts.get('fly')
    if fly is None:
        fly = ghosts['fly'] = FlyOverlay(main)
    fly.play(pix, QRectF(origin), QRectF(rect), False, 1.0, None)
    fly._anim.setDuration(int(240 * speed))


# ------------------------------------------------------- widgets that are not windows

def widget_reveal_possible():
    """Animations are on and the main window can host them."""
    main = _main()
    return (ks.window_reveal() and _speed() > 0 and main is not None
            and main.isVisible() and not main.isMinimized())


def reveal_widget(widget, origin, show):
    """Open a child widget (a bar over the map) out of *origin* (global QRectF).

    *show* makes the real widget visible; it is called with the widget laid out and
    visible for the photograph, then the widget is hidden again until the
    animation lands and show() is called a second time. Returns False when
    nothing was animated (the widget is then left shown).
    """
    if origin is None or not widget_reveal_possible():
        show()
        return False
    main = _main()
    try:
        show()
        widget.ensurePolished()
        pix = widget.grab()
        rect = QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())
        widget.hide()
        if pix.isNull():
            show()
            return False
        _settle()
        _play_open(main, pix, rect, QRectF(origin), _speed(), show)
    except RuntimeError:
        return False
    return True


def close_widget(widget, origin):
    """Shrink a child widget that is about to be hidden into *origin*; call before hide()."""
    if origin is None or not widget_reveal_possible() or not widget.isVisible():
        return
    try:
        pix = widget.grab()
        rect = QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())
    except RuntimeError:
        return
    if pix.isNull():
        return
    _settle()
    _play_close(_main(), pix, rect, QRectF(origin), _speed())


def unload():
    """Drop the helper windows and undo the patches (plugin unload)."""
    clear_origin()
    set_tracker(None)
    for cls, orig_visible, orig_hide, (own_visible, own_hide) in reversed(_patched):
        for name, original, own in (('setVisible', orig_visible, own_visible),
                                    ('hideEvent', orig_hide, own_hide)):
            with suppress(Exception):
                if own:
                    setattr(cls, name, original)
                else:
                    delattr(cls, name)      # back to the inherited one
        with suppress(Exception):
            cls._kga_reveal_patched = False
    _patched.clear()
    for pair in list(_ghosts.values()):
        for w in pair.values():
            try:
                w.hide()
                w.deleteLater()
            except RuntimeError:
                pass
    _ghosts.clear()
    for t in list(_running):
        try:
            t.stop()
        except RuntimeError:
            pass
    _running.clear()

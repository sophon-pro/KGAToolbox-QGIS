# -*- coding: utf-8 -*-
"""Symbol Bag – motion engine.

Small, dependency-free helpers for physics-style motion: damped springs,
easing curves, curved (bezier) paths and a tween object driven by Qt's
animation clock. Everything is computed per frame from a normalised time
0..1, so animations stay perfectly in sync with the duration and cost
almost nothing.
"""

import gc
import math

from qgis.PyQt.QtCore import QObject, QPointF, QTimer, QVariantAnimation

# Speed presets (duration multipliers) offered in Bag Style
SPEEDS = [("off", "Off", 0.0), ("snappy", "Snappy", 0.75), ("normal", "Normal", 1.0), ("relaxed", "Relaxed", 1.35)]
SPEED_FACTOR = {k: f for k, _l, f in SPEEDS}


# ----------------------------------------------------------------------
# curves
# ----------------------------------------------------------------------
def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def spring(t, damping=0.62, settle=6.0):
    """Damped spring from 0 to 1 in normalised time t (0..1).

    damping < 1 overshoots: 0.62 ≈ 8 % overshoot (smooth, professional),
    0.45 ≈ 20 % (playful pop). The tail is blended so the value lands on
    exactly 1 at t = 1 with no visible snap on the last frame.
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    z = max(0.05, min(0.99, damping))
    w = settle / z                       # natural frequency so that e^(-z*w) = e^-settle
    wd = w * math.sqrt(1.0 - z * z)
    decay = math.exp(-z * w * t)
    x = 1.0 - decay * (math.cos(wd * t) + (z * w / wd) * math.sin(wd * t))
    return x + (1.0 - x) * t ** 6


def ease_out(t, power=3):
    t = clamp(t)
    return 1.0 - (1.0 - t) ** power


def ease_in(t, power=3):
    t = clamp(t)
    return t ** power


def ease_in_out(t):
    t = clamp(t)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def segment(t, start, end):
    """Re-map t so that it runs 0..1 between start and end (clamped)."""
    if end <= start:
        return 1.0 if t >= end else 0.0
    return clamp((t - start) / (end - start))


def pulse(t):
    """Press-and-release bump: 0.93 → ~1.03 → 1 (scale multiplier)."""
    t = clamp(t)
    return 1.0 - 0.07 * math.exp(-6.0 * t) * math.cos(2.0 * math.pi * 1.6 * t)


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------
def arc_control(p0, p1, bend=0.28):
    """Control point that bends the straight line p0→p1 into a gentle arc
    that always bulges upward (like something tossed)."""
    mid = QPointF((p0.x() + p1.x()) / 2.0, (p0.y() + p1.y()) / 2.0)
    dx, dy = p1.x() - p0.x(), p1.y() - p0.y()
    dist = math.hypot(dx, dy)
    if dist < 1:
        return mid
    nx, ny = -dy / dist, dx / dist             # perpendicular
    if ny > 0:                                  # make it bulge upward
        nx, ny = -nx, -ny
    return QPointF(mid.x() + nx * dist * bend, mid.y() + ny * dist * bend)


def bezier(p0, c, p1, s):
    """Quadratic bezier. s may exceed 1 (spring overshoot) – it extrapolates
    along the end tangent, which looks like natural momentum."""
    u = 1.0 - s
    return QPointF(u * u * p0.x() + 2 * u * s * c.x() + s * s * p1.x(),
                   u * u * p0.y() + 2 * u * s * c.y() + s * s * p1.y())


def bezier_tangent(p0, c, p1, s):
    return QPointF(2 * (1 - s) * (c.x() - p0.x()) + 2 * s * (p1.x() - c.x()),
                   2 * (1 - s) * (c.y() - p0.y()) + 2 * s * (p1.y() - c.y()))


# ----------------------------------------------------------------------
# tween
# ----------------------------------------------------------------------
_gc = {"holds": 0, "was_enabled": False}


def _gc_hold():
    """Keep Python's garbage collector from running while something animates.

    Every frame allocates hundreds of small objects, which triggers collections; with QGIS's big
    Python heap a full one takes 40-80 ms and shows up as a stutter. Reference counted, so
    overlapping animations are fine; collection resumes when the last one is over."""
    if _gc["holds"] == 0:
        _gc["was_enabled"] = gc.isenabled()
        gc.disable()
    _gc["holds"] += 1


def _gc_release():
    _gc["holds"] = max(0, _gc["holds"] - 1)
    if _gc["holds"] == 0 and _gc["was_enabled"]:
        gc.enable()


class Tween(QObject):
    """Runs frame(t) with t going 0 → 1 over `duration` ms on Qt's animation
    clock (vsync-friendly, ~60 fps), then finished(). Cheap and cancellable."""

    def __init__(self, parent, duration, frame, finished=None):
        super().__init__(parent)
        self._held = False
        self._frame = frame
        self._finished = finished
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(max(1, int(duration)))
        self.anim.valueChanged.connect(self._on_value)
        self.anim.finished.connect(self._on_finished)
        self.running = False

    def _on_value(self, v):
        frame = self._frame
        if frame is None:
            return
        try:
            frame(float(v))
        except RuntimeError:          # a widget went away mid-animation
            self.stop()

    def _release(self):
        """A Tween is single-use. It is parented to a long-lived widget (often QGIS's main
        window), so without this every animation would stay behind as a child object forever
        and QGIS would slow down the longer it runs."""
        self._frame = None
        self._finished = None
        self._unhold()
        try:
            self.deleteLater()
        except RuntimeError:
            pass

    def _on_finished(self):
        self.running = False
        cb, self._finished = self._finished, None
        try:
            if cb is not None:
                try:
                    cb()
                except RuntimeError:
                    pass
        finally:
            self._release()

    def _unhold(self):
        if self._held:
            self._held = False
            _gc_release()

    def start(self):
        self.running = True
        self._held = True
        _gc_hold()
        # never leave the collector off, whatever happens to this object (a deleted parent, an error)
        QTimer.singleShot(self.anim.duration() + 1500, lambda tw=self: tw._unhold())   # not bound to the QObject
        try:
            self._frame(0.0)
            self.anim.start()
        except Exception:
            self._unhold()
            raise
        return self

    def stop(self, finish=False):
        """Stop. With finish=True jump to the end state and run finished()."""
        if not self.running:
            return
        self.running = False
        self.anim.stop()
        try:
            if finish:
                try:
                    self._frame(1.0)
                except RuntimeError:
                    pass
                cb, self._finished = self._finished, None
                if cb is not None:
                    try:
                        cb()
                    except RuntimeError:
                        pass
        finally:
            self._release()

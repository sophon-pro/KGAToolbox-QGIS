# -*- coding: utf-8 -*-
"""The translucent "working" panel, shared by the tools that block the GUI.

Lifted out of the Domain & Schema Manager, which had it first, so that the Add
Open Data window can use the same one rather than growing a second. Two tools
that both stop and think at the user should look identical while they do it.

Enum members are written in their scoped form because the unscoped spelling is
gone in PyQt6, which QGIS 4 moves to.
"""

from qgis.PyQt.QtCore import (QCoreApplication, QElapsedTimer, QEvent,
                              QEventLoop, Qt, QTimer)
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtWidgets import (QApplication, QFrame, QLabel, QProgressBar,
                                 QVBoxLayout, QWidget)


class BusyOverlay(QWidget):
    """A translucent "working" panel laid over the widget it is given.

    The work these tools do runs on the GUI thread. A worker thread would not
    help the Domain Manager, where the time goes into creating one combo box
    per field and widgets can only be made on the GUI thread; and it would not
    help the Add Open Data window, whose network calls block on an event loop
    of their own. So the panel is pumped by hand instead - `step` and `message`
    run the event loop just long enough to repaint, with user input excluded so
    a second click cannot re-enter an operation that is still running.

    Calls nest. An inner phase started from inside an outer one leaves the
    panel up until the outermost caller finishes, so the window never flickers
    between phases of a single operation.
    """

    #: Milliseconds between repaints. Pumping the event loop more often than
    #: this costs more than the progress it reports.
    _REPAINT_MS = 60

    def __init__(self, parent):
        super().__init__(parent)
        self._depth = 0
        self._shown = False
        self._clock = QElapsedTimer()
        self._clock.start()
        parent.installEventFilter(self)

        # Lets `begin(delay_ms=...)` hold the panel back, so an operation that
        # turns out to be instant never flashes it. Single-shot: `end` stops it
        # if the work finished first.
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._show_now)

        outer = QVBoxLayout(self)

        panel = QFrame(self)
        # An object-name selector, not `QFrame` - QLabel is a QFrame too, and
        # a plain type selector would draw a border around the caption.
        panel.setObjectName('kgaBusyPanel')
        panel.setStyleSheet(
            '#kgaBusyPanel { background: palette(window); '
            'border: 1px solid palette(mid); border-radius: 4px; }')
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(10)

        self._label = QLabel('', panel)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(self._label)

        self._bar = QProgressBar(panel)
        self._bar.setFixedWidth(280)
        self._bar.setTextVisible(False)
        inner.addWidget(self._bar)

        # Aligned on the *widget*, not the layout: a layout alignment still
        # lets the panel stretch to the full width of the dialog.
        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignCenter)
        self.hide()

    # -- lifecycle ---------------------------------------------------------

    def begin(self, text, delay_ms=0):
        """Raise the panel. Always pair with `end()` in a `finally`.

        `delay_ms` holds it back for that long first. Pass it where the work
        is usually instant but occasionally slow - a cached answer that is
        sometimes a network round trip - so the common case stays still and
        only the slow case explains itself.
        """
        self._depth += 1
        self._label.setText(text)
        if self._depth == 1 and not self._shown:
            if delay_ms > 0:
                self._show_timer.start(delay_ms)
            else:
                self._show_now()
        if self._shown:
            self.message(text, force=True)

    def end(self):
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            self._show_timer.stop()
            if self._shown:
                self._shown = False
                self.hide()
                QApplication.restoreOverrideCursor()

    def _show_now(self):
        if self._shown or self._depth == 0:
            return
        self._shown = True
        self.setGeometry(self.parentWidget().rect())
        # 0..0 is Qt's marching indeterminate bar, for the work whose length
        # is not known until it finishes.
        self._bar.setRange(0, 0)
        self.show()
        self.raise_()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._pump(True)

    # -- progress ----------------------------------------------------------

    def message(self, text, force=False):
        """Name the phase, leaving the bar as it is."""
        self._label.setText(text)
        if self._shown:
            self._pump(force)

    def step(self, done, total, text=None):
        """Report measured progress, turning the bar determinate."""
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
        if text is not None:
            self._label.setText(text)
        if self._shown:
            self._pump(False)

    def _pump(self, force):
        if not force and self._clock.elapsed() < self._REPAINT_MS:
            return
        self._clock.restart()
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    # -- painting ----------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

    def paintEvent(self, event):
        # A wash of black rather than of palette(window): the window colour
        # over a window-coloured page is no dimming at all, and black at this
        # alpha reads as "behind glass" in a light theme and a dark one alike.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 70))

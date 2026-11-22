# -*- coding: utf-8 -*-
"""A modeless tool window for a Processing algorithm: source box, tabs, Log tab.

The look of Generate Contour for ROI: a box at the top for the input, a tab
area whose height follows the page shown, a Log tab that takes over while the
algorithm runs on a worker thread (``QgsProcessingAlgRunnerTask``), and the
Help / Run / Close row. The window opens in place of the Toolbox form
(``createCustomParametersWidget``); the algorithm and its parameters are
unchanged, so scripts, the modeller and batch still work.

A tool subclasses this and fills in:

    TITLE, ALG_NAME, RUN_TEXT      names and the run button's text
    algorithm_class()              the QgsProcessingAlgorithm class
    init_state()                   plain attributes, before any widget exists
    build_top()                    the input box (or None)
    build_sections()               [(group box, tab name, tooltip), ...]
    parameters()                   the dict handed to the algorithm
    problem()                      None = fine, text = warn, False = cancelled
    done_ok(results)               after a successful run (optional)

It is opened with the toolbar animation: decorating the base class is enough,
the subclasses inherit the wrapped ``setVisible`` (decorating each would wrap
it twice).

Enum members are written scoped because the unscoped spelling is gone in PyQt6.
"""

from contextlib import suppress

from qgis.core import (Qgis, QgsApplication, QgsProcessingAlgRunnerTask,
                       QgsProcessingAlgorithm, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject)
from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from ..core.compat import scoped
from .window_reveal import revealed_preview
from ..branding import open_docs

BLUE = '#174592'
ORANGE = '#FC663A'
RED = '#B3261E'
GREEN = '#1E7B34'

STYLE = """
QGroupBox { font-weight: bold; }
QGroupBox::title { color: %(blue)s; }
QLabel#kgaNote { font-weight: normal; font-style: italic; }
QLabel#kgaStatus { font-weight: normal; }
QPushButton#kgaPrimary { background: %(blue)s; color: white; font-weight: bold;
    padding: 5px 18px; border: none; border-radius: 3px; }
QPushButton#kgaPrimary:disabled { background: palette(mid); color: palette(light); }
QPushButton#kgaPrimary[running="true"] { background: %(orange)s; }
QProgressBar { text-align: center; }
QProgressBar::chunk { background: %(orange)s; }
""" % {'blue': BLUE, 'orange': ORANGE}


def tr(text):
    return QgsApplication.translate('KgaRunDialog', text)


def runs_on_gui_thread(alg):
    """True for an algorithm that must not be run on a worker thread."""
    try:
        flag = Qgis.ProcessingAlgorithmFlag.NoThreading
    except AttributeError:                      # QGIS < 3.36
        flag = scoped(QgsProcessingAlgorithm, 'Flag', 'FlagNoThreading')
    return bool(alg.flags() & flag)


def geometry_kind(wkb_type):
    """'point', 'line', 'polygon' or 'none' for a WKB type, on any QGIS version."""
    from qgis.core import QgsWkbTypes
    kind = QgsWkbTypes.geometryType(wkb_type)
    try:
        number = int(kind)
    except TypeError:
        number = kind.value
    return {0: 'point', 1: 'line', 2: 'polygon'}.get(number, 'none')


def note(text='', parent=None):
    label = QLabel(text, parent)
    label.setObjectName('kgaNote')
    label.setWordWrap(True)
    return label


class RunFeedback(QgsProcessingFeedback):
    """The run's feedback: everything goes to the window's log."""

    logged = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.error = None
        self.pump = False       # True when the algorithm runs on the GUI thread

    def _pump(self):
        if self.pump:
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def setProgress(self, progress):
        super().setProgress(progress)
        self._pump()

    def pushInfo(self, info):
        self.logged.emit(str(info))
        self._pump()

    def pushWarning(self, warning):
        self.logged.emit('WARNING: ' + str(warning))
        self._pump()

    def pushDebugInfo(self, info):
        pass

    def pushCommandInfo(self, info):
        pass

    def pushConsoleInfo(self, info):
        pass

    def reportError(self, error, fatalError=False):
        if fatalError:
            self.error = str(error)
        self.logged.emit('ERROR: ' + str(error))


@revealed_preview
class TabbedRunDialog(QDialog):

    TITLE = ''
    ALG_NAME = ''
    RUN_TEXT = 'Run'
    MIN_WIDTH = 560

    # ---------------------------------------------------------------- hooks

    def algorithm_class(self):
        raise NotImplementedError

    def init_state(self):
        pass

    def build_top(self):
        return None

    def build_sections(self):
        return []

    def parameters(self):
        return {}

    def problem(self):
        return None

    def done_ok(self, results):
        pass

    def after_build(self):
        pass

    def run_text(self):
        """The run button's text when idle (a tool may change it with its options)."""
        return self.RUN_TEXT

    def refresh_run_text(self):
        if not self._running() and self.go_btn.isEnabled():
            self.go_btn.setText(tr(self.run_text()))

    # ---------------------------------------------------------------- build

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr(self.TITLE))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(self.MIN_WIDTH)

        self._torn_down = False
        self._ignore_close = False
        self._closing = False
        self._task = None
        self._alg = None
        self._feedback = None
        self._context = None
        self.init_state()

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        top = self.build_top()
        if top is not None:
            root.addWidget(top)
        root.addWidget(self._make_tabs())
        root.addStretch(1)
        root.addLayout(self._make_buttons())
        self.after_build()

    def _make_tabs(self):
        """One tab per section; the tab area is as tall as the page shown."""
        self.tabs = QTabWidget(self)
        sections = list(self.build_sections()) + [
            (self._build_log(), tr('Log'), tr('Progress and messages from the run'))]
        self._pages = []
        for box, name, tip in sections:
            box.setFlat(True)
            box.setTitle('')
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(2, 6, 2, 2)
            lay.addWidget(box)
            lay.addStretch(1)
            self._pages.append(page)
            self.tabs.setTabToolTip(self.tabs.addTab(page, name), tip)
        self.tabs.currentChanged.connect(self._fit_tab)
        self._fit_tab()
        return self.tabs

    def _fit_tab(self, *_args):
        """Size the tab area to the page shown, not the tallest page."""
        current = self.tabs.currentWidget()
        for page in self._pages:
            page.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred if page is current
                               else QSizePolicy.Policy.Ignored)
        self.tabs.updateGeometry()
        if self.isVisible():
            self.later_resize()

    def _build_log(self):
        box = QGroupBox(self)
        lay = QVBoxLayout(box)
        self.bar = QProgressBar(box)
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(16)
        self.log = QPlainTextEdit(box)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(170)
        lay.addWidget(self.bar)
        lay.addWidget(self.log)
        return box

    def _make_buttons(self):
        row = QHBoxLayout()
        self.help_btn = QPushButton(tr('Help'), self)
        self.help_btn.clicked.connect(lambda: open_docs(self.ALG_NAME))
        self.go_btn = QPushButton(tr(self.run_text()), self)
        self.go_btn.setObjectName('kgaPrimary')
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._go_or_stop)
        self.close_btn = QPushButton(tr('Close'), self)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.help_btn)
        row.addStretch(1)
        row.addWidget(self.go_btn)
        row.addWidget(self.close_btn)
        return row

    def card(self, title=''):
        return QGroupBox(title, self)

    # Processing drives a custom parameters dialog like its own: setParameters(),
    # show(), exec(), results(), then close(). This one is modeless, so exec()
    # just shows it, and the close() that follows straight away is ignored.
    def exec(self):
        self._ignore_close = True
        QTimer.singleShot(0, self._allow_close)
        self.show()
        self.raise_()
        self.activateWindow()
        return 0

    exec_ = exec

    def _allow_close(self):
        self._ignore_close = False

    def setParameters(self, parameters):
        """Pre-fill from a Processing parameter dict (tools override)."""

    def results(self):
        return {}

    # ---------------------------------------------------------------- sizing

    def showEvent(self, event):
        super().showEvent(event)
        self.later_resize()

    def later_resize(self):
        # After the layout has settled, so the new size hint is the real one.
        QTimer.singleShot(0, self._refit)

    def _refit(self):
        if self._torn_down:
            return
        self.layout().invalidate()
        self.layout().activate()
        # Keep the width the user chose; take the height the content needs.
        self.resize(self.width(), self.layout().totalMinimumSize().height()
                    if self.isMaximized() else self.layout().totalSizeHint().height())

    # ------------------------------------------------------------------ run

    def locked_widgets(self):
        """Widgets switched off while a run is going."""
        return []

    def warn(self, text):
        QMessageBox.warning(self, tr(self.TITLE), text)

    def _running(self):
        return self._task is not None

    def _go_or_stop(self):
        if self._running():
            self.go_btn.setEnabled(False)
            self.bar.setFormat(tr('Stopping...') + '  %p%')
            if self._feedback is not None:
                self._feedback.cancel()
            self._task.cancel()
            return
        self._start()

    def _start(self):
        issue = self.problem()
        if issue is False:
            return
        if issue:
            self.warn(issue)
            return

        alg = self.algorithm_class()().create()
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        params = self.parameters()
        ok, message = alg.checkParameterValues(params, context)
        if not ok:
            self.warn(message)
            return

        self._alg, self._context = alg, context
        self._feedback = RunFeedback()
        self._feedback.logged.connect(self.log.appendPlainText)
        self._feedback.progressChanged.connect(lambda v: self.bar.setValue(int(v)))

        self.log.clear()
        self.tabs.setCurrentIndex(self.tabs.count() - 1)    # the Log tab shows progress
        self.bar.setFormat('%p%')
        self.bar.setValue(0)
        if runs_on_gui_thread(alg):
            self._run_here(alg, params, context)
            return
        self._task = QgsProcessingAlgRunnerTask(alg, params, context, self._feedback)
        self._task.executed.connect(self._on_finished)
        self._set_running(True)
        QgsApplication.taskManager().addTask(self._task)

    def _run_here(self, alg, params, context):
        """Run on the GUI thread (the algorithm touches project layers)."""
        feedback = self._feedback
        feedback.pump = True
        self._set_running(True)
        self.go_btn.setEnabled(False)               # no Stop here: the window is busy
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results, ok = alg.run(params, context, feedback)
        except Exception as exc:                    # noqa: BLE001 - shown to the user
            feedback.reportError(str(exc), True)
            results, ok = {}, False
        finally:
            QApplication.restoreOverrideCursor()
            feedback.pump = False
        self._set_running(False)
        self._finish(ok, results)

    def _set_running(self, running):
        self.go_btn.setText(tr('Stop') if running else tr(self.run_text()))
        self.go_btn.setProperty('running', running)
        self.go_btn.style().unpolish(self.go_btn)
        self.go_btn.style().polish(self.go_btn)
        self.go_btn.setEnabled(True)
        for w in self.locked_widgets():
            w.setEnabled(not running)

    def _on_finished(self, ok, results):
        task, self._task = self._task, None
        if task is not None:
            task.deleteLater()
        if self._closing or self._torn_down:
            self._teardown()
            return
        self._set_running(False)
        self._finish(ok, results)

    def _finish(self, ok, results):
        feedback, self._feedback = self._feedback, None
        if feedback is not None and feedback.isCanceled():
            self.log.appendPlainText(tr('Stopped.'))
            self.bar.setValue(0)
            return
        if not ok or not results:
            self.warn((feedback.error if feedback is not None else None)
                      or tr('The run failed. See the log.'))
            return
        self.bar.setValue(100)
        self._load_queued(feedback)
        self.done_ok(results)

    def _load_queued(self, feedback):
        """Add the layers the algorithm queued, with their styles and labels."""
        context = self._context
        if context is None or not context.layersToLoadOnCompletion():
            return
        self.rename_queued(context)
        try:
            from processing.gui.Postprocessing import handleAlgorithmResults
            handleAlgorithmResults(self._alg, context, feedback, False)
        except Exception as exc:                    # noqa: BLE001 - shown to the user
            self.log.appendPlainText(tr('Could not add the layers: {0}').format(exc))

    def rename_queued(self, context):
        """Hook: set `.name` on the queued layers' details."""

    # ---------------------------------------------------------------- closing

    def closeEvent(self, event):
        if self._ignore_close:
            event.ignore()
            return
        if self._running():
            self._confirm_stop()
            event.ignore()                  # torn down when the task ends
            return
        self._teardown()
        super().closeEvent(event)

    def done(self, result):
        if self._ignore_close:
            return
        if self._running():
            self._confirm_stop()
            return
        self._teardown()
        super().done(result)

    def _confirm_stop(self):
        answer = QMessageBox.question(
            self, tr(self.TITLE), tr('Stop the run and close?'))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._closing = True
        self.hide()
        if self._feedback is not None:
            self._feedback.cancel()
        if self._task is not None:
            self._task.cancel()
        return True

    def shutdown(self):
        """Plugin unload: stop any job cleanly, then go away."""
        self._closing = True
        task = self._task
        if task is not None:
            if self._feedback is not None:
                self._feedback.cancel()
            task.cancel()
            with suppress(Exception):
                task.waitForFinished(15000)
        self._task = None
        self._teardown()

    def _teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self.deleteLater()

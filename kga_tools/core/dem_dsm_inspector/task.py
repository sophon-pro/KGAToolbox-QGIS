"""
task.py - Runs the diagnostic in a QGIS background thread (QgsTask), so the
interface stays responsive and the run can be cancelled.
"""

import traceback

from qgis.core import QgsTask

from . import pipeline
from .engine import Cancelled


class InspectTask(QgsTask):
    """Background analysis.  `on_done(ok, result, error)` runs on the main thread."""

    def __init__(self, description, kwargs, on_done):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.kwargs = kwargs
        self.on_done = on_done
        self.result = None
        self.error = None
        self.message = "Starting"

    # Called by pipeline/engine from the worker thread
    def _progress(self, pct, msg):
        self.message = msg
        self.setProgress(float(pct))

    def run(self):
        try:
            self.result = pipeline.run(progress=self._progress,
                                       is_cancelled=self.isCanceled, **self.kwargs)
            return True
        except Cancelled:
            self.error = "Cancelled by user."
            return False
        except Exception as exc:  # report everything back to the UI
            self.error = "{}\n\n{}".format(exc, traceback.format_exc())
            return False

    def finished(self, ok):
        if self.isCanceled() and not self.error:
            self.error = "Cancelled by user."
        self.on_done(ok, self.result, self.error)

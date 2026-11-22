# -*- coding: utf-8 -*-
"""
Layer Path Manager - audit log.

Two streams are kept:

* **Audit entries** - one structured record per layer operation (what changed,
  from/to, validity before/after, result, diagnostics).  These are exported
  to CSV/TXT/JSON.
* **Activity messages** - human readable progress messages for the Activity
  panel.  They are also forwarded to the QGIS "Log Messages" panel through the
  Python ``logging`` bridge.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .utils import logger

RESULT_SUCCESS = "Success"
RESULT_FAILED = "Failed"
RESULT_SKIPPED = "Skipped"


@dataclass
class AuditEntry:
    timestamp: str
    operation: str
    layer_name: str
    layer_id: str
    original_source: str
    new_source: str
    previous_status: str
    updated_status: str
    result: str
    message: str = ""
    operation_id: str = ""

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ActivityMessage:
    timestamp: datetime
    level: str      # info / success / warning / error
    text: str
    details: str = ""


class AuditLogger(QObject):
    entryAdded = pyqtSignal(object)      # AuditEntry
    messageAdded = pyqtSignal(object)    # ActivityMessage
    cleared = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: List[AuditEntry] = []
        self.messages: List[ActivityMessage] = []
        self.scan_stats: Dict[str, int] = {}

    # ---------------- activity ----------------
    def _msg(self, level, text, details=""):
        m = ActivityMessage(datetime.now(), level, text, details)
        self.messages.append(m)
        log_line = text + (f" | {details}" if details else "")
        if level == "error":
            logger.error(log_line)
        elif level == "warning":
            logger.warning(log_line)
        else:
            logger.info(log_line)
        self.messageAdded.emit(m)
        return m

    def info(self, text, details=""):
        return self._msg("info", text, details)

    def success(self, text, details=""):
        return self._msg("success", text, details)

    def warning(self, text, details=""):
        return self._msg("warning", text, details)

    def error(self, text, details=""):
        return self._msg("error", text, details)

    # ---------------- audit ----------------
    def record(self, operation, layer_name, layer_id, original_source, new_source,
               previous_status, updated_status, result, message="", operation_id=""):
        e = AuditEntry(datetime.now().isoformat(timespec="seconds"), operation, layer_name,
                       layer_id, original_source, new_source, previous_status, updated_status,
                       result, message, operation_id)
        self.entries.append(e)
        self.entryAdded.emit(e)
        return e

    def record_results(self, results, operation_id=""):
        """Record a list of ``path_repair.UpdateResult`` objects."""
        for r in results:
            self.record(r.operation, r.layer_name, r.layer_id, r.old_source, r.new_source,
                        "Valid" if r.old_valid else "Broken",
                        "Valid" if r.new_valid else "Broken", r.result, r.message, operation_id)

    def counts(self) -> Dict[str, int]:
        c = {RESULT_SUCCESS: 0, RESULT_FAILED: 0, RESULT_SKIPPED: 0}
        for e in self.entries:
            c[e.result] = c.get(e.result, 0) + 1
        return c

    def repaired_layer_ids(self) -> set:
        """Layers that went from Broken to Valid at least once this session."""
        return {e.layer_id for e in self.entries
                if e.result == RESULT_SUCCESS and e.previous_status == "Broken"
                and e.updated_status == "Valid" and e.operation != "Restore"}

    def clear_messages(self):
        self.messages.clear()
        self.cleared.emit()

    def clear_all(self):
        self.entries.clear()
        self.messages.clear()
        self.cleared.emit()

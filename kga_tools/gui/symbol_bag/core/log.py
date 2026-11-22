# -*- coding: utf-8 -*-
"""Logging helpers – everything goes to the 'Symbol Bag' tab of the QGIS log panel."""

TAG = "Symbol Bag"

try:
    from qgis.core import Qgis, QgsMessageLog

    def _log(msg, level):
        QgsMessageLog.logMessage(str(msg), TAG, level)

    def log_info(msg):
        _log(msg, Qgis.MessageLevel.Info)

    def log_warning(msg):
        _log(msg, Qgis.MessageLevel.Warning)

    def log_error(msg):
        _log(msg, Qgis.MessageLevel.Critical)

except ImportError:  # pragma: no cover - allows unit tests of storage without QGIS
    def log_info(msg):
        print("[Symbol Bag] " + str(msg))

    log_warning = log_error = log_info

# -*- coding: utf-8 -*-
"""A column drop-down for Processing parameters that follows a CSV file input.

`QgsProcessingParameterField` only works against a layer, not a plain file
path, so the CSV tools use a string parameter and swap its widget for this one.
It lists the header of the CSV chosen in the sibling file parameter, and still
accepts a typed name or column number. The stored value stays a plain string,
so scripts, the Python console and the modeller are unaffected.
"""

import csv

from qgis.PyQt.QtWidgets import QComboBox

# Header names that identify each coordinate column (lower case).
COLUMN_ALIASES = {
    "X": ("easting", "east", "x", "lon", "long", "longitude"),
    "Y": ("northing", "north", "y", "lat", "latitude"),
    "Z": ("elevation", "elev", "z", "height", "alt", "altitude", "rl"),
}


def read_csv_headers(path):
    """Header names of the CSV at `path`, or [] if it cannot be read."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            row = next(csv.reader(fh, dialect), [])
    except (OSError, UnicodeDecodeError):
        return []
    return [c.strip() for c in row if c.strip()]


def guess_column(headers, axis):
    """The header that looks like `axis` ('X', 'Y' or 'Z'), or ''."""
    lower = {h.lower(): h for h in headers}
    return next((lower[n] for n in COLUMN_ALIASES[axis] if n in lower), "")


_WRAPPER = None


def column_wrapper_class():
    """The wrapper class, built on first use.

    The `processing` plugin is not importable while this plugin's modules are
    first loaded, so the base class is resolved lazily from initAlgorithm.
    """
    global _WRAPPER
    if _WRAPPER is not None:
        return _WRAPPER

    from processing.gui.wrappers import WidgetWrapper

    class CsvColumnWidgetWrapper(WidgetWrapper):
        """Editable combo of the CSV's header names, tied to a file parameter."""

        def createWidget(self):
            self._combo = QComboBox()
            self._combo.setEditable(True)
            self._combo.lineEdit().setPlaceholderText("auto-detect")
            self._combo.addItem("")
            self._combo.currentTextChanged.connect(
                lambda *_: self.widgetValueHasChanged.emit(self))
            return self._combo

        def postInitialize(self, wrappers):
            """Follow the file parameter named in the metadata."""
            src = self.parameterDefinition().metadata().get("csv_param")
            for w in wrappers:
                if w.parameterDefinition().name() == src:
                    self._source = w
                    w.widgetValueHasChanged.connect(self._refresh)
                    self._refresh()
                    break

        def _csv_path(self):
            w = getattr(self, "_source", None)
            if w is None:
                return ""
            value = w.parameterValue()
            return value if isinstance(value, str) else ""

        def _refresh(self, *_):
            headers = read_csv_headers(self._csv_path())
            current = self._combo.currentText().strip()
            axis = self.parameterDefinition().metadata().get("axis", "")
            self._combo.blockSignals(True)
            self._combo.clear()
            self._combo.addItem("")
            self._combo.addItems(headers)
            # Keep what the user picked if it is still a column; otherwise
            # offer the header that looks right for this axis.
            if current in headers or current.isdigit():
                self._combo.setCurrentText(current)
            elif len(headers) < 10:     # 10+ = side-by-side tables, leave to auto
                self._combo.setCurrentText(guess_column(headers, axis))
            self._combo.blockSignals(False)
            self.widgetValueHasChanged.emit(self)

        def setWidgetValue(self, value, context):
            self._combo.setCurrentText("" if value is None else str(value))

        def widgetValue(self):
            return self._combo.currentText().strip()

        # Older Processing builds call these names instead.
        def setValue(self, value):
            self.setWidgetValue(value, None)

        def value(self):
            return self.widgetValue()

    _WRAPPER = CsvColumnWidgetWrapper
    return _WRAPPER

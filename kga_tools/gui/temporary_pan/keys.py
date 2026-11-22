# -*- coding: utf-8 -*-
"""Temporary pan - the pan key: rules for which key is allowed, and a button to pick one."""

from contextlib import suppress

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import QPushButton

DEFAULT_KEY = int(Qt.Key.Key_C.value) if hasattr(Qt.Key.Key_C, "value") else int(Qt.Key.Key_C)


def _k(name):
    v = getattr(Qt.Key, name)
    return int(v.value) if hasattr(v, "value") else int(v)


# Keys that would break normal digitizing or are already QGIS's own temporary pan
FORBIDDEN = {
    _k("Key_Escape"): "Escape cancels the current feature",
    _k("Key_Backspace"): "Backspace removes the last vertex",
    _k("Key_Delete"): "Delete removes the last vertex",
    _k("Key_Return"): "Enter finishes the feature",
    _k("Key_Enter"): "Enter finishes the feature",
    _k("Key_Tab"): "Tab moves the keyboard focus",
    _k("Key_Space"): "Space is already QGIS's own temporary pan (hold Space and move the mouse)",
    _k("Key_Shift"): "Shift changes snapping/selection behaviour",
    _k("Key_Control"): "Ctrl changes snapping/selection behaviour",
    _k("Key_Alt"): "Alt opens menus",
    _k("Key_Meta"): "This key is used by the operating system",
}
# Single-letter keys the Advanced Digitizing panel uses while it is enabled
CAD_LETTERS = {_k(f"Key_{c}"): c for c in "ADXYZMCPNE"}


def key_name(key: int) -> str:
    return QKeySequence(key).toString() or f"key {key}"


def qgis_shortcut_conflicts(key: int):
    """Names of QGIS actions whose shortcut is exactly this single key."""
    out = []
    with suppress(Exception):          # no shortcuts manager (e.g. outside QGIS): no conflicts known
        from qgis.gui import QgsGui
        mgr = QgsGui.shortcutsManager()
        seq = QKeySequence(key)
        for action in mgr.listActions():
            with suppress(RuntimeError):   # an action deleted meanwhile
                if action.shortcut() == seq:
                    out.append(action.text().replace("&", "") or action.objectName())
    return out


def check_key(key: int):
    """Return (allowed, message). Message explains problems or conflicts."""
    if key in FORBIDDEN:
        return False, FORBIDDEN[key]
    if key in (0, _k("Key_unknown")):
        return False, "This key can't be used"
    notes = []
    clashes = qgis_shortcut_conflicts(key)
    if clashes:
        notes.append("QGIS uses this key for: " + ", ".join(clashes[:4]) +
                     ". On the map the pan key wins; elsewhere QGIS keeps the shortcut.")
    if key in CAD_LETTERS:
        notes.append("The Advanced Digitizing panel also uses this letter while it is enabled; "
                     "on the map the pan key wins.")
    return True, " ".join(notes)


class KeyCaptureButton(QPushButton):
    """Click, then press a single key."""
    keyCaptured = pyqtSignal(int)

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self._capturing = False
        self.setCheckable(True)
        self.setMinimumWidth(160)
        self.toggled.connect(self._toggled)
        self._show()

    def _show(self):
        self.setText("Press a key..." if self._capturing else key_name(self.key))

    def _toggled(self, on):
        self._capturing = on
        self._show()
        if on:
            self.setFocus()
            self.grabKeyboard()
        else:
            self.releaseKeyboard()

    def keyPressEvent(self, event):  # noqa: N802
        if not self._capturing:
            return super().keyPressEvent(event)
        k = int(event.key())
        if k in (_k("Key_Shift"), _k("Key_Control"), _k("Key_Alt"), _k("Key_Meta")):
            return                     # a modifier alone: keep waiting for a real key
        self.setChecked(False)
        self.keyCaptured.emit(k)

    def set_key(self, key):
        self.key = key
        self._show()

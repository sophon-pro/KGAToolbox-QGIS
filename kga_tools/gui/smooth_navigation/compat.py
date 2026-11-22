# -*- coding: utf-8 -*-
"""Helpers that keep the plugin working on both Qt5 (QGIS 3.x) and Qt6 (QGIS 4).

All Qt enums elsewhere in the plugin are written fully scoped
(e.g. ``Qt.MouseButton.LeftButton``), which both PyQt5 and PyQt6 accept.
"""
from qgis.PyQt.QtCore import Qt


MODIFIER_MASK = (
    Qt.KeyboardModifier.ShiftModifier
    | Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


def flag_value(flag):
    """Integer value of a Qt enum/flag under PyQt5 or PyQt6."""
    if flag is None:
        return 0
    try:
        return int(flag)
    except TypeError:
        return int(flag.value)


def has_modifiers(event):
    """True when Shift/Ctrl/Alt/Meta is held (Keypad modifier is ignored)."""
    return flag_value(event.modifiers() & MODIFIER_MASK) != 0


def event_pos(event):
    """(x, y) floats of a mouse or wheel event in widget coordinates."""
    if hasattr(event, "position"):      # Qt6 mouse/wheel, Qt >= 5.14 wheel
        p = event.position()
    elif hasattr(event, "localPos"):    # Qt5 mouse
        p = event.localPos()
    else:                               # very old Qt5 wheel
        p = event.posF()
    return p.x(), p.y()

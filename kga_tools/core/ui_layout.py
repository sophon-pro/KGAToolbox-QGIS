# -*- coding: utf-8 -*-
"""Remember which toolbars and panels are on, and put them back.

A snapshot is the main window's own saved layout (`QMainWindow.saveState`, which
holds every toolbar's and panel's place and visibility) plus the names of what
was visible. `reset` restores that layout and then switches on anything from the
snapshot that is still off, without turning off whatever was enabled since.
Snapshot and switch live under `KGA/settings/` (see core/kga_settings.py).
"""

import json
from datetime import datetime

from qgis.PyQt.QtCore import QByteArray
from qgis.PyQt.QtWidgets import QDockWidget, QMenuBar, QToolBar

from . import kga_settings as ks

KINDS = (QDockWidget, QToolBar, QMenuBar)


def _named(window):
    """Every toolbar, panel and the menu bar that has a name (so it can be found again)."""
    found = []
    for kind in KINDS:
        found.extend(w for w in window.findChildren(kind) if w.objectName())
    return found


def visible_names(window):
    return sorted({w.objectName() for w in _named(window) if w.isVisible()})


def remember(window):
    """Take a snapshot of the layout as it is now; returns (toolbars, panels)."""
    names = visible_names(window)
    toolbars = sum(1 for w in window.findChildren(QToolBar)
                   if w.objectName() in names)
    panels = sum(1 for w in window.findChildren(QDockWidget)
                 if w.objectName() in names)
    state = bytes(window.saveState().toBase64()).decode('ascii')
    ks.set_value(ks.KEY_LAYOUT_STATE, state)
    ks.set_value(ks.KEY_LAYOUT_NAMES, json.dumps(names))
    ks.set_value(ks.KEY_LAYOUT_STAMP, json.dumps(
        [datetime.now().strftime('%d %b %Y %H:%M'), toolbars, panels]))
    return toolbars, panels


def has_snapshot():
    return bool(ks.get_str(ks.KEY_LAYOUT_STATE))


def describe():
    """'15 toolbars and 3 panels, remembered 20 Oct 2026 10:12', or ''."""
    if not has_snapshot():
        return ''
    try:
        stamp, toolbars, panels = json.loads(ks.get_str(ks.KEY_LAYOUT_STAMP))
    except (ValueError, TypeError):
        return 'a layout'
    return '{} toolbar{} and {} panel{}, remembered {}'.format(
        toolbars, '' if toolbars == 1 else 's',
        panels, '' if panels == 1 else 's', stamp)


def reset(window):
    """Bring the remembered toolbars and panels back; returns how many were switched on."""
    if not has_snapshot():
        return 0
    before = set(visible_names(window))
    try:
        names = set(json.loads(ks.get_str(ks.KEY_LAYOUT_NAMES)))
    except (ValueError, TypeError):
        names = set()
    window.restoreState(QByteArray.fromBase64(
        ks.get_str(ks.KEY_LAYOUT_STATE).encode('ascii')))
    wanted = names | before
    switched = 0
    for widget in _named(window):
        if widget.objectName() not in wanted:
            continue
        if not widget.isVisible():
            widget.show()
        if widget.objectName() not in before:
            switched += 1
    return switched

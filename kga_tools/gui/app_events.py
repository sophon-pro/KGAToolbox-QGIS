# -*- coding: utf-8 -*-
"""One application-wide event filter for the whole plugin.

A filter installed on the QApplication is called for every event of every object in QGIS - several
thousand a second while the map is drawing or animating - and each call into Python costs a few
microseconds before a single line of the filter has run. Three features need such a filter (Ctrl+C /
Ctrl+V in any Browser tree, the entries added to the Layers panel's right-click menu, and layer
drop-downs that list only the current map). Each used to install its own, so every event crossed
into Python three times.

Here they share one: a feature registers the event types it cares about, and the filter leaves at
once - one call, one dictionary lookup - for every other type (paint, timer, mouse move, ...), which
is nearly all of them.

    app_events.add((QEvent.Type.KeyPress,), handler)     # handler(obj, event) -> bool
    app_events.remove(handler)
"""

from contextlib import suppress

from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QApplication

_BY_TYPE = {}           # event type -> (handler, ...): a tuple, so a running call never sees it change
_STATE = {}


class _Filter(QObject):

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API name)
        handlers = _BY_TYPE.get(event.type())
        if handlers is None:
            return False
        for handler in handlers:
            swallowed = False
            with suppress(Exception):   # an event filter must never break the application
                swallowed = handler(obj, event)
            if swallowed:
                return True
        return False


def add(types, handler):
    """Call handler(obj, event) for every event of one of *types*, application-wide.

    A handler that returns True swallows the event, exactly like QObject.eventFilter."""
    for kind in types:
        current = _BY_TYPE.get(kind, ())
        if handler not in current:
            _BY_TYPE[kind] = current + (handler,)
    if 'filter' not in _STATE:
        app = QApplication.instance()
        if app is not None:
            _STATE['filter'] = _Filter(app)
            app.installEventFilter(_STATE['filter'])


def remove(handler):
    for kind in list(_BY_TYPE):
        rest = tuple(h for h in _BY_TYPE[kind] if h != handler)
        if rest:
            _BY_TYPE[kind] = rest
        else:
            del _BY_TYPE[kind]
    if not _BY_TYPE:
        watcher = _STATE.pop('filter', None)
        if watcher is not None:
            with suppress(RuntimeError):    # the application is already going down
                app = QApplication.instance()
                if app is not None:
                    app.removeEventFilter(watcher)
                watcher.deleteLater()


def handler_count():
    """How many (type, handler) registrations there are - for the tests."""
    return sum(len(handlers) for handlers in _BY_TYPE.values())

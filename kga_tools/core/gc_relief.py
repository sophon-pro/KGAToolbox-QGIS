# -*- coding: utf-8 -*-
"""Keep Python's garbage collector from freezing QGIS for a tenth of a second at a time.

Besides counting references, Python now and then looks for groups of objects that only refer to
each other. A full pass visits every container object alive in the process. With QGIS, its
Processing framework and this plugin loaded that is more than 300 000 objects - classes, functions,
module dictionaries - and one pass takes about 100 ms on the GUI thread (measured: 94 ms). While the
user works the plugin's panels and tools create Python objects all the time, so such a pass came
every 10-15 seconds: a visible hitch in the middle of a zoom or a drag.

Nearly all of those objects are the code itself and live as long as QGIS does, so there is nothing
to find among them. `settle()` runs one full pass and then moves everything that is alive into the
collector's permanent generation (`gc.freeze()`), which later passes skip: a full pass afterwards
only visits what was created since, and takes well under a millisecond. Reference counting is not
affected - frozen objects are still freed the moment nothing refers to them.

`release()` (plugin unload) gives the objects back to the collector, so a reloaded plugin's old
objects can be reclaimed.
"""

import gc
from contextlib import suppress

_STATE = {'frozen': False, 'wanted': False}


def settle_later(msec=4000):
    """`settle()` once start-up is over (unless the plugin is unloaded before that)."""
    from qgis.PyQt.QtCore import QTimer
    _STATE['wanted'] = True
    QTimer.singleShot(msec, _settle_if_wanted)


def _settle_if_wanted():
    if _STATE['wanted']:
        settle()


def settle():
    """Collect once, then exempt what is alive now from later collections."""
    with suppress(Exception):   # an optimisation only: never let it break the plugin
        gc.collect()
        gc.freeze()
        _STATE['frozen'] = True


def release():
    """Undo `settle()`."""
    _STATE['wanted'] = False
    if _STATE['frozen']:
        _STATE['frozen'] = False
        with suppress(Exception):
            gc.unfreeze()


def settled():
    return _STATE['frozen']

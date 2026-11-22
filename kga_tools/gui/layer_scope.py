"""
One rule for every layer drop-down: while Map Tabs is on, a layer list shows the layers of the map you are on,
not those of the other maps.

It is not done tool by tool. An application-wide event filter finds every QgsMapLayerComboBox - the plugin's, the
Processing dialogs', QGIS's own dialogs' - and hides the layers of the other maps from it (setExceptedLayerList)
as the combo is shown, clicked, focused or scrolled, and once more when the map is switched. A layer in no map
stays listed, the layer the combo is showing stays listed, and a tool's own exclusions are left alone.
Settings > Appearance > Map tabs turns it off.
"""

import weakref
from contextlib import suppress

from qgis.core import QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject
from qgis.PyQt.QtWidgets import QApplication, QComboBox

from ..core.compat import scoped
from ..core import kga_settings as ks
from . import app_events, map_tabs

_SEEN = '_kga_scope_seen'                         # set once the combo has been scoped for the first time
_PROP = '_kga_scope_hidden'                       # layer ids this filter added to the combo's exceptions
_FOREIGN = '_kga_scope_foreign'                   # the combo's own exceptions, as ids, when it was last scoped
#: Every layer combo met so far, keyed by a running number, as callables that give the combo or None.
#: A map switch updates these; it used to look for them among all the widgets of QGIS
#: (QApplication.allWidgets(): thousands, more with every layer and panel) each time.
_COMBOS = {}
_NEXT_KEY = [0]


def _forget(key, *_args):
    _COMBOS.pop(key, None)


def _remember(combo):
    """Note a combo without keeping it alive. One that Python owns (no parent) is held weakly and goes
    when its owner drops it. One that Qt owns is reached through a wrapper that may be the only one
    there is (sip.cast makes a new one), so that wrapper is kept until Qt deletes the combo.

    Every entry is dropped on the combo's `destroyed` signal. sip.isdeleted() cannot be trusted for this:
    the extra wrapper sip.cast makes for a combo that C++ made (Processing dialogs, the layout designer's
    item panels) is not told when C++ deletes the combo, still reports False, and touching it then
    crashes QGIS with an access violation."""
    _NEXT_KEY[0] += 1
    key = _NEXT_KEY[0]
    if sip.ispyowned(combo):
        _COMBOS[key] = weakref.ref(combo)
    else:
        _COMBOS[key] = lambda c=combo: c
    combo.destroyed.connect(lambda *_a, k=key: _forget(k))


try:
    _EVENTS = {QEvent.Type.Show, QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn,
               QEvent.Type.Wheel, QEvent.Type.KeyPress}
except AttributeError:                            # older PyQt5 enum spelling
    _EVENTS = {scoped(QEvent, 'Type', n) for n in ('Show', 'MouseButtonPress', 'FocusIn', 'Wheel', 'KeyPress')}


def hidden_ids():
    """Ids to hide from layer lists right now: the layers of the other maps; empty when the rule is off."""
    if not ks.layer_lists_current_map():
        return set()
    return map_tabs.other_map_layer_ids() or set()


def as_combo(obj):
    """`obj` as a QgsMapLayerComboBox, or None. Widgets made in C++ (Processing dialogs, QGIS dialogs) reach
    Python as plain QComboBox, so the class is asked of Qt and the wrapper cast."""
    if isinstance(obj, QgsMapLayerComboBox):
        return obj
    if isinstance(obj, QComboBox) and obj.inherits('QgsMapLayerComboBox'):
        return sip.cast(obj, QgsMapLayerComboBox)
    return None


def apply(combo, hide=None, keep_shown=True):
    """Bring one layer combo in line with the current map. Safe to call as often as wanted.

    The layer the combo shows stays listed (keep_shown); after a map switch it does not, so a combo still
    showing a layer of the map just left moves on to a layer of the new one.
    """
    if sip.isdeleted(combo):
        return
    hide = hidden_ids() if hide is None else hide
    if not combo.property(_SEEN):
        combo.setProperty(_SEEN, True)
        _remember(combo)
        keep_shown = False                        # a fresh list's pre-selected layer is just the first one, not a pick
    previous = set(combo.property(_PROP) or [])
    if not hide and not previous:
        return                                    # nothing to hide, nothing of ours to undo
    keep = combo.currentLayer() if keep_shown else None
    project = QgsProject.instance()
    known_foreign = combo.property(_FOREIGN) or []
    stale = any(project.mapLayer(i) is None for i in previous.union(known_foreign))
    if stale:
        # A layer in the combo's exception list has left the project and may be deleted. The combo keeps
        # raw pointers, and exceptedLayerList() would wrap a dangling one: an access violation (seen when
        # QGIS quits and unload() gives the lists back). Rebuild from the ids remembered instead.
        foreign = [layer for layer in (project.mapLayer(i) for i in known_foreign) if layer is not None]
    else:
        foreign = [layer for layer in combo.exceptedLayerList() if layer.id() not in previous]
    foreign_ids = {layer.id() for layer in foreign}
    ours = [layer for layer in (project.mapLayer(i) for i in hide)
            if layer is not None and layer is not keep and layer.id() not in foreign_ids]
    wanted = {layer.id() for layer in ours}
    if wanted == previous and not stale:
        return                                    # already right: no model reset, no flicker
    combo.setProperty(_PROP, sorted(wanted))
    combo.setProperty(_FOREIGN, sorted(foreign_ids))
    combo.setExceptedLayerList(foreign + ours)


class _Scoper(QObject):
    def eventFilter(self, obj, event):            # noqa: N802 (Qt API name)
        with suppress(Exception):                 # an event filter must never break the application
            if event.type() in _EVENTS and isinstance(obj, QComboBox):
                combo = as_combo(obj)
                if combo is not None:
                    apply(combo)
        return False


_STATE = {}


def install():
    """Start scoping every layer combo. Called once the plugin is up."""
    if _STATE:
        return
    app = QApplication.instance()
    scoper = _Scoper(app)
    # through the plugin's one application-wide filter, and only for the events a combo is scoped on
    app_events.add(_EVENTS, scoper.eventFilter)
    map_tabs.add_listener(refresh_all)
    _STATE['scoper'] = scoper
    _STATE['seeded'] = False


def uninstall():
    scoper = _STATE.pop('scoper', None)
    if scoper is None:
        return
    map_tabs.remove_listener(refresh_all)
    _STATE.pop('seeded', None)
    if not sip.isdeleted(scoper):
        app_events.remove(scoper.eventFilter)
        scoper.deleteLater()
    _COMBOS.clear()
    with suppress(Exception):
        for combo in all_combos():
            apply(combo, hide=set())              # give every list its layers back
            combo.setProperty(_SEEN, None)        # a later install() meets it afresh
            combo.setProperty(_FOREIGN, None)
    _COMBOS.clear()


def all_combos():
    return [c for c in (as_combo(w) for w in QApplication.allWidgets() if isinstance(w, QComboBox))
            if c is not None]


def known_combos():
    """The layer combos met so far that still exist. The first call also takes in the combos made
    before this module was installed (the panels of QGIS itself), with one walk over the widgets."""
    if not _STATE.get('seeded'):
        _STATE['seeded'] = True
        for combo in all_combos():
            if not combo.property(_SEEN):         # not met yet: apply() will note it
                apply(combo)
    alive = []
    for key, ref in list(_COMBOS.items()):
        combo = ref()
        if combo is not None and not sip.isdeleted(combo):
            alive.append(combo)
        else:
            _COMBOS.pop(key, None)
    return alive


def refresh_all(*_args):
    """The map changed (or a setting did): update every combo that already exists."""
    hide = hidden_ids()
    for combo in (known_combos() if 'scoper' in _STATE else all_combos()):
        with suppress(Exception):
            apply(combo, hide, keep_shown=False)

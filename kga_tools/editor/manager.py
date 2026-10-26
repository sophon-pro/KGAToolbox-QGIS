"""
Edit Session manager (no UI).

Keeps the layers you allow to be edited in QGIS edit mode all the time, so you can simply
edit and then Save (or Discard) - no Start/Stop Editing.

* Per-layer "editable" switch, stored in the project (custom layer property), like a map
  setting: it never changes permissions on the data itself.
* Only ONE layer per data source is put in edit mode (two layers editing the same file would
  silently overwrite each other's changes).
* Save / Discard write or drop the edits of every layer WITHOUT leaving edit mode.
* If something else stops editing on a layer that should be editable (e.g. QGIS's own
  Toggle Editing button), editing is switched back on right after.
* Layers whose source cannot be edited (rasters, CSV, read-only layers ...) are skipped.
* When the project uses a QGIS transaction mode, QGIS manages editing itself and this
  manager stays out of the way.
"""

import os
from contextlib import suppress

from qgis.core import Qgis, QgsDataProvider, QgsDataSourceUri, QgsProject, QgsProviderRegistry, QgsSettings, QgsVectorLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

LAYER_PROPERTY = "edit_session/editable"
SELECT_PROPERTY = "edit_session/selectable"
# Layer properties keep the stand-alone plugin's names so projects saved with it still work.
SETTINGS_GROUP = "kga_editor/"

# Option name -> default value
OPTIONS = {
    "new_layers_editable": True,     # Make newly added layers editable by default
    "save_with_project": False,      # Save edits when saving the project
    "confirm_save": False,           # Show a dialog to confirm saving edits
    "confirm_discard": True,         # Show a dialog to confirm discarding edits
    "active_follows_selection": True,  # Selecting a feature makes its layer the active layer
    "enabled": False,                # Edit Session On/Off switch (Off = normal QGIS behaviour); opt-in in KGA
    "follow_mouse_edit": True,       # QGIS editing tools work on the layer under the mouse
}


def option(name):
    return QgsSettings().value(SETTINGS_GROUP + name, OPTIONS[name], type=bool)


def set_option(name, value):
    QgsSettings().setValue(SETTINGS_GROUP + name, bool(value))


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


_KEY_CACHE = {}


def source_key(layer):
    """Identifies the data behind a layer (file + table), ignoring filters. Cached per layer + source."""
    cache_key = (layer.id(), layer.source())
    if cache_key not in _KEY_CACHE:
        if len(_KEY_CACHE) > 5000:
            _KEY_CACHE.clear()
        _KEY_CACHE[cache_key] = _source_key(layer)
    return _KEY_CACHE[cache_key]


def _source_key(layer):
    if layer.providerType() == "memory":
        return "memory|" + layer.id()          # every memory layer holds its own data
    with suppress(Exception):
        provider = layer.providerType()
        parts = QgsProviderRegistry.instance().decodeUri(provider, layer.source())
        if provider in ("postgres", "spatialite", "oracle", "mssql", "hana"):
            # same table = same data, whatever filter (sql), key, srid or options the layers use
            uri = QgsDataSourceUri(layer.source())
            db = uri.database() or uri.service() or uri.host() or parts.get("path") or ""
            if provider == "spatialite" and db:
                db = os.path.normcase(os.path.realpath(db))      # same file, however the path is written
            where = (db,
                     uri.host(), uri.port(), uri.schema(), uri.table())
            return provider + "|" + repr(where)
        parts = {k: v for k, v in parts.items() if k not in ("subset", "sql") and v not in (None, "")}
        if "path" in parts:
            parts["path"] = os.path.normcase(os.path.abspath(str(parts["path"])))
        if provider == "ogr" and "layerName" not in parts:
            # opened by number (layerid=1) or as the file's first layer: use the table name,
            # so the same table opened in different ways is recognised as the same data
            with suppress(Exception):
                sub = [e.split(QgsDataProvider.sublayerSeparator()) for e in layer.dataProvider().subLayers()]
                want = str(parts.get("layerId")) if parts.get("layerId") is not None else None
                hit = [b for b in sub if len(b) > 1 and (want is None or b[0] == want)]
                if len(hit) == 1 or (want is None and hit):
                    parts["layerName"] = hit[0][1]
                    parts.pop("layerId", None)
        if parts:
            return provider + "|" + repr(sorted((k, str(v)) for k, v in parts.items()))
    return layer.providerType() + "|" + layer.source().split("|subset=")[0]


def can_edit(layer):
    return (isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer)
            and layer.isValid() and layer.supportsEditing())


class EditSessionManager(QObject):
    """Emits `changed` whenever editability or pending edits change (for the UI)."""

    changed = pyqtSignal()
    projectSavedWithPendingEdits = pyqtSignal(int)   # number of layers with unsaved edits
    editsSavedWithProject = pyqtSignal(int, dict)    # layers saved, errors

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._paused = set()          # layer ids whose editing is paused on purpose
        self._started = set()         # layer ids put in edit mode by this manager
        self._watched = set()
        self._running = False
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.setInterval(0)
        self._emit_timer.timeout.connect(self.changed.emit)

    # --------------------------------------------------------------- lifecycle
    def start(self):
        if self._running:
            return
        self._running = True
        p = QgsProject.instance()
        p.layersAdded.connect(self._on_layers_added)
        p.readProject.connect(self._on_project_read)
        p.projectSaved.connect(self._on_project_saved)
        p.cleared.connect(self._on_cleared)
        p.transactionModeChanged.connect(self.schedule_apply)
        for layer in self.vector_layers():
            self._ensure_flag(layer)
        self.apply()

    def stop(self):
        """Plugin unload: give QGIS back its normal state, without losing any edit."""
        if not self._running:
            return
        self._running = False
        p = QgsProject.instance()
        for signal, slot in ((p.layersAdded, self._on_layers_added), (p.readProject, self._on_project_read),
                             (p.projectSaved, self._on_project_saved), (p.cleared, self._on_cleared),
                             (p.transactionModeChanged, self.schedule_apply)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._busy = True
        try:
            for layer in self.vector_layers():
                self._unwatch(layer)
                # layers we switched on and that hold no edits go back to normal (not editing)
                if layer.id() in self._started and layer.isEditable() and not layer.isModified():
                    layer.rollBack()
        finally:
            self._busy = False
        self._started.clear()

    def is_running(self):
        return self._running

    def end_session(self, choice):
        """
        Edit Session OFF: save or discard pending edits, take EVERY layer out of edit mode and
        stop managing - QGIS behaves normally again. Returns {layer name: errors}; if anything
        could not be saved, that layer stays in edit mode and the session keeps running.
        """
        errors = {}
        self._busy = True
        try:
            for layer in self.vector_layers():
                if not layer.isEditable():
                    continue
                if layer.isModified() and choice == "save":
                    if not layer.commitChanges():            # stopEditing=True
                        errors[layer.name()] = list(layer.commitErrors())
                else:
                    layer.rollBack()                          # discard (or nothing to save)
                layer.triggerRepaint()
        finally:
            self._busy = False
        if not errors:
            self.stop()
        else:
            self.apply()          # the session stays on: put the other layers back into edit mode
        self._emit_timer.start()
        return errors

    # --------------------------------------------------------------- queries
    def managed(self):
        """False when a QGIS transaction mode is active (QGIS then manages editing itself)."""
        return QgsProject.instance().transactionMode() == Qgis.TransactionMode.Disabled

    def vector_layers(self):
        """Vector layers in Layers-panel order."""
        layers = []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            layer = node.layer()
            if isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer):
                layers.append(layer)
        return layers

    def panel_layers(self):
        """The layers the Editable Layers panel lists and its bulk actions change: those of the active map when Map
        Tabs is on, else every vector layer."""
        from ..gui import map_tabs                         # not at import time: it pulls in a lot of the GUI
        ids = map_tabs.active_map_layer_ids()
        layers = self.vector_layers()
        return layers if ids is None else [layer for layer in layers if layer.id() in ids]

    def is_flagged(self, layer):
        value = layer.customProperty(LAYER_PROPERTY, None)
        return option("new_layers_editable") if value is None else _as_bool(value)

    def plan(self):
        """
        [(layer, should_edit, reason)] in Layers-panel order.
        Only one layer per data source may edit. A layer that is already editing keeps its data
        source, so adding a duplicate (e.g. Duplicate Layer) never takes editing away from it.
        """
        layers = self.vector_layers()
        owners = {}
        for layer in layers:                                   # pass 1: current editors keep their source
            if can_edit(layer) and self.is_flagged(layer) and layer.isEditable():
                owners.setdefault(source_key(layer), layer)
        result = []
        for layer in layers:                                   # pass 2: everybody else, in panel order
            if not can_edit(layer):
                result.append((layer, False, "read-only data source"))
                continue
            key = source_key(layer)
            owner = owners.get(key)
            if not self.is_flagged(layer):
                if owner is not None and owner is not layer:
                    result.append((layer, False, f"same data as '{owner.name()}'"))
                else:
                    result.append((layer, False, "editing turned off"))
            elif owner is None or owner is layer:
                owners[key] = layer
                result.append((layer, True, ""))
            else:
                result.append((layer, False, f"same data as '{owner.name()}'"))
        return result

    def status(self, layer):
        """Short text for the Editable Layers panel."""
        if not self.managed():
            return "managed by QGIS transaction mode"
        for lyr, want, reason in self.plan():
            if lyr.id() == layer.id():
                if want:
                    return "editable - unsaved edits" if layer.isModified() else "editable"
                if layer.isEditable() and layer.isModified():
                    return f"{reason} (unsaved edits)"
                return reason
        return ""

    def pending_layers(self):
        return [lyr for lyr in self.vector_layers() if lyr.isEditable() and lyr.isModified()]

    # --------------------------------------------------------------- core
    def schedule_apply(self, *args):
        QTimer.singleShot(0, self.apply)

    def apply(self):
        """Put every layer in the state the plan asks for."""
        if self._busy or not self._running:
            return
        self._busy = True
        try:
            for layer, want, _reason in self.plan():
                self._watch(layer)
                if not self.managed() or layer.id() in self._paused:
                    continue
                if want and not layer.isEditable():
                    if layer.startEditing():
                        self._started.add(layer.id())
                elif (not want and layer.isEditable() and not layer.isModified()
                      and layer.id() in self._started):
                    layer.rollBack()
                    self._started.discard(layer.id())
        finally:
            self._busy = False
        self._emit_timer.start()

    def set_editable(self, layer, editable, ask=None):
        """
        Turn editing on/off for one layer. `ask(layer)` -> 'save' | 'discard' | 'cancel' is called
        when the layer holds unsaved edits that would otherwise be lost. Returns False if cancelled.
        """
        if editable:
            # only one layer per data source may edit: switch the others off first
            key = source_key(layer)
            for other in self.vector_layers():
                if other.id() != layer.id() and source_key(other) == key and self.is_flagged(other):
                    if not self._switch_off(other, ask):
                        return False
            layer.setCustomProperty(LAYER_PROPERTY, True)
        else:
            if not self._switch_off(layer, ask):
                return False
        QgsProject.instance().setDirty(True)
        self.apply()
        return True

    def make_only_editable(self, layer, ask=None):
        for other in self.panel_layers():
            if other.id() != layer.id() and self.is_flagged(other):
                if not self._switch_off(other, ask):
                    return False
        return self.set_editable(layer, True, ask)

    def set_all_editable(self, editable, ask=None):
        for layer in self.panel_layers():
            if editable:
                layer.setCustomProperty(LAYER_PROPERTY, True)
            elif not self._switch_off(layer, ask):
                return False
        QgsProject.instance().setDirty(True)
        self.apply()
        return True

    def _switch_off(self, layer, ask):
        if layer.isEditable() and layer.isModified():
            choice = ask(layer) if ask else "cancel"
            if choice == "cancel":
                return False
            self._busy = True
            try:
                ok = layer.commitChanges() if choice == "save" else layer.rollBack()
            finally:
                self._busy = False
            if not ok:
                return False
        layer.setCustomProperty(LAYER_PROPERTY, False)
        if layer.isEditable() and not layer.isModified():     # an explicit "off" really stops editing
            self._busy = True
            try:
                layer.rollBack()
            finally:
                self._busy = False
        self._started.discard(layer.id())
        return True

    # --------------------------------------------------------------- selectable layers
    def is_selectable(self, layer):
        value = layer.customProperty(SELECT_PROPERTY, None)
        return True if value is None else _as_bool(value)

    def set_selectable(self, layer, selectable):
        layer.setCustomProperty(SELECT_PROPERTY, bool(selectable))
        if not selectable and layer.selectedFeatureCount():
            layer.removeSelection()
        QgsProject.instance().setDirty(True)
        self._emit_timer.start()

    def make_only_selectable(self, layer):
        for other in self.panel_layers():
            self.set_selectable(other, other.id() == layer.id())

    def set_all_selectable(self, selectable):
        for layer in self.panel_layers():
            self.set_selectable(layer, selectable)

    def selectable_layers(self, canvas=None):
        """Visible, selectable spatial layers in Layers-panel order (top first)."""
        result = []
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            layer = node.layer()
            if not isinstance(layer, QgsVectorLayer) or sip.isdeleted(layer) or not layer.isSpatial():
                continue
            if not node.isVisible() or not self.is_selectable(layer):
                continue
            if canvas is not None and layer.hasScaleBasedVisibility() and not layer.isInScaleRange(canvas.scale()):
                continue
            result.append(layer)
        return result

    # --------------------------------------------------------------- save / discard
    def save_all(self):
        """Write all pending edits; layers stay editable. Returns (layers_saved, {name: errors})."""
        saved, errors = 0, {}
        self._busy = True
        try:
            for layer in self.pending_layers():
                if layer.commitChanges(False):          # stopEditing=False
                    saved += 1
                else:
                    errors[layer.name()] = list(layer.commitErrors())
                layer.triggerRepaint()
        finally:
            self._busy = False
        self.apply()
        return saved, errors

    def discard_all(self):
        """Throw away all pending edits; layers stay editable. Returns number of layers reverted."""
        count = 0
        self._busy = True
        try:
            for layer in self.pending_layers():
                if layer.rollBack(False):               # deleteBuffer=False -> keep editing
                    count += 1
                if not layer.isEditable() and self.managed():
                    layer.startEditing()
                layer.triggerRepaint()
        finally:
            self._busy = False
        self.apply()
        return count

    def save_layer(self, layer):
        self._busy = True
        try:
            ok = layer.commitChanges(False)
        finally:
            self._busy = False
        self.apply()
        return ok, list(layer.commitErrors()) if not ok else []

    def discard_layer(self, layer):
        self._busy = True
        try:
            ok = layer.rollBack(False)
            if not layer.isEditable():
                layer.startEditing()
        finally:
            self._busy = False
        self.apply()
        return ok

    # --------------------------------------------------------------- pause (e.g. to set a filter)
    def run_paused(self, layer, func, ask=None):
        """
        Leave edit mode on `layer`, run func(), then resume editing. Used for operations QGIS
        refuses while editing (setting a filter). Returns func's result, or None if cancelled.
        """
        if layer.isEditable() and layer.isModified():
            choice = ask(layer) if ask else "cancel"
            if choice == "cancel":
                return None
        else:
            choice = None
        self._paused.add(layer.id())
        self._busy = True
        try:
            if layer.isEditable():
                ok = layer.commitChanges() if choice == "save" else layer.rollBack()
                if not ok:
                    return None
            result = func()
        finally:
            self._busy = False
            self._paused.discard(layer.id())
        self.apply()
        return result

    # --------------------------------------------------------------- signals
    def _ensure_flag(self, layer):
        if layer.customProperty(LAYER_PROPERTY, None) is None:
            layer.setCustomProperty(LAYER_PROPERTY, option("new_layers_editable"))

    def _watch(self, layer):
        if layer.id() in self._watched:
            return
        self._watched.add(layer.id())
        layer.editingStopped.connect(self._on_editing_stopped)
        for sig in (layer.editingStarted, layer.layerModified, layer.afterCommitChanges,
                    layer.afterRollBack, layer.nameChanged):
            sig.connect(self._emit_timer.start)

    def _unwatch(self, layer):
        if layer.id() not in self._watched:
            return
        self._watched.discard(layer.id())
        for sig, slot in ((layer.editingStopped, self._on_editing_stopped),
                          (layer.editingStarted, self._emit_timer.start),
                          (layer.layerModified, self._emit_timer.start),
                          (layer.afterCommitChanges, self._emit_timer.start),
                          (layer.afterRollBack, self._emit_timer.start),
                          (layer.nameChanged, self._emit_timer.start)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_layers_added(self, layers):
        new_ids = {lyr.id() for lyr in layers}
        for layer in layers:
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.customProperty(LAYER_PROPERTY, None) is None:
                key = source_key(layer)
                taken = any(o.id() not in new_ids and source_key(o) == key and self.is_flagged(o)
                            for o in self.vector_layers())
                # a duplicate of a layer that is already editable starts with editing off
                layer.setCustomProperty(LAYER_PROPERTY, False if taken else option("new_layers_editable"))
        self.schedule_apply()

    def _on_project_read(self, *args):
        self._started.clear()
        self.schedule_apply()

    def _on_cleared(self):
        self._started.clear()
        self._watched.clear()
        self._paused.clear()
        self._emit_timer.start()

    def _on_editing_stopped(self):
        # Something else (e.g. QGIS's Toggle Editing) stopped editing: switch it back on,
        # because in an edit session editable layers are always editable.
        if self._busy or not self._running:
            return
        self.schedule_apply()
        self._emit_timer.start()

    def _on_project_saved(self):
        pending = self.pending_layers()
        if not pending:
            return
        if option("save_with_project"):
            saved, errors = self.save_all()
            self.editsSavedWithProject.emit(saved, errors)
        else:
            self.projectSavedWithPendingEdits.emit(len(pending))

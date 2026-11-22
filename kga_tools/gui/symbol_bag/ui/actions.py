# -*- coding: utf-8 -*-
"""Symbol Bag – everything the user can *do*: apply, manage, import, export.

Mixed into the bag window (ui/panel.py). Expects the host to provide:
store, iface, settings, theme, cache, model, view, undo, plugin_dir, version,
collection_id, category, refresh(), _update_footer().
"""

import os
import time
from contextlib import suppress

from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (QApplication, QFileDialog, QInputDialog, QLineEdit, QMenu, QMessageBox,
                                 QProgressDialog)
from qgis.core import Qgis, QgsLayerTree, QgsProject

from ..core import io as sbio
from ..core.applier import (MODE_SAME, ApplyError, apply_to_layers, apply_to_legend_items, classified_type,
                            incompatibility_reason, is_compatible, legend_rule_key, renderer_keys)
from ..core.compat import event_pos
from ..core.defaults import seed_library
from ..core.log import log_error, log_info
from ..core.storage import KIND_FILL, KIND_LINE, KIND_MARKER, KIND_RASTER, VECTOR_KINDS, LibraryError
from ..core.symbols import layer_kind, raster_preview_image, symbol_from_xml, vector_preview
from .dialogs import AboutDialog, ImportPickerDialog, SymbolEditorDialog
from .symbol_view import MIME_SYMBOLS, ROLE_UID, decode_uids

UNSORTED = -1


class LayerDropFilter(QObject):
    """Lets symbols be dropped onto layers in the Layers panel, or onto the map
    canvas (applies to the selected layers)."""

    def __init__(self, panel, tree_view, canvas):
        super().__init__(panel)
        self.panel = panel
        self.tree_view = tree_view
        self.canvas = canvas
        self.targets = []
        for w in (tree_view.viewport() if tree_view else None, canvas.viewport() if canvas else None):
            if w is not None:
                w.installEventFilter(self)
                w.setAcceptDrops(True)
                self.targets.append(w)

    def remove(self):
        for w in self.targets:
            try:
                w.removeEventFilter(self)
            except RuntimeError:
                pass
        self.targets = []

    def _target_at(self, pos):
        """(layer, rule_key or None) under the mouse in the Layers panel."""
        idx = self.tree_view.indexAt(pos)
        if not idx.isValid():
            return None, None
        node = self.tree_view.index2node(idx)
        if node is not None and QgsLayerTree.isLayer(node):
            return node.layer(), None
        legend = self.tree_view.index2legendNode(idx)
        if legend is not None:
            layer = legend.layerNode().layer()
            key = legend_rule_key(legend)
            if key and classified_type(layer) and key in renderer_keys(layer):
                return layer, key
            return layer, None
        return None, None

    def eventFilter(self, obj, event):
        t = event.type()
        if t not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            return False
        mime = event.mimeData()
        if mime is None or not mime.hasFormat(MIME_SYMBOLS):
            return False
        uids = decode_uids(mime)
        rec = self.panel.store.record(uids[0]) if uids else None
        if rec is None:
            event.ignore()
            return True
        on_tree = self.tree_view is not None and obj is self.tree_view.viewport()
        if t == QEvent.Type.DragEnter:
            event.acceptProposedAction()
            return True
        layers, key = [], None
        if on_tree:
            layer, key = self._target_at(event_pos(event))
            if layer is not None:
                layers = [layer]
        else:
            layers = self.panel.target_layers(multiple=True)
        compatible = [lyr for lyr in layers if is_compatible(lyr, rec.kind)]
        if t == QEvent.Type.DragMove:
            if compatible:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                if on_tree:
                    self.tree_view.setCurrentIndex(self.tree_view.indexAt(event_pos(event)))
            else:
                event.ignore()
            return True
        # drop
        if compatible:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            if key is not None:
                targets = [(compatible[0], [key])]
                QTimer.singleShot(0, lambda: self.panel.apply_symbol_to_classes(rec.uid, targets))
            else:
                QTimer.singleShot(0, lambda: self.panel.apply_symbol(rec.uid, compatible))
        else:
            event.ignore()
        return True



# ----------------------------------------------------------------------
class BagActions(object):
    # ==================================================================
    # selection helpers
    # ==================================================================
    def selected_uids(self):
        sel = self.view.selectionModel()
        if sel is None:
            return []
        rows = sorted(i.row() for i in sel.selectedIndexes())
        return [self.model.records[r].uid for r in rows if r < len(self.model.records)]

    def current_uid(self):
        idx = self.view.currentIndex()
        return idx.data(ROLE_UID) if idx.isValid() else None

    def target_layers(self, multiple=False):
        if self.iface is None:
            return []
        if multiple:
            layers = [lyr for lyr in self.iface.layerTreeView().selectedLayers() if lyr is not None]
            if layers:
                return layers
        layer = self.iface.activeLayer()
        return [layer] if layer is not None else []

    def _selected_legend_nodes(self):
        view = self.iface.layerTreeView() if self.iface else None
        if view is None:
            return []
        try:
            return list(view.selectedLegendNodes())
        except AttributeError:  # QGIS < 3.32
            out = []
            for idx in view.selectionModel().selectedIndexes():
                n = view.index2legendNode(idx)
                if n is not None:
                    out.append(n)
            return out

    def _claimed_target(self):
        """(layer, class key or None) when another window (Quick Symbology) has claimed the next apply."""
        provider = getattr(self, "target_override", None)
        if provider is None:
            return None
        try:
            target = provider()
            layer = target[0] if target else None
            if layer is None or not layer.isValid():
                return None
        except (RuntimeError, TypeError, AttributeError):
            return None
        keys = target[1]
        if keys and not isinstance(keys, (list, tuple)):
            keys = [keys]
        if keys:
            valid = renderer_keys(layer) if classified_type(layer) else []
            keys = [k for k in keys if k in valid]
        return layer, (list(keys) if keys else None)

    @staticmethod
    def _class_label(layer, key):
        with suppress(Exception):
            for cat in layer.renderer().categories():
                if cat.uuid() == key:
                    return cat.label() or "(no label)"
        return "(no label)"

    def selection_targets(self):
        """What the user selected in the Layers panel.

        Returns (class_targets, layers):
          class_targets – [(layer, [rule keys], [labels])] for selected categories /
                          ranges / rules: only those classes will change;
          layers        – whole layers that were selected.
        """
        if self.iface is None:
            return [], []
        claimed = self._claimed_target()
        if claimed is not None:
            layer, keys = claimed
            if keys:
                return [(layer, keys, [self._class_label(layer, k) for k in keys])], []
            return [], [layer]
        layers = [lyr for lyr in self.iface.layerTreeView().selectedLayers() if lyr is not None]
        layer_ids = {lyr.id() for lyr in layers}
        classes = {}
        for node in self._selected_legend_nodes():
            try:
                layer = node.layerNode().layer()
            except (AttributeError, RuntimeError):
                continue
            if layer is None or layer.id() in layer_ids:
                continue
            key = legend_rule_key(node)
            if key and classified_type(layer) and key in renderer_keys(layer):
                entry = classes.setdefault(layer.id(), (layer, [], []))
                entry[1].append(key)
                entry[2].append(str(node.data(Qt.ItemDataRole.DisplayRole) or "").strip() or "(no label)")
            else:
                layers.append(layer)
                layer_ids.add(layer.id())
        if not classes and not layers:
            active = self.iface.activeLayer()
            if active is not None:
                layers = [active]
        return list(classes.values()), layers

    def describe_target(self):
        """Short text for the bag: where a double-click will apply."""
        classes, layers = self.selection_targets()
        if classes:
            labels = [lbl for _l, _k, lbls in classes for lbl in lbls]
            if len(labels) == 1:
                return "class “{}” of {}".format(labels[0], classes[0][0].name())
            return "{} selected classes".format(len(labels))
        if len(layers) > 1:
            return "{} selected layers".format(len(layers))
        if layers:
            return "whole layer {}".format(layers[0].name())
        return ""

    def _msg(self, text, level=Qgis.MessageLevel.Info, duration=4):
        if self.iface is not None:
            self.iface.messageBar().pushMessage("Symbol Bag", text, level, duration)
        else:
            log_info(text)

    # ==================================================================
    # applying
    # ==================================================================
    def _double_clicked(self, index):
        uid = index.data(ROLE_UID)
        if self.settings.get("double_click") == "edit":
            self.edit_symbol(uid)
        else:
            self.apply_to_selection(uid)

    def apply_selected(self, multiple=True):
        uid = self.current_uid() or (self.selected_uids() or [None])[0]
        if uid:
            self.apply_to_selection(uid)

    def apply_to_selection(self, uid, xml=None):
        """Apply strictly to what is selected in the Layers panel:
        selected categories -> only those categories; selected layer -> whole layer."""
        classes, layers = self.selection_targets()
        if not classes and not layers:
            self._msg("Select a layer or a category in the Layers panel first.", Qgis.MessageLevel.Warning)
            return
        if classes:
            self.apply_symbol_to_classes(uid, [(lyr, k) for lyr, k, _lbl in classes],
                                         labels=[lbl for _l, _k, lbls in classes for lbl in lbls], xml=xml)
        if layers:
            self.apply_symbol(uid, layers, xml=xml)

    def _refresh_legends(self, layers):
        if self.iface is None:
            return
        for layer in layers:
            try:
                self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            except RuntimeError:
                pass

    def apply_symbol_to_classes(self, uid, targets, labels=None, xml=None):
        rec = self.store.record(uid)
        if rec is None:
            return
        try:
            applied, changed, skipped = apply_to_legend_items(targets, rec.kind, xml or self.store.symbol_xml(uid),
                                                              undo=self.undo)
        except ApplyError as e:
            self._msg(str(e), Qgis.MessageLevel.Critical)
            return
        self._refresh_legends(applied)
        if applied:
            self.store.mark_used(uid, notify=self.category == "recent")
            self.view.pulse(uid)
            if labels and len(labels) == 1:
                where = "class “{}” of {}".format(labels[0], applied[0].name())
            else:
                where = "{} class{} of {}".format(changed, "" if changed == 1 else "es",
                                                   ", ".join(lyr.name() for lyr in applied))
            text = "Applied '{}' to {}.".format(rec.name, where)
            if skipped:
                text += " Skipped: " + "; ".join(r for _n, r in skipped[:2])
            self._msg(text, Qgis.MessageLevel.Success if not skipped else Qgis.MessageLevel.Warning)
            QgsProject.instance().setDirty(True)
            self.symbolApplied.emit(list(applied))
        elif skipped:
            self._msg("; ".join(r for _n, r in skipped[:3]), Qgis.MessageLevel.Warning)
        self._update_footer()

    def apply_symbol(self, uid, layers, xml=None):
        """Whole layers. A classified layer keeps its classes, but every class
        gets this same symbol, so the layer looks uniform."""
        rec = self.store.record(uid)
        if rec is None:
            return
        if not layers:
            self._msg("Select a layer in the Layers panel first.", Qgis.MessageLevel.Warning)
            return
        compatible = [lyr for lyr in layers if is_compatible(lyr, rec.kind)]
        if not compatible:
            self._msg(incompatibility_reason(layers[0], rec.kind), Qgis.MessageLevel.Warning)
            return
        try:
            applied, skipped, warnings = apply_to_layers(layers, rec.kind, xml or self.store.symbol_xml(uid),
                                                         mode=MODE_SAME, undo=self.undo)
        except ApplyError as e:
            self._msg(str(e), Qgis.MessageLevel.Critical)
            return
        self._refresh_legends(applied)
        if applied:
            self.store.mark_used(uid, notify=self.category == "recent")
            self.view.pulse(uid)
            names = ", ".join(lyr.name() for lyr in applied[:3]) + ("…" if len(applied) > 3 else "")
            text = "Applied '{}' to {}.".format(rec.name, names)
            if skipped:
                text += " Skipped {}: {}".format(len(skipped), "; ".join(r for _n, r in skipped[:2]))
            if warnings:
                text += " " + " ".join(warnings[:2])
            self._msg(text, Qgis.MessageLevel.Success if not (skipped or warnings) else Qgis.MessageLevel.Warning)
            QgsProject.instance().setDirty(True)
            self.symbolApplied.emit(list(applied))
        elif skipped:
            self._msg("; ".join(r for _n, r in skipped[:3]), Qgis.MessageLevel.Warning)
        self._update_footer()

    def undo_last(self):
        n = self.undo.undo()
        if n:
            if self.iface:
                for layer in QgsProject.instance().mapLayers().values():
                    self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            self._msg("Restored the previous style of {} layer(s).".format(n))
        else:
            self._msg("Nothing to undo.")
        self._update_footer()

    # ==================================================================
    # context menus
    # ==================================================================
    def _view_menu(self, pos):
        idx = self.view.indexAt(pos)
        if idx.isValid() and not self.view.selectionModel().isSelected(idx):
            self.view.setCurrentIndex(idx)
        if not idx.isValid():
            m = QMenu(self)
            m.addAction(self.theme.icon("plus"), "Create Symbol…", self.create_symbol)
            m.addAction(self.theme.icon("layers"), "Add Current Layer Symbol to Bag", self.add_current_layer)
            m.addAction(self.theme.icon("import"), "Import…", lambda: self.import_files())
            m.exec(self.view.viewport().mapToGlobal(pos))
            return
        self._show_symbol_menu(self.view.viewport().mapToGlobal(pos))

    def _show_symbol_menu(self, global_pos):
        uids = self.selected_uids()
        if not uids:
            return
        uid = self.current_uid() if self.current_uid() in uids else uids[0]
        rec = self.store.record(uid)
        t = self.theme
        m = QMenu(self)
        one = len(uids) == 1
        target = self.describe_target()
        a = m.addAction(t.icon("apply"), "Apply to {}".format(target) if target else "Apply (select a layer first)",
                        lambda: self.apply_to_selection(uid))
        a.setEnabled(bool(target))
        m.addSeparator()
        m.addAction(t.icon("palette"), "Recolor…", lambda: self.open_picker(uid, global_pos)).setEnabled(one)
        m.addAction(t.icon("edit"), "Edit Symbol…", lambda: self.edit_symbol(uid)).setEnabled(one)
        m.addAction(t.icon("copy"), "Duplicate Symbol", self.duplicate_selected)
        m.addAction(t.icon("rename"), "Rename…", self.rename_selected).setEnabled(one)
        fav = all(self.store.record(u).favorite for u in uids)
        m.addAction(t.icon("star_on" if not fav else "star"),
                    "Remove from Favorites" if fav else "Add to Favorites",
                    lambda: self.store.set_favorite(uids, not fav))
        move = m.addMenu(t.icon("folder"), "Move to Collection")
        self._fill_move_menu(move, uids)
        m.addSeparator()
        m.addAction(t.icon("export"), "Export Symbol{}…".format("" if one else "s"), lambda: self.export_symbols(uids))
        m.addAction(t.icon("style"), "Save to QGIS Style Manager", lambda: self.save_to_style_manager(uids))
        m.addSeparator()
        m.addAction(t.icon("trash", "#c0392b"), "Delete", self.delete_selected)
        if rec and rec.kind == KIND_RASTER:
            pass
        m.exec(global_pos)

    def _fill_move_menu(self, menu, uids):
        menu.addAction("Unsorted", lambda: self._move_to_collection(uids, None))
        menu.addSeparator()
        cols = self.store.collections()
        children = {}
        for c in cols:
            children.setdefault(c["parent_id"], []).append(c)

        def add(m, pid):
            for c in children.get(pid, []):
                if children.get(c["id"]):
                    sub = m.addMenu(c["name"])
                    sub.addAction("(this collection)", lambda _c=False, cid=c["id"]: self._move_to_collection(uids, cid))
                    sub.addSeparator()
                    add(sub, c["id"])
                else:
                    m.addAction(c["name"], lambda _c=False, cid=c["id"]: self._move_to_collection(uids, cid))
        add(menu, None)
        menu.addSeparator()
        menu.addAction("New Collection…", lambda: self._move_to_new_collection(uids))

    # ==================================================================
    # symbol management
    # ==================================================================
    def _toggle_favorite(self, uid):
        rec = self.store.record(uid)
        if rec:
            self.store.set_favorite([uid], not rec.favorite)
            self.view.star_pop(uid)

    def create_symbol(self, kind=None):
        cid = self.collection_id if self.collection_id not in (None, UNSORTED) else None
        if kind is None and self.category in (KIND_MARKER, KIND_LINE, KIND_FILL, KIND_RASTER):
            kind = self.category
        dlg = SymbolEditorDialog(self.store, self.iface, kind=kind, collection_id=cid, parent=self)
        if dlg.exec() and dlg.uid:
            self._select_after_refresh(dlg.uid)
            self._msg("Symbol '{}' saved to the bag.".format(self.store.record(dlg.uid).name),
                      Qgis.MessageLevel.Success)

    def edit_symbol(self, uid):
        if not uid:
            return
        dlg = SymbolEditorDialog(self.store, self.iface, uid=uid, parent=self)
        if dlg.exec():
            self.cache.invalidate(uid)
            self.refresh()

    def duplicate_selected(self):
        new = [self.store.duplicate_symbol(u) for u in self.selected_uids()]
        if new:
            self._select_after_refresh(new[-1])

    def rename_selected(self):
        uid = self.current_uid() or (self.selected_uids() or [None])[0]
        rec = self.store.record(uid) if uid else None
        if rec is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Symbol", "New name:", QLineEdit.EchoMode.Normal, rec.name)
        if ok and name.strip():
            self.store.update_symbol(uid, name=name.strip())

    def delete_selected(self):
        uids = self.selected_uids()
        if not uids:
            return
        if len(uids) == 1:
            q = "Delete '{}' from Symbol Bag?".format(self.store.record(uids[0]).name)
        else:
            q = "Delete {} symbols from Symbol Bag?".format(len(uids))
        q += "\n\nLayers already styled with them are not affected."
        if QMessageBox.question(self, "Delete", q) == QMessageBox.StandardButton.Yes:
            self.store.delete_symbols(uids)
            for u in uids:
                self.cache.invalidate(u)

    def _move_to_collection(self, uids, cid):
        if cid == UNSORTED:
            cid = None
        self.store.move_symbols(uids, cid)
        label = "Unsorted" if cid is None else " / ".join(self.store.collection_path(cid))
        self._msg("Moved {} symbol(s) to {}.".format(len(uids), label))

    def _move_to_new_collection(self, uids):
        cid = self.new_collection(None)
        if cid is not None:
            self._move_to_collection(uids, cid)

    def _reorder(self, uids, target_uid):
        order = [r.uid for r in self.store.symbol_records()]
        moving = [u for u in order if u in uids]
        rest = [u for u in order if u not in uids]
        pos = rest.index(target_uid) if target_uid and target_uid in rest else len(rest)
        self.store.reorder(rest[:pos] + moving + rest[pos:])

    def _select_after_refresh(self, uid):
        def sel():
            idx = self.model.index_of(uid)
            if idx.isValid():
                self.view.setCurrentIndex(idx)
                self.view.scrollTo(idx)
        QTimer.singleShot(30, sel)

    # collections ------------------------------------------------------------
    def new_collection(self, parent_id):
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        if not ok or not name.strip():
            return None
        try:
            cid = self.store.add_collection(name.strip(), parent_id=parent_id)
        except LibraryError as e:
            QMessageBox.warning(self, "Symbol Bag", str(e))
            return None
        return cid

    def rename_collection(self, cid):
        c = self.store.collection(cid)
        if not c:
            return
        name, ok = QInputDialog.getText(self, "Rename Collection", "New name:", QLineEdit.EchoMode.Normal, c["name"])
        if ok and name.strip():
            try:
                self.store.rename_collection(cid, name.strip())
            except LibraryError as e:
                QMessageBox.warning(self, "Symbol Bag", str(e))

    def _move_collection(self, cid, parent):
        try:
            self.store.move_collection(cid, parent)
        except LibraryError as e:
            QMessageBox.warning(self, "Symbol Bag", str(e))

    def delete_collection(self, cid):
        c = self.store.collection(cid)
        if not c:
            return
        ids = self.store.descendant_collections(cid)
        n = sum(1 for r in self.store.symbol_records() if r.collection_id in ids)
        box = QMessageBox(QMessageBox.Icon.Question, "Delete Collection",
                          "Delete the collection '{}'{}?".format(
                              c["name"], " and its sub-collections" if len(ids) > 1 else ""), parent=self)
        keep = box.addButton("Delete, keep symbols", QMessageBox.ButtonRole.AcceptRole)
        drop = box.addButton("Delete with {} symbol(s)".format(n), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        drop.setEnabled(n > 0)
        box.exec()
        if box.clickedButton() is keep:
            self.store.delete_collection(cid, delete_symbols=False)
        elif box.clickedButton() is drop:
            self.store.delete_collection(cid, delete_symbols=True)
        else:
            return
        if self.collection_id in ids:
            self.collection_id = None
        self.refresh()

    # ==================================================================
    # importing
    # ==================================================================
    def _pick_and_store(self, cands, title, errors=None):
        errors = errors or []
        if not cands:
            if errors:
                QMessageBox.warning(self, title, "\n\n".join(errors))
            else:
                self._msg("Nothing to import.", Qgis.MessageLevel.Warning)
            return []
        default = self.collection_id if self.collection_id not in (None, UNSORTED) else None
        dlg = ImportPickerDialog(cands, self.store, title, self, default_collection=default)
        if errors:
            dlg.setToolTip("\n".join(errors))
        if not dlg.exec():
            return []
        chosen = dlg.selected_candidates()
        progress = None
        if len(chosen) > 40:
            progress = QProgressDialog("Adding symbols…", None, 0, 0, self)
            progress.setMinimumDuration(300)
            progress.show()
            QApplication.processEvents()
        added, dupes = sbio.store_candidates(self.store, chosen, dlg.target_collection(),
                                             keep_paths=dlg.keep_paths.isChecked())
        if progress:
            progress.close()
        text = "Added {} symbol(s).".format(len(added))
        if dupes:
            text += " {} already in the bag were skipped.".format(dupes)
        if errors:
            text += " {} file(s) could not be read – see the log.".format(len(errors))
            for e in errors:
                log_error(e)
        self._msg(text, Qgis.MessageLevel.Success if added else Qgis.MessageLevel.Info)
        return added

    def import_files(self, paths=None):
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(self, "Import Symbols", self._last_dir(), sbio.IMPORT_FILE_FILTER)
        if not paths:
            return
        self._remember_dir(paths[0])
        svg_mode = ("marker",)
        if any(p.lower().endswith(".svg") for p in paths):
            items = ["Point markers", "Polygon pattern fills", "Both"]
            choice, ok = QInputDialog.getItem(self, "Import SVG", "Import SVG graphics as:", items, 0, False)
            if not ok:
                return
            svg_mode = {"Point markers": ("marker",), "Polygon pattern fills": ("fill",)}.get(choice, ("marker", "fill"))
        cands, errors = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for p in paths:
                try:
                    cands.extend(sbio.candidates_from_file(p, svg_mode))
                except sbio.ImportFailed as e:
                    errors.append(str(e))
                except Exception as e:
                    errors.append("{}: unexpected error – {}".format(os.path.basename(p), e))
        finally:
            QApplication.restoreOverrideCursor()
        if errors and cands:
            QMessageBox.warning(self, "Import", "Some files could not be imported:\n\n" + "\n\n".join(errors))
            errors = []
        self._pick_and_store(cands, "Import Symbols", errors)

    def import_collection(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Collection", self._last_dir(),
                                              "Symbol Bag collection (*.sbag)")
        if path:
            self.import_files([path])

    def import_style_manager(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cands = sbio.from_default_style()
        finally:
            QApplication.restoreOverrideCursor()
        for c in cands:
            c.selected = False
        self._pick_and_store(cands, "Import from QGIS Style Manager")

    def import_kga_layer_symbol(self):
        """Bring symbols of the KGA Layer Symbol gallery into the bag, by category."""
        from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cats = sbio.kga_layer_symbol_categories()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "KGA Layer Symbol", "The KGA Layer Symbol library could not be read: {}".format(e))
            return
        QApplication.restoreOverrideCursor()
        dlg = QDialog(self)
        dlg.setWindowTitle("Import from KGA Layer Symbol")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Choose the categories to bring into the bag. "
                             "Each one becomes a collection "
                             "under \"KGA Layer Symbol\"."))
        lst = QListWidget()
        for name, n in cats:
            item = QListWidgetItem("{}  ({})".format(name, n))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, name)
            lst.addItem(item)
        lst.setMinimumSize(340, 320)
        lay.addWidget(lst, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if not dlg.exec():
            return
        chosen = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())
                  if lst.item(i).checkState() == Qt.CheckState.Checked]
        if not chosen:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cands = sbio.from_kga_layer_symbol(chosen)
        finally:
            QApplication.restoreOverrideCursor()
        self._pick_and_store(cands, "Import from KGA Layer Symbol")

    def import_project(self):
        self._pick_and_store(sbio.from_project(), "Import from Current Project")

    def add_current_layer(self, layer=None):
        layer = layer or (self.iface.activeLayer() if self.iface else None)
        if layer is None:
            self._msg("Select a layer whose symbol you want to add.", Qgis.MessageLevel.Warning)
            return
        if layer_kind(layer) is None:
            self._msg("'{}' has no symbology Symbol Bag can store.".format(layer.name()), Qgis.MessageLevel.Warning)
            return
        try:
            cands = sbio.from_layer(layer)
        except Exception as e:
            self._msg("Could not read the style of '{}': {}".format(layer.name(), e), Qgis.MessageLevel.Critical)
            return
        if len(cands) == 1 and self.store.find_duplicate(cands[0].kind, cands[0].xml) is None:
            cid = self.collection_id if self.collection_id not in (None, UNSORTED) else None
            added, _ = sbio.store_candidates(self.store, cands, cid)
            if added:
                self._msg("Added the symbol of '{}' to the bag.".format(layer.name()), Qgis.MessageLevel.Success)
                self._select_after_refresh(added[0])
                if self.isHidden():
                    self.show_animated()
            return
        if len(cands) == 1:
            existing = self.store.find_duplicate(cands[0].kind, cands[0].xml)
            rec = self.store.record(existing) if existing else None
            self._msg("The symbol of '{}' is already in your bag{}.".format(
                layer.name(), " as '{}'".format(rec.name) if rec else ""))
            if existing:
                self._select_after_refresh(existing)
            return
        self._pick_and_store(cands, "Add Symbols from '{}'".format(layer.name()))

    # ==================================================================
    # exporting / style manager
    # ==================================================================
    def export_symbols(self, uids):
        recs = [self.store.record(u) for u in uids]
        recs = [r for r in recs if r]
        if not recs:
            return
        filters = ["Symbol Bag collection (*.sbag)"]
        if any(r.kind in VECTOR_KINDS for r in recs):
            filters.append("QGIS style library (*.xml)")
        if len(recs) == 1 and recs[0].kind == KIND_RASTER:
            filters.append("QGIS layer style (*.qml)")
        if len(recs) == 1:
            filters.append("PNG preview image (*.png)")
        base = recs[0].name if len(recs) == 1 else "symbols"
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in base).strip() or "symbol"
        path, flt = QFileDialog.getSaveFileName(self, "Export Symbol", os.path.join(self._last_dir(), safe),
                                                ";;".join(filters))
        if not path:
            return
        self._remember_dir(path)
        ext = {"sbag": ".sbag", "xml": ".xml", "qml": ".qml", "png": ".png"}
        for key, e in ext.items():
            if "*.{}".format(key) in flt and not path.lower().endswith(e):
                path += e
        try:
            if path.lower().endswith(".xml"):
                n = sbio.export_qgis_xml(self.store, uids, path)
            elif path.lower().endswith(".qml"):
                sbio.export_raster_qml(self.store.symbol_xml(recs[0].uid), path)
                n = 1
            elif path.lower().endswith(".png"):
                r = recs[0]
                if r.kind == KIND_RASTER:
                    raster_preview_image(self.store.symbol_xml(r.uid), 256).save(path)
                else:
                    vector_preview(symbol_from_xml(self.store.symbol_xml(r.uid)), 256).save(path)
                n = 1
            else:
                n = sbio.export_sbag(self.store, uids, path)
        except Exception as e:
            QMessageBox.warning(self, "Export", "Export failed:\n{}".format(e))
            return
        self._msg("Exported {} symbol(s) to {}".format(n, os.path.basename(path)), Qgis.MessageLevel.Success)

    def export_collection(self, cid):
        if cid in (None, UNSORTED):
            records = [r for r in self.store.symbol_records()
                       if cid is None or r.collection_id is None]
            name = "Symbol Bag library" if cid is None else "Unsorted"
            root = None
        else:
            ids = set(self.store.descendant_collections(cid))
            records = [r for r in self.store.symbol_records() if r.collection_id in ids]
            name = self.store.collection(cid)["name"]
            root = cid
        if not records:
            self._msg("This collection is empty.", Qgis.MessageLevel.Warning)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Collection", os.path.join(self._last_dir(), name + ".sbag"),
                                              "Symbol Bag collection (*.sbag)")
        if not path:
            return
        if not path.lower().endswith(".sbag"):
            path += ".sbag"
        self._remember_dir(path)
        try:
            n = sbio.export_sbag(self.store, [r.uid for r in records], path, root_collection=root)
        except Exception as e:
            QMessageBox.warning(self, "Export", "Export failed:\n{}".format(e))
            return
        self._msg("Exported '{}' ({} symbols).".format(name, n), Qgis.MessageLevel.Success)

    def export_current_view(self):
        uids = [r.uid for r in self.model.records]
        if not uids:
            self._msg("Nothing to export.", Qgis.MessageLevel.Warning)
            return
        if self.collection_id not in (None, UNSORTED) and self.category == "all" and not self.search.text():
            self.export_collection(self.collection_id)
        else:
            self.export_symbols(uids)

    def save_to_style_manager(self, uids):
        saved, errors = 0, []
        for uid in uids:
            rec = self.store.record(uid)
            try:
                sbio.save_to_qgis_style(rec.name, rec.kind, self.store.symbol_xml(uid), rec.tag_list)
                saved += 1
            except FileExistsError:
                if QMessageBox.question(self, "Style Manager",
                                        "'{}' already exists in the QGIS Style Manager. Overwrite it?"
                                        .format(rec.name)) == QMessageBox.StandardButton.Yes:
                    sbio.save_to_qgis_style(rec.name, rec.kind, self.store.symbol_xml(uid), rec.tag_list, True)
                    saved += 1
            except Exception as e:
                errors.append("{}: {}".format(rec.name, e))
        if saved:
            self._msg("Saved {} item(s) to the QGIS Style Manager (tag 'Symbol Bag').".format(saved),
                      Qgis.MessageLevel.Success)
        if errors:
            QMessageBox.warning(self, "Style Manager", "\n".join(errors))

    # ==================================================================
    # library maintenance
    # ==================================================================
    def refresh_library(self):
        self.cache.clear()
        self.store.clear_previews()
        self.refresh()
        self._msg("Previews regenerated.")

    def restore_defaults(self):
        n = seed_library(self.store)
        self._msg("Restored {} default symbol(s).".format(n) if n else "All default symbols are already present.")

    def backup_library(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup Symbol Bag", os.path.join(self._last_dir(),
                                                    "SymbolBag-{}.sbbackup".format(time.strftime("%Y%m%d"))),
            "Symbol Bag backup (*.sbbackup)")
        if not path:
            return
        if not path.lower().endswith(".sbbackup"):
            path += ".sbbackup"
        self._remember_dir(path)
        try:
            self.store.backup_to(path)
        except Exception as e:
            QMessageBox.warning(self, "Backup", "Backup failed:\n{}".format(e))
            return
        self._msg("Library backed up to {}".format(path), Qgis.MessageLevel.Success)

    def restore_library(self):
        path, _ = QFileDialog.getOpenFileName(self, "Restore Symbol Bag", self.store.backup_folder,
                                              "Symbol Bag backup (*.sbbackup)")
        if not path:
            return
        try:
            n = self.store.validate_backup(path)
        except LibraryError as e:
            QMessageBox.warning(self, "Restore", str(e))
            return
        if QMessageBox.question(self, "Restore Library",
                                "Replace your current library ({} symbols) with this backup ({} symbols)?\n\n"
                                "A safety copy of the current library is saved first.".format(self.store.count(), n)
                                ) != QMessageBox.StandardButton.Yes:
            return
        try:
            _n, safety = self.store.restore_from(path)
        except LibraryError as e:
            QMessageBox.warning(self, "Restore", str(e))
            return
        self.cache.clear()
        self.collection_id = None
        self.settings.update(self.store.get_setting("ui", {}) or {})
        self.apply_settings(self.settings, save=False)
        self.refresh()
        self._msg("Library restored. Safety copy: {}".format(os.path.basename(safety)), Qgis.MessageLevel.Success)

    def show_about(self):
        AboutDialog(self.version, self.store.path, os.path.join(self.plugin_dir, "help", "index.html"), self).exec()

    def _last_dir(self):
        return self.store.get_setting("last_dir", os.path.expanduser("~")) or os.path.expanduser("~")

    def _remember_dir(self, path):
        self.store.set_setting("last_dir", os.path.dirname(path))


# -*- coding: utf-8 -*-
"""Contents panel for a layout tab, like ArcGIS Pro's Contents pane on a layout.

It takes the place of the Layers panel while a layout tab is on screen and lists
everything on the page: text, arrows, pictures, grids, ... and the map frames.
A map frame opens up into the layers of the map it shows, so layers can be
switched on and off from the layout without leaving it.

The tree is rebuilt from the layout whenever it changes. Rows keep only an item's
uuid (never a QGIS object), so nothing can dangle.
"""

from qgis.core import (
    QgsApplication,
    QgsIconUtils,
    QgsLayerTree,
    QgsLayoutItem,
    QgsLayoutItemGroup,
    QgsLayoutItemMap,
    QgsLayoutItemPage,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHeaderView,
    QInputDialog,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
)

from . import pro_layers

try:  # Qt6: QShortcut lives in QtGui
    from qgis.PyQt.QtGui import QShortcut
except ImportError:
    from qgis.PyQt.QtWidgets import QShortcut

ROLE_KIND = Qt.ItemDataRole.UserRole          # "item" | "node" | "grid" | "overview" | "empty"
ROLE_KEY = Qt.ItemDataRole.UserRole + 1       # uuid of the layout item (or node id)
ROLE_MAP = Qt.ItemDataRole.UserRole + 2       # uuid of the map frame a child belongs to
ROLE_INDEX = Qt.ItemDataRole.UserRole + 3     # position of a grid / overview in its map

LOCK = 1  # column of the lock indicator


def _icon(name):
    return QgsApplication.getThemeIcon(name)


class _ItemTree(QTreeWidget):
    """Tree whose drops reorder the layout's items instead of moving rows."""

    def __init__(self, reorder):
        super().__init__()
        self._reorder = reorder
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def _target(self, event):
        """(row, above) when the pointer is between top-level items, else None."""
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        row = self.itemAt(pos)
        if row is None or row.parent() is not None or row.data(0, ROLE_KIND) != "item":
            return None
        rect = self.visualItemRect(row)
        return row, pos.y() < rect.center().y()

    def dragMoveEvent(self, event):  # noqa: N802 (Qt API name)
        if self._target(event) is None:
            event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802 (Qt API name)
        target = self._target(event)
        dragged = [r for r in self.selectedItems()
                   if r.parent() is None and r.data(0, ROLE_KIND) == "item"]
        event.ignore()          # never let Qt move the rows itself
        if target is not None and dragged:
            row, above = target
            self._reorder([r.data(0, ROLE_KEY) for r in dragged],
                          row.data(0, ROLE_KEY), above)


class LayoutContents(QDockWidget):
    """The tree of a layout's items (with map contents under the map frames)."""

    def __init__(self, parent, maps, link_map, map_changed, show_properties=None,
                 map_focused=None):
        """maps() -> [(map group, its theme name)]; link_map(frame, group) shows a
        map in a frame; map_changed(group, frame) refreshes a frame after its
        map's layers were switched; show_properties() brings up Item Properties."""
        super().__init__("Contents", parent)
        self.setObjectName("KgaLayoutContents")
        self._maps = maps
        self._link_map = link_map
        self._map_changed = map_changed
        self._show_properties = show_properties
        self._map_focused = map_focused
        self._layout = None
        self._nodes = {}            # int -> layer tree node (this build only)
        self._guard = False
        self._open = set()
        self._connections = []

        tree = _ItemTree(self.reorder)
        tree.setHeaderHidden(True)
        tree.setColumnCount(2)
        tree.setIndentation(pro_layers.INDENT)
        tree.setIconSize(QSize(18, 18))
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.setUniformRowHeights(True)
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(LOCK, QHeaderView.ResizeMode.Fixed)
        tree.setColumnWidth(LOCK, 26)
        tree.setStyleSheet(pro_layers.build_style(pro_layers.is_dark(tree))
                           .replace("QgsLayerTreeView", "QTreeWidget"))
        tree.itemChanged.connect(self._on_item_changed)
        tree.itemClicked.connect(self._on_clicked)
        tree.itemSelectionChanged.connect(self._on_selection)
        tree.itemDoubleClicked.connect(self._on_double_click)
        tree.customContextMenuRequested.connect(self._on_menu)
        self.tree = tree
        self.setWidget(tree)
        rename = QShortcut(QKeySequence("F2"), tree)
        rename.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        rename.activated.connect(self._rename_current)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.rebuild)

    # ---------------------------------------------------------- the layout
    def set_layout(self, layout):
        """Show the contents of this print layout (None: show nothing)."""
        if layout is not None and self._layout is not None \
                and layout is self._layout:
            self.schedule()
            return
        self._disconnect()
        self._layout = layout
        if layout is not None:
            model = layout.itemsModel()
            for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset,
                           model.layoutChanged, model.dataChanged):
                self._connect(signal)
            self._connect(layout.selectedItemChanged, self._on_layout_selection)
        self.rebuild()

    def schedule(self, *args):
        self._timer.start()

    def _connect(self, signal, slot=None):
        slot = slot or self.schedule
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _disconnect(self):
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []

    def _alive(self):
        return self._layout is not None and not sip.isdeleted(self._layout)

    def _item(self, uuid):
        if not uuid or not self._alive():
            return None
        item = self._layout.itemByUuid(uuid)
        return item if item is not None and not sip.isdeleted(item) else None

    # ------------------------------------------------------------- building
    def rebuild(self):
        tree = self.tree
        self._guard = True
        try:
            expanded = self._expanded()
            self._open = expanded
            selected = {i.data(0, ROLE_KEY) for i in tree.selectedItems()
                        if i.data(0, ROLE_KIND) == "item"}
            tree.clear()
            self._nodes = {}
            if self._alive():
                items = [i for i in self._layout.items()
                         if isinstance(i, QgsLayoutItem)
                         and not isinstance(i, QgsLayoutItemPage)
                         and i.parentGroup() is None]
                items.sort(key=lambda i: i.zValue(), reverse=True)  # top of the stack first
                for item in items:
                    self._add_item(tree.invisibleRootItem(), item, expanded)
            for row in self._rows():
                if row.data(0, ROLE_KIND) == "item" and row.data(0, ROLE_KEY) in selected:
                    row.setSelected(True)
        finally:
            self._guard = False

    def _rows(self, parent=None):
        parent = parent or self.tree.invisibleRootItem()
        for i in range(parent.childCount()):
            child = parent.child(i)
            yield child
            yield from self._rows(child)

    def _expanded(self):
        return {self._path(r) for r in self._rows() if r.isExpanded()}

    @staticmethod
    def _path(row):
        return (row.data(0, ROLE_KIND), row.data(0, ROLE_KEY), row.data(0, ROLE_MAP))

    def _row(self, parent, text, kind, key, icon=None, checked=None, map_uuid=""):
        row = QTreeWidgetItem(parent, [text, ""])
        row.setData(0, ROLE_KIND, kind)
        row.setData(0, ROLE_KEY, key)
        row.setData(0, ROLE_MAP, map_uuid)
        if icon is not None:
            row.setIcon(0, icon)
        flags = row.flags() | Qt.ItemFlag.ItemIsSelectable
        if checked is not None:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
            row.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        row.setFlags(flags)
        return row

    def _add_item(self, parent, item, expanded):
        uuid = item.uuid()
        row = self._row(parent, item.displayName(), "item", uuid, item.icon(),
                        item.isVisible())
        row.setIcon(LOCK, _icon("/lockedGray.svg" if item.isLocked() else "/unlockedGray.svg"))
        row.setToolTip(LOCK, "Locked" if item.isLocked() else "Click to lock")
        if isinstance(item, QgsLayoutItemGroup):
            for child in sorted(item.items(), key=lambda i: i.zValue(), reverse=True):
                if isinstance(child, QgsLayoutItem):
                    self._add_item(row, child, expanded)
        elif isinstance(item, QgsLayoutItemMap):
            self._add_map_children(row, item)
        if isinstance(item, QgsLayoutItemMap):   # only the selected map shows its layers
            row.setExpanded(uuid in self._selected_uuids())
        else:
            row.setExpanded(self._path(row) in expanded)

    def _selected_uuids(self):
        if not self._alive():
            return set()
        return {i.uuid() for i in self._layout.selectedLayoutItems()}

    def _focus_map(self, uuid):
        """Open this map frame's layers and fold the other maps away."""
        for row in self._rows():
            if row.data(0, ROLE_KIND) != "item":
                continue
            item = self._item(row.data(0, ROLE_KEY))
            if isinstance(item, QgsLayoutItemMap):
                row.setExpanded(row.data(0, ROLE_KEY) == uuid)
        if self._map_focused is not None:
            frame = self._item(uuid)
            self._map_focused(self._group_of(frame) if frame is not None else None)

    def _group_of(self, frame):
        name = frame.followVisibilityPresetName() if frame.followVisibilityPreset() else ""
        for group, theme in self._maps():
            if theme == name:
                return group
        return None

    def _add_map_children(self, row, frame):
        uuid = frame.uuid()
        group = self._group_of(frame)
        if group is not None:
            self._add_nodes(row, group, uuid)
        else:
            note = self._row(row, "Not linked to a map — right-click to choose one",
                             "empty", "", None)
            note.setFlags(Qt.ItemFlag.NoItemFlags)
        self._add_stack(row, frame, "grid", "Grids", "Grid", frame.grids().asList(),
                        "/mActionShowGrid.svg")
        self._add_stack(row, frame, "overview", "Overviews", "Overview",
                        frame.overviews().asList(), "/mActionShowAllLayersGray.svg")

    def _add_stack(self, row, frame, kind, title, single, entries, icon):
        """A folded group (Grids / Overviews) holding one row per grid or overview."""
        if not entries:
            return
        uuid = frame.uuid()
        enabled = [e.enabled() for e in entries]
        state = Qt.CheckState.Checked if all(enabled) else (
            Qt.CheckState.Unchecked if not any(enabled) else Qt.CheckState.PartiallyChecked)
        group = self._row(row, title, kind + "group", uuid, _icon(icon), False, uuid)
        group.setCheckState(0, state)
        for i, entry in enumerate(entries):
            child = self._row(group, entry.name() or f"{single} {i + 1}", kind, uuid,
                              _icon(icon), entry.enabled(), uuid)
            child.setData(0, ROLE_INDEX, i)
        group.setExpanded(self._path(group) in self._open)

    def _add_nodes(self, parent, group, map_uuid):
        for node in group.children():
            key = len(self._nodes) + 1
            self._nodes[key] = node
            if QgsLayerTree.isGroup(node):
                row = self._row(parent, node.name(), "node", str(key),
                                _icon("/mActionFolder.svg"), node.itemVisibilityChecked(),
                                map_uuid)
                self._add_nodes(row, node, map_uuid)
                row.setExpanded(node.isExpanded())
            elif QgsLayerTree.isLayer(node):
                layer = node.layer()
                icon = QgsIconUtils.iconForLayer(layer) if layer is not None else None
                self._row(parent, node.name(), "node", str(key), icon,
                          node.itemVisibilityChecked(), map_uuid)

    # --------------------------------------------------------- user actions
    def reorder(self, moved, target, above):
        """Drop these items (uuids) above/below the target; the list is top first."""
        if not self._alive() or target in moved:
            return
        items = [i for i in self._layout.items()
                 if isinstance(i, QgsLayoutItem) and not isinstance(i, QgsLayoutItemPage)
                 and i.parentGroup() is None]
        items.sort(key=lambda i: i.zValue(), reverse=True)
        order = [i.uuid() for i in items]
        if target not in order:
            return
        moved = [u for u in order if u in moved]            # keep their relative order
        rest = [u for u in order if u not in moved]
        at = rest.index(target) + (0 if above else 1)
        order = rest[:at] + moved + rest[at:]
        lookup = {i.uuid(): i for i in items}
        # place the items from the top of the stack down, one step at a time
        for place, uuid in enumerate(order):
            item = lookup[uuid]
            for _ in range(len(items) * 4):          # bounded: steps cannot loop forever
                current = [i.uuid() for i in sorted(items, key=lambda i: i.zValue(),
                                                    reverse=True)]
                here = current.index(uuid)
                if here == place:
                    break
                if here > place:
                    self._layout.raiseItem(item)
                else:
                    self._layout.lowerItem(item)
        self.rebuild()

    def _on_item_changed(self, row, column):
        if self._guard or column != 0:
            return
        kind = row.data(0, ROLE_KIND)
        checked = row.checkState(0) == Qt.CheckState.Checked
        key = row.data(0, ROLE_KEY)
        if kind == "item":
            item = self._item(key)
            if item is not None:
                item.setVisibility(checked)
        elif kind == "node":
            node = self._nodes.get(int(key))
            frame = self._item(row.data(0, ROLE_MAP))
            if node is None or sip.isdeleted(node) or frame is None:
                self.schedule()
                return
            node.setItemVisibilityChecked(checked)
            group = self._group_of(frame)
            if group is not None:
                self._map_changed(group, frame)
            self.schedule()
        elif kind in ("gridgroup", "overviewgroup"):
            frame = self._item(key)
            if frame is None:
                return
            entries = (frame.grids() if kind == "gridgroup" else frame.overviews()).asList()
            for entry in entries:
                entry.setEnabled(checked)
            frame.invalidateCache()
            frame.update()
            self.schedule()
        elif kind in ("grid", "overview"):
            frame = self._item(key)
            if frame is None:
                return
            stack = frame.grids() if kind == "grid" else frame.overviews()
            entries = stack.asList()
            index = row.data(0, ROLE_INDEX)
            if index is not None and index < len(entries):
                entries[index].setEnabled(checked)
                frame.invalidateCache()
                frame.update()
                self.schedule()   # the group's own check box follows

    def _on_clicked(self, row, column):
        if column != LOCK or row.data(0, ROLE_KIND) != "item":
            return
        item = self._item(row.data(0, ROLE_KEY))
        if item is not None:
            item.setLocked(not item.isLocked())
            self.schedule()

    def _on_selection(self):
        if self._guard or not self._alive():
            return
        chosen = []
        for row in self.tree.selectedItems():
            kind = row.data(0, ROLE_KIND)
            if kind == "item":
                chosen.append(self._item(row.data(0, ROLE_KEY)))
            elif kind in ("node", "grid", "overview", "gridgroup", "overviewgroup"):  # a map's content
                chosen.append(self._item(row.data(0, ROLE_MAP)))
        chosen = [item for item in chosen if item is not None]
        maps = [i for i in chosen if isinstance(i, QgsLayoutItemMap)]
        if len(maps) == 1:
            self._focus_map(maps[0].uuid())
        self._guard = True
        try:
            self._layout.deselectAll()
            for item in chosen[:-1]:
                item.setSelected(True)
            if chosen:
                # the layout announces this one, which is what fills Item Properties
                self._layout.setSelectedItem(chosen[-1])
        finally:
            self._guard = False
        if any(item is not None for item in chosen) and self._show_properties is not None:
            self._show_properties()

    def _rename_current(self):
        row = self.tree.currentItem()
        if row is not None and row.data(0, ROLE_KIND) == "item":
            self._rename(self._item(row.data(0, ROLE_KEY)))

    def _on_layout_selection(self, *args):
        """The page selection changed (clicked on the canvas): follow it."""
        if self._guard or not self._alive():
            return
        selected = self._selected_uuids()
        maps = [u for u in selected if isinstance(self._item(u), QgsLayoutItemMap)]
        if len(maps) == 1:
            self._focus_map(maps[0])
        self._guard = True
        try:
            self.tree.clearSelection()
            for row in self._rows():
                if row.data(0, ROLE_KIND) == "item" and row.data(0, ROLE_KEY) in selected:
                    row.setSelected(True)
        finally:
            self._guard = False

    def _on_double_click(self, row, column):
        if row.data(0, ROLE_KIND) == "item" and column == 0:
            self._rename(self._item(row.data(0, ROLE_KEY)))

    def _rename(self, item):
        if item is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=item.displayName())
        if ok and name.strip():
            item.setId(name.strip())
            self.schedule()

    def _on_menu(self, pos):
        row = self.tree.itemAt(pos)
        if row is None or row.data(0, ROLE_KIND) != "item":
            return
        item = self._item(row.data(0, ROLE_KEY))
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self._rename(item))
        menu.addAction("Unlock" if item.isLocked() else "Lock",
                       lambda: (item.setLocked(not item.isLocked()), self.schedule()))
        menu.addAction("Hide" if item.isVisible() else "Show",
                       lambda: (item.setVisibility(not item.isVisible()), self.schedule()))
        if isinstance(item, QgsLayoutItemMap):
            show = menu.addMenu("Show map")
            maps = self._maps()
            current = self._group_of(item)
            for group, _theme in maps:
                action = show.addAction(
                    group.name(),
                    lambda g=group: (self._link_map(item, g), self.schedule()))
                action.setCheckable(True)
                action.setChecked(current is not None and group is current)
            show.setEnabled(bool(maps))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self._delete(item))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _delete(self, item):
        if self._alive() and not sip.isdeleted(item):
            self._layout.removeLayoutItem(item)

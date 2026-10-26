"""
Favorite tools of the Modify Features pane.

Right-click a tile in the pane > Add to favorites, and the tool gets a button on the KGA Editor toolbar (up to
MAX_FAVORITES of them); right-click that button > Remove from favorites. The pane's Favorites tab lists them.
The list is stored in the user's QGIS settings, so it is the same in every project; it starts as the ten KGA
editing tools that used to be fixed on the toolbar.
"""

import json
from contextlib import suppress

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QEvent, QMimeData, QObject, QPoint, QRect, Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor, QDrag, QIcon
from qgis.PyQt.QtWidgets import QApplication, QFrame, QMenu

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

from .tools import all_tools

MAX_FAVORITES = 10
SETTINGS_KEY = "kga_editor/modify_favorites"
MIME = "application/x-kga-favorite-tool"           # what a dragged favorite button carries: the tool id
DEFAULT_FAVORITES = ("copy_paste", "construct_polygons", "copy_parallel", "buffer", "split_cogo_lines", "divide",
                     "merge", "clip", "sequential_numbering", "planarize")


class FavoriteStore(QObject):
    """The favorite tool ids, in order, kept in QgsSettings. `changed` fires after every change."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ids = self._load()

    # ------------------------------------------------------------------ storage
    @staticmethod
    def _known():
        return {t.id for t in all_tools()}

    def _load(self):
        raw = QgsSettings().value(SETTINGS_KEY, None)
        ids = list(DEFAULT_FAVORITES)
        if raw not in (None, ""):                    # "[]" is a real choice (no favorites), not a missing value
            with suppress(ValueError, TypeError):
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    ids = [str(i) for i in loaded]
        return self._clean(ids)

    def _clean(self, ids):
        known, out = self._known(), []
        for tool_id in ids:
            if tool_id in known and tool_id not in out:
                out.append(tool_id)
        return out[:MAX_FAVORITES]

    def _save(self):
        QgsSettings().setValue(SETTINGS_KEY, json.dumps(self._ids))

    # ------------------------------------------------------------------ the list
    def ids(self):
        return list(self._ids)

    def has(self, tool_id):
        return tool_id in self._ids

    def is_full(self):
        return len(self._ids) >= MAX_FAVORITES

    def add(self, tool_id):
        """True if added; False when it is already a favorite, unknown, or the list is full."""
        if tool_id in self._ids or tool_id not in self._known() or self.is_full():
            return False
        self._ids.append(tool_id)
        self._save()
        self.changed.emit()
        return True

    def remove(self, tool_id):
        if tool_id not in self._ids:
            return False
        self._ids.remove(tool_id)
        self._save()
        self.changed.emit()
        return True

    def move(self, tool_id, index):
        """Put a favorite at position `index` (0 = first, counted among the others). False if nothing changed."""
        if tool_id not in self._ids:
            return False
        rest = [i for i in self._ids if i != tool_id]
        rest.insert(max(0, min(int(index), len(rest))), tool_id)
        if rest == self._ids:
            return False
        self._ids = rest
        self._save()
        self.changed.emit()
        return True

    def reset(self):
        """Back to the default ten."""
        self._ids = self._clean(DEFAULT_FAVORITES)
        self._save()
        self.changed.emit()


def favorite_menu(store, tool_id, parent=None):
    """The right-click menu of a tile in the pane: Add to / Remove from favorites."""
    menu = QMenu(parent)
    if store.has(tool_id):
        menu.addAction("Remove from favorites").triggered.connect(lambda _c=False: store.remove(tool_id))
    elif store.is_full():
        action = menu.addAction(f"Add to favorites (all {MAX_FAVORITES} places are used - remove one first)")
        action.setEnabled(False)
    else:
        menu.addAction("Add to favorites").triggered.connect(lambda _c=False: store.add(tool_id))
    return menu


class FavoritesBar(QObject):
    """
    The favorite tools as buttons on the KGA Editor toolbar, between `before` and whatever precedes it, with a
    separator in front of them. Rebuilt whenever the store changes. A click calls run_tile(tool id). Drag a button
    along the toolbar to reorder: a blue marker shows where it will land, and the new order is saved.
    """

    def __init__(self, store, toolbar, before, run_tile, parent=None):
        super().__init__(parent)
        self.store = store
        self.toolbar = toolbar
        self.before = before
        self.run_tile = run_tile
        self.enabled = True
        self.actions = {}                    # tool id -> QAction
        self._widgets = {}                   # button widget -> tool id
        self._separator = None
        self._press = None                   # (tool id, position) of a left press on a button
        self._dragging = False
        self._pending_move = None            # a drop that arrived while the drag was still running
        self._indicator = QFrame(toolbar)    # the blue "it will land here" marker
        self._indicator.setStyleSheet("background: #0079c1;")
        self._indicator.hide()
        toolbar.setAcceptDrops(True)
        toolbar.installEventFilter(self)
        store.changed.connect(self.rebuild)
        self.rebuild()

    def rebuild(self):
        self._clear()
        tiles = {t.id: t for t in all_tools()}
        ids = [i for i in self.store.ids() if i in tiles]
        if not ids:
            return
        self._separator = self.toolbar.insertSeparator(self.before)
        for tool_id in ids:
            tile = tiles[tool_id]
            action = QAction(QIcon(tile.icon), tile.label.replace("\n", " "), self.toolbar)
            action.setObjectName(f"mActionKgaFav_{tool_id}")
            action.setToolTip(f"{tile.tip}\nFavorite - drag to reorder, right-click to remove it from the toolbar")
            action.setEnabled(self.enabled)
            action.triggered.connect(lambda _c=False, t=tool_id: self.run_tile(t))
            self.toolbar.insertAction(self.before, action)
            self.actions[tool_id] = action
            widget = self.toolbar.widgetForAction(action)
            if widget is not None:
                widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                widget.customContextMenuRequested.connect(lambda pos, t=tool_id, w=widget: self._menu(t, w, pos))
                widget.installEventFilter(self)
                self._widgets[widget] = tool_id

    def _clear(self):
        self._press = None
        for widget in list(self._widgets):
            with suppress(RuntimeError):
                widget.removeEventFilter(self)
        self._widgets = {}
        for action in self.actions.values():
            self.toolbar.removeAction(action)
            action.deleteLater()
        self.actions = {}
        if self._separator is not None:
            self.toolbar.removeAction(self._separator)
            self._separator.deleteLater()
            self._separator = None

    def build_menu(self, tool_id):
        """The right-click menu of a toolbar button."""
        menu = QMenu(self.toolbar)
        menu.addAction("Remove from favorites").triggered.connect(lambda _c=False: self.store.remove(tool_id))
        return menu

    def _menu(self, tool_id, widget, pos):
        self.build_menu(tool_id).exec(widget.mapToGlobal(pos) if pos is not None else QCursor.pos())

    # ------------------------------------------------------------------ drag to reorder
    @staticmethod
    def _pos(event):
        try:                                  # Qt6 / QGIS 4
            return event.position().toPoint()
        except AttributeError:                # Qt5 / QGIS 3.44
            return event.pos()

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj is self.toolbar:
            if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if event.mimeData().hasFormat(MIME):
                    event.acceptProposedAction()
                    dragged = self._dragged_id(event)
                    self._show_indicator(self._target_index(self._pos(event), dragged), dragged)
                    return True
            elif kind == QEvent.Type.DragLeave:
                self._indicator.hide()
            elif kind == QEvent.Type.Drop:
                if event.mimeData().hasFormat(MIME):
                    tool_id = self._dragged_id(event)
                    self._indicator.hide()
                    event.acceptProposedAction()
                    self.drop(tool_id, self._pos(event))
                    return True
            return False
        tool_id = self._widgets.get(obj)
        if tool_id is None:
            return False
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._press = (tool_id, self._pos(event))
        elif kind == QEvent.Type.MouseMove and self._press is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            if (self._pos(event) - self._press[1]).manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                self.start_drag(tool_id, obj)
                return True                   # the press is over: no click comes of it
        elif kind == QEvent.Type.MouseButtonRelease:
            self._press = None
        return False

    @staticmethod
    def _dragged_id(event):
        return bytes(event.mimeData().data(MIME)).decode("utf-8")

    def start_drag(self, tool_id, widget):
        """Pick the button up. Returns when it is dropped (or the drag is cancelled)."""
        mime = QMimeData()
        mime.setData(MIME, tool_id.encode("utf-8"))
        drag = QDrag(widget)
        drag.setMimeData(mime)
        drag.setPixmap(widget.grab())
        drag.setHotSpot(QPoint(widget.width() // 2, widget.height() // 2))
        self._dragging = True
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._dragging = False
            self._indicator.hide()
            with suppress(RuntimeError):
                widget.setDown(False)
        if self._pending_move is not None:      # the bar is rebuilt now that the dragged button is no longer in use
            moved, self._pending_move = self._pending_move, None
            self.store.move(*moved)

    def drop(self, tool_id, pos):
        """A dragged favorite was dropped at `pos` (toolbar coordinates)."""
        if tool_id not in self.actions:
            return False
        index = self._target_index(pos, tool_id)
        if self._dragging:                      # rebuilding now would delete the button being dragged
            self._pending_move = (tool_id, index)
            return True
        return self.store.move(tool_id, index)

    def _others(self, dragged):
        return [i for i in self.store.ids() if i in self.actions and i != dragged]

    def _target_index(self, pos, dragged):
        """Where among the other favorites a drop at `pos` lands: how many of them are before it."""
        horizontal = self.toolbar.orientation() == Qt.Orientation.Horizontal
        place = pos.x() if horizontal else pos.y()
        index = 0
        for tool_id in self._others(dragged):
            rect = self.toolbar.actionGeometry(self.actions[tool_id])
            centre = rect.center().x() if horizontal else rect.center().y()
            if centre < place:
                index += 1
        return index

    def _show_indicator(self, index, dragged):
        others = self._others(dragged)
        if not others:
            return
        horizontal = self.toolbar.orientation() == Qt.Orientation.Horizontal
        if index < len(others):
            rect = self.toolbar.actionGeometry(self.actions[others[index]])
            at = rect.left() - 2 if horizontal else rect.top() - 2
        else:
            rect = self.toolbar.actionGeometry(self.actions[others[-1]])
            at = rect.right() + 1 if horizontal else rect.bottom() + 1
        if horizontal:
            self._indicator.setGeometry(QRect(at, 2, 3, max(4, self.toolbar.height() - 4)))
        else:
            self._indicator.setGeometry(QRect(2, at, max(4, self.toolbar.width() - 4), 3))
        self._indicator.show()
        self._indicator.raise_()

    def set_enabled(self, on):
        """The buttons follow the Edit Session switch, like the Modify Features button."""
        self.enabled = on
        for action in self.actions.values():
            action.setEnabled(on)

    def remove(self):
        with suppress(TypeError, RuntimeError):
            self.store.changed.disconnect(self.rebuild)
        self._clear()
        with suppress(RuntimeError):
            self.toolbar.removeEventFilter(self)
            self.toolbar.setAcceptDrops(False)
            self._indicator.deleteLater()

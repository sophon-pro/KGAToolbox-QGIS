"""KGA Geoprocessing panel.

A dockable pane modelled on the ArcGIS Pro Geoprocessing pane: a search box at
the top, then Favorites, Recent, and the algorithm groups. Double-click or
press Enter to run; right-click to favorite or copy the algorithm id.

The panel owns no algorithms of its own. It is handed a callable that returns
the current algorithm list (so it survives provider refreshes) and emits
`algorithmTriggered(str)` with an algorithm id; the plugin decides how to run it.

Enum members are written in their scoped form (Qt.ItemDataRole.UserRole rather
than Qt.UserRole) because the unscoped spelling is removed in PyQt6, which QGIS
moves to in 4.0. The scoped form works in PyQt5 as well.
"""

from contextlib import suppress

from qgis.core import QgsSettings
from qgis.gui import QgsDockWidget, QgsFilterLineEdit
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..branding import (
    algorithm_icon,
    group_icon,
    icon,
    open_docs,
    ordered_algorithms,
    ordered_groups,
)

SETTINGS_FAVORITES = 'KGA/geoprocessing/favorites'
SETTINGS_RECENT = 'KGA/geoprocessing/recent'
RECENT_LIMIT = 8

ROLE_ALG_ID = Qt.ItemDataRole.UserRole
ROLE_SECTION = Qt.ItemDataRole.UserRole + 1

SECTION_FAVORITES = 'favorites'
SECTION_RECENT = 'recent'
SECTION_GROUP = 'group'


# --------------------------------------------------------------- persistence
#
# Module-level so the plugin can record a run started from the toolbar without
# the panel having to exist.

def _read_list(key):
    value = QgsSettings().value(key, [])
    if isinstance(value, str):
        # QSettings collapses a one-element list to a bare string on some
        # backends (notably the Windows registry).
        return [value] if value else []
    return list(value or [])


def favorites():
    return _read_list(SETTINGS_FAVORITES)


def set_favorites(alg_ids):
    QgsSettings().setValue(SETTINGS_FAVORITES, list(alg_ids))


def toggle_favorite(alg_id):
    current = favorites()
    if alg_id in current:
        current.remove(alg_id)
    else:
        current.append(alg_id)
    set_favorites(current)
    return alg_id in current


def recent():
    return _read_list(SETTINGS_RECENT)


def push_recent(alg_id):
    """Record a run. Called for toolbar runs too, not just panel runs."""
    current = [a for a in recent() if a != alg_id]
    current.insert(0, alg_id)
    QgsSettings().setValue(SETTINGS_RECENT, current[:RECENT_LIMIT])


def clear_recent():
    QgsSettings().setValue(SETTINGS_RECENT, [])


# --------------------------------------------------------------------- panel

class KgaGeoprocessingPanel(QgsDockWidget):

    algorithmTriggered = pyqtSignal(str)

    def __init__(self, algorithms_callable, parent=None):
        super().__init__('KGA Geoprocessing', parent)
        self.setObjectName('KGAGeoprocessingPanel')

        self._algorithms = algorithms_callable
        self._by_id = {}

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.search = QgsFilterLineEdit(container)
        self.search.setShowSearchIcon(True)
        self.search.setPlaceholderText('Search KGA tools')
        self.search.valueChanged.connect(self._on_search)
        layout.addWidget(self.search)

        self.tree = QTreeWidget(container)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemDoubleClicked.connect(self._on_activated)
        self.tree.itemActivated.connect(self._on_activated)
        layout.addWidget(self.tree, 1)

        self.hint = QLabel('Double-click a tool to run it.', container)
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)
        layout.addWidget(self.hint)

        self.setWidget(container)
        self.refresh()

    # ------------------------------------------------------------- population

    def refresh(self):
        """Rebuild from the current provider algorithms. Safe to call any time."""
        expanded = self._expanded_sections()
        selected = self._selected_alg_id()

        self.tree.clear()
        self._by_id = {alg.id(): alg for alg in self._algorithms()}

        text = (self.search.value() or '').strip().lower()
        if text:
            self._populate_filtered(text)
        else:
            self._populate_full(expanded)

        if selected:
            self._select_alg(selected)

    def _populate_full(self, expanded):
        fav_ids = [a for a in favorites() if a in self._by_id]
        if fav_ids:
            section = self._add_section('Favorites', SECTION_FAVORITES,
                                        icon('kga.png'))
            for alg_id in fav_ids:
                self._add_algorithm(section, self._by_id[alg_id])
            section.setExpanded(expanded.get(SECTION_FAVORITES, True))

        recent_ids = [a for a in recent() if a in self._by_id]
        if recent_ids:
            section = self._add_section('Recent', SECTION_RECENT, QIcon())
            for alg_id in recent_ids:
                self._add_algorithm(section, self._by_id[alg_id])
            section.setExpanded(expanded.get(SECTION_RECENT, True))

        for group_name, algs in self._grouped().items():
            gicon = group_icon(group_name)
            section = self._add_section(group_name, SECTION_GROUP, gicon)
            for alg in algs:
                self._add_algorithm(section, alg, gicon)
            # Groups start collapsed on first open; the pane stays scannable.
            section.setExpanded(expanded.get(group_name, False))

    def _populate_filtered(self, text):
        for group_name, algs in self._grouped().items():
            matches = [a for a in algs if self._matches(a, text)]
            if not matches:
                continue
            gicon = group_icon(group_name)
            section = self._add_section(group_name, SECTION_GROUP, gicon)
            for alg in matches:
                self._add_algorithm(section, alg, gicon)
            section.setExpanded(True)

        if self.tree.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(self.tree, ['No KGA tool matches that.'])
            empty.setDisabled(True)

    def _grouped(self):
        groups = {}
        for alg in self._by_id.values():
            groups.setdefault(alg.group() or 'General', []).append(alg)
        result = {}
        for name in ordered_groups(groups.keys()):
            result[name] = ordered_algorithms(name, groups[name])
        return result

    @staticmethod
    def _matches(alg, text):
        haystack = [alg.displayName(), alg.name(), alg.group() or '']
        # tags() is optional on an algorithm and may not be implemented.
        with suppress(Exception):
            haystack.extend(alg.tags())
        return any(text in part.lower() for part in haystack if part)

    def _add_section(self, label, section_kind, section_icon):
        item = QTreeWidgetItem(self.tree, [label])
        item.setData(0, ROLE_SECTION, section_kind)
        if section_icon is not None and not section_icon.isNull():
            item.setIcon(0, section_icon)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    def _add_algorithm(self, parent, alg, fallback_icon=None):
        item = QTreeWidgetItem(parent, [alg.displayName()])
        item.setData(0, ROLE_ALG_ID, alg.id())
        item.setIcon(0, algorithm_icon(alg, fallback_icon))
        tooltip = alg.shortDescription() or alg.displayName()
        item.setToolTip(0, '{}\n{}'.format(tooltip, alg.id()))
        return item

    # ------------------------------------------------------------------ state

    def _expanded_sections(self):
        state = {}
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            kind = item.data(0, ROLE_SECTION)
            key = kind if kind in (SECTION_FAVORITES, SECTION_RECENT) else item.text(0)
            state[key] = item.isExpanded()
        return state

    def _selected_alg_id(self):
        items = self.tree.selectedItems()
        return items[0].data(0, ROLE_ALG_ID) if items else None

    def _select_alg(self, alg_id):
        for i in range(self.tree.topLevelItemCount()):
            section = self.tree.topLevelItem(i)
            for j in range(section.childCount()):
                child = section.child(j)
                if child.data(0, ROLE_ALG_ID) == alg_id:
                    self.tree.setCurrentItem(child)
                    return

    # ---------------------------------------------------------------- actions

    def _on_search(self, _text=None):
        self.refresh()

    def _on_activated(self, item, _column=0):
        alg_id = item.data(0, ROLE_ALG_ID)
        if alg_id:
            self.algorithmTriggered.emit(alg_id)

    def _on_context_menu(self, point):
        item = self.tree.itemAt(point)
        if item is None:
            return

        menu = QMenu(self.tree)
        alg_id = item.data(0, ROLE_ALG_ID)

        if alg_id:
            menu.addAction('Run', lambda: self.algorithmTriggered.emit(alg_id))
            # The one route to a tool's documentation that works for all of
            # them. The Processing dialog's own Help button never appears for
            # the tools that open their own window, because they never show
            # that dialog.
            menu.addAction('Help', lambda: open_docs(alg_id.split(':', 1)[-1]))
            menu.addSeparator()
            is_fav = alg_id in favorites()
            label = 'Remove from Favorites' if is_fav else 'Add to Favorites'
            menu.addAction(label, lambda: self._toggle_favorite(alg_id))
            menu.addAction(
                'Copy Algorithm ID',
                lambda: QApplication.clipboard().setText(alg_id))
        elif item.data(0, ROLE_SECTION) == SECTION_RECENT:
            menu.addAction('Clear Recent', self._clear_recent)
        else:
            return

        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _toggle_favorite(self, alg_id):
        toggle_favorite(alg_id)
        self.refresh()

    def _clear_recent(self):
        clear_recent()
        self.refresh()

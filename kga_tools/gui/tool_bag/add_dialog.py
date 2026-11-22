"""Add Tool window: groups on the left (toolbars, panels, menus, Processing), tools of the
selected group on the right."""

from contextlib import suppress

from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QPushButton, QSplitter, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout, QWidget)

from .compat import OWN_PREFIX, alive
from .panel import icon
from .registry import SEP

ROLE = Qt.ItemDataRole.UserRole

# (key, label) - the top-level groups on the left, in display order
ROOTS = (
    ("toolbar", "Toolbars"),
    ("panel", "Panels"),
    ("menu", "Menus"),
    ("processing", "Processing"),
    ("other", "Other"),
)
ALL_KEY = ("all", ())

STYLE = """
QDialog#%(n)s { background: #ffffff; }
QTreeWidget#%(n)s_groups { background: #f3f7fd; border: none; border-right: 1px solid #dde6f3;
    outline: 0; padding: 6px 4px; }
QTreeWidget#%(n)s_groups::item { height: 27px; border-radius: 7px; padding-left: 2px; color: #24324a; }
QTreeWidget#%(n)s_groups::item:hover { background: #e6eefb; }
QTreeWidget#%(n)s_groups::item:selected { background: #d5e4ff; color: #1d4ed8; }
QTreeWidget#%(n)s_groups::branch { background: transparent; }
QListWidget#%(n)s_tools { border: none; outline: 0; background: #ffffff; }
QListWidget#%(n)s_tools::item { height: 30px; padding-left: 6px; border-radius: 7px; color: #24324a; }
QListWidget#%(n)s_tools::item:hover { background: #f2f7ff; }
QListWidget#%(n)s_tools::item:selected { background: #dbe8ff; color: #1d4ed8; }
QLineEdit#%(n)s_search { border: 1px solid #d6e2f3; border-radius: 8px; padding: 6px 10px;
    background: #f8fbff; color: #24324a; }
QLineEdit#%(n)s_search:focus { border-color: #5b95f5; background: #ffffff; }
QLabel#%(n)s_title { font-size: 14px; font-weight: bold; color: #24324a; }
QLabel#%(n)s_hint { color: #6b7a90; }
QSplitter::handle { background: #dde6f3; width: 1px; }
"""


def group_key(source, location):
    """Which left-hand group a (source, location) pair belongs to -> (root, path parts)."""
    if source == "toolbar":
        return ("panel" if location.endswith(" panel") else "toolbar"), (location,)
    if source == "menu":
        return "menu", tuple(p for p in location.split(SEP) if p)
    if source == "processing":
        parts = tuple(p for p in location.split(SEP) if p)
        return "processing", parts[1:] if parts and parts[0] == "Processing" else parts
    return "other", ()


class AddToolDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.ctrl = controller
        self.setObjectName(OWN_PREFIX + "_adddialog")
        self.setWindowTitle("Add Tools to Tool Bag")
        self.setWindowIcon(icon("plugin.svg"))
        self.resize(860, 580)
        self.setMinimumSize(560, 360)
        self.setStyleSheet(STYLE % {"n": self.objectName()})
        self.items = []
        self.members = {}              # group key -> set of item indexes
        self.nodes = {}                # group key -> QTreeWidgetItem
        self._last_key = ALL_KEY

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.split = QSplitter(Qt.Orientation.Horizontal, self)
        self.split.setChildrenCollapsible(False)
        outer.addWidget(self.split, 1)

        # ---- left: groups
        self.groups = QTreeWidget()
        self.groups.setObjectName(self.objectName() + "_groups")
        self.groups.setHeaderHidden(True)
        self.groups.setIndentation(14)
        self.groups.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.groups.setIconSize(QSize(16, 16))
        self.groups.setMinimumWidth(180)
        self.groups.currentItemChanged.connect(self._group_changed)
        self.split.addWidget(self.groups)

        # ---- right: search + tools
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 12, 14, 10)
        rl.setSpacing(8)
        self.title = QLabel("All Tools")
        self.title.setObjectName(self.objectName() + "_title")
        rl.addWidget(self.title)
        self.search = QLineEdit()
        self.search.setObjectName(self.objectName() + "_search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        rl.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName(self.objectName() + "_tools")
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setIconSize(QSize(20, 20))
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemDoubleClicked.connect(lambda *_: self.add_selected())
        rl.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.count = QLabel()
        self.count.setObjectName(self.objectName() + "_hint")
        row.addWidget(self.count, 1)
        add = QPushButton(icon("add.svg"), "Add to Tool Bag")
        add.setDefault(True)
        add.clicked.connect(self.add_selected)
        row.addWidget(add)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        rl.addLayout(row)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([250, 610])

    # ------------------------------------------------------------ building
    def populate(self):
        self.ctrl.registry.scan()
        self.list.clear()
        self.items = []
        self.members = {ALL_KEY: set()}
        tools = sorted(self.ctrl.registry.tools(include_processing=True), key=lambda t: t.text.lower())
        for idx, info in enumerate(tools):
            it = QListWidgetItem(info.text)
            if info.action is not None and alive(info.action):
                it.setIcon(info.action.icon())
            where = info.where or info.category
            it.setData(ROLE, (info.id, info.text,
                              " ".join((info.text, where, info.category, info.id)).lower(), where))
            it.setToolTip(f"{info.description or info.text}\n{where}")
            self.list.addItem(it)
            self.items.append(it)
            self.members[ALL_KEY].add(idx)
            pairs = list(zip(info.locations, info.loc_sources)) or [("Other", "other")]
            for loc, src in pairs:
                root, parts = group_key(src, loc)
                self.members.setdefault((root, ()), set()).add(idx)
                for i in range(1, len(parts) + 1):
                    self.members.setdefault((root, parts[:i]), set()).add(idx)
        self._build_tree()
        QTimer.singleShot(0, self._alg_icons)
        self.mark_added()
        key = self._last_key if self._last_key in self.nodes else ALL_KEY
        self.groups.setCurrentItem(self.nodes[key])
        self.apply_filter()

    def _build_tree(self):
        self.groups.blockSignals(True)
        self.groups.clear()
        self.nodes = {}
        bold = QFont(self.groups.font())
        bold.setBold(True)
        top = QTreeWidgetItem([f"All Tools  ({len(self.items)})"])
        top.setData(0, ROLE, ALL_KEY)
        top.setFont(0, bold)
        top.setIcon(0, icon("plugin.svg"))
        self.groups.addTopLevelItem(top)
        self.nodes[ALL_KEY] = top
        for root, label in ROOTS:
            if (root, ()) not in self.members:
                continue
            node = QTreeWidgetItem([f"{label}  ({len(self.members[(root, ())])})"])
            node.setData(0, ROLE, (root, ()))
            node.setFont(0, bold)
            self.groups.addTopLevelItem(node)
            self.nodes[(root, ())] = node
        keys = sorted((k for k in self.members if k[1]), key=lambda k: (len(k[1]), k[1]))
        for root, parts in keys:
            parent = self.nodes.get((root, parts[:-1]))
            if parent is None:
                continue
            node = QTreeWidgetItem([f"{parts[-1]}  ({len(self.members[(root, parts)])})"])
            node.setData(0, ROLE, (root, parts))
            parent.addChild(node)
            self.nodes[(root, parts)] = node
        for node in self.nodes.values():
            node.sortChildren(0, Qt.SortOrder.AscendingOrder)
        for root in ("toolbar", "panel"):
            if (root, ()) in self.nodes:
                self.nodes[(root, ())].setExpanded(True)
        self.groups.blockSignals(False)

    def _alg_icons(self):
        from qgis.core import QgsApplication
        with suppress(Exception):
            reg = QgsApplication.processingRegistry()
            for it in self.items:
                tid = it.data(ROLE)[0]
                if tid.startswith("processing:"):
                    alg = reg.algorithmById(tid.split(":", 1)[1])
                    if alg is not None:
                        it.setIcon(alg.icon())

    def mark_added(self):
        ids = {t["id"] for t in self.ctrl.tools}
        for it in self.items:
            font = it.font()
            font.setBold(it.data(ROLE)[0] in ids)
            it.setFont(font)

    # ------------------------------------------------------------ filtering
    def _current_key(self):
        node = self.groups.currentItem()
        return node.data(0, ROLE) if node is not None else ALL_KEY

    def _group_changed(self, *_):
        self._last_key = self._current_key()
        self.apply_filter()

    def apply_filter(self):
        key = self._current_key()
        node = self.nodes.get(key)
        name = "All Tools" if key == ALL_KEY else (node.text(0).rsplit("  (", 1)[0] if node else "")
        if key[1] and key[0] in ("menu", "processing"):
            name = SEP.join(key[1])                 # full path, e.g. Vector › Geoprocessing Tools
            name = f"{dict(ROOTS)[key[0]]}{SEP}{name}"
        elif key[0] != "all" and not key[1]:
            name = dict(ROOTS).get(key[0], name)
        self.title.setText(name)
        self.search.setPlaceholderText(
            "Search all tools – e.g. add ring, reshape, split, buffer, clip…" if key == ALL_KEY
            else f"Search in this group…")
        allowed = self.members.get(key, set())
        # a group that has sub-groups shows where each tool lives; a leaf group does not need to
        show_where = key == ALL_KEY or (node is not None and node.childCount() > 0)
        words = self.search.text().lower().split()
        shown = 0
        for idx, it in enumerate(self.items):
            ok = idx in allowed and all(w in it.data(ROLE)[2] for w in words)
            it.setHidden(not ok)
            if ok:
                shown += 1
                _tid, text, _k, where = it.data(ROLE)
                it.setText(f"{text}   — {where}" if show_where else text)
        self.count.setText(f"{shown} of {len(allowed)} tools  (bold = already in the bag)")

    def add_selected(self):
        for it in self.list.selectedItems():
            tid, text = it.data(ROLE)[:2]
            self.ctrl.add_tool(tid, text)
        self.mark_added()

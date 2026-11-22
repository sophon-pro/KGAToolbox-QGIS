"""
The Create Features pane: a dock with the feature templates of every editable layer.

Laid out like ArcGIS Pro's Create Features pane: filter, search and view buttons on top; a
"Create ... completed" strip; Templates / Favorites tabs; templates grouped under their layer. Click
a template to select it - the construction tools for its geometry type appear under its name, and
the arrow opens its properties. Everything that draws lives in the controller and the map tool;
the pane only picks (layer, template, tool).
"""

import os
import re
from contextlib import suppress
from functools import partial

from qgis.core import QgsProject, QgsSettings, QgsSymbolLayerUtils
from ...gui.panel_bubble import ToolbarReveal
from qgis.gui import QgsDockWidget
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QActionGroup
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QActionGroup

from ...branding import open_docs
from ..attributes.widgets import make_tab_bar
from .properties import TemplateProperties
from .templates import (
    GEOMETRY_NAMES,
    layer_templates,
    load_favorites,
    project_layers,
    save_favorites,
)
from .tools import default_tool, tools_for

TITLE = "Create Features"
DOCS_NAME = "create_features"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
SETTINGS = "kga_editor/create_"
GROUP_BY = (("layer", "Layer"), ("geometry", "Geometry Type"), ("tag", "Tag"), ("none", "None"))
SHOW = (("all", "All templates"),
        ("point", "Point templates"), ("line", "Line templates"), ("polygon", "Polygon templates"))
SWATCH = 16

BLUE = "#0079c1"
QSS = f"""
QWidget#KgaCreateRoot {{ background: #f6f8f8; }}
QTabBar {{ background: transparent; font-size: 10pt; }}
QTabBar::tab {{
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 4px 6px 3px 6px; margin-right: 8px; color: #5f6368;
}}
QTabBar::tab:hover {{ color: {BLUE}; }}
QTabBar::tab:selected {{ color: #111111; border-bottom: 2px solid {BLUE}; }}
QLineEdit#KgaCreateSearch {{
    background: white; border: 1px solid #a9adb1; border-radius: 2px; padding: 3px 4px; font-size: 10pt;
}}
QLineEdit#KgaCreateSearch:focus {{ border-color: {BLUE}; }}
QToolButton#KgaCreateHeadButton {{ border: 1px solid transparent; border-radius: 2px; padding: 2px; }}
QToolButton#KgaCreateHeadButton:hover {{ background: #e1ecf5; border-color: #b8d3e8; }}
QToolButton#KgaCreateHeadButton::menu-indicator {{ image: none; }}
QFrame#KgaCreateDone {{ background: #e3f1e1; border: 1px solid #c6e0c2; }}
QFrame#KgaCreateDone QLabel {{ font-size: 10pt; color: #1f3a1f; }}
QToolButton#KgaCreateClose {{ border: none; color: #5f6368; font-size: 11pt; padding: 0 4px; }}
QToolButton#KgaCreateClose:hover {{ color: #111111; background: #d4e7d1; }}
QToolButton#KgaCreateGroup {{
    border: none; background: transparent; color: #1b1b1b;
    font-size: 11pt; font-weight: 500; text-align: left; padding: 9px 4px 3px 2px;
}}
QToolButton#KgaCreateGroup:hover {{ color: {BLUE}; }}
QFrame#KgaCreateTemplate {{ background: #f6f8f8; border: 1px solid transparent; margin: 0; }}
QFrame#KgaCreateTemplate[hot="true"] {{ background: #e4eef6; }}
QFrame#KgaCreateTemplate[picked="true"] {{ background: #dfeaf4; border: 1px solid #8fb4d0; }}
QFrame#KgaCreateTemplate QLabel {{ font-size: 10pt; color: #1b1b1b; background: transparent; }}
QToolButton#KgaCreateTool {{ border: 1px solid transparent; border-radius: 2px; padding: 2px; background: transparent; }}
QToolButton#KgaCreateTool:hover {{ background: #cfe3f3; border-color: #9cc2de; }}
QToolButton#KgaCreateTool:checked {{ background: #f6f9fc; border: 1px solid #7a8a99; }}
QToolButton#KgaCreateGo {{ border: none; background: transparent; padding: 2px; }}
QToolButton#KgaCreateGo:hover {{ background: #cfe3f3; }}
QLabel#KgaCreateEmpty {{ color: #5f6368; font-size: 10pt; padding: 10px 6px; }}
QScrollArea {{ border: none; background: transparent; }}
"""


def _natural(text):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def symbol_pixmap(symbol, size=SWATCH):
    """A small picture of a template's symbol."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    if symbol is None:
        return pix
    with suppress(Exception):
        return QgsSymbolLayerUtils.symbolPreviewPixmap(symbol, QSize(size, size), 1)
    return pix


class TemplateRow(QFrame):
    """One template: swatch and name; when picked it grows the tool strip and the properties arrow."""

    picked = pyqtSignal(object)                 # the row
    toolChosen = pyqtSignal(object, str)        # row, tool id
    propertiesRequested = pyqtSignal(object)
    menuRequested = pyqtSignal(object, object)  # row, global position

    def __init__(self, layer, tmpl, parent=None, pixmap=None):
        super().__init__(parent)
        self.layer = layer
        self.tmpl = tmpl
        self.setObjectName("KgaCreateTemplate")
        self.setProperty("picked", False)
        self.setProperty("hot", False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tool_buttons = {}
        self._tool_strip = None
        self._go = None

        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(14, 3, 6, 3)
        self._vbox.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.swatch = QLabel(self)
        self.swatch.setPixmap(pixmap if pixmap is not None else symbol_pixmap(tmpl.symbol))
        self.swatch.setFixedSize(SWATCH + 2, SWATCH + 2)
        self.label = QLabel(tmpl.title, self)
        self.label.setToolTip(self._tip())
        for w in (self.swatch, self.label):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        top.addWidget(self.swatch)
        top.addWidget(self.label, 1)
        self._top = top
        self._vbox.addLayout(top)

    def _tip(self):
        lines = [f"{self.layer.name()} - {GEOMETRY_NAMES[self.layer.geometryType()]}"]
        if self.tmpl.description:
            lines.append(self.tmpl.description)
        if self.tmpl.note:
            lines.append(f"Values not set: {self.tmpl.note}.")
        return "\n".join(lines)

    # ------------------------------------------------------------- state
    def set_picked(self, picked, tool_id=""):
        self.setProperty("picked", bool(picked))
        self._repolish()
        if picked:
            self._build_strip()
            self.check_tool(tool_id)
        else:
            self._drop_strip()

    def check_tool(self, tool_id):
        for key, button in self.tool_buttons.items():
            button.setChecked(key == tool_id)

    def _build_strip(self):
        if self._tool_strip is not None:
            return
        self._go = QToolButton(self)
        self._go.setObjectName("KgaCreateGo")
        self._go.setIcon(QIcon(os.path.join(ICON_DIR, "go.svg")))
        self._go.setIconSize(QSize(20, 20))
        self._go.setToolTip("Template properties: name, description, tags, default tool and default values")
        self._go.setCursor(Qt.CursorShape.PointingHandCursor)
        self._go.clicked.connect(lambda: self.propertiesRequested.emit(self))
        self._top.addWidget(self._go)
        self._tool_strip = QWidget(self)
        strip = QHBoxLayout(self._tool_strip)
        strip.setContentsMargins(26, 0, 0, 2)
        strip.setSpacing(3)
        for tool_id, label, icon, tip in tools_for(self.layer.geometryType()):
            button = QToolButton(self._tool_strip)
            button.setObjectName("KgaCreateTool")
            button.setCheckable(True)
            button.setIcon(icon)
            button.setIconSize(QSize(22, 22))
            button.setToolTip(tip)
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, t=tool_id: self.toolChosen.emit(self, t))
            self.tool_buttons[tool_id] = button
            strip.addWidget(button)
        strip.addStretch(1)
        self._vbox.addWidget(self._tool_strip)

    def _drop_strip(self):
        if self._tool_strip is None:
            return
        self._vbox.removeWidget(self._tool_strip)
        self._tool_strip.deleteLater()
        self._tool_strip = None
        self.tool_buttons = {}
        self._top.removeWidget(self._go)
        self._go.deleteLater()
        self._go = None

    def _repolish(self):
        self.style().unpolish(self)
        self.style().polish(self)

    # ------------------------------------------------------------- mouse
    def enterEvent(self, event):
        self.setProperty("hot", True)
        self._repolish()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setProperty("hot", False)
        self._repolish()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.menuRequested.emit(self, event.globalPos())


class TemplateList(QScrollArea):
    """Scrollable list of groups and template rows."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("KgaCreateBody")
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)    # else rows leave stale paint behind
        self.body.setStyleSheet("QWidget#KgaCreateBody { background: #f6f8f8; }")
        self.vbox = QVBoxLayout(self.body)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)
        self.setWidget(self.body)
        self.rows = {}

    def clear(self):
        self.rows = {}
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def fill(self, groups, empty_text):
        """groups: [(title, [(layer, template)])]."""
        self.clear()
        if not groups:
            label = QLabel(empty_text, self.body)
            label.setObjectName("KgaCreateEmpty")
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self.vbox.addWidget(label)
            self.vbox.addStretch(1)
            return
        for title, items in groups:
            collapsed = title in self.panel.collapsed
            if title is not None:
                header = QToolButton(self.body)
                header.setObjectName("KgaCreateGroup")
                header.setText(title)
                header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                header.setArrowType(Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow)
                header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                header.setCursor(Qt.CursorShape.PointingHandCursor)
                header.clicked.connect(lambda _c=False, t=title: self.panel.toggle_group(t))
                self.vbox.addWidget(header)
            if collapsed:
                continue
            for layer, tmpl in items:
                row = TemplateRow(layer, tmpl, self.body, self.panel.swatch(tmpl))
                row.picked.connect(self.panel.row_picked)
                row.toolChosen.connect(self.panel.row_tool_chosen)
                row.propertiesRequested.connect(self.panel.row_properties)
                row.menuRequested.connect(self.panel.row_menu)
                self.rows[tmpl.key] = row
                self.vbox.addWidget(row)
        self.vbox.addStretch(1)


class CreateFeaturesPanel(ToolbarReveal, QgsDockWidget):

    templateActivated = pyqtSignal(object, object, str)    # layer, template, tool id
    templateReleased = pyqtSignal()                        # the picked template went away
    templateChanged = pyqtSignal(object, object)           # layer, template (properties saved)

    def __init__(self, iface, parent=None):
        super().__init__(TITLE, parent)
        self.setObjectName("KGACreateFeaturesPanel")
        self.iface = iface
        self.collapsed = set()
        self.picked_key = None
        self.picked_tool = ""
        self.last_tool = {}                          # template key -> tool id used last
        self.favorites = set(load_favorites())
        self._watched = set()
        self._templates = {}                         # key -> (layer, template), of the last rebuild
        # Rebuilding the list draws one swatch per template and builds one row per template; in a big
        # project that is a few hundred milliseconds, and it used to run on every layer added, renamed,
        # restyled or switched on / off - with the pane closed too. So: a closed pane only notes that it
        # is out of date (_stale) and rebuilds when it is shown, and the swatches are kept (_swatches)
        # until their layer's symbology changes.
        self._stale = False
        self._swatches = {}                          # template key -> (symbol fingerprint, QPixmap)
        self._forgetters = {}                        # layer id -> the slot connected to its style signals
        settings = QgsSettings()
        self.group_by = settings.value(SETTINGS + "group_by", "layer")
        self.show_filter = settings.value(SETTINGS + "show", "all")
        if self.group_by not in dict(GROUP_BY):
            self.group_by = "layer"
        if self.show_filter not in dict(SHOW):
            self.show_filter = "all"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._scheduled_refresh)

        self._build_ui()
        project = QgsProject.instance()
        project.layersAdded.connect(self._schedule)
        project.layersRemoved.connect(self._layers_removed)
        project.cleared.connect(self._project_cleared)
        project.readProject.connect(self._project_read)
        project.layerTreeRoot().visibilityChanged.connect(self._visibility_changed)
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QWidget(self)
        root.setObjectName("KgaCreateRoot")
        root.setStyleSheet(QSS)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.filter_button = self._head_button("filter.svg", "Filter the templates")
        self.filter_menu = QMenu(self.filter_button)
        self.filter_group = QActionGroup(self)
        for key, text in SHOW:
            action = self.filter_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(key == self.show_filter)
            action.setData(key)
            self.filter_group.addAction(action)
            action.triggered.connect(lambda _c=False, k=key: self._set_show(k))
        self.filter_button.setMenu(self.filter_menu)
        self.filter_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        head.addWidget(self.filter_button)

        self.search = QLineEdit(root)
        self.search.setObjectName("KgaCreateSearch")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._schedule)
        head.addWidget(self.search, 1)

        self.view_button = self._head_button("view.svg", "Change how the templates are grouped")
        self.view_menu = QMenu(self.view_button)
        self.view_menu.addSection("Group by")
        self.view_group = QActionGroup(self)
        for key, text in GROUP_BY:
            action = self.view_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(key == self.group_by)
            self.view_group.addAction(action)
            action.triggered.connect(lambda _c=False, k=key: self._set_group_by(k))
        self.view_menu.addSeparator()
        self.view_menu.addAction("Collapse all groups").triggered.connect(self._collapse_all)
        self.view_menu.addAction("Expand all groups").triggered.connect(self._expand_all)
        self.view_menu.addSeparator()
        self.view_menu.addAction("Help").triggered.connect(lambda: open_docs(DOCS_NAME))
        self.view_button.setMenu(self.view_menu)
        self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        head.addWidget(self.view_button)
        outer.addLayout(head)

        self.done = QFrame(root)
        self.done.setObjectName("KgaCreateDone")
        done_row = QHBoxLayout(self.done)
        done_row.setContentsMargins(8, 4, 4, 4)
        tick = QLabel(self.done)
        tick.setPixmap(QIcon(os.path.join(ICON_DIR, "done.svg")).pixmap(QSize(18, 18)))
        self.done_text = QLabel(self.done)
        self.done_text.setWordWrap(True)
        close = QToolButton(self.done)
        close.setObjectName("KgaCreateClose")
        close.setText("✕")
        close.setToolTip("Dismiss")
        close.clicked.connect(self.done.hide)
        done_row.addWidget(tick)
        done_row.addWidget(self.done_text, 1)
        done_row.addWidget(close)
        self.done.hide()
        outer.addWidget(self.done)

        self.stack = QStackedWidget(root)
        page = QWidget(self.stack)
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(4)
        self.tabs = make_tab_bar(["Templates", "Favorites"], page)
        self.tabs.currentChanged.connect(self._tab_changed)
        pv.addWidget(self.tabs)
        self.lists = QStackedWidget(page)
        self.templates_view = TemplateList(self, self.lists)
        self.favorites_view = TemplateList(self, self.lists)
        self.lists.addWidget(self.templates_view)
        self.lists.addWidget(self.favorites_view)
        pv.addWidget(self.lists, 1)
        self.properties = TemplateProperties(self.stack)
        self.properties.finished.connect(self._properties_done)
        self.stack.addWidget(page)
        self.stack.addWidget(self.properties)
        outer.addWidget(self.stack, 1)
        self.setWidget(root)

    def _head_button(self, icon, tip):
        button = QToolButton(self)
        button.setObjectName("KgaCreateHeadButton")
        button.setIcon(QIcon(os.path.join(ICON_DIR, icon)))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(tip)
        return button

    # ------------------------------------------------------------------ menus / settings
    def _set_show(self, key):
        self.show_filter = key
        QgsSettings().setValue(SETTINGS + "show", key)
        self.refresh()

    def _set_group_by(self, key):
        self.group_by = key
        QgsSettings().setValue(SETTINGS + "group_by", key)
        self.refresh()

    def _collapse_all(self):
        self.collapsed = {title for title, _items in self._current_groups() if title is not None}
        self.refresh()

    def _expand_all(self):
        self.collapsed = set()
        self.refresh()

    def toggle_group(self, title):
        self.collapsed.symmetric_difference_update({title})
        self.refresh()

    def _tab_changed(self, index):
        self.lists.setCurrentIndex(index)
        self.refresh()

    # ------------------------------------------------------------------ data
    def _schedule(self, *_args):
        self._timer.start()

    def _scheduled_refresh(self):
        """The project changed. A pane nobody is looking at is not rebuilt now (see __init__)."""
        if not self.isHidden():                            # open (or its window is not up yet)
            self.refresh()
            return
        self._stale = True
        if self.picked_key is not None:                    # a template in use must still let go when it is gone
            self._templates = {t.key: (layer, t) for layer, t in self._all_templates()}
            if self.picked_key not in self._templates and not self._project_has(self.picked_key):
                self._release_if_gone(self.picked_key)

    def showEvent(self, event):  # noqa: N802 - Qt virtual
        if self._stale:
            self.refresh()                                 # before the first paint (and before an opening animation)
        super().showEvent(event)

    def swatch(self, tmpl):
        """The picture of a template's symbol, drawn once and kept until the symbology changes."""
        symbol = tmpl.symbol
        mark = ""
        with suppress(Exception):
            mark = symbol.dump() if symbol is not None else ""
        kept = self._swatches.get(tmpl.key)
        if kept is not None and kept[0] == mark:
            return kept[1]
        pixmap = symbol_pixmap(symbol)
        self._swatches[tmpl.key] = (mark, pixmap)
        return pixmap

    def _forget_swatches(self, layer_id, *_args):
        prefix = layer_id + "::"
        for key in [k for k in self._swatches if k == layer_id or k.startswith(prefix)]:
            del self._swatches[key]

    def _layers_removed(self, layer_ids):
        self._watched.difference_update(layer_ids)
        for layer_id in layer_ids:
            self._forget_swatches(layer_id)
            self._forgetters.pop(layer_id, None)
        self._schedule()

    def _project_cleared(self):
        self._swatches = {}
        self.favorites = set()
        self._release_if_gone(None)
        self._schedule()

    def _project_read(self):
        self.favorites = set(load_favorites())
        self._schedule()

    def _visibility_changed(self, *_args):
        self._schedule()                                   # a layer switched off in the Layers panel leaves the list

    def _watch(self, layer):
        if layer.id() in self._watched:
            return
        self._watched.add(layer.id())
        for signal in (layer.rendererChanged, layer.nameChanged, layer.editingStopped):
            signal.connect(self._schedule)
        forget = self._forgetters[layer.id()] = partial(self._forget_swatches, layer.id())
        for signal in (layer.rendererChanged, layer.styleChanged, layer.repaintRequested):
            signal.connect(forget)                         # any of these may mean a new symbol

    @staticmethod
    def _layer_visible(layer, root=None):
        """Ticked in the Layers panel, with every group above it ticked too."""
        node = (root or QgsProject.instance().layerTreeRoot()).findLayer(layer.id())
        if node is None:
            return False
        while node is not None and node.parent() is not None:        # the layer and every group above it
            if not node.itemVisibilityChecked():
                return False
            node = node.parent()
        return True

    def _all_templates(self):
        """[(layer, template)] for every digitizable layer, honouring the Show filter and the search."""
        text = self.search.text().strip().lower()
        root = QgsProject.instance().layerTreeRoot()
        out = []
        for layer in project_layers():
            self._watch(layer)
            if not self._layer_visible(layer, root):
                continue
            if self.show_filter in ("point", "line", "polygon"):
                if GEOMETRY_NAMES[layer.geometryType()].lower() != self.show_filter:
                    continue
            for tmpl in layer_templates(layer):
                if text:
                    hay = " ".join([tmpl.title, tmpl.label, layer.name(), tmpl.description, " ".join(tmpl.tags)])
                    if text not in hay.lower():
                        continue
                out.append((layer, tmpl))
        return out

    def _group(self, pairs):
        if self.group_by == "none":
            return [(None, sorted(pairs, key=lambda p: _natural(p[1].title)))] if pairs else []
        buckets = {}
        for layer, tmpl in pairs:
            if self.group_by == "geometry":
                names = [GEOMETRY_NAMES[layer.geometryType()]]
            elif self.group_by == "tag":
                names = tmpl.tags or ["(no tag)"]
            else:
                names = [layer.name()]
            for name in names:
                buckets.setdefault(name, []).append((layer, tmpl))
        groups = []
        for name in sorted(buckets, key=_natural):
            items = buckets[name]
            if self.group_by == "layer":
                items.sort(key=lambda p: 0)        # keep the symbology order inside a layer
            else:
                items.sort(key=lambda p: (_natural(p[0].name()), _natural(p[1].title)))
            groups.append((name, items))
        return groups

    def _current_groups(self):
        pairs = self._all_templates()
        if self.tabs.currentIndex() == 1:
            pairs = [p for p in pairs if p[1].key in self.favorites]
        return self._group(pairs)

    def refresh(self):
        if self.stack.currentIndex() == 1 and self.properties.editing:
            return                                         # do not pull the page away under the user
        self._stale = False
        everything = self._all_templates()
        self._templates = {t.key: (layer, t) for layer, t in everything}
        favorites_tab = self.tabs.currentIndex() == 1
        pairs = [p for p in everything if p[1].key in self.favorites] if favorites_tab else everything
        view = self.favorites_view if favorites_tab else self.templates_view
        if favorites_tab:
            empty = "No favorites yet. Right-click a template and choose Add to Favorites."
        elif self.search.text().strip():
            empty = "No template matches the search."
        else:
            empty = ("No layer can take new features. Add a point, line or polygon layer that is not "
                     "read-only, then its templates appear here.")
        view.fill(self._group(pairs), empty)
        if self.picked_key is not None:
            if self.picked_key not in self._templates and not self._project_has(self.picked_key):
                self._release_if_gone(self.picked_key)
            row = view.rows.get(self.picked_key)
            if row is not None:
                row.set_picked(True, self.picked_tool)

    def _project_has(self, key):
        """The picked template may only be hidden by the search / filter, not gone (or its layer switched off)."""
        layer_id = key.split("::", 1)[0]
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None or sip.isdeleted(layer) or not self._layer_visible(layer):
            return False
        return any(t.key == key for t in layer_templates(layer))

    def _release_if_gone(self, key):
        if self.picked_key is None or key not in (None, self.picked_key):
            return
        self.picked_key = None
        self.picked_tool = ""
        self.templateReleased.emit()

    # ------------------------------------------------------------------ picking
    def _view(self):
        return self.favorites_view if self.tabs.currentIndex() == 1 else self.templates_view

    def _pick_row(self, row, tool_id=""):
        view = self._view()
        old = view.rows.get(self.picked_key) if self.picked_key else None
        if old is not None and old is not row and not sip.isdeleted(old):
            old.set_picked(False)
        tmpl = row.tmpl
        wanted = tool_id or self.last_tool.get(tmpl.key) or tmpl.tool
        tool = default_tool(row.layer.geometryType(), wanted)
        self.picked_key, self.picked_tool = tmpl.key, tool
        self.last_tool[tmpl.key] = tool
        row.set_picked(True, tool)
        self.done.hide()
        self.templateActivated.emit(row.layer, tmpl, tool)

    def row_picked(self, row):
        if row.tmpl.key == self.picked_key and self.picked_tool:
            self.templateActivated.emit(row.layer, row.tmpl, self.picked_tool)    # re-arm the tool
            return
        self._pick_row(row)

    def row_tool_chosen(self, row, tool_id):
        self._pick_row(row, tool_id)

    def pick(self, layer, tmpl, tool_id=""):
        """Pick a template from code (tests, the controller)."""
        row = self._view().rows.get(tmpl.key)
        if row is None:
            self.tabs.setCurrentIndex(0)
            self.refresh()
            row = self.templates_view.rows.get(tmpl.key)
        if row is not None:
            self._pick_row(row, tool_id)
        return row

    def set_active_tool(self, active):
        """The map tool was switched on / off from outside: show it on the picked template's tool strip."""
        row = self._view().rows.get(self.picked_key) if self.picked_key else None
        if row is not None and not sip.isdeleted(row):
            row.check_tool(self.picked_tool if active else "")

    # ------------------------------------------------------------------ banner
    def show_completed(self, layer_name):
        self.done_text.setText(f"Create {layer_name} completed")
        self.done.show()

    # ------------------------------------------------------------------ properties page
    def row_properties(self, row):
        if row.tmpl.key != self.picked_key:
            self._pick_row(row)
        self.properties.edit(row.layer, row.tmpl)
        self.stack.setCurrentIndex(1)

    def _properties_done(self, saved):
        self.stack.setCurrentIndex(0)
        if saved:
            layer, tmpl = self.properties.layer, self.properties.template
            self.templateChanged.emit(layer, tmpl)
        self.refresh()
        if saved and self.picked_key == self.properties.template.key:
            layer, tmpl = self.properties.layer, self.properties.template
            self.templateActivated.emit(layer, tmpl, self.picked_tool)

    # ------------------------------------------------------------------ right-click
    def row_menu(self, row, pos):
        menu = QMenu(self)
        menu.addAction("Template properties...").triggered.connect(lambda: self.row_properties(row))
        key = row.tmpl.key
        if key in self.favorites:
            menu.addAction("Remove from Favorites").triggered.connect(lambda: self.set_favorite(key, False))
        else:
            menu.addAction("Add to Favorites").triggered.connect(lambda: self.set_favorite(key, True))
        menu.addSeparator()
        zoom = menu.addAction("Zoom to layer")
        zoom.triggered.connect(lambda: self._zoom_to(row.layer))
        try:
            menu.exec(pos)
        finally:
            menu.deleteLater()

    def set_favorite(self, key, on):
        if on:
            self.favorites.add(key)
        else:
            self.favorites.discard(key)
        save_favorites(self.favorites)
        self.refresh()

    def _zoom_to(self, layer):
        with suppress(Exception):
            self.iface.mapCanvas().setExtent(
                self.iface.mapCanvas().mapSettings().layerExtentToOutputExtent(layer, layer.extent()))
            self.iface.mapCanvas().refresh()

    # ------------------------------------------------------------------ teardown
    def disconnect_all(self):
        project = QgsProject.instance()
        for signal, slot in ((project.layersAdded, self._schedule), (project.layersRemoved, self._layers_removed),
                             (project.cleared, self._project_cleared), (project.readProject, self._project_read),
                             (project.layerTreeRoot().visibilityChanged, self._visibility_changed)):
            with suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
        for layer_id in list(self._watched):
            layer = project.mapLayer(layer_id)
            if layer is not None and not sip.isdeleted(layer):
                for signal in (layer.rendererChanged, layer.nameChanged, layer.editingStopped):
                    with suppress(TypeError, RuntimeError):
                        signal.disconnect(self._schedule)
                forget = self._forgetters.get(layer_id)
                if forget is not None:
                    for signal in (layer.rendererChanged, layer.styleChanged, layer.repaintRequested):
                        with suppress(TypeError, RuntimeError):
                            signal.disconnect(forget)
        self._forgetters = {}
        self._watched = set()
        self._timer.stop()

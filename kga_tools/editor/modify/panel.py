"""
The Modify Features pane: a dock with the editing tools, grouped by category. A tool's page has a back arrow, its name
and, at the right of that row, a small ? button that opens the tool's page of the documentation (khmergrs.com/docs/qgis/
modify_<tool id>) in the browser.

Laid out like ArcGIS Pro's Modify Features pane: a search box on top, All Tools / Favorites tabs, then
collapsible categories of icon tiles - as a grid (the default) or, with the buttons at the right of the tabs, as a list
(icon and name on one row each); the choice is remembered. The tools themselves are registered in tools.py; until some are,
the pane shows a placeholder. Right-click a tile to add it to / remove it from the favorites (favorites.py),
which are also the buttons on the KGA Editor toolbar.
"""

from contextlib import suppress

from ...gui.panel_bubble import ToolbarReveal
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...branding import docs_url, open_docs, themed_icon
from ..attributes.widgets import make_tab_bar
from .favorites import favorite_menu
from .tools import CATEGORIES

TITLE = "Modify Features"
VIEW_SETTING = "kga_editor/modify_view"
VIEWS = ("grid", "list")
TILE_WIDTH = 86
BLUE = "#0079c1"
QSS = f"""
QWidget#KgaModifyRoot {{ background: #f6f8f8; }}
QTabBar {{ background: transparent; font-size: 10pt; }}
QTabBar::tab {{
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 4px 6px 3px 6px; margin-right: 8px; color: #5f6368;
}}
QTabBar::tab:hover {{ color: {BLUE}; }}
QTabBar::tab:selected {{ color: #111111; border-bottom: 2px solid {BLUE}; }}
QLineEdit#KgaModifySearch {{
    background: white; border: 1px solid #a9adb1; border-radius: 2px; padding: 3px 4px; font-size: 10pt;
}}
QLineEdit#KgaModifySearch:focus {{ border-color: {BLUE}; }}
QToolButton#KgaModifyGroup {{
    border: none; background: transparent; color: #1b1b1b;
    font-size: 10pt; font-weight: 600; text-align: left; padding: 8px 2px 4px 2px;
}}
QToolButton#KgaModifyGroup:hover {{ color: {BLUE}; }}
QToolButton#KgaModifyTile {{
    border: 1px solid transparent; border-radius: 2px; background: transparent;
    font-size: 9pt; color: #1b1b1b; padding: 4px 2px;
}}
QToolButton#KgaModifyTile:hover {{ background: #e1ecf5; border-color: #b8d3e8; }}
QToolButton#KgaModifyTile[list="true"] {{ text-align: left; padding: 3px 6px; font-size: 10pt; }}
QToolButton#KgaModifyView {{
    border: 1px solid transparent; border-radius: 2px; background: transparent; padding: 2px; min-width: 20px;
}}
QToolButton#KgaModifyView:hover {{ background: #e1ecf5; border-color: #b8d3e8; }}
QToolButton#KgaModifyView:checked {{ background: #dfeaf4; border: 1px solid #8fb4d0; }}
QToolButton#KgaModifyTile[active="true"] {{ background: #dfeaf4; border: 1px solid #8fb4d0; }}
QLabel#KgaModifyEmpty {{ color: #5f6368; font-size: 10pt; padding: 14px 6px; }}
QScrollArea {{ border: none; background: transparent; }}
QToolButton#KgaModifyBack {{ border: 1px solid transparent; border-radius: 2px; padding: 3px; }}
QToolButton#KgaModifyBack:hover {{ background: #e1ecf5; border-color: #b8d3e8; }}
QLabel#KgaModifyTitle {{ font-size: 12pt; font-weight: 600; color: #1b1b1b; }}
QToolButton#KgaModifyHelp {{
    border: 1px solid #8fb4d0; border-radius: 9px; background: white; color: {BLUE};
    font-size: 9pt; font-weight: 700; min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px; padding: 0;
}}
QToolButton#KgaModifyHelp:hover {{ background: #e1ecf5; border-color: {BLUE}; }}
QLabel#KgaModifyHint {{ color: #5f6368; font-size: 10pt; }}
QLabel#KgaModifySection {{ font-size: 10pt; font-weight: 600; color: #1b1b1b; padding-top: 4px; }}
QLabel#KgaModifySelection {{ font-size: 10pt; color: #1b1b1b; background: #e4eef6; padding: 4px 6px; }}
QLabel#KgaModifyStatus {{ font-size: 10pt; color: #1f3a1f; }}
QPushButton#KgaModifyButton {{
    background: white; border: 1px solid #a9adb1; border-radius: 2px; padding: 4px 10px; font-size: 10pt;
}}
QPushButton#KgaModifyButton:hover {{ background: #e1ecf5; border-color: {BLUE}; }}
QPushButton#KgaModifyButton:disabled {{ color: #9aa0a6; }}
QPushButton#KgaModifyButton:checked {{ background: #dfeaf4; border-color: #8fb4d0; }}
QGroupBox {{
    border: 1px solid #d5dadd; border-radius: 2px; margin-top: 14px; padding-top: 6px;
    font-size: 10pt; font-weight: 600; color: #1b1b1b;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
QCheckBox {{ font-size: 10pt; }}
QDoubleSpinBox, QComboBox, QTableWidget {{ font-size: 10pt; }}
QToolButton#KgaModifyBanner {{
    background: #e6f0f8; border: 1px solid #b8d3e8; border-radius: 2px;
    padding: 5px 6px; text-align: left; color: #1f2d3a; font-size: 10pt;
}}
QToolButton#KgaModifyBanner:hover {{ background: #d7e8f5; }}
QToolButton#KgaModifyBanner:checked {{ background: #c5e1f5; border: 1px solid #7ab6e3; }}
QToolButton#KgaModifyToolTile {{
    background: #f6f8f8; border: 1px solid transparent; border-radius: 2px;
    padding: 5px 6px; text-align: left; color: #1b1b1b; font-size: 10pt;
}}
QToolButton#KgaModifyToolTile:hover {{ background: #e1ecf5; border-color: #b8d3e8; }}
QToolButton#KgaModifyToolTile:checked {{ background: #c5dff2; border: 1px solid #7ab6e3; }}
QTreeWidget#KgaModifySelectedTree {{ background: #fbfcfc; border: 1px solid #cfd6da; font-size: 10pt; outline: 0; }}
QCheckBox {{ font-size: 10pt; color: #1b1b1b; }}
"""


def view_icon(kind, color="#3a4a56"):
    """The grid (four squares) or list (three rows) icon of the view buttons."""
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    if kind == "grid":
        for x in (1, 9):
            for y in (1, 9):
                painter.drawRect(x, y, 6, 6)
    else:
        for y in (2, 7, 12):
            painter.drawRect(1, y, 3, 3)
            painter.drawRect(6, y, 9, 3)
    painter.end()
    return QIcon(pixmap)


class ModifyFeaturesPanel(ToolbarReveal, QgsDockWidget):

    toolRequested = pyqtSignal(str)             # a tool tile was clicked: its id
    backRequested = pyqtSignal()                # the back arrow of a tool page

    def __init__(self, iface, parent=None, favorites=None):
        super().__init__(TITLE, parent)
        self.setObjectName("KGAModifyFeaturesPanel")
        self.iface = iface
        self.page_widget = None
        view = str(QgsSettings().value(VIEW_SETTING, "grid"))
        self.view_mode = view if view in VIEWS else "grid"      # "grid" (default) or "list"
        self.doc_slug = None                    # the documentation page of the tool being shown
        self.collapsed = set()
        self.favorites = favorites              # FavoriteStore: the Favorites tab and the right-click menu
        self.active_tool = None                 # id of the tile whose map tool is armed
        self._laid_out_columns = None
        self._build_ui()
        self.scroll.viewport().installEventFilter(self)
        if self.favorites is not None:
            self.favorites.changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self):
        root = QWidget(self)
        root.setObjectName("KgaModifyRoot")
        root.setStyleSheet(QSS)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        self.search = QLineEdit(root)
        self.search.setObjectName("KgaModifySearch")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        outer.addWidget(self.search)

        self.tabs = make_tab_bar(["All Tools", "Favorites"], root)
        self.tabs.currentChanged.connect(self.refresh)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(2)
        bar.addWidget(self.tabs)
        bar.addStretch(1)
        self.view_buttons = {}
        group = QButtonGroup(root)
        for kind, tip in (("grid", "Show as grid"), ("list", "Show as list")):
            button = QToolButton(root)
            button.setObjectName("KgaModifyView")
            button.setIcon(view_icon(kind))
            button.setCheckable(True)
            button.setChecked(self.view_mode == kind)
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, k=kind: self.set_view(k))
            group.addButton(button)
            bar.addWidget(button)
            self.view_buttons[kind] = button
        outer.addLayout(bar)

        self.scroll = QScrollArea(root)
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName("KgaModifyRoot")
        self.vbox = QVBoxLayout(self.body)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(root)                  # 0: the tool tiles
        self.stack.addWidget(self._build_host())    # 1: the page of the active tool
        self.setWidget(self.stack)

    def _build_host(self):
        """Back arrow and tool name on top, the tool's own page (scrollable) below."""
        host = QWidget(self)
        host.setObjectName("KgaModifyRoot")
        host.setStyleSheet(QSS)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)
        head = QHBoxLayout()
        self.back_button = QToolButton(host)
        self.back_button.setObjectName("KgaModifyBack")
        self.back_button.setArrowType(Qt.ArrowType.LeftArrow)
        self.back_button.setToolTip("Back to all tools")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(lambda _c=False: self.backRequested.emit())
        self.title_label = QLabel("", host)
        self.title_label.setObjectName("KgaModifyTitle")
        self.help_button = QToolButton(host)
        self.help_button.setObjectName("KgaModifyHelp")
        self.help_button.setText("?")
        self.help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_button.clicked.connect(lambda _c=False: self.open_help())
        head.addWidget(self.back_button)
        head.addWidget(self.title_label, 1)
        head.addWidget(self.help_button)
        outer.addLayout(head)
        self.page_scroll = QScrollArea(host)
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.page_holder = QWidget()
        self.page_holder.setObjectName("KgaModifyRoot")
        self.page_layout = QVBoxLayout(self.page_holder)
        self.page_layout.setContentsMargins(0, 0, 0, 0)
        self.page_scroll.setWidget(self.page_holder)
        outer.addWidget(self.page_scroll, 1)
        return host

    # ------------------------------------------------------------------ tool page
    def show_page(self, title, widget, doc=None):
        """Replace the tile list by a tool's page. `doc` is the slug of the tool's documentation page (None: no help
        button)."""
        self._drop_page()
        self.title_label.setText(title)
        self.doc_slug = doc
        self.help_button.setVisible(bool(doc))
        if doc:
            self.help_button.setToolTip(f"Help: open the documentation for {title} in your browser\n{docs_url(doc)}")
        self.page_widget = widget
        self.page_layout.addWidget(widget)
        widget.show()
        self.stack.setCurrentIndex(1)

    def show_tiles(self):
        self._drop_page()
        self.doc_slug = None
        self.stack.setCurrentIndex(0)

    def open_help(self):
        """The ? button: the documentation page of the tool being shown."""
        if self.doc_slug:
            open_docs(self.doc_slug)

    def _drop_page(self):
        widget, self.page_widget = self.page_widget, None
        if widget is not None:
            self.page_layout.removeWidget(widget)
            widget.setParent(None)      # the tool keeps its page; it is shown again next time

    def showing_page(self):
        return self.stack.currentIndex() == 1

    # ------------------------------------------------------------------ content
    def set_view(self, mode):
        """Show the tools as a grid of tiles or as a list; remembered for next time."""
        if mode not in VIEWS:
            return
        self.view_mode = mode
        QgsSettings().setValue(VIEW_SETTING, mode)
        if not self.view_buttons[mode].isChecked():
            self.view_buttons[mode].setChecked(True)
        self.refresh()

    def refresh(self, *_args):
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)      # gone from the pane now, not at the next event-loop turn
                widget.deleteLater()
        text = self.search.text().strip().lower()
        mine_only = self.tabs.currentIndex() == 1
        favorite_ids = self.favorites.ids() if self.favorites is not None else []
        shown = 0
        self._laid_out_columns = self._columns()
        for name, tools in CATEGORIES:
            tools = [t for t in tools if (not text or text in t.label.replace("\n", " ").lower())
                     and (not mine_only or t.id in favorite_ids)]
            if tools:
                shown += 1
                self._add_group(name, tools)
        if not shown:
            if not CATEGORIES:
                message = "Modify tools will appear here."
            elif mine_only:
                message = "No favorites yet. Right-click a tool in All Tools and choose Add to favorites."
            else:
                message = "No tools match your search."
            empty = QLabel(message, self.body)
            empty.setObjectName("KgaModifyEmpty")
            empty.setWordWrap(True)
            self.vbox.addWidget(empty)
        self.vbox.addStretch(1)

    def tile_menu(self, tool_id):
        """The right-click menu of a tile: Add to / Remove from favorites."""
        return favorite_menu(self.favorites, tool_id, self)

    def _add_group(self, name, tools):
        folded = name in self.collapsed and not self.search.text().strip()
        head = QToolButton(self.body)
        head.setObjectName("KgaModifyGroup")
        head.setText(name)
        head.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        head.setArrowType(Qt.ArrowType.RightArrow if folded else Qt.ArrowType.DownArrow)
        head.setCursor(Qt.CursorShape.PointingHandCursor)
        head.clicked.connect(lambda _c=False, n=name: self._toggle_group(n))
        self.vbox.addWidget(head)
        if folded:
            return
        listing = self.view_mode == "list"
        grid_host = QWidget(self.body)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(1 if listing else 2)
        columns = self._columns()
        for i, tool in enumerate(tools):
            button = QToolButton(grid_host)
            button.setObjectName("KgaModifyTile")
            button.setText(tool.label.replace("\n", " ") if listing else tool.label)
            button.setIcon(themed_icon(tool.icon))
            button.setIconSize(QSize(22, 22) if listing else QSize(32, 32))
            button.setProperty("list", listing)
            favorite = self.favorites is not None and self.favorites.has(tool.id)
            button.setToolTip(tool.tip + ("\nFavorite" if favorite else "")
                              + "\nRight-click: add to / remove from favorites")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if listing
                                      else Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setProperty("active", tool.id == self.active_tool)
            button.setProperty("favorite", favorite)
            if self.favorites is not None:
                button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                button.customContextMenuRequested.connect(
                    lambda pos, t=tool.id, b=button: self.tile_menu(t).exec(b.mapToGlobal(pos)))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, t=tool.id: self.toolRequested.emit(t))
            if listing:                                                  # one tool per row, the whole width
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                grid.addWidget(button, i, 0)
            else:
                button.setFixedWidth(TILE_WIDTH - 4)
                grid.addWidget(button, i // columns, i % columns, Qt.AlignmentFlag.AlignTop)
        if not listing:
            grid.setColumnStretch(columns, 1)
        self.vbox.addWidget(grid_host)

    def set_active_tool(self, tool_id):
        """Highlight the tile of the armed map tool (None clears it)."""
        if tool_id != self.active_tool:
            self.active_tool = tool_id
            self.refresh()

    def _columns(self):
        if getattr(self, "view_mode", "grid") == "list":
            return 1                                    # a list does not reflow with the width
        return max(1, (self.scroll.viewport().width() - 8) // TILE_WIDTH)

    def eventFilter(self, obj, event):
        """Tiles reflow to the pane's width, as Pro's do (watches the scroll area's viewport)."""
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize \
                and self._laid_out_columns != self._columns():
            QTimer.singleShot(0, self.refresh)
        return super().eventFilter(obj, event)

    def _toggle_group(self, name):
        self.collapsed.symmetric_difference_update({name})
        self.refresh()

    def disconnect_all(self):
        if self.favorites is not None:
            with suppress(TypeError, RuntimeError):
                self.favorites.changed.disconnect(self.refresh)

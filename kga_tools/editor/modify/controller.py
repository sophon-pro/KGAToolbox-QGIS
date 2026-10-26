"""
Modify Features: the toolbar button and the pane behind it.

The button shows or hides the pane. The pane docks on the right and joins the Processing Toolbox's tab
group, like the Create Features and Attributes panes. It follows the Edit Session switch the same way
Create Features does.

Clicking a tile opens that tool inside the pane, as in ArcGIS Pro: the tile list gives way to the tool's page
(options, buttons, hints) with a back arrow on top. A page is one of
    a map tool's page      (Move, Rotate, Fillet ...): the tool is armed on the canvas while its page is open
    a command's page       (Generalize, Explode ...): options and a button that act on the selection
    an embedded window     (Divide, Clip, Merge ...): the KGA algorithm's own window, shown inside the pane
Going back (the arrow, closing the pane, picking another map tool) puts the tool away.
"""

import importlib
import os
from contextlib import suppress

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDockWidget

from .buffer import TOOL_CLASSES as BUFFER_TOOLS
from .clip import TOOL_CLASSES as CLIP_TOOLS
from .cogo import COMMAND_PAGES as COGO_COMMAND_PAGES
from .cogo import TOOL_CLASSES as COGO_TOOLS
from .copypaste import TOOL_CLASSES as COPYPASTE_TOOLS
from .divide import TOOL_CLASSES as DIVIDE_TOOLS
from .explode import TOOL_CLASSES as EXPLODE_TOOLS
from .favorites import FavoriteStore, FavoritesBar
from .merge import TOOL_CLASSES as MERGE_TOOLS
from .numbering import TOOL_CLASSES as NUMBERING_TOOLS
from .offset import TOOL_CLASSES as OFFSET_TOOLS
from .panel import TITLE, ModifyFeaturesPanel
from .parallel import TOOL_CLASSES as PARALLEL_TOOLS
from .polygons import TOOL_CLASSES as POLYGON_TOOLS
from .planarize import TOOL_CLASSES as PLANARIZE_TOOLS
from .reshape import COMMAND_PAGES as RESHAPE_COMMAND_PAGES
from .reshape import TOOL_CLASSES as RESHAPE_TOOLS
from .split import TOOL_CLASSES as SPLIT_TOOLS
from .split_cogo import TOOL_CLASSES as SPLITCOGO_TOOLS
from .culdesac import TOOL_CLASSES as CULDESAC_TOOLS
from .traverse import TOOL_CLASSES as TRAVERSE_TOOLS
from .update_cogo import TOOL_CLASSES as UPDATECOGO_TOOLS
from .tools import all_tools
from .transfer import TOOL_CLASSES as TRANSFER_TOOLS
from .transform import TOOL_CLASSES as ALIGNMENT_TOOLS

TOOL_CLASSES = {**ALIGNMENT_TOOLS, **RESHAPE_TOOLS, **TRANSFER_TOOLS, **COGO_TOOLS, **SPLIT_TOOLS,
                **DIVIDE_TOOLS, **CLIP_TOOLS, **EXPLODE_TOOLS,
                **PLANARIZE_TOOLS, **MERGE_TOOLS, **BUFFER_TOOLS,
                **PARALLEL_TOOLS, **POLYGON_TOOLS, **OFFSET_TOOLS, **COPYPASTE_TOOLS,
                **NUMBERING_TOOLS, **SPLITCOGO_TOOLS,
                **TRAVERSE_TOOLS, **UPDATECOGO_TOOLS, **CULDESAC_TOOLS}
COMMAND_PAGES = {**RESHAPE_COMMAND_PAGES, **COGO_COMMAND_PAGES}

#: tile id -> (module in kga_tools.algorithms, the window class that algorithm opens)
EMBEDDED = {
}
PACKAGE = __package__.rsplit(".", 2)[0]         # kga_tools

ICON = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "icons", "modifyFeature.png"))


class ModifyFeaturesController:

    def __init__(self, iface, run_algorithm=None, select_tool=None, vertex_tool=None, split_tool=None):
        self.iface = iface
        self._vertex_tool = vertex_tool         # starts QGIS's Vertex tool in all-layers mode (Edit Vertices)
        self._split_tool = split_tool           # arms the editor's Split (all layers) tool
        self._run_algorithm = run_algorithm     # the plugin's opener for Processing algorithms
        self.select_tool = select_tool          # the Edit Session select tool (a click picks a feature)
        self.tools = {}                         # key -> map tool, built the first time it is used
        self.command_pages = {}                 # key -> command page, built the first time it is used
        self.current = None                     # (kind, key, object) of the page being shown
        self.favorites = None                   # FavoriteStore (made in build)
        self.favorites_bar = None               # the favorite tools' buttons on the toolbar
        self.panel = None
        self.action = None
        self.toolbar = None

    def build(self, toolbar, before=None, favorites_before=None):
        """`before` is where the Modify Features button goes; `favorites_before` (optional) where the favorite
        tools' buttons go - without it there are no favorite buttons on the toolbar (the pane still has the tab)."""
        win = self.iface.mainWindow()
        self.favorites = FavoriteStore()
        self.panel = ModifyFeaturesPanel(self.iface, win, self.favorites)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel.hide()
        # QGIS restores the saved window layout after plugins load, which would undo a tabify done now.
        QTimer.singleShot(0, self.tabify_with_toolbox)

        self.action = self.panel.toggleViewAction()
        self.action.setObjectName("mActionKgaModifyFeatures")
        self.action.setIcon(QIcon(ICON))
        self.action.setText(TITLE)
        self.action.setToolTip("Modify Features (needs Edit Session: On) - move, reshape, split, merge ... "
                               "existing features")
        self.action.triggered.connect(self._toggled)
        self.panel.toolRequested.connect(self._run_tool)
        self.panel.backRequested.connect(self.back)
        self.panel.closed.connect(self._pane_closed)
        self.iface.mapCanvas().selectionChanged.connect(self._selection_changed)

        self.toolbar = toolbar
        if before is not None:
            toolbar.insertAction(before, self.action)
        else:
            toolbar.addAction(self.action)
        if favorites_before is not None:
            self.favorites_bar = FavoritesBar(self.favorites, toolbar, favorites_before, self.open_tile)

    def open_tile(self, tool_id):
        """A favorite's toolbar button: show the pane and open that tool in it."""
        if self.panel is None:
            return
        self.panel.show()
        self.panel.raise_()
        self._run_tool(tool_id)

    def set_enabled(self, on):
        """The button follows the Edit Session switch; switching it Off closes the pane."""
        if self.action is None:
            return
        if self.favorites_bar is not None:
            self.favorites_bar.set_enabled(on)
        self.action.setEnabled(on)
        if not on and self.panel is not None and not self.panel.isHidden():
            self.panel.close()

    def tabify_with_toolbox(self):
        """Put the pane in the Processing Toolbox's tab group (skipped if either was pulled out)."""
        if self.panel is None:
            return
        window = self.iface.mainWindow()
        toolbox = window.findChild(QDockWidget, "ProcessingToolbox")
        if toolbox is None or toolbox.isFloating() or self.panel.isFloating():
            return
        window.tabifyDockWidget(toolbox, self.panel)

    def remove(self):
        canvas = self.iface.mapCanvas()
        with suppress(TypeError, RuntimeError):
            canvas.selectionChanged.disconnect(self._selection_changed)
        self._leave_current()
        for tool in self.tools.values():
            if canvas.mapTool() is tool:
                canvas.unsetMapTool(tool)
                self.iface.actionPan().trigger()
            tool.dispose()
            tool.dispose_page()
            tool.deleteLater()
        self.tools = {}
        for page in self.command_pages.values():
            with suppress(Exception):
                page.dispose()
        self.command_pages = {}
        if self.favorites_bar is not None:
            self.favorites_bar.remove()
            self.favorites_bar = None
        if self.panel is not None:
            self.panel.disconnect_all()
            self.iface.removeDockWidget(self.panel)
            if self.toolbar is not None and self.action is not None:
                self.toolbar.removeAction(self.action)
            self.panel.deleteLater()
            self.panel = None
        self.action = None

    def _toggled(self, checked):
        if checked and self.panel is not None:
            self.panel.raise_()

    def _run_tool(self, tool_id):
        tool = next((t for t in all_tools() if t.id == tool_id), None)
        if tool is not None:
            tool.run(self)

    def _title(self, key):
        tool = next((t for t in all_tools() if t.id == key), None)
        return tool.label.replace("\n", " ") if tool is not None else key

    # ------------------------------------------------------------------ what the pane shows
    def _show(self, kind, key, obj, widget):
        self.current = (kind, key, obj)
        self.panel.show_page(self._title(key), widget, doc="modify_" + key)

    def _leave_current(self):
        """Put away whatever the pane is showing: disarm the map tool, close the embedded window."""
        current, self.current = self.current, None
        if current is None:
            return
        kind, key, obj = current
        canvas = self.iface.mapCanvas()
        if kind == "tool":
            if canvas.mapTool() is obj:
                canvas.unsetMapTool(obj)
                with suppress(Exception):
                    self.iface.actionPan().trigger()
        elif kind == "command":
            obj.deactivate()
        elif kind == "embedded":
            with suppress(RuntimeError):
                obj.finished.disconnect(self._embedded_finished)
            with suppress(RuntimeError):
                obj.close()
                obj.deleteLater()
        if self.panel is not None:
            self.panel.set_active_tool(None)

    def back(self):
        """The back arrow: put the tool away and show the tile list again."""
        self._leave_current()
        if self.panel is not None:
            self.panel.show_tiles()

    def _selection_changed(self, *_args):
        if self.current is not None and self.current[0] in ("tool", "command"):
            self.current[2].refresh_page()

    # ------------------------------------------------------------------ map tools
    def start_tool(self, key):
        """Arm a map tool and open its page; the pane highlights its tile while it is the active tool."""
        if self.current is not None and self.current[:2] != ("tool", key):
            self._leave_current()
        tool = self.tools.get(key)
        if tool is None:
            tool = TOOL_CLASSES[key](self.iface.mapCanvas(), self.iface, self.select_tool)
            tool.activated.connect(lambda k=key: self._tool_state(k))
            tool.deactivated.connect(lambda k=key: self._tool_state(k, False))
            self.tools[key] = tool
        canvas = self.iface.mapCanvas()
        self.current = ("tool", key, tool)
        if canvas.mapTool() is not tool:
            canvas.setMapTool(tool)
        self._show("tool", key, tool, tool.page())
        self.panel.set_active_tool(key)

    def _tool_state(self, key, active=True):
        if self.panel is None:
            return
        if active:
            self.panel.set_active_tool(key)
        elif self.current is not None and self.current[:2] == ("tool", key):
            self.back()                         # another map tool was picked elsewhere: this tool's page goes
        elif self.panel.active_tool == key:
            self.panel.set_active_tool(None)

    def _pane_closed(self):
        """Closing the pane puts the tool away, as in ArcGIS Pro (switching to another tab does not)."""
        self.back()

    # ------------------------------------------------------------------ commands
    def open_command(self, key):
        """Open the page of a tile that acts on the selection (Generalize, Explode ...)."""
        self._leave_current()
        page = self.command_pages.get(key)
        if page is None:
            page = COMMAND_PAGES[key](self)
            self.command_pages[key] = page
        self._show("command", key, page, page.page())
        page.activate()

    def edit_vertices(self):
        if self._vertex_tool is None:
            self.iface.messageBar().pushInfo(TITLE, "Edit Vertices needs the KGA Editor toolbar.")
            return
        self._vertex_tool()

    def start_split(self):
        if self._split_tool is None:
            self.iface.messageBar().pushInfo(TITLE, "Split needs the KGA Editor toolbar.")
            return
        self._split_tool()

    # ------------------------------------------------------------------ KGA algorithms, inside the pane
    def open_algorithm(self, key):
        """Show the KGA algorithm's own window inside the pane (or open it the usual way if it cannot be)."""
        if key not in EMBEDDED:
            return
        self._leave_current()
        module_name, class_name = EMBEDDED[key]
        module = importlib.import_module(f"{PACKAGE}.algorithms.{module_name}")
        dialog = getattr(module, class_name)(self.iface, self.panel)
        dialog.setWindowFlags(Qt.WindowType.Widget)           # an ordinary widget, not a window of its own
        dialog.setModal(False)
        dialog.finished.connect(self._embedded_finished)
        close_button = getattr(dialog, "close_button", None)
        if close_button is not None:            # its own Close button (runs after the window's clean-up)
            close_button.clicked.connect(self._embedded_finished)
        self._show("embedded", key, dialog, dialog)

    def _embedded_finished(self, *_args):
        """The window's own Close button: back to the tiles."""
        if self.current is not None and self.current[0] == "embedded":
            dialog, self.current = self.current[2], None
            with suppress(RuntimeError):
                dialog.deleteLater()
            if self.panel is not None:
                self.panel.show_tiles()
                self.panel.set_active_tool(None)

    def run_algorithm(self, alg_id):
        if self._run_algorithm is None:
            self.iface.messageBar().pushWarning(TITLE, f"Cannot open {alg_id} here.")
            return
        self._run_algorithm(alg_id)

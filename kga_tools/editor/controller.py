"""
KGA Editor: the second KGA toolbar.

Edit Session (select and edit on the map without choosing a layer first, Save / Discard for all
layers at once, every advanced editing tool on any layer) and Create Features (templates and precision
digitizing with a right-click construction menu), brought together on one toolbar.

Everything here is opt-in: the Edit Session switch starts Off, so KGA Toolbox changes nothing
about how QGIS edits until the user turns it on.
"""

import os

from qgis.core import Qgis, QgsApplication, QgsMapLayer, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMenu, QToolButton
from ..core.compat import enum_value

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

from .advanced import AdvancedTools
from .attributes.controller import AttributesController
from .edit_tools import SplitTool
from .follow import LayerUnderMouse
from .manager import EditSessionManager, option, set_option
from .panel import (
    TITLE,
    EditableLayersPanel,
    ask_save_or_discard,
    ask_stop_session,
    confirm,
    run_query_builder,
    show_save_errors,
)
from .selection import SelectTool, delete_selected
from .snapping import enable_all_snapping
from .create.controller import CreateFeaturesController
from .modify.controller import ModifyFeaturesController

PLUGIN_DIR = os.path.dirname(__file__)

_LAYER_TYPE_VECTOR = enum_value((Qgis, 'LayerType.Vector'), (QgsMapLayer, 'VectorLayer'))        # Qgis.LayerType: QGIS 3.30+


# ---------------------------------------------------------------- controller
class EditorController:
    """Builds the KGA Editor toolbar and owns everything on it (Edit Session + Create Features)."""

    def __init__(self, iface, parent_menu=None, menu_before=None, run_algorithm=None):
        self.iface = iface
        self.run_algorithm = run_algorithm   # the plugin's opener for KGA algorithms (Modify Features tiles)
        self.parent_menu = parent_menu     # the KGA Toolbox menu; the editor adds a submenu to it
        self.menu_before = menu_before     # the menu entry the submenu goes in front of (None = at the end)
        self.menu_action = None
        self.create = None
        self.modify = None
        self.attributes = None
        self.manager = None
        self.toolbar = None
        self.panel = None
        self.filter_action = None
        self.actions = []

    def initGui(self):
        self.manager = EditSessionManager()
        win = self.iface.mainWindow()
        theme = QgsApplication.getThemeIcon

        self.toolbar = self.iface.addToolBar("KGA Editor")
        self.toolbar.setObjectName("KGAEditorToolbar")
        self.toolbar.setToolTip("KGA Editor")

        icons = os.path.join(PLUGIN_DIR, "icons")
        self.icon_saved = QIcon(os.path.join(icons, "save_saved.svg"))
        self.icon_pending = QIcon(os.path.join(icons, "save_pending.svg"))
        self.icon_on = QIcon(os.path.join(icons, "session_on.svg"))
        self.icon_off = QIcon(os.path.join(icons, "session_off.svg"))

        self.session_action = QAction(self.icon_on, "Edit Session: On", win)
        self.session_action.setObjectName("mActionKgaEditorOnOff")
        self.session_action.setCheckable(True)
        self.session_action.setChecked(option("enabled"))
        self.session_action.triggered.connect(self.set_session_enabled)

        self.save_action = QAction(self.icon_saved, "All saved", win)
        self.save_action.setObjectName("mActionKgaEditorSave")
        self.save_action.triggered.connect(self.save_all)

        self.discard_action = QAction(theme("/mActionCancelAllEdits.svg"), "Discard Edits", win)
        self.discard_action.setObjectName("mActionKgaEditorDiscard")
        self.discard_action.setToolTip("Discard Edits - throw away all unsaved edits (layers stay editable)")
        self.discard_action.triggered.connect(self.discard_all)


        self.panel_action = QAction(QIcon(os.path.join(PLUGIN_DIR, "icons", "editable_layers.svg")), "Editable Layers", win)
        self.panel_action.setObjectName("mActionKgaEditorPanel")
        self.panel_action.setCheckable(True)
        self.panel_action.setToolTip("Show the Editable Layers panel")

        self.options_button = QToolButton()
        self.options_button.setObjectName("KgaEditorOptions")
        self.options_button.setIcon(theme("/mActionOptions.svg"))
        self.options_button.setToolTip("Edit Session options")
        self.options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.options_menu = QMenu(self.options_button)
        for key, text in (("new_layers_editable", "Make newly added layers editable"),
                          ("save_with_project", "Save edits when saving the project"),
                          ("confirm_save", "Confirm before saving edits"),
                          ("confirm_discard", "Confirm before discarding edits"),
                          ("active_follows_selection", "Selecting a feature makes its layer active"),
                          ("follow_mouse_edit", "QGIS editing tools work on the layer under the mouse")):
            a = self.options_menu.addAction(text)
            a.setCheckable(True)
            a.setChecked(option(key))
            a.toggled.connect(lambda on, k=key: set_option(k, on))
        self.options_button.setMenu(self.options_menu)

        self.select_tool = SelectTool(self.iface.mapCanvas(), self.manager, self.iface)
        self.select_action = QAction(QIcon(os.path.join(icons, "select_all_layers.svg")), "Select (all layers)", win)
        self.select_action.setObjectName("mActionKgaEditorSelect")
        self.select_action.setCheckable(True)
        self.select_action.setToolTip("Select (all layers) - click or drag on the map; works on every visible, "
                                      "selectable layer. Shift = add, Ctrl = remove, N = next overlapping, "
                                      "Delete = delete selected")
        self.select_tool.setAction(self.select_action)
        self.select_action.triggered.connect(lambda on: self._set_tool(self.select_tool, on))
        self.split_tool = SplitTool(self.iface.mapCanvas(), self.iface)
        self.split_action = QAction(QIcon(os.path.join(icons, "split_all_layers.svg")), "Split (all layers)", win)
        self.split_action.setObjectName("mActionKgaEditorSplit")
        self.split_action.setCheckable(True)
        self.split_action.setToolTip("Split (all layers) - draw a line; features it crosses are split in every "
                                     "visible, editable layer (only the selected ones if there is a selection)")
        self.split_tool.setAction(self.split_action)
        self.split_action.triggered.connect(lambda on: self._set_tool(self.split_tool, on))
        self.vertex_action = QAction(theme("/mActionVertexTool.svg"), "Vertex (all layers)", win)
        self.vertex_action.setObjectName("mActionKgaEditorVertex")
        self.vertex_action.setToolTip("Vertex (all layers) - QGIS's Vertex Tool in 'All Layers' mode: edit the "
                                      "vertices of any editable layer, whichever layer is highlighted")
        self.vertex_action.triggered.connect(self.vertex_tool_all_layers)
        self.delete_action = QAction(theme("/mActionDeleteSelected.svg"), "Delete Selected (all layers)", win)
        self.delete_action.setObjectName("mActionKgaEditorDelete")
        self.delete_action.setToolTip("Delete the selected features of every editable layer")
        self.delete_action.triggered.connect(lambda: delete_selected(self.iface))
        self.clear_action = QAction(theme("/mActionDeselectAll.svg"), "Clear Selection", win)
        self.clear_action.setObjectName("mActionKgaEditorClear")
        self.clear_action.setToolTip("Clear Selection - clear the selection in all layers")
        self.clear_action.triggered.connect(self.clear_selection)
        tb = self.toolbar                    # groups, left to right; a separator between each
        tb.addAction(self.session_action)
        tb.addSeparator()
        self._create_anchor = tb.addSeparator()         # Create Features goes before this one
        tb.addAction(self.select_action)
        tb.addAction(self.clear_action)                  # right after Select (all layers)
        self._advanced_start = tb.addSeparator()
        self._advanced_anchor = tb.addSeparator()       # the five drop-down groups go before it
        tb.addAction(self.save_action)
        tb.addAction(self.discard_action)
        tb.addAction(self.delete_action)                 # Save, Discard and Delete sit together
        self._cad_start = tb.addSeparator()
        self._cad_anchor = tb.addSeparator()            # QGIS's Advanced Digitizing switch goes before it
        tb.addAction(self.panel_action)
        tb.addSeparator()
        tb.addWidget(self.options_button)
        for action in (self.session_action, self.save_action):      # icon only; the tooltip says the state
            button = self.toolbar.widgetForAction(action)
            if button is not None:
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.actions = [self.session_action, self.save_action, self.discard_action, self.panel_action,
                        self.select_action, self.clear_action, self.vertex_action, self.split_action,
                        self.delete_action]

        self.advanced = AdvancedTools(self.iface, self.manager)
        self.advanced.build(self.toolbar, before=self._advanced_anchor, cad_before=self._cad_anchor)
        if not self.advanced.groups:
            self.toolbar.removeAction(self._advanced_start)        # nothing to separate
        if self.advanced.cad_action is None:
            self.toolbar.removeAction(self._cad_start)

        self.create = CreateFeaturesController(self.iface)
        self.create.build(self.toolbar, before=self._create_anchor)
        self.modify = ModifyFeaturesController(self.iface, self.run_algorithm, self.select_tool,
                                          self.vertex_tool_all_layers, self.start_split)
        fav_before = self._advanced_start               # the favorite tools sit between Select and the QGIS groups
        if fav_before not in self.toolbar.actions():     # removed when no QGIS groups exist
            fav_before = self._advanced_anchor
        self.modify.build(self.toolbar, before=self._create_anchor,         # right after Create Features
                          favorites_before=fav_before)
        self.attributes = AttributesController(self.iface, self.manager, before_edit=self._session_for_edit)
        self.attributes.build(self.toolbar, before=self._create_anchor)     # right after Modify Features
        self._build_menu()                   # after everything it lists exists

        self.panel = EditableLayersPanel(self)
        self.iface.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.panel)
        self.panel.hide()
        self.panel_action.toggled.connect(self.panel.setVisible)
        self.panel.visibilityChanged.connect(self.panel_action.setChecked)

        # Layers panel right-click: QGIS greys out "Filter..." while editing, so offer this instead
        self.filter_action = QAction(QgsApplication.getThemeIcon("/mActionFilter2.svg"),
                                     "Filter (pause editing)...", win)
        self.iface.addCustomActionForLayerType(self.filter_action, "", _LAYER_TYPE_VECTOR, True)
        self.filter_action.triggered.connect(self.filter_current_layer)

        self.manager.changed.connect(self._update_status)
        self.manager.projectSavedWithPendingEdits.connect(self._remind_pending)
        self.manager.editsSavedWithProject.connect(self._saved_with_project)
        self.follower = LayerUnderMouse(self.iface, self.manager)
        self.follower.install()
        QgsProject.instance().readProject.connect(self._project_read)
        if option("enabled"):
            self.manager.start()
            enable_all_snapping()
        self._update_status()

    def _build_menu(self):
        """A 'KGA Editor Toolbar' show/hide checkbox in the KGA Toolbox menu."""
        if self.parent_menu is None:
            return
        self.menu_action = self.toolbar.toggleViewAction()
        self.menu_action.setText("KGA Editor Toolbar")
        if self.menu_before is not None:
            self.parent_menu.insertAction(self.menu_before, self.menu_action)
        else:
            self.parent_menu.addAction(self.menu_action)

    def unload(self):
        canvas = self.iface.mapCanvas()
        if self.attributes is not None:
            self.attributes.remove()
            self.attributes = None
        if self.modify is not None:
            self.modify.remove()
            self.modify = None
        if self.create is not None:
            self.create.remove()
            self.create = None
        if getattr(self, "advanced", None) is not None:
            self.advanced.remove()
            self.advanced.deleteLater()
            self.advanced = None
        if getattr(self, "follower", None) is not None:
            self.follower.uninstall()
            self.follower.deleteLater()
            self.follower = None
        if self.manager is not None:
            try:
                self.manager.changed.disconnect(self._update_status)
            except (TypeError, RuntimeError):
                pass
        try:
            QgsProject.instance().readProject.disconnect(self._project_read)
        except (TypeError, RuntimeError):
            pass
        for tool in (getattr(self, "select_tool", None), getattr(self, "split_tool", None)):
            if tool is not None:
                if canvas.mapTool() is tool:
                    canvas.unsetMapTool(tool)
                    self.iface.actionPan().trigger()
                tool.dispose()
                tool.deleteLater()
        self.select_tool = self.split_tool = None
        if self.manager:
            self.manager.stop()
        if self.filter_action:
            self.iface.removeCustomActionForLayerType(self.filter_action)
            self.filter_action.deleteLater()
        if self.panel:
            self.panel.disconnect_all()
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
        if self.menu_action is not None and self.parent_menu is not None:
            self.parent_menu.removeAction(self.menu_action)
            self.menu_action = None
        for a in self.actions:
            a.deleteLater()
        if self.toolbar:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        self.manager = self.panel = self.toolbar = self.filter_action = None

    # ------------------------------------------------------------ on / off
    def set_session_enabled(self, on):
        """The On/Off switch. Off = every layer leaves edit mode, QGIS behaves as usual."""
        if on:
            set_option("enabled", True)
            self.manager.start()
            enable_all_snapping()
            self.panel_action.setChecked(True)      # shows the panel: which layers are editable / selectable
            self.panel.show()
            self.panel.raise_()
            self.iface.mainWindow().statusBar().showMessage(
                "Edit Session ON: editable layers are ready, snapping is on (all types) - "
                "edit, then Save Edits.", 6000)
        else:
            pending = self.manager.pending_layers()
            choice = "discard"
            if pending:
                choice = ask_stop_session(self.iface.mainWindow(), [lyr.name() for lyr in pending])
                if choice == "cancel":
                    self.session_action.setChecked(True)
                    return
            errors = self.manager.end_session(choice)
            if errors:
                show_save_errors(self.iface.mainWindow(), errors)
                self.session_action.setChecked(True)
                self._update_status()
                return
            canvas = self.iface.mapCanvas()
            for tool in (self.split_tool,):          # Select stays available when the session is Off
                if canvas.mapTool() is tool:
                    canvas.unsetMapTool(tool)
                    self.iface.actionPan().trigger()
            self.panel.hide()
            set_option("enabled", False)
            self.iface.mainWindow().statusBar().showMessage(
                "Edit Session OFF: layers are back to normal QGIS editing (Toggle Editing).", 6000)
        self._update_status()

    # ------------------------------------------------------------ actions
    def _set_tool(self, tool, on):
        canvas = self.iface.mapCanvas()
        if on:
            canvas.setMapTool(tool)
        elif canvas.mapTool() is tool:
            self.iface.actionPan().trigger()

    def start_split(self):
        """Arm the Split (all layers) tool - the Modify Features pane's Split tile uses it."""
        if not self.split_action.isChecked():
            self.split_action.trigger()

    def vertex_tool_all_layers(self):
        """Start QGIS's own Vertex Tool in 'All Layers' mode (never the 'Current Layer' variant)."""
        action = self.iface.actionVertexTool()
        if not action.isEnabled():
            # QGIS enables its editing tools only when the highlighted layer is editable:
            # make an editable layer active first (the tool still works on ALL editable layers).
            layer = next((lyr for lyr in self.manager.selectable_layers(self.iface.mapCanvas())
                          if lyr.isEditable()), None)
            if layer is None:
                layer = next((lyr for lyr in self.manager.vector_layers() if lyr.isEditable()), None)
            if layer is not None:
                self.iface.setActiveLayer(layer)
        if action.isEnabled():
            action.trigger()
            self.iface.mainWindow().statusBar().showMessage(
                "Vertex (all layers): hover over any editable layer's feature and drag its vertices.", 6000)
        else:
            self.iface.mainWindow().statusBar().showMessage(
                "Vertex (all layers): no editable layer - tick a layer in the Editable Layers panel.", 6000)

    def _session_for_edit(self, layer):
        """
        A value typed (and confirmed) in the Attributes pane while Edit Session is Off: turn the
        session On first, then tell the user, so the edit is an ordinary unsaved edit with Save and
        Discard ready.
        """
        if self.manager is None or self.manager.is_running():
            return
        self.session_action.trigger()                       # the same path as clicking the toggle
        if self.manager.is_running():
            self.iface.messageBar().pushMessage(
                TITLE, "Edit Session turned on: you changed '{}', so editable layers are now in edit mode. "
                       "Click Save Edits to write your changes, or Discard Edits to throw them away."
                .format(layer.name()), Qgis.MessageLevel.Info, 10)

    def clear_selection(self):
        for layer in self.manager.vector_layers():
            if layer.selectedFeatureCount():
                layer.removeSelection()

    def ask_turn_off(self, layer):
        return ask_save_or_discard(self.iface.mainWindow(), layer, "turning editing off")

    def save_all(self):
        pending = self.manager.pending_layers()
        if not pending:
            self.iface.mainWindow().statusBar().showMessage("Edit Session: nothing to save - all edits are saved.", 4000)
            return
        if option("confirm_save") and not confirm(self.iface.mainWindow(),
                                                 f"Save edits in {len(pending)} layer(s)?"):
            return
        saved, errors = self.manager.save_all()
        if errors:
            show_save_errors(self.iface.mainWindow(), errors)
        self.iface.mainWindow().statusBar().showMessage(f"Edit Session: edits saved ({saved} layer(s)).", 5000)

    def discard_all(self):
        pending = self.manager.pending_layers()
        if not pending:
            return
        if option("confirm_discard") and not confirm(
                self.iface.mainWindow(),
                f"Discard all unsaved edits in {len(pending)} layer(s)?\nThis cannot be undone."):
            return
        n = self.manager.discard_all()
        self.iface.mainWindow().statusBar().showMessage(f"Edit Session: edits discarded ({n} layer(s)).", 5000)

    def save_layer(self, layer):
        ok, errors = self.manager.save_layer(layer)
        if not ok:
            show_save_errors(self.iface.mainWindow(), {layer.name(): errors})

    def discard_layer(self, layer):
        if option("confirm_discard") and not confirm(self.iface.mainWindow(),
                                                    f"Discard unsaved edits of '{layer.name()}'?"):
            return
        self.manager.discard_layer(layer)

    def filter_current_layer(self):
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsVectorLayer):
            return
        self.manager.run_paused(
            layer, lambda: run_query_builder(layer, self.iface.mainWindow()),
            ask=lambda lyr: ask_save_or_discard(self.iface.mainWindow(), lyr, "setting a filter"))

    # ------------------------------------------------------------ status
    def _update_status(self):
        if self.manager is None:
            return
        on = self.manager.is_running()
        self.session_action.setChecked(on)
        self.session_action.setIcon(self.icon_on if on else self.icon_off)
        self.session_action.setText("Edit Session: On" if on else "Edit Session: Off")
        self.session_action.setToolTip(
            "Edit Session: On\nClick to turn it off (back to normal QGIS editing)" if on else
            "Edit Session: Off\nClick to turn it on (normal QGIS editing until then)")
        for a in (self.vertex_action, self.split_action, self.delete_action, self.panel_action):
            a.setEnabled(on)
        for a in (self.select_action, self.clear_action):      # selecting never needs the session
            a.setEnabled(True)
        self.filter_action.setEnabled(on)
        if self.create is not None:
            self.create.set_enabled(on)
        if self.modify is not None:
            self.modify.set_enabled(on)
        if getattr(self, "advanced", None) is not None:
            self.advanced.update_enabled(on)

        pending = self.manager.pending_layers() if on else []
        n = len(pending)
        if not on:
            text, tip, icon = "Edit Session off", "Turn Edit Session on to use Save Edits", self.icon_saved
        elif not self.manager.managed():
            text, tip, icon = "QGIS transaction mode", "This project uses a QGIS transaction mode", self.icon_saved
        elif n == 0:
            text, tip, icon = "All saved", "All edits are saved", self.icon_saved
        else:
            text = f"Save Edits ({n} layer{'s' if n != 1 else ''})"
            tip = "Click to save the unsaved edits in:\n" + "\n".join(lyr.name() for lyr in pending)
            icon = self.icon_pending
        self.save_action.setText(text)
        self.save_action.setToolTip(text + "\n" + tip)          # icon only: the state is in the tooltip
        self.save_action.setIcon(icon)
        self.save_action.setEnabled(on)          # stays readable ("All saved" with a green tick)
        self.discard_action.setEnabled(on and n > 0)

    def _project_read(self, *args):
        """A project was opened while Edit Session is On: snapping is part of the project, so set it again."""
        if self.manager is not None and self.manager.is_running():
            QTimer.singleShot(0, self._snapping_for_open_project)

    def _snapping_for_open_project(self):
        if self.manager is not None and self.manager.is_running():
            enable_all_snapping()

    def _remind_pending(self, n):
        self.iface.messageBar().pushInfo(
            TITLE, f"Project saved. {n} layer(s) still have unsaved edits - click Save Edits to write them.")

    def _saved_with_project(self, saved, errors):
        if errors:
            show_save_errors(self.iface.mainWindow(), errors)
        else:
            self.iface.mainWindow().statusBar().showMessage(
                f"Edit Session: edits saved with the project ({saved} layer(s)).", 5000)

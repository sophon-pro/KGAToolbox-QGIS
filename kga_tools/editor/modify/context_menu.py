"""
Modify Features tools on the map's right-click menu: Edit Vertices, Reshape, Split, Divide, Merge.

They show up when features are selected. Clicking one opens the tool in the Modify Features pane, like
the pane's own tile. The tools need the Edit Session, so if it is Off it is turned on first and a message
says so (with a "Don't show this again" box; Edit Session options brings the message back).
"""

from contextlib import suppress

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QCheckBox, QMessageBox

from ...branding import themed_icon
from ..manager import option, set_option
from .tools import all_tools

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

#: tile ids, in menu order; Merge needs two features
MENU_TOOLS = ("edit_vertices", "reshape", "split", "divide", "merge")
NOTICE_OPTION = "context_session_notice"


def selected_count():
    return sum(lyr.selectedFeatureCount() for lyr in QgsProject.instance().mapLayers().values()
               if isinstance(lyr, QgsVectorLayer) and lyr.isValid())


class ContextMenuTools:

    def __init__(self, editor):
        self.editor = editor                    # the EditorController
        self.iface = editor.iface
        self._connected = False

    def install(self):
        self.iface.mapCanvas().contextMenuAboutToShow.connect(self._canvas_menu)
        self._connected = True

    def uninstall(self):
        if self._connected:
            with suppress(TypeError, RuntimeError):
                self.iface.mapCanvas().contextMenuAboutToShow.disconnect(self._canvas_menu)
            self._connected = False

    def _canvas_menu(self, menu, _event):
        with suppress(Exception):       # never break a menu over this
            if self.editor.modify is None or self.editor.manager is None:
                return
            count = selected_count()
            if not count:
                return
            tools = {t.id: t for t in all_tools()}
            first = menu.actions()[0] if menu.actions() else None
            added = False
            for key in MENU_TOOLS:
                tool = tools.get(key)
                if tool is None:
                    continue
                text = tool.label.replace("\n", " ")
                action = QAction(themed_icon(tool.icon), text, menu)
                if key == "merge" and count < 2:
                    action.setEnabled(False)
                    action.setToolTip("Select at least two features to merge")
                else:
                    action.setToolTip(tool.tip)
                action.triggered.connect(lambda _c=False, k=key: self.run(k))
                menu.insertAction(first, action)
                added = True
            if added:
                menu.insertSeparator(first)

    def run(self, key):
        editor = self.editor
        if editor.manager is None or editor.modify is None:
            return
        if not editor.manager.is_running():
            editor.session_action.trigger()                  # the same path as clicking the toggle
            if not editor.manager.is_running():
                return                                       # the switch was refused
            self._tell_session_on(key)
        editor.modify.open_tile(key)

    def _tell_session_on(self, key):
        if not option(NOTICE_OPTION):
            return
        name = next((t.label.replace("\n", " ") for t in all_tools() if t.id == key), key)
        box = QMessageBox(self.iface.mainWindow())
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Edit Session")
        box.setText("Edit Session is turned on.")
        box.setInformativeText(
            "{} needs it, so editable layers are now in edit mode. Click Save Edits when you are done, "
            "or Discard Edits to throw your changes away.".format(name))
        check = QCheckBox("Don't show this again", box)
        box.setCheckBox(check)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        if check.isChecked():
            set_option(NOTICE_OPTION, False)

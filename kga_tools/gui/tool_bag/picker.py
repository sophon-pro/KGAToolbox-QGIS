"""Collecting tools directly from the QGIS interface.

* PickMode            - while active, clicking any toolbar button or menu item
                        anywhere in QGIS adds that tool to a workspace instead
                        of running it. Esc / "Done" ends the mode.
* NativeToolbarHook   - Shift + right-click on a button of any QGIS toolbar
                        shows an "Add to workspace" menu.
"""

from contextlib import suppress

from qgis.PyQt.QtCore import QEvent, QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import (QApplication, QMenu, QMenuBar, QPushButton, QToolBar,
                                 QToolButton)

from .compat import (OWN_PREFIX, QWidgetAction, alive, event_pos, is_own_object,
                     message_level, ptr)


def _inside_message_bar(widget):
    w, depth = widget, 0
    while w is not None and depth < 30:
        if w.inherits("QgsMessageBar") or w.inherits("QgsMessageBarItem"):
            return True
        w = w.parentWidget()
        depth += 1
    return False


def _toolbar_ancestor(widget):
    w, depth = widget.parentWidget(), 0
    while w is not None and depth < 10:
        if isinstance(w, QToolBar):
            return w
        w = w.parentWidget()
        depth += 1
    return None


class PickMode(QObject):
    picked = pyqtSignal(object)    # QAction
    rejected = pyqtSignal(str)     # message
    finished = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setObjectName(OWN_PREFIX + "_picker")
        self.iface = iface
        self.active = False
        self.target_name = ""
        self._msg_item = None
        self._cursor = False

    def start(self, target_name):
        self.target_name = target_name
        if self.active:
            return
        self.active = True
        QApplication.instance().installEventFilter(self)
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.CrossCursor))
        self._cursor = True
        bar = self.iface.messageBar()
        try:
            widget = bar.createMessage(
                "Pick tools",
                f"Click a QGIS toolbar button or menu item to put it in your {target_name}. "
                "Click anywhere else or press Esc to cancel.")
            btn = QPushButton("Cancel", widget)
            btn.clicked.connect(self.stop)
            widget.layout().addWidget(btn)
            self._msg_item = bar.pushWidget(widget, message_level("Info"), 0)
            if alive(self._msg_item) and hasattr(self._msg_item, "destroyed"):
                self._msg_item.destroyed.connect(self._on_message_closed)
        except Exception:
            self._msg_item = None

    def _on_message_closed(self, *_):
        self._msg_item = None
        if self.active:
            self.stop()

    def stop(self):
        if not self.active:
            return
        self.active = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self._cursor:
            QApplication.restoreOverrideCursor()
            self._cursor = False
        item, self._msg_item = self._msg_item, None
        if alive(item):
            with suppress(Exception):
                item.destroyed.disconnect(self._on_message_closed)
            with suppress(Exception):
                self.iface.messageBar().popWidget(item)
        self.finished.emit()

    # ------------------------------------------------------------ event filter
    def eventFilter(self, obj, event):
        """One pick per click on ＋: the first QGIS tool clicked is collected and the
        mode ends by itself. Clicking anywhere else (map, panels, dialogs) simply
        cancels pick mode and the click works normally - no Esc needed."""
        t = event.type()
        if t == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            if not isinstance(QApplication.activePopupWidget(), QMenu):
                self.stop()
                return True
            return False
        if t not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                     QEvent.Type.MouseButtonDblClick):
            return False
        if not hasattr(obj, "isWidgetType") or not obj.isWidgetType():
            return False
        press = t == QEvent.Type.MouseButtonPress
        left = event.button() == Qt.MouseButton.LeftButton

        if isinstance(obj, QMenu):
            if is_own_object(obj) or not left:
                return False
            action = obj.actionAt(event_pos(event))
            if action is None or action.isSeparator() or action.menu() is not None:
                return False  # let sub-menus open normally
            if t == QEvent.Type.MouseButtonRelease:
                self._close_popups()
                self._accept(action)
            return True

        if isinstance(obj, QMenuBar):
            return False  # opening a menu keeps pick mode on

        if is_own_object(obj):
            if obj.property("ctw_pick_button"):
                return False  # the ＋ button toggles the mode itself
            if press:
                self.stop()
            return False

        if _inside_message_bar(obj):
            return False

        if left and isinstance(obj, QToolButton) and _toolbar_ancestor(obj) is not None:
            menu = obj.menu()
            if menu is not None:
                instant = obj.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
                on_arrow = event_pos(event).x() > obj.width() - 14
                if instant or on_arrow:
                    return False  # open the drop-down, the user picks inside it
            obj.setDown(False)
            if press:
                action = obj.defaultAction()
                if action is None:
                    self.rejected.emit("This button is not a QGIS tool and cannot be added.")
                    self.stop()
                else:
                    self._accept(action)
            return True

        # Anything else: leave pick mode and let the click do its normal job.
        if press:
            self.stop()
        return False

    @staticmethod
    def _close_popups():
        popup, guard = QApplication.activePopupWidget(), 0
        while popup is not None and guard < 10:
            popup.close()
            popup = QApplication.activePopupWidget()
            guard += 1

    def _accept(self, action):
        if isinstance(action, QWidgetAction):
            self.rejected.emit("Embedded widgets (combo boxes, spin boxes) cannot be added.")
        else:
            self.picked.emit(action)
        self.stop()


class NativeToolbarHook(QObject):
    """Shift + right-click on any QGIS toolbar button -> 'Add to workspace' menu."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName(OWN_PREFIX + "_toolbarhook")
        self.ctrl = controller
        self._hooked = {}

    def attach(self, toolbars):
        for tb in toolbars:
            key = ptr(tb)
            if key in self._hooked and alive(self._hooked[key]):
                continue
            tb.installEventFilter(self)
            self._hooked[key] = tb

    def detach(self):
        for tb in self._hooked.values():
            if alive(tb):
                tb.removeEventFilter(self)
        self._hooked.clear()

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.ContextMenu or not isinstance(obj, QToolBar):
            return False
        if not (QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier):
            return False
        action = obj.actionAt(event.pos())
        if action is None:
            return False
        if isinstance(action, QWidgetAction):
            w = obj.widgetForAction(action)
            action = w.defaultAction() if isinstance(w, QToolButton) else None
            if action is None:
                return False
        self.ctrl.show_add_menu_for_action(action, event.globalPos())
        return True

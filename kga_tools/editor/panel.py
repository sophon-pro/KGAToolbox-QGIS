"""
The Editable Layers dock panel and the question dialogs the KGA Editor uses.
"""

from qgis.core import QgsProject
from qgis.gui import QgsQueryBuilder
from qgis.PyQt.QtCore import QRect, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QStyle,
    QStyleOptionButton,
    QStyleOptionHeader,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .manager import can_edit

TITLE = "KGA Editor"


# ---------------------------------------------------------------- dialogs (module level: easy to test)
def ask_save_or_discard(parent, layer, action_text):
    """'save' | 'discard' | 'cancel' for a layer that holds unsaved edits."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(TITLE)
    box.setText(f"'{layer.name()}' has unsaved edits.")
    box.setInformativeText(f"Save them before {action_text}?")
    save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    clicked = box.clickedButton()
    return "save" if clicked is save else ("discard" if clicked is discard else "cancel")


def confirm(parent, text):
    buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    return QMessageBox.question(parent, TITLE, text, buttons) == QMessageBox.StandardButton.Yes


def show_save_errors(parent, errors):
    lines = []
    for name, errs in errors.items():
        lines.append(f"{name}:")
        lines.extend("    " + e.strip() for e in errs if e.strip())
    QMessageBox.warning(parent, TITLE, "Some edits could not be saved:\n\n" + "\n".join(lines))


def ask_stop_session(parent, layer_names):
    """Turning Edit Session OFF with unsaved edits: 'save' | 'discard' | 'cancel'."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(TITLE)
    box.setText(f"{len(layer_names)} layer(s) have unsaved edits.")
    box.setInformativeText("Save them before turning Edit Session off?\n\n" + "\n".join(layer_names))
    save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    clicked = box.clickedButton()
    return "save" if clicked is save else ("discard" if clicked is discard else "cancel")


def run_query_builder(layer, parent):
    dlg = QgsQueryBuilder(layer, parent)
    dlg.exec()


# ---------------------------------------------------------------- panel
class CheckHeader(QHeaderView):
    """Header with a tri-state checkbox left of the label in chosen columns (select all / clear all)."""

    toggled = pyqtSignal(int)

    def __init__(self, columns, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._columns = set(columns)
        self._states = {}
        self.setSectionsClickable(True)

    def set_state(self, column, state):
        self._states[column] = state
        self.viewport().update()

    def _box_rect(self, rect):
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth)
        return QRect(rect.left() + 4, rect.top() + (rect.height() - size) // 2, size, size)

    def paintSection(self, painter, rect, logical_index):
        if logical_index not in self._columns:
            super().paintSection(painter, rect, logical_index)
            return
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.rect = rect
        opt.section = logical_index
        opt.text = ""
        # CE_HeaderSection (background only): CE_Header on the Windows style blanks later drawing
        self.style().drawControl(QStyle.ControlElement.CE_HeaderSection, opt, painter, self)
        box = self._box_rect(rect)
        cb = QStyleOptionButton()
        cb.rect = box
        state = self._states.get(logical_index, Qt.CheckState.Unchecked)
        cb.state = QStyle.StateFlag.State_Enabled | (
            QStyle.StateFlag.State_On if state == Qt.CheckState.Checked else
            QStyle.StateFlag.State_NoChange if state == Qt.CheckState.PartiallyChecked else
            QStyle.StateFlag.State_Off)
        self.style().drawControl(QStyle.ControlElement.CE_CheckBox, cb, painter, self)
        text = self.model().headerData(logical_index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        text_rect = rect.adjusted(box.width() + 10, 0, -4, 0)
        painter.save()
        painter.setPen(self.palette().color(self.palette().ColorRole.ButtonText))
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         str(text or ""))
        painter.restore()

    def mousePressEvent(self, event):
        index = self.logicalIndexAt(event.pos())
        if index in self._columns:
            self.toggled.emit(index)
            event.accept()
            return
        super().mousePressEvent(event)


class EditableLayersPanel(QDockWidget):
    """Like 'List By Editing': a checkbox per layer controls whether it can be edited."""

    def __init__(self, plugin):
        super().__init__("Editable Layers", plugin.iface.mainWindow())
        self.setObjectName("KgaEditorEditableLayersPanel")
        self.plugin = plugin
        self.manager = plugin.manager
        self._filling = False
        self._listed = []
        body = QWidget(self)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        self.info = QLabel("Tick the layers you want to edit or select. Right-click for more options.")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.tree = QTreeWidget(body)
        self.header = CheckHeader((0, 1), self.tree)
        self.tree.setHeader(self.header)
        self.tree.setHeaderLabels(["Layer (editable)", "Selectable", "Status"])
        self.header.toggled.connect(self._header_toggled)
        self.tree.setRootIsDecorated(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemChanged.connect(self._item_changed)
        lay.addWidget(self.tree)
        self.setWidget(body)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(60)
        self._refresh_timer.timeout.connect(self._refresh_now)
        self.manager.changed.connect(self.refresh)
        root = QgsProject.instance().layerTreeRoot()
        root.addedChildren.connect(self.refresh)
        root.removedChildren.connect(self.refresh)
        root.visibilityChanged.connect(self._map_switched)       # Map Tabs switches maps by toggling their groups
        self._stale = False         # the project changed while the panel was closed: rebuild when it is shown
        self._refresh_now()

    def disconnect_all(self):
        try:
            self.manager.changed.disconnect(self.refresh)
            root = QgsProject.instance().layerTreeRoot()
            root.addedChildren.disconnect(self.refresh)
            root.removedChildren.disconnect(self.refresh)
            root.visibilityChanged.disconnect(self._map_switched)
        except (TypeError, RuntimeError):
            pass

    def _map_switched(self, *args):
        """A group was shown or hidden: when that changed which map is active the list follows it."""
        if self.isHidden():
            self._stale = True      # nobody is looking: no walk over every layer for each tick box
            return
        ids = [layer.id() for layer in self.manager.panel_layers()]
        if ids != self._listed:
            QTimer.singleShot(0, self._refresh_now)

    def refresh(self, *args):
        """Rebuild the list once the burst of tree changes (adding 40 layers) is over.

        A closed panel is not rebuilt (planning every layer of a big project took tens of milliseconds
        for each layer added); it rebuilds when it is shown."""
        if self.isHidden():                 # closed (a panel whose window is not up yet counts as open)
            self._stale = True
            return
        self._refresh_timer.start()

    def showEvent(self, event):  # noqa: N802 - Qt virtual
        if self._stale:
            self._refresh_now()
        super().showEvent(event)

    def _refresh_now(self):
        self._stale = False
        self._filling = True
        try:
            self.tree.clear()
            managed = self.manager.managed()
            self.info.setText("Tick the layers you want to edit or select. Right-click for more options." if managed else
                              "This project uses a QGIS transaction mode (Project Properties > Data Sources), "
                              "so QGIS manages editing itself. Set it to 'Local Edit Buffer' to use Edit Session.")
            plan = self.manager.plan() if managed else None
            for layer in self.manager.panel_layers():
                item = QTreeWidgetItem([layer.name(), "", self.manager.status(layer, plan)])
                item.setData(0, Qt.ItemDataRole.UserRole, layer.id())
                flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                if can_edit(layer) and managed:
                    flags |= Qt.ItemFlag.ItemIsEnabled
                if layer.isSpatial():
                    flags |= Qt.ItemFlag.ItemIsEnabled      # the Selectable box works for every spatial layer
                item.setFlags(flags)
                if can_edit(layer) and managed:          # no "editable" box at all for read-only sources
                    item.setCheckState(0, Qt.CheckState.Checked if self.manager.is_flagged(layer)
                                       else Qt.CheckState.Unchecked)
                else:
                    item.setForeground(0, self.tree.palette().brush(self.tree.palette().ColorGroup.Disabled,
                                                                     self.tree.palette().ColorRole.Text))
                if layer.isSpatial():
                    item.setCheckState(1, Qt.CheckState.Checked if self.manager.is_selectable(layer)
                                       else Qt.CheckState.Unchecked)
                if layer.isEditable() and layer.isModified():
                    font = item.font(0)
                    font.setBold(True)
                    item.setFont(0, font)
                self.tree.addTopLevelItem(item)
            self._listed = [layer.id() for layer in self.manager.panel_layers()]
            self.tree.resizeColumnToContents(0)
            self._update_header(managed)
        finally:
            self._filling = False

    def _update_header(self, managed):
        for column in (0, 1):
            states = []
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and item.data(column, Qt.ItemDataRole.CheckStateRole) is not None:
                    states.append(item.checkState(column) == Qt.CheckState.Checked)
            if states and all(states):
                state = Qt.CheckState.Checked
            elif any(states):
                state = Qt.CheckState.PartiallyChecked
            else:
                state = Qt.CheckState.Unchecked
            self.header.set_state(column, state)

    def _header_toggled(self, column):
        """Header checkbox: select all when not everything is ticked, otherwise clear all."""
        everything = self.header._states.get(column) == Qt.CheckState.Checked
        QTimer.singleShot(0, lambda: self._apply_all(column, not everything))

    def _apply_all(self, column, on):
        if self.manager is None:
            return
        if column == 1:
            self.manager.set_all_selectable(on)
        elif self.manager.managed():
            self.manager.set_all_editable(on, ask=self.plugin.ask_turn_off)
        self._refresh_now()

    def _layer(self, item):
        if item is None:
            return None
        return QgsProject.instance().mapLayer(item.data(0, Qt.ItemDataRole.UserRole))

    def _item_changed(self, item, column):
        if self._filling or column not in (0, 1):
            return
        layer = self._layer(item)
        if layer is None:
            return
        on = item.checkState(column) == Qt.CheckState.Checked
        # Act after Qt has finished with this click: rebuilding the list (or a Save/Discard
        # question) inside itemChanged would touch an item Qt is still using.
        QTimer.singleShot(0, lambda: self._apply_check(layer.id(), column, on))

    def _apply_check(self, layer_id, column, on):
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is not None and self.manager is not None:
            if column == 1:
                self.manager.set_selectable(layer, on)
            elif can_edit(layer) and self.manager.managed():
                self.manager.set_editable(layer, on, ask=self.plugin.ask_turn_off)
        self._refresh_now()

    def _context_menu(self, pos):
        layer = self._layer(self.tree.itemAt(pos))
        menu = QMenu(self.tree)
        if layer is not None and layer.isSpatial():
            menu.addAction("Make this the only selectable layer").triggered.connect(
                lambda: self.manager.make_only_selectable(layer))
        menu.addAction("Make all layers selectable").triggered.connect(
            lambda: self.manager.set_all_selectable(True))
        menu.addSeparator()
        if layer is not None and can_edit(layer):
            a = menu.addAction("Make this the only editable layer")
            a.triggered.connect(lambda: self.manager.make_only_editable(layer, ask=self.plugin.ask_turn_off))
            pending = layer.isEditable() and layer.isModified()
            a = menu.addAction(f"Save edits of '{layer.name()}'")
            a.setEnabled(pending)
            a.triggered.connect(lambda: self.plugin.save_layer(layer))
            a = menu.addAction(f"Discard edits of '{layer.name()}'")
            a.setEnabled(pending)
            a.triggered.connect(lambda: self.plugin.discard_layer(layer))
            menu.addSeparator()
        menu.addAction("Make all layers editable").triggered.connect(
            lambda: self.manager.set_all_editable(True, ask=self.plugin.ask_turn_off))
        menu.addAction("Make no layers editable").triggered.connect(
            lambda: self.manager.set_all_editable(False, ask=self.plugin.ask_turn_off))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        menu.deleteLater()

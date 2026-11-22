# -*- coding: utf-8 -*-
"""ArcGIS Pro's "List By Selection" and "List By Editing" for the Layers panel.

Three buttons sit at the end of the Layers panel's own toolbar (the first returns to the normal
list). Switching one on changes what the tick boxes of the tree mean, exactly like the Pro Contents tabs:

* **List By Selection** - the tree shows the layers that can be selected, and each tick box says
  whether features of that layer can be selected (unticked = the Select tool skips it).
* **List By Editing** - the tree shows the layers that can be edited, all ticked by default; an
  unticked layer cannot be edited.

A group's box ticks or clears all the layers below it. Click the active button again to go back to
the normal drawing-order list (visibility boxes).

Both boxes are the very ones of the Editable Layers panel (`editor/manager.py`: the
`edit_session/selectable` and `edit_session/editable` layer properties), so the two always agree and
the choice is saved in the project.

How it is drawn: the tree view is not replaced (that would lose QGIS's drag and drop, menus and
indicators). While a mode is on, a style sheet makes the view's own check indicator invisible and a
transparent child widget paints the mode's boxes in the same place; clicks and the space bar on those
boxes are handled here instead of switching visibility.
"""

import os
from contextlib import suppress

from qgis.core import QgsLayerTree, QgsProject, QgsVectorLayer
from qgis.PyQt import sip

from ..branding import ICON_DIR
from qgis.PyQt.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from qgis.PyQt.QtGui import QIcon, QPainter
from qgis.PyQt.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QMenu,
    QStyle,
    QStyleOption,
    QStyleOptionViewItem,
    QToolBar,
    QToolButton,
    QWidget,
)

SELECTION = 'selection'
EDITING = 'editing'

TITLES = {None: 'List By Drawing Order', SELECTION: 'List By Selection', EDITING: 'List By Editing'}
TIPS = {
    None: 'List By Drawing Order\nThe normal layer list with visibility boxes.',
    SELECTION: 'List By Selection\nTick the layers whose features can be selected; '
               'an unticked layer cannot be selected.',
    EDITING: 'List By Editing\nTick the layers that can be edited; '
             'an unticked layer cannot be edited.',
}
ICONS = {None: 'interface/pro_list_drawing.png', SELECTION: 'interface/pro_list_selection.png', EDITING: 'interface/pro_list_editing.png'}

#: Hides the view's own check indicator but keeps its room, so the boxes painted here sit on it.
_HIDE_INDICATOR = '\nQgsLayerTreeView::indicator {{ width: {n}px; height: {n}px; image: none; }}\n'


def _is_vector(layer):
    return isinstance(layer, QgsVectorLayer) and not sip.isdeleted(layer)


class _Overlay(QWidget):
    """Transparent child of the view's viewport that paints the mode's tick boxes."""

    def __init__(self, owner, viewport):
        super().__init__(viewport)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.resize(viewport.size())

    def paintEvent(self, event):  # noqa: N802 - Qt virtual
        with suppress(Exception):               # never let a paint error reach Qt
            self._owner.paint_boxes(self)


class LayerModes(QObject):
    """The List By Selection / List By Editing buttons and what they do to the Layers panel."""

    def __init__(self, iface, editor=None, parent=None):
        """*editor*: a callable returning the KGA Editor controller (or None while it is not loaded)."""
        super().__init__(parent)
        self.iface = iface
        self._editor = editor or (lambda: None)
        self.mode = None
        self._actions = {}
        self._menu_actions = []
        self._separator = None
        self._bar = None
        self._overlay = None
        self._prior_sheet = None
        self._installed = False
        self._press_eaten = False
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(0)
        self._later.timeout.connect(self._sync_rows)
        self._wired_manager = None

    # ------------------------------------------------------------- lifecycle
    def install(self):
        """Put the list buttons into the Layers panel's own toolbar."""
        if self._installed:
            return True
        view = self.iface.layerTreeView()
        host = view.parentWidget()
        bars = host.findChildren(QToolBar) if host is not None else []
        if not bars:
            return False
        bar = bars[0]
        self._bar = bar
        self._group = QActionGroup(bar)
        self._group.setExclusive(True)
        self._actions = {}
        native = [x for x in bar.actions() if not x.isSeparator()]

        def find(word, default):
            for x in native:
                if word in (x.objectName() + ' ' + x.text() + ' ' + x.toolTip()).lower():
                    return x
            return default

        # Layout: Drawing Order, then ONE drop-down button holding the KGA List by Data Source and Show Full Path
        # buttons (gui/layer_path_manager) with List By Selection and List By Editing, then a separator and the
        # panel's own buttons from Open the Layer Styling Panel on. Found by name, not by position.
        source = find('kgalistbysourceaction', None)
        path = find('kgashowfullpathaction', None)
        after = find('mactionstyledock', None) or find('styling', native[2] if len(native) > 2 else None)
        # List By Data Source groups the tree by source, a drawing-order style list: when it is switched on
        # the tick-box modes end
        if source is not None and source.isCheckable():
            source.toggled.connect(self._native_source_toggled)
            self._native_source = source
        if source is not None:
            self._source_icon = source.icon()
            source.setIcon(QIcon(os.path.join(ICON_DIR, 'interface/pro_list_datasource.png')))
        for mode in (None, SELECTION, EDITING):
            action = QAction(QIcon(os.path.join(ICON_DIR, ICONS[mode])), TITLES[mode], bar)
            action.setCheckable(True)
            action.setToolTip(TIPS[mode])
            action.setObjectName('KgaLayerMode_' + str(mode))
            action.triggered.connect(lambda _checked=False, m=mode: self.set_mode(m))
            self._group.addAction(action)
            self._actions[mode] = action
        bar.insertAction(after, self._actions[None])
        # the group button: a menu button like Manage Map Themes; its icon is the list that is on
        self._menu = QMenu(bar)
        self._menu_actions = [x for x in (source, path) if x is not None]
        self._menu_actions += [self._actions[SELECTION], self._actions[EDITING]]
        for action in self._menu_actions:
            if action in bar.actions():
                bar.removeAction(action)            # they live in the menu now, not as separate buttons
            self._menu.addAction(action)
        self._menu_button = QToolButton(bar)
        self._menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu_button.setMenu(self._menu)
        self._menu_button.setToolTip('List By Data Source, Selection or Editing')
        self._menu_button.setAutoRaise(True)
        self._menu_widget_action = bar.insertWidget(after, self._menu_button)
        self._separator = QAction(bar)
        self._separator.setSeparator(True)
        bar.insertAction(after, self._separator)
        self._actions[None].setChecked(True)
        self._installed = True
        self._sync_buttons()
        return True

    def uninstall(self):
        if not self._installed:
            return
        self._set_check_mode(None)
        self._installed = False
        native = getattr(self, '_native_source', None)
        if native is not None and not sip.isdeleted(native) and getattr(self, '_source_icon', None) is not None:
            native.setIcon(self._source_icon)
        bar = getattr(self, '_bar', None)
        if bar is not None and not sip.isdeleted(bar):
            styling = None
            for x in bar.actions():
                if x is self._separator and self._separator is not None:
                    styling = bar.actions()[bar.actions().index(x) + 1:][:1]
            for action in [x for x in self._menu_actions if x.objectName().startswith('kga')]:
                self._menu.removeAction(action)     # the path manager's own buttons go back to the toolbar
                if styling:
                    bar.insertAction(styling[0], action)
            for action in (self._actions[None], self._menu_widget_action, self._separator):
                bar.removeAction(action)
            for action in (self._actions[SELECTION], self._actions[EDITING], self._separator, self._actions[None]):
                action.deleteLater()
            self._menu_button.deleteLater()
            self._menu.deleteLater()
        self._actions = {}
        self._menu_actions = []

    # ------------------------------------------------------------- the mode
    def set_mode(self, mode):
        """None = drawing order (the normal list), SELECTION / EDITING = the tick-box lists."""
        native = getattr(self, '_native_source', None)
        if native is not None and not sip.isdeleted(native) and native.isChecked():
            native.setChecked(False)                # a tick-box list is not grouped by source
        self._set_check_mode(mode)
        self._sync_buttons()

    def _native_source_toggled(self, on):
        if on and self.mode is not None:
            self._set_check_mode(None)
        self._sync_buttons()

    def _set_check_mode(self, mode):
        if mode == self.mode:
            return
        view = self.iface.layerTreeView()
        if self.mode is None:                       # entering a mode from the normal list
            self._prior_sheet = view.styleSheet()
            # the boxes get the size of the view's normal visibility boxes, so the lists look alike
            size = max(QApplication.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, None, view), 8)
            view.setStyleSheet(self._prior_sheet + _HIDE_INDICATOR.format(n=size))
            self._overlay = _Overlay(self, view.viewport())
            self._overlay.show()
            view.viewport().installEventFilter(self)
            view.installEventFilter(self)
            self._wire(True)
        if mode is None:                            # leaving it
            self.mode = None
            self._wire(False)
            with suppress(Exception):
                view.viewport().removeEventFilter(self)
                view.removeEventFilter(self)
            if self._overlay is not None:
                self._overlay.hide()
                self._overlay.deleteLater()
                self._overlay = None
            if self._prior_sheet is not None:
                view.setStyleSheet(self._prior_sheet)
                self._prior_sheet = None
            self._set_hidden(view, None, False)
            with suppress(Exception):               # Map Tabs hides the other maps' groups: put that back
                from . import map_tabs
                tabs = map_tabs.active()
                if tabs is not None:
                    tabs._refresh_rows()
        else:
            self.mode = mode
            self._sync_rows()
            self._overlay.update()

    def _sync_buttons(self):
        action = getattr(self, '_actions', {}).get(self.mode)
        if action is not None and not sip.isdeleted(action):
            action.setChecked(True)
        self._sync_group_icon()

    def _sync_group_icon(self):
        """The group button shows the icon of the list that is on (Data Source when none of its lists is)."""
        button = getattr(self, '_menu_button', None)
        if button is None or sip.isdeleted(button):
            return
        source = getattr(self, '_native_source', None)
        if self.mode in (SELECTION, EDITING):
            icon = self._actions[self.mode].icon()
        elif source is not None and not sip.isdeleted(source):
            icon = source.icon()
        else:
            icon = self._actions[SELECTION].icon()
        button.setIcon(icon)

    def _wire(self, on):
        """Follow the tree (rows come and go) and the Edit Session manager (the other panel's boxes)."""
        view = self.iface.layerTreeView()
        model = view.model()
        pairs = [(model.rowsInserted, self._queue), (model.rowsRemoved, self._queue),
                 (model.layoutChanged, self._queue), (model.modelReset, self._queue),
                 (view.expanded, self._repaint), (view.collapsed, self._repaint)]
        root = QgsProject.instance().layerTreeRoot()
        pairs.append((root.visibilityChanged, self._queue))
        manager = self._manager()
        if on and manager is not None:
            self._wired_manager = manager
        if self._wired_manager is not None:
            pairs.append((self._wired_manager.changed, self._repaint))
        for signal, slot in pairs:
            with suppress(Exception):
                signal.connect(slot) if on else signal.disconnect(slot)
        if not on:
            self._wired_manager = None

    def _queue(self, *_args):
        if self.mode is not None:
            self._later.start()

    def _repaint(self, *_args):
        if self._overlay is not None and not sip.isdeleted(self._overlay):
            self._overlay.update()

    # ------------------------------------------------------------- what each mode lists
    def _manager(self):
        editor = self._editor()
        return getattr(editor, 'manager', None) if editor is not None else None

    def _map_ids(self):
        from . import map_tabs
        with suppress(Exception):
            return map_tabs.active_map_layer_ids()
        return None

    def _eligible(self, layer, ids=False):
        if not _is_vector(layer):
            return False
        if ids is False:
            ids = self._map_ids()
        if ids is not None and layer.id() not in ids:      # only the map being shown
            return False
        if self.mode == SELECTION:
            return layer.isSpatial()
        from ..editor.manager import can_edit
        manager = self._manager()
        return can_edit(layer) and (manager is None or manager.managed())

    def _checked(self, layer):
        manager = self._manager()
        if self.mode == SELECTION:
            if manager is not None:
                return manager.is_selectable(layer)
            value = layer.customProperty('edit_session/selectable', None)
            return True if value is None else str(value).strip().lower() in ('true', '1', 'yes')
        if manager is not None:
            return manager.is_flagged(layer)
        from ..editor.manager import LAYER_PROPERTY, option
        value = layer.customProperty(LAYER_PROPERTY, None)
        return option('new_layers_editable') if value is None else str(value).strip().lower() in ('true', '1', 'yes')

    def _layers_below(self, node):
        ids = self._map_ids()
        return [n.layer() for n in node.findLayers() if self._eligible(n.layer(), ids)]

    def node_state(self, node):
        """Qt.CheckState for the node's box, or None when the node has none in this mode."""
        if node is None:
            return None
        if QgsLayerTree.isLayer(node):
            layer = node.layer()
            if layer is None or not self._eligible(layer):
                return None
            return Qt.CheckState.Checked if self._checked(layer) else Qt.CheckState.Unchecked
        if QgsLayerTree.isGroup(node):
            states = [self._checked(layer) for layer in self._layers_below(node)]
            if not states:
                return None
            if all(states):
                return Qt.CheckState.Checked
            return Qt.CheckState.PartiallyChecked if any(states) else Qt.CheckState.Unchecked
        return None

    # ------------------------------------------------------------- setting a layer
    def _set(self, layer, on):
        """Apply one box. Returns False when the user cancelled (unsaved edits)."""
        manager = self._manager()
        editor = self._editor()
        if self.mode == SELECTION:
            if manager is not None:
                manager.set_selectable(layer, on)
            else:
                layer.setCustomProperty('edit_session/selectable', bool(on))
                if not on and layer.selectedFeatureCount():
                    layer.removeSelection()
                QgsProject.instance().setDirty(True)
            return True
        if manager is not None:
            ask = editor.ask_turn_off if editor is not None else None
            return manager.set_editable(layer, on, ask=ask)
        from ..editor.manager import LAYER_PROPERTY
        layer.setCustomProperty(LAYER_PROPERTY, bool(on))
        QgsProject.instance().setDirty(True)
        return True

    def toggle(self, node):
        state = self.node_state(node)
        if state is None:
            return
        if QgsLayerTree.isLayer(node):
            self._set(node.layer(), state != Qt.CheckState.Checked)
        else:
            on = state != Qt.CheckState.Checked            # a group: clear it when full, else fill it
            for layer in self._layers_below(node):
                if not self._set(layer, on):
                    break
        self._repaint()

    # ------------------------------------------------------------- rows shown
    def _sync_rows(self):
        if self.mode is None:
            return
        view = self.iface.layerTreeView()
        self._set_hidden(view, None, True)
        self._repaint()

    def _set_hidden(self, view, _unused, apply_mode):
        """Hide the rows that have no box in this mode (or, with apply_mode False, show every row again)."""
        model = view.model()
        if model is None:
            return

        def walk(parent):
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                node = view.index2node(index)
                if node is None:                    # a legend row: Pro lists no symbols here
                    view.setRowHidden(row, parent, apply_mode)
                    continue
                hide = apply_mode and self.node_state(node) is None
                view.setRowHidden(row, parent, hide)
                if not hide and (QgsLayerTree.isGroup(node) or QgsLayerTree.isLayer(node)):
                    walk(index)

        walk(view.rootIndex())

    # ------------------------------------------------------------- the boxes
    def _box_rect(self, view, index):
        option = QStyleOptionViewItem()
        option.initFrom(view)
        option.rect = view.visualRect(index)
        option.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        option.widget = view
        return view.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, view)

    def _visible_indexes(self, view):
        index = view.indexAt(QPoint(2, 2))
        height = view.viewport().height()
        guard = 0
        while index.isValid() and guard < 2000:
            if view.visualRect(index).top() > height:
                break
            yield index
            index = view.indexBelow(index)
            guard += 1

    def paint_boxes(self, overlay):
        if self.mode is None:
            return
        view = self.iface.layerTreeView()
        painter = QPainter(overlay)
        style = QApplication.style()
        try:
            for index in self._visible_indexes(view):
                state = self.node_state(view.index2node(index))
                if state is None:
                    continue
                option = QStyleOption()
                option.initFrom(view)
                option.rect = self._box_rect(view, index)
                option.state = QStyle.StateFlag.State_Enabled | {
                    Qt.CheckState.Checked: QStyle.StateFlag.State_On,
                    Qt.CheckState.Unchecked: QStyle.StateFlag.State_Off,
                    Qt.CheckState.PartiallyChecked: QStyle.StateFlag.State_NoChange}[state]
                style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck, option, painter, view)
        finally:
            painter.end()

    # ------------------------------------------------------------- clicks
    def eventFilter(self, obj, event):  # noqa: N802 - Qt virtual
        if self.mode is None or sip.isdeleted(self):
            return False
        view = self.iface.layerTreeView()
        kind = event.type()
        try:
            if obj is view.viewport():
                if kind == QEvent.Type.Resize and self._overlay is not None:
                    self._overlay.resize(event.size())
                elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) \
                        and event.button() == Qt.MouseButton.LeftButton:
                    index = view.indexAt(event.pos())
                    if index.isValid() and self.node_state(view.index2node(index)) is not None \
                            and self._box_rect(view, index).contains(event.pos()):
                        if kind == QEvent.Type.MouseButtonPress:
                            self.toggle(view.index2node(index))
                        self._press_eaten = True
                        return True
                elif kind == QEvent.Type.MouseButtonRelease and self._press_eaten:
                    self._press_eaten = False
                    return True
            elif obj is view and kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space:
                index = view.currentIndex()
                if index.isValid() and self.node_state(view.index2node(index)) is not None:
                    self.toggle(view.index2node(index))
                    return True
        except Exception:
            return False
        return False

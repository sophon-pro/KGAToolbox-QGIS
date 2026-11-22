"""
The Quick Symbology window.

Three pages, like the mock-ups:

1. Single symbol - Fill Color, Outline Color, Outline width and a Preview. The same page edits ONE class
   of a unique-value map (then a back arrow returns to the class list).
2. Unique Value symbol - Field (or expression), Color Scheme and the class list (Class | Value | Label)
   with + (add values), down and up (order the classes).
3. Select values to add - the values that are not classes yet, with a back arrow that adds the selected ones.

Everything is applied to the layer as soon as you change it; there is no OK button.
"""

import os

from qgis.core import QgsProject, QgsSettings, QgsSymbolLayerUtils
from qgis.gui import QgsColorButton, QgsFieldExpressionWidget
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...branding import icon
from ...gui.window_reveal import revealed_preview
from ..layer_list import CurrentMapLayerComboBox
from . import engine
from .widgets import SymbolPreview, ToggleSwitch, color_pixmap, switch_label

TITLE = "Quick Symbology"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
KEY_ROLE = Qt.ItemDataRole.UserRole
SHOW_LAYER_KEY = "kga_editor/quick_symbol_show_layer"
CLEAR = QColor(0, 0, 0, 0)


class ClassTree(QTreeWidget):
    """The class list; tells its owner when Delete is pressed."""

    deletePressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def _tool_button(parent, icon, tip):
    b = QToolButton(parent)
    b.setIcon(icon)
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setIconSize(QSize(18, 18))
    return b


@revealed_preview
class QuickSymbolDialog(QDialog):

    layerChanged = pyqtSignal(object)          # the layer's symbols were changed: refresh the Layers panel
    message = pyqtSignal(str)                  # something to tell the user (status bar)
    visibilityChanged = pyqtSignal(bool)
    layer_chosen = pyqtSignal(object)          # the user picked a layer in the Layer list
    bag_requested = pyqtSignal()               # the Symbol Bag button was clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("KgaQuickSymbolDialog")
        self.setWindowTitle(TITLE)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.symbols = engine.QuickSymbols()
        self.layer = None
        self._class_key = None                 # set while one class is being edited
        self._building = False
        self._addable = []                     # [(value, QColor)] shown on the "Select values to add" page

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        # the layer: follows the Layers panel; any other layer can be picked here without selecting it there
        self.layer_row = QWidget(self)
        layer_lay = QHBoxLayout(self.layer_row)
        layer_lay.setContentsMargins(0, 0, 0, 0)
        self.layer_check = ToggleSwitch(self.layer_row)
        self.layer_check.setText("Layer")
        self.layer_check.setToolTip("Show the Layer list. Turn it off to hide it: the window still follows the layer "
                                    "selected in the Layers panel")
        self.layer_check.setChecked(QgsSettings().value(SHOW_LAYER_KEY, True, type=bool))
        self.layer_box = CurrentMapLayerComboBox(self.layer_row)
        self.layer_box.setToolTip("The layer to restyle. It follows the layer selected in the Layers panel; "
                                  "pick another layer here to restyle it without selecting it there")
        self.layer_box.setVisible(self.layer_check.isChecked())
        layer_lay.addWidget(self.layer_check)
        layer_lay.addWidget(switch_label("Layer", self.layer_check, self.layer_row))
        layer_lay.addWidget(self.layer_box, 1)
        root.addWidget(self.layer_row)
        self.shadow_row = QWidget(self)
        shadow_lay = QHBoxLayout(self.shadow_row)
        shadow_lay.setContentsMargins(0, 0, 0, 0)
        self.shadow_check = ToggleSwitch(self.shadow_row)
        self.shadow_check.setText("Drop shadow")
        self.shadow_check.setToolTip("Draw a soft shadow under the layer's shapes")
        shadow_lay.addWidget(self.shadow_check)
        shadow_lay.addWidget(switch_label("Drop shadow", self.shadow_check, self.shadow_row))
        shadow_lay.addStretch(1)
        root.addWidget(self.shadow_row)

        # header: the unique-value switch, or the class being edited
        self.mode_bar = QWidget(self)
        mode = QHBoxLayout(self.mode_bar)
        mode.setContentsMargins(0, 0, 0, 0)
        self.toggle = ToggleSwitch(self.mode_bar)
        self.toggle.setToolTip("Draw the layer with one colour per value of a field")
        mode.addWidget(self.toggle)
        mode.addWidget(QLabel("Unique Value symbol", self.mode_bar))
        mode.addStretch(1)
        root.addWidget(self.mode_bar)

        self.class_bar = QWidget(self)
        cls = QHBoxLayout(self.class_bar)
        cls.setContentsMargins(0, 0, 0, 0)
        self.class_back = _tool_button(self.class_bar, self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack),
                                       "Back to the classes")
        self.class_name = QLabel("", self.class_bar)
        bold = self.class_name.font()
        bold.setBold(True)
        self.class_name.setFont(bold)
        cls.addWidget(self.class_back)
        cls.addWidget(self.class_name, 1)
        root.addWidget(self.class_bar)

        self.notice = QWidget(self)
        note = QHBoxLayout(self.notice)
        note.setContentsMargins(0, 0, 0, 0)
        self.notice_text = QLabel("", self.notice)
        self.notice_text.setWordWrap(True)
        self.replace_button = QPushButton("Use a single symbol", self.notice)
        note.addWidget(self.notice_text, 1)
        note.addWidget(self.replace_button)
        root.addWidget(self.notice)

        self.stack = QStackedWidget(self)
        self.page_single = self._build_single()
        self.page_unique = self._build_unique()
        self.page_values = self._build_values()
        for page in (self.page_single, self.page_unique, self.page_values):
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)
        self.setMinimumWidth(430)

        self.layer_box.activated.connect(self._layer_picked)
        self.layer_check.toggled.connect(self._layer_list_toggled)
        self.toggle.toggled.connect(self._toggled)
        self.shadow_check.toggled.connect(self._shadow_toggled)
        self.class_back.clicked.connect(self._leave_class)
        self.replace_button.clicked.connect(self._replace_renderer)
        QgsProject.instance().layersWillBeRemoved.connect(self._layers_removed)

    # ================================================================== building the pages
    def _build_single(self):
        page = QWidget(self)
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.fill_label = QLabel("Fill Color", page)
        self.fill_button = QgsColorButton(page)
        self.fill_button.setAllowOpacity(True)
        self.fill_button.setToolTip("Fill colour (use the arrow for transparency)")
        self.outline_label = QLabel("Outline Color", page)
        self.outline_button = QgsColorButton(page)
        self.outline_button.setAllowOpacity(True)
        self.outline_button.setToolTip("Outline colour")
        self.width_label = QLabel("Outline width", page)
        self.width_box = QDoubleSpinBox(page)
        self.width_box.setRange(0.0, 200.0)
        self.width_box.setDecimals(4)
        self.width_box.setSingleStep(0.1)
        self.width_box.setSuffix(" pt")
        self.width_box.setToolTip("Outline width in points")
        self.preview_label = QLabel("Preview", page)
        self.preview_frame = QFrame(page)
        self.preview_frame.setFrameShape(QFrame.Shape.StyledPanel)
        inner = QVBoxLayout(self.preview_frame)
        inner.setContentsMargins(4, 4, 4, 4)
        self.preview = SymbolPreview(self.preview_frame)
        inner.addWidget(self.preview)
        self.bag_button = self._bag_tool_button(page)
        grid.addWidget(self.fill_label, 0, 0)
        grid.addWidget(self.fill_button, 0, 1)
        grid.addWidget(self.outline_label, 1, 0)
        grid.addWidget(self.outline_button, 1, 1)
        self._single_grid = grid
        grid.addWidget(self.bag_button, 1, 2, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(self.width_label, 2, 0)
        grid.addWidget(self.width_box, 2, 1, 1, 2)
        grid.addWidget(self.preview_label, 3, 0)
        grid.addWidget(self.preview_frame, 3, 1, 1, 2)
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(1, 120)
        self.fill_button.colorChanged.connect(self._fill_changed)
        self.outline_button.colorChanged.connect(self._outline_changed)
        self.width_box.valueChanged.connect(self._width_changed)
        return page

    def _bag_tool_button(self, parent):
        b = QToolButton(parent)
        b.setText("Symbol Bag")
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bag_icon = icon("mapping/symbolBag.png")
        if not bag_icon.isNull():
            b.setIcon(bag_icon)
        b.setToolTip("Pick a symbol from the Symbol Bag: double-click one there and it is applied to this layer, "
                     "to the class being edited, ")
        b.clicked.connect(self.bag_requested)
        return b

    def _build_unique(self):
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("Field", page), 0, 0)
        self.field = QgsFieldExpressionWidget(page)
        self.field.setToolTip("Field to draw unique values of, or click the expression button to write one")
        grid.addWidget(self.field, 0, 1)
        grid.addWidget(QLabel("Color Scheme", page), 1, 0)
        self.scheme_box = QComboBox(page)
        self.scheme_box.setIconSize(QSize(72, 16))
        self.scheme_box.setMaxVisibleItems(16)
        self._fill_schemes()
        grid.addWidget(self.scheme_box, 1, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        bar.addWidget(QLabel("Classes", page))
        bar.addStretch(1)
        self.add_button = _tool_button(page, QIcon(os.path.join(ICON_DIR, "plus.svg")), "Add values as classes")
        self.down_button = _tool_button(page, QIcon(os.path.join(ICON_DIR, "arrow_down.svg")), "Move the class down")
        self.up_button = _tool_button(page, QIcon(os.path.join(ICON_DIR, "arrow_up.svg")), "Move the class up")
        for b in (self.add_button, self.down_button, self.up_button):
            bar.addWidget(b)
        lay.addLayout(bar)

        self.tree = ClassTree(page)
        self.tree.setHeaderLabels(["Class", "Value", "Label"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(48, 16))
        self.tree.setUniformRowHeights(True)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setMinimumHeight(190)
        self._stretch_columns(self.tree)
        self.tree.setToolTip("Double-click a swatch to edit that class's symbol, or a label to rename it. "
                             "Delete removes the selected classes.")
        lay.addWidget(self.tree, 1)

        self.field.fieldChanged.connect(self._field_changed)
        self.scheme_box.activated.connect(self._scheme_chosen)
        self.add_button.clicked.connect(self._open_values)
        self.down_button.clicked.connect(lambda: self._move(1))
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)
        self.tree.itemChanged.connect(self._label_edited)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.deletePressed.connect(self._remove_selected)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        return page

    def _build_values(self):
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self.values_back = _tool_button(page, self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack),
                                        "Back to the classes (adds nothing)")
        title = QLabel("Select values to add", page)
        bold = title.font()
        bold.setBold(True)
        title.setFont(bold)
        self.add_values_button = QPushButton("Add", page)
        self.add_values_button.setToolTip("Add the selected values as classes")
        self.add_values_button.setEnabled(False)
        self.options_button = QPushButton("Options", page)
        menu = QMenu(self.options_button)
        menu.addAction("Select all").triggered.connect(lambda: self.values_tree.selectAll())
        menu.addAction("Clear selection").triggered.connect(lambda: self.values_tree.clearSelection())
        menu.addSeparator()
        menu.addAction("Add all values").triggered.connect(self._add_all_values)
        self.options_button.setMenu(menu)
        head.addWidget(self.values_back)
        head.addWidget(title, 1)
        head.addWidget(self.add_values_button)
        head.addWidget(self.options_button)
        lay.addLayout(head)

        self.values_tree = QTreeWidget(page)
        self.values_tree.setHeaderLabels(["Symbol", "Value", "Label"])
        self.values_tree.setRootIsDecorated(False)
        self.values_tree.setIconSize(QSize(48, 16))
        self.values_tree.setUniformRowHeights(True)
        self.values_tree.setAlternatingRowColors(True)
        self.values_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.values_tree.setMinimumHeight(260)
        self._stretch_columns(self.values_tree)
        self.values_tree.setToolTip("Select the values to add (Ctrl or Shift for several), then press Add")
        lay.addWidget(self.values_tree, 1)
        self.values_back.clicked.connect(self._close_values)
        self.add_values_button.clicked.connect(self._add_selected_values)
        self.values_tree.itemSelectionChanged.connect(self._values_selection_changed)
        return page

    @staticmethod
    def _stretch_columns(tree):
        """The swatch column fits its swatch; Value and Label share the rest."""
        header = tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _fill_schemes(self):
        self.scheme_box.blockSignals(True)
        self.scheme_box.clear()
        for name in engine.ramp_names():
            icon = QIcon(QgsSymbolLayerUtils.colorRampPreviewPixmap(engine.make_ramp(name), QSize(72, 16)))
            self.scheme_box.addItem(icon, name, name)
        wanted = self.scheme_box.findData(engine.default_ramp_name())
        self.scheme_box.setCurrentIndex(max(0, wanted))
        self.scheme_box.blockSignals(False)

    # ================================================================== opening / showing
    @property
    def class_key(self):
        """The legend key of the class being edited, or None."""
        return self._class_key

    def open_for(self, layer, class_key=None):
        """Show the window for `layer`; with a class key, straight on that class's symbol."""
        self.layer = layer
        self._class_key = class_key
        self._show_layer(layer)
        self.refresh()

    def _show_layer(self, layer):
        """Put `layer` in the Layer list without that counting as the user picking it."""
        if layer is None or sip.isdeleted(layer):
            return
        self.layer_box.show_layer(layer)

    def _layer_picked(self, _index):
        layer = self.layer_box.currentLayer()
        if layer is not None:
            self.layer_chosen.emit(layer)

    def _layer_list_toggled(self, on):
        self.layer_box.setVisible(on)
        QgsSettings().setValue(SHOW_LAYER_KEY, bool(on))
        self.layout().activate()
        self.adjustSize()

    def refresh(self):
        """Show the page that fits the layer's renderer."""
        layer = self.layer
        if layer is None or sip.isdeleted(layer):
            return
        kind = engine.renderer_kind(layer)
        self.notice.setVisible(False)
        if self._class_key and kind == engine.CATEGORIZED and engine.class_index(layer.renderer(), self._class_key) >= 0:
            self._show_single()
        elif kind == engine.CATEGORIZED:
            self._class_key = None
            self._show_unique()
        else:
            self._class_key = None
            self._show_single()
            if kind == engine.OTHER:
                self.notice_text.setText("This layer is drawn with a {} renderer. Turn on Unique Value symbol, or "
                                         "replace it with one symbol to edit it here.".format(engine.renderer_name(layer)))
                self.notice.setVisible(True)

    def _set_page(self, page):
        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            h = QSizePolicy.Policy.Preferred if w is page else QSizePolicy.Policy.Ignored
            w.setSizePolicy(h, h)
        self.stack.setCurrentWidget(page)
        self._update_shadow_row(page)
        self.layout().activate()
        self.adjustSize()

    def _update_shadow_row(self, page):
        """The drop-shadow box belongs to the whole layer: shown with the layer's symbol or its class list."""
        layer = self.layer
        usable = layer is not None and not sip.isdeleted(layer)             and engine.renderer_kind(layer) in (engine.SINGLE, engine.CATEGORIZED)
        show = usable and (page is self.page_unique or (page is self.page_single and self._class_key is None))
        if show:
            self.shadow_check.blockSignals(True)
            self.shadow_check.setChecked(engine.has_shadow(layer))
            self.shadow_check.blockSignals(False)
        self.shadow_row.setVisible(bool(show))

    def _shadow_toggled(self, on):
        if self._building or self.layer is None or sip.isdeleted(self.layer):
            return
        engine.set_shadow(self.layer, on)
        self.layerChanged.emit(self.layer)

    def _header(self, mode):
        """mode: 'toggle' | 'class' | None (the values page has its own header)."""
        self.mode_bar.setVisible(mode == "toggle")
        self.class_bar.setVisible(mode == "class")

    def _switch_possible(self):
        return self.layer.fields().count() > 0 or engine.renderer_kind(self.layer) == engine.CATEGORIZED

    # ------------------------------------------------------------------ page 1
    def _show_single(self):
        layer, key = self.layer, self._class_key
        kind = engine.renderer_kind(layer)
        editable_renderer = kind == engine.SINGLE or key is not None
        symbol = engine.QuickSymbols.symbol_of(layer, key) if editable_renderer else engine.base_symbol(layer)
        style = engine.read_style(symbol)
        self._building = True
        try:
            line = style["kind"] == "line"
            self.fill_label.setVisible(not line)
            self.fill_button.setVisible(not line)
            self._single_grid.addWidget(self.bag_button, 1 if line else 0, 2, Qt.AlignmentFlag.AlignLeft)      # beside the first colour button
            self.outline_label.setText("Color" if line else "Outline Color")
            self.width_label.setText("Width" if line else "Outline width")
            self.fill_button.setColor(style["fill"] if style["fill"] is not None else CLEAR)
            self.outline_button.setColor(style["outline"] if style["outline"] is not None else CLEAR)
            self.width_box.setValue(style["width"] if style["width"] is not None else 0.0)
            self.fill_button.setEnabled(editable_renderer)
            self.outline_button.setEnabled(editable_renderer and style["editable"])
            self.width_box.setEnabled(editable_renderer and style["editable"])
            if not style["editable"] and editable_renderer:
                tip = "This symbol is built from layers Quick Symbology does not edit; open Layer Properties for them."
                self.outline_button.setToolTip(tip)
                self.width_box.setToolTip(tip)
            self.preview.set_symbol(symbol)
            if key is not None:
                i = engine.class_index(layer.renderer(), key)
                self.class_name.setText("Class: " + (layer.renderer().categories()[i].label() or engine.OTHER_LABEL))
                self._header("class")
            else:
                self.toggle.setChecked(False)
                self.toggle.setEnabled(self._switch_possible())
                self._header("toggle")
        finally:
            self._building = False
        self._set_page(self.page_single)

    def _apply_part(self, fill=None, outline=None, width=None):
        if self._building or self.layer is None or sip.isdeleted(self.layer):
            return
        layer, key = self.layer, self._class_key
        if engine.renderer_kind(layer) not in (engine.SINGLE, engine.CATEGORIZED):
            return
        symbol = engine.QuickSymbols.symbol_of(layer, key)
        if symbol is None:                      # a unique-value layer with no class chosen: nothing to edit here
            return
        engine.apply_style(symbol, fill, outline, width)
        engine.QuickSymbols.set_symbol(layer, symbol, key)
        self.preview.set_symbol(symbol)
        self.layerChanged.emit(layer)

    def _fill_changed(self, color):
        self._apply_part(fill=QColor(color) if color.isValid() else QColor(CLEAR))

    def _outline_changed(self, color):
        self._apply_part(outline=QColor(color) if color.isValid() else QColor(CLEAR))

    def _width_changed(self, value):
        self._apply_part(width=value)

    def _replace_renderer(self):
        if self.layer is None:
            return
        self.symbols.replace_with_single(self.layer)
        self.layerChanged.emit(self.layer)
        self.refresh()

    # ------------------------------------------------------------------ page 2
    def _show_unique(self):
        layer = self.layer
        renderer = layer.renderer()
        self._building = True
        try:
            self.toggle.setChecked(True)
            self.toggle.setEnabled(True)
            self.field.setLayer(layer)
            self.field.setField(renderer.classAttribute())
        finally:
            self._building = False
        self._header("toggle")
        self._fill_tree()
        self._set_page(self.page_unique)

    def _fill_tree(self, keep=None):
        """Show the layer's classes. `keep` = legend keys to select afterwards (default: the ones selected now)."""
        if keep is None:
            keep = [i.data(0, KEY_ROLE) for i in self.tree.selectedItems()]
        self._building = True
        self.tree.clear()
        selected = []
        try:
            for cat in self.layer.renderer().categories():
                other = engine.is_other(cat)
                item = QTreeWidgetItem(["", engine.OTHER_LABEL if other else str(cat.value()), cat.label()])
                item.setIcon(0, QIcon(engine.preview_pixmap(cat.symbol(), 48, 16)))
                item.setData(0, KEY_ROLE, cat.uuid())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                self.tree.addTopLevelItem(item)
                if cat.uuid() in keep:
                    selected.append(item)
            for item in selected:
                item.setSelected(True)
        finally:
            self._building = False
        self._update_buttons()

    def _update_buttons(self):
        keys = self._selected_keys()
        renderer = self.layer.renderer() if self.layer is not None and not sip.isdeleted(self.layer) else None
        movable = bool(keys) and renderer is not None and renderer.type() == "categorizedSymbol"
        self.up_button.setEnabled(movable)
        self.down_button.setEnabled(movable)

    def _selected_keys(self):
        keys = []
        for item in self.tree.selectedItems():
            keys.append(item.data(0, KEY_ROLE))
        # in list order, so several classes move together in a predictable way
        order = {self.tree.topLevelItem(i).data(0, KEY_ROLE): i for i in range(self.tree.topLevelItemCount())}
        return sorted(keys, key=lambda k: order.get(k, 0))

    def _toggled(self, on):
        if self._building or self.layer is None:
            return
        layer = self.layer
        if on:
            result = self.symbols.to_unique(layer, ramp_name=self.scheme_box.currentData())
            if result is None:
                self._building = True
                self.toggle.setChecked(False)
                self._building = False
                self.message.emit("Quick Symbology: '{}' has no field to draw unique values of.".format(layer.name()))
                return
            field, count, truncated = result
            if truncated:
                self.message.emit("Quick Symbology: '{}' has more than {} values; the first {} became classes. "
                                  "Use + to add more.".format(field, engine.MAX_AUTO_CLASSES, count))
        else:
            self.symbols.to_single(layer)
        self.layerChanged.emit(layer)
        self._class_key = None
        self.refresh()

    def _field_changed(self, *_args):
        if self._building or self.layer is None:
            return
        text, _is_expression, valid = self.field.currentField()
        if not text or not valid:
            return
        if engine.renderer_kind(self.layer) != engine.CATEGORIZED or text == self.layer.renderer().classAttribute():
            return
        count, truncated = self.symbols.set_field(self.layer, text, self.scheme_box.currentData())
        if truncated:
            self.message.emit("Quick Symbology: '{}' has more than {} values; the first {} became classes. "
                              "Use + to add more.".format(text, engine.MAX_AUTO_CLASSES, count))
        self._fill_tree(keep=[])
        self.layerChanged.emit(self.layer)

    def _scheme_chosen(self, _index):
        if self._building or self.layer is None or engine.renderer_kind(self.layer) != engine.CATEGORIZED:
            return
        self.symbols.set_scheme(self.layer, self.scheme_box.currentData())
        self._fill_tree()
        self.layerChanged.emit(self.layer)

    def _move(self, delta):
        keys = self._selected_keys()
        if not keys or self.layer is None:
            return
        moved = False
        for key in (reversed(keys) if delta > 0 else keys):         # keep several selected classes in order
            moved = self.symbols.move(self.layer, key, delta) or moved
        if moved:
            self._fill_tree(keep=keys)
            self.layerChanged.emit(self.layer)

    def _remove_selected(self):
        keys = self._selected_keys()
        if not keys or self.layer is None:
            return
        removed = [self.symbols.remove(self.layer, key) for key in keys]
        if any(removed):
            self._fill_tree(keep=[])
            self.layerChanged.emit(self.layer)

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        menu = QMenu(self.tree)
        menu.addAction("Edit symbol...").triggered.connect(lambda: self._edit_class(item.data(0, KEY_ROLE)))
        menu.addAction("Rename label").triggered.connect(lambda: self.tree.editItem(item, 2))
        menu.addSeparator()
        menu.addAction("Move up").triggered.connect(lambda: self._move(-1))
        menu.addAction("Move down").triggered.connect(lambda: self._move(1))
        remove = menu.addAction("Remove class")
        remove.triggered.connect(self._remove_selected)
        remove.setEnabled(any(not engine.is_other(self.layer.renderer().categories()[
            engine.class_index(self.layer.renderer(), k)]) for k in self._selected_keys()))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _tree_double_clicked(self, item, column):
        if column == 2:
            self.tree.editItem(item, 2)
        elif column == 0:
            self._edit_class(item.data(0, KEY_ROLE))

    def _label_edited(self, item, column):
        if self._building or column != 2 or self.layer is None:
            return
        self.symbols.set_label(self.layer, item.data(0, KEY_ROLE), item.text(2))
        self.layerChanged.emit(self.layer)

    def _edit_class(self, key):
        self._class_key = key
        self.refresh()

    def _leave_class(self):
        self._class_key = None
        self.refresh()

    # ------------------------------------------------------------------ page 3
    def _open_values(self):
        if self.layer is None or engine.renderer_kind(self.layer) != engine.CATEGORIZED:
            return
        self._addable = engine.QuickSymbols.addable(self.layer, self.scheme_box.currentData())
        if not self._addable:
            self.message.emit("Quick Symbology: every value of '{}' already has a class.".format(
                self.layer.renderer().classAttribute()))
            return
        self.values_tree.clear()
        for value, color in self._addable:
            item = QTreeWidgetItem(["", str(value), str(value)])
            item.setIcon(0, QIcon(color_pixmap(color)))
            self.values_tree.addTopLevelItem(item)
        self._header(None)
        self._set_page(self.page_values)

    def _values_selection_changed(self):
        self.add_values_button.setEnabled(bool(self.values_tree.selectedItems()))

    def _chosen(self, all_rows=False):
        if all_rows:
            return list(self._addable)
        rows = sorted(self.values_tree.indexOfTopLevelItem(i) for i in self.values_tree.selectedItems())
        return [self._addable[r] for r in rows]

    def _add_selected_values(self):
        self._add(self._chosen())

    def _add_all_values(self):
        self._add(self._chosen(all_rows=True))

    def _add(self, items):
        if items and self.layer is not None:
            self.symbols.add_values(self.layer, items)
            self.layerChanged.emit(self.layer)
        self._close_values()

    def _close_values(self):
        self._show_unique()

    # ================================================================== housekeeping
    def _layers_removed(self, removed):
        ids = [item if isinstance(item, str) else item.id() for item in removed]
        self.symbols.forget(ids)
        if self.layer is not None and not sip.isdeleted(self.layer) and self.layer.id() in ids:
            self.layer = None
            self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibilityChanged.emit(False)

    def disconnect_all(self):
        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self._layers_removed)
        except (TypeError, RuntimeError):
            pass

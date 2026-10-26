"""
Tool pages: what the Modify Features pane shows while a tool is active, like ArcGIS Pro's tool panes.

Clicking a tile replaces the tile list with the tool's page: a back arrow and the tool's name on top (drawn by the
pane), then the tool's hint, how many features are selected, its options and buttons, and a line saying what it
just did. PageMixin gives a map tool that page; CommandPage does the same for the tiles that act on the selection
straight away (Generalize, Reverse Direction ...).
"""

import os
from contextlib import suppress

from qgis.core import QgsProject
from qgis.gui import QgsMapTool
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QCursor, QGuiApplication, QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..selection import _status, all_vector_layers, map_layers


# ---------------------------------------------------------------------------------------- widgets
def hint_label(text):
    label = QLabel(text[:1].upper() + text[1:] if text else "")
    label.setObjectName("KgaModifyHint")
    label.setWordWrap(True)
    return label


def section_label(text):
    label = QLabel(text)
    label.setObjectName("KgaModifySection")
    return label


def spin(value=0.0, low=-1e12, high=1e12, decimals=3, step=1.0, suffix=""):
    box = QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


def button(text, slot, tip=""):
    b = QPushButton(text)
    b.setObjectName("KgaModifyButton")
    b.clicked.connect(lambda _c=False: slot())
    if tip:
        b.setToolTip(tip)
    return b


def check(text, checked, slot):
    box = QCheckBox(text)
    box.setChecked(checked)
    box.toggled.connect(slot)
    return box


def combo(items, current=0):
    box = QComboBox()
    for item in items:
        if isinstance(item, tuple):
            box.addItem(item[0], item[1])
        else:
            box.addItem(item)
    box.setCurrentIndex(current)
    return box


def separator():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def add_form(box):
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    form.setContentsMargins(0, 0, 0, 0)
    box.addLayout(form)
    return form


# ---------------------------------------------------------------------------------------- selection
def selection_text(canvas):
    """'3 features selected in 1 layer', or what to do when nothing is."""
    shown = map_layers(canvas)
    layers = [layer for layer in all_vector_layers()
              if layer.selectedFeatureCount() and layer.isSpatial() and (shown is None or layer.id() in shown)]
    count = sum(layer.selectedFeatureCount() for layer in layers)
    if not count:
        return "No features selected - use the Select tool on the KGA Editor toolbar."
    editable = [layer for layer in layers if layer.isEditable()]
    text = f"{count} feature{'s' if count != 1 else ''} selected in {len(layers)} layer{'s' if len(layers) != 1 else ''}"
    if len(editable) < len(layers):
        text += f" ({len(layers) - len(editable)} not editable)"
    return text + "."


# ---------------------------------------------------------------------------------------- Pro-style pieces
STRIP_QSS = """
QFrame#KgaModifyDone { background: #e3f1e1; border: 1px solid #c6e0c2; }
QFrame#KgaModifyDone[ok="false"] { background: #fdf0d9; border: 1px solid #ecd3a1; }
QFrame#KgaModifyDone[ok="info"] { background: #e0eef8; border: 1px solid #bcd7ee; }
QFrame#KgaModifyDone[ok="info"] QLabel { color: #1d3b57; }
QFrame#KgaModifyDone QLabel { font-size: 10pt; color: #1f3a1f; }
QToolButton#KgaModifyDoneClose { border: none; color: #5f6368; font-size: 11pt; padding: 0 4px; }
QToolButton#KgaModifyDoneClose:hover { color: #111111; }
"""


class DoneStrip(QFrame):
    """The green "Reshape completed." strip (amber when nothing happened) with a dismiss X; hidden until used."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("KgaModifyDone")
        self.setStyleSheet(STRIP_QSS)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 4, 4)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        close = QToolButton(self)
        close.setObjectName("KgaModifyDoneClose")
        close.setText("✕")
        close.setToolTip("Dismiss")
        close.clicked.connect(self.hide)
        row.addWidget(self.label, 1)
        row.addWidget(close)
        self.hide()

    def show_text(self, text, ok=True):
        """ok True = green, False = amber, "info" = blue (an instruction rather than a result)."""
        self.setProperty("ok", "info" if ok == "info" else ("true" if ok else "false"))
        self.style().unpolish(self)
        self.style().polish(self)
        self.label.setText(text)
        self.show()


class SelectedTree(QTreeWidget):
    """The selected features, grouped by layer: "land_parcel (1)" with the feature names under it."""

    MAX_PER_LAYER = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("KgaModifySelectedTree")
        self.setHeaderHidden(True)
        self.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.setMinimumHeight(110)
        self.setMaximumHeight(170)

    def refresh(self, canvas):
        from ..attributes.layer_browser import feature_label
        shown = map_layers(canvas)
        self.blockSignals(True)                 # rebuilding is not the user picking a feature
        self.clear()
        for layer in all_vector_layers():
            n = layer.selectedFeatureCount()
            if not n or not layer.isSpatial() or (shown is not None and layer.id() not in shown):
                continue
            top = QTreeWidgetItem(self, [f"{layer.name()} ({n})"])
            for i, feature in enumerate(layer.getSelectedFeatures()):
                if i >= self.MAX_PER_LAYER:
                    QTreeWidgetItem(top, [f"... and {n - i} more"])
                    break
                child = QTreeWidgetItem(top, [feature_label(layer, feature)])
                child.setData(0, Qt.ItemDataRole.UserRole, (layer.id(), feature.id()))   # which feature this line is
            top.setExpanded(True)
        self.blockSignals(False)


BANNER_TIP = ("Click or drag on the map to select the features to work on. "
              "Shift adds, Ctrl removes, N picks the next overlapping feature.")


def selection_banner(on_click, tooltip=BANNER_TIP):
    """The "Change the selection." bar every tool that works on the selection shows above the selected features."""
    banner = QToolButton()
    banner.setObjectName("KgaModifyBanner")
    banner.setCheckable(True)
    banner.setIcon(QIcon(os.path.join(os.path.dirname(__file__), "icons", "select_change.svg")))
    banner.setIconSize(QSize(26, 26))
    banner.setText("Change the selection.")
    banner.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    banner.setToolTip(tooltip)
    banner.clicked.connect(lambda _c=False: on_click())
    return banner


# ---------------------------------------------------------------------------------------- select mode
class SelectModeMixin:
    """
    For a map tool that works on the selection: a "Select features" button on its page and a Select mode in
    which the map tool stays this one (so its page stays open) but clicks and drags go to the Edit Session
    select tool. The tool's own handlers call `forward_select` first and stop when it returns True.
    """

    SELECTS = True              # offer the Select button
    AUTO_SELECT = True          # start in Select mode when nothing is selected yet
    # A manual selection (the Select features button) ends Select mode too, so the tool goes straight back to its
    # work. Shift / Ctrl keep it on to build a selection; pressing on a selected feature ends it.
    SELECT_ONE_SHOT = False
    CURSOR = Qt.CursorShape.CrossCursor
    _selecting = False          # clicks / drags select features instead of acting on them
    _select_auto = False        # ... switched on by the tool itself because nothing was selected
    _select_button = None

    def _has_selection(self):
        return any(layer.selectedFeatureCount() for layer in all_vector_layers())

    def start_select_mode(self):
        """Called from the tool's activate(): Select mode on when there is nothing to work on yet."""
        self.set_selecting(self.AUTO_SELECT and not self._has_selection(), auto=True)

    def build_selection_row(self, box):
        """"Change the selection." (the Select mode switch) and the selected features, grouped by layer."""
        if not self.SELECTS:
            return
        self.tree = SelectedTree()
        if self.select_tool is not None:
            self._select_button = selection_banner(lambda: self.set_selecting(not self._selecting))
            self._select_button.setChecked(self._selecting)
            box.addWidget(self._select_button)
        box.addWidget(self.tree)
        self._selection_label.setVisible(False)             # the list says it

    def set_selecting(self, on, auto=False):
        """Turn the tool's own Select mode on or off."""
        if self.select_tool is None or not self.SELECTS:
            return
        on = bool(on)
        if not on:
            self._cancel_selecting()
        self._selecting = on
        self._select_auto = on and auto
        if on:
            self.leave_pending_work()
            if hasattr(self.select_tool, "cursor"):
                self.setCursor(self.select_tool.cursor())
            self.say("click or drag on the map to select features.")
        else:
            self.setCursor(QCursor(self.CURSOR))
        if self._select_button is not None:
            self._select_button.setChecked(on)
        self.refresh_page()

    def leave_pending_work(self):
        """Select mode was switched on: drop whatever half-done gesture the tool had."""
        for name in ("_start", ):
            if hasattr(self, name):
                setattr(self, name, None)
        if hasattr(self, "_picking_anchor"):
            self._picking_anchor = False
        if hasattr(self, "_clear_preview"):
            self._clear_preview()
        elif hasattr(self, "reset"):
            self.reset()

    def _cancel_selecting(self):
        cancel = getattr(self.select_tool, "_cancel_drag", None)
        if cancel is not None:
            cancel()

    @staticmethod
    def _adding(e):
        """Shift or Ctrl is held: the user is building a selection click by click."""
        modifiers = e.modifiers() if hasattr(e, "modifiers") else Qt.KeyboardModifier.NoModifier
        return bool(modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))

    def _press_on_selection(self, e):
        """A plain left press that lands on an already selected feature (that is grabbing it, not selecting)."""
        hits_at = getattr(self.select_tool, "hits_at", None)
        if hits_at is None or e.button() != Qt.MouseButton.LeftButton or self._adding(e):
            return False
        try:
            hits = hits_at(e.mapPoint())
        except Exception:
            return False
        return any(layer.selectedFeatureCount() and fid in layer.selectedFeatureIds() for layer, fid in hits)

    def forward_select(self, handler, e):
        """In Select mode hand the event to the select tool and return True; otherwise return False."""
        if not self._selecting:
            if (self.SELECT_ONE_SHOT and self.SELECTS and handler == "keyPressEvent" and e.key() == Qt.Key.Key_N
                    and getattr(self.select_tool, "_candidates", None)):
                self.select_tool.keyPressEvent(e)                    # N = the next overlapping feature, any time
                if hasattr(self, "_items"):
                    self._items = None
                self.refresh_page()
                return True
            return False
        if handler == "keyPressEvent" and e.key() == Qt.Key.Key_Escape:
            self.set_selecting(False)
            e.ignore()
            return True
        if handler == "canvasPressEvent" and self.SELECT_ONE_SHOT and self._press_on_selection(e):
            self.set_selecting(False)                  # grabbing a selected feature: carry on as the tool's own press
            return False
        getattr(self.select_tool, handler)(e)
        if handler == "canvasReleaseEvent":
            if hasattr(self, "_items"):
                self._items = None
            self.refresh_page()
            done = self._select_auto or (self.SELECT_ONE_SHOT and not self._adding(e))
            if done and self._has_selection():
                self.set_selecting(False)
                self.say("features selected.")
        return True


# ---------------------------------------------------------------------------------------- the page
class PageMixin:
    """For a map tool: `page()` is its options page; `say()` writes to the status bar and to the page."""

    LABEL = ""
    HINT = ""
    SHOWS_SELECTION = True          # show the "n features selected" line
    SHOW_HINT = True                # the hint paragraph at the top of the page
    SHOW_STATUS = True              # the line at the bottom saying what the tool just did

    def page(self):
        page = getattr(self, "_page", None)
        if page is None:
            page = QWidget()
            page.setObjectName("KgaModifyPage")
            self._page = page
            box = QVBoxLayout(page)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(8)
            if self.HINT and self.SHOW_HINT:
                box.addWidget(hint_label(self.HINT))
            self._selection_label = QLabel()
            self._selection_label.setObjectName("KgaModifySelection")
            self._selection_label.setWordWrap(True)
            self._selection_label.setVisible(self.SHOWS_SELECTION)
            box.addWidget(self._selection_label)
            self.build_selection_row(box)
            self.build_page(box)
            box.addStretch(1)
            self.build_footer(box)
            if self.SHOW_STATUS:
                self._status_label = QLabel()
                self._status_label.setObjectName("KgaModifyStatus")
                self._status_label.setWordWrap(True)
                box.addWidget(self._status_label)
            self.refresh_page()
        return page

    def build_selection_row(self, box):
        """Between the "n features selected" line and the options: a tool may put a Select button here."""

    def build_page(self, box):
        """Add the tool's options and buttons to the page layout."""

    def build_footer(self, box):
        """Options pinned to the bottom of the page, under the stretch."""

    def refresh_page(self):
        """Called when the selection changes and after the tool acts: bring the page up to date."""
        page = getattr(self, "_page", None)
        if page is None or sip.isdeleted(page):          # the pane was torn down while the tool lived on
            return
        if self.SHOWS_SELECTION:
            self._selection_label.setText(selection_text(self.canvas()))
        tree = getattr(self, "tree", None)
        if tree is not None and not sip.isdeleted(tree):
            tree.refresh(self.canvas())
        self.update_page()

    def update_page(self):
        """Tool-specific part of refresh_page."""

    def page_message(self, text):
        label = getattr(self, "_status_label", None)
        if label is not None:
            label.setText(text[:1].upper() + text[1:] if text else "")

    def say(self, text, ms=8000):
        _status(self.iface, f"{self.LABEL}: {text}", ms)
        self.page_message(text)

    def dispose_page(self):
        page = getattr(self, "_page", None)
        if page is not None:
            page.deleteLater()
            self._page = None
            self._status_label = None


class CommandPage(PageMixin):
    """
    A tile that acts on the selection: a page with its options and an action button. Subclasses fill `build_page`
    and do the work in a method the button calls. `ctl` is the Modify Features controller.
    """

    LABEL = ""
    HINT = ""

    def __init__(self, ctl):
        self.ctl = ctl
        self.iface = ctl.iface

    def canvas(self):
        return self.iface.mapCanvas()

    _select_armed = False       # the Select button put the Select tool in hand and a selection is awaited
    _select_button = None

    def build_selection_row(self, box):
        """"Change the selection." (arms the Edit Session select tool; checked while that tool is in hand) and the
        selected features, grouped by layer."""
        if not self.SHOWS_SELECTION:
            return
        self.tree = SelectedTree()
        if getattr(self.ctl, "select_tool", None) is not None:
            self._select_button = selection_banner(self._banner_clicked)
            box.addWidget(self._select_button)
            self.canvas().mapToolSet.connect(self._sync_banner)
            self._sync_banner()
        box.addWidget(self.tree)
        self._selection_label.setVisible(False)             # the list says it

    def _banner_clicked(self):
        self.arm_select()
        self._sync_banner()

    def _sync_banner(self, *_args):
        button_ = self._select_button
        if button_ is not None and not sip.isdeleted(button_):
            button_.setChecked(self.canvas().mapTool() is getattr(self.ctl, "select_tool", None))

    def arm_select(self):
        """The Select tool becomes the active map tool; a plain selection hands the map back (resume_after_select)."""
        self.canvas().setMapTool(self.ctl.select_tool)
        self._select_armed = True
        self.say("click or drag on the map to select features.")

    def refresh_page(self):
        super().refresh_page()
        if (self._select_armed and self.canvas().mapTool() is getattr(self.ctl, "select_tool", None)
                and any(layer.selectedFeatureCount() for layer in all_vector_layers())
                and not (QGuiApplication.keyboardModifiers()
                         & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))):
            self._select_armed = False                  # Shift / Ctrl are for building a selection: keep selecting
            QTimer.singleShot(0, self._resume)          # after the Select tool has finished handling its click

    def _resume(self):
        current = getattr(self.ctl, "current", None)
        if current is not None and current[2] is self and self.canvas().mapTool() is self.ctl.select_tool:
            self.resume_after_select()

    def resume_after_select(self):
        """The Select tool did its job: a page that works with a map tool of its own takes the map back here."""

    def activate(self):
        """The page was opened (arm a tool here if the command needs one)."""

    def deactivate(self):
        """The page was left."""

    def dispose(self):
        """The pane is going away: hand back everything the page put on the canvas."""
        if self._select_button is not None:
            with suppress(TypeError, RuntimeError):
                self.canvas().mapToolSet.disconnect(self._sync_banner)
        self.deactivate()
        self.dispose_page()


def project_layer(layer_id):
    return QgsProject.instance().mapLayer(layer_id)


class ActionPage(CommandPage):
    """A command page: hint, selection line, (options added by build_options) and one action button."""

    ACTION = ""
    ACTION_TIP = ""

    def build_page(self, box):
        self.build_options(box)
        box.addWidget(button(self.ACTION, self.run, self.ACTION_TIP))

    def _select_tool(self):
        """The Edit Session select tool, if there is a real one to arm."""
        select = getattr(self.ctl, "select_tool", None)
        return select if isinstance(select, QgsMapTool) else None

    def activate(self):
        """These commands act on the selection: the Select tool is in hand from the start - click features,
        click empty map to clear, drag for an area (Shift / Ctrl add or remove) - then press the action button."""
        if self.SHOWS_SELECTION and self._select_tool() is not None:
            self.canvas().setMapTool(self._select_tool())
            self.say("click features on the map to select them, then press the button below.")

    def deactivate(self):
        select = self._select_tool()
        canvas = self.canvas()
        if select is not None and canvas.mapTool() is select:
            canvas.unsetMapTool(select)
            with suppress(Exception):
                self.ctl.iface.actionPan().trigger()

    def build_options(self, box):
        pass

    def run(self):
        result = self.act()
        self.refresh_page()
        return result

    def act(self):
        raise NotImplementedError

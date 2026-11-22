# -*- coding: utf-8 -*-
"""The dockable Panels and Toolbars Manager."""
from qgis.PyQt.QtCore import QEvent, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu,
    QMessageBox, QScrollArea, QStackedWidget, QToolBar, QToolButton, QVBoxLayout, QWidget,
)
from qgis.core import QgsApplication, QgsSettings
from qgis.gui import QgsFilterLineEdit

from ..panel_bubble import ToolbarReveal
from .core.profiles import ProfileStore
from .core.registry import PANEL, TOOLBAR, discover
from .core.thumbnails import ThumbnailCache
from .ui.card import ItemCard
from .ui.style import build_stylesheet
from .ui.widgets import Segmented

SETTINGS_COMPACT = "KGA/ui_manager/compact"
SETTINGS_TAB = "KGA/ui_manager/tab"
FILTER_ALL, FILTER_ON, FILTER_OFF = range(3)


def _theme_icon(name):
    return QgsApplication.getThemeIcon(name)


def _icon_button(icon_name, tip, checkable=False):
    btn = QToolButton()
    btn.setObjectName("uiIconBtn")
    btn.setIcon(_theme_icon(icon_name))
    btn.setToolTip(tip)
    btn.setCheckable(checkable)
    btn.setAutoRaise(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


class _ItemList(QScrollArea):
    """A scrolling list of rows with an empty-state message."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.cards = []
        self.setObjectName("uiScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setObjectName("uiListBody")
        self.list_layout = QVBoxLayout(body)
        self.list_layout.setContentsMargins(0, 2, 2, 2)
        self.list_layout.setSpacing(5)
        self.empty = QLabel("No {} match your search.".format("panels" if kind == PANEL else "toolbars"))
        self.empty.setObjectName("uiEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(60)
        self.empty.hide()
        self.list_layout.addWidget(self.empty)
        self.list_layout.addStretch(1)
        self.setWidget(body)

    def clear(self):
        # Never setParent(None) a shown card: it becomes a top-level window for an instant, which
        # flashes on screen (once per card). Hide it while it still has a parent, then delete it.
        for card in self.cards:
            self.list_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []

    def add(self, card):
        self.list_layout.insertWidget(self.list_layout.count() - 1, card)
        self.cards.append(card)

    def listed(self):
        return [c for c in self.cards if not c.isHidden()]

    def set_all(self, visible):
        for card in self.listed():
            card._set(visible)

    def counts(self):
        on = total = 0
        for card in self.cards:
            try:
                on += 1 if card.item.is_visible() else 0
                total += 1
            except RuntimeError:
                pass
        return on, total


class UiManagerDock(ToolbarReveal, QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Panels and Toolbars Manager", parent)
        self.setObjectName("KGAUiManagerDock")
        self.iface = iface
        self.mw = iface.mainWindow()
        self.settings = QgsSettings()
        self.thumbs = ThumbnailCache()
        self.profiles = ProfileStore()
        self._items = []
        self._hooked = []
        self._dirty = True

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self.refresh)

        self._build_ui()
        self.mw.installEventFilter(self)

    # ================================================================ UI
    def _build_ui(self):
        self.root = QWidget()
        self.root.setObjectName("uiRoot")
        outer = QVBoxLayout(self.root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QVBoxLayout()
        top.setContentsMargins(8, 8, 8, 6)
        top.setSpacing(7)

        self.search = QgsFilterLineEdit()
        self.search.setObjectName("uiSearch")
        self.search.setShowSearchIcon(True)
        self.search.setPlaceholderText("Search panels, toolbars or tools (e.g. buffer)")
        self.search.textChanged.connect(self.apply_filter)
        top.addWidget(self.search)

        self.kind_seg = Segmented(["Panels", "Toolbars"], expand=True)
        self.kind_seg.changed.connect(self._on_kind)
        top.addWidget(self.kind_seg)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.state_seg = Segmented(["All", "Open", "Hidden"])
        self.state_seg.changed.connect(self.apply_filter)
        row.addWidget(self.state_seg)
        row.addStretch(1)

        show_all = QToolButton()
        show_all.setObjectName("uiLink")
        show_all.setText("Show all")
        show_all.setToolTip("Show every item currently listed")
        show_all.setCursor(Qt.CursorShape.PointingHandCursor)
        show_all.clicked.connect(lambda: self._current_list().set_all(True))
        hide_all = QToolButton()
        hide_all.setObjectName("uiLink")
        hide_all.setText("Hide all")
        hide_all.setToolTip("Hide every item currently listed")
        hide_all.setCursor(Qt.CursorShape.PointingHandCursor)
        hide_all.clicked.connect(lambda: self._current_list().set_all(False))
        row.addWidget(show_all)
        row.addWidget(hide_all)

        self.compact_btn = _icon_button("/mActionCollapseTree.svg", "Compact rows", checkable=True)
        self.compact_btn.setChecked(self.settings.value(SETTINGS_COMPACT, False, type=bool))
        self.compact_btn.toggled.connect(self._set_compact)
        row.addWidget(self.compact_btn)
        refresh = _icon_button("/mActionRefresh.svg", "Re-scan panels and toolbars and refresh previews")
        refresh.clicked.connect(lambda: self.refresh(capture=True))
        row.addWidget(refresh)
        top.addLayout(row)
        outer.addLayout(top)

        self.stack = QStackedWidget()
        self.panel_list = _ItemList(PANEL)
        self.toolbar_list = _ItemList(TOOLBAR)
        self.stack.addWidget(self.panel_list)
        self.stack.addWidget(self.toolbar_list)
        holder = QHBoxLayout()
        holder.setContentsMargins(8, 0, 4, 6)
        holder.addWidget(self.stack)
        outer.addLayout(holder, 1)

        # Footer: layout profiles --------------------------------------------
        footer = QFrame()
        footer.setObjectName("uiFooter")
        prof = QHBoxLayout(footer)
        prof.setContentsMargins(8, 6, 8, 8)
        prof.setSpacing(4)
        label = QLabel("LAYOUT")
        label.setObjectName("uiSection")
        prof.addWidget(label)
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip("Saved workspace layouts")
        if hasattr(self.profile_combo, "setPlaceholderText"):  # Qt >= 5.15
            self.profile_combo.setPlaceholderText("No saved layouts")
        prof.addWidget(self.profile_combo, 1)

        apply_btn = QToolButton()
        apply_btn.setObjectName("uiLink")
        apply_btn.setText("Apply")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setToolTip("Restore the selected layout")
        apply_btn.clicked.connect(self._apply_profile)
        prof.addWidget(apply_btn)

        save_btn = _icon_button("/mActionFileSave.svg", "Save the current layout as a profile")
        save_btn.clicked.connect(self._save_profile)
        prof.addWidget(save_btn)

        more = _icon_button("/mActionOptions.svg", "More layout actions")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        menu = QMenu(more)
        menu.addAction("Delete selected layout", self._delete_profile)
        menu.addSeparator()
        menu.addAction("Export layouts to file…", self._export_profiles)
        menu.addAction("Import layouts from file…", self._import_profiles)
        menu.addSeparator()
        menu.addAction("Clear saved previews", self._clear_thumbnails)
        more.setMenu(menu)
        prof.addWidget(more)
        outer.addWidget(footer)

        self.setWidget(self.root)
        self._apply_style()
        self._reload_profile_combo()

        tab = self.settings.value(SETTINGS_TAB, 0, type=int)
        self.kind_seg.set_index(tab if tab in (0, 1) else 0)
        self.stack.setCurrentIndex(self.kind_seg.index())

    def _apply_style(self):
        self.root.setStyleSheet(build_stylesheet(self.root.palette()))

    def changeEvent(self, event):  # noqa: N802
        # Follow QGIS theme switches (Settings > Options > General > UI theme).
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "root"):
            self._apply_style()
        super().changeEvent(event)

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _current_list(self):
        return self.stack.currentWidget()

    def _on_kind(self, index):
        self.stack.setCurrentIndex(index)
        self.settings.setValue(SETTINGS_TAB, index)
        self._update_counts()

    # ======================================================== discovery
    def refresh(self, capture=False):
        self._dirty = False
        panels, toolbars = discover(self.mw, exclude=[self])
        self._items = panels + toolbars
        compact = self.compact_btn.isChecked()

        self.setUpdatesEnabled(False)   # one repaint for the whole rebuild
        try:
            for lst, items in ((self.panel_list, panels), (self.toolbar_list, toolbars)):
                lst.clear()
                for item in items:
                    card = ItemCard(item, self.thumbs, compact)
                    card.stateChanged.connect(self._on_card_state)
                    lst.add(card)
        finally:
            self.setUpdatesEnabled(True)

        for item in self._items:
            needs_preview = item.kind == PANEL or not item.tool_actions()
            if not needs_preview:
                continue
            self._hook(item.widget)
            if item.is_visible() and (capture or self.thumbs.get(item.key) is None):
                QTimer.singleShot(250, lambda w=item.widget: self._capture(w))

        self.apply_filter()

    def _schedule_refresh(self):
        if self.isVisible():
            self._refresh_timer.start()
        else:
            self._dirty = True

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._dirty or not self._items:
            self._schedule_refresh()

    def eventFilter(self, obj, event):  # noqa: N802
        # Plugins add or remove toolbars/panels at any time: rebuild the list automatically.
        if obj is self.mw:
            etype = event.type()
            # ChildAdded arrives before the child is fully constructed, so its Python type is
            # not reliable yet; ChildPolished arrives once it is a real QToolBar/QDockWidget.
            if etype in (QEvent.Type.ChildAdded, QEvent.Type.ChildPolished):
                child = event.child()
                if isinstance(child, (QDockWidget, QToolBar)) and child is not self:
                    self._schedule_refresh()
            elif etype == QEvent.Type.ChildRemoved:
                # Dialogs, menus and tooltips come and go all the time; only a panel or toolbar
                # we list leaving matters (its Python type is unreliable while it is destroyed).
                child = event.child()
                if any(child is it.widget for it in self._items):
                    self._schedule_refresh()
        return False

    # ========================================================= previews
    def _hook(self, widget):
        if widget not in self._hooked:
            widget.visibilityChanged.connect(self._on_widget_visibility)
            self._hooked.append(widget)

    def _on_widget_visibility(self, visible):
        widget = self.sender()
        if visible and widget is not None:
            QTimer.singleShot(400, lambda w=widget: self._capture(w))

    def _capture(self, widget):
        try:
            for card in self.panel_list.cards + self.toolbar_list.cards:
                if card.item.widget is widget:
                    if self.thumbs.capture(card.item) is not None:
                        card.update_thumbnail()
                    return
        except RuntimeError:
            pass

    def _clear_thumbnails(self):
        self.thumbs.clear()
        for card in self.panel_list.cards + self.toolbar_list.cards:
            card.update_thumbnail()

    # ========================================================= filtering
    def apply_filter(self, *_):
        text = self.search.text().strip().lower()
        mode = self.state_seg.index()
        for lst in (self.panel_list, self.toolbar_list):
            for card in lst.cards:
                try:
                    on = card.item.is_visible()
                except RuntimeError:
                    card.hide()
                    continue
                match_text = not text or text in card.search_blob
                match_state = mode == FILTER_ALL or (mode == FILTER_ON) == on
                card.setVisible(match_text and match_state)
                card.set_search_hint(text)
            lst.empty.setVisible(bool(lst.cards) and not lst.listed())
        self._update_counts()

        # While searching, jump to the list that has results.
        if text and not self._current_list().listed():
            for i, lst in enumerate((self.panel_list, self.toolbar_list)):
                if lst.listed():
                    self.kind_seg.set_index(i)
                    self.stack.setCurrentIndex(i)
                    break

    def _on_card_state(self):
        if self.state_seg.index() != FILTER_ALL:
            QTimer.singleShot(0, self.apply_filter)  # defer: do not delete under the cursor
        else:
            self._update_counts()

    def _update_counts(self):
        for i, (lst, name) in enumerate(((self.panel_list, "Panels"), (self.toolbar_list, "Toolbars"))):
            on, total = lst.counts()
            self.kind_seg.set_text(i, "{}   {}/{}".format(name, on, total))

    def _set_compact(self, compact):
        self.settings.setValue(SETTINGS_COMPACT, compact)
        for card in self.panel_list.cards + self.toolbar_list.cards:
            card.set_compact(compact)

    # ========================================================= profiles
    def _reload_profile_combo(self, select=None):
        self.profile_combo.clear()
        self.profile_combo.addItems(self.profiles.names())
        if select:
            self.profile_combo.setCurrentText(select)

    def _save_profile(self):
        name, ok = QInputDialog.getText(self, "Save layout", "Layout name (e.g. Digitizing, Raster class):",
                                        text=self.profile_combo.currentText())
        name = name.strip()
        if not ok or not name:
            return
        if name in self.profiles.names() and QMessageBox.question(
                self, "Save layout", "Replace the existing layout \"{}\"?".format(name)) != QMessageBox.StandardButton.Yes:
            return
        self.profiles.save(name, self.mw, self._items)
        self._reload_profile_combo(select=name)
        self.iface.messageBar().pushSuccess("Panels and Toolbars Manager", "Layout \"{}\" saved.".format(name))

    def _apply_profile(self):
        name = self.profile_combo.currentText()
        if not name:
            return
        if self.profiles.apply(name, self.mw, self._items):
            self.show()  # never let a profile hide the manager itself
            self.raise_()
            self._schedule_refresh()
            self.iface.messageBar().pushInfo("Panels and Toolbars Manager", "Layout \"{}\" applied.".format(name))

    def _delete_profile(self):
        name = self.profile_combo.currentText()
        if name and QMessageBox.question(self, "Delete layout",
                                         "Delete the layout \"{}\"?".format(name)) == QMessageBox.StandardButton.Yes:
            self.profiles.delete(name)
            self._reload_profile_combo()

    def _export_profiles(self):
        if not self.profiles.names():
            QMessageBox.information(self, "Export layouts", "There are no saved layouts yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export layouts", "qgis_layouts.json", "JSON (*.json)")
        if path:
            count = self.profiles.export_to(path)
            self.iface.messageBar().pushSuccess("Panels and Toolbars Manager", "{} layout(s) exported.".format(count))

    def _import_profiles(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import layouts", "", "JSON (*.json)")
        if not path:
            return
        try:
            count = self.profiles.import_from(path)
        except (OSError, ValueError) as err:
            QMessageBox.warning(self, "Import layouts", str(err))
            return
        self._reload_profile_combo()
        self.iface.messageBar().pushSuccess("Panels and Toolbars Manager", "{} layout(s) imported.".format(count))

    # ========================================================== cleanup
    def cleanup(self):
        self._refresh_timer.stop()
        self.mw.removeEventFilter(self)
        for widget in self._hooked:
            try:
                widget.visibilityChanged.disconnect(self._on_widget_visibility)
            except (RuntimeError, TypeError):
                pass
        self._hooked = []
        self.panel_list.clear()
        self.toolbar_list.clear()

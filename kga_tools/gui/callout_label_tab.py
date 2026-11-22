# -*- coding: utf-8 -*-
"""Callout Label Studio as a labeling mode on Layer Properties > Labels and on
the Labels tab of the Layer Styling panel.

QGIS builds that page in C++ with a fixed list of modes (No Labels / Single /
Rule-based / Blocking). A map-layer config factory is the one hook QGIS gives
plugins when a Layer Properties dialog is built, so `CalloutLabelHookFactory`
uses it only as a signal: it declines to add a tab, and once the dialog exists
`LabelsPageHook` adds the mode to the Labels page's own combo box and shows the
studio in place of that page's stack.
"""
import weakref
from contextlib import suppress
from functools import partial

from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QApplication, QComboBox, QDialogButtonBox, QListWidget, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)
from qgis.core import Qgis, QgsMessageLog, QgsVectorLayer
from qgis.gui import QgsMapLayerConfigWidgetFactory

from .callout_label_dialog import PANEL_PROP

MODE_ID = "kga_callout_studio"
MODE_TEXT = "Callout Label Studio"
LOG_TAG = "KGA Toolbox"


HOOKED = "kga_callout_hooked"

_DOCK_HOOKS = weakref.WeakSet()           # the live Layer Styling hooks, so other tools can reach their panel


def labels_removed_elsewhere(layer):
    """Another tool (the Quick Labeling button) is about to switch `layer`'s labels off on purpose.

    Call this BEFORE switching them off: an open studio panel for that layer would otherwise treat the
    change as QGIS's own apply and put the studio style back."""
    for hook in list(_DOCK_HOOKS):
        with suppress(RuntimeError, AttributeError):
            panel = hook.panel
            if panel is not None and panel._layer is not None and panel._layer.id() == layer.id():
                panel.mark_labels_removed()


def _class_name(widget):
    try:
        return widget.metaObject().className()
    except RuntimeError:
        return ""


class LabelsPageHook(QObject):
    """Adds the Callout Label Studio mode to one Layer Properties dialog."""

    def __init__(self, iface, dialog, layer, page):
        super().__init__(dialog)
        from .callout_label_dialog import CalloutStudioPanel
        self._dialog = dialog
        self._layer = layer
        self._busy = False
        self._loaded = False
        self.combo = page.findChild(QComboBox, "mLabelModeComboBox")
        self.stack = page.findChild(QStackedWidget, "mStackedWidget")
        if self.combo is None or self.stack is None or not isinstance(page.layout(), QVBoxLayout):
            raise RuntimeError("the Labels page has an unexpected layout")

        self.panel = CalloutStudioPanel(iface, layer, page)
        self.panel.hide()
        page.layout().insertWidget(page.layout().indexOf(self.stack) + 1, self.panel, 1)

        self.combo.addItem(MODE_TEXT, MODE_ID)
        self._previous = self.combo.currentIndex()
        self.combo.currentIndexChanged.connect(self._mode_changed)

        # connected after the dialog's own handlers, so these run after them
        for box in dialog.findChildren(QDialogButtonBox):
            apply_btn = box.button(QDialogButtonBox.StandardButton.Apply)
            if apply_btn is not None:
                apply_btn.clicked.connect(self._after_apply)
        dialog.accepted.connect(self._after_apply)
        dialog.finished.connect(self.panel.discard)

        if self.panel.has_saved_style():
            self.combo.setCurrentIndex(self.combo.findData(MODE_ID))

    def _is_ours(self):
        return self.combo.currentData() == MODE_ID

    def _mode_changed(self, index):
        if self._busy:
            return
        if self._is_ours():
            self.stack.hide()
            if not self._loaded:
                self._loaded = True
                self.panel.load_from_layer()
            self.panel.show()
            self.panel._on_layer_changed(self.panel.layer_cb.currentLayer()) \
                if not self.panel.has_saved_style() else None
            self.panel._live_apply()
            return
        had_preview = self.panel._backup is not None
        self.panel.discard()
        self.panel.hide()
        self.stack.show()
        if had_preview:
            # QGIS has just built the page from the previewed labels: build it again
            self._busy = True
            try:
                target = self.combo.currentIndex()
                self.combo.setCurrentIndex(0 if target != 0 else 1)
                self.combo.setCurrentIndex(target)
            finally:
                self._busy = False

    def _after_apply(self, *_):
        """Layer Properties has just applied the page that was current; for this
        mode that switches the layer's labels off, so write the studio style."""
        try:
            if self._is_ours():
                self.panel.commit()
            else:
                self._layer.removeCustomProperty(PANEL_PROP)
        except RuntimeError:
            pass                    # the layer or the page is already gone


class DockLabelsHook(QObject):
    """Adds the mode to the Labels tab of the Layer Styling panel.

    That panel has one labeling widget for every layer and no OK / Cancel: the
    studio writes straight to the layer, and is rebuilt when the layer changes."""

    def __init__(self, iface, styling, page):
        super().__init__(page)
        _DOCK_HOOKS.add(self)
        self.iface = iface
        self.page = page
        self.panel = None
        self._busy = False
        self._layer_id = None
        self.combo = page.findChild(QComboBox, "mLabelModeComboBox")
        self.stack = page.findChild(QStackedWidget, "mStackedWidget")
        if self.combo is None or self.stack is None or not isinstance(page.layout(), QVBoxLayout):
            raise RuntimeError("the Labels page has an unexpected layout")
        self.combo.addItem(MODE_TEXT, MODE_ID)
        self.combo.currentIndexChanged.connect(self._mode_changed)
        # QGIS re-reads a new layer's mode without emitting the combo's signal
        try:
            iface.currentLayerChanged.connect(self._layer_changed)
        except (AttributeError, TypeError):
            pass
        # backstop: the panel must never outlive the mode (the panel's layer can
        # change without any signal we can hear)
        self._watch = QTimer(self)
        self._watch.setInterval(250)
        self._watch.timeout.connect(self._reconcile)
        self._watch.start()
        for btn in styling.findChildren(QPushButton):
            if btn.text().replace("&", "") == "Apply":
                btn.clicked.connect(self._after_apply)

    def _is_ours(self):
        return self.combo.currentData() == MODE_ID

    def uninstall(self):
        """Undo the hook (plugin unload / reload): QGIS keeps these widgets."""
        self._watch.stop()
        self._busy = True
        self._drop_panel()
        self.stack.show()
        self.combo.blockSignals(True)
        try:
            i = self.combo.findData(MODE_ID)
            if i >= 0:
                if self.combo.currentIndex() == i:
                    self.combo.setCurrentIndex(0)
                self.combo.removeItem(i)
        finally:
            self.combo.blockSignals(False)
        try:
            self.combo.currentIndexChanged.disconnect(self._mode_changed)
            self.iface.currentLayerChanged.disconnect(self._layer_changed)
        except (TypeError, RuntimeError, AttributeError):
            pass
        self.page.setProperty(HOOKED, False)
        self.deleteLater()

    def _layer(self):
        layer = self.iface.activeLayer()
        return layer if isinstance(layer, QgsVectorLayer) and layer.isSpatial() else None

    def _drop_panel(self):
        if self.panel is not None:
            self.panel.discard()
            self.panel.hide()
            try:
                self.panel._layer.repaintRequested.disconnect(self._guard)
            except (TypeError, RuntimeError):
                pass
            self.panel.deleteLater()
            self.panel = None

    def _select_ours(self):
        self._busy = True
        try:
            self.combo.setCurrentIndex(self.combo.findData(MODE_ID))
        finally:
            self._busy = False
        self._mode_changed(self.combo.currentIndex())

    def _reconcile(self):
        try:
            if self.panel is not None and not self._is_ours():
                self._mode_changed(self.combo.currentIndex())
        except RuntimeError:
            self._watch.stop()

    def _layer_changed(self, *_):
        QTimer.singleShot(0, self._follow_layer)    # after the panel has re-read the layer

    def _follow_layer(self):
        try:
            self._mode_changed(self.combo.currentIndex(), follow=True)
        except RuntimeError:
            pass                    # the panel is gone

    def _mode_changed(self, index, follow=False):
        if self._busy:
            return
        from .callout_label_dialog import CalloutStudioPanel
        layer = self._layer()
        if follow and self.panel is not None and layer is not None                 and layer.id() != self._layer_id:
            # QGIS has put the new layer's own mode in the combo, whatever it said before
            self.panel.hide()
        if layer is not None and layer.id() != self._layer_id:
            # the panel follows the layer: QGIS has just re-read the new layer's labels
            self._layer_id = layer.id()
            self._drop_panel()
            self.stack.show()
            if CalloutStudioPanel.layer_has_saved_style(layer) and not self._is_ours():
                self._select_ours()
                return
        if not self._is_ours():
            if self.panel is not None:
                # the user picked another mode: this layer is no longer a studio layer
                old = self.panel._layer
                self._drop_panel()
                if layer is not None and old.id() == layer.id():
                    old.removeCustomProperty(PANEL_PROP)
            self.stack.show()
            return
        if layer is None:
            return
        self.stack.hide()
        if self.panel is None:
            self.panel = CalloutStudioPanel(self.iface, layer, self.page, direct=True)
            self.panel.setMinimumHeight(640)
            self.panel.load_from_layer()
            self.page.layout().insertWidget(self.page.layout().indexOf(self.stack) + 1,
                                            self.panel, 1)
            layer.repaintRequested.connect(self._guard)
        self.panel.show()
        self.panel._live_apply()

    def _guard(self):
        """QGIS's own apply (the panel's live update, a mode change) turns labels
        off for a mode it does not know: put the studio style back."""
        QTimer.singleShot(0, self._restore)

    def _restore(self):
        try:
            panel = self.panel
            if panel is not None and self._is_ours() and not panel.labels_removed                     and not panel._layer.labelsEnabled():
                panel._live_apply()
        except RuntimeError:
            pass

    def _after_apply(self, *_):
        """The panel's Apply has just switched labels off for this mode: restore."""
        if self.panel is not None and self._is_ours():
            self.panel._live_apply()


class CalloutLabelHookFactory(QgsMapLayerConfigWidgetFactory):
    """Not a tab: `supportsLayer` is QGIS telling us a Layer Properties dialog is
    being built for `layer`, so the Labels page can be extended once it exists."""

    def __init__(self, iface):
        super().__init__(MODE_TEXT, QIcon())
        self.iface = iface
        self._links = []            # (tab list, slot) pairs, to disconnect on unload
        self._hooks = []
        self.attach_dock()
        try:
            iface.initializationCompleted.connect(self.attach_dock)
        except (AttributeError, TypeError):
            pass

    def attach_dock(self):
        """Watch the Layer Styling panel's tab list; hook its Labels page when it appears."""
        try:
            for sw in self.iface.mainWindow().findChildren(QWidget):
                if _class_name(sw) != "QgsLayerStylingWidget":
                    continue
                if sw.property(HOOKED):
                    self._hook_dock_page(sw)     # a page built since the last call
                    continue
                tabs = sw.findChild(QListWidget, "mOptionsListWidget")
                if tabs is None:
                    continue
                sw.setProperty(HOOKED, True)
                slot = partial(self._hook_dock_page, sw)
                tabs.currentRowChanged.connect(slot)
                self._links.append((tabs, slot, sw))
                self._hook_dock_page(sw)
        except RuntimeError:
            pass

    def _hook_dock_page(self, styling, *_row):
        """The tab's page is built lazily, just after the tab is selected."""
        QTimer.singleShot(0, lambda: self._hook_dock_page_now(styling))

    def _hook_dock_page_now(self, styling):
        try:
            for page in styling.findChildren(QWidget):
                if _class_name(page) == "QgsLabelingWidget" and not page.property(HOOKED):
                    page.setProperty(HOOKED, True)
                    self._hooks.append(DockLabelsHook(self.iface, styling, page))
        except Exception:
            QgsMessageLog.logMessage(
                "Could not add Callout Label Studio to Layer Styling:\n"
                + __import__("traceback").format_exc(), LOG_TAG, Qgis.MessageLevel.Warning)

    def uninstall(self):
        for tabs, slot, sw in self._links:
            try:
                tabs.currentRowChanged.disconnect(slot)
                sw.setProperty(HOOKED, False)
            except (TypeError, RuntimeError):
                pass
        self._links = []
        try:
            self.iface.initializationCompleted.disconnect(self.attach_dock)
        except (AttributeError, TypeError, RuntimeError):
            pass
        for hook in self._hooks:
            try:
                hook.uninstall()
            except RuntimeError:
                pass
        self._hooks = []

    def supportsLayer(self, layer):
        QTimer.singleShot(0, self.attach_dock)
        if isinstance(layer, QgsVectorLayer) and layer.isSpatial() \
                and layer.geometryType() in (Qgis.GeometryType.Point, Qgis.GeometryType.Line,
                                             Qgis.GeometryType.Polygon):
            QTimer.singleShot(0, lambda lid=layer.id(): self._hook(lid))
        return False

    def supportLayerPropertiesDialog(self):
        return True

    def supportsStyleDock(self):
        return False

    def _hook(self, layer_id):
        from qgis.core import QgsProject
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return
        for dialog in QApplication.topLevelWidgets():
            if _class_name(dialog) != "QgsVectorLayerProperties" \
                    or dialog.property(HOOKED) \
                    or layer.name() not in dialog.windowTitle():
                continue
            page = next((w for w in dialog.findChildren(QWidget)
                         if _class_name(w) == "QgsLabelingWidget"), None)
            if page is None:
                continue
            dialog.setProperty(HOOKED, True)
            try:
                LabelsPageHook(self.iface, dialog, layer, page)
            except Exception:
                QgsMessageLog.logMessage(
                    "Could not add Callout Label Studio to Layer Properties:\n"
                    + __import__("traceback").format_exc(), LOG_TAG, Qgis.MessageLevel.Warning)
            return

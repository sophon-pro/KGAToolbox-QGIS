"""
Copy Paste: the KGA Copy-Paste Feature tool as a tool of the Modify Features pane, laid out like the other tools.

Same clipboard and same paste engine as the Copy-Paste Feature window (algorithms/copy_paste_feature.py) - what is
copied here can be pasted there and the other way round.

    Copy    "Change the selection." and the selected features: click features on the map (select mode stays on),
            choose the Layer when features of several layers are selected, press Copy. The blue bar tells what is on
            the clipboard; "Clear clipboard" empties it. The copied features are outlined on the map.
    Paste   the Paste Special fields (target layer, how attributes carry across, geometry options) right in the
            page, then Paste - or Paste again for the same target and mapping; a green bar reports what was pasted.
"""

from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMessageLog,
    QgsProject,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QTabBar, QWidget

from ...algorithms.copy_paste_feature import (
    BAND_TAG,
    BAND_TAG_KEY,
    CLIPBOARD,
    LARGE_SELECTION,
    PasteSpecialForm,
    geometry_type,
    paste_features,
    sweep_stale_bands,
)
from ...branding import LOG_TAG
from ..selection import all_vector_layers, map_layers
from .page import DoneStrip, SelectedTree, button, project_layer, selection_banner
from .reshape import PickTool
from .split import TABS_QSS


class CopyPasteTool(PickTool):
    KEY = "copy_paste"
    LABEL = "Copy Paste"
    HINT = "select features and press Copy, then paste them into any layer on the Paste tab."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = False
    SELECT_ONE_SHOT = False                # Copy tab: clicking selects until you press Copy

    def __init__(self, canvas, iface, select_tool=None):
        self.tab = 0                       # 0 Copy, 1 Paste
        self._last_paste = None            # (layer id, mapping, reproject, multipart mode) of the last paste
        self._band = None
        self._connected = False
        self._open = False
        self._listed = None
        self.info = self.tab_bar = self.banner = self.layer_combo = self.form = None
        self.copy_row = self.paste_row = None
        self.copy_button = self.clear_button = self.repeat_button = None
        self._result = None                # (text, ok) shown on the blue bar until something else happens
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def start_select_mode(self):
        self.set_selecting(self.tab == 0)

    def activate(self):
        self._open = True
        canvas = self.canvas()
        sweep_stale_bands(canvas, self._band)
        if self._band is None:
            self._band = self._make_band(canvas)
        if not self._connected:
            canvas.destinationCrsChanged.connect(self._refresh_band)
            self._connected = True
        super().activate()
        if self.form is not None:
            self.form.refresh_clipboard()
        self._refresh_band()
        self.refresh_page()

    def deactivate(self):
        self._open = False
        if self._connected:
            with suppress(TypeError, RuntimeError):
                self.canvas().destinationCrsChanged.disconnect(self._refresh_band)
            self._connected = False
        if self._band is not None:
            self._band.reset(geometry_type("Line"))
        super().deactivate()

    def dispose(self):
        self.deactivate()
        band, self._band = self._band, None
        if band is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(band)
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)
        self.tab_bar = QTabBar()
        self.tab_bar.setStyleSheet(TABS_QSS)
        self.tab_bar.addTab("Copy")
        self.tab_bar.addTab("Paste")
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setCurrentIndex(self.tab)
        self.tab_bar.currentChanged.connect(self.set_tab)
        box.addWidget(self.tab_bar)
        self.banner = selection_banner(lambda: self.set_selecting(not self._selecting))
        self._select_button = self.banner
        self.banner.setChecked(self._selecting)
        box.addWidget(self.banner)
        self.tree = SelectedTree()
        box.addWidget(self.tree)

    def build_page(self, box):
        row = QHBoxLayout()
        self.layer_label = QLabel("Layer")
        row.addWidget(self.layer_label)
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(lambda _i: self.refresh_page())
        row.addWidget(self.layer_combo, 1)
        self.layer_row = QWidget()
        self.layer_row.setLayout(row)
        box.addWidget(self.layer_row)
        self.form = PasteSpecialForm(self.iface)
        self.form.source_label.hide()                       # the blue bar says the same
        self.form.table.setMinimumHeight(150)
        self.form.pasted.connect(self._pasted)
        box.addWidget(self.form)
        self._apply_tab()

    def build_footer(self, box):
        self.copy_row = QWidget()
        cr = QHBoxLayout(self.copy_row)
        cr.setContentsMargins(0, 0, 0, 0)
        self.clear_button = button("Clear clipboard", self.clear, "Empty the clipboard")
        self.copy_button = button("Copy", self.copy_selected, "Put the layer's selected features on the KGA clipboard")
        cr.addStretch(1)
        cr.addWidget(self.clear_button)
        cr.addWidget(self.copy_button)
        box.addWidget(self.copy_row)
        self.paste_row = QWidget()
        pr = QHBoxLayout(self.paste_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.repeat_button = button("Paste again", self.paste_again, "Repeat the last paste: same target layer, "
                                    "same field mapping")
        self.form.paste_button.setObjectName("KgaModifyButton")
        pr.addStretch(1)
        pr.addWidget(self.repeat_button)
        pr.addWidget(self.form.paste_button)
        box.addWidget(self.paste_row)
        self._apply_tab()

    def set_tab(self, index):
        self.tab = int(index)
        self._result = None
        self._apply_tab()
        self.refresh_page()

    def _apply_tab(self):
        if self.banner is None or self.form is None or self.copy_row is None or self.paste_row is None:
            return
        copy = self.tab == 0
        for widget in (self.banner, self.tree, self.layer_row, self.copy_row):
            widget.setVisible(copy)
        for widget in (self.form, self.paste_row):
            widget.setVisible(not copy)
        self.set_selecting(copy)

    # ------------------------------------------------------------------ what can be copied
    def _candidates(self):
        """[(layer, selected count)] of the vector layers with a selection, shown on the map."""
        shown = map_layers(self.canvas())
        return [(layer, layer.selectedFeatureCount()) for layer in all_vector_layers()
                if layer.isSpatial() and layer.selectedFeatureCount() and (shown is None or layer.id() in shown)]

    def current_layer(self):
        return project_layer(self.layer_combo.currentData()) if self.layer_combo is not None else None

    def _sync_layers(self, candidates):
        signature = tuple(layer.id() for layer, _n in candidates)
        if signature == self._listed:
            return
        self._listed = signature
        current = self.layer_combo.currentData()
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer, count in candidates:
            self.layer_combo.addItem(f"{layer.name()} ({count})", layer.id())
        self.layer_combo.setCurrentIndex(max(0, self.layer_combo.findData(current)))
        self.layer_combo.blockSignals(False)

    def update_page(self):
        if self.copy_button is None or sip.isdeleted(self.copy_button) or not self._open:
            return
        if getattr(self, "tree", None) is not None and not sip.isdeleted(self.tree):
            self.tree.refresh(self.canvas())
        candidates = self._candidates()
        self._sync_layers(candidates)
        for index in range(self.layer_combo.count()):                 # keep the counts in the labels true
            layer = project_layer(self.layer_combo.itemData(index))
            if layer is not None:
                self.layer_combo.setItemText(index, f"{layer.name()} ({layer.selectedFeatureCount()})")
        self.layer_row.setVisible(self.tab == 0 and len(candidates) > 1)
        self.copy_button.setEnabled(bool(candidates) and self.current_layer() is not None)
        empty = CLIPBOARD.is_empty()
        self.clear_button.setEnabled(not empty)
        self.repeat_button.setEnabled(not empty and self._last_paste is not None)
        self._show_info(empty, candidates)

    def _show_info(self, empty, candidates):
        if self._result is not None:
            self.info.show_text(*self._result)
        elif self.tab == 0 and not candidates and empty:
            self.info.show_text("Make a selection, then press Copy.", ok="info")
        elif empty:
            self.info.show_text("The clipboard is empty - copy features on the Copy tab first.", ok="info")
        else:
            self.info.show_text(CLIPBOARD.describe(), ok="info")

    def on_click(self, point):
        pass

    # ------------------------------------------------------------------ copy
    def tell(self, text, ok=True):
        self._result = (text, ok)
        self.say(text)
        self.refresh_page()

    def copy_selected(self):
        layer = self.current_layer()
        if layer is None:
            self.tell("Select the features to copy first.", ok=False)
            return 0
        count = layer.selectedFeatureCount()
        if not count:
            self.tell("Nothing is selected in that layer.", ok=False)
            return 0
        if count > LARGE_SELECTION:
            answer = QMessageBox.question(
                self.page(), "Copy Paste",
                f"{count} features are selected. They are all held in memory until the clipboard is cleared.\n\n"
                "Copy them anyway?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return 0
        return self.take(layer, layer.getSelectedFeatures())

    def take(self, layer, features, append=False):
        """Put features on the clipboard."""
        try:
            added = CLIPBOARD.take(layer, features, append=append)
        except Exception as exc:            # pragma: no cover
            QgsMessageLog.logMessage(f"Copy from {layer.name()} failed: {exc}", LOG_TAG, Qgis.MessageLevel.Warning)
            self.tell(f"Could not copy: {exc}", ok=False)
            return 0
        if not append:
            self._last_paste = None         # the old mapping no longer describes what is on the clipboard
        self.form.refresh_clipboard()
        self._refresh_band()
        self.tell(f"Copy completed: {added} feature(s) from {layer.name()} are on the clipboard - paste them on the "
                  "Paste tab.")
        return added

    def clear(self):
        CLIPBOARD.clear()
        self._last_paste = None
        self.form.refresh_clipboard()
        self._refresh_band()
        self.tell("The clipboard is empty.")

    # ------------------------------------------------------------------ paste
    def _pasted(self, layer, report):
        form = self.form
        self._last_paste = (layer.id(), form.current_mapping(), form.reproject_check.isChecked(),
                            form.multipart_combo.currentIndex())
        self.tell(f"Paste completed: {report.summary()}")

    def paste_again(self):
        """Repeat the last paste without going through the fields again."""
        if self._last_paste is None or CLIPBOARD.is_empty():
            return None
        layer_id, mapping, reproject, multipart = self._last_paste
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            self._last_paste = None
            self.tell("That target layer is no longer in the project - choose a new one and press Paste.", ok=False)
            return None
        if not layer.isEditable() and not layer.startEditing():
            self.tell(f'"{layer.name()}" could not be put into edit mode.', ok=False)
            return None
        try:
            report = paste_features(CLIPBOARD, layer, mapping, reproject=reproject, multipart_mode=multipart)
        except Exception as exc:
            QgsMessageLog.logMessage(f"Repeat paste into {layer.name()} failed: {exc}", LOG_TAG,
                                     Qgis.MessageLevel.Critical)
            self.tell(f"Nothing was pasted; the edit was rolled back. {exc}", ok=False)
            return None
        if report.new_ids:
            layer.selectByIds(report.new_ids)
        self.canvas().refresh()
        if report.has_details():
            QgsMessageLog.logMessage(f"Repeat paste into {layer.name()}: {report.summary()}\n{report.details()}",
                                     LOG_TAG, Qgis.MessageLevel.Info)
        self.tell(f'Paste completed into "{layer.name()}": {report.summary()}')
        return report

    # ------------------------------------------------------------------ the outline of what is on the clipboard
    def _make_band(self, canvas):
        band = QgsRubberBand(canvas, geometry_type("Line"))
        with suppress(Exception):
            band.setData(BAND_TAG_KEY, BAND_TAG)
        band.setColor(QColor(255, 120, 0, 190))
        band.setWidth(2)
        with suppress(AttributeError):
            band.setFillColor(QColor(255, 120, 0, 40))
            band.setStrokeColor(QColor(255, 120, 0, 220))
        with suppress(AttributeError):
            band.setIcon(QgsRubberBand.IconType.ICON_CIRCLE)
            band.setIconSize(9)
        return band

    def _refresh_band(self, *_args):
        """Outline what is on the clipboard, in canvas coordinates."""
        if self._band is None or not self._open:
            return
        kind = QgsWkbTypes.geometryType(CLIPBOARD.wkb_type) if not CLIPBOARD.is_empty() else geometry_type("Line")
        self._band.reset(kind)
        if CLIPBOARD.is_empty():
            return
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        transform = None
        if CLIPBOARD.crs is not None and CLIPBOARD.crs.isValid() and canvas_crs != CLIPBOARD.crs:
            transform = QgsCoordinateTransform(CLIPBOARD.crs, canvas_crs, QgsProject.instance())
        for geometry, _attributes in CLIPBOARD.rows:
            if geometry is None or geometry.isEmpty():
                continue
            drawn = QgsGeometry(geometry)
            if transform is not None and drawn.transform(transform) != 0:
                continue
            self._band.addGeometry(drawn, None)


TOOL_CLASSES = {CopyPasteTool.KEY: CopyPasteTool}

"""
Explode, laid out like the Explode pane of ArcGIS Pro, in two tabs. It works on multi-part features only.

    Features   "Change the selection." and the selected features (a single-part one is marked with a warning and
               the blue bar says "Unselect single part features."); "Keep original features"; the Explode button,
               enabled only while every selected feature is multi-part. Each part becomes a feature of its own.
    Parts      pick which parts leave their multi-part feature: hover a part on the map (it is outlined) and click it,
               and it becomes a feature of its own. Only selected features unless "Allow explode without selection".

New features copy the attributes (primary key and unique fields get fresh values). One undoable edit per layer.
"""

import os
from contextlib import suppress

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsFeatureRequest,
    QgsGeometry,
    QgsSettings,
    QgsVectorLayerUtils,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .page import DoneStrip, SelectedTree, button, check, selection_banner
from .pick import pick_features
from .reshape import PickTool, _copyable, explode_selection
from .split import TABS_QSS
from .transform import editable_selection

SETTINGS = "kga_editor/modify_"
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
LINE = Qgis.GeometryType.Line
POLYGON = Qgis.GeometryType.Polygon
NEED_MULTIPART = "Select one or more multipart features"


def is_multipart(geom):
    return geom is not None and not geom.isNull() and geom.isMultipart() and geom.constGet().partCount() >= 2


def selection_status(canvas):
    """(multi-part count, single-part count, {(layer id, fid): is multi-part}) of the selected editable features."""
    multi = single = 0
    flags = {}
    for layer, ids in editable_selection(canvas):
        for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(ids))):
            many = is_multipart(f.geometry())
            flags[(layer.id(), f.id())] = many
            if many:
                multi += 1
            else:
                single += 1
    return multi, single, flags


def explode_part(canvas, layer, fid, part_index):
    """Make part `part_index` of a multi-part feature a feature of its own. Returns True when it was done."""
    feature = layer.getFeature(fid)
    parts = feature.geometry().asGeometryCollection()
    if len(parts) < 2 or not 0 <= part_index < len(parts):
        return False
    taken = parts.pop(part_index)
    rest = QgsGeometry.collectGeometry(parts)
    if not taken.isMultipart():
        taken.convertToMultiType()
    layer.beginEditCommand("Explode part")
    ok = layer.changeGeometry(fid, rest)
    if ok:
        new = QgsVectorLayerUtils.createFeature(layer, taken, _copyable(layer, feature.attributes()))
        ok = layer.addFeature(new)
    if ok:
        layer.endEditCommand()
    else:
        layer.destroyEditCommand()
    layer.triggerRepaint()
    return ok


class ExplodeTool(PickTool):
    KEY = "explode"
    LABEL = "Explode"
    HINT = "explode multi-part features into single parts, or pull single parts out of them."
    SHOW_HINT = False
    SHOW_STATUS = False
    SHOWS_SELECTION = False
    SELECTS = True
    AUTO_SELECT = True
    SELECT_ONE_SHOT = False                # Features tab: selecting stays on; the Parts tab ends it after a pick

    def __init__(self, canvas, iface, select_tool=None):
        settings = QgsSettings()
        self.keep_original = settings.value(SETTINGS + "explode_keep", False, type=bool)
        self.allow_no_selection = settings.value(SETTINGS + "explode_no_selection", False, type=bool)
        self.tab = 0
        self.info = None
        self.tabs = None
        self.features_banner = self.parts_banner = None
        self.features_tree = self.parts_tree = None
        self.explode_button = None
        self._band = None
        self._open = False
        self._result = None                    # (text, ok): what the last Explode did, until the selection changes
        self._result_sig = frozenset()
        super().__init__(canvas, iface, select_tool)

    # ------------------------------------------------------------------ lifecycle
    def start_select_mode(self):
        self.set_selecting(self.tab == 0 or not self.allow_no_selection and not self._has_selection(), auto=True)

    def activate(self):
        self._open = True
        super().activate()

    def deactivate(self):
        self._open = False
        self._highlight(None)
        super().deactivate()

    def dispose(self):
        if self._band is not None:
            with suppress(Exception):
                self.canvas().scene().removeItem(self._band)
            self._band = None
        super().dispose()

    # ------------------------------------------------------------------ page
    def build_selection_row(self, box):
        self.info = DoneStrip()
        box.addWidget(self.info)

    def build_page(self, box):
        self.tabs = QTabWidget()
        self.tabs.setObjectName("KgaSplitTabs")
        self.tabs.setStyleSheet(TABS_QSS)
        self.tabs.addTab(self._features_tab(), "Features")
        self.tabs.addTab(self._parts_tab(), "Parts")
        self.tabs.setCurrentIndex(self.tab)
        self.tabs.currentChanged.connect(self.set_tab)
        box.addWidget(self.tabs, 100)
        self._select_button = self.features_banner

    def _features_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        self.features_banner = selection_banner(self._banner_clicked)
        self.features_banner.setChecked(self._selecting)
        lay.addWidget(self.features_banner)
        self.features_tree = SelectedTree()
        lay.addWidget(self.features_tree)
        self.keep_check = check("Keep original features", self.keep_original, self.set_keep)
        self.keep_check.setToolTip("Leave the multi-part features as they are and add every part as a new feature")
        lay.addWidget(self.keep_check)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.explode_button = button("Explode", self.explode, "Explode the selected multi-part features")
        row.addWidget(self.explode_button)
        lay.addLayout(row)
        return page

    def _parts_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        self.parts_banner = selection_banner(self._banner_clicked)
        lay.addWidget(self.parts_banner)
        self.parts_tree = SelectedTree()
        lay.addWidget(self.parts_tree)
        tile = QToolButton()
        tile.setObjectName("KgaModifyToolTile")
        tile.setCheckable(True)
        tile.setChecked(True)
        tile.setIcon(QIcon(os.path.join(ICON_DIR, "explode.svg")))
        tile.setIconSize(QSize(26, 26))
        tile.setText("Explode")
        tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tile.setToolTip("Hover a part of a multi-part feature and click it to make it a feature of its own")
        tile.clicked.connect(lambda _c=False: (self.set_selecting(False), tile.setChecked(True)))
        self.parts_tile = tile
        lay.addWidget(tile)
        lay.addStretch(1)
        self.allow_check = check("Allow explode without selection", self.allow_no_selection, self.set_allow)
        self.allow_check.setToolTip("With nothing selected, click any part of an editable multi-part feature")
        lay.addWidget(self.allow_check)
        return page

    def _mark_warnings(self, tree, flags):
        """A warning icon on the single-part features (they cannot be exploded)."""
        icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if data and flags.get(tuple(data)) is False:
                    child.setIcon(0, icon)

    def update_page(self):
        if self.explode_button is None or sip.isdeleted(self.explode_button):
            return
        multi, single, flags = selection_status(self.canvas())
        for tree in (self.features_tree, self.parts_tree):
            tree.refresh(self.canvas())
        self._mark_warnings(self.features_tree, flags)
        valid = multi > 0 and single == 0
        self.explode_button.setEnabled(valid)
        self.features_banner.setText("Change the selection." if valid else NEED_MULTIPART)
        self.parts_banner.setText("Change the selection.")
        if self._result and frozenset(flags) != self._result_sig:
            self._result = None                          # the selection moved on: the result no longer applies
        if self._result:
            self.info.show_text(*self._result)
        elif single and self.tab == 0:
            self.info.show_text("Unselect single part features.", ok="info")
        else:
            self.info.hide()
        for widget in (self.parts_banner, self.parts_tree):
            widget.setVisible(not self.allow_no_selection)

    def set_tab(self, index):
        self.tab = int(index)
        self._result = None
        self._select_button = self.features_banner if self.tab == 0 else self.parts_banner
        self.SELECT_ONE_SHOT = self.tab == 1                       # the Parts tab goes back to its tile after a pick
        self._highlight(None)
        self.set_selecting(self.tab == 0 or (not self.allow_no_selection and not self._has_selection()))
        self.refresh_page()

    def set_keep(self, on):
        self.keep_original = bool(on)
        QgsSettings().setValue(SETTINGS + "explode_keep", self.keep_original)

    def set_allow(self, on):
        self.allow_no_selection = bool(on)
        QgsSettings().setValue(SETTINGS + "explode_no_selection", self.allow_no_selection)
        if on and self._selecting and self.tab == 1:
            self.set_selecting(False)
        self.refresh_page()

    def _banner_clicked(self):
        self.set_selecting(not self._selecting)

    # ------------------------------------------------------------------ Features tab
    def explode(self):
        multi, single, _flags = selection_status(self.canvas())
        if not multi or single:
            self.info.show_text("Unselect single part features." if single else NEED_MULTIPART, ok="info")
            return 0
        exploded, created, skipped = explode_selection(self.canvas(), self.keep_original)
        if exploded:
            text = f"Explode completed: {exploded} feature(s), {created} new feature(s)."
        else:
            text = "Nothing was exploded."
        if skipped:
            text += f" Failed in: {', '.join(skipped)}."
        self._result = (text, bool(exploded) and not skipped)
        self._result_sig = frozenset(selection_status(self.canvas())[2])        # the exploded parts are selected too
        self.say(text + (" (Ctrl+Z undoes it)" if exploded else ""))
        self.refresh_page()
        return exploded

    # ------------------------------------------------------------------ Parts tab
    def _part_at(self, point):
        """(layer, fid, geometry in the map's CRS, index of the part under the mouse) or None."""
        chosen = None
        if not self.allow_no_selection:
            chosen = {(layer.id(), fid) for layer, ids in editable_selection(self.canvas()) for fid in ids}
            if not chosen:
                return None
        for layer, fid, geom in pick_features(self.canvas(), point, (LINE, POLYGON), editable_only=True):
            if chosen is not None and (layer.id(), fid) not in chosen:
                continue
            if not is_multipart(geom):
                continue
            here = QgsGeometry.fromPointXY(point)
            parts = geom.asGeometryCollection()
            index = min(range(len(parts)), key=lambda i: parts[i].distance(here))
            return layer, fid, geom, index
        return None

    def _highlight(self, geom):
        if self._band is None:
            if geom is None:
                return
            self._band = QgsRubberBand(self.canvas(), LINE)
            self._band.setColor(QColor(0, 200, 220))
            self._band.setWidth(3)
        self._band.reset(LINE)
        if geom is not None:
            outline = geom if geom.type() == LINE else geom.convertToType(LINE, False)
            if outline is not None and not outline.isNull():
                self._band.addGeometry(outline, QgsCoordinateReferenceSystem(), True)

    def on_move(self, point):
        if self.tab != 1 or self._selecting:
            return
        found = self._part_at(point)
        self._highlight(found[2].asGeometryCollection()[found[3]] if found else None)

    def on_click(self, point):
        if self.tab != 1:
            return
        if not self.allow_no_selection and not self._has_selection():
            self.set_selecting(True, auto=True)
            self.say("select the multipart features first.")
            return
        found = self._part_at(point)
        if found is None:
            self.say("no part of a multi-part feature there.")
            return
        layer, fid, _geom, index = found
        if explode_part(self.canvas(), layer, fid, index):
            self._result = ("Explode completed: 1 part is now a feature of its own.", True)
            self._result_sig = frozenset(selection_status(self.canvas())[2])
            self.say(self._result[0] + " (Ctrl+Z undoes it)")
            self._highlight(None)
            self.refresh_page()
        else:
            self.say("could not explode that part.")


TOOL_CLASSES = {ExplodeTool.KEY: ExplodeTool}

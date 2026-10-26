"""
Advanced editing tools for Edit Session.

Every tool of QGIS's Advanced Digitizing toolbar (Move, Copy and Move, Rotate, Scale, Simplify,
Add/Fill/Delete Ring, Add/Delete Part, Reshape, Offset Curve, Reverse Line, Trim/Extend, Split
Features, Split Parts, Merge Selected, Merge Attributes, Rotate/Offset Point Symbols) is on the
Edit Session toolbar, grouped in five drop-down buttons (Move / Rotate / Scale, Reshape, Rings and
Parts, Split and Merge, Point Symbols). Each button shows the tool used last.

QGIS enables these tools only for the ACTIVE layer, and only when that layer fits (Reverse Line
needs a line layer, Merge needs a selection ...). Clicking a greyed-out QGIS button does nothing.
The Edit Session buttons are enabled whenever ANY editable layer can use the tool: they first make
a suitable layer active, then start QGIS's own tool. While the tool is in use, the layer under the
mouse is followed (see follow.py), so it works on whatever layer you click.

The tools themselves are QGIS's own (nothing is re-implemented), so they behave exactly as usual:
snapping, the Advanced Digitizing panel, undo, attribute forms.
"""

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QInputDialog, QMenu, QToolButton, QWidgetAction

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.44
    from qgis.PyQt.QtWidgets import QAction

PT, LN, PG = Qgis.GeometryType.Point, Qgis.GeometryType.Line, Qgis.GeometryType.Polygon
ANY = (PT, LN, PG)
LINE_POLY = (LN, PG)

# objectName of QGIS's action -> (button text, geometry types, kind)
#   kind "map"       : a map tool; follows the layer under the mouse when you click
#   kind "hover"     : a map tool that chooses its layer while the mouse MOVES (Trim/Extend)
#   kind "selection" : acts at once on the selected features of the active layer (Merge ...)
#   kind "one_selected": a map tool that works on the ONE selected feature (Add Part)
#   kind "fixed"     : a drawing tool whose line usually starts OUTSIDE the feature (Reshape, Split):
#                      the layer is chosen when you FINISH the line (right-click / Enter), from the
#                      features the line crosses (selected ones first)
SPECS = {
    "mActionMoveFeature": ("Move Feature(s)", ANY, "map"),
    "mActionMoveFeatureCopy": ("Copy and Move Feature(s)", ANY, "map"),
    "mActionRotateFeature": ("Rotate Feature(s)", ANY, "map"),
    "mActionScaleFeature": ("Scale Feature(s)", ANY, "map"),
    "mActionSimplifyFeature": ("Simplify Feature", LINE_POLY, "map"),
    "mActionAddRing": ("Add Ring", (PG,), "map"),
    "mActionAddPart": ("Add Part", ANY, "one_selected"),
    "mActionFillRing": ("Fill Ring", (PG,), "map"),
    "mActionDeleteRing": ("Delete Ring", (PG,), "map"),
    "mActionDeletePart": ("Delete Part", ANY, "map"),
    "mActionReshapeFeatures": ("Reshape Features", LINE_POLY, "fixed"),
    "mActionOffsetCurve": ("Offset Curve", LINE_POLY, "map"),
    "mActionReverseLine": ("Reverse Line", (LN,), "map"),
    "mActionTrimExtendFeature": ("Trim/Extend Feature", LINE_POLY, "hover"),
    "mActionSplitFeatures": ("Split Features", LINE_POLY, "fixed"),
    "mActionSplitParts": ("Split Parts", LINE_POLY, "fixed"),
    "mActionMergeFeatures": ("Merge Selected Features", ANY, "selection"),
    "mActionMergeFeatureAttributes": ("Merge Attributes of Selected Features", ANY, "selection"),
    "mActionRotatePointSymbols": ("Rotate Point Symbols", (PT,), "map"),
    "mActionOffsetPointSymbol": ("Offset Point Symbols", (PT,), "map"),
}
ORDER = list(SPECS)

# The advanced tools are shown as a few drop-down buttons: (button title, unused, tool names)
GROUPS = [
    ("Move / Rotate / Scale", None, ["mActionMoveFeature", "mActionMoveFeatureCopy", "mActionRotateFeature",
                                     "mActionScaleFeature"]),
    ("Reshape", None, ["mActionReshapeFeatures", "mActionOffsetCurve", "mActionSimplifyFeature",
                       "mActionReverseLine", "mActionTrimExtendFeature"]),
    ("Rings and Parts", None, ["mActionAddRing", "mActionFillRing", "mActionDeleteRing", "mActionAddPart",
                               "mActionDeletePart"]),
    ("Split and Merge", None, ["mActionSplitFeatures", "mActionSplitParts", "mActionMergeFeatures",
                               "mActionMergeFeatureAttributes"]),
    ("Point Symbols", None, ["mActionRotatePointSymbols", "mActionOffsetPointSymbol"]),
]
BUTTON_TITLE = "Modify Features"   # the one drop-down; its sub-menus are GROUPS
NOT_PROXIED = {"mActionVertexTool", "mActionVertexToolActiveLayer", "mActionAddFeature"}


def spec_for_action(action):
    """(text, geometry types, kind) for a QGIS action, or None if it is not an advanced tool."""
    if action is None or sip.isdeleted(action):
        return None
    return SPECS.get(action.objectName())


def choose_layer(parent, layers, tool_name):
    """Several layers have a selection: ask which one (returns a layer or None)."""
    names = [lyr.name() for lyr in layers]
    name, ok = QInputDialog.getItem(parent, tool_name, f"{tool_name}: which layer?", names, 0, False)
    return layers[names.index(name)] if ok and name in names else None


def _expand(action):
    """Toolbar entry -> the QAction(s) behind it (drop-down buttons hold several)."""
    if isinstance(action, QWidgetAction):
        w = action.defaultWidget()
        if isinstance(w, QToolButton):
            menu = w.menu()
            if menu is not None and menu.actions():
                return [a for a in menu.actions() if not a.isSeparator()]
            if w.actions():                                  # QGIS fills its drop-downs with addAction
                return [a for a in w.actions() if not a.isSeparator()]
            if w.defaultAction() is not None:
                return [w.defaultAction()]
        return []
    return [] if action.isSeparator() else [action]


class AdvancedTools(QObject):

    def __init__(self, iface, manager, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.manager = manager
        self.toolbar = None
        self.proxies = []          # (proxy QAction, real QAction, spec)
        self.cad_action = None
        self._slots = []           # our connections to QGIS's actions (removed on unload)

    # ------------------------------------------------------------ discovery
    def qgis_actions(self):
        """QGIS's advanced editing actions, in toolbar order (new QGIS tools are picked up too)."""
        found, seen = [], set()
        try:
            bar = self.iface.advancedDigitizeToolBar()
        except Exception:
            bar = None
        if bar is not None:
            for entry in bar.actions():
                for a in _expand(entry):
                    name = a.objectName()
                    if name in SPECS and name not in seen:      # QGIS's own tools only (never other plugins')
                        seen.add(name)
                        found.append(a)
        win = self.iface.mainWindow()
        for name in ORDER:                                   # anything not on the toolbar (moved/hidden)
            if name not in seen:
                a = win.findChild(QAction, name)
                if a is not None:
                    seen.add(name)
                    found.append(a)
        rank = {n: i for i, n in enumerate(ORDER)}
        return sorted(found, key=lambda a: rank[a.objectName()])

    def _cad(self):
        try:
            return self.iface.cadDockWidget().enableAction()
        except Exception:
            return None

    # ------------------------------------------------------------ toolbar (grouped drop-downs)
    def build(self, toolbar, before=None, cad_before=None):
        """
        Put the advanced tools into the given toolbar as a few drop-down buttons (GROUPS).
        Each button shows the tool used last: click it to use that tool again, or open the
        arrow to pick another one. The drop-downs go before the action 'before' (or at the end); QGIS's own
        Advanced Digitizing switch goes before 'cad_before' (default: with the drop-downs).
        """
        self.toolbar = toolbar
        self._widget_actions = []
        self.groups = []                     # (QToolButton, [proxies])
        try:
            actions = {a.objectName(): a for a in self.qgis_actions()}
        except Exception as err:             # never fail silently
            QgsMessageLog.logMessage(f"Could not read QGIS's advanced editing tools: {err}", "Edit Session",
                                     Qgis.MessageLevel.Warning)
            actions = {}
        if not actions:
            QgsMessageLog.logMessage("No QGIS advanced editing tools were found.", "Edit Session",
                                     Qgis.MessageLevel.Warning)
        self.cad_action = self._cad()
        if self.cad_action is not None:      # QGIS's own Advanced Digitizing panel switch
            where = cad_before if cad_before is not None else before
            if where is not None:
                toolbar.insertAction(where, self.cad_action)
            else:
                toolbar.addAction(self.cad_action)
        button = QToolButton(toolbar)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.setAutoRaise(True)
        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        all_proxies = []
        for title, icon_hint, names in GROUPS:
            proxies = []
            for name in names:
                real = actions.get(name)
                if real is None:
                    continue
                try:
                    proxies.append(self._add_proxy(real))
                except Exception as err:
                    QgsMessageLog.logMessage(f"Advanced tool '{name}' skipped: {err}", "Edit Session",
                                             Qgis.MessageLevel.Warning)
            if not proxies:
                continue
            sub = menu.addMenu(title)
            sub.setToolTipsVisible(True)
            for proxy in proxies:
                sub.addAction(proxy)
                proxy.triggered.connect(lambda checked=False, b=button, p=proxy: self._set_default(b, p))
            all_proxies += proxies
        if not all_proxies:
            button.deleteLater()
            return self.groups
        button.setMenu(menu)
        button.setDefaultAction(all_proxies[0])
        button.setToolTip(BUTTON_TITLE)
        button.setObjectName("KgaEditorGroup_ModifyFeatures")
        wa = toolbar.insertWidget(before, button) if before is not None else toolbar.addWidget(button)
        self._widget_actions.append(wa)
        self.groups.append((button, all_proxies))
        return self.groups

    def _real_toggled(self, proxy, on):
        self._mirror(proxy, on)
        if on and not sip.isdeleted(proxy):                  # started from QGIS's own toolbar too:
            for button, proxies in getattr(self, "groups", []):   # show it (pressed) on its group button
                if proxy in proxies:
                    self._set_default(button, proxy)

    @staticmethod
    def _set_default(button, proxy):
        if not sip.isdeleted(button) and not sip.isdeleted(proxy) and button.defaultAction() is not proxy:
            button.setDefaultAction(proxy)   # the button now shows (and repeats) the tool used last

    def group_of(self, real_name):
        for button, proxies in self.groups:
            for proxy in proxies:
                if proxy.objectName() == "mActionKgaEditorAdv_" + real_name:
                    return button
        return None

    def _add_proxy(self, real):
        spec = SPECS[real.objectName()]
        proxy = QAction(real.icon(), spec[0], self.iface.mainWindow())
        proxy.setObjectName("mActionKgaEditorAdv_" + real.objectName())
        proxy.setToolTip(f"{spec[0]} (any editable layer) - {self._hint(spec)}")
        proxy.setCheckable(real.isCheckable())
        proxy.triggered.connect(lambda checked=False, r=real, s=spec: self.start(r, s))
        if real.isCheckable():
            on_toggle = (lambda on, p=proxy: self._real_toggled(p, on))
            real.toggled.connect(on_toggle)
            self._slots.append((real.toggled, on_toggle))
            proxy.setChecked(real.isChecked())
        on_change = (lambda r=real, p=proxy: self._copy_icon(r, p))
        real.changed.connect(on_change)
        self._slots.append((real.changed, on_change))
        self.proxies.append((proxy, real, spec))
        return proxy

    @staticmethod
    def _hint(spec):
        kind = spec[2]
        if kind == "selection":
            return "works on the layer that has selected features"
        if kind == "one_selected":
            return "select one feature (in any layer) first"
        if kind == "fixed":
            return "draw across a feature of any editable layer; the layer is chosen when you finish"
        return "click a feature of any editable layer"

    @staticmethod
    def _mirror(proxy, on):
        if not sip.isdeleted(proxy) and proxy.isChecked() != on:
            proxy.blockSignals(True)
            proxy.setChecked(on)
            proxy.blockSignals(False)

    @staticmethod
    def _copy_icon(real, proxy):
        if not sip.isdeleted(real) and not sip.isdeleted(proxy):
            proxy.setIcon(real.icon())                       # QGIS changes it for point/line/polygon

    def remove(self):
        for signal, slot in self._slots:                     # only OUR connections - never QGIS's
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._slots = []
        bar = self.toolbar
        if bar is not None and not sip.isdeleted(bar):
            for wa in getattr(self, "_widget_actions", []):
                if not sip.isdeleted(wa):
                    bar.removeAction(wa)
            if self.cad_action is not None and not sip.isdeleted(self.cad_action):
                bar.removeAction(self.cad_action)            # QGIS's own action: never deleted
        for button, proxies in getattr(self, "groups", []):
            if not sip.isdeleted(button):
                button.deleteLater()
        for proxy, real, spec in self.proxies:
            if not sip.isdeleted(proxy):
                proxy.deleteLater()
        self.proxies, self.groups, self._widget_actions = [], [], []
        self.toolbar = None

    # ------------------------------------------------------------ state
    def candidates(self, spec):
        """Editable layers that fit the tool, best first."""
        types = spec[1]
        canvas = self.iface.mapCanvas()
        visible = [lyr for lyr in self.manager.selectable_layers(canvas) if lyr.isEditable()]
        others = [lyr for lyr in self.manager.vector_layers()
                  if lyr.isEditable() and lyr not in visible]
        layers = [lyr for lyr in visible + others if lyr.geometryType() in types]
        kind = spec[2]
        if kind == "selection":
            layers = [lyr for lyr in layers if lyr.selectedFeatureCount() > 0]
        elif kind == "one_selected":
            one = [lyr for lyr in layers if lyr.selectedFeatureCount() == 1]
            layers = one + [lyr for lyr in layers if lyr not in one]
        else:
            with_sel = [lyr for lyr in layers if lyr.selectedFeatureCount()]
            layers = with_sel + [lyr for lyr in layers if lyr not in with_sel]
        active = self.iface.activeLayer()
        others_selected = any(lyr.selectedFeatureCount() for lyr in layers if lyr is not active)
        if kind == "one_selected":
            keep = active in layers and not (active.selectedFeatureCount() != 1
                                             and any(lyr.selectedFeatureCount() == 1 for lyr in layers))
        elif kind == "fixed":                                # Split: the selection decides what is split
            keep = active in layers and (active.selectedFeatureCount() > 0 or not others_selected)
        else:
            keep = active in layers
        if keep:
            layers.remove(active)
            layers.insert(0, active)                         # keep the layer you are on if it fits
        return layers

    def update_enabled(self, on):
        for proxy, real, spec in self.proxies:
            if sip.isdeleted(proxy):
                continue
            ok = on and (spec[2] == "selection" or any(
                lyr.isEditable() and lyr.geometryType() in spec[1] for lyr in self.manager.vector_layers()))
            proxy.setEnabled(ok)
        for button, proxies in getattr(self, "groups", []):
            if sip.isdeleted(button):
                continue
            current = button.defaultAction()
            if current is not None and not current.isEnabled():
                usable = next((p for p in proxies if p.isEnabled()), None)
                if usable is not None:
                    button.setDefaultAction(usable)      # don't grey out the whole group

    # ------------------------------------------------------------ start a tool
    def start(self, real, spec):
        name = spec[0]
        bar = self.iface.mainWindow().statusBar()
        if not self.manager.is_running():
            bar.showMessage("Edit Session is off - turn it on to use the advanced tools.", 5000)
            return False
        layers = self.candidates(spec)
        if spec[2] == "selection":
            if not layers:
                bar.showMessage(f"{name}: select the features first (Select (all layers)).", 6000)
                self._uncheck(real)
                return False
            active = self.iface.activeLayer()
            if active not in layers and len(layers) > 1:
                chosen = choose_layer(self.iface.mainWindow(), layers, name)
                if chosen is None:
                    return False
                layers = [chosen]
        previous = self.iface.activeLayer()
        for layer in layers:
            if self.iface.activeLayer() is not layer:
                self.iface.setActiveLayer(layer)             # QGIS enables its tools for this layer
            if real.isEnabled():
                if real.isCheckable() and real.isChecked() and spec[2] != "selection":
                    self._mirror(self._proxy_for(real), True)  # already active: just keep it on
                else:
                    real.trigger()
                if spec[2] == "one_selected" and layer.selectedFeatureCount() != 1:
                    bar.showMessage(f"{name}: select ONE feature first (Select (all layers)), then draw the part.", 8000)
                elif spec[2] == "fixed":
                    bar.showMessage(f"{name}: draw the line across the feature - when you finish (right-click), "
                                    f"the layer it crosses is used (selected features first).", 8000)
                elif spec[2] != "selection":
                    bar.showMessage(f"{name}: {self._hint(spec)} - it is used automatically.", 6000)
                return True
        if previous is not None and self.iface.activeLayer() is not previous:
            self.iface.setActiveLayer(previous)
        self._uncheck(real)
        bar.showMessage(f"{name}: no editable layer can use this tool "
                        f"({'/'.join(self._type_names(spec[1]))} layers"
                        f"{', with point rotation/offset set in the symbology' if spec[1] == (PT,) else ''}).", 8000)
        return False

    def _proxy_for(self, real):
        return next((p for p, r, s in self.proxies if r is real), None)

    def _uncheck(self, real):
        proxy = self._proxy_for(real)
        if proxy is not None and proxy.isCheckable():
            self._mirror(proxy, real.isChecked())

    @staticmethod
    def _type_names(types):
        return [{PT: "point", LN: "line", PG: "polygon"}[t] for t in types]


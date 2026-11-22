"""Tool Bag: a floating bag of favourite QGIS tools that can shrink into a bubble on the map.

Runs inside KGA Toolbox (`embedded=True`): the toolbar button is then owned by KgaToolsPlugin,
which puts `action` in the Bags drop-down of the KGA Layer toolbar, and nothing is added to the
Plugins menu or toolbar. The settings stay under ``ToolBag/`` so a bag set up with the
stand-alone plugin keeps its tools, design and position.
"""

import json
from contextlib import suppress

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QObject, QPoint, QRect, QRectF, QTimer
from qgis.PyQt.QtWidgets import QApplication, QMenu, QMessageBox

from .compat import OWN_PREFIX, PLUGIN_NAME, QAction, alive, message_level, ptr, to_bool
from .bubble import (BUBBLE_POSITIONS, DEFAULT_BUBBLE, DEFAULT_BUBBLE_POSITION, MAX_BUBBLE, MIN_BUBBLE,
                     BubbleButton, FlyOverlay)
from .panel import ToolPanel, icon
from .themes import THEMES
from .picker import NativeToolbarHook, PickMode
from .registry import ActionRegistry

MENU = "&Tool Bag"
KEY = "ToolBag/"
OLD_KEY = "CustomToolWorkspace/panel/"
FIRST_RUN = ["action:mActionToggleEditing", "action:mActionAddFeature", "action:mActionVertexTool",
             "action:mActionSelectFeatures", "action:mActionIdentify"]


class ToolBagPlugin(QObject):
    def __init__(self, iface, embedded=False, toggle_icon=None, group=None):
        super().__init__()
        self.group = group       # BubbleGroup: the bubbles of all the bags in one column (or None)
        self._genie_w = None
        self._genie_tween = None
        self.setObjectName(OWN_PREFIX + "_plugin")
        self.iface = iface
        self.embedded = embedded
        self._toggle_icon = toggle_icon
        self.action = None       # the show / hide toggle (toolbar button)
        self.settings = QgsSettings()
        self.tools = []          # [{"id": stable id, "text": label}]
        self.labels = True
        self.view_mode = "list"     # "list" | "grid_icons" | "grid_labels"
        self.columns = 1           # columns of tools (list or grid), 1-3
        self.icon_size = 20
        self.theme = "sky"
        self.opacity = 1.0
        self.bg_transparency = 0.0
        self.handle_style = "handle"
        self.compact = False
        self.bubble_visible = True
        self.bubble_position = DEFAULT_BUBBLE_POSITION
        self.bubble_size = DEFAULT_BUBBLE
        self.panel = None
        self.bubble = None
        self.flyer = None
        self._state = "hidden"   # "open" (bag on screen) | "bubble" (minimized) | "hidden"
        self.add_dialog = None
        self._unloading = False
        self._geometry_restored = False
        self._watched = set()
        self._retry_delay = 3000

    # ================================================================ lifecycle
    def initGui(self):
        self.registry = ActionRegistry(self.iface, self)
        self.registry.scan()
        self._load()

        mw = self.iface.mainWindow()
        self.panel = ToolPanel(self, mw)
        self.flyer = FlyOverlay(mw)
        self.bubble = BubbleButton(self, self.iface.mapCanvas())
        self.bubble.hide()
        if self.group is not None:
            self.group.register("tool", self.bubble)
            self.group.register_style("tool", self.style_key, self.set_style_key)
            self.group.settingsChanged.connect(self._on_common)
            self._on_common("bubble")

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(0)
        self._rebuild_timer.timeout.connect(self.rebuild)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_missing)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(400)
        self._geo_timer.timeout.connect(self.save_geometry)

        self.picker = PickMode(self.iface, self)
        self.picker.picked.connect(self._on_picked)
        self.picker.rejected.connect(lambda msg: self.notify(msg, "Warning"))
        self.picker.finished.connect(lambda: self.panel.set_picking(False) if alive(self.panel) else None)
        self.hook = NativeToolbarHook(self, self)
        self.hook.attach(self.registry.native_toolbars())

        self.act_toggle = QAction(self._toggle_icon or icon("plugin.svg"),
                                  "Tool Bag" if self.embedded else "Show Tool Bag", mw)
        self.act_toggle.setObjectName(OWN_PREFIX + "_act_toggle")
        self.act_toggle.setCheckable(True)
        self.act_toggle.setToolTip(
            "Tool Bag: a floating bag of your favourite QGIS tools. Minimize it to a bubble on the map.")
        self.act_toggle.toggled.connect(lambda on: self.set_panel_visible(on))
        self.action = self.act_toggle
        self.act_help = QAction(icon("help.svg"), "Help", mw)
        self.act_help.setObjectName(OWN_PREFIX + "_act_help")
        self.act_help.triggered.connect(self.show_help)
        if not self.embedded:                    # embedded: KgaToolsPlugin places the button itself
            self.iface.addPluginToMenu(MENU, self.act_toggle)
            self.iface.addPluginToMenu(MENU, self.act_help)
            self.iface.addToolBarIcon(self.act_toggle)

        if hasattr(self.iface, "initializationCompleted"):
            self.iface.initializationCompleted.connect(self._on_initialized)
        self.rebuild()
        self._show_when_ready(0)

    def unload(self):
        self._unloading = True
        with suppress(Exception):
            self.picker.stop()
        self.hook.detach()
        for t in (self._rebuild_timer, self._retry_timer, self._geo_timer):
            t.stop()
        if hasattr(self.iface, "initializationCompleted"):
            with suppress(Exception):
                self.iface.initializationCompleted.disconnect(self._on_initialized)
        self._settle()
        if self.group is not None:
            with suppress(Exception):
                self.group.settingsChanged.disconnect(self._on_common)
            self.group.unregister("tool")
        if self._genie_w is not None and alive(self._genie_w):
            self._genie_w.hide()
            self._genie_w.deleteLater()
        if alive(self.flyer):
            self.flyer._done = None
            self.flyer._anim.stop()
            self.flyer.hide()
            self.flyer.deleteLater()
        if alive(self.bubble):
            self.bubble.hide()
            self.bubble.deleteLater()
        if alive(self.panel):
            if self.panel.isVisible():
                self.save_geometry()
            self.panel.hide()
            self.panel.deleteLater()
        if self.add_dialog is not None and alive(self.add_dialog):
            self.add_dialog.close()
            self.add_dialog.deleteLater()
        if not self.embedded:
            for a in (self.act_toggle, self.act_help):
                self.iface.removePluginMenu(MENU, a)
            self.iface.removeToolBarIcon(self.act_toggle)
        self.registry.unload()

    def _on_initialized(self):
        self.refresh()

    def refresh(self):
        """Look for tools again (e.g. after KGA Toolbox has built its toolbars)."""
        if self._unloading or not alive(self.panel):
            return
        self.registry.scan()
        self.hook.attach(self.registry.native_toolbars())
        self.schedule_rebuild()

    def _show_when_ready(self, attempt):
        """Show the panel once the QGIS window itself is on screen."""
        if self._unloading or not alive(self.panel):
            return
        visible = to_bool(self.settings.value(KEY + "visible", True), True)
        if not self.iface.mainWindow().isVisible() and attempt < 120:
            QTimer.singleShot(250, lambda: self._show_when_ready(attempt + 1))
            return
        state = str(self.settings.value(KEY + "state", "") or "")
        if state not in ("open", "bubble", "hidden"):
            state = "open" if visible else "hidden"
        if state == "bubble" and not self.bubble_visible:
            state = "open"
        self._set_state("hidden")
        if state == "open":
            self.open_bag(animate=False)
        elif state == "bubble":
            self._set_state("bubble")
            self.bubble.apply_size()
            self.bubble.reposition()
            self.bubble.show()
            self.bubble.pop_in()

    # ================================================================ settings
    def _migrate_old_name(self):
        """Copy settings saved under the plugin's former name (Custom Tool Workspace 2.x)."""
        s = self.settings
        if s.value(KEY + "tools", None) is not None:
            return
        for name in ("tools", "labels", "icon_size", "geometry", "visible"):
            value = s.value(OLD_KEY + name, None)
            if value is not None:
                s.setValue(KEY + name, value)

    def _load(self):
        self._migrate_old_name()
        s = self.settings
        raw = s.value(KEY + "tools", None)
        tools = None
        if raw:
            try:
                tools = [t for t in json.loads(raw) if isinstance(t, dict) and t.get("id")]
            except Exception:
                tools = None
        if tools is None:
            tools = self._migrate_v1() or [
                {"id": tid, "text": self.registry.display_text(tid)}
                for tid in FIRST_RUN if self.registry.action_for(tid) is not None]
        seen, self.tools = set(), []
        for t in tools:
            if t["id"] not in seen:
                seen.add(t["id"])
                self.tools.append({"id": str(t["id"]), "text": str(t.get("text") or "")})
        self.labels = to_bool(s.value(KEY + "labels", True), True)
        vm = str(s.value(KEY + "view_mode", "list"))
        self.view_mode = vm if vm in ("list", "grid_icons", "grid_labels") else "list"
        try:
            self.columns = max(1, min(3, int(s.value(KEY + "tool_columns", 1))))
        except (TypeError, ValueError):
            self.columns = 1
        try:
            self.icon_size = max(12, min(64, int(s.value(KEY + "icon_size", 20))))
        except (TypeError, ValueError):
            self.icon_size = 20
        th = str(s.value(KEY + "theme", "sky"))
        self.theme = th if th in THEMES else "sky"
        try:
            self.opacity = max(0.4, min(1.0, float(s.value(KEY + "opacity", 1.0))))
        except (TypeError, ValueError):
            self.opacity = 1.0
        try:
            self.bg_transparency = max(0.0, min(0.9, float(s.value(KEY + "bg_transparency", 0.0))))
        except (TypeError, ValueError):
            self.bg_transparency = 0.0
        from .themes import HANDLE_STYLES
        hs = str(s.value(KEY + "handle_style", "") or "")
        if hs not in HANDLE_STYLES:  # older versions stored a simple on/off
            hs = "handle" if to_bool(s.value(KEY + "show_handle", True), True) else "none"
        self.handle_style = hs
        self.compact = to_bool(s.value(KEY + "compact", False), False)
        self.bubble_visible = to_bool(s.value(KEY + "bubble_visible", True), True)
        bp = str(s.value(KEY + "bubble_position", DEFAULT_BUBBLE_POSITION))
        self.bubble_position = bp if bp in BUBBLE_POSITIONS else DEFAULT_BUBBLE_POSITION
        try:
            self.bubble_size = max(MIN_BUBBLE, min(MAX_BUBBLE, int(s.value(KEY + "bubble_size", DEFAULT_BUBBLE))))
        except (TypeError, ValueError):
            self.bubble_size = DEFAULT_BUBBLE
        if self.group is not None:               # the bubbles' place and size are shared (Settings > Bubbles)
            self._load_bubble_geometry()
        self.save()

    def _migrate_v1(self):
        """Take over the tools of the active workspace from version 1.x, if present."""
        raw = self.settings.value("CustomToolWorkspace/config", "")
        try:
            data = json.loads(raw) if raw else None
            ws = next(w for w in data["workspaces"] if w["uid"] == data.get("active"))
            return [{"id": it["id"], "text": it.get("text", "")}
                    for g in ws["groups"] for it in g["items"] if it.get("type") == "tool"]
        except Exception:
            return None

    def save(self):
        s = self.settings
        s.setValue(KEY + "tools", json.dumps(self.tools, ensure_ascii=False))
        s.setValue(KEY + "labels", self.labels)
        s.setValue(KEY + "view_mode", self.view_mode)
        s.setValue(KEY + "tool_columns", self.columns)
        s.setValue(KEY + "icon_size", self.icon_size)
        s.setValue(KEY + "theme", self.theme)
        s.setValue(KEY + "opacity", self.opacity)
        s.setValue(KEY + "bg_transparency", self.bg_transparency)
        s.setValue(KEY + "handle_style", self.handle_style)
        s.setValue(KEY + "compact", self.compact)
        s.setValue(KEY + "bubble_visible", self.bubble_visible)
        s.setValue(KEY + "bubble_position", self.bubble_position)
        s.setValue(KEY + "bubble_size", self.bubble_size)

    def geometry_changed(self):
        if self._geometry_restored and not self._unloading:
            self._geo_timer.start()

    def save_geometry(self):
        if alive(self.panel) and self.panel.isVisible():
            g = self.panel.geometry()
            self.settings.setValue(KEY + "geometry", [g.x(), g.y(), g.width(), g.height()])

    def _restore_geometry(self):
        mw = self.iface.mainWindow()
        rect = None
        raw = self.settings.value(KEY + "geometry", None)
        try:
            x, y, w, h = [int(v) for v in raw]
            rect = QRect(x, y, max(48, w), max(90, h))
        except Exception:
            rect = None
        if rect is None or QApplication.screenAt(rect.center()) is None:
            mg = mw.geometry()
            w, h = 240, min(580, max(320, mg.height() - 200))
            rect = QRect(mg.right() - w - 60, mg.top() + 140, w, h)
        self.panel.setGeometry(rect)
        self._geometry_restored = True

    # ================================================================ panel
    @property
    def _open(self):
        return self._state == "open"

    def _set_state(self, state):
        self._state = state
        self.settings.setValue(KEY + "state", state)
        self.settings.setValue(KEY + "visible", state == "open")
        on = state == "open"
        if self.group is not None:
            self.group.set_present("tool", state == "bubble")
        if self.act_toggle.isChecked() != on:
            self.act_toggle.blockSignals(True)
            self.act_toggle.setChecked(on)
            self.act_toggle.blockSignals(False)
        if alive(self.bubble):
            self.bubble.update()

    def set_panel_visible(self, on, animate=True):
        """Toolbar icon, menu and the \u2715 button: show the bag, or hide it completely."""
        if on:
            self.open_bag(animate)
        else:
            self.hide_bag(animate)

    def toggle_bag(self):
        """Bubble click: the bag comes out of the bubble."""
        if self._state == "bubble":
            self.open_bag()
        else:
            self.minimize_bag()

    def open_bag(self, animate=True):
        if not alive(self.panel):
            return
        self._settle()                           # settle an animation that is still running
        if self._state == "open":
            self.panel.raise_()
            return
        from_bubble = self._state == "bubble" and alive(self.bubble)
        if not self._geometry_restored:
            self._restore_geometry()
        self.panel.fit_height()
        circle = None
        if from_bubble:
            if self.bubble_visible:
                self._anchor_to_bubble()         # the bag appears where the bubble was
            circle = QRectF(self.bubble.global_circle())
            self.bubble.hide()
        elif self.group is not None and self.bubble_visible:
            circle = self.group.slot_rect("tool")      # from the toolbar: out of where its bubble sits
            if circle is not None:
                self._anchor_to_bubble(circle.toAlignedRect())
        self._set_state("open")
        if not (animate and self._fly(True, circle)):
            self.panel.show()
            self.panel.raise_()

    def minimize_bag(self, animate=True):
        """Tuck the bag into the bubble (or just hide it when the bubble is turned off)."""
        if not alive(self.panel):
            return
        self._settle()
        if self._state != "open":
            return
        if not (self.bubble_visible and alive(self.bubble)):
            self.hide_bag(animate)
            return
        self.save_geometry()
        if self.picker.active:
            self.picker.stop()
        self.bubble.apply_size()
        if self.group is not None:
            self.group.set_present("tool", True)     # the other bubbles make room for it
        self.bubble.reposition()
        circle = QRectF(self.bubble.global_circle())
        self._set_state("bubble")
        bubble = self.bubble

        def landed():
            if alive(bubble) and self._state == "bubble":
                bubble.show()
                bubble.pop_in(0.75)
        if not (animate and self._fly(False, circle, landed)):
            self.panel.hide()
            landed()

    def hide_bag(self, animate=True):
        if not alive(self.panel):
            return
        self._settle()
        was = self._state
        if was == "open":
            self.save_geometry()
            if self.picker.active:
                self.picker.stop()
        elif was == "bubble" and alive(self.bubble):
            self.bubble.hide()
        self._set_state("hidden")
        slot = self.group.slot_rect("tool") if (self.group is not None and self.bubble_visible) else None
        if was == "open" and not (animate and self._fly(False, slot)):
            self.panel.hide()

    def _anchor_to_bubble(self, circle=None):
        """Place the bag so that it grows out of the bubble's spot on the map
        (top-left bubble -> bag's top-left corner there, bottom-right -> its bottom-right, ...)."""
        canvas = self.iface.mapCanvas()
        area = QRect(canvas.mapToGlobal(QPoint(0, 0)), canvas.size())
        circle = circle if circle is not None else self.bubble.global_circle()
        panel = self.panel
        left, top, right, bottom = panel.margins
        g = panel.geometry()
        vis_w, vis_h = g.width() - left - right, g.height() - top - bottom
        _label, hx, vy = BUBBLE_POSITIONS.get(self.bubble_position, BUBBLE_POSITIONS[DEFAULT_BUBBLE_POSITION])
        x = (circle.left(), circle.center().x() - vis_w // 2, circle.right() + 1 - vis_w)[hx]
        y = (circle.top(), circle.center().y() - vis_h // 2, circle.bottom() + 1 - vis_h)[vy]
        if self.group is not None:
            side = self.group.beside_x("tool", circle.center().x(), circle.width() / 2.0, vis_w)
            if side is not None:                 # other bubbles are there: open beside the column
                x = side
        x = max(area.left() + 4, min(area.right() - 3 - vis_w, x))
        y = max(area.top() + top, min(area.bottom() - 3 - vis_h, y))
        panel.setGeometry(x - left, y - top, g.width(), g.height())

    def _on_common(self, key):
        """A setting the three bags share changed (or must be taken over at start-up)."""
        if self.group is None or self._unloading:
            return
        if key == "bubble":
            on = self.group.get("bubble")
            if on != self.bubble_visible:
                self.set_bubble(visible=on)
        elif key == "bubble_geometry" and alive(self.bubble):
            self._load_bubble_geometry()
            self.bubble.apply_size()
            self.bubble.reposition()
            self.bubble.update()

    def style_key(self):
        """This bag's colour in the shared menu's naming (the Tool Bag calls white 'classic')."""
        return "white" if self.theme == "classic" else self.theme

    def set_style_key(self, key):
        self.set_style(theme="classic" if key == "white" else key)

    def _load_bubble_geometry(self):
        if self.group is not None:
            self.bubble_position = self.group.position_of("tool")
            self.bubble_size = max(MIN_BUBBLE, min(MAX_BUBBLE, self.group.size_of("tool")))
            return
        s = self.settings
        bp = str(s.value(KEY + "bubble_position", DEFAULT_BUBBLE_POSITION))
        self.bubble_position = bp if bp in BUBBLE_POSITIONS else DEFAULT_BUBBLE_POSITION
        with suppress(Exception):
            self.bubble_size = max(MIN_BUBBLE, min(MAX_BUBBLE, int(s.value(KEY + "bubble_size", DEFAULT_BUBBLE))))

    def _settle(self):
        """Finish an open / minimize animation that is still running."""
        self.flyer.finish()
        if self._genie_tween is not None:
            tween, self._genie_tween = self._genie_tween, None
            tween.stop(finish=True)

    def _genie(self, opening, circle, landed=None):
        """The macOS-style genie: the bag pours into the bubble and flows back out."""
        from ..symbol_bag.ui.motion import Tween
        from ..symbol_bag.ui.widgets import GenieWidget
        mw = self.iface.mainWindow()
        panel = self.panel
        speed = self.group.speed() if self.group is not None else 1.0
        if not mw.isVisible() or speed <= 0:
            return False
        try:
            if opening:
                panel.ensurePolished()
                panel.layout().activate()
            pix = panel.grab()
        except Exception:
            return False
        if pix.isNull():
            return False
        if self._genie_w is None or not alive(self._genie_w):
            self._genie_w = GenieWidget(mw)
        g = self._genie_w
        g.setup(pix, QRect(panel.geometry()), QRectF(circle))
        if self.group is not None:
            self.group.play_sound("out" if opening else "in")
        g.set_t(1.0 if opening else 0.0)
        g.show_now()                          # exactly over the bag ...
        if not opening:
            panel.hide()                      # ... so hiding it changes nothing on screen

        def frame(t):
            g.set_t(1.0 - t if opening else t)

        def end():
            self._genie_tween = None
            if opening:
                if alive(panel) and self._state == "open":
                    panel.show()              # the bag appears exactly under the last frame
                    panel.raise_()
                g.hide_soon()
            else:
                g.hide()
                g.pixmap = None
                if landed is not None:
                    landed()
        self._genie_tween = Tween(self, (460 if opening else 520) * speed, frame, end).start()
        return True

    def _fly(self, opening, circle, landed=None):
        """Animate the bag between the bubble and its place on screen. False = no animation."""
        from ..bag_group import animation_style
        if self.group is not None and self.group.speed() <= 0:
            return False                         # animations are off
        if circle is not None and animation_style() == "genie" and self._genie(opening, circle, landed):
            return True
        mw = self.iface.mainWindow()
        panel = self.panel
        if not mw.isVisible() or not alive(self.flyer):
            return False
        try:
            if opening:
                panel.ensurePolished()
                panel.layout().activate()
            pix = panel.grab()
        except Exception:
            return False
        if pix.isNull():
            return False
        bag = QRectF(panel.geometry())
        if circle is None:
            c = bag.center()
            circle = QRectF(c.x() - 14, c.y() - 14, 28, 28)
        if opening:
            def done():
                if alive(panel) and self._state == "open":
                    panel.show()
                    panel.raise_()
        else:
            panel.hide()
            done = landed
        self.flyer.play(pix, circle, bag, opening, panel.windowOpacity(), done)
        return True

    # ---------------------------------------------------------------- bubble settings
    def set_bubble(self, visible=None, position=None, size=None):
        if visible is not None:
            self.bubble_visible = bool(visible)
            if self.group is not None:
                self.group.set("bubble", self.bubble_visible)
        if position is not None and position in BUBBLE_POSITIONS:
            self.bubble_position = position
        if size is not None:
            self.bubble_size = max(MIN_BUBBLE, min(MAX_BUBBLE, int(size)))
        if self.group is not None and (position is not None or size is not None):
            from ..bag_group import set_override          # this bubble's own place / size
            if position is not None:
                set_override("tool", "position", position)
            if size is not None:
                set_override("tool", "size", self.bubble_size)
        self.save()
        if not alive(self.bubble):
            return
        self.bubble.apply_size()
        self.bubble.reposition()
        if self._state == "bubble" and not self.bubble_visible:
            self.open_bag()                      # no bubble any more: bring the bag back
        self.bubble.update()

    def add_bubble_menu(self, menu):
        """The bubble entries shared by the bag menu and the bubble's own menu."""
        show = menu.addAction("Minimize to a Bubble")
        show.setCheckable(True)
        show.setChecked(self.bubble_visible)
        show.toggled.connect(lambda on: self.defer(lambda: self.set_bubble(visible=on)))
        size_menu = menu.addMenu("Bubble Size")
        for label, px in (("Tiny", 28), ("Small", 34), ("Medium", 40), ("Large", 52), ("Extra large", 64)):
            act = size_menu.addAction(f"{label} ({px} px)", lambda v=px: self.defer(lambda: self.set_bubble(size=v)))
            act.setCheckable(True)
            act.setChecked(self.bubble_size == px)
        sub_menu = menu.addMenu("Bubble Position")
        for key, (label, _h, _v) in BUBBLE_POSITIONS.items():
            act = sub_menu.addAction(label, lambda k=key: self.defer(lambda: self.set_bubble(position=k)))
            act.setCheckable(True)
            act.setChecked(self.bubble_position == key)

    def show_bubble_menu(self, global_pos):
        menu = QMenu(self.iface.mainWindow())
        menu.setObjectName(OWN_PREFIX + "_bubblemenu")
        menu.addAction("Open Tool Bag", lambda: self.defer(self.open_bag))
        menu.addAction("Hide Tool Bag", lambda: self.defer(self.hide_bag))
        menu.addSeparator()
        if self.group is not None:
            from ..bag_common import fill_common_menu
            fill_common_menu(menu, self.group, self.style_key, self.set_style_key, key="tool")
        else:
            self.add_bubble_menu(menu)
        from .compat import qexec
        qexec(menu, global_pos)

    def defer(self, fn):
        QTimer.singleShot(0, fn)

    def schedule_rebuild(self, *_):
        if not self._unloading:
            self._rebuild_timer.start()

    def rebuild(self):
        if self._unloading or not alive(self.panel):
            return
        self.panel.rebuild(self.tools, self.labels, self.icon_size)
        if alive(self.bubble):
            self.bubble.update()
        missing = False
        for b in self.panel.buttons:
            a = b.defaultAction()
            if a is None:
                missing = True
            elif alive(a):
                key = ptr(a)
                if key not in self._watched and not (a.objectName() or "").startswith(OWN_PREFIX):
                    self._watched.add(key)
                    a.destroyed.connect(self._on_action_destroyed)
        if missing and not self._retry_timer.isActive():
            self._retry_timer.start(self._retry_delay)
        if self.add_dialog is not None and alive(self.add_dialog) and self.add_dialog.isVisible():
            self.add_dialog.mark_added()

    def _on_action_destroyed(self, *_):
        self._watched.clear()
        self.schedule_rebuild()

    def _retry_missing(self):
        if self._unloading:
            return
        self.registry.scan()
        self.hook.attach(self.registry.native_toolbars())
        if any(self.registry.action_for(t["id"]) is not None
               for b, t in zip(self.panel.buttons, self.tools) if b.defaultAction() is None):
            self._retry_delay = 3000
            self.rebuild()
        else:
            self._retry_delay = min(self._retry_delay * 2, 60000)
            self._retry_timer.start(self._retry_delay)

    # ================================================================ editing the tool list
    def tool_name(self, index):
        t = self.tools[index]
        return self.registry.display_text(t["id"], t.get("text"))

    def add_tool(self, tid, text=""):
        if any(t["id"] == tid for t in self.tools):
            self.status(f"“{text or tid}” is already in the Tool Bag.")
            return False
        self.tools.append({"id": tid, "text": text})
        self._changed()
        self.status(f"Added “{text or tid}” to the Tool Bag.")
        return True

    def remove(self, index):
        if 0 <= index < len(self.tools):
            del self.tools[index]
            self._changed()

    def move_to(self, src, dst):
        """Move tool *src* so it sits before the tool currently at *dst*."""
        n = len(self.tools)
        if not (0 <= src < n):
            return
        dst = max(0, min(dst, n))
        item = self.tools.pop(src)
        if dst > src:
            dst -= 1
        self.tools.insert(dst, item)
        self._changed()

    def clear_all(self):
        if QMessageBox.question(
                self.panel, "Remove All Tools",
                "Remove all tools from the panel?\n\nYour QGIS tools themselves are not affected.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes:
            self.tools = []
            self._changed()

    def set_labels(self, on):
        self.labels = bool(on)
        self._changed()
        self.defer(self._fit_width)

    def set_view(self, mode=None, columns=None):
        """List or grid (icons / icons with names), and how many grid columns (1-3)."""
        if mode in ("list", "grid_icons", "grid_labels"):
            if mode != "list" and self.view_mode == "list" and columns is None and self.columns == 1:
                columns = 2                       # a grid with one column is just a list: start with two
            self.view_mode = mode
        if columns is not None:
            self.columns = max(1, min(3, int(columns)))
        self._changed()
        self.defer(self._fit_width)

    def _fit_width(self):
        """Slim bag for icons only, comfortable width when names are shown."""
        if not alive(self.panel):
            return
        self.rebuild()
        for lay in (self.panel.header.layout(), self.panel.bag.layout(), self.panel.layout()):
            lay.invalidate()
            lay.activate()
        wanted = self.panel.preferred_width()
        multi_before = getattr(self, "_fit_multi", False)
        self._fit_multi = wanted is not None
        if wanted is not None:
            width = max(wanted, self.panel.minimumSizeHint().width())
        elif self.labels:
            width = 230 if multi_before else max(self.panel.width(), 230)
        else:
            width = self.panel.minimumSizeHint().width() + 4
        self.panel.resize(width, self.panel.height())

    def panel_supports_transparency(self):
        return alive(self.panel) and self.panel.translucent

    def set_style(self, theme=None, opacity=None, handle_style=None, compact=None, bg_transparency=None):
        """Change the bag design live (from the Bag Style picker)."""
        old_top = self.panel.margins[1] if alive(self.panel) else 0
        if theme is not None:
            self.theme = theme
        if opacity is not None:
            self.opacity = max(0.4, min(1.0, float(opacity)))
        if bg_transparency is not None:
            self.bg_transparency = max(0.0, min(0.9, float(bg_transparency)))
        if handle_style is not None:
            from .themes import HANDLE_STYLES
            self.handle_style = handle_style if handle_style in HANDLE_STYLES else "handle"
        if compact is not None:
            self.compact = bool(compact)
        self.save()
        if not alive(self.panel):
            return
        self.panel.apply_style()
        if alive(self.bubble):
            self.bubble.update()
        delta = self.panel.margins[1] - old_top
        if delta:  # keep the bag itself in place when the handle appears/disappears
            g = self.panel.geometry()
            self.panel.setGeometry(g.x(), g.y() - delta, g.width(), g.height())
        if compact is not None:
            self.schedule_rebuild()

    def set_icon_size(self, size):
        self.icon_size = int(size)
        self._changed()
        if self.view_mode != "list" or self.columns > 1:
            self.defer(self._fit_width)

    def _changed(self):
        self.save()
        self._retry_delay = 3000
        self.schedule_rebuild()

    # ================================================================ adding tools
    def toggle_pick(self, on):
        if on:
            if alive(self.panel) and not self.panel.isVisible():
                self.set_panel_visible(True)
            self.picker.start("Tool Bag")
        else:
            self.picker.stop()
        if alive(self.panel):
            self.panel.set_picking(self.picker.active)

    def _on_picked(self, action):
        self.add_action(action)

    def add_action(self, action):
        tid = self.registry.id_for_action(action)
        if tid is None:
            self.notify("This item cannot be added: it is not a reusable QGIS tool.", "Warning")
            return
        text = self.registry.display_text(tid)
        if self.add_tool(tid, text):
            self.notify(f"\u201c{text}\u201d is now in your Tool Bag.", "Success", 3)
        else:
            self.notify(f"\u201c{text}\u201d is already in your Tool Bag.", "Info", 3)

    def after_tool_click(self, action):
        """After choosing a map tool, give keyboard focus back to the map so Esc,
        Backspace, Enter and shortcuts work immediately while digitizing."""
        if not alive(action) or not action.isCheckable():
            return
        try:
            canvas = self.iface.mapCanvas()
        except Exception:
            return
        if canvas is None or QApplication.activeModalWidget() is not None:
            return
        mw = self.iface.mainWindow()
        if QApplication.activeWindow() in (None, self.panel):
            mw.activateWindow()
        canvas.setFocus()

    def show_add_menu_for_action(self, action, global_pos):
        """Shift + right-click on any QGIS toolbar button."""
        self.add_action(action)
        if alive(self.panel) and not self.panel.isVisible():
            self.set_panel_visible(True)

    def open_add_dialog(self):
        from .add_dialog import AddToolDialog
        if self.add_dialog is None or not alive(self.add_dialog):
            self.add_dialog = AddToolDialog(self, self.iface.mainWindow())
        self.add_dialog.populate()
        self.add_dialog.show()
        self.add_dialog.raise_()
        self.add_dialog.activateWindow()

    # ================================================================ feedback
    def notify(self, text, level="Info", duration=5):
        self.iface.messageBar().pushMessage(PLUGIN_NAME, text, level=message_level(level), duration=duration)

    def status(self, text, ms=3500):
        with suppress(Exception):
            self.iface.statusBarIface().showMessage(text, ms)

    def report_missing(self, tool):
        name = tool.get("text") or tool["id"]
        self.notify(f"“{name}” is not available right now – the plugin or Processing provider "
                    "that offers it may be disabled. It comes back automatically when available. "
                    "Right-click it to remove it from the panel.", "Warning", 7)

    def show_help(self):
        if self.embedded:                         # KGA Toolbox: the documentation page in the browser
            from ...branding import open_docs
            open_docs("tool_bag")
            return
        QMessageBox.information(self.panel if alive(self.panel) and self.panel.isVisible()
                                else self.iface.mainWindow(), "Tool Bag – Help", HELP_HTML)


HELP_HTML = """
<h3>Tool Bag</h3>
<p>A small panel that floats over QGIS and holds the tools you use most.</p>
<p><b>Put a tool in the bag</b></p>
<ol>
<li>Click <b>＋</b> at the top of the bag (the cursor becomes a cross).</li>
<li>Click any QGIS toolbar button or menu item – e.g. <i>Add Ring</i>, <i>Split Features</i>,
    <i>Vector › Geoprocessing › Buffer</i>. It goes into the bag instead of running.</li>
<li>Click anywhere else (or press <b>Esc</b>) to cancel.</li>
</ol>
<p>Other ways: <b>Shift + right-click</b> any QGIS toolbar button, or ⚙ › <i>Add Tool from List</i>.</p>
<p><b>Bubble</b> – the <b>–</b> button shrinks the bag into a bubble on the map; click the bubble to open
the bag again. <b>✕</b> hides it completely (use the toolbar button to bring it back).</p>
<p><b>Organise</b> – drag tools to reorder, right-click to remove. ⚙ › <i>Show Tools As</i> switches between
a list and a grid, ⚙ › <i>Columns</i> uses up to three columns, and <i>Bag Style</i> changes the design,
the bubble position and size.</p>
<p>The tools are the original QGIS tools, so they work exactly as usual (greyed out when they need e.g. a
layer in edit mode). Removing a tool from the bag never removes it from QGIS. Everything is saved
automatically.</p>
"""

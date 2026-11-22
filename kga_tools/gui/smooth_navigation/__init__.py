# -*- coding: utf-8 -*-
"""ArcGIS Pro-style map navigation: animated wheel zoom and kinetic panning.

Ported from the standalone Smooth Navigation plugin. It works through Qt event
filters on each map canvas, so switching it off (Settings > Appearance) removes
the filters and QGIS goes back to its stock navigation at once. The per-canvas
render properties it tunes are restored on uninstall.
"""

from contextlib import suppress

from qgis.PyQt import sip

from ...core import kga_settings as settings
from .layout_content import LayoutContentNavigator
from .navigator import SmoothNavigator

#: Tuning that is not worth a Settings control. Names match what the navigator reads.
_FIXED = {
    'pan_tool_drag': True,       # left-drag while the Pan tool is active
    'middle_pan': True,          # middle-drag in any map tool
    'flick_gain': 1.0,           # multiplier on release velocity
    'key_pan_speed': 900,        # px/s at full speed
    'settle_delay_ms': 40,       # idle time before the single re-render
    'preview_jobs': False,       # QGIS preview renders repeat the underlay's work and hog the CPU
    'underlay_factor': 3.0,      # underlay covers N x the view
    'boost_canvas': True,        # progressive display, render cache, parallel rendering
    'progressive_ms': 2000,      # partial renders replace the stretched map, so show them only when slow
}


class NavConfig:
    """The live options every canvas navigator reads; `load` follows Settings."""

    def __init__(self):
        self.load()

    def load(self):
        for key, value in _FIXED.items():
            setattr(self, key, value)
        self.smooth_zoom = settings.smooth_nav_zoom()
        self.zoom_tau_ms = settings.smooth_nav_zoom_ms()
        self.inertia = settings.smooth_nav_inertia()
        self.inertia_tau_ms = settings.smooth_nav_glide_ms()
        self.keyboard = settings.smooth_nav_keyboard()
        self.right_drag_zoom = settings.smooth_nav_right_zoom()
        # +1: dragging down zooms out; -1: dragging down zooms in
        self.right_drag_sign = (-1.0 if settings.smooth_nav_right_dir() == settings.RIGHT_UP_OUT
                                else 1.0)
        self.context_underlay = settings.smooth_nav_underlay()


class SmoothNavigation:
    """Switch the map canvases between stock and smooth navigation."""

    def __init__(self, iface):
        self.iface = iface
        self.cfg = NavConfig()
        self._navs = []
        self._layout_navs = []
        self._hooked = False

    @property
    def installed(self):
        return bool(self._navs or self._layout_navs)

    def _canvases(self):
        canvases = []
        with suppress(Exception):
            canvases = list(self.iface.mapCanvases())
        main = self.iface.mapCanvas()
        if main is not None and all(c is not main for c in canvases):
            canvases.insert(0, main)
        return canvases

    def _prune(self):
        self._navs = [n for n in self._navs
                      if not sip.isdeleted(n) and not sip.isdeleted(n.canvas)]

    def _install_layouts(self):
        """Smooth Move Item Content in every open layout designer."""
        self._layout_navs = [n for n in self._layout_navs
                             if not sip.isdeleted(n) and not sip.isdeleted(n.view)]
        known = [n.view for n in self._layout_navs]
        designers = []
        with suppress(Exception):
            designers = list(self.iface.openLayoutDesigners())
        for designer in designers:
            view = designer.view()
            if view is None or any(view is k for k in known):
                continue
            nav = LayoutContentNavigator(view, self.cfg)
            nav.install()
            self._layout_navs.append(nav)
        if not self._hooked:
            with suppress(Exception):
                self.iface.layoutDesignerOpened.connect(self._on_designer_opened)
                self._hooked = True

    def _on_designer_opened(self, *_):
        if self.installed:
            self._install_layouts()

    def install(self):
        self._install_layouts()
        self._prune()
        known = [n.canvas for n in self._navs]
        for canvas in self._canvases():
            if canvas is None or any(canvas is k for k in known):
                continue
            nav = SmoothNavigator(canvas, self.cfg)
            nav.install()
            self._navs.append(nav)

    def uninstall(self):
        if self._hooked:
            with suppress(Exception):
                self.iface.layoutDesignerOpened.disconnect(self._on_designer_opened)
            self._hooked = False
        for nav in self._layout_navs:
            if not sip.isdeleted(nav):
                with suppress(RuntimeError):
                    nav.uninstall()
                    nav.deleteLater()
        self._layout_navs = []
        self._prune()
        for nav in self._navs:
            with suppress(RuntimeError):
                nav.uninstall()
                nav.deleteLater()
        self._navs = []

    def refresh(self):
        """Follow the Settings window (also picks up map views opened since)."""
        if not settings.smooth_nav_enabled():
            self.uninstall()
            return
        self.cfg.load()
        self.install()
        for nav in self._navs:
            nav.apply_config()
